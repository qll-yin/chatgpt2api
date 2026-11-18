"""auto-loop 控制器：多 worker 并发，每个 worker 用独立代理。

设计：
  - 主控线程 manage_loop：监听 stop/pause、根据 concurrency 启停 worker
  - 多个 worker 线程：claim_next() → 注册 → 完成 → 继续
  - 代理池：每次注册**动态挑**一个可用代理（启用中 + 未超配额 + 用得少的优先），
    避免同 IP 堆太多号；某个代理用满配额或被停用后自动绕开
  - 状态机：stopped → running → paused → running / stopped
  - 优雅暂停/停止：当前 worker 跑完才退出，不强杀
  - 复用 registrar.start_registration：每个号开一个 run，由 worker 等其结束
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
import time
from typing import Optional

from services.storage import register_store as db
from services import register_registrar as registrar
from services.register.mail_providers import MailProviderError, get_provider_class

logger = logging.getLogger("auto_loop")


# ── worker 级重试：哪些失败值得「换个姿势再来一次」──────────────────
#
# ⚠️ 这跟 auth_flow 内部的 signup 重试是**两个层次**，别混：
#
#   · auth_flow 内部：signup 链撞 409 invalid_state 时重建会话 + 刷 device_id
#     再试（max_attempts=3），**复用同一个邮箱**，不额外花钱。
#     它解决的是「session 过期」这种可自愈的问题。
#
#   · worker 级（本表）：整个流程跑完了还是失败 —— 换代理 / 换号再跑一遍。
#     它解决的是「出口 IP 不通」「环境抽风」这类**换条件才有救**的问题。
#
# 一整个流程（含 auth_flow 内部的重建会话重试）只算**一次**尝试，
# 这才是旧注册机的语义：「按整个流程重试次数算」。
#
# ⚠️ invalid_state（409 "Please start over to continue"）必须在可重试集合里。
#    以前它被归到 account（不可重试）→ 流程级重试**根本没触发**，
#    worker 直接换下一个号 → 每换一个号就白烧一个付费邮箱，
#    而且重试计数被清零，worker 永远也不会因为它停下来。
#    实际上 409 几乎总是**出口 IP 被 OpenAI 记了** —— 换个 IP 重跑就能过。
RETRYABLE_CATEGORIES = frozenset(
    {"cf_blocked", "network", "unknown", "invalid_state"}
)

# 不可重试的分类及原因（写清楚是为了以后有人问「为什么不重试」）：
#   account     —— 号/邮箱本身废了（user_already_exists / 邮箱已注册 /
#                  已有账号 / refresh_token 失效）。重试同一个号毫无意义，
#                  换号继续才是对的。
#   mail_limited—— 邮箱侧固有限制（短效邮箱已消耗）。重试只会再撞一次。
NON_RETRYABLE_CATEGORIES = frozenset({"account", "mail_limited"})

# 「这个 run 不是失败，是被外部停掉了」的哨兵值。
#
# ⚠️ 之前 _wait_run_finish 在 stop_event 已置位时返回空 category，
#    空 category 既不在可重试集合也不在不可重试集合 → 静默换号；
#    而超时也返回空串 → 同样静默吞掉。两种情况都让"失败重试"形同虚设。
#    现在用哨兵值把「被停止」和「真失败」明确区分开。
STOPPED_CATEGORY = "__stopped__"

# 「代理池暂时拿不到可用代理」的分类。
# ⚠️ 它不是失败 —— 往往是并发 worker 暂时把配额占满了（失败会归还）。
#    单独归一类，这样它既不会计入失败数，也不会误触发熔断。
NO_PROXY_CATEGORY = "no_proxy"

# 「注册使用代理」的模式：注册时从代理池里挑哪一类代理用。
#
#   dynamic —— 只用动态代理（每次连接换出口 IP，注册风控最松）
#   static  —— 只用静态代理（固定出口 IP）
#   mixed   —— 混合随机（默认，池子里两类都能用）
#
# ⚠️ 这是**注册时**用哪条代理，跟「注册成功后入池绑哪个代理」是两件
#    独立的事（后者见 pool_proxy_mode）—— 两者不冲突：
#    注册用动态代理时，入池仍按「入池代理」的设置走（动态代理不入池）；
#    注册用静态代理时，入池会**沿用同一条静态代理**（出口保持一致）。
REGISTER_PROXY_MODE_DYNAMIC = "dynamic"
REGISTER_PROXY_MODE_STATIC = "static"
REGISTER_PROXY_MODE_MIXED = "mixed"
REGISTER_PROXY_MODES = frozenset({
    REGISTER_PROXY_MODE_DYNAMIC,
    REGISTER_PROXY_MODE_STATIC,
    REGISTER_PROXY_MODE_MIXED,
})


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    # 等资源（号池空 / 代理用尽）—— 不是「运行中」，也不是「已停止」。
    # ⚠️ 单独区分很重要：号池空的时候 worker 其实在空转，什么都没注册。
    #    显示「运行中」会让人以为还在跑（用户实测：worker 都停了，
    #    前端进度还在转）。显示「等待中」才如实。
    WAITING = "waiting"


def _parse_proxy_pool(text: str) -> list[str]:
    """把多行代理字符串拆成列表。空行 / # 开头注释跳过。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _proxy_type_label(proxy_type: str) -> str:
    """代理类型的中文说法（日志和前端提示里用）。"""
    return {
        "static": "静态",
        "dynamic": "动态",
    }.get(str(proxy_type or "").strip().lower(), "")


class AutoLoopController:
    """多 worker auto-loop 控制器。

    options 关键字段：
      proxy:                单代理（兼容旧版，concurrency=1 时用）
      proxy_pool:           多代理字符串（每行一个；多 worker 会按 worker index 轮流取）
      concurrency:          并发 worker 数（1-20）
      cool_down_seconds:    每个 worker 跑完后冷却时间（默认 3）
      其余参数透传给 registrar.start_registration
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停
        # 进度统计
        self._started_at: float = 0.0
        # 批次结束时间（非 0 = 已结束）。用它冻结「已用时」——
        # 否则跑完了还显示"用了 3 小时"。
        self._finished_at: float = 0.0
        self._registered_ok = 0
        self._registered_fail = 0
        # 一共跑了多少遍完整流程（含重试）。用于前端显示「共尝试 N 遍」，
        # 与「失败数（按号算）」分开。
        self._attempts = 0
        # 本次批量导入号池的 access_token —— 批量结束后统一刷额度用
        self._imported_tokens: list[str] = []
        # 当前每个 worker 在跑啥（worker_id → email）
        self._worker_status: dict[int, dict] = {}
        # 活着的 worker（含还没进 run、正在 claim 的）。
        # ⚠️ 不能只看 _worker_status —— 它只在 run 启动后才有值，
        #    worker 在 claim 阶段退出时它是空的，会误判成「都退出了」。
        # ⚠️ 用 (batch_id, worker_id) 而不是光 worker_id ——
        #    上一批的 worker 没退干净时，光按 worker_id 算会把
        #    新一批的 worker 也算进来（或反过来漏掉），导致误收敛。
        self._alive_workers: set[tuple[str, int]] = set()
        self._last_message = ""
        # 最后一次失败的分类（给前端显示「为什么停了」）
        self._last_fail_reason = ""
        # 熔断状态
        self._consecutive_network_fails = 0
        self._circuit_break_threshold = 3
        self._last_break_reason = ""
        # SSE 订阅
        self._subscribers: list[queue.Queue] = []
        # 代理池 / 并发数
        self._proxy_pool: list[str] = []
        self._backend_proxy_pool: bool = False
        self._concurrency: int = 1
        # 目标成功数：0 = 不限量（保持旧行为）；>0 时累计成功达标即自动停止
        self._target_count: int = 0
        # worker 级重试：整个流程失败后，同一个 worker 再跑几遍。
        # 0 = 不重试（失败一次就换号继续）；默认 3（首次 + 3 次重试）。
        self._max_retries: int = 3
        # 代理池暂时拿不到可用代理时，等多久（秒）。
        # ⚠️ 并发 worker 数 > 代理数时，后面的 worker 会暂时拿不到 ——
        #    失败的 worker 会把配额还回来，等一等就能拿到。
        self._proxy_wait_secs: int = 120
        # 「注册使用代理」：注册时只挑某一类代理用
        # （dynamic / static / mixed，mixed = 不限类型）。
        self._register_proxy_mode: str = REGISTER_PROXY_MODE_MIXED

    # ──────────────────────── 公共 API ────────────────────────

    def start(self, options: dict) -> dict:
        with self._lock:
            # ⚠️ WAITING 也要算「已经在跑」——
            #    worker 还在等号池/代理，只是暂时没在注册。
            #    不拦住的话，用户点两次「开始」就会起两批 worker，
            #    抢同一个邮箱池、互相踩代理配额。
            if self._state in (
                AutoLoopState.RUNNING,
                AutoLoopState.PAUSED,
                AutoLoopState.WAITING,
            ):
                return {"ok": False, "error": f"已经在跑了 (state={self._state})"}
            # ⚠️ 状态可能还停在 stopped，但**上一批的 worker 还活着**
            #    （比如它正在等号池、状态还没来得及切）。
            #    只看 state 会漏判 → 起两批 worker 抢同一个号池。
            #    所以再查一次「有没有活着的 worker」。
            if self._alive_workers:
                return {
                    "ok": False,
                    "error": f"上一批 worker 还没退出（{len(self._alive_workers)} 个），稍后再试",
                }
            # 重置
            self._stop_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            # 批次号 —— 一次批量 = 一个批次，下面的每个 run 都带上它，
            # 运行记录就能按「这一轮」归组看日志和邮箱。
            self._options.setdefault("batch_id", uuid.uuid4().hex[:12])
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._finished_at = 0.0
            self._imported_tokens = []
            self._registered_ok = 0
            self._registered_fail = 0
            self._attempts = 0
            self._last_fail_reason = ""
            self._worker_status.clear()
            # ⚠️ 不清 _alive_workers —— 旧批次的 worker 可能还活着，
            #    它们带着自己的 batch_id，判定时会自动隔离。
            self._consecutive_network_fails = 0
            self._last_message = "auto-loop 启动"
            # 解析并发参数
            self._concurrency = max(1, min(20, int(self._options.get("concurrency") or 1)))
            # 代理池：优先用后端 proxy_pool 表（有启用状态 / 上限 / 用量），
            # 表为空时回落到前端传来的 proxy_pool 文本（每行一个）。
            pool_text = self._options.get("proxy_pool") or ""
            self._proxy_pool = _parse_proxy_pool(pool_text)
            self._backend_proxy_pool = bool(db.list_proxies())
            if self._backend_proxy_pool:
                # 后端池是权威来源 —— 前端传来的文本只作兜底
                self._proxy_pool = []
            # 目标成功数（0=不限量）
            self._target_count = max(0, int(self._options.get("target_count") or 0))
            # worker 级重试次数（不含首次尝试）。0 = 不重试。
            # ⚠️ 不能用 `or 3` —— 那会把「0 = 不重试」当成「没传」给覆盖掉。
            _raw_retries = self._options.get("max_retries")
            self._max_retries = (
                3
                if _raw_retries is None or _raw_retries == ""
                else max(0, min(20, int(_raw_retries)))
            )
            # 代理池等待时长（秒）
            _raw_wait = self._options.get("proxy_wait_secs")
            self._proxy_wait_secs = (
                120 if _raw_wait is None or _raw_wait == "" else max(0, min(1800, int(_raw_wait)))
            )
            # ── 注册使用代理：只挑某一类代理 ──
            # dynamic（只动态）/ static（只静态）/ mixed（混合随机，默认）
            _raw_mode = str(self._options.get("register_proxy_mode") or "").strip().lower()
            self._register_proxy_mode = (
                _raw_mode if _raw_mode in REGISTER_PROXY_MODES else REGISTER_PROXY_MODE_MIXED
            )
            self._options["register_proxy_mode"] = self._register_proxy_mode
            # 启动前自愈：上次进程崩溃 / 被 kill 时，claim 了的号会卡在 in_use，
            # 没有任何入口能恢复（release_stale 以前从没被调用过）。
            # 只释放 10 分钟以上没动静的，避免误伤正在跑的号。
            try:
                stale = db.release_stale(600)
                if stale:
                    logger.warning(
                        "[auto-loop] 启动自愈：把 %s 个卡在 in_use 的邮箱放回 available", stale
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("[auto-loop] 启动自愈失败（不影响启动）: %s", e)
            # 「注册使用代理」选了具体类型时，池里必须有该类可用代理 ——
            # 否则 worker 要等满 120 秒才报「没有可用的动态代理」，像卡死了。
            if self._register_proxy_mode in (
                REGISTER_PROXY_MODE_DYNAMIC,
                REGISTER_PROXY_MODE_STATIC,
            ):
                usable = 0
                try:
                    usable = db.usable_proxy_count(self._register_proxy_mode)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[auto-loop] 查询可用代理失败: %s", e)
                if usable <= 0:
                    label = (
                        "动态代理"
                        if self._register_proxy_mode == REGISTER_PROXY_MODE_DYNAMIC
                        else "静态代理"
                    )
                    self._state = AutoLoopState.STOPPED
                    self._last_message = f"代理池里没有可用的{label}，无法启动"
                    logger.warning(self._last_message)
                    return {
                        "ok": False,
                        "error": (
                            f"代理池里没有可用的{label}（启用中且未超注册上限）。"
                            f"请先到「代理池」导入并选对类型，或把「注册使用代理」"
                            f"改成「混合随机」。"
                        ),
                    }
            # 启动时把关键参数打出来 —— 用户反馈「设了重试 3 次却没重试」时，
            # 一眼就能确认服务端到底收到了多少（而不是靠猜前端传没传）。
            logger.info(
                "[auto-loop] 启动：并发 %s，目标成功数 %s，流程级重试 %s 次"
                "（= 一个号最多 %s 个「开始注册」），注册代理 %s",
                self._concurrency,
                self._target_count or "不限",
                self._max_retries,
                self._max_retries + 1,
                self._register_proxy_mode,
            )
            # 启 manage 线程
            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()
        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
            "target_count": self._target_count,
        }

    def pause(self) -> dict:
        with self._lock:
            # 「等待资源」也允许暂停 —— worker 正在等代理/号池，
            # 用户想先停下来补资源是合理操作（以前直接拒绝）。
            if self._state not in (AutoLoopState.RUNNING, AutoLoopState.WAITING):
                return {"ok": False, "error": f"当前 state={self._state}，不可暂停"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_message = "已请求暂停（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"当前 state={self._state}，不可恢复"}
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._finished_at = 0.0
            self._last_message = "已恢复"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "没在跑"}
            self._stop_event.set()
            self._pause_event.clear()
            # ⚠️ 「等待中」时**立刻**置为 stopped。
            #    等资源的时候没有任何注册在跑，再等 worker 退出是白等 ——
            #    用户点了停止却看着状态卡在「等待中」，会以为没点生效。
            if self._state == AutoLoopState.WAITING:
                self._state = AutoLoopState.STOPPED
                if not self._finished_at:
                    self._finished_at = time.time()
                self._last_message = "已停止"
            else:
                self._last_message = "已请求停止（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True}

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        try:
            q.put_nowait({"kind": "state", "data": self._snapshot()})
        except queue.Full:
            pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try: self._subscribers.remove(q)
            except ValueError: pass

    # ──────────────────────── 内部 ────────────────────────

    def _snapshot(self) -> dict:
        with self._lock:
            stats = db.stats()
            # 已用时：批次结束后**冻结**，不能一直累加 ——
            # 跑完了还显示"用了 3 小时"是错的。
            end_at = self._finished_at or time.time()
            elapsed = (end_at - self._started_at) if self._started_at else 0
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": info.get("proxy", ""),
                    "started_at": info.get("started_at", 0),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            return {
                "state": self._state,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "elapsed": elapsed,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                # 尝试遍数（含重试）—— 与「失败数（按号）」分开显示
                "attempts": self._attempts,
                "target_count": self._target_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok)
                    if self._target_count else None
                ),
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "workers": workers_info,
                "last_message": self._last_message,
                # 最后一次失败的分类 —— 前端要能看到「为什么停了」，
                # 否则只看到「成功 1 / 失败 0」完全无从判断。
                "last_fail_reason": self._last_fail_reason,
                "pool_stats": stats,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _emit_log(self, text: str, level: str = "info") -> None:
        """把**批次级**决策写进前端实时日志。

        ⚠️ 为什么需要
        ------------
        registrar 的日志走 run queue 转发到前端，但 auto-loop 自己的决策
        （「第 N 遍流程失败 → 开始第 x/y 次重试」「代理池没代理了，停止 worker」
        「重试次数用尽，停止 worker」）只在服务端 logger 里，**前端一行都看不到**。
        用户看到的现象就是：注册失败 → 然后就结束了，完全不知道为什么。

        这里把这些决策同时写进日志文件和前端面板（kind="batch_log"）。
        """
        msg = str(text or "")
        if not msg:
            return
        log = logger.error if level == "error" else (
            logger.warning if level == "warning" else logger.info
        )
        try:
            log(f"[auto-loop] {msg}")
        except Exception:  # noqa: BLE001
            pass
        self._broadcast("batch_log", {"message": msg, "level": level})

    def _set_state(self, state: str, batch_id: str = "") -> None:
        """切换状态（只在真的变了才广播，避免刷屏）。

        ⚠️ 切到 RUNNING 时必须清掉 _finished_at ——
           否则会出现「state=running 但 finished_at 有值」的矛盾状态，
           前端显示「运行中」而「已用时」却被冻结住。

        ⚠️ batch_id 守卫：worker 只能改**自己那一批**的状态。
           上一批的 worker 若还没退干净，它领到号时调 _set_state(RUNNING)
           会把新一轮已经结束的状态改回 running —— 前端就永远显示「运行中」。
        """
        with self._lock:
            if batch_id and str(self._options.get("batch_id") or "") != batch_id:
                return  # 不是当前批次，忽略
            if self._state == state:
                return
            self._state = state
            if state == AutoLoopState.RUNNING:
                self._finished_at = 0.0
        self._broadcast("state", self._snapshot())

    def _set_message(self, msg: str):
        with self._lock:
            self._last_message = msg
        self._broadcast("state", self._snapshot())

    def _proxy_for_worker(
        self, worker_id: int, exclude: set[str] | None = None
    ) -> str:
        """给这个 worker 取一个代理。

        ⚠️ 这里从"按 worker_id 取模"改成了"每次动态挑"，因为取模是**静态绑定**：
        worker-0 永远拿第 1 个代理、worker-1 永远拿第 2 个。某个代理被 OpenAI
        风控后，所有 worker 会一直死磕那个 IP，永不自动换走 —— 这正是
        "同一 IP 账号太多 → 全挂"的根因。

        现在改成每次注册都从后端代理池里挑：
          - 启用中（enabled=1）
          - 未超配额（used_count < max_uses）
          - 优先挑用得少的（天然均衡负载）
        某个代理用满配额或被手动停用后，其余 worker 自动绕开它。

        exclude：本次要避开的代理（刚失败/刚被 CF 拦的那条固定出口代理）。
        流程级重试必须换出口 IP —— 同一个固定 IP 再跑一遍必然还是同样的结果。

        ⚠️ 只挑「注册使用代理」指定的那一类（dynamic / static / mixed）。
           选了动态就只走动态，选了静态就只走静态 —— 类型不匹配时**不**
           偷偷换类型（那会让用户以为在用动态代理，实际用的是静态）。
           池里没有该类型的代理时返回空串，由调用方明确提示。

        后端池为空时回落到旧的文本代理池（按 worker_id 取模，保持老行为）。
        """
        if self._backend_proxy_pool:
            try:
                picked = db.pick_proxy(
                    exclude=exclude,
                    proxy_type=self._register_proxy_mode_filter(),
                )
            except Exception as e:  # noqa: BLE001
                # ⚠️ 取代理时数据库抽风不能让 worker 当场死掉 ——
                #    那样批次会静默结束、重试一次都不跑。当成「暂时没代理」，
                #    交给调用方等待 / 复用上一条。
                logger.warning(f"[worker-{worker_id}] 挑代理失败（按暂时没代理处理）: {e}")
                picked = ""
            if picked:
                return picked
            # 池里没有可用代理了 —— 返回空串，让调用方决定是等待还是停止。
            # 这里**不**静默回落成 options.proxy：那样会绕过配额限制，
            # 所有 worker 挤到同一个全局代理上，正好是限流要防的事。
            return ""
        if self._proxy_pool:
            return self._proxy_pool[worker_id % len(self._proxy_pool)]
        return self._options.get("proxy", "") or ""

    def _wait_for_proxy(
        self,
        worker_id: int,
        exclude: set[str] | None = None,
        timeout: int | None = None,
    ) -> str:
        """代理池暂时拿不到可用代理时，等一会儿再试。

        ⚠️ 为什么不能直接放弃
        --------------------
        pick_proxy() 是「选中即预占」（used_count +1）。并发 worker 数超过
        代理数时，后启动的 worker 会暂时拿不到代理 —— 而**失败的 worker 会把
        配额还回来**（proxy_release），或者成功的 worker 用完就腾出来了。

        之前这里直接 return：整个批次被停掉，而且失败数不记账，用户看到
        「成功 1 / 失败 0，然后就停了」。现在改成轮询等待。

        等待期间状态切到「等待资源」，前端能看出它在等什么。

        timeout：本次最多等多少秒（None = 用批次设置）。重试时有兜底代理
        可复用的话会传一个小值 —— 不必为了等一条新代理把重试卡两分钟。
        """
        budget = self._proxy_wait_secs if timeout is None else timeout
        deadline = time.time() + max(0, int(budget))
        announced = False
        paused_at: float | None = None
        while time.time() < deadline:
            if self._stop_event.is_set():
                return ""
            if self._pause_event.is_set():
                # ⚠️ 暂停时**原地等恢复**，不能直接 return ""。
                #    以前这里返回空串 → 第 1 遍流程会走「没有可用代理」分支
                #    收尾退出 → 暂停一下就把 worker 弄死了，resume 也救不回来。
                #    暂停的这段时间不算进等待预算。
                paused_at = paused_at or time.time()
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                deadline += time.time() - paused_at
                paused_at = None
                continue
            picked = self._proxy_for_worker(worker_id, exclude=exclude)
            if picked:
                # 拿到代理 → 如果刚才因为等待被标成「等待资源」，切回运行中。
                # ⚠️ 只在 WAITING 时切 —— 别把用户刚点的 PAUSED 覆盖掉。
                if self._state == AutoLoopState.WAITING:
                    self._set_state(
                        AutoLoopState.RUNNING, str(self._options.get("batch_id") or "")
                    )
                return picked
            if not announced:
                announced = True
                self._set_message(
                    f"worker-{worker_id} 暂时拿不到代理（池内被其他 worker 占用），等待中…"
                )
                # ⚠️ 只有「一个注册都没在跑」时才把整批标成等待资源 ——
                #    否则别的 worker 正在注册，前端却显示「等待资源」。
                with self._lock:
                    busy = bool(self._worker_status)
                if not busy:
                    self._set_state(
                        AutoLoopState.WAITING, str(self._options.get("batch_id") or "")
                    )
                    self._broadcast("state", self._snapshot())
            time.sleep(2)
        return ""

    def _record_finish(
        self,
        ok: bool,
        category: str,
        count_fail: bool = True,
        count_breaker: bool = True,
    ):
        """worker 结束一个 run 后调，更新计数 + 熔断。

        ⚠️ count_fail / count_breaker 是为「流程级重试」拆开的：
           · count_fail    —— 每一遍完整流程都算一次失败
             （用户能看到"试了 N 遍"，而不是只看到 1 次）
           · count_breaker —— 熔断只在**整条重试链**最终失败时计一次。
             否则同一遍流程里的 3 次重试会把熔断计数凑满，
             批次在重试还没跑完就被暂停掉。
        """
        with self._lock:
            if ok:
                self._registered_ok += 1
                self._consecutive_network_fails = 0
                self._last_fail_reason = ""
            else:
                if count_fail:
                    self._registered_fail += 1
                # ⚠️ 记录最后一次失败原因 —— 前端状态栏要能看到「为什么停了」，
                #    否则用户只看到「成功 1 / 失败 0 然后停止」，完全无从判断。
                self._last_fail_reason = str(category or "unknown")
                # ⚠️ 只有真正的环境/网络故障才计入熔断。
                #    cf_blocked 是「这个出口 IP 被 CF 拒了」—— 换一个代理就能继续，
                #    不该把整个批次停掉（实测日志里 7 次 CF 403 触发了熔断，
                #    白白暂停了自动注册）。
                if count_breaker:
                    if category == "network":
                        self._consecutive_network_fails += 1
                    else:
                        self._consecutive_network_fails = 0
            self._last_message = (
                f"累计 ok={self._registered_ok} fail={self._registered_fail}"
                + (f"（最后失败原因：{self._last_fail_reason}）" if self._last_fail_reason else "")
            )
            # 目标数量：累计成功达标 → 触发停止（stop_event 幂等，多 worker 同时命中也安全）
            target_reached = bool(
                self._target_count and self._registered_ok >= self._target_count
            )
            trigger_break = (
                self._consecutive_network_fails >= self._circuit_break_threshold
                and self._state == AutoLoopState.RUNNING
            )

        if target_reached:
            with self._lock:
                self._stop_event.set()
                self._last_message = (
                    f"🎯 已达目标 {self._target_count} 个，自动停止"
                    f"（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            logger.info(f"已达目标 {self._target_count} 个成功，触发自动停止")
            self._broadcast("state", self._snapshot())
            return

        if trigger_break:
            with self._lock:
                self._pause_event.set()
                self._state = AutoLoopState.PAUSED
                self._last_break_reason = (
                    f"连续 {self._consecutive_network_fails} 次网络/环境错误，"
                    f"自动暂停（号已自动 release，请检查代理后点恢复）"
                )
                self._last_message = self._last_break_reason
                self._consecutive_network_fails = 0
            logger.warning(self._last_break_reason)
            self._broadcast("circuit_break", {"reason": self._last_break_reason})

    def _manage_loop(self):
        """主控线程：启动 worker，等所有 worker 结束，更新最终状态。"""
        # 本批次的标识 —— 用来识别「我是上一批残留的主控」。
        # ⚠️ 没有它的话，用户「停止」后马上「开始」，
        #    上一批的 manage_loop 收尾时会把**新一批**的状态
        #    覆盖成 stopped —— 前端立刻显示「已停止」，
        #    但新批次其实正在跑。
        my_batch = str(self._options.get("batch_id") or "")
        try:
            workers = []
            for wid in range(self._concurrency):
                t = threading.Thread(
                    target=self._worker_loop, args=(wid,),
                    daemon=True, name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                # 每个 worker 之间错开 1s 启动，避免同时打 OpenAI
                time.sleep(1.0)
            self._workers = workers
            # 等所有 worker 退出
            for t in workers:
                t.join()
        except Exception as e:
            logger.exception(f"manage_loop 异常: {e}")
        finally:
            # ⚠️ 批次守卫 —— 上一批的收尾不能覆盖新一批的状态。
            with self._lock:
                stale = str(self._options.get("batch_id") or "") != my_batch
                if not stale:
                    self._state = AutoLoopState.STOPPED
                    if not self._finished_at:
                        self._finished_at = time.time()
                    self._worker_status.clear()
                    self._last_message = (
                        f"已停止（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                    )
            if stale:
                logger.info("[auto-loop] 上一批 manage_loop 收尾（不影响当前批次）")
                return
            self._broadcast("state", self._snapshot())
            # 批量期间为了不拖慢 worker，入池时没刷额度。
            # 这里统一刷一次 —— 后台线程跑，不阻塞管理线程。
            if self._options.get("auto_import", True):
                self._refresh_pool_quota_async()

    # ─────────────────── 流程级重试的三个辅助 ───────────────────

    def _register_proxy_mode_filter(self) -> str:
        """把「注册使用代理」模式翻译成 pick_proxy 的类型过滤值。

        mixed → ""（不限类型）；dynamic / static → 原样传下去。
        """
        mode = str(getattr(self, "_register_proxy_mode", "") or "").strip().lower()
        if mode == REGISTER_PROXY_MODE_MIXED:
            return ""
        if mode in (REGISTER_PROXY_MODE_DYNAMIC, REGISTER_PROXY_MODE_STATIC):
            return mode
        return ""

    def _proxy_type_of(self, proxy: str) -> str:
        """查这条代理的类型（static / dynamic）。查不到返回空串。"""
        p = str(proxy or "").strip()
        if not p:
            return ""
        try:
            row = db.find_proxy(p) or {}
        except Exception:  # noqa: BLE001
            return ""
        return str(row.get("proxy_type") or "").strip().lower()

    def _settle_failed_proxy(
        self, worker_id: int, proxy: str, category: str, reserved: bool = True
    ) -> None:
        """失败时结算这条代理 —— **记账失败绝不能中断重试**。

        ⚠️ 这里必须自己吞异常：这只是记账（还配额 / 记失败次数 / 按分类
        处置代理）。一旦抛出去，worker 线程当场死亡 → 批次瞬间变 stopped →
        用户看到的就是「失败一次就结束 worker，重试一次都没跑」。
        记账出错最多数据不准，绝不能因此把整条重试链掐掉。
        """
        try:
            self._settle_failed_proxy_inner(worker_id, proxy, category, reserved)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[worker-{worker_id}] 结算代理失败（忽略，继续重试）: {e}")

    def _settle_failed_proxy_inner(
        self, worker_id: int, proxy: str, category: str, reserved: bool = True
    ) -> None:
        """失败时结算这条代理：还配额 + 记失败次数 + 按分类做代理级处置。

        ⚠️ 用的是「**实际用的那条**」—— 流程内可能换过代理
        （auth_flow 换出口 IP），原代理那条的预占已经被转移，
        按旧的结算就会算错对象。

        reserved=False 表示这条代理不是 pick_proxy 预占来的
        （重试时没别的代理可换、临时复用了上一条），
        这时**不能**调 proxy_release —— 会把别的 worker 预占的配额还掉。
        """
        if not proxy or not self._backend_proxy_pool:
            return
        # 失败不占配额：把 pick_proxy 的预占还回去，只记失败次数。
        if reserved:
            db.proxy_release(proxy)
        db.proxy_used(proxy, ok=False)
        # ⚠️ CF 拦截的处置与普通失败**完全不同**：
        #    · 静态代理（固定出口 IP）→ 这个 IP 已被 CF 拉黑，
        #      连续被拦达阈值就自动停用，别再拿它试
        #    · 动态代理（每次换 IP）→ 不停用，只是这个出口不行，
        #      换一次 IP 可能就通过了
        # 具体动作交给 db.mark_proxy_cf_blocked 按类型决定。
        #
        # ⚠️ 但「停用」有个硬前提：停掉之后池里还得有别的代理。
        #    池里只剩这一条时它会被保留（kept_enabled）—— 否则流程级重试
        #    拿不到代理，worker 会提前收工，重试次数形同虚设。
        if category == "cf_blocked":
            cfres = db.mark_proxy_cf_blocked(proxy)
            # ⚠️ 键名是 "disabled"，不是 "auto_disabled" —— 以前写错了，
            #    所以「静态代理已自动停用」这条提示**从来没触发过**。
            if cfres.get("disabled"):
                logger.warning(
                    f"[worker-{worker_id}] 静态代理被 CF 连续拦截 "
                    f"{cfres.get('cf_blocked')} 次 → 已自动停用: {proxy[:60]}"
                )
                self._broadcast("proxy_disabled", {
                    "proxy": proxy,
                    "reason": f"被 Cloudflare 连续拦截 {cfres.get('cf_blocked')} 次",
                    "count": cfres.get("cf_blocked"),
                })
            elif cfres.get("kept_enabled"):
                logger.warning(
                    f"[worker-{worker_id}] 代理被 CF 拦截 {cfres.get('cf_blocked')} 次，"
                    f"但它是池里唯一可用的{_proxy_type_label(cfres.get('proxy_type'))}代理 —— "
                    f"**不停用**（停掉就没有代理可换，重试会直接中断），"
                    f"继续降权使用: {proxy[:60]}"
                )
            else:
                logger.info(
                    f"[worker-{worker_id}] 代理被 CF 拦截"
                    f"（{cfres.get('proxy_type')} 类型，累计 "
                    f"{cfres.get('cf_blocked')} 次）→ 换下一个代理: {proxy[:60]}"
                )
        elif category == "invalid_state":
            # ── 409 invalid_state 的代理级处置 ──
            # ⚠️ 根因往往就是这个出口 IP 被 OpenAI 记了。
            #    不处置的话，被记死的静态代理会被反复挑中，
            #    每次都撞 409，每次都白烧一个付费邮箱。
            isres = db.mark_proxy_invalid_state(proxy)
            if isres.get("disabled"):
                logger.warning(
                    f"[worker-{worker_id}] 静态代理连续 {isres.get('invalid_state')} 次"
                    f"会话失效(409 invalid_state) → 已自动停用: {proxy[:60]}"
                )
                self._broadcast("proxy_disabled", {
                    "proxy": proxy,
                    "reason": "连续会话失效(409 invalid_state)",
                    "count": isres.get("invalid_state"),
                })
            elif isres.get("kept_enabled"):
                logger.warning(
                    f"[worker-{worker_id}] 代理连续 {isres.get('invalid_state')} 次"
                    f"会话失效(409)，但它是池里唯一可用的{_proxy_type_label(isres.get('proxy_type'))}代理 —— "
                    f"**不停用**（停掉就没有代理可换，重试会直接中断），"
                    f"继续降权使用: {proxy[:60]}"
                )
            else:
                logger.info(
                    f"[worker-{worker_id}] 代理会话失效(409 invalid_state)"
                    f"（{isres.get('proxy_type')} 类型，累计 "
                    f"{isres.get('invalid_state')} 次）→ 换下一个代理: {proxy[:60]}"
                )

    def _release_account(self, account: dict, pooled: bool) -> None:
        """把号放回 available（用户停止 / 还没真正注册就被打断时）。

        ⚠️ 与 mark_failed 的区别：这不是号的问题，下次还能用。
        """
        if not pooled:
            return
        email = str((account or {}).get("email") or "").strip()
        if not email:
            return
        try:
            db.release_unused(email)
            logger.info(f"[register] {email} 已放回号池（任务被停止，号未消耗）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"账号 {email} 放回号池失败: {e}")

    def _finalize_account(self, account: dict, category: str, pooled: bool) -> None:
        """这个号重试到底之后的最终处置（号池型来源才有号池记录）。

        ⚠️ 重试期间号一直挂在 in_use（hold_account=True，registrar 不碰它），
        所以链条结束时必须由这里收尾，否则号会永远卡在 in_use。

        · network / cf_blocked / invalid_state → 号无辜，放回 available 复用
        · 其它（account / mail_limited / unknown）→ 这个号废了，标 failed
        """
        if not pooled:
            return
        email = str((account or {}).get("email") or "").strip()
        if not email:
            return
        try:
            if category in ("network", "cf_blocked", "invalid_state"):
                db.release_unused(email)
                logger.info(
                    f"[register] {email} 判定为出口/环境问题（{category}），"
                    f"号无辜，已放回号池复用"
                )
            else:
                db.mark_failed(email, f"[{category}] 流程重试用尽")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"账号 {email} 收尾失败: {e}")

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        # 本轮批次的标识 —— 用来识别「我是上一批残留的 worker」。
        # ⚠️ 没有它的话，上一批 worker 退出时会把**新一批**的状态误杀成 stopped
        #    （start() 刚设好 running，旧 worker 的 finally 一跑就把它覆盖了）。
        my_batch = str(self._options.get("batch_id") or "")
        try:
            self._worker_loop_inner(worker_id, my_batch)
        except Exception as e:  # noqa: BLE001
            # ⚠️ 这条 except 是「止血」用的，不能删。
            #    以前 worker 线程里任何未捕获异常都会**静默杀死**这个 worker：
            #    线程直接退出 → manage_loop join 到 → 批次瞬间变 stopped，
            #    前端实时日志里一行提示都没有（用户看到的是「注册失败后
            #    莫名其妙就结束了，重试一次都没跑」）。
            #    现在至少：① 日志打出完整 traceback；② 广播到前端；
            #    ③ 状态栏说清楚是 worker 崩了而不是正常结束。
            logger.exception(f"[worker-{worker_id}] 线程异常退出")
            try:
                self._emit_log(
                    f"worker-{worker_id} 线程异常退出：{type(e).__name__}: {e}",
                    level="error",
                )
                self._set_message(
                    f"worker-{worker_id} 线程异常退出（{type(e).__name__}: {e}），"
                    f"已停止该 worker —— 请把服务端日志发给开发者"
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            with self._lock:
                # ⚠️ 按 worker_id 清掉**所有**条目，不只 (my_batch, worker_id)。
                #    因为 my_batch 是在函数入口读的 self._options["batch_id"]，
                #    若中途 start() 换了批次，这个 key 就跟登记时的不一致，
                #    discard((my_batch, worker_id)) 会删不掉 → worker 永远「活着」
                #    → 之后再也 start 不了。
                self._alive_workers = {
                    item for item in self._alive_workers if item[1] != worker_id
                }
                stale = str(self._options.get("batch_id") or "") != my_batch
            if stale:
                logger.info("[auto-loop] 旧批次 worker 退出（不影响当前批次状态）")
                return
            self._settle_if_all_workers_done(worker_id, my_batch)

    def _worker_loop_inner(self, worker_id: int, my_batch: str = ""):
        idle_round = 0
        # ── worker 级重试计数 ──
        # 连续「可重试」的失败次数。成功一次就清零。
        # ⚠️ 这是 **per-worker** 的局部计数，不是全局 —— 一个 worker 挂了
        #    不该把别的 worker 也拖下水。
        consecutive_retryable_fails = 0
        logger.info(f"[worker-{worker_id}] 启动")
        with self._lock:
            self._alive_workers.add((my_batch, worker_id))

        while True:
            # 检查停止
            if self._stop_event.is_set():
                logger.info(f"[worker-{worker_id}] 已停止")
                return

            # 检查暂停
            if self._pause_event.is_set():
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    return

            # 目标数量闸门：已成功 + 在跑的（复用 _worker_status 当在跑数）≥ 目标 → 本 worker 退出
            # 不新增易泄漏的计数器；_worker_status 已在锁内正常维护，最大限度压低超额
            with self._lock:
                if self._target_count and (
                    self._registered_ok + len(self._worker_status) >= self._target_count
                ):
                    logger.info(
                        f"[worker-{worker_id}] 目标 {self._target_count} 已锁定，退出"
                    )
                    return

            # ── claim 下一个号。要不要走号池由 provider 的 pooled 决定，
            # 非池化的（CF 这类自己造地址的）用虚拟占位。
            #
            # ⚠️ 优先用**本次批量指定的** mail_source（用户在批量注册页选的），
            #    没有才回退到全局配置 —— 否则用户在页面上选了 Remail，
            #    这里还是按配置的 outlook 去 claim，结果一个号都领不到。
            mail_source = str(
                self._options.get("mail_source") or db.get_setting("mail_source", "outlook")
            ).strip() or "outlook"
            try:
                pooled = get_provider_class(mail_source).pooled
            except MailProviderError as e:
                logger.error(f"[worker-{worker_id}] {e}，停止")
                self._set_message(str(e))
                return
            if pooled:
                account = db.claim_next(kind=mail_source)
            else:
                account = {
                    "email": f"{mail_source}_placeholder_"
                             f"{int(time.time())}_{worker_id}@placeholder.local",
                    "password": "", "client_id": "", "refresh_token": "",
                    "relay_url": "", "kind": mail_source,
                }
            if not account:
                idle_round += 1
                if idle_round == 1:
                    self._set_message(
                        f"worker-{worker_id} 号池空，等待新号..."
                    )
                    # 状态切到「等待中」—— 此刻没有任何注册在跑，
                    # 显示「运行中」是误导。
                    self._set_state(
                        AutoLoopState.WAITING,
                        str(self._options.get("batch_id") or ""),
                    )
                # ⚠️ 号池空时**一直等**，不放弃。
                #    原来空 10 轮（30s）就退出 —— 用户导入邮箱慢一点、
                #    或者自动补充正好在等下一批号，都会白等一场。
                #    现在改成无限等待，状态显示「等待资源」，
                #    用户补上号后 worker 自己就继续跑。
                #
                #    停止靠 stop_event（用户点停止 / 目标达成 / 代理用尽），
                #    不靠「等不到就放弃」这种隐式超时。
                for _ in range(30):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)
                continue
            idle_round = 0
            # 领到号了 → 回到「运行中」
            self._set_state(
                AutoLoopState.RUNNING, str(self._options.get("batch_id") or "")
            )

            # ══════════════════════════════════════════════════════════
            # 流程级重试：同一个号，最多跑 1 + max_retries 遍**完整流程**
            # ══════════════════════════════════════════════════════════
            #
            # ⚠️ 语义（用户明确要求）：
            #    「一遍流程」= 一次完整注册 = 一个「开始注册」事件。
            #    流程**内部** auth_flow 自己的重建会话重试（warmup 4 次 /
            #    409 invalid_state 3 次）**不算**流程级重试 —— 它们都发生在
            #    同一次「开始注册」里面，属于同一遍流程。
            #    流程级重试 = 换一个出口 IP，**从头再走一遍完整流程**
            #    （于是你会看到第 2、第 3、第 4 个「开始注册」）。
            #
            # ⚠️ 为什么必须换出口 IP：同一个 IP 再走一遍 = 同样的 409 /
            #    CF 403，重试只是白转圈。所以每遍失败就把这条（静态）代理
            #    记下来，下一遍避开它 —— 见 pick_proxy(exclude=...)。
            #
            # ⚠️ 为什么复用同一个号：这些失败几乎都是**出口 IP 的锅**
            #    （cf_blocked / invalid_state / network），不是号的锅。
            #    换号 = 白烧一个付费邮箱（Remail 一封 10 块）。
            attempt = 0
            failed_proxies: set[str] = set()
            last_proxy = ""          # 上一遍实际用的代理（重试兜底用）
            chain_ok = False
            chain_category = ""
            recorded = False
            run_id = ""
            while True:
                # 暂停时不开始新的尝试（等恢复）；停止则把号放回号池再退出。
                # ⚠️ 以前只在「领号」之前检查暂停 —— 一个号正在重试链中间时，
                #    用户点暂停，下一遍流程还是会照跑。
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    self._release_account(account, pooled)
                    with self._lock:
                        self._worker_status.pop(worker_id, None)
                    self._set_message("已停止")
                    self._broadcast("state", self._snapshot())
                    return

                attempt += 1
                with self._lock:
                    self._attempts += 1

                # ── 取代理（避开本轮已失败的那条）──
                # reserved：这条代理是不是 pick_proxy **预占**来的。
                # 预占的才需要在失败时 proxy_release 还回去。
                reserved = False
                proxy = ""
                if self._backend_proxy_pool:
                    proxy = self._proxy_for_worker(worker_id, exclude=failed_proxies)
                    reserved = bool(proxy)
                    if not proxy:
                        # 重试时有兜底代理（last_proxy）可复用 → 只需短暂等一小会儿
                        # （万一别的 worker 正好把代理配额还回来了）；否则等满设置值。
                        grace = 5 if (attempt > 1 and last_proxy) else None
                        proxy = self._wait_for_proxy(
                            worker_id, exclude=failed_proxies, timeout=grace
                        )
                        reserved = bool(proxy)
                    if not proxy and attempt > 1 and last_proxy:
                        # ⚠️ 重试期间「没有别的代理可用」**不能**直接收工。
                        #    「失败重试次数」是用户定的，重试没跑完就停 worker，
                        #    用户看到的正是：「失败一次就结束 worker，根本没重试」。
                        #    常见成因：上一条代理被自动停用 / 配额用满 / 被手动停用。
                        #    这里退一步：暂时复用上一条，把这一遍流程跑完
                        #    （预算有上限，不会无限空转），并明确告诉用户。
                        proxy = last_proxy
                        logger.warning(
                            f"[worker-{worker_id}] 代理池里没有别的可用代理了，"
                            f"第 {attempt} 遍流程暂时复用上一条（{proxy[:60]}）—— "
                            f"重试次数是用户设置的，不能因为没代理就提前收工；"
                            f"建议补充代理"
                        )
                    if not proxy:
                        # 用户点了停止 → 不是「代理没了」，别报成代理故障
                        if self._stop_event.is_set():
                            self._release_account(account, pooled)
                            with self._lock:
                                self._worker_status.pop(worker_id, None)
                            self._set_message("已停止")
                            self._broadcast("state", self._snapshot())
                            return
                        stats = db.proxy_pool_stats()
                        by_type = stats.get("by_type") or {}
                        want = {
                            REGISTER_PROXY_MODE_DYNAMIC: "动态代理",
                            REGISTER_PROXY_MODE_STATIC: "静态代理",
                        }.get(self._register_proxy_mode, "代理")
                        msg = (
                            f"代理池里没有可用的{want}了"
                            f"（池内动态 {by_type.get('dynamic', 0)} 条 / "
                            f"静态 {by_type.get('static', 0)} 条；"
                            f"可用 {stats['available']} / 停用 {stats['disabled']}"
                            f" / 已满 {stats['exhausted']}），本轮已排除 "
                            f"{len(failed_proxies)} 条失败的固定出口代理 —— "
                            f"换出口 IP 重试才有意义，请补充代理后重新开始，"
                            f"已停止 worker-{worker_id}"
                        )
                        self._set_message(msg)
                        logger.warning(msg)
                        self._emit_log(msg, level="error")
                        self._finalize_account(account, "cf_blocked", pooled)
                        with self._lock:
                            self._worker_status.pop(worker_id, None)
                        self._broadcast("state", self._snapshot())
                        return
                else:
                    proxy = self._proxy_for_worker(worker_id, exclude=failed_proxies)

                # ⚠️ 池里没有「别的」代理时，pick_proxy 会回退到被排除的那条
                #    （不这样的话重试会直接卡死）。这等于**没有换出口 IP** ——
                #    必须说清楚，否则用户以为在换 IP 重试。
                if proxy and proxy in failed_proxies:
                    logger.warning(
                        f"[worker-{worker_id}] 池里没有别的可用代理，第 {attempt} 遍"
                        f"仍复用同一条（{proxy[:60]}）—— 这一遍不会换出口 IP"
                    )

                # ── 给这个 run 注入 worker 自己的代理 ──
                run_options = dict(self._options)
                if proxy:
                    run_options["proxy"] = proxy
                # 记录「这次实际用的代理」—— 流程内如果换了代理，
                # registrar 会把 holder 更新成新的，最终按它结算配额。
                proxy_holder = {"proxy": proxy}
                run_options["proxy_state"] = proxy_holder
                # 账号状态由本 worker 统一处置：重试期间号留在 in_use，
                # 不能放回池子 —— 否则另一个 worker 会领到同一个号，
                # 同一个邮箱注册两次 → user_already_exists。
                run_options["hold_account"] = True

                # 启一个 run
                try:
                    run_id = registrar.start_registration(account, run_options)
                except Exception as e:
                    logger.exception(f"[worker-{worker_id}] 启动注册失败: {e}")
                    if proxy and self._backend_proxy_pool:
                        db.proxy_release(proxy)
                    chain_ok = False
                    chain_category = "unknown"
                    self._finalize_account(account, "network", pooled)
                    self._record_finish(
                        False, "unknown", count_fail=True, count_breaker=True
                    )
                    recorded = True
                    break

                with self._lock:
                    self._worker_status[worker_id] = {
                        "email": account["email"],
                        "run_id": run_id,
                        "proxy": proxy,
                        "started_at": time.time(),
                        "attempt": attempt,
                        "max_attempts": self._max_retries + 1,
                    }
                self._broadcast("state", self._snapshot())
                self._broadcast("run_started", {
                    "worker_id": worker_id,
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": proxy,
                    "attempt": attempt,
                    "max_attempts": self._max_retries + 1,
                })
                # ── 把这个 run 的详细日志转发到 batch SSE ──
                # ⚠️ 这一步原来完全缺失：registrar 把日志写进自己的 _run_queues，
                #    而 batch 的 SSE 只广播 state / run_started / run_finished ——
                #    两边**没接上**，所以实时日志只有一个「开始注册」就没了，
                #    详细过程只能去「运行记录」里翻。
                self._forward_run_logs(run_id, worker_id, account["email"])

                # ── 等这一遍跑完（停止请求也等它结束：当前号跑完再停）──
                ok, category = self._wait_run_finish(run_id)

                # 空分类一律按 unknown 处理（unknown 在可重试集合里）。
                # ⚠️ 之前空分类既不在 RETRYABLE 也不在 NON_RETRYABLE 里 →
                #    走「换号继续」分支且重试计数清零 → 失败重试完全不起作用。
                category = str(category or "").strip() or "unknown"
                # 流程内可能换过代理 → 以「实际用的那条」为准
                used_proxy = str(proxy_holder.get("proxy") or proxy or "")
                chain_ok = ok
                chain_category = category

                # ── 注册成功 → 写入号池 ──
                # ⚠️ 这一步原来完全缺失：auto_loop 是从旧注册机迁来的，
                #    旧注册机不管号池，所以「注册成功自动导入号池」这个开关
                #    形同虚设 —— 号注册出来了，但号池里一个都没有。
                if ok:
                    try:
                        self._import_to_pool(account, used_proxy, run_id)
                    except Exception as e:  # noqa: BLE001
                        # 入池失败不能让 worker 死掉（号已经注册出来了，
                        # 死了等于白烧一个邮箱还不给号）。
                        logger.exception("[auto-loop] 注册成功但写入号池失败")
                        self._emit_log(
                            f"注册成功但写入号池失败：{type(e).__name__}: {e}",
                            level="error",
                        )
                    # 这条出口刚刚注册成功 → 清零它的「连续被拦/连续失效」计数。
                    # ⚠️ 不清零的话，计数会跨天累计：一条跑了几百次成功的代理
                    #    会被历史上第 2 次 409 永久停用，而停用又发生在某个
                    #    重试链中间 → worker 没代理可用 → 「失败一次就停」。
                    if used_proxy and self._backend_proxy_pool:
                        try:
                            db.proxy_mark_success(used_proxy)
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"清零代理计数失败: {e}")
                    consecutive_retryable_fails = 0
                    self._record_finish(True, "", count_fail=False, count_breaker=True)
                    recorded = True
                    break

                # ── 失败：结算代理 ──
                # reserved=False（复用上一条兜底）时不能还配额 —— 那一格
                # 不是这条失败占掉的。
                self._settle_failed_proxy(
                    worker_id, used_proxy, category, reserved=reserved
                )
                last_proxy = used_proxy or proxy or last_proxy

                # ── 用户已请求停止：这一遍跑完就不再重试 ──
                # 「当前号跑完再停」—— 号不该因为用户点停止被判定为废号，
                # 放回 available 下次还能用。
                if self._stop_event.is_set():
                    self._release_account(account, pooled)
                    self._record_finish(
                        False, category, count_fail=True, count_breaker=True
                    )
                    recorded = True
                    self._emit_log(
                        f"已请求停止：第 {attempt} 遍流程失败（{category}）后不再重试，"
                        f"号已放回号池",
                        level="warning",
                    )
                    break

                # ── worker 级重试判定 ──
                #
                # 一个完整流程（含 auth_flow 内部的重建会话重试）算**一次**尝试。
                # 失败后：
                #   · 可重试分类（cf_blocked / invalid_state / network / unknown）
                #     → **换代理 + 用同一个号从头再走一遍完整流程**
                #   · 不可重试分类（account / mail_limited）→ 重试也没意义，换号继续
                #
                # 连续可重试失败超过 max_retries → 这个 worker 停下来。
                # 为什么是「停 worker」而不是「无限换号」：出口 IP 不通时换多少个号
                # 都是同一个死路，一直空转只会白烧付费邮箱。
                retryable = category in RETRYABLE_CATEGORIES
                if not retryable:
                    # 换号继续 —— 这次失败不算「连续失败」，重试计数清零。
                    consecutive_retryable_fails = 0
                    self._finalize_account(account, category, pooled)
                    self._record_finish(
                        False, category, count_fail=True, count_breaker=True
                    )
                    recorded = True
                    if category in NON_RETRYABLE_CATEGORIES:
                        self._emit_log(
                            f"第 {attempt} 遍流程失败（{category}：号/邮箱本身的问题），"
                            f"不可重试 → 换下一个号",
                            level="warning",
                        )
                    break

                # 可重试 → 这条代理这一遍已经失败，下一遍优先避开它。
                # ⚠️ 静态**和**动态都要避开：
                #    · 静态：出口 IP 固定，再用一次还是撞同一堵墙
                #    · 动态：用户常常导入多条；失败后还随机回同一条，
                #      等于这一遍没换出口（用户明确要求：导入 5 条就不能
                #      还是只选那一条）。池里只有一条时 pick_proxy 会自动
                #      回退到它，不会因为排除而卡死。
                if used_proxy:
                    failed_proxies.add(used_proxy)

                consecutive_retryable_fails += 1
                if consecutive_retryable_fails > self._max_retries:
                    msg = (
                        f"worker-{worker_id} 连续 {self._max_retries} 次重试仍失败"
                        f"（最后原因：{category or '未知'}），已停止该 worker"
                    )
                    logger.warning(msg)
                    self._set_message(msg)
                    self._emit_log(msg, level="error")
                    self._finalize_account(account, category, pooled)
                    self._record_finish(
                        False, category, count_fail=True, count_breaker=True
                    )
                    recorded = True
                    with self._lock:
                        self._worker_status.pop(worker_id, None)
                    self._broadcast("state", self._snapshot())
                    self._broadcast("run_finished", {
                        "worker_id": worker_id,
                        "email": account["email"],
                        "run_id": run_id,
                        "ok": False,
                        "category": category,
                        "attempt": attempt,
                        "max_attempts": self._max_retries + 1,
                    })
                    return

                # 这一遍失败**不**计失败数 —— 失败数按「号」算（用户口径：
                # 一个号重试 3 遍最终成功 = 成功 1 / 失败 0，而不是失败 3）。
                # 尝试遍数单独由 _attempts 统计，前端显示「共尝试 N 遍」。
                # 熔断也只在整条重试链最终失败时计一次。
                self._record_finish(
                    False, category, count_fail=False, count_breaker=False
                )
                recorded = True
                self._emit_log(
                    f"第 {attempt}/{self._max_retries + 1} 遍流程失败（{category}）→ "
                    f"第 {consecutive_retryable_fails}/{self._max_retries} 次重试："
                    f"换出口 IP，用同一个号从头再走一遍完整流程",
                    level="warning",
                )
                # 继续内层循环 → 同一个号、换代理、再走一遍完整流程
                # （用户会看到又一个「开始注册」）

            # ── 一遍流程链结束 ──
            with self._lock:
                self._worker_status.pop(worker_id, None)
            if not recorded:
                self._record_finish(
                    False, chain_category or "unknown",
                    count_fail=True, count_breaker=True,
                )
            self._broadcast("state", self._snapshot())
            self._broadcast("run_finished", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "ok": chain_ok,
                "category": chain_category,
                "attempt": attempt,
                "max_attempts": self._max_retries + 1,
            })

            # 冷却（每个 worker 自己的节奏）
            cool_down = float(self._options.get("cool_down_seconds") or 3)
            if cool_down > 0:
                for _ in range(int(cool_down * 10)):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)

    def _forward_run_logs(self, run_id: str, worker_id: int, email: str) -> None:
        """把某个 run 的详细日志转发到 batch 的 SSE 订阅者。

        registrar 把日志写进 `registrar._run_queues[run_id]`（同时也写日志文件），
        但 batch 的 SSE 频道只广播状态类事件 —— 两边不接的话，前端实时日志
        只有「开始注册 xxx」，后面的每一步（warmup / PoW / 发码 / verify…）
        全部收不到。

        这里起一个后台线程读 run queue，转成 batch 的 "log" 事件广播出去。
        并发 worker 各自读自己的 run —— registrar 的 QueueLogHandler 按
        线程局部变量(_current_run)过滤，别的 run 的日志不会串进来。
        """

        def _run():
            import queue as _queue

            q = registrar.get_run_queue(run_id)
            if q is None:
                return
            while True:
                try:
                    line = q.get(timeout=1.0)
                except _queue.Empty:
                    # run 可能还没结束（worker 还在等），继续等
                    if not self._worker_status.get(worker_id) or (
                        self._worker_status.get(worker_id) or {}
                    ).get("run_id") != run_id:
                        return  # 这个 run 已经收尾了
                    continue
                if line is None:
                    return  # sentinel: 流结束
                text = str(line)
                # __EVENT__: 前缀是结构化状态事件（error / progress 等）
                if text.startswith("__EVENT__:"):
                    try:
                        payload = json.loads(text[len("__EVENT__:"):])
                    except Exception:  # noqa: BLE001
                        payload = {"kind": "raw", "message": text}
                    payload.setdefault("kind", "log")
                    payload["worker_id"] = worker_id
                    payload["email"] = email
                    payload["run_id"] = run_id
                    # ⚠️ 结构化事件的 kind 要单独带一个标记（如 "run_error"），
                    #    不能直接用 payload["kind"] —— 那是**注册流程内部**的
                    #    事件名（error / progress / warning…），前端会拿它
                    #    当成 batch 的事件类型去匹配，匹配不上就掉进兜底分支，
                    #    当成普通日志行显示。
                    self._broadcast("run_event", payload)
                    continue
                self._broadcast("log", {
                    "worker_id": worker_id,
                    "email": email,
                    "run_id": run_id,
                    "message": text,
                })

        threading.Thread(
            target=_run, daemon=True, name=f"auto-loop-log-{run_id}"
        ).start()

    def _settle_if_all_workers_done(self, worker_id: int, batch_id: str = "") -> None:
        """如果已经没有 worker 在干活了，把状态立刻收敛到 stopped。

        ⚠️ 为什么需要
        ----------
        worker 退出（号池空 / 目标达成 / provider 不可用）后，主控线程
        要等所有 worker 都 join 完才把状态置为 STOPPED。问题是：

        · 号池空的那个 worker 要空转 30s 才退出
        · 其他 worker 可能还在跑一个长注册

        这段时间里前端显示「运行中」，但实际可能一个号都没在注册 ——
        用户看到的就是「worker 都停了，进度还在转」。

        这里在 worker 退出时立刻检查：**还有没有别的 worker 在跑？**
        没有就直接收敛状态，不必等主控线程。
        """
        with self._lock:
            # ⚠️ 批次守卫：只收敛**当前批次**。旧批次的 worker 退出时
            #    不能把新批次（正在跑的）说成 stopped。
            if batch_id and str(self._options.get("batch_id") or "") != batch_id:
                return
            # 还有别的 worker 在跑 → 不动，等它们结束。
            # ⚠️ 用 _alive_workers 而不是 _worker_status ——
            #    后者只在 run 启动后才有值，worker 还在 claim 阶段时会误判。
            cur_batch = str(self._options.get("batch_id") or "")
            alive = {
                wid for (bid, wid) in self._alive_workers if bid == cur_batch
            }
            # ⚠️ 收敛的前提是「**一个 worker 都不在了**」。
            #    只看「除了自己还有没有别人」是不够的 —— 调用点可能
            #    发生在 worker 还活着的时候（跑完一个 run），那时
            #    排除自己后集合是空的，但这个 worker 还会去领下一个号，
            #    收敛成 stopped 就把还在跑的批次说成结束了。
            if alive:
                return
            if self._state == AutoLoopState.STOPPED:
                return
            # ⚠️ 「等待中」**不收敛** —— worker 还在等号池/代理，
            #    用户补上号后它会自己继续跑。收敛成 stopped 就等于把
            #    还在等待的批次说成「已结束」，用户会以为白跑了。
            if self._state == AutoLoopState.WAITING:
                return
            self._state = AutoLoopState.STOPPED
            # ⚠️ _finished_at 只在**第一次**结束时设置 —— 后面还有
            #    _manage_loop 的 finally 和 stop() 都会再写一次，
            #    每次都往后推 → 「已用时」显示得比实际长。
            if not self._finished_at:
                self._finished_at = time.time()
            self._last_message = (
                f"已停止（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
            )
        logger.info("[auto-loop] 所有 worker 已退出，状态收敛为 stopped")
        self._broadcast("state", self._snapshot())

    def _refresh_pool_quota_async(self) -> None:
        """批量结束后统一刷新号池额度（后台线程）。"""

        def _run():
            try:
                from services.account_service import account_service

                with self._lock:
                    tokens = list(self._imported_tokens)
                if not tokens:
                    return
                result = account_service.refresh_accounts(tokens)
                refreshed = int(result.get("refreshed") or 0)
                errors = result.get("errors") or []
                if refreshed:
                    logger.info("[auto-loop] 已刷新 %s 个号的额度", refreshed)
                if errors:
                    logger.warning("[auto-loop] 刷新额度有 %s 个失败", len(errors))
                self._broadcast(
                    "quota_refreshed",
                    {"refreshed": refreshed, "errors": len(errors)},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[auto-loop] 刷新额度失败: %s", exc)

        threading.Thread(target=_run, daemon=True, name="auto-loop-quota-refresh").start()

    def _import_to_pool(
        self,
        account: dict,
        proxy: str,
        run_id: str = "",
    ) -> bool:
        """注册成功后把这个号写入号池（chatgpt2api 的「号池管理」）。

        走 chatgpt2api 原生的号池写入（和手动导入号池同一套），
        格式与注册机导出的 `access_token----proxy` 完全一致。

        ⚠️ 这里**不刷新额度** —— 批量过程中逐个刷新会拖慢 worker，
        而且额度刷新要走 OpenAI 网络请求。批量结束后由前端统一刷一次。

        ⚠️ 邮箱必须用**注册成功后的真实邮箱**，不能用 worker 手里的那个：
           · 池化来源（outlook / icloud）：两者一致
           · 非池化来源（Remail / CF 临时邮箱）：worker 手里只有占位地址
             （remail_placeholder_xxx@placeholder.local），
             用它去查注册结果永远查不到 → 号注册出来了却**入不了池**。
             真实邮箱由 registrar 写进运行记录（run.email），这里从那里取。
        """
        if not self._options.get("auto_import", True):
            return False
        email = self._resolve_registered_email(account, run_id)
        if not email:
            logger.warning(
                "[auto-loop] 注册成功但拿不到真实邮箱，跳过入池（run=%s）", run_id
            )
            return False
        try:
            from services import register_service

            record = db.get_registered(email) or {}
            access_token = str(record.get("access_token") or "").strip()
            if not access_token:
                logger.warning("[auto-loop] 注册成功但没拿到 access_token，跳过入池: %s", email)
                return False

            # ── 入池代理决策 ──────────────────────────────────
            # ⚠️ 动态代理永远不能进号池（出口 IP 每次变 → 账号用起来
            #    更容易触发风控，还会把代理流量包耗光）。
            #    之前这里直接用 worker 领到的注册代理（多半是动态的）。
            mode = str(self._options.get("pool_proxy_mode") or "").strip().lower()
            if mode not in register_service.POOL_PROXY_MODES:
                mode = (
                    register_service.POOL_PROXY_MODE_REGISTER
                    if self._options.get("with_proxy", True)
                    else register_service.POOL_PROXY_MODE_NONE
                )
            assigned_proxy, reason = register_service.resolve_pool_proxy(
                mode,
                register_proxy=str(proxy or "").strip(),
                bind_proxy=str(self._options.get("bind_proxy") or "").strip(),
                auto_bind=bool(self._options.get("auto_bind")),
                # ⚠️ 「注册使用代理」选的是静态代理时，入池**沿用同一条** ——
                #    注册出口 = 长期使用出口，账号不会注册完就换 IP。
                prefer_register_static=True,
                # 号已在池中（重跑/重复导入）→ 沿用原绑定，不重复占名额
                existing_proxy=register_service.pool_proxy_of_email(email),
            )
            if assigned_proxy:
                logger.info(
                    "[auto-loop] 入池代理：%s（%s）", assigned_proxy, reason
                )
            elif reason and mode != register_service.POOL_PROXY_MODE_NONE:
                logger.info("[auto-loop] 入池不带代理：%s", reason)

            # allow_record_proxy=False —— 决策结果为空就是空，
            # 不能再回落到注册记录里的动态代理。
            payload = register_service.build_pool_payload(
                email, access_token, assigned_proxy, allow_record_proxy=False
            )

            result = register_service.import_to_pool_with_refresh([payload], refresh=False)
            imported = int(result.get("imported") or 0)
            skipped = int(result.get("skipped") or 0)
            # ⚠️ imported=0 但 skipped>0 = 这个号**已经在池子里**（重跑同一个号）。
            #    这时也要算成功 —— 号池里那条记录的代理已经被更新成这次的代理了
            #    （add_account_items 是合并覆盖），不算成功的话用户会以为没入池。
            if imported or skipped:
                with self._lock:
                    self._imported_tokens.append(access_token)
                if imported:
                    logger.info(
                        "[auto-loop] 已导入号池: %s（代理=%s）",
                        email,
                        payload.get("proxy") or "直连",
                    )
                else:
                    logger.info(
                        "[auto-loop] 号已在池中，已更新代理: %s（代理=%s）",
                        email,
                        payload.get("proxy") or "直连",
                    )
                self._broadcast(
                    "imported",
                    {"email": email, "imported": imported or skipped},
                )
                return True
            logger.warning(
                "[auto-loop] 入池未新增（可能已存在）: %s skipped=%s",
                email,
                skipped,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            # 入池失败不能拖垮整个批次 —— 号已经注册成功了
            logger.warning("[auto-loop] 导入号池失败 %s: %s", email, exc)
            return False

    @staticmethod
    def _resolve_registered_email(account: dict, run_id: str = "") -> str:
        """找出这次注册**真实**用掉的邮箱。

        顺序：
          1. 运行记录里的邮箱（registrar 成功后回写的是真实邮箱，最准）
          2. worker 手里的 account 邮箱（池化来源就是这个）
          3. 都拿不到 → 空串（调用方跳过入池）
        """
        candidates: list[str] = []
        if run_id:
            try:
                row = db.get_run(run_id) or {}
                candidates.append(str(row.get("email") or ""))
            except Exception:  # noqa: BLE001
                pass
        candidates.append(str((account or {}).get("email") or ""))
        for raw in candidates:
            email = raw.strip().lower()
            # 占位邮箱（remail_placeholder_xxx@placeholder.local）不能用来查凭证
            if not email or "@" not in email:
                continue
            if "placeholder" in email:
                continue
            return email
        return ""

    def _wait_run_finish(self, run_id: str, timeout: int = 1800) -> tuple[bool, str]:
        """轮询 runs 表，等 run 跑完。

        返回 (ok, category)。

        ⚠️ 停止语义：**当前号跑完再停**（用户明确要求）。
           以前 stop_event 一置位这里就立刻返回哨兵 STOPPED，worker 直接
           return —— 但 registrar 的线程还在跑，后果是：
             · 号永远卡在 in_use（registrar 被 hold_account 关了状态回写，
               worker 又提前走了，没人收尾）
             · 代理预占的配额不归还
             · 该 run 之后若注册成功，**永远不会入号池**（入池在 worker 里）
           现在不管 stop 有没有置位，都把这一遍等完；收尾逻辑跑完后再退出。

        category 的两种含义：
          · 正常失败 → 具体分类（cf_blocked / network / account /
            mail_limited / invalid_state / unknown）—— 空值一律归 unknown。
          · 超时     → 归 unknown（可重试）—— 之前返回空串，被当成不可重试，
            worker 直接换号收工。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                row = db.get_run(run_id)
            except Exception as e:  # noqa: BLE001
                # ⚠️ 读运行记录出错不能让 worker 直接死掉（那样批次会静默结束、
                #    重试一次都不跑）。记一笔，下一轮接着读。
                logger.warning(f"[auto-loop] 读运行记录 {run_id} 失败（稍后重试）: {e}")
                time.sleep(1)
                continue
            if row:
                st = row.get("status")
                if st == "done":
                    return True, ""
                if st == "failed":
                    return False, (row.get("error_category") or "unknown")
            time.sleep(1)
        logger.warning(f"run {run_id} 等了 {timeout}s 没结束，超时放弃")
        return False, "unknown"


# 全局单例
CONTROLLER = AutoLoopController()

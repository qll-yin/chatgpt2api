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
RETRYABLE_CATEGORIES = frozenset({"cf_blocked", "network", "unknown"})

# 不可重试的分类及原因（写清楚是为了以后有人问「为什么不重试」）：
#   account     —— 号/邮箱本身废了（user_already_exists / 邮箱已注册 /
#                  已有账号 / refresh_token 失效）。重试同一个号毫无意义，
#                  换号继续才是对的。
#   mail_limited—— 邮箱侧固有限制（短效邮箱已消耗）。重试只会再撞一次。
NON_RETRYABLE_CATEGORIES = frozenset({"account", "mail_limited"})


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
            if self._state == AutoLoopState.WAITING:
                return {"ok": False, "error": "当前在等资源，没在跑，不用暂停"}
            if self._state != AutoLoopState.RUNNING:
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
                "target_count": self._target_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok)
                    if self._target_count else None
                ),
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "workers": workers_info,
                "last_message": self._last_message,
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

    def _proxy_for_worker(self, worker_id: int) -> str:
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

        后端池为空时回落到旧的文本代理池（按 worker_id 取模，保持老行为）。
        """
        if self._backend_proxy_pool:
            picked = db.pick_proxy()
            if picked:
                return picked
            # 池里没有可用代理了 —— 返回空串，让调用方决定是等待还是停止。
            # 这里**不**静默回落成 options.proxy：那样会绕过配额限制，
            # 所有 worker 挤到同一个全局代理上，正好是限流要防的事。
            return ""
        if self._proxy_pool:
            return self._proxy_pool[worker_id % len(self._proxy_pool)]
        return self._options.get("proxy", "") or ""

    def _record_finish(self, ok: bool, category: str):
        """worker 结束一个 run 后调，更新计数 + 熔断。"""
        with self._lock:
            if ok:
                self._registered_ok += 1
                self._consecutive_network_fails = 0
            else:
                self._registered_fail += 1
                # ⚠️ 只有真正的环境/网络故障才计入熔断。
                #    cf_blocked 是「这个出口 IP 被 CF 拒了」—— 换一个代理就能继续，
                #    不该把整个批次停掉（实测日志里 7 次 CF 403 触发了熔断，
                #    白白暂停了自动注册）。
                if category == "network":
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            self._last_message = (
                f"累计 ok={self._registered_ok} fail={self._registered_fail}"
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

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        # 本轮批次的标识 —— 用来识别「我是上一批残留的 worker」。
        # ⚠️ 没有它的话，上一批 worker 退出时会把**新一批**的状态误杀成 stopped
        #    （start() 刚设好 running，旧 worker 的 finally 一跑就把它覆盖了）。
        my_batch = str(self._options.get("batch_id") or "")
        try:
            self._worker_loop_inner(worker_id, my_batch)
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
        no_proxy_round = 0
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

            # 每次注册前动态取代理（后端池有启用/上限/用量状态）。
            # 代理池非空但取不到可用代理 → 说明全部停用或全部超配额，
            # 这种情况要停下整个 auto-loop 并说清楚原因，不能静默直连 ——
            # 直连等于所有 worker 挤同一个真实 IP，正是限流要防的事。
            proxy = self._proxy_for_worker(worker_id)
            if self._backend_proxy_pool and not proxy:
                no_proxy_round += 1
                stats = db.proxy_pool_stats()
                msg = (
                    f"代理池无可用代理（可用 {stats['available']} / 停用 {stats['disabled']}"
                    f" / 已满 {stats['exhausted']}），已停止自动跑号"
                )
                if no_proxy_round == 1:
                    self._set_message(msg)
                    logger.warning(msg)
                with self._lock:
                    self._stop_event.set()
                self._broadcast("state", self._snapshot())
                return

            # claim 下一个号。要不要走号池由 provider 的 pooled 决定，
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

            # 给这个 run 注入 worker 自己的代理
            run_options = dict(self._options)
            if proxy:
                run_options["proxy"] = proxy

            # 启一个 run
            try:
                run_id = registrar.start_registration(account, run_options)
            except Exception as e:
                logger.exception(f"[worker-{worker_id}] 启动注册失败: {e}")
                if pooled:
                    db.release_unused(account["email"])
                time.sleep(2)
                continue

            with self._lock:
                self._worker_status[worker_id] = {
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": proxy,
                    "started_at": time.time(),
                }
            self._broadcast("state", self._snapshot())
            self._broadcast("run_started", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "proxy": proxy,
            })
            # ── 把这个 run 的详细日志转发到 batch SSE ──
            # ⚠️ 这一步原来完全缺失：registrar 把日志写进自己的 _run_queues，
            #    而 batch 的 SSE 只广播 state / run_started / run_finished ——
            #    两边**没接上**，所以实时日志只有一个「开始注册」就没了，
            #    详细过程只能去「运行记录」里翻。
            self._forward_run_logs(run_id, worker_id, account["email"])

            # 等当前 run 跑完
            ok, category = self._wait_run_finish(run_id)

            # 失败计数在这里结算：成功计数由 registrar 在真正拿到凭证时结算
            #（那里才知道这个号到底注册成功没有），失败则在这里记。
            # 失败**不占用配额**（只累加 failed_count），符合"失败不计入上限"。
            # ⚠️ pick_proxy() 选中时已经**预占**了一格，所以失败要先把预占还回去，
            #    否则失败的号也会白占一个名额。
            if not ok and proxy and self._backend_proxy_pool:
                db.proxy_release(proxy)
                db.proxy_used(proxy, ok=False)
                # ⚠️ CF 拦截的处置与普通失败**完全不同**：
                #    · 静态代理（固定出口 IP）→ 这个 IP 已被 CF 拉黑，
                #      连续被拦达阈值就自动停用，别再拿它试
                #    · 动态代理（每次换 IP）→ 不停用，只是这个出口不行，
                #      换一次 IP 可能就通过了
                # 具体动作交给 db.mark_proxy_cf_blocked 按类型决定。
                if category == "cf_blocked":
                    cfres = db.mark_proxy_cf_blocked(proxy)
                    if cfres.get("auto_disabled"):
                        logger.warning(
                            f"[worker-{worker_id}] 静态代理被 CF 连续拦截 "
                            f"{cfres.get('cf_blocked')} 次 → 已自动停用: {proxy[:60]}"
                        )
                    else:
                        logger.info(
                            f"[worker-{worker_id}] 代理被 CF 拦截"
                            f"（{cfres.get('proxy_type')} 类型，累计 "
                            f"{cfres.get('cf_blocked')} 次）→ 换下一个代理: {proxy[:60]}"
                        )
                elif category == "invalid_state":
                    # ── 409 invalid_state 的代理级处置 ──
                    # ⚠️ 这个错误被分类成 account（不是 network），所以不会熔断
                    #    整批 —— 但**根因往往就是这个出口 IP 被 OpenAI 记了**。
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
                    else:
                        logger.info(
                            f"[worker-{worker_id}] 代理会话失效(409 invalid_state)"
                            f"（{isres.get('proxy_type')} 类型，累计 "
                            f"{isres.get('invalid_state')} 次）→ 换下一个代理: {proxy[:60]}"
                        )

            # ── 注册成功 → 写入号池 ──
            # ⚠️ 这一步原来完全缺失：auto_loop 是从旧注册机迁来的，
            #    旧注册机不管号池，所以「注册成功自动导入号池」这个开关
            #    形同虚设 —— 号注册出来了，但号池里一个都没有。
            if ok:
                self._import_to_pool(account, proxy, run_id)
                consecutive_retryable_fails = 0

            # ── worker 级重试判定 ──
            #
            # 一个完整流程（含 auth_flow 内部的重建会话重试）算**一次**尝试。
            # 失败后：
            #   · 可重试分类（cf_blocked / network / unknown）→ 换个代理再跑一遍
            #   · 不可重试分类（account / mail_limited）→ 重试也没意义，换号继续
            #
            # 连续可重试失败超过 max_retries → 这个 worker 停下来。
            # 为什么是「停 worker」而不是「无限换号」：出口 IP 不通时换多少个号
            # 都是同一个死路，一直空转只会白烧付费邮箱。
            if not ok:
                retryable = category in RETRYABLE_CATEGORIES
                if not retryable:
                    # 换号继续 —— 这次失败不算「连续失败」，重试计数清零。
                    consecutive_retryable_fails = 0
                    if category in NON_RETRYABLE_CATEGORIES:
                        logger.info(
                            f"[worker-{worker_id}] 失败分类 {category} 不可重试"
                            f"（号/邮箱本身的问题）→ 换下一个号继续"
                        )
                else:
                    consecutive_retryable_fails += 1
                    if consecutive_retryable_fails > self._max_retries:
                        msg = (
                            f"worker-{worker_id} 连续 {self._max_retries} 次重试仍失败"
                            f"（最后原因：{category or '未知'}），已停止该 worker"
                        )
                        logger.warning(msg)
                        self._set_message(msg)
                        # ⚠️ 必须在 return 前结算 —— 这次失败也要算进批次统计，
                        #    否则「成功 X / 失败 Y」里的 Y 会少算这一次。
                        self._record_finish(False, category)
                        with self._lock:
                            self._worker_status.pop(worker_id, None)
                        self._broadcast("state", self._snapshot())
                        return
                    logger.info(
                        f"[worker-{worker_id}] 第 {consecutive_retryable_fails}/{self._max_retries} "
                        f"次重试（原因：{category or '未知'}）→ 换条件再试一遍"
                    )

            with self._lock:
                self._worker_status.pop(worker_id, None)
            self._record_finish(ok, category)
            # ⚠️ 刚跑完一个 run —— 这是「worker 确实停下来了」的时刻。
            #    如果此刻没有别的 worker 在跑（号池空 / provider 不可用 /
            #    目标达成），状态要立刻收敛，别让前端一直显示「运行中」。
            # ⚠️ 这里**不**收敛状态 —— worker 还活着，可能继续领下一个号。
            #    收敛只在 worker 真正退出时做（见 _worker_loop 的 finally）。
            self._broadcast("state", self._snapshot())
            self._broadcast("run_finished", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "ok": ok,
                "category": category,
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

            payload = register_service.build_pool_payload(email, access_token, proxy)
            if not self._options.get("with_proxy", True):
                payload.pop("proxy", None)

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
        """轮询 runs 表，等 run 跑完。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False, ""
            row = db.get_run(run_id)
            if row:
                st = row.get("status")
                if st == "done":
                    return True, ""
                if st == "failed":
                    return False, (row.get("error_category") or "")
            time.sleep(1)
        logger.warning(f"run {run_id} 等了 {timeout}s 没结束，超时放弃")
        return False, ""


# 全局单例
CONTROLLER = AutoLoopController()

"""账号生成失败次数巡检：累计失败太多就自动标「异常」。

背景
----
号池里总有一些号「额度还没用完，但一张图都生不出来」——比如额度 25、成功 15、
失败 227、剩余 10。这类号留在池子里只会：占着额度、拖慢整体成功率、让调度一直
撞在同一个坏号上。

这里按**累计失败次数**（账号的 `fail` 字段）定时巡检，超过阈值就把状态改成
「异常」，从此不再参与取号。

口径说明
--------
- 用**累计**失败次数，不是「连续失败」：用户明确要求「失败数 ≥ 阈值就标异常」。
- 只看状态为「正常」的账号；已经是「异常 / 禁用」的跳过（避免反复处理），
  「限流」的也跳过（额度本来就是 0，不参与取号，等它恢复到正常再说）。
- **不动额度**：只改状态，剩余额度保留。用户手动把状态改回「正常」后可以继续用。
  ⚠️ 但累计失败数不清零 —— 如果它还是坏的，下一轮巡检会再次标异常（这正是期望行为）。
- 默认关闭，需要在系统设置里手动打开。
"""

from __future__ import annotations

import threading
import time

from services.account_service import account_service
from services.config import config
from services.log_service import LOG_TYPE_ACCOUNT, log_service

# 这些状态不参与巡检
_SKIP_STATUSES = {"异常", "禁用", "限流"}

_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_lock = threading.Lock()
# 最近一次巡检结果（供前端/接口查）
_last_result: dict = {"checked": 0, "marked": [], "ran_at": ""}


def scan_once(threshold: int | None = None) -> dict:
    """跑一轮巡检，返回本轮标记了哪些账号。

    幂等：已经是「异常」的号不会重复处理；连续调用只会在有新号达标时产生变化。
    """
    from datetime import datetime

    limit = int(threshold if threshold is not None else config.fail_guard_threshold)
    checked = 0
    marked: list[dict] = []
    for account in account_service.list_accounts():
        status = str(account.get("status") or "")
        if status in _SKIP_STATUSES:
            continue
        checked += 1
        try:
            fail_count = int(account.get("fail") or 0)
        except (TypeError, ValueError):
            fail_count = 0
        if fail_count < limit:
            continue
        access_token = str(account.get("access_token") or "")
        if not access_token:
            continue
        email = str(account.get("email") or "")
        reason = f"累计生成失败 {fail_count} 次（自动检测，阈值 {limit}）"
        updated = account_service.update_account(
            access_token,
            {
                "status": "异常",
                "abnormal_reason": reason,
                "abnormal_marked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            quiet=True,
        )
        if updated is None:
            continue
        marked.append({"email": email, "fail": fail_count, "reason": reason})
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "自动标记异常账号",
            {
                "email": email,
                "token": access_token[:12] + "..." if len(access_token) > 12 else access_token,
                "fail": fail_count,
                "threshold": limit,
                "reason": reason,
            },
        )
    result = {
        "checked": checked,
        "marked": marked,
        "threshold": limit,
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    global _last_result
    _last_result = result
    if marked:
        print(f"[fail-guard] marked {len(marked)} account(s) abnormal (threshold={limit})")
    return result


def last_result() -> dict:
    return dict(_last_result)


def _worker(stop_event: threading.Event) -> None:
    # 启动后先等一个间隔再跑：避免服务重启的瞬间就把一批号标掉（也给配置留出时间）
    while not stop_event.is_set():
        interval = max(1, int(config.fail_guard_interval_minute)) * 60
        # 分段等待：既能快速响应 stop，也能让「改间隔」在一分钟内生效
        deadline = time.monotonic() + interval
        while not stop_event.is_set() and time.monotonic() < deadline:
            stop_event.wait(min(30.0, max(1.0, deadline - time.monotonic())))
        if stop_event.is_set():
            return
        if not config.fail_guard_enabled:
            continue
        try:
            scan_once()
        except Exception as exc:  # noqa: BLE001
            print(f"[fail-guard] scan failed: {exc}")


def start(stop_event: threading.Event | None = None) -> threading.Thread | None:
    """启动巡检线程（应用 lifespan 统一控制生命周期）。"""
    global _thread, _stop_event
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop_event = stop_event or threading.Event()
        _thread = threading.Thread(
            target=_worker, args=(_stop_event,), daemon=True, name="fail-guard"
        )
        _thread.start()
        return _thread


def stop() -> None:
    global _thread
    with _lock:
        if _stop_event is not None:
            _stop_event.set()
        _thread = None

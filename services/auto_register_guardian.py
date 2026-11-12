"""
号池自动补充守护进程。

作用
----
按固定周期检查 chatgpt2api 号池里的**可用账号数**，低于阈值时自动触发注册，
让号池维持在目标水位。用于「账号消耗快、需要持续补充」的生产场景。

触发逻辑
--------
```
每 check_interval_minute 分钟检查一次
  ↓
可用账号数（status == "正常"） < threshold ?
  ↓ 是
检查是否已有批量注册在跑（幂等保护）→ 没有才触发
  ↓
启动本进程的批量注册控制器（services/register_batch.py），
target_count = target_batch，注册满这么多成功后自动停
```

为什么是**进程内调用**而不是 HTTP 调注册机
-------------------------------------------
注册机已经合并进本项目（services/register/ + services/register_batch.py），
它不是独立部署的服务。早期版本这里按「外部服务」设计，去打
`http://register:8765/api/auto/start` —— 那台主机根本不存在，
于是守护每次触发都是
`HTTPConnectionPool(host='register', port=8765): NameResolutionError`。

现在改成直接调 `register_batch.CONTROLLER.start()`：
同一个进程、同一个数据库、同一个号池，没有网络往返，也不会因为
「注册机没起」而静默失败。

可用账号的口径
--------------
与 chatgpt2api 自身的号池统计保持一致：`status == "正常"`。
限流 / 异常 / 禁用的账号不计入 —— 它们当前不能用来生图。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# 默认配置（可被 config.json 的 auto_register 段覆盖）
DEFAULT_SETTINGS = {
    "enabled": False,
    # 号池可用账号低于这个数就触发注册
    "threshold": 10,
    # 每次注册的目标成功数
    "target_batch": 20,
    # 检查间隔（分钟）
    "check_interval_minute": 30,
    # 触发的批量注册并发数
    "concurrency": 3,
    # 触发的批量注册用哪个邮箱来源（留空 = 用「邮箱配置」里选的那个）
    "mail_source": "",
    # 传给批量控制器的额外参数（代理池、邮箱来源、是否要 refresh_token 等）
    "register_options": {},
    # 触发后多久内不再重复触发（分钟），防止注册还没跑完就又触发
    "cooldown_minute": 30,
}

# 守护进程状态（供 API 查询）
_state: dict[str, Any] = {
    "running": False,
    "last_check_at": 0.0,
    "last_check_active": -1,
    "last_trigger_at": 0.0,
    "last_trigger_result": "",
    "last_error": "",
    "trigger_count": 0,
    "in_flight": False,
}
_state_lock = threading.RLock()
_stop_event = threading.Event()
_wakeup_event = threading.Event()
_thread: threading.Thread | None = None


def _now() -> float:
    return time.time()


def get_settings() -> dict[str, Any]:
    """读取自动补充配置（config.json 的 auto_register 段）。"""
    from services.config import config

    raw = config.get_auto_register_settings()
    settings = dict(DEFAULT_SETTINGS)
    for key, value in (raw or {}).items():
        if key in settings:
            settings[key] = value
    # 数值型字段做钳制，避免配置写错导致异常行为
    settings["threshold"] = max(0, int(settings.get("threshold") or 0))
    settings["target_batch"] = max(1, int(settings.get("target_batch") or 1))
    settings["check_interval_minute"] = max(1, int(settings.get("check_interval_minute") or 1))
    settings["cooldown_minute"] = max(0, int(settings.get("cooldown_minute") or 0))
    settings["concurrency"] = max(1, min(20, int(settings.get("concurrency") or 1)))
    settings["mail_source"] = str(settings.get("mail_source") or "").strip()
    settings["enabled"] = bool(settings.get("enabled"))
    options = settings.get("register_options")
    settings["register_options"] = options if isinstance(options, dict) else {}
    return settings


def get_state() -> dict[str, Any]:
    with _state_lock:
        return dict(_state)


def _set_state(**updates: Any) -> None:
    with _state_lock:
        _state.update(updates)


def _active_account_count() -> int:
    """号池里当前可用的账号数（status == "正常"）。"""
    from services.account_service import account_service

    return int(account_service.get_stats().get("active") or 0)


def _batch_running() -> bool:
    """批量注册控制器当前是否在跑。

    用于幂等保护 —— 守护自己触发的注册还没结束时，不重复触发。
    """
    try:
        from services import register_batch

        state = str(register_batch.CONTROLLER.status().get("state") or "").lower()
        return state in {"running", "paused"}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[auto-register] 查询批量注册状态失败: %s", exc)
        return False


def trigger_registration(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """触发一轮注册（进程内直接启动批量控制器）。

    返回 {"ok": bool, "error": str, ...}
    """
    settings = settings or get_settings()

    options = dict(settings.get("register_options") or {})
    options["target_count"] = int(settings.get("target_batch") or 1)
    options["concurrency"] = int(settings.get("concurrency") or 1)
    # 邮箱来源：优先用守护自己配的，留空则用「邮箱配置」里选的那个
    mail_source = str(settings.get("mail_source") or "").strip()
    if mail_source:
        options["mail_source"] = mail_source
    # 注册成功自动入号池 —— 守护的整个目的就是补号池
    options.setdefault("auto_import", True)

    try:
        from services import register_batch

        result = register_batch.CONTROLLER.start(options)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动批量注册失败: {exc}"}

    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("error") or "批量注册拒绝启动")}
    return {"ok": True, "result": result}


def _check_once() -> dict[str, Any]:
    """执行一次检查。返回本次检查的结论。"""
    settings = get_settings()
    if not settings.get("enabled"):
        return {"skipped": True, "reason": "未启用"}

    active = _active_account_count()
    _set_state(last_check_at=_now(), last_check_active=active)

    threshold = int(settings.get("threshold") or 0)
    if active >= threshold:
        return {"skipped": True, "reason": f"可用账号 {active} ≥ 阈值 {threshold}", "active": active}

    # 冷却期内不重复触发
    cooldown_secs = int(settings.get("cooldown_minute") or 0) * 60
    with _state_lock:
        last_trigger = float(_state.get("last_trigger_at") or 0)
    if cooldown_secs and (_now() - last_trigger) < cooldown_secs:
        remaining = int(cooldown_secs - (_now() - last_trigger))
        return {
            "skipped": True,
            "reason": f"冷却中（剩余约 {remaining // 60} 分钟）",
            "active": active,
        }

    if _batch_running():
        return {"skipped": True, "reason": "批量注册已在运行", "active": active}

    result = trigger_registration(settings)
    if result.get("ok"):
        _set_state(
            last_trigger_at=_now(),
            last_trigger_result="已触发",
            last_error="",
            trigger_count=int(get_state().get("trigger_count") or 0) + 1,
        )
        logger.info(
            "[auto-register] 可用账号 %s < 阈值 %s，已触发注册（目标 %s 个）",
            active, threshold, settings.get("target_batch"),
        )
        return {"skipped": False, "triggered": True, "active": active, "result": result}
    _set_state(last_error=str(result.get("error") or ""), last_trigger_result="失败")
    logger.warning("[auto-register] 触发注册失败: %s", result.get("error"))
    return {"skipped": False, "triggered": False, "active": active, "error": result.get("error")}


def _worker() -> None:
    """守护线程主体。

    等待策略：用两个 Event —— `_stop_event`（要退出）与 `_wakeup_event`（要立刻再查一次）。
    只用单个 Event 无法区分「停止」和「唤醒」，会导致唤醒时线程直接退出。
    """
    logger.info("[auto-register] 号池自动补充守护已启动")
    while not _stop_event.is_set():
        try:
            _check_once()
        except Exception:
            logger.exception("[auto-register] 检查执行异常")
        interval = get_settings().get("check_interval_minute") or 30
        _wakeup_event.clear()
        # 等到「间隔到了」或「被唤醒」或「要停止」三者之一
        _wakeup_event.wait(max(60, int(interval) * 60))
        if _stop_event.is_set():
            break
    logger.info("[auto-register] 号池自动补充守护已停止")


def notify_wakeup() -> None:
    """唤醒守护线程，让它立即重新检查（配置变更时调用）。"""
    _wakeup_event.set()


def start(stop_event: threading.Event | None = None) -> threading.Thread | None:
    """启动守护进程。

    传入 stop_event 时沿用它（应用 lifespan 统一控制生命周期）。
    不传时使用模块自己的 event —— 且**每次 start 都重建**，
    否则 stop() 之后无法再次启动（守护线程会立刻退出）。
    """
    global _thread, _stop_event, _wakeup_event
    if _thread is not None and _thread.is_alive():
        return _thread
    if stop_event is not None:
        _stop_event = stop_event
    else:
        _stop_event = threading.Event()
    _wakeup_event = threading.Event()
    _thread = threading.Thread(target=_worker, daemon=True, name="auto-register-guardian")
    _thread.start()
    return _thread


def stop() -> None:
    """停止守护进程。"""
    _stop_event.set()
    global _thread
    if _thread is not None:
        _thread.join(timeout=2)
        _thread = None

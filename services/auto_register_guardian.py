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
检查是否已有注册任务在跑（幂等保护）→ 没有才触发
  ↓
调用注册机的 /api/auto/start，target_count = target_batch
  ↓
注册机注册满 target_batch 个成功后自动停止
```

为什么用「HTTP 调注册机」而不是把注册代码搬进来
--------------------------------------------------
注册机是独立部署的服务，已经具备完整的自动循环能力
（`/api/auto/start` 支持 `target_count` 达标自动停止）。
通过 HTTP 调用它：
- 不需要在本项目里复制 1.2 万行注册逻辑
- 注册机可以独立升级、独立重启
- 职责边界清晰：本项目管「什么时候需要补」，注册机管「怎么补」

可用账号的口径
--------------
与 chatgpt2api 自身的号池统计保持一致：`status == "正常"`。
限流 / 异常 / 禁用的账号不计入 —— 它们当前不能用来生图。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import requests

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
    # 注册机地址（容器内默认用服务名）
    "register_url": "http://register:8765",
    # 注册机接口鉴权（注册机的 auth-key，留空则不带）
    "register_auth_key": "",
    # 传给注册机的额外参数（并发数、代理池、是否要 refresh_token 等）
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
    settings["register_url"] = str(settings.get("register_url") or "").strip().rstrip("/")
    settings["register_auth_key"] = str(settings.get("register_auth_key") or "").strip()
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


def _register_auto_running(register_url: str, auth_key: str) -> bool:
    """问注册机：现在有没有自动循环在跑。

    用于幂等保护 —— 守护自己触发的注册还没结束时不重复触发。
    注册机不可达时返回 False（宁可多触发一次，也不要卡死不动）。
    """
    try:
        headers = {}
        if auth_key:
            headers["Authorization"] = f"Bearer {auth_key}"
        resp = requests.get(f"{register_url}/api/auto/status", headers=headers, timeout=10)
        if resp.status_code >= 400:
            return False
        payload = resp.json()
        state = str(payload.get("state") or "").lower()
        return state in {"running", "paused"}
    except Exception as exc:
        logger.debug("[auto-register] 查询注册机状态失败: %s", exc)
        return False


def trigger_registration(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """触发一轮注册。

    返回 {"ok": bool, "error": str, ...}
    """
    settings = settings or get_settings()
    register_url = str(settings.get("register_url") or "").strip().rstrip("/")
    if not register_url:
        return {"ok": False, "error": "未配置 register_url"}

    options = dict(settings.get("register_options") or {})
    options["target_count"] = int(settings.get("target_batch") or 1)

    headers = {"Content-Type": "application/json"}
    auth_key = str(settings.get("register_auth_key") or "").strip()
    if auth_key:
        headers["Authorization"] = f"Bearer {auth_key}"

    try:
        resp = requests.post(
            f"{register_url}/api/auto/start",
            json=options,
            headers=headers,
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "error": f"调用注册机失败: {exc}"}

    if resp.status_code >= 400:
        return {"ok": False, "error": f"注册机返回 HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        payload = resp.json()
    except Exception:
        payload = {}
    if not payload.get("ok", True):
        return {"ok": False, "error": str(payload.get("error") or "注册机拒绝启动")}
    return {"ok": True, "result": payload}


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

    register_url = str(settings.get("register_url") or "")
    if _register_auto_running(register_url, str(settings.get("register_auth_key") or "")):
        return {"skipped": True, "reason": "注册机已有任务在运行", "active": active}

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

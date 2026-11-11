"""
注册服务：把注册机核心接入 chatgpt2api，并在注册成功后写入号池。

与原注册机的关系
----------------
原注册机是独立 Web 服务，注册结果要人工导出再导入。
这里把注册核心（services/register/）直接跑在本进程内，
注册成功后**立刻**写入 chatgpt2api 的号池：

    注册成功 → AccessTokenService 写入 accounts 表
             → 导出格式即 `access_token----proxy`（proxy 可为空）

两种入池方式
------------
1. **自动入池**（auto_import=True）：注册成功即刻写入号池。
   批量注册时可用，适合无人值守。
2. **手动入池**：注册结果列表页勾选后调用
   `import_registered_to_pool(emails)` 批量导入。

号池写入用的是 chatgpt2api 原生的账号服务，
所以号池页面看到的就是刚注册的号，无需任何导出步骤。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# 注册运行状态（内存态：运行中的任务不该落库成"历史记录"）
# key: run_id
_runs: dict[str, dict[str, Any]] = {}
_runs_lock = threading.RLock()
_log_sinks: dict[str, Callable[[str, str], None]] = {}


# ──────────────────────── 配置读取 ────────────────────────


def _reg_setting(key: str, default: str = "") -> str:
    from services.storage import register_store as store

    return store.get_setting(key, default)


def _reg_bool(key: str, default: bool = False) -> bool:
    return str(_reg_setting(key, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


def _load_mail_settings(mail_source: str = "") -> dict:
    """读邮箱来源的配置（含明文密钥，仅服务端构造 provider 时用）。

    按 provider 自己声明的 config_fields 逐项取 —— 加一种邮箱不用改这里。
    """
    from services.register.mail_providers import list_providers

    out: dict[str, Any] = {"mail_source": mail_source or _reg_setting("mail_source", "outlook")}
    for provider in list_providers():
        for field in provider.get("config_fields") or []:
            key = field.get("key")
            if not key:
                continue
            out[key] = _reg_setting(key, "")
    return out


def _build_config(options: dict[str, Any]):
    """构造注册用的 Config 对象。

    Config 是注册机的最小配置 dataclass，当前只有 proxy 字段。
    其余开关（是否要 token、2FA 等）通过 env_overrides 传给 AuthFlow。
    """
    from services.register.config import Config

    return Config(proxy=str(options.get("proxy") or "").strip() or None)


def _build_env_overrides(options: dict[str, Any]) -> dict[str, str]:
    """本次注册专属的环境覆盖（挂在实例上，不污染进程全局）。

    ⚠️ 不能用 os.environ：并发跑多个 worker 时，A 设的 OTP_TIMEOUT 会被
    B 读到，B 跑完还原成 A 之前的值，A 后半程就用上别人的配置了。
    """
    env: dict[str, str] = {}
    # 单号注册场景 fast-fail 没意义（fast-fail 是批量时"跳过被识别的号"用的）
    env["WEBUI_ALLOW_LOGIN"] = "1"
    try:
        otp_timeout = int(options.get("otp_timeout") or 180)
    except (TypeError, ValueError):
        otp_timeout = 180
    # 下限 60 秒：Remail 这类聚合转发邮箱实测要 20~30 秒才到
    env["OTP_TIMEOUT"] = str(max(60, otp_timeout))
    # 关掉 Codex 二次收件：短效邮箱（code 模式，一次性）只保留一封邮件，
    # 授权链再发一封验证码就会把唯一那封顶掉 → 死锁
    if not options.get("want_refresh_token"):
        env["OAUTH_SECONDARY_AUTHORIZE_EXCHANGE"] = "0"
        env["SKIP_OAUTH_TOKEN_EXCHANGE"] = "1"
    return env


# ──────────────────────── 号码导入号池 ────────────────────────


def build_pool_payload(email: str, access_token: str, proxy: str = "") -> dict[str, Any]:
    """构造写入号池的账号载荷。

    与 chatgpt2api 的 `access_token----proxy` 导入格式一致：
    access_token 是主键，proxy 是该号专属代理（可为空）。
    """
    from services.storage import register_store as store

    record = store.get_registered(email) or {}
    payload: dict[str, Any] = {
        "access_token": str(access_token or "").strip(),
        "email": str(email or "").strip(),
        "source_type": "register",
    }
    refresh_token = str(record.get("refresh_token") or "").strip()
    if refresh_token:
        payload["refresh_token"] = refresh_token
    id_token = str(record.get("id_token") or "").strip()
    if id_token:
        payload["id_token"] = id_token
    proxy_value = str(proxy or record.get("proxy") or "").strip()
    if proxy_value:
        payload["proxy"] = proxy_value
    return payload


def import_registered_to_pool(
    emails: list[str],
    *,
    with_proxy: bool = True,
    source_type: str = "register",
) -> dict[str, Any]:
    """把已注册的账号批量导入号池。

    with_proxy=True  → 带 `access_token----proxy`（proxy 为空则不带）
    with_proxy=False → 只导入 access_token

    返回 {"total", "imported", "skipped", "missing", "items"}
    """
    from services.account_service import account_service
    from services.storage import register_store as store

    keys = [str(e or "").strip().lower() for e in (emails or []) if str(e or "").strip()]
    if not keys:
        return {"total": 0, "imported": 0, "skipped": 0, "missing": [], "items": []}

    payloads: list[dict[str, Any]] = []
    missing: list[str] = []
    for email in keys:
        record = store.get_registered(email)
        if not record:
            missing.append(email)
            continue
        access_token = str(record.get("access_token") or "").strip()
        if not access_token:
            missing.append(email)
            continue
        payload = {
            "access_token": access_token,
            "email": email,
            "source_type": source_type,
        }
        refresh_token = str(record.get("refresh_token") or "").strip()
        if refresh_token:
            payload["refresh_token"] = refresh_token
        id_token = str(record.get("id_token") or "").strip()
        if id_token:
            payload["id_token"] = id_token
        if with_proxy:
            proxy_value = str(record.get("proxy") or "").strip()
            if proxy_value:
                payload["proxy"] = proxy_value
        payloads.append(payload)

    if not payloads:
        return {
            "total": len(keys),
            "imported": 0,
            "skipped": 0,
            "missing": missing,
            "items": [],
        }

    result = account_service.add_account_items(payloads)
    return {
        "total": len(keys),
        "imported": int(result.get("added") or 0),
        "skipped": int(result.get("skipped") or 0),
        "missing": missing,
        "items": result.get("items") or [],
    }


def import_token_text_to_pool(
    text: str,
    *,
    source_type: str = "register",
) -> dict[str, Any]:
    """按 `access_token----proxy` 文本格式直接导入号池。

    这是 chatgpt2api 原生的导入格式（注册机导出格式也用它），
    在这里复用，保证「注册机导出 → chatgpt2api 导入」的格式永远一致。
    """
    from services.account_service import account_service

    tokens: list[str] = []
    payloads: list[dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 第一个 ---- 之前是 access_token，之后整段是该号专属代理
        parts = line.split("----", 1)
        access_token = parts[0].strip()
        if not access_token:
            continue
        payload: dict[str, Any] = {
            "access_token": access_token,
            "source_type": source_type,
        }
        if len(parts) > 1:
            proxy_value = parts[1].strip()
            if proxy_value:
                payload["proxy"] = proxy_value
        payloads.append(payload)
        tokens.append(access_token)

    if not payloads:
        return {"total": 0, "imported": 0, "skipped": 0, "items": []}

    result = account_service.add_account_items(payloads)
    return {
        "total": len(tokens),
        "imported": int(result.get("added") or 0),
        "skipped": int(result.get("skipped") or 0),
        "items": result.get("items") or [],
    }


# ──────────────────────── 单次注册 ────────────────────────


def _set_run(run_id: str, **updates: Any) -> None:
    with _runs_lock:
        run = _runs.get(run_id)
        if run is None:
            return
        run.update(updates)
        run["updated_at"] = time.time()


def _emit(run_id: str, message: str, level: str = "info") -> None:
    sink = _log_sinks.get(run_id)
    if sink is not None:
        try:
            sink(message, level)
        except Exception:
            pass


def register_one(options: dict[str, Any]) -> dict[str, Any]:
    """执行一次注册（同步）。

    返回 {"ok", "run_id", "email", "access_token", "error", "error_category", "imported"}
    """
    from services.register.auth_flow import AuthFlow
    from services.register.mail_providers import (
        create_mail_provider,
        get_provider_class,
    )
    from services.storage import register_store as store

    run_id = str(options.get("run_id") or uuid.uuid4().hex)
    started = time.time()

    with _runs_lock:
        _runs[run_id] = {
            "run_id": run_id,
            "status": "running",
            "started_at": started,
            "updated_at": started,
            "email": "",
            "error": "",
            "error_category": "",
        }

    store.create_run(run_id, email="", log_path="")

    proxy = str(options.get("proxy") or "").strip()
    want_refresh = bool(options.get("want_refresh_token", False))
    auto_import = bool(options.get("auto_import", False))
    with_proxy = bool(options.get("with_proxy", True))

    mail_provider = None
    try:
        # 邮箱来源：优先从号池 claim 一个号（pooled 类 provider），
        # 否则用 provider 自己造号（Remail 下单 / CF 临时邮箱）
        mail_source = str(options.get("mail_source") or _reg_setting("mail_source", "outlook"))
        settings = dict(options.get("mail_options") or _load_mail_settings(mail_source))
        account = None
        try:
            provider_class = get_provider_class(mail_source)
        except Exception:
            provider_class = None
        if provider_class is not None and getattr(provider_class, "pooled", False):
            # 池化来源：从号池原子 claim 一个号
            account = store.claim_next(mail_source)
            if account is None:
                raise RuntimeError(f"号池里没有可用的 {mail_source} 邮箱")
        mail_provider = create_mail_provider(mail_source, settings, account)

        cfg = _build_config(options)
        env_overrides = _build_env_overrides(options)

        flow = AuthFlow(
            cfg,
            env_overrides=env_overrides,
            on_password=lambda email, password: store.save_password_early(email, password),
        )
        result = flow.run_register(mail_provider)

        email = str(result.email or "").strip().lower()
        access_token = str(result.access_token or "").strip()
        _set_run(run_id, email=email, status="done")

        store.save_registered(
            email=email,
            access_token=access_token,
            session_token=str(result.session_token or ""),
            refresh_token=str(result.refresh_token or ""),
            id_token=str(result.id_token or ""),
            password=str(result.password or ""),
            device_id=str(result.device_id or ""),
            csrf_token=str(result.csrf_token or ""),
            cookie_header=str(result.cookie_header or ""),
            totp_secret=str(result.totp_secret or ""),
            session_json=str(result.session_json or ""),
            proxy=proxy,
        )
        store.finish_run(run_id, ok=True, email=email)

        imported = False
        if auto_import and access_token:
            # 自动入池：成功即刻写入 chatgpt2api 号池
            payload = build_pool_payload(email, access_token, proxy)
            from services.account_service import account_service

            account_service.add_account_items([payload])
            imported = True
            _emit(run_id, f"✅ 已自动导入号池：{email}", "ok")

        return {
            "ok": True,
            "run_id": run_id,
            "email": email,
            "access_token": access_token,
            "proxy": proxy,
            "imported": imported,
            "duration": time.time() - started,
        }
    except Exception as exc:
        error_text = str(exc) or exc.__class__.__name__
        category = classify_error(error_text)
        _set_run(run_id, status="failed", error=error_text, error_category=category)
        store.finish_run(run_id, ok=False, error=error_text, error_category=category)
        logger.warning("[register] 注册失败 (%s): %s", category, error_text)
        return {
            "ok": False,
            "run_id": run_id,
            "email": "",
            "error": error_text,
            "error_category": category,
            "duration": time.time() - started,
        }
    finally:
        if mail_provider is not None:
            close = getattr(mail_provider, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def classify_error(err: str, mail_source: str = "") -> str:
    """错误分类（与注册机 registrar.classify_error 保持一致）。

    分类决定后续处置：
      - network / account → 可以重试
      - cf_blocked         → 换代理，不触发整批熔断
      - mail_limited       → 邮箱侧限制，不再复用该邮箱
    """
    text = str(err or "").lower()
    if "invalid_state" in text or "http 409" in text:
        return "account"
    if "cloudflare" in text or "403" in text or "cf_blocked" in text:
        return "cf_blocked"
    if "otp" in text or "验证码" in text or "timeout" in text or "超时" in text:
        return "mail_limited"
    if "timeout" in text or "timed out" in text or "connection" in text or "ssl" in text:
        return "network"
    if "proxy" in text or "代理" in text:
        return "network"
    return "unknown"


def get_run(run_id: str) -> dict[str, Any]:
    with _runs_lock:
        return dict(_runs.get(run_id) or {})


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    from services.storage import register_store as store

    items, _count = store.list_runs(limit=limit)
    return items


def register_status() -> dict[str, Any]:
    """注册业务总览。"""
    from services.storage import register_store as store

    with _runs_lock:
        running = sum(1 for r in _runs.values() if r.get("status") == "running")
    stats = store.stats()
    return {**stats, "running": running}

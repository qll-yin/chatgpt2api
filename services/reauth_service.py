"""重新授权（刷新 401）—— 用账号已有的密码 / 2FA 重新走一次授权，拿一份新的凭证。

为什么需要这个
--------------
Codex 的 refresh_token 是**单次使用 + 轮换制**：每次刷新服务端都会把旧的
立即作废。所以同一份配置一旦被导出到别的池（CPA / sub2api），两个池共用
一个 RT，谁先刷新谁活、另一个立刻 401 invalid_grant。

另外 OAuth grant 绑定 client_id：codex 的 RT 只能用 Codex CLI 的 client
（app_EMoamEEZ73f0CkXaXp7hrann）刷新，用 platform 的 client 刷必然
invalid_grant。

所以「凭证失效」只能靠**重新走一次授权**来恢复 —— 这就是本模块做的事。

已有能力的复用
--------------
注册流程（services/register/auth_flow.py）里已经具备完整能力：
  · run_protocol_login()      登录已有账号（密码 → TOTP 2FA → 必要时邮箱码）
  · oauth_codex_rt_exchange()  用 Codex client 走 PKCE 换新的 refresh_token
  · get_auth_session()         拉 /api/auth/session 拿 session_json
  · _account_callback          从库里取 password / totp_secret
本模块只是把它们按「刷新 401」的顺序串起来。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Codex CLI 的公开 OAuth client —— 与 auth_flow._build_codex_authorize 保持一致
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class _NoMailProvider:
    """占位邮箱 provider。

    重新授权**不提供**邮箱来源：如果账号的登录链路需要邮箱验证码，
    这里直接抛一个说得清楚的错，而不是让流程干等 180 秒。
    （注册时勾了「设置密码」的号走的是密码 + TOTP 分支，用不到这里。）
    """

    kind = "none"
    display_name = "无"
    pooled = False
    ephemeral = False
    ephemeral_mailbox = False
    accepts_existing_account = True
    config_fields: list = []

    def create_mailbox(self) -> str:
        return ""

    def mark_dead(self, email_addr: str, reason: str = "") -> None:
        return None

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: float | None = None,
        earliest: float | None = None,
    ) -> str:
        raise RuntimeError(
            "该账号的登录链路要求邮箱验证码，但「刷新 401」不提供邮箱来源。"
            "请确认这个号在注册时设置过密码（密码 + 2FA 就能自动过），"
            "或换用其它方式登录后再重新导入。"
        )


def _build_account_callback():
    """给 AuthFlow 的凭证回调 —— 从注册结果表里取密码 / TOTP secret。"""

    def _callback(email: str) -> dict:
        try:
            from services.storage import register_store as store

            record = store.get_registered(email) or {}
            return {
                "password": str(record.get("password") or ""),
                "totp_secret": str(record.get("totp_secret") or ""),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取账号凭证异常: %s", exc)
            return {}

    return _callback


def reauthorize_registered_account(
    email: str,
    *,
    proxy: str = "",
    timeout: int = 300,
) -> dict[str, Any]:
    """对注册结果里的账号重新授权。

    参数
    ----
    email   要刷新的账号邮箱（必须在注册结果表里）
    proxy   本次授权走的出口代理（可选）。空 = 沿用账号原来记的代理。
    timeout 单次尝试的上限（秒），超时返回失败，不挂死。

    返回
    ----
    {"ok": bool, "email": str, "error": str, "codex_refresh_token": bool,
     "has_access_token": bool, "has_refresh_token": bool,
     "has_session": bool, "proxy": str, "pool_updated": bool}
    """
    from services.register.config import Config
    from services.register.auth_flow import AuthFlow
    from services.storage import register_store as store

    email = str(email or "").strip().lower()
    if not email:
        return {"ok": False, "email": "", "error": "邮箱不能为空"}

    record = store.get_registered(email)
    if not record:
        return {"ok": False, "email": email, "error": "注册结果里没有这个账号"}

    # 代理：用户指定的优先，否则沿用账号原来记的那个
    used_proxy = str(proxy or "").strip() or str(record.get("proxy") or "").strip()

    was_codex = bool(record.get("refresh_token")) and _record_is_codex(record)

    cfg = Config(proxy=used_proxy or None)
    flow = AuthFlow(
        cfg,
        env_overrides={
            # 走「登录已有账号」分支
            "WEBUI_ALLOW_LOGIN": "1",
            # 强制走 Codex 授权拿 codex 的 refresh_token
            "OAUTH_CODEX_RT_EXCHANGE": "1",
            # ⚠️ 不开 secondary —— 它换的是 platform 的 RT。
            #    对已经是 codex 的号，拿 platform RT 顶掉 codex RT 是倒退。
            "OAUTH_SECONDARY_AUTHORIZE_EXCHANGE": "0",
            "OTP_TIMEOUT": "120",
        },
        account_callback=_build_account_callback(),
    )

    # 把库里存的凭证先灌进去（少一次数据库往返，也保证 TOTP secret 可用）
    flow.result.email = email
    stored_password = str(record.get("password") or "").strip()
    if stored_password:
        flow.result.password = stored_password
    stored_totp = str(record.get("totp_secret") or "").strip()
    if stored_totp:
        flow.result.totp_secret = stored_totp
    stored_device = str(record.get("device_id") or "").strip()
    if stored_device:
        flow.result.device_id = stored_device

    started = time.time()
    try:
        result = flow.run_protocol_login(_NoMailProvider(), email, stored_password)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[reauth] %s 重新授权失败: %s", email, exc)
        return {
            "ok": False,
            "email": email,
            "error": str(exc) or exc.__class__.__name__,
            "proxy": used_proxy,
            "elapsed": round(time.time() - started, 1),
        }

    access_token = str(getattr(result, "access_token", "") or "").strip()
    refresh_token = str(getattr(result, "refresh_token", "") or "").strip()
    id_token = str(getattr(result, "id_token", "") or "").strip()
    session_json = str(getattr(result, "session_json", "") or "").strip()
    session_token = str(getattr(result, "session_token", "") or "").strip()
    cookie_header = str(getattr(result, "cookie_header", "") or "").strip()
    got_codex_rt = bool(getattr(result, "codex_refresh_token", False))

    if not access_token and not refresh_token:
        return {
            "ok": False,
            "email": email,
            "error": "授权流程走完了但没拿到任何凭证",
            "proxy": used_proxy,
            "elapsed": round(time.time() - started, 1),
        }

    # 已经是 codex 的号，却只换到 platform 的 RT —— 不能让它顶掉 codex RT
    if was_codex and not got_codex_rt:
        keep_rt = str(record.get("refresh_token") or "").strip()
        refresh_token = keep_rt

    payload: dict[str, Any] = {
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "session_token": session_token,
        "cookie_header": cookie_header,
        "session_json": session_json,
    }
    if got_codex_rt:
        payload["codex_refresh_token"] = True

    store.save_registered(payload)

    pool_updated = _sync_to_pool(email, access_token, refresh_token, id_token)

    return {
        "ok": True,
        "email": email,
        "error": "" if got_codex_rt or not was_codex else "只换到了普通 access_token，codex 的 refresh_token 没刷出来",
        "codex_refresh_token": got_codex_rt,
        "has_access_token": bool(access_token),
        "has_refresh_token": bool(refresh_token),
        "has_session": bool(session_json),
        "proxy": used_proxy,
        "pool_updated": pool_updated,
        "elapsed": round(time.time() - started, 1),
    }


def _record_is_codex(record: dict) -> bool:
    extra = record.get("extra")
    if extra is None:
        # get_registered 会同时给 extra（已解析）和 extra_json（原文），
        # 直接拿原始行时只有 extra_json —— 两种都认。
        raw = record.get("extra_json")
        if raw:
            try:
                extra = json.loads(raw)
            except Exception:
                extra = {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    if isinstance(extra, dict) and extra.get("codex_refresh_token"):
        return True
    return str(record.get("source_type") or "").strip().lower() == "codex"


def _sync_to_pool(email: str, access_token: str, refresh_token: str, id_token: str) -> bool:
    """把新凭证同步到号池里同一个邮箱的账号。

    号池以 access_token 为主键，新旧 token 不同 → 要走「换 token」那条路
    （保留旧 token 的别名映射、迁移在途任务计数），不能直接 update。
    """
    if not access_token:
        return False
    try:
        from services.account_service import account_service

        existing = account_service.get_account_by_email(email)
        if not existing:
            return False
        old_token = str(existing.get("access_token") or "").strip()
        if not old_token or old_token == access_token:
            account_service.update_account(
                old_token,
                {
                    "refresh_token": refresh_token,
                    "id_token": id_token,
                    "status": "正常",
                    "last_token_refresh_error": None,
                    "last_token_refresh_error_at": None,
                },
            )
            return True
        account_service._apply_refreshed_tokens(
            old_token,
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
            },
            "reauthorize",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[reauth] 同步到号池失败 %s: %s", email, exc)
        return False

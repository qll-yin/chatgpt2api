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

import json
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


def _is_codex_record(record: dict | None) -> bool:
    """这条注册记录的 refresh_token 是不是 Codex CLI 的 client 铸的。

    ⚠️ 判断依据只能是「哪个 client 换出来的」（注册时记在 extra 里），
    不能靠猜 —— OAuth grant 绑定 client_id，用错了刷新一定 invalid_grant。
    """
    if not record:
        return False
    extra = record.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    if isinstance(extra, dict) and extra.get("codex_refresh_token"):
        return True
    return str(record.get("source_type") or "").strip().lower() == "codex"


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
        # codex 来源要标出来 —— 号池刷新时按它选 OAuth client_id，
        # 标错会导致 refresh_token 一刷就 invalid_grant。
        "source_type": "codex" if _is_codex_record(record) else "register",
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


def import_to_pool_with_refresh(
    payloads: list[dict[str, Any]],
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    """写入号池，并按号池管理的同一套流程刷新额度。

    ⚠️ 为什么必须刷新
    ----------------
    只调 `add_account_items` 的话，号是进去了，但**额度是 0** ——
    因为额度是 refresh 时从 OpenAI 拉的。号池管理页的导入接口
    （`POST /api/accounts`）都是「入池 + refresh_accounts」两步，
    注册结果导入号池必须走同一套流程，否则用户还得手动点一次刷新。

    refresh=False 用于批量注册过程中的高频入池 —— 那时账号量大，
    逐个刷新会拖慢 worker；额度可以在批量结束后统一刷。
    """
    from services.account_service import account_service

    if not payloads:
        return {"imported": 0, "skipped": 0, "refreshed": 0, "errors": [], "items": []}

    result = account_service.add_account_items(payloads)
    out: dict[str, Any] = {
        "imported": int(result.get("added") or 0),
        "skipped": int(result.get("skipped") or 0),
        "refreshed": 0,
        "errors": [],
        "items": result.get("items") or [],
    }
    if not refresh:
        return out

    tokens = [
        str(p.get("access_token") or "").strip()
        for p in payloads
        if str(p.get("access_token") or "").strip()
    ]
    if tokens:
        try:
            refresh_result = account_service.refresh_accounts(tokens)
            out["refreshed"] = int(refresh_result.get("refreshed") or 0)
            out["errors"] = refresh_result.get("errors") or []
            if refresh_result.get("items"):
                out["items"] = refresh_result.get("items")
        except Exception as exc:  # noqa: BLE001
            # 刷新失败不影响「号已入池」这个事实
            out["errors"] = [str(exc)]
    return out


def import_registered_to_pool(
    emails: list[str],
    *,
    with_proxy: bool = True,
    source_type: str = "register",
    refresh: bool = True,
    bind_proxy: str | None = None,
    auto_bind: bool = False,
) -> dict[str, Any]:
    """把已注册的账号批量导入号池。

    with_proxy=True  → 带 `access_token----proxy`（proxy 为空则不带）
    with_proxy=False → 只导入 access_token

    ★ bind_proxy / auto_bind —— 号池绑定静态代理
    ---------------------------------------------
    注册时用的是**动态代理**（出口 IP 每次都变，防注册风控）。但账号导入
    号池后要长期使用，IP 不能每次都变 —— 否则同一账号在多个出口之间跳变，
    反而更容易触发使用侧风控。

    所以导入时改绑**静态代理**：
      - bind_proxy="http://..." → 全部绑到这一个静态代理
      - auto_bind=True           → 从静态代理池里自动挑（绑得少的优先），
                                   每个代理受 bind_max_uses 限制
      - 两者都不给              → 保持原行为（沿用注册时的代理）

    绑定会占用代理的绑定配额（bind_used_count）；名额不够时该账号跳过，
    不会硬绑超额。

    返回 {"total", "imported", "skipped", "missing", "items", "bind_stats"}
    """
    from services.storage import register_store as store

    keys = [str(e or "").strip().lower() for e in (emails or []) if str(e or "").strip()]
    if not keys:
        return {
            "total": 0, "imported": 0, "skipped": 0, "missing": [], "items": [],
            "bind_stats": {"bound": 0, "skipped_no_quota": 0, "no_proxy": 0},
        }

    payloads: list[dict[str, Any]] = []
    missing: list[str] = []
    # 实际绑上代理的记录 —— 导入失败时要还回名额
    bound: list[str] = []
    bind_stats = {"bound": 0, "skipped_no_quota": 0, "no_proxy": 0}

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
            # codex 来源自动纠正 —— 号池刷新时按 source_type 选 OAuth
            # client_id，标错会导致 refresh_token 一刷就 invalid_grant。
            "source_type": "codex" if _is_codex_record(record) else source_type,
        }
        refresh_token = str(record.get("refresh_token") or "").strip()
        if refresh_token:
            payload["refresh_token"] = refresh_token
        id_token = str(record.get("id_token") or "").strip()
        if id_token:
            payload["id_token"] = id_token

        # ── 绑定静态代理（优先于注册时的动态代理）────────────────
        assigned_proxy = ""
        if bind_proxy:
            assigned_proxy = str(bind_proxy).strip()
            # 指定代理也要占配额 —— 超额就跳过这个账号
            if not store.bind_proxy(assigned_proxy):
                bind_stats["skipped_no_quota"] += 1
                assigned_proxy = ""
        elif auto_bind:
            assigned_proxy = store.pick_bind_proxy()
            if not assigned_proxy:
                bind_stats["skipped_no_quota"] += 1

        if assigned_proxy:
            payload["proxy"] = assigned_proxy
            bound.append(assigned_proxy)
            bind_stats["bound"] += 1
        elif with_proxy:
            # 没绑到静态代理 → 回落到注册时用的代理（通常是动态代理）
            proxy_value = str(record.get("proxy") or "").strip()
            if proxy_value:
                payload["proxy"] = proxy_value
                bind_stats["no_proxy"] += 1
        payloads.append(payload)

    if not payloads:
        return {
            "total": len(keys),
            "imported": 0,
            "skipped": 0,
            "missing": missing,
            "items": [],
            "bind_stats": bind_stats,
        }

    result = import_to_pool_with_refresh(payloads, refresh=refresh)
    imported = int(result.get("imported") or 0)
    # 导入失败的账号要把名额还回去 —— 否则配额被"幽灵账号"占着
    if imported < len(payloads) and bound:
        # import_to_pool_with_refresh 的 items 里有每个账号的结果，
        # 这里按成功数粗略还：宁可少还也别多占（多占会让代理提前耗尽）
        release_count = len(bound) - imported
        for proxy in bound[:max(0, release_count)]:
            store.release_bind_proxy(proxy)

    return {
        "total": len(keys),
        "imported": imported,
        "skipped": int(result.get("skipped") or 0),
        "missing": missing,
        "refreshed": int(result.get("refreshed") or 0),
        "errors": result.get("errors") or [],
        "items": result.get("items") or [],
        "bind_stats": bind_stats,
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

    result = import_to_pool_with_refresh(payloads)
    return {
        "total": len(tokens),
        "imported": int(result.get("imported") or 0),
        "skipped": int(result.get("skipped") or 0),
        "refreshed": int(result.get("refreshed") or 0),
        "errors": result.get("errors") or [],
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
    """推一条日志到该 run 的日志队列（前端 SSE 会读到）。"""
    from services import register_registrar as registrar

    q = registrar.get_run_queue(run_id)
    if q is not None:
        try:
            q.put_nowait(f"[{level}] {message}")
        except Exception:
            pass


def _publish(run_id: str, kind: str, payload: dict | None = None) -> None:
    """推一个结构化事件（进度、状态）。"""
    import json as _json

    from services import register_registrar as registrar

    q = registrar.get_run_queue(run_id)
    if q is None:
        return
    body = payload if isinstance(payload, dict) else {"message": str(payload)}
    body["kind"] = kind
    try:
        q.put_nowait("__EVENT__:" + _json.dumps(body, ensure_ascii=False))
    except Exception:
        pass


def register_one(options: dict[str, Any]) -> dict[str, Any]:
    """执行一次注册（同步等待完成）。

    实现复用迁移过来的 registrar（services/register_registrar.py），
    那里是原注册机的完整注册逻辑 —— 不重复实现。

    返回 {"ok", "run_id", "email", "access_token", "error", "error_category", "imported"}
    """
    from services import register_registrar as registrar
    from services.register.mail_providers import get_provider_class
    from services.storage import register_store as store

    started = time.time()

    # ── 准备邮箱 ──
    mail_source = str(options.get("mail_source") or _reg_setting("mail_source", "outlook"))
    settings = dict(options.get("mail_options") or _load_mail_settings(mail_source))
    # account 已由调用方（如批量 worker）认领时直接复用，避免二次 claim
    account = options.get("account")
    if account is None:
        try:
            provider_class = get_provider_class(mail_source)
        except Exception:
            provider_class = None
        if provider_class is not None and getattr(provider_class, "pooled", False):
            account = store.claim_next(mail_source)
            if account is None:
                raise RuntimeError(f"号池里没有可用的 {mail_source} 邮箱")
    if account is None:
        # 非池化来源（Remail / CF 临时邮箱）由 provider 自己造号
        account = {
            "email": "",
            "password": "",
            "client_id": "",
            "refresh_token": "",
            "relay_url": "",
            "kind": mail_source,
        }

    run_options = dict(options)
    run_options["mail_source"] = mail_source
    run_options["mail_options"] = settings

    # 启动注册（异步线程），返回 run_id
    run_id = registrar.start_registration(account, run_options)

    # 等它跑完
    ok, category = _wait_run(run_id, timeout=int(options.get("timeout") or 1800))

    # ⚠️ 邮箱要用**注册成功后的真实邮箱**，不能直接用 account 里的：
    #    非池化来源（Remail / CF 临时邮箱）的 account["email"] 是占位地址
    #    （remail_placeholder_xxx@placeholder.local），真实邮箱只写在
    #    运行记录（registrar 成功后回写）里。用占位邮箱查凭证 → 查不到 →
    #    号注册出来了却没入池（用户实测：注册结果里有，号池里没有）。
    run_row = store.get_run(run_id) or {}
    real_email = str(
        run_row.get("email") or account.get("email") or ""
    ).strip().lower()
    if "placeholder" in real_email:
        real_email = ""
    record = store.get_registered(real_email) or {}
    access_token = str(record.get("access_token") or "").strip()
    email = str(record.get("email") or real_email or account.get("email") or "").strip().lower()

    imported = False
    if ok and access_token and bool(options.get("auto_import", False)):
        payload = build_pool_payload(email, access_token, str(options.get("proxy") or ""))
        # 「入池时携带代理」关掉时，即使注册记录里存了代理也不带进号池
        if not options.get("with_proxy", True):
            payload.pop("proxy", None)
        # 单次注册只有一个号，直接带刷新（用户想立刻看到额度）
        result = import_to_pool_with_refresh([payload])
        imported = bool(result.get("imported"))

    return {
        "ok": ok,
        "run_id": run_id,
        "email": email,
        "access_token": access_token,
        "proxy": str(options.get("proxy") or ""),
        "imported": imported,
        "error": "" if ok else _last_run_error(run_id),
        "error_category": category,
        "duration": time.time() - started,
    }


def _wait_run(run_id: str, timeout: int = 1800) -> tuple[bool, str]:
    """等一个 run 跑完，返回 (ok, error_category)。"""
    # ⚠️ 这里必须自己 import —— 本模块的 store 是**函数内局部导入**的，
    #    模块级没有这个名字。漏了这行会 NameError：
    #    单次注册接口直接 500（实测就是这个坑）。
    from services.storage import register_store as store

    deadline = time.time() + max(10, timeout)
    while time.time() < deadline:
        row = store.get_run(run_id)
        if row:
            status = row.get("status")
            if status == "done":
                return True, ""
            if status == "failed":
                return False, str(row.get("error_category") or "")
        time.sleep(0.5)
    return False, ""


def _last_run_error(run_id: str) -> str:
    from services.storage import register_store as store

    row = store.get_run(run_id) or {}
    return str(row.get("error") or "")


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

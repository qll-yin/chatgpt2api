"""
注册业务的数据访问层（SQLAlchemy 实现）。

对应 gpt-outlook-register 的 `webui/db.py`，但：
1. 底层从原生 sqlite3 换成 SQLAlchemy（复用主项目的连接池与方言适配）
2. 表名加 `reg_` 前缀，与主业务表隔离
3. **保留全部原有语义**，尤其是：
   - 代理「选中即预占」——不预占的话并发 worker 会同时挑中同一个代理
   - 失败「不占配额」——只加 failed_count
   - 邮箱 claim 是原子的——并发抢号不会拿到同一个

并发安全
--------
所有写操作走数据库事务；「原子 claim」用
`UPDATE ... WHERE status='available' RETURNING`（PG/SQLite 3.35+）或
「先 SELECT 再条件 UPDATE + 影响行数判定」的兼容写法实现，
保证多个 worker 并发时只有一个能 claim 到。
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update as sa_update

from services.storage.db import get_engine, register_session_scope
from services.storage.register_models import (
    OutlookAccountModel,
    ProxyPoolModel,
    RegisterSettingModel,
    RegisteredModel,
    RemailOrderModel,
    RunModel,
    create_register_tables,
)

# 建表守卫：与 runtime_store 同理 —— 注册服务在模块导入阶段就可能访问数据库。
_register_tables_ready = False


def ensure_register_tables() -> None:
    """确保注册业务表已创建（幂等）。"""
    global _register_tables_ready
    if _register_tables_ready:
        return
    create_register_tables(get_engine())
    _register_tables_ready = True


# ──────────────────────── 设置 ────────────────────────


def get_setting(key: str, default: str = "") -> str:
    with register_session_scope() as session:
        row = session.execute(
            select(RegisterSettingModel.value).where(RegisterSettingModel.key == key)
        ).scalar_one_or_none()
        return row if row is not None else default


def set_setting(key: str, value: Any) -> None:
    with register_session_scope() as session:
        stmt = (
            sa_update(RegisterSettingModel)
            .where(RegisterSettingModel.key == key)
            .values(value=str(value))
        )
        result = session.execute(stmt)
        if not result.rowcount:
            session.add(RegisterSettingModel(key=key, value=str(value)))


def get_settings(prefix: str = "") -> dict[str, str]:
    with register_session_scope() as session:
        stmt = select(RegisterSettingModel)
        if prefix:
            stmt = stmt.where(RegisterSettingModel.key.like(f"{prefix}%"))
        return {row.key: (row.value or "") for row in session.execute(stmt).scalars()}


def _is_true(value: object) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def get_mail_settings() -> dict:
    """内部用：给 create_mail_provider 的 settings（含明文密钥）。

    绝不能直接返回给前端 —— 里面有各邮箱平台的 API Key。
    按 provider 自己声明的 config_fields 逐项取，加一种邮箱不用改这里。
    """
    from services.register.mail_providers import list_providers

    out: dict[str, Any] = {"mail_source": get_setting("mail_source", "outlook")}
    for provider in list_providers():
        for field in provider.get("config_fields") or []:
            key = field.get("key")
            if not key:
                continue
            out[key] = get_setting(key, "")
    return out


def get_mail_config() -> dict[str, Any]:
    """返回邮箱来源配置（密码类字段脱敏成 '***'）。

    遍历**所有** provider 声明的 config_fields —— 前端一次拿到所有邮箱的
    配置项，切换来源时不用再请求一次。
    """
    from services.register.mail_providers import list_providers

    out: dict[str, Any] = {"mail_source": get_setting("mail_source", "outlook")}
    for provider in list_providers():
        for field in provider.get("config_fields") or []:
            key = field.get("key")
            if not key:
                continue
            if field.get("type") == "password":
                out[key] = "***" if get_setting(key) else ""
            else:
                out[key] = get_setting(key, "")
    return out


def save_mail_config(data: dict[str, Any]) -> None:
    """保存邮箱配置。

    password 类字段传空或 '***' 表示**不修改**（前端回填的就是掩码，
    直接保存会把真值覆盖掉）。

    mail_source 校验查 provider 注册表 —— 未知来源直接抛错，
    避免出现「保存成功但选择没生效」的静默失败。
    """
    from services.register.mail_providers import get_provider_class, list_providers

    if "mail_source" in data:
        src = str(data["mail_source"]).strip().lower()
        get_provider_class(src)  # 未注册的 kind 会抛错
        set_setting("mail_source", src)

    for provider in list_providers():
        for field in provider.get("config_fields") or []:
            key = field.get("key")
            if not key or key not in data:
                continue
            val = data[key]
            if field.get("type") == "password":
                if not val or val == "***":
                    continue  # 没填 / 是掩码 → 保持原值
            set_setting(key, str(val).strip())


def get_sms_internal_config() -> dict:
    """内部用：拿明文 sms_api_key，供 sms_provider 实例化使用。"""
    return {
        "sms_enabled":            _is_true(get_setting("sms_enabled", "0")),
        "sms_provider":           get_setting("sms_provider", "smsbower"),
        "sms_api_key":            get_setting("sms_api_key", ""),
        "sms_country":            get_setting("sms_country", "52"),
        "sms_service":            get_setting("sms_service", "dr"),
        "sms_max_price":          get_setting("sms_max_price", ""),
        "sms_fixed_price":        get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":        _is_true(get_setting("sms_reuse_phone", "0")),
        "sms_phone_success_max":  get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":       _is_true(get_setting("sms_auto_country", "0")),
        "sms_strict_whitelist":   _is_true(get_setting("sms_strict_whitelist", "0")),
        "sms_allowed_countries":  get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":     get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":     get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts": get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":  get_setting("sms_per_phone_timeout", "80"),
    }


def get_export_internal_config() -> dict:
    """内部用：拿明文密钥 + 解析后的 enabled 布尔。

    ⚠️ CPA / SUB2API 导出在本项目**默认不启用**（那两个平台要求 Codex
    风格的 access_token，与本项目号池消费的 NextAuth 风格 token 不是一回事）。
    这里保留读取能力是为了兼容既有配置，enabled 默认为关，不会有任何行为。
    """
    cpa = {
        "enabled":      _is_true(get_setting("export_cpa_enabled", "0")),
        "cpa_url":      get_setting("export_cpa_url", ""),
        "cpa_mgmt_key": get_setting("export_cpa_mgmt_key", ""),
        "cpa_timeout":  get_setting("export_cpa_timeout", "30"),
    }
    sub2api = {
        "enabled":           _is_true(get_setting("export_sub2api_enabled", "0")),
        "sub2api_url":       get_setting("export_sub2api_url", ""),
        "sub2api_api_key":   get_setting("export_sub2api_api_key", ""),
        "sub2api_group_ids": get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":   get_setting("export_sub2api_timeout", "30"),
    }
    return {"cpa": cpa, "sub2api": sub2api}


# ──────────────────────── 邮箱池 ────────────────────────


def _account_to_dict(row) -> dict[str, Any]:
    return {
        "email": row.email,
        "password": row.password or "",
        "client_id": row.client_id or "",
        "refresh_token": row.refresh_token or "",
        "relay_url": row.relay_url or "",
        "kind": row.kind or "outlook",
        "status": row.status or "available",
        "imported_at": row.imported_at,
        "claimed_at": row.claimed_at,
        "finished_at": row.finished_at,
        "fail_reason": row.fail_reason or "",
    }


def import_accounts(text: str, kind: str = "") -> dict[str, Any]:
    """批量导入邮箱号池。已存在的 email 仅在凭证变化时更新。"""
    rows = parse_lines(text, kind)
    now = time.time()
    inserted = updated = skipped = 0
    with register_session_scope() as session:
        for r in rows:
            email = str(r.get("email") or "").strip().lower()
            if not email:
                continue
            row_kind = r.get("kind") or kind or "outlook"
            existing = session.execute(
                select(OutlookAccountModel).where(OutlookAccountModel.email == email)
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    OutlookAccountModel(
                        email=email,
                        password=r.get("password", "") or "",
                        client_id=r.get("client_id", "") or "",
                        refresh_token=r.get("refresh_token", "") or "",
                        relay_url=r.get("relay_url", "") or "",
                        kind=row_kind,
                        status="available",
                        imported_at=now,
                    )
                )
                inserted += 1
                continue
            # 已存在：仅在凭证变化时更新（不重置状态，避免打断进行中的注册）
            changed = False
            for field in ("password", "client_id", "refresh_token", "relay_url"):
                new_value = r.get(field, "") or ""
                if new_value and getattr(existing, field) != new_value:
                    setattr(existing, field, new_value)
                    changed = True
            if changed:
                updated += 1
            else:
                skipped += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "total": len(rows)}


def claim_next(kind: str = "") -> Optional[dict]:
    """原子 claim 任一 available 号。

    kind 指定 → 只从该类型里挑（"选了 gmail 就只跑 gmail 号"）
    kind 为空 → 全池子里挑最早导入的

    原子性实现：先按顺序取候选，再对每个候选做**条件 UPDATE**
    （WHERE status='available'），用影响行数判断是否抢到。
    并发时只有一个 worker 能把 status 改成 in_use。
    """
    k = (kind or "").strip().lower()
    with register_session_scope() as session:
        stmt = select(OutlookAccountModel).where(
            OutlookAccountModel.status == "available"
        )
        if k:
            stmt = stmt.where(OutlookAccountModel.kind == k)
        stmt = stmt.order_by(OutlookAccountModel.imported_at.asc()).limit(50)
        candidates = list(session.execute(stmt).scalars())
        for candidate in candidates:
            result = session.execute(
                sa_update(OutlookAccountModel)
                .where(
                    OutlookAccountModel.email == candidate.email,
                    OutlookAccountModel.status == "available",
                )
                .values(status="in_use", claimed_at=time.time())
            )
            if result.rowcount:
                candidate.status = "in_use"
                candidate.claimed_at = time.time()
                return _account_to_dict(candidate)
        return None


def mark_done(email: str) -> None:
    with register_session_scope() as session:
        session.execute(
            sa_update(OutlookAccountModel)
            .where(OutlookAccountModel.email == str(email or "").strip().lower())
            .values(status="done", finished_at=time.time(), fail_reason=None)
        )


def mark_failed(email: str, reason: str = "") -> None:
    with register_session_scope() as session:
        session.execute(
            sa_update(OutlookAccountModel)
            .where(OutlookAccountModel.email == str(email or "").strip().lower())
            .values(
                status="failed",
                finished_at=time.time(),
                fail_reason=str(reason or "")[:500],
            )
        )


def release_unused(email: str) -> None:
    """claim 后没真注册（异常 / 用户取消）→ 还回 available。"""
    with register_session_scope() as session:
        session.execute(
            sa_update(OutlookAccountModel)
            .where(
                OutlookAccountModel.email == str(email or "").strip().lower(),
                OutlookAccountModel.status == "in_use",
            )
            .values(status="available", claimed_at=None)
        )


def delete_accounts(emails: list[str]) -> int:
    if not emails:
        return 0
    keys = [str(e or "").strip().lower() for e in emails if str(e or "").strip()]
    with register_session_scope() as session:
        result = session.execute(
            sa_delete(OutlookAccountModel).where(OutlookAccountModel.email.in_(keys))
        )
        return int(result.rowcount or 0)


def list_accounts(
    status: str = "",
    kind: str = "",
    limit: int = 0,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    with register_session_scope() as session:
        stmt = select(OutlookAccountModel)
        if status:
            stmt = stmt.where(OutlookAccountModel.status == status)
        if kind:
            stmt = stmt.where(OutlookAccountModel.kind == kind)
        count = int(
            session.execute(
                select(func.count()).select_from(stmt.subquery())
            ).scalar_one()
        )
        stmt = stmt.order_by(OutlookAccountModel.imported_at.desc())
        if limit:
            stmt = stmt.limit(limit).offset(offset)
        return [_account_to_dict(row) for row in session.execute(stmt).scalars()], count


def accounts_by_kind() -> dict[str, int]:
    with register_session_scope() as session:
        rows = session.execute(
            select(OutlookAccountModel.kind, func.count())
            .group_by(OutlookAccountModel.kind)
        ).all()
        return {row[0] or "outlook": int(row[1]) for row in rows}


def release_stale(timeout_secs: float = 3600) -> int:
    """把 claim 太久没动静的号还回 available（进程崩溃后的自愈）。"""
    cutoff = time.time() - max(60, timeout_secs)
    with register_session_scope() as session:
        result = session.execute(
            sa_update(OutlookAccountModel)
            .where(
                OutlookAccountModel.status == "in_use",
                OutlookAccountModel.claimed_at.isnot(None),
                OutlookAccountModel.claimed_at < cutoff,
            )
            .values(status="available", claimed_at=None)
        )
        return int(result.rowcount or 0)


def reset_failed_accounts() -> int:
    with register_session_scope() as session:
        result = session.execute(
            sa_update(OutlookAccountModel)
            .where(OutlookAccountModel.status == "failed")
            .values(status="available", fail_reason=None, claimed_at=None, finished_at=None)
        )
        return int(result.rowcount or 0)


def reset_account(email: str) -> bool:
    with register_session_scope() as session:
        result = session.execute(
            sa_update(OutlookAccountModel)
            .where(OutlookAccountModel.email == str(email or "").strip().lower())
            .values(status="available", fail_reason=None, claimed_at=None, finished_at=None)
        )
        return bool(result.rowcount)


# ──────────────────────── 注册结果 ────────────────────────


def save_registered(
    email: str | dict = "",
    access_token: str = "",
    session_token: str = "",
    refresh_token: str = "",
    id_token: str = "",
    password: str = "",
    device_id: str = "",
    csrf_token: str = "",
    cookie_header: str = "",
    totp_secret: str = "",
    totp_factor_id: str = "",
    session_json: str = "",
    proxy: str = "",
    extra: dict | None = None,
) -> None:
    """保存注册成功的账号（覆盖写）。

    支持两种调用方式：
      1. 关键字参数：save_registered(email="a@x.com", access_token="...")
      2. 整字典：save_registered({...})  —— registrar 用这种
    """
    # 方式 2：第一个参数是 dict
    if isinstance(email, dict):
        payload_in = dict(email)
        email = str(payload_in.pop("email", "") or "")
        access_token = str(payload_in.pop("access_token", "") or access_token)
        session_token = str(payload_in.pop("session_token", "") or session_token)
        refresh_token = str(payload_in.pop("refresh_token", "") or refresh_token)
        id_token = str(payload_in.pop("id_token", "") or id_token)
        password = str(payload_in.pop("password", "") or password)
        device_id = str(payload_in.pop("device_id", "") or device_id)
        csrf_token = str(payload_in.pop("csrf_token", "") or csrf_token)
        cookie_header = str(payload_in.pop("cookie_header", "") or cookie_header)
        totp_secret = str(payload_in.pop("totp_secret", "") or totp_secret)
        totp_factor_id = str(payload_in.pop("totp_factor_id", "") or totp_factor_id)
        session_json = str(payload_in.pop("session_json", "") or session_json)
        proxy = str(payload_in.pop("proxy", "") or proxy)
        # 其余字段归入 extra
        if payload_in:
            extra = {**(extra or {}), **payload_in}

    email = str(email or "").strip().lower()
    if not email:
        return
    with register_session_scope() as session:
        existing = session.execute(
            select(RegisteredModel).where(RegisteredModel.email == email)
        ).scalar_one_or_none()
        payload = {
            "access_token": access_token or None,
            "session_token": session_token or None,
            "refresh_token": refresh_token or None,
            "id_token": id_token or None,
            "password": password or None,
            "device_id": device_id or None,
            "csrf_token": csrf_token or None,
            "cookie_header": cookie_header or None,
            "totp_secret": totp_secret or None,
            "totp_factor_id": totp_factor_id or None,
            "session_json": session_json or None,
            "proxy": proxy or None,
            "extra_json": json.dumps(extra, ensure_ascii=False) if extra else None,
            "created_at": time.time(),
        }
        if existing is None:
            session.add(RegisteredModel(email=email, **payload))
        else:
            for key, value in payload.items():
                if value is not None:
                    setattr(existing, key, value)


def save_password_early(email: str, password: str) -> None:
    """密码在 OpenAI 侧一生效就落盘（不等流程跑完）。"""
    email = str(email or "").strip().lower()
    if not email or not password:
        return
    with register_session_scope() as session:
        existing = session.execute(
            select(RegisteredModel).where(RegisteredModel.email == email)
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                RegisteredModel(email=email, password=password, created_at=time.time())
            )
        else:
            existing.password = password


def save_totp_early(email: str, totp_secret: str, totp_factor_id: str) -> None:
    email = str(email or "").strip().lower()
    if not email:
        return
    with register_session_scope() as session:
        existing = session.execute(
            select(RegisteredModel).where(RegisteredModel.email == email)
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                RegisteredModel(
                    email=email,
                    totp_secret=totp_secret or None,
                    totp_factor_id=totp_factor_id or None,
                    created_at=time.time(),
                )
            )
        else:
            if totp_secret:
                existing.totp_secret = totp_secret
            if totp_factor_id:
                existing.totp_factor_id = totp_factor_id


def get_registered(email: str) -> Optional[dict[str, Any]]:
    with register_session_scope() as session:
        row = session.execute(
            select(RegisteredModel).where(
                RegisteredModel.email == str(email or "").strip().lower()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        item = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        if item.get("extra_json"):
            try:
                item["extra"] = json.loads(item["extra_json"])
            except (TypeError, ValueError):
                item["extra"] = {}
        item.pop("extra_json", None)
        return item


def list_registered(limit: int = 0, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    with register_session_scope() as session:
        count = int(
            session.execute(select(func.count()).select_from(RegisteredModel)).scalar_one()
        )
        stmt = select(RegisteredModel).order_by(RegisteredModel.created_at.desc())
        if limit:
            stmt = stmt.limit(limit).offset(offset)
        items = []
        for row in session.execute(stmt).scalars():
            item = {c.name: getattr(row, c.name) for c in row.__table__.columns}
            if item.get("extra_json"):
                try:
                    item["extra"] = json.loads(item["extra_json"])
                except (TypeError, ValueError):
                    item["extra"] = {}
            items.append(item)
        return items, count


def delete_registered(emails: list[str]) -> int:
    if not emails:
        return 0
    keys = [str(e or "").strip().lower() for e in emails if str(e or "").strip()]
    with register_session_scope() as session:
        result = session.execute(
            sa_delete(RegisteredModel).where(RegisteredModel.email.in_(keys))
        )
        return int(result.rowcount or 0)


def update_registered_credentials(email: str, updates: dict[str, Any]) -> bool:
    """更新已注册账号的凭证（补 refresh_token 等）。"""
    email = str(email or "").strip().lower()
    if not email or not updates:
        return False
    allowed = {
        "access_token", "session_token", "refresh_token", "id_token", "password",
        "device_id", "csrf_token", "cookie_header", "totp_secret", "totp_factor_id",
        "session_json",
    }
    payload = {k: v for k, v in updates.items() if k in allowed and v}
    if not payload:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(RegisteredModel)
            .where(RegisteredModel.email == email)
            .values(**payload)
        )
        return bool(result.rowcount)


# ──────────────────────── 运行记录 ────────────────────────


def create_run(run_id: str, email: str = "", log_path: str = "") -> None:
    with register_session_scope() as session:
        session.add(
            RunModel(
                run_id=run_id,
                email=email,
                status="running",
                started_at=time.time(),
                log_path=log_path or None,
            )
        )


def finish_run(
    run_id: str,
    ok: bool,
    error: str = "",
    error_category: str = "",
    email: str = "",
) -> None:
    with register_session_scope() as session:
        session.execute(
            sa_update(RunModel)
            .where(RunModel.run_id == run_id)
            .values(
                status="done" if ok else "failed",
                finished_at=time.time(),
                error=(error or None),
                error_category=(error_category or None),
                email=(email or None),
            )
        )


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    """取单个运行记录（status / error_category 等）。"""
    with register_session_scope() as session:
        row = session.execute(
            select(RunModel).where(RunModel.run_id == str(run_id or ""))
        ).scalar_one_or_none()
        if row is None:
            return None
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def list_runs(limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    with register_session_scope() as session:
        count = int(session.execute(select(func.count()).select_from(RunModel)).scalar_one())
        stmt = select(RunModel).order_by(RunModel.started_at.desc()).limit(limit).offset(offset)
        items = []
        for row in session.execute(stmt).scalars():
            item = {c.name: getattr(row, c.name) for c in row.__table__.columns}
            item["duration"] = (
                (item.get("finished_at") or 0) - (item.get("started_at") or 0)
                if item.get("finished_at")
                else None
            )
            items.append(item)
        return items, count


# ──────────────────────── 代理池 ────────────────────────


def _proxy_to_dict(row) -> dict[str, Any]:
    """ORM 行 → dict，并派生 status 字段。

    status 三态（与原注册机一致，前端按这个展示「可用 N / 停用 M / 已满 K」）：
      available —— 启用中且未超配额
      disabled  —— 被手动停用，或连续被 CF 拦截自动停用
      exhausted —— 启用中但成功数已达上限
    """
    item = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    enabled = bool(item.get("enabled"))
    max_uses = int(item.get("max_uses") or 0)
    used = int(item.get("used_count") or 0)
    if not enabled:
        item["status"] = "disabled"
    elif max_uses and used >= max_uses:
        item["status"] = "exhausted"
    else:
        item["status"] = "available"
    return item


def list_proxies() -> list[dict[str, Any]]:
    with register_session_scope() as session:
        # ⚠️ 不能用 rowid 排序 —— 那是 SQLite 特有的，PG/MySQL 没有。
        # 用 created_at + proxy 做稳定排序。
        stmt = select(ProxyPoolModel).order_by(ProxyPoolModel.created_at, ProxyPoolModel.proxy)
        return [_proxy_to_dict(row) for row in session.execute(stmt).scalars()]


def find_proxy(proxy: str) -> Optional[dict[str, Any]]:
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    with register_session_scope() as session:
        row = session.execute(
            select(ProxyPoolModel).where(ProxyPoolModel.proxy == proxy)
        ).scalar_one_or_none()
        return _proxy_to_dict(row) if row is not None else None


def replace_proxies(proxies: list[str], *, mode: str = "replace") -> dict:
    """批量导入代理。

    mode="replace"：清空后写入（保留已有代理的 enabled/上限/用量？
                  不保留 —— 「覆盖保存」的语义就是重来，但用量清零会让
                  已经注册成功的号对应的代理计数丢失。
                  这里改为：replace 只清掉**不在新列表里**的代理，
                  新列表里已存在的代理保留其状态和用量。）
    mode="append"：只追加新代理，已存在的保持不变。
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for p in proxies or []:
        s = str(p or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)

    now = time.time()
    with register_session_scope() as session:
        if mode == "replace":
            # 删掉不在新列表里的
            if cleaned:
                session.execute(
                    sa_delete(ProxyPoolModel).where(ProxyPoolModel.proxy.notin_(cleaned))
                )
            else:
                session.execute(sa_delete(ProxyPoolModel))
        # 新增（已存在的跳过，保留状态和用量）
        existing = {
            row.proxy
            for row in session.execute(
                select(ProxyPoolModel).where(ProxyPoolModel.proxy.in_(cleaned))
            ).scalars()
        } if cleaned else set()
        for s in cleaned:
            if s in existing:
                continue
            session.add(
                ProxyPoolModel(
                    proxy=s,
                    enabled=True,
                    max_uses=50,
                    used_count=0,
                    failed_count=0,
                    proxy_type="static",
                    cf_blocked=0,
                    ipv6_status="untested",
                    created_at=now,
                )
            )
    items = list_proxies()
    return {"total": len(cleaned), "items": items}


def update_proxy(
    proxy: str,
    *,
    enabled: Optional[bool] = None,
    max_uses: Optional[int] = None,
    note: Optional[str] = None,
    proxy_type: Optional[str] = None,
    ipv6_status: Optional[str] = None,
) -> bool:
    """改单个代理的启用状态 / 上限 / 备注 / 类型。返回是否命中。"""
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    payload: dict[str, Any] = {}
    if enabled is not None:
        payload["enabled"] = bool(enabled)
    if max_uses is not None:
        payload["max_uses"] = max(0, int(max_uses))
    if note is not None:
        payload["note"] = str(note)
    if proxy_type is not None:
        payload["proxy_type"] = str(proxy_type)
    if ipv6_status is not None:
        payload["ipv6_status"] = str(ipv6_status)
    if not payload:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(ProxyPoolModel).where(ProxyPoolModel.proxy == proxy).values(**payload)
        )
        return bool(result.rowcount)


def delete_proxy(proxy: str) -> bool:
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_delete(ProxyPoolModel).where(ProxyPoolModel.proxy == proxy)
        )
        return bool(result.rowcount)


def clear_proxies() -> int:
    """清空整个代理池（连同用量记录）。"""
    with register_session_scope() as session:
        result = session.execute(sa_delete(ProxyPoolModel))
        return int(result.rowcount or 0)


def reset_proxy_counts(proxy: Optional[str] = None) -> int:
    """重置用量计数。proxy 为空则重置全部。

    注意：只清 used_count / failed_count，不动 enabled / max_uses / cf_blocked。
    """
    with register_session_scope() as session:
        stmt = sa_update(ProxyPoolModel).values(used_count=0, failed_count=0)
        target = str(proxy or "").strip()
        if target:
            stmt = stmt.where(ProxyPoolModel.proxy == target)
        result = session.execute(stmt)
        return int(result.rowcount or 0)


def reset_proxy_cf_blocked(proxy: str) -> bool:
    """清零某个代理的 CF 拦截计数。

    场景：静态代理被 CF 拉黑后自动停用了，换了 IP / 供应商修了之后手动清一下，
    不用删了重导。
    """
    target = str(proxy or "").strip()
    if not target:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(ProxyPoolModel)
            .where(ProxyPoolModel.proxy == target)
            .values(cf_blocked=0)
        )
        return bool(result.rowcount)


def reset_all_proxy_cf_blocked() -> int:
    """清零全部代理的 CF 拦截计数。"""
    with register_session_scope() as session:
        result = session.execute(
            sa_update(ProxyPoolModel).where(ProxyPoolModel.cf_blocked > 0).values(cf_blocked=0)
        )
        return int(result.rowcount or 0)


def set_proxy_ipv6_status(proxy: str, status: str) -> bool:
    """记录代理的 IPv6 检测结果（ok / fail / untested）。"""
    target = str(proxy or "").strip()
    if not target:
        return False
    st = str(status or "untested").strip().lower()
    if st not in ("ok", "fail", "untested"):
        st = "untested"
    with register_session_scope() as session:
        result = session.execute(
            sa_update(ProxyPoolModel)
            .where(ProxyPoolModel.proxy == target)
            .values(ipv6_status=st, ipv6_checked_at=time.time())
        )
        return bool(result.rowcount)


def pick_proxy() -> str:
    """挑一个可用代理：启用中 + 未超配额 + 用得少的优先。

    ★ 关键：**选中即预占一格**（used_count +1）。
    不预占的话，两个 worker 会在同一瞬间读到同一个 used_count，
    都挑中同一个代理 —— 并发时等于没做均衡。预占后下一个 worker 就会
    自动避开它，去用另一个还没人用的代理。

    预占的结算：
      - 注册成功 → 什么都不做（预占的那格就是这次的配额）
      - 注册失败 → 调用 proxy_release 把这格还回去（失败不占配额）

    池里没有可用代理时返回空串（调用方负责停下并提示）。
    """
    now = time.time()
    with register_session_scope() as session:
        stmt = (
            select(ProxyPoolModel)
            .where(ProxyPoolModel.enabled.is_(True))
            .where(
                (ProxyPoolModel.max_uses == 0)
                | (ProxyPoolModel.used_count < ProxyPoolModel.max_uses)
            )
            .order_by(ProxyPoolModel.used_count.asc(), ProxyPoolModel.failed_count.asc())
            .limit(50)
        )
        candidates = list(session.execute(stmt).scalars())
        for candidate in candidates:
            # 条件 UPDATE 保证并发下只有一个 worker 拿到
            result = session.execute(
                sa_update(ProxyPoolModel)
                .where(
                    ProxyPoolModel.proxy == candidate.proxy,
                    ProxyPoolModel.enabled.is_(True),
                    (ProxyPoolModel.max_uses == 0)
                    | (ProxyPoolModel.used_count < ProxyPoolModel.max_uses),
                )
                .values(used_count=ProxyPoolModel.used_count + 1, last_used_at=now)
            )
            if result.rowcount:
                return candidate.proxy
        return ""


def proxy_used(proxy: str, *, ok: bool) -> bool:
    """登记一次注册结果。

    成功 → used_count += 1（占用配额）
    失败 → 只 failed_count += 1，**不占配额**（失败不计入上限）
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    with register_session_scope() as session:
        if ok:
            session.execute(
                sa_update(ProxyPoolModel)
                .where(ProxyPoolModel.proxy == proxy)
                .values(
                    used_count=ProxyPoolModel.used_count + 1,
                    last_used_at=time.time(),
                )
            )
        else:
            session.execute(
                sa_update(ProxyPoolModel)
                .where(ProxyPoolModel.proxy == proxy)
                .values(
                    failed_count=ProxyPoolModel.failed_count + 1,
                    last_used_at=time.time(),
                )
            )
    return True


def proxy_release(proxy: str) -> bool:
    """释放一个代理的配额（账号被删除 / 标注异常时调用）。

    used_count 不会减到负数 —— 重复释放是安全的。
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(ProxyPoolModel)
            .where(ProxyPoolModel.proxy == proxy)
            .values(used_count=func.max(0, ProxyPoolModel.used_count - 1))
        )
        return bool(result.rowcount)


def mark_proxy_cf_blocked(proxy: str, max_blocked: int = 2) -> dict[str, Any]:
    """记录一次 CF 403。静态代理连续被拦到阈值 → 自动停用。"""
    proxy = str(proxy or "").strip()
    if not proxy:
        return {}
    with register_session_scope() as session:
        row = session.execute(
            select(ProxyPoolModel).where(ProxyPoolModel.proxy == proxy)
        ).scalar_one_or_none()
        if row is None:
            return {}
        row.cf_blocked = int(row.cf_blocked or 0) + 1
        row.last_used_at = time.time()
        disabled = False
        # 只有静态代理才停用 —— 动态代理换 IP 可能就通了
        if row.proxy_type == "static" and row.cf_blocked >= max_blocked:
            row.enabled = False
            disabled = True
        return {
            "proxy": proxy,
            "cf_blocked": row.cf_blocked,
            "disabled": disabled,
            "proxy_type": row.proxy_type,
        }


def proxy_pool_stats() -> dict[str, Any]:
    """代理池概览，供前端展示「可用 N / 停用 M / 已满 K」。"""
    items = list_proxies()
    return {
        "total": len(items),
        "available": sum(1 for i in items if i.get("status") == "available"),
        "disabled": sum(1 for i in items if i.get("status") == "disabled"),
        "exhausted": sum(1 for i in items if i.get("status") == "exhausted"),
        "used": sum(int(i.get("used_count") or 0) for i in items),
        "failed": sum(int(i.get("failed_count") or 0) for i in items),
        "enabled": sum(1 for i in items if i.get("enabled")),
    }


# ──────────────────────── Remail 订单 ────────────────────────


def save_remail_order(order: dict[str, Any]) -> None:
    order_no = str(order.get("order_no") or "").strip()
    if not order_no:
        return
    with register_session_scope() as session:
        existing = session.execute(
            select(RemailOrderModel).where(RemailOrderModel.order_no == order_no)
        ).scalar_one_or_none()
        payload = {
            "email": order.get("email"),
            "suffix": order.get("suffix"),
            "service_mode": order.get("service_mode"),
            "supply_policy": order.get("supply_policy"),
            "project_id": order.get("project_id"),
            "order_status": order.get("order_status"),
            "service_token": order.get("service_token"),
            "pay_amount": order.get("pay_amount"),
            "received_at": order.get("received_at"),
            "receive_started_at": order.get("receive_started_at"),
            "after_sale_until": order.get("after_sale_until"),
            "used_email": order.get("used_email"),
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        if existing is None:
            session.add(
                RemailOrderModel(order_no=order_no, created_at=time.time(), **payload)
            )
        else:
            for key, value in payload.items():
                setattr(existing, key, value)


def get_remail_order(order_no: str) -> Optional[dict[str, Any]]:
    with register_session_scope() as session:
        row = session.execute(
            select(RemailOrderModel).where(RemailOrderModel.order_no == order_no)
        ).scalar_one_or_none()
        return {c.name: getattr(row, c.name) for c in row.__table__.columns} if row else None


def list_remail_orders(limit: int = 100) -> list[dict[str, Any]]:
    with register_session_scope() as session:
        stmt = select(RemailOrderModel).order_by(RemailOrderModel.created_at.desc()).limit(limit)
        return [
            {c.name: getattr(row, c.name) for c in row.__table__.columns}
            for row in session.execute(stmt).scalars()
        ]


def update_remail_order(order_no: str, updates: dict[str, Any]) -> bool:
    allowed = {
        "order_status", "used_email", "attempts", "received_at",
        "receive_started_at", "after_sale_until", "service_token",
    }
    payload = {k: v for k, v in (updates or {}).items() if k in allowed}
    if not payload:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(RemailOrderModel)
            .where(RemailOrderModel.order_no == order_no)
            .values(**payload)
        )
        return bool(result.rowcount)


def link_remail_order(email: str, order_no: str = "") -> bool:
    """把 Remail 订单关联到注册成功的账号。"""
    order_no = str(order_no or "").strip()
    email = str(email or "").strip().lower()
    if not order_no:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(RemailOrderModel)
            .where(RemailOrderModel.order_no == order_no)
            .values(used_email=email)
        )
        return bool(result.rowcount)


def mark_remail_order_attempt(order_no: str) -> None:
    with register_session_scope() as session:
        session.execute(
            sa_update(RemailOrderModel)
            .where(RemailOrderModel.order_no == order_no)
            .values(attempts=RemailOrderModel.attempts + 1)
        )


def stats() -> dict[str, Any]:
    """邮箱池按状态统计（available / in_use / done / failed / total）。"""
    with register_session_scope() as session:
        rows = session.execute(
            select(OutlookAccountModel.status, func.count())
            .group_by(OutlookAccountModel.status)
        ).all()
        out: dict[str, Any] = {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
        for status, count in rows:
            out[str(status) or "available"] = int(count)
            out["total"] += int(count)
        return out


def stats_by_kind() -> dict:
    """按邮箱类型分组统计，给前端展示「每种邮箱各有多少号」。"""
    with register_session_scope() as session:
        rows = session.execute(
            select(
                OutlookAccountModel.kind,
                OutlookAccountModel.status,
                func.count(),
            ).group_by(OutlookAccountModel.kind, OutlookAccountModel.status)
        ).all()
        out: dict[str, dict[str, int]] = {}
        for kind, status, count in rows:
            k = kind or "outlook"
            slot = out.setdefault(
                k, {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
            )
            slot[str(status)] = int(count)
            slot["total"] += int(count)
        return out


# ──────────────────────── 导入解析 ────────────────────────


def parse_lines(text: str, kind: str = "") -> list[dict[str, Any]]:
    """解析导入的邮箱文本。

    每行格式：`email----password----client_id----refresh_token`
    分隔符用四连横线（与 token----proxy 格式一致），顺序与导入顺序一致。
    """
    rows: list[dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        email = parts[0].strip().lower()
        if not email or "@" not in email:
            continue
        rows.append({
            "email": email,
            "password": parts[1].strip() if len(parts) > 1 else "",
            "client_id": parts[2].strip() if len(parts) > 2 else "",
            "refresh_token": parts[3].strip() if len(parts) > 3 else "",
            "relay_url": parts[4].strip() if len(parts) > 4 else "",
            "kind": kind or "outlook",
        })
    return rows


def parse_import_text(text: str, kind: str = "") -> list[dict[str, Any]]:
    """parse_lines 的别名（与原 db.py 命名保持一致）。"""
    return parse_lines(text, kind)

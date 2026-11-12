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


# SMS 接码配置的默认值 —— 与原注册机一致。
# ⚠️ 一定要有默认值：前端拿到空串会显示成空的下拉框/空输入框，
#    用户以为没配，实际是有默认值的。
SMS_CONFIG_DEFAULTS = {
    "sms_enabled": "0",
    "sms_provider": "smsbower",
    "sms_country": "52",
    "sms_service": "dr",
    "sms_max_price": "",
    "sms_fixed_price": "",
    "sms_reuse_phone": "0",
    "sms_phone_success_max": "3",
    "sms_auto_country": "0",
    "sms_strict_whitelist": "0",
    "sms_allowed_countries": "",
    "sms_auto_min_stock": "20",
    "sms_auto_max_price": "",
    "sms_max_phone_attempts": "",
    "sms_per_phone_timeout": "80",
}

# SMS 接码配置的全部键（读写都用它，避免两处漏字段）
SMS_CONFIG_KEYS = (
    "sms_enabled",
    "sms_provider",
    "sms_api_key",
    "sms_country",
    "sms_service",
    "sms_max_price",
    "sms_fixed_price",
    "sms_reuse_phone",
    "sms_phone_success_max",
    "sms_auto_country",
    "sms_strict_whitelist",
    "sms_allowed_countries",
    "sms_auto_min_stock",
    "sms_auto_max_price",
    "sms_max_phone_attempts",
    "sms_per_phone_timeout",
)


def get_sms_config() -> dict[str, Any]:
    """返回 SMS 接码配置（api_key 隐藏明文）。

    sms_enabled:        '0'/'1' 是否启用接码（命中 add-phone 时才会用）
    sms_provider:       接码平台（smsbower / herosms）
    sms_country:        国家代码或 ID（推荐 '52' = 泰国，OpenAI 走 SMS 的稳定国家）
    sms_service:        服务代码（OpenAI = 'dr'）
    sms_max_price:      号码最高单价（空 / -1 = 不限）
    sms_reuse_phone:    '0'/'1' 同号复用（省钱）
    sms_phone_success_max: 同号最多复用几次（默认 3）
    sms_auto_country:   '0'/'1' 自动选最优国家（按价格 + 库存）
    """
    out = {
        key: get_setting(key, SMS_CONFIG_DEFAULTS.get(key, ""))
        for key in SMS_CONFIG_KEYS
    }
    out["sms_api_key"] = "***" if get_setting("sms_api_key") else ""
    return out


def save_sms_config(data: dict[str, Any]) -> None:
    """保存 SMS 接码配置。

    sms_api_key 传 '***' 或空 = **不修改**（前端回填的就是掩码，
    直接保存会把真值覆盖掉）。
    """
    for key in SMS_CONFIG_KEYS:
        if key not in data:
            continue
        value = data[key]
        if key == "sms_api_key":
            if not value or value == "***":
                continue
        if isinstance(value, bool):
            value = "1" if value else "0"
        set_setting(key, str(value).strip())


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


def release_proxy_for_emails(emails: list[str]) -> int:
    """把这些号占用的代理配额还回去。返回释放了几个。

    只对**真正占用过配额**的号释放 —— 用 extra_json.proxy_released 标记位
    防重复释放（同一个号先标异常再删除，不能减两次）。
    """
    cleaned = [str(e or "").strip().lower() for e in (emails or []) if str(e or "").strip()]
    if not cleaned:
        return 0
    released = 0
    with register_session_scope() as session:
        rows = session.execute(
            select(RegisteredModel).where(RegisteredModel.email.in_(cleaned))
        ).scalars()
        for row in rows:
            extra: dict[str, Any] = {}
            if row.extra_json:
                try:
                    extra = json.loads(row.extra_json) or {}
                except (TypeError, ValueError):
                    extra = {}
            if extra.get("proxy_released"):
                continue
            # 代理地址：优先看 extra（历史数据），再看独立列
            proxy = str(extra.get("proxy") or row.proxy or "").strip()
            if not proxy:
                continue
            # 只释放真正占过配额的代理 —— 用量已经是 0 的说明没占过
            proxy_row = session.execute(
                select(ProxyPoolModel).where(ProxyPoolModel.proxy == proxy)
            ).scalar_one_or_none()
            if proxy_row is not None and int(proxy_row.used_count or 0) > 0:
                proxy_row.used_count = int(proxy_row.used_count) - 1
            extra["proxy_released"] = True
            row.extra_json = json.dumps(extra, ensure_ascii=False)
            released += 1
    return released


def mark_registered_abnormal(email: str, abnormal: bool = True) -> bool:
    """标注账号异常（被封 / 额度耗尽 / 不可用）。

    标注为异常时**自动释放该号占用的代理配额** —— 号已经废了，
    它的 IP 名额应该还回池子里给别的号用。
    """
    email = str(email or "").strip().lower()
    if not email:
        return False
    if abnormal:
        release_proxy_for_emails([email])
    with register_session_scope() as session:
        row = session.execute(
            select(RegisteredModel).where(RegisteredModel.email == email)
        ).scalar_one_or_none()
        if row is None:
            return False
        extra: dict[str, Any] = {}
        if row.extra_json:
            try:
                extra = json.loads(row.extra_json) or {}
            except (TypeError, ValueError):
                extra = {}
        extra["abnormal"] = bool(abnormal)
        row.extra_json = json.dumps(extra, ensure_ascii=False)
        return True


def mark_registered_abnormal_by_emails(emails: list[str], abnormal: bool = True) -> int:
    """批量标注账号异常。返回实际标注了几个。"""
    cleaned = [str(e or "").strip().lower() for e in (emails or []) if str(e or "").strip()]
    if not cleaned:
        return 0
    count = 0
    for email in cleaned:
        if mark_registered_abnormal(email, abnormal):
            count += 1
    return count


def list_run_batches(limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """按批次聚合运行记录。

    一次批量注册 = 一个批次，下面挂多个 run（每个 run 一次注册）。
    返回每批：开始/结束时间、耗时、成功数、失败数、邮箱列表、逐个 run 的明细。
    """
    with register_session_scope() as session:
        rows = session.execute(
            select(RunModel).order_by(RunModel.started_at.desc())
        ).scalars()
        batches: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for row in rows:
            item = {c.name: getattr(row, c.name) for c in row.__table__.columns}
            if item.get("finished_at"):
                item["duration"] = item["finished_at"] - (item.get("started_at") or 0)
            else:
                item["duration"] = None
            batch_key = str(item.get("batch_id") or item.get("run_id"))
            batch = batches.get(batch_key)
            if batch is None:
                batch = {
                    "batch_id": batch_key,
                    "started_at": item.get("started_at"),
                    "finished_at": item.get("finished_at"),
                    "duration": item.get("duration"),
                    "total": 0,
                    "ok": 0,
                    "fail": 0,
                    "running": 0,
                    "emails": [],
                    "runs": [],
                }
                batches[batch_key] = batch
                order.append(batch_key)
            batch["total"] += 1
            if item.get("status") == "done":
                batch["ok"] += 1
                if item.get("email"):
                    batch["emails"].append(item["email"])
            elif item.get("status") == "failed":
                batch["fail"] += 1
            else:
                batch["running"] += 1
            batch["runs"].append(item)
            # 批次的起止时间 = 子 run 的最小/最大
            started = item.get("started_at")
            finished = item.get("finished_at")
            if started:
                batch["started_at"] = min(batch["started_at"] or started, started)
            if finished:
                batch["finished_at"] = max(batch["finished_at"] or finished, finished)
            if batch["started_at"] and batch["finished_at"]:
                batch["duration"] = batch["finished_at"] - batch["started_at"]

        total = len(order)
        page = order[offset : offset + limit] if limit else order
        return [batches[key] for key in page], total


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


def create_run(
    run_id: str,
    email: str = "",
    log_path: str = "",
    batch_id: str = "",
) -> None:
    with register_session_scope() as session:
        session.add(
            RunModel(
                run_id=run_id,
                email=email,
                status="running",
                started_at=time.time(),
                log_path=log_path or None,
                batch_id=batch_id or None,
            )
        )


def finish_run(
    run_id: str,
    status: str = "done",
    error: str = "",
    category: str = "",
    email: str = "",
) -> None:
    """标记一次注册尝试结束。

    ⚠️ status 是**字符串**（"done" / "failed"），不是 bool。
       历史上这里用过 bool 语义（ok=True/False），但调用点是按字符串传的
       —— "failed" 也是 truthy，会把失败记成成功。已统一成字符串，
       与原注册机 webui/db.py 的签名一致。

    ⚠️ 参数名是 category（不是 error_category）—— 调用方传的也是 category，
       名字必须对齐，否则 TypeError 会让 run 永远停在 "running"，
       worker 死等 1800 秒，前端永远显示「运行中」。

    ⚠️ email 传空 = **不改这一列**（不是清空）。
       非池化来源（Remail / CF 临时邮箱）一开始登记的是占位邮箱
       （xxx_placeholder_N@placeholder.local），失败时我们往往拿不到真实邮箱；
       这时候把列清空会让「运行记录」里连占位信息都没了。
       真实邮箱只在**注册成功**时回写 —— 那时 flow.result.email 才是准的。
    """
    st = str(status or "failed").strip().lower()
    if st not in ("done", "failed"):
        st = "failed"
    values: dict[str, Any] = {
        "status": st,
        "finished_at": time.time(),
        "error": (error or None),
        "error_category": (category or None),
    }
    email_value = str(email or "").strip()
    if email_value:
        values["email"] = email_value
    with register_session_scope() as session:
        session.execute(
            sa_update(RunModel)
            .where(RunModel.run_id == run_id)
            .values(**values)
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


def mark_proxy_invalid_state(proxy: str, max_invalid: int = 2) -> dict[str, Any]:
    """记录一次「注册会话失效」（409 invalid_state）。

    ⚠️ 为什么单独记，而不是并进 failed_count
    ----------------------------------------
    failed_count 只是「这个代理失败过几次」的软信号 —— pick_proxy 拿它排序
    但**不会**把代理排除掉。invalid_state 是更强的信号：
    服务端明确说「Your sign-in session is no longer valid」，
    几乎总是这个出口 IP 被 OpenAI 记了（同一 IP 注册次数太多 / 被风控）。

    所以处置跟 cf_blocked 同构：
      · static（固定出口 IP）→ 连续 N 次失效就自动停用，别再拿它试
      · dynamic（每次换 IP）→ 只计数不停用，换个出口可能就通

    不这么做的话，一个被 OpenAI 记死的静态代理会被反复挑中 ——
    每个 worker 每次都撞 409，邮箱白白消耗（Remail 是要花钱的），
    直到整个池子里的号都被浪费光。
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return {}
    with register_session_scope() as session:
        row = session.execute(
            select(ProxyPoolModel).where(ProxyPoolModel.proxy == proxy)
        ).scalar_one_or_none()
        if row is None:
            return {}
        row.invalid_state = int(row.invalid_state or 0) + 1
        row.failed_count = int(row.failed_count or 0) + 1
        row.last_used_at = time.time()
        disabled = False
        if row.proxy_type == "static" and row.invalid_state >= max(1, int(max_invalid)):
            row.enabled = False
            disabled = True
        return {
            "proxy": proxy,
            "invalid_state": row.invalid_state,
            "disabled": disabled,
            "proxy_type": row.proxy_type,
        }


def reset_proxy_invalid_state(proxy: str) -> bool:
    """清零某个代理的 invalid_state 计数（换了 IP / 供应商修了之后手动点一下）。"""
    target = str(proxy or "").strip()
    if not target:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(ProxyPoolModel)
            .where(ProxyPoolModel.proxy == target)
            .values(invalid_state=0)
        )
        return bool(result.rowcount)


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


# ──────────────────────── Remail 订单 ────────────────────────
#
# Remail 只交付「邮箱地址 + serviceToken」这一对取件凭证，不交付账号密码，
# 所以本地必须自己记一份 —— 后续取件、重复收件、看邮箱类型都读这里，
# 不再回查 Remail 的 projects / orders 接口。
#
# ⚠️ 这套函数被 services/register/mail_providers/remail.py 直接调用
#    （下单落库、复用挑选、尝试计数），也 registrar 调用（成功后关联账号）。

# 一个长效订单最多被拿去注册几次。超过就不再复用它 ——
# 否则会死循环卡在一个坏邮箱上（每次都失败又每次都复用它）。
REMAIL_MAX_ATTEMPTS = 10


def _remail_order_to_dict(row) -> dict[str, Any]:
    item = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    # service_token 是取件凭证，列表页不返回明文（详情/取件时才用）。
    item["has_token"] = bool(item.get("service_token"))
    item.pop("service_token", None)
    item["attempt_count"] = int(item.get("attempt_count") or 0)
    item["disabled"] = bool(item.get("disabled"))
    # 最大复用次数，前端显示「已用 N / 上限 M」用，别在前端硬编码。
    item["max_attempts"] = REMAIL_MAX_ATTEMPTS
    # 可复用 = 长效 + 没关联账号 + 状态有效 + 有 token + 未禁用 + 尝试次数没超限。
    # 前端据此显示「可复用」标签，并提示下一个注册任务会直接用它（不再扣费）。
    item["reusable"] = bool(
        str(item.get("service_mode") or "").lower() == "purchase"
        and not (item.get("used_email") or "").strip()
        and item.get("has_token")
        and str(item.get("order_status") or "") in ("paid", "active", "completed")
        and not item["disabled"]
        and item["attempt_count"] < REMAIL_MAX_ATTEMPTS
    )
    return item


def save_remail_order(d: dict[str, Any]) -> None:
    """记录一笔 Remail 订单（覆盖写）。"""
    order_no = str(d.get("order_no") or "").strip()
    if not order_no:
        return
    with register_session_scope() as session:
        # ⚠️ used_email / disabled / attempt_count 不能被覆盖成默认值：
        #    这张表的写操作是整行替换，不显式带上就会冲掉 ——
        #    used_email 被冲空 → 那个邮箱会被重复注册；
        #    disabled 被冲掉 → 手动禁用的订单又会被复用。
        #    所以先读旧值，没传就沿用。
        prev = session.execute(
            select(RemailOrderModel).where(RemailOrderModel.order_no == order_no)
        ).scalar_one_or_none()
        prev_used = (prev.used_email or "").strip() if prev is not None else ""
        prev_attempt = int(prev.attempt_count or 0) if prev is not None else 0
        prev_disabled = bool(prev.disabled) if prev is not None else False

        attempt = d.get("attempt_count")
        disabled = d.get("disabled")
        values = {
            "email": str(d.get("email") or "").strip(),
            "suffix": str(d.get("suffix") or "").strip(),
            "service_mode": str(d.get("service_mode") or "").strip(),
            "supply_policy": str(d.get("supply_policy") or "").strip(),
            "project_id": d.get("project_id"),
            "order_status": str(d.get("order_status") or "").strip(),
            "service_token": str(d.get("service_token") or "").strip(),
            "pay_amount": str(d.get("pay_amount") or "").strip(),
            "received_at": d.get("received_at") or time.time(),
            "receive_started_at": str(d.get("receive_started_at") or "").strip(),
            "after_sale_until": str(d.get("after_sale_until") or "").strip(),
            "used_email": str(d.get("used_email") or "").strip() or prev_used,
            "attempt_count": int(attempt) if attempt is not None else prev_attempt,
            "last_error": d.get("last_error"),
            "disabled": bool(disabled) if disabled is not None else prev_disabled,
            "created_at": d.get("created_at") or time.time(),
        }
        if prev is None:
            session.add(RemailOrderModel(order_no=order_no, **values))
        else:
            for key, value in values.items():
                setattr(prev, key, value)


def list_remail_orders(limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    with register_session_scope() as session:
        stmt = (
            select(RemailOrderModel)
            .order_by(RemailOrderModel.received_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_remail_order_to_dict(row) for row in session.execute(stmt).scalars()]


def count_remail_orders() -> int:
    with register_session_scope() as session:
        return int(
            session.execute(select(func.count()).select_from(RemailOrderModel)).scalar_one()
        )


def get_remail_order(order_no: str, *, with_token: bool = False) -> Optional[dict[str, Any]]:
    with register_session_scope() as session:
        row = session.execute(
            select(RemailOrderModel).where(
                RemailOrderModel.order_no == str(order_no or "").strip()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        item = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        if not with_token:
            item["has_token"] = bool(item.get("service_token"))
            item.pop("service_token", None)
        return item


def link_remail_order(order_no: str, email: str) -> bool:
    """把订单关联到本地注册成功的账号。

    顺带：短效（code）订单关联成功后即视为「已消耗」——
    它的取件凭证是一次性的，用完就该标 consumed，而不是继续挂在 active。
    长效（purchase）订单保持 active（还能重复收件）。
    """
    order_no = str(order_no or "").strip()
    if not order_no:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(RemailOrderModel)
            .where(RemailOrderModel.order_no == order_no)
            .values(used_email=str(email or "").strip().lower())
        )
        ok = bool(result.rowcount)
    if ok:
        _consume_short_lived_order(order_no)
    return ok


def update_remail_order_status(order_no: str, status: str) -> bool:
    order_no = str(order_no or "").strip()
    if not order_no:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(RemailOrderModel)
            .where(RemailOrderModel.order_no == order_no)
            .values(order_status=str(status or "").strip())
        )
        return bool(result.rowcount)


def set_remail_order_disabled(order_no: str, disabled: bool = True) -> bool:
    """本地禁用 / 启用一笔 Remail 订单。

    ⚠️ 用**独立的 disabled 字段**，不改 order_status ——
    order_status 是 Remail 服务端返回的状态（paid/active/completed…），
    不该把「我不想再用这个」混进去，否则分不清
    「服务端说这单废了」和「我手动不用了」。

    禁用的订单不会被复用（claim_reusable_remail_order 会跳过），
    但仍可手动取件 —— 邮箱还在，钱已经花了。
    """
    order_no = str(order_no or "").strip()
    if not order_no:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(RemailOrderModel)
            .where(RemailOrderModel.order_no == order_no)
            .values(disabled=bool(disabled))
        )
        return bool(result.rowcount)


def delete_remail_order(order_no: str) -> bool:
    """从本地删除一笔 Remail 订单记录。

    ⚠️ 只删**本地记录**，不会去 Remail 退单/退款 —— 那个要走服务端的售后流程
    （Order.afterSaleUntil）。这里只是让主人不再看到它、不再复用它。
    """
    order_no = str(order_no or "").strip()
    if not order_no:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_delete(RemailOrderModel).where(RemailOrderModel.order_no == order_no)
        )
        return bool(result.rowcount)


def delete_remail_orders(order_nos: list[str]) -> int:
    """批量删除本地 Remail 订单记录。"""
    cleaned = [str(o or "").strip() for o in (order_nos or []) if str(o or "").strip()]
    if not cleaned:
        return 0
    with register_session_scope() as session:
        result = session.execute(
            sa_delete(RemailOrderModel).where(RemailOrderModel.order_no.in_(cleaned))
        )
        return int(result.rowcount or 0)


def find_remail_order_by_email(email: str) -> Optional[dict[str, Any]]:
    with register_session_scope() as session:
        row = session.execute(
            select(RemailOrderModel)
            .where(RemailOrderModel.email == str(email or "").strip().lower())
            .order_by(RemailOrderModel.received_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        item = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        item["has_token"] = bool(item.get("service_token"))
        item.pop("service_token", None)
        return item


def claim_reusable_remail_order(
    *,
    service_mode: str = "purchase",
    suffix: str = "",
    project_id: Any = None,
    max_attempts: int = REMAIL_MAX_ATTEMPTS,
) -> Optional[dict[str, Any]]:
    """找一笔可以复用的 Remail 订单，返回**含 service_token** 的完整记录。

    复用条件（全部满足）：
      1. service_mode = purchase（**长效**订单才可重复收件；code 短效是一次性的）
      2. 没有关联账号（used_email 为空）—— 已注册成功的号不能再拿来注册
      3. 订单还有 service_token（没它就取不到件）
      4. 订单状态是有效的（paid / active / completed 都算；
         refunded / failed / closed 的不能再用）
      5. suffix / project_id 匹配（不同后缀的邮箱不能混用）
      6. 尝试次数没超限（attempt_count < max_attempts）

    为什么要复用：长效订单是**已经付过钱**的邮箱，注册失败（OTP 超时、
    409、风控）之后那个邮箱本身还是好的 —— 换个号重新注册完全可行，
    不该再花一次钱买新的。

    挑哪一笔：最早下的一笔（received_at 升序）—— 优先把已经付过钱的用掉，
    避免囤着一堆闲置订单还继续买新的。
    """
    mode = str(service_mode or "purchase").strip().lower()
    if mode != "purchase":
        # 短效订单一次性，用完就没了，不存在「复用」
        return None
    sf = str(suffix or "").strip()

    with register_session_scope() as session:
        stmt = select(RemailOrderModel).where(
            RemailOrderModel.service_mode == "purchase",
            (RemailOrderModel.used_email.is_(None)) | (RemailOrderModel.used_email == ""),
            RemailOrderModel.service_token.is_not(None),
            RemailOrderModel.service_token != "",
            RemailOrderModel.order_status.in_(["paid", "active", "completed"]),
            RemailOrderModel.disabled.is_(False),
            RemailOrderModel.attempt_count < max(1, int(max_attempts)),
        )
        if sf:
            stmt = stmt.where(RemailOrderModel.suffix == sf)
        if project_id is not None:
            stmt = stmt.where(RemailOrderModel.project_id == int(project_id))
        stmt = stmt.order_by(RemailOrderModel.received_at.asc()).limit(1)
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def mark_remail_order_attempt(order_no: str, ok: bool, error: str = "") -> bool:
    """记录一次注册尝试的结果。

    失败 → attempt_count +1（超过上限就不再被复用，避免死循环用一个坏邮箱）
    成功 → 由 registrar 走 link_remail_order 关联账号，不再计数
    """
    order_no = str(order_no or "").strip()
    if not order_no:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(RemailOrderModel)
            .where(RemailOrderModel.order_no == order_no)
            .values(
                attempt_count=RemailOrderModel.attempt_count + 1,
                last_error=None if ok else str(error or "")[:200],
            )
        )
        ok_row = bool(result.rowcount)

    # 短效订单进入终态 → 状态置为「已消耗」
    # ⚠️ 只在短效（code）订单上做。长效（purchase）能重复收件，
    #    它的 active 是**真实状态**，不能改成 consumed。
    if ok_row:
        _consume_short_lived_order(order_no)
    return ok_row


def _consume_short_lived_order(order_no: str) -> bool:
    """短效（code）订单用尽后，把状态置为 consumed（已消耗）。

    背景：短效订单的 serviceToken 是**一次性凭证**，实测取件返回
    `401 Credential is invalid or expired` —— 凭证早已失效，
    但订单状态还写着 'active'，是**假状态**。
    """
    order_no = str(order_no or "").strip()
    if not order_no:
        return False
    with register_session_scope() as session:
        result = session.execute(
            sa_update(RemailOrderModel)
            .where(
                RemailOrderModel.order_no == order_no,
                RemailOrderModel.service_mode == "code",
                (
                    RemailOrderModel.order_status.is_(None)
                    | (RemailOrderModel.order_status.notin_(["consumed", "refunded"]))
                ),
            )
            .values(order_status="consumed")
        )
        return bool(result.rowcount)

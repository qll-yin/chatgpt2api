"""
注册业务的 ORM 模型（从 gpt-outlook-register 的 webui/db.py 迁移而来）。

这些表与 chatgpt2api 主业务的表（accounts / auth_keys）**职责不同**：
- 主业务表存「已注册好、可直接调用的成品号」
- 这里存「注册过程的生产资料」：邮箱池、运行记录、代理池、Remail 订单

刻意不与 accounts 混表：
1. 生命周期不同 —— 邮箱池是一次性原料，runs 是过程记录，成品号才是资产
2. 写入频率差一个数量级 —— runs/proxy_pool 的写是高频的
3. 可以独立清理 —— 邮箱池用完即弃，runs 有保留期

与 accounts 的唯一交汇点：
注册成功后把 access_token（可带 proxy）写入 accounts —— 见
services/register_service.py 的 `_import_to_pool`。
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base

register_metadata = MetaData()
RegisterBase = declarative_base(metadata=register_metadata)


class OutlookAccountModel(RegisterBase):
    """邮箱池：待注册的邮箱账号（原料）。"""

    __tablename__ = "reg_outlook_accounts"

    email = Column(String(320), primary_key=True)
    password = Column(String(255), nullable=True)
    client_id = Column(String(255), nullable=True)
    refresh_token = Column(Text, nullable=True)
    relay_url = Column(Text, nullable=True)
    # 邮箱类型，对应 mail_providers 的 kind（outlook / cf_temp / icloud_relay / remail）
    kind = Column(String(32), nullable=False, default="outlook")
    # available / in_use / done / failed
    status = Column(String(16), nullable=False, default="available")
    imported_at = Column(Float, nullable=True)
    claimed_at = Column(Float, nullable=True)
    finished_at = Column(Float, nullable=True)
    fail_reason = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_reg_outlook_status", "status"),
        Index("ix_reg_outlook_kind", "kind", "status"),
    )


class RegisterSettingModel(RegisterBase):
    """注册业务配置（邮箱来源、接码、导出目标等）。"""

    __tablename__ = "reg_settings"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)


class RegisteredModel(RegisterBase):
    """注册成功的账号（成品号的完整凭证快照）。

    与主库 accounts 表的关系：这里存的是**全量凭证**（含 session_json、
    totp_secret 等），主库 accounts 只需要 access_token + proxy 就能用。
    """

    __tablename__ = "reg_registered"

    email = Column(String(320), primary_key=True)
    password = Column(Text, nullable=True)
    access_token = Column(Text, nullable=True)
    session_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    id_token = Column(Text, nullable=True)
    device_id = Column(Text, nullable=True)
    csrf_token = Column(Text, nullable=True)
    cookie_header = Column(Text, nullable=True)
    totp_secret = Column(Text, nullable=True)
    totp_factor_id = Column(Text, nullable=True)
    # /api/auth/session 的完整响应 JSON 原文（一行紧凑 JSON）
    session_json = Column(Text, nullable=True)
    # 注册时使用的代理（导入号池时按 access_token----proxy 带上）
    proxy = Column(Text, nullable=True)
    extra_json = Column(Text, nullable=True)
    created_at = Column(Float, nullable=True)


class RunModel(RegisterBase):
    """单次注册运行记录。"""

    __tablename__ = "reg_runs"

    run_id = Column(String(64), primary_key=True)
    email = Column(String(320), nullable=True)
    # running / done / failed
    status = Column(String(16), nullable=True)
    started_at = Column(Float, nullable=True)
    finished_at = Column(Float, nullable=True)
    log_path = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    # network / account / unknown / cf_blocked / mail_limited ...
    error_category = Column(String(32), nullable=True)

    __table_args__ = (
        Index("ix_reg_runs_started", "started_at"),
        Index("ix_reg_runs_status", "status"),
    )


class ProxyPoolModel(RegisterBase):
    """代理池。

    proxy_type 区分静态 / 动态出口，处置策略不同：
    - static：出口 IP 固定，被 CF 拉黑后换多少次都一样 → 连续被拦自动停用
    - dynamic：每次连接可换 IP → 只计数不停用
    """

    __tablename__ = "reg_proxy_pool"

    proxy = Column(String(512), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True)
    # 成功注册数上限，0 = 不限
    max_uses = Column(Integer, nullable=False, default=50)
    used_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    proxy_type = Column(String(16), nullable=False, default="static")
    # 连续被 CF 403 的次数（达阈值 → 自动停用 static 代理）
    cf_blocked = Column(Integer, nullable=False, default=0)
    # untested / ok / fail
    ipv6_status = Column(String(16), nullable=False, default="untested")
    # 上次做 IPv6 检测的时间（Unix 时间戳）
    ipv6_checked_at = Column(Float, nullable=True)
    ipv6_checked_at = Column(Float, nullable=True)
    last_used_at = Column(Float, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(Float, nullable=True)


class RemailOrderModel(RegisterBase):
    """Remail 订单。

    Remail 只交付「邮箱地址 + serviceToken」这一对取件凭证，不交付账号密码，
    所以本地必须自己记一份 —— 后续取件、重复收件、看邮箱类型都读这里，
    不再回查 Remail 的 projects / orders 接口。
    """

    __tablename__ = "reg_remail_orders"

    order_no = Column(String(128), primary_key=True)
    email = Column(String(320), nullable=True)
    # 下单时的 emailSuffix（邮箱类型）
    suffix = Column(String(64), nullable=True)
    # purchase / code
    service_mode = Column(String(32), nullable=True)
    supply_policy = Column(String(64), nullable=True)
    project_id = Column(Integer, nullable=True)
    order_status = Column(String(32), nullable=True)
    # 取件凭证（明文，本机自用）
    service_token = Column(Text, nullable=True)
    pay_amount = Column(String(32), nullable=True)
    received_at = Column(Float, nullable=True)
    receive_started_at = Column(Text, nullable=True)
    after_sale_until = Column(Text, nullable=True)
    # 关联到本地注册成功的账号
    used_email = Column(String(320), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(Float, nullable=True)

    __table_args__ = (
        Index("ix_reg_remail_received", "received_at"),
    )


def create_register_tables(engine) -> None:
    """建表 + 建索引。幂等。"""
    register_metadata.create_all(engine)

"""
运行态数据的 ORM 模型（图片任务 / 图片索引 / 标签 / 可编辑文件任务）。

设计原则
--------
1. **一张表一件事**，不复用 `accounts` 的「单列 JSON blob」模式 —— 这些表
   需要真正的列和索引（按 owner 查任务、按状态清理、按日期筛图片）。
2. **JSON 列只存真正变长的部分**（任务的 data / usage），其余全部拆成标量列，
   这样索引才有意义。
3. **主键用业务键**：`image_tasks` 用 (owner_id, task_id) 复合主键，
   `image_records` 用相对路径，`image_tags` 用 (rel_path, tag) —— 全部天然唯一，
   写入走 UPSERT 不会有冲突。
4. **时间字段双轨**：`created_at`/`updated_at` 是 ISO 字符串（与旧 JSON 数据兼容，
   前端直接用），`*_ts` 是 float 时间戳（用于范围查询和清理，避免字符串比较）。

索引依据
--------
- `image_tasks(owner_id, updated_at DESC)`：任务列表页的主查询，前端轮询高频命中。
- `image_tasks(status)`：清理过期任务时按状态筛选。
- `image_records(date)`：图片管理页按日期区间筛选。
- `image_tags(tag)`：按标签反查图片。
"""

from __future__ import annotations

from typing import Any

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

# 独立 MetaData：与 accounts/auth_keys 的 Base 分开，避免 create_all 时
# 把运行态表塞进账号存储后端的建表流程。
runtime_metadata = MetaData()
RuntimeBase = declarative_base(metadata=runtime_metadata)

# JSON 字段统一用 Text 存 JSON 字符串。
# 理由：跨方言最稳（PG/MySQL/SQLite 都支持），且这些字段（data/usage）
# 从来不参与 SQL 条件过滤，只做整体读写 —— 用原生 JSON 类型没有收益。
JSON_TEXT = Text


class ImageTaskModel(RuntimeBase):
    """图片生成/编辑任务。"""

    __tablename__ = "image_tasks"

    owner_id = Column(String(128), primary_key=True)
    task_id = Column(String(191), primary_key=True)

    status = Column(String(16), nullable=False, default="queued", index=True)
    mode = Column(String(16), nullable=False, default="generate")
    model = Column(String(128), nullable=False, default="")
    size = Column(String(32), nullable=False, default="")
    quality = Column(String(32), nullable=False, default="auto")

    # GIF 输出（作者 v1.10.0 的生图动图能力）。
    # ⚠️ 这三列是后加的，生产库里已存在的 image_tasks 表没有它们 ——
    #    create_runtime_tables() → ensure_columns() 会在启动时自动补上。
    output_format = Column(String(16), nullable=False, default="png")
    frame_count = Column(Integer, nullable=True)
    frame_duration_ms = Column(Integer, nullable=True)

    created_at = Column(String(32), nullable=False, default="")
    updated_at = Column(String(32), nullable=False, default="")
    created_ts = Column(Float, nullable=True)
    updated_ts = Column(Float, nullable=True)
    started_ts = Column(Float, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    progress = Column(String(64), nullable=True)
    error = Column(Text, nullable=True)
    conversation_id = Column(String(191), nullable=True)

    # 变长 JSON：生成结果数组 / token 用量
    data_json = Column(JSON_TEXT, nullable=True)
    usage_json = Column(JSON_TEXT, nullable=True)

    __table_args__ = (
        # 任务列表页主查询：WHERE owner_id = ? ORDER BY updated_at DESC
        Index("ix_image_tasks_owner_updated", "owner_id", "updated_at"),
    )


class ImageRecordModel(RuntimeBase):
    """图片元数据索引（对应旧 data/image_index.json）。"""

    __tablename__ = "image_records"

    rel_path = Column(String(512), primary_key=True)

    name = Column(String(255), nullable=False, default="")
    # YYYY-MM-DD，按日期筛选图片时用
    date = Column(String(16), nullable=False, default="", index=True)
    size = Column(Integer, nullable=False, default=0)
    created_at = Column(String(32), nullable=False, default="")
    created_ts = Column(Float, nullable=True)

    storage = Column(String(16), nullable=False, default="local")
    local = Column(Boolean, nullable=False, default=True)

    # ── 远端存储 ──
    # remote / remote_provider 是通用写法（webdav 或 s3 都算远端）。
    # webdav 是老字段，保留是为了读老数据；新逻辑统一看 remote，
    # 迁移把 webdav=1 的老行刷成 remote=1 + remote_provider='webdav'。
    remote = Column(Boolean, nullable=False, default=False)
    remote_provider = Column(String(16), nullable=False, default="")
    # webdav 字段已废弃：仅用于迁移期的老数据识别，不再写入
    webdav = Column(Boolean, nullable=False, default=False)
    remote_url = Column(Text, nullable=True)
    # 远端上传失败时的原因（降级存本地时记下来，图片管理页能看到）
    remote_error = Column(Text, nullable=True)
    # 远端上传时间（本地索引侧记录；清理策略另以对象自身 LastModified 为准）
    remote_uploaded_at = Column(String(32), nullable=False, default="")
    remote_uploaded_ts = Column(Float, nullable=True)

    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)


class ImageTagModel(RuntimeBase):
    """图片标签（对应旧 data/image_tags.json 的 rel -> [tag] 结构）。"""

    __tablename__ = "image_tags"

    rel_path = Column(String(512), primary_key=True)
    tag = Column(String(128), primary_key=True)

    __table_args__ = (
        Index("ix_image_tags_tag", "tag"),
    )


class EditableFileTaskModel(RuntimeBase):
    """可编辑文件（PPT/PSD）生成任务。"""

    __tablename__ = "editable_file_tasks"

    owner_id = Column(String(128), primary_key=True)
    task_id = Column(String(191), primary_key=True)

    status = Column(String(16), nullable=False, default="queued", index=True)
    mode = Column(String(32), nullable=False, default="")
    model = Column(String(128), nullable=False, default="")

    created_at = Column(String(32), nullable=False, default="")
    updated_at = Column(String(32), nullable=False, default="")
    created_ts = Column(Float, nullable=True)
    updated_ts = Column(Float, nullable=True)
    started_ts = Column(Float, nullable=True)
    ended_ts = Column(Float, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    error = Column(Text, nullable=True)
    data_json = Column(JSON_TEXT, nullable=True)
    usage_json = Column(JSON_TEXT, nullable=True)

    __table_args__ = (
        Index("ix_editable_file_tasks_owner_updated", "owner_id", "updated_at"),
    )


def create_runtime_tables(engine) -> None:
    """建表 + 建索引 + 补列。幂等，可重复调用。

    ⚠️ create_all 只建缺失的表，不给已存在的表加新列 ——
    模型加字段后，跑过的库必须补一次，否则查询直接报
    ``no such column``。详见 services/storage/migrations.py。
    """
    from services.storage.migrations import ensure_columns

    runtime_metadata.create_all(engine)
    for table in runtime_metadata.tables.values():
        ensure_columns(engine, table.name, list(table.columns))


def row_to_dict(row: Any) -> dict[str, Any]:
    """ORM 行 → 纯 dict（供上层逻辑使用，避免 ORM 对象泄漏到业务层）。"""
    return {c.name: getattr(row, c.name, None) for c in row.__table__.columns}

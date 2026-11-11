"""
运行态数据的数据库访问层。

这一层存在的唯一理由：**消除「读全文件 → 改 → 写全文件」模式**。
所有方法都是精确的 INSERT / UPDATE / DELETE，走索引，绝不整表重写。

对照旧实现的写放大（实测，2 万条任务）：
- 旧：单次写入 = 读 47 ms + 写 12 ms，一天 3 万张图 → 9 万次全量写、600 GB 写盘
- 新：单次写入 = 一次 UPSERT（亚毫秒级），写盘量与实际改动量成正比

方言兼容
--------
UPSERT 用各方言的 `INSERT ... ON CONFLICT DO UPDATE`：
- PostgreSQL / SQLite：标准语法
- MySQL：需要 `on_duplicate_key_update`，语法不同 → 单独分支处理
- 其他方言：退回 `merge()`（多一次 SELECT，但语义正确）
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Iterable

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update as sa_update

from services.storage.db import get_engine, runtime_session_scope
from services.storage.runtime_models import (
    EditableFileTaskModel,
    ImageRecordModel,
    ImageTagModel,
    ImageTaskModel,
    create_runtime_tables,
)

logger = logging.getLogger(__name__)

# 建表守卫：运行态服务在模块导入时就会实例化并访问数据库，
# 因此表必须在第一次访问前存在。create_all 幂等（默认 checkfirst=True），
# 这里用模块级标志保证只执行一次。
_tables_ready = False
_tables_lock = threading.Lock()


def ensure_tables() -> None:
    """确保运行态表已创建（幂等，进程内只执行一次建表语句）。"""
    global _tables_ready
    if _tables_ready:
        return
    with _tables_lock:
        if _tables_ready:
            return
        create_runtime_tables(get_engine())
        _tables_ready = True


def reset_table_guard() -> None:
    """重置建表守卫（测试用：切换数据库后需要重新建表）。"""
    global _tables_ready
    with _tables_lock:
        _tables_ready = False

# ── JSON 序列化辅助 ────────────────────────────────────────────────────────


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ── 通用 UPSERT ────────────────────────────────────────────────────────────


def _upsert(session, model, values: dict[str, Any], pk_cols: list[str]) -> None:
    """按主键做 INSERT ... ON CONFLICT DO UPDATE。

    `values` 必须包含全部主键列；冲突时非主键列全部更新为新值。
    """
    dialect = session.bind.dialect.name if session.bind is not None else ""

    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    elif dialect in {"mysql", "mariadb"}:
        from sqlalchemy.dialects.mysql import insert as dialect_insert
    else:
        # 未知方言：退回 merge()（语义正确，多一次 SELECT）
        session.merge(model(**values))
        return

    stmt = dialect_insert(model).values(**values)
    update_cols = {
        col.name: stmt.excluded[col.name]
        for col in model.__table__.columns
        if col.name not in pk_cols
    }
    if dialect in {"mysql", "mariadb"}:
        # MySQL 语法：ON DUPLICATE KEY UPDATE
        stmt = stmt.on_duplicate_key_update(**update_cols)
    else:
        stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
    session.execute(stmt)


# ── 图片任务 ───────────────────────────────────────────────────────────────

_TASK_FIELDS = (
    "status", "mode", "model", "size", "quality",
    "created_at", "updated_at", "created_ts", "updated_ts",
    "started_ts", "duration_ms", "progress", "error", "conversation_id",
)


# 标量列的缺省值：这些列在模型里是 NOT NULL，
# 业务层没传时必须落到缺省值而不是 None，否则插入直接违反非空约束。
_TASK_STR_DEFAULTS = {
    "status": "queued",
    "mode": "generate",
    "model": "",
    "size": "",
    "quality": "auto",
    "created_at": "",
    "updated_at": "",
}


def _task_values(task: dict[str, Any]) -> dict[str, Any]:
    values = {
        "owner_id": str(task.get("owner_id") or ""),
        "task_id": str(task.get("task_id") or task.get("id") or ""),
    }
    for name in _TASK_FIELDS:
        value = task.get(name)
        if value is None and name in _TASK_STR_DEFAULTS:
            value = _TASK_STR_DEFAULTS[name]
        values[name] = value
    values["data_json"] = dumps(task["data"]) if task.get("data") is not None else None
    values["usage_json"] = dumps(task["usage"]) if task.get("usage") is not None else None
    return values


def _task_from_row(row) -> dict[str, Any]:
    task = {
        "id": row.task_id,
        "owner_id": row.owner_id,
        "status": row.status,
        "mode": row.mode,
        "model": row.model,
        "size": row.size,
        "quality": row.quality,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "created_ts": row.created_ts,
        "updated_ts": row.updated_ts,
        "started_ts": row.started_ts,
        "duration_ms": row.duration_ms,
    }
    if row.progress:
        task["progress"] = row.progress
    if row.error:
        task["error"] = row.error
    if row.conversation_id:
        task["conversation_id"] = row.conversation_id
    data = loads(row.data_json)
    if data is not None:
        task["data"] = data
    usage = loads(row.usage_json)
    if usage is not None:
        task["usage"] = usage
    return task


def upsert_image_task(task: dict[str, Any]) -> None:
    """写入/覆盖一个图片任务（按 owner_id + task_id）。"""
    values = _task_values(task)
    if not values["owner_id"] or not values["task_id"]:
        return
    with runtime_session_scope() as session:
        _upsert(session, ImageTaskModel, values, ["owner_id", "task_id"])


def update_image_task(owner_id: str, task_id: str, updates: dict[str, Any]) -> None:
    """局部更新一个图片任务 —— 状态机的热路径。

    只 UPDATE 受影响的一行，不读全表、不重写全表。
    """
    if not owner_id or not task_id or not updates:
        return
    payload: dict[str, Any] = {}
    for name in _TASK_FIELDS:
        if name in updates:
            payload[name] = updates[name]
    if "data" in updates:
        payload["data_json"] = dumps(updates["data"]) if updates["data"] is not None else None
    if "usage" in updates:
        payload["usage_json"] = dumps(updates["usage"]) if updates["usage"] is not None else None
    if not payload:
        return
    with runtime_session_scope() as session:
        session.execute(
            sa_update(ImageTaskModel)
            .where(ImageTaskModel.owner_id == owner_id, ImageTaskModel.task_id == task_id)
            .values(**payload)
        )


def get_image_task(owner_id: str, task_id: str) -> dict[str, Any] | None:
    with runtime_session_scope() as session:
        row = session.execute(
            select(ImageTaskModel).where(
                ImageTaskModel.owner_id == owner_id,
                ImageTaskModel.task_id == task_id,
            )
        ).scalar_one_or_none()
        return _task_from_row(row) if row is not None else None


def list_image_tasks(owner_id: str, task_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """列出某 owner 的任务。

    - 指定 task_ids：走 (owner_id, task_id) 主键精确查询
    - 不指定：走 ix_image_tasks_owner_updated 索引，按 updated_at 倒序
    """
    with runtime_session_scope() as session:
        stmt = select(ImageTaskModel).where(ImageTaskModel.owner_id == owner_id)
        ids = [str(t) for t in (task_ids or []) if str(t or "").strip()]
        if ids:
            stmt = stmt.where(ImageTaskModel.task_id.in_(ids))
        else:
            stmt = stmt.order_by(ImageTaskModel.updated_at.desc())
        return [_task_from_row(row) for row in session.execute(stmt).scalars()]


def list_all_image_tasks() -> list[dict[str, Any]]:
    """列出全部图片任务（备份用）。

    不分 owner，按 updated_at 倒序。备份要的是完整快照。
    """
    with runtime_session_scope() as session:
        stmt = select(ImageTaskModel).order_by(ImageTaskModel.updated_at.desc())
        return [_task_from_row(row) for row in session.execute(stmt).scalars()]


def delete_expired_image_tasks(cutoff_ts: float) -> int:
    """删除更新时间早于 cutoff_ts 的**终态**任务，返回删除条数。

    条件与旧实现一致：只有 success/error 才清理，queued/running 保留。
    """
    with runtime_session_scope() as session:
        result = session.execute(
            sa_delete(ImageTaskModel).where(
                ImageTaskModel.status.in_(["success", "error"]),
                ImageTaskModel.updated_ts.isnot(None),
                ImageTaskModel.updated_ts < cutoff_ts,
            )
        )
        return int(result.rowcount or 0)


def recover_unfinished_image_tasks() -> int:
    """服务重启时把未完成任务标记为 error，返回处理条数。"""
    with runtime_session_scope() as session:
        result = session.execute(
            sa_update(ImageTaskModel)
            .where(ImageTaskModel.status.in_(["queued", "running"]))
            .values(status="error", error="服务已重启，未完成的图片任务已中断")
        )
        return int(result.rowcount or 0)


def count_image_tasks() -> int:
    with runtime_session_scope() as session:
        return int(session.execute(select(func.count()).select_from(ImageTaskModel)).scalar_one())


# ── 图片索引 ───────────────────────────────────────────────────────────────

_RECORD_FIELDS = (
    "name", "date", "size", "created_at", "created_ts",
    "storage", "local", "webdav", "remote_url", "width", "height",
)


_RECORD_STR_DEFAULTS = {
    "name": "",
    "date": "",
    "storage": "local",
}


def _record_values(item: dict[str, Any]) -> dict[str, Any]:
    values = {"rel_path": str(item.get("rel_path") or item.get("rel") or "")}
    for name in _RECORD_FIELDS:
        value = item.get(name)
        if value is None and name in _RECORD_STR_DEFAULTS:
            value = _RECORD_STR_DEFAULTS[name]
        elif value is None and name in {"size", "created_ts", "width", "height"}:
            value = 0
        elif value is None and name in {"local", "webdav"}:
            value = False
        values[name] = value
    return values


def _record_from_row(row) -> dict[str, Any]:
    item = {"rel": row.rel_path, "path": row.rel_path}
    for name in _RECORD_FIELDS:
        value = getattr(row, name, None)
        if value is not None:
            item[name] = value
    return item


def upsert_image_record(item: dict[str, Any]) -> None:
    values = _record_values(item)
    if not values["rel_path"]:
        return
    with runtime_session_scope() as session:
        _upsert(session, ImageRecordModel, values, ["rel_path"])


def bulk_upsert_image_records(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    with runtime_session_scope() as session:
        for item in items:
            values = _record_values(item)
            if values["rel_path"]:
                _upsert(session, ImageRecordModel, values, ["rel_path"])


def get_image_record(rel_path: str) -> dict[str, Any] | None:
    with runtime_session_scope() as session:
        row = session.execute(
            select(ImageRecordModel).where(ImageRecordModel.rel_path == rel_path)
        ).scalar_one_or_none()
        return _record_from_row(row) if row is not None else None


def list_image_records(start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
    """按日期区间列出图片索引（走 ix_image_records_date 索引）。"""
    with runtime_session_scope() as session:
        stmt = select(ImageRecordModel)
        if start_date:
            stmt = stmt.where(ImageRecordModel.date >= start_date)
        if end_date:
            stmt = stmt.where(ImageRecordModel.date <= end_date)
        stmt = stmt.order_by(ImageRecordModel.created_at.desc())
        return [_record_from_row(row) for row in session.execute(stmt).scalars()]


def list_all_image_records() -> list[dict[str, Any]]:
    with runtime_session_scope() as session:
        stmt = select(ImageRecordModel).order_by(ImageRecordModel.created_at.desc())
        return [_record_from_row(row) for row in session.execute(stmt).scalars()]


def delete_image_record(rel_path: str) -> None:
    with runtime_session_scope() as session:
        session.execute(sa_delete(ImageRecordModel).where(ImageRecordModel.rel_path == rel_path))


# ── 图片标签 ───────────────────────────────────────────────────────────────


def get_image_tags(rel_path: str) -> list[str]:
    with runtime_session_scope() as session:
        rows = session.execute(
            select(ImageTagModel.tag)
            .where(ImageTagModel.rel_path == rel_path)
            .order_by(ImageTagModel.tag)
        ).scalars()
        return list(rows)


def replace_image_tags(rel_path: str, tags: list[str]) -> list[str]:
    """整体替换某张图的标签列表。

    先删后插（同一事务内），避免「删了旧的但新的没插上」的中间态被读到。
    """
    cleaned = list(dict.fromkeys(t.strip() for t in tags if t and t.strip()))
    with runtime_session_scope() as session:
        session.execute(sa_delete(ImageTagModel).where(ImageTagModel.rel_path == rel_path))
        for tag in cleaned:
            session.add(ImageTagModel(rel_path=rel_path, tag=tag))
    return cleaned


def delete_image_tags(rel_path: str) -> None:
    with runtime_session_scope() as session:
        session.execute(sa_delete(ImageTagModel).where(ImageTagModel.rel_path == rel_path))


def delete_tag_everywhere(tag: str) -> int:
    """删除某个标签（所有图片上），返回受影响的图片数。"""
    with runtime_session_scope() as session:
        rows = session.execute(
            select(ImageTagModel.rel_path).where(ImageTagModel.tag == tag)
        ).scalars()
        affected = list(dict.fromkeys(rows))
        session.execute(sa_delete(ImageTagModel).where(ImageTagModel.tag == tag))
        return len(affected)


def list_all_tags() -> list[str]:
    with runtime_session_scope() as session:
        rows = session.execute(
            select(ImageTagModel.tag).distinct().order_by(ImageTagModel.tag)
        ).scalars()
        return list(rows)


def list_tagged_images(tag: str) -> list[str]:
    with runtime_session_scope() as session:
        rows = session.execute(
            select(ImageTagModel.rel_path).where(ImageTagModel.tag == tag)
        ).scalars()
        return list(rows)


# ── 可编辑文件任务 ─────────────────────────────────────────────────────────

_EDITABLE_FIELDS = (
    "status", "mode", "model",
    "created_at", "updated_at", "created_ts", "updated_ts",
    "started_ts", "ended_ts", "duration_ms", "error",
)


_EDITABLE_STR_DEFAULTS = {
    "status": "queued",
    "mode": "",
    "model": "",
    "created_at": "",
    "updated_at": "",
}

# 可编辑文件服务用的是 kind(ppt/psd) / result 两个字段名，
# 与表里的 mode / data_json 对应，这里做别名映射，避免语义丢失。
_EDITABLE_ALIASES = {
    "mode": ("kind",),
    "data_json": ("result",),
}


def _editable_values(task: dict[str, Any]) -> dict[str, Any]:
    values = {
        "owner_id": str(task.get("owner_id") or ""),
        "task_id": str(task.get("task_id") or task.get("id") or ""),
    }
    for name in _EDITABLE_FIELDS:
        value = task.get(name)
        if value is None:
            for alias in _EDITABLE_ALIASES.get(name, ()):
                if task.get(alias) is not None:
                    value = task.get(alias)
                    break
        if value is None and name in _EDITABLE_STR_DEFAULTS:
            value = _EDITABLE_STR_DEFAULTS[name]
        values[name] = value
    if values.get("data_json") is None and task.get("result") is not None:
        values["data_json"] = dumps(task["result"])
    values["usage_json"] = dumps(task["usage"]) if task.get("usage") is not None else None
    return values


def _editable_from_row(row) -> dict[str, Any]:
    task = {
        "id": row.task_id,
        "owner_id": row.owner_id,
        "status": row.status,
        # 兼容旧字段名：上层（editable_file_task_service）用 kind / result
        "kind": row.mode,
        "mode": row.mode,
        "model": row.model,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "created_ts": row.created_ts,
        "updated_ts": row.updated_ts,
        "started_ts": row.started_ts,
        "ended_ts": row.ended_ts,
        "duration_ms": row.duration_ms,
    }
    if row.error:
        task["error"] = row.error
    data = loads(row.data_json)
    if data is not None:
        task["result"] = data
        task["data"] = data
    usage = loads(row.usage_json)
    if usage is not None:
        task["usage"] = usage
    return task


def upsert_editable_file_task(task: dict[str, Any]) -> None:
    values = _editable_values(task)
    if not values["owner_id"] or not values["task_id"]:
        return
    with runtime_session_scope() as session:
        _upsert(session, EditableFileTaskModel, values, ["owner_id", "task_id"])


def update_editable_file_task(owner_id: str, task_id: str, updates: dict[str, Any]) -> None:
    if not owner_id or not task_id or not updates:
        return
    payload: dict[str, Any] = {}
    for name in _EDITABLE_FIELDS:
        if name in updates:
            payload[name] = updates[name]
    if "data" in updates:
        payload["data_json"] = dumps(updates["data"]) if updates["data"] is not None else None
    if "usage" in updates:
        payload["usage_json"] = dumps(updates["usage"]) if updates["usage"] is not None else None
    if not payload:
        return
    with runtime_session_scope() as session:
        session.execute(
            sa_update(EditableFileTaskModel)
            .where(
                EditableFileTaskModel.owner_id == owner_id,
                EditableFileTaskModel.task_id == task_id,
            )
            .values(**payload)
        )


def list_editable_file_tasks(owner_id: str) -> list[dict[str, Any]]:
    with runtime_session_scope() as session:
        stmt = (
            select(EditableFileTaskModel)
            .where(EditableFileTaskModel.owner_id == owner_id)
            .order_by(EditableFileTaskModel.updated_at.desc())
        )
        return [_editable_from_row(row) for row in session.execute(stmt).scalars()]


def get_editable_file_task(owner_id: str, task_id: str) -> dict[str, Any] | None:
    with runtime_session_scope() as session:
        row = session.execute(
            select(EditableFileTaskModel).where(
                EditableFileTaskModel.owner_id == owner_id,
                EditableFileTaskModel.task_id == task_id,
            )
        ).scalar_one_or_none()
        return _editable_from_row(row) if row is not None else None


def recover_unfinished_editable_file_tasks() -> int:
    """服务重启时把未完成的可编辑文件任务标记为 error。"""
    with runtime_session_scope() as session:
        result = session.execute(
            sa_update(EditableFileTaskModel)
            .where(EditableFileTaskModel.status.in_(["queued", "running"]))
            .values(status="error", error="服务已重启，未完成的任务已中断")
        )
        return int(result.rowcount or 0)

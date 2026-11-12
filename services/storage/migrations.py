"""轻量列迁移：给已存在的表补上模型新增的列。

为什么需要
----------
SQLAlchemy 的 ``metadata.create_all()`` **只建缺失的表，不会改已存在的表**。
所以给模型加一列之后：

- 新建的库 → 正常
- 已经跑过的库 → 表还在，但没有新列 → 一查询就
  ``OperationalError: no such column: xxx``

这在开发时很隐蔽（新建的临时库一切正常，一上生产就 500）。

所以这里做一次列比对：表存在就把缺的列 ALTER 出来。

约束
----
- 只做 **ADD COLUMN**，不做删列/改类型（那需要完整迁移框架）
- 新列必须可空，或带常量默认值（SQLite 的 ADD COLUMN 不允许
  「NOT NULL 且无默认值」）
- 每个引擎只检查一次（模块级缓存），避免每次开事务都去 inspect
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)

# 已检查过的 (引擎 id, 表名) → True（同一张表不重复检查）
#
# ⚠️ key 必须带表名 —— 只按引擎缓存的话，一次调用里循环检查多张表时，
#    第一张表就把引擎标记成「已检查」，后面全部被跳过。
_checked_tables: set[tuple[int, str]] = set()


def _quote(name: str, dialect: str) -> str:
    """SQLite 用双引号包标识符；PG/MySQL 用反引号/保持原样。"""
    if dialect == "sqlite":
        return f'"{name}"'
    if dialect == "mysql":
        return f"`{name}`"
    return name


def _default_literal(default: Any, dialect: str) -> str | None:
    """把 Python 标量默认值编译成 SQL 字面量。"""
    if default is None:
        return None
    if not getattr(default, "is_scalar", False):
        # callable / SQL 表达式 —— 不敢猜，跳过（列会是 NULL 或用空值）
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


def ensure_columns(engine, table_name: str, columns: list) -> list[str]:
    """给已存在的表补上缺失的列。返回实际补上的列名列表。"""
    dialect = engine.dialect.name
    cache_key = (id(engine), table_name)
    if cache_key in _checked_tables:
        return []

    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        # 表不存在 → create_all 会按最新模型建出来，不需要补列
        _checked_tables.add(cache_key)
        return []

    existing = {col["name"] for col in inspector.get_columns(table_name)}
    missing = [col for col in columns if col.name not in existing]
    if not missing:
        _checked_tables.add(cache_key)
        return []

    added: list[str] = []
    with engine.begin() as conn:
        for col in missing:
            type_sql = col.type.compile(dialect=engine.dialect)
            parts = [_quote(col.name, dialect), type_sql]
            literal = _default_literal(col.default, dialect)
            if literal is not None:
                parts.append(f"DEFAULT {literal}")
            if not col.nullable:
                # SQLite 不允许 ADD COLUMN 时既 NOT NULL 又无默认值
                parts.append("NOT NULL")
            ddl = f"ALTER TABLE {_quote(table_name, dialect)} ADD COLUMN " + " ".join(parts)
            try:
                conn.execute(text(ddl))
                added.append(col.name)
                logger.info("[migrate] %s 新增列 %s", table_name, col.name)
            except Exception as exc:  # noqa: BLE001
                # 加不了就让它暴露在查询错误里，别吞
                logger.warning("[migrate] %s 补列 %s 失败: %s", table_name, col.name, exc)

    _checked_tables.add(cache_key)
    return added


def drop_columns(engine, table_name: str, columns: list[str]) -> list[str]:
    """删掉表里已废弃的列。返回实际删掉的列名。

    为什么需要
    ----------
    改列名 / 删字段后，老库里会留着旧列。而旧列往往是
    ``INTEGER NOT NULL`` **且没有 SQL 级 DEFAULT**（SQLAlchemy 的
    ``default=0`` 是应用层默认值，不会写进 DDL）——
    于是新 INSERT 不带这一列时直接
    ``IntegrityError: NOT NULL constraint failed``。

    所以模型里已经没有的列，必须真的把它从表里删掉。

    只删**显式声明**的废弃列 —— 不做「模型里没有就自动删」的推断，
    那种推断太危险（万一某列只是这次没映射到）。
    """
    dialect = engine.dialect.name
    try:
        inspector = inspect(engine)
        if table_name not in inspector.get_table_names():
            return []
        existing = {col["name"] for col in inspector.get_columns(table_name)}
        targets = [c for c in columns if c in existing]
        if not targets:
            return []

        dropped: list[str] = []
        with engine.begin() as conn:
            for col in targets:
                if dialect == "sqlite":
                    ddl = f"ALTER TABLE {_quote(table_name, dialect)} DROP COLUMN {_quote(col, dialect)}"
                elif dialect == "postgresql":
                    ddl = f"ALTER TABLE {table_name} DROP COLUMN IF EXISTS {col}"
                else:
                    ddl = f"ALTER TABLE {table_name} DROP COLUMN {col}"
                try:
                    conn.execute(text(ddl))
                    dropped.append(col)
                    logger.info("[migrate] %s 删除废弃列 %s", table_name, col)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[migrate] %s 删除列 %s 失败: %s", table_name, col, exc)
        return dropped
    except Exception:  # noqa: BLE001
        logger.debug("[migrate] 清理废弃列失败", exc_info=True)
        return []


def reset_check_cache() -> None:
    """清空检查缓存（换引擎 / 测试时用）。"""
    _checked_tables.clear()

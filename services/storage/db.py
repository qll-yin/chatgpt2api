"""
统一数据库引擎与连接池。

背景
----
项目原先把绝大多数运行态数据放在 JSON 文件里，每次写入都是「读全文件 → 改 → 写全文件」。
在「一天几万张图」的量级下，这个模式有三个硬伤：

1. **写放大**：任务状态每变一次就重写整个文件（实测 2 万条任务 ≈ 6.8 MB，
   一次读写约 60 ms；一天 3 万张图 → 9 万次全量写 ≈ 600 GB 写盘、89 分钟锁内耗时）。
2. **全局锁阻塞**：所有写都在同一把 `threading.Lock` 里，串行化。
3. **无法索引**：查询只能全量扫 + Python 层过滤。

本模块提供**唯一**的数据库引擎出口，所有走数据库的服务都从这里拿 engine/session，
避免各服务各建连接池（连接数膨胀、无法统一调优）。

后端选择
--------
- `DATABASE_URL` 环境变量优先，支持 PostgreSQL / MySQL / SQLite。
- 未设置时回退到 `STORAGE_BACKEND`（json/git 时用本地 SQLite 承载运行态数据，
  保证「无数据库也能跑」的既有部署方式不被破坏）。

连接池参数
----------
`pool_size` / `max_overflow` / `pool_timeout` / `pool_recycle` 的取值依据：

- `pool_size=10`：FastAPI 单进程 + 后台线程（本项目线程模型下并发写者不超过 ~8）。
- `max_overflow=20`：生图高峰期的突发写入允许临时溢出，超出后等待而不是直接报错。
- `pool_timeout=30`：拿不到连接时最多等 30 秒再抛 `TimeoutError`，
  配合上层重试，避免瞬时抖动直接变成 500。
- `pool_pre_ping=True`：执行前探活，防止容器网络抖动后拿到死连接（PG 尤其需要）。
- `pool_recycle=1800`：30 分钟回收，避开服务端 idle 超时断连。

SQLite 特殊处理
---------------
SQLite 不支持 `pool_size` 之外的多数队列参数，且默认禁止跨线程共享连接。
这里用 `StaticPool` + `check_same_thread=False`，让多线程共用同一连接；
同时开启 WAL 模式提升读写并发（生产推荐 PG，此处仅为兼容旧部署）。
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services.config import DATA_DIR

# ── 默认连接串 ──────────────────────────────────────────────────────────────
# 未配置 DATABASE_URL 时的兜底：本地 SQLite 文件，和旧部署的数据目录放在一起。
DEFAULT_SQLITE_FILENAME = "chatgpt2api.db"

# 连接池默认值（见模块 docstring 的取值依据）
POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "20"))
POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))
POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))

_engine: Engine | None = None
_session_factory: sessionmaker | None = None
_engine_lock = threading.Lock()


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def resolve_database_url() -> str:
    """解析最终使用的数据库连接串。

    优先级：
    1. `DATABASE_URL` 环境变量（生产推荐）
    2. `STORAGE_BACKEND` 为 sqlite/postgres/mysql 时按约定拼出连接串
    3. 回退到本地 SQLite 文件

    注意：这里**不复用** `services.storage.factory` 的逻辑，因为它只服务于
    accounts/auth_keys 的存储后端选择，而运行态数据（图片任务等）必须始终落库。
    """
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return url

    backend = (os.getenv("STORAGE_BACKEND") or "json").strip().lower()
    db_path = Path(DATA_DIR) / DEFAULT_SQLITE_FILENAME

    if backend in {"sqlite", "database"}:
        return f"sqlite:///{db_path.as_posix()}"
    if backend in {"postgres", "postgresql"}:
        return (
            "postgresql+psycopg2://postgres:postgres@localhost:5432/chatgpt2api"
        )
    if backend in {"mysql", "mariadb"}:
        return "mysql+pymysql://root:root@localhost:3306/chatgpt2api"

    # json / git 后端：accounts 仍走文件，运行态数据落到本地 SQLite
    return f"sqlite:///{db_path.as_posix()}"


def get_engine() -> Engine:
    """返回全局唯一的 engine（懒加载，线程安全）。"""
    global _engine, _session_factory
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        url = resolve_database_url()
        kwargs: dict = {
            "future": True,
            "pool_pre_ping": True,
        }
        if _is_sqlite(url):
            from sqlalchemy.pool import StaticPool

            # SQLite：单连接 + 跨线程共享 + WAL
            kwargs.update(
                poolclass=StaticPool,
                connect_args={"check_same_thread": False, "timeout": POOL_TIMEOUT},
            )
        else:
            kwargs.update(
                pool_size=POOL_SIZE,
                max_overflow=MAX_OVERFLOW,
                pool_timeout=POOL_TIMEOUT,
                pool_recycle=POOL_RECYCLE,
            )
        engine = create_engine(url, **kwargs)

        if _is_sqlite(url):
            _install_sqlite_pragmas(engine)

        _engine = engine
        _session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        return _engine


def _install_sqlite_pragmas(engine: Engine) -> None:
    """给 SQLite 连接设置 WAL 等 PRAGMA。

    - `journal_mode=WAL`：读写不互相阻塞，是 SQLite 并发的关键。
    - `synchronous=NORMAL`：WAL 下的推荐值，牺牲极端断电场景的最后一点持久性，
      换取显著的写入吞吐（对本地兼容模式够用）。
    - `busy_timeout`：拿不到写锁时等待而不是立刻报 "database is locked"。
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - 驱动回调
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={POOL_TIMEOUT * 1000}")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def get_session_factory() -> sessionmaker:
    """返回全局唯一的 sessionmaker。"""
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务上下文：正常提交，异常回滚，始终关闭。

    用法：
        with session_scope() as session:
            session.add(obj)
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def runtime_session_scope() -> Iterator[Session]:
    """运行态数据专用事务上下文。

    与 `session_scope` 的区别：**首次使用时自动建表**。
    运行态服务（图片任务、图片索引、标签、可编辑文件任务）在模块导入阶段
    就会被实例化并访问数据库，那时表可能还不存在 —— 由这里兜底。
    """
    from services.storage.runtime_store import ensure_tables

    ensure_tables()
    with session_scope() as session:
        yield session


def database_dialect() -> str:
    """返回当前方言名（postgresql / sqlite / mysql），用于方言相关 SQL 分支。"""
    return get_engine().dialect.name


def supports_jsonb() -> bool:
    """当前方言是否原生支持 JSONB 列类型。"""
    return database_dialect() in {"postgresql", "mysql"}


def reset_engine() -> None:
    """重置 engine（测试用：切换到临时库后需要重建）。"""
    global _engine, _session_factory
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None

"""测试夹具：让每个测试模块使用**独立的临时数据库**。

为什么需要
----------
存储层改造后，图片任务 / 图片索引 / 标签 / 可编辑文件任务都落在数据库里。
如果测试直接连生产库（`data/chatgpt2api.db`），会出现两类问题：

1. **跨测试污染**：一个测试写入的图片记录，另一个测试能读到，
   断言「列表里只有 N 条」就会随机失败。
2. **污染真实数据**：测试写的图片索引会出现在用户真实的图片管理页里。

因此测试必须在导入被测模块**之前**把 `DATABASE_URL` 指向临时库，
并调用 `reset_engine()` + `reset_table_guard()` 让连接池与建表守卫重建。

用法（在测试模块顶部、导入业务模块之前）：
    from test.db_fixture import use_temp_database
    use_temp_database()
"""

from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path


def use_temp_database(prefix: str = "chatgpt2api-test-") -> str:
    """把数据库指向临时 SQLite 文件，返回该文件路径。

    必须在导入 `services.*` 之前调用。
    """
    tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
    db_path = tmpdir / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    # 确保后端选择不会把 accounts 存到别处
    os.environ.setdefault("STORAGE_BACKEND", "sqlite")
    os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth-key-000000000")

    def _cleanup() -> None:
        try:
            from services.storage.db import reset_engine

            reset_engine()
        except Exception:
            pass
        # ⚠️ 必须把临时目录删掉 —— 每个测试模块建一个，跑几十轮测试就会在
        #    系统临时目录里攒几百个垃圾目录（SQLite 还在被 WAL 占用时可能
        #    删不掉，ignore_errors 兜住，最坏情况留给系统清理临时目录）。
        try:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    atexit.register(_cleanup)
    return str(db_path)


def reset_database_state() -> None:
    """清空运行态表（同一测试内需要干净状态时调用）。"""
    from services.storage.db import get_engine, runtime_session_scope
    from services.storage.runtime_models import (
        EditableFileTaskModel,
        ImageRecordModel,
        ImageTagModel,
        ImageTaskModel,
    )
    from sqlalchemy import delete

    with runtime_session_scope() as session:
        session.execute(delete(ImageTaskModel))
        session.execute(delete(ImageRecordModel))
        session.execute(delete(ImageTagModel))
        session.execute(delete(EditableFileTaskModel))

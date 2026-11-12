"""导入完整性检查。

合并注册机时踩过的坑：顶层 import 改了，但**函数体内的延迟 import**
还指向旧项目的模块路径（`from sentinel import ...`、`from webui import db`）。
这些不会在启动时报错，只在**跑到那条代码路径时**才 ImportError ——
表现是「注册跑到一半失败」「订单表永远是空的」这种极难定位的问题。

所以这里做一次全量静态扫描：把 services/ 下所有模块（含函数体内的
延迟 import）都解析一遍，确保没有指向不存在模块的 import。

新增模块或改了 import 结构后跑一下这个测试，能立刻发现断链。
"""

from __future__ import annotations

import ast
import importlib
import io
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ["services", "api", "utils"]


def _module_name(path: Path) -> str:
    """相对项目根的模块名（点号形式）。

    `services/register/mail_providers/__init__.py` → `services.register.mail_providers`
    `services/register/mail_providers/base.py`     → `services.register.mail_providers.base`
    """
    rel = path.relative_to(ROOT).as_posix()[:-3]  # 去掉 .py
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def _iter_python_files():
    for root in SCAN_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                d for d in dirnames if d not in ("__pycache__", "node_modules", ".venv")
            ]
            for fn in filenames:
                if fn.endswith(".py"):
                    yield Path(dirpath) / fn


def _resolve_relative(module_name: str, is_package: bool, level: int, node_module: str) -> str:
    """把相对导入解析成绝对包路径。

    level=1 的基准是**当前包**（__init__.py 是它自己，普通模块是它的父包）。
    """
    parts = module_name.split(".")
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: -(level - 1)] if len(parts) >= level else []
    if node_module:
        parts = parts + [node_module]
    return ".".join(parts)


class ImportIntegrityTests(unittest.TestCase):
    def test_all_imports_resolve(self):
        broken: list[str] = []
        checked = 0

        for path in _iter_python_files():
            with io.open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), str(path))
            module_name = _module_name(path)
            is_package = path.name == "__init__.py"

            for node in ast.walk(tree):
                targets: list[str] = []
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        target = _resolve_relative(
                            module_name, is_package, node.level, node.module or ""
                        )
                    else:
                        target = node.module or ""
                    if target:
                        targets.append(target)
                elif isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]

                for target in targets:
                    checked += 1
                    try:
                        importlib.import_module(target)
                    except Exception as exc:  # noqa: BLE001
                        rel = path.relative_to(ROOT).as_posix()
                        broken.append(f"{rel}:{node.lineno}  {target}  →  {exc}")

        self.assertGreater(checked, 100, "扫描到的 import 太少，检查脚本是否失效")
        self.assertEqual(
            broken,
            [],
            "以下 import 无法解析（多半是合并时漏改的旧项目路径）：\n" + "\n".join(broken),
        )

    def test_no_legacy_project_imports(self):
        """旧项目顶层模块名不应该再出现在 import 语句里。

        `services/register/` 下已经是包，模块之间必须用相对导入
        （`from .sentinel import ...`），否则脱离旧项目的 sys.path hack 就断了。
        """
        legacy = {
            "sentinel",
            "sentinel_quickjs",
            "http_client",
            "config",
            "auth_flow",
            "fingerprint",
            "mail_providers",
            "sms_provider",
            "webui",
        }
        offenders: list[str] = []

        for path in _iter_python_files():
            with io.open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level:
                    continue
                root = (node.module or "").split(".")[0]
                if root in legacy:
                    rel = path.relative_to(ROOT).as_posix()
                    offenders.append(f"{rel}:{node.lineno}  from {node.module} import ...")

        self.assertEqual(
            offenders,
            [],
            "这些 import 指向旧项目的顶层模块（合并后不存在）：\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()


class ColumnMigrationTests(unittest.TestCase):
    """模型加列后，已存在的库必须能自动补上。

    SQLAlchemy 的 create_all 只建缺失的表，**不会给已存在的表加新列**。
    开发时用新建的临时库一切正常，一上生产（表早就存在）就直接 500：
    ``OperationalError: no such column: xxx``。
    """

    def test_missing_columns_are_added(self):
        import sqlite3
        import tempfile

        from sqlalchemy import create_engine

        from services.storage.migrations import ensure_columns, reset_check_cache

        tmp = tempfile.mkdtemp()
        db_path = f"{tmp}/old.db"
        con = sqlite3.connect(db_path)
        # 造一张「旧结构」的表
        con.execute(
            "CREATE TABLE reg_remail_orders ("
            " order_no TEXT PRIMARY KEY, email TEXT, suffix TEXT,"
            " service_mode TEXT, supply_policy TEXT, project_id INTEGER,"
            " order_status TEXT, service_token TEXT, pay_amount TEXT,"
            " received_at REAL, receive_started_at TEXT, after_sale_until TEXT,"
            " used_email TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL)"
        )
        con.commit()
        con.close()

        engine = create_engine(f"sqlite:///{db_path}")
        reset_check_cache()
        from sqlalchemy import inspect

        from services.storage.register_models import RemailOrderModel

        added = ensure_columns(
            engine, "reg_remail_orders", list(RemailOrderModel.__table__.columns)
        )
        self.assertIn("attempt_count", added)
        self.assertIn("last_error", added)
        self.assertIn("disabled", added)

        cols = {c["name"] for c in inspect(engine).get_columns("reg_remail_orders")}
        self.assertTrue({"attempt_count", "last_error", "disabled"} <= cols)

    def test_cache_is_per_table(self):
        """缓存 key 必须带表名 —— 只按引擎缓存的话，一次循环里
        只有第一张表会被检查，其余全部被跳过。"""
        import sqlite3
        import tempfile

        from sqlalchemy import create_engine

        from services.storage.migrations import ensure_columns, reset_check_cache

        tmp = tempfile.mkdtemp()
        db_path = f"{tmp}/two.db"
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY)")
        con.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()

        engine = create_engine(f"sqlite:///{db_path}")
        reset_check_cache()

        from sqlalchemy import Column, Integer, MetaData, Table

        md = MetaData()
        t1 = Table("t1", md, Column("id", Integer, primary_key=True), Column("extra", Integer))
        t2 = Table("t2", md, Column("id", Integer, primary_key=True), Column("extra", Integer))

        first = ensure_columns(engine, "t1", list(t1.columns))
        second = ensure_columns(engine, "t2", list(t2.columns))
        self.assertEqual(first, ["extra"])
        self.assertEqual(second, ["extra"])  # 不能因为 t1 检查过就跳过 t2

    def test_existing_columns_are_not_touched(self):
        import sqlite3
        import tempfile

        from sqlalchemy import create_engine, inspect

        from services.storage.migrations import ensure_columns, reset_check_cache

        tmp = tempfile.mkdtemp()
        db_path = f"{tmp}/ok.db"
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, extra INTEGER)")
        con.commit()
        con.close()

        engine = create_engine(f"sqlite:///{db_path}")
        reset_check_cache()

        from sqlalchemy import Column, Integer, MetaData, Table

        md = MetaData()
        t1 = Table("t1", md, Column("id", Integer, primary_key=True), Column("extra", Integer))
        self.assertEqual(ensure_columns(engine, "t1", list(t1.columns)), [])

        cols = {c["name"] for c in inspect(engine).get_columns("t1")}
        self.assertEqual(cols, {"id", "extra"})

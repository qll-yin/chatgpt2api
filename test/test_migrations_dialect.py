"""列迁移的方言兼容。

Postgres 是现在的默认部署形态，补列语句必须能在它上面跑通。
"""
from __future__ import annotations

import unittest

from sqlalchemy import Boolean, Column, Float, Integer, String, Text
from sqlalchemy.dialects import postgresql, sqlite

from services.storage.migrations import _default_literal, _quote


class DefaultLiteralTests(unittest.TestCase):
    """布尔默认值在不同方言下的字面量不一样。"""

    def _default(self, value):
        from sqlalchemy import Column, Boolean
        return Column("c", Boolean, default=value).default

    def test_postgres_uses_true_false(self):
        """⚠️ Postgres 不接受整数给布尔列。

        `ADD COLUMN remote BOOLEAN DEFAULT 0` →
        "column \"remote\" is of type boolean but default expression is of type integer"
        → 补列失败 → 后续查询直接 no such column → 500。
        """
        self.assertEqual(_default_literal(self._default(False), "postgresql"), "false")
        self.assertEqual(_default_literal(self._default(True), "postgresql"), "true")

    def test_sqlite_keeps_01(self):
        self.assertEqual(_default_literal(self._default(False), "sqlite"), "0")
        self.assertEqual(_default_literal(self._default(True), "sqlite"), "1")

    def test_mysql_keeps_01(self):
        self.assertEqual(_default_literal(self._default(False), "mysql"), "0")

    def test_numbers_and_strings(self):
        from sqlalchemy import Column, Float, String
        self.assertEqual(_default_literal(Column("c", Integer, default=0).default, "postgresql"), "0")
        self.assertEqual(_default_literal(Column("c", Float, default=0).default, "postgresql"), "0")
        self.assertEqual(_default_literal(Column("c", String, default="local").default, "postgresql"), "'local'")

    def test_string_default_escapes_quote(self):
        from sqlalchemy import Column, String
        literal = _default_literal(Column("c", String, default="it's").default, "postgresql")
        self.assertEqual(literal, "'it''s'")

    def test_none_default(self):
        from sqlalchemy import Column, String
        self.assertIsNone(_default_literal(Column("c", String, nullable=True).default, "postgresql"))


class QuoteTests(unittest.TestCase):
    def test_sqlite_quotes_with_double_quotes(self):
        self.assertEqual(_quote("image_records", "sqlite"), '"image_records"')

    def test_postgres_keeps_plain_name(self):
        self.assertEqual(_quote("image_records", "postgresql"), "image_records")

    def test_mysql_uses_backticks(self):
        self.assertEqual(_quote("image_records", "mysql"), "`image_records`")


class DdlShapeTests(unittest.TestCase):
    """按各方言拼出来的 ADD COLUMN 语句要合法。"""

    def _ddl(self, dialect, column):
        type_sql = column.type.compile(dialect=dialect)
        literal = _default_literal(column.default, dialect.name)
        parts = [_quote(column.name, dialect.name), type_sql]
        if literal is not None:
            parts.append(f"DEFAULT {literal}")
        if not column.nullable:
            parts.append("NOT NULL")
        return f"ALTER TABLE {_quote('image_records', dialect.name)} ADD COLUMN " + " ".join(parts)

    def test_postgres_boolean_column(self):
        ddl = self._ddl(postgresql.dialect(), Column("remote", Boolean, nullable=False, default=False))
        self.assertIn("BOOLEAN", ddl)
        self.assertIn("DEFAULT false", ddl)
        self.assertIn("NOT NULL", ddl)
        # Postgres 拒收整数给布尔列
        self.assertNotIn("DEFAULT 0", ddl)

    def test_postgres_string_and_float_columns(self):
        ddl = self._ddl(postgresql.dialect(), Column("remote_provider", String(16), nullable=False, default=""))
        self.assertIn("VARCHAR(16)", ddl)
        self.assertIn("DEFAULT ''", ddl)

        ddl = self._ddl(postgresql.dialect(), Column("remote_uploaded_ts", Float, nullable=True))
        self.assertIn("FLOAT", ddl)

        ddl = self._ddl(postgresql.dialect(), Column("remote_error", Text, nullable=True))
        self.assertIn("TEXT", ddl)

    def test_sqlite_boolean_column(self):
        ddl = self._ddl(sqlite.dialect(), Column("remote", Boolean, nullable=False, default=False))
        self.assertIn("BOOLEAN", ddl)
        self.assertIn("DEFAULT 0", ddl)


if __name__ == "__main__":
    unittest.main()

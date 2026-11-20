"""accounts 表从「data 单列 JSON」拆成真列后的读写行为。

关注三件事：
1. 新结构写入真列、读回来字段一致（含冷字段进 extra）
2. 老库（只有 data 一列）能直接读（快照兜底），补列脚本能回填
3. UPSERT 冲突时更新的是**所有列**（以前只更新 data，列会停留在首次写入的值）
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text

from services.storage.database_storage import AccountModel, DatabaseStorageBackend


def _temp_url() -> str:
    tmpdir = Path(tempfile.mkdtemp(prefix="accounts-col-test-"))
    return f"sqlite:///{(tmpdir / 'accounts.db').as_posix()}"


def _raw_rows(backend: DatabaseStorageBackend) -> list[dict]:
    with backend.engine.begin() as conn:
        result = conn.execute(text("SELECT * FROM accounts ORDER BY access_token"))
        return [dict(row._mapping) for row in result]


class AccountColumnTests(unittest.TestCase):
    def test_roundtrip_keeps_fields_and_fills_columns(self):
        backend = DatabaseStorageBackend(_temp_url())
        accounts = [
            {
                "access_token": "tok-1",
                "email": "a@example.com",
                "type": "Plus",
                "status": "正常",
                "quota": 25,
                "success": 15,
                "fail": 227,
                "source_type": "register",
                "proxy": "http://p:1",
                "user_id": "user-1",
                "created_at": "2026-11-20 10:00:00",
                "last_used_at": "2026-11-20 11:00:00",
                "restore_at": None,
                "invalid_count": 0,
                "default_model_slug": "gpt-5",
                "limits_progress": [{"feature": "image", "remaining": 10}],
                "last_refresh_error": None,
            }
        ]
        backend.save_accounts(accounts)

        rows = _raw_rows(backend)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["email"], "a@example.com")
        self.assertEqual(row["status"], "正常")
        self.assertEqual(row["quota"], 25)
        self.assertEqual(row["fail"], 227)
        self.assertEqual(row["proxy"], "http://p:1")
        self.assertIn("limits_progress", json.loads(row["extra"]))
        # 完整快照仍然写了一份
        self.assertEqual(json.loads(row["data"])["access_token"], "tok-1")

        loaded = backend.load_accounts()
        self.assertEqual(len(loaded), 1)
        item = loaded[0]
        self.assertEqual(item["email"], "a@example.com")
        self.assertEqual(item["quota"], 25)
        self.assertEqual(item["fail"], 227)
        self.assertEqual(item["status"], "正常")
        self.assertEqual(item["limits_progress"], [{"feature": "image", "remaining": 10}])
        self.assertIsNone(item["restore_at"])

    def test_upsert_updates_real_columns(self):
        backend = DatabaseStorageBackend(_temp_url())
        account = {
            "access_token": "tok-1",
            "email": "a@example.com",
            "status": "正常",
            "quota": 5,
            "created_at": "2026-11-20 10:00:00",
        }
        backend.save_accounts([account])

        account["quota"] = 4
        account["status"] = "限流"
        backend.save_accounts([account])

        row = _raw_rows(backend)[0]
        self.assertEqual(row["quota"], 4, "UPSERT 必须更新 quota 列，而不是只更新 data")
        self.assertEqual(row["status"], "限流")

    def test_save_single_account_touches_only_that_row(self):
        """单账号保存不能删掉别人（save_accounts 的差集删除语义不适用）。"""
        backend = DatabaseStorageBackend(_temp_url())
        first = {"access_token": "tok-1", "status": "正常", "quota": 5, "created_at": "2026-11-20 10:00:00"}
        second = {"access_token": "tok-2", "status": "正常", "quota": 9, "created_at": "2026-11-20 10:00:00"}
        backend.save_accounts([first, second])

        first["quota"] = 4
        backend.save_account(first)

        rows = {row["access_token"]: row for row in _raw_rows(backend)}
        self.assertEqual(len(rows), 2, "单行保存不应该删掉其他账号")
        self.assertEqual(rows["tok-1"]["quota"], 4)
        self.assertEqual(rows["tok-2"]["quota"], 9)

    def test_removed_accounts_are_deleted(self):
        backend = DatabaseStorageBackend(_temp_url())
        backend.save_accounts(
            [
                {"access_token": "tok-1", "status": "正常", "created_at": "2026-11-20 10:00:00"},
                {"access_token": "tok-2", "status": "正常", "created_at": "2026-11-20 10:00:00"},
            ]
        )
        backend.save_accounts(
            [{"access_token": "tok-1", "status": "正常", "created_at": "2026-11-20 10:00:00"}]
        )
        rows = _raw_rows(backend)
        self.assertEqual([row["access_token"] for row in rows], ["tok-1"])


class LegacyAccountTableTests(unittest.TestCase):
    def _make_legacy_db(self, url: str) -> None:
        """造一个只有 data 一列的老库，并塞一行老数据。"""
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE accounts ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "access_token VARCHAR(2048) NOT NULL UNIQUE, "
                    "data TEXT NOT NULL)"
                )
            )
            payload = {
                "access_token": "legacy-1",
                "email": "old@example.com",
                "status": "正常",
                "quota": 7,
                "fail": 30,
                "success": 2,
                "source_type": "outlook",
                "limits_progress": [{"feature": "image"}],
            }
            conn.execute(
                text("INSERT INTO accounts (access_token, data) VALUES (:t, :d)"),
                {"t": "legacy-1", "d": json.dumps(payload, ensure_ascii=False)},
            )
        engine.dispose()

    def test_legacy_rows_are_readable_before_backfill(self):
        url = _temp_url()
        self._make_legacy_db(url)

        backend = DatabaseStorageBackend(url)  # 构造时补列 + 补索引
        loaded = backend.load_accounts()
        self.assertEqual(len(loaded), 1)
        # 没回填（created_at 为 NULL）→ 走完整快照，字段一个不少
        self.assertEqual(loaded[0]["email"], "old@example.com")
        self.assertEqual(loaded[0]["quota"], 7)
        self.assertEqual(loaded[0]["limits_progress"], [{"feature": "image"}])

    def test_backfill_splits_columns_and_is_idempotent(self):
        url = _temp_url()
        self._make_legacy_db(url)
        backend = DatabaseStorageBackend(url)

        first = backend.backfill_account_columns(batch_size=10)
        self.assertEqual(first["scanned"], 1)
        self.assertEqual(first["updated"], 1)

        row = _raw_rows(backend)[0]
        self.assertEqual(row["email"], "old@example.com")
        self.assertEqual(row["quota"], 7)
        self.assertEqual(row["fail"], 30)
        self.assertIsNotNone(row["created_at"])
        self.assertIn("limits_progress", json.loads(row["extra"]))

        # 再跑一次不会重复处理
        second = backend.backfill_account_columns(batch_size=10)
        self.assertEqual(second["scanned"], 0)
        self.assertEqual(second["updated"], 0)

        # 回填后仍然能正常读出
        loaded = backend.load_accounts()
        self.assertEqual(loaded[0]["quota"], 7)
        self.assertEqual(loaded[0]["limits_progress"], [{"feature": "image"}])

    def test_dry_run_does_not_write(self):
        url = _temp_url()
        self._make_legacy_db(url)
        backend = DatabaseStorageBackend(url)

        result = backend.backfill_account_columns(batch_size=10, dry_run=True)
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertIsNone(_raw_rows(backend)[0]["created_at"], "dry-run 不应写库")


class IndexTests(unittest.TestCase):
    def test_accounts_indexes_exist(self):
        from sqlalchemy import inspect

        backend = DatabaseStorageBackend(_temp_url())
        backend.save_accounts(
            [{"access_token": "tok-1", "status": "正常", "created_at": "2026-11-20 10:00:00"}]
        )
        names = {item["name"] for item in inspect(backend.engine).get_indexes("accounts")}
        self.assertIn("ix_accounts_status_quota", names)
        self.assertIn("ix_accounts_email", names)
        self.assertIn("ix_accounts_status", names)
        self.assertIn("ix_accounts_access_token", names)


if __name__ == "__main__":
    unittest.main()

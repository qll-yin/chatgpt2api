"""日志入库（含账号邮箱筛选）+ 生成失败自动标异常。

背景
----
1. 日志以前是 data/logs.jsonl：日志页每次把整个文件读进内存倒扫，
   想按「账号邮箱」查某个号的生成记录只能逐行反序列化。现在落库，
   邮箱单独成列并建索引。
2. 号池里有些号额度没用完但一张图都生不出来（失败 227 / 成功 15 / 剩余 10），
   留着重试只会拖慢整体。现在按累计失败次数定时巡检，达阈值标「异常」。

⚠️ 必须在导入业务模块**之前**把数据库指向临时库。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test.db_fixture import use_temp_database

use_temp_database()

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services import fail_guard
from services.log_service import LogService
from services.storage import runtime_store


class LogDatabaseTests(unittest.TestCase):
    def setUp(self):
        from services.storage.db import runtime_session_scope
        from services.storage.runtime_models import LogModel
        from sqlalchemy import delete

        with runtime_session_scope() as session:
            session.execute(delete(LogModel))
        self.tmpdir = Path(tempfile.mkdtemp(prefix="logs-test-"))
        self.service = LogService(self.tmpdir / "logs.jsonl")

    def test_add_and_list(self):
        self.service.add("call", "调用完成", {"account_email": "a@x.com", "status": "success"})
        items = self.service.list(type="call")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["summary"], "调用完成")
        self.assertEqual(items[0]["detail"]["account_email"], "a@x.com")

    def test_filter_by_account_email_is_fuzzy_and_case_insensitive(self):
        self.service.add("call", "ok", {"account_email": "Alpha@Example.com"})
        self.service.add("call", "ok", {"account_email": "beta@example.com"})
        self.service.add("account", "新增账号", {"email": "alpha@example.com"})

        # 模糊包含 + 忽略大小写
        items = self.service.list(type="call", account_email="alpha")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["detail"]["account_email"], "Alpha@Example.com")

        # 子串命中
        self.assertEqual(len(self.service.list(type="call", account_email="example.com")), 2)
        # 命不中就空
        self.assertEqual(self.service.list(type="call", account_email="nobody"), [])

    def test_filter_by_date_range(self):
        from services.storage import runtime_store as store

        store.insert_log(
            {
                "id": "old-1",
                "time": "2026-01-01 10:00:00",
                "type": "call",
                "summary": "old",
                "detail": {"account_email": "a@x.com"},
            }
        )
        store.insert_log(
            {
                "id": "new-1",
                "time": "2026-11-20 10:00:00",
                "type": "call",
                "summary": "new",
                "detail": {"account_email": "a@x.com"},
            }
        )
        self.assertEqual(len(self.service.list(type="call", start_date="2026-11-01")), 1)
        self.assertEqual(len(self.service.list(type="call", end_date="2026-01-31")), 1)

    def test_delete(self):
        self.service.add("call", "one", {"account_email": "a@x.com"})
        self.service.add("call", "two", {"account_email": "b@x.com"})
        ids = [item["id"] for item in self.service.list(type="call")]
        removed = self.service.delete(ids[:1])
        self.assertEqual(removed["removed"], 1)
        self.assertEqual(len(self.service.list(type="call")), 1)

    def test_export_jsonl_lines(self):
        self.service.add("call", "one", {"account_email": "a@x.com"})
        payload = self.service.export_jsonl_bytes().decode("utf-8")
        lines = [line for line in payload.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["summary"], "one")

    def test_legacy_file_is_imported_once(self):
        legacy = self.tmpdir / "logs.jsonl"
        legacy.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "id": "legacy-1",
                            "time": "2026-11-19 09:00:00",
                            "type": "call",
                            "summary": "老日志",
                            "detail": {"account_email": "old@x.com"},
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        service = LogService(legacy)
        items = service.list(type="call")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["summary"], "老日志")
        # 邮箱筛选同样能用（说明导入时把邮箱提到列上了）
        self.assertEqual(len(service.list(type="call", account_email="old@x")), 1)
        # marker 已写 → 再建一个实例不会重复导入
        service2 = LogService(legacy)
        self.assertEqual(len(service2.list(type="call")), 1)


class _FakeAccountService:
    def __init__(self, items):
        self.items = [dict(item) for item in items]
        self.updates: list[tuple[str, dict]] = []

    def list_accounts(self):
        return [dict(item) for item in self.items]

    def update_account(self, access_token, updates, quiet=False):
        self.updates.append((access_token, dict(updates)))
        for item in self.items:
            if item.get("access_token") == access_token:
                item.update(updates)
                return dict(item)
        return None


class _FakeLogService:
    def __init__(self):
        self.items = []

    def add(self, type, summary="", detail=None, **data):
        self.items.append((type, summary, detail or data))


class FailGuardTests(unittest.TestCase):
    def _run(self, accounts, threshold=20, fake_accounts=None):
        fake_accounts = fake_accounts or _FakeAccountService(accounts)
        fake_logs = _FakeLogService()
        with mock.patch.object(fail_guard, "account_service", fake_accounts), \
             mock.patch.object(fail_guard, "log_service", fake_logs):
            result = fail_guard.scan_once(threshold=threshold)
        return result, fake_accounts, fake_logs

    def test_marks_account_at_threshold(self):
        result, accounts, logs = self._run(
            [{"access_token": "t1", "email": "bad@x.com", "status": "正常", "fail": 20, "quota": 10}]
        )
        self.assertEqual(len(result["marked"]), 1)
        self.assertEqual(accounts.updates[0][0], "t1")
        self.assertEqual(accounts.updates[0][1]["status"], "异常")
        # 额度不动
        self.assertNotIn("quota", accounts.updates[0][1])
        self.assertEqual(accounts.items[0]["quota"], 10)
        self.assertTrue(logs.items, "标记异常要留一条账号日志")

    def test_below_threshold_is_untouched(self):
        result, accounts, _ = self._run(
            [{"access_token": "t1", "email": "ok@x.com", "status": "正常", "fail": 19}]
        )
        self.assertEqual(result["marked"], [])
        self.assertEqual(accounts.updates, [])

    def test_skips_already_handled_statuses(self):
        result, accounts, _ = self._run(
            [
                {"access_token": "t1", "status": "异常", "fail": 999},
                {"access_token": "t2", "status": "禁用", "fail": 999},
                {"access_token": "t3", "status": "限流", "fail": 999},
            ]
        )
        self.assertEqual(result["marked"], [])
        self.assertEqual(accounts.updates, [])
        self.assertEqual(result["checked"], 0)

    def test_idempotent_after_marking(self):
        fake = _FakeAccountService(
            [{"access_token": "t1", "email": "bad@x.com", "status": "正常", "fail": 30}]
        )
        first, _, _ = self._run(None, fake_accounts=fake)
        self.assertEqual(len(first["marked"]), 1)
        # 第二次：状态已经变成异常了 → 不会再动它
        second, fake_accounts, _ = self._run(None, fake_accounts=fake)
        self.assertEqual(second["marked"], [])
        self.assertEqual(fake_accounts.updates[0][1]["status"], "异常")
        self.assertEqual(len(fake_accounts.updates), 1, "同一个号不该被重复标记")

    def test_default_threshold_comes_from_config(self):
        from services.config import config

        original = config.data.get("fail_guard_threshold")
        config.data["fail_guard_threshold"] = 5
        try:
            result, _, _ = self._run(
                [{"access_token": "t1", "status": "正常", "fail": 5}], threshold=None
            )
            self.assertEqual(result["threshold"], 5)
            self.assertEqual(len(result["marked"]), 1)
        finally:
            if original is None:
                config.data.pop("fail_guard_threshold", None)
            else:
                config.data["fail_guard_threshold"] = original


class FailGuardConfigTests(unittest.TestCase):
    def test_defaults_are_off_and_20_and_60(self):
        from services.config import config

        original = {
            key: config.data.pop(key, None)
            for key in ("fail_guard_enabled", "fail_guard_threshold", "fail_guard_interval_minute")
        }
        try:
            self.assertFalse(config.fail_guard_enabled)
            self.assertEqual(config.fail_guard_threshold, 20)
            self.assertEqual(config.fail_guard_interval_minute, 60)
        finally:
            for key, value in original.items():
                if value is not None:
                    config.data[key] = value


if __name__ == "__main__":
    unittest.main()

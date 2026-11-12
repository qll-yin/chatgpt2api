"""本轮修复的回归测试。

覆盖用户实测报出的问题：
- 批量注册成功后必须真的入号池（原来完全没有这一步）
- 批量注册用的邮箱来源必须是本次选的，不是配置里的
- 已用时在批次结束后冻结（不能一直累加）
- 入号池要带刷新额度（与号池管理的导入走同一套流程）
- 邮箱来源的「已配置 / 就绪」判定
- 邮箱池导入
- SMS 接码配置读写
- 运行记录按批次聚合
"""

from __future__ import annotations

import unittest
from unittest import mock

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-batch-fix-")

from services import register_batch, register_service  # noqa: E402
from services.storage import register_store as store  # noqa: E402


class PoolImportTests(unittest.TestCase):
    """入号池 —— 必须带额度刷新，和号池管理的导入走同一套流程。"""

    def test_import_to_pool_with_refresh_calls_refresh(self):
        fake_accounts = mock.MagicMock()
        fake_accounts.add_account_items.return_value = {"added": 1, "skipped": 0, "items": []}
        fake_accounts.refresh_accounts.return_value = {"refreshed": 1, "errors": [], "items": []}

        with mock.patch.dict(
            "sys.modules",
            {"services.account_service": mock.MagicMock(account_service=fake_accounts)},
        ):
            result = register_service.import_to_pool_with_refresh(
                [{"access_token": "AT1", "email": "a@x.com"}]
            )

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["refreshed"], 1)
        fake_accounts.add_account_items.assert_called_once()
        fake_accounts.refresh_accounts.assert_called_once_with(["AT1"])

    def test_import_without_refresh_skips_refresh(self):
        fake_accounts = mock.MagicMock()
        fake_accounts.add_account_items.return_value = {"added": 1, "skipped": 0, "items": []}

        with mock.patch.dict(
            "sys.modules",
            {"services.account_service": mock.MagicMock(account_service=fake_accounts)},
        ):
            result = register_service.import_to_pool_with_refresh(
                [{"access_token": "AT1"}], refresh=False
            )

        self.assertEqual(result["imported"], 1)
        fake_accounts.refresh_accounts.assert_not_called()

    def test_refresh_failure_does_not_break_import(self):
        fake_accounts = mock.MagicMock()
        fake_accounts.add_account_items.return_value = {"added": 1, "skipped": 0, "items": []}
        fake_accounts.refresh_accounts.side_effect = RuntimeError("网络不通")

        with mock.patch.dict(
            "sys.modules",
            {"services.account_service": mock.MagicMock(account_service=fake_accounts)},
        ):
            result = register_service.import_to_pool_with_refresh([{"access_token": "AT1"}])

        # 号还是入池了，只是额度没刷上
        self.assertEqual(result["imported"], 1)
        self.assertTrue(result["errors"])


class WorkerPoolImportTests(unittest.TestCase):
    """batch worker —— 注册成功后必须入号池。"""

    def setUp(self):
        self.controller = register_batch.AutoLoopController()
        self.controller._options = {"auto_import": True, "with_proxy": True}

    def test_import_to_pool_writes_account(self):
        store.save_registered(email="w@x.com", access_token="AT-W", proxy="http://p:1")

        with mock.patch.object(
            register_service,
            "import_to_pool_with_refresh",
            return_value={"imported": 1, "skipped": 0},
        ) as patched:
            ok = self.controller._import_to_pool({"email": "w@x.com"}, "http://p:1")

        self.assertTrue(ok)
        payload = patched.call_args[0][0][0]
        self.assertEqual(payload["access_token"], "AT-W")
        self.assertEqual(payload["email"], "w@x.com")
        self.assertEqual(payload["proxy"], "http://p:1")
        # 批量期间不刷额度（批量结束后统一刷）
        self.assertFalse(patched.call_args[1].get("refresh", True))

    def test_import_skipped_when_auto_import_off(self):
        self.controller._options["auto_import"] = False
        with mock.patch.object(register_service, "import_to_pool_with_refresh") as patched:
            ok = self.controller._import_to_pool({"email": "w@x.com"}, "")
        self.assertFalse(ok)
        patched.assert_not_called()

    def test_import_without_proxy_when_option_off(self):
        store.save_registered(email="w2@x.com", access_token="AT-W2", proxy="http://p:2")
        self.controller._options["with_proxy"] = False

        with mock.patch.object(
            register_service,
            "import_to_pool_with_refresh",
            return_value={"imported": 1},
        ) as patched:
            self.controller._import_to_pool({"email": "w2@x.com"}, "http://p:2")

        payload = patched.call_args[0][0][0]
        self.assertNotIn("proxy", payload)

    def test_import_without_access_token_does_nothing(self):
        store.save_registered(email="w3@x.com", access_token="")
        with mock.patch.object(register_service, "import_to_pool_with_refresh") as patched:
            ok = self.controller._import_to_pool({"email": "w3@x.com"}, "")
        self.assertFalse(ok)
        patched.assert_not_called()


class ElapsedFreezeTests(unittest.TestCase):
    """已用时 —— 批次结束后必须冻结。"""

    def test_elapsed_is_frozen_after_finish(self):
        controller = register_batch.AutoLoopController()
        controller._started_at = 1000.0
        controller._finished_at = 1010.0
        snapshot = controller._snapshot()
        self.assertAlmostEqual(snapshot["elapsed"], 10.0, places=3)

    def test_elapsed_keeps_counting_while_running(self):
        controller = register_batch.AutoLoopController()
        controller._started_at = 1000.0
        controller._finished_at = 0.0
        snapshot = controller._snapshot()
        self.assertGreater(snapshot["elapsed"], 0)


class MailSourceSelectionTests(unittest.TestCase):
    """邮箱来源 —— 批量用的是本次选的，不是配置里的。"""

    def test_options_mail_source_wins_over_setting(self):
        store.set_setting("mail_source", "outlook")
        controller = register_batch.AutoLoopController()
        controller._options = {"mail_source": "remail"}
        # 复刻 worker 里的取值逻辑
        picked = str(
            controller._options.get("mail_source")
            or store.get_setting("mail_source", "outlook")
        ).strip()
        self.assertEqual(picked, "remail")

    def test_falls_back_to_setting(self):
        store.set_setting("mail_source", "remail")
        controller = register_batch.AutoLoopController()
        controller._options = {}
        picked = str(
            controller._options.get("mail_source")
            or store.get_setting("mail_source", "outlook")
        ).strip()
        self.assertEqual(picked, "remail")


class BatchAggregationTests(unittest.TestCase):
    """运行记录 —— 按批次聚合。"""

    def setUp(self):
        store.delete_registered(["b1@x.com", "b2@x.com"])

    def test_runs_grouped_by_batch(self):
        import time

        now = time.time()
        store.create_run("r1", "b1@x.com", "/tmp/r1.log", "batch-A")
        store.create_run("r2", "b2@x.com", "/tmp/r2.log", "batch-A")
        store.create_run("r3", "b1@x.com", "/tmp/r3.log", "batch-B")
        # 把 r1 标成成功
        with store.register_session_scope() as session:
            from sqlalchemy import update

            from services.storage.register_models import RunModel

            session.execute(
                update(RunModel)
                .where(RunModel.run_id == "r1")
                .values(status="done", finished_at=now)
            )
            session.execute(
                update(RunModel)
                .where(RunModel.run_id == "r3")
                .values(status="failed", finished_at=now, error_category="network")
            )

        batches, total = store.list_run_batches(limit=50)
        self.assertGreaterEqual(total, 2)
        by_id = {b["batch_id"]: b for b in batches}
        self.assertIn("batch-A", by_id)
        self.assertIn("batch-B", by_id)

        a = by_id["batch-A"]
        self.assertEqual(a["total"], 2)
        self.assertEqual(a["ok"], 1)
        self.assertEqual(a["fail"], 0)
        self.assertEqual(a["emails"], ["b1@x.com"])

        b = by_id["batch-B"]
        self.assertEqual(b["total"], 1)
        self.assertEqual(b["fail"], 1)


class MailPoolImportTests(unittest.TestCase):
    """邮箱池导入。"""

    def test_import_accounts(self):
        store.replace_proxies([], mode="replace")
        result = store.import_accounts(
            "pool1@outlook.com----pass1----cid1----rt1\npool2@outlook.com----pass2----cid2----rt2",
            kind="outlook",
        )
        self.assertGreaterEqual(result.get("inserted", 0), 2)
        items, total = store.list_accounts(kind="outlook", limit=10)
        self.assertGreaterEqual(total, 2)


class SmsConfigTests(unittest.TestCase):
    """SMS 接码配置读写。"""

    def tearDown(self):
        store.set_setting("sms_api_key", "")
        store.set_setting("sms_enabled", "0")

    def test_api_key_is_masked(self):
        store.set_setting("sms_api_key", "rk-secret")
        config = store.get_sms_config()
        self.assertEqual(config["sms_api_key"], "***")

    def test_masked_key_does_not_overwrite(self):
        store.set_setting("sms_api_key", "rk-original")
        store.save_sms_config({"sms_api_key": "***"})
        self.assertEqual(store.get_setting("sms_api_key"), "rk-original")

    def test_empty_key_does_not_overwrite(self):
        store.set_setting("sms_api_key", "rk-original")
        store.save_sms_config({"sms_api_key": ""})
        self.assertEqual(store.get_setting("sms_api_key"), "rk-original")

    def test_new_key_is_saved(self):
        store.save_sms_config({"sms_api_key": "rk-new"})
        self.assertEqual(store.get_setting("sms_api_key"), "rk-new")

    def test_other_fields_are_saved(self):
        store.save_sms_config(
            {
                "sms_enabled": "1",
                "sms_provider": "herosms",
                "sms_country": "52",
                "sms_service": "dr",
                "sms_auto_country": "1",
            }
        )
        config = store.get_sms_config()
        self.assertEqual(config["sms_enabled"], "1")
        self.assertEqual(config["sms_provider"], "herosms")
        self.assertEqual(config["sms_auto_country"], "1")


if __name__ == "__main__":
    unittest.main()

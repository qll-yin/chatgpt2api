"""号池自动补充守护的测试。

覆盖：
- 阈值判断：可用账号 ≥ 阈值不触发；< 阈值触发
- 目标批量数正确传给批量控制器（target_count）
- 冷却期内不重复触发
- 批量注册已在跑时不重复触发（幂等）
- 控制器报错时记录错误但不崩溃
- 配置归一化：非法数值被钳制
- 状态记录正确

守护是**进程内调用**（注册机已合并进本项目），所以这里 mock 的是
`services.register_batch.CONTROLLER`，不涉及任何 HTTP。
"""

from __future__ import annotations

import unittest
from unittest import mock

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-auto-register-")

from services import auto_register_guardian as guardian  # noqa: E402


BASE_SETTINGS = {
    "enabled": True,
    "threshold": 10,
    "target_batch": 20,
    "check_interval_minute": 30,
    "concurrency": 3,
    "register_options": {},
    "cooldown_minute": 30,
}


class FakeController:
    """批量控制器的替身。"""

    def __init__(self):
        self.state = "idle"
        self.start_calls: list[dict] = []
        self.start_error = ""
        self.status_calls = 0

    def start(self, options: dict):
        self.start_calls.append(dict(options or {}))
        if self.start_error:
            return {"ok": False, "error": self.start_error}
        self.state = "running"
        return {"ok": True, "state": "running", "concurrency": options.get("concurrency")}

    def status(self):
        self.status_calls += 1
        return {"state": self.state}


class AutoRegisterGuardianTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(BASE_SETTINGS)
        self.controller = FakeController()

        # 用替身替换真实的批量控制器
        self.batch_patcher = mock.patch(
            "services.register_batch.CONTROLLER", self.controller
        )
        self.batch_patcher.start()
        self.addCleanup(self.batch_patcher.stop)

        self.settings_patcher = mock.patch.object(
            guardian, "get_settings", side_effect=lambda: dict(self.settings)
        )
        self.settings_patcher.start()
        self.addCleanup(self.settings_patcher.stop)

        self.active_patcher = mock.patch.object(guardian, "_active_account_count", return_value=0)
        self.active_patcher.start()
        self.addCleanup(self.active_patcher.stop)

        guardian._set_state(
            last_trigger_at=0.0,
            last_error="",
            last_trigger_result="",
            trigger_count=0,
            last_check_at=0.0,
            last_check_active=-1,
        )

    def test_below_threshold_triggers_registration(self):
        result = guardian._check_once()
        self.assertFalse(result.get("skipped"))
        self.assertTrue(result.get("triggered"))
        self.assertEqual(len(self.controller.start_calls), 1)

    def test_target_batch_is_passed_as_target_count(self):
        guardian._check_once()
        self.assertEqual(self.controller.start_calls[0]["target_count"], 20)

    def test_concurrency_is_passed(self):
        guardian._check_once()
        self.assertEqual(self.controller.start_calls[0]["concurrency"], 3)

    def test_auto_import_is_enabled_by_default(self):
        """守护的整个目的就是补号池 —— 注册成功必须自动入池。"""
        guardian._check_once()
        self.assertTrue(self.controller.start_calls[0]["auto_import"])

    def test_max_retries_is_forwarded(self):
        """守护触发的注册走同一套批量控制器 —— 出口 IP 不通时同样要重试。

        否则一次 CF 拦截就白烧一批付费邮箱然后收工。
        """
        guardian._check_once()
        self.assertEqual(self.controller.start_calls[0]["max_retries"], 3)

    def test_max_retries_can_be_configured(self):
        self.settings["max_retries"] = 7
        guardian._check_once()
        self.assertEqual(self.controller.start_calls[0]["max_retries"], 7)

    def test_register_options_can_override_max_retries(self):
        """用户在 register_options 里显式写了就以那个为准。"""
        self.settings["register_options"] = {"max_retries": 1}
        guardian._check_once()
        self.assertEqual(self.controller.start_calls[0]["max_retries"], 1)

    def test_register_options_are_forwarded(self):
        self.settings["register_options"] = {"want_refresh_token": True, "mail_source": "remail"}
        guardian._check_once()
        payload = self.controller.start_calls[0]
        self.assertTrue(payload["want_refresh_token"])
        self.assertEqual(payload["mail_source"], "remail")
        self.assertEqual(payload["target_count"], 20)

    def test_above_threshold_does_not_trigger(self):
        with mock.patch.object(guardian, "_active_account_count", return_value=50):
            result = guardian._check_once()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(len(self.controller.start_calls), 0)

    def test_equal_to_threshold_does_not_trigger(self):
        with mock.patch.object(guardian, "_active_account_count", return_value=10):
            result = guardian._check_once()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(len(self.controller.start_calls), 0)

    def test_disabled_does_not_trigger(self):
        self.settings["enabled"] = False
        result = guardian._check_once()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(len(self.controller.start_calls), 0)

    def test_cooldown_prevents_retrigger(self):
        import time

        guardian._set_state(last_trigger_at=time.time())
        result = guardian._check_once()
        self.assertTrue(result.get("skipped"))
        self.assertIn("冷却", result.get("reason", ""))
        self.assertEqual(len(self.controller.start_calls), 0)

    def test_skips_when_batch_already_running(self):
        self.controller.state = "running"
        result = guardian._check_once()
        self.assertTrue(result.get("skipped"))
        self.assertIn("批量注册已在运行", result.get("reason", ""))
        # 只查了状态，没有发起注册
        self.assertEqual(len(self.controller.start_calls), 0)
        self.assertEqual(self.controller.status_calls, 1)

    def test_paused_batch_also_blocks(self):
        self.controller.state = "paused"
        result = guardian._check_once()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(len(self.controller.start_calls), 0)

    def test_controller_error_is_recorded(self):
        self.controller.start_error = "已经在跑了"
        result = guardian._check_once()
        self.assertFalse(result.get("triggered"))
        self.assertIn("已经在跑了", str(result.get("error")))
        self.assertIn("已经在跑了", guardian.get_state().get("last_error", ""))

    def test_controller_exception_is_recorded(self):
        def boom(options):
            raise RuntimeError("启动器崩了")

        self.controller.start = boom
        result = guardian._check_once()
        self.assertFalse(result.get("triggered"))
        self.assertIn("启动器崩了", str(result.get("error")))
        self.assertIn("启动器崩了", guardian.get_state().get("last_error", ""))

    def test_state_records_trigger_count(self):
        guardian._check_once()
        self.assertEqual(guardian.get_state().get("trigger_count"), 1)
        self.assertGreater(guardian.get_state().get("last_trigger_at", 0), 0)


class AutoRegisterConfigTests(unittest.TestCase):
    """配置归一化：非法输入必须被钳制，不能导致失控行为。"""

    def test_defaults(self):
        from services.config import _normalize_auto_register_settings

        result = _normalize_auto_register_settings({})
        self.assertFalse(result["enabled"])
        self.assertEqual(result["threshold"], 10)
        self.assertEqual(result["target_batch"], 20)
        self.assertEqual(result["check_interval_minute"], 30)
        self.assertEqual(result["concurrency"], 3)
        self.assertEqual(result["max_retries"], 3)

    def test_numeric_clamping(self):
        from services.config import _normalize_auto_register_settings

        result = _normalize_auto_register_settings({
            "threshold": -5,
            "target_batch": 0,
            "check_interval_minute": 0,
            "cooldown_minute": -1,
            "concurrency": 999,
        })
        self.assertEqual(result["threshold"], 0)
        self.assertEqual(result["target_batch"], 1)
        self.assertEqual(result["check_interval_minute"], 1)
        self.assertEqual(result["cooldown_minute"], 0)
        self.assertEqual(result["concurrency"], 20)

    def test_invalid_numbers_fall_back_to_default(self):
        from services.config import _normalize_auto_register_settings

        result = _normalize_auto_register_settings({"threshold": "abc", "target_batch": None})
        self.assertEqual(result["threshold"], 10)
        self.assertEqual(result["target_batch"], 20)

    def test_register_options_must_be_dict(self):
        from services.config import _normalize_auto_register_settings

        result = _normalize_auto_register_settings({"register_options": "not-a-dict"})
        self.assertEqual(result["register_options"], {})

    def test_no_external_register_url_setting(self):
        """注册机已合并进本项目，不该再有「注册机地址」这种外部依赖配置。"""
        from services.config import _normalize_auto_register_settings

        result = _normalize_auto_register_settings({"register_url": "http://register:8765"})
        self.assertNotIn("register_url", result)
        self.assertNotIn("register_auth_key", result)


if __name__ == "__main__":
    unittest.main()

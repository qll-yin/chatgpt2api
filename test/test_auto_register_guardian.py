"""号池自动补充守护的测试。

覆盖：
- 阈值判断：可用账号 ≥ 阈值不触发；< 阈值触发
- 目标批量数正确传给注册机（target_count）
- 冷却期内不重复触发
- 注册机已有任务在跑时不重复触发（幂等）
- 注册机不可达时记录错误但不崩溃
- 配置归一化：非法数值被钳制
- 状态记录正确
"""

from __future__ import annotations

import unittest
from unittest import mock

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-auto-register-")

from services import auto_register_guardian as guardian  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


BASE_SETTINGS = {
    "enabled": True,
    "threshold": 10,
    "target_batch": 20,
    "check_interval_minute": 30,
    "register_url": "http://register:8765",
    "register_auth_key": "",
    "register_options": {},
    "cooldown_minute": 30,
}


class AutoRegisterGuardianTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(BASE_SETTINGS)
        self.post_calls: list[dict] = []
        self.get_calls: list[str] = []
        # 注册机状态：默认「没有任务在跑」
        self.register_state = "idle"

        def fake_post(url, json=None, headers=None, timeout=None):
            self.post_calls.append({"url": url, "json": json, "headers": headers})
            if self.register_state == "running":
                return FakeResponse(400, {"ok": False, "error": "已经在跑了"})
            return FakeResponse(200, {"ok": True, "state": "running"})

        def fake_get(url, headers=None, timeout=None):
            self.get_calls.append(url)
            return FakeResponse(200, {"state": self.register_state})

        self.post_patcher = mock.patch.object(guardian.requests, "post", side_effect=fake_post)
        self.get_patcher = mock.patch.object(guardian.requests, "get", side_effect=fake_get)
        self.post_patcher.start()
        self.get_patcher.start()
        self.addCleanup(self.post_patcher.stop)
        self.addCleanup(self.get_patcher.stop)

        self.settings_patcher = mock.patch.object(guardian, "get_settings", side_effect=lambda: dict(self.settings))
        self.settings_patcher.start()
        self.addCleanup(self.settings_patcher.stop)

        self.active_patcher = mock.patch.object(guardian, "_active_account_count", return_value=0)
        self.active_patcher.start()
        self.addCleanup(self.active_patcher.stop)

        # 重置状态与冷却时间
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
        self.assertEqual(len(self.post_calls), 1)
        self.assertEqual(self.post_calls[0]["url"], "http://register:8765/api/auto/start")

    def test_target_batch_is_passed_as_target_count(self):
        guardian._check_once()
        self.assertEqual(self.post_calls[0]["json"]["target_count"], 20)

    def test_register_options_are_forwarded(self):
        self.settings["register_options"] = {"concurrency": 3, "want_refresh_token": True}
        guardian._check_once()
        payload = self.post_calls[0]["json"]
        self.assertEqual(payload["concurrency"], 3)
        self.assertTrue(payload["want_refresh_token"])
        self.assertEqual(payload["target_count"], 20)

    def test_above_threshold_does_not_trigger(self):
        self.active_patcher.stop()
        with mock.patch.object(guardian, "_active_account_count", return_value=50):
            result = guardian._check_once()
        self.active_patcher.start()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(len(self.post_calls), 0)

    def test_equal_to_threshold_does_not_trigger(self):
        self.active_patcher.stop()
        with mock.patch.object(guardian, "_active_account_count", return_value=10):
            result = guardian._check_once()
        self.active_patcher.start()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(len(self.post_calls), 0)

    def test_disabled_does_not_trigger(self):
        self.settings["enabled"] = False
        result = guardian._check_once()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(len(self.post_calls), 0)

    def test_cooldown_prevents_retrigger(self):
        import time

        guardian._set_state(last_trigger_at=time.time())
        result = guardian._check_once()
        self.assertTrue(result.get("skipped"))
        self.assertIn("冷却", result.get("reason", ""))
        self.assertEqual(len(self.post_calls), 0)

    def test_skips_when_register_already_running(self):
        self.register_state = "running"
        result = guardian._check_once()
        self.assertTrue(result.get("skipped"))
        self.assertIn("注册机已有任务在运行", result.get("reason", ""))
        # 只查询了状态，没有发起注册
        self.assertEqual(len(self.post_calls), 0)
        self.assertEqual(len(self.get_calls), 1)

    def test_register_error_is_recorded(self):
        def failing_post(url, json=None, headers=None, timeout=None):
            raise RuntimeError("connection refused")

        with mock.patch.object(guardian.requests, "post", side_effect=failing_post):
            result = guardian._check_once()
        self.assertFalse(result.get("triggered"))
        self.assertIn("connection refused", str(result.get("error")))
        self.assertIn("connection refused", guardian.get_state().get("last_error", ""))

    def test_state_records_trigger_count(self):
        guardian._check_once()
        self.assertEqual(guardian.get_state().get("trigger_count"), 1)
        self.assertGreater(guardian.get_state().get("last_trigger_at", 0), 0)

    def test_trigger_registration_requires_url(self):
        self.settings["register_url"] = ""
        result = guardian.trigger_registration()
        self.assertFalse(result.get("ok"))
        self.assertIn("register_url", str(result.get("error")))


class AutoRegisterConfigTests(unittest.TestCase):
    """配置归一化：非法输入必须被钳制，不能导致失控行为。"""

    def test_defaults(self):
        from services.config import _normalize_auto_register_settings

        result = _normalize_auto_register_settings({})
        self.assertFalse(result["enabled"])
        self.assertEqual(result["threshold"], 10)
        self.assertEqual(result["target_batch"], 20)
        self.assertEqual(result["check_interval_minute"], 30)

    def test_numeric_clamping(self):
        from services.config import _normalize_auto_register_settings

        result = _normalize_auto_register_settings({
            "threshold": -5,
            "target_batch": 0,
            "check_interval_minute": 0,
            "cooldown_minute": -1,
        })
        self.assertEqual(result["threshold"], 0)
        self.assertEqual(result["target_batch"], 1)
        self.assertEqual(result["check_interval_minute"], 1)
        self.assertEqual(result["cooldown_minute"], 0)

    def test_invalid_numbers_fall_back_to_default(self):
        from services.config import _normalize_auto_register_settings

        result = _normalize_auto_register_settings({"threshold": "abc", "target_batch": None})
        self.assertEqual(result["threshold"], 10)
        self.assertEqual(result["target_batch"], 20)

    def test_register_options_must_be_dict(self):
        from services.config import _normalize_auto_register_settings

        result = _normalize_auto_register_settings({"register_options": "not-a-dict"})
        self.assertEqual(result["register_options"], {})

    def test_register_url_trailing_slash_stripped(self):
        from services.config import _normalize_auto_register_settings

        result = _normalize_auto_register_settings({"register_url": "http://register:8765/"})
        self.assertEqual(result["register_url"], "http://register:8765")


if __name__ == "__main__":
    unittest.main()

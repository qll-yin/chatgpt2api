"""入池代理策略 + worker 重试语义 + 双维度阈值。

覆盖用户实测反馈的四个问题：
  1. worker 失败一次就收工（空分类 / 代理池暂时拿不到代理）
  2. 动态代理被带进号池（把代理服务商的流量包耗光）
  3. 入池代理没有 2 选 1 的选项
  4. 自动补充只看账号数，不看剩余额度
"""
from __future__ import annotations

import unittest
from unittest import mock

from services import auto_register_guardian as guardian
from services import register_batch, register_service
from services.storage import register_store as store


class ProxyTypeTests(unittest.TestCase):
    """代理池里的静态/动态判定。"""

    def test_registered_static_proxy_is_static(self):
        with mock.patch.object(store, "find_proxy", return_value={"proxy_type": "static"}):
            self.assertTrue(store.is_static_proxy("http://a:1"))

    def test_registered_dynamic_proxy_is_not_static(self):
        with mock.patch.object(store, "find_proxy", return_value={"proxy_type": "dynamic"}):
            self.assertFalse(store.is_static_proxy("http://a:1"))

    def test_unregistered_proxy_is_treated_as_dynamic(self):
        """来路不明的代理一律不绑 —— 宁可不绑也不能把动态代理放进号池。"""
        with mock.patch.object(store, "find_proxy", return_value=None):
            self.assertFalse(store.is_static_proxy("http://unknown:1"))

    def test_missing_proxy_type_defaults_to_static(self):
        with mock.patch.object(store, "find_proxy", return_value={}):
            self.assertTrue(store.is_static_proxy("http://a:1"))


class ResolvePoolProxyTests(unittest.TestCase):
    """resolve_pool_proxy —— 入池代理的唯一决策入口。"""

    def test_none_mode_never_binds(self):
        proxy, reason = register_service.resolve_pool_proxy("none")
        self.assertEqual(proxy, "")
        self.assertTrue(reason)

    def test_register_mode_uses_static_register_proxy(self):
        with mock.patch.object(store, "is_static_proxy", return_value=True):
            proxy, _ = register_service.resolve_pool_proxy(
                "register", register_proxy="http://static:1"
            )
        self.assertEqual(proxy, "http://static:1")

    def test_register_mode_drops_dynamic_proxy(self):
        """⚠️ 核心铁律：动态代理不能进号池。"""
        with mock.patch.object(store, "is_static_proxy", return_value=False):
            proxy, reason = register_service.resolve_pool_proxy(
                "register", register_proxy="socks5://dynamic:2"
            )
        self.assertEqual(proxy, "")
        self.assertIn("静态", reason)

    def test_register_mode_without_proxy(self):
        proxy, _ = register_service.resolve_pool_proxy("register")
        self.assertEqual(proxy, "")

    def test_static_mode_picks_from_pool(self):
        with mock.patch.object(store, "pick_bind_proxy", return_value="http://s:1"):
            proxy, _ = register_service.resolve_pool_proxy("static")
        self.assertEqual(proxy, "http://s:1")

    def test_static_mode_empty_pool_gives_empty_proxy(self):
        """静态池空了 → 空代理入池，**不回落**到动态代理。"""
        with mock.patch.object(store, "pick_bind_proxy", return_value=""):
            proxy, reason = register_service.resolve_pool_proxy("static")
        self.assertEqual(proxy, "")
        self.assertTrue(reason)

    def test_static_mode_explicit_proxy_takes_quota(self):
        with mock.patch.object(store, "is_static_proxy", return_value=True), \
             mock.patch.object(store, "bind_proxy", return_value=True) as bind:
            proxy, _ = register_service.resolve_pool_proxy(
                "static", bind_proxy="http://s:1"
            )
        self.assertEqual(proxy, "http://s:1")
        self.assertTrue(bind.called)

    def test_static_mode_explicit_proxy_over_quota(self):
        with mock.patch.object(store, "is_static_proxy", return_value=True), \
             mock.patch.object(store, "bind_proxy", return_value=False):
            proxy, reason = register_service.resolve_pool_proxy(
                "static", bind_proxy="http://s:1"
            )
        self.assertEqual(proxy, "")
        self.assertIn("名额", reason)

    def test_unknown_mode_falls_back_to_none(self):
        proxy, _ = register_service.resolve_pool_proxy("garbage")
        self.assertEqual(proxy, "")


class BuildPoolPayloadTests(unittest.TestCase):
    """build_pool_payload 不得再偷偷回落���注册记录里的动态代理。"""

    def test_allow_record_proxy_false_does_not_fall_back(self):
        with mock.patch.object(
            store, "get_registered",
            return_value={"email": "a@b.c", "proxy": "socks5://dynamic:1"},
        ):
            payload = register_service.build_pool_payload(
                "a@b.c", "tok", "", allow_record_proxy=False
            )
        self.assertNotIn("proxy", payload)

    def test_allow_record_proxy_true_keeps_legacy_behavior(self):
        with mock.patch.object(
            store, "get_registered",
            return_value={"email": "a@b.c", "proxy": "http://s:1"},
        ):
            payload = register_service.build_pool_payload("a@b.c", "tok", "")
        self.assertEqual(payload["proxy"], "http://s:1")


class TokenTextImportTests(unittest.TestCase):
    """按文本导入时也要拦掉动态代理。"""

    def test_dynamic_proxy_is_dropped_and_counted(self):
        def _static_only(proxy: str) -> bool:
            return proxy == "http://s:2"

        with mock.patch.object(store, "is_static_proxy", side_effect=_static_only), \
             mock.patch.object(register_service, "import_to_pool_with_refresh",
                               return_value={"imported": 2, "skipped": 0, "items": []}) as imp:
            result = register_service.import_token_text_to_pool(
                "tok1----socks5://dyn:1\ntok2----http://s:2"
            )
            payloads = imp.call_args[0][0]
        self.assertEqual(result["dropped_dynamic"], 1)
        self.assertNotIn("proxy", payloads[0])
        self.assertEqual(payloads[1]["proxy"], "http://s:2")


class WorkerCategoryTests(unittest.TestCase):
    """空分类不能被静默吞掉 —— 那会让「失败重试」形同虚设。"""

    def test_empty_category_is_treated_as_unknown(self):
        ctrl = register_batch.AutoLoopController.__new__(register_batch.AutoLoopController)
        ctrl._lock = __import__("threading").RLock()
        ctrl._max_retries = 3
        ctrl._registered_ok = 0
        ctrl._registered_fail = 0
        ctrl._consecutive_network_fails = 0
        ctrl._circuit_break_threshold = 3
        ctrl._target_count = 0
        ctrl._state = "running"
        ctrl._last_message = ""
        ctrl._last_fail_reason = ""
        ctrl._last_break_reason = ""
        ctrl._stop_event = __import__("threading").Event()
        ctrl._pause_event = __import__("threading").Event()

        ctrl._record_finish(False, "")
        # 空分类按 unknown 处理 → 不计入熔断，但失败数要 +1
        self.assertEqual(ctrl._registered_fail, 1)
        self.assertEqual(ctrl._consecutive_network_fails, 0)
        self.assertEqual(ctrl._last_fail_reason, "unknown")

    def test_stopped_category_is_a_sentinel(self):
        self.assertEqual(register_batch.STOPPED_CATEGORY, "__stopped__")
        self.assertNotIn(register_batch.STOPPED_CATEGORY, register_batch.RETRYABLE_CATEGORIES)
        self.assertNotIn(register_batch.STOPPED_CATEGORY, register_batch.NON_RETRYABLE_CATEGORIES)


class GuardianThresholdTests(unittest.TestCase):
    """自动补充的双维度阈值。"""

    def _settings(self, **over):
        base = {
            "enabled": True,
            "threshold": 10,
            "quota_threshold": 25,
            "target_batch": 5,
            "concurrency": 1,
            "check_interval_minute": 30,
            "cooldown_minute": 0,
            "mail_source": "",
            "register_options": {},
            "max_retries": 3,
            "pool_proxy_mode": "static",
        }
        base.update(over)
        return base

    def _run(self, active, quota, **over):
        settings = self._settings(**over)
        with mock.patch.object(guardian, "get_settings", return_value=settings), \
             mock.patch.object(guardian, "_active_account_count", return_value=active), \
             mock.patch.object(guardian, "_pool_quota_total", return_value=quota), \
             mock.patch.object(guardian, "_batch_running", return_value=False), \
             mock.patch.object(guardian, "trigger_registration",
                               return_value={"ok": True}) as trigger:
            result = guardian._check_once()
        return result, trigger

    def test_accounts_ok_but_quota_low_triggers(self):
        """用户场景：账号数 10（阈值 10 达标）但额度 15（阈值 25）→ 触发。"""
        result, trigger = self._run(active=10, quota=15)
        self.assertTrue(result["triggered"])
        self.assertTrue(trigger.called)

    def test_quota_ok_but_accounts_low_triggers(self):
        result, trigger = self._run(active=3, quota=100)
        self.assertTrue(result["triggered"])

    def test_both_ok_does_not_trigger(self):
        result, trigger = self._run(active=10, quota=30)
        self.assertFalse(result.get("triggered"))
        self.assertFalse(trigger.called)

    def test_quota_threshold_zero_disables_dimension(self):
        result, trigger = self._run(active=10, quota=0, quota_threshold=0)
        # 额度维度关闭 → 只看账号数（10 >= 10）→ 不触发
        self.assertFalse(result.get("triggered"))

    def test_quota_threshold_zero_with_low_accounts_triggers(self):
        result, trigger = self._run(active=2, quota=0, quota_threshold=0)
        self.assertTrue(result["triggered"])

    def test_reason_mentions_both_dimensions(self):
        result, _ = self._run(active=1, quota=2)
        self.assertIn("可用账号", result["reason"])
        self.assertIn("剩余额度", result["reason"])


class GuardianProxyModeTests(unittest.TestCase):
    """守护触发的注册，入池必须走静态代理。"""

    def test_default_pool_proxy_mode_is_static(self):
        with mock.patch("services.config.config.get_auto_register_settings", return_value={}):
            settings = guardian.get_settings()
        self.assertEqual(settings["pool_proxy_mode"], "static")

    def test_trigger_passes_pool_proxy_mode(self):
        settings = {
            "target_batch": 3, "concurrency": 1, "max_retries": 3,
            "pool_proxy_mode": "static", "mail_source": "",
            "register_options": {},
        }
        with mock.patch.object(register_batch.CONTROLLER, "start",
                               return_value={"ok": True}) as start:
            guardian.trigger_registration(settings)
        self.assertEqual(start.call_args[0][0].get("pool_proxy_mode"), "static")


if __name__ == "__main__":
    unittest.main()

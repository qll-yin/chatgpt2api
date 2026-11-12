"""代理池操作与邮箱配置读写的测试。

覆盖旧注册机前端有、而合并后必须补齐的那批能力：
- 代理：改启用状态 / 改上限 / 改类型 / 删单个 / 清空 / 重置用量 / 清 CF 计数 / IPv6 状态
- 邮箱配置：密码脱敏回填、'***' 表示不修改、未知来源直接报错
"""

from __future__ import annotations

import unittest

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-proxy-ops-")

from services.storage import register_store as store  # noqa: E402


class ProxyMaintenanceTests(unittest.TestCase):
    """代理池的维护操作。"""

    def setUp(self):
        store.replace_proxies(["http://p1:1", "http://p2:2", "socks5://p3:3"], mode="replace")
        store.reset_proxy_counts()
        store.reset_all_proxy_cf_blocked()

    def tearDown(self):
        store.replace_proxies([], mode="replace")

    def test_update_proxy_changes_fields(self):
        ok = store.update_proxy("http://p1:1", enabled=False, max_uses=5, proxy_type="dynamic")
        self.assertTrue(ok)
        item = store.find_proxy("http://p1:1")
        self.assertFalse(item["enabled"])
        self.assertEqual(item["max_uses"], 5)
        self.assertEqual(item["proxy_type"], "dynamic")
        # 派生状态跟着变
        self.assertEqual(item["status"], "disabled")

    def test_update_proxy_ignores_missing(self):
        self.assertFalse(store.update_proxy("http://nope:1", enabled=False))
        self.assertFalse(store.update_proxy("", enabled=False))

    def test_exhausted_status_when_used_reaches_limit(self):
        store.update_proxy("http://p2:2", max_uses=1)
        store.proxy_used("http://p2:2", ok=True)
        self.assertEqual(store.find_proxy("http://p2:2")["status"], "exhausted")

    def test_delete_proxy(self):
        self.assertTrue(store.delete_proxy("http://p1:1"))
        self.assertFalse(store.delete_proxy("http://p1:1"))
        self.assertIsNone(store.find_proxy("http://p1:1"))
        self.assertEqual(len(store.list_proxies()), 2)

    def test_clear_proxies(self):
        removed = store.clear_proxies()
        self.assertEqual(removed, 3)
        self.assertEqual(store.list_proxies(), [])

    def test_reset_proxy_counts_single(self):
        store.proxy_used("http://p1:1", ok=True)
        store.proxy_used("http://p2:2", ok=True)
        reset = store.reset_proxy_counts("http://p1:1")
        self.assertEqual(reset, 1)
        self.assertEqual(store.find_proxy("http://p1:1")["used_count"], 0)
        self.assertEqual(store.find_proxy("http://p2:2")["used_count"], 1)

    def test_reset_proxy_counts_all(self):
        store.proxy_used("http://p1:1", ok=True)
        store.proxy_used("http://p2:2", ok=True)
        store.reset_proxy_counts()
        self.assertEqual(store.find_proxy("http://p1:1")["used_count"], 0)
        self.assertEqual(store.find_proxy("http://p2:2")["used_count"], 0)

    def test_reset_cf_blocked_single_and_all(self):
        store.mark_proxy_cf_blocked("http://p1:1")
        store.mark_proxy_cf_blocked("http://p2:2")
        self.assertGreater(store.find_proxy("http://p1:1")["cf_blocked"], 0)

        self.assertTrue(store.reset_proxy_cf_blocked("http://p1:1"))
        self.assertEqual(store.find_proxy("http://p1:1")["cf_blocked"], 0)
        self.assertGreater(store.find_proxy("http://p2:2")["cf_blocked"], 0)

        store.reset_all_proxy_cf_blocked()
        self.assertEqual(store.find_proxy("http://p2:2")["cf_blocked"], 0)

    def test_set_ipv6_status(self):
        self.assertTrue(store.set_proxy_ipv6_status("http://p1:1", "ok"))
        self.assertEqual(store.find_proxy("http://p1:1")["ipv6_status"], "ok")
        # 非法值退化为 untested
        store.set_proxy_ipv6_status("http://p1:1", "bogus")
        self.assertEqual(store.find_proxy("http://p1:1")["ipv6_status"], "untested")
        self.assertFalse(store.set_proxy_ipv6_status("", "ok"))


class MailConfigTests(unittest.TestCase):
    """邮箱配置：脱敏 + 保存语义。"""

    def tearDown(self):
        for key in (
            "mail_source",
            "remail_api_key",
            "remail_api_base",
            "remail_service_mode",
            "remail_supply_policy",
        ):
            store.set_setting(key, "")

    def test_password_field_is_masked(self):
        store.set_setting("remail_api_key", "rk-secret-value")
        config = store.get_mail_config()
        self.assertEqual(config["remail_api_key"], "***")

    def test_masked_value_does_not_overwrite(self):
        store.set_setting("remail_api_key", "rk-original")
        # 前端回填的就是 '***' —— 保存时必须跳过，不能把真值覆盖掉
        store.save_mail_config({"remail_api_key": "***", "mail_source": "remail"})
        self.assertEqual(store.get_setting("remail_api_key"), "rk-original")

    def test_empty_password_does_not_overwrite(self):
        store.set_setting("remail_api_key", "rk-original")
        store.save_mail_config({"remail_api_key": ""})
        self.assertEqual(store.get_setting("remail_api_key"), "rk-original")

    def test_new_password_is_saved(self):
        store.save_mail_config({"remail_api_key": "rk-new"})
        self.assertEqual(store.get_setting("remail_api_key"), "rk-new")

    def test_select_field_is_saved(self):
        store.save_mail_config(
            {
                "mail_source": "remail",
                "remail_service_mode": "code",
                "remail_supply_policy": "public_only",
            }
        )
        self.assertEqual(store.get_setting("remail_service_mode"), "code")
        self.assertEqual(store.get_setting("remail_supply_policy"), "public_only")

    def test_unknown_mail_source_raises(self):
        with self.assertRaises(Exception):
            store.save_mail_config({"mail_source": "not-a-provider"})

    def test_config_contains_select_options_for_ui(self):
        """前端靠 config_fields[].options 渲染下拉框 —— 必须带上。"""
        from services.register.mail_providers import list_providers

        providers = {p["kind"]: p for p in list_providers()}
        remail = providers.get("remail")
        self.assertIsNotNone(remail, "remail provider 缺失")
        fields = {f["key"]: f for f in remail["config_fields"]}
        self.assertIn("remail_service_mode", fields)
        self.assertEqual(fields["remail_service_mode"]["type"], "select")
        self.assertIn("purchase", fields["remail_service_mode"]["options"])
        self.assertIn("code", fields["remail_service_mode"]["options"])
        self.assertEqual(fields["remail_supply_policy"]["type"], "select")


if __name__ == "__main__":
    unittest.main()

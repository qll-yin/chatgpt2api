"""注册服务与号池打通链路的测试。

重点验证：
- 注册成功的账号能按「含代理 / 不含代理」两种方式导入号池
- `access_token----proxy` 文本格式导入（与注册机导出格式一致）
- 导入是幂等的（重复导入不会产生重复账号）
- 代理池的「选中即预占 / 失败不占配额 / CF 连续拦截停用」语义
- 邮箱池的原子 claim（并发不会拿到同一个号）
"""

from __future__ import annotations

import unittest

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-register-")

from services import register_service as rs  # noqa: E402
from services.storage import register_store as store  # noqa: E402


class RegisterPoolImportTests(unittest.TestCase):
    """注册结果 → 号池 的导入链路。"""

    def setUp(self):
        from services.account_service import account_service

        # 清空号池，避免跨用例影响
        existing = [item.get("access_token") for item in account_service.list_accounts()]
        if existing:
            account_service.delete_accounts([t for t in existing if t])

    def _make_registered(self, email, token, proxy="", refresh=""):
        store.save_registered(
            email=email,
            access_token=token,
            refresh_token=refresh,
            proxy=proxy,
        )

    def test_import_with_proxy(self):
        self._make_registered("a@x.com", "AT_A", proxy="socks5://u:p@h:1")
        result = rs.import_registered_to_pool(["a@x.com"], with_proxy=True)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["missing"], [])

        from services.account_service import account_service

        items = {i["email"]: i for i in account_service.list_accounts()}
        self.assertIn("a@x.com", items)
        self.assertEqual(items["a@x.com"].get("proxy"), "socks5://u:p@h:1")

    def test_import_without_proxy(self):
        self._make_registered("b@x.com", "AT_B", proxy="socks5://u:p@h:2")
        result = rs.import_registered_to_pool(["b@x.com"], with_proxy=False)
        self.assertEqual(result["imported"], 1)

        from services.account_service import account_service

        items = {i["email"]: i for i in account_service.list_accounts()}
        self.assertEqual(items["b@x.com"].get("proxy"), "")

    def test_import_missing_email_reported(self):
        result = rs.import_registered_to_pool(["nope@x.com"])
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["missing"], ["nope@x.com"])

    def test_import_is_idempotent(self):
        self._make_registered("c@x.com", "AT_C")
        first = rs.import_registered_to_pool(["c@x.com"])
        second = rs.import_registered_to_pool(["c@x.com"])
        self.assertEqual(first["imported"], 1)
        # 第二次应被跳过（已存在）
        self.assertEqual(second["imported"], 0)
        self.assertGreaterEqual(second["skipped"], 1)

        from services.account_service import account_service

        tokens = [i["access_token"] for i in account_service.list_accounts()]
        self.assertEqual(tokens.count("AT_C"), 1)

    def test_import_empty_list(self):
        result = rs.import_registered_to_pool([])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["imported"], 0)

    def test_refresh_token_is_carried_over(self):
        self._make_registered("d@x.com", "AT_D", refresh="RT_D")
        rs.import_registered_to_pool(["d@x.com"])

        from services.account_service import account_service

        items = {i["email"]: i for i in account_service.list_accounts()}
        self.assertEqual(items["d@x.com"].get("refresh_token"), "RT_D")

    def test_text_import_with_proxy_format(self):
        result = rs.import_token_text_to_pool(
            "AT_TEXT_1----socks5://a:b@c:2\nAT_TEXT_2"
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["imported"], 2)

        from services.account_service import account_service

        items = {i["access_token"]: i for i in account_service.list_accounts()}
        self.assertEqual(items["AT_TEXT_1"].get("proxy"), "socks5://a:b@c:2")
        self.assertEqual(items["AT_TEXT_2"].get("proxy"), "")

    def test_text_import_skips_comments_and_blanks(self):
        result = rs.import_token_text_to_pool(
            "# 这是注释\n\nAT_OK\n"
        )
        self.assertEqual(result["total"], 1)

    def test_build_pool_payload(self):
        self._make_registered("e@x.com", "AT_E", proxy="http://p:1", refresh="RT_E")
        payload = rs.build_pool_payload("e@x.com", "AT_E", "http://p:1")
        self.assertEqual(payload["access_token"], "AT_E")
        self.assertEqual(payload["email"], "e@x.com")
        self.assertEqual(payload["proxy"], "http://p:1")
        self.assertEqual(payload["refresh_token"], "RT_E")
        self.assertEqual(payload["source_type"], "register")


class RegisterPoolManagementTests(unittest.TestCase):
    """代理池与邮箱池的核心语义。"""

    def setUp(self):
        store.replace_proxies("http://px1:1\nhttp://px2:2", mode="replace")

    def test_pick_proxy_reserves_quota(self):
        p1 = store.pick_proxy()
        self.assertIn(p1, {"http://px1:1", "http://px2:2"})
        # 选中即预占：used_count 已经是 1
        self.assertEqual(store.find_proxy(p1)["used_count"], 1)

    def test_pick_proxy_distributes(self):
        p1 = store.pick_proxy()
        p2 = store.pick_proxy()
        # 两个 worker 拿到不同代理（预占生效）
        self.assertNotEqual(p1, p2)

    def test_failure_does_not_consume_quota(self):
        p = store.pick_proxy()
        store.proxy_used(p, ok=False)
        record = store.find_proxy(p)
        self.assertEqual(record["failed_count"], 1)
        # used_count 不变（预占那格还在）
        self.assertEqual(record["used_count"], 1)

    def test_release_returns_quota(self):
        p = store.pick_proxy()
        store.proxy_release(p)
        self.assertEqual(store.find_proxy(p)["used_count"], 0)

    def test_release_never_negative(self):
        p = store.pick_proxy()
        store.proxy_release(p)
        store.proxy_release(p)
        self.assertEqual(store.find_proxy(p)["used_count"], 0)

    def test_static_proxy_disabled_after_repeated_cf_block(self):
        p = store.pick_proxy()
        store.update_proxy(p, {"proxy_type": "static"})
        store.mark_proxy_cf_blocked(p)
        self.assertTrue(store.find_proxy(p)["enabled"])
        store.mark_proxy_cf_blocked(p)
        self.assertFalse(store.find_proxy(p)["enabled"])

    def test_dynamic_proxy_not_disabled(self):
        p = store.pick_proxy()
        store.update_proxy(p, {"proxy_type": "dynamic"})
        store.mark_proxy_cf_blocked(p)
        store.mark_proxy_cf_blocked(p)
        # 动态代理换 IP 可能就通了 → 不停用
        self.assertTrue(store.find_proxy(p)["enabled"])

    def test_email_claim_is_exclusive(self):
        store.import_accounts("c1@x.com----p\nc2@x.com----p", "outlook")
        first = store.claim_next("outlook")
        second = store.claim_next("outlook")
        self.assertNotEqual(first["email"], second["email"])

    def test_email_claim_respects_kind(self):
        store.import_accounts("g@x.com----p", "cf_temp")
        claimed = store.claim_next("outlook")
        self.assertNotEqual((claimed or {}).get("email"), "g@x.com")


class RegisterErrorClassifyTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(rs.classify_error("HTTP 409 invalid_state"), "account")
        self.assertEqual(rs.classify_error("Cloudflare 403 blocked"), "cf_blocked")
        self.assertEqual(rs.classify_error("OTP timeout waiting"), "mail_limited")
        self.assertEqual(rs.classify_error("connection reset by peer"), "network")
        self.assertEqual(rs.classify_error("some random issue"), "unknown")


if __name__ == "__main__":
    unittest.main()

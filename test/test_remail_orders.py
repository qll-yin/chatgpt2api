"""Remail 订单管理 + 标注异常释放代理配额的测试。

覆盖：
- 订单落库 / 列表分页 / 复用挑选（长效才有复用）/ 关联账号 / 禁用 / 删除
- 短效订单关联后置为 consumed（一次性凭证，用完就失效）
- 标注异常自动释放代理配额，且**不重复释放**
"""

from __future__ import annotations

import unittest

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-remail-orders-")

from services.storage import register_store as store  # noqa: E402


def _make_order(order_no: str = "ORD-1", **overrides) -> dict:
    data = {
        "order_no": order_no,
        "email": f"{order_no}@example.com",
        "suffix": "outlook",
        "service_mode": "purchase",
        "supply_policy": "private_first",
        "project_id": 1001,
        "order_status": "active",
        "service_token": "tok-abc",
        "pay_amount": "1.50",
        "received_at": 1000.0,
    }
    data.update(overrides)
    store.save_remail_order(data)
    return data


class RemailOrderTests(unittest.TestCase):
    def setUp(self):
        store.delete_remail_orders(
            [o["order_no"] for o in store.list_remail_orders(limit=500)]
        )

    def tearDown(self):
        store.delete_remail_orders(
            [o["order_no"] for o in store.list_remail_orders(limit=500)]
        )

    def test_save_and_list(self):
        _make_order("ORD-A")
        _make_order("ORD-B")
        items = store.list_remail_orders(limit=50)
        self.assertEqual(len(items), 2)
        self.assertEqual(store.count_remail_orders(), 2)

    def test_list_pagination(self):
        for i in range(5):
            _make_order(f"ORD-{i}", received_at=1000.0 + i)
        page1 = store.list_remail_orders(limit=2, offset=0)
        page2 = store.list_remail_orders(limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        # 倒序（received_at desc）→ 第 5 笔在第一页
        self.assertEqual(page1[0]["order_no"], "ORD-4")

    def test_service_token_is_masked_in_list(self):
        _make_order("ORD-A")
        item = store.list_remail_orders(limit=1)[0]
        self.assertNotIn("service_token", item)
        self.assertTrue(item["has_token"])

    def test_get_with_token(self):
        _make_order("ORD-A")
        item = store.get_remail_order("ORD-A", with_token=True)
        self.assertEqual(item["service_token"], "tok-abc")
        masked = store.get_remail_order("ORD-A")
        self.assertNotIn("service_token", masked)

    def test_reusable_flag(self):
        _make_order("ORD-A")
        item = store.list_remail_orders(limit=1)[0]
        self.assertTrue(item["reusable"])
        self.assertEqual(item["max_attempts"], store.REMAIL_MAX_ATTEMPTS)

    def test_reusable_requires_purchase_mode(self):
        _make_order("ORD-CODE", service_mode="code")
        item = store.list_remail_orders(limit=1)[0]
        self.assertFalse(item["reusable"])

    def test_claim_reusable_picks_oldest(self):
        _make_order("ORD-OLD", received_at=1000.0)
        _make_order("ORD-NEW", received_at=2000.0)
        claimed = store.claim_reusable_remail_order(service_mode="purchase", suffix="outlook")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["order_no"], "ORD-OLD")
        self.assertEqual(claimed["service_token"], "tok-abc")

    def test_claim_reusable_skips_used_and_disabled(self):
        _make_order("ORD-USED", received_at=1000.0)
        store.link_remail_order("ORD-USED", "acc@example.com")
        _make_order("ORD-DIS", received_at=2000.0)
        store.set_remail_order_disabled("ORD-DIS", True)
        claimed = store.claim_reusable_remail_order(service_mode="purchase", suffix="outlook")
        self.assertIsNone(claimed)

    def test_claim_reusable_none_for_code_mode(self):
        _make_order("ORD-CODE", service_mode="code")
        self.assertIsNone(store.claim_reusable_remail_order(service_mode="code"))

    def test_link_sets_used_email(self):
        _make_order("ORD-A")
        self.assertTrue(store.link_remail_order("ORD-A", "Acc@Example.com"))
        item = store.get_remail_order("ORD-A")
        self.assertEqual(item["used_email"], "acc@example.com")

    def test_short_lived_order_becomes_consumed_after_link(self):
        """短效的 serviceToken 是一次性的 —— 用完状态必须改成 consumed，
        否则列表里一直挂着「生效中」，是假状态。"""
        _make_order("ORD-CODE", service_mode="code", order_status="active")
        store.link_remail_order("ORD-CODE", "acc@example.com")
        self.assertEqual(store.get_remail_order("ORD-CODE")["order_status"], "consumed")

    def test_long_lived_order_stays_active_after_link(self):
        """长效订单能重复收件 —— 不能改成 consumed。"""
        _make_order("ORD-PUR", service_mode="purchase", order_status="active")
        store.link_remail_order("ORD-PUR", "acc@example.com")
        self.assertEqual(store.get_remail_order("ORD-PUR")["order_status"], "active")

    def test_mark_attempt_increments_and_records_error(self):
        _make_order("ORD-A")
        store.mark_remail_order_attempt("ORD-A", ok=False, error="OTP 超时")
        item = store.get_remail_order("ORD-A")
        self.assertEqual(item["attempt_count"], 1)
        self.assertEqual(item["last_error"], "OTP 超时")

    def test_mark_attempt_clears_error_on_success(self):
        _make_order("ORD-A")
        store.mark_remail_order_attempt("ORD-A", ok=False, error="boom")
        store.mark_remail_order_attempt("ORD-A", ok=True)
        item = store.get_remail_order("ORD-A")
        self.assertEqual(item["attempt_count"], 2)
        self.assertIsNone(item["last_error"])

    def test_disabled_toggles(self):
        _make_order("ORD-A")
        self.assertTrue(store.set_remail_order_disabled("ORD-A", True))
        self.assertTrue(store.get_remail_order("ORD-A")["disabled"])
        store.set_remail_order_disabled("ORD-A", False)
        self.assertFalse(store.get_remail_order("ORD-A")["disabled"])

    def test_save_preserves_used_email_and_disabled(self):
        """整行覆盖时不能把 used_email / disabled 冲掉 ——
        used_email 被冲空 → 那个邮箱会被重复注册。"""
        _make_order("ORD-A")
        store.link_remail_order("ORD-A", "acc@example.com")
        store.set_remail_order_disabled("ORD-A", True)
        # 再存一次（不带这些字段）
        _make_order("ORD-A")
        item = store.get_remail_order("ORD-A")
        self.assertEqual(item["used_email"], "acc@example.com")
        self.assertTrue(item["disabled"])

    def test_delete_single_and_bulk(self):
        _make_order("ORD-A")
        _make_order("ORD-B")
        self.assertTrue(store.delete_remail_order("ORD-A"))
        self.assertFalse(store.delete_remail_order("ORD-A"))
        self.assertEqual(store.delete_remail_orders(["ORD-B", "ORD-X"]), 1)

    def test_find_by_email(self):
        _make_order("ORD-A", email="target@example.com")
        found = store.find_remail_order_by_email("target@example.com")
        self.assertIsNotNone(found)
        self.assertEqual(found["order_no"], "ORD-A")
        self.assertIsNone(store.find_remail_order_by_email("nope@example.com"))


class MarkAbnormalTests(unittest.TestCase):
    """标注异常 → 自动释放代理配额。"""

    def setUp(self):
        store.replace_proxies(["http://px1:1", "http://px2:2"], mode="replace")
        store.reset_proxy_counts()
        store.delete_registered(["abn@example.com"])

    def tearDown(self):
        store.replace_proxies([], mode="replace")
        store.delete_registered(["abn@example.com"])

    def _register_with_proxy(self, email: str, proxy: str) -> None:
        store.save_registered(
            email=email,
            access_token="AT",
            proxy=proxy,
        )
        # 模拟注册成功占用了一格配额
        store.proxy_used(proxy, ok=True)

    def test_mark_abnormal_releases_quota(self):
        self._register_with_proxy("abn@example.com", "http://px1:1")
        self.assertEqual(store.find_proxy("http://px1:1")["used_count"], 1)

        ok = store.mark_registered_abnormal("abn@example.com", True)
        self.assertTrue(ok)
        self.assertEqual(store.find_proxy("http://px1:1")["used_count"], 0)

    def test_released_flag_prevents_double_release(self):
        """同一个号先标异常再标一次，不能减两次。"""
        self._register_with_proxy("abn@example.com", "http://px1:1")
        store.mark_registered_abnormal("abn@example.com", True)
        # 人工把用量加回去（模拟其他号占了同一代理）
        store.proxy_used("http://px1:1", ok=True)
        self.assertEqual(store.find_proxy("http://px1:1")["used_count"], 1)

        store.mark_registered_abnormal("abn@example.com", True)
        # 不该再减
        self.assertEqual(store.find_proxy("http://px1:1")["used_count"], 1)

    def test_abnormal_flag_is_stored(self):
        self._register_with_proxy("abn@example.com", "http://px1:1")
        store.mark_registered_abnormal("abn@example.com", True)
        record = store.get_registered("abn@example.com")
        self.assertTrue((record.get("extra") or {}).get("abnormal"))

    def test_unmark_does_not_release_again(self):
        self._register_with_proxy("abn@example.com", "http://px1:1")
        store.mark_registered_abnormal("abn@example.com", True)
        store.mark_registered_abnormal("abn@example.com", False)
        record = store.get_registered("abn@example.com")
        self.assertFalse((record.get("extra") or {}).get("abnormal"))
        self.assertEqual(store.find_proxy("http://px1:1")["used_count"], 0)

    def test_batch_mark(self):
        self._register_with_proxy("abn@example.com", "http://px1:1")
        count = store.mark_registered_abnormal_by_emails(
            ["abn@example.com", "not-exist@example.com"], True
        )
        self.assertEqual(count, 1)
        self.assertEqual(store.find_proxy("http://px1:1")["used_count"], 0)

    def test_release_ignores_account_without_proxy(self):
        store.save_registered(email="abn@example.com", access_token="AT")
        # 没有代理 → 不该报错，也释放不了
        self.assertEqual(store.release_proxy_for_emails(["abn@example.com"]), 0)

    def test_used_count_never_goes_negative(self):
        self._register_with_proxy("abn@example.com", "http://px1:1")
        store.reset_proxy_counts()  # 用量清零
        store.mark_registered_abnormal("abn@example.com", True)
        self.assertEqual(store.find_proxy("http://px1:1")["used_count"], 0)


if __name__ == "__main__":
    unittest.main()

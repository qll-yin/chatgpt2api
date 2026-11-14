"""号池绑定静态代理的测试。

场景
----
注册用**动态代理**（出口 IP 每次都变，防注册风控）；账号导入号池后要
长期使用，IP 不能每次都变 —— 同一账号在多个出口之间跳变反而更容易触发
使用侧风控。所以导入号池时改绑**静态代理**。

两个配额互相独立：
- max_uses / used_count        → 限制「成功注册多少次」（注册阶段）
- bind_max_uses / bind_used_count → 限制「能绑多少个账号」（号池阶段）
"""

from __future__ import annotations

import unittest

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-bind-proxy-")

from services.storage import register_store as store  # noqa: E402


class BindProxyQuotaTests(unittest.TestCase):
    """静态代理的绑定配额。"""

    def setUp(self):
        store.replace_proxies(
            ["http://static1:1", "http://static2:2", "http://dynamic1:3"],
            mode="replace",
        )
        store.reset_proxy_counts()
        # 明确类型：前两个静态、第三个动态
        store.update_proxy("http://static1:1", proxy_type="static", bind_max_uses=2)
        store.update_proxy("http://static2:2", proxy_type="static", bind_max_uses=2)
        store.update_proxy("http://dynamic1:3", proxy_type="dynamic")

    def tearDown(self):
        store.replace_proxies([], mode="replace")

    def test_bind_quota_is_independent_from_registration_quota(self):
        """绑账号不该影响「成功注册次数」那个配额，反之亦然。"""
        before = store.find_proxy("http://static1:1")
        self.assertTrue(store.bind_proxy("http://static1:1"))
        after = store.find_proxy("http://static1:1")

        self.assertEqual(after["bind_used_count"], before["bind_used_count"] + 1)
        # 注册配额没被动过
        self.assertEqual(after["used_count"], before["used_count"])
        self.assertEqual(after["max_uses"], before["max_uses"])

    def test_bind_respects_max(self):
        """超过绑定上限就绑不上。"""
        self.assertTrue(store.bind_proxy("http://static1:1"))
        self.assertTrue(store.bind_proxy("http://static1:1"))
        # 第 3 次超了（上限 2）
        self.assertFalse(store.bind_proxy("http://static1:1"))

    def test_bind_zero_max_means_unlimited(self):
        store.update_proxy("http://static2:2", bind_max_uses=0)
        for _ in range(5):
            self.assertTrue(store.bind_proxy("http://static2:2"))
        item = store.find_proxy("http://static2:2")
        self.assertEqual(item["bind_used_count"], 5)
        self.assertEqual(item["bind_status"], "available")

    def test_dynamic_proxy_cannot_be_bound(self):
        """动态代理不给账号绑定 —— 出口 IP 会变，绑了等于没绑。"""
        self.assertFalse(store.bind_proxy("http://dynamic1:3"))
        self.assertFalse(store.pick_bind_proxy() == "http://dynamic1:3")

    def test_disabled_proxy_cannot_be_bound(self):
        store.update_proxy("http://static2:2", enabled=False)
        self.assertFalse(store.bind_proxy("http://static2:2"))

    def test_missing_proxy_cannot_be_bound(self):
        self.assertFalse(store.bind_proxy("http://nope:1"))
        self.assertFalse(store.bind_proxy(""))

    def test_release_frees_quota(self):
        self.assertTrue(store.bind_proxy("http://static1:1"))
        self.assertTrue(store.bind_proxy("http://static1:1"))
        self.assertFalse(store.bind_proxy("http://static1:1"))

        store.release_bind_proxy("http://static1:1")
        # 释放一个名额后又可以绑了
        self.assertTrue(store.bind_proxy("http://static1:1"))

    def test_release_never_goes_negative(self):
        store.release_bind_proxy("http://static1:1")
        store.release_bind_proxy("http://static1:1")
        item = store.find_proxy("http://static1:1")
        self.assertGreaterEqual(item["bind_used_count"], 0)

    def test_pick_bind_proxy_balances(self):
        """自动分配时优先挑绑得少的。"""
        first = store.pick_bind_proxy()
        second = store.pick_bind_proxy()
        self.assertIn(first, {"http://static1:1", "http://static2:2"})
        self.assertIn(second, {"http://static1:1", "http://static2:2"})
        # 两个不同代理（各绑一个，都没超上限 2）
        self.assertNotEqual(first, second)

    def test_pick_bind_proxy_respects_quota(self):
        """配额用完的代理不再被挑中。"""
        store.update_proxy("http://static2:2", bind_max_uses=0)
        # static2 不限，static1 上限 2 —— 连续挑 3 次，static1 最多中 2 次
        picked = [store.pick_bind_proxy() for _ in range(3)]
        self.assertLessEqual(picked.count("http://static1:1"), 2)

    def test_pick_bind_proxy_returns_empty_when_all_exhausted(self):
        store.update_proxy("http://static1:1", bind_max_uses=1)
        store.update_proxy("http://static2:2", bind_max_uses=1)
        self.assertTrue(store.pick_bind_proxy())
        self.assertTrue(store.pick_bind_proxy())
        # 全满了 → 返回空串（调用方负责提示）
        self.assertEqual(store.pick_bind_proxy(), "")


class BindProxyStatusTests(unittest.TestCase):
    """派生状态字段。"""

    def setUp(self):
        store.replace_proxies(["http://s1:1", "http://d1:2"], mode="replace")
        store.reset_proxy_counts()
        store.update_proxy("http://s1:1", proxy_type="static", bind_max_uses=1)
        store.update_proxy("http://d1:2", proxy_type="dynamic")

    def tearDown(self):
        store.replace_proxies([], mode="replace")

    def test_bind_status_available(self):
        item = store.find_proxy("http://s1:1")
        self.assertEqual(item["bind_status"], "available")
        self.assertEqual(item["bind_remaining"], 1)

    def test_bind_status_exhausted(self):
        store.bind_proxy("http://s1:1")
        item = store.find_proxy("http://s1:1")
        self.assertEqual(item["bind_status"], "exhausted")
        self.assertEqual(item["bind_remaining"], 0)

    def test_bind_status_not_applicable_for_dynamic(self):
        item = store.find_proxy("http://d1:2")
        self.assertEqual(item["bind_status"], "not_applicable")

    def test_bind_remaining_minus_one_means_unlimited(self):
        store.update_proxy("http://s1:1", bind_max_uses=0)
        item = store.find_proxy("http://s1:1")
        self.assertEqual(item["bind_remaining"], -1)

    def test_list_bind_proxies_only_returns_available_static(self):
        items = store.list_bind_proxies()
        self.assertEqual([i["proxy"] for i in items], ["http://s1:1"])

    def test_bind_proxy_stats(self):
        store.bind_proxy("http://s1:1")
        stats = store.bind_proxy_stats()
        self.assertEqual(stats["static_total"], 1)
        self.assertEqual(stats["bind_used"], 1)
        self.assertEqual(stats["bind_capacity"], 1)


if __name__ == "__main__":
    unittest.main()

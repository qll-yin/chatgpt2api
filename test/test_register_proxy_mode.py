"""「注册使用代理」（dynamic / static / mixed）的回归测试。

背景
----
用户要求批量注册和自动补充都能选「注册时用哪一类代理」：

- 动态代理：注册时只在代理池里挑**动态**类型的代理
- 静态代理：注册时只在代理池里挑**静态**类型的代理
- 混合随机：两类都能用（随机挑）

并且：「注册使用代理」与「注册成功后入池代理」**不冲突** ——
但注册用的是静态代理时，入池要**沿用同一条静态代理**（出口保持一致）。
"""

from __future__ import annotations

import unittest
from unittest import mock

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-proxy-mode-")

from services import register_batch  # noqa: E402
from services import register_service  # noqa: E402


class PickProxyTypeTests(unittest.TestCase):
    """代理池按类型挑选。"""

    def _pool(self, entries: list[tuple[str, str]]):
        """entries: [(proxy, proxy_type), ...]"""
        from services.storage import register_store

        register_store.replace_proxies([p for p, _ in entries], mode="replace")
        for proxy, ptype in entries:
            register_store.update_proxy(proxy, proxy_type=ptype)

    def test_dynamic_only_picks_dynamic(self):
        from services.storage import register_store

        self._pool([("http://dyn:1", "dynamic"), ("http://sta:1", "static")])
        picked = register_store.pick_proxy(proxy_type="dynamic")
        self.assertEqual(picked, "http://dyn:1")

    def test_static_only_picks_static(self):
        from services.storage import register_store

        self._pool([("http://dyn:1", "dynamic"), ("http://sta:1", "static")])
        picked = register_store.pick_proxy(proxy_type="static")
        self.assertEqual(picked, "http://sta:1")

    def test_mixed_picks_anything(self):
        from services.storage import register_store

        self._pool([("http://dyn:1", "dynamic"), ("http://sta:1", "static")])
        picked = register_store.pick_proxy(proxy_type="mixed")
        self.assertIn(picked, {"http://dyn:1", "http://sta:1"})

    def test_no_match_returns_empty(self):
        """选了一类但池里没有 → 返回空串（不偷偷换类型）。"""
        from services.storage import register_store

        self._pool([("http://sta:1", "static")])
        self.assertEqual(register_store.pick_proxy(proxy_type="dynamic"), "")

    def test_exclude_still_works_with_type_filter(self):
        from services.storage import register_store

        self._pool([("http://dyn:1", "dynamic"), ("http://dyn2:1", "dynamic")])
        picked = register_store.pick_proxy(exclude={"http://dyn:1"}, proxy_type="dynamic")
        self.assertEqual(picked, "http://dyn2:1")


class RegisterProxyModeFilterTests(unittest.TestCase):
    """控制器把模式翻译成 pick_proxy 的过滤值。"""

    def test_mode_mapping(self):
        ctrl = register_batch.AutoLoopController()
        ctrl._register_proxy_mode = register_batch.REGISTER_PROXY_MODE_MIXED
        self.assertEqual(ctrl._register_proxy_mode_filter(), "")
        ctrl._register_proxy_mode = register_batch.REGISTER_PROXY_MODE_DYNAMIC
        self.assertEqual(ctrl._register_proxy_mode_filter(), "dynamic")
        ctrl._register_proxy_mode = register_batch.REGISTER_PROXY_MODE_STATIC
        self.assertEqual(ctrl._register_proxy_mode_filter(), "static")

    def test_start_normalizes_invalid_mode(self):
        ctrl = register_batch.AutoLoopController()
        with mock.patch.object(
            register_batch.AutoLoopController, "_manage_loop", return_value=None
        ), mock.patch.object(register_batch, "db"), mock.patch.object(
            register_batch, "_parse_proxy_pool", return_value=[]
        ):
            ctrl.start({"concurrency": 1, "register_proxy_mode": "瞎写的"})
        self.assertEqual(ctrl._register_proxy_mode, register_batch.REGISTER_PROXY_MODE_MIXED)

    def test_start_keeps_valid_mode(self):
        ctrl = register_batch.AutoLoopController()
        with mock.patch.object(
            register_batch.AutoLoopController, "_manage_loop", return_value=None
        ), mock.patch.object(register_batch, "db"), mock.patch.object(
            register_batch, "_parse_proxy_pool", return_value=[]
        ):
            ctrl.start({"concurrency": 1, "register_proxy_mode": "static"})
        self.assertEqual(ctrl._register_proxy_mode, register_batch.REGISTER_PROXY_MODE_STATIC)

    def test_proxy_for_worker_passes_mode_to_pick(self):
        """worker 取代理时把类型过滤传给存储层。"""
        ctrl = register_batch.AutoLoopController()
        ctrl._backend_proxy_pool = True
        ctrl._register_proxy_mode = register_batch.REGISTER_PROXY_MODE_DYNAMIC
        with mock.patch.object(
            register_batch.db, "pick_proxy", return_value="http://dyn:1"
        ) as pick:
            got = ctrl._proxy_for_worker(0)
        self.assertEqual(got, "http://dyn:1")
        self.assertEqual(pick.call_args.kwargs.get("proxy_type"), "dynamic")


class PoolProxyConsistencyTests(unittest.TestCase):
    """注册用静态代理 → 入池沿用同一条（保持一致）。"""

    def _pool(self, entries: list[tuple[str, str]]):
        from services.storage import register_store

        register_store.replace_proxies([p for p, _ in entries], mode="replace")
        for proxy, ptype in entries:
            register_store.update_proxy(proxy, proxy_type=ptype)

    def test_static_registration_proxy_is_reused(self):
        self._pool([("http://sta:1", "static"), ("http://sta2:1", "static")])
        proxy, reason = register_service.resolve_pool_proxy(
            "static",
            register_proxy="http://sta:1",
            prefer_register_static=True,
        )
        self.assertEqual(proxy, "http://sta:1")
        self.assertIn("一致", reason)

    def test_dynamic_registration_proxy_never_enters_pool(self):
        """注册用动态代理 → 绝不能进号池（改走自动分配静态代理）。"""
        self._pool([("http://dyn:1", "dynamic"), ("http://sta:1", "static")])
        proxy, _ = register_service.resolve_pool_proxy(
            "static",
            register_proxy="http://dyn:1",
            prefer_register_static=True,
        )
        self.assertNotEqual(proxy, "http://dyn:1")
        self.assertEqual(proxy, "http://sta:1")

    def test_none_mode_stays_empty(self):
        """显式选「入池不带代理」时，即使注册用的是静态代理也不带。"""
        self._pool([("http://sta:1", "static")])
        proxy, _ = register_service.resolve_pool_proxy(
            "none",
            register_proxy="http://sta:1",
            prefer_register_static=True,
        )
        self.assertEqual(proxy, "")

    def test_register_mode_reuses_static_registration_proxy(self):
        self._pool([("http://sta:1", "static")])
        proxy, _ = register_service.resolve_pool_proxy(
            "register",
            register_proxy="http://sta:1",
            prefer_register_static=True,
        )
        self.assertEqual(proxy, "http://sta:1")


class GuardianOptionTests(unittest.TestCase):
    """自动补充守护把「注册使用代理」透传给批量控制器。"""

    def test_trigger_passes_register_proxy_mode(self):
        from services import auto_register_guardian as guardian

        settings = guardian.get_settings()
        settings["register_proxy_mode"] = "static"
        with mock.patch("services.register_batch.CONTROLLER.start") as start:
            start.return_value = {"ok": True}
            guardian.trigger_registration(settings)
        options = start.call_args.args[0]
        self.assertEqual(options.get("register_proxy_mode"), "static")

    def test_settings_default_is_mixed(self):
        from services import auto_register_guardian as guardian

        self.assertEqual(guardian.get_settings().get("register_proxy_mode"), "mixed")


if __name__ == "__main__":
    unittest.main()

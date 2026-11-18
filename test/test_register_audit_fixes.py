"""注册管理审查后的一批修复回归测试。

覆盖：
  1. 失败数按「号」算（重试不重复计数），尝试遍数单独统计。
  2. 停止语义：当前号跑完再停；失败后不再重试，号放回号池。
  3. 代理导入可选类型；已有代理不被覆盖。
  4. 入池代理幂等：重复导入不重复占用绑定名额。
  5. 启动前校验「注册使用代理」选的那一类有没有可用代理。
  6. 409 不再重复累加 failed_count。
  7. auth_flow 换代理后类型跟着更新。
"""

from __future__ import annotations

import unittest
from unittest import mock

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-audit-")

from services import register_batch  # noqa: E402


class _FakeProvider:
    pooled = False


def _run_worker(ctrl, *, outcomes, batch_id="audit-test"):
    """跑一个 worker；outcomes 决定每一遍流程的结果。"""
    seen: list[str] = []

    def fake_start(account, options):
        seen.append(account["email"])
        return f"run-{len(seen)}"

    def fake_wait(run_id, timeout=1800):
        idx = len(seen) - 1
        result = outcomes[idx] if idx < len(outcomes) else outcomes[-1]
        # 成功后让 worker 收工，否则它会继续领下一个号（测试里号是无限的）
        if result[0]:
            ctrl._stop_event.set()
        return result

    provider = _FakeProvider()
    with mock.patch.object(register_batch, "get_provider_class", return_value=provider), \
         mock.patch.object(register_batch.db, "get_setting", return_value="remail"), \
         mock.patch.object(register_batch.registrar, "start_registration", side_effect=fake_start), \
         mock.patch.object(ctrl, "_wait_run_finish", side_effect=fake_wait), \
         mock.patch.object(ctrl, "_proxy_for_worker", return_value=""), \
         mock.patch.object(ctrl, "_forward_run_logs", return_value=None), \
         mock.patch.object(ctrl, "_import_to_pool", return_value=None), \
         mock.patch.object(ctrl, "_settle_failed_proxy", return_value=None), \
         mock.patch.object(ctrl, "_finalize_account", return_value=None):
        ctrl._worker_loop_inner(0, batch_id)
    return seen


def _controller(max_retries=3):
    ctrl = register_batch.AutoLoopController()
    ctrl._options = {
        "max_retries": max_retries,
        "cool_down_seconds": 0.0,
        "target_count": 0,
        "batch_id": "audit-test",
        "mail_source": "remail",
    }
    ctrl._max_retries = max_retries
    return ctrl


class FailureCountingTests(unittest.TestCase):
    """失败数按号算；尝试遍数单独记。"""

    def test_retry_then_success_counts_one_ok_zero_fail(self):
        ctrl = _controller(max_retries=3)
        seen = _run_worker(ctrl, outcomes=[(False, "cf_blocked"), (True, "")])
        self.assertEqual(len(seen), 2)
        self.assertEqual(ctrl._registered_ok, 1)
        self.assertEqual(ctrl._registered_fail, 0, "重试的那一遍不该算一个失败号")
        self.assertEqual(ctrl._attempts, 2)

    def test_exhausted_retries_counts_one_fail(self):
        ctrl = _controller(max_retries=2)
        seen = _run_worker(ctrl, outcomes=[(False, "cf_blocked")])
        # 首次 + 2 次重试 = 3 遍，最后算 1 个失败号
        self.assertEqual(len(seen), 3)
        self.assertEqual(ctrl._registered_fail, 1)
        self.assertEqual(ctrl._attempts, 3)


class GracefulStopTests(unittest.TestCase):
    """停止 = 当前号跑完再停。"""

    def test_stop_after_failed_run_releases_account_and_skips_retry(self):
        ctrl = _controller(max_retries=3)

        def fake_wait(run_id, timeout=1800):
            ctrl._stop_event.set()
            return False, "cf_blocked"

        released: list[str] = []
        seen: list[str] = []

        def fake_start(account, options):
            seen.append(account["email"])
            return f"run-{len(seen)}"

        provider = _FakeProvider()
        provider.pooled = True

        with mock.patch.object(register_batch, "get_provider_class", return_value=provider), \
             mock.patch.object(register_batch.db, "get_setting", return_value="outlook"), \
             mock.patch.object(register_batch.db, "claim_next", side_effect=lambda kind="": {
                 "email": "stop@x.com", "password": "pw", "client_id": "",
                 "refresh_token": "", "relay_url": "", "kind": "outlook",
             }), \
             mock.patch.object(register_batch.registrar, "start_registration", side_effect=fake_start), \
             mock.patch.object(ctrl, "_wait_run_finish", side_effect=fake_wait), \
             mock.patch.object(ctrl, "_proxy_for_worker", return_value=""), \
             mock.patch.object(ctrl, "_forward_run_logs", return_value=None), \
             mock.patch.object(ctrl, "_settle_failed_proxy", return_value=None), \
             mock.patch.object(ctrl, "_release_account", side_effect=lambda a, p: released.append(a["email"])):
            ctrl._worker_loop_inner(0, "audit-test")

        # 只跑 1 遍（不再重试），号放回号池
        self.assertEqual(len(seen), 1)
        self.assertEqual(released, ["stop@x.com"])


class ProxyImportTypeTests(unittest.TestCase):
    """导入代理时可以选类型，已有代理不被覆盖。"""

    def setUp(self):
        from services.storage import register_store

        self.store = register_store
        self.store.replace_proxies([], mode="replace")

    def tearDown(self):
        self.store.replace_proxies([], mode="replace")

    def test_import_as_dynamic(self):
        self.store.replace_proxies(["http://d1:1"], mode="append", proxy_type="dynamic")
        self.assertEqual(self.store.find_proxy("http://d1:1")["proxy_type"], "dynamic")

    def test_existing_proxy_type_is_kept_on_reimport(self):
        self.store.replace_proxies(["http://s1:1"], mode="append", proxy_type="static")
        self.store.replace_proxies(["http://s1:1"], mode="append", proxy_type="dynamic")
        self.assertEqual(self.store.find_proxy("http://s1:1")["proxy_type"], "static")

    def test_usable_count_filters_by_type(self):
        self.store.replace_proxies(["http://s2:1"], mode="append", proxy_type="static")
        self.store.replace_proxies(["http://d2:1"], mode="append", proxy_type="dynamic")
        self.assertEqual(self.store.usable_proxy_count("static"), 1)
        self.assertEqual(self.store.usable_proxy_count("dynamic"), 1)
        self.assertEqual(self.store.usable_proxy_count(""), 2)


class ResolvePoolProxyIdempotencyTests(unittest.TestCase):
    """重复导入同一个号不能反复占绑定名额。"""

    def setUp(self):
        from services.register_service import POOL_PROXY_MODE_STATIC
        from services.storage import register_store

        self.store = register_store
        self.mode = POOL_PROXY_MODE_STATIC
        self.store.replace_proxies([], mode="replace")
        self.store.replace_proxies(["http://bind1:1"], mode="append", proxy_type="static")

    def tearDown(self):
        self.store.replace_proxies([], mode="replace")

    def test_reimport_with_existing_proxy_does_not_consume_again(self):
        from services.register_service import resolve_pool_proxy

        first, _ = resolve_pool_proxy(self.mode, register_proxy="", auto_bind=True)
        self.assertEqual(first, "http://bind1:1")
        self.assertEqual(self.store.find_proxy(first)["bind_used_count"], 1)

        # 第二次：带上「号池里现绑的代理」→ 直接沿用，不再 +1
        second, reason = resolve_pool_proxy(
            self.mode, auto_bind=True, existing_proxy=first
        )
        self.assertEqual(second, first)
        self.assertIn("不重复", reason)
        self.assertEqual(self.store.find_proxy(first)["bind_used_count"], 1)

    def test_rebind_to_another_proxy_releases_the_old_one(self):
        from services.register_service import resolve_pool_proxy

        self.store.replace_proxies(["http://bind2:1"], mode="append", proxy_type="static")
        resolve_pool_proxy(self.mode, auto_bind=True)  # 先绑 bind1
        # 指定换到 bind2：应该释放 bind1、占用 bind2
        # （bind_proxy 幂等由 resolve 的 _rebind 负责）
        picked, _ = resolve_pool_proxy(
            self.mode, bind_proxy="http://bind2:1", existing_proxy="http://bind1:1"
        )
        self.assertEqual(picked, "http://bind2:1")
        self.assertEqual(self.store.find_proxy("http://bind2:1")["bind_used_count"], 1)


class InvalidStateCountingTests(unittest.TestCase):
    """409 只记一次失败（proxy_used 已经记过）。"""

    def setUp(self):
        from services.storage import register_store

        self.store = register_store
        self.store.replace_proxies([], mode="replace")
        self.store.replace_proxies(["http://is1:1"], mode="append", proxy_type="static")

    def tearDown(self):
        self.store.replace_proxies([], mode="replace")

    def test_mark_invalid_state_does_not_bump_failed_count(self):
        self.store.mark_proxy_invalid_state("http://is1:1")
        row = self.store.find_proxy("http://is1:1")
        self.assertEqual(row["invalid_state"], 1)
        self.assertEqual(row["failed_count"], 0)


class RotateProxyTypeTests(unittest.TestCase):
    """auth_flow 换代理后，类型要跟着新代理走。"""

    def test_rotation_updates_proxy_type(self):
        from services.register.auth_flow import AuthFlow
        from services.register.config import Config

        cfg = Config()
        cfg.proxy = "http://old:1"
        flow = AuthFlow(
            cfg,
            proxy_rotator=lambda: "http://new:1",
            proxy_type="static",
            proxy_type_resolver=lambda p: "dynamic" if p == "http://new:1" else "static",
        )
        self.assertTrue(flow._rotate_proxy("test"))
        self.assertFalse(flow._proxy_is_static())


class ReleaseBindOnDeleteTests(unittest.TestCase):
    """号从号池删除后，绑定名额要还回去。"""

    def setUp(self):
        from services.storage import register_store

        self.store = register_store
        self.store.replace_proxies([], mode="replace")
        self.store.replace_proxies(["http://del1:1"], mode="append", proxy_type="static")
        self.store.bind_proxy("http://del1:1")

    def tearDown(self):
        self.store.replace_proxies([], mode="replace")

    def test_release_bind_proxies_helper(self):
        from services.account_service import _release_bind_proxies

        self.assertEqual(self.store.find_proxy("http://del1:1")["bind_used_count"], 1)
        _release_bind_proxies(["http://del1:1", "http://not-registered:1"])
        self.assertEqual(self.store.find_proxy("http://del1:1")["bind_used_count"], 0)


if __name__ == "__main__":
    unittest.main()

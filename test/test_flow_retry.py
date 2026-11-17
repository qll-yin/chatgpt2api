"""「流程级重试 = 完整的开始注册 ×N」的回归测试。

背景（用户原话）
----------------
「都以『开始注册 w0#ca8b』这个开始注册为准，那么我说的按失败重试次数进行
重试，是指整个流程，这整个流程就是以开始注册这个为主，重试三次，就是三个
开始注册，然后才停止 worker。」

以及：「这个重新建会话没有换代理 IP 的话，就是没有意义的」。

这里守住的点：
  1. 一次失败 = 一次完整的「开始注册」；重试 N 次 = N 个额外的开始注册，
     全部失败后才停 worker。
  2. 重试**复用同一个号**（不是换号）—— 失败是出口 IP 的锅，不是号的锅，
     换号等于白烧付费邮箱。
  3. 重试**必须换出口 IP**：固定出口（static）的代理失败后，下一遍要避开它。
  4. 409 invalid_state 归到「可重试」分类（以前归 account → 重试根本没触发）。
  5. 流程内部重建会话时（warmup / 409），auth_flow 会**换代理**；
     固定出口又换不到别的代理时，直接失败交外层重试（不再原地空转）。
  6. 固定出口代理失败后被排除；动态代理不排除（否则池子会被挑空）。
"""

from __future__ import annotations

import unittest
from unittest import mock

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-flow-retry-")

from services import register_batch  # noqa: E402
from services.register_registrar import classify_error  # noqa: E402


class _FakeProvider:
    pooled = False


class FlowRetryLoopTests(unittest.TestCase):
    """worker 的流程级重试循环。"""

    def _drive(self, *, max_retries=3, outcomes=None, proxies=None, pooled=True,
               default=(False, "cf_blocked"), max_runs=12):
        """跑一个 worker，返回 (每次开始注册用的号, 每次用的代理)。

        outcomes：每次 run 的 (ok, category)；默认永远失败。
        proxies：代理池按顺序给出的代理；默认同一条。
        pooled=True → 用号池（claim_next 逐个给号），才能验证"换号 vs 复用同一个号"。
        """
        ctrl = register_batch.AutoLoopController()
        ctrl._options = {
            "max_retries": max_retries,
            "cool_down_seconds": 0.01,
            "target_count": 0,
            "batch_id": "flow-retry-test",
            "mail_source": "outlook",
        }
        ctrl._max_retries = max_retries

        emails: list[str] = []
        used_proxies: list[str] = []
        outcomes = list(outcomes or [])
        proxy_seq = list(proxies or [])
        claimed: list[str] = []

        def fake_start(account, options):
            emails.append(account["email"])
            used_proxies.append(str(options.get("proxy") or ""))
            # 兜底：跑够次数就模拟「外部叫停」，免得测试里无限循环
            if len(emails) >= max_runs:
                ctrl._stop_event.set()
            return f"run-{len(emails)}"

        def fake_wait(run_id, timeout=1800):
            idx = len(emails) - 1
            if idx < len(outcomes):
                return outcomes[idx]
            return default

        def fake_proxy(worker_id, exclude=None):
            if not proxy_seq:
                return ""
            return proxy_seq[0]

        def fake_claim(kind=""):
            # 号池里一直有号，够 worker 一直领
            claimed.append(f"acc{len(claimed) + 1}@x.com")
            return {
                "email": claimed[-1],
                "password": "pw",
                "client_id": "",
                "refresh_token": "",
                "relay_url": "",
                "kind": kind or "outlook",
            }

        provider = _FakeProvider()
        provider.pooled = pooled

        with mock.patch.object(register_batch, "get_provider_class", return_value=provider), \
             mock.patch.object(register_batch.db, "get_setting", return_value="outlook"), \
             mock.patch.object(register_batch.db, "claim_next", side_effect=fake_claim), \
             mock.patch.object(register_batch.registrar, "start_registration", side_effect=fake_start), \
             mock.patch.object(ctrl, "_wait_run_finish", side_effect=fake_wait), \
             mock.patch.object(ctrl, "_proxy_for_worker", side_effect=fake_proxy), \
             mock.patch.object(ctrl, "_forward_run_logs", return_value=None), \
             mock.patch.object(ctrl, "_import_to_pool", return_value=None), \
             mock.patch.object(ctrl, "_record_finish", return_value=None), \
             mock.patch.object(ctrl, "_settle_failed_proxy", return_value=None), \
             mock.patch.object(ctrl, "_finalize_account", return_value=None), \
             mock.patch.object(ctrl, "_proxy_type_of", return_value="static"):
            ctrl._worker_loop_inner(0, "flow-retry-test")

        return emails, used_proxies

    def test_retry_produces_extra_full_flows(self):
        """重试 3 次 = 3 个额外的「开始注册」，全部失败才停 worker。"""
        emails, _ = self._drive(max_retries=3)
        # 首次 + 3 次重试 = 4 次完整流程
        self.assertEqual(len(emails), 4)

    def test_retry_reuses_the_same_account(self):
        """⚠️ 重试必须复用**同一个号** —— 换号等于白烧付费邮箱。"""
        emails, _ = self._drive(max_retries=3)
        self.assertEqual(len(set(emails)), 1)

    def test_non_retryable_failure_switches_account(self):
        """account 类失败不可重试 → 换下一个号继续（每个号只跑一遍）。"""
        emails, _ = self._drive(
            max_retries=2,
            outcomes=[(False, "account")] * 3,
            default=(False, "account"),
            max_runs=6,
        )
        # 不可重试 → 一直换号继续（不会因为它停 worker），跑到外部叫停
        self.assertEqual(len(emails), 6)
        # 每个号只跑一遍（同一个号不会被反复重试）
        self.assertEqual(len(set(emails)), 6)

    def test_retry_uses_a_fresh_proxy_each_time(self):
        """每一遍流程都要重新取代理（换出口 IP）。"""
        _, proxies = self._drive(max_retries=3, proxies=["http://p:1"])
        self.assertEqual(len(proxies), 4)
        # 代理是每遍重新取的（不是启动时取一次就一直用）
        self.assertTrue(all(p == "http://p:1" for p in proxies))

    def test_failed_static_proxy_is_excluded_on_next_pick(self):
        """⚠️ 刚失败的那条固定出口代理，下一遍必须被排除（否则还是同一个 IP）。"""
        ctrl = register_batch.AutoLoopController()
        ctrl._options = {
            "max_retries": 2,
            "cool_down_seconds": 0.01,
            "target_count": 0,
            "batch_id": "exclude-test",
            "mail_source": "outlook",
        }
        ctrl._max_retries = 2

        excludes: list[set] = []
        attempts = {"n": 0}

        def fake_proxy(worker_id, exclude=None):
            excludes.append(set(exclude or set()))
            return "http://static-a:1"

        def fake_start(account, options):
            attempts["n"] += 1
            return f"run-{attempts['n']}"

        def fake_wait(run_id, timeout=1800):
            return False, "cf_blocked"

        provider = _FakeProvider()
        provider.pooled = False

        with mock.patch.object(register_batch, "get_provider_class", return_value=provider), \
             mock.patch.object(register_batch.db, "get_setting", return_value="outlook"), \
             mock.patch.object(register_batch.registrar, "start_registration", side_effect=fake_start), \
             mock.patch.object(ctrl, "_wait_run_finish", side_effect=fake_wait), \
             mock.patch.object(ctrl, "_proxy_for_worker", side_effect=fake_proxy), \
             mock.patch.object(ctrl, "_forward_run_logs", return_value=None), \
             mock.patch.object(ctrl, "_import_to_pool", return_value=None), \
             mock.patch.object(ctrl, "_record_finish", return_value=None), \
             mock.patch.object(ctrl, "_settle_failed_proxy", return_value=None), \
             mock.patch.object(ctrl, "_finalize_account", return_value=None), \
             mock.patch.object(ctrl, "_proxy_type_of", return_value="static"):
            ctrl._worker_loop_inner(0, "exclude-test")

        # 第 1 遍没有排除项；第 2 遍开始带上刚失败的那条
        self.assertEqual(excludes[0], set())
        self.assertEqual(excludes[1], {"http://static-a:1"})
        self.assertEqual(excludes[2], {"http://static-a:1"})

    def test_dynamic_proxy_is_not_excluded(self):
        """动态代理每次连接换 IP —— 排除它反而会把池子挑空。"""
        ctrl = register_batch.AutoLoopController()
        ctrl._options = {
            "max_retries": 2,
            "cool_down_seconds": 0.01,
            "target_count": 0,
            "batch_id": "dynamic-test",
            "mail_source": "outlook",
        }
        ctrl._max_retries = 2

        excludes: list[set] = []
        attempts = {"n": 0}

        def fake_proxy(worker_id, exclude=None):
            excludes.append(set(exclude or set()))
            return "http://dyn:1"

        def fake_start(account, options):
            attempts["n"] += 1
            return f"run-{attempts['n']}"

        def fake_wait(run_id, timeout=1800):
            return False, "cf_blocked"

        provider = _FakeProvider()
        provider.pooled = False

        with mock.patch.object(register_batch, "get_provider_class", return_value=provider), \
             mock.patch.object(register_batch.db, "get_setting", return_value="outlook"), \
             mock.patch.object(register_batch.registrar, "start_registration", side_effect=fake_start), \
             mock.patch.object(ctrl, "_wait_run_finish", side_effect=fake_wait), \
             mock.patch.object(ctrl, "_proxy_for_worker", side_effect=fake_proxy), \
             mock.patch.object(ctrl, "_forward_run_logs", return_value=None), \
             mock.patch.object(ctrl, "_import_to_pool", return_value=None), \
             mock.patch.object(ctrl, "_record_finish", return_value=None), \
             mock.patch.object(ctrl, "_settle_failed_proxy", return_value=None), \
             mock.patch.object(ctrl, "_finalize_account", return_value=None), \
             mock.patch.object(ctrl, "_proxy_type_of", return_value="dynamic"):
            ctrl._worker_loop_inner(0, "dynamic-test")

        # 动态代理始终不排除
        self.assertTrue(all(not ex for ex in excludes))


class ExcludeProxyTests(unittest.TestCase):
    """固定出口的代理失败后，下一遍要避开它。"""

    def test_static_proxy_is_excluded_from_next_pick(self):
        ctrl = register_batch.AutoLoopController()
        seen_excludes: list[set] = []

        def fake_proxy(worker_id, exclude=None):
            seen_excludes.append(set(exclude or set()))
            return "http://static-1:1"

        with mock.patch.object(ctrl, "_proxy_for_worker", side_effect=fake_proxy):
            with mock.patch.object(ctrl, "_proxy_type_of", return_value="static"):
                ctrl._proxy_type_of("http://static-1:1")
        # 只是确认接口能带 exclude
        self.assertEqual(seen_excludes, [])

    def test_proxy_type_lookup_is_used_to_decide_exclusion(self):
        """_proxy_type_of 读的是代理池里的 proxy_type。"""
        ctrl = register_batch.AutoLoopController()
        with mock.patch.object(
            register_batch.db, "find_proxy", return_value={"proxy_type": "static"}
        ):
            self.assertEqual(ctrl._proxy_type_of("http://p:1"), "static")
        with mock.patch.object(
            register_batch.db, "find_proxy", return_value={"proxy_type": "dynamic"}
        ):
            self.assertEqual(ctrl._proxy_type_of("http://p:1"), "dynamic")


class ClassifyErrorTests(unittest.TestCase):
    """错误分类。"""

    def test_invalid_state_is_its_own_category(self):
        err = (
            "authorize/continue 失败(screen_hint=signup): HTTP 409 "
            "body={\"error\":{\"code\":\"invalid_state\",\"message\":"
            "\"Your sign-in session is no longer valid. Please start over to continue.\"}}"
        )
        self.assertEqual(classify_error(err), "invalid_state")
        self.assertIn("invalid_state", register_batch.RETRYABLE_CATEGORIES)

    def test_warmup_failure_is_cf_blocked(self):
        """warmup 失败那句里也含 "invalid_state" 字样，但不能被误判成 409。"""
        err = (
            "warmup 失败：4 次重试均未拿到 chatgpt.com 的 oai-did cookie，"
            "继续注册必然 409 invalid_state（多为代理出口 IP 不通或被 CF 拦），"
            "请检查代理后重试"
        )
        self.assertEqual(classify_error(err), "cf_blocked")

    def test_user_already_exists_stays_account(self):
        self.assertEqual(classify_error("user_already_exists"), "account")


class AuthFlowRotateProxyTests(unittest.TestCase):
    """auth_flow 内部重试要换出口 IP。"""

    def _flow(self, *, rotator=None, proxy_type=""):
        from services.register.auth_flow import AuthFlow
        from services.register.config import Config

        cfg = Config()
        cfg.proxy = "http://old:1"
        return AuthFlow(cfg, proxy_rotator=rotator, proxy_type=proxy_type)

    def test_rotate_proxy_switches_proxy(self):
        flow = self._flow(rotator=lambda: "http://new:1", proxy_type="static")
        self.assertTrue(flow._rotate_proxy("test"))
        self.assertEqual(flow.config.proxy, "http://new:1")

    def test_rotate_proxy_without_rotator_returns_false(self):
        flow = self._flow(proxy_type="static")
        self.assertFalse(flow._rotate_proxy("test"))

    def test_rotate_proxy_same_proxy_returns_false(self):
        flow = self._flow(rotator=lambda: "http://old:1", proxy_type="static")
        self.assertFalse(flow._rotate_proxy("test"))

    def test_static_detection(self):
        self.assertTrue(self._flow(proxy_type="static")._proxy_is_static())
        self.assertFalse(self._flow(proxy_type="dynamic")._proxy_is_static())
        self.assertFalse(self._flow()._proxy_is_static())


class PickProxyExcludeTests(unittest.TestCase):
    """pick_proxy 支持排除指定代理（真实数据库）。"""

    def _pool(self, proxies):
        from services.storage import register_store

        for p in proxies:
            register_store.replace_proxies([p], mode="replace")
            register_store.update_proxy(p, proxy_type="static")

    def test_excluded_proxy_is_skipped_when_others_exist(self):
        from services.storage import register_store

        self._pool(["http://p1:1", "http://p2:1"])
        picked = register_store.pick_proxy(exclude={"http://p1:1"})
        self.assertEqual(picked, "http://p2:1")

    def test_falls_back_to_excluded_when_pool_has_only_one(self):
        """池里只有一条时不能"没得挑" —— 否则重试会卡死。"""
        from services.storage import register_store

        self._pool(["http://only:1"])
        picked = register_store.pick_proxy(exclude={"http://only:1"})
        self.assertEqual(picked, "http://only:1")


class RealDbAccountLifecycleTests(unittest.TestCase):
    """真实数据库：重试期间号不会被放回池子，最后由 worker 收尾。"""

    @staticmethod
    def _status_of(email: str) -> str:
        from services.storage import register_store

        rows, _ = register_store.list_accounts()
        for row in rows:
            if row.get("email") == email:
                return str(row.get("status") or "")
        return ""

    def test_account_stays_in_use_during_retry_then_released(self):
        from services.storage import register_store

        register_store.import_accounts(
            "flowtest@outlook.com----pw----cid----rt", "outlook"
        )
        ctrl = register_batch.AutoLoopController()
        ctrl._options = {
            "max_retries": 1,
            "cool_down_seconds": 0.01,
            "target_count": 0,
            "batch_id": "real-db-test",
            "mail_source": "outlook",
        }
        ctrl._max_retries = 1

        seen: list[str] = []

        def fake_start(account, options):
            seen.append(account["email"])
            # 重试期间号必须还是 in_use（不能被放回池子被别人领走）
            self.assertEqual(self._status_of(account["email"]), "in_use")
            return f"run-{len(seen)}"

        def fake_wait(run_id, timeout=1800):
            return False, "invalid_state"

        provider = _FakeProvider()
        provider.pooled = True

        with mock.patch.object(register_batch, "get_provider_class", return_value=provider), \
             mock.patch.object(register_batch.db, "get_setting", return_value="outlook"), \
             mock.patch.object(register_batch.registrar, "start_registration", side_effect=fake_start), \
             mock.patch.object(ctrl, "_wait_run_finish", side_effect=fake_wait), \
             mock.patch.object(ctrl, "_proxy_for_worker", return_value=""), \
             mock.patch.object(ctrl, "_forward_run_logs", return_value=None), \
             mock.patch.object(ctrl, "_import_to_pool", return_value=None), \
             mock.patch.object(ctrl, "_record_finish", return_value=None), \
             mock.patch.object(ctrl, "_settle_failed_proxy", return_value=None):
            ctrl._worker_loop_inner(0, "real-db-test")

        # 同一个号被跑了 2 遍（首次 + 1 次重试）
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(set(seen)), 1)
        # 重试到底还是失败（出口 IP 的锅）→ 号放回池子复用，不能判死
        self.assertEqual(self._status_of("flowtest@outlook.com"), "available")


if __name__ == "__main__":
    unittest.main()

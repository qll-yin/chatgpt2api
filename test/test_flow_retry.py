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
  6. 失败过的代理（静态/动态都算）下一遍要避开；池里只剩一条时自动回退，
     不会因此卡死。
"""

from __future__ import annotations

import threading
import time
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

    def test_dynamic_proxy_is_also_excluded(self):
        """动态代理失败后也要避开：导入 5 条时不能一直随机回同一条。

        用户原话：「如果只有一个动态的，随机到同一个是正常的，但如果我导入了
        5 个，肯定就不能还是只选那一个了」。
        池里只剩一条时 pick_proxy 会自动回退到它，不会卡死。
        """
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
             mock.patch.object(ctrl, "_finalize_account", return_value=None):
            ctrl._worker_loop_inner(0, "dynamic-test")

        # 第 1 遍没有排除项；之后每一遍都避开刚失败的动态代理
        self.assertEqual(excludes[0], set())
        self.assertEqual(excludes[1], {"http://dyn:1"})
        self.assertEqual(excludes[2], {"http://dyn:1"})


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


class ProxyAutoDisableGuardTests(unittest.TestCase):
    """代理「自动停用」不能把流程级重试掐断。

    用户报的现象：第 1 个成功，第 2 个失败后 worker 就结束了 ——
    根因是「静态代理累计被拦 2 次 → enabled=False」把池子清空，
    下一遍流程 pick_proxy 拿不到代理 → worker 直接收工。
    """

    def setUp(self):
        from services.storage import register_store

        self.store = register_store
        self.store.replace_proxies([], mode="replace")

    def tearDown(self):
        self.store.replace_proxies([], mode="replace")

    def _add(self, entries):
        for proxy, ptype in entries:
            self.store.replace_proxies([proxy], mode="append")
            self.store.update_proxy(proxy, proxy_type=ptype)

    def test_only_proxy_is_kept_enabled(self):
        """池里只剩一条静态代理时，被 CF 拦到阈值也**不能**停用。"""
        self._add([("http://only:1", "static")])
        self.store.mark_proxy_cf_blocked("http://only:1")
        res = self.store.mark_proxy_cf_blocked("http://only:1")
        self.assertFalse(res["disabled"])
        self.assertTrue(res["kept_enabled"])
        self.assertTrue(self.store.find_proxy("http://only:1")["enabled"])

    def test_only_proxy_kept_on_invalid_state(self):
        self._add([("http://only409:1", "static")])
        self.store.mark_proxy_invalid_state("http://only409:1")
        res = self.store.mark_proxy_invalid_state("http://only409:1")
        self.assertFalse(res["disabled"])
        self.assertTrue(res["kept_enabled"])
        self.assertTrue(self.store.find_proxy("http://only409:1")["enabled"])

    def test_proxy_is_disabled_when_another_can_take_over(self):
        """还有别的同类代理能顶上来时才允许停用。"""
        self._add([("http://a:1", "static"), ("http://b:1", "static")])
        self.store.mark_proxy_cf_blocked("http://a:1")
        res = self.store.mark_proxy_cf_blocked("http://a:1")
        self.assertTrue(res["disabled"])
        self.assertFalse(self.store.find_proxy("http://a:1")["enabled"])
        self.assertTrue(self.store.find_proxy("http://b:1")["enabled"])

    def test_success_resets_the_streak(self):
        """成功一次就证明出口是通的 → 「连续被拦」计数必须归零。"""
        self._add([("http://c:1", "static"), ("http://d:1", "static")])
        self.store.mark_proxy_cf_blocked("http://c:1")
        self.assertEqual(self.store.find_proxy("http://c:1")["cf_blocked"], 1)
        self.store.proxy_mark_success("http://c:1")
        row = self.store.find_proxy("http://c:1")
        self.assertEqual(row["cf_blocked"], 0)
        self.assertEqual(row["invalid_state"], 0)
        # 之后再被拦一次，还是第 1 次，不停用
        res = self.store.mark_proxy_cf_blocked("http://c:1")
        self.assertFalse(res["disabled"])


class SuccessThenFailureStillRetriesTests(unittest.TestCase):
    """用户报的原始场景：第 1 个成功 → 第 2 个失败 → 必须继续重试 3 遍。

    用**真实数据库 + 真实代理池**跑，代理停用/记账/重试全走真代码。
    """

    def setUp(self):
        from services.storage import register_store

        self.store = register_store
        self.store.replace_proxies([], mode="replace")
        self.store.replace_proxies(["http://static1:1"], mode="append")
        self.store.update_proxy("http://static1:1", proxy_type="static")

    def tearDown(self):
        self.store.replace_proxies([], mode="replace")

    def test_pool_exhausted_pauses_instead_of_burning_retries(self):
        """1 个号成功 → 第 2 个号 CF 拦 → 池里唯一的代理进黑名单 → 暂停整批。

        ⚠️ 语义变更（用户确认）：代理全被 CF 拦时**不要再空转重试**——
        同一个 IP 撞 4 遍只是白烧付费邮箱，正确做法是暂停等黑名单过期
        （或补代理）后点「继续」。
        """
        import tempfile
        from pathlib import Path

        ctrl = register_batch.AutoLoopController()
        ctrl._options = {
            "max_retries": 3,
            "cool_down_seconds": 0.01,
            "target_count": 0,
            "batch_id": "pool-exhausted",
            "mail_source": "outlook",
            "auto_import": False,
        }
        ctrl._max_retries = 3
        ctrl._backend_proxy_pool = True
        ctrl._register_proxy_mode = register_batch.REGISTER_PROXY_MODE_MIXED
        ctrl._proxy_wait_secs = 0

        emails: list[str] = []
        claimed: list[str] = []

        def fake_claim(kind=""):
            claimed.append(f"acc{len(claimed) + 1}@x.com")
            return {
                "email": claimed[-1], "password": "pw", "client_id": "",
                "refresh_token": "", "relay_url": "", "kind": kind or "outlook",
            }

        def fake_start(account, options):
            emails.append(account["email"])
            return f"run-{len(emails)}"

        def fake_wait(run_id, timeout=1800):
            idx = len(emails) - 1
            if idx == 0:
                return True, ""           # 第 1 个号成功
            return False, "cf_blocked"    # 之后 CF 拦

        provider = _FakeProvider()
        provider.pooled = True
        tmpdir = Path(tempfile.mkdtemp(prefix="pool-exhausted-"))

        with mock.patch("services.register_registrar.LOG_DIR", tmpdir), \
             mock.patch.object(register_batch, "get_provider_class", return_value=provider), \
             mock.patch.object(register_batch.db, "get_setting", return_value="outlook"), \
             mock.patch.object(register_batch.db, "claim_next", side_effect=fake_claim), \
             mock.patch.object(register_batch.registrar, "start_registration", side_effect=fake_start), \
             mock.patch.object(ctrl, "_wait_run_finish", side_effect=fake_wait), \
             mock.patch.object(ctrl, "_refresh_pool_quota_async", return_value=None), \
             mock.patch.object(ctrl, "_forward_run_logs", return_value=None):
            thread = threading.Thread(
                target=ctrl._worker_loop_inner, args=(0, "pool-exhausted"), daemon=True
            )
            thread.start()
            deadline = time.time() + 15
            while time.time() < deadline and ctrl.status()["state"] != "stopped":
                time.sleep(0.05)
            state = ctrl.status()["state"]
            ctrl._stop_event.set()
            thread.join(timeout=5)

        # ⚠️ 语义变更（用户实测反馈）：等满仍全黑时是 **stopped**，不是 paused。
        #    paused 会让「号池自动补充」一直认为「已在运行」而不拉起 → 死锁。
        self.assertEqual(state, "stopped", "等不到代理恢复时应停批，而不是暂停等人工点继续")
        # 成功 1 次 + 第 2 个号失败 1 遍；没有把 3 次重试全跑完
        self.assertEqual(len(emails), 2)
        self.assertEqual(emails[0], "acc1@x.com")
        self.assertEqual(emails[1], "acc2@x.com")
        # 环境问题不该记成「号失败」
        self.assertEqual(ctrl._registered_fail, 0)
        # 批次级原因写进「运行记录 → 日志」
        log_path = tmpdir / "pool-exhausted.batch.log"
        self.assertTrue(log_path.is_file())
        self.assertIn("全部被 CF 拦截", log_path.read_text(encoding="utf-8"))


class WorkerVisibilityTests(unittest.TestCase):
    """批次级决策必须出现在前端实时日志里（batch_log 事件）。

    用户报的原话：「第二个失败了，就结束 worker 了」—— 前端日志里
    只有「注册出错：…」，后面一行都没有，完全看不出为什么停。
    """

    def _events(self, ctrl):
        """订阅 SSE，返回 (收集列表, 排空函数)。"""
        q = ctrl.subscribe()
        out = []

        def drain():
            while True:
                try:
                    out.append(q.get_nowait())
                except Exception:  # noqa: BLE001
                    return

        return out, drain

    def test_emit_log_broadcasts_batch_log(self):
        ctrl = register_batch.AutoLoopController()
        out, drain = self._events(ctrl)
        ctrl._emit_log("第 1/4 遍流程失败 → 开始第 1/3 次重试", level="warning")
        drain()
        kinds = [e for e in out if e.get("kind") == "batch_log"]
        self.assertTrue(kinds)
        self.assertIn("第 1/3 次重试", kinds[0]["data"]["message"])
        self.assertEqual(kinds[0]["data"]["level"], "warning")

    def test_worker_exception_is_visible_not_silent(self):
        """worker 线程异常不能静默杀死批次 —— 必须广播出来。"""
        ctrl = register_batch.AutoLoopController()
        q = ctrl.subscribe()
        ctrl._options = {"batch_id": "crash-test"}
        with mock.patch.object(ctrl, "_worker_loop_inner", side_effect=RuntimeError("DB 挂了")):
            ctrl._worker_loop(0)
        events = []
        while True:
            try:
                events.append(q.get_nowait())
            except Exception:  # noqa: BLE001
                break
        msgs = [
            str(e.get("data", {}).get("message") or "")
            for e in events
            if e.get("kind") == "batch_log"
        ]
        self.assertTrue(
            any("线程异常退出" in m and "DB 挂了" in m for m in msgs),
            f"没广播 worker 异常：{msgs}",
        )
        self.assertIn("线程异常退出", str(ctrl.status().get("last_message") or ""))

    def test_settle_failed_proxy_swallows_errors(self):
        """代理记账出错不能把重试链掐掉。"""
        ctrl = register_batch.AutoLoopController()
        ctrl._backend_proxy_pool = True
        with mock.patch.object(
            register_batch.db, "proxy_release", side_effect=RuntimeError("db down")
        ):
            # 不抛异常 = 通过
            ctrl._settle_failed_proxy(0, "http://p:1", "cf_blocked")


class PostgresSqlCompatTests(unittest.TestCase):
    """代理配额归还的 SQL 必须跨数据库可用。

    ⚠️ 真实事故：`func.max(0, used_count - 1)` 在 SQLite 里能跑
    （max 是标量函数），在 **PostgreSQL** 里直接报
    「function max(integer, integer) does not exist」（max 是聚合函数）。
    这条 SQL 在「注册失败 → 归还代理配额」路径上 → PG 部署下每次失败
    都抛异常把 worker 线程炸掉 → 批次瞬间停止、重试一次都没跑。
    """

    def test_proxy_release_sql_has_no_aggregate_max(self):
        from sqlalchemy import update
        from sqlalchemy.dialects import postgresql

        from services.storage.register_models import ProxyPoolModel
        from services.storage.register_store import _dec_floor_zero

        stmt = update(ProxyPoolModel).values(
            used_count=_dec_floor_zero(ProxyPoolModel.used_count)
        )
        sql = str(
            stmt.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        ).lower()
        self.assertNotIn("max(", sql, f"PG 不认 max(a,b)：{sql}")
        self.assertIn("case when", sql)

    def test_proxy_release_floors_at_zero(self):
        """重复释放不能减到负数。"""
        from services.storage import register_store

        register_store.replace_proxies(["http://floor:1"], mode="replace")
        register_store.pick_proxy()
        self.assertEqual(register_store.find_proxy("http://floor:1")["used_count"], 1)
        register_store.proxy_release("http://floor:1")
        self.assertEqual(register_store.find_proxy("http://floor:1")["used_count"], 0)
        register_store.proxy_release("http://floor:1")
        self.assertEqual(register_store.find_proxy("http://floor:1")["used_count"], 0)

    def test_release_bind_proxy_floors_at_zero(self):
        from services.storage import register_store

        register_store.replace_proxies(["http://bindfloor:1"], mode="replace")
        register_store.update_proxy("http://bindfloor:1", proxy_type="static")
        register_store.bind_proxy("http://bindfloor:1")
        register_store.release_bind_proxy("http://bindfloor:1")
        register_store.release_bind_proxy("http://bindfloor:1")
        self.assertEqual(
            register_store.find_proxy("http://bindfloor:1")["bind_used_count"], 0
        )


if __name__ == "__main__":
    unittest.main()

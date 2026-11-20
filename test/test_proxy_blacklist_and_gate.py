"""代理黑名单（批次级）+ 下单前 auth 预检。

用户日志里两个具体问题：
1. 5 条代理全被 CF 403，3 个 worker 各自把同一批 IP 撞了 56 次；流程内换代理
   还会「换回刚失败那条」（只排除当前一条）。
2. 12 单付费邮箱里 10 单是在 auth 域名还没验证的情况下下单的 —— 钱花了才失败。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services import register_batch
from services.register_batch import AutoLoopController


class ProxyBlacklistTests(unittest.TestCase):
    def setUp(self):
        self.ctrl = AutoLoopController()
        self.ctrl._options = {"batch_id": "test-batch"}

    def test_mark_and_read(self):
        self.ctrl._mark_proxy_blocked("http://p1:1", "cf_blocked")
        self.assertIn("http://p1:1", self.ctrl._blacklisted_proxies())

    def test_blacklist_expires(self):
        with mock.patch("services.register_batch.time.monotonic", return_value=100.0):
            self.ctrl._mark_proxy_blocked("http://p1:1", "cf_blocked")
        # TTL 内还在
        with mock.patch("services.register_batch.time.monotonic", return_value=150.0):
            self.assertIn("http://p1:1", self.ctrl._blacklisted_proxies())
        # 过期后自动清掉
        with mock.patch("services.register_batch.time.monotonic", return_value=1000.0):
            self.assertEqual(self.ctrl._blacklisted_proxies(), set())

    def test_zero_ttl_disables_blacklist(self):
        from services.config import config

        original = config.data.get("register_proxy_blacklist_secs")
        config.data["register_proxy_blacklist_secs"] = 0
        try:
            self.ctrl._mark_proxy_blocked("http://p1:1")
            self.assertEqual(self.ctrl._blacklisted_proxies(), set())
        finally:
            if original is None:
                config.data.pop("register_proxy_blacklist_secs", None)
            else:
                config.data["register_proxy_blacklist_secs"] = original

    def test_pick_proxy_gets_blacklist_and_strict(self):
        self.ctrl._backend_proxy_pool = True
        self.ctrl._mark_proxy_blocked("http://dead:1")
        captured = {}

        def fake_pick(exclude=None, proxy_type="", strict=False):
            captured["exclude"] = set(exclude or set())
            captured["strict"] = strict
            return "http://alive:1"

        with mock.patch.object(register_batch.db, "pick_proxy", side_effect=fake_pick):
            picked = self.ctrl._proxy_for_worker(0, exclude={"http://failed:1"})

        self.assertEqual(picked, "http://alive:1")
        self.assertIn("http://dead:1", captured["exclude"])
        self.assertIn("http://failed:1", captured["exclude"])
        self.assertTrue(captured["strict"], "有黑名单时必须严格排除，不能回退到死 IP")

    def test_no_blacklist_keeps_lenient_fallback(self):
        self.ctrl._backend_proxy_pool = True
        captured = {}

        def fake_pick(exclude=None, proxy_type="", strict=False):
            captured["strict"] = strict
            return "http://only:1"

        with mock.patch.object(register_batch.db, "pick_proxy", side_effect=fake_pick):
            self.ctrl._proxy_for_worker(0, exclude={"http://only:1"})
        self.assertFalse(captured["strict"], "没有黑名单时保留「只有一条代理」的回退行为")

    def test_wait_writes_batch_log(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="batch-log-"))
        with mock.patch("services.register_registrar.LOG_DIR", tmpdir), \
             mock.patch.object(self.ctrl, "_emit_log", return_value=None), \
             mock.patch.object(self.ctrl, "_broadcast", return_value=None), \
             mock.patch.object(self.ctrl, "_proxy_recovery_budget", return_value=0), \
             mock.patch.object(register_batch.db, "count_usable_proxies", return_value=0):
            outcome = self.ctrl._wait_for_proxy_recovery(0, 3)

        self.assertEqual(outcome, "timeout")
        log_path = tmpdir / "test-batch.batch.log"
        self.assertTrue(log_path.is_file())
        self.assertIn("全部被 CF 拦截", log_path.read_text(encoding="utf-8"))


class ProxyRecoveryWaitTests(unittest.TestCase):
    """池全黑不能「暂停等人工点继续」，要自己等恢复、恢复后自己继续。

    用户实测反馈（2026-11-20）：
      · 日志写着「黑名单 120 秒后自动解除」，但批次一直是 paused ——
        黑名单确实过期了，可没有任何代码去清 `_pause_event`，必须手动点；
      · 更糟的是「号池自动补充」把 paused 算作「批量注册已在运行」，
        于是一路 skip，全自动场景直接死锁。
    """

    def setUp(self):
        self.ctrl = AutoLoopController()
        self.ctrl._options = {"batch_id": "test-batch"}
        self.ctrl._state = "running"

    def test_thread_never_enters_paused(self):
        """等待期间不能置全局 PAUSED —— 那会让自动补充永远拉不起来。"""
        calls = {"n": 0}

        def fake_count(*args, **kwargs):
            calls["n"] += 1
            return 0 if calls["n"] < 2 else 1  # 第 2 次检查时代理恢复了

        tmpdir = Path(tempfile.mkdtemp(prefix="proxy-wait-"))
        with mock.patch("services.register_registrar.LOG_DIR", tmpdir), \
             mock.patch.object(self.ctrl, "_emit_log", return_value=None), \
             mock.patch.object(self.ctrl, "_broadcast", return_value=None), \
             mock.patch.object(self.ctrl, "_proxy_recovery_budget", return_value=5), \
             mock.patch.object(register_batch.time, "sleep", return_value=None), \
             mock.patch.object(register_batch.db, "count_usable_proxies", side_effect=fake_count):
            outcome = self.ctrl._wait_for_proxy_recovery(0, 3)

        self.assertEqual(outcome, "recovered")
        self.assertFalse(self.ctrl._pause_event.is_set(), "自动等待不能顺手把批次暂停掉")
        self.assertEqual(self.ctrl.status()["state"], "running", "恢复后应自动回到运行中")

    def test_waiting_state_while_exhausted(self):
        """没有其它 worker 在跑时，等待要显示成「等待中」而不是「运行中」。"""
        seen = {}

        def fake_count(*args, **kwargs):
            seen["state"] = self.ctrl.status()["state"]
            return 1

        tmpdir = Path(tempfile.mkdtemp(prefix="proxy-wait2-"))
        with mock.patch("services.register_registrar.LOG_DIR", tmpdir), \
             mock.patch.object(self.ctrl, "_emit_log", return_value=None), \
             mock.patch.object(self.ctrl, "_broadcast", return_value=None), \
             mock.patch.object(self.ctrl, "_proxy_recovery_budget", return_value=5), \
             mock.patch.object(register_batch.time, "sleep", return_value=None), \
             mock.patch.object(register_batch.db, "count_usable_proxies", side_effect=fake_count):
            self.ctrl._wait_for_proxy_recovery(0, 3)

        self.assertEqual(seen.get("state"), "waiting")

    def test_unrecoverable_stops_instead_of_pausing(self):
        """等满仍全黑 → 停批（stopped），不是 paused。

        stopped 不在守护的 active 集合（running / paused / waiting）里，
        所以「号池自动补充」下一轮就能重新拉起，不会死锁。
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="proxy-stop-"))
        with mock.patch("services.register_registrar.LOG_DIR", tmpdir), \
             mock.patch.object(self.ctrl, "_emit_log", return_value=None), \
             mock.patch.object(self.ctrl, "_broadcast", return_value=None), \
             mock.patch.object(self.ctrl, "_proxy_recovery_budget", return_value=0):
            self.ctrl._stop_for_proxy_unrecoverable(0, 3)

        self.assertEqual(self.ctrl.status()["state"], "stopped")
        self.assertNotEqual(self.ctrl.status()["state"], "paused")
        self.assertTrue(self.ctrl._stop_event.is_set())
        content = (tmpdir / "test-batch.batch.log").read_text(encoding="utf-8")
        self.assertIn("全部被 CF 拦截", content)
        self.assertIn("已停止", content)

    def test_does_not_interrupt_other_running_workers(self):
        """别的 worker 还在跑时，不能 set 全局 stop_event 把它的注册打断。"""
        self.ctrl._alive_workers.add(("test-batch", 1))  # 还有 worker-1 活着
        tmpdir = Path(tempfile.mkdtemp(prefix="proxy-keepalive-"))
        with mock.patch("services.register_registrar.LOG_DIR", tmpdir), \
             mock.patch.object(self.ctrl, "_emit_log", return_value=None), \
             mock.patch.object(self.ctrl, "_broadcast", return_value=None), \
             mock.patch.object(self.ctrl, "_proxy_recovery_budget", return_value=0):
            self.ctrl._stop_for_proxy_unrecoverable(0, 3)

        self.assertFalse(self.ctrl._stop_event.is_set(), "不能打断正在注册的 worker")
        self.assertNotEqual(self.ctrl.status()["state"], "stopped")

    def test_pick_timeout_skips_two_minute_idle_wait(self):
        """全黑时 `_wait_for_proxy` 要立刻返回，不能先空等满 2 分钟。"""
        self.ctrl._backend_proxy_pool = True
        self.ctrl._proxy_wait_secs = 120
        self.ctrl._mark_proxy_blocked("http://dead:1", "cf_blocked")

        started = time.time()

        def fake_count(exclude=None, proxy_type=""):
            # 排除黑名单后 0 条可用，池里总共 3 条 → 判定为「全黑」
            return 0 if exclude else 3

        with mock.patch.object(register_batch.db, "pick_proxy", return_value=""), \
             mock.patch.object(register_batch.db, "count_usable_proxies", side_effect=fake_count):
            picked = self.ctrl._wait_for_proxy(0, exclude=set())
        elapsed = time.time() - started

        self.assertEqual(picked, "")
        self.assertLess(elapsed, 5, "池全黑应立即返回，交给等待恢复的分支，不能先睡满 120 秒")


class BatchLogEndpointTests(unittest.TestCase):
    def test_appends_batch_log_at_the_end(self):
        from api.register import _append_batch_level_log

        tmpdir = Path(tempfile.mkdtemp(prefix="batch-log-ep-"))
        (tmpdir / "b1.batch.log").write_text("03:00:00 [ERROR] paused\n", encoding="utf-8")
        with mock.patch("services.register_registrar.LOG_DIR", tmpdir):
            content = _append_batch_level_log("run-1 log", "b1")
        self.assertIn("run-1 log", content)
        self.assertIn("批次日志", content)
        self.assertTrue(content.rstrip().endswith("paused"), "批次级日志要拼在最末尾")

    def test_no_batch_log_returns_content(self):
        from api.register import _append_batch_level_log

        tmpdir = Path(tempfile.mkdtemp(prefix="batch-log-ep2-"))
        with mock.patch("services.register_registrar.LOG_DIR", tmpdir):
            self.assertEqual(_append_batch_level_log("run-1 log", "nope"), "run-1 log")


class AuthProbeGateTests(unittest.TestCase):
    """下单付费邮箱前必须先过 auth 域名预检。"""

    def _make_flow(self):
        from services.register.auth_flow import AuthFlow
        from services.register.config import Config

        flow = AuthFlow(Config())
        flow.check_proxy = lambda: True
        flow.warmup = lambda: True
        flow.get_csrf_token = lambda: "csrf-token"
        return flow

    def test_probe_failure_blocks_mailbox_order(self):
        flow = self._make_flow()

        def boom(csrf):
            raise RuntimeError("下单邮箱前预检 auth 域名失败（HTTP 403）")

        flow.probe_auth_endpoint = boom
        provider = mock.MagicMock()
        with self.assertRaises(RuntimeError) as ctx:
            flow.run_register(provider)
        self.assertIn("预检", str(ctx.exception))
        provider.create_mailbox.assert_not_called()

    def test_probe_wraps_underlying_error(self):
        flow = self._make_flow()
        flow.get_auth_url = mock.Mock(side_effect=RuntimeError("HTTP 403 forbidden"))
        with self.assertRaises(RuntimeError) as ctx:
            flow.probe_auth_endpoint("csrf")
        self.assertIn("403", str(ctx.exception))
        self.assertIn("未消耗邮箱", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

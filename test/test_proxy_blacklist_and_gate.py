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

    def test_stop_writes_batch_log(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="batch-log-"))
        with mock.patch("services.register_registrar.LOG_DIR", tmpdir), \
             mock.patch.object(self.ctrl, "_emit_log", return_value=None), \
             mock.patch.object(self.ctrl, "_broadcast", return_value=None), \
             mock.patch.object(self.ctrl, "_refresh_pool_quota_async", return_value=None):
            self.ctrl._stop_batch_for_proxy_exhausted(0, 3)

        self.assertEqual(self.ctrl.status()["state"], "stopped")
        log_path = tmpdir / "test-batch.batch.log"
        self.assertTrue(log_path.is_file())
        self.assertIn("全部被 CF 拦截", log_path.read_text(encoding="utf-8"))


class ProxyExhaustedStopTests(unittest.TestCase):
    """批次内代理全进黑名单 → 直接停批，不等待。

    用户要求（2026-11-20）：
      「谁说一定非得 180 秒的，这个机制移除吧，如果当前批次内代理 IP 都黑名单后，
        直接停止任务并刷新账号额度。等待下一次的自动补充任务启动。
        手动批量注册也这样做，不要加时间等待了」
    """

    def setUp(self):
        self.ctrl = AutoLoopController()
        self.ctrl._options = {"batch_id": "test-batch"}
        self.ctrl._state = "running"

    def _stop(self, total=3):
        tmpdir = Path(tempfile.mkdtemp(prefix="proxy-stop-"))
        refreshed = []
        with mock.patch("services.register_registrar.LOG_DIR", tmpdir), \
             mock.patch.object(self.ctrl, "_emit_log", return_value=None), \
             mock.patch.object(self.ctrl, "_broadcast", return_value=None), \
             mock.patch.object(
                 self.ctrl, "_refresh_pool_quota_async",
                 side_effect=lambda: refreshed.append(True),
             ):
            started = time.time()
            self.ctrl._stop_batch_for_proxy_exhausted(0, total)
            elapsed = time.time() - started
        return tmpdir, refreshed, elapsed

    def test_stops_immediately(self):
        """不能有任何等待 —— 全黑就立刻停。"""
        tmpdir, _, elapsed = self._stop()
        self.assertLess(elapsed, 1.0, "全黑必须立刻停批，不能 sleep 等 TTL")
        self.assertEqual(self.ctrl.status()["state"], "stopped")

    def test_never_leaves_batch_paused(self):
        """必须是 stopped，不能是 paused —— paused 会让自动补充永远拉不起来。"""
        self._stop()
        self.assertEqual(self.ctrl.status()["state"], "stopped")
        self.assertFalse(self.ctrl._pause_event.is_set())
        self.assertTrue(self.ctrl._stop_event.is_set())

    def test_refreshes_quota_on_stop(self):
        """停批时要刷一次额度，让前端数字和守护判断都用最新值。"""
        _, refreshed, _ = self._stop()
        self.assertEqual(len(refreshed), 1)

    def test_stop_reason_is_visible(self):
        """状态栏要能看到「为什么停」，不能被「已停止（成功 X / 失败 Y）」盖掉。"""
        self._stop()
        self.assertIn("全部被 CF 拦截", self.ctrl.status()["last_message"])

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
        self.assertLess(elapsed, 5, "池全黑应立即返回，不能先睡满 120 秒")


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

"""worker 级重试的回归测试。

背景
----
旧注册机的语义是「**按整个流程**算重试次数」：
一次完整的注册流程（哪怕内部撞了 409 invalid_state 重建过会话）
只算**一次**尝试；流程整体失败后，worker 再跑几遍，
都不行才停掉这个 worker。

原先这套在本项目里是缺失的 —— 流程失败一次，worker 就直接收工，
号没注册出来，对用户毫无价值。

这里守住的点：
  1. max_retries 的解析（含 0 = 不重试这个边界）
  2. 哪些失败该重试 / 不该重试
  3. 重试次数用尽 → worker 真的停下来
  4. 成功一次 → 重试计数清零
  5. 失败也要结算进批次统计（成功 X / 失败 Y 的 Y）
"""

from __future__ import annotations

import unittest
from unittest import mock

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-worker-retry-")

from services import register_batch  # noqa: E402


class RetryCategoryTests(unittest.TestCase):
    """可重试 / 不可重试的分类。"""

    def test_cf_blocked_is_retryable(self):
        """出口 IP 被 CF 拒 —— 换个 IP 就可能通，必须重试。"""
        self.assertIn("cf_blocked", register_batch.RETRYABLE_CATEGORIES)

    def test_network_is_retryable(self):
        """网络/环境抽风 —— 重试有意义。"""
        self.assertIn("network", register_batch.RETRYABLE_CATEGORIES)

    def test_unknown_is_retryable(self):
        """兜底分类 —— 保守起见重试（不重试等于直接放弃）。"""
        self.assertIn("unknown", register_batch.RETRYABLE_CATEGORIES)

    def test_account_is_not_retryable(self):
        """号/邮箱本身废了（user_already_exists 等）—— 重试同一号毫无意义。"""
        self.assertNotIn("account", register_batch.RETRYABLE_CATEGORIES)
        self.assertIn("account", register_batch.NON_RETRYABLE_CATEGORIES)

    def test_mail_limited_is_not_retryable(self):
        """短效邮箱已消耗 —— 重试只会再撞一次。"""
        self.assertNotIn("mail_limited", register_batch.RETRYABLE_CATEGORIES)
        self.assertIn("mail_limited", register_batch.NON_RETRYABLE_CATEGORIES)

    def test_two_sets_cover_every_category(self):
        """两类不能有交集，也不能有遗漏 —— 漏了会走「不重试也不换号」的缝。"""
        self.assertEqual(
            register_batch.RETRYABLE_CATEGORIES & register_batch.NON_RETRYABLE_CATEGORIES,
            frozenset(),
        )
        self.assertEqual(
            register_batch.RETRYABLE_CATEGORIES | register_batch.NON_RETRYABLE_CATEGORIES,
            frozenset({"network", "account", "mail_limited", "cf_blocked", "unknown"}),
        )


class MaxRetriesParsingTests(unittest.TestCase):
    """max_retries 的解析。"""

    def _start(self, options: dict) -> register_batch.AutoLoopController:
        """起一个 controller（不真的跑 worker）。"""
        ctrl = register_batch.AutoLoopController()
        # ⚠️ start() 会真的拉起 manage 线程去跑 worker ——
        #    测试里必须把它掐掉，否则会打到真实数据库。
        with mock.patch.object(register_batch.AutoLoopController, "_manage_loop", return_value=None), \
             mock.patch.object(register_batch, "db"), \
             mock.patch.object(register_batch, "_parse_proxy_pool", return_value=[]):
            ctrl.start(options)
        return ctrl

    def test_default_is_three(self):
        """默认重试 3 次（= 首次 + 3 次重试，共 4 次尝试）。"""
        ctrl = self._start({"concurrency": 1, "target_count": 0, "cool_down_seconds": 0})
        self.assertEqual(ctrl._max_retries, 3)

    def test_zero_means_no_retry(self):
        """⚠️ 0 = 不重试。不能被 `or 3` 吃掉。"""
        ctrl = self._start(
            {"concurrency": 1, "target_count": 0, "cool_down_seconds": 0, "max_retries": 0}
        )
        self.assertEqual(ctrl._max_retries, 0)

    def test_explicit_value_is_kept(self):
        ctrl = self._start(
            {"concurrency": 1, "target_count": 0, "cool_down_seconds": 0, "max_retries": 5}
        )
        self.assertEqual(ctrl._max_retries, 5)

    def test_value_is_clamped(self):
        """上限 20 —— 防止手滑填个 999999 变成无限重试。"""
        ctrl = self._start(
            {"concurrency": 1, "target_count": 0, "cool_down_seconds": 0, "max_retries": 99999}
        )
        self.assertEqual(ctrl._max_retries, 20)


class WorkerRetryLoopTests(unittest.TestCase):
    """worker 循环里的重试行为。"""

    def _drive_worker(self, *, max_retries=3, fail_count=99, category="cf_blocked"):
        """驱动一个 worker 跑完，返回实际执行的 run 次数与结束原因。

        fail_count = 允许连续失败的次数上限；到达后模拟外部停止信号。
        """
        ctrl = register_batch.AutoLoopController()
        ctrl._options = {
            "max_retries": max_retries,
            "cool_down_seconds": 0.01,
            "target_count": 0,
            "batch_id": "batch-retry-test",
            "mail_source": "remail",
        }
        ctrl._max_retries = max_retries

        runs: list[str] = []

        def fake_start(account, options):
            run_id = f"run-{len(runs) + 1}"
            runs.append(run_id)
            return run_id

        def fake_wait(run_id, timeout=1800):
            # 连续失败到 fail_count 次就模拟「外部叫停」，
            # 免得测试里无限循环。
            if len(runs) >= fail_count:
                ctrl._stop_event.set()
            return False, category

        provider = mock.MagicMock()
        provider.pooled = False

        with mock.patch.object(register_batch, "get_provider_class", return_value=provider), \
             mock.patch.object(register_batch.db, "get_setting", return_value="remail"), \
             mock.patch.object(register_batch.registrar, "start_registration", side_effect=fake_start), \
             mock.patch.object(ctrl, "_wait_run_finish", side_effect=fake_wait), \
             mock.patch.object(ctrl, "_proxy_for_worker", return_value=""), \
             mock.patch.object(ctrl, "_forward_run_logs", return_value=None), \
             mock.patch.object(ctrl, "_import_to_pool", return_value=None), \
             mock.patch.object(ctrl, "_record_finish") as record_finish:
            ctrl._worker_loop_inner(0, "batch-retry-test")

        return runs, record_finish

    def test_retryable_failure_triggers_retries(self):
        """cf_blocked 连续失败 → 重试到上限才停。"""
        runs, record_finish = self._drive_worker(max_retries=3, fail_count=99, category="cf_blocked")

        # 首次 + 3 次重试 = 4 次尝试
        self.assertEqual(len(runs), 4)
        # 每次失败都要结算进统计
        self.assertEqual(record_finish.call_count, 4)
        for call in record_finish.call_args_list:
            self.assertFalse(call.args[0])
            self.assertEqual(call.args[1], "cf_blocked")

    def test_retry_exhaustion_stops_the_worker(self):
        """重试用尽 → worker 真的停下来（而不是无限领号空转）。"""
        runs, _ = self._drive_worker(max_retries=2, fail_count=99, category="cf_blocked")
        self.assertEqual(len(runs), 3)  # 首次 + 2 次重试

    def test_zero_retries_runs_once(self):
        """max_retries=0 → 失败一次就停。"""
        runs, _ = self._drive_worker(max_retries=0, fail_count=99, category="cf_blocked")
        self.assertEqual(len(runs), 1)

    def test_non_retryable_failure_does_not_consume_retries(self):
        """account 类失败 → 不算重试消耗，直接换号继续。"""
        runs, record_finish = self._drive_worker(max_retries=1, fail_count=5, category="account")
        # 1 次重试额度 + 首次 = 最多 2 次；account 失败不消耗额度，
        # 所以一直跑到外部叫停（fail_count=5）
        self.assertEqual(len(runs), 5)

    def test_success_resets_the_retry_counter(self):
        """成功一次 → 重试计数清零，后面还能再重试满额。"""
        ctrl = register_batch.AutoLoopController()
        ctrl._options = {
            "max_retries": 2,
            "cool_down_seconds": 0.01,
            "target_count": 0,
            "batch_id": "batch-reset-test",
            "mail_source": "remail",
        }
        ctrl._max_retries = 2

        runs: list[str] = []
        # 失败, 失败, 成功, 失败, 失败, 失败 → 第 3 次成功应清零计数，
        # 所以第 4、5 次失败不终止，第 6 次失败时计数才到 3 > 2 → 停。
        outcomes = [False, False, True, False, False, False]

        def fake_start(account, options):
            run_id = f"run-{len(runs) + 1}"
            runs.append(run_id)
            return run_id

        def fake_wait(run_id, timeout=1800):
            idx = len(runs) - 1
            ok = outcomes[idx] if idx < len(outcomes) else False
            return ok, "" if ok else "cf_blocked"

        provider = mock.MagicMock()
        provider.pooled = False

        with mock.patch.object(register_batch, "get_provider_class", return_value=provider), \
             mock.patch.object(register_batch.db, "get_setting", return_value="remail"), \
             mock.patch.object(register_batch.registrar, "start_registration", side_effect=fake_start), \
             mock.patch.object(ctrl, "_wait_run_finish", side_effect=fake_wait), \
             mock.patch.object(ctrl, "_proxy_for_worker", return_value=""), \
             mock.patch.object(ctrl, "_forward_run_logs", return_value=None), \
             mock.patch.object(ctrl, "_import_to_pool", return_value=None), \
             mock.patch.object(ctrl, "_record_finish"):
            ctrl._worker_loop_inner(0, "batch-reset-test")

        # 走到第 6 次失败时计数超限 → 停
        self.assertEqual(len(runs), 6)


class ProxyWaitTests(unittest.TestCase):
    """代理池暂时拿不到代理时，要等，而不是立刻收工。

    ⚠️ 用户实测现象：「成功 1 / 失败 0，然后就停了」。
       根因就是 pick_proxy() 选中即预占（used_count+1），并发 worker 数超过
       代理数时，后面的 worker 暂时拿不到代理 —— 之前直接 return，
       整个批次被停掉且**失败数不记账**。
    """

    def _controller(self):
        ctrl = register_batch.AutoLoopController()
        ctrl._proxy_wait_secs = 5
        ctrl._stop_event = __import__("threading").Event()
        ctrl._pause_event = __import__("threading").Event()
        ctrl._lock = __import__("threading").RLock()
        ctrl._options = {"batch_id": "b1"}
        ctrl._state = register_batch.AutoLoopState.RUNNING
        ctrl._last_message = ""
        ctrl._subscribers = []
        return ctrl

    def test_returns_proxy_once_it_becomes_available(self):
        ctrl = self._controller()
        calls = {"n": 0}

        def fake_pick(worker_id):
            calls["n"] += 1
            return "http://p:1" if calls["n"] >= 2 else ""

        with mock.patch.object(ctrl, "_proxy_for_worker", side_effect=fake_pick):
            proxy = ctrl._wait_for_proxy(0)
        self.assertEqual(proxy, "http://p:1")

    def test_returns_empty_after_waiting(self):
        ctrl = self._controller()
        with mock.patch.object(ctrl, "_proxy_for_worker", return_value=""):
            proxy = ctrl._wait_for_proxy(0)
        self.assertEqual(proxy, "")

    def test_stops_immediately_when_stop_event_set(self):
        ctrl = self._controller()
        ctrl._stop_event.set()
        with mock.patch.object(ctrl, "_proxy_for_worker", return_value=""):
            proxy = ctrl._wait_for_proxy(0)
        self.assertEqual(proxy, "")


class StoppedCategoryTests(unittest.TestCase):
    """被停止的 run 不该算失败、更不该触发重试。"""

    def test_wait_run_finish_returns_stopped_sentinel(self):
        ctrl = register_batch.AutoLoopController()
        ctrl._stop_event = __import__("threading").Event()
        ctrl._stop_event.set()
        ok, category = ctrl._wait_run_finish("run-x", timeout=1)
        self.assertFalse(ok)
        self.assertEqual(category, register_batch.STOPPED_CATEGORY)


if __name__ == "__main__":
    unittest.main()

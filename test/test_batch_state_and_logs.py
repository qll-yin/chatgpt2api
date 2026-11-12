"""批量注册的三个硬 bug 回归测试。

覆盖：
  1. finish_run 的签名 —— 迁移时把 status 字符串改成 bool，调用点没跟着改，
     导致 TypeError → run 永远停在 running → worker 死等 1800s →
     前端永远显示「运行中」。
  2. batch SSE 必须能收到 run 的详细日志（原来两边没接上，
     实时日志只有一个「开始」就没了）。
  3. 状态收敛 —— worker 都退出后必须立刻变成 stopped，
     elapsed 必须冻结（不能一直累加）。
"""
from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
import unittest

# ⚠️ 必须**强制**覆盖，不能用 setdefault ——
#    环境里若已有 DATABASE_URL，setdefault 不会生效，
#    测试就会连到生产库（数据被污染，还会让断言不稳定）。
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/t.db"
os.environ["STORAGE_BACKEND"] = "sqlite"
os.environ["CHATGPT2API_AUTH_KEY"] = "test-key-1234567890"

from services import register_batch as rb  # noqa: E402
from services import register_registrar as registrar  # noqa: E402
from services.storage import register_store as db  # noqa: E402


def _instant_fail_registrar():
    """返回一个「立刻失败」的 registrar.start_registration。

    ⚠️ 不能返回假 run_id（如 "NEVER"）—— worker 会进
    _wait_run_finish 轮询那个不存在的 run，等满 1800 秒才放弃，
    worker 永远不退出 → 后续用例全被污染。
    """
    from services.storage import register_store as _db

    def _start(account, options):
        run_id = f"FAKE-{time.time_ns()}"
        _db.create_run(
            run_id, account.get("email", "x@y.com"), "", options.get("batch_id", "")
        )

        def _fail():
            time.sleep(0.05)
            _db.finish_run(run_id, "failed", "boom", category="account")

        threading.Thread(target=_fail, daemon=True).start()
        return run_id

    return _start


class FinishRunSignatureTests(unittest.TestCase):
    """finish_run 的签名必须与调用方一致（这是迁移时的真实事故）。"""

    def test_status_string_is_recorded(self):
        db.create_run("SIG-1", "a@b.com")
        # 与 registrar.py:643 完全一致的调用方式
        db.finish_run("SIG-1", "failed", "409 invalid_state", category="account")
        row = db.get_run("SIG-1")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_category"], "account")
        self.assertIsNotNone(row["finished_at"])

    def test_done_status_is_recorded(self):
        db.create_run("SIG-2", "c@d.com")
        db.finish_run("SIG-2", "done")
        self.assertEqual(db.get_run("SIG-2")["status"], "done")

    def test_failed_is_not_recorded_as_done(self):
        """回归：ok 是 bool 位时，"failed"（truthy）会被记成成功。"""
        db.create_run("SIG-3", "e@f.com")
        db.finish_run("SIG-3", "failed", "boom")
        self.assertNotEqual(db.get_run("SIG-3")["status"], "done")

    def test_category_kwarg_accepted(self):
        """category= 是调用方实际用的关键字名，改名就会 TypeError。"""
        db.create_run("SIG-4", "g@h.com")
        db.finish_run("SIG-4", "failed", "x", category="network")  # 不抛异常
        self.assertEqual(db.get_run("SIG-4")["error_category"], "network")


class BatchLogForwardTests(unittest.TestCase):
    """batch SSE 必须能收到 run 的详细日志行。"""

    def setUp(self):
        self.ctrl = rb.CONTROLLER
        self.q: queue.Queue = queue.Queue()
        with self.ctrl._lock:
            self.ctrl._subscribers.append(self.q)
        self.run_id = f"LOG-{id(self)}"
        db.create_run(self.run_id, "demo@outlook.com", "", "B1")
        self.rq: queue.Queue = queue.Queue()
        registrar._run_queues[self.run_id] = self.rq
        self.ctrl._forward_run_logs(self.run_id, 0, "demo@outlook.com")
        time.sleep(0.3)

    def tearDown(self):
        with self.ctrl._lock:
            try:
                self.ctrl._subscribers.remove(self.q)
            except ValueError:
                pass
        registrar._run_queues.pop(self.run_id, None)

    def _collect(self, n: int, timeout: float = 5.0) -> list[dict]:
        out: list[dict] = []
        deadline = time.time() + timeout
        while time.time() < deadline and len(out) < n:
            try:
                out.append(self.q.get(timeout=0.5))
            except queue.Empty:
                break
        return out

    def test_detailed_log_line_is_broadcast(self):
        self.rq.put("16:13:02 [ERROR] services.register.auth_flow: authorize/continue 非 200")
        events = self._collect(1)
        self.assertTrue(events, "没收到任何事件")
        data = events[0].get("data") or {}
        self.assertEqual(events[0].get("kind"), "log")
        self.assertIn("authorize/continue", data.get("message", ""))
        self.assertEqual(data.get("email"), "demo@outlook.com")

    def test_structured_event_uses_run_event_kind(self):
        """结构化事件必须用 run_event —— 不能把 registrar 内部的 kind
        （error/progress）直接当 batch 事件类型，否则前端匹配不上。"""
        self.rq.put('__EVENT__:' + '{"kind": "error", "message": "boom"}')
        events = self._collect(1)
        self.assertTrue(events)
        self.assertEqual(events[0].get("kind"), "run_event")
        self.assertEqual((events[0].get("data") or {}).get("kind"), "error")

    def test_sentinel_ends_the_forwarder(self):
        self.rq.put(None)
        time.sleep(0.5)
        # sentinel 之后线程结束，不应再有新事件
        self.rq.put("after-sentinel")
        time.sleep(0.5)
        events = self._collect(1, timeout=1.0)
        self.assertFalse(events, "sentinel 之后不该再收到事件")


class StateConvergeTests(unittest.TestCase):
    """worker 都退出后，状态必须立刻收敛，elapsed 必须冻结。"""

    def setUp(self):
        self.ctrl = rb.CONTROLLER
        self._orig_start = rb.registrar.start_registration
        # ⚠️ 测试之间必须完全隔离：上一个用例残留的 worker 线程还在跑，
        #    会让 start() 被拒（"已经在跑了"），断言就全乱了。
        self._drain()

    def tearDown(self):
        rb.registrar.start_registration = self._orig_start
        self._drain()

    def _drain(self):
        """停掉并等 worker 完全退出（只等 state 不够，worker 是异步退出的）。"""
        # ⚠️ 先把 registrar mock 成「立刻失败」，否则 worker 会卡在
        #    _wait_run_finish 里等一个真跑几分钟的注册。
        rb.registrar.start_registration = lambda account, options: "NEVER"
        self.ctrl.stop()
        deadline = time.time() + 20
        while time.time() < deadline:
            if not self.ctrl._alive_workers and self.ctrl.status()["state"] == "stopped":
                break
            time.sleep(0.2)
        time.sleep(0.3)

    def _patch_registrar(self, fn):
        rb.registrar.start_registration = fn

    def test_elapsed_freezes_when_stopped(self):
        """跑完之后 elapsed 不能继续涨。"""
        self.ctrl.start({"concurrency": 1, "target_count": 0, "cool_down_seconds": 0})
        time.sleep(0.8)
        self.ctrl.stop()
        # 等状态收敛（finished_at 落定，elapsed 才真正冻结）
        deadline = time.time() + 15
        while time.time() < deadline:
            st = self.ctrl.status()
            if st["state"] == "stopped" and st["finished_at"]:
                break
            time.sleep(0.2)
        self.assertEqual(self.ctrl.status()["state"], "stopped")
        self.assertTrue(self.ctrl.status()["finished_at"], "finished_at 没落定")
        # ⚠️ 取两次快照，间隔 1.2s —— 收敛后 elapsed 必须**完全不变**。
        #    （第一份快照可能正好赶上 _settle_if_all_workers_done 覆写
        #      _finished_at 的瞬间，所以要多等一拍再比）
        time.sleep(0.5)
        e1 = self.ctrl.status()["elapsed"]
        time.sleep(1.2)
        e2 = self.ctrl.status()["elapsed"]
        self.assertAlmostEqual(e1, e2, delta=0.1, msg="elapsed 还在累加")

    def test_state_converges_when_all_workers_exit(self):
        """所有 worker 都退出后状态应变成 stopped（而不是一直 running）。"""

        def fake_start(account, options):
            run_id = f"CONV-{id(self)}"
            db.create_run(run_id, account.get("email", "x@y.com"), "", "B1")

            def _fail():
                time.sleep(0.2)
                db.finish_run(run_id, "failed", "boom", category="account")

            threading.Thread(target=_fail, daemon=True).start()
            return run_id

        self._patch_registrar(fake_start)
        # 用非池化来源（避免号池为空导致 30s 空转）
        self.ctrl.start({
            "concurrency": 1,
            "target_count": 0,
            "cool_down_seconds": 0,
            "mail_source": "remail",
            "auto_import": False,
        })
        deadline = time.time() + 30
        while time.time() < deadline:
            st = self.ctrl.status()
            if st["state"] in ("stopped", "waiting"):
                break
            time.sleep(0.3)
        self.assertIn(
            self.ctrl.status()["state"],
            ("stopped", "waiting"),
            "状态没有收敛",
        )


class WaitingStateTests(unittest.TestCase):
    """号池空时的「等待资源」状态。"""

    def setUp(self):
        self.ctrl = rb.CONTROLLER
        self._orig_start = rb.registrar.start_registration
        # ⚠️ 必须 mock 掉注册 —— 否则 worker 会真的去跑注册，
        #    stop() 是「优雅停止」（等当前 run 跑完），
        #    worker 可能要等好几分钟才退出，测试会卡死。
        rb.registrar.start_registration = _instant_fail_registrar()
        # ⚠️ 清空代理池 —— 别的前面用例可能留下「已停用」的代理，
        #    那样 worker 会走「代理池无可用代理」分支直接退出，
        #    状态变成 stopped 而不是 waiting，断言就废了。
        try:
            db.clear_proxies()
        except Exception:  # noqa: BLE001
            pass
        self._drain()

    def tearDown(self):
        rb.registrar.start_registration = self._orig_start
        self.ctrl.stop()
        self._drain()

    def _drain(self):
        """停掉并等 worker **完全**退出。

        ⚠️ 只等 state 变成 stopped 不够 —— worker 是异步退出的，
        state 先变、worker 后退。下一个用例的 start() 会撞上
        「上一批 worker 还没退出」而被拒，断言全乱。
        """
        self.ctrl.stop()
        deadline = time.time() + 20
        while time.time() < deadline:
            if not self.ctrl._alive_workers and self.ctrl.status()["state"] == "stopped":
                break
            time.sleep(0.2)
        time.sleep(0.3)

    def test_empty_pool_enters_waiting(self):
        """号池空 → waiting（不是 running）。"""
        self.ctrl.start({"concurrency": 1, "target_count": 0, "cool_down_seconds": 0})
        deadline = time.time() + 10
        state = None
        while time.time() < deadline:
            state = self.ctrl.status()["state"]
            if state in ("waiting", "stopped"):
                break
            time.sleep(0.2)
        self.assertEqual(
            state,
            "waiting",
            f"号池空应该是「等待资源」，不是「运行中」；"
            f"实际 state={state} alive={len(self.ctrl._alive_workers)} "
            f"msg={self.ctrl.status().get('last_message')!r}",
        )

    def test_stop_works_from_waiting(self):
        """waiting 状态下点停止要立刻生效。"""
        self.ctrl.start({"concurrency": 1, "target_count": 0, "cool_down_seconds": 0})
        deadline = time.time() + 10
        while time.time() < deadline and self.ctrl.status()["state"] != "waiting":
            time.sleep(0.2)
        self.ctrl.stop()
        time.sleep(0.5)
        self.assertEqual(self.ctrl.status()["state"], "stopped")

    def test_start_twice_is_rejected_while_waiting(self):
        """waiting 时不能重复 start（否则会起两批 worker 抢同一个号池）。"""
        self.ctrl.start({"concurrency": 1, "target_count": 0, "cool_down_seconds": 0})
        deadline = time.time() + 10
        while time.time() < deadline and self.ctrl.status()["state"] != "waiting":
            time.sleep(0.2)
        second = self.ctrl.start({"concurrency": 1})
        self.assertFalse(second["ok"], "waiting 状态下不该允许再次 start")


class FingerprintSupportTests(unittest.TestCase):
    """指纹只能产出当前 curl_cffi 支持的 impersonate 名。

    ⚠️ 不支持的 impersonate 名会让每个请求抛
    「Impersonating xxx is not supported」→ warmup 拿不到 oai-did
    → authorize/continue 必然 409 invalid_state。
    错误信息完全看不出真因，必须在源头拦住。
    """

    def test_generated_fingerprints_are_supported(self):
        import random

        from services.register.fingerprint import (
            generate_fingerprint,
            is_supported_impersonate,
        )

        for i in range(200):
            fp = generate_fingerprint(random.Random(i))
            self.assertTrue(
                is_supported_impersonate(fp["impersonate"]),
                f"第 {i} 次生成了不支持的 impersonate: {fp['impersonate']}",
            )
            for fb in fp.get("fallback_impersonates") or []:
                self.assertTrue(
                    is_supported_impersonate(fb),
                    f"第 {i} 次的 fallback 不支持: {fb}",
                )

    def test_unsupported_name_is_detected(self):
        from services.register.fingerprint import is_supported_impersonate

        self.assertFalse(is_supported_impersonate("chrome9999_not_real"))


class InvalidStateProxyTests(unittest.TestCase):
    """409 invalid_state 的代理级处置。"""

    def test_static_proxy_auto_disabled_after_threshold(self):
        db.replace_proxies(["http://is-1:1"], mode="replace")
        db.update_proxy("http://is-1:1", proxy_type="static")
        r1 = db.mark_proxy_invalid_state("http://is-1:1")
        self.assertFalse(r1["disabled"])
        r2 = db.mark_proxy_invalid_state("http://is-1:1")
        self.assertTrue(r2["disabled"], "静态代理连续失效应自动停用")
        self.assertFalse(db.find_proxy("http://is-1:1")["enabled"])

    def test_dynamic_proxy_not_disabled(self):
        db.replace_proxies(["http://is-2:2"], mode="replace")
        db.update_proxy("http://is-2:2", proxy_type="dynamic")
        for _ in range(3):
            db.mark_proxy_invalid_state("http://is-2:2")
        self.assertTrue(
            db.find_proxy("http://is-2:2")["enabled"],
            "动态代理换 IP 可能就通，不该停用",
        )

    def test_reset_clears_counter(self):
        db.replace_proxies(["http://is-3:3"], mode="replace")
        db.mark_proxy_invalid_state("http://is-3:3")
        self.assertTrue(db.reset_proxy_invalid_state("http://is-3:3"))
        self.assertEqual(db.find_proxy("http://is-3:3")["invalid_state"], 0)


if __name__ == "__main__":
    unittest.main()

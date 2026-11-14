"""批量注册控制器与注册服务的测试。

重点验证（不触网，全部用 mock）：
- 批量控制器的状态机：start / pause / resume / stop
- 达标自动停止（target_count）
- 熔断只对 network 类错误生效，cf_blocked 不触发
- 代理预占与失败归还
- 邮箱状态回写：network → release_unused（复用），其它 → mark_failed
- registrar 依赖的 db 函数在 register_store 里都存在（防迁移遗漏）
"""

from __future__ import annotations

import ast
import io
import unittest
from pathlib import Path
from unittest import mock

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-batch-")

from services import register_batch, register_registrar, register_service  # noqa: E402
from services.storage import register_store as store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class MigrationCompletenessTests(unittest.TestCase):
    """迁移完整性：迁移过来的文件不能再有未解析的相对导入。"""

    def test_no_relative_imports_left(self):
        for name in ("services/register_registrar.py", "services/register_batch.py"):
            with io.open(ROOT / name, encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level and node.level > 0:
                    # 相对导入（from . import xxx）必须为 0 —— 迁移后不该再有
                    self.fail(f"{name} 仍有相对导入: from {'.' * node.level} import {node.module}")

    def test_all_db_functions_exist(self):
        """迁移过来的代码用到的 db.* 函数必须在 register_store 里存在。"""
        available = {n for n in dir(store) if not n.startswith("_")}
        for name in ("services/register_registrar.py", "services/register_batch.py"):
            with io.open(ROOT / name, encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src)
            used = {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "db"
            }
            missing = sorted(used - available)
            self.assertFalse(missing, f"{name} 缺少 db 函数: {missing}")

    def test_registrar_module_loads(self):
        self.assertTrue(hasattr(register_registrar, "start_registration"))
        self.assertTrue(hasattr(register_registrar, "get_run_queue"))
        self.assertTrue(hasattr(register_batch, "CONTROLLER"))


class BatchControllerTests(unittest.TestCase):
    """批量控制器状态机。"""

    def setUp(self):
        self.controller = register_batch.AutoLoopController()

    def test_start_returns_ok(self):
        result = self.controller.start({"concurrency": 1, "target_count": 0})
        # 没有邮箱可跑，worker 会因号池空退出，最终 state=stopped
        self.assertTrue(result["ok"])
        self.assertEqual(result["concurrency"], 1)
        self.controller.stop()

    def test_start_twice_is_rejected(self):
        self.controller.start({"concurrency": 1})
        second = self.controller.start({"concurrency": 1})
        self.assertFalse(second["ok"])
        self.controller.stop()

    def test_concurrency_clamped(self):
        result = self.controller.start({"concurrency": 99})
        # 上限 20
        self.assertEqual(result["concurrency"], 20)
        self.controller.stop()

    def test_concurrency_minimum_one(self):
        result = self.controller.start({"concurrency": 0})
        self.assertEqual(result["concurrency"], 1)
        self.controller.stop()

    def test_pause_and_resume(self):
        # ⚠️ 显式置成 RUNNING 再测 —— 不能依赖 start() 之后 worker 还没
        #    进入「等待资源」的那个时序窗口（那是竞态，时快时慢）。
        #    等资源状态下 pause() 返回 ok=False 是**正确**行为
        #    （「当前在等资源，没在跑，不用暂停」）。
        self.controller._state = register_batch.AutoLoopState.RUNNING
        paused = self.controller.pause()
        self.assertTrue(paused["ok"])
        self.assertEqual(self.controller.status()["state"], "paused")
        resumed = self.controller.resume()
        self.assertTrue(resumed["ok"])
        self.controller.stop()

    def test_stop_when_idle(self):
        result = self.controller.stop()
        self.assertFalse(result["ok"])

    def test_snapshot_fields(self):
        self.controller.start({"concurrency": 2, "target_count": 5})
        snap = self.controller.status()
        for key in ("state", "elapsed", "registered_ok", "registered_fail",
                    "target_count", "remaining", "concurrency", "workers", "last_message"):
            self.assertIn(key, snap)
        self.assertEqual(snap["target_count"], 5)
        self.assertEqual(snap["remaining"], 5)
        self.controller.stop()


class CircuitBreakTests(unittest.TestCase):
    """熔断逻辑：只有 network 类错误才计入。"""

    def setUp(self):
        self.controller = register_batch.AutoLoopController()
        self.controller._state = register_batch.AutoLoopState.RUNNING

    def test_network_errors_trigger_break(self):
        for _ in range(3):
            self.controller._record_finish(False, "network")
        self.assertEqual(self.controller.status()["state"], "paused")

    def test_cf_blocked_does_not_trigger_break(self):
        for _ in range(5):
            self.controller._record_finish(False, "cf_blocked")
        # cf_blocked 不是环境问题，不该暂停整个批次
        self.assertEqual(self.controller.status()["state"], "running")

    def test_success_resets_counter(self):
        self.controller._record_finish(False, "network")
        self.controller._record_finish(False, "network")
        self.controller._record_finish(True, "")
        self.controller._record_finish(False, "network")
        # 成功打断了连续计数，不该熔断
        self.assertEqual(self.controller.status()["state"], "running")

    def test_target_reached_triggers_stop(self):
        self.controller._target_count = 2
        self.controller._record_finish(True, "")
        self.controller._record_finish(True, "")
        self.assertTrue(self.controller._stop_event.is_set())
        self.assertEqual(self.controller.status()["registered_ok"], 2)


class ProxyQuotaTests(unittest.TestCase):
    """代理预占与失败归还（迁移后的语义必须保持）。"""

    def setUp(self):
        # replace 模式**保留**已有代理的用量（原注册机语义），
        # 所以这里先清空池子，保证每个用例从零开始。
        store.replace_proxies([], mode="replace")
        store.replace_proxies(["http://px1:1"], mode="replace")

    def tearDown(self):
        store.replace_proxies([], mode="replace")

    def test_pick_reserves(self):
        proxy = store.pick_proxy()
        self.assertEqual(proxy, "http://px1:1")
        self.assertEqual(store.find_proxy(proxy)["used_count"], 1)

    def test_failure_returns_quota(self):
        proxy = store.pick_proxy()
        store.proxy_release(proxy)
        store.proxy_used(proxy, ok=False)
        record = store.find_proxy(proxy)
        self.assertEqual(record["used_count"], 0)
        self.assertEqual(record["failed_count"], 1)

    def test_success_keeps_quota(self):
        proxy = store.pick_proxy()
        store.proxy_used(proxy, ok=True)
        self.assertEqual(store.find_proxy(proxy)["used_count"], 2)

    def test_status_derivation(self):
        store.update_proxy("http://px1:1", enabled=False)
        self.assertEqual(store.proxy_pool_stats()["disabled"], 1)
        store.update_proxy("http://px1:1", enabled=True, max_uses=1)
        store.pick_proxy()
        self.assertEqual(store.proxy_pool_stats()["exhausted"], 1)


class ReplaceProxiesTests(unittest.TestCase):
    """replace 语义：只删不在新列表里的，已有的保留用量。"""

    def tearDown(self):
        store.replace_proxies([], mode="replace")

    def test_replace_keeps_existing_usage(self):
        store.replace_proxies(["http://a:1", "http://b:2"], mode="replace")
        store.pick_proxy()
        used_before = {i["proxy"]: i["used_count"] for i in store.list_proxies()}

        # 重新导入同样的列表 —— 用量应当保留
        store.replace_proxies(["http://a:1", "http://b:2"], mode="replace")
        used_after = {i["proxy"]: i["used_count"] for i in store.list_proxies()}
        self.assertEqual(used_before, used_after)

    def test_replace_removes_missing(self):
        store.replace_proxies(["http://a:1", "http://b:2"], mode="replace")
        store.replace_proxies(["http://a:1"], mode="replace")
        proxies = [i["proxy"] for i in store.list_proxies()]
        self.assertEqual(proxies, ["http://a:1"])

    def test_append_keeps_all(self):
        store.replace_proxies(["http://a:1"], mode="replace")
        store.replace_proxies(["http://b:2"], mode="append")
        proxies = sorted(i["proxy"] for i in store.list_proxies())
        self.assertEqual(proxies, ["http://a:1", "http://b:2"])

    def test_dedup(self):
        result = store.replace_proxies(["http://a:1", "http://a:1"], mode="replace")
        self.assertEqual(result["total"], 1)


class RegisterStoreCompatTests(unittest.TestCase):
    """register_store 与原注册机 db 层的接口兼容。"""

    def test_save_registered_accepts_dict(self):
        store.save_registered({
            "email": "compat@x.com",
            "access_token": "AT",
            "refresh_token": "RT",
            "proxy": "http://p:1",
        })
        record = store.get_registered("compat@x.com")
        self.assertEqual(record["access_token"], "AT")
        self.assertEqual(record["refresh_token"], "RT")
        self.assertEqual(record["proxy"], "http://p:1")
        # extra_json 应被 pop 掉（与原实现一致）
        self.assertNotIn("extra_json", record)

    def test_save_registered_accepts_kwargs(self):
        store.save_registered(email="kw@x.com", access_token="AT2")
        self.assertEqual(store.get_registered("kw@x.com")["access_token"], "AT2")

    def test_mail_settings_contains_provider_fields(self):
        settings = store.get_mail_settings()
        self.assertIn("mail_source", settings)
        # Remail 的 api key 应该在（值为空也算有这个键）
        self.assertIn("remail_api_key", settings)

    def test_sms_internal_config_shape(self):
        cfg = store.get_sms_internal_config()
        for key in ("sms_enabled", "sms_provider", "sms_api_key", "sms_country"):
            self.assertIn(key, cfg)

    def test_export_config_disabled_by_default(self):
        cfg = store.get_export_internal_config()
        self.assertFalse(cfg["cpa"]["enabled"])
        self.assertFalse(cfg["sub2api"]["enabled"])

    def test_stats_shape(self):
        stats = store.stats()
        for key in ("available", "in_use", "done", "failed", "total"):
            self.assertIn(key, stats)


class ClassifyErrorTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(register_service.classify_error("HTTP 409 invalid_state"), "account")
        self.assertEqual(register_service.classify_error("Cloudflare 403 blocked"), "cf_blocked")
        self.assertEqual(register_service.classify_error("OTP timeout waiting"), "mail_limited")
        self.assertEqual(register_service.classify_error("connection reset by peer"), "network")
        self.assertEqual(register_service.classify_error("some random issue"), "unknown")


if __name__ == "__main__":
    unittest.main()

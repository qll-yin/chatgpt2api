"""注册 → 号池（chatgpt2api 号池管理）同步链路的回归测试。

覆盖用户实测报出的两个问题：

1. **批量注册必须跟旧注册机一致**
   - registrar 必须用「本次批量选的」邮箱来源，不能只看全局配置
     （worker 按 remail claim、registrar 却按 outlook 取邮箱 → 必然失败）
   - 注册成功后运行记录要留下**真实**邮箱

2. **注册成功后自动导入号池 + 入池携带代理**
   - 非池化来源（Remail / CF 临时邮箱）worker 手里只有占位邮箱
     （xxx_placeholder_N@placeholder.local），用它查凭证永远查不到
     → 号注册出来了却**没进号池**。必须用运行记录里的真实邮箱。
   - 入池目标是 chatgpt2api 的「号池管理」（account_service），
     不是注册管理页的「注册结果」表。
   - 入池要带上注册时用的那个代理（注册和使用同一个出口 IP）。
"""

from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from test.db_fixture import use_temp_database

use_temp_database("chatgpt2api-test-pool-sync-")

from services import register_batch, register_registrar, register_service  # noqa: E402
from services.storage import register_store as store  # noqa: E402


def _clear_pool() -> None:
    from services.account_service import account_service

    existing = [item.get("access_token") for item in account_service.list_accounts()]
    tokens = [t for t in existing if t]
    if tokens:
        account_service.delete_accounts(tokens)


class FinishRunEmailTests(unittest.TestCase):
    """运行记录里的邮箱 —— 必须是注册成功后的真实邮箱。"""

    def test_finish_run_records_real_email(self):
        store.create_run("run-mail-1", "remail_placeholder_1@placeholder.local", "", "b1")
        store.finish_run("run-mail-1", "done", email="real@remail.local")
        row = store.get_run("run-mail-1")
        self.assertEqual(row["email"], "real@remail.local")
        self.assertEqual(row["status"], "done")

    def test_finish_run_keeps_email_when_not_given(self):
        """失败时拿不到真实邮箱 —— 不能把占位邮箱清掉。"""
        store.create_run("run-mail-2", "remail_placeholder_2@placeholder.local", "", "b1")
        store.finish_run("run-mail-2", "failed", "boom", category="network")
        row = store.get_run("run-mail-2")
        self.assertEqual(row["email"], "remail_placeholder_2@placeholder.local")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_category"], "network")

    def test_finish_run_overwrites_email_when_given(self):
        store.create_run("run-mail-3", "remail_placeholder_3@placeholder.local", "", "b1")
        store.finish_run("run-mail-3", "done", email="real3@remail.local")
        self.assertEqual(store.get_run("run-mail-3")["email"], "real3@remail.local")


class RegistrarMailSourceTests(unittest.TestCase):
    """registrar 必须用本次批量选的邮箱来源。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "run.log"

    def tearDown(self):
        self.tmp.cleanup()

    def _run_registrar(self, options: dict, account: dict, result_email: str):
        fake_flow = mock.MagicMock()
        fake_flow.result.email = result_email
        fake_flow.result.to_dict.return_value = {
            "email": result_email,
            "password": "pw-123456",
            "access_token": "AT-REG",
            "session_token": "ST-REG",
            "refresh_token": "",
            "id_token": "",
            "cookie_header": "",
        }
        fake_flow.run_register.return_value = fake_flow.result

        provider_cls = mock.MagicMock()
        provider_cls.pooled = False

        run_id = f"run-src-{uuid.uuid4().hex[:8]}"
        store.create_run(run_id, account.get("email", ""), str(self.log_path))

        with mock.patch.object(
            register_registrar, "get_provider_class", return_value=provider_cls
        ), mock.patch.object(
            register_registrar, "create_mail_provider", return_value=mock.MagicMock()
        ) as create_provider, mock.patch.object(
            register_registrar, "AuthFlow", return_value=fake_flow
        ):
            register_registrar._do_register(run_id, account, options, self.log_path)

        return create_provider, run_id

    def test_batch_mail_source_wins_over_global_setting(self):
        store.set_setting("mail_source", "outlook")
        create_provider, _ = self._run_registrar(
            {"mail_source": "remail"},
            {"email": "remail_placeholder_9@placeholder.local"},
            "real@remail.local",
        )
        self.assertEqual(create_provider.call_args[0][0], "remail")

    def test_falls_back_to_global_setting(self):
        store.set_setting("mail_source", "remail")
        create_provider, _ = self._run_registrar(
            {},
            {"email": "remail_placeholder_10@placeholder.local"},
            "real2@remail.local",
        )
        self.assertEqual(create_provider.call_args[0][0], "remail")

    def test_run_record_gets_real_email(self):
        store.set_setting("mail_source", "remail")
        _, run_id = self._run_registrar(
            {"mail_source": "remail"},
            {"email": "remail_placeholder_11@placeholder.local"},
            "real3@remail.local",
        )
        row = store.get_run(run_id)
        self.assertEqual(row["email"], "real3@remail.local")
        self.assertEqual(row["status"], "done")


class BatchPoolImportTests(unittest.TestCase):
    """批量注册成功 → 写入 chatgpt2api 号池管理。"""

    def setUp(self):
        _clear_pool()
        self.controller = register_batch.AutoLoopController()
        self.controller._options = {"auto_import": True, "with_proxy": True}

    def tearDown(self):
        _clear_pool()

    def test_import_uses_real_email_from_run_record(self):
        """Remail 这类非池化来源：worker 只有占位邮箱，靠运行记录里的真实邮箱入池。"""
        store.save_registered(email="real@remail.local", access_token="AT-REAL")
        store.create_run(
            "run-import-1",
            "remail_placeholder_123@placeholder.local",
            "",
            "batch-x",
        )
        store.finish_run(
            "run-import-1", "done", email="real@remail.local"
        )

        with mock.patch.object(store, "is_static_proxy", return_value=True):
            ok = self.controller._import_to_pool(
                {"email": "remail_placeholder_123@placeholder.local"},
                "http://proxy-1:8080",
                "run-import-1",
            )
        self.assertTrue(ok)

        from services.account_service import account_service

        items = {i["email"]: i for i in account_service.list_accounts()}
        self.assertIn("real@remail.local", items)
        self.assertEqual(items["real@remail.local"]["access_token"], "AT-REAL")
        # 入池要携带注册时用的代理 —— 注册和使用同一个出口 IP
        self.assertEqual(items["real@remail.local"].get("proxy"), "http://proxy-1:8080")

    def test_import_carries_static_proxy(self):
        """静态代理随账号一起入池。"""
        store.save_registered(email="p@x.com", access_token="AT-P")
        with mock.patch.object(store, "is_static_proxy", return_value=True):
            self.controller._import_to_pool({"email": "p@x.com"}, "http://static:9", "run-x")

        from services.account_service import account_service

        items = {i["email"]: i for i in account_service.list_accounts()}
        self.assertEqual(items["p@x.com"].get("proxy"), "http://static:9")

    def test_import_drops_dynamic_proxy(self):
        """⚠️ 动态代理不进号池 —— 按空代理入池。

        用户实测：自动补充沿用注册用的动态代理，把代理服务商的流量包耗光。
        """
        store.save_registered(email="pd@x.com", access_token="AT-PD")
        with mock.patch.object(store, "is_static_proxy", return_value=False):
            self.controller._import_to_pool({"email": "pd@x.com"}, "socks5://u:p@h:9", "run-pd")

        from services.account_service import account_service

        items = {i["email"]: i for i in account_service.list_accounts()}
        self.assertEqual(items["pd@x.com"].get("proxy"), "")

    def test_import_without_proxy_when_option_off(self):
        store.save_registered(email="q@x.com", access_token="AT-Q", proxy="http://old:1")
        self.controller._options["with_proxy"] = False
        self.controller._import_to_pool({"email": "q@x.com"}, "http://new:2", "run-y")

        from services.account_service import account_service

        items = {i["email"]: i for i in account_service.list_accounts()}
        self.assertEqual(items["q@x.com"].get("proxy"), "")

    def test_import_skipped_when_auto_import_off(self):
        store.save_registered(email="r@x.com", access_token="AT-R")
        self.controller._options["auto_import"] = False
        self.assertFalse(self.controller._import_to_pool({"email": "r@x.com"}, "", "run-z"))

        from services.account_service import account_service

        tokens = [i["access_token"] for i in account_service.list_accounts()]
        self.assertNotIn("AT-R", tokens)

    def test_placeholder_email_alone_is_not_used(self):
        """只有占位邮箱、运行记录也没有真实邮箱 → 跳过（别把占位邮箱写进号池）。"""
        store.save_registered(email="s@x.com", access_token="AT-S")
        ok = self.controller._import_to_pool(
            {"email": "remail_placeholder_1@placeholder.local"}, "", ""
        )
        self.assertFalse(ok)

    def test_imported_token_tracked_for_quota_refresh(self):
        store.save_registered(email="t@x.com", access_token="AT-T")
        self.controller._import_to_pool({"email": "t@x.com"}, "http://p:1", "run-t")
        self.assertIn("AT-T", self.controller._imported_tokens)

    def test_existing_account_is_updated_not_duplicated(self):
        """重跑同一个号：号池里那条要更新代理，不能重复添加，也不能当失败。"""
        store.save_registered(email="u@x.com", access_token="AT-U")
        with mock.patch.object(store, "is_static_proxy", return_value=True):
            self.controller._import_to_pool({"email": "u@x.com"}, "http://p-old:1", "run-u1")
            ok = self.controller._import_to_pool({"email": "u@x.com"}, "http://p-new:2", "run-u2")
        self.assertTrue(ok)

        from services.account_service import account_service

        items = [i for i in account_service.list_accounts() if i["access_token"] == "AT-U"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].get("proxy"), "http://p-new:2")


class RegisterOncePoolImportTests(unittest.TestCase):
    """单次注册 → 同样要按真实邮箱入池。"""

    def setUp(self):
        _clear_pool()

    def tearDown(self):
        _clear_pool()
        store.delete_proxy("http://proxy-once:1")

    def test_register_one_imports_with_real_email(self):
        store.save_registered(email="one@remail.local", access_token="AT-ONE")
        # ⚠️ 入池只接受**登记过的静态代理**（动态/未登记一律不带入号池）。
        #    这里先把这条代理登记为静态，验证「注册用静态 → 入池沿用同一条」。
        store.replace_proxies(["http://proxy-once:1"], mode="append", proxy_type="static")

        def _fake_start_registration(account, options):
            store.create_run(
                "run-once-1",
                account.get("email", ""),
                "",
                "batch-once",
            )
            store.finish_run("run-once-1", "done", email="one@remail.local")
            return "run-once-1"

        with mock.patch.object(
            register_registrar, "start_registration", side_effect=_fake_start_registration
        ):
            result = register_service.register_one(
                {
                    "mail_source": "remail",
                    "account": {
                        "email": "remail_placeholder_77@placeholder.local",
                        "password": "",
                        "client_id": "",
                        "refresh_token": "",
                        "relay_url": "",
                        "kind": "remail",
                    },
                    "auto_import": True,
                    "with_proxy": True,
                    "proxy": "http://proxy-once:1",
                    "timeout": 30,
                }
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["imported"])

        from services.account_service import account_service

        items = {i["email"]: i for i in account_service.list_accounts()}
        self.assertIn("one@remail.local", items)
        self.assertEqual(items["one@remail.local"].get("proxy"), "http://proxy-once:1")


class BatchWorkerIntegrationTests(unittest.TestCase):
    """完整跑一轮 worker：挑代理 → 注册 → 入 chatgpt2api 号池。

    这是最贴近真实使用的一条链路：
    代理从代理池动态挑 → 注册用这个代理 → 号带着**同一个代理**进号池。
    """

    def setUp(self):
        _clear_pool()
        store.replace_proxies([], mode="replace")
        store.replace_proxies(["http://px-int:1"], mode="replace")
        # 记下原来的来源，tearDown 还原 —— 否则会把全局 mail_source 改成
        # remail，影响其它测试模块（它们以为号池是空的，却一直跑非池化来源）。
        self._orig_mail_source = store.get_setting("mail_source", "outlook")
        store.set_setting("mail_source", "remail")

    def tearDown(self):
        _clear_pool()
        try:
            store.set_setting("mail_source", self._orig_mail_source or "outlook")
        except Exception:  # noqa: BLE001
            pass

    def test_worker_registers_and_imports_to_pool(self):
        controller = register_batch.AutoLoopController()
        controller._options = {
            "auto_import": True,
            "with_proxy": True,
            "concurrency": 1,
            "cool_down_seconds": 0,
            "target_count": 1,
            "mail_source": "remail",
            "batch_id": "batch-int-1",
        }
        controller._state = register_batch.AutoLoopState.RUNNING
        controller._started_at = time.time()
        controller._backend_proxy_pool = True
        controller._concurrency = 1
        controller._target_count = 1

        real_email = "int@remail.local"
        used_proxy: list[str] = []

        def _fake_start_registration(account, options):
            run_id = f"run-int-{uuid.uuid4().hex[:8]}"
            store.create_run(run_id, account.get("email", ""), "", "batch-int-1")
            used_proxy.append(str(options.get("proxy") or ""))
            store.save_registered(
                email=real_email,
                access_token="AT-INT",
                proxy=str(options.get("proxy") or ""),
            )
            store.finish_run(run_id, "done", email=real_email)
            return run_id

        with mock.patch.object(
            register_batch.registrar,
            "start_registration",
            side_effect=_fake_start_registration,
        ):
            controller._worker_loop_inner(0, "batch-int-1")

        # worker 用的是代理池里挑出来的那个代理
        self.assertEqual(used_proxy, ["http://px-int:1"])

        # 号进了 chatgpt2api 的号池管理，且带着同一个代理
        from services.account_service import account_service

        items = {i["email"]: i for i in account_service.list_accounts()}
        self.assertIn(real_email, items)
        self.assertEqual(items[real_email]["access_token"], "AT-INT")
        self.assertEqual(items[real_email].get("proxy"), "http://px-int:1")


class ConcurrentDbAccessTests(unittest.TestCase):
    """多线程同时读写数据库 —— 结果必须正确，不能出现空行 / None 计数。

    ⚠️ 曾经的根因
    ------------
    SQLite 之前用 `StaticPool`：**全进程共用同一条连接**。
    连接是有状态的（游标 / 事务），一个线程的 `commit()` 会把另一个线程
    正在遍历的结果集作废 → 对方拿到空行：

        IndexError: tuple index out of range
        TypeError: int() argument ... not 'NoneType'

    批量注册正是「N 个 worker 线程 + FastAPI 请求线程」一起读写，
    所以这个坑会表现为批量跑着跑着状态乱掉、统计接口 500，而且是**随机**的。

    改成真正的连接池后，这条测试就是回归防线。
    """

    def test_stats_stays_correct_under_concurrent_access(self):
        import threading

        store.import_accounts("a@x.com----p\nb@x.com----p", "outlook")
        errors: list[str] = []
        totals: list[int] = []

        def _reader() -> None:
            try:
                for _ in range(40):
                    totals.append(int(store.stats()["total"]))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        def _writer() -> None:
            try:
                for i in range(40):
                    store.set_setting(f"conc-key-{i}", str(i))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=_reader) for _ in range(4)]
        threads += [threading.Thread(target=_writer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "并发读写出现异常：\n" + "\n".join(errors))
        self.assertEqual(len(totals), 160)
        self.assertTrue(all(t >= 0 for t in totals))

    def test_registered_records_are_readable_while_writing(self):
        import threading

        errors: list[str] = []
        seen: list[str] = []

        def _reader() -> None:
            try:
                for _ in range(30):
                    row = store.get_registered("conc@x.com")
                    seen.append(str((row or {}).get("access_token") or ""))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        def _writer() -> None:
            try:
                for i in range(30):
                    store.save_registered(
                        email="conc@x.com", access_token=f"AT-{i}", proxy="http://p:1"
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=_reader) for _ in range(3)]
        threads += [threading.Thread(target=_writer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "并发读写出现异常：\n" + "\n".join(errors))
        self.assertEqual(len(seen), 90)


if __name__ == "__main__":
    unittest.main()

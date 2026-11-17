"""Codex 凭证刷新与「刷新 401（重新授权）」的测试。

覆盖三条链路：

1. **client_id 分流** —— codex 的 refresh_token 只能用 Codex CLI 的 client
   刷新。OAuth grant 绑定 client_id，用 platform 的 client 刷必然
   invalid_grant → codex 账号永远刷不出来。

2. **keepalive 不抢 codex 的 RT** —— codex 的 RT 是单次使用 + 轮换制，
   keepalive 每 3 天主动刷一次等于定期把凭证从别的池手里抢走。

3. **重新授权** —— 用库里已有的密码 + TOTP 重走一次登录与 Codex 授权，
   换一份新凭证写回注册结果表。
"""
from __future__ import annotations

import unittest
from unittest import mock

from services.account_service import AccountService
from services import reauth_service


class ClientIdRoutingTests(unittest.TestCase):
    """按账号来源选 OAuth client_id。"""

    def test_codex_source_uses_codex_client(self):
        account = {"source_type": "codex"}
        self.assertEqual(
            AccountService._oauth_client_id_for(account),
            AccountService._CODEX_OAUTH_CLIENT_ID,
        )

    def test_export_type_codex_uses_codex_client(self):
        account = {"export_type": "codex"}
        self.assertEqual(
            AccountService._oauth_client_id_for(account),
            AccountService._CODEX_OAUTH_CLIENT_ID,
        )

    def test_normal_account_uses_platform_client(self):
        account = {"source_type": "web"}
        self.assertEqual(
            AccountService._oauth_client_id_for(account),
            AccountService._OAUTH_CLIENT_ID,
        )

    def test_no_account_uses_platform_client(self):
        self.assertEqual(
            AccountService._oauth_client_id_for(None),
            AccountService._OAUTH_CLIENT_ID,
        )

    def test_two_clients_are_different(self):
        """两个 client 必须不同 —— 相同的话分流就没意义了。"""
        self.assertNotEqual(
            AccountService._CODEX_OAUTH_CLIENT_ID,
            AccountService._OAUTH_CLIENT_ID,
        )

    def test_id_token_aud_fallback(self):
        """source_type 没标对时，用 id_token 的 aud 反推是哪个 client 铸的。"""
        import base64
        import json

        def make(aud: str) -> str:
            body = base64.urlsafe_b64encode(
                json.dumps({"aud": aud}).encode()
            ).decode().rstrip("=")
            return f"header.{body}.sig"

        codex = make(AccountService._CODEX_OAUTH_CLIENT_ID)
        platform = make(AccountService._OAUTH_CLIENT_ID)

        self.assertEqual(
            AccountService._oauth_client_id_for({"id_token": codex}),
            AccountService._CODEX_OAUTH_CLIENT_ID,
        )
        self.assertEqual(
            AccountService._oauth_client_id_for({"id_token": platform}),
            AccountService._OAUTH_CLIENT_ID,
        )

    def test_refresh_request_sends_routed_client_id(self):
        """真正发请求时用的 client_id 必须跟着账号走。"""
        service = AccountService.__new__(AccountService)
        captured = {}

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"access_token": "AT", "refresh_token": "RT2", "id_token": "IT"}

        class _Session:
            def __init__(self, **_kwargs):
                pass

            def post(self, url, headers=None, data=None, timeout=None):
                captured.update(data or {})
                return _Resp()

            def close(self):
                pass

        with mock.patch("curl_cffi.requests.Session", return_value=_Session()):
            out = service._request_access_token_refresh(
                "RT1", {"source_type": "codex"}
            )

        self.assertEqual(
            captured.get("client_id"), AccountService._CODEX_OAUTH_CLIENT_ID
        )
        self.assertEqual(out["refresh_token"], "RT2")


class CodexAccountDetectionTests(unittest.TestCase):
    def test_is_codex_account(self):
        self.assertTrue(AccountService._is_codex_account({"source_type": "codex"}))
        self.assertTrue(AccountService._is_codex_account({"export_type": "codex"}))
        self.assertFalse(AccountService._is_codex_account({"source_type": "web"}))
        self.assertFalse(AccountService._is_codex_account(None))


class KeepaliveSkipsCodexTests(unittest.TestCase):
    """keepalive 不能去刷 codex 账号的 RT。"""

    def _service_with(self, accounts: list[dict]):
        service = AccountService.__new__(AccountService)
        service._accounts = {a["access_token"]: a for a in accounts}
        service._lock = __import__("threading").RLock()
        return service

    def test_codex_account_is_not_in_keepalive_list(self):
        service = self._service_with(
            [
                {
                    "access_token": "codex-token",
                    "source_type": "codex",
                    "refresh_token": "RT",
                    "created_at": 0,
                    "last_token_refresh_at": None,
                }
            ]
        )
        self.assertEqual(service.list_refresh_token_keepalive_tokens(), [])

    def test_normal_account_still_in_keepalive_list(self):
        service = self._service_with(
            [
                {
                    "access_token": "web-token",
                    "source_type": "web",
                    "refresh_token": "RT",
                    "created_at": 0,
                    "last_token_refresh_at": None,
                }
            ]
        )
        self.assertEqual(service.list_refresh_token_keepalive_tokens(), ["web-token"])


class RecordCodexDetectionTests(unittest.TestCase):
    """注册记录里怎么判断「这枚 RT 是 codex 的」。"""

    def test_extra_flag(self):
        self.assertTrue(
            reauth_service._record_is_codex(
                {"extra": {"codex_refresh_token": True}}
            )
        )

    def test_extra_json_string(self):
        self.assertTrue(
            reauth_service._record_is_codex(
                {"extra_json": '{"codex_refresh_token": true}'}
            )
        )

    def test_source_type_fallback(self):
        self.assertTrue(reauth_service._record_is_codex({"source_type": "codex"}))

    def test_normal_record(self):
        self.assertFalse(reauth_service._record_is_codex({"extra": {}}))
        self.assertFalse(reauth_service._record_is_codex({}))


class NoMailProviderTests(unittest.TestCase):
    """重新授权不提供邮箱来源 —— 需要邮箱码时要明确报错而不是干等。"""

    def test_wait_for_otp_raises_clear_error(self):
        provider = reauth_service._NoMailProvider()
        with self.assertRaises(RuntimeError) as ctx:
            provider.wait_for_otp("a@b.com", timeout=10)
        message = str(ctx.exception)
        self.assertIn("邮箱验证码", message)

    def test_create_mailbox_returns_empty(self):
        self.assertEqual(reauth_service._NoMailProvider().create_mailbox(), "")


class ReauthorizeGuardTests(unittest.TestCase):
    """重新授权的入参校验 —— 必须在发起网络请求前挡掉。"""

    def test_empty_email_rejected(self):
        result = reauth_service.reauthorize_registered_account("")
        self.assertFalse(result["ok"])
        self.assertIn("邮箱", result["error"])

    def test_unknown_email_rejected(self):
        with mock.patch(
            "services.storage.register_store.get_registered", return_value=None
        ):
            result = reauth_service.reauthorize_registered_account("nobody@example.com")
        self.assertFalse(result["ok"])
        self.assertIn("注册结果", result["error"])


class ReauthorizeSuccessTests(unittest.TestCase):
    """mock 掉 AuthFlow，验证写回与号池同步。"""

    def _patch_store(self, record):
        return mock.patch.multiple(
            "services.storage.register_store",
            get_registered=mock.DEFAULT,
            save_registered=mock.DEFAULT,
        )

    def test_success_writes_back_and_syncs_pool(self):
        record = {
            "email": "a@b.com",
            "password": "pw123",
            "totp_secret": "SECRET",
            "device_id": "dev-1",
            "proxy": "http://old:1",
            "refresh_token": "OLD_RT",
            "extra": {"codex_refresh_token": True},
        }

        class _Result:
            access_token = "NEW_AT"
            refresh_token = "NEW_RT"
            id_token = "NEW_IT"
            session_json = '{"accessToken":"x"}'
            session_token = "ST"
            cookie_header = "ck"
            codex_refresh_token = True

        saved = {}

        def _fake_save_registered(payload):
            saved.update(payload)

        with mock.patch(
            "services.storage.register_store.get_registered", return_value=record
        ), mock.patch(
            "services.storage.register_store.save_registered", side_effect=_fake_save_registered
        ), mock.patch(
            "services.register.auth_flow.AuthFlow"
        ) as flow_cls:
            flow_cls.return_value.run_protocol_login.return_value = _Result()

            with mock.patch(
                "services.account_service.account_service.get_account_by_email",
                return_value={"access_token": "OLD_AT", "email": "a@b.com"},
            ), mock.patch(
                "services.account_service.account_service._apply_refreshed_tokens"
            ) as apply_tokens:
                result = reauth_service.reauthorize_registered_account(
                    "a@b.com", proxy="http://new:2"
                )

        self.assertTrue(result["ok"], msg=result.get("error"))
        self.assertTrue(result["codex_refresh_token"])
        self.assertTrue(result["has_session"])
        self.assertTrue(result["pool_updated"])
        self.assertEqual(result["proxy"], "http://new:2")

        # 新凭证写回了注册结果表
        self.assertEqual(saved.get("access_token"), "NEW_AT")
        self.assertEqual(saved.get("refresh_token"), "NEW_RT")
        self.assertEqual(saved.get("session_json"), '{"accessToken":"x"}')
        # codex 标记要带上 —— 否则下次刷新又用错 client_id
        self.assertTrue(saved.get("codex_refresh_token"))

        # 号池里同一个邮箱的号换了 token
        apply_tokens.assert_called_once()
        args = apply_tokens.call_args[0]
        self.assertEqual(args[0], "OLD_AT")
        self.assertEqual(args[1]["access_token"], "NEW_AT")

    def test_codex_account_keeps_old_rt_when_codex_exchange_fails(self):
        """只换到普通 access_token 时，不能把 codex 的 RT 顶掉。"""
        record = {
            "email": "a@b.com",
            "password": "pw",
            "refresh_token": "OLD_CODEX_RT",
            "extra": {"codex_refresh_token": True},
        }

        class _Result:
            access_token = "NEW_AT"
            refresh_token = "PLATFORM_RT"
            id_token = ""
            session_json = "{}"
            session_token = ""
            cookie_header = ""
            codex_refresh_token = False

        saved = {}

        with mock.patch(
            "services.storage.register_store.get_registered", return_value=record
        ), mock.patch(
            "services.storage.register_store.save_registered",
            side_effect=lambda p: saved.update(p),
        ), mock.patch(
            "services.register.auth_flow.AuthFlow"
        ) as flow_cls:
            flow_cls.return_value.run_protocol_login.return_value = _Result()
            with mock.patch(
                "services.account_service.account_service.get_account_by_email",
                return_value=None,
            ):
                result = reauth_service.reauthorize_registered_account("a@b.com")

        self.assertTrue(result["ok"])
        self.assertFalse(result["codex_refresh_token"])
        # 老的 codex RT 保留
        self.assertEqual(saved.get("refresh_token"), "OLD_CODEX_RT")
        self.assertNotIn("codex_refresh_token", saved)

    def test_flow_exception_returns_error(self):
        record = {"email": "a@b.com", "password": "pw"}

        with mock.patch(
            "services.storage.register_store.get_registered", return_value=record
        ), mock.patch(
            "services.register.auth_flow.AuthFlow"
        ) as flow_cls:
            flow_cls.return_value.run_protocol_login.side_effect = RuntimeError(
                "warmup 失败：代理出口不通"
            )
            result = reauth_service.reauthorize_registered_account("a@b.com")

        self.assertFalse(result["ok"])
        self.assertIn("warmup", result["error"])


if __name__ == "__main__":
    unittest.main()

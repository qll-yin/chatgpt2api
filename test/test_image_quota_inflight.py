"""生图选号：额度必须大于在途 / 超时对外文案。

两个用户实际遇到的问题：

1. 账号剩余额度 1，但同时来了 2 个请求 —— 两个都打到那个号上，第二个必然
   被上游以「free 号额度不足」拒掉。选号因此要求 `剩余额度 > 在途数`。
2. 生图超时把内部细节（等了多少秒、去改哪个 config）直接返回给终端用户。
   现在统一成一句英文，状态码仍是 502。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services import log_service
from services.account_service import AccountService
from services.config import config
from services.openai_backend_api import ImagePollTimeoutError
from services.protocol.conversation import (
    IMAGE_TIMEOUT_MESSAGE,
    is_image_timeout_error,
    public_image_error_message,
)
from services.storage.json_storage import JSONStorageBackend


def _make_service(accounts: list[dict]) -> AccountService:
    tmp = tempfile.mkdtemp()
    service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
    service.add_account_items(accounts)
    service.fetch_remote_info = lambda access_token, event="fetch_remote_info": service.get_account(access_token)
    return service


class _ConfigPatch:
    """临时改 config.data 里的键，用完还原。"""

    def __init__(self, **values):
        self.values = values
        self.original: dict[str, object] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.original[key] = config.data.get(key, None)
            if value is None:
                config.data.pop(key, None)
            else:
                config.data[key] = value
        return self

    def __exit__(self, *exc):
        for key, value in self.original.items():
            if value is None:
                config.data.pop(key, None)
            else:
                config.data[key] = value


class QuotaVsInflightTests(unittest.TestCase):
    def test_single_quota_account_is_never_shared(self):
        """额度 1 + 2 个请求：第二个不能再拿到同一个号。"""
        service = _make_service([{"access_token": "t1", "type": "Plus", "status": "正常", "quota": 1}])
        with _ConfigPatch(image_account_concurrency=3, image_quota_wait_secs=0):
            first = service.get_available_access_token(plan_type="plus")
            self.assertEqual(first, "t1")
            with self.assertRaises(RuntimeError) as ctx:
                service.get_available_access_token(plan_type="plus")
            self.assertIn("image quota", str(ctx.exception).lower())
            # 确认没把第二个请求也记在途
            self.assertEqual(service._image_inflight.get("t1"), 1)
            service.release_image_slot(first)

    def test_second_request_waits_then_succeeds_when_slot_frees(self):
        """第一个请求失败（额度不扣）→ 排队的第二个应当接手。"""
        service = _make_service([{"access_token": "t1", "type": "Plus", "status": "正常", "quota": 1}])
        with _ConfigPatch(image_account_concurrency=3, image_quota_wait_secs=5):
            first = service.get_available_access_token(plan_type="plus")
            self.assertEqual(first, "t1")

            def free_slot():
                time.sleep(0.2)
                service.release_image_slot("t1")

            import threading

            threading.Thread(target=free_slot, daemon=True).start()
            second = service.get_available_access_token(plan_type="plus")
            self.assertEqual(second, "t1")
            service.release_image_slot(second)

    def test_quota_exhausted_returns_insufficient_quota(self):
        """成功一次后额度归零 → 后续请求立刻判没额度，不会继续打到该号。"""
        service = _make_service([{"access_token": "t1", "type": "Plus", "status": "正常", "quota": 1}])
        with _ConfigPatch(image_account_concurrency=3, image_quota_wait_secs=0):
            token = service.get_available_access_token(plan_type="plus")
            service.mark_image_result(token, success=True)
            self.assertEqual(service.get_account("t1")["quota"], 0)
            with self.assertRaises(RuntimeError):
                service.get_available_access_token(plan_type="plus")

    def test_quota_two_allows_two_then_blocks(self):
        service = _make_service([{"access_token": "t1", "type": "Plus", "status": "正常", "quota": 2}])
        with _ConfigPatch(image_account_concurrency=3, image_quota_wait_secs=0):
            first = service.get_available_access_token(plan_type="plus")
            second = service.get_available_access_token(plan_type="plus")
            self.assertEqual({first, second}, {"t1"})
            with self.assertRaises(RuntimeError):
                service.get_available_access_token(plan_type="plus")
            service.release_image_slot(first)
            service.release_image_slot(second)

    def test_concurrency_cap_still_applies(self):
        """额度很多时，仍然受 image_account_concurrency 限制。"""
        service = _make_service([{"access_token": "t1", "type": "Plus", "status": "正常", "quota": 99}])
        with _ConfigPatch(image_account_concurrency=1, image_quota_wait_secs=0):
            token = service.get_available_access_token(plan_type="plus")
            with self.assertRaises(RuntimeError):
                service.get_available_access_token(plan_type="plus")
            service.release_image_slot(token)

    def test_index_cache_invalidated_by_quota_change(self):
        """额度变化后候选索引必须失效（否则会选到已经没额度的号）。"""
        service = _make_service(
            [
                {"access_token": "t1", "type": "Plus", "status": "正常", "quota": 1},
                {"access_token": "t2", "type": "Plus", "status": "正常", "quota": 1},
            ]
        )
        with _ConfigPatch(image_account_concurrency=3, image_quota_wait_secs=0):
            self.assertEqual(len(service._image_ready_tokens("plus", None, None)), 2)
            rev_before = service._accounts_rev
            token = service._acquire_next_candidate_token(plan_type="plus")
            service.release_image_slot(token)
            service.mark_image_result(token, success=True)
            self.assertGreater(service._accounts_rev, rev_before)
            # 额度归零的号已从候选里消失
            ready = service._image_ready_tokens("plus", None, None)
            self.assertNotIn(token, ready)
            self.assertEqual(len(ready), 1)

    def test_excluded_tokens_are_skipped_then_reported(self):
        """超时换号：优先避开试过的号；全都试过才报没额度。"""
        service = _make_service(
            [
                {"access_token": "t1", "type": "Plus", "status": "正常", "quota": 5},
                {"access_token": "t2", "type": "Plus", "status": "正常", "quota": 5},
            ]
        )
        with _ConfigPatch(image_account_concurrency=3, image_quota_wait_secs=0):
            first = service.get_available_access_token(plan_type="plus")
            second = service.get_available_access_token(
                plan_type="plus", excluded_tokens={first}
            )
            self.assertNotEqual(first, second)
            service.release_image_slot(first)
            service.release_image_slot(second)


class ImageTimeoutMessageTests(unittest.TestCase):
    def test_timeout_message_is_generic_english(self):
        text = (
            "ChatGPT 生图超时（已等待 66.45449818699853 秒）。"
            "当前超时阈值可在 config.json 中调大 image_poll_timeout_secs，"
            "也可能是账号被限流或生图队列拥堵导致。"
        )
        self.assertEqual(public_image_error_message(text), IMAGE_TIMEOUT_MESSAGE)

    def test_timeout_exception_detected(self):
        self.assertTrue(is_image_timeout_error(ImagePollTimeoutError("ChatGPT 生图超时（已等待 1 秒）")))
        self.assertFalse(is_image_timeout_error("ChatGPT 文件下载超时（已等待 60 秒）。"))

    def test_error_response_keeps_502_and_hides_details(self):
        exc = ImagePollTimeoutError(
            "ChatGPT 生图超时（已等待 66.45449818699853 秒）。"
            "当前超时阈值可在 config.json 中调大 image_poll_timeout_secs"
        )
        response = log_service._image_error_response(exc)
        self.assertEqual(response.status_code, 502)
        body = response.body.decode("utf-8")
        self.assertIn("Image generation timed out, please try again", body)
        self.assertNotIn("config.json", body)
        self.assertNotIn("66.45", body)


class ConfigDefaultTests(unittest.TestCase):
    def test_default_wait_is_15_seconds(self):
        with _ConfigPatch(image_quota_wait_secs=None):
            self.assertEqual(config.image_quota_wait_secs, 15.0)

    def test_zero_means_no_wait(self):
        with _ConfigPatch(image_quota_wait_secs=0):
            service = _make_service([{"access_token": "t1", "type": "Plus", "status": "正常", "quota": 1}])
            token = service.get_available_access_token(plan_type="plus")
            started = time.monotonic()
            with self.assertRaises(RuntimeError):
                service.get_available_access_token(plan_type="plus")
            self.assertLess(time.monotonic() - started, 2.0)
            service.release_image_slot(token)


if __name__ == "__main__":
    unittest.main()

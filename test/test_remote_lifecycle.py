"""远端生命周期规则（R2 服务端过期删除）。

R2 的 put_bucket_lifecycle_configuration 是**整桶替换**语义 ——
直接写会把桶上其它规则冲掉。这套用例守住「只替换我们自己那条」。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

# ⚠️ 必须早于 services.* 的导入：把数据库指向临时库
from test.db_fixture import reset_database_state, use_temp_database

use_temp_database("chatgpt2api-test-lifecycle-")

from services.image_storage_service import ImageStorageError, ImageStorageService, S3Client  # noqa: E402


class FakeS3Client:
    """记录生命周期规则调用。"""

    put_calls: list[int] = []
    remove_calls: int = 0
    bucket = "my-images"
    root_path = "chatgpt2api/images"

    def __init__(self, _settings):
        pass

    def put_lifecycle_configuration(self, days):
        FakeS3Client.put_calls.append(days)
        return {"rules": [], "replaced": 0, "kept": 0}

    def remove_lifecycle_configuration(self):
        FakeS3Client.remove_calls += 1
        return {"removed": 1, "kept": 0}

    def get_lifecycle_configuration(self):
        return {"Rules": []}

    def list_objects(self, prefix=""):
        return []

    def delete_keys(self, keys):
        return 0


def make_client(**extra):
    client = S3Client.__new__(S3Client)
    client.endpoint = "https://acct.r2.cloudflarestorage.com"
    client.region = "auto"
    client.bucket = "my-images"
    client.root_path = "chatgpt2api/images"
    client.client = mock.MagicMock()
    for key, value in extra.items():
        setattr(client, key, value)
    return client


class S3ClientLifecycleTests(unittest.TestCase):
    def test_put_writes_rule_with_prefix_and_days(self):
        client = make_client()
        client.get_lifecycle_configuration = mock.MagicMock(return_value={"Rules": []})

        client.put_lifecycle_configuration(1)

        kwargs = client.client.put_bucket_lifecycle_configuration.call_args.kwargs
        self.assertEqual(kwargs["Bucket"], "my-images")
        rules = kwargs["LifecycleConfiguration"]["Rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["ID"], S3Client.LIFECYCLE_RULE_ID)
        self.assertEqual(rules[0]["Status"], "Enabled")
        self.assertEqual(rules[0]["Filter"]["Prefix"], "chatgpt2api/images/")
        self.assertEqual(rules[0]["Expiration"]["Days"], 1)

    def test_put_keeps_other_rules(self):
        """桶上别的规则（备份/日志保留）不能被冲掉。"""
        client = make_client()
        client.get_lifecycle_configuration = mock.MagicMock(return_value={
            "Rules": [
                {"ID": "keep-logs", "Status": "Enabled", "Filter": {"Prefix": "logs/"}, "Expiration": {"Days": 90}},
            ]
        })

        result = client.put_lifecycle_configuration(7)

        rules = client.client.put_bucket_lifecycle_configuration.call_args.kwargs["LifecycleConfiguration"]["Rules"]
        self.assertEqual(len(rules), 2)
        ids = {rule["ID"] for rule in rules}
        self.assertIn("keep-logs", ids)
        self.assertIn(S3Client.LIFECYCLE_RULE_ID, ids)
        self.assertEqual(result["kept"], 1)

    def test_put_replaces_our_own_rule_not_duplicates(self):
        """重复应用不会堆出两条我们的规则。"""
        client = make_client()
        client.get_lifecycle_configuration = mock.MagicMock(return_value={
            "Rules": [
                {"ID": S3Client.LIFECYCLE_RULE_ID, "Status": "Enabled",
                 "Filter": {"Prefix": "chatgpt2api/images/"}, "Expiration": {"Days": 30}},
            ]
        })

        client.put_lifecycle_configuration(1)

        rules = client.client.put_bucket_lifecycle_configuration.call_args.kwargs["LifecycleConfiguration"]["Rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["Expiration"]["Days"], 1)

    def test_put_treats_legacy_rule_without_id_as_ours(self):
        """老版本写的规则没有 ID，但 prefix 一样 —— 也要当自己的处理。"""
        client = make_client()
        client.get_lifecycle_configuration = mock.MagicMock(return_value={
            "Rules": [
                {"Status": "Enabled", "Filter": {"Prefix": "chatgpt2api/images/"}, "Expiration": {"Days": 30}},
            ]
        })

        client.put_lifecycle_configuration(1)

        rules = client.client.put_bucket_lifecycle_configuration.call_args.kwargs["LifecycleConfiguration"]["Rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["ID"], S3Client.LIFECYCLE_RULE_ID)

    def test_put_raises_storage_error(self):
        client = make_client()
        client.get_lifecycle_configuration = mock.MagicMock(return_value={"Rules": []})
        client.client.put_bucket_lifecycle_configuration.side_effect = RuntimeError("AccessDenied")

        with self.assertRaises(ImageStorageError) as ctx:
            client.put_lifecycle_configuration(1)
        self.assertIn("AccessDenied", str(ctx.exception))

    def test_get_returns_empty_when_no_configuration(self):
        client = make_client()
        client.client.get_bucket_lifecycle_configuration.side_effect = _no_such_lifecycle()

        self.assertEqual(client.get_lifecycle_configuration(), {"rules": []})

    def test_get_raises_on_other_errors(self):
        client = make_client()
        client.client.get_bucket_lifecycle_configuration.side_effect = RuntimeError("boom")
        with self.assertRaises(ImageStorageError):
            client.get_lifecycle_configuration()

    def test_remove_keeps_other_rules(self):
        client = make_client()
        client.get_lifecycle_configuration = mock.MagicMock(return_value={
            "Rules": [
                {"ID": S3Client.LIFECYCLE_RULE_ID, "Status": "Enabled",
                 "Filter": {"Prefix": "chatgpt2api/images/"}, "Expiration": {"Days": 1}},
                {"ID": "keep-logs", "Status": "Enabled", "Filter": {"Prefix": "logs/"}, "Expiration": {"Days": 90}},
            ]
        })

        result = client.remove_lifecycle_configuration()

        rules = client.client.put_bucket_lifecycle_configuration.call_args.kwargs["LifecycleConfiguration"]["Rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["ID"], "keep-logs")
        self.assertEqual(result["removed"], 1)
        client.client.delete_bucket_lifecycle.assert_not_called()

    def test_remove_deletes_whole_config_when_only_ours(self):
        client = make_client()
        client.get_lifecycle_configuration = mock.MagicMock(return_value={
            "Rules": [
                {"ID": S3Client.LIFECYCLE_RULE_ID, "Status": "Enabled",
                 "Filter": {"Prefix": "chatgpt2api/images/"}, "Expiration": {"Days": 1}},
            ]
        })

        result = client.remove_lifecycle_configuration()

        client.client.delete_bucket_lifecycle.assert_called_once_with(Bucket="my-images")
        client.client.put_bucket_lifecycle_configuration.assert_not_called()
        self.assertEqual(result["removed"], 1)

    def test_remove_noop_when_nothing_to_remove(self):
        client = make_client()
        client.get_lifecycle_configuration = mock.MagicMock(return_value={"Rules": []})

        result = client.remove_lifecycle_configuration()

        self.assertEqual(result["removed"], 0)
        client.client.put_bucket_lifecycle_configuration.assert_not_called()
        client.client.delete_bucket_lifecycle.assert_not_called()


def _no_such_lifecycle():
    """构造一个 botocore 风格的 NoSuchLifecycleConfiguration 异常。"""
    error = RuntimeError("no such lifecycle")
    error.response = {"Error": {"Code": "NoSuchLifecycleConfiguration"}}
    return error


class ApplyRemoteLifecycleTests(unittest.TestCase):
    """存储服务层的下发逻辑：指纹比对、按需重下、关闭时摘除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        reset_database_state()
        self.images_dir = Path(self.tmp.name) / "images"
        self.settings = {
            "enabled": True,
            "mode": "remote",
            "provider": "s3",
            "s3_endpoint": "https://acct.r2.cloudflarestorage.com",
            "s3_bucket": "my-images",
            "s3_access_key_id": "AK",
            "s3_secret_access_key": "SK",
            "remote_root_path": "chatgpt2api/images",
            "public_base_url": "",
            "remote_cleanup_enabled": True,
            "remote_cleanup_fallback": False,
            "remote_retention_days": 1,
            "remote_lifecycle_applied": "",
        }
        patcher = mock.patch("services.image_storage_service.config")
        self.mock_config = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_config.images_dir = self.images_dir
        self.mock_config.base_url = "http://app.test"
        self.mock_config.get_image_storage_settings.side_effect = lambda: dict(self.settings)
        # 模拟真实行为：写回指纹后，settings 里就能读到它
        self.mock_config.set_image_storage_lifecycle_applied.side_effect = (
            lambda fingerprint: self.settings.__setitem__("remote_lifecycle_applied", fingerprint)
        )
        FakeS3Client.put_calls = []
        FakeS3Client.remove_calls = 0

    def service(self) -> ImageStorageService:
        return ImageStorageService(Path(self.tmp.name) / "image_index.json")

    def _patch_client(self):
        return mock.patch("services.image_storage_service.S3Client", FakeS3Client)

    def test_applies_rule_when_not_applied_yet(self):
        with self._patch_client():
            result = self.service().apply_remote_lifecycle()

        self.assertTrue(result["applied"])
        self.assertEqual(result["days"], 1)
        self.assertEqual(FakeS3Client.put_calls, [1])

    def test_skips_when_fingerprint_unchanged(self):
        """指纹没变就不该发请求 —— 定时器每 30 分钟跑一次，不能每次都写。"""
        with self._patch_client():
            self.service().apply_remote_lifecycle()
            FakeS3Client.put_calls.clear()
            result = self.service().apply_remote_lifecycle()

        self.assertFalse(result["applied"])
        self.assertIn("最新", result["reason"])
        self.assertEqual(FakeS3Client.put_calls, [])

    def test_force_reapplies(self):
        with self._patch_client():
            self.service().apply_remote_lifecycle()
            FakeS3Client.put_calls.clear()
            result = self.service().apply_remote_lifecycle(force=True)

        self.assertTrue(result["applied"])
        self.assertEqual(FakeS3Client.put_calls, [1])

    def test_changed_retention_triggers_reapply(self):
        with self._patch_client():
            self.service().apply_remote_lifecycle()
            FakeS3Client.put_calls.clear()
            self.settings["remote_retention_days"] = 7
            result = self.service().apply_remote_lifecycle()

        self.assertTrue(result["applied"])
        self.assertEqual(result["days"], 7)
        self.assertEqual(FakeS3Client.put_calls, [7])

    def test_changed_bucket_triggers_reapply(self):
        with self._patch_client():
            self.service().apply_remote_lifecycle()
            FakeS3Client.put_calls.clear()
            self.settings["s3_bucket"] = "other-bucket"
            result = self.service().apply_remote_lifecycle()

        self.assertTrue(result["applied"])
        self.assertEqual(FakeS3Client.put_calls, [1])

    def test_disabling_removes_our_rule(self):
        with self._patch_client():
            self.service().apply_remote_lifecycle()
            self.settings["remote_cleanup_enabled"] = False
            result = self.service().apply_remote_lifecycle()

        self.assertTrue(result["applied"])
        self.assertEqual(result["removed"], 1)
        self.assertEqual(FakeS3Client.remove_calls, 1)

    def test_disabled_and_never_applied_does_nothing(self):
        self.settings["remote_cleanup_enabled"] = False
        with self._patch_client():
            result = self.service().apply_remote_lifecycle()

        self.assertFalse(result["applied"])
        self.assertEqual(FakeS3Client.remove_calls, 0)

    def test_webdav_provider_is_unsupported(self):
        self.settings["provider"] = "webdav"
        with self._patch_client():
            result = self.service().apply_remote_lifecycle()

        self.assertFalse(result["applied"])
        self.assertIn("不支持", result["reason"])

    def test_local_mode_is_skipped(self):
        self.settings["mode"] = "local"
        with self._patch_client():
            result = self.service().apply_remote_lifecycle()

        self.assertFalse(result["applied"])
        self.assertIn("未启用", result["reason"])

    def test_fingerprint_includes_all_relevant_fields(self):
        service = self.service()
        base = service._lifecycle_fingerprint()
        self.settings["remote_retention_days"] = 3
        self.assertNotEqual(base, service._lifecycle_fingerprint())
        self.settings["remote_retention_days"] = 1
        self.settings["s3_bucket"] = "other"
        self.assertNotEqual(base, service._lifecycle_fingerprint())


if __name__ == "__main__":
    unittest.main()

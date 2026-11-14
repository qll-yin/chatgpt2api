"""远端过期清理：应用侧兜底扫描（Lifecycle Rule 才是主路径）。

兜底扫描每 30 分钟列一遍全桶、按对象上传时间删过期对象 ——
桶大了很贵也很慢，所以默认关闭（``remote_cleanup_fallback``）。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test.db_fixture import reset_database_state, use_temp_database

use_temp_database("chatgpt2api-test-cleanup-")

from services.image_storage_service import ImageStorageService  # noqa: E402


class FakeS3Client:
    """记录列举与删除调用，模拟对象的 LastModified。"""

    objects: list[dict] = []
    deleted_keys: list[str] = []
    list_error: Exception | None = None

    def __init__(self, _settings):
        pass

    def list_objects(self, prefix: str = ""):
        if self.list_error:
            raise self.list_error
        return list(self.objects)

    def delete_keys(self, keys):
        self.deleted_keys.extend(keys)
        for key in keys:
            self.objects = [obj for obj in self.objects if obj["key"] != key]
        return len(keys)

    def put(self, rel, payload, content_type="image/png"):
        return ""

    def get(self, rel):
        return b""

    def delete(self, rel):
        return True

    def test(self):
        return {"ok": True, "status": 200, "error": None}


class RemoteCleanupTests(unittest.TestCase):
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
            "s3_bucket": "b",
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

        FakeS3Client.objects = []
        FakeS3Client.deleted_keys = []
        FakeS3Client.list_error = None

    def service(self) -> ImageStorageService:
        return ImageStorageService(Path(self.tmp.name) / "image_index.json")

    def _patch_client(self):
        return mock.patch("services.image_storage_service.S3Client", FakeS3Client)

    def test_fallback_disabled_by_default(self):
        """兜底扫描默认关闭 —— 不能擅自扫用户的桶。"""
        FakeS3Client.objects = [{"key": "chatgpt2api/images/old.png", "last_modified": 0, "size": 1}]
        with self._patch_client():
            result = self.service().cleanup_remote()

        self.assertFalse(result["enabled"])
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(FakeS3Client.deleted_keys, [])

    def test_fallback_deletes_only_expired_objects(self):
        self.settings["remote_cleanup_fallback"] = True
        self.settings["remote_retention_days"] = 1
        now = time.time()
        FakeS3Client.objects = [
            {"key": "chatgpt2api/images/old.png", "last_modified": now - 48 * 3600, "size": 1},
            {"key": "chatgpt2api/images/fresh.png", "last_modified": now - 3600, "size": 1},
        ]
        with self._patch_client():
            result = self.service().cleanup_remote()

        self.assertTrue(result["enabled"])
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(FakeS3Client.deleted_keys, ["chatgpt2api/images/old.png"])

    def test_fallback_uses_retention_days(self):
        self.settings["remote_cleanup_fallback"] = True
        self.settings["remote_retention_days"] = 7
        now = time.time()
        FakeS3Client.objects = [
            {"key": "chatgpt2api/images/a.png", "last_modified": now - 3 * 86400, "size": 1},
            {"key": "chatgpt2api/images/b.png", "last_modified": now - 10 * 86400, "size": 1},
        ]
        with self._patch_client():
            result = self.service().cleanup_remote()

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["retention_days"], 7)
        self.assertEqual(FakeS3Client.deleted_keys, ["chatgpt2api/images/b.png"])

    def test_fallback_skipped_when_remote_disabled(self):
        self.settings["remote_cleanup_fallback"] = True
        self.settings["mode"] = "local"
        with self._patch_client():
            result = self.service().cleanup_remote()
        self.assertEqual(result["deleted"], 0)
        self.assertIn("未启用", result["reason"])

    def test_fallback_unsupported_for_webdav(self):
        """WebDAV 没有列举接口 —— 明确说不支持。"""
        self.settings["remote_cleanup_fallback"] = True
        self.settings["provider"] = "webdav"
        result = self.service().cleanup_remote()
        self.assertEqual(result["deleted"], 0)
        self.assertIn("不支持", result["reason"])

    def test_fallback_survives_remote_error(self):
        """远端不可达时不能抛异常 —— 定时器那一轮要能继续跑。"""
        self.settings["remote_cleanup_fallback"] = True
        FakeS3Client.list_error = RuntimeError("connection reset")
        with self._patch_client():
            result = self.service().cleanup_remote()
        self.assertEqual(result["deleted"], 0)
        self.assertIn("connection reset", result["error"])


if __name__ == "__main__":
    unittest.main()

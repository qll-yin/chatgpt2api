from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

# ⚠️ 必须早于 services.* 的导入：把数据库指向临时库，
# 否则图片索引会写进真实的 data/chatgpt2api.db 并跨测试污染。
from test.db_fixture import reset_database_state, use_temp_database

use_temp_database("chatgpt2api-test-storage-")

from services.image_storage_service import ImageStorageService  # noqa: E402
from services.storage import runtime_store as store  # noqa: E402


def png_bytes() -> bytes:
    """直接生成 PNG 字节 —— 不落盘，免得在系统临时目录里留垃圾文件。"""
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


class FakeWebDAVClient:
    uploaded: dict[str, bytes] = {}
    deleted: list[str] = []
    fail_on_put = False

    def __init__(self, _settings):
        pass

    def put(self, rel: str, payload: bytes, content_type: str = "image/png") -> str:
        if self.fail_on_put:
            raise RuntimeError("webdav unavailable")
        self.uploaded[rel] = payload
        return f"https://dav.example.test/{rel}"

    def get(self, rel: str) -> bytes:
        return self.uploaded[rel]

    def delete(self, rel: str) -> bool:
        self.deleted.append(rel)
        self.uploaded.pop(rel, None)
        return True

    def test(self) -> dict[str, object]:
        self.put(".chatgpt2api_webdav_test.txt", b"chatgpt2api webdav test\n")
        self.delete(".chatgpt2api_webdav_test.txt")
        return {"ok": True, "status": 200, "error": None}


class FakeS3Client:
    uploaded: dict[str, bytes] = {}
    deleted: list[str] = []
    fail_on_put = False
    instances = 0

    def __init__(self, _settings):
        FakeS3Client.instances += 1

    def put(self, rel: str, payload: bytes, content_type: str = "image/png") -> str:
        if self.fail_on_put:
            raise RuntimeError("s3 unavailable")
        self.uploaded[rel] = payload
        return f"https://pub.example.test/{rel}"

    def get(self, rel: str) -> bytes:
        return self.uploaded[rel]

    def delete(self, rel: str) -> bool:
        self.deleted.append(rel)
        self.uploaded.pop(rel, None)
        return True

    def test(self) -> dict[str, object]:
        return {"ok": True, "status": 200, "error": None}


class ImageStorageServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 每个用例从干净的表开始
        reset_database_state()
        self.data_dir = Path(self.tmp.name)
        self.images_dir = self.data_dir / "images"
        self.settings = {
            "enabled": False,
            "mode": "local",
            "provider": "s3",
            "s3_endpoint": "",
            "s3_region": "auto",
            "s3_bucket": "",
            "s3_access_key_id": "",
            "s3_secret_access_key": "",
            "s3_path_style": True,
            "webdav_url": "",
            "webdav_username": "",
            "webdav_password": "",
            "remote_root_path": "chatgpt2api/images",
            "public_base_url": "",
            "remote_cleanup_enabled": False,
            "remote_retention_hours": 24,
        }
        self.config_patcher = mock.patch("services.image_storage_service.config")
        self.mock_config = self.config_patcher.start()
        self.addCleanup(self.config_patcher.stop)
        self.mock_config.images_dir = self.images_dir
        self.mock_config.base_url = "http://app.test"
        self.mock_config.cleanup_old_images.return_value = 0
        self.mock_config.get_image_storage_settings.side_effect = lambda: dict(self.settings)
        FakeWebDAVClient.uploaded = {}
        FakeWebDAVClient.deleted = []
        FakeWebDAVClient.fail_on_put = False
        FakeS3Client.uploaded = {}
        FakeS3Client.deleted = []
        FakeS3Client.fail_on_put = False
        FakeS3Client.instances = 0

    def service(self) -> ImageStorageService:
        return ImageStorageService(self.data_dir / "image_index.json")

    def _webdav_settings(self, **extra):
        self.settings.update({
            "enabled": True,
            "mode": "remote",
            "provider": "webdav",
            "webdav_url": "https://dav.example.test",
            "webdav_password": "secret",
        })
        self.settings.update(extra)

    def _s3_settings(self, **extra):
        self.settings.update({
            "enabled": True,
            "mode": "remote",
            "provider": "s3",
            "s3_endpoint": "https://acct.r2.cloudflarestorage.com",
            "s3_bucket": "my-images",
            "s3_access_key_id": "AK",
            "s3_secret_access_key": "SK",
        })
        self.settings.update(extra)

    def test_local_mode_saves_to_local_directory(self):
        stored = self.service().save(png_bytes(), "http://app.test")

        self.assertEqual(stored.storage, "local")
        self.assertTrue((self.images_dir / stored.rel).is_file())
        self.assertEqual(stored.url, f"http://app.test/images/{stored.rel}")

    def test_webdav_mode_uploads_without_local_file(self):
        self._webdav_settings()
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            stored = self.service().save(png_bytes(), "http://app.test")
            payload = self.service().get_bytes(stored.rel)

        self.assertEqual(stored.storage, "remote")
        self.assertFalse((self.images_dir / stored.rel).exists())
        self.assertIn(stored.rel, FakeWebDAVClient.uploaded)
        self.assertEqual(payload, FakeWebDAVClient.uploaded[stored.rel])

    def test_s3_mode_uses_s3_client(self):
        self._s3_settings()
        with mock.patch("services.image_storage_service.S3Client", FakeS3Client):
            stored = self.service().save(png_bytes(), "http://app.test")
            payload = self.service().get_bytes(stored.rel)

        self.assertEqual(stored.storage, "remote")
        self.assertIn(stored.rel, FakeS3Client.uploaded)
        self.assertEqual(payload, FakeS3Client.uploaded[stored.rel])
        # 索引里要记清楚是哪种协议
        row = store.get_image_record(stored.rel)
        self.assertTrue(row["remote"])
        self.assertEqual(row["remote_provider"], "s3")

    def test_remote_failure_falls_back_to_local(self):
        """远端挂了不能把生图带崩 —— 降级存本地，链接照常返回。"""
        self._webdav_settings()
        FakeWebDAVClient.fail_on_put = True
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            stored = self.service().save(png_bytes(), "http://app.test")

        self.assertEqual(stored.storage, "local")
        self.assertTrue((self.images_dir / stored.rel).is_file())
        self.assertEqual(stored.url, f"http://app.test/images/{stored.rel}")
        # 失败原因要记进索引，方便排查
        row = store.get_image_record(stored.rel)
        self.assertFalse(row["remote"])
        self.assertIn("webdav unavailable", row["remote_error"])

    def test_s3_failure_falls_back_to_local(self):
        self._s3_settings()
        FakeS3Client.fail_on_put = True
        with mock.patch("services.image_storage_service.S3Client", FakeS3Client):
            stored = self.service().save(png_bytes(), "http://app.test")

        self.assertEqual(stored.storage, "local")
        self.assertTrue((self.images_dir / stored.rel).is_file())
        row = store.get_image_record(stored.rel)
        self.assertIn("s3 unavailable", row["remote_error"])

    def test_list_items_ignores_non_image_files(self):
        image = png_bytes()
        image_path = self.images_dir / "2026" / "05" / "07" / "sample.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(image)
        (self.images_dir / ".DS_Store").write_text("not an image", encoding="utf-8")
        (self.images_dir / "2026" / ".DS_Store").write_text("not an image", encoding="utf-8")

        items = self.service().list_items("http://app.test")

        self.assertEqual([item["rel"] for item in items], ["2026/05/07/sample.png"])
        self.assertEqual(items[0]["storage"], "local")

    def test_both_mode_saves_to_local_and_webdav(self):
        self._webdav_settings(mode="both", public_base_url="https://cdn.example.test/images")
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            stored = self.service().save(png_bytes(), "http://app.test")

        self.assertEqual(stored.storage, "both")
        self.assertTrue((self.images_dir / stored.rel).is_file())
        self.assertIn(stored.rel, FakeWebDAVClient.uploaded)
        # 公开前缀没写远端目录 → 程序补上（老配置里写全了就不会重复拼）
        self.assertEqual(
            stored.url,
            f"https://cdn.example.test/images/chatgpt2api/images/{stored.rel}",
        )

    def test_public_url_appends_remote_root_path(self):
        """公开访问前缀只填域名，远端目录由程序拼。"""
        self._s3_settings(public_base_url="https://pub.example.test")
        with mock.patch("services.image_storage_service.S3Client", FakeS3Client):
            stored = self.service().save(png_bytes(), "http://app.test")

        self.assertEqual(
            stored.url,
            f"https://pub.example.test/chatgpt2api/images/{stored.rel}",
        )

    def test_public_url_does_not_duplicate_root_path(self):
        """老配置里 public_base_url 已经含了远端目录 → 不重复拼。"""
        self._s3_settings(public_base_url="https://cdn.example.test/chatgpt2api/images")
        with mock.patch("services.image_storage_service.S3Client", FakeS3Client):
            stored = self.service().save(png_bytes(), "http://app.test")

        self.assertEqual(
            stored.url,
            f"https://cdn.example.test/chatgpt2api/images/{stored.rel}",
        )

    def test_test_webdav_writes_and_deletes_probe_file(self):
        self._webdav_settings()
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            result = self.service().test_webdav()

        self.assertTrue(result["ok"])
        self.assertIn(".chatgpt2api_webdav_test.txt", FakeWebDAVClient.deleted)

    def test_test_remote_dispatches_by_provider(self):
        self._s3_settings()
        with mock.patch("services.image_storage_service.S3Client", FakeS3Client):
            result = self.service().test_remote()
        self.assertTrue(result["ok"])
        self.assertEqual(FakeS3Client.instances, 1)


if __name__ == "__main__":
    unittest.main()

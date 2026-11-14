"""S3 客户端（Cloudflare R2 / S3 兼容）单元测试。

不连真实桶 —— boto3 client 全部 mock 掉，只验证我们自己的那层逻辑：
key 拼接、path-style 配置、错误映射、列举与批量删除。
"""
from __future__ import annotations

import unittest
from unittest import mock

from services.image_storage_service import ImageStorageError, S3Client


def make_settings(**extra):
    settings = {
        "s3_endpoint": "https://acct.r2.cloudflarestorage.com",
        "s3_region": "auto",
        "s3_bucket": "my-images",
        "s3_access_key_id": "AK",
        "s3_secret_access_key": "SK",
        "s3_path_style": True,
        "remote_root_path": "chatgpt2api/images",
        "public_base_url": "",
    }
    settings.update(extra)
    return settings


class S3ClientKeyTests(unittest.TestCase):
    def test_key_includes_remote_root_path(self):
        client = S3Client.__new__(S3Client)
        client.root_path = "chatgpt2api/images"
        self.assertEqual(
            client.key("2026/11/14/abc.png"),
            "chatgpt2api/images/2026/11/14/abc.png",
        )

    def test_key_without_root_path(self):
        client = S3Client.__new__(S3Client)
        client.root_path = ""
        self.assertEqual(client.key("2026/11/14/abc.png"), "2026/11/14/abc.png")

    def test_key_rejects_path_traversal(self):
        client = S3Client.__new__(S3Client)
        client.root_path = "chatgpt2api/images"
        with self.assertRaises(Exception):
            client.key("../../etc/passwd")


class S3ClientConfigTests(unittest.TestCase):
    """boto3 client 的构造参数 —— R2 的两个坑都在这里。"""

    def _build(self, **extra):
        settings = make_settings(**extra)
        with mock.patch("boto3.client") as boto_client:
            S3Client(settings)
        return boto_client.call_args.kwargs

    def test_uses_path_style_addressing(self):
        """R2 必须 path-style —— 默认的 virtual-host style 会找不到桶。"""
        kwargs = self._build()
        self.assertEqual(kwargs["config"].s3["addressing_style"], "path")

    def test_virtual_host_style_can_be_opted_in(self):
        kwargs = self._build(s3_path_style=False)
        self.assertEqual(kwargs["config"].s3["addressing_style"], "virtual")

    def test_region_defaults_to_auto(self):
        kwargs = self._build(s3_region="")
        self.assertEqual(kwargs["region_name"], "auto")

    def test_endpoint_and_credentials_passed_through(self):
        kwargs = self._build()
        self.assertEqual(kwargs["endpoint_url"], "https://acct.r2.cloudflarestorage.com")
        self.assertEqual(kwargs["aws_access_key_id"], "AK")
        self.assertEqual(kwargs["aws_secret_access_key"], "SK")

    def test_sigv4_signature(self):
        kwargs = self._build()
        self.assertEqual(kwargs["config"].signature_version, "s3v4")


class S3ClientOperationTests(unittest.TestCase):
    def setUp(self):
        self.client = S3Client.__new__(S3Client)
        self.client.endpoint = "https://acct.r2.cloudflarestorage.com"
        self.client.region = "auto"
        self.client.bucket = "my-images"
        self.client.root_path = "chatgpt2api/images"
        self.client.client = mock.MagicMock()

    def test_put_sends_correct_bucket_key_and_content_type(self):
        self.client.put("2026/11/14/a.png", b"payload", content_type="image/gif")

        kwargs = self.client.client.put_object.call_args.kwargs
        self.assertEqual(kwargs["Bucket"], "my-images")
        self.assertEqual(kwargs["Key"], "chatgpt2api/images/2026/11/14/a.png")
        self.assertEqual(kwargs["ContentType"], "image/gif")
        self.assertEqual(kwargs["Body"], b"payload")

    def test_put_raises_storage_error_on_failure(self):
        self.client.client.put_object.side_effect = RuntimeError("AccessDenied")
        with self.assertRaises(ImageStorageError) as ctx:
            self.client.put("2026/11/14/a.png", b"payload")
        self.assertIn("AccessDenied", str(ctx.exception))

    def test_get_reads_body(self):
        self.client.client.get_object.return_value = {"Body": mock.MagicMock(**{"read.return_value": b"bytes"})}
        self.assertEqual(self.client.get("2026/11/14/a.png"), b"bytes")

    def test_get_raises_storage_error_on_failure(self):
        self.client.client.get_object.side_effect = RuntimeError("NoSuchKey")
        with self.assertRaises(ImageStorageError):
            self.client.get("2026/11/14/a.png")

    def test_delete_removes_object(self):
        self.assertTrue(self.client.delete("2026/11/14/a.png"))
        kwargs = self.client.client.delete_object.call_args.kwargs
        self.assertEqual(kwargs["Key"], "chatgpt2api/images/2026/11/14/a.png")

    def test_list_objects_normalizes_last_modified(self):
        import datetime

        modified = datetime.datetime(2026, 11, 14, 3, 0, 0)
        paginator = mock.MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "chatgpt2api/images/a.png", "LastModified": modified, "Size": 10}]}
        ]
        self.client.client.get_paginator.return_value = paginator

        items = self.client.list_objects()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["key"], "chatgpt2api/images/a.png")
        self.assertEqual(items[0]["size"], 10)
        self.assertAlmostEqual(items[0]["last_modified"], modified.timestamp(), places=3)

    def test_list_objects_uses_root_prefix(self):
        paginator = mock.MagicMock()
        paginator.paginate.return_value = []
        self.client.client.get_paginator.return_value = paginator

        self.client.list_objects()
        kwargs = paginator.paginate.call_args.kwargs
        self.assertEqual(kwargs["Prefix"], "chatgpt2api/images/")
        self.assertEqual(kwargs["Bucket"], "my-images")

    def test_delete_keys_batches(self):
        keys = [f"chatgpt2api/images/{i}.png" for i in range(1500)]
        deleted = self.client.delete_keys(keys)
        self.assertEqual(deleted, 1500)
        # 1000 个一批
        self.assertEqual(self.client.client.delete_objects.call_count, 2)

    def test_delete_keys_empty(self):
        self.assertEqual(self.client.delete_keys([]), 0)
        self.client.client.delete_objects.assert_not_called()


class S3ClientTestTests(unittest.TestCase):
    def setUp(self):
        self.client = S3Client.__new__(S3Client)
        self.client.endpoint = "https://acct.r2.cloudflarestorage.com"
        self.client.region = "auto"
        self.client.bucket = "my-images"
        self.client.root_path = "chatgpt2api/images"
        self.client.client = mock.MagicMock()

    def test_test_probes_bucket_and_object_roundtrip(self):
        result = self.client.test()
        self.assertTrue(result["ok"])
        self.assertEqual(result["bucket"], "my-images")
        self.client.client.head_bucket.assert_called_once()
        self.client.client.put_object.assert_called_once()
        self.client.client.delete_object.assert_called_once()

    def test_test_requires_endpoint(self):
        self.client.endpoint = ""
        result = self.client.test()
        self.assertFalse(result["ok"])
        self.assertIn("Endpoint", result["error"])

    def test_test_requires_bucket(self):
        self.client.bucket = ""
        result = self.client.test()
        self.assertFalse(result["ok"])
        self.assertIn("bucket", result["error"])

    def test_test_rejects_bad_endpoint_scheme(self):
        self.client.endpoint = "ftp://x.example.com"
        result = self.client.test()
        self.assertFalse(result["ok"])
        self.assertIn("invalid", result["error"])

    def test_test_reports_failure(self):
        self.client.client.head_bucket.side_effect = RuntimeError("InvalidAccessKeyId")
        result = self.client.test()
        self.assertFalse(result["ok"])
        self.assertIn("InvalidAccessKeyId", result["error"])


if __name__ == "__main__":
    unittest.main()

"""图片存储配置：旧格式兼容、密钥脱敏、按协议校验。

重点是**老配置不能因为这次改造而失效** —— 用户的 config.json 里
存的还是 mode="webdav" 这种老写法。
"""
from __future__ import annotations

import unittest

from services.config import (
    SECRET_MASK,
    _mask_image_storage_secrets,
    _normalize_image_storage_settings,
    _validate_image_storage_settings,
)


def normalize(value, existing=None):
    return _normalize_image_storage_settings(value, existing=existing)


class LegacyModeMappingTests(unittest.TestCase):
    """老配置 → 新配置（mode × provider 正交化）。"""

    def test_legacy_webdav_mode_becomes_remote(self):
        result = normalize({
            "enabled": True,
            "mode": "webdav",
            "webdav_url": "https://dav.example.com",
            "webdav_password": "pw",
        })
        self.assertEqual(result["mode"], "remote")
        self.assertEqual(result["provider"], "webdav")

    def test_legacy_both_mode_keeps_webdav_provider(self):
        """老配置 mode=both 表示「本机 + WebDAV」—— 不能被误判成 S3。"""
        result = normalize({
            "enabled": True,
            "mode": "both",
            "webdav_url": "https://dav.example.com",
            "webdav_password": "pw",
        })
        self.assertEqual(result["mode"], "both")
        self.assertEqual(result["provider"], "webdav")

    def test_legacy_both_without_webdav_fields_defaults_to_s3(self):
        result = normalize({"enabled": True, "mode": "both"})
        self.assertEqual(result["mode"], "both")
        self.assertEqual(result["provider"], "s3")

    def test_legacy_webdav_root_path_maps_to_remote_root_path(self):
        result = normalize({
            "enabled": True,
            "mode": "webdav",
            "webdav_root_path": "my/dir",
        })
        self.assertEqual(result["remote_root_path"], "my/dir")

    def test_disabled_forces_local_mode(self):
        result = normalize({"enabled": False, "mode": "remote", "provider": "s3"})
        self.assertEqual(result["mode"], "local")

    def test_unknown_mode_falls_back_to_local(self):
        result = normalize({"enabled": True, "mode": "wat"})
        self.assertEqual(result["mode"], "local")

    def test_new_mode_remote_keeps_s3_provider(self):
        result = normalize({"enabled": True, "mode": "remote", "provider": "s3"})
        self.assertEqual(result["mode"], "remote")
        self.assertEqual(result["provider"], "s3")


class SecretMaskingTests(unittest.TestCase):
    def test_mask_hides_secret_but_keeps_access_key_id(self):
        """Access Key ID 是标识符不是密钥，要明文显示。"""
        settings = normalize({
            "enabled": True, "mode": "remote", "provider": "s3",
            "s3_access_key_id": "AKIA-REAL",
            "s3_secret_access_key": "SK-REAL",
            "webdav_password": "PW",
        })
        masked = _mask_image_storage_secrets(settings)
        self.assertEqual(masked["s3_access_key_id"], "AKIA-REAL")
        self.assertEqual(masked["s3_secret_access_key"], SECRET_MASK)
        self.assertEqual(masked["webdav_password"], SECRET_MASK)

    def test_mask_leaves_empty_values_alone(self):
        settings = normalize({"enabled": False, "mode": "local"})
        masked = _mask_image_storage_secrets(settings)
        self.assertEqual(masked["s3_secret_access_key"], "")
        self.assertEqual(masked["webdav_password"], "")

    def test_placeholder_secret_falls_back_to_existing(self):
        """前端回传 *** 时要沿用库里的真值，不能把密钥冲掉。"""
        prior = normalize({
            "enabled": True, "mode": "remote", "provider": "s3",
            "s3_access_key_id": "AK", "s3_secret_access_key": "SK-REAL",
        })
        result = normalize(
            {"enabled": True, "mode": "remote", "provider": "s3", "s3_secret_access_key": SECRET_MASK},
            existing=prior,
        )
        self.assertEqual(result["s3_secret_access_key"], "SK-REAL")

    def test_changed_secret_is_taken_as_is(self):
        prior = normalize({"s3_secret_access_key": "SK-OLD"})
        result = normalize({"s3_secret_access_key": "SK-NEW"}, existing=prior)
        self.assertEqual(result["s3_secret_access_key"], "SK-NEW")

    def test_empty_secret_falls_back_to_existing(self):
        prior = normalize({"webdav_password": "PW-REAL"})
        result = normalize({"webdav_password": ""}, existing=prior)
        self.assertEqual(result["webdav_password"], "PW-REAL")


class ValidationTests(unittest.TestCase):
    def test_disabled_skips_validation(self):
        _validate_image_storage_settings(normalize({"enabled": False}))

    def test_local_mode_skips_validation(self):
        _validate_image_storage_settings(normalize({"enabled": True, "mode": "local"}))

    def test_webdav_requires_url_and_password(self):
        settings = normalize({"enabled": True, "mode": "remote", "provider": "webdav"})
        with self.assertRaises(ValueError):
            _validate_image_storage_settings(settings)
        settings = normalize({
            "enabled": True, "mode": "remote", "provider": "webdav",
            "webdav_url": "https://dav.example.com",
        })
        with self.assertRaises(ValueError):
            _validate_image_storage_settings(settings)

    def test_s3_requires_all_credentials(self):
        base = {"enabled": True, "mode": "remote", "provider": "s3"}
        with self.assertRaises(ValueError):
            _validate_image_storage_settings(normalize(base))
        with self.assertRaises(ValueError):
            _validate_image_storage_settings(normalize({**base, "s3_endpoint": "https://x"}))
        with self.assertRaises(ValueError):
            _validate_image_storage_settings(normalize({**base, "s3_endpoint": "https://x", "s3_bucket": "b"}))
        with self.assertRaises(ValueError):
            _validate_image_storage_settings(
                normalize({**base, "s3_endpoint": "https://x", "s3_bucket": "b", "s3_access_key_id": "AK"})
            )

    def test_s3_valid_config_passes(self):
        settings = normalize({
            "enabled": True, "mode": "remote", "provider": "s3",
            "s3_endpoint": "https://acct.r2.cloudflarestorage.com",
            "s3_bucket": "b", "s3_access_key_id": "AK", "s3_secret_access_key": "SK",
        })
        _validate_image_storage_settings(settings)

    def test_s3_endpoint_must_be_http(self):
        settings = normalize({
            "enabled": True, "mode": "remote", "provider": "s3",
            "s3_endpoint": "ftp://x", "s3_bucket": "b",
            "s3_access_key_id": "AK", "s3_secret_access_key": "SK",
        })
        with self.assertRaises(ValueError):
            _validate_image_storage_settings(settings)


class RetentionTests(unittest.TestCase):
    def test_retention_defaults_to_one_day(self):
        self.assertEqual(normalize({})["remote_retention_days"], 1)

    def test_retention_is_clamped(self):
        self.assertEqual(normalize({"remote_retention_days": 0})["remote_retention_days"], 1)
        self.assertEqual(normalize({"remote_retention_days": 999999})["remote_retention_days"], 3650)

    def test_legacy_hours_converted_to_days(self):
        """老配置是小时 —— 换算成天，不能丢掉用户的设置。"""
        self.assertEqual(normalize({"remote_retention_hours": 24})["remote_retention_days"], 1)
        self.assertEqual(normalize({"remote_retention_hours": 48})["remote_retention_days"], 2)
        # 不足一天向上取整，至少 1 天
        self.assertEqual(normalize({"remote_retention_hours": 30})["remote_retention_days"], 2)
        self.assertEqual(normalize({"remote_retention_hours": 1})["remote_retention_days"], 1)

    def test_legacy_hours_from_existing_are_used(self):
        prior = normalize({"remote_retention_hours": 72})
        self.assertEqual(normalize({}, existing=prior)["remote_retention_days"], 3)

    def test_days_take_priority_over_legacy_hours(self):
        prior = normalize({"remote_retention_hours": 72})
        result = normalize({"remote_retention_days": 5}, existing=prior)
        self.assertEqual(result["remote_retention_days"], 5)

    def test_cleanup_disabled_by_default(self):
        self.assertFalse(normalize({})["remote_cleanup_enabled"])

    def test_fallback_disabled_by_default(self):
        self.assertFalse(normalize({})["remote_cleanup_fallback"])

    def test_path_style_defaults_to_true(self):
        """R2 必须 path-style —— 默认就要是对的。"""
        self.assertTrue(normalize({})["s3_path_style"])

    def test_region_defaults_to_auto(self):
        self.assertEqual(normalize({})["s3_region"], "auto")


if __name__ == "__main__":
    unittest.main()

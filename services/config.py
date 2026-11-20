from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import math
import os
import sys
from pathlib import Path
import time

from services.storage.base import StorageBackend

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
VERSION_FILE = BASE_DIR / "VERSION"
BACKUP_STATE_FILE = DATA_DIR / "backup_state.json"

# 密钥脱敏占位符：返回给前端时用它替掉真实密钥；
# 保存时如果传回来的还是它，说明用户没改这一项 → 沿用库里已有的真值。
SECRET_MASK = "***"

DEFAULT_BACKUP_INCLUDE = {
    "config": True,
    "cpa": True,
    "sub2api": True,
    "logs": True,
    "image_tasks": True,
    "accounts_snapshot": True,
    "auth_keys_snapshot": True,
    "images": False,
}

DEFAULT_IMAGE_STORAGE = {
    "enabled": False,
    # 本地开关：local=只存本机 / remote=只存远端 / both=两边都存
    "mode": "local",
    # 远端协议：webdav / s3（Cloudflare R2 等 S3 兼容服务）
    "provider": "s3",
    # ── S3 / Cloudflare R2 ──
    "s3_endpoint": "",          # R2: https://<AccountID>.r2.cloudflarestorage.com
    "s3_region": "auto",        # R2 固定 auto
    "s3_bucket": "",
    "s3_access_key_id": "",
    "s3_secret_access_key": "",
    "s3_path_style": True,      # R2 必须 path-style
    # ── WebDAV（兼容保留）──
    "webdav_url": "",
    "webdav_username": "",
    "webdav_password": "",
    # ── 通用 ──
    "remote_root_path": "chatgpt2api/images",  # 远端目录（对象 key 前缀），两个协议共用
    "public_base_url": "",                     # 公开访问前缀，留空则走本应用 /images 代理
    # ── 远端过期清理 ──
    "remote_cleanup_enabled": False,           # 总开关：开启后自动在桶上配生命周期规则
    "remote_retention_days": 1,               # 远端对象保留天数（Lifecycle Rule 只能按天）
    "remote_cleanup_fallback": False,         # 额外用应用侧定时扫描兜底（默认不需要）
    # 内部字段：上次已应用的规则指纹，用来判断是否需要重新下发
    "remote_lifecycle_applied": "",
}

DEFAULT_CHAT_COMPLETION_CACHE = {
    "enabled": True,
    "ttl_seconds": 60,
    "max_entries": 256,
    "dedupe_inflight": True,
    "stream_cache": True,
    "normalize_messages": True,
    "drop_adjacent_duplicates": True,
    "drop_assistant_history": False,
}

# 号池自动补充守护（见 services/auto_register_guardian.py）
DEFAULT_AUTO_REGISTER = {
    "enabled": False,
    "threshold": 10,            # 可用账号数低于此值触发注册
    # 剩余额度阈值（0 = 不启用这个维度）。与号池管理页「剩余额度」同口径。
    # 判定是「或」关系：账号数达标但额度不达标 → 同样触发。
    "quota_threshold": 0,
    "target_batch": 20,         # 每次注册的目标成功数
    "check_interval_minute": 30,  # 检查间隔（分钟）
    "concurrency": 3,           # 触发的批量注册并发数
    "mail_source": "",          # 触发的批量注册用哪个邮箱来源（留空 = 用「邮箱配置」里选的）
    "register_options": {},     # 传给批量控制器的额外参数
    "cooldown_minute": 30,       # 触发后的冷却期（分钟）
    # worker 级重试次数（不含首次尝试）。守护触发的注册走的是同一套
    # 批量控制器，出口 IP 不通时同样需要重试 —— 否则一次 CF 拦截就
    # 白烧一批付费邮箱然后收工。
    "max_retries": 3,
    # 入池绑什么代理：none / register / static。
    # ⚠️ 默认 static —— 补进来的号要长期用，绝不能带注册用的动态代理
    #    （出口 IP 每次变 → 账号更容易触发风控 + 代理流量被耗光）。
    "pool_proxy_mode": "static",
    # 「注册使用代理」：注册时从代理池里挑哪一类代理用。
    # dynamic（只动态）/ static（只静态）/ mixed（混合随机，默认）。
    # ⚠️ 与「入池绑什么代理」是两件独立的事：这里选 static 时，
    #    补进来的号会沿用注册时那条静态代理（出口保持一致）。
    "register_proxy_mode": "mixed",
}

DEFAULT_PROXY_RUNTIME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

DEFAULT_PROXY_RUNTIME = {
    "enabled": False,
    "egress_mode": "direct",
    "proxy_url": "",
    "resource_proxy_url": "",
    "skip_ssl_verify": False,
    "reset_session_status_codes": [403],
    "clearance": {
        "enabled": False,
        "mode": "none",
        "cf_cookies": "",
        "cf_clearance": "",
        "user_agent": DEFAULT_PROXY_RUNTIME_USER_AGENT,
        "browser": "chrome",
        "flaresolverr_url": "",
        "timeout_sec": 60,
        "refresh_interval": 3600,
        "warm_up_on_start": False,
    },
}

DEFAULT_THIRD_PARTY_APPS = {
    "infinite_canvas": {
        "enabled": False,
        "url": "https://canvas.best",
    },
}

DEFAULT_IMAGE_MODELS = [
    # 2.6 三兄弟都留：裸名是上游目录里出现的名字，-flare/-sunburst 是
    # 作者 v1.9.5 加的两个变体。模型列表是「内置 + 上游目录」合并的，
    # 多一个名字不会冲突，真不存在也不会被误选。
    "gpt-image-2.6",
    "gpt-image-2.6-sunburst",
    "gpt-image-2.6-flare",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "gpt-image-2",
    "gpt-5-5-thinking",
    "gpt-5-5",
    "gpt-5-3",
]
DEFAULT_IMAGE_MODEL = "gpt-image-2"


def _normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _normalize_positive_int(value: object, default: int, minimum: int = 0) -> int:
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        normalized = default
    return max(minimum, normalized)


def _model_entries(value: object) -> list[object]:
    if isinstance(value, str):
        return value.replace(",", "\n").splitlines()
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []


def _normalize_image_model_list(value: object) -> list[str]:
    normalized: list[str] = []
    for item in _model_entries(value):
        model = str(item or "").strip().lower()
        if model and model not in normalized:
            normalized.append(model)
    return normalized


def _normalize_custom_image_models(value: object) -> list[str]:
    builtin = set(DEFAULT_IMAGE_MODELS)
    return [model for model in _normalize_image_model_list(value) if model not in builtin]


def _image_models_with_cache(cache: object, legacy_custom_models: object = None) -> list[str]:
    """合并内置模型列表与上游拉取的缓存。

    ⚠️ 内置模型**永远保留**，缓存只负责"追加"而不是"覆盖"。

    为什么：上游目录不一定同步得这么快。官方刚发的新模型（比如
    gpt-image-2.6）在 /backend-api/models 里可能还没出现，或者服务器上的
    config.json 里存着**旧版本**写入的缓存（不含新模型）—— 如果让缓存
    覆盖内置，用户就选不到刚加进去的新模型，只能等下一次刷新成功。
    保留内置可以保证"代码里声明支持的模型"始终可选。

    合并顺序：内置在前（保持稳定顺序），上游新发现的在后。

    ⚠️ 作者 v1.9.5 的实现是「有缓存就直接 return 内置+缓存」——
       那样**用户自定义模型（legacy）会被整段丢弃**（用户在设置里加的
       模型凭空消失）。所以这里保留三合一合并：内置 + 缓存 + 自定义。
    """
    cached = _normalize_image_model_list(cache)
    legacy = _normalize_custom_image_models(legacy_custom_models)

    models: list[str] = []
    for item in [*DEFAULT_IMAGE_MODELS, *cached, *legacy]:
        model = str(item or "").strip().lower()
        if model and model not in models:
            models.append(model)
    return models


def _normalize_backup_include(value: object) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    normalized = dict(DEFAULT_BACKUP_INCLUDE)
    for key in normalized:
        normalized[key] = _normalize_bool(source.get(key), normalized[key])
    return normalized


def _normalize_backup_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), False),
        "provider": "cloudflare_r2",
        "account_id": str(source.get("account_id") or "").strip(),
        "access_key_id": str(source.get("access_key_id") or "").strip(),
        "secret_access_key": str(source.get("secret_access_key") or "").strip(),
        "bucket": str(source.get("bucket") or "").strip(),
        "prefix": str(source.get("prefix") or "backups").strip().strip("/") or "backups",
        "interval_minutes": _normalize_positive_int(source.get("interval_minutes"), 360, 1),
        "rotation_keep": _normalize_positive_int(source.get("rotation_keep"), 10, 0),
        "encrypt": _normalize_bool(source.get("encrypt"), False),
        "passphrase": str(source.get("passphrase") or "").strip(),
        "include": _normalize_backup_include(source.get("include")),
    }


def _normalize_backup_state(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "last_started_at": str(source.get("last_started_at") or "").strip() or None,
        "last_finished_at": str(source.get("last_finished_at") or "").strip() or None,
        "last_status": str(source.get("last_status") or "idle").strip() or "idle",
        "last_error": str(source.get("last_error") or "").strip() or None,
        "last_object_key": str(source.get("last_object_key") or "").strip() or None,
    }


_IMAGE_STORAGE_MODES = {"local", "remote", "both"}
_IMAGE_STORAGE_PROVIDERS = {"webdav", "s3"}
# 远端对象保留天数上限（10 年）—— 防止手滑填个 999999 把桶当成永久存储。
_IMAGE_STORAGE_MAX_RETENTION_DAYS = 3650


def _looks_like_legacy_webdav(source: dict, prior: dict) -> bool:
    """判断一份配置是不是「老格式的 WebDAV 配置」。

    判据是任意一个 WebDAV 字段有值 —— 老配置必然带着 webdav_url 或
    webdav_root_path，即使当时只填了远端目录。
    """
    for key in ("webdav_url", "webdav_username", "webdav_password", "webdav_root_path"):
        if str(source.get(key) or "").strip():
            return True
        if str(prior.get(key) or "").strip():
            return True
    return False


def _normalize_image_storage_settings(
    value: object,
    existing: dict[str, object] | None = None,
) -> dict[str, object]:
    """归一化图片存储配置。

    这里同时承担两件事：

    1. **旧配置兼容** —— 老的 ``mode`` 把「本地开关」和「远端协议」混成了一个枚举
       （local / webdav / both）。现在拆成两个正交字段：
       ``mode``（local|remote|both）× ``provider``（webdav|s3）。
       老配置 ``mode="webdav"`` → ``mode="remote" + provider="webdav"``，
       ``mode="both"`` → ``mode="both" + provider="webdav"``。
       用户不用改任何文件。

    2. **密钥占位还原** —— 前端拿到的是脱敏值 ``***``。保存时如果传回来的还是
       ``***`` 或空串，说明用户没改这一项，要沿用库里已有的真值，
       否则一次无关的保存就会把密钥冲成 ``***``（见 ``existing`` 参数）。
    """
    source = value if isinstance(value, dict) else {}
    prior = existing if isinstance(existing, dict) else {}

    raw_mode = str(source.get("mode") or "local").strip().lower()
    provider = str(source.get("provider") or "").strip().lower()

    if raw_mode in {"webdav", "s3"}:
        # 老格式：mode 本身就是协议名
        provider = provider or raw_mode
        mode = "remote"
    elif raw_mode == "both" and not provider:
        # 老配置的 "both" = 本机 + WebDAV。provider 缺失时靠 WebDAV 字段推断，
        # 否则会把老用户的远端协议误判成 S3（配置里还留着 webdav_* 却用不上）。
        provider = "webdav" if _looks_like_legacy_webdav(source, prior) else "s3"
        mode = "both"
    else:
        mode = raw_mode if raw_mode in _IMAGE_STORAGE_MODES else "local"
        provider = provider if provider in _IMAGE_STORAGE_PROVIDERS else "s3"

    if provider not in _IMAGE_STORAGE_PROVIDERS:
        provider = "s3"

    enabled = _normalize_bool(source.get("enabled"), False)
    if not enabled:
        mode = "local"

    def _secret(key: str) -> str:
        """密钥类字段：``***``/空 → 沿用库里的真值。

        只对**真正的密钥**生效。``s3_access_key_id`` 是标识符（Cloudflare 官方
        也不视为机密），要明文显示，否则用户每次打开设置页都看不到自己的 Key ID。
        """
        incoming = str(source.get(key) or "").strip()
        if incoming in {"", SECRET_MASK}:
            return str(prior.get(key) or "").strip()
        return incoming

    def _int(key: str, low: int, high: int, default: int) -> int:
        try:
            raw = int(source.get(key, default))
        except (TypeError, ValueError):
            raw = default
        return max(low, min(high, raw))

    # 保留时长统一按「天」。
    # 兼容老配置（当时是小时）：按 24 小时/天向上取整，至少 1 天 —— 不丢用户设置。
    retention_days = source.get("remote_retention_days")
    if retention_days in (None, ""):
        retention_days = prior.get("remote_retention_days")
    if retention_days in (None, ""):
        legacy_hours = source.get("remote_retention_hours") or prior.get("remote_retention_hours")
        try:
            legacy_days = int(legacy_hours) / 24 if legacy_hours not in (None, "") else 0
        except (TypeError, ValueError):
            legacy_days = 0
        retention_days = (
            int(DEFAULT_IMAGE_STORAGE["remote_retention_days"])
            if legacy_days <= 0
            else math.ceil(legacy_days)
        )
    try:
        retention_days = int(retention_days)
    except (TypeError, ValueError):
        retention_days = int(DEFAULT_IMAGE_STORAGE["remote_retention_days"])
    retention_days = max(1, min(_IMAGE_STORAGE_MAX_RETENTION_DAYS, retention_days))

    # 远端目录：老配置存在 webdav_root_path，新字段是 remote_root_path（两协议共用）。
    # ⚠️ incoming 的两个字段都要排在 prior 前面 —— 否则配置一旦被归一化保存过，
    #    prior.remote_root_path 会把老配置里的 webdav_root_path 顶掉。
    root_path = str(
        source.get("remote_root_path")
        or source.get("webdav_root_path")
        or prior.get("remote_root_path")
        or prior.get("webdav_root_path")
        or DEFAULT_IMAGE_STORAGE["remote_root_path"]
    ).strip().strip("/")

    return {
        "enabled": enabled,
        "mode": mode,
        "provider": provider,
        "s3_endpoint": str(source.get("s3_endpoint") or prior.get("s3_endpoint") or "").strip().rstrip("/"),
        "s3_region": str(source.get("s3_region") or prior.get("s3_region") or DEFAULT_IMAGE_STORAGE["s3_region"]).strip() or "auto",
        "s3_bucket": str(source.get("s3_bucket") or prior.get("s3_bucket") or "").strip(),
        "s3_access_key_id": str(source.get("s3_access_key_id") or prior.get("s3_access_key_id") or "").strip(),
        "s3_secret_access_key": _secret("s3_secret_access_key"),
        "s3_path_style": _normalize_bool(source.get("s3_path_style"), True),
        "webdav_url": str(source.get("webdav_url") or prior.get("webdav_url") or "").strip().rstrip("/"),
        "webdav_username": str(source.get("webdav_username") or prior.get("webdav_username") or "").strip(),
        "webdav_password": _secret("webdav_password"),
        "remote_root_path": root_path or str(DEFAULT_IMAGE_STORAGE["remote_root_path"]),
        "public_base_url": str(source.get("public_base_url") or prior.get("public_base_url") or "").strip().rstrip("/"),
        "remote_cleanup_enabled": _normalize_bool(source.get("remote_cleanup_enabled"), False),
        "remote_cleanup_fallback": _normalize_bool(source.get("remote_cleanup_fallback"), False),
        "remote_retention_days": retention_days,
        "remote_lifecycle_applied": str(source.get("remote_lifecycle_applied") or prior.get("remote_lifecycle_applied") or "").strip(),
    }


def _mask_image_storage_secrets(settings: dict[str, object]) -> dict[str, object]:
    """把密钥字段替换成 ``***``，用于返回给前端。

    只做展示层脱敏 —— 内部读取（``get_image_storage_settings``）拿到的仍是真值。
    """
    masked = dict(settings)
    for key in ("s3_secret_access_key", "webdav_password"):
        if masked.get(key):
            masked[key] = SECRET_MASK
    return masked


def _normalize_auto_register_settings(value: object) -> dict[str, object]:
    """归一化「号池自动补充」配置。

    数值字段做上下界钳制，非法值退回默认，避免写错配置导致
    「每秒触发一次注册」这类失控行为。
    """
    source = value if isinstance(value, dict) else {}
    default = DEFAULT_AUTO_REGISTER

    def _int(key: str, low: int, high: int) -> int:
        try:
            raw = int(source.get(key, default[key]))
        except (TypeError, ValueError):
            raw = int(default[key])
        return max(low, min(high, raw))

    options = source.get("register_options")
    return {
        "enabled": _normalize_bool(source.get("enabled"), default["enabled"]),
        "threshold": _int("threshold", 0, 100000),
        "target_batch": _int("target_batch", 1, 10000),
        "check_interval_minute": _int("check_interval_minute", 1, 10080),
        "concurrency": _int("concurrency", 1, 20),
        "mail_source": str(source.get("mail_source") or "").strip(),
        "register_options": options if isinstance(options, dict) else {},
        "cooldown_minute": _int("cooldown_minute", 0, 10080),
        "max_retries": _int("max_retries", 0, 20),
        # 剩余额度阈值（0 = 不启用这个维度）
        "quota_threshold": _int("quota_threshold", 0, 100000000),
        # 入池代理模式：none / register / static（其它值退回默认）
        "pool_proxy_mode": _normalize_pool_proxy_mode(
            source.get("pool_proxy_mode"), str(default["pool_proxy_mode"])
        ),
        # 「注册使用代理」：dynamic / static / mixed（其它值退回默认）
        "register_proxy_mode": _normalize_register_proxy_mode(
            source.get("register_proxy_mode"), str(default["register_proxy_mode"])
        ),
    }


def _normalize_register_proxy_mode(value: object, fallback: str = "mixed") -> str:
    """归一化「注册使用代理」模式。

    dynamic → 注册时只挑动态代理（每次连接换出口 IP）
    static  → 注册时只挑静态代理（固定出口 IP）
    mixed   → 混合随机（池子里两类都能用）
    """
    text = str(value or "").strip().lower()
    if text in {"dynamic", "static", "mixed"}:
        return text
    return fallback


def _normalize_pool_proxy_mode(value: object, fallback: str = "static") -> str:
    """归一化「入池代理模式」。

    none    → 不带代理入池
    register→ 沿用注册时的代理（必须是登记过的静态代理）
    static  → 从静态代理池分配
    """
    text = str(value or "").strip().lower()
    if text in {"none", "register", "static"}:
        return text
    return fallback


def _normalize_chat_completion_cache_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), DEFAULT_CHAT_COMPLETION_CACHE["enabled"]),
        "ttl_seconds": _normalize_positive_int(
            source.get("ttl_seconds"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["ttl_seconds"]),
            0,
        ),
        "max_entries": _normalize_positive_int(
            source.get("max_entries"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["max_entries"]),
            1,
        ),
        "dedupe_inflight": _normalize_bool(
            source.get("dedupe_inflight"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["dedupe_inflight"]),
        ),
        "stream_cache": _normalize_bool(
            source.get("stream_cache"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["stream_cache"]),
        ),
        "normalize_messages": _normalize_bool(
            source.get("normalize_messages"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["normalize_messages"]),
        ),
        "drop_adjacent_duplicates": _normalize_bool(
            source.get("drop_adjacent_duplicates"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_adjacent_duplicates"]),
        ),
        "drop_assistant_history": _normalize_bool(
            source.get("drop_assistant_history"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_assistant_history"]),
        ),
    }


def _normalize_status_codes(value: object) -> list[int]:
    items = value if isinstance(value, list) else DEFAULT_PROXY_RUNTIME["reset_session_status_codes"]
    normalized: list[int] = []
    for item in items:
        if isinstance(item, bool):
            continue
        try:
            status = int(item)
        except (OverflowError, TypeError, ValueError):
            continue
        if 100 <= status <= 599 and status not in normalized:
            normalized.append(status)
    if not normalized:
        return list(DEFAULT_PROXY_RUNTIME["reset_session_status_codes"])
    return normalized


def _normalize_proxy_runtime_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    default_clearance = DEFAULT_PROXY_RUNTIME["clearance"]
    clearance_source = source.get("clearance") if isinstance(source.get("clearance"), dict) else {}

    egress_mode = str(source.get("egress_mode") or DEFAULT_PROXY_RUNTIME["egress_mode"]).strip().lower()
    if egress_mode not in {"direct", "single_proxy"}:
        egress_mode = str(DEFAULT_PROXY_RUNTIME["egress_mode"])

    clearance_mode = str(clearance_source.get("mode") or default_clearance["mode"]).strip().lower()
    if clearance_mode not in {"none", "manual", "flaresolverr"}:
        clearance_mode = str(default_clearance["mode"])

    user_agent = str(clearance_source.get("user_agent") or default_clearance["user_agent"]).strip()
    browser = str(clearance_source.get("browser") or default_clearance["browser"]).strip()

    existing_clearance_cookies = str(source.get("_existing_cf_cookies") or "").strip()
    existing_cf_clearance = str(source.get("_existing_cf_clearance") or "").strip()
    cf_cookies = str(clearance_source.get("cf_cookies") or "").strip()
    cf_clearance = str(clearance_source.get("cf_clearance") or "").strip()
    if not cf_cookies and _normalize_bool(clearance_source.get("has_cf_cookies"), False):
        cf_cookies = existing_clearance_cookies
    if not cf_clearance and _normalize_bool(clearance_source.get("has_cf_clearance"), False):
        cf_clearance = existing_cf_clearance

    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_PROXY_RUNTIME["enabled"])),
        "egress_mode": egress_mode,
        "proxy_url": str(source.get("proxy_url") or "").strip(),
        "resource_proxy_url": str(source.get("resource_proxy_url") or "").strip(),
        "skip_ssl_verify": _normalize_bool(
            source.get("skip_ssl_verify"),
            bool(DEFAULT_PROXY_RUNTIME["skip_ssl_verify"]),
        ),
        "reset_session_status_codes": _normalize_status_codes(source.get("reset_session_status_codes")),
        "clearance": {
            "enabled": _normalize_bool(clearance_source.get("enabled"), bool(default_clearance["enabled"])),
            "mode": clearance_mode,
            "cf_cookies": cf_cookies,
            "cf_clearance": cf_clearance,
            "user_agent": user_agent or str(default_clearance["user_agent"]),
            "browser": browser or str(default_clearance["browser"]),
            "flaresolverr_url": str(clearance_source.get("flaresolverr_url") or "").strip(),
            "timeout_sec": _normalize_positive_int(
                clearance_source.get("timeout_sec"),
                int(default_clearance["timeout_sec"]),
                1,
            ),
            "refresh_interval": _normalize_positive_int(
                clearance_source.get("refresh_interval"),
                int(default_clearance["refresh_interval"]),
                60,
            ),
            "warm_up_on_start": _normalize_bool(
                clearance_source.get("warm_up_on_start"),
                bool(default_clearance["warm_up_on_start"]),
            ),
        },
    }


def _normalize_third_party_apps_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    canvas_source = source.get("infinite_canvas") if isinstance(source.get("infinite_canvas"), dict) else {}
    return {
        "infinite_canvas": {
            "enabled": _normalize_bool(canvas_source.get("enabled"), False),
            "url": str(canvas_source.get("url") or DEFAULT_THIRD_PARTY_APPS["infinite_canvas"]["url"]).strip(),
        },
    }


def _validate_image_storage_settings(settings: dict[str, object]) -> None:
    """按远端协议分别校验。

    只有真正要用远端（enabled 且 mode != local）时才要求填凭据 ——
    关掉远端存储后残留的半截配置不该阻止用户保存别的设置。
    """
    if not _normalize_bool(settings.get("enabled"), False):
        return
    mode = str(settings.get("mode") or "local").strip().lower()
    if mode == "local":
        return

    provider = str(settings.get("provider") or "s3").strip().lower()
    if provider == "webdav":
        if not str(settings.get("webdav_url") or "").strip():
            raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV URL")
        if not str(settings.get("webdav_password") or "").strip():
            raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV 密码")
        return

    # provider == "s3"
    endpoint = str(settings.get("s3_endpoint") or "").strip()
    if not endpoint:
        raise ValueError("启用 S3 图片存储后必须填写 Endpoint")
    if not endpoint.startswith(("http://", "https://")):
        raise ValueError("S3 Endpoint 必须以 http:// 或 https:// 开头")
    if not str(settings.get("s3_bucket") or "").strip():
        raise ValueError("启用 S3 图片存储后必须填写 Bucket 名称")
    if not str(settings.get("s3_access_key_id") or "").strip():
        raise ValueError("启用 S3 图片存储后必须填写 Access Key ID")
    if not str(settings.get("s3_secret_access_key") or "").strip():
        raise ValueError("启用 S3 图片存储后必须填写 Secret Access Key")


@dataclass(frozen=True)
class LoadedSettings:
    auth_key: str
    refresh_account_interval_minute: int


def _normalize_auth_key(value: object) -> str:
    return str(value or "").strip()


def _is_invalid_auth_key(value: object) -> bool:
    return _normalize_auth_key(value) == ""


def _read_json_object(path: Path, *, name: str) -> dict[str, object]:
    if not path.exists():
        return {}
    if path.is_dir():
        print(
            f"Warning: {name} at '{path}' is a directory, ignoring it and falling back to other configuration sources.",
            file=sys.stderr,
        )
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_settings() -> LoadedSettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw_config = _read_json_object(CONFIG_FILE, name="config.json")
    auth_key = _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or raw_config.get("auth-key"))
    if _is_invalid_auth_key(auth_key):
        raise ValueError(
            "❌ auth-key 未设置！\n"
            "请在环境变量 CHATGPT2API_AUTH_KEY 中设置，或者在 config.json 中填写 auth-key。"
        )

    try:
        refresh_interval = int(raw_config.get("refresh_account_interval_minute", 5))
    except (TypeError, ValueError):
        refresh_interval = 5

    return LoadedSettings(
        auth_key=auth_key,
        refresh_account_interval_minute=refresh_interval,
    )


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        self._storage_backend: StorageBackend | None = None
        if _is_invalid_auth_key(self.auth_key):
            raise ValueError(
                "❌ auth-key 未设置！\n"
                "请按以下任意一种方式解决：\n"
                "1. 在 Render 的 Environment 变量中添加：\n"
                "   CHATGPT2API_AUTH_KEY = your_real_auth_key\n"
                "2. 或者在 config.json 中填写：\n"
                '   "auth-key": "your_real_auth_key"'
            )

    def _load(self) -> dict[str, object]:
        return _read_json_object(self.path, name="config.json")

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @property
    def auth_key(self) -> str:
        return _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or self.data.get("auth-key"))

    @property
    def accounts_file(self) -> Path:
        return DATA_DIR / "accounts.json"

    @property
    def refresh_account_interval_minute(self) -> int:
        try:
            return int(self.data.get("refresh_account_interval_minute", 5))
        except (TypeError, ValueError):
            return 5

    @property
    def image_retention_days(self) -> int:
        try:
            return max(1, int(self.data.get("image_retention_days", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def image_poll_timeout_secs(self) -> int:
        try:
            return max(1, int(self.data.get("image_poll_timeout_secs", 120)))
        except (TypeError, ValueError):
            return 120

    @property
    def image_poll_interval_secs(self) -> float:
        try:
            return max(0.5, float(self.data.get("image_poll_interval_secs", 10.0)))
        except (TypeError, ValueError):
            return 10.0

    @property
    def image_poll_initial_wait_secs(self) -> float:
        """Image generation upstream takes ~30s; polling immediately wastes requests
        and trips a transient 429. Default 10s gives the conversation document time
        to commit before the first poll."""
        try:
            return max(0.0, float(self.data.get("image_poll_initial_wait_secs", 10.0)))
        except (TypeError, ValueError):
            return 10.0

    @property
    def image_timeout_retry_secs(self) -> int:
        try:
            return max(1, int(self.data.get("image_timeout_retry_secs", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def image_account_concurrency(self) -> int:
        try:
            return max(1, int(self.data.get("image_account_concurrency", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def image_quota_wait_secs(self) -> float:
        """请求过来时没有「剩余额度 > 在途」的账号，最多排队等多久（秒）。

        账号的额度是「成功一次 -1」，在途期间额度还没扣。所以一个额度只剩 1
        的号如果同时接两个请求，第二个必然被上游以额度不足拒掉。选号因此要求
        `剩余额度 > 在途数`。

        没有这样的号时有两种选择：
          · 立刻返回 429 —— 并发稍高就会误杀（前一个请求几秒后就结束）
          · 排队等一会儿，等某个在途请求结束再重新判断

        这里就是「等多久」的上限。0 = 不排队（立刻 429）。
        默认 15 秒：足够覆盖「前一个请求失败/成功立刻让位」，又不至于把客户端
        和线程池一起拖死。
        """
        try:
            return max(0.0, float(self.data.get("image_quota_wait_secs", 15)))
        except (TypeError, ValueError):
            return 15.0

    @property
    def custom_image_models(self) -> list[str]:
        """兼容旧配置；新版本由自动拉取的 image_models_cache 管理模型列表。"""
        return _normalize_custom_image_models(self.data.get("custom_image_models"))

    @property
    def image_models(self) -> list[str]:
        return _image_models_with_cache(
            self.data.get("image_models_cache"),
            self.data.get("custom_image_models"),
        )

    @property
    def image_models_source(self) -> str:
        if _normalize_image_model_list(self.data.get("image_models_cache")):
            return str(self.data.get("image_models_source") or "upstream").strip() or "upstream"
        return "fallback"

    @property
    def image_models_updated_at(self) -> str:
        return str(self.data.get("image_models_updated_at") or "").strip()

    @property
    def default_image_model(self) -> str:
        model = str(self.data.get("default_image_model") or DEFAULT_IMAGE_MODEL).strip().lower()
        return model if model in self.image_models else DEFAULT_IMAGE_MODEL

    @property
    def image_parallel_generation(self) -> bool:
        value = self.data.get("image_parallel_generation", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_settle_enabled(self) -> bool:
        """图片二次确认机制：找到 file_ids 后等待一段时间再次确认。"""
        value = self.data.get("image_settle_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_check_before_hit_enabled(self) -> bool:
        """先check再hit：通过轮询确认 file_ids 存在后再返回，而非仅依赖 SSE 事件。"""
        value = self.data.get("image_check_before_hit_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_remove_conversation_after_result(self) -> bool:
        """出图成功后异步隐藏 ChatGPT 本地对话记录。"""
        value = self.data.get("image_remove_conversation_after_result", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_settle_secs(self) -> float:
        """二次确认等待时间（秒）。"""
        try:
            return max(0.5, float(self.data.get("image_settle_secs", 2.0)))
        except (TypeError, ValueError):
            return 2.0

    @property
    def auto_remove_invalid_accounts(self) -> bool:
        value = self.data.get("auto_remove_invalid_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def strip_ui_markup(self) -> bool:
        """把上游的组件标记（<box>/<row>/<AsyncImage> 等）降级为 Markdown。"""
        value = self.data.get("strip_ui_markup", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    @property
    def stream_variant_policy(self) -> str:
        """上游并行生成多条消息时的处理策略。

        - `extend`：只补「已发长度」之后的差额，两条消息的内容都不丢（默认）
        - `follow_first`：本轮只跟第一条消息，其余整条忽略（绝不重复，但可能被截断）
        - `merge`：和已发文本做内容对齐拼接（可能重复）
        """
        value = str(self.data.get("stream_variant_policy") or "").strip().lower()
        return value if value in {"extend", "follow_first", "merge"} else "extend"

    @property
    def upstream_variant_purpose(self) -> str:
        """上游 conversation 请求里的 `variant_purpose`。

        `comparison_implicit` 会让 ChatGPT 为同一轮并行生成两条回答，这也是流式输出里
        出现两份内容的原因。配成空字符串表示不发送这个字段。
        """
        value = self.data.get("upstream_variant_purpose")
        if value is None:
            return "comparison_implicit"
        return str(value).strip()

    @property
    def auto_remove_rate_limited_accounts(self) -> bool:
        value = self.data.get("auto_remove_rate_limited_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_relogin_after_refresh(self) -> bool:
        value = self.data.get("auto_relogin_after_refresh", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    # ── 生成失败自动标异常 ──
    # 号池里有些号额度没用完，但一直生成失败（既浪费调度又拖慢整体）。
    # 这里按**累计失败次数**（账号的 fail 字段）定时巡检，超过阈值就标成「异常」。
    # 默认关闭：升级后不会突然把历史失败多的号批量标掉，需要用户手动打开。

    @property
    def fail_guard_enabled(self) -> bool:
        value = self.data.get("fail_guard_enabled", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def fail_guard_threshold(self) -> int:
        """累计失败次数达到这个值就标异常（默认 20）。"""
        try:
            return max(1, min(100000, int(self.data.get("fail_guard_threshold", 20))))
        except (TypeError, ValueError):
            return 20

    @property
    def fail_guard_interval_minute(self) -> int:
        """巡检间隔（分钟，默认 60）。"""
        try:
            return max(1, min(7 * 24 * 60, int(self.data.get("fail_guard_interval_minute", 60))))
        except (TypeError, ValueError):
            return 60

    @property
    def log_levels(self) -> list[str]:
        levels = self.data.get("log_levels")
        if not isinstance(levels, list):
            return []
        allowed = {"debug", "info", "warning", "error"}
        return [level for item in levels if (level := str(item or "").strip().lower()) in allowed]

    @property
    def sensitive_words(self) -> list[str]:
        words = self.data.get("sensitive_words")
        return [word for item in words if (word := str(item or "").strip())] if isinstance(words, list) else []

    @property
    def ai_review(self) -> dict[str, object]:
        value = self.data.get("ai_review")
        return value if isinstance(value, dict) else {}

    @property
    def global_system_prompt(self) -> str:
        return str(self.data.get("global_system_prompt") or "").strip()

    @property
    def images_dir(self) -> Path:
        path = DATA_DIR / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def image_thumbnails_dir(self) -> Path:
        path = DATA_DIR / "image_thumbnails"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_old_images(self) -> int:
        cutoff = time.time() - self.image_retention_days * 86400
        removed = 0
        for path in self.images_dir.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        for path in sorted((p for p in self.images_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
        return removed

    @property
    def base_url(self) -> str:
        return str(
            os.getenv("CHATGPT2API_BASE_URL")
            or self.data.get("base_url")
            or ""
        ).strip().rstrip("/")

    @property
    def app_version(self) -> str:
        try:
            value = VERSION_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "0.0.0"
        return value or "0.0.0"

    def get(self) -> dict[str, object]:
        data = dict(self.data)
        data["refresh_account_interval_minute"] = self.refresh_account_interval_minute
        data["image_retention_days"] = self.image_retention_days
        data["image_poll_timeout_secs"] = self.image_poll_timeout_secs
        data["image_poll_interval_secs"] = self.image_poll_interval_secs
        data["image_poll_initial_wait_secs"] = self.image_poll_initial_wait_secs
        data["image_timeout_retry_secs"] = self.image_timeout_retry_secs
        data["image_account_concurrency"] = self.image_account_concurrency
        data["image_quota_wait_secs"] = self.image_quota_wait_secs
        data["custom_image_models"] = self.custom_image_models
        data["image_models"] = self.image_models
        data["image_models_source"] = self.image_models_source
        data["image_models_updated_at"] = self.image_models_updated_at
        data["default_image_model"] = self.default_image_model
        data["image_parallel_generation"] = self.image_parallel_generation
        data["image_remove_conversation_after_result"] = self.image_remove_conversation_after_result
        data["auto_remove_invalid_accounts"] = self.auto_remove_invalid_accounts
        data["auto_remove_rate_limited_accounts"] = self.auto_remove_rate_limited_accounts
        data["fail_guard_enabled"] = self.fail_guard_enabled
        data["fail_guard_threshold"] = self.fail_guard_threshold
        data["fail_guard_interval_minute"] = self.fail_guard_interval_minute
        data["auto_relogin_after_refresh"] = self.auto_relogin_after_refresh
        data["log_levels"] = self.log_levels
        data["sensitive_words"] = self.sensitive_words
        data["ai_review"] = self.ai_review
        data["global_system_prompt"] = self.global_system_prompt
        data["backup"] = self.get_backup_settings()
        data["image_storage"] = _mask_image_storage_secrets(self.get_image_storage_settings())
        data["chat_completion_cache"] = self.get_chat_completion_cache_settings()
        data["proxy_runtime"] = self.get_public_proxy_runtime_settings()
        data["third_party_apps"] = self.get_third_party_apps_settings()
        data.pop("auth-key", None)
        return data

    def get_proxy_settings(self) -> str:
        return str(self.data.get("proxy") or "").strip()

    def get_proxy_runtime_settings(self) -> dict[str, object]:
        return _normalize_proxy_runtime_settings(self.data.get("proxy_runtime"))

    def get_public_proxy_runtime_settings(self) -> dict[str, object]:
        runtime = copy.deepcopy(self.get_proxy_runtime_settings())
        clearance = runtime.get("clearance") if isinstance(runtime.get("clearance"), dict) else {}
        if isinstance(clearance, dict):
            cf_cookies = str(clearance.get("cf_cookies") or "").strip()
            cf_clearance = str(clearance.get("cf_clearance") or "").strip()
            clearance["cf_cookies"] = ""
            clearance["cf_clearance"] = ""
            clearance["has_cf_cookies"] = bool(cf_cookies)
            clearance["has_cf_clearance"] = bool(cf_clearance)
        return runtime

    def get_third_party_apps_settings(self) -> dict[str, object]:
        return _normalize_third_party_apps_settings(self.data.get("third_party_apps"))

    def update(self, data: dict[str, object]) -> dict[str, object]:
        incoming = dict(data or {})
        next_data = dict(self.data)
        next_data.update(incoming)
        if "image_models_cache" in incoming:
            next_data["image_models_cache"] = _normalize_image_model_list(next_data.get("image_models_cache"))
        if "custom_image_models" in next_data:
            next_data["custom_image_models"] = _normalize_custom_image_models(next_data.get("custom_image_models"))
        next_data.pop("image_models", None)
        default_image_model = str(next_data.get("default_image_model") or DEFAULT_IMAGE_MODEL).strip().lower()
        available_image_models = _image_models_with_cache(
            next_data.get("image_models_cache"),
            next_data.get("custom_image_models"),
        )
        next_data["default_image_model"] = (
            default_image_model if default_image_model in available_image_models else DEFAULT_IMAGE_MODEL
        )
        if "backup" in next_data:
            next_data["backup"] = _normalize_backup_settings(next_data.get("backup"))
        if "image_storage" in next_data:
            # existing 传当前已存的真值：前端回传 *** 时要沿用真密钥，
            # 不能让一次无关的保存把密钥冲成 ***。
            next_data["image_storage"] = _normalize_image_storage_settings(
                next_data.get("image_storage"),
                existing=self.get_image_storage_settings(),
            )
            _validate_image_storage_settings(next_data["image_storage"])
        if "chat_completion_cache" in next_data:
            next_data["chat_completion_cache"] = _normalize_chat_completion_cache_settings(
                next_data.get("chat_completion_cache")
            )
        if "auto_register" in next_data:
            next_data["auto_register"] = _normalize_auto_register_settings(
                next_data.get("auto_register")
            )
        if "third_party_apps" in next_data:
            next_data["third_party_apps"] = _normalize_third_party_apps_settings(next_data.get("third_party_apps"))
        if "proxy_runtime" in next_data:
            incoming_runtime = next_data.get("proxy_runtime")
            if isinstance(incoming_runtime, dict):
                previous_clearance = self.get_proxy_runtime_settings().get("clearance")
                if isinstance(previous_clearance, dict):
                    incoming_runtime = dict(incoming_runtime)
                    incoming_runtime["_existing_cf_cookies"] = previous_clearance.get("cf_cookies")
                    incoming_runtime["_existing_cf_clearance"] = previous_clearance.get("cf_clearance")
            next_data["proxy_runtime"] = _normalize_proxy_runtime_settings(incoming_runtime)
        next_data.pop("backup_state", None)
        self.data = next_data
        self._save()
        return self.get()

    def get_backup_settings(self) -> dict[str, object]:
        return _normalize_backup_settings(self.data.get("backup"))

    def get_image_storage_settings(self) -> dict[str, object]:
        return _normalize_image_storage_settings(self.data.get("image_storage"))

    def set_image_storage_lifecycle_applied(self, fingerprint: str) -> None:
        """记录「已下发到桶上的生命周期规则指纹」。

        这是内部状态，用来判断需不需要重新下发规则 ——
        指纹变了（换桶 / 改保留天数 / 改远端目录）才重写，
        避免每次都发一次 put_bucket_lifecycle_configuration。

        走 _save() 而不是 update()：这是应用内部写回的状态，
        不该触发一遍配置校验（会要求填 S3 凭据）。
        """
        storage = _normalize_image_storage_settings(self.data.get("image_storage"))
        storage["remote_lifecycle_applied"] = str(fingerprint or "").strip()
        next_data = dict(self.data)
        next_data["image_storage"] = storage
        self.data = next_data
        self._save()

    def get_chat_completion_cache_settings(self) -> dict[str, object]:
        return _normalize_chat_completion_cache_settings(self.data.get("chat_completion_cache"))

    def get_auto_register_settings(self) -> dict[str, object]:
        """号池自动补充守护的配置。"""
        return _normalize_auto_register_settings(self.data.get("auto_register"))

    def get_storage_backend(self) -> StorageBackend:
        """获取存储后端实例（单例）"""
        if self._storage_backend is None:
            from services.storage.factory import create_storage_backend
            self._storage_backend = create_storage_backend(DATA_DIR)
        return self._storage_backend


def load_backup_state() -> dict[str, object]:
    return _normalize_backup_state(_read_json_object(BACKUP_STATE_FILE, name="backup_state.json"))


def save_backup_state(state: dict[str, object]) -> dict[str, object]:
    normalized = _normalize_backup_state(state)
    BACKUP_STATE_FILE.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


config = ConfigStore(CONFIG_FILE)

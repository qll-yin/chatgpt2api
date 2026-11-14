from __future__ import annotations

import hashlib
import io
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlparse

from curl_cffi import requests
from fastapi import HTTPException
from PIL import Image

from services.config import DATA_DIR, config
from services.storage import runtime_store as store

logger = logging.getLogger(__name__)

IMAGE_INDEX_FILE = DATA_DIR / "image_index.json"
# .gif 是作者 v1.10.0 加的 GIF 输出支持需要的（按真实字节后缀存盘）。
# ⚠️ 不要把 IMAGE_INDEX_LOCK / json 索引加回来 —— 本项目已经改成 DB 索引了，
#    两套并存会写出两套元数据。
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class ImageStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredImage:
    rel: str
    url: str
    storage: str
    size: int


class RemoteImageClient(Protocol):
    """远端图片存储的统一接口。

    抽出这层是为了让「远端协议」可插拔：WebDAV 与 S3(R2) 各实现一份，
    ``ImageStorageService`` 只按 provider 选实现，不关心底下是哪种协议。
    """

    def put(self, rel: str, payload: bytes, content_type: str = "image/png") -> str: ...
    def get(self, rel: str) -> bytes: ...
    def delete(self, rel: str) -> bool: ...
    def test(self) -> dict[str, object]: ...


def _clean(value: object) -> str:
    return str(value or "").strip()


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_relative_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise HTTPException(status_code=404, detail="image not found")
    parts = Path(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=404, detail="image not found")
    return Path(*parts).as_posix()


def _image_mime_type(payload: bytes) -> str:
    """按真实字节判断图片 MIME，用于落盘后缀和 WebDAV 的 Content-Type。

    上游除了 PNG，还可能在要求 GIF 时直接回一段动图字节，不能一律当成 image/png。
    """
    try:
        with Image.open(io.BytesIO(payload)) as image:
            return Image.MIME.get(image.format or "", "image/png")
    except Exception:
        return "image/png"


def _image_extension(payload: bytes) -> str:
    extension = _image_mime_type(payload).split("/", 1)[-1] or "png"
    if extension == "jpeg":
        return "jpg"
    return extension if extension in {"png", "jpg", "jpeg", "webp", "gif"} else "png"


def _image_dimensions(payload: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            return image.size
    except Exception:
        return None


def _is_image_rel(path: str) -> bool:
    try:
        safe_rel = _safe_relative_path(path)
    except HTTPException:
        return False
    return Path(safe_rel).suffix.lower() in IMAGE_EXTENSIONS


def _local_image_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    root = config.images_dir.resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    return path


class WebDAVClient:
    """WebDAV 远端存储（兼容保留）。

    实现 ``RemoteImageClient`` 协议，行为与改造前完全一致 ——
    远端目录统一读 ``remote_root_path``（老配置里的 ``webdav_root_path``
    在 config 层已经映射过来了）。
    """

    def __init__(self, settings: dict[str, object]):
        self.url = _clean(settings.get("webdav_url")).rstrip("/")
        self.username = _clean(settings.get("webdav_username"))
        self.password = _clean(settings.get("webdav_password"))
        self.root_path = _clean(settings.get("remote_root_path")).strip("/")
        self.session = requests.Session()

    def _auth_kwargs(self) -> dict[str, object]:
        return {"auth": (self.username, self.password)} if self.username or self.password else {}

    def _request(self, method: str, url: str, **kwargs):
        response = self.session.request(method, url, timeout=30, **self._auth_kwargs(), **kwargs)
        if response.status_code >= 400 and not (method == "MKCOL" and response.status_code in {405}):
            raise ImageStorageError(f"WebDAV {method} failed: HTTP {response.status_code}")
        return response

    def remote_url(self, rel: str = "") -> str:
        parts = [part for part in [self.root_path, _safe_relative_path(rel) if rel else ""] if part]
        encoded = "/".join(quote(part, safe="") for item in parts for part in item.split("/") if part)
        return f"{self.url}/{encoded}" if encoded else self.url

    def ensure_dirs(self, rel: str) -> None:
        parts = [part for part in [self.root_path, Path(_safe_relative_path(rel)).parent.as_posix()] if part and part != "."]
        current = self.url
        for item in "/".join(parts).split("/"):
            if not item:
                continue
            current = f"{current}/{quote(item, safe='')}"
            response = self.session.request("MKCOL", current, timeout=30, **self._auth_kwargs())
            if response.status_code in {201, 405}:
                continue
            if response.status_code >= 400:
                raise ImageStorageError(f"WebDAV MKCOL failed: HTTP {response.status_code}")

    def put(self, rel: str, payload: bytes, content_type: str = "image/png") -> str:
        self.ensure_dirs(rel)
        url = self.remote_url(rel)
        self._request("PUT", url, data=payload, headers={"Content-Type": content_type})
        return url

    def get(self, rel: str) -> bytes:
        response = self._request("GET", self.remote_url(rel))
        return bytes(response.content)

    def delete(self, rel: str) -> bool:
        response = self.session.request("DELETE", self.remote_url(rel), timeout=30, **self._auth_kwargs())
        if response.status_code in {200, 202, 204, 404}:
            return response.status_code != 404
        raise ImageStorageError(f"WebDAV DELETE failed: HTTP {response.status_code}")

    def test(self) -> dict[str, object]:
        if not self.url:
            return {"ok": False, "status": 0, "error": "WebDAV URL is required"}
        if urlparse(self.url).scheme not in {"http", "https"}:
            return {"ok": False, "status": 0, "error": "invalid WebDAV URL"}
        test_rel = ".chatgpt2api_webdav_test.txt"
        try:
            self.put(test_rel, b"chatgpt2api webdav test\n", content_type="text/plain")
            self.delete(test_rel)
            return {"ok": True, "status": 200, "error": None}
        except ImageStorageError as exc:
            return {"ok": False, "status": 0, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc) or exc.__class__.__name__}
        finally:
            self.session.close()


class S3Client:
    """S3 兼容远端存储（Cloudflare R2 / MinIO / 阿里云 OSS / AWS S3 …）。

    两个必须写对的点：

    1. **path-style 寻址** —— R2 的 endpoint 是
       ``https://<AccountID>.r2.cloudflarestorage.com``，对象路径是
       ``/<bucket>/<key>``。boto3 默认用 virtual-host style
       （``<bucket>.<account>.r2.cloudflarestorage.com``），R2 对这种解析
       支持不稳，会报 bucket 找不到。所以强制 ``addressing_style="path"``。
    2. **region 固定 ``auto``** —— R2 没有真正的区域概念，但签名里必须带，
       填别的值会直接被拒签（SignatureDoesNotMatch）。

    这两点在 ``_build_client`` 里做成显式配置，用户可以改但默认就是对的。
    """

    PROBE_KEY = ".chatgpt2api_s3_test.txt"

    def __init__(self, settings: dict[str, object]):
        self.endpoint = _clean(settings.get("s3_endpoint")).rstrip("/")
        self.region = _clean(settings.get("s3_region")) or "auto"
        self.bucket = _clean(settings.get("s3_bucket"))
        self.root_path = _clean(settings.get("remote_root_path")).strip("/")
        self.access_key_id = _clean(settings.get("s3_access_key_id"))
        self.secret_access_key = _clean(settings.get("s3_secret_access_key"))
        self.path_style = bool(settings.get("s3_path_style", True))
        self.client = self._build_client()

    def _build_client(self):
        # boto3 是可选依赖：没装时给出明确报错，而不是让整个服务 import 就崩。
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise ImageStorageError(
                "缺少 boto3 依赖，无法使用 S3 存储。请执行：uv sync"
            ) from exc

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint or None,
            region_name=self.region,
            aws_access_key_id=self.access_key_id or None,
            aws_secret_access_key=self.secret_access_key or None,
            config=Config(
                s3={
                    "addressing_style": "path" if self.path_style else "virtual",
                    # R2/S3 兼容服务大多不实现分段上传的 checksum 算法，
                    # 老版本 s3transfer 会算 MD5 导致 SignatureDoesNotMatch。
                    "request_checksum_calculation": "when_required",
                    "response_checksum_validation": "when_required",
                },
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=60,
            ),
        )

    def key(self, rel: str) -> str:
        """相对路径 → 对象 key（含远端目录前缀）。"""
        safe_rel = _safe_relative_path(rel)
        return f"{self.root_path}/{safe_rel}" if self.root_path else safe_rel

    def public_url(self, rel: str) -> str:
        """对象的公开地址（仅在桶公开读时有意义）。"""
        base = _clean(config.get_image_storage_settings().get("public_base_url")).rstrip("/")
        if not base:
            return ""
        root = self.root_path.strip("/")
        safe_rel = _safe_relative_path(rel)
        parts = [part for part in (root, safe_rel) if part]
        return f"{base}/{'/'.join(parts)}"

    def put(self, rel: str, payload: bytes, content_type: str = "image/png") -> str:
        key = self.key(rel)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                ContentType=content_type,
            )
        except Exception as exc:
            raise ImageStorageError(f"S3 PUT failed: {exc}") from exc
        return self.public_url(rel) or f"{self.endpoint}/{self.bucket}/{key}"

    def get(self, rel: str) -> bytes:
        key = self.key(rel)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return bytes(response["Body"].read())
        except Exception as exc:
            raise ImageStorageError(f"S3 GET failed: {exc}") from exc

    def delete(self, rel: str) -> bool:
        key = self.key(rel)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:
            raise ImageStorageError(f"S3 DELETE failed: {exc}") from exc

    def list_objects(self, prefix: str = "") -> list[dict[str, object]]:
        """列出远端对象。

        返回 ``[{"key":..., "last_modified": epoch 秒, "size":...}]``。
        过期清理按对象自己的 LastModified 判断 —— 即使本地索引丢了也能正确清理。
        """
        base = f"{self.root_path}/" if self.root_path else ""
        paginator = self.client.get_paginator("list_objects_v2")
        items: list[dict[str, object]] = []
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=base + prefix):
                for obj in page.get("Contents") or []:
                    modified = obj.get("LastModified")
                    items.append({
                        "key": str(obj.get("Key") or ""),
                        "last_modified": modified.timestamp() if hasattr(modified, "timestamp") else 0.0,
                        "size": int(obj.get("Size") or 0),
                    })
        except Exception as exc:
            raise ImageStorageError(f"S3 LIST failed: {exc}") from exc
        return items

    def delete_keys(self, keys: list[str]) -> int:
        """批量删除对象。返回成功删除的数量。"""
        if not keys:
            return 0
        deleted = 0
        for index in range(0, len(keys), 1000):
            chunk = keys[index:index + 1000]
            try:
                self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": key} for key in chunk], "Quiet": True},
                )
                deleted += len(chunk)
            except Exception as exc:
                raise ImageStorageError(f"S3 batch DELETE failed: {exc}") from exc
        return deleted

    # ── 生命周期规则（R2 的服务端过期删除）───────────────────────────────
    #
    # put_bucket_lifecycle_configuration 是**整桶替换**语义 ——
    # 直接写会把桶上原有的其它规则（备份、日志保留…）全部冲掉。
    # 所以每次写之前先读回来，只替换/删除「我们自己那条」，
    # 别的规则原样保留。

    LIFECYCLE_RULE_ID = "chatgpt2api-expire"

    def get_lifecycle_configuration(self) -> dict[str, object]:
        """读桶上的生命周期规则。桶没配过时返回空列表。"""
        try:
            response = self.client.get_bucket_lifecycle_configuration(Bucket=self.bucket)
        except Exception as exc:
            code = ""
            error = getattr(exc, "response", None)
            if isinstance(error, dict):
                code = str((error.get("Error") or {}).get("Code") or "")
            if code in {"NoSuchLifecycleConfiguration", "NoSuchLifecycleConfigurationError", "404"}:
                return {"rules": []}
            raise ImageStorageError(f"S3 GET lifecycle failed: {exc}") from exc
        return response if isinstance(response, dict) else {"rules": []}

    def _our_lifecycle_rule(self, days: int) -> dict[str, object]:
        prefix = f"{self.root_path}/" if self.root_path else ""
        return {
            "ID": self.LIFECYCLE_RULE_ID,
            "Status": "Enabled",
            "Filter": {"Prefix": prefix},
            "Expiration": {"Days": int(days)},
        }

    @staticmethod
    def _rule_prefix(rule: dict[str, object]) -> str:
        raw = rule.get("Filter")
        if isinstance(raw, dict):
            return str(raw.get("Prefix") or "")
        return str(rule.get("Prefix") or "")

    def put_lifecycle_configuration(self, days: int) -> dict[str, object]:
        """把「超过 N 天自动删除」的生命周期规则写到桶上。

        保留桶上其它规则不动 —— 只替换我们自己那一条（按规则 ID 认）。
        """
        days = max(1, int(days))
        current = self.get_lifecycle_configuration()
        rules = list(current.get("Rules") or [])
        kept = [
            rule for rule in rules
            if isinstance(rule, dict)
            and str(rule.get("ID") or "") != self.LIFECYCLE_RULE_ID
            # 兜底：老版本可能没写 ID，但 prefix 跟我们的一样 —— 也当自己的处理
            and not (not rule.get("ID") and self._rule_prefix(rule).rstrip("/") == self.root_path.rstrip("/"))
        ]
        merged = kept + [self._our_lifecycle_rule(days)]
        try:
            self.client.put_bucket_lifecycle_configuration(
                Bucket=self.bucket,
                LifecycleConfiguration={"Rules": merged},
            )
        except Exception as exc:
            raise ImageStorageError(f"S3 PUT lifecycle failed: {exc}") from exc
        return {"rules": merged, "replaced": len(rules) - len(kept), "kept": len(kept)}

    def remove_lifecycle_configuration(self) -> dict[str, object]:
        """移除「我们自己那条」生命周期规则，桶上其它规则保留。

        桶上只剩我们这一条时，整份配置删掉（R2 对空规则列表不接受）。
        """
        current = self.get_lifecycle_configuration()
        rules = list(current.get("Rules") or [])
        kept = [
            rule for rule in rules
            if isinstance(rule, dict)
            and str(rule.get("ID") or "") != self.LIFECYCLE_RULE_ID
            and not (not rule.get("ID") and self._rule_prefix(rule).rstrip("/") == self.root_path.rstrip("/"))
        ]
        removed = len(rules) - len(kept)
        if not removed:
            return {"removed": 0, "kept": len(kept)}
        try:
            if kept:
                self.client.put_bucket_lifecycle_configuration(
                    Bucket=self.bucket,
                    LifecycleConfiguration={"Rules": kept},
                )
            else:
                self.client.delete_bucket_lifecycle(Bucket=self.bucket)
        except Exception as exc:
            raise ImageStorageError(f"S3 DELETE lifecycle failed: {exc}") from exc
        return {"removed": removed, "kept": len(kept)}

    def test(self) -> dict[str, object]:
        if not self.endpoint:
            return {"ok": False, "status": 0, "error": "S3 Endpoint is required"}
        if not self.bucket:
            return {"ok": False, "status": 0, "error": "S3 bucket is required"}
        if urlparse(self.endpoint).scheme not in {"http", "https"}:
            return {"ok": False, "status": 0, "error": "invalid S3 endpoint"}
        try:
            # 1) 桶存在且凭据有权限
            self.client.head_bucket(Bucket=self.bucket)
            # 2) 写 → 读 → 删，验证完整的对象读写链路
            self.client.put_object(
                Bucket=self.bucket,
                Key=f"{self.root_path}/{self.PROBE_KEY}" if self.root_path else self.PROBE_KEY,
                Body=b"chatgpt2api s3 test\n",
                ContentType="text/plain",
            )
            self.client.get_object(
                Bucket=self.bucket,
                Key=f"{self.root_path}/{self.PROBE_KEY}" if self.root_path else self.PROBE_KEY,
            )["Body"].read()
            self.client.delete_object(
                Bucket=self.bucket,
                Key=f"{self.root_path}/{self.PROBE_KEY}" if self.root_path else self.PROBE_KEY,
            )
            return {
                "ok": True,
                "status": 200,
                "error": None,
                "bucket": self.bucket,
                "endpoint": self.endpoint,
                "region": self.region,
            }
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc) or exc.__class__.__name__}


class ImageStorageService:
    """图片存储服务。

    索引存储：**数据库**（services/storage/runtime_store.py）。

    历史实现用 JSON 文件做索引，每次保存图片都要「读全文件 → 加一条 → 写全文件」，
    并且在保存前调用 `cleanup_old_images()`（rglob 遍历整个图片目录树 + 逐文件 stat）。
    2 万张图时，每保存一张图要多付 2 万次系统调用。

    现在：索引走数据库 UPSERT（单行写）；过期图片清理由
    `services/image_service.py` 的定时器（每 30 分钟）统一负责，
    保存路径不再做目录遍历。
    """

    def __init__(self, index_file: Path = IMAGE_INDEX_FILE):
        self.index_file = index_file

    def settings(self) -> dict[str, object]:
        return config.get_image_storage_settings()

    def mode(self) -> str:
        return _clean(self.settings().get("mode")) or "local"

    def provider(self) -> str:
        """当前远端协议：webdav / s3。"""
        provider = _clean(self.settings().get("provider")).lower()
        return provider if provider in {"webdav", "s3"} else "s3"

    def _remote_client(self) -> RemoteImageClient:
        """按 provider 造一个远端客户端。"""
        settings = self.settings()
        if self.provider() == "webdav":
            return WebDAVClient(settings)
        return S3Client(settings)

    def _public_url(self, rel: str, base_url: str | None = None) -> str:
        """对外图片地址。

        填了「公开访问前缀」且**远端存储实际生效**时，返回远端直链
        （Cloudflare R2 的 r2.dev 或自定义域）；否则走本应用的 /images 代理。

        远端目录由程序自动拼接 —— 用户只填域名就行，不用把目录再写一遍。
        老配置里 public_base_url 已经含了远端目录的，这里不重复拼。

        ⚠️ 「公开访问前缀」只在远端生效时才用。
        关闭远端存储（mode=local）后图片只存在本地，此时若还返回 R2 直链
        会直接 404 —— 图在本地、链接指向云端。这是原作者项目就有的问题：
        用户在设置页关掉远端存储，却忘了清掉公开前缀，历史配置里那行
        还留着，于是每张新图的 URL 都指向一个根本没写入过的 R2 桶。
        """
        settings = self.settings()
        safe_rel = _safe_relative_path(rel)
        local_fallback = f"{(base_url or config.base_url).rstrip('/')}/images/{safe_rel}"

        # 远端没生效 → 一律走本地地址（public_base_url 此时是无意义的残留配置）
        mode = _clean(settings.get("mode")) or "local"
        if mode not in {"remote", "both"}:
            return local_fallback

        public_base_url = _clean(settings.get("public_base_url")).rstrip("/")
        if not public_base_url:
            return local_fallback

        root = _clean(settings.get("remote_root_path")).strip("/")
        prefix = public_base_url
        if root and prefix != root and not prefix.endswith(f"/{root}"):
            prefix = f"{prefix}/{root}"
        return f"{prefix}/{safe_rel}"

    def make_relative_path(self, image_data: bytes) -> str:
        file_hash = hashlib.md5(image_data).hexdigest()
        filename = f"{int(time.time())}_{file_hash}.{_image_extension(image_data)}"
        relative_dir = Path(time.strftime("%Y"), time.strftime("%m"), time.strftime("%d"))
        return f"{relative_dir.as_posix()}/{filename}"

    def save(self, image_data: bytes, base_url: str | None = None) -> StoredImage:
        # ⚠️ 这里原本调用 config.cleanup_old_images()——每次保存图片都遍历
        # 整个图片目录树做 stat，是"一天几万张"下的主要 CPU/IO 开销之一。
        # 过期清理已由 image_service 的 30 分钟定时器统一负责，此处不再重复。
        rel = self.make_relative_path(image_data)
        mode = self.mode()
        if mode not in {"local", "remote", "both"}:
            mode = "local"

        stored_local = False
        stored_remote = False
        remote_url = ""
        remote_error = ""
        remote_uploaded_ts = 0.0

        def _write_local() -> None:
            nonlocal stored_local
            path = _local_image_path(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(image_data)
            stored_local = True

        if mode in {"local", "both"}:
            _write_local()

        if mode in {"remote", "both"}:
            try:
                client = self._remote_client()
                remote_url = client.put(rel, image_data, content_type=_image_mime_type(image_data))
                stored_remote = True
                remote_uploaded_ts = time.time()
            except Exception as exc:  # noqa: BLE001
                # 容错：远端（WebDAV / R2）挂了不能把生图一起带崩 ——
                # 降级存到本地，链接照常返回，只是这条图没进远端桶。
                remote_error = str(exc) or exc.__class__.__name__
                logger.warning(
                    "[image-storage] 远端上传失败(%s)，降级存本地: %s",
                    self.provider(), remote_error,
                )
                if not stored_local:
                    _write_local()

        dimensions = _image_dimensions(image_data)
        item = {
            "rel_path": rel,
            "name": Path(rel).name,
            "date": "-".join(rel.split("/")[:3]),
            "size": len(image_data),
            "created_at": _now_iso(),
            "created_ts": time.time(),
            "storage": "both" if stored_local and stored_remote else ("remote" if stored_remote else "local"),
            "local": stored_local,
            "remote": stored_remote,
            "remote_provider": self.provider() if stored_remote else "",
            "remote_url": remote_url,
            "remote_error": remote_error,
            "remote_uploaded_at": _now_iso() if remote_uploaded_ts else "",
            "remote_uploaded_ts": remote_uploaded_ts,
        }
        if dimensions:
            item["width"], item["height"] = dimensions
        store.upsert_image_record(item)
        return StoredImage(rel=rel, url=self._public_url(rel, base_url), storage=str(item["storage"]), size=len(image_data))

    def get_bytes(self, rel: str) -> bytes:
        safe_rel = _safe_relative_path(rel)
        if not _is_image_rel(safe_rel):
            raise HTTPException(status_code=404, detail="image not found")
        path = _local_image_path(safe_rel)
        if path.is_file():
            return path.read_bytes()
        item = store.get_image_record(safe_rel) or {}
        if item.get("remote") or item.get("webdav"):
            return self._remote_client().get(safe_rel)
        raise HTTPException(status_code=404, detail="image not found")

    def exists(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        if not _is_image_rel(safe_rel):
            return False
        if _local_image_path(safe_rel).is_file():
            return True
        item = store.get_image_record(safe_rel) or {}
        return bool(item.get("remote") or item.get("webdav"))

    def has_local(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        return _is_image_rel(safe_rel) and _local_image_path(safe_rel).is_file()

    def list_items(self, base_url: str, start_date: str = "", end_date: str = "") -> list[dict[str, object]]:
        # 1) 扫描磁盘，把索引里没有的文件补录进来（首次运行/手工放入的图片）
        root = config.images_dir
        pending: list[dict[str, object]] = []
        for path in root.rglob("*"):
            if not path.is_file() or not _is_image_rel(path.name):
                continue
            rel = path.relative_to(root).as_posix()
            if store.get_image_record(rel) is not None:
                continue
            try:
                stat = path.stat()
                dimensions = _image_dimensions(path.read_bytes())
            except Exception:
                dimensions = None
                stat = path.stat()
            pending.append({
                "rel_path": rel,
                "name": path.name,
                "date": "/".join(rel.split("/")[:3]) if len(rel.split("/")) >= 4 else datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d"),
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "created_ts": stat.st_mtime,
                "storage": "local",
                "local": True,
                "remote": False,
                "remote_provider": "",
                "webdav": False,
                **({"width": dimensions[0], "height": dimensions[1]} if dimensions else {}),
            })
        store.bulk_upsert_image_records(pending)

        # 2) 从数据库读回全部记录，按日期过滤
        items: list[dict[str, object]] = []
        stale: list[str] = []
        for item in store.list_image_records(start_date, end_date):
            rel = str(item.get("rel") or "")
            if not rel or not _is_image_rel(rel):
                continue
            local = _local_image_path(rel).is_file()
            remote = bool(item.get("remote") or item.get("webdav"))
            if not local and not remote:
                # 文件已被外部删除（手工清理 / 磁盘故障）→ 索引记录失效，顺手清掉
                stale.append(rel)
                continue
            items.append({
                **item,
                "rel": rel,
                "path": rel,
                "url": self._public_url(rel, base_url),
            })
        for rel in stale:
            store.delete_image_record(rel)
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return items

    def delete(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        removed = False
        path = _local_image_path(safe_rel)
        if path.is_file():
            path.unlink()
            removed = True
        item = store.get_image_record(safe_rel) or {}
        if item.get("remote") or item.get("webdav"):
            try:
                removed = self._remote_client().delete(safe_rel) or removed
            except ImageStorageError:
                if not removed:
                    raise
        store.delete_image_record(safe_rel)
        return removed

    def sync_all(self) -> dict[str, int]:
        """把本地已有图片补传到远端。"""
        settings = self.settings()
        mode = _clean(settings.get("mode"))
        if mode not in {"remote", "both"}:
            raise ImageStorageError("远端图片存储未启用")
        uploaded = 0
        skipped = 0
        failed = 0
        client = self._remote_client()
        provider = self.provider()
        for path in sorted(config.images_dir.rglob("*")):
            if not path.is_file() or not _is_image_rel(path.name):
                continue
            rel = path.relative_to(config.images_dir).as_posix()
            item = store.get_image_record(rel) or {}
            if item.get("remote"):
                skipped += 1
                continue
            try:
                payload = path.read_bytes()
                remote_url = client.put(rel, payload, content_type=_image_mime_type(payload))
                dimensions = _image_dimensions(payload)
                stat = path.stat()
                store.upsert_image_record({
                    **item,
                    "rel_path": rel,
                    "name": path.name,
                    "date": "/".join(rel.split("/")[:3]) if len(rel.split("/")) >= 4 else datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d"),
                    "size": len(payload),
                    "created_at": str(item.get("created_at") or datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")),
                    "created_ts": float(item.get("created_ts") or stat.st_mtime),
                    "storage": "both",
                    "local": True,
                    "remote": True,
                    "remote_provider": provider,
                    "remote_url": remote_url,
                    "remote_error": "",
                    "remote_uploaded_at": _now_iso(),
                    "remote_uploaded_ts": time.time(),
                    **({"width": dimensions[0], "height": dimensions[1]} if dimensions else {}),
                })
                uploaded += 1
            except Exception:
                failed += 1
        return {"uploaded": uploaded, "skipped": skipped, "failed": failed}

    def cleanup_remote(self) -> dict[str, object]:
        """应用侧兜底清理：按保留时长扫描并删除远端对象。

        ⚠️ 这是**兜底**，不是主路径。主路径是 ``apply_remote_lifecycle()``
        —— 把生命周期规则下发到桶上，由 R2 服务端删除（零扫描、零成本、
        应用停机也照清）。这里每 30 分钟扫一遍全桶，桶大了很贵也很慢，
        所以默认关闭（``remote_cleanup_fallback``）。

        判据用的是**对象自己的 LastModified**（R2/S3 返回的上传时间），
        不是本地索引里的创建时间 —— 索引可能丢、也可能和实际对不上，
        用对象自身的时间戳才准。
        """
        settings = self.settings()
        if not bool(settings.get("remote_cleanup_fallback")):
            return {"enabled": False, "scanned": 0, "deleted": 0, "reason": "应用侧兜底清理未开启"}
        mode = _clean(settings.get("mode"))
        if mode not in {"remote", "both"}:
            return {"enabled": True, "scanned": 0, "deleted": 0, "reason": "远端存储未启用"}
        if self.provider() != "s3":
            return {"enabled": True, "scanned": 0, "deleted": 0, "reason": "当前远端协议不支持列举清理"}
        try:
            days = max(1, int(settings.get("remote_retention_days") or 1))
        except (TypeError, ValueError):
            days = 1
        cutoff = time.time() - days * 86400
        try:
            client = self._remote_client()
            objects = client.list_objects()
            expired = [
                str(item.get("key") or "")
                for item in objects
                if str(item.get("key") or "") and float(item.get("last_modified") or 0) < cutoff
            ]
            deleted = client.delete_keys(expired)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[image-storage] 远端清理失败: %s", exc)
            return {"enabled": True, "scanned": 0, "deleted": 0, "error": str(exc)}
        if deleted:
            logger.info("[image-storage] 远端兜底清理：删除 %s 个过期对象（保留 %s 天）", deleted, days)
        return {"enabled": True, "scanned": len(objects), "deleted": deleted, "retention_days": days}

    def _lifecycle_fingerprint(self) -> str:
        """期望的生命周期规则指纹。变了才需要重新下发。"""
        settings = self.settings()
        return "|".join([
            self.provider(),
            _clean(settings.get("s3_bucket")),
            _clean(settings.get("remote_root_path")),
            str(int(settings.get("remote_retention_days") or 1)),
            "on" if bool(settings.get("remote_cleanup_enabled")) else "off",
        ])

    def apply_remote_lifecycle(self, force: bool = False) -> dict[str, object]:
        """把生命周期规则下发到远端桶（R2 服务端执行过期删除）。

        这是远端过期清理的**主路径**：规则写进桶后，R2 自己会按天删除
        过期对象 —— 不占应用资源、没有扫描延迟，应用停机也照清。

        指纹没变就跳过（``force=True`` 强制重下）。
        关掉「远端过期清理」时会把我们那条规则从桶上摘掉
        （只摘自己那一条，桶上其它规则不动）。
        """
        settings = self.settings()
        mode = _clean(settings.get("mode"))
        applied = _clean(settings.get("remote_lifecycle_applied"))

        if self.provider() != "s3":
            # ⚠️ 从 R2 切到 WebDAV（或别的协议）时，之前下发给 R2 的那条规则
            #    还留在桶上 —— 不摘掉的话，桶会继续按那条规则删图。
            #    指纹带 provider 前缀，正好能识别出「之前是 S3」。
            if applied.startswith("s3|"):
                result = S3Client(settings).remove_lifecycle_configuration()
                config.set_image_storage_lifecycle_applied("")
                logger.info("[image-storage] 远端协议已切换，摘掉 R2 上残留的生命周期规则")
                return {
                    "applied": True,
                    "removed": result.get("removed", 0),
                    "kept_rules": result.get("kept", 0),
                    "note": "远端协议已切换，已清理 R2 上残留的规则",
                }
            return {"applied": False, "reason": "当前远端协议不支持生命周期规则"}

        if mode not in {"remote", "both"}:
            return {"applied": False, "reason": "远端存储未启用"}

        enabled = bool(settings.get("remote_cleanup_enabled"))
        fingerprint = self._lifecycle_fingerprint()

        if not enabled:
            # 关掉了 → 摘掉我们那条规则（之前下发过的才需要摘）
            if not applied and not force:
                return {"applied": False, "reason": "远端过期清理未开启"}
            result = self._remote_client().remove_lifecycle_configuration()
            config.set_image_storage_lifecycle_applied("")
            return {
                "applied": True,
                "removed": result.get("removed", 0),
                "kept_rules": result.get("kept", 0),
            }

        if not force and fingerprint == applied:
            return {"applied": False, "reason": "规则已是最新", "fingerprint": fingerprint}

        days = max(1, int(settings.get("remote_retention_days") or 1))
        client = self._remote_client()
        result = client.put_lifecycle_configuration(days)
        config.set_image_storage_lifecycle_applied(fingerprint)
        logger.info(
            "[image-storage] 已下发生命周期规则：保留 %s 天（桶=%s 前缀=%s，保留其它规则 %s 条）",
            days,
            _clean(settings.get("s3_bucket")),
            _clean(settings.get("remote_root_path")),
            result.get("kept"),
        )
        return {
            "applied": True,
            "days": days,
            "fingerprint": fingerprint,
            "kept_rules": result.get("kept"),
            "note": "R2 通常在过期时间后 24 小时内完成删除",
        }

    def remove_remote_lifecycle(self) -> dict[str, object]:
        """把「我们自己那条」生命周期规则从桶上摘掉（桶上其它规则保留）。"""
        if self.provider() != "s3":
            return {"removed": 0, "reason": "当前远端协议不支持生命周期规则"}
        result = self._remote_client().remove_lifecycle_configuration()
        config.set_image_storage_lifecycle_applied("")
        return {"removed": result.get("removed", 0), "kept_rules": result.get("kept", 0)}

    def get_remote_lifecycle(self) -> dict[str, object]:
        """读桶上当前的规则（给设置页展示）。"""
        settings = self.settings()
        if self.provider() != "s3":
            return {"supported": False, "rules": [], "reason": "当前远端协议不支持生命周期规则"}
        if not _clean(settings.get("s3_bucket")):
            return {"supported": True, "rules": [], "reason": "还没填 Bucket 名称"}
        try:
            current = self._remote_client().get_lifecycle_configuration()
        except Exception as exc:  # noqa: BLE001
            return {"supported": True, "rules": [], "error": str(exc)}
        rules = current.get("Rules") or current.get("rules") or []
        summary = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            expiration = rule.get("Expiration") if isinstance(rule.get("Expiration"), dict) else {}
            summary.append({
                "id": str(rule.get("ID") or ""),
                "status": str(rule.get("Status") or "Enabled"),
                "prefix": self._rule_prefix_of(rule),
                "days": expiration.get("Days"),
            })
        return {
            "supported": True,
            "rules": summary,
            "ours": any(item["id"] == S3Client.LIFECYCLE_RULE_ID for item in summary),
            "fingerprint": self._lifecycle_fingerprint(),
            "applied_fingerprint": _clean(settings.get("remote_lifecycle_applied")),
        }

    @staticmethod
    def _rule_prefix_of(rule: dict[str, object]) -> str:
        raw = rule.get("Filter")
        if isinstance(raw, dict):
            return str(raw.get("Prefix") or "")
        return str(rule.get("Prefix") or "")

    def test_remote(self) -> dict[str, object]:
        """连通性测试（按 provider 分发）。"""
        provider = self.provider()
        if provider == "webdav":
            return WebDAVClient(self.settings()).test()
        return S3Client(self.settings()).test()

    def stats(self) -> dict[str, object]:
        """远端存储概况（给图片管理页展示）。"""
        settings = self.settings()
        mode = _clean(settings.get("mode")) or "local"
        try:
            remote_count = store.count_image_records(remote_only=True)
        except Exception:  # noqa: BLE001
            remote_count = 0
        return {
            "enabled": bool(settings.get("enabled")),
            "mode": mode,
            "provider": self.provider(),
            "remote_enabled": mode in {"remote", "both"},
            "remote_count": remote_count,
            "remote_cleanup_enabled": bool(settings.get("remote_cleanup_enabled")),
            "remote_cleanup_fallback": bool(settings.get("remote_cleanup_fallback")),
            "remote_retention_days": int(settings.get("remote_retention_days") or 0),
            "public_base_url": _clean(settings.get("public_base_url")),
            "remote_root_path": _clean(settings.get("remote_root_path")),
        }

    # 旧接口名保留 —— 外部（api/system.py、前端）还在用
    def test_webdav(self) -> dict[str, object]:
        return self.test_remote()


image_storage_service = ImageStorageService()

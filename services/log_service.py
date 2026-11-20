from __future__ import annotations

import hashlib
import json
import itertools
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from services.config import DATA_DIR
from services.protocol.error_response import anthropic_error_response, openai_error_response
from utils.helper import anthropic_sse_stream, sse_json_stream

LOG_TYPE_CALL = "call"
LOG_TYPE_ACCOUNT = "account"
INTERNAL_RESPONSE_KEYS = {"_account_email", "_conversation_id"}


class LogService:
    """系统日志。

    ⚠️ 历史上日志写在 `data/logs.jsonl`（一行一条），日志页每次都要把整个
    文件读进内存、倒着扫到 200 条。想「查某个账号的生成记录」时，邮箱又埋在
    detail JSON 里，只能逐行反序列化 —— 数据一多就明显变慢。

    现在日志落库（运行态库，和图片任务同一套连接），`account_email` 单独成列
    并建索引。文件保留两件事：
      1. **兜底**：数据库不可用时照旧写文件，不丢日志
      2. **一次性导入**：启动后第一次访问时把老的 logs.jsonl 增量导入（按 id
         upsert，可重复执行），导入进度记在 `.logs_db_imported`（已导入字节数）
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db_ready = False
        self._db_retry_at = 0.0
        self._db_lock = threading.Lock()
        self._import_marker = self.path.parent / ".logs_db_imported"

    # ── 数据库可用性 ──

    def _ensure_db(self) -> bool:
        """确保日志表可用（首次调用时建表 + 导入老文件）。失败则降级到文件。"""
        if self._db_ready:
            return True
        now = time.monotonic()
        if now < self._db_retry_at:
            return False
        with self._db_lock:
            if self._db_ready:
                return True
            if time.monotonic() < self._db_retry_at:
                return False
            try:
                from services.storage import runtime_store

                runtime_store.ensure_tables()
                self._import_legacy_file(runtime_store)
                self._db_ready = True
                return True
            except Exception as exc:  # noqa: BLE001
                # 数据库还没起来 / 连接串错 → 30 秒内不重试，期间写文件
                self._db_retry_at = time.monotonic() + 30
                print(f"[logs] database unavailable, falling back to file: {exc}")
                return False

    def _read_imported_size(self) -> int:
        try:
            return max(0, int(self._import_marker.read_text(encoding="utf-8").strip() or 0))
        except Exception:  # noqa: BLE001
            return 0

    def _import_legacy_file(self, store) -> None:
        """把 logs.jsonl 里尚未导入的部分灌进数据库（按 id upsert，幂等）。"""
        if not self.path.exists():
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        imported = self._read_imported_size()
        if size <= imported:
            return
        items: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as file:
                file.seek(imported)
                for offset, raw_line in enumerate(file):
                    item = self._parse_line(raw_line, imported + offset)
                    if item is not None:
                        items.append(item)
        except Exception as exc:  # noqa: BLE001
            print(f"[logs] import legacy file failed: {exc}")
            return
        if items:
            try:
                store.bulk_insert_logs(items)
            except Exception as exc:  # noqa: BLE001
                print(f"[logs] import legacy file failed: {exc}")
                return
        try:
            self._import_marker.write_text(str(size), encoding="utf-8")
        except OSError:
            pass

    # ── 文件兜底 ──

    @staticmethod
    def _legacy_id(raw_line: str, line_number: int) -> str:
        payload = f"{line_number}:{raw_line}".encode("utf-8", errors="ignore")
        return hashlib.sha1(payload).hexdigest()[:24]

    def _parse_line(self, raw_line: str, line_number: int) -> dict[str, Any] | None:
        try:
            item = json.loads(raw_line)
        except Exception:
            return None
        if not isinstance(item, dict):
            return None
        parsed = dict(item)
        parsed["id"] = str(parsed.get("id") or self._legacy_id(raw_line, line_number))
        return parsed

    @staticmethod
    def _serialize_item(item: dict[str, Any]) -> str:
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    def _append_file(self, item: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            file.write(self._serialize_item(item) + "\n")

    # ── 对外接口 ──

    def add(self, type: str, summary: str = "", detail: dict[str, Any] | None = None, **data: Any) -> None:
        item = {
            "id": uuid4().hex,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": type,
            "summary": summary,
            "detail": detail or data,
        }
        if self._ensure_db():
            try:
                from services.storage import runtime_store

                runtime_store.insert_log(item)
                return
            except Exception as exc:  # noqa: BLE001
                # 写库失败不能让业务崩，也不能丢日志 → 落文件，30 秒后再试数据库
                self._db_ready = False
                self._db_retry_at = time.monotonic() + 30
                print(f"[logs] insert failed, falling back to file: {exc}")
        self._append_file(item)

    def list(
        self,
        type: str = "",
        start_date: str = "",
        end_date: str = "",
        account_email: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if self._ensure_db():
            try:
                from services.storage import runtime_store

                return runtime_store.list_logs(
                    type=type,
                    start_date=start_date,
                    end_date=end_date,
                    account_email=account_email,
                    limit=limit,
                )
            except Exception as exc:  # noqa: BLE001
                self._db_ready = False
                self._db_retry_at = time.monotonic() + 30
                print(f"[logs] query failed, falling back to file: {exc}")
        return self._list_from_file(type, start_date, end_date, account_email, limit)

    def _list_from_file(
        self,
        type: str,
        start_date: str,
        end_date: str,
        account_email: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        email = str(account_email or "").strip().lower()
        items: list[dict[str, Any]] = []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for line_number in range(len(lines) - 1, -1, -1):
            item = self._parse_line(lines[line_number], line_number)
            if item is None:
                continue
            if not self._matches_filters(item, type=type, start_date=start_date, end_date=end_date):
                continue
            if email and email not in str((item.get("detail") or {}).get("account_email") or "").lower():
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _matches_filters(item: dict[str, Any], *, type: str = "", start_date: str = "", end_date: str = "") -> bool:
        t = str(item.get("time") or "")
        day = t[:10]
        if type and item.get("type") != type:
            return False
        if start_date and day < start_date:
            return False
        if end_date and day > end_date:
            return False
        return True

    def delete(self, ids: list[str]) -> dict[str, int]:
        target_ids = {str(item or "").strip() for item in ids if str(item or "").strip()}
        if not target_ids:
            return {"removed": 0}
        if self._ensure_db():
            try:
                from services.storage import runtime_store

                return {"removed": runtime_store.delete_logs(list(target_ids))}
            except Exception as exc:  # noqa: BLE001
                self._db_ready = False
                self._db_retry_at = time.monotonic() + 30
                print(f"[logs] delete failed, falling back to file: {exc}")
        return self._delete_from_file(target_ids)

    def _delete_from_file(self, target_ids: set[str]) -> dict[str, int]:
        if not self.path.exists():
            return {"removed": 0}
        lines = self.path.read_text(encoding="utf-8").splitlines()
        kept_lines: list[str] = []
        removed = 0
        for line_number, raw_line in enumerate(lines):
            item = self._parse_line(raw_line, line_number)
            if item is None:
                kept_lines.append(raw_line)
                continue
            if str(item.get("id") or "") in target_ids:
                removed += 1
                continue
            kept_lines.append(self._serialize_item(item))
        content = "\n".join(kept_lines)
        if content:
            content += "\n"
        self.path.write_text(content, encoding="utf-8")
        return {"removed": removed}

    def export_jsonl_bytes(self) -> bytes:
        """导出成 logs.jsonl 格式（备份用，归档里的路径与格式保持不变）。"""
        if self._ensure_db():
            try:
                from services.storage import runtime_store

                return runtime_store.export_logs_jsonl()
            except Exception as exc:  # noqa: BLE001
                print(f"[logs] export failed, falling back to file: {exc}")
        if not self.path.exists():
            return b""
        return self.path.read_bytes()


log_service = LogService(DATA_DIR / "logs.jsonl")


def _collect_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "url" and isinstance(item, str):
                urls.append(item)
            elif key == "urls" and isinstance(item, list):
                urls.extend(str(url) for url in item if isinstance(url, str))
            else:
                urls.extend(_collect_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_collect_urls(item))
    return urls


def _collect_account_emails(value: object) -> list[str]:
    emails: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"_account_email", "account_email"} and isinstance(item, str) and item.strip():
                emails.append(item.strip())
            else:
                emails.extend(_collect_account_emails(item))
    elif isinstance(value, list):
        for item in value:
            emails.extend(_collect_account_emails(item))
    return emails


def _collect_conversation_ids(value: object) -> list[str]:
    ids: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "_conversation_id" and isinstance(item, str) and item.strip():
                ids.append(item.strip())
            else:
                ids.extend(_collect_conversation_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.extend(_collect_conversation_ids(item))
    return ids


def _strip_internal_response_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_internal_response_fields(item)
            for key, item in value.items()
            if key not in INTERNAL_RESPONSE_KEYS
        }
    if isinstance(value, list):
        return [_strip_internal_response_fields(item) for item in value]
    return value


def _request_excerpt(text: object, limit: int = 1000) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _image_error_response(exc: Exception) -> JSONResponse:
    from services.protocol.conversation import (
        IMAGE_TIMEOUT_MESSAGE,
        is_image_timeout_error,
        public_image_error_message,
    )

    # 生图超时：状态码仍是 502，但对外只说一句「超时了，请重试」。
    # ⚠️ 秒数、config 提示、账号是否被限流都属于内部细节，只写服务端日志。
    if is_image_timeout_error(exc):
        return openai_error_response(
            {
                "error": {
                    "message": IMAGE_TIMEOUT_MESSAGE,
                    "type": "server_error",
                    "param": None,
                    "code": "image_timeout",
                }
            },
            502,
        )

    message = public_image_error_message(str(exc))
    # ⚠️ 不能只匹配 "no available image quota"：带 plan 的文案是
    #    "no available plus image quota"，那样会漏判、变成 502。
    _lower_message = message.lower()
    if "no available" in _lower_message and "image quota" in _lower_message:
        return openai_error_response(
            {
                "error": {
                    "message": "no available image quota",
                    "type": "insufficient_quota",
                    "param": None,
                    "code": "insufficient_quota",
                }
            },
            429,
        )
    if hasattr(exc, "to_openai_error") and hasattr(exc, "status_code"):
        return JSONResponse(status_code=int(exc.status_code), content=exc.to_openai_error())
    return openai_error_response(message, 502)


def _protocol_error_response(exc: Exception, status_code: int, sse: str) -> JSONResponse:
    message = str(exc)
    if sse == "anthropic":
        return anthropic_error_response(message, status_code)
    return openai_error_response(message, status_code)


def _next_item(items):
    try:
        return True, next(items)
    except StopIteration:
        return False, None


@dataclass
class LoggedCall:
    identity: dict[str, object]
    endpoint: str
    model: str
    summary: str
    started: float = field(default_factory=time.time)
    request_text: str = ""
    request_shape: dict[str, int] | None = None

    async def run(self, handler, *args, sse: str = "openai"):
        from services.protocol.conversation import ImageGenerationError

        try:
            result = await run_in_threadpool(handler, *args)
        except ImageGenerationError as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)

        if isinstance(result, dict):
            self.log("调用完成", result)
            response = dict(result)
            response.pop("_account_email", None)
            return response

        sender = anthropic_sse_stream if sse == "anthropic" else sse_json_stream
        try:
            has_first, first = await run_in_threadpool(_next_item, result)
        except ImageGenerationError as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)
        if not has_first:
            self.log("流式调用结束")
            return StreamingResponse(sender(()), media_type="text/event-stream")
        return StreamingResponse(sender(self.stream(itertools.chain([first], result))), media_type="text/event-stream")

    def stream(self, items):
        urls: list[str] = []
        account_emails: list[str] = []
        conversation_ids: list[str] = []
        failed = False
        try:
            for item in items:
                urls.extend(_collect_urls(item))
                account_emails.extend(_collect_account_emails(item))
                conversation_ids.extend(_collect_conversation_ids(item))
                yield _strip_internal_response_fields(item)
        except Exception as exc:
            failed = True
            self.log(
                "流式调用失败",
                status="failed",
                error=str(exc),
                urls=urls,
                account_email=(account_emails[0] if account_emails else getattr(exc, "account_email", "")),
                conversation_id=(conversation_ids[0] if conversation_ids else getattr(exc, "conversation_id", "")),
            )
            if self.endpoint.startswith("/v1/images") and not hasattr(exc, "to_openai_error"):
                from services.protocol.conversation import ImageGenerationError, public_image_error_message

                raise ImageGenerationError(public_image_error_message(str(exc))) from exc
            raise
        finally:
            if not failed:
                self.log("流式调用结束", urls=urls, account_email=account_emails[0] if account_emails else "",
                         conversation_id=conversation_ids[0] if conversation_ids else "")

    def log(self, suffix: str, result: object = None, status: str = "success", error: str = "",
            urls: list[str] | None = None, account_email: str = "", conversation_id: str = "") -> None:
        detail = {
            "key_id": self.identity.get("id"),
            "key_name": self.identity.get("name"),
            "role": self.identity.get("role"),
            "endpoint": self.endpoint,
            "model": self.model,
            "started_at": datetime.fromtimestamp(self.started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_ms": int((time.time() - self.started) * 1000),
            "status": status,
        }
        request_excerpt = _request_excerpt(self.request_text)
        if request_excerpt:
            detail["request_text"] = request_excerpt
        if self.request_shape:
            detail["request_shape"] = self.request_shape
        if error:
            detail["error"] = error
        email = str(account_email or "").strip()
        if not email:
            emails = _collect_account_emails(result)
            email = emails[0] if emails else ""
        if email:
            detail["account_email"] = email
        conv_id = str(conversation_id or "").strip()
        if not conv_id:
            conv_ids = _collect_conversation_ids(result)
            conv_id = conv_ids[0] if conv_ids else ""
        if conv_id:
            detail["conversation_id"] = conv_id
        collected_urls = [*(urls or []), *_collect_urls(result)]
        if collected_urls and not self.endpoint.startswith("/v1/search"):
            detail["urls"] = list(dict.fromkeys(collected_urls))
        log_service.add(LOG_TYPE_CALL, f"{self.summary}{suffix}", detail)

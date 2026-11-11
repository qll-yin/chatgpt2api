from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from services.account_service import account_service
from services.config import DATA_DIR
from services.content_filter import request_text
from services.log_service import LOG_TYPE_CALL, log_service
from services.openai_backend_api import EDITABLE_FILE_MODEL, OpenAIBackendAPI
from services.storage import runtime_store as store
from utils.helper import new_uuid

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_ERROR = "error"
EDITABLE_FILE_PLAN_TYPES = ("Plus", "Team", "Pro", "Enterprise")
EDITABLE_FILE_ROOT = DATA_DIR / "files"
EDITABLE_FILE_TASKS_PATH = DATA_DIR / "editable_file_tasks.json"


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _elapsed_seconds(task: dict[str, Any]) -> int:
    start = float(task.get("started_ts") or task.get("created_ts") or 0)
    end = float(task.get("ended_ts") or time.time())
    return max(0, int(end - start)) if start else 0


def _file_url(path: Path, base_url: str) -> str:
    rel = path.resolve().relative_to(EDITABLE_FILE_ROOT.resolve()).as_posix()
    prefix = str(base_url or "").strip().rstrip("/")
    return f"{prefix}/files/{quote(rel, safe='/')}" if prefix else f"/files/{quote(rel, safe='/')}"


def _editable_access_token() -> str:
    accounts = [
        item for item in account_service.list_accounts()
        if _clean(item.get("access_token"))
           and item.get("status") not in {"禁用", "异常"}
           and account_service._account_matches_any_plan_type(item, EDITABLE_FILE_PLAN_TYPES)
    ]
    if not accounts:
        raise RuntimeError("no available plus/team/pro account")
    accounts.sort(key=lambda item: _clean(item.get("last_used_at")))
    token = _clean(accounts[0].get("access_token"))
    return account_service.refresh_access_token(token, event="editable_file_task") or token


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": task.get("id"),
        "taskId": task.get("id"),
        "status": task.get("status"),
        "kind": task.get("kind"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "elapsed_seconds": _elapsed_seconds(task),
    }
    for key in ("result", "error"):
        if task.get(key):
            item[key] = task[key]
    return item


class EditableFileTaskService:
    """可编辑文件（PPT/PSD）任务服务。

    存储：**数据库**（services/storage/runtime_store.py）。
    旧实现是「内存 dict + 全量 JSON 重写」，每次状态更新都重写整个文件。
    """

    def __init__(self, path: Path = EDITABLE_FILE_TASKS_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 服务重启：未完成的任务标记为中断
        with self._lock:
            self._recover_unfinished()

    def submit_ppt(self, identity: dict[str, object], *, client_task_id: str = "", prompt: str = "", base64_images: list[str] | None = None, base_url: str = "") -> dict[str, Any]:
        return self._submit(identity, client_task_id=client_task_id, kind="ppt", prompt=prompt, base64_images=base64_images or [], base_url=base_url)

    def submit_psd(self, identity: dict[str, object], *, client_task_id: str = "", prompt: str = "", base64_images: list[str] | None = None, base_url: str = "") -> dict[str, Any]:
        return self._submit(identity, client_task_id=client_task_id, kind="psd", prompt=prompt, base64_images=base64_images or [], base_url=base_url)

    def list_tasks(self, identity: dict[str, object], task_ids: list[str]) -> dict[str, Any]:
        owner = _owner_id(identity)
        requested = [_clean(item) for item in task_ids if _clean(item)]
        rows = store.list_editable_file_tasks(owner)
        by_id = {str(row.get("id")): row for row in rows}
        if requested:
            items = [by_id[task_id] for task_id in requested if task_id in by_id]
            return {"items": [_public_task(item) for item in items], "missing_ids": [task_id for task_id in requested if task_id not in by_id]}
        items = list(rows)
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return {"items": [_public_task(item) for item in items], "missing_ids": []}

    def _submit(self, identity: dict[str, object], *, client_task_id: str, kind: str, prompt: str, base64_images: list[str], base_url: str) -> dict[str, Any]:
        task_id = _clean(client_task_id) or new_uuid()
        owner = _owner_id(identity)
        now = _now_iso()
        with self._lock:
            existing = store.get_editable_file_task(owner, task_id)
            if existing is not None:
                return _public_task(existing)
            ts = time.time()
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": TASK_STATUS_QUEUED,
                "kind": kind,
                "model": EDITABLE_FILE_MODEL,
                "created_at": now,
                "updated_at": now,
                "created_ts": ts,
                "updated_ts": ts,
            }
            store.upsert_editable_file_task(task)
        threading.Thread(target=self._run_task, args=(owner, task_id, kind, prompt, base64_images, dict(identity), base_url), name=f"{kind}-file-task-{task_id[:16]}", daemon=True).start()
        return _public_task(task)

    def _run_task(self, owner: str, task_id: str, kind: str, prompt: str, base64_images: list[str], identity: dict[str, object], base_url: str) -> None:
        started = time.time()
        token = ""
        account_email = ""
        self._update_task(owner, task_id, status=TASK_STATUS_RUNNING, error="", started_ts=started)
        backend = None
        try:
            if kind == "psd" and not base64_images:
                raise ValueError("base64_images is empty")
            token = _editable_access_token()
            account = account_service.get_account(token) or {}
            account_email = _clean(account.get("email"))
            backend = OpenAIBackendAPI(token)
            output_dir = EDITABLE_FILE_ROOT / kind / task_id
            result = backend.export_psd_zip(base64_images, prompt, output_dir) if kind == "psd" else backend.export_ppt_zip(base64_images, prompt, output_dir)
            account_service.mark_text_used(token)
            data = {"conversation_id": result.conversation_id, "primary_url": _file_url(result.primary_path, base_url), "zip_url": _file_url(result.zip_path, base_url)}
            self._update_task(owner, task_id, status=TASK_STATUS_SUCCESS, result=data, account_email=account_email, error="", ended_ts=time.time())
            self._log_call(identity, kind, started, request_text(prompt), account_email=account_email, result=data)
        except Exception as exc:
            error = str(exc) or "editable file task failed"
            self._update_task(owner, task_id, status=TASK_STATUS_ERROR, error=error, account_email=account_email, ended_ts=time.time())
            self._log_call(identity, kind, started, request_text(prompt), status="failed", error=error, account_email=account_email)
        finally:
            if backend is not None:
                backend.close()

    def public_file_path(self, relative_path: str) -> Path:
        raw = str(relative_path or "").replace("\\", "/").lstrip("/")
        path = (EDITABLE_FILE_ROOT / raw).resolve()
        path.relative_to(EDITABLE_FILE_ROOT.resolve())
        if not path.is_file():
            raise FileNotFoundError(raw)
        return path

    def _update_task(self, owner: str, task_id: str, **updates: Any) -> None:
        """局部更新 —— 只 UPDATE 受影响的一行。"""
        updates = dict(updates)
        updates["updated_at"] = _now_iso()
        updates["updated_ts"] = time.time()
        try:
            store.update_editable_file_task(owner, task_id, updates)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "更新可编辑文件任务状态失败 owner=%s task=%s", owner, task_id
            )

    def _recover_unfinished(self) -> None:
        """服务重启时把未完成的任务标记为中断。"""
        try:
            store.recover_unfinished_editable_file_tasks()
        except Exception:
            pass

    def _log_call(
            self,
            identity: dict[str, object],
            kind: str,
            started: float,
            request_preview: str,
            *,
            status: str = "success",
            error: str = "",
            account_email: str = "",
            result: dict[str, str] | None = None,
    ) -> None:
        detail = {
            "key_id": identity.get("id"),
            "key_name": identity.get("name"),
            "role": identity.get("role"),
            "endpoint": f"/v1/{kind}/generations",
            "model": EDITABLE_FILE_MODEL,
            "started_at": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "status": status,
        }
        if request_preview:
            detail["request_text"] = request_preview
        if account_email:
            detail["account_email"] = account_email
        if error:
            detail["error"] = error
        if result:
            detail["result"] = result
        try:
            log_service.add(LOG_TYPE_CALL, f"{kind.upper()}生成任务{'失败' if status == 'failed' else '完成'}", detail)
        except Exception:
            pass


editable_file_task_service = EditableFileTaskService()

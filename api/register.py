"""注册管理 API。

接口：
- GET  /api/register/status             注册业务总览
- GET  /api/register/runs                运行记录
- GET  /api/register/registered          注册成功的账号列表
- POST /api/register/import-to-pool      手动批量导入号池（勾选后导入）
- POST /api/register/import-text         按 access_token----proxy 文本导入号池
- GET  /api/register/proxies             代理池
- POST /api/register/proxies             批量导入代理
- GET  /api/register/mail-providers      邮箱来源列表
- POST /api/register/settings            注册配置读写
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from api.support import require_admin
from services import register_service
from services.storage import register_store as store


class ImportToPoolRequest(BaseModel):
    emails: list[str]
    # True → 按 access_token----proxy 带代理入池；False → 只入 access_token
    with_proxy: bool = True


class ImportTextRequest(BaseModel):
    text: str
    source_type: str = "register"


class ProxyImportRequest(BaseModel):
    text: str
    mode: str = "replace"  # replace | append


class RegisterSettingsRequest(BaseModel):
    values: dict[str, str]


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/register/status")
    async def status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return register_service.register_status()

    @router.get("/api/register/runs")
    async def runs(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        items = register_service.list_runs(limit=100)
        return {"items": items}

    @router.get("/api/register/registered")
    async def registered(
        limit: int = 50,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        items, total = store.list_registered(limit=limit, offset=offset)
        # 不返回完整凭证（access_token 等），前端只需要 email / 是否有 token / 代理
        brief = [
            {
                "email": item.get("email"),
                "has_access_token": bool(item.get("access_token")),
                "has_refresh_token": bool(item.get("refresh_token")),
                "proxy": item.get("proxy") or "",
                "created_at": item.get("created_at"),
            }
            for item in items
        ]
        return {"items": brief, "total": total}

    @router.post("/api/register/import-to-pool")
    async def import_to_pool(
        body: ImportToPoolRequest,
        authorization: str | None = Header(default=None),
    ):
        """把勾选的注册结果批量导入号池。"""
        require_admin(authorization)
        if not body.emails:
            raise HTTPException(status_code=400, detail={"error": "请至少选择一个账号"})
        return register_service.import_registered_to_pool(
            body.emails, with_proxy=body.with_proxy
        )

    @router.post("/api/register/import-text")
    async def import_text(
        body: ImportTextRequest,
        authorization: str | None = Header(default=None),
    ):
        """按 `access_token----proxy` 文本格式导入号池。"""
        require_admin(authorization)
        if not body.text.strip():
            raise HTTPException(status_code=400, detail={"error": "导入内容为空"})
        return register_service.import_token_text_to_pool(
            body.text, source_type=body.source_type
        )

    @router.get("/api/register/proxies")
    async def proxies(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": store.list_proxies(), "stats": store.proxy_pool_stats()}

    @router.post("/api/register/proxies")
    async def import_proxies(
        body: ProxyImportRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        mode = body.mode if body.mode in {"replace", "append"} else "replace"
        return store.replace_proxies(body.text, mode=mode)

    @router.get("/api/register/mail-providers")
    async def mail_providers(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        from services.register.mail_providers import list_providers

        return {"providers": list_providers()}

    @router.get("/api/register/settings")
    async def get_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"values": store.get_settings()}

    @router.post("/api/register/settings")
    async def update_settings(
        body: RegisterSettingsRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        if not isinstance(body.values, dict):
            raise HTTPException(status_code=400, detail={"error": "values 必须是对象"})
        for key, value in body.values.items():
            store.set_setting(str(key), value)
        return {"values": store.get_settings()}

    return router

"""号池自动补充守护的 API。

接口：
- GET  /api/auto-register/settings   读取配置 + 守护状态
- POST /api/auto-register/settings   更新配置
- POST /api/auto-register/check      立即执行一次检查（可手动触发）
- POST /api/auto-register/trigger    强制触发一轮注册（忽略阈值）
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from api.support import require_admin
from services import auto_register_guardian as guardian


class AutoRegisterSettingsRequest(BaseModel):
    enabled: bool | None = None
    threshold: int | None = None
    target_batch: int | None = None
    check_interval_minute: int | None = None
    concurrency: int | None = None
    mail_source: str | None = None
    register_options: dict | None = None
    cooldown_minute: int | None = None


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/auto-register/settings")
    async def get_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "settings": guardian.get_settings(),
            "state": guardian.get_state(),
        }

    @router.post("/api/auto-register/settings")
    async def update_settings(
        body: AutoRegisterSettingsRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        from services.config import config

        current = guardian.get_settings()
        payload = body.model_dump(exclude_none=True)
        if not payload:
            raise HTTPException(status_code=400, detail={"error": "没有需要更新的字段"})

        merged = {**current, **payload}
        # register_options 整体替换（不是合并），避免残留旧键
        if "register_options" in payload:
            merged["register_options"] = payload["register_options"] or {}

        try:
            config.update({"auto_register": merged})
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": f"保存配置失败: {exc}"})
        # 配置变了就立刻唤醒守护线程重查，不用等满一个检查周期
        guardian.notify_wakeup()
        return {"settings": guardian.get_settings()}

    @router.post("/api/auto-register/check")
    async def check_now(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return guardian._check_once()

    @router.post("/api/auto-register/trigger")
    async def trigger_now(authorization: str | None = Header(default=None)):
        """强制触发一轮注册，忽略阈值判断（仍受「批量注册已在跑」保护）。"""
        require_admin(authorization)
        settings = guardian.get_settings()
        result = guardian.trigger_registration(settings)
        if result.get("ok"):
            guardian._set_state(
                last_trigger_at=guardian._now(),
                last_trigger_result="手动触发",
                last_error="",
                trigger_count=int(guardian.get_state().get("trigger_count") or 0) + 1,
            )
        else:
            guardian._set_state(last_error=str(result.get("error") or ""))
        return result

    return router

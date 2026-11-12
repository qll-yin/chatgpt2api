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

批量注册（services/register_batch.py —— 原注册机的 auto_loop）：
- POST /api/register/batch/start        启动批量注册
- POST /api/register/batch/pause        暂停
- POST /api/register/batch/resume       恢复
- POST /api/register/batch/stop         停止
- GET  /api/register/batch/status       批量状态
- GET  /api/register/batch/stream       SSE 实时日志流

单次注册：
- POST /api/register/once               跑一次注册
- GET  /api/register/stream/{run_id}    SSE 单次注册日志流
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
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


class ProxyUpdateRequest(BaseModel):
    """改单个代理的属性（字段不传的保持原值）。"""

    proxy: str
    enabled: bool | None = None
    max_uses: int | None = None
    note: str | None = None
    # static=固定出口 IP / dynamic=每次连接换 IP
    # ⚠️ 被 Cloudflare 403 拦时两者处置不同：
    #    static → 出口 IP 已被拉黑，连续被拦达阈值就自动停用
    #    dynamic → 换个出口 IP 可能就通过，只记次数不停用
    proxy_type: str | None = None


class ProxyResetRequest(BaseModel):
    """proxy 留空 = 作用于全部。"""

    proxy: str | None = None


class ProxyTestRequest(BaseModel):
    proxies: list[str] = []
    timeout: int = 8


class MailConfigRequest(BaseModel):
    """邮箱配置。password 类字段传空或 '***' 表示不修改。"""

    model_config = {"extra": "allow"}

    mail_source: str | None = None


class BatchStartRequest(BaseModel):
    """批量注册参数。字段与原注册机的 auto-loop 一致。"""

    concurrency: int = 1              # 并发 worker 数（1-20）
    target_count: int = 0             # 目标成功数（0 = 不限量）
    cool_down_seconds: float = 3.0    # 每个 worker 跑完后的冷却
    proxy: str = ""                   # 单代理（后端代理池为空时用）
    mail_source: str = ""             # 邮箱来源（空则取配置里的）
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = False
    want_2fa: bool = False
    otp_timeout: int = 180
    auto_import: bool = True          # 注册成功自动导入号池
    with_proxy: bool = True           # 入池时是否带代理


class RegisterOnceRequest(BaseModel):
    """单次注册参数。"""

    proxy: str = ""
    mail_source: str = ""
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = False
    want_2fa: bool = False
    otp_timeout: int = 180
    auto_import: bool = True
    with_proxy: bool = True


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/register/status")
    async def status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return register_service.register_status()

    @router.get("/api/register/runs")
    async def runs(
        limit: int = 50,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        items, total = store.list_runs(limit=max(1, min(limit, 500)), offset=max(0, offset))
        return {"items": items, "total": total}

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
        # 文本 → 代理列表（每行一个，空行与 # 注释跳过）
        proxies = [
            line.strip()
            for line in str(body.text or "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return store.replace_proxies(proxies, mode=mode)

    @router.patch("/api/register/proxies")
    async def update_proxy(
        body: ProxyUpdateRequest,
        authorization: str | None = Header(default=None),
    ):
        """改单个代理：启用开关 / 成功注册上限 / 备注 / 类型（静态 or 动态）。"""
        require_admin(authorization)
        ok = store.update_proxy(
            body.proxy,
            enabled=body.enabled,
            max_uses=body.max_uses,
            note=body.note,
            proxy_type=body.proxy_type,
        )
        if not ok:
            raise HTTPException(status_code=404, detail={"error": "代理不存在"})
        return {"items": store.list_proxies(), "stats": store.proxy_pool_stats()}

    @router.delete("/api/register/proxies")
    async def clear_proxies(authorization: str | None = Header(default=None)):
        """清空整个代理池（连同用量记录）。"""
        require_admin(authorization)
        removed = store.clear_proxies()
        return {"removed": removed, "items": store.list_proxies(), "stats": store.proxy_pool_stats()}

    @router.delete("/api/register/proxies/{proxy:path}")
    async def delete_proxy(
        proxy: str,
        authorization: str | None = Header(default=None),
    ):
        """删掉单个代理。"""
        require_admin(authorization)
        if not store.delete_proxy(proxy):
            raise HTTPException(status_code=404, detail={"error": "代理不存在"})
        return {"items": store.list_proxies(), "stats": store.proxy_pool_stats()}

    @router.post("/api/register/proxies/reset")
    async def reset_proxy_usage(
        body: ProxyResetRequest,
        authorization: str | None = Header(default=None),
    ):
        """重置用量计数（不删代理）。proxy 留空 = 全部重置。"""
        require_admin(authorization)
        reset = store.reset_proxy_counts(body.proxy)
        return {"reset": reset, "items": store.list_proxies(), "stats": store.proxy_pool_stats()}

    @router.post("/api/register/proxies/cf-reset")
    async def reset_proxy_cf(
        body: ProxyResetRequest,
        authorization: str | None = Header(default=None),
    ):
        """清零 CF 拦截计数（代理换了 IP / 供应商修了之后手动点一下）。"""
        require_admin(authorization)
        target = str(body.proxy or "").strip()
        if target:
            store.reset_proxy_cf_blocked(target)
            reset = 1
        else:
            reset = store.reset_all_proxy_cf_blocked()
        return {"reset": reset, "items": store.list_proxies(), "stats": store.proxy_pool_stats()}

    @router.post("/api/register/proxies/test")
    async def test_proxy_conn(
        body: ProxyTestRequest,
        authorization: str | None = Header(default=None),
    ):
        """并发测试代理连通性（复用注册流程的 session 工厂）。"""
        require_admin(authorization)
        from services import proxy_test

        if not body.proxies:
            raise HTTPException(status_code=400, detail={"error": "请先选择要测试的代理"})
        results = proxy_test.test_proxies(body.proxies, timeout=body.timeout)
        return {"results": results}

    @router.post("/api/register/proxies/ipv6-test")
    async def test_proxy_ipv6(
        body: ProxyTestRequest,
        authorization: str | None = Header(default=None),
    ):
        """检测哪些代理走 IPv6 出口（IPv6 被 CF 按段拉黑的概率低得多）。"""
        require_admin(authorization)
        from services import proxy_test

        if not body.proxies:
            raise HTTPException(status_code=400, detail={"error": "请先选择要检测的代理"})
        results = proxy_test.test_ipv6(body.proxies, timeout=body.timeout or 10)
        return {
            "results": results,
            "items": store.list_proxies(),
            "stats": store.proxy_pool_stats(),
        }

    @router.get("/api/register/mail-providers")
    async def mail_providers(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        from services.register.mail_providers import list_providers

        return {"providers": list_providers()}

    @router.get("/api/register/mail-config")
    async def get_mail_config(authorization: str | None = Header(default=None)):
        """邮箱配置（密码类字段脱敏成 '***'）。"""
        require_admin(authorization)
        return {"config": store.get_mail_config()}

    @router.post("/api/register/mail-config")
    async def save_mail_config(
        body: MailConfigRequest,
        authorization: str | None = Header(default=None),
    ):
        """保存邮箱配置。password 类字段传空或 '***' = 不修改。"""
        require_admin(authorization)
        try:
            store.save_mail_config(body.model_dump(exclude_none=True))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail={"error": f"保存失败: {exc}"})
        return {"config": store.get_mail_config()}

    @router.post("/api/register/mail-test")
    async def test_mail(authorization: str | None = Header(default=None)):
        """测试当前邮箱来源的连通性（具体怎么测由 provider 的 self_test 决定）。"""
        require_admin(authorization)
        from services.register.mail_providers import (
            MailProviderError,
            create_mail_provider,
            get_provider_class,
        )

        mail_source = store.get_setting("mail_source", "outlook")
        try:
            provider_cls = get_provider_class(mail_source)
        except MailProviderError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})

        # 池化 provider 的连通性绑在具体某个号上，没号可测
        if provider_cls.pooled:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"{provider_cls.display_name} 是号池类型，不需要单独测试；"
                    "导入时会校验格式"
                },
            )

        try:
            provider = create_mail_provider(mail_source, store.get_mail_settings())
        except MailProviderError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail={"error": f"构造 {provider_cls.display_name} 失败: {exc}"}
            )

        try:
            result = provider.self_test()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail={"error": f"连接失败: {exc}"})
        if not result.get("ok"):
            raise HTTPException(
                status_code=500, detail={"error": result.get("message") or "连接失败"}
            )
        return {"ok": True, "message": result.get("message", "连接成功")}

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

    # ──────────────────────── 批量注册 ────────────────────────

    @router.post("/api/register/batch/start")
    async def batch_start(
        body: BatchStartRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        from services import register_batch

        result = register_batch.CONTROLLER.start(body.model_dump())
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail={"error": result.get("error") or "启动失败"})
        return result

    @router.post("/api/register/batch/pause")
    async def batch_pause(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        from services import register_batch

        result = register_batch.CONTROLLER.pause()
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail={"error": result.get("error") or "暂停失败"})
        return result

    @router.post("/api/register/batch/resume")
    async def batch_resume(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        from services import register_batch

        result = register_batch.CONTROLLER.resume()
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail={"error": result.get("error") or "恢复失败"})
        return result

    @router.post("/api/register/batch/stop")
    async def batch_stop(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        from services import register_batch

        result = register_batch.CONTROLLER.stop()
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail={"error": result.get("error") or "停止失败"})
        return result

    @router.get("/api/register/batch/status")
    async def batch_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        from services import register_batch

        return register_batch.CONTROLLER.status()

    @router.get("/api/register/batch/stream")
    async def batch_stream(authorization: str | None = Header(default=None)):
        """批量注册的实时日志流（SSE）。

        订阅前会先推当前状态快照，前端不用额外请求一次。
        """
        require_admin(authorization)
        from services import register_batch

        def _generate():
            import json
            import queue
            import time

            q: queue.Queue = queue.Queue(maxsize=2000)
            controller = register_batch.CONTROLLER
            # 订阅前先拿一份快照
            try:
                q.put_nowait({"kind": "state", "data": controller.status()})
            except queue.Full:
                pass
            # 用原 auto_loop 的订阅机制（返回 queue 的那种）
            controller._subscribers.append(q)
            try:
                while True:
                    try:
                        event = q.get(timeout=15)
                    except queue.Empty:
                        # 心跳：防止反向代理因空闲断开连接
                        yield f"data: {json.dumps({'kind': 'ping'}, ensure_ascii=False)}\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("kind") == "ended":
                        return
            finally:
                with controller._lock:
                    try:
                        controller._subscribers.remove(q)
                    except ValueError:
                        pass

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ──────────────────────── 单次注册 ────────────────────────

    @router.post("/api/register/once")
    async def register_once(
        body: RegisterOnceRequest,
        authorization: str | None = Header(default=None),
    ):
        """跑一次注册（同步等待完成）。"""
        require_admin(authorization)
        import threading

        result: dict = {}

        def _run():
            try:
                result.update(register_service.register_one(body.model_dump()))
            except Exception as exc:  # noqa: BLE001
                result.update({"ok": False, "error": str(exc), "error_category": "unknown"})

        thread = threading.Thread(target=_run, daemon=True, name="register-once")
        thread.start()
        thread.join()
        return result

    @router.get("/api/register/stream/{run_id}")
    async def register_stream(run_id: str, authorization: str | None = Header(default=None)):
        """单次注册的实时日志流（SSE）。"""
        require_admin(authorization)
        from services import register_registrar as registrar

        def _generate():
            import json
            import queue

            q = registrar.get_run_queue(run_id)
            if q is None:
                yield f"data: {json.dumps({'kind': 'error', 'message': 'run 不存在或已结束'}, ensure_ascii=False)}\n\n"
                return
            while True:
                try:
                    line = q.get(timeout=15)
                except queue.Empty:
                    yield f"data: {json.dumps({'kind': 'ping'}, ensure_ascii=False)}\n\n"
                    continue
                # __EVENT__: 前缀的是结构化事件，其余是纯日志行
                if isinstance(line, str) and line.startswith("__EVENT__:"):
                    try:
                        payload = json.loads(line[len("__EVENT__:"):])
                    except Exception:
                        payload = {"kind": "raw", "message": line}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps({'kind': 'log', 'message': str(line)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router

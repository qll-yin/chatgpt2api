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
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from api.support import require_admin
from services import register_service, reauth_service
from services.storage import register_store as store


class AccessTokensRequest(BaseModel):
    """批量取 access_token 的请求体（批量复制用）。"""

    emails: list[str]


class ImportToPoolRequest(BaseModel):
    emails: list[str]
    # True → 按 access_token----proxy 带代理入池；False → 只入 access_token
    with_proxy: bool = True
    # 指定一个静态代理，导入时全部绑到它（受该代理的绑定上限约束）
    bind_proxy: str | None = None
    # 自动从静态代理池里挑（绑得少的优先），每个代理受 bind_max_uses 限制
    auto_bind: bool = False
    # 入池代理模式（明确优先于 with_proxy）：
    #   none    → 不带代理入池
    #   register→ 沿用注册时的代理（**必须是登记过的静态代理**）
    #   static  → 从静态代理池分配
    # ⚠️ 动态代理永远不能进号池 —— 出口 IP 每次变，账号用起来更容易触发
    #    风控，还会把代理服务商的流量包耗光。
    proxy_mode: str | None = None


class ReauthorizeRequest(BaseModel):
    """重新授权（刷新 401）。"""

    email: str
    # 本次授权走的出口代理。留空 = 沿用账号原来记的代理。
    proxy: str = ""
    # 单次尝试的上限（秒）
    timeout: int = 300


class ImportTextRequest(BaseModel):
    text: str
    source_type: str = "register"


class ProxyImportRequest(BaseModel):
    text: str
    mode: str = "replace"  # replace | append
    # 新导入代理的类型：static（固定出口）/ dynamic（每次换 IP）。
    # ⚠️ 以前写死 static，导入一批动态代理后必须逐个手改，
    #    选「注册使用代理 = 只用动态」时一个号都跑不了。
    proxy_type: str = "static"


class RegisterSettingsRequest(BaseModel):
    values: dict[str, str]


class ProxyUpdateRequest(BaseModel):
    """改单个代理的属性（字段不传的保持原值）。"""

    proxy: str
    enabled: bool | None = None
    max_uses: int | None = None
    # 号池绑定配额：能绑多少个账号到号池（0 = 不限）
    # 与 max_uses（成功注册多少次）互相独立
    bind_max_uses: int | None = None
    note: str | None = None
    # static=固定出口 IP / dynamic=每次连接换 IP
    # ⚠️ 被 Cloudflare 403 拦时两者处置不同：
    #    static → 出口 IP 已被拉黑，连续被拦达阈值就自动停用
    #    dynamic → 换个出口 IP 可能就通过，只记次数不停用
    proxy_type: str | None = None


class ProxyResetRequest(BaseModel):
    """proxy 留空 = 作用于全部。"""

    proxy: str | None = None


class ProxyInvalidStateRequest(BaseModel):
    """清零某个代理的「会话失效」计数（409 invalid_state）。"""

    proxy: str


class ProxyTestRequest(BaseModel):
    proxies: list[str] = []
    timeout: int = 8


class MailConfigRequest(BaseModel):
    """邮箱配置。password 类字段传空或 '***' 表示不修改。"""

    model_config = {"extra": "allow"}

    mail_source: str | None = None


class SmsConfigRequest(BaseModel):
    """SMS 接码配置。字段与原注册机一致；api_key 传 '***' 或空 = 不修改。"""

    sms_enabled: str | None = None
    sms_provider: str | None = None
    sms_api_key: str | None = None
    sms_country: str | None = None
    sms_service: str | None = None
    sms_max_price: str | None = None
    sms_fixed_price: str | None = None
    sms_reuse_phone: str | None = None
    sms_phone_success_max: str | None = None
    sms_auto_country: str | None = None
    sms_strict_whitelist: str | None = None
    sms_allowed_countries: str | None = None
    sms_auto_min_stock: str | None = None
    sms_auto_max_price: str | None = None
    sms_max_phone_attempts: str | None = None
    sms_per_phone_timeout: str | None = None


class MailPoolImportRequest(BaseModel):
    """批量导入邮箱号池。"""

    text: str
    kind: str = "outlook"


class MailPoolDeleteRequest(BaseModel):
    emails: list[str]


class RemailOrderRequest(BaseModel):
    """手动下一单（测试用 / 补单）。留空则用「邮箱配置」里的默认值。"""

    suffix: str | None = None
    project_id: int | None = None
    service_mode: str | None = None
    supply_policy: str | None = None


class LinkOrderRequest(BaseModel):
    email: str


class OrderDisabledRequest(BaseModel):
    disabled: bool = True


class BulkDeleteOrdersRequest(BaseModel):
    order_nos: list[str]


class MarkAbnormalRequest(BaseModel):
    """标注账号异常（被封 / 额度耗尽 / 不可用）。"""

    emails: list[str]
    abnormal: bool = True


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
    with_proxy: bool = True           # 入池时是否带代理（老参数，proxy_mode 优先）
    # 入池代理模式：none / register / static（默认沿用注册代理）
    # ⚠️ 动态代理不会进号池 —— 沿用注册代理时若那是动态代理，按空代理入池。
    pool_proxy_mode: str = ""
    # 「注册使用代理」：注册时从代理池里挑哪一类代理用。
    # dynamic（只动态）/ static（只静态）/ mixed（混合随机，默认）。
    # ⚠️ 与「入池代理」是两件独立的事：选了 static 时，注册成功入池时
    #    会沿用同一条静态代理（注册出口 = 长期使用出口，保持一致）。
    register_proxy_mode: str = ""
    # worker 级重试：整个流程失败后，同一个 worker 再跑几遍（0-20，默认 3）。
    # ⚠️ 这是**按整个流程**算的 —— 流程内部撞 409 重建会话那种重试不额外计数。
    max_retries: int = 3


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
        """运行记录 —— 按批次聚合。

        一次批量注册 = 一个批次（下面挂多个 run，每个 run 一次注册），
        这样「这一轮跑了几个号、哪些邮箱、日志在哪」一眼能看到。
        """
        require_admin(authorization)
        items, total = store.list_run_batches(
            limit=max(1, min(limit, 500)), offset=max(0, offset)
        )
        return {"items": items, "total": total}

    @router.get("/api/register/runs/{run_id}/log")
    async def run_log(run_id: str, authorization: str | None = Header(default=None)):
        """单个 run 的完整日志。

        日志文件是注册时写下的（data/register_logs/<run_id>.log），
        run 结束后照样能翻 —— 排查问题靠它。
        """
        require_admin(authorization)
        from pathlib import Path

        from services.storage import register_store as run_store

        row = run_store.get_run(run_id)
        if not row:
            raise HTTPException(status_code=404, detail={"error": "运行记录不存在"})
        log_path = str(row.get("log_path") or "").strip()
        if not log_path:
            return {"run_id": run_id, "email": row.get("email"), "content": ""}
        path = Path(log_path)
        if not path.is_file():
            return {"run_id": run_id, "email": row.get("email"), "content": ""}
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail={"error": f"读取日志失败: {exc}"})
        return {"run_id": run_id, "email": row.get("email"), "content": content}

    @router.get("/api/register/batches/{batch_id}/log")
    async def batch_log(batch_id: str, authorization: str | None = Header(default=None)):
        """一个批次下所有 run 的日志（按时间顺序拼起来）。

        「每一轮的日志都区分开」—— 按批次给，前端一次拉齐。
        """
        require_admin(authorization)
        from pathlib import Path

        items, _total = store.list_run_batches(limit=500, offset=0)
        batch = next((b for b in items if b.get("batch_id") == batch_id), None)
        if batch is None:
            raise HTTPException(status_code=404, detail={"error": "批次不存在"})
        chunks: list[str] = []
        for run in batch.get("runs") or []:
            log_path = str(run.get("log_path") or "").strip()
            head = f"───── {run.get('email') or run.get('run_id')} [{run.get('status')}] ─────"
            chunks.append(head)
            if log_path:
                path = Path(log_path)
                if path.is_file():
                    try:
                        chunks.append(
                            path.read_text(encoding="utf-8", errors="replace")
                        )
                    except Exception:  # noqa: BLE001
                        chunks.append("(读取日志失败)")
                else:
                    chunks.append("(日志文件不存在)")
            else:
                chunks.append("(无日志文件)")
        return {
            "batch_id": batch_id,
            "emails": batch.get("emails") or [],
            "content": "\n".join(chunks),
        }

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
                # 标注异常的号（被封 / 额度耗尽 / 不可用）
                "abnormal": bool((item.get("extra") or {}).get("abnormal")),
            }
            for item in items
        ]
        return {"items": brief, "total": total}

    @router.get("/api/register/registered/{email}")
    async def registered_detail(
        email: str,
        authorization: str | None = Header(default=None),
    ):
        """单个注册账号的完整详情（管理员可看凭证）。"""
        require_admin(authorization)
        record = store.get_registered(email)
        if not record:
            raise HTTPException(status_code=404, detail={"error": "账号不存在"})
        return {"item": record}

    @router.post("/api/register/registered/access-tokens")
    async def registered_access_tokens(
        body: AccessTokensRequest,
        authorization: str | None = Header(default=None),
    ):
        """批量取选中账号的 access_token（用于「批量复制」）。

        列表接口只返回 `has_access_token` 布尔值（不泄露凭证），
        所以复制要单独走这个接口一次拿全 —— 比前端循环 N 次详情请求快得多。

        同时返回 proxy，方便直接复制成 `access_token----proxy` 的
        导入格式（导入其它号池时用）。
        """
        require_admin(authorization)
        emails = [str(e or "").strip().lower() for e in (body.emails or [])]
        emails = [e for e in emails if e]
        if not emails:
            raise HTTPException(status_code=400, detail={"error": "请至少选择一个账号"})
        items: list[dict[str, object]] = []
        for email in emails[:500]:
            record = store.get_registered(email)
            if not record:
                continue
            token = str(record.get("access_token") or "").strip()
            if not token:
                continue
            items.append({
                "email": email,
                "access_token": token,
                "proxy": str(record.get("proxy") or "").strip(),
            })
        return {"items": items, "missing": [e for e in emails if e not in {i["email"] for i in items}]}

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
            body.emails,
            with_proxy=body.with_proxy,
            bind_proxy=body.bind_proxy,
            auto_bind=body.auto_bind,
            proxy_mode=body.proxy_mode or "",
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

    @router.post("/api/register/reauthorize")
    async def reauthorize_account(
        body: ReauthorizeRequest,
        authorization: str | None = Header(default=None),
    ):
        """重新授权一个账号（刷新 401 / 凭证失效）。

        用账号库里已有的密码 + 2FA 重新走一次登录与 Codex 授权，
        拿到新的 access_token / refresh_token / session 并写回注册结果表。

        ⚠️ Codex 的 refresh_token 是单次使用 + 轮换制 —— 同一份凭证不能
        同时给两个池用。谁先刷新谁活，另一个立刻 401。所以刷新完要把
        新凭证重新导出给目标池。
        """
        require_admin(authorization)
        try:
            result = await run_in_threadpool(
                reauth_service.reauthorize_registered_account,
                body.email,
                proxy=body.proxy,
                timeout=body.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if not result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail={"error": result.get("error") or "重新授权失败", "result": result},
            )
        return {"result": result}

    @router.get("/api/register/proxies")
    async def proxies(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "items": store.list_proxies(),
            "stats": store.proxy_pool_stats(),
            # 号池绑定配额概览（静态代理专用）
            "bind_stats": store.bind_proxy_stats(),
        }

    @router.get("/api/register/proxies/bindable")
    async def bindable_proxies(authorization: str | None = Header(default=None)):
        """可绑账号的静态代理列表（含剩余名额），供导入号池时选择。"""
        require_admin(authorization)
        return {
            "items": store.list_bind_proxies(),
            "stats": store.bind_proxy_stats(),
        }

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
        return store.replace_proxies(
            proxies, mode=mode, proxy_type=body.proxy_type or "static"
        )

    @router.patch("/api/register/proxies")
    async def update_proxy(
        body: ProxyUpdateRequest,
        authorization: str | None = Header(default=None),
    ):
        """改单个代理：启用开关 / 成功注册上限 / 号池绑定上限 / 备注 / 类型（静态 or 动态）。"""
        require_admin(authorization)
        ok = store.update_proxy(
            body.proxy,
            enabled=body.enabled,
            max_uses=body.max_uses,
            bind_max_uses=body.bind_max_uses,
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

    @router.post("/api/register/proxies/invalid-reset")
    async def reset_proxy_invalid_state(
        body: ProxyInvalidStateRequest,
        authorization: str | None = Header(default=None),
    ):
        """清零单个代理的 409 invalid_state 计数。

        场景：代理换了出口 IP / 供应商解封之后手动清一下，
        不用删了重导（删了会丢 used_count 等统计）。
        """
        require_admin(authorization)
        ok = store.reset_proxy_invalid_state(body.proxy)
        if not ok:
            raise HTTPException(status_code=404, detail={"error": "代理不存在"})
        return {"items": store.list_proxies(), "stats": store.proxy_pool_stats()}

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
        """邮箱来源清单 + 每个来源的「是否已配置 / 是否就绪」。

        前端据此排序并禁用没配好的来源 —— 否则用户会选一个根本没配的
        来源，点开始后才发现一个号都领不到。
        """
        require_admin(authorization)
        from services.register.mail_providers import list_providers

        providers = list_providers()
        settings = store.get_mail_settings()
        pool_stats = store.stats()
        for item in providers:
            missing = [
                str(field.get("key"))
                for field in item.get("config_fields") or []
                if field.get("required") and not str(settings.get(field.get("key")) or "").strip()
            ]
            item["missing"] = missing
            item["configured"] = not missing
            # 池化来源（outlook / icloud_relay）还要邮箱池里有号才算就绪
            if item.get("pooled"):
                item["pool_available"] = int(pool_stats.get("available") or 0)
                item["ready"] = item["configured"] and item["pool_available"] > 0
            else:
                item["pool_available"] = None
                item["ready"] = item["configured"]
        return {"providers": providers, "current": settings.get("mail_source", "outlook")}

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

    # ──────────────────────── SMS 接码配置 ────────────────────────
    #
    # 接码与「获取 refresh_token」强关联 —— 部分账号在 codex auth 阶段
    # 会命中 add-phone，必须接码才能过。所以这套配置要能配。

    @router.get("/api/register/sms")
    async def get_sms_config(authorization: str | None = Header(default=None)):
        """SMS 接码配置（api_key 脱敏成 '***'）。"""
        require_admin(authorization)
        return {"config": store.get_sms_config()}

    @router.post("/api/register/sms")
    async def save_sms_config(
        body: SmsConfigRequest,
        authorization: str | None = Header(default=None),
    ):
        """保存 SMS 接码配置。"""
        require_admin(authorization)
        store.save_sms_config(body.model_dump(exclude_none=True))
        return {"ok": True, "config": store.get_sms_config()}

    @router.post("/api/register/sms/test")
    async def test_sms(authorization: str | None = Header(default=None)):
        """测试接码平台连通性（查余额）。"""
        require_admin(authorization)
        from services.register.sms_provider import create_sms_provider

        cfg = store.get_sms_internal_config()
        if not cfg.get("sms_api_key"):
            raise HTTPException(status_code=400, detail={"error": "未配置 sms_api_key"})
        try:
            provider = create_sms_provider(cfg["sms_provider"], cfg)
            balance = provider.get_balance()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail={"error": f"连接失败: {exc}"})
        return {
            "ok": True,
            "provider": cfg["sms_provider"],
            "balance": balance,
            "message": f"连接成功，余额: {balance}",
        }

    @router.get("/api/register/sms/countries")
    async def sms_top_countries(authorization: str | None = Header(default=None)):
        """接码平台的国家排名（价格 + 库存）。"""
        require_admin(authorization)
        from services.register.sms_provider import (
            OPENAI_SMS_COUNTRIES,
            SMS_COUNTRY_NAMES_CN,
            create_sms_provider,
        )

        cfg = store.get_sms_internal_config()
        if not cfg.get("sms_api_key"):
            raise HTTPException(status_code=400, detail={"error": "未配置 sms_api_key"})
        try:
            provider = create_sms_provider(cfg["sms_provider"], cfg)
            rows = provider.get_top_countries(service=cfg.get("sms_service") or "dr")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail={"error": f"查询失败: {exc}"})
        for row in rows:
            cid = str(row.get("country"))
            row["openai_sms_safe"] = cid in OPENAI_SMS_COUNTRIES
            row["name_cn"] = SMS_COUNTRY_NAMES_CN.get(cid, "未知")
        return {
            "ok": True,
            "countries": rows[:30],
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES),
        }

    @router.get("/api/register/sms/all-countries")
    async def sms_all_countries(
        provider: str = "",
        authorization: str | None = Header(default=None),
    ):
        """平台实际有库存的国家（动态查询）；查询失败则回落到静态字典。"""
        require_admin(authorization)
        from services.register.sms_provider import (
            OPENAI_SMS_COUNTRIES,
            SMS_COUNTRY_NAMES_CN,
            create_sms_provider,
        )

        cfg = store.get_sms_internal_config()
        if provider:
            cfg["sms_provider"] = provider

        if cfg.get("sms_api_key"):
            try:
                p = create_sms_provider(cfg["sms_provider"], cfg)
                rows = p.get_top_countries(service=cfg.get("sms_service") or "dr")
                countries = [
                    {
                        "id": str(r.get("country") or ""),
                        "name_cn": SMS_COUNTRY_NAMES_CN.get(
                            str(r.get("country") or ""), f"国家{r.get('country')}"
                        ),
                        "openai_sms_safe": str(r.get("country") or "") in OPENAI_SMS_COUNTRIES,
                        "price": r.get("price"),
                        "count": r.get("count"),
                    }
                    for r in rows
                ]
                if countries:
                    return {
                        "ok": True,
                        "countries": countries,
                        "openai_sms_safe": list(OPENAI_SMS_COUNTRIES),
                        "source": "live",
                    }
            except Exception:  # noqa: BLE001
                pass

        items = sorted(
            SMS_COUNTRY_NAMES_CN.items(),
            key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999,
        )
        return {
            "ok": True,
            "countries": [
                {
                    "id": cid,
                    "name_cn": name,
                    "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
                }
                for cid, name in items
            ],
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES),
            "source": "static",
        }

    # ──────────────────────── 邮箱池 ────────────────────────
    #
    # 池化的邮箱来源（outlook / icloud_relay）需要先把邮箱列表导进来，
    # 否则批量注册时一个号都领不到。

    @router.get("/api/register/mail-pool")
    async def mail_pool(
        status: str = "",
        kind: str = "",
        limit: int = 50,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ):
        """邮箱池列表。"""
        require_admin(authorization)
        items, total = store.list_accounts(
            status=status, kind=kind, limit=max(1, min(limit, 500)), offset=max(0, offset)
        )
        return {
            "items": items,
            "total": total,
            "stats": store.stats(),
        }

    @router.post("/api/register/mail-pool")
    async def import_mail_pool(
        body: MailPoolImportRequest,
        authorization: str | None = Header(default=None),
    ):
        """批量导入邮箱号池。

        **有一行不合法就整批拒绝**，一个都不写库 —— 不会出现
        「写进去一半对不上账」。错误里带行号和原因，前端直接展示。
        """
        require_admin(authorization)
        from services.register.mail_providers import ImportValidationError, MailProviderError

        try:
            result = store.import_accounts(body.text, kind=body.kind)
        except ImportValidationError as exc:
            return JSONResponse(
                status_code=422,
                content={"ok": False, "message": str(exc), "errors": exc.errors},
            )
        except MailProviderError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        return {"ok": True, **result, "stats": store.stats()}

    @router.post("/api/register/mail-pool/release-stale")
    async def release_stale_mail_pool(
        timeout_secs: int = 600,
        authorization: str | None = Header(default=None),
    ):
        """把卡在「使用中」的邮箱放回可用（进程崩溃 / 强制停止后的手动自愈）。

        只释放 claim 超过 timeout_secs 的号，不会动正在跑的那批。
        """
        require_admin(authorization)
        released = store.release_stale(max(60, int(timeout_secs)))
        return {"released": released, "stats": store.stats()}

    @router.delete("/api/register/mail-pool")
    async def delete_mail_pool(
        body: MailPoolDeleteRequest,
        authorization: str | None = Header(default=None),
    ):
        """从邮箱池移除账号。"""
        require_admin(authorization)
        removed = store.delete_accounts(body.emails)
        return {"removed": removed, "stats": store.stats()}

    # ──────────────────────── Remail 订单 ────────────────────────
    #
    # Remail 只交付「邮箱地址 + serviceToken」这一对取件凭证，不交付账号密码，
    # 所以本地必须自己记一份 —— 后续取件、重复收件、看邮箱类型都读本地表，
    # 不回查 Remail 的 projects / orders 接口。

    @router.get("/api/register/mail/orders")
    async def mail_orders(
        limit: int = 50,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ):
        """本机记录的 Remail 订单列表。"""
        require_admin(authorization)
        items = store.list_remail_orders(limit=max(1, min(limit, 500)), offset=max(0, offset))
        return {"items": items, "total": store.count_remail_orders()}

    @router.post("/api/register/mail/orders")
    async def create_mail_order(
        body: RemailOrderRequest,
        authorization: str | None = Header(default=None),
    ):
        """手动下一单（测试用 / 补单）。

        走的是和注册流程完全一样的 provider.create_mailbox()，
        所以订单会落在同一张表里，行为一致。
        """
        require_admin(authorization)
        from services.register.mail_providers import create_mail_provider

        try:
            provider = create_mail_provider("remail", store.get_mail_settings())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail={"error": f"构造 Remail provider 失败: {exc}"}
            )

        # 允许这一单覆盖配置里的后缀 / 项目 / 模式
        for attr, value in (
            ("email_suffix", body.suffix),
            ("project_id", body.project_id),
            ("service_mode", body.service_mode),
            ("supply_policy", body.supply_policy),
        ):
            if value not in (None, ""):
                setattr(provider, attr, value)

        try:
            email = provider.create_mailbox()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail={"error": f"下单失败: {exc}"})
        return {"ok": True, "email": email, "items": store.list_remail_orders()}

    @router.get("/api/register/mail/orders/{order_no}/mails")
    async def mail_order_mails(
        order_no: str,
        authorization: str | None = Header(default=None),
    ):
        """对某个订单取件 —— 这就是「长效订单可以重复收件」的入口。

        订单里的 serviceToken 是取件凭证，这里从本地表读，不回查服务端。
        """
        require_admin(authorization)
        from services.register.mail_providers import create_mail_provider

        order = store.get_remail_order(order_no, with_token=True)
        if not order:
            raise HTTPException(status_code=404, detail={"error": "订单不存在"})
        token = order.get("service_token") or ""
        if not token:
            raise HTTPException(status_code=400, detail={"error": "该订单没有 serviceToken，无法取件"})

        try:
            provider = create_mail_provider("remail", store.get_mail_settings())
            mails = provider.fetch_mails(order.get("email") or "", token)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail={"error": f"取件失败: {exc}"})

        # 只回必要字段。邮件正文可能很长，前端只展示摘要 + 验证码。
        items = [
            {
                "id": mail.get("id"),
                "sender": mail.get("sender") or "",
                "subject": mail.get("subject") or "",
                "receivedAt": mail.get("receivedAt") or "",
                "verificationCode": mail.get("verificationCode") or "",
                "bodyPreview": (mail.get("bodyPreview") or "")[:300],
            }
            for mail in mails
        ]
        return {"ok": True, "email": order.get("email") or "", "items": items}

    @router.post("/api/register/mail/orders/{order_no}/link")
    async def link_mail_order(
        order_no: str,
        body: LinkOrderRequest,
        authorization: str | None = Header(default=None),
    ):
        """把订单关联到本地注册成功的账号。"""
        require_admin(authorization)
        if not store.link_remail_order(order_no, body.email):
            raise HTTPException(status_code=404, detail={"error": "订单不存在"})
        return {"ok": True, "items": store.list_remail_orders()}

    @router.post("/api/register/mail/orders/{order_no}/disabled")
    async def mail_order_disabled(
        order_no: str,
        body: OrderDisabledRequest,
        authorization: str | None = Header(default=None),
    ):
        """本地禁用 / 启用一笔订单。

        禁用的订单不会再被自动复用（但仍可手动取件 —— 邮箱还在，钱已经花了）。
        只改本地标记，不动 Remail 服务端的状态。
        """
        require_admin(authorization)
        if not store.set_remail_order_disabled(order_no, body.disabled):
            raise HTTPException(status_code=404, detail={"error": "订单不存在"})
        return {"ok": True, "items": store.list_remail_orders()}

    @router.delete("/api/register/mail/orders/{order_no}")
    async def delete_mail_order(
        order_no: str,
        authorization: str | None = Header(default=None),
    ):
        """删除本地订单记录。

        ⚠️ 只删本地记录，不会去 Remail 退单 —— 退款要走服务端的售后流程。
        """
        require_admin(authorization)
        if not store.delete_remail_order(order_no):
            raise HTTPException(status_code=404, detail={"error": "订单不存在"})
        return {"ok": True, "items": store.list_remail_orders()}

    @router.post("/api/register/mail/orders/bulk_delete")
    async def bulk_delete_mail_orders(
        body: BulkDeleteOrdersRequest,
        authorization: str | None = Header(default=None),
    ):
        """批量删除本地订单记录。"""
        require_admin(authorization)
        deleted = store.delete_remail_orders(body.order_nos)
        return {"ok": True, "deleted": deleted, "items": store.list_remail_orders()}

    @router.get("/api/register/mail/wallet")
    async def mail_wallet(authorization: str | None = Header(default=None)):
        """查 Remail 余额（透传 apikey/profile）。"""
        require_admin(authorization)
        from services.register.mail_providers import create_mail_provider

        try:
            provider = create_mail_provider("remail", store.get_mail_settings())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail={"error": f"构造 Remail provider 失败: {exc}"}
            )
        try:
            resp = provider._request("GET", "/v1/open/apikey/profile", timeout=15)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail={"error": f"查询余额失败: {exc}"})
        status = getattr(resp, "status_code", 0)
        if status != 200:
            raise HTTPException(
                status_code=500, detail={"error": f"查询余额失败: status={status}"}
            )
        data = provider._parse_json(resp)
        key = data.get("apiKey") if isinstance(data.get("apiKey"), dict) else data
        return {
            "ok": True,
            "balance": str(key.get("balance") or "0"),
            "enabled": key.get("enabled"),
        }

    # ──────────────────────── 标注账号异常 ────────────────────────

    @router.post("/api/register/registered/mark-abnormal")
    async def mark_abnormal(
        body: MarkAbnormalRequest,
        authorization: str | None = Header(default=None),
    ):
        """标注账号异常（被封 / 额度耗尽 / 不可用）。

        标注异常时会**自动释放该号占用的代理配额** —— 号已经废了，
        它的 IP 名额应该还回池子里给别的号用。
        """
        require_admin(authorization)
        updated = store.mark_registered_abnormal_by_emails(body.emails, body.abnormal)
        items, total = store.list_registered(limit=200)
        return {
            "ok": True,
            "updated": updated,
            "items": items,
            "total": total,
            "proxy_stats": store.proxy_pool_stats(),
        }

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
        """跑一次注册（同步等待完成）。

        ⚠️ 必须走线程池：注册是分钟级阻塞操作，直接在 async 里
        `thread.join()` 会把整个事件循环卡住（SSE、状态轮询、所有接口
        全部无响应）。
        """
        require_admin(authorization)
        return await run_in_threadpool(
            register_service.register_one, body.model_dump()
        )

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

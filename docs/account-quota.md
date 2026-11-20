# 账号额度（生图剩余次数）是怎么来的

本文说明号池里那个「剩余额度」数字的来源、更新时机，以及自己动手查的方法。
状态码、字段名以当前实现为准（`services/openai_backend_api.py`、`services/account_service.py`）。

## 一句话结论

额度只有一个来源：`POST https://chatgpt.com/backend-api/conversation/init`
返回的 `limits_progress[]` 里 `feature_name == "image_gen"` 那一项的 `remaining`。

数据库里的 `quota` **不是**实时值，而是「上次刷新的快照 − 本系统此后成功生图次数」。

## 获取流程

```
账号页点「刷新」
  → POST /api/accounts/refresh            （传 access_tokens，返回 progress_id）
  → 服务端线程池（最多 10 并发）
  → 每个账号一次 fetch_remote_info
      ├─ 先检查 access_token 是否临近过期（剩余有效期不足 24 小时就提前换新）
      └─ 并发发出 3 个上游请求：
           GET  /backend-api/me                  邮箱、user_id
           POST /backend-api/conversation/init   额度、默认模型   ← 额度来源
           GET  /backend-api/accounts/check/...  套餐 plan_type
  → 从 limits_progress 提取 remaining / reset_after
  → update_account 写入 quota、restore_at、status
```

如果任一请求返回 401，会强制用 `refresh_token` 换一次 token 再试；仍然 401 才把这个号移出号池
（`InvalidAccessTokenError`）。所以偶尔看到某个号突然消失，通常不是额度问题，而是 token 真失效了。

对应代码位置：

| 环节 | 位置 |
| --- | --- |
| 接口入口 | `api/accounts.py` → `POST /api/accounts/refresh` |
| 并发调度 | `services/account_service.py` → `refresh_accounts()` |
| 单号刷新 | `services/account_service.py` → `fetch_remote_info()` |
| 上游请求 | `services/openai_backend_api.py` → `get_user_info()` |
| 额度提取 | `services/openai_backend_api.py` → `_extract_quota_and_restore_at()` |

## 额度提取与状态联动

```python
for item in limits_progress:
    if item.get("feature_name") == "image_gen":
        return int(item.get("remaining") or 0), item.get("reset_after")
```

- `quota = remaining`
- `restore_at = reset_after`（额度恢复时间）
- **`quota == 0` → 状态自动变「限流」，`quota > 0` → 「正常」**

套餐类型（`free` / `plus` / `team` / `pro`）来自 `/accounts/check`，与额度分开取。
`codex-gpt-image-2` 走的是另一条链路，同一账号通常有独立的 Codex 生图额度，
所以网页额度为 0 不代表 Codex 额度也为 0。

## 为什么本地数字和网页上看到的对不上

本地 `quota` 是**快照**，只有两个时机变化：

| 时机 | 变化 |
| --- | --- |
| 点「刷新」/ 定时刷新 | 用上游最新值覆盖 |
| 本系统成功生图一张 | `quota - 1`；减到 0 时状态变「限流」 |

因此以下情况会造成偏差，**点一次刷新即可校准**：

- 你本人在 chatgpt.com 网页上生成了图（上游已扣，本地不知道）
- 别的工具 / 别的部署也用这个号
- 额度按时间自动恢复（上游 +N，本地还是旧值）

反过来也成立：本系统把 `quota` 扣到 0 后不会再试这个号，但上游可能过一会儿就恢复了 ——
所以「限流」状态的账号会被定时任务重新刷新，恢复后自动回到「正常」。

## 自己查当前额度

### 方式一：管理页

号池管理 → 某一行右侧的刷新按钮（只刷这一个号），或批量刷新后看「剩余额度」列。
这是最稳妥的方式：请求走**账号自己绑定的代理**，出口 IP 和平时生图一致。

### 方式二：调后台接口

```bash
# 默认宿主机端口 3000（docker-compose 的 CHATGPT2API_PORT，容器内是 80）

# 触发刷新，拿 progress_id
curl -X POST http://127.0.0.1:3000/api/accounts/refresh \
  -H "Authorization: Bearer <管理员密钥>" \
  -H "Content-Type: application/json" \
  -d '{"access_tokens":["eyJhbGciOi..."]}'

# 轮询结果，done=true 时 total_quota 就是这次所刷账号的最新额度合计
curl http://127.0.0.1:3000/api/accounts/refresh/progress/<progress_id> \
  -H "Authorization: Bearer <管理员密钥>"
```

### 方式三：浏览器控制台（了解原理用）

登录 chatgpt.com 后打开 F12 → Console：

```js
const s = await (await fetch('/api/auth/session')).json();
const token = s.accessToken;
if (!token) throw new Error('没有 accessToken，先确认已登录');

const r = await fetch('/backend-api/conversation/init', {
  method: 'POST',
  headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
  body: JSON.stringify({
    gizmo_id: null,
    requested_default_model: null,
    conversation_id: null,
    timezone_offset_min: -480
  })
});
console.log('HTTP', r.status);
const d = await r.json();
console.table(d.limits_progress || []);
```

表格里 `feature_name` 为 `image_gen` 的那一行，`remaining` 就是当前剩余额度。

> ⚠️ 这种方式走的是**你自己浏览器的出口 IP**，不是账号绑定的代理。
> 批量注册出来的账号在陌生 IP 上突然请求 backend 更容易触发风控，
> 只是看额度建议用方式一或方式二。返回 403 通常是被要求带 sentinel token，不要反复重试。

## 已知隐患

额度提取依赖硬编码的 `"image_gen"` 字符串。如果上游改了 feature 名
（比如变成 `image_generation`），提取会失败并得到 `0`，进而把整个号池误标成「限流」。

目前的处理是「拿不到就当 0」。如果以后遇到大批账号突然同时变「限流」且刷新无效，
先怀疑这里 —— 可以在 `_extract_quota_and_restore_at` 里打日志确认上游实际返回的
`feature_name` 是什么。

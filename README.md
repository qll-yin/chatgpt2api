<h1 align="center">ChatGPT2API</h1>


<p align="center">ChatGPT2API 主要是对 ChatGPT 官网相关能力进行逆向整理与封装，提供面向 ChatGPT 图片生成、图片编辑、多图组图编辑场景的 OpenAI 兼容图片 API / 代理，并集成在线画图、号池管理、批量注册管理、多种账号导入方式、Cloudflare R2 对象存储与 Docker 自托管部署能力。</p>

> [!WARNING]
> 免责声明：
>
> 本项目涉及对 ChatGPT 官网文本生成、图片生成与图片编辑等相关接口的逆向研究，仅供个人学习、技术研究与非商业性技术交流使用。
>
> - 严禁将本项目用于任何商业用途、盈利性使用、批量操作、自动化滥用或规模化调用。
> - 严禁将本项目用于破坏市场秩序、恶意竞争、套利倒卖、二次售卖相关服务，以及任何违反 OpenAI 服务条款或当地法律法规的行为。
> - 严禁将本项目用于生成、传播或协助生成违法、暴力、色情、未成年人相关内容，或用于诈骗、欺诈、骚扰等非法或不当用途。
> - 使用者应自行承担全部风险，包括但不限于账号被限制、临时封禁或永久封禁以及因违规使用等所导致的法律责任。
> - 使用本项目即视为你已充分理解并同意本免责声明全部内容；如因滥用、违规或违法使用造成任何后果，均由使用者自行承担。
> - 本项目基于对 ChatGPT 官网相关能力的逆向研究实现，存在账号受限、临时封禁或永久封禁的风险。请勿使用你自己的重要账号、常用账号或高价值账号进行测试。

## 快速开始

### Docker 运行（推荐）

本仓库使用**本地源码构建**，不拉取预构建镜像（预构建镜像里的前端与本仓库代码不同源，本地改动不会生效）。

```bash
git clone https://github.com/qll-yin/chatgpt2api.git
cd chatgpt2api

# 1. 准备真实配置（仓库里只提交脱敏的 config.example.json）
cp config.example.json config.json
vi config.json          # 至少改掉 auth-key

# 2. 准备环境变量（compose 里所有可调项都走这里，改 .env 就行，compose 永远不用动）
cp .env.example .env
vi .env                 # 至少改掉 CHATGPT2API_AUTH_KEY 和 POSTGRES_PASSWORD

# 3. 构建并启动
docker compose up -d --build
```

> **配置只改 `.env`，不用改 `docker-compose.yml`** ——
> 端口、数据库密码、数据库名、存储后端全是 `${VAR:-默认值}`，
> `.env` 里改完 `docker compose up -d` 就生效。
> 而且 `DATABASE_URL` 里的密码和 db 服务的 `POSTGRES_PASSWORD`
> 引用的是**同一个变量**，改一处两边自动一致（不会像手改 compose 那样出现
> 「连接串改了、容器密码没改」导致认证失败）。
>
> `.env` 已在 `.gitignore` 里，`git pull` 升级不会覆盖它。
>
> 要连外部数据库或换端口？建一个 `docker-compose.override.yml`（同样不进版本库），
> compose 会自动合并。

> 默认会**同时起一个 Postgres 容器**（`chatgpt2api-db`）——
> 生图任务状态、图片索引、标签这些写入非常频繁的数据走 Postgres，
> 高并发下比 SQLite 稳得多（SQLite 的 WAL 模式容易撞 `database is locked`）。
> 数据落在宿主机 `./pgdata`。
>
> 不想多起一个容器？用 `docker-compose.local.yml`（SQLite 版，端口 8000）：
>
> ```bash
> docker compose -f docker-compose.local.yml up -d --build
> ```

启动前请先在 `config.json` 中设置 `auth-key`，也可以在 `docker-compose.yml` 中通过 `CHATGPT2API_AUTH_KEY` 覆盖。

- Web 面板：`http://localhost:3000`
- API 地址：`http://localhost:3000/v1`
- 数据目录：`./data`（compose 挂载到容器 `/app/data`）
- 配置文件：`./config.json`（compose 挂载到容器 `/app/config.json`，**不进入镜像层**）

> 镜像内已内置一份 `config.example.json` 作为兜底，容器即使没挂载配置也能启动；
> 但**正式使用必须挂载自己的 `config.json`**，否则用的是默认 `auth-key`。

**更新到新版本**：

```bash
cd chatgpt2api
git pull
docker compose up -d --build
```

`./data` 与 `./config.json` 都在宿主机上，`git pull` 与容器重建都不会丢数据。

**服务器出站网络要求**：

| 目标 | 用途 | 必需性 |
| --- | --- | --- |
| `chatgpt.com` | 生图、编辑、账号刷新 | **必需** |
| `registry.npmjs.org` | 构建期装前端依赖 | **必需**（仅构建期） |
| `mirrors.aliyun.com/pypi` | 构建期装 Python 依赖（`uv.lock` 里锁的源） | **必需**（仅构建期） |
| `*.r2.cloudflarestorage.com` | 存图到 Cloudflare R2 | 用 R2 存储时必需 |
| `sentinel.openai.com` | 注册功能下载 sentinel sdk.js | 用注册功能时必需 |

> 容器**启动**阶段不需要外网：Python 依赖在构建期已装好，运行时不会再去下载。
> 真正需要联网的是生图请求本身。

### 本地开发

启动后端：

```bash
git clone https://github.com/qll-yin/chatgpt2api.git
cd chatgpt2api
cp config.example.json config.json   # 本地开发也需要一份 config.json
uv sync
uv run main.py
```

启动前端：

```bash
cd chatgpt2api/web
bun install
bun run dev
```

前端产物构建（Docker 镜像用的是 npm 锁文件，本地开发用 bun）：

```bash
cd chatgpt2api/web
bun install && bun run build
```

### 存储后端配置

支持通过环境变量 `STORAGE_BACKEND` 切换存储方式：

- `postgres` - 外部 PostgreSQL / 内置 Postgres 容器（**默认**）
- `sqlite` - 本地 SQLite 数据库
- `json` - 本地 JSON 文件
- `git` - Git 私有仓库（需配置 `GIT_REPO_URL` 和 `GIT_TOKEN`）

> 运行态数据（生图任务状态、图片索引、标签、可编辑文件任务、注册数据）
> **无论 `STORAGE_BACKEND` 是什么都会走数据库** —— 所以 `DATABASE_URL` 才是关键那项。

示例：改用外部 PostgreSQL

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

## 功能

### API 兼容能力

- 兼容 `POST /v1/images/generations` 图片生成接口
- 兼容 `POST /v1/images/edits` 图片编辑接口
- 兼容面向图片场景的 `POST /v1/chat/completions`
- 兼容面向图片场景的 `POST /v1/responses`
- `GET /v1/models` 返回当前可用的图像模型列表（内置模型 + 上游目录自动拉取，见下）
- 支持通过 `n` 返回多张生成结果
- 支持生成可编辑 PPT 文件
- 支持生成可编辑 PSD 文件
- 支持 Codex 中的画图接口逆向，仅 `Plus` / `Team` / `Pro` 订阅可用，模型别名为 `codex-gpt-image-2`，如有需要可自行在其他场景映射回
  `gpt-image-2`，用于和官网画图区分；也就意味着同一账号会同时有官网和 Codex 两份生图额度

### 图像模型目录

- 内置模型：`gpt-image-2.6`、`gpt-image-2.6-sunburst`、`gpt-image-2.6-flare`、`gpt-image-2.5-sunburst`、`gpt-image-2.5-flare`、`gpt-image-2`、`gpt-5-5-thinking`、`gpt-5-5`、`gpt-5-3`
- 后台设置页提供「刷新模型目录」：调用 `POST /api/image-models/refresh`，
  内部请求 `GET chatgpt.com/backend-api/models` 拉取上游真实模型列表并缓存到 `config.json`
- **内置模型永远保留**，上游拉取只做追加 —— 上游目录同步慢或服务器上存着旧缓存时，
  代码里声明支持的模型（如 `gpt-image-2.6`）依然可选，不会被旧缓存覆盖
- `gpt-image-2.6-flare` / `gpt-image-2.6-sunburst` / `gpt-image-2.5-flare` 直接透传官方 slug，
  不做 `auto` 映射；只有 `gpt-image-2` 会映射到 `gpt-5-5-thinking`

### 在线画图功能

- 内置在线画图工作台，支持生成、图片编辑与多图组图编辑
- 模型下拉框与后端模型目录联动，刷新后可选到最新模型
- 编辑模式支持参考图上传
- 前端支持多图生成交互
- 本地保存图片会话历史，支持回看、删除和清空
- 支持服务端缓存图片URL
- 图片生成进度追踪，超时后可继续等待
- 图片懒加载与滚动位置记忆，优化大量图片场景性能

### 图片存储：Cloudflare R2 / S3 兼容

生成的图片默认存在服务器本地 `data/images/`。后台「设置 → 图片存储」可以改成存到对象存储：

- **支持 Cloudflare R2**（推荐），同时兼容 MinIO、阿里云 OSS（S3 兼容模式）、AWS S3、腾讯云 COS 等任意 S3 兼容服务
- 也保留 **WebDAV** 作为可选项（老配置不受影响）
- 三种保存模式：`仅本机` / `仅远端` / `本机 + 远端`
- **远端上传失败会自动降级存到本机**，返回本机链接 —— 远端挂了不影响出图，失败原因记录在图片索引里

R2 配置项（后台填或写进 `config.json` 的 `image_storage`）：

| 字段 | 说明 |
| --- | --- |
| `s3_endpoint` | R2：`https://<AccountID>.r2.cloudflarestorage.com` |
| `s3_region` | R2 固定 `auto` |
| `s3_bucket` | 存储桶名称 |
| `s3_access_key_id` | S3 API 凭据的 Access Key ID |
| `s3_secret_access_key` | S3 API 凭据的 Secret（**明文存于 `config.json`**，建议只给该桶权限） |
| `s3_path_style` | R2 必须 `true`（默认就是） |
| `remote_root_path` | 远端目录（对象 key 前缀），两个协议共用 |
| `public_base_url` | 公开访问前缀。留空 → 返回本应用 `/images/...` 代理地址；填 `https://pub-xxx.r2.dev` 或自定义域 → 直接返回远端直链（**R2 出网流量免费，推荐**） |

**远端过期清理**：开启「自动清理远端过期图片」并填写保留天数后，应用会把一条**生命周期规则（Lifecycle Rule）直接下发到桶上**，由 R2 服务端按天删除过期对象 —— 不占应用资源、没有扫描延迟，服务停了也照清。

- 规则只作用在 `remote_root_path` 前缀下，**桶上你其它规则不受影响**
- R2 通常在过期时间后 24 小时内完成删除
- 需要小时级精度时，可另外打开「额外用应用侧扫描兜底」（会定时列桶扫描，桶大了较贵）
- 从 R2 切到 WebDAV 时，会自动把桶上残留的那条规则摘掉

### 号池管理功能

- 自动刷新账号邮箱、类型、额度和恢复时间（异步进度追踪）
- 轮询可用账号执行图片生成与图片编辑
- 遇到 Token 失效类错误时自动剔除无效 Token
- 定时检查限流账号并自动刷新
- 支持密码重新登录恢复异常账号，刷新后可自动重登
- 支持网页端配置全局 HTTP / HTTPS / SOCKS5 / SOCKS5H 代理
- 支持稳定代理运行时（可接自建的 FlareSolverr 刷新 CF clearance，配置在后台设置页）
- 支持搜索、筛选、批量刷新、导出、手动编辑和清理账号
- 支持四种导入方式：本地 CPA JSON 文件导入、远程 CPA 服务器导入、`sub2api` 服务器导入、`access_token` 导入
- 支持在设置页配置 `sub2api` 服务器，筛选并批量导入其中的 OpenAI OAuth 账号

### 注册管理功能

内置的批量注册管理台（后台「注册管理」），把独立的注册机整合进了本项目：

- **邮箱来源**：Remail 长效/短效订单、Outlook 邮箱池、CF 临时邮箱等，可在批量启动时按需选择
- **Remail 订单管理**：下单、取件、复用、禁用、删除、余额查询
- **代理池**：静态/动态类型、单代理注册配额上限、CF 拦截计数、IPv6 检测、删除/停用/改上限/改类型/重置/用量重置
- **批量注册**：可设并发数、目标成功数、冷却时间、验证码超时、失败重试次数；实时日志逐行输出，耗时在停止后冻结
- **注册使用代理**：注册时从代理池里挑哪一类代理用 ——「只用动态」「只用静态」「混合随机」（默认）。类型是硬过滤，池里没有该类型会明确报错，不会偷偷换类型
- **入池代理**：注册成功后账号进号池时绑哪个代理 ——「携带静态代理」「携带注册所用代理」「不带代理」。**动态代理永远不进号池**（出口 IP 每次变，账号用起来更容易触发风控，还会耗光代理服务商的流量包）
- **注册成功自动入号池**：写进左侧导航的「号池管理」（chatgpt2api 账号池）
- **远端失败降级**：远端上传失败自动落本地，不中断出图
- **号池自动补充**：可用账号数低于阈值时自动触发批量注册（后台守护进程，无需外部注册机服务）

#### 失败重试：一次「开始注册」= 一遍完整流程

批量注册的「失败重试次数」（默认 3）按**整个流程**算：

```
开始注册 → 走完整条注册链 → 失败
                              ↓
                    换一个出口 IP，用同一个号
                    从头再走一遍完整流程（又一个「开始注册」）
                              ↓
                    连续 N 遍都失败 → 停掉这个 worker
```

- 流程**内部**的重建会话重试（warmup 4 次、409 invalid_state 3 次）不额外计数 —— 它们都发生在同一次「开始注册」里面
- 重试**必须换出口 IP**：同一个 IP 再跑一遍必然还是同样的 409 / CF 403。所以每遍失败就把那条固定出口代理排掉，下一遍换一条；固定出口又没得换时，直接失败交外层换 IP 重跑，不在原地空转
- 重试**复用同一个号**：这些失败几乎都是出口 IP 的锅（CF 拦截 / 409 会话失效 / 网络抽风），不是号的锅 —— 换号等于白烧一个付费邮箱
- 只对「出口 IP 被拦 / 网络抽风 / 409 会话失效」这类失败重试；邮箱或账号本身的问题（如邮箱已注册）不消耗重试次数，直接换号

#### 静态注册代理与入池保持一致

「注册使用代理」选了**静态代理**时，账号入池会**沿用同一条静态代理** —— 注册出口 = 长期使用出口，账号不会注册完就换 IP。

「注册使用代理」与「入池代理」是两个独立设置，互不干扰：

| 注册使用代理 | 入池代理 | 结果 |
| --- | --- | --- |
| 静态 | 携带静态代理 | 沿用注册那条静态代理（名额满了才自动分配别的） |
| 静态 | 携带注册所用代理 | 同上 |
| 静态 | 不带代理 | 仍然不带代理（尊重明确选择） |
| 动态 | 任意 | 动态代理**绝不入池**，按「入池代理」的设置走 |
| 混合随机 | 任意 | 注册时可能拿到动态也可能拿到静态；静态那条沿用，动态那条不入池 |


### 账号导入与代理绑定

Access Token 导入支持**一行一个 + 可选携带该号专属代理**：

```text
eyJhbGciOi...----http://1.2.3.4:8080
eyJhbGciOi...----socks5://user:pass@5.6.7.8:1080
eyJhbGciOi...                                    # 不带代理
```

- 分隔符取**第一个** `----`，其后整段作为该账号的专属代理（代理地址里不会再有 `----`）
- 带代理的账号走**账号级代理**路径，优先级高于全局代理与稳定代理运行时
- 典型用法：与注册机配合——注册时用什么 IP，就用什么 IP 跑后续请求，避免账号在多个 IP 间跳变触发风控


## 与 gpt-outlook-register 配合

配套项目：[Regert888/gpt-outlook-register](https://github.com/Regert888/gpt-outlook-register)（批量注册 ChatGPT 账号的纯协议注册机）。

> 提示：本项目已内置**注册管理**页（见上文），常规批量注册不再需要单独部署注册机。
> 下面这条链路适合「注册机跑在另一台机器 / 另一套流程」时，把结果并进本项目号池。

典型工作流：

1. 在注册机 WebUI 的「代理池」里配置代理（支持静态/动态类型、单代理注册配额上限、CF 拦截计数、IPv6 检测）
2. 批量注册账号，导出格式选 **「access_token----代理」**
3. 在本项目「号池管理 → 导入账号 → Access Token」里粘贴这批内容

这样每个账号在**注册和使用阶段走同一个 IP**，避免账号在多个出口之间跳变触发风控。

注册机还支持导出**「原始 session JSON」**（`/api/auth/session` 的完整响应，含 token 与 expires），
适合需要自行接管鉴权流程的场景。

## 效果展示

<table width="100%">
  <tr>
    <td width="50%"><img src="https://i.ibb.co/Jj8nfwwP/image.png" alt="image" border="0"></td>
    <td width="50%"><img src="https://i.ibb.co/pqf235v/image-edit.png" alt="image edit" border="0"></td>
  </tr>
  <tr>
    <td width="50%"><img src="https://i.ibb.co/tPcqtVfd/chery-studio.png" alt="chery studio" border="0"></td>
    <td width="50%"><img src="https://i.ibb.co/PsT9YHBV/account-pool.png" alt="account pool" border="0"></td>
  </tr>
  <tr>
    <td width="50%"><img src="https://i.ibb.co/rRWLG08q/new-api.png" alt="new api" border="0"></td>
  </tr>
</table>

## API

所有 AI 接口都需要请求头：

```http
Authorization: Bearer <auth-key>
```

<details>
<summary><code>GET /v1/models</code></summary>
<br>

返回当前暴露的图片模型列表。

```bash
curl http://localhost:3000/v1/models \
  -H "Authorization: Bearer <auth-key>"
```

<details>
<summary>说明</summary>
<br>

| 字段   | 说明                                                                                                         |
|:-----|:-----------------------------------------------------------------------------------------------------------|
| 返回模型 | 内置 `gpt-image-2.6`、`gpt-image-2.6-sunburst`、`gpt-image-2.6-flare`、`gpt-image-2.5-sunburst`、`gpt-image-2.5-flare`、`gpt-image-2`、`gpt-5-5-thinking`、`gpt-5-5`、`gpt-5-3`，以及上游目录自动拉取到的其他模型；实际以接口返回为准 |
| 接入场景 | 可接入 Cherry Studio、New API 等上游或客户端                                                                          |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/generations</code></summary>
<br>

OpenAI 兼容图片生成接口，用于文生图。

```bash
curl http://localhost:3000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "一只漂浮在太空里的猫",
    "n": 1,
    "response_format": "b64_json"
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段                | 说明                                                 |
|:------------------|:---------------------------------------------------|
| `model`           | 图片模型，当前可用值以 `/v1/models` 返回结果为准，推荐使用 `gpt-image-2` |
| `prompt`          | 图片生成提示词                                            |
| `n`               | 生成数量，当前后端限制为 `1-4`                                 |
| `output_format`   | `png`（默认）或 `gif`；图片编辑与带参考图的链路不支持 GIF              |
| `frame_count`     | 仅 GIF：帧数 `2-64`，默认 `9`                              |
| `frame_duration_ms` | 仅 GIF：每帧时长 `20-2000` 且为 `10` 的倍数，默认 `200`          |
| `response_format` | 当前请求模型中包含该字段，默认值为 `b64_json`                       |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/edits</code></summary>
<br>

OpenAI 兼容图片编辑接口，可上传图片文件，也可按官方 JSON 格式传入图片链接并生成编辑结果。

```bash
curl http://localhost:3000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -F "model=gpt-image-2" \
  -F "prompt=把这张图改成赛博朋克夜景风格" \
  -F "n=1" \
  -F "image=@./input.png"
```

也可以直接传图片 URL：

```bash
curl http://localhost:3000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "把这张图改成赛博朋克夜景风格",
    "images": [
      {"image_url": "https://example.com/input.png"}
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段          | 说明                                            |
|:------------|:----------------------------------------------|
| `model`     | 图片模型， `gpt-image-2`                           |
| `prompt`    | 图片编辑提示词                                       |
| `n`         | 生成数量，当前后端限制为 `1-4`                            |
| `image`     | 需要编辑的图片文件，使用 multipart/form-data 上传           |
| `images`    | JSON 图片引用数组，支持 `{"image_url": "https://..."}` |
| `image_url` | 表单模式下也可直接传图片链接，支持重复字段传多张图                     |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/chat/completions</code></summary>
<br>

面向文本、网页搜索与图片场景的 Chat Completions 兼容接口，不是完整通用聊天代理。

```bash
curl http://localhost:3000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "messages": [
      {
        "role": "user",
        "content": "生成一张雨夜东京街头的赛博朋克猫"
      }
    ],
    "n": 1
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段                   | 说明                                                                           |
|:---------------------|:-----------------------------------------------------------------------------|
| `model`              | 文本、搜索或图片模型；搜索模型会触发网页搜索兼容逻辑                                                   |
| `messages`           | 消息数组，支持文本、搜索和图片请求内容                                                          |
| `n`                  | 图片生成数量，按当前实现解析为图片数量                                                          |
| `stream`             | 文本、搜索和图片场景均支持，仍在测试                                                           |
| `tools`              | 文本场景支持 `web_search` / `web_search_preview` / `web_search_preview_2025_03_11` |
| `web_search_options` | 传入时会触发网页搜索兼容逻辑                                                               |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/responses</code></summary>
<br>

面向文本、网页搜索和图片生成工具调用的 Responses API 兼容接口，不是完整通用 Responses API 代理。

```bash
curl http://localhost:3000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-5",
    "input": "生成一张未来感城市天际线图片",
    "tools": [
      {
        "type": "image_generation"
      }
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段       | 说明                                                                                      |
|:---------|:----------------------------------------------------------------------------------------|
| `model`  | 响应中会回显该模型字段，搜索和图片生成会走对应兼容逻辑                                                             |
| `input`  | 输入内容；搜索使用最后一条用户文本，图片生成需能解析出提示词                                                          |
| `tools`  | 支持 `image_generation`、`web_search`、`web_search_preview`、`web_search_preview_2025_03_11` |
| `stream` | 已实现，但仍在测试                                                                               |

<br>
</details>
</details>

## 社区支持

学 AI , 上 L 站：[LinuxDO](https://linux.do)

## 上游与致谢

本仓库基于 [basketikun/chatgpt2api](https://github.com/basketikun/chatgpt2api) 二次开发，
上游 v1.9.4 的改动已完整合并。致谢原作者及所有贡献者：

<a href="https://github.com/basketikun/chatgpt2api/graphs/contributors">
  <img alt="Contributors" src="https://contrib.rocks/image?repo=basketikun/chatgpt2api" />
</a>

[![Star History Chart](https://api.star-history.com/chart?repos=basketikun/chatgpt2api&type=date&legend=top-left)](https://www.star-history.com/?repos=basketikun%2Fchatgpt2api&type=date&legend=top-left)

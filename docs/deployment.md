# 部署与升级指南

本文介绍 ChatGPT2API 的常见部署方式，以及后续升级项目时需要保留的数据和执行步骤。

## 部署前准备

服务器需要安装：

- Docker
- Docker Compose v2
- Git

首次部署前建议确认：

```bash
docker version
docker compose version
git --version
```

### 出站网络要求

| 目标 | 用途 | 必需性 |
| --- | --- | --- |
| `chatgpt.com` | 生图、编辑、账号刷新 | **必需** |
| `registry.npmjs.org` | 构建期装前端依赖 | **必需**（仅构建期） |
| `mirrors.aliyun.com/pypi` | 构建期装 Python 依赖（`uv.lock` 里锁的源） | **必需**（仅构建期） |
| `*.r2.cloudflarestorage.com` | 存图到 Cloudflare R2 | 用 R2 存储时必需 |
| `sentinel.openai.com` | 注册功能下载 sentinel sdk.js | 用注册功能时必需 |

> 容器**启动**阶段不需要外网：Python 依赖在构建期已装好（Dockerfile 的 CMD 带
> `--frozen --no-dev`，运行时不会再去同步依赖）。

项目核心持久化文件：

| 路径 | 作用 |
| --- | --- |
| `config.json` | 主配置、后台密钥、代理、图片、备份等配置 |
| `.env` | Docker compose 环境变量 |
| `data/` | 账号、日志、图片、任务记录等运行数据 |

升级和迁移时重点保留以上内容。

## 方式一：Docker 部署

```bash
git clone https://github.com/qll-yin/chatgpt2api.git
cd chatgpt2api

# 仓库里只提交脱敏的 config.example.json，先复制一份真实配置
cp config.example.json config.json
vi config.json          # 至少改掉 auth-key

# 环境变量：端口、数据库密码、存储后端都在这里配
cp .env.example .env
vi .env                 # 至少改掉 CHATGPT2API_AUTH_KEY 和 POSTGRES_PASSWORD
```

> ⚠️ **`docker-compose.yml` 不需要改** —— 所有可调项都走 `${VAR:-默认值}`，
> 只改 `.env`。`git pull` 升级不会覆盖 `.env`。
>
> `DATABASE_URL` 里的密码与 db 服务的 `POSTGRES_PASSWORD` 引用的是**同一个变量**，
> 所以改一处两边自动一致 —— 不会出现「连接串改了、容器密码没改」的认证失败。
>
> `.env` 里没有的值用 compose 的默认值兜底，所以不填也能跑（但密码是弱密码，
> 上线前务必改）。

需要个性化更多（比如连外部数据库）时，建一个 `docker-compose.override.yml`
（不进版本库，compose 会自动合并）：

```yaml
services:
  app:
    environment:
      DATABASE_URL: postgresql://user:password@external-host:5432/dbname
```

设置 `config.json` 中的 `auth-key`，或在 `.env` 中配置：

```yaml
environment:
  - CHATGPT2API_AUTH_KEY=your_secret_key
```

启动：

```bash
docker compose up -d --build
```

访问：

```text
http://localhost:3000
```

API 基础地址：

```text
http://localhost:3000/v1
```

查看日志：

```bash
docker logs -f chatgpt2api
```

停止：

```bash
docker compose down
```

## 方式二：源码运行

适合本地开发或临时调试。

后端：

```bash
git clone https://github.com/qll-yin/chatgpt2api.git
cd chatgpt2api
cp config.example.json config.json
uv sync
uv run main.py
```

前端开发服务：

```bash
cd web
bun install
bun run dev
```

源码方式运行时，后端默认读取项目根目录的 `config.json` 和 `data/`。

## 存储后端

默认使用 **Postgres**（`docker-compose.yml` 里已内置 db 容器，开箱即用）：

```text
STORAGE_BACKEND=postgres
DATABASE_URL=postgresql://chatgpt2api:chatgpt2api@db:5432/chatgpt2api
```

可选值：

| 值 | 说明 |
| --- | --- |
| `postgres` | PostgreSQL，默认方式；compose 里带 db 服务 |
| `sqlite` | 本地 SQLite，通常存放在 `data/accounts.db` |
| `json` | 本地 JSON 文件（账号数据用 `data/accounts.json`） |
| `git` | Git 私有仓库存储账号数据 |

> ⚠️ 运行态数据（生图任务状态、图片索引、标签、可编辑文件任务、注册数据）
> **无论 `STORAGE_BACKEND` 是什么都会走数据库** —— `DATABASE_URL` 才是关键那项。
> 没配 `DATABASE_URL` 时回退到本地 SQLite：`sqlite:////app/data/chatgpt2api.db`。

**为什么默认 Postgres 而不是 SQLite**：运行态数据写入非常频繁，SQLite 在高并发写
+ WAL 模式下容易出现 `database is locked`；日均几万张图的量级下 Postgres 明显更稳。

换成 SQLite / JSON（不想多起一个容器）：

```yaml
environment:
  - STORAGE_BACKEND=sqlite
  - DATABASE_URL=sqlite:////app/data/chatgpt2api.db
# 并删掉 app.depends_on 里的 db，以及整个 db 服务
```

或者直接用现成的轻量版（SQLite，端口 8000）：

```bash
docker compose -f docker-compose.local.yml up -d --build
```

外部 PostgreSQL 示例：

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

## 图片存储（Cloudflare R2 / S3 兼容）

图片默认存在服务器本地 `data/images/`。要存到 Cloudflare R2：

1. R2 控制台 → 创建存储桶
2. R2 → 存储桶 → **S3 API 凭据** → 创建一份只对该桶有权限的凭据，
   拿到 `Access Key ID` / `Secret Access Key`，以及 Endpoint
   （`https://<AccountID>.r2.cloudflarestorage.com`）
3. 存储桶 → **公开访问** → 启用 `r2.dev` 域或绑定自定义域（拿到直链）
4. 后台「设置 → 图片存储」：

   - 勾选「启用远端图片存储」
   - 远端存储类型选 **Cloudflare R2 / S3 兼容**
   - 保存模式：`仅远端`（省磁盘）或 `本机 + 远端`
   - 填 Endpoint / Bucket / Access Key ID / Secret Access Key
   - 远端目录默认 `chatgpt2api/images`
   - 公开访问前缀填 `https://pub-xxxx.r2.dev`（或自定义域）
     → 之后返回的就是远端直链，**R2 出网流量免费**
     → 留空则走本应用 `/images/...` 代理（桶保持私有时用）
5. 点「测试连接」确认连通，再点「全量同步」把已有本地图片补传上去

### 远端过期清理

勾选「自动清理远端过期图片」并填保留天数（如 `1` = 保留 1 天）后保存，
应用会把一条生命周期规则**直接下发到桶上**：

```text
PUT /{bucket}?lifecycle
Rule: ID=chatgpt2api-expire, Filter.Prefix={远端目录}/, Expiration.Days={天数}
```

- 由 R2 服务端执行，不占应用资源、无扫描延迟，服务停了也照清
- R2 通常在过期时间后 24 小时内完成删除
- 只作用在远端目录前缀下，**桶上其它规则不受影响**
- 需要小时级精度时，另外打开「额外用应用侧扫描兜底」（会定时列桶扫描，桶大了较贵）
- 从 R2 切到 WebDAV 时，会自动把桶上残留的那条规则摘掉

### 密钥安全

`Secret Access Key` 与 `webdav_password` **明文存于 `config.json`**（该文件已在
`.gitignore` 与 `.dockerignore` 中排除，不会进镜像层）。建议：

- 只用**只有该桶权限**的 R2 凭据，不要用主账号 API Token
- `config.json` 权限收紧到 `600`
- 桶如果设公开读，图片内容本身没有访问控制 —— 内容敏感就别开公开读，留空「公开访问前缀」走应用代理

## 号池自动补充与批量注册

后台「注册管理」页内置批量注册能力，无需额外部署注册机：

- 邮箱来源：Remail 长效/短效订单、Outlook 邮箱池、CF 临时邮箱
- 代理池：静态/动态类型（**导入时可统一指定这批代理的类型**）、单代理注册配额上限、CF 拦截计数、IPv6 检测
- 批量参数：并发数、目标成功数（默认 5，设 1 = 只注册一个）、冷却时间、验证码超时、失败重试次数
- **注册使用代理**：注册时只挑代理池里的某一类代理（只用动态 / 只用静态 / 混合随机）
- **入池代理**：注册成功后账号进号池时绑哪个（携带静态代理 / 携带注册所用代理 / 不带代理）—— 动态代理永远不进号池
- **注册成功自动写入「号池管理」**（chatgpt2api 账号池）；注册用的是静态代理时，入池沿用同一条静态代理
- 号池自动补充：可用账号低于阈值时自动触发注册（守护进程内跑，不需要外部 HTTP 服务）

失败重试按**整个流程**算：一次「开始注册」= 一遍完整流程，失败后换一个出口 IP、
用同一个号从头再走一遍完整流程，连续 N 遍都失败才停掉这个 worker。
失败数按**号**统计（重试不重复计数），界面另外显示「共尝试 N 遍」。
点「停止」是**当前号跑完再停**：正在跑的那一遍会走完（成功照常入池，失败把号放回号池）。

前置条件：

- 容器能访问 `sentinel.openai.com`（注册时下载 sentinel sdk.js，缓存在 `data/sentinel_cache`）
- 配置了可用的邮箱来源与代理
- 选了「只用动态 / 只用静态」时，代理池里必须有对应类型的代理，否则注册会明确报错停下

## 升级前备份

升级前建议备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

如果没有 `.env`，可以去掉：

```bash
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json data
```

也可以在后台设置页配置 Cloudflare R2 备份，用于定时备份关键数据。

## 升级：普通 Docker 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码和镜像：

```bash
git pull
docker compose up -d --build
```

> `git pull` **不会覆盖** `.env` 和 `config.json`（都在 `.gitignore` 里），
> 也不会覆盖 `docker-compose.override.yml`（同样在 `.gitignore` 里）。
> 升级后配置依然生效。

查看状态：

```bash
docker compose ps
docker logs -f chatgpt2api
```

### 本次升级的注意事项

- **需要重新构建镜像**：新增了 `boto3` 依赖（Cloudflare R2 / S3 存储），
  `uv.lock` 已更新。`docker compose up -d --build` 会自动装上。
- **`image_storage` 配置结构变了，但老配置不用改**：
  - `mode` 从 `local / webdav / both` 改成 `local / remote / both`
  - 新增 `provider`（`webdav` / `s3`）表示远端协议
  - `webdav_root_path` 改名为 `remote_root_path`（两个协议共用）
  - 后端读取时会自动把老结构映射成新结构，**已有的 WebDAV 配置照常生效**
- **图片索引表会自动补列**：数据库启动时自动给 `image_records` 加远端相关字段并
  迁移老数据，不需要手工执行 SQL。
- 如果升级前用的是 WebDAV，升级后远端存储类型默认仍保持 WebDAV，不受影响。

## 升级：源码运行

```bash
cd chatgpt2api
git pull
uv sync
```

如果需要重新构建前端静态产物：

```bash
cd web
bun install
bun run build
```

然后按你的进程管理方式重启后端服务。

## 回滚

如果升级后需要回滚代码：

```bash
git log --oneline -n 20
git checkout <旧版本commit>
```

Docker 部署：

```bash
docker compose up -d --build
```

如果需要恢复数据：

```bash
tar -xzf backups/你的备份文件.tgz
```

恢复数据前建议先停止容器，避免运行中写入覆盖：

```bash
docker compose down
```


## 常用维护命令

查看容器：

```bash
docker compose ps
```

查看主服务日志：

```bash
docker logs -f chatgpt2api
```

重启普通部署：

```bash
docker compose restart
```

清理未使用镜像：

```bash
docker image prune
```

不要直接删除 `data/`、`config.json`、`.env`，除非已经确认有可用备份。

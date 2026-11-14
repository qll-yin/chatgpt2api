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

## 方式一：普通 Docker 部署

适合不需要 WARP / FlareSolverr 清障的场景。

```bash
git clone https://github.com/qll-yin/chatgpt2api.git
cd chatgpt2api

# 仓库里只提交脱敏的 config.example.json，先复制一份真实配置
cp config.example.json config.json
vi config.json          # 至少改掉 auth-key
```

设置 `config.json` 中的 `auth-key`，或在 `docker-compose.yml` 中配置：

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

## 方式二：WARP / FlareSolverr 部署

适合上游请求经常遇到 Cloudflare 拦截的场景。该方式会启动：

- `warp-proxy`
- `privoxy`
- `flaresolverr`
- `init-config`
- `app`

复制环境变量模板：

```bash
cp .env.example .env
```

至少修改 `.env` 中的：

```text
CHATGPT2API_AUTH_KEY=your_secret_key_here
```

启动：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

访问：

```text
http://localhost:3000
```

FlareSolverr 相关配置可以在后台设置页的 `FlareSolverr` tab 中查看和测试。

查看容器状态：

```bash
docker compose -f docker-compose.warp.yml ps
```

查看日志：

```bash
docker logs -f chatgpt2api-warp
docker logs -f chatgpt2api-flaresolverr
```

停止：

```bash
docker compose -f docker-compose.warp.yml down
```

## 方式三：源码运行

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

默认使用本地 JSON 文件：

```text
STORAGE_BACKEND=json
```

可选值：

| 值 | 说明 |
| --- | --- |
| `json` | 本地 JSON 文件，默认方式 |
| `sqlite` | 本地 SQLite，通常存放在 `data/accounts.db` |
| `postgres` | 外部 PostgreSQL |
| `git` | Git 私有仓库存储账号数据 |

PostgreSQL 示例：

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

SQLite 示例：

```yaml
environment:
  - STORAGE_BACKEND=sqlite
  - DATABASE_URL=sqlite:////app/data/accounts.db
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
- 代理池：静态/动态类型、单代理注册配额上限、CF 拦截计数、IPv6 检测
- 批量参数：并发数、目标成功数、冷却时间、验证码超时
- **注册成功自动写入「号池管理」**（chatgpt2api 账号池），并携带注册时用的代理
- 号池自动补充：可用账号低于阈值时自动触发注册（守护进程内跑，不需要外部 HTTP 服务）

前置条件：

- 容器能访问 `sentinel.openai.com`（注册时下载 sentinel sdk.js，缓存在 `data/sentinel_cache`）
- 配置了可用的邮箱来源与代理

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

## 升级：WARP / FlareSolverr 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码并重新构建：

```bash
git pull
docker compose -f docker-compose.warp.yml up -d --build
```

查看状态：

```bash
docker compose -f docker-compose.warp.yml ps
docker logs -f chatgpt2api-warp
```

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

普通 Docker 部署：

```bash
docker compose up -d --build
```

WARP / FlareSolverr 部署：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

如果需要恢复数据：

```bash
tar -xzf backups/你的备份文件.tgz
```

恢复数据前建议先停止容器，避免运行中写入覆盖：

```bash
docker compose down
```

或：

```bash
docker compose -f docker-compose.warp.yml down
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

查看 WARP 部署主服务日志：

```bash
docker logs -f chatgpt2api-warp
```

重启普通部署：

```bash
docker compose restart
```

重启 WARP 部署：

```bash
docker compose -f docker-compose.warp.yml restart
```

清理未使用镜像：

```bash
docker image prune
```

不要直接删除 `data/`、`config.json`、`.env`，除非已经确认有可用备份。

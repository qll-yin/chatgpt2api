ARG BUILDPLATFORM
ARG TARGETPLATFORM
ARG TARGETARCH

# ── 阶段 1：构建前端 ────────────────────────────────────────────────
# node:22-alpine 只用于这一阶段（npm ci + npm run build），不进最终镜像。
FROM --platform=$BUILDPLATFORM node:22-alpine AS web-build

WORKDIR /app/web

COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md
COPY web ./
RUN NEXT_PUBLIC_APP_VERSION="$(cat /app/VERSION)" npm run build


# ── 阶段 2：运行时 ──────────────────────────────────────────────────
FROM --platform=$TARGETPLATFORM python:3.13-slim AS app

ARG TARGETPLATFORM
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    OPENAI_SENTINEL_CACHE_DIR=/app/data/sentinel_cache

WORKDIR /app

# 安装系统依赖
# - git: Git 存储后端需要
# - libpq-dev: PostgreSQL 客户端库
# - gcc: 编译 psycopg2-binary 需要
# - libstdc++6: node 二进制的动态依赖（node 官方 tarball 不自带）
# - ca-certificates: HTTPS 出站（boto3 连 R2 / S3、curl_cffi 连上游都要验签）
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    gcc \
    openssl \
    ca-certificates \
    libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# ── node 运行时（注册功能的 sentinel 需要）────────────────────────────
#
# 【为什么必须有 node】
#   Sentinel token 用 JS VM 方案：纯 Python 算的合成 PoW 只能骗过 OpenAI 的
#   浅层校验（HTTP 200），但真正派发验证码的服务端会跑真的 sentinel SDK JS
#   来验 token。合成 token 过不了深层校验 → **验证码邮件被静默丢弃**
#   （表现为注册跑到最后一步收不到码）。必须真在 JS VM 里跑一遍
#   OpenAI 的 sdk.js → 需要 `node <script>`。
#
#   （sentinel_quickjs.py 用到的 node:vm / node:crypto / Buffer 全是 Node 特有，
#    pip 的 quickjs 绑定没有 vm 模块，替代不了。）
#
# 【为什么不能直接用上面 web-build 阶段的 node:22-alpine】
#   1. libc 不兼容 —— alpine 是 musl libc，最终镜像是 glibc（debian slim），
#      二进制拷过去跑不起来（缺 ld-musl / symbol 错误）。
#   2. 架构不对 —— web-build 标了 --platform=$BUILDPLATFORM，跨 stage COPY
#      拿到的是**构建机**架构。Apple Silicon 上构建 amd64 镜像时架构就错了。
#
# 【所以从外部镜像取 —— 它自动走 TARGETPLATFORM】
#   只 COPY 一个可执行文件（~110MB），不要 npm / node_modules / corepack：
#   sentinel_quickjs.py 只是 `node <script>`，不需要 npm。
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node

# ⚠️ 构建期就验证 node 能跑 —— 缺动态库（比如 libstdc++6）的话，
#    在 build 阶段就会报错，而不是等到跑注册时才发现 sentinel 静默失败
#    （验证码收不到，排查起来非常费时间）。
RUN node -e "console.log('node ok:', process.version)"

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py ./
# 配置：镜像内只放脱敏的 config.example.json 作为兜底（真实 config.json
# 已被 .gitignore 排除，且仓库里本来就没有这个文件）。
# 运行时由 compose 挂载真实的 ./config.json 到 /app/config.json 覆盖。
COPY config.example.json ./config.json
COPY VERSION ./
COPY api ./api
COPY services ./services
COPY utils ./utils
COPY scripts ./scripts
COPY --from=web-build /app/web/out ./web_dist

EXPOSE 80

# ⚠️ 必须带 --frozen --no-dev：
#
#   `uv run` 默认会**重新同步环境**并把 dev 依赖组（httpx/httpcore）也装上。
#   上面 `uv sync --no-dev` 明确排除了 dev 依赖，镜像里本来没有 httpx 的
#   wheel 缓存 —— 运行时 `uv run` 要联网下载。
#   服务器无外网 / 出网被限时，容器会**卡在启动阶段**（装不上依赖直接退出）。
#
#   --frozen：不重新解析 lock（构建时已经装好，lock 与 pyproject 一致）
#   --no-dev：不装 dev 依赖（生产运行不需要）
#
#   实测：不带这两个参数时每次启动都会打印 "Installed 2 packages"；
#   带上之后启动零安装，纯离线也能起。
CMD ["uv", "run", "--frozen", "--no-dev", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--access-log"]

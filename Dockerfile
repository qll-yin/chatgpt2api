ARG BUILDPLATFORM
ARG TARGETPLATFORM
ARG TARGETARCH

FROM --platform=$BUILDPLATFORM node:22-alpine AS web-build

WORKDIR /app/web

# ⚠️ 用 package-lock.json（npm 格式）而不是 web/bun.lock ——
#    bun.lock 虽然也是 lockfileVersion 1 的 JSON，但它是 bun 生成的，
#    字段语义和 npm 的不完全一致；用 npm install 却只 COPY bun.lock
#    等于没锁版本，构建结果会随时间漂移。
#    package-lock.json 才是 npm install --frozen-lockfile 认的。
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md
COPY web ./
RUN NEXT_PUBLIC_APP_VERSION="$(cat /app/VERSION)" npm run build


FROM --platform=$TARGETPLATFORM python:3.13-slim AS app

ARG TARGETPLATFORM
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 安装系统依赖
# - git: Git 存储后端需要
# - libpq-dev: PostgreSQL 客户端库
# - gcc: 编译 psycopg2-binary 需要
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    gcc \
    openssl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py ./
# ⚠️ 用 config.example.json（脱敏模板），不用 config.json：
#    config.json 含真实的 auth-key / backup.secret_access_key，
#    被 .gitignore 挡着不会进仓库 —— 服务器上 git clone 之后根本没有这个文件，
#    直接 COPY config.json 会让**构建直接失败**。
#    运行时 compose 会把宿主机的真实 config.json 挂进来覆盖：
#      volumes: [ "./config.json:/app/config.json" ]
#    模板里的 auth-key 是空的，用环境变量 CHATGPT2API_AUTH_KEY 也能覆盖。
COPY config.example.json ./config.json
COPY VERSION ./
COPY api ./api
COPY services ./services
COPY utils ./utils
COPY scripts ./scripts
COPY --from=web-build /app/web/out ./web_dist

EXPOSE 80

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--access-log"]

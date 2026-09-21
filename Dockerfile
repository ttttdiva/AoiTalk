# syntax=docker/dockerfile:1.7
# ============================================================================
# AoiTalk Dockerfile for Linux/WSL2/Enterprise
# マルチステージビルドによる最適化されたDockerイメージ
# pyproject.toml ベースで core 依存のみインストール（audio/windows/irodori 不要）
# ============================================================================

# =============================================================================
# Stage 1: Frontend Builder - Next.js ビルド
# =============================================================================
FROM scratch AS npm-cache
# Offline/restricted-network builds bind a verified companion directory to
# this stage.  The stage stays empty for the normal online path so existing
# `npm ci` behaviour is unchanged.
FROM scratch AS offline-inputs

FROM node@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS enterprise-node-base

FROM enterprise-node-base AS frontend-builder

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./frontend/
WORKDIR /app/frontend
ARG NPM_INSTALL_MODE=online
ARG AOITALK_OFFLINE_BUILD=0
# Online is the default (`RUN npm ci`); the target launcher opts into the
# offline cache only when an operator supplies an external named context.
RUN --mount=type=bind,from=npm-cache,target=/tmp/npm-cache-input,ro \
    --mount=type=bind,from=offline-inputs,target=/tmp/enterprise-offline,ro \
    if [ "$NPM_INSTALL_MODE" = "online" ]; then \
        npm ci; \
    elif [ "$NPM_INSTALL_MODE" = "offline" ]; then \
        rm -rf /tmp/npm-cache && mkdir -p /tmp/npm-cache && \
        cp -a /tmp/npm-cache-input/. /tmp/npm-cache/ && \
        npm ci --offline --cache=/tmp/npm-cache; \
    else \
        echo "unsupported NPM_INSTALL_MODE: $NPM_INSTALL_MODE" >&2; \
        exit 1; \
    fi
WORKDIR /app
COPY frontend/ ./frontend/
RUN --mount=type=secret,id=nextauth_secret \
    cd frontend && \
    NEXTAUTH_SECRET="$(cat /run/secrets/nextauth_secret)" npm run build:production

# =============================================================================
# Stage 2: Python Builder - 依存関係のビルド
# =============================================================================
FROM python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS enterprise-python-base

FROM enterprise-python-base AS builder

ARG AOITALK_OFFLINE_BUILD=0

COPY --chmod=0755 docker/ensure-https-apt-sources.sh /usr/local/sbin/ensure-https-apt-sources
COPY --chmod=0755 docker/install-enterprise-system-deps.sh /usr/local/sbin/install-enterprise-system-deps

# ビルド用システム依存関係
RUN /usr/local/sbin/ensure-https-apt-sources
RUN --mount=type=bind,from=offline-inputs,target=/tmp/enterprise-offline,ro \
    if [ "$AOITALK_OFFLINE_BUILD" = "1" ]; then \
        test -d /tmp/enterprise-offline/apt/archives && \
        test -d /tmp/enterprise-offline/apt/lists && \
        cp -a /tmp/enterprise-offline/apt/lists/. /var/lib/apt/lists/ && \
        cp -a /tmp/enterprise-offline/apt/archives/. /var/cache/apt/archives/ && \
        /usr/local/sbin/install-enterprise-system-deps /tmp/enterprise-offline builder; \
    else \
        apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            build-essential cmake libssl-dev libffi-dev libpq-dev \
            portaudio19-dev libsndfile1-dev git; \
    fi && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# pyproject.toml ベースで依存関係とアプリケーションの wheel をビルド
# Docker環境では core 依存のみ（audio/windows/irodori は extras に含めない）
COPY pyproject.toml README.enterprise.md ./
RUN cp README.enterprise.md README.md
COPY src/ src/
RUN --mount=type=bind,from=offline-inputs,target=/tmp/enterprise-offline,ro \
    if [ "$AOITALK_OFFLINE_BUILD" = "1" ]; then \
        test -d /tmp/enterprise-offline/python-wheels && \
        test -f /tmp/enterprise-offline/python/build-requirements.lock && \
        pip install --no-cache-dir --no-index --require-hashes \
            --find-links=/tmp/enterprise-offline/python-wheels \
            -r /tmp/enterprise-offline/python/build-requirements.lock && \
        pip wheel --no-cache-dir --no-index --no-build-isolation \
            --find-links=/tmp/enterprise-offline/python-wheels --wheel-dir /wheels \
            --no-deps . && \
        pip wheel --no-cache-dir --no-index --no-build-isolation --require-hashes \
            --find-links=/tmp/enterprise-offline/python-wheels --wheel-dir /wheels \
            -r /tmp/enterprise-offline/python/runtime-requirements.lock; \
    else \
        pip wheel --no-cache-dir --wheel-dir /wheels .; \
    fi

# =============================================================================
# Stage 3: Runtime - 実行環境
# =============================================================================
FROM enterprise-python-base AS runtime

ARG AOITALK_OFFLINE_BUILD=0

COPY --chmod=0755 docker/ensure-https-apt-sources.sh /usr/local/sbin/ensure-https-apt-sources
COPY --chmod=0755 docker/install-enterprise-system-deps.sh /usr/local/sbin/install-enterprise-system-deps

# Node.js インストール（Next.js実行用）
RUN /usr/local/sbin/ensure-https-apt-sources
RUN --mount=type=bind,from=offline-inputs,target=/tmp/enterprise-offline,ro \
    if [ "$AOITALK_OFFLINE_BUILD" = "1" ]; then \
        test -d /tmp/enterprise-offline/apt/archives && \
        test -d /tmp/enterprise-offline/apt/lists && \
        cp -a /tmp/enterprise-offline/apt/lists/. /var/lib/apt/lists/ && \
        cp -a /tmp/enterprise-offline/apt/archives/. /var/cache/apt/archives/ && \
        /usr/local/sbin/install-enterprise-system-deps /tmp/enterprise-offline runtime; \
    else \
        apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            libportaudio2 libportaudiocpp0 ffmpeg sox libsox-fmt-all \
            libsndfile1 libpq5 postgresql-client fonts-noto-cjk \
            fonts-noto-cjk-extra locales tzdata curl ca-certificates gosu; \
    fi && rm -rf /var/lib/apt/lists/*

# Node.js 22 インストール（Next.jsランタイム用）
ARG NODESOURCE_SETUP_URL=https://deb.nodesource.com/setup_22.x
ARG NODESOURCE_SETUP_SHA256=575583bbac2fccc0b5edd0dbc03e222d9f9dc8d724da996d22754d6411104fd1
RUN --mount=type=bind,from=offline-inputs,target=/tmp/enterprise-offline,ro \
    if [ "$AOITALK_OFFLINE_BUILD" = "1" ]; then \
        test -f /tmp/enterprise-offline/nodesource/setup_22.x && \
        cp /tmp/enterprise-offline/nodesource/setup_22.x /tmp/nodesource_setup.sh && \
        echo "$NODESOURCE_SETUP_SHA256  /tmp/nodesource_setup.sh" | sha256sum -c - && \
        test -f /tmp/enterprise-offline/nodesource/sources.list && \
        install -m 0644 /tmp/enterprise-offline/nodesource/sources.list /etc/apt/sources.list.d/nodesource.list && \
        test -f /tmp/enterprise-offline/nodesource/nodesource.gpg && \
        install -m 0644 /tmp/enterprise-offline/nodesource/nodesource.gpg /usr/share/keyrings/nodesource.gpg && \
        /usr/local/sbin/ensure-https-apt-sources && \
        cp -a /tmp/enterprise-offline/apt/lists/. /var/lib/apt/lists/ && \
        cp -a /tmp/enterprise-offline/apt/archives/. /var/cache/apt/archives/ && \
        /usr/local/sbin/install-enterprise-system-deps /tmp/enterprise-offline nodejs; \
    else \
        /usr/local/sbin/ensure-https-apt-sources && \
        curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location "$NODESOURCE_SETUP_URL" -o /tmp/nodesource_setup.sh && \
        echo "$NODESOURCE_SETUP_SHA256  /tmp/nodesource_setup.sh" | sha256sum -c - && \
        bash /tmp/nodesource_setup.sh && \
        rm -f /tmp/nodesource_setup.sh && \
        /usr/local/sbin/ensure-https-apt-sources && \
        DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs; \
    fi && rm -f /tmp/nodesource_setup.sh && rm -rf /var/lib/apt/lists/*

# 日本語ロケール設定
RUN sed -i '/ja_JP.UTF-8/s/^# //g' /etc/locale.gen && \
    locale-gen ja_JP.UTF-8
ENV LANG=ja_JP.UTF-8 \
    LANGUAGE=ja_JP:ja \
    LC_ALL=ja_JP.UTF-8

# 非rootユーザー作成
RUN useradd -m -s /bin/bash -u 1000 aoitalk && \
    mkdir -p /app && \
    chown aoitalk:aoitalk /app

WORKDIR /app

# Wheelからパッケージインストール（依存関係も含める）
# The final pip check makes a missing runtime-lock dependency fail closed in
# both restricted and normal builds.
COPY --from=builder /wheels /wheels
RUN --mount=type=bind,from=offline-inputs,target=/tmp/enterprise-offline,ro \
    if [ "$AOITALK_OFFLINE_BUILD" = "1" ]; then \
        test -f /tmp/enterprise-offline/python/runtime-requirements.lock && \
        pip install --no-cache-dir --no-index --require-hashes \
            --find-links=/wheels -r /tmp/enterprise-offline/python/runtime-requirements.lock && \
        pip install --no-cache-dir --no-index --no-deps --find-links=/wheels aoitalk; \
    else \
        pip install --no-cache-dir --no-index --find-links=/wheels aoitalk; \
    fi && \
    pip check && \
    rm -rf /wheels

# アプリケーションコードをコピー
COPY --chown=aoitalk:aoitalk . .

# Next.js ビルド出力をコピー
COPY --from=frontend-builder --chown=aoitalk:aoitalk /app/frontend/.next /app/frontend/.next
COPY --from=frontend-builder --chown=aoitalk:aoitalk /app/frontend/node_modules /app/frontend/node_modules
COPY --from=frontend-builder --chown=aoitalk:aoitalk /app/frontend/package.json /app/frontend/package.json

# The Enterprise entrypoint crosses the Docker-secret boundary as root, then
# drops to the application user before starting Python/Next.js.
RUN install -m 0755 -o root -g root /app/docker/entrypoint.enterprise.sh /usr/local/bin/aoitalk-entrypoint

# 必要なディレクトリ作成
RUN mkdir -p /app/logs /app/cache /app/workspaces /app/temp && \
    chown -R aoitalk:aoitalk /app/logs /app/cache /app/workspaces /app/temp

# 環境変数設定
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AIVTUBER_ENV=enterprise \
    AOITALK_PROFILE=enterprise \
    AOITALK_DOCKER=true \
    AOITALK_REQUIRE_DATABASE=true \
    AOITALK_REQUIRE_AUTH_SECRET=true \
    AOITALK_SKIP_CADDY=true \
    AOITALK_HEADLESS=true \
    AOITALK_WEB_HOST=0.0.0.0 \
    AOITALK_NEXT_HOST=0.0.0.0 \
    AOITALK_FRONTEND_HOST=0.0.0.0 \
    # Docker内ではブラウザ自動起動しない
    AOITALK_WEB_AUTO_OPEN=false \
    # Next.js設定
    NEXTJS_URL=http://127.0.0.1:3002

# Secret files are mounted with Docker-managed permissions.  The entrypoint
# reads them before dropping to the non-root application user.
USER root

# ポート公開（3000: FastAPI, 3002: Next.js）
EXPOSE 3000 3002

# ヘルスチェック
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:3000/health && curl -f http://localhost:3002/login || exit 1

# エントリーポイント
ENTRYPOINT ["/usr/local/bin/aoitalk-entrypoint"]

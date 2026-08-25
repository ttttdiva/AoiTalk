# syntax=docker/dockerfile:1.7
# ============================================================================
# AoiTalk Dockerfile for Linux/WSL2/Enterprise
# マルチステージビルドによる最適化されたDockerイメージ
# pyproject.toml ベースで core 依存のみインストール（audio/windows/irodori 不要）
# ============================================================================

# =============================================================================
# Stage 1: Frontend Builder - Next.js ビルド
# =============================================================================
FROM node@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS frontend-builder

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./frontend/
WORKDIR /app/frontend
RUN npm ci
WORKDIR /app
COPY frontend/ ./frontend/
RUN --mount=type=secret,id=nextauth_secret \
    cd frontend && \
    NEXTAUTH_SECRET="$(cat /run/secrets/nextauth_secret)" npm run build:production

# =============================================================================
# Stage 2: Python Builder - 依存関係のビルド
# =============================================================================
FROM python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS builder

# ビルド用システム依存関係
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    libssl-dev \
    libffi-dev \
    libpq-dev \
    portaudio19-dev \
    libsndfile1-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# pyproject.toml ベースで依存関係とアプリケーションの wheel をビルド
# Docker環境では core 依存のみ（audio/windows/irodori は extras に含めない）
COPY pyproject.toml README.enterprise.md ./
RUN cp README.enterprise.md README.md
COPY src/ src/
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# =============================================================================
# Stage 3: Runtime - 実行環境
# =============================================================================
FROM python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

# Node.js インストール（Next.js実行用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 音声処理
    libportaudio2 \
    libportaudiocpp0 \
    ffmpeg \
    sox \
    libsox-fmt-all \
    libsndfile1 \
    # PostgreSQL クライアント
    libpq5 \
    postgresql-client \
    # 日本語フォント
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    # ロケール
    locales \
    # ユーティリティ
    curl \
    ca-certificates \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 インストール（Next.jsランタイム用）
ARG NODESOURCE_SETUP_URL=https://deb.nodesource.com/setup_22.x
ARG NODESOURCE_SETUP_SHA256=575583bbac2fccc0b5edd0dbc03e222d9f9dc8d724da996d22754d6411104fd1
RUN curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location "$NODESOURCE_SETUP_URL" -o /tmp/nodesource_setup.sh && \
    echo "$NODESOURCE_SETUP_SHA256  /tmp/nodesource_setup.sh" | sha256sum -c - && \
    bash /tmp/nodesource_setup.sh && \
    rm -f /tmp/nodesource_setup.sh && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

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
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels aoitalk && \
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

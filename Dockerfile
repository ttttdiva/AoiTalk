# ============================================================================
# AoiTalk Dockerfile for Linux/WSL2/Enterprise
# マルチステージビルドによる最適化されたDockerイメージ
# pyproject.toml ベースで core 依存のみインストール（audio/windows/qwen3 不要）
# ============================================================================

# =============================================================================
# Stage 1: Builder - 依存関係のビルド
# =============================================================================
FROM python:3.11-slim-bookworm AS builder

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
# Docker環境では core 依存のみ（audio/windows/qwen3 は extras に含めない）
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# =============================================================================
# Stage 2: Runtime - 実行環境
# =============================================================================
FROM python:3.11-slim-bookworm

# ランタイム依存関係
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
    && rm -rf /var/lib/apt/lists/*

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

# 必要なディレクトリ作成
RUN mkdir -p /app/logs /app/cache /app/workspaces /app/temp && \
    chown -R aoitalk:aoitalk /app/logs /app/cache /app/workspaces /app/temp

# 環境変数設定
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AOITALK_HEADLESS=true \
    # Docker内ではブラウザ自動起動しない
    AOITALK_WEB_AUTO_OPEN=false

# ユーザー切り替え
USER aoitalk

# ポート公開
EXPOSE 3000

# ヘルスチェック
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:3000/health || exit 1

# エントリーポイント
ENTRYPOINT ["python", "main.py"]
CMD ["--mode", "terminal"]

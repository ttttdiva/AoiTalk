# AoiTalk Docker セットアップガイド

このガイドでは、AoiTalkをLinux/WSL2 Docker環境で実行する方法を説明します。

## 前提条件

- Docker Engine 20.10 以上
- Docker Compose 2.0 以上
- (オプション) NVIDIA GPU + nvidia-container-toolkit (Whisper高速化用)

### WSL2での確認
```bash
# Dockerのバージョン確認
docker --version
docker compose version
```

## クイックスタート

### 1. 環境変数の設定

`.env.sample` を `.env` にコピーして、必要なAPIキーを設定します：

```bash
cp .env.sample .env
```

最低限必要な設定：
```env
# LLM プロバイダー (少なくとも1つ必須)
GEMINI_API_KEY=your-gemini-api-key
# または
OPENAI_API_KEY=your-openai-api-key

# PostgreSQLパスワード (例: change-me)
POSTGRES_PASSWORD=your-secure-password

# WebUI認証 (外部公開時は必須)
AOITALK_WEB_AUTH_USER=admin
AOITALK_WEB_AUTH_PASSWORD=your-password
AOITALK_WEB_AUTH_SECRET=random-secret-string
```

### 2. Docker Compose で起動

```bash
# 基本起動 (PostgreSQL + Qdrant + AoiTalk)
docker compose up -d

# VOICEVOX付きで起動
docker compose --profile voicevox up -d

# GPU版VOICEVOX (NVIDIA GPU必須)
docker compose --profile voicevox-gpu up -d
```

### 3. アクセス確認

```bash
# ヘルスチェック
curl http://localhost:3000/health

# ログ確認
docker compose logs -f aoitalk
```

ブラウザで http://localhost:3000 にアクセスしてWebUIを開きます。

## サービス構成

| サービス | ポート | 説明 |
|----------|--------|------|
| aoitalk | 3000 | メインアプリケーション (WebUI) |
| postgres | 5432 | PostgreSQL + pgvector (会話履歴) |
| qdrant | 6333, 6334 | ベクトルDB (RAG) |
| voicevox | 50021 | VOICEVOX Engine (オプション) |

## 設定

### Docker用設定ファイル

Docker環境では `config/config.docker.yaml` を使用することを推奨します。
この設定ファイルには以下が事前設定されています：

- サービス名によるホスト解決 (`postgres`, `qdrant`, `voicevox`)
- headlessモード有効化
- ブラウザ自動起動無効化

使用方法：
```bash
# config.yamlをバックアップしてDocker用設定を使用
cp config/config.yaml config/config.yaml.bak
cp config/config.docker.yaml config/config.yaml
```

または、環境変数でホストを上書きできます（docker-compose.ymlで自動設定済み）。

### キャラクター設定

Docker環境では以下のTTSエンジンのみ使用可能です：

| エンジン | 状態 | 備考 |
|----------|------|------|
| VOICEVOX | ✅ 利用可能 | docker compose --profile voicevox で起動 |
| AivisSpeech | ⚠️ 要ビルド | 公式Dockerイメージなし |
| にじボイス | ✅ 利用可能 | クラウドAPI (要NIJIVOICE_API_KEY) |
| VOICEROID | ❌ 利用不可 | Windows専用 |
| A.I.VOICE | ❌ 利用不可 | Windows専用 |
| CeVIO AI | ❌ 利用不可 | Windows専用 |

キャラクター設定ファイル (`config/characters/*.yaml`) で使用するエンジンを確認してください：

```yaml
# VOICEVOX使用例 (ずんだもん)
voice:
  engine: voicevox
  speaker_id: 3  # ずんだもん

# にじボイス使用例
voice:
  engine: nijivoice
  voice_id: "dba2fa0e-f750-43ad-b9f6-d5aeaea7dc16"
```

## ボリュームとデータ永続化

以下のデータはDockerボリュームまたはバインドマウントで永続化されます：

| パス | 内容 |
|------|------|
| `./config` | 設定ファイル (読み取り専用マウント) |
| `./workspaces` | ユーザーデータ |
| `./cache` | キャッシュファイル (埋め込みモデル等) |
| `./logs` | ログファイル |
| `postgres_data` | PostgreSQLデータ (Dockerボリューム) |
| `qdrant_data` | Qdrantベクトルデータ (Dockerボリューム) |

## よくある問題

### PostgreSQL接続エラー

```
Connection refused
```

**解決策**: PostgreSQLコンテナが起動完了するまで待ちます。
```bash
# ヘルスチェック確認
docker compose ps
# postgresが"healthy"になるまで待機
```

### VOICEVOX接続エラー

```
Failed to connect to VOICEVOX engine
```

**解決策**: VOICEVOXプロファイルで起動しているか確認します。
```bash
docker compose --profile voicevox up -d
```

### 音声が再生されない

Docker内では物理的な音声出力ができません。これは仕様です。

**対応方法**:
- WebUIでテキストチャットを使用
- TTS音声はWebSocket経由でブラウザに送信され、ブラウザで再生されます

### メモリ不足

WhisperやBGE-M3などの大きなモデルを使用する場合、十分なメモリが必要です。

**推奨**: 16GB以上のRAM

メモリ制限を設定する場合：
```yaml
# docker-compose.yml
services:
  aoitalk:
    deploy:
      resources:
        limits:
          memory: 8G
```

## 開発・デバッグ

### コンテナ内でシェルを開く

```bash
docker compose exec aoitalk bash
```

### ログをリアルタイム表示

```bash
docker compose logs -f aoitalk
```

### データベースに直接接続

```bash
docker compose exec postgres psql -U aoitalk -d aoitalk_memory
```

### イメージの再ビルド

コードを変更した後：
```bash
docker compose build --no-cache aoitalk
docker compose up -d
```

## サービスの停止・削除

```bash
# サービス停止
docker compose down

# サービス停止 + ボリューム削除 (データも消去)
docker compose down -v

# 特定のプロファイルを含めて停止
docker compose --profile voicevox down
```

## GPU対応 (オプション)

NVIDIA GPUを使用してWhisper等を高速化する場合：

### 1. nvidia-container-toolkitのインストール

```bash
# WSL2/Ubuntu
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | sudo tee /etc/apt/sources.list.d/nvidia-docker.list

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart docker
```

### 2. Docker Composeでの設定

`docker-compose.yml` にGPU設定を追加：

```yaml
services:
  aoitalk:
    # ... 既存の設定 ...
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

### 3. 確認

```bash
docker compose exec aoitalk nvidia-smi
```

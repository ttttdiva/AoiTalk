# AoiTalk

[日本語](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md)

**一款可以与语音合成角色对话，并在同一个工作区中管理任务、日程、知识和文件的 AI 助手。**

AoiTalk 可以把配置好的语音合成角色作为 AI 助手的“声音与角色界面”，将文字或语音对话与日常、工作中的信息管理连接起来。

它不只是把 AI 回复朗读出来的聊天应用。AoiTalk 可以保留对话和项目上下文，并在同一个环境中处理 **任务管理、日程管理、Docs / Files、知识检索、Agent 以及外部工具调用**。

## AoiTalk 能做什么

### 与语音合成角色对话

可以为每个角色单独设置声音，并让 AI 的回复使用该角色的声音进行朗读。AoiTalk 也支持语音输入，因此既可以作为文字聊天工具，也可以作为真正的语音对话界面使用。

尤其是，AoiTalk 同时支持 **VOICEROID2** 和 **Irodori-TTS**。

- **VOICEROID2** — 可将 VOICEROID2 角色作为 AoiTalk 的语音使用
- **Irodori-TTS** — 已集成本地 TTS。除默认的 v4.1 Small 外，还可按角色选择 v4.1 Anime、v3 VoiceDesign 等模型
- **Gemini TTS** — 可按角色设置音色与朗读风格，使用 Gemini 3.8 Flash TTS / Flash-Lite TTS（[Gemini TTS 指南](docs/gemini_tts.md)）
- **其他 TTS** — VOICEVOX、AivisSpeech、A.I.VOICE、CeVIO、Nijivoice、Azure TTS、gTTS、MioTTS 等
- **语音识别** — Whisper、Parakeet、Google Speech、Gemini 系 ASR 等

有关 Irodori-TTS 的详细信息，请参阅 [Irodori-TTS 指南](docs/irodori_tts.md)。

### 让角色帮你管理任务和日程

任务与计划可以直接在对话所在的同一个工作区中持续管理。

- 创建、更新和完成任务
- 日程、重复任务和通知
- 工作时间记录
- 报告与进度确认
- 与对话和项目上下文结合的管理

### 积累并检索知识

信息不必只停留在聊天记录中。AoiTalk 可以保存 Docs、Files、项目信息等内容，并在需要时让 AI 检索和引用。

- Docs / project information
- 文件管理与引用
- Office 文档与 PDF 读取
- ClipIngest 信息导入
- 基于 Qdrant 的知识检索
- 对话历史搜索

### 使用 Agent 和外部工具

AoiTalk 并不是只有一个聊天界面的应用。它可以组合 Agent 和外部服务来推进实际工作。

- Execution Team / specialist 执行
- Heartbeat / scheduled execution
- MCP
- Web 搜索
- Google Calendar
- Discord Bot
- Spotify
- 其他外部工具集成

可用的 provider、model 和 tool 会随当前配置与实现而变化。请以应用内设置、catalog 和 `src/config_defaults.py` 等为准。

### 同时使用 Web 与原生 Mobile 客户端

- **Web** — `frontend/` 下的 Next.js / React 应用
- **Mobile** — `mobile/` 下的 Expo / React Native 原生应用。它不是简单的 WebView 包装，而是拥有本地数据库、同步和状态管理的 first-class client

当前 Mobile 规格请参阅 [Mobile Product Contract / Conformance](docs/mobile_product_contract.md)。

## 支持的 LLM 路径

AoiTalk 支持多种 LLM 使用方式，包括云端 API 与本地推理环境。

- OpenAI
- Gemini
- OpenRouter
- Ollama
- SGLang
- OpenAI 兼容的本地服务器
- 其他已配置的 provider / model

## 当前架构

- **Web / BFF**: `frontend/` — Next.js 16 + React 19，并包含使用 Drizzle ORM 的 Next.js Route Handler。
- **Python runtime / API**: `main.py`、`src/api/` — 负责 FastAPI、WebSocket、AI / Agent runtime、音频与外部集成。
- **Database**: PostgreSQL。`alembic/versions/` 是数据库 schema 历史的权威来源。
- **RAG**: 使用 Qdrant 的检索路径。
- **Mobile**: `mobile/` — Expo + React Native 原生客户端。
- **Audio / Bot**: `src/audio/`、`src/tts/`、`src/bot/`。

文档索引以及“当前规格 / 设计记录”的区分请参阅 [docs/README.md](docs/README.md)。

## 本仓库（41_AoiTalk）的责任范围

本仓库内部拥有语音对话、任务/日程管理、Docs/Files/知识检索、Agent、PC Bridge，以及 Web/Native
Mobile 客户端本身。以下功能不属于本仓库，各自由独立应用作为正本，AoiTalk 的 Apps 界面仅提供指向
这些应用的外部链接。

| 功能 | 正本应用 |
| --- | --- |
| Hugging Face 浏览/同步 | `60_huggingface-sync` |
| Hydrus 浏览 | `57_Hydrus_App` |
| Story/Novel Studio、TRPG | `86_Novel-Graph-Studio` |
| 公共 Gateway / Caddy / Cloudflare / 搜索入口 | [`85_CaddyGateway`](https://github.com/ttttdiva/85_CaddyGateway) |

本地 LLM 的 Hugging Face Hub 获取/缓存，以及通过 URL ingest 抓取模型页面的能力仍保留在本仓库。
本仓库 PostgreSQL 中的 `story_*` / `trpg_*` 表予以保留（不删除），仅作为 `86_Novel-Graph-Studio`
的导入来源，本仓库运行时不再使用它们。

## 安装

### Windows

请使用仓库提供的标准安装入口：

```powershell
setup.bat
run.bat
```

当前 Windows 安装需要 Python 3.12+、Node.js 22+、Git、PostgreSQL / `.env`，并会准备 Python 虚拟环境、前端依赖、数据库 schema、production build 以及 Windows 音频 / TTS 相关 extra。

### Linux / WSL

Debian / Ubuntu 系 Linux 与 WSL：

```bash
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

当前 Linux 安装需要 Python 3.12+ 和 Node.js 20+。默认不会安装体积较大的音频依赖。如需同时安装音频与 Irodori-TTS 依赖：

```bash
AOITALK_INSTALL_AUDIO_DEPS=true ./setup.sh
```

外部数据库与平台相关详情请参阅 [安装指南](docs/setup_guide.md)。

### macOS

Python 包本身考虑了 macOS，但 `setup.sh` 中部分步骤以 Debian / Ubuntu 的包管理方式为前提。在 macOS 上，请手动准备 Python 3.12+、Node.js、PostgreSQL、Python 虚拟环境和前端依赖，然后按照 [安装指南](docs/setup_guide.md) 执行 `scripts/init_db_schema.py` 与 production build。

## 默认服务端口

| 服务 | 默认端口 |
| --- | --- |
| FastAPI | `3000` |
| Next.js | `3002` |
| 共享 Gateway（独立仓库） | `6002` |

个人环境中的共享 Caddy、Cloudflare Worker、技术站点和搜索入口由 [`85_CaddyGateway`](https://github.com/ttttdiva/85_CaddyGateway) 管理。Windows `run.bat` 只启动 AoiTalk 的 FastAPI/Next.js，并默认使用 `/at` 公共前缀。Enterprise/Docker 分发继续使用本仓库中的 `caddy/Caddyfile.enterprise`。详情请参阅 [公共 Gateway 边界](docs/public_gateway.md)。

## 配置与数据库

- `.env.sample` — 环境变量模板
- `pyproject.toml` — Python 依赖
- `frontend/package.json` — Web 依赖
- `alembic/versions/` — 数据库 migration
- `scripts/init_db_schema.py` — schema 初始化入口
- `src/config_defaults.py` — Python runtime 默认配置

不要在 README 中写固定登录密码。初始管理员凭据从本地 `.env` 中读取，包括 `AOITALK_BOOTSTRAP_ADMIN_PASSWORD`。

## 开发与验证

通常应根据变更范围执行本地针对性验证，并在推送 `main` 后确认远程提交 SHA。不等待 GitHub Actions / CI 完成，也不将其成功作为完成条件。涉及 WebUI 用户行为的变更，还需要按照 [AI WebUI QA](docs/ai_webui_qa.md) 进行独立的真实浏览器验证。

## 发布

私有开发仓库与公开发布仓库是有意分离的。`scripts/publish_public.ps1` 会为 `ttttdiva/AoiTalk` 生成公开发布用 tree。

- Mobile APK / 自动更新标准: [docs/mobile-auto-update-standard.md](docs/mobile-auto-update-standard.md)
- Release checklist: [docs/release-checklist.md](docs/release-checklist.md)

## License

根目录的 [LICENSE](LICENSE)（MIT）适用于本仓库中未另行标注的内容。`resources/seethrough-rig/runtime` 是独立的包，其 upstream license 未指定（package.json 为 `UNLICENSED`），不适用 MIT，也不授予任何形式的许可。详见该目录下的 `FIRST_PARTY_NOTICE.md`。

- [功能名称与职责](docs/feature_terminology.md)

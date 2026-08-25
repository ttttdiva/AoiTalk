# AoiTalk ドキュメント索引

この索引は、リポジトリ内の文書を **現行運用**, **実装契約**, **設計・履歴**, **生成・参照** に分けます。2026-08-20 時点の `main` 実装を基準に棚卸ししています。

重要: 設計書や調査記録に書かれた「現状」「未実装」「件数」「行番号」は、その文書を書いた時点の記録です。現在の runtime 仕様を確認するときは、現行コード・設定・migration・テストと、下記の現行運用文書を優先してください。

## 1. 現行運用・利用手順

実際のセットアップ、運用、release で参照する文書です。実装変更時に追随させる対象です。

| 文書 | 用途 / 正本として照合する実装 |
| --- | --- |
| [setup_guide.md](setup_guide.md) | Windows/Linux/macOS セットアップ。`setup.bat`, `setup.sh`, `run.bat`, `run.sh`, `pyproject.toml`, `frontend/package.json` |
| [DISCORD_BOT_SETUP.md](DISCORD_BOT_SETUP.md) | Discord。`src/bot/handlers/command_handler.py` と bot/service 設定 |
| [desktop_tauri.md](desktop_tauri.md) | Tauri desktop。`desktop/` |
| [live_voice_backend.md](live_voice_backend.md) | Live Voice。`src/services/live_voice_service.py`, live voice routes/defaults |
| [llama_cpp_muse_glimmer_setup.md](llama_cpp_muse_glimmer_setup.md) | managed llama.cpp / GGUF。model profile registry と local provider 実装 |
| [irodori_tts.md](irodori_tts.md) | Irodori-TTS runtime/setup。`pyproject.toml`, setup scripts, vendored runtime |
| [irodori_character_voice_integration.md](irodori_character_voice_integration.md) | Irodori character voice assets/API |
| [miotts_tts.md](miotts_tts.md) | MioTTS integration |
| [yomi_linter.md](yomi_linter.md) | TTS 共通読みリスク検出 |
| [mage_vl.md](mage_vl.md) | Mage-VL / SGLang video route |
| [growi-rag-setup.md](growi-rag-setup.md) | GROWI knowledge source |
| [logging.md](logging.md) | ログ配置・rotation。`src/utils/log_layout.py`, housekeeping |
| [workspace_storage_cleanup.md](workspace_storage_cleanup.md) | workspace orphan audit/cleanup |
| [security_hardening.md](security_hardening.md) | field encryption / local security |
| [pricing_catalog.md](pricing_catalog.md) | cost catalog。実単価の正本は `config/pricing_catalog.json` |
| [openapi_typegen.md](openapi_typegen.md) | FastAPI OpenAPI → TypeScript 型生成 |
| [schema_drift_check.md](schema_drift_check.md) | Alembic と Drizzle の schema drift 検知 |
| [public_publish.md](public_publish.md) | private development tree → public `ttttdiva/AoiTalk` 同期 |
| [mobile-auto-update-standard.md](mobile-auto-update-standard.md) | APK / `latest.json` / mobile release |
| [release-checklist.md](release-checklist.md) | 通常 push、mobile、public publish、Enterprise handoff の入口 |
| [enterprise_project_permissions.md](enterprise_project_permissions.md) | Enterprise project ACL / migration operation |
| [enterprise_secrets.md](enterprise_secrets.md) | Enterprise secret boundary |
| [chatgpt_web_director.md](chatgpt_web_director.md) | ChatGPT Web Director の利用・CLI |
| [ai_webui_qa.md](ai_webui_qa.md) | WebUI 変更時の独立 AI browser QA |
| [task_workspace_rebuild.md](task_workspace_rebuild.md) | 現行 task workspace の構成と参照先。歴史的なファイル名は維持 |

## 2. 現行の実装契約 / データ契約

以下は一般ユーザー向け runbook ではなく、特定機能のデータ形・writer・curation の不変条件です。コード変更時に契約として扱います。

- [clip_ingest_contract.md](clip_ingest_contract.md) — ClipIngest plan / provenance / editable typed-content / sync 契約
- [docs_editing_invariants.md](docs_editing_invariants.md) — Docs writer / node 形状の不変条件
- [docs_curation_contract.md](docs_curation_contract.md) — 現在の Docs DB 整理契約
- [docs_content_audit_runbook.md](docs_content_audit_runbook.md) — Docs DB の content audit 運用
- `docs_content_audit_ledger.jsonl` — audit ledger。一般文書として手編集しない
- `latest.json.example` — mobile 配布 metadata の例

## 3. 設計・再構築・履歴文書

これらは重要な設計根拠ですが、**現在の runtime 仕様そのものではありません**。過去時点の DB 件数、未実装項目、ファイル行数、API の有無を現行仕様として引用しないでください。

- [scenario_studio_rebuild_plan.md](scenario_studio_rebuild_plan.md) — Scenario Studio 再構築時点の調査・設計。2026-08-02 の実データ計測など時点情報を含む
- [project_information_docs_supertag_design.md](project_information_docs_supertag_design.md) — Project Information を Docs 正本へ寄せる設計記録。`推奨` や移行条件を含む
- [adr/2026-08-07-scoped-memory-v2.md](adr/2026-08-07-scoped-memory-v2.md) — ADR。採用判断の履歴として保持
- [reference/spotify/genre_search_investigation.md](reference/spotify/genre_search_investigation.md) — Spotify genre search の調査記録

設計文書と実装が食い違う場合は、設計文書を推測で現行化せず、現在のコードを確認したうえで「設計記録」と「現行 runbook」を分離してください。

## 4. 参照データ

- `reference/characters/*.csv` — TTS character/speaker 参照データ
- `reference/spotify/seed_genres.json`
- `reference/spotify/seed_genres.md`

参照データは生成元・provider の契約を確認せずに文章都合で書き換えないでください。

## 5. リポジトリ直下の文書

- `README.md` — 日本語の現行概要 / public publish でも利用
- `docs_i18n/README_en.md` — 英語の現行概要 / public publish でも利用
- `README.enterprise.md` — Enterprise handoff の唯一の人間向け runbook
- `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` — AI agent / repository workflow。製品仕様書ではない

## 文書更新ルール

1. バージョン、model list、DB table count、migration count、料金、API listのような変動値を複数文書へ複製しない。
2. setup の正本は setup/run scripts、DB schema の正本は Alembic、Python defaults は `src/config_defaults.py`、frontend dependencies/scripts は `frontend/package.json`。
3. Next.js BFF と FastAPI は共存している。片方を「すべての CRUD」「AI 専用」と断定する場合は実コードを確認する。
4. `未実装` と書く前に route/service/test を検索する。
5. 履歴文書の古い事実は削除して現代化するのではなく、履歴であることを明記して現行 runbook から分離する。
6. 個人 checkout の絶対パスを一般手順へ書かない。`<repo>`、`$REPO_ROOT`、現在の checkout からの相対パスを使う。

# 開発・運用スクリプト

ここには現在のアプリのセットアップ、検証、配布、再利用可能な保守に必要なCLIを置く。一度限りの修復コード、LLMへの相談文と回答、端末固有のログなどは追跡しない `.local/` に置く。廃止したCLIは専用テスト・参照とともに削除し、互換wrapperとして残さない。

| 用途 | 入口 / 参照先 |
| --- | --- |
| セットアップ・起動 | ルートの `setup.bat` / `setup.sh`、`run.bat` / `run.sh`。[セットアップガイド](../docs/setup_guide.md) |
| DB初期化 | `init_db_schema.py`。schemaの正本は `alembic/versions/` |
| DB確認 | `check_db.py`。接続情報はローカル設定から取得する |
| Schema整合性 | `check_schema_drift.py`。[スキーマドリフト](../docs/schema_drift_check.md) |
| API契約 | `generate_openapi.py` → `contracts/openapi/fastapi.json`。[型生成手順](../docs/openapi_typegen.md) |
| Mobile契約 | `validate_mobile_product_contract.py`。[Mobile Product Contract](../docs/mobile_product_contract.md) |
| CI確認 | `wait_ci.ps1`。フル検証の定義は `.github/workflows/ci.yml` |
| 手動フル検証 | `run_canonical_verification.ps1`。明示的な手動実行用で、通常作業のターゲット検証と区別する |
| Public公開 | ルートの `Publish.bat` → `publish_public.ps1`。[公開手順](../docs/public_publish.md) |
| Enterprise配布 | ルートの `Enterprise.bat` → `build_enterprise_handoff.ps1`。正本は `README.enterprise.md` |
| Mobile公開 | `check_mobile_release_gate.ps1`、`build_apk.bat` / `build_apk.sh`、`verify_mobile_apk.ps1`、`publish_latest_json.ps1`。[リリースチェック](../docs/release-checklist.md) |
| ChatGPT Web Director | `chatgpt_web_director.py` / `chatgpt_web_director.ps1`。[利用手順](../docs/chatgpt_web_director.md) |
| Project Management CLI | `agent_project_management_tool.py`。既存Agentのツール定義を呼び出す |

`maintenance/` は運用保守、`migrations/` は明示的なデータ移行、`verification/` は再利用する実環境検証に使用する。各CLIの引数・対象・副作用は実装と `--help` を確認する。これらを通常起動時に一括実行しない。

コード整理と実データの削除は別の作業である。DB、`data/`、`workspaces/`、証明書や認証情報を一時成果物として扱わない。

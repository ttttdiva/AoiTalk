# Scripts

このディレクトリには現行セットアップ、CI、公開、運用で参照されるスクリプトだけを置きます。

## Enterprise

- `build_enterprise_handoff.ps1`: **唯一の人間向け入口**。clean tracked sourceから単一
  `AoiTalk_Enterprise_Handoff_<commit>.zip` を生成し、sanitization/import closure/secret/ZIP検証まで行う。
- `check_enterprise_python_imports.py`: builderが内部検証に使う非入口 checker。
- `check_env_contract.ps1`: Enterprise環境の契約確認。
- `init-db.sql`, `init_db_schema.py`, `setup_env_db.ps1`: PostgreSQLとAlembicの初期化。

Enterpriseの手順、HF_TOKENモデル取得、Docker/HTTPS smokeはルートの `README.enterprise.md` だけを正本とします。

## CI・開発

- `check_schema_drift.py`: AlembicとDrizzleのschema drift検査
- `generate_openapi.py`: FastAPIから`frontend/openapi.json`を生成
- `python_312_gate.py`: Python 3.12以上の起動条件を検査
- `merge_ready_prs.ps1`: merge前のchecksとmobile release gateを確認
- `wait_ci.ps1`: push 後の GitHub Actions 完了待ち。`gh run list --commit` をポーリング。exit: 0=PASS, 1=FAIL, 2=UNAVAILABLE（billing 等）, 3=TIMEOUT。`push: main` または `pull_request` で CI 起動。UNAVAILABLE 時はローカル full CI を自動開始しない（ターゲット検証 PASS なら `COMPLETE_CI_UNAVAILABLE`）。
- `run_canonical_verification.ps1`: ci.yml 同等のローカル canonical 検証（Windows）。**手動専用** — エージェントは通常作業や CI UNAVAILABLE 時に自動実行しない。各ゲート PASS/FAIL/SKIP、最初の FAIL で fail-fast。`-SkipE2E` は文書化済みの省略オプション。

## 公開

- `publish_public.ps1`: public repositoryへの同期
- `check_mobile_release_gate.ps1`: mobile差分のrelease要否判定
- `build_apk.bat`, `build_apk.sh`: APKを`artifacts/releases/mobile/v<version>/`へ生成して公開
- `verify_mobile_apk.ps1`, `publish_latest_json.ps1`: APK検証と更新metadataの公開

## 運用ツール

暗号化、HTTPS、LLM server、料金backfill、Excel/WBS検査など、現行docs・設定・runtimeから参照される管理ツールを直下に置きます。

- `chatgpt_web_director.py` / `chatgpt_web_director.ps1`: Grok など外部 Operator から AoiTalk の Playwright `ChatGPTWebProvider` 経由で ChatGPT Web へ送受信する CLI。`status` / `send` / `new`。cwd 不問。詳細は `docs/chatgpt_web_director.md`。

## データ移行・保守

- `migrations/`: 現在の配布先で完了確認が必要なデータ移行
- `maintenance/`: 再実行可能な復旧処理

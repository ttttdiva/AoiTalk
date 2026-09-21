# Web client

共通の規約は [ルートAGENTS.md](../AGENTS.md)。このディレクトリはNext.js / ReactのWebUIとNext.js APIを持つ。

- バージョンとコマンドは `package.json` とlockfileを確認する。Next.jsのAPI変更を扱う場合は、インストール済みの `node_modules/next/dist/docs/` に該当ガイドがあれば参照し、なければ導入バージョンの公式資料を確認する。
- 変更範囲のVitest / ESLint / 型検証を実行する。ビルドが必要な場合は `npm run build`、本番起動を確認する場合は `npm run build:production` を使用する。出力先の定義はNext.js設定を確認する。
- ユーザー挙動を変える変更では [AI WebUI QA](../docs/ai_webui_qa.md) を行い、既知の回帰は `e2e/` で検証する。テストは既存の配置とrunnerに合わせる。
- `src/app/api/` のNext.js APIをMobileの共有APIと混同しない。FastAPI型の生成は [型生成手順](../docs/openapi_typegen.md) に従い、生成型を手編集しない。
- Docsノード本文の変更は [Docs編集の不変条件](../docs/docs_editing_invariants.md) に従う。

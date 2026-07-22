// openapi-typescript が生成した型ファイルの冒頭に、日本語の
// 「自動生成・手編集禁止・再生成手順」注記を付与する後処理スクリプト。
// typegen コマンド（package.json）から呼ばれる。再生成のたびに冪等に適用される。
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const target = resolve(here, "..", "src", "lib", "api-types.gen.ts");

const banner = `/**
 * =====================================================================
 * 自動生成ファイル — 手編集禁止
 * ---------------------------------------------------------------------
 * FastAPI の OpenAPI スキーマ（frontend/openapi.json）から
 * openapi-typescript により生成された TypeScript 型定義です。
 * このファイルを直接編集しないでください。編集は次回の再生成で失われます。
 *
 * 再生成手順（リポジトリルートで実行）:
 *   1. venv\\Scripts\\python.exe scripts/generate_openapi.py
 *   2. cd frontend && npm run typegen
 *
 * backend（Pydantic モデル / ルート）を変更したら上記を再実行してください。
 * =====================================================================
 */
`;

const original = readFileSync(target, "utf8");
// 既にバナーが付いている場合は二重付与しない（冪等化）。
const marker = "自動生成ファイル — 手編集禁止";
if (original.includes(marker)) {
  process.exit(0);
}
writeFileSync(target, banner + original, "utf8");

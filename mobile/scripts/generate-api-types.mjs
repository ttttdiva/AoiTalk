#!/usr/bin/env node
/**
 * Generate the mobile FastAPI types from the shared OpenAPI artifact.
 *
 * The backend schema is deliberately read from contracts/openapi rather than
 * copied from frontend/openapi.json.  This keeps native and web clients on one
 * backend contract while allowing each client to own its generated output.
 */
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const mobileRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(mobileRoot, "..");
const args = process.argv.slice(2);

function option(name, fallback) {
  const index = args.indexOf(name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`${name} requires a value`);
  }
  return value;
}

const input = resolve(repoRoot, option("--input", "contracts/openapi/fastapi.json"));
const output = resolve(mobileRoot, option("--output", "src/types/api-types.gen.ts"));
const packageEntry = resolve(
  repoRoot,
  "frontend/node_modules/openapi-typescript/dist/index.mjs",
);

if (!existsSync(input)) {
  throw new Error(
    `OpenAPI source not found: ${input}. Run scripts/generate_openapi.py --canonical-only first.`,
  );
}
if (!existsSync(packageEntry)) {
  throw new Error(
    `openapi-typescript is not installed at ${packageEntry}; install frontend dependencies first.`,
  );
}

const schema = JSON.parse(readFileSync(input, "utf8"));
const { default: openapiTS, astToString } = await import(pathToFileURL(packageEntry));
const ast = await openapiTS(schema, { silent: true });
const generated = astToString(ast);
const header = `/**
 * =====================================================================
 * 自動生成ファイル — 手編集禁止
 * ---------------------------------------------------------------------
 * contracts/openapi/fastapi.json から openapi-typescript により生成された
 * Mobile 用 FastAPI 型定義です。API 型を変更する場合は backend の
 * Pydantic/OpenAPI と共有正本を更新し、このスクリプトを再実行してください。
 *
 * 再生成: cd mobile && npm run api:typegen
 * =====================================================================
 */
`;

writeFileSync(output, `${header}${generated.trimEnd()}\n`, "utf8");
console.log(`Mobile API types written to ${output}`);

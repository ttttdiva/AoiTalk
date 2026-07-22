/**
 * Docs Supertag クリーンアップスクリプト（一回限り）
 *
 * DEFAULT_DOCS_SUPERTAGS を11種へ絞り込んだ再設計(D11)に伴い、
 * 定義から外した以下のデフォルト由来 supertag を既存DBから除去する。
 *   Decision / Risk / Vendor / Question / Note / Spec / Estimate / Email / Project / URL
 *
 * seed(seedDefaultDocsWorkspace) は定義済みタグしか触らないため、
 * 定義から消したタグは再作成されないが自動削除もされない。本スクリプトで一度だけ掃除する。
 *
 * 特定条件（誤爆防止）:
 *   - name が上記デフォルト名に完全一致し、かつ base_type が旧デフォルト定義の値
 *     （Decision→decision 等）に完全一致し、かつ system_key IS NULL のもののみ対象。
 *     UIから作成したタグ（frontend/src/app/api/docs/supertags/route.ts）も system_key は
 *     常に NULL のため、system_key だけでは seed 由来と区別できない。旧デフォルト定義に
 *     存在した name × base_type の組み合わせを AND 条件で併用することで、ユーザーが同名で
 *     作成したタグ（base_type が既定の "note" など）を巻き込まないようにする。
 *
 * 削除順（子→親）:
 *   1. knowledge_node_supertags     … ノードへの付与関係
 *   2. knowledge_supertag_fields    … タグ⇔フィールドの関連
 *   3. knowledge_field_values       … 対象タグの field に紐づく値
 *   4. knowledge_fields             … 対象タグの field 定義
 *   5. knowledge_supertags          … 対象タグ本体
 *   ※ knowledge_nodes（ノード本体）は削除しない。
 *
 * モード:
 *   引数なし        … dry-run（削除対象の件数を表示するだけ。DBは変更しない）
 *   --apply         … 実際に削除する（トランザクション）
 *
 * 実行方法（frontend の node_modules に tsx / postgres があるためそれを使う）:
 *   # dry-run
 *   frontend/node_modules/.bin/tsx scripts/cleanup_docs_supertags.ts
 *   # 実行
 *   frontend/node_modules/.bin/tsx scripts/cleanup_docs_supertags.ts --apply
 *
 * 接続情報はリポジトリ直下 .env の POSTGRES_*（または DATABASE_URL）から取得する。
 * DB: aoitalk_memory（frontend/src/db/index.ts の getConnectionString と同じ組み立て）。
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// frontend の node_modules にある postgres を絶対パスで解決する。
import postgres from "../frontend/node_modules/postgres/src/index.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, "..");

// 定義から外したデフォルト由来タグ。旧 DEFAULT_DOCS_SUPERTAGS 定義の name × base_type に完全一致。
// base_type は削除前の docs-model.ts 定義（git 履歴）から取得した正確な値。
const REMOVED_DEFAULT_TAGS: Array<{ name: string; baseType: string }> = [
  { name: "Decision", baseType: "decision" },
  { name: "Risk", baseType: "risk" },
  { name: "Vendor", baseType: "vendor" },
  { name: "Question", baseType: "question" },
  { name: "Note", baseType: "note" },
  { name: "Spec", baseType: "spec" },
  { name: "Estimate", baseType: "estimate" },
  { name: "Email", baseType: "email" },
  { name: "Project", baseType: "project" },
  { name: "URL", baseType: "url" },
];

function loadDotEnv(path: string): void {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return;
  }
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = value;
  }
}

function getConnectionString(): string {
  if (process.env.DATABASE_URL) return process.env.DATABASE_URL;
  const user = process.env.POSTGRES_USER || "aoitalk";
  const password = process.env.POSTGRES_PASSWORD || "";
  const host = process.env.POSTGRES_HOST || "127.0.0.1";
  const port = process.env.POSTGRES_PORT || "5432";
  const dbName = process.env.POSTGRES_DB || "aoitalk_memory";
  return `postgres://${user}:${encodeURIComponent(password)}@${host}:${port}/${dbName}`;
}

async function main(): Promise<void> {
  const apply = process.argv.includes("--apply");
  loadDotEnv(resolve(REPO_ROOT, ".env"));

  const sql = postgres(getConnectionString(), { max: 1 });

  try {
    // name × base_type の各組を AND で表し、それらを OR で束ねた条件を組み立てる。
    // これにより「旧デフォルト定義の name と base_type の両方に一致」する行だけを対象にする。
    const tagCondition = REMOVED_DEFAULT_TAGS
      .map((t) => sql`(name = ${t.name} and base_type = ${t.baseType})`)
      .reduce((acc, frag) => sql`${acc} or ${frag}`);

    // 対象 supertag（name × base_type が旧デフォルト定義に完全一致 かつ system_key IS NULL）
    const targetTags = await sql`
      select id, workspace_id, name
      from knowledge_supertags
      where system_key is null
        and (${tagCondition})
      order by name
    `;

    if (targetTags.length === 0) {
      console.log("[cleanup] 対象タグは存在しません（既にクリーン済み）。");
      return;
    }

    const tagIds = targetTags.map((t: { id: string }) => t.id);

    // 影響件数の集計
    const [nodeSupertagCount] = await sql`
      select count(*)::int as c
      from knowledge_node_supertags
      where supertag_id in ${sql(tagIds)}
    `;
    const [supertagFieldCount] = await sql`
      select count(*)::int as c
      from knowledge_supertag_fields
      where supertag_id in ${sql(tagIds)}
    `;
    const targetFields = await sql`
      select id from knowledge_fields
      where supertag_id in ${sql(tagIds)}
    `;
    const fieldIds = targetFields.map((f: { id: string }) => f.id);
    const [fieldValueCount] = fieldIds.length
      ? await sql`
          select count(*)::int as c
          from knowledge_field_values
          where field_id in ${sql(fieldIds)}
        `
      : [{ c: 0 }];

    // 集計サマリ表示
    console.log("[cleanup] 対象タグ (name / workspace_id):");
    for (const t of targetTags as Array<{ name: string; workspace_id: string }>) {
      console.log(`  - ${t.name}  (workspace ${t.workspace_id})`);
    }
    console.log("[cleanup] 削除対象件数:");
    console.log(`  knowledge_supertags        : ${targetTags.length}`);
    console.log(`  knowledge_fields           : ${fieldIds.length}`);
    console.log(`  knowledge_field_values     : ${fieldValueCount.c}`);
    console.log(`  knowledge_supertag_fields  : ${supertagFieldCount.c}`);
    console.log(`  knowledge_node_supertags   : ${nodeSupertagCount.c}`);
    console.log("  knowledge_nodes            : 0 (削除しない)");

    if (!apply) {
      console.log(
        "\n[cleanup] dry-run のため DB は変更していません。実行するには --apply を付けてください。",
      );
      return;
    }

    // 実削除（子→親の順、単一トランザクション）
    await sql.begin(async (tx: typeof sql) => {
      const delNodeSupertags = await tx`
        delete from knowledge_node_supertags
        where supertag_id in ${sql(tagIds)}
      `;
      const delSupertagFields = await tx`
        delete from knowledge_supertag_fields
        where supertag_id in ${sql(tagIds)}
      `;
      const delFieldValues = fieldIds.length
        ? await tx`
            delete from knowledge_field_values
            where field_id in ${sql(fieldIds)}
          `
        : { count: 0 };
      const delFields = await tx`
        delete from knowledge_fields
        where supertag_id in ${sql(tagIds)}
      `;
      const delSupertags = await tx`
        delete from knowledge_supertags
        where id in ${sql(tagIds)}
      `;

      console.log("\n[cleanup] 削除実行完了 (削除行数):");
      console.log(`  knowledge_node_supertags   : ${delNodeSupertags.count}`);
      console.log(`  knowledge_supertag_fields  : ${delSupertagFields.count}`);
      console.log(`  knowledge_field_values     : ${delFieldValues.count}`);
      console.log(`  knowledge_fields           : ${delFields.count}`);
      console.log(`  knowledge_supertags        : ${delSupertags.count}`);
    });

    console.log("[cleanup] --apply 完了。");
  } finally {
    await sql.end({ timeout: 5 });
  }
}

main().catch((err) => {
  console.error("[cleanup] エラー:", err);
  process.exitCode = 1;
});

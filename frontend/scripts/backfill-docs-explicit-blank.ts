/**
 * Backfill and verify the SQL-visible Docs explicit-blank discriminator.
 *
 * body_json is encrypted for application-managed rows, therefore this script
 * deliberately decrypts each row in the application process before deciding
 * whether is_explicit_blank should be true.  It never attempts a JSONB
 * predicate over ciphertext and fails closed when a row cannot be decrypted.
 *
 * Usage:
 *   pnpm --dir frontend docs:verify-explicit-blanks
 *   pnpm --dir frontend docs:backfill-explicit-blanks
 *   pnpm --dir frontend docs:backfill-explicit-blanks -- --workspace-id=<uuid>
 */

import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import postgres from "postgres";

const args = process.argv.slice(2);
const apply = args.includes("--apply");
const verify = args.includes("--verify") || !apply;

function argValue(name: string): string | null {
  const prefix = `${name}=`;
  const match = args.find((arg) => arg.startsWith(prefix));
  return match ? match.slice(prefix.length) : null;
}

function loadEnvFiles() {
  for (const fileName of [".env", ".env.local"]) {
    const filePath = resolve(process.cwd(), fileName);
    if (!existsSync(filePath)) continue;
    for (const line of readFileSync(filePath, "utf8").split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
      const index = trimmed.indexOf("=");
      const key = trimmed.slice(0, index).trim();
      let value = trimmed.slice(index + 1).trim();
      if (
        (value.startsWith('"') && value.endsWith('"'))
        || (value.startsWith("'") && value.endsWith("'"))
      ) {
        value = value.slice(1, -1);
      }
      if (key && process.env[key] === undefined) process.env[key] = value;
    }
  }
}

function connectionString() {
  if (process.env.DATABASE_URL) return process.env.DATABASE_URL;
  const user = process.env.POSTGRES_USER || "aoitalk";
  const password = process.env.POSTGRES_PASSWORD || "";
  const host = process.env.POSTGRES_HOST || "127.0.0.1";
  const port = process.env.POSTGRES_PORT || "5432";
  const database = process.env.POSTGRES_DB || "aoitalk_memory";
  return `postgres://${user}:${password}@${host}:${port}/${database}`;
}

function assertUuid(value: string | null, name: string): string | null {
  if (
    value
    && !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
  ) {
    throw new Error(`${name} must be a UUID`);
  }
  return value;
}

loadEnvFiles();

const workspaceId = assertUuid(argValue("--workspace-id"), "--workspace-id");
const limitValue = argValue("--limit");
const limit = limitValue === null ? null : Number.parseInt(limitValue, 10);
if (limit !== null && (!Number.isSafeInteger(limit) || limit <= 0)) {
  throw new Error("--limit must be a positive integer");
}

type Row = {
  id: string;
  docs_library_id: string;
  title: string;
  body_json: unknown;
  body_text: string | null;
  node_type: string | null;
  system_key: string | null;
  is_explicit_blank: boolean;
};

async function main() {
  // Dynamic imports happen after env loading so the field-crypto key provider
  // sees the same configuration as the running Next server.
  const { decryptDocsNodeBodyJson, decryptDocsNodeBodyText } = await import("@/lib/server/docs-node-writer");
  const { isExplicitBlankParagraph } = await import("@/lib/docs-block-model");
  const sql = postgres(connectionString(), { max: 1 });
  try {
    const rows = await sql<Row[]>`
    select
      id,
      docs_library_id,
      title,
      body_json,
      body_text,
      node_type,
      system_key,
      is_explicit_blank
    from knowledge_nodes
    where title = ''
       or is_explicit_blank = true
      ${workspaceId ? sql`and docs_library_id = ${workspaceId}` : sql``}
    order by id
    ${limit === null ? sql`` : sql`limit ${limit}`}
    `;

    const mismatches: Array<{ id: string; current: boolean; expected: boolean }> = [];
    for (const row of rows) {
    // decryptDocsNodeBodyJson is intentionally strict about malformed
    // ciphertext.  A key/decryption error aborts the run rather than
    // silently classifying a row as legacy/non-blank.
      const bodyJson = decryptDocsNodeBodyJson(row.body_json ?? {});
      // Validate the encrypted title mirror before normalizing it. A key or
      // authentication failure must abort rather than discard opaque data.
      decryptDocsNodeBodyText(row.body_text ?? "");
      const expected = !row.system_key
        && isExplicitBlankParagraph(row.title, bodyJson, row.node_type ?? "node");
      const current = row.is_explicit_blank === true;
      if (current !== expected || (expected && row.body_text !== null && row.body_text !== "")) {
        mismatches.push({ id: row.id, current, expected });
      }
    }

    if (apply && mismatches.length > 0) {
      await sql.begin(async (tx) => {
      // postgres-js's TransactionSql type omits the callable signature even
      // though the runtime transaction object remains a tagged-template
      // function (the same API used by the existing migration scripts).
        const txSql = tx as unknown as typeof sql;
        for (const mismatch of mismatches) {
        await txSql`
          update knowledge_nodes
          set is_explicit_blank = ${mismatch.expected},
              body_text = case when ${mismatch.expected} then '' else body_text end
          where id = ${mismatch.id}
            and (title = '' or is_explicit_blank = true)
        `;
        }
      });
    }

    const mode = apply ? "APPLY" : "VERIFY";
    console.log(`MODE=${mode}`);
    console.log(`ROWS_SCANNED=${rows.length}`);
    console.log(`MISMATCHES=${mismatches.length}`);
    if (mismatches.length > 0) {
      for (const mismatch of mismatches.slice(0, 100)) {
        console.log(`${apply ? "UPDATED" : "MISMATCH"} ${mismatch.id} current=${mismatch.current} expected=${mismatch.expected}`);
      }
      if (verify && !apply) process.exitCode = 1;
    }
  } finally {
    await sql.end({ timeout: 5 });
  }
}

void main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});

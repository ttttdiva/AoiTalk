import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import postgres from "postgres";

function loadEnvFiles() {
  const loadedKeys = new Set();
  for (const fileName of [".env", ".env.local"]) {
    const filePath = resolve(process.cwd(), fileName);
    if (!existsSync(filePath)) continue;
    for (const line of readFileSync(filePath, "utf8").split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
      const index = trimmed.indexOf("=");
      const key = trimmed.slice(0, index).trim();
      let value = trimmed.slice(index + 1).trim();
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      if (key && (process.env[key] === undefined || loadedKeys.has(key))) {
        process.env[key] = value;
        loadedKeys.add(key);
      }
    }
  }
}

function getConnectionString() {
  if (process.env.DATABASE_URL) return process.env.DATABASE_URL;
  const user = process.env.POSTGRES_USER || "aoitalk";
  const password = process.env.POSTGRES_PASSWORD || "";
  const host = process.env.POSTGRES_HOST || "127.0.0.1";
  const port = process.env.POSTGRES_PORT || "5432";
  const dbName = process.env.POSTGRES_DB || "aoitalk_memory";
  return `postgres://${user}:${password}@${host}:${port}/${dbName}`;
}

function normalize(text) {
  return String(text ?? "")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .join("\n");
}

function buildMarkdown(pageId, nodesByParent, depth = 0) {
  return (nodesByParent.get(pageId) ?? [])
    .sort((a, b) => Number(a.sort_order ?? 0) - Number(b.sort_order ?? 0))
    .flatMap((node) => {
      const text = String(node.body_text ?? "");
      return [text, buildMarkdown(node.id, nodesByParent, depth + 1)].filter(Boolean);
    })
    .join("\n");
}

loadEnvFiles();
const sql = postgres(getConnectionString(), { max: 1 });

try {
  const pages = await sql`
    select id, title, body_json
    from knowledge_nodes
    where coalesce(node_type, 'page') = 'page'
      and body_json->'migratedFromPlainMarkdown' is not null
    order by title asc
  `;
  const pageIds = pages.map((page) => page.id);
  const blocks = pageIds.length
    ? await sql`
        select id, parent_id, root_page_id, body_text, sort_order
        from knowledge_nodes
        where root_page_id in ${sql(pageIds)}
          and coalesce(node_type, 'page') = 'block'
          and archived_at is null
      `
    : [];
  const byParent = new Map();
  for (const block of blocks) {
    const key = block.parent_id;
    const rows = byParent.get(key) ?? [];
    rows.push(block);
    byParent.set(key, rows);
  }

  let ok = 0;
  let failed = 0;
  for (const page of pages) {
    const original = page.body_json?.migratedFromPlainMarkdown?.originalText ?? "";
    const rebuilt = buildMarkdown(page.id, byParent);
    const matches = normalize(original) === normalize(rebuilt);
    if (matches) ok += 1;
    else failed += 1;
    console.log(`${matches ? "OK" : "NG"} ${page.id} "${page.title}" blocks=${(byParent.get(page.id) ?? []).length}`);
  }
  console.log(`PAGES=${pages.length}`);
  console.log(`OK=${ok}`);
  console.log(`FAILED=${failed}`);
  if (failed > 0) process.exitCode = 1;
} finally {
  await sql.end({ timeout: 5 });
}

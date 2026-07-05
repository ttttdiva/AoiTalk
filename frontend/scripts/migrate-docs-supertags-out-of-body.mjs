import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import postgres from "postgres";
import { deriveTitleFromBodyText, stripAssignedSupertagTokens } from "./docs-supertag-token-utils.mjs";

const args = new Set(process.argv.slice(2));
const getArgValue = (name) => {
  const prefix = `${name}=`;
  const match = process.argv.slice(2).find((arg) => arg.startsWith(prefix));
  return match ? match.slice(prefix.length) : null;
};

const apply = args.has("--apply");
const workspaceId = getArgValue("--workspace-id");
const limit = Number.parseInt(getArgValue("--limit") ?? "5000", 10);

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

function summarize(text) {
  const normalized = String(text ?? "").replace(/\s+/g, " ").trim();
  return normalized.length > 100 ? `${normalized.slice(0, 97)}...` : normalized;
}

loadEnvFiles();

if (!Number.isFinite(limit) || limit <= 0) {
  throw new Error("--limit must be a positive integer.");
}

if (workspaceId && !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(workspaceId)) {
  throw new Error("--workspace-id must be a UUID.");
}

const sql = postgres(getConnectionString(), { max: 1 });

try {
  const rows = workspaceId
    ? await sql`
        select
          n.id,
          n.title,
          n.body_text,
          array_agg(t.name order by t.name) as tag_names
        from knowledge_nodes n
        inner join knowledge_node_supertags nst on nst.node_id = n.id
        inner join knowledge_supertags t on t.id = nst.supertag_id
        where n.workspace_id = ${workspaceId}
          and coalesce(n.body_text, '') <> ''
        group by n.id, n.title, n.body_text
        order by n.updated_at desc nulls last, n.created_at desc nulls last
        limit ${limit}
      `
    : await sql`
        select
          n.id,
          n.title,
          n.body_text,
          array_agg(t.name order by t.name) as tag_names
        from knowledge_nodes n
        inner join knowledge_node_supertags nst on nst.node_id = n.id
        inner join knowledge_supertags t on t.id = nst.supertag_id
        where coalesce(n.body_text, '') <> ''
        group by n.id, n.title, n.body_text
        order by n.updated_at desc nulls last, n.created_at desc nulls last
        limit ${limit}
      `;

  const plans = rows
    .map((row) => {
      const result = stripAssignedSupertagTokens(row.body_text, row.tag_names ?? []);
      return {
        id: row.id,
        beforeTitle: row.title ?? "",
        beforeText: row.body_text ?? "",
        afterText: result.text,
        afterTitle: deriveTitleFromBodyText(result.text),
        removed: result.removed,
      };
    })
    .filter((plan) => plan.removed > 0 && plan.beforeText !== plan.afterText);

  const beforeTokens = plans.reduce((total, plan) => total + plan.removed, 0);
  const afterTokens = plans.reduce((total, plan) => total + stripAssignedSupertagTokens(plan.afterText, rows.find((row) => row.id === plan.id)?.tag_names ?? []).removed, 0);

  console.log(`MODE=${apply ? "apply" : "dry-run"}`);
  console.log(`CANDIDATE_NODES=${rows.length}`);
  console.log(`NODES_TO_UPDATE=${plans.length}`);
  console.log(`TOKENS_BEFORE=${beforeTokens}`);
  console.log(`TOKENS_AFTER=${afterTokens}`);

  for (const plan of plans.slice(0, 20)) {
    console.log(`- ${plan.id}`);
    console.log(`  title: "${summarize(plan.beforeTitle)}" -> "${summarize(plan.afterTitle)}"`);
    console.log(`  body: "${summarize(plan.beforeText)}" -> "${summarize(plan.afterText)}"`);
  }

  if (!apply || plans.length === 0) {
    console.log(apply ? "No changes applied." : "Dry-run only. Re-run with --apply to update body_text and title.");
  } else {
    await sql.begin(async (tx) => {
      for (const plan of plans) {
        await tx`
          update knowledge_nodes
          set
            body_text = ${plan.afterText},
            title = ${plan.afterTitle},
            updated_at = now()
          where id = ${plan.id}
        `;
      }
    });

    console.log("Migration applied.");
  }
} finally {
  await sql.end({ timeout: 5 });
}

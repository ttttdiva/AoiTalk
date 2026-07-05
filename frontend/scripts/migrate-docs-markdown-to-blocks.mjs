import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import postgres from "postgres";

const args = new Set(process.argv.slice(2));
const getArgValue = (name) => {
  const prefix = `${name}=`;
  const match = process.argv.slice(2).find((arg) => arg.startsWith(prefix));
  return match ? match.slice(prefix.length) : null;
};

const apply = args.has("--apply");
const workspaceId = getArgValue("--workspace-id");
const limit = Number.parseInt(getArgValue("--limit") ?? "100", 10);

function loadEnvFiles() {
  const loadedKeys = new Set();
  for (const fileName of [".env", ".env.local"]) {
    const filePath = resolve(process.cwd(), fileName);
    if (!existsSync(filePath)) continue;

    const lines = readFileSync(filePath, "utf8").split(/\r?\n/);
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;

      const index = trimmed.indexOf("=");
      const key = trimmed.slice(0, index).trim();
      let value = trimmed.slice(index + 1).trim();
      if (
        (value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))
      ) {
        value = value.slice(1, -1);
      }

      if (key && (process.env[key] === undefined || loadedKeys.has(key))) {
        process.env[key] = value;
        loadedKeys.add(key);
      }
    }
  }
}

loadEnvFiles();

if (!Number.isFinite(limit) || limit <= 0) {
  throw new Error("--limit must be a positive integer.");
}

if (workspaceId && !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(workspaceId)) {
  throw new Error("--workspace-id must be a UUID.");
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

function markdownToBlocks(bodyText) {
  const lines = String(bodyText ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let inFence = false;
  let fenceBuffer = [];

  for (const rawLine of lines) {
    const trimmed = rawLine.trim();
    const isFence = trimmed.startsWith("```") || trimmed.startsWith("~~~");

    if (isFence) {
      fenceBuffer.push(rawLine);
      inFence = !inFence;
      if (!inFence) {
        blocks.push({ text: fenceBuffer.join("\n").trim(), depth: 0 });
        fenceBuffer = [];
      }
      continue;
    }

    if (inFence) {
      fenceBuffer.push(rawLine);
      continue;
    }

    if (!trimmed) continue;

    const heading = trimmed.match(/^(#{1,6})\s+/);
    if (heading) {
      blocks.push({ text: trimmed, depth: Math.max(0, heading[1].length - 1) });
      continue;
    }

    if (/^\s*(?:[-*+]|\d+\.)\s+/.test(rawLine)) {
      const indent = rawLine.match(/^\s*/)?.[0].replace(/\t/g, "  ").length ?? 0;
      blocks.push({ text: trimmed, depth: Math.floor(indent / 2) });
      continue;
    }

    blocks.push({ text: trimmed, depth: 0 });
  }

  if (fenceBuffer.length) blocks.push({ text: fenceBuffer.join("\n").trim(), depth: 0 });
  return blocks.filter((block) => block.text);
}

function summarizeText(text) {
  const normalized = String(text ?? "").replace(/\s+/g, " ").trim();
  return normalized.length > 80 ? `${normalized.slice(0, 77)}...` : normalized;
}

const sql = postgres(getConnectionString(), { max: 1 });

try {
  const candidates = workspaceId
    ? await sql`
        select
          n.id,
          n.workspace_id,
          n.project_id,
          n.title,
          n.body_json,
          n.body_text,
          n.created_by,
          n.updated_by
        from knowledge_nodes n
        where n.archived_at is null
          and coalesce(n.node_type, 'page') = 'page'
          and coalesce(n.body_text, '') <> ''
          and coalesce(n.body_json->>'format', '') <> 'outliner_page'
          and n.workspace_id = ${workspaceId}
          and not exists (
            select 1
            from knowledge_nodes child
            where child.parent_id = n.id
              and child.archived_at is null
          )
        order by n.updated_at desc nulls last, n.created_at desc nulls last
        limit ${limit}
      `
    : await sql`
        select
          n.id,
          n.workspace_id,
          n.project_id,
          n.title,
          n.body_json,
          n.body_text,
          n.created_by,
          n.updated_by
        from knowledge_nodes n
        where n.archived_at is null
          and coalesce(n.node_type, 'page') = 'page'
          and coalesce(n.body_text, '') <> ''
          and coalesce(n.body_json->>'format', '') <> 'outliner_page'
          and not exists (
            select 1
            from knowledge_nodes child
            where child.parent_id = n.id
              and child.archived_at is null
          )
        order by n.updated_at desc nulls last, n.created_at desc nulls last
        limit ${limit}
      `;

  const plans = candidates
    .map((page) => ({
      page,
      blocks: markdownToBlocks(page.body_text),
    }))
    .filter((plan) => plan.blocks.length > 0);

  console.log(`MODE=${apply ? "apply" : "dry-run"}`);
  console.log(`CANDIDATE_PAGES=${plans.length}`);
  console.log(`BLOCKS_TO_CREATE=${plans.reduce((total, plan) => total + plan.blocks.length, 0)}`);

  for (const { page, blocks } of plans) {
    console.log(`- ${page.id} "${page.title}" -> ${blocks.length} blocks`);
    console.log(`  first: ${summarizeText(blocks[0]?.text)}`);
  }

  if (!apply || plans.length === 0) {
    console.log(apply ? "No changes applied." : "Dry-run only. Re-run with --apply to write blocks.");
    process.exit(0);
  }

  const migratedAt = new Date().toISOString();

  await sql.begin(async (tx) => {
    for (const { page, blocks } of plans) {
      const parentStack = [page.id];
      const rows = blocks.map((block, index) => {
        const safeDepth = Math.max(0, Math.min(block.depth, parentStack.length));
        const parentId = parentStack[safeDepth] ?? page.id;
        const id = randomUUID();
        parentStack[safeDepth + 1] = id;
        parentStack.length = safeDepth + 2;
        return {
        id,
        workspace_id: page.workspace_id,
        parent_id: parentId,
        root_page_id: page.id,
        project_id: page.project_id,
        title: block.text.split("\n").find((line) => line.trim())?.trim().slice(0, 500) ?? "",
        body_json: {
          format: "outliner_block",
          migratedFrom: "plain_markdown_body",
          sourcePageId: page.id,
          sourceLine: index + 1,
          depth: safeDepth,
        },
        body_text: block.text,
        node_type: "block",
        sort_order: index + 1,
        created_by: page.created_by,
        updated_by: page.updated_by,
      };
      });

      await tx`
        insert into knowledge_nodes ${tx(
          rows,
          "id",
          "workspace_id",
          "parent_id",
          "root_page_id",
          "project_id",
          "title",
          "body_json",
          "body_text",
          "node_type",
          "sort_order",
          "created_by",
          "updated_by",
        )}
      `;

      await tx`
        update knowledge_nodes
        set
          body_json = ${{
            ...(page.body_json && typeof page.body_json === "object" ? page.body_json : {}),
            format: "outliner_page",
            migratedFromPlainMarkdown: {
              at: migratedAt,
              blockCount: blocks.length,
              originalText: page.body_text,
            },
          }},
          body_text = '',
          updated_at = now()
        where id = ${page.id}
      `;
    }
  });

  console.log("Migration applied.");
} finally {
  await sql.end({ timeout: 5 });
}

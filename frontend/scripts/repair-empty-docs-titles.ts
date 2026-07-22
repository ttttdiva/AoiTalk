import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

function loadEnvFiles() {
  for (const fileName of [resolve(process.cwd(), "..", ".env"), resolve(process.cwd(), ".env.local")]) {
    if (!existsSync(fileName)) continue;
    for (const line of readFileSync(fileName, "utf8").split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
      const index = trimmed.indexOf("=");
      const key = trimmed.slice(0, index).trim();
      let value = trimmed.slice(index + 1).trim();
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      if (key && process.env[key] === undefined) process.env[key] = value;
    }
  }
}

type ManifestNode = { key: string; title: string };
type Manifest = { nodes?: ManifestNode[] };

async function main() {
  loadEnvFiles();
  const [{ and, desc, eq, isNull, ne }, { db }, schema, writer, docsUtils] = await Promise.all([
    import("drizzle-orm"),
    import("@/db"),
    import("@/db/schema"),
    import("@/lib/server/docs-node-writer"),
    import("@/lib/server/knowledge-docs-utils"),
  ]);
  const manifestPath = resolve(process.cwd(), "..", "artifacts", "foam_curation", "phase3_source_v1", "manifest.json");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as Manifest;
  const manifestTitles = new Map((manifest.nodes ?? []).map((node) => [
    `foam_source_grounded_v1:node:${node.key}`,
    node.title,
  ]));
  const emptyNodes = await db
    .select()
    .from(schema.knowledgeNodes)
    .where(and(eq(schema.knowledgeNodes.title, ""), isNull(schema.knowledgeNodes.archivedAt)));
  const repairs: Array<{ node: typeof emptyNodes[number]; title: string; source: "revision" | "manifest" }> = [];

  for (const node of emptyNodes) {
    const [revision] = await db
      .select({ title: schema.knowledgeRevisions.title })
      .from(schema.knowledgeRevisions)
      .where(and(eq(schema.knowledgeRevisions.nodeId, node.id), ne(schema.knowledgeRevisions.title, "")))
      .orderBy(desc(schema.knowledgeRevisions.createdAt))
      .limit(1);
    const revisionTitle = revision?.title ?? "";
    const manifestTitle = node.systemKey ? manifestTitles.get(node.systemKey) ?? "" : "";
    const title = revisionTitle || manifestTitle;
    if (title) repairs.push({ node, title, source: revisionTitle ? "revision" : "manifest" });
  }

  console.log(JSON.stringify({ empty: emptyNodes.length, repairable: repairs.length, repairs: repairs.map((item) => ({
    id: item.node.id,
    system_key: item.node.systemKey,
    title: item.title,
    source: item.source,
  })) }, null, 2));
  if (!process.argv.includes("--apply")) return;

  await db.transaction(async (tx) => {
    for (const item of repairs) {
      const actorId = item.node.updatedBy ?? item.node.createdBy;
      if (!actorId) throw new Error(`復元履歴の実行者を特定できません: ${item.node.id}`);
      const updated = await writer.updateDocsNode(tx, item.node.id, {
        title: item.title,
        updatedBy: actorId,
        updatedAt: new Date(),
      });
      await docsUtils.upsertKnowledgeSearchIndex(tx, updated, updated.title);
      await docsUtils.syncKnowledgeNodeReferenceEdges(tx, updated, actorId);
      await docsUtils.appendKnowledgeRevision(
        tx,
        updated,
        actorId,
        `誤った空タイトルを${item.source === "revision" ? "履歴" : "移行manifest"}から復元`,
      );
    }
  });
  console.log(`restored=${repairs.length}`);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });

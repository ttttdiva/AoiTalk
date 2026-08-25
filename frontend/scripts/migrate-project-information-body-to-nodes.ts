import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { and, asc, inArray, isNull, sql } from "drizzle-orm";

type FixtureNode = {
  title: string;
  children?: FixtureNode[];
};

const APPLY = process.argv.includes("--apply");
const MAX_TITLE = 500;

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
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      if (key && process.env[key] === undefined) process.env[key] = value;
    }
  }
}

function cleanLine(line: string) {
  return line.trim().replace(/^[-*]\s+/, "").replace(/\s+/g, " ").trim();
}

function splitLongTitle(value: string): FixtureNode[] {
  const text = cleanLine(value);
  if (!text) return [];
  if (text.length <= MAX_TITLE) return [{ title: text }];
  const nodes: FixtureNode[] = [];
  let rest = text;
  while (rest.length > MAX_TITLE) {
    let cut = Math.max(rest.lastIndexOf("。", MAX_TITLE - 1), rest.lastIndexOf("、", MAX_TITLE - 1));
    if (cut < 80) cut = MAX_TITLE - 1;
    nodes.push({ title: rest.slice(0, cut + 1).trim() });
    rest = rest.slice(cut + 1).trim();
  }
  if (rest) nodes.push({ title: rest });
  return nodes;
}

function splitAttributeLine(line: string): FixtureNode[] {
  const text = cleanLine(line);
  if (text.startsWith("[[")) return splitLongTitle(text);
  const labelMatch = text.match(/^([^:：]{1,40})[:：]\s*(.+)$/);
  if (!labelMatch) return splitLongTitle(text);
  const [, label, value] = labelMatch;
  const parts = value
    .split(/。/)
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length <= 1) return [{ title: `${label}: ${value}` }];
  return [{
    title: label.trim(),
    children: parts.flatMap((part) => splitLongTitle(part)),
  }];
}

function bodyTextToFixture(sourceText: string): FixtureNode[] {
  const lines = sourceText
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map(cleanLine)
    .filter(Boolean);
  return lines.flatMap(splitAttributeLine);
}

function extractIntegrityTokens(value: string) {
  const tokens = new Set<string>();
  for (const match of value.matchAll(/\[\[[^\]]+\]\]/g)) tokens.add(match[0]);
  for (const match of value.matchAll(/`[^`]+`/g)) tokens.add(match[0]);
  for (const match of value.matchAll(/\b\d{4}-\d{2}-\d{2}\b/g)) tokens.add(match[0]);
  for (const match of value.matchAll(/\b\d+(?:\.\d+){1,3}\b/g)) tokens.add(match[0]);
  for (const match of value.matchAll(/[A-Za-z0-9_.-]+\.(?:md|xlsx|xlsm|pdf|html|txt|csv|ts|tsx|py|bat|json)\b/g)) {
    tokens.add(match[0]);
  }
  return Array.from(tokens);
}

function flattenFixture(nodes: FixtureNode[]): string {
  const lines: string[] = [];
  const visit = (node: FixtureNode) => {
    lines.push(node.title);
    for (const child of node.children ?? []) visit(child);
  };
  for (const node of nodes) visit(node);
  return lines.join("\n");
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

async function main() {
  loadEnvFiles();
  const [
    { db },
    { knowledgeNodes, users },
    { appendKnowledgeRevision, decryptNodeBodyJson, decryptNodeBodyText, syncKnowledgeNodeReferenceEdges, upsertKnowledgeSearchIndex },
    { insertDocsNode, updateDocsNode },
  ] = await Promise.all([
    import("@/db"),
    import("@/db/schema"),
    import("@/lib/server/knowledge-docs-utils"),
    import("@/lib/server/docs-node-writer"),
  ]);
  const [fallbackUser] = await db.select({ id: users.id }).from(users).limit(1);
  const fallbackUserId = fallbackUser?.id ?? null;

  const candidates = await db
    .select()
    .from(knowledgeNodes)
    .where(
      and(
        isNull(knowledgeNodes.archivedAt),
        sql`coalesce(${knowledgeNodes.bodyText}, '') <> ''`,
        sql`coalesce(${knowledgeNodes.bodyText}, '') like 'enc:v1:%'`,
      ),
    )
    .orderBy(asc(knowledgeNodes.rootPageId), asc(knowledgeNodes.sortOrder), asc(knowledgeNodes.createdAt));

  const targets = candidates
    .map((node) => ({
      node,
      sourceText: decryptNodeBodyText(node.bodyText ?? ""),
      bodyJson: decryptNodeBodyJson(node.bodyJson ?? {}),
    }))
    .filter(({ node, sourceText }) => sourceText.trim() && sourceText.trim() !== node.title.trim());

  const targetIds = targets.map(({ node }) => node.id);
  const childRows = targetIds.length > 0
    ? await db
        .select({ parentId: knowledgeNodes.parentId })
        .from(knowledgeNodes)
        .where(and(inArray(knowledgeNodes.parentId, targetIds), isNull(knowledgeNodes.archivedAt)))
    : [];
  const childCountByParent = new Map<string, number>();
  for (const row of childRows) {
    if (!row.parentId) continue;
    childCountByParent.set(row.parentId, (childCountByParent.get(row.parentId) ?? 0) + 1);
  }

  const plans = targets.map((target) => {
    const fixture = bodyTextToFixture(target.sourceText);
    const flattened = flattenFixture(fixture);
    const missingTokens = extractIntegrityTokens(target.sourceText).filter((token) => !flattened.includes(token));
    return {
      ...target,
      fixture,
      missingTokens,
      existingChildren: childCountByParent.get(target.node.id) ?? 0,
    };
  });

  const invalid = plans.filter((plan) => plan.fixture.length === 0 || plan.missingTokens.length > 0);
  console.log(`MODE=${APPLY ? "apply" : "dry-run"}`);
  console.log(`TARGET_NODES=${plans.length}`);
  console.log(`CHILDREN_TO_CREATE=${plans.reduce((sum, plan) => sum + plan.fixture.length, 0)}`);
  console.log(`INVALID_PLANS=${invalid.length}`);
  for (const plan of plans) {
    console.log(`- ${plan.node.id} ${plan.node.title}: fixture=${plan.fixture.length} existingChildren=${plan.existingChildren} missingTokens=${plan.missingTokens.length}`);
    if (plan.missingTokens.length > 0) console.log(`  missing: ${plan.missingTokens.join(", ")}`);
  }
  if (invalid.length > 0) {
    throw new Error("Migration plan is invalid. Resolve missing tokens before applying.");
  }
  if (!APPLY) return;

  await db.transaction(async (tx) => {
    for (const plan of plans) {
      const actorId = isUuid(plan.node.updatedBy)
        ? plan.node.updatedBy
        : isUuid(plan.node.createdBy)
          ? plan.node.createdBy
          : fallbackUserId;
      if (!actorId) throw new Error(`No valid actor user id for ${plan.node.id}`);
      const rootPageId = plan.node.rootPageId ?? plan.node.id;
      const createTree = async (parentId: string, nodes: FixtureNode[], depth: number) => {
        for (const [index, item] of nodes.entries()) {
          const child = await insertDocsNode(tx, {
            id: randomUUID(),
            docsLibraryId: plan.node.docsLibraryId,
            parentId,
            rootPageId,
            projectId: plan.node.projectId,
            title: item.title,
            bodyJson: {
              format: "project_information_migrated_node",
              sourceSectionId: plan.node.id,
              depth,
            },
            nodeType: "node",
            sortOrder: index,
            createdBy: actorId,
            updatedBy: actorId,
          });
          await upsertKnowledgeSearchIndex(tx, child, child.title);
          await syncKnowledgeNodeReferenceEdges(tx, child, actorId);
          if (item.children?.length) await createTree(child.id, item.children, depth + 1);
        }
      };
      await createTree(plan.node.id, plan.fixture, plan.existingChildren);
      const updatedParent = await updateDocsNode(tx, plan.node.id, {
        title: plan.node.title,
        bodyJson: {
          ...plan.bodyJson,
          migratedBodyTextToNodes: {
            at: new Date().toISOString(),
            childCount: plan.fixture.length,
            source: "migrate-project-information-body-to-nodes",
          },
        },
        updatedBy: actorId,
        updatedAt: new Date(),
      });
      await upsertKnowledgeSearchIndex(tx, updatedParent, updatedParent.title);
      await syncKnowledgeNodeReferenceEdges(tx, updatedParent, actorId);
      await appendKnowledgeRevision(tx, updatedParent, actorId, "案件情報body_textを子ノードへ移行");
    }
  });
  console.log("Migration applied.");
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });

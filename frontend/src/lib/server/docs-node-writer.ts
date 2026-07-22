import { eq, inArray } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes } from "@/db/schema";
import {
  decryptJsonValueIfNeeded,
  decryptTextIfNeeded,
  encryptJsonValue,
  encryptText,
} from "./field-crypto";

type DocsTransaction = Parameters<Parameters<typeof db.transaction>[0]>[0];
type DocsDb = typeof db | DocsTransaction;

const NODE_BODY_TEXT_AAD = "knowledge_nodes.body_text";
const NODE_BODY_JSON_AAD = "knowledge_nodes.body_json";
export const DOCS_NODE_TITLE_MAX = 20_000;

type KnowledgeNodeInsert = typeof knowledgeNodes.$inferInsert;
type KnowledgeNodeUpdate = Partial<KnowledgeNodeInsert>;

export type DocsNodeWriterInsert = Omit<KnowledgeNodeInsert, "bodyText" | "bodyJson"> & {
  bodyJson?: Record<string, unknown>;
};

export type DocsNodeWriterUpdate = Omit<KnowledgeNodeUpdate, "bodyText" | "bodyJson"> & {
  bodyJson?: Record<string, unknown>;
};

export function docsNodeTitleMirror(title: string | null | undefined) {
  const mirror = String(title ?? "");
  if (mirror.includes("\n") || mirror.includes("\r")) {
    throw new Error("Docs node body_text mirror must not contain newlines");
  }
  if (mirror.length > DOCS_NODE_TITLE_MAX) {
    throw new Error(`Docs node body_text mirror must be ${DOCS_NODE_TITLE_MAX} characters or less`);
  }
  return mirror;
}

function encryptBodyTextMirror(title: string | null | undefined) {
  return encryptText(docsNodeTitleMirror(title), NODE_BODY_TEXT_AAD) ?? "";
}

function encryptBodyJson(value: Record<string, unknown> | null | undefined) {
  return encryptJsonValue(value ?? {}, NODE_BODY_JSON_AAD) as Record<string, unknown> | string;
}

export function decryptDocsNodeBodyText(value: string | null | undefined) {
  return decryptTextIfNeeded(value ?? "", NODE_BODY_TEXT_AAD) ?? "";
}

export function decryptDocsNodeBodyJson(value: unknown): Record<string, unknown> {
  const decrypted = decryptJsonValueIfNeeded(value ?? {}, NODE_BODY_JSON_AAD);
  if (!decrypted || typeof decrypted !== "object" || Array.isArray(decrypted)) return {};
  return { ...(decrypted as Record<string, unknown>) };
}

export async function insertDocsNode(client: DocsDb, input: DocsNodeWriterInsert) {
  if (input.projectId && !input.parentId) {
    throw new Error("Project-scoped Docs nodes require a parent under 案件情報");
  }
  const [node] = await client
    .insert(knowledgeNodes)
    .values({
      ...input,
      bodyText: encryptBodyTextMirror(input.title),
      bodyJson: encryptBodyJson(input.bodyJson),
    })
    .returning();
  return node;
}

export async function updateDocsNode(
  client: DocsDb,
  nodeId: string,
  input: DocsNodeWriterUpdate,
) {
  const values: KnowledgeNodeUpdate = { ...input };
  if (input.title !== undefined) {
    values.bodyText = encryptBodyTextMirror(input.title);
  }
  if (input.bodyJson !== undefined) {
    values.bodyJson = encryptBodyJson(input.bodyJson);
  }
  const [node] = await client
    .update(knowledgeNodes)
    .set(values)
    .where(eq(knowledgeNodes.id, nodeId))
    .returning();
  return node;
}

export async function updateDocsNodesByIds(
  client: DocsDb,
  nodeIds: string[],
  input: DocsNodeWriterUpdate,
) {
  if (nodeIds.length === 0) return [];
  const values: KnowledgeNodeUpdate = { ...input };
  if (input.title !== undefined) {
    values.bodyText = encryptBodyTextMirror(input.title);
  }
  if (input.bodyJson !== undefined) {
    values.bodyJson = encryptBodyJson(input.bodyJson);
  }
  return await client
    .update(knowledgeNodes)
    .set(values)
    .where(inArray(knowledgeNodes.id, nodeIds))
    .returning();
}

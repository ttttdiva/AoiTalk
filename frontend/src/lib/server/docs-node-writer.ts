import { eq, inArray } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes } from "@/db/schema";
import {
  blankParagraphBodyJson,
  clearBlankParagraphMarker,
  isExplicitBlankParagraph,
} from "@/lib/docs-block-model";
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

const EDITABLE_DOC_BLOCK_TYPES = new Set(["markdown", "code"]);
const LEGACY_IMMUTABLE_BODY_KEYS = new Set(["verbatim_blocks", "verbatim_content"]);

type KnowledgeNodeInsert = typeof knowledgeNodes.$inferInsert;
type KnowledgeNodeUpdate = Partial<KnowledgeNodeInsert>;

type DocsNodeWriterInsertBase = Omit<KnowledgeNodeInsert, "bodyText" | "bodyJson" | "docsLibraryId"> & {
  bodyJson?: Record<string, unknown>;
};

/**
 * `docsLibraryId` is canonical.  Accepting `workspaceId` here is a narrow
 * source-level compatibility boundary for older scripts/mobile callers; it
 * is normalized before the Drizzle insert/update reaches the DB.
 */
export type DocsNodeWriterInsert = DocsNodeWriterInsertBase &
  ({ docsLibraryId: string; workspaceId?: string } | { docsLibraryId?: string; workspaceId: string });

export type DocsNodeWriterUpdate = Omit<KnowledgeNodeUpdate, "bodyText" | "bodyJson" | "docsLibraryId"> & {
  docsLibraryId?: string;
  workspaceId?: string;
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

function isMeaningfulDocsNodeTitle(title: string | null | undefined) {
  return String(title ?? "").trim().length > 0;
}

/**
 * Empty titles are a persisted editor state only for an explicit paragraph
 * block.  Keep this check at the writer boundary as well as at the route
 * boundary: imports, jobs, and direct transaction callers must not be able
 * to create arbitrary blank KnowledgeNodes.
 */
function assertDocsNodeTitleWrite(
  title: string | null | undefined,
  bodyJson: unknown,
  nodeType: string | null | undefined,
  systemKey?: string | null,
) {
  if (isMeaningfulDocsNodeTitle(title)) return;
  if (
    !systemKey &&
    isExplicitBlankParagraph(title, bodyJson, nodeType ?? "node")
  ) {
    return;
  }
  throw new Error("空行はDocs nodeとして保存できません");
}

export function normalizeDocsNodeTitleIdentity(title: string | null | undefined) {
  return String(title ?? "")
    .replace(/[\s\u3000]+/gu, " ")
    .trim()
    .toLocaleLowerCase("ja-JP");
}

export function docsNodeTitlesMatch(parentTitle: string | null | undefined, childTitle: string | null | undefined) {
  const parentIdentity = normalizeDocsNodeTitleIdentity(parentTitle);
  return parentIdentity.length > 0 && parentIdentity === normalizeDocsNodeTitleIdentity(childTitle);
}

/**
 * 親と同名の子nodeになる作成・改名・移動を拒否する。
 *
 * 判定対象は親node自身のtitleだけで、兄弟node同士の同名は許可する
 * （例: 同じ件名のメールを「メール管理」配下へ複数保存する場合）。
 * Python側 `DocsGraphService._ensure_parent_title_available` と同じ意味論。
 */
async function ensureDocsNodeParentTitleAvailable(
  client: DocsDb,
  parentId: string,
  title: string | null | undefined,
) {
  if (!normalizeDocsNodeTitleIdentity(title)) return;
  // 親行を FOR UPDATE でロックし、そのロック下で読んだ title を判定に使う。
  // 判定と insert/update の間に親の改名が割り込む競合を防ぐ。
  // 通常の呼び出し元はすべてトランザクション内なので、ロックはその範囲で保持される。
  const [parent] = await client
    .select({ title: knowledgeNodes.title })
    .from(knowledgeNodes)
    .where(eq(knowledgeNodes.id, parentId))
    .limit(1)
    .for("update");
  if (!parent) return;
  if (docsNodeTitlesMatch(parent.title, title)) {
    throw new Error("親と同名の子nodeは作成できません");
  }
}

function encryptBodyTextMirror(title: string | null | undefined) {
  return encryptText(docsNodeTitleMirror(title), NODE_BODY_TEXT_AAD) ?? "";
}

function encryptBodyJson(value: Record<string, unknown> | null | undefined) {
  return encryptJsonValue(value ?? {}, NODE_BODY_JSON_AAD) as Record<string, unknown> | string;
}

function assertNoLegacyImmutableBodyKeys(value: unknown, seen = new WeakSet<object>()) {
  if (Array.isArray(value)) {
    for (const item of value) assertNoLegacyImmutableBodyKeys(item, seen);
    return;
  }
  if (!value || typeof value !== "object") return;
  if (seen.has(value)) return;
  seen.add(value);
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    if (LEGACY_IMMUTABLE_BODY_KEYS.has(key)) {
      throw new Error(`bodyJson.${key} is no longer accepted; use an editable markdown/code block`);
    }
    assertNoLegacyImmutableBodyKeys(child, seen);
  }
}

/** Validate the shared editable Docs body envelope before encryption. */
export function normalizeDocsNodeBodyJson(value: unknown): Record<string, unknown> {
  if (value === null || value === undefined) return {};
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Docs bodyJson must be an object");
  }
  assertNoLegacyImmutableBodyKeys(value);
  const body = { ...(value as Record<string, unknown>) };
  const blockType = body.block_type;
  if (typeof blockType === "string" && EDITABLE_DOC_BLOCK_TYPES.has(blockType)) {
    if (body.format !== "doc_block") {
      throw new Error("editable markdown/code blocks require format=doc_block");
    }
    if (typeof body.content !== "string") {
      throw new Error("editable markdown/code blocks require string content");
    }
    if (typeof body.label !== "string") {
      throw new Error("editable markdown/code blocks require string label");
    }
  }
  return body;
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
  const bodyJson = normalizeDocsNodeBodyJson(input.bodyJson);
  assertDocsNodeTitleWrite(
    input.title,
    bodyJson,
    input.nodeType ?? "node",
    input.systemKey,
  );
  if (input.projectId && !input.parentId) {
    throw new Error("Project-scoped Docs nodes require a parent under 案件情報");
  }
  if (input.parentId) {
    await ensureDocsNodeParentTitleAvailable(client, String(input.parentId), input.title);
  }
  const { workspaceId: _legacyWorkspaceId, docsLibraryId, ...rest } = input;
  const normalizedDocsLibraryId = docsLibraryId ?? _legacyWorkspaceId;
  if (!normalizedDocsLibraryId) throw new Error("Docs node requires docsLibraryId");
  const persistedBodyJson = isMeaningfulDocsNodeTitle(input.title)
    ? clearBlankParagraphMarker(bodyJson)
    : blankParagraphBodyJson(bodyJson);
  const [node] = await client
    .insert(knowledgeNodes)
    .values({
      ...rest,
      docsLibraryId: normalizedDocsLibraryId,
      bodyText: encryptBodyTextMirror(input.title),
      bodyJson: encryptBodyJson(persistedBodyJson),
    })
    .returning();
  return node;
}

export async function updateDocsNode(
  client: DocsDb,
  nodeId: string,
  input: DocsNodeWriterUpdate,
) {
  const [current] = input.title !== undefined || input.parentId !== undefined || input.bodyJson !== undefined || input.nodeType !== undefined || input.systemKey !== undefined
    ? await client
      .select({
        parentId: knowledgeNodes.parentId,
        title: knowledgeNodes.title,
        bodyJson: knowledgeNodes.bodyJson,
        nodeType: knowledgeNodes.nodeType,
        systemKey: knowledgeNodes.systemKey,
      })
      .from(knowledgeNodes)
      .where(eq(knowledgeNodes.id, nodeId))
      .limit(1)
    : [undefined];

  const normalizedBodyJson = input.bodyJson !== undefined
    ? normalizeDocsNodeBodyJson(input.bodyJson)
    : undefined;
  const nextTitle = input.title !== undefined ? input.title : current?.title;
  const nextNodeType = input.nodeType !== undefined
    ? String(input.nodeType)
    : current?.nodeType ?? "node";
  const nextSystemKey = input.systemKey !== undefined
    ? input.systemKey
    : current?.systemKey;

  if (input.title !== undefined) {
    // A transition to blank must carry the explicit body envelope in the
    // same mutation.  Do not accept a previously blank row as proof for a
    // title-only PATCH; this prevents accidental blanking through autosave.
    assertDocsNodeTitleWrite(
      input.title,
      normalizedBodyJson,
      nextNodeType,
      nextSystemKey,
    );
  } else if (current && !isMeaningfulDocsNodeTitle(nextTitle)) {
    // Existing blank rows may receive metadata-only updates, but only while
    // they remain a valid explicit paragraph.  Changing node type/system
    // identity without the matching body envelope is rejected.
    assertDocsNodeTitleWrite(
      nextTitle,
      normalizedBodyJson ?? current?.bodyJson,
      nextNodeType,
      nextSystemKey,
    );
  }

  if (input.title !== undefined || input.parentId !== undefined) {
    const nextParentId = input.parentId !== undefined ? input.parentId : current?.parentId;
    if (current && nextParentId) {
      await ensureDocsNodeParentTitleAvailable(
        client,
        String(nextParentId),
        nextTitle,
      );
    }
  }
  const { workspaceId: _legacyWorkspaceId, ...rest } = input;
  const values: KnowledgeNodeUpdate = {
    ...rest,
    ...(input.docsLibraryId === undefined && _legacyWorkspaceId !== undefined
      ? { docsLibraryId: _legacyWorkspaceId }
      : {}),
  };
  if (input.title !== undefined) {
    values.bodyText = encryptBodyTextMirror(input.title);
  }
  if (normalizedBodyJson !== undefined) {
    values.bodyJson = encryptBodyJson(
      isMeaningfulDocsNodeTitle(nextTitle)
        ? clearBlankParagraphMarker(normalizedBodyJson)
        : blankParagraphBodyJson(normalizedBodyJson),
    );
  } else if (
    input.title !== undefined &&
    isMeaningfulDocsNodeTitle(input.title) &&
    current &&
    isExplicitBlankParagraph(current.title, current.bodyJson, current.nodeType)
  ) {
    // Returning from a blank paragraph must clear the marker atomically with
    // the title mirror, even for direct writer callers that omit body_json.
    values.bodyJson = encryptBodyJson(clearBlankParagraphMarker(current.bodyJson));
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
  const normalizedBodyJson = input.bodyJson !== undefined
    ? normalizeDocsNodeBodyJson(input.bodyJson)
    : undefined;
  if (input.title !== undefined) {
    assertDocsNodeTitleWrite(
      input.title,
      normalizedBodyJson,
      input.nodeType ?? "node",
      input.systemKey,
    );
  }
  const { workspaceId: _legacyWorkspaceId, ...rest } = input;
  const values: KnowledgeNodeUpdate = {
    ...rest,
    ...(input.docsLibraryId === undefined && _legacyWorkspaceId !== undefined
      ? { docsLibraryId: _legacyWorkspaceId }
      : {}),
  };
  if (input.title !== undefined) {
    values.bodyText = encryptBodyTextMirror(input.title);
  }
  if (normalizedBodyJson !== undefined) {
    values.bodyJson = encryptBodyJson(
      isMeaningfulDocsNodeTitle(input.title)
        ? clearBlankParagraphMarker(normalizedBodyJson)
        : blankParagraphBodyJson(normalizedBodyJson),
    );
  }
  return await client
    .update(knowledgeNodes)
    .set(values)
    .where(inArray(knowledgeNodes.id, nodeIds))
    .returning();
}

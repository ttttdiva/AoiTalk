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

/** A client-visible invariant failure, distinct from an unexpected DB error. */
export class DocsNodeInvariantError extends Error {
  readonly status = 409;
  readonly code = "docs_node_invariant_violation";

  constructor(message: string) {
    super(message);
    this.name = "DocsNodeInvariantError";
  }
}

type KnowledgeNodeInsert = typeof knowledgeNodes.$inferInsert;
type KnowledgeNodeUpdate = Partial<KnowledgeNodeInsert>;

type DocsNodeWriterInsertBase = Omit<KnowledgeNodeInsert, "bodyText" | "bodyJson" | "docsLibraryId" | "isExplicitBlank"> & {
  bodyJson?: Record<string, unknown>;
};

/**
 * `docsLibraryId` is canonical.  Accepting `workspaceId` here is a narrow
 * source-level compatibility boundary for older scripts/mobile callers; it
 * is normalized before the Drizzle insert/update reaches the DB.
 */
export type DocsNodeWriterInsert = DocsNodeWriterInsertBase &
  ({ docsLibraryId: string; workspaceId?: string } | { docsLibraryId?: string; workspaceId: string });

export type DocsNodeWriterUpdate = Omit<KnowledgeNodeUpdate, "bodyText" | "bodyJson" | "docsLibraryId" | "isExplicitBlank"> & {
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

/** Identity-bearing Project-information roots may intentionally share the
 * Personal hub label (a project itself can be named 「案件情報」).  Ordinary
 * children still obey the parent-title uniqueness invariant. */
function isProjectInformationRootSystemKey(systemKey: string | null | undefined) {
  return typeof systemKey === "string"
    && systemKey.trim().startsWith("project_information:");
}

/**
 * Derive the SQL-visible blank discriminator from the same strict body
 * envelope used by the encrypted body_json contract.  Callers never supply
 * this value directly: every writer mutation derives it so a markerless or
 * non-paragraph empty row cannot become visible by accident.
 */
function deriveExplicitBlankFlag(
  title: string | null | undefined,
  bodyJson: unknown,
  nodeType: string | null | undefined,
  systemKey?: string | null,
) {
  return !systemKey && isExplicitBlankParagraph(title, bodyJson, nodeType ?? "node");
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
  throw new DocsNodeInvariantError("空行はDocs nodeとして保存できません");
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
    .select({ title: knowledgeNodes.title, archivedAt: knowledgeNodes.archivedAt })
    .from(knowledgeNodes)
    .where(eq(knowledgeNodes.id, parentId))
    .limit(1)
    .for("update");
  if (!parent) return;
  if (parent.archivedAt) {
    throw new DocsNodeInvariantError("アーカイブ済みnodeの下には作成/移動できません");
  }
  if (docsNodeTitlesMatch(parent.title, title)) {
    throw new DocsNodeInvariantError("親と同名の子nodeは作成できません");
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
  if (input.parentId && !isProjectInformationRootSystemKey(input.systemKey)) {
    await ensureDocsNodeParentTitleAvailable(client, String(input.parentId), input.title);
  }
  const { workspaceId: _legacyWorkspaceId, docsLibraryId, ...rest } = input;
  const normalizedDocsLibraryId = docsLibraryId ?? _legacyWorkspaceId;
  if (!normalizedDocsLibraryId) throw new Error("Docs node requires docsLibraryId");
  const persistedBodyJson = isMeaningfulDocsNodeTitle(input.title)
    ? clearBlankParagraphMarker(bodyJson)
    : blankParagraphBodyJson(bodyJson);
  const isExplicitBlank = deriveExplicitBlankFlag(
    input.title,
    persistedBodyJson,
    input.nodeType ?? "node",
    input.systemKey,
  );
  const [node] = await client
    .insert(knowledgeNodes)
    .values({
      ...rest,
      docsLibraryId: normalizedDocsLibraryId,
      isExplicitBlank,
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
        isExplicitBlank: knowledgeNodes.isExplicitBlank,
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
    if (normalizedBodyJson !== undefined) {
      assertDocsNodeTitleWrite(nextTitle, normalizedBodyJson, nextNodeType, nextSystemKey);
    } else if (
      (
        !current.isExplicitBlank
        && !isExplicitBlankParagraph(nextTitle, current.bodyJson, nextNodeType)
      )
      || nextNodeType !== "node"
      || Boolean(nextSystemKey)
    ) {
      throw new DocsNodeInvariantError("空行はDocs nodeとして保存できません");
    }
  }

  if (input.title !== undefined || input.parentId !== undefined) {
    const nextParentId = input.parentId !== undefined ? input.parentId : current?.parentId;
    if (current && nextParentId && !isProjectInformationRootSystemKey(nextSystemKey)) {
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
    values.isExplicitBlank = deriveExplicitBlankFlag(
      nextTitle,
      normalizedBodyJson,
      nextNodeType,
      nextSystemKey,
    );
  } else if (
    input.title !== undefined &&
    isMeaningfulDocsNodeTitle(input.title) &&
    current
  ) {
    // Returning from a blank paragraph must clear the marker atomically with
    // the title mirror, even for direct writer callers that omit body_json.
    // body_json may still be ciphertext here, so do not inspect it merely to
    // derive the independent SQL discriminator.  It is safe to decrypt at the
    // application boundary, however, and doing so preserves any non-marker
    // metadata while removing a stale blank marker.
    if (
      current.isExplicitBlank
      || isExplicitBlankParagraph(current.title, current.bodyJson, current.nodeType)
    ) {
      values.bodyJson = encryptBodyJson(
        clearBlankParagraphMarker(decryptDocsNodeBodyJson(current.bodyJson)),
      );
    }
    values.isExplicitBlank = false;
  } else if (current && input.title === undefined && input.bodyJson === undefined) {
    // Metadata-only updates preserve the already-verified discriminator.  The
    // migration/backfill verifier guarantees that true rows carry the strict
    // encrypted body marker; no plaintext body inspection belongs in SQL.
    values.isExplicitBlank = Boolean(current.isExplicitBlank);
  }
  const [node] = await client
    .update(knowledgeNodes)
    .set(values)
    .where(eq(knowledgeNodes.id, nodeId))
    .returning();
  return node;
}

/** Apply lifecycle-only metadata (archive/restore) without revalidating the
 * title/body contract.  It still derives the non-sensitive blank discriminator
 * so legacy markerless rows remain visible/restorable when their encrypted
 * paragraph marker is valid. */
export async function updateDocsNodeLifecycle(
  client: DocsDb,
  nodeId: string,
  input: Pick<DocsNodeWriterUpdate, "archivedAt" | "updatedBy" | "updatedAt">,
) {
  const [current] = await client
    .select({
      title: knowledgeNodes.title,
      bodyJson: knowledgeNodes.bodyJson,
      nodeType: knowledgeNodes.nodeType,
      systemKey: knowledgeNodes.systemKey,
      isExplicitBlank: knowledgeNodes.isExplicitBlank,
    })
    .from(knowledgeNodes)
    .where(eq(knowledgeNodes.id, nodeId))
    .limit(1);
  const values: KnowledgeNodeUpdate = { ...input };
  if (current) {
    try {
      // Lifecycle-only archive/restore must still maintain the SQL-visible
      // discriminator for legacy rows whose encrypted marker predates the
      // column migration.  Decryption failures are surfaced rather than
      // silently classifying sensitive data as an ordinary blank.
      values.isExplicitBlank = current.title !== "" && current.isExplicitBlank !== true
        ? false
        : deriveExplicitBlankFlag(
            current.title,
            decryptDocsNodeBodyJson(current.bodyJson),
            current.nodeType,
            current.systemKey,
          );
    } catch {
      throw new DocsNodeInvariantError(
        "Docs nodeのblank状態を確認できないためライフサイクル更新を中止しました",
      );
    }
  }
  const [node] = await client
    .update(knowledgeNodes)
    .set(values)
    .where(eq(knowledgeNodes.id, nodeId))
    .returning();
  return node;
}

/**
 * Archive a pre-validated stale/orphan closure without reading encrypted body
 * fields.  The Project-information cleanup route has already locked and
 * verified the complete hierarchy; requiring a body decrypt here would make a
 * malformed legacy ciphertext permanently undeletable.  Keep this escape hatch
 * private to the writer so the generic Docs mutation gate still has one clear
 * write owner.
 */
export async function archiveDocsNodesForCleanup(
  client: DocsDb,
  nodeIds: string[],
  input: Pick<DocsNodeWriterUpdate, "archivedAt" | "updatedBy" | "updatedAt">,
) {
  if (nodeIds.length === 0) return [];
  return client
    .update(knowledgeNodes)
    .set(input)
    .where(inArray(knowledgeNodes.id, nodeIds))
    .returning();
}

export async function updateDocsNodesByIds(
  client: DocsDb,
  nodeIds: string[],
  input: DocsNodeWriterUpdate,
) {
  if (nodeIds.length === 0) return [];
  const lifecycleOnly = Object.keys(input).every((key) =>
    key === "archivedAt" || key === "updatedBy" || key === "updatedAt",
  );
  if (lifecycleOnly) {
    const currentRows = await client
      .select({
        id: knowledgeNodes.id,
        title: knowledgeNodes.title,
        bodyJson: knowledgeNodes.bodyJson,
        nodeType: knowledgeNodes.nodeType,
        systemKey: knowledgeNodes.systemKey,
        isExplicitBlank: knowledgeNodes.isExplicitBlank,
      })
      .from(knowledgeNodes)
      .where(inArray(knowledgeNodes.id, nodeIds));
    const updatedRows: Array<typeof knowledgeNodes.$inferSelect> = [];
    for (const current of currentRows) {
      let isExplicitBlank: boolean;
      try {
        isExplicitBlank = current.title !== "" && current.isExplicitBlank !== true
          ? false
          : deriveExplicitBlankFlag(
              current.title,
              decryptDocsNodeBodyJson(current.bodyJson),
              current.nodeType,
              current.systemKey,
            );
      } catch {
        throw new DocsNodeInvariantError(
          "Docs nodeのblank状態を確認できないためライフサイクル更新を中止しました",
        );
      }
      const [updated] = await client
        .update(knowledgeNodes)
        .set({ ...input, isExplicitBlank })
        .where(eq(knowledgeNodes.id, current.id))
        .returning();
      if (updated) updatedRows.push(updated);
    }
    return updatedRows;
  }
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
    values.isExplicitBlank = deriveExplicitBlankFlag(
      input.title,
      normalizedBodyJson,
      input.nodeType ?? "node",
      input.systemKey,
    );
  } else if (input.title !== undefined) {
    values.isExplicitBlank = deriveExplicitBlankFlag(
      input.title,
      normalizedBodyJson,
      input.nodeType ?? "node",
      input.systemKey,
    );
  }
  return await client
    .update(knowledgeNodes)
    .set(values)
    .where(inArray(knowledgeNodes.id, nodeIds))
    .returning();
}

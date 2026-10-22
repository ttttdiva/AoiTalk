import AsyncStorage from "@react-native-async-storage/async-storage";
import { eq } from "drizzle-orm";
import { STORAGE_KEYS } from "../constants/config";
import { getDb, schema } from "../db/client";

export type AuthRecoveryCategorySummary = {
  /** False when this local inventory could not be read safely. */
  readable: boolean;
  /** Null means the count is unknown; never treat a failed read as zero. */
  pendingCount: number | null;
  /** Actor scopes found in durable metadata, without payloads or draft text. */
  authScopes: string[];
  /** True for legacy/unowned rows or malformed scoped keys. */
  hasUnknownOwner: boolean;
};

export type AuthRecoveryOutboxSummary = AuthRecoveryCategorySummary;

export const AUTH_RECOVERY_DIRTY_DOCS_TABLES = [
  "knowledge_nodes",
  "knowledge_supertags",
  "knowledge_node_supertags",
  "knowledge_field_values",
] as const;

export type AuthRecoveryDirtyDocsTable = (typeof AUTH_RECOVERY_DIRTY_DOCS_TABLES)[number];

export type AuthRecoveryDirtyDocsSummary = AuthRecoveryCategorySummary & {
  tableCounts: Record<AuthRecoveryDirtyDocsTable, number | null>;
};

export type AuthRecoveryInventory = {
  outbox: AuthRecoveryCategorySummary;
  dirtyDocs: AuthRecoveryDirtyDocsSummary;
  composerDrafts: AuthRecoveryCategorySummary;
};

const unreadableSummary = (): AuthRecoveryCategorySummary => ({
  readable: false,
  pendingCount: null,
  authScopes: [],
  hasUnknownOwner: true,
});

const emptySummary = (): AuthRecoveryCategorySummary => ({
  readable: true,
  pendingCount: 0,
  authScopes: [],
  hasUnknownOwner: false,
});

const unreadableDirtyDocsSummary = (): AuthRecoveryDirtyDocsSummary => ({
  ...unreadableSummary(),
  tableCounts: Object.fromEntries(
    AUTH_RECOVERY_DIRTY_DOCS_TABLES.map((tableName) => [tableName, null]),
  ) as Record<AuthRecoveryDirtyDocsTable, number | null>,
});

const emptyDirtyDocsSummary = (): AuthRecoveryDirtyDocsSummary => ({
  ...emptySummary(),
  tableCounts: Object.fromEntries(
    AUTH_RECOVERY_DIRTY_DOCS_TABLES.map((tableName) => [tableName, 0]),
  ) as Record<AuthRecoveryDirtyDocsTable, number | null>,
});

function summarizeScopes(scopes: Array<string | null | undefined>, count: number): AuthRecoveryCategorySummary {
  const authScopes = new Set<string>();
  let hasUnknownOwner = false;
  for (const value of scopes) {
    const scope = typeof value === "string" ? value.trim() : "";
    if (!scope) hasUnknownOwner = true;
    else authScopes.add(scope);
  }
  return {
    readable: true,
    pendingCount: count,
    authScopes: [...authScopes].sort(),
    hasUnknownOwner,
  };
}

async function readOutboxSummary(): Promise<AuthRecoveryOutboxSummary> {
  try {
    const rows = await getDb()
      .select({ authScope: schema.outbox.authScope })
      .from(schema.outbox);
    return summarizeScopes(
      rows.map((row) => row.authScope),
      rows.length,
    );
  } catch {
    return unreadableSummary();
  }
}

type DirtyReference = { tableName: string; entityKey: string };

async function readDirtyDocsSummary(): Promise<AuthRecoveryDirtyDocsSummary> {
  try {
    const db = getDb();
    const [nodes, supertags, nodeSupertags, fieldValues] = await Promise.all([
      db
        .select({ id: schema.knowledgeNodes.id })
        .from(schema.knowledgeNodes)
        .where(eq(schema.knowledgeNodes.dirty, true)),
      db
        .select({ id: schema.knowledgeSupertags.id })
        .from(schema.knowledgeSupertags)
        .where(eq(schema.knowledgeSupertags.dirty, true)),
      db
        .select({ nodeId: schema.knowledgeNodeSupertags.nodeId, supertagId: schema.knowledgeNodeSupertags.supertagId })
        .from(schema.knowledgeNodeSupertags)
        .where(eq(schema.knowledgeNodeSupertags.dirty, true)),
      db
        .select({ nodeId: schema.knowledgeFieldValues.nodeId, fieldId: schema.knowledgeFieldValues.fieldId })
        .from(schema.knowledgeFieldValues)
        .where(eq(schema.knowledgeFieldValues.dirty, true)),
    ]);
    const tableCounts: Record<AuthRecoveryDirtyDocsTable, number | null> = {
      knowledge_nodes: nodes.length,
      knowledge_supertags: supertags.length,
      knowledge_node_supertags: nodeSupertags.length,
      knowledge_field_values: fieldValues.length,
    };
    const dirtyRows: DirtyReference[] = [
      ...nodes.map((row) => ({ tableName: "knowledge_nodes", entityKey: row.id })),
      ...supertags.map((row) => ({ tableName: "knowledge_supertags", entityKey: row.id })),
      ...nodeSupertags.map((row) => ({
        tableName: "knowledge_node_supertags",
        entityKey: `${row.nodeId}:${row.supertagId}`,
      })),
      ...fieldValues.map((row) => ({
        tableName: "knowledge_field_values",
        entityKey: `${row.nodeId}:${row.fieldId}`,
      })),
    ];
    if (!dirtyRows.length) return emptyDirtyDocsSummary();

    const memberships = await db
      .select({
        authScope: schema.docsScopeMembership.authScope,
        tableName: schema.docsScopeMembership.tableName,
        entityKey: schema.docsScopeMembership.entityKey,
      })
      .from(schema.docsScopeMembership);

    const scopes: Array<string | null> = [];
    let hasUnknownOwner = false;
    for (const dirty of dirtyRows) {
      const owners = new Set(
        memberships
          .filter(
            (membership) =>
              membership.tableName === dirty.tableName
              && membership.entityKey === dirty.entityKey,
          )
          .map((membership) => membership.authScope),
      );
      if (owners.size !== 1) {
        hasUnknownOwner = true;
        continue;
      }
      scopes.push([...owners][0]);
    }
    const summary = summarizeScopes(scopes, dirtyRows.length);
    return {
      ...summary,
      tableCounts,
      hasUnknownOwner: hasUnknownOwner || summary.hasUnknownOwner,
    };
  } catch {
    return unreadableDirtyDocsSummary();
  }
}

function draftScopeFromStorageKey(key: string, prefix: string): string | null {
  if (!key.startsWith(prefix)) return null;
  const encoded = key.slice(prefix.length);
  try {
    const parsed: unknown = JSON.parse(decodeURIComponent(encoded));
    if (!Array.isArray(parsed) || parsed.length !== 3) return null;
    const authUser = typeof parsed[1] === "string" ? parsed[1].trim() : "";
    return authUser ? `auth:${authUser}` : null;
  } catch {
    return null;
  }
}

async function readComposerDraftSummary(): Promise<AuthRecoveryCategorySummary> {
  try {
    const prefix = `${STORAGE_KEYS.CHAT_COMPOSER_DRAFT_PREFIX}:`;
    const keys = await AsyncStorage.getAllKeys();
    const draftKeys = keys.filter((key) => key.startsWith(prefix));
    if (!draftKeys.length) return emptySummary();
    const records = await AsyncStorage.multiGet(draftKeys);
    const valuesByKey = new Map(records.map(([key, value]) => [key, value]));
    const scopes: Array<string | null> = [];
    let hasUnknownOwner = false;
    for (const key of draftKeys) {
      const scope = draftScopeFromStorageKey(key, prefix);
      const raw = valuesByKey.get(key);
      if (!scope || typeof raw !== "string" || !raw.trim()) {
        hasUnknownOwner = true;
        continue;
      }
      try {
        const record = JSON.parse(raw) as Record<string, unknown>;
        if (
          record.version !== 1
          || typeof record.text !== "string"
          || typeof record.revision !== "string"
          || !record.revision.trim()
        ) {
          hasUnknownOwner = true;
          continue;
        }
      } catch {
        hasUnknownOwner = true;
        continue;
      }
      scopes.push(scope);
    }
    const summary = summarizeScopes(scopes, draftKeys.length);
    return {
      ...summary,
      hasUnknownOwner: hasUnknownOwner || summary.hasUnknownOwner,
    };
  } catch {
    return unreadableSummary();
  }
}

/**
 * Read only the durable metadata needed to gate an unknown-actor
 * reauthentication. Payloads, entity content, and operation IDs are never
 * selected. Draft text is inspected transiently only to validate its record;
 * it is never returned, logged, or displayed.
 */
export async function readAuthRecoveryInventory(): Promise<AuthRecoveryInventory> {
  const [outbox, dirtyDocs, composerDrafts] = await Promise.all([
    readOutboxSummary(),
    readDirtyDocsSummary(),
    readComposerDraftSummary(),
  ]);
  return { outbox, dirtyDocs, composerDrafts };
}

/** Backward-compatible focused view for callers that only need outbox metadata. */
export async function readAuthRecoveryOutboxSummary(): Promise<AuthRecoveryOutboxSummary> {
  return readOutboxSummary();
}

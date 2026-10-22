/** Exact authenticated destinations for new Docs. Never infer from a sibling library. */
import { and, eq, isNull } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { getTokenAuthScope, getTokenSnapshot } from "../lib/auth";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import { notifyMutationQueued } from "../sync/task-mutation-wake";

type Reader = Pick<ReturnType<typeof getDb>, "select">;
export type DocsCreateScope = {
  workspaceId: string;
  projectId: string | null;
  scopeKey: string;
  source: string;
  access: string;
};

export function resolveDocsCreateScope(
  db: Reader,
  authScope: string,
  projectId: string | null,
  workspaceId?: string,
): DocsCreateScope {
  const row = db.select().from(schema.syncState).where(eq(
    schema.syncState.tableName, `docs_scopes:v2:${authScope.slice("auth:".length)}`,
  )).get();
  let scopes: unknown = null;
  try { scopes = JSON.parse(row?.cursor ?? "null"); } catch { /* fail closed */ }
  const candidates = Array.isArray(scopes) ? scopes.filter((scope) =>
    scope && typeof scope === "object"
    && typeof scope.workspace_id === "string" && scope.workspace_id.trim()
    && typeof scope.source === "string"
    && (scope.project_id ?? null) === projectId
    && (projectId !== null || scope.source === "personal")
    && (workspaceId === undefined || scope.workspace_id === workspaceId),
  ) : [];
  const scope = candidates.length === 1 ? candidates[0] : null;
  if (!scope || scope.read_only !== false
    || (projectId === null ? scope.access !== "owner"
      : !["owner", "write"].includes(scope.access))) {
    throw new Error("保存先Docsの権限を確認できません。接続して同期してから再度お試しください");
  }
  return {
    workspaceId: scope.workspace_id, projectId,
    scopeKey: `${scope.workspace_id}|project:${projectId ?? ""}`,
    source: scope.source, access: scope.access,
  };
}

/** Parent membership is the proof for shared subtrees and freshly created children. */
export function resolveDocsParentCreateScope(
  db: Reader, authScope: string,
  parent: { id: string; workspaceId: string | null; projectId: string | null; source: string | null },
): DocsCreateScope {
  // Re-read inside the caller's final synchronous transaction too. Auth cache
  // clearing removes live nodes before the token changes, but retains memberships.
  const live = db.select().from(schema.knowledgeNodes)
    .where(eq(schema.knowledgeNodes.id, parent.id)).get();
  if (!live || live.workspaceId !== parent.workspaceId || live.projectId !== parent.projectId
    || live.archivedAt || live.readOnly !== false || !["owner", "write"].includes(live.access ?? "")) {
    throw new Error("親Docsの現在の権限を確認できないため保存できません");
  }
  const scopeKey = `${parent.workspaceId}|project:${parent.projectId ?? ""}`;
  const membership = db.select().from(schema.docsScopeMembership).where(and(
    eq(schema.docsScopeMembership.authScope, authScope),
    eq(schema.docsScopeMembership.scopeKey, scopeKey),
    eq(schema.docsScopeMembership.tableName, "knowledge_nodes"),
    eq(schema.docsScopeMembership.entityKey, parent.id),
  )).get();
  if (!parent.workspaceId || !membership || membership.state !== "active"
    || membership.readOnly !== false || !["owner", "write"].includes(membership.access ?? "")) {
    throw new Error("親Docsの現在の権限を確認できないため保存できません");
  }
  return {
    workspaceId: parent.workspaceId, projectId: parent.projectId,
    scopeKey, source: parent.source ?? (parent.projectId ? "project" : "personal"),
    access: membership.access!,
  };
}

function objectPayload(value: string): Record<string, unknown> | null {
  try {
    const payload = JSON.parse(value);
    return payload && typeof payload === "object" && !Array.isArray(payload) ? payload : null;
  } catch { return null; }
}

/**
 * Repair only the known never-sent Personal create defect. A child additionally
 * needs its current writable parent in the same verified owner scope. The exact
 * actor, an explicit NULL project, a single personal owner scope and
 * untouched local ownership must all agree. Keep every operation ID/content;
 * revoked/conflicted/sent/foreign/legacy-ownerless rows remain blocked.
 */
export async function recoverUnscopedPersonalDocsCreates(): Promise<number> {
  const expected = await getTokenSnapshot();
  if (!expected.token) return 0;
  const authScope = getTokenAuthScope(expected.token);
  const count = await runForegroundSqliteWrite(async () => {
    const current = await getTokenSnapshot();
    if (current.token !== expected.token || current.revision !== expected.revision) return 0;
    const db = getDb();
    return db.transaction((tx) => {
      let scope: DocsCreateScope;
      try { scope = resolveDocsCreateScope(tx, authScope, null); } catch { return 0; }
      const creates = tx.select().from(schema.outbox).where(and(
        eq(schema.outbox.authScope, authScope),
        eq(schema.outbox.tableName, "knowledge_nodes"),
        eq(schema.outbox.action, "create"),
        isNull(schema.outbox.docsScopeKey),
        eq(schema.outbox.blockedReason, "docs_scope_ambiguous"),
      )).all();
      let repaired = 0;
      for (const create of creates) {
        const initial = objectPayload(create.payload);
        if (!initial || initial.id !== create.entityId || initial.workspace_id !== null
          || initial.project_id !== null
          || (initial.parent_id !== null && (typeof initial.parent_id !== "string" || !initial.parent_id))) continue;
        const node = tx.select().from(schema.knowledgeNodes)
          .where(eq(schema.knowledgeNodes.id, create.entityId)).get();
        if (!node || !node.dirty || node.workspaceId !== null || node.projectId !== null
          || node.parentId !== initial.parent_id || node.serverUpdatedAt !== null || node.archivedAt
          || node.systemKey || node.source !== "personal" || node.readOnly !== false
          || node.access !== "write") continue;
        if (node.parentId) {
          const parent = tx.select().from(schema.knowledgeNodes)
            .where(eq(schema.knowledgeNodes.id, node.parentId)).get();
          if (!parent || parent.id === node.id || parent.workspaceId !== scope.workspaceId
            || parent.projectId !== null || parent.source !== "personal" || parent.systemKey
            || node.rootPageId !== (parent.rootPageId ?? parent.id)) continue;
          try {
            if (resolveDocsParentCreateScope(tx, authScope, parent).scopeKey !== scope.scopeKey) continue;
          } catch { continue; }
        }
        // Include every owner for this ID. A conflicting owner is never adopted.
        const operations = tx.select().from(schema.outbox).where(and(
          eq(schema.outbox.tableName, "knowledge_nodes"),
          eq(schema.outbox.entityId, create.entityId),
        )).all();
        const payloads = operations.map((op) => objectPayload(op.payload));
        if (operations.filter((op) => op.action === "create").length !== 1
          || operations.some((op, i) => {
            const payload = payloads[i];
            return op.authScope !== authScope || op.docsScopeKey !== null
              || op.blockedReason !== "docs_scope_ambiguous" || op.retryCount !== 0
              || Boolean(op.lastError || op.conflictPayload || op.baseUpdatedAt)
              || !["create", "update"].includes(op.action) || !payload
              || ["workspace_id", "workspaceId", "project_id", "projectId"]
                .some((key) => key in payload && payload[key] !== null)
              || ["parent_id", "parentId"].some((key) => key in payload && payload[key] !== node.parentId)
              || ("id" in payload && payload.id !== create.entityId);
          })) continue;
        const memberships = tx.select().from(schema.docsScopeMembership).where(and(
          eq(schema.docsScopeMembership.tableName, "knowledge_nodes"),
          eq(schema.docsScopeMembership.entityKey, create.entityId),
        )).all();
        if (memberships.length) continue;
        tx.update(schema.knowledgeNodes).set({
          workspaceId: scope.workspaceId, access: scope.access, readOnly: false,
        }).where(eq(schema.knowledgeNodes.id, create.entityId)).run();
        tx.insert(schema.docsScopeMembership).values({
          authScope, scopeKey: scope.scopeKey, scopeId: scope.workspaceId,
          projectId: null, tableName: "knowledge_nodes", entityKey: create.entityId,
          state: "active", access: scope.access, readOnly: false, updatedAt: new Date().toISOString(),
        }).run();
        for (let i = 0; i < operations.length; i++) {
          tx.update(schema.outbox).set({
            payload: JSON.stringify({ ...payloads[i], workspace_id: scope.workspaceId, project_id: null }),
            docsScopeKey: scope.scopeKey, blockedReason: null,
          }).where(eq(schema.outbox.opId, operations[i].opId)).run();
        }
        repaired++;
      }
      return repaired;
    });
  });
  if (count) notifyMutationQueued(authScope);
  return count;
}

import type { DocsSqliteAsyncTransaction } from "../db/docs-sync-async";
import type { DocsSyncPromotionOptions } from "./docs";

type SyncStateRow = {
  table_name: string;
  last_pulled_at: string | null;
  cursor: string | null;
};

function scopeSignature(scopes: NonNullable<DocsSyncPromotionOptions["scopes"]>): string {
  return JSON.stringify(scopes.map((scope) => [
    scope.workspace_id, scope.project_id ?? null, scope.source ?? null,
    scope.access ?? null, scope.read_only ?? null,
  ]).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right))));
}

/** Complete a validated run in the caller's transaction, including its receipt. */
export async function finalizeDocsSyncRunTx(
  tx: DocsSqliteAsyncTransaction,
  options: DocsSyncPromotionOptions,
): Promise<void> {
  const finalize = options.finalize;
  if (!finalize) return;
  const saveSyncState = async (
    tableName: string,
    values: { lastPulledAt?: string | null; cursor?: string | null },
  ): Promise<void> => {
    const updates: string[] = [];
    if (values.lastPulledAt !== undefined) updates.push("last_pulled_at = excluded.last_pulled_at");
    if (values.cursor !== undefined) updates.push("cursor = excluded.cursor");
    await tx.runAsync(
      `INSERT INTO sync_state(table_name, last_pulled_at, last_pushed_at, cursor)
         VALUES (?, ?, ?, ?)
       ON CONFLICT(table_name) DO UPDATE SET ${updates.join(", ")}`,
      tableName, values.lastPulledAt ?? null, null, values.cursor ?? null,
    );
  };
  for (const [table, digest] of Object.entries(finalize.digestByTable)) {
    const key = finalize.digestKeys[table];
    if (key && digest) await saveSyncState(key, { cursor: digest });
  }
  await saveSyncState(finalize.scopeDigestKey, { cursor: finalize.scopeDigest });
  if (finalize.scopeRevisionKey && finalize.scopeRevision) {
    await saveSyncState(finalize.scopeRevisionKey, { cursor: finalize.scopeRevision });
  }
  await saveSyncState(finalize.lastPulledKey, { lastPulledAt: finalize.serverTime });
  if (finalize.scopesKey && options.scopes) {
    await saveSyncState(finalize.scopesKey, { cursor: JSON.stringify(options.scopes) });
  }
  if (finalize.workspaceKey && finalize.workspaceId) {
    await saveSyncState(finalize.workspaceKey, { cursor: finalize.workspaceId });
  }
  await tx.runAsync(
    `UPDATE docs_sync_runs SET state = 'completed', updated_at = ?
       WHERE run_id = ? AND auth_scope = ?`,
    finalize.serverTime, options.runId, options.authScope,
  );
  await tx.runAsync(
    "DELETE FROM docs_sync_staging WHERE run_id = ? AND auth_scope = ?",
    options.runId, options.authScope,
  );
}

/**
 * An empty wire response alone is not proof of an unchanged projection.
 * Verify the complete receipt written by an earlier atomic promotion, the
 * current ACL revision/scope set, and the actual staged rows inside this same
 * transaction. Force repair, partial/resumed rows, tombstones and even an
 * explicitly empty authoritative ID set must take the ordinary reconcile path.
 * A bounded existence check retains the normal protection path for local
 * edits. No membership collection or live payload is materialized or changed.
 */
export async function tryFinalizeUnchangedDocsRunTx(
  tx: DocsSqliteAsyncTransaction,
  options: DocsSyncPromotionOptions,
  tableNames: readonly string[],
): Promise<boolean> {
  const finalize = options.finalize;
  if (options.force || options.staged || !finalize
    || !finalize.scopeDigest || !finalize.scopeRevision || !finalize.scopeRevisionKey
    || Object.values(options.authoritative).some((value) => value.ids != null)
    || tableNames.some((table) => !finalize.digestByTable[table] || !finalize.digestKeys[table])) return false;
  if (options.scopeSet && scopeSignature(options.scopeSet.previousScopes)
    !== scopeSignature(options.scopeSet.newScopes)) return false;

  const expected = new Map<string, string>([
    [finalize.scopeDigestKey, finalize.scopeDigest],
    [finalize.scopeRevisionKey, finalize.scopeRevision],
    ...tableNames.map((table) => [finalize.digestKeys[table], finalize.digestByTable[table]] as [string, string]),
  ]);
  if (finalize.workspaceKey && finalize.workspaceId) expected.set(finalize.workspaceKey, finalize.workspaceId);
  const keys = [...expected.keys(), finalize.lastPulledKey];
  if (finalize.scopesKey) keys.push(finalize.scopesKey);
  const rows = await tx.getAllAsync<SyncStateRow>(
    `SELECT table_name, last_pulled_at, cursor FROM sync_state
       WHERE table_name IN (${keys.map(() => "?").join(", ")})`,
    ...keys,
  );
  const saved = new Map(rows.map((row) => [row.table_name, row]));
  if (!saved.get(finalize.lastPulledKey)?.last_pulled_at
    || [...expected].some(([key, value]) => saved.get(key)?.cursor !== value)) return false;
  if (finalize.scopesKey) {
    try {
      const prior = JSON.parse(saved.get(finalize.scopesKey)?.cursor ?? "null");
      if (!Array.isArray(prior) || !options.scopes
        || scopeSignature(prior) !== scopeSignature(options.scopes)) return false;
    } catch { return false; }
  }
  const run = await tx.getFirstAsync<{ state: string; pending_json: string; scope_key: string }>(
    "SELECT state, pending_json, scope_key FROM docs_sync_runs WHERE run_id = ? AND auth_scope = ?",
    options.runId, options.authScope,
  );
  if (!run || run.state !== "ready" || run.scope_key !== options.scopeKey) return false;
  try {
    const pending = JSON.parse(run.pending_json);
    if (!Array.isArray(pending) || pending.length) return false;
  } catch { return false; }
  const staged = await tx.getFirstAsync<{ present: number }>(
    "SELECT 1 AS present FROM docs_sync_staging WHERE run_id = ? AND auth_scope = ? LIMIT 1",
    options.runId, options.authScope,
  );
  if (staged) return false;
  const localEdits = await tx.getFirstAsync<{ present: number }>(
    `SELECT 1 AS present WHERE
       EXISTS (SELECT 1 FROM outbox WHERE auth_scope = ? AND docs_scope_key = ?)
       OR EXISTS (SELECT 1 FROM knowledge_nodes WHERE dirty = 1)
       OR EXISTS (SELECT 1 FROM knowledge_supertags WHERE dirty = 1)
       OR EXISTS (SELECT 1 FROM knowledge_node_supertags WHERE dirty = 1)
       OR EXISTS (SELECT 1 FROM knowledge_field_values WHERE dirty = 1)`,
    options.authScope, options.scopeKey,
  );
  if (localEdits) return false;
  await finalizeDocsSyncRunTx(tx, options);
  return true;
}

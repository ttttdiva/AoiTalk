import { withDocsPreparedWrites } from "../db/docs-prepared-writes";
import { withForegroundYieldingDocsExclusiveTransaction } from "../db/docs-sync-async";
import { finalizeDocsSyncRunTx } from "./docs-sync-finalize";
import type { DocsSyncPromotionOptions } from "./docs";

export type DocsFastNoopFinalizeOptions = DocsSyncPromotionOptions & {
  unchangedFastDigestReceipt: true;
};

function scopeSignature(scopes: NonNullable<DocsSyncPromotionOptions["scopes"]>): string {
  return JSON.stringify(scopes.map((scope) => [
    scope.workspace_id,
    scope.project_id ?? null,
    scope.source ?? null,
    scope.access ?? null,
    scope.read_only ?? null,
  ]).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right))));
}

/**
 * Finalize an empty Docs run after the client and server fast row-set receipts
 * match. This writes only sync state and run metadata; it never reconciles live
 * rows or membership, so pending local edits/outbox remain untouched.
 */
export async function finalizeDocsFastNoopRun(
  options: DocsFastNoopFinalizeOptions,
): Promise<boolean> {
  const finalize = options.finalize;
  const scopes = options.scopes;
  if (!finalize || !scopes?.length) return false;
  const tables = Object.keys(finalize.digestKeys);
  if (
    !options.unchangedFastDigestReceipt
    || !options.validatedIncremental
    || options.force
    || options.staged
    || !finalize.scopeDigest
    || !finalize.scopeDigestKey
    || !finalize.scopeRevision
    || !finalize.scopeRevisionKey
    || !finalize.scopesKey
    || !finalize.workspaceKey
    || !finalize.workspaceId
    || !finalize.lastPulledKey
    || tables.length !== 8
    || tables.some((table) => !finalize.digestByTable[table])
    || (options.scopeSet && scopeSignature(options.scopeSet.previousScopes)
      !== scopeSignature(options.scopeSet.newScopes))
  ) {
    return false;
  }

  return withForegroundYieldingDocsExclusiveTransaction(
    (rawTx) => withDocsPreparedWrites(rawTx, async (tx) => {
      options.assertActive?.();
      const expected = new Map<string, string>();
      expected.set(finalize.scopeDigestKey, finalize.scopeDigest);
      expected.set(finalize.scopeRevisionKey!, finalize.scopeRevision!);
      expected.set(finalize.workspaceKey!, finalize.workspaceId!);
      for (const table of tables) {
        const key = finalize.digestKeys[table];
        const value = finalize.digestByTable[table];
        if (!key || !value) return false;
        expected.set(key, value);
      }
      const keys = [...new Set([
        ...expected.keys(),
        finalize.lastPulledKey,
        finalize.scopesKey!,
      ])];
      const rows = await tx.getAllAsync<{
        table_name: string;
        cursor: string | null;
        last_pulled_at: string | null;
      }>(
        `SELECT table_name, cursor, last_pulled_at FROM sync_state
          WHERE table_name IN (${keys.map(() => "?").join(", ")})`,
        ...keys,
      );
      const saved = new Map(rows.map((row) => [row.table_name, row]));
      if (
        keys.some((key) => !saved.has(key))
        || !saved.get(finalize.lastPulledKey)?.last_pulled_at
        || [...expected].some(([key, value]) => saved.get(key)?.cursor !== value)
      ) {
        return false;
      }
      try {
        const previousScopes = JSON.parse(saved.get(finalize.scopesKey!)?.cursor ?? "null");
        if (!Array.isArray(previousScopes)
          || scopeSignature(previousScopes) !== scopeSignature(scopes)) {
          return false;
        }
      } catch {
        return false;
      }

      const run = await tx.getFirstAsync<{
        state: string;
        pending_json: string;
        scope_key: string;
      }>(
        "SELECT state, pending_json, scope_key FROM docs_sync_runs WHERE run_id = ? AND auth_scope = ?",
        options.runId,
        options.authScope,
      );
      if (!run || run.state !== "ready" || run.scope_key !== options.scopeKey) return false;
      try {
        const pending = JSON.parse(run.pending_json);
        if (!Array.isArray(pending) || pending.length > 0) return false;
      } catch {
        return false;
      }
      const staged = await tx.getFirstAsync<{ present: number }>(
        "SELECT 1 AS present FROM docs_sync_staging WHERE run_id = ? AND auth_scope = ? LIMIT 1",
        options.runId,
        options.authScope,
      );
      if (staged) return false;

      options.assertActive?.();
      await finalizeDocsSyncRunTx(tx, options);
      return true;
    }),
    options.assertActive,
    options.onSqliteProgress,
  );
}

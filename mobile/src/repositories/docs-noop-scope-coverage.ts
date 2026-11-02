import { getSqlite } from "../db/client";
import { docsSqliteAsyncAvailable } from "../db/docs-sync-async";

export type DocsNoopCoverageScope = {
  workspace_id: string;
  project_id?: string | null;
  source?: string | null;
};

export type DocsRootProjectScopeCoverage = {
  coveredScopeKeys: Set<string>;
  fastReceiptScopeKeys: Set<string>;
};

const DOCS_SYNC_TABLES = [
  "knowledge_nodes",
  "knowledge_supertags",
  "knowledge_node_supertags",
  "knowledge_supertag_fields",
  "knowledge_fields",
  "knowledge_field_values",
  "knowledge_node_placements",
  "knowledge_edges",
] as const;

/**
 * Find previously completed project scopes covered by a validated unchanged
 * root pull. The marker is written only after a scope promotion commits; an
 * incomplete run suppresses the fast path. Candidate lookup is one SQLite
 * set query regardless of the number of projects.
 */
export async function getRootCoveredDocsProjectScopeKeys(
  authScope: string,
  scopes: readonly DocsNoopCoverageScope[],
  serverProjectDigests: Record<string, Record<string, string>>,
  serverProjectScopeRevisions: Record<string, string>,
  serverProjectFastDigests: Record<string, Record<string, string>> = {},
): Promise<Set<string>> {
  return (await getRootDocsProjectScopeCoverage(
    authScope,
    scopes,
    serverProjectDigests,
    serverProjectScopeRevisions,
    serverProjectFastDigests,
  )).coveredScopeKeys;
}

/** Return the matched receipt kind so callers need not rewrite a valid fast cache. */
export async function getRootDocsProjectScopeCoverage(
  authScope: string,
  scopes: readonly DocsNoopCoverageScope[],
  serverProjectDigests: Record<string, Record<string, string>>,
  serverProjectScopeRevisions: Record<string, string>,
  serverProjectFastDigests: Record<string, Record<string, string>> = {},
): Promise<DocsRootProjectScopeCoverage> {
  const empty: DocsRootProjectScopeCoverage = {
    coveredScopeKeys: new Set(),
    fastReceiptScopeKeys: new Set(),
  };
  if (!docsSqliteAsyncAvailable()) return empty;
  const userId = authScope.startsWith("auth:") ? authScope.slice("auth:".length) : "";
  if (!userId) return empty;
  const candidates = scopes
    .filter((scope) => scope.source === "project"
      && typeof scope.project_id === "string"
      && scope.project_id.length > 0
      && typeof serverProjectScopeRevisions[scope.project_id!] === "string"
      && (
        DOCS_SYNC_TABLES.every((table) =>
          typeof serverProjectFastDigests[scope.project_id!]?.[table] === "string",
        )
        || DOCS_SYNC_TABLES.every((table) =>
          typeof serverProjectDigests[scope.project_id!]?.[table] === "string",
        )
      ))
    .map((scope) => ({
      scopeKey: `${scope.workspace_id}|project:${scope.project_id}`,
      workspaceKey: `docs_workspace:v2:${userId}:scope:${scope.workspace_id}:project:${scope.project_id}`,
      revisionKey: `docs_scope_revision:v2:${userId}:scope:${scope.workspace_id}:project:${scope.project_id}`,
      serverRevision: serverProjectScopeRevisions[scope.project_id!],
      digestPrefix: `docs_digest:v2:${userId}:scope:${scope.workspace_id}:project:${scope.project_id}:`,
      fastDigestPrefix: `docs_fast_digest:v1:${userId}:scope:${scope.workspace_id}:project:${scope.project_id}:`,
      workspaceId: scope.workspace_id,
      serverDigests: serverProjectDigests[scope.project_id!] ?? {},
      serverFastDigests: serverProjectFastDigests[scope.project_id!] ?? {},
    }));
  if (!candidates.length) return empty;

  let rows: Array<{ scope_key: string; fast_receipt_match: number | boolean }>;
  try {
    rows = await getSqlite().getAllAsync<{
      scope_key: string;
      fast_receipt_match: number | boolean;
    }>(
    `WITH candidates(scope_key, workspace_key, revision_key, server_revision, digest_prefix, fast_digest_prefix, workspace_id, server_digests, server_fast_digests) AS MATERIALIZED (
       SELECT CAST(json_extract(value, '$.scopeKey') AS TEXT),
              CAST(json_extract(value, '$.workspaceKey') AS TEXT),
              CAST(json_extract(value, '$.revisionKey') AS TEXT),
              CAST(json_extract(value, '$.serverRevision') AS TEXT),
              CAST(json_extract(value, '$.digestPrefix') AS TEXT),
              CAST(json_extract(value, '$.fastDigestPrefix') AS TEXT),
              CAST(json_extract(value, '$.workspaceId') AS TEXT),
              CAST(json_extract(value, '$.serverDigests') AS TEXT),
              CAST(json_extract(value, '$.serverFastDigests') AS TEXT)
         FROM json_each(?)
     ), coverage_checks AS MATERIALIZED (
       SELECT candidates.scope_key,
              ((SELECT COUNT(*) FROM json_each(candidates.server_fast_digests)) = ?
                AND NOT EXISTS (
                  SELECT 1
                    FROM json_each(candidates.server_fast_digests) AS remote_fast_digest
                    LEFT JOIN sync_state AS local_fast_digest
                      ON local_fast_digest.table_name = candidates.fast_digest_prefix || remote_fast_digest.key
                   WHERE local_fast_digest.cursor IS NULL
                      OR local_fast_digest.cursor <> CAST(remote_fast_digest.value AS TEXT)
                )) AS fast_receipt_match,
              ((SELECT COUNT(*) FROM json_each(candidates.server_digests)) = ?
                AND NOT EXISTS (
                  SELECT 1
                    FROM json_each(candidates.server_digests) AS remote_digest
                    LEFT JOIN sync_state AS local_digest
                      ON local_digest.table_name = candidates.digest_prefix || remote_digest.key
                   WHERE local_digest.cursor IS NULL
                      OR local_digest.cursor <> CAST(remote_digest.value AS TEXT)
                )) AS legacy_receipt_match
         FROM candidates
         JOIN sync_state AS workspace
           ON workspace.table_name = candidates.workspace_key
          AND workspace.cursor = candidates.workspace_id
         JOIN sync_state AS revision
           ON revision.table_name = candidates.revision_key
          AND revision.cursor = candidates.server_revision
        WHERE NOT EXISTS (
          SELECT 1 FROM docs_sync_runs AS run
           WHERE run.auth_scope = ?
             AND run.scope_key = candidates.scope_key
             AND run.state <> 'completed'
        )
     )
     SELECT scope_key, fast_receipt_match
       FROM coverage_checks
      WHERE fast_receipt_match OR legacy_receipt_match
      ORDER BY scope_key`,
      JSON.stringify(candidates),
      DOCS_SYNC_TABLES.length,
      DOCS_SYNC_TABLES.length,
      authScope,
    );
  } catch {
    // Older caches may not yet have the staged Docs tables. Fail closed and
    // keep using the ordinary per-scope pull path.
    return empty;
  }
  return {
    coveredScopeKeys: new Set(rows.map((row) => row.scope_key)),
    fastReceiptScopeKeys: new Set(
      rows.filter((row) => Boolean(row.fast_receipt_match)).map((row) => row.scope_key),
    ),
  };
}

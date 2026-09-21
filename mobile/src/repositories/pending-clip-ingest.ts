/**
 * Durable ClipIngest operation journal.
 *
 * ``operation_key = NULL`` is the immutable legacy boundary. Legacy rows are
 * never automatically replayed or attributed to a newly signed-in account;
 * new operations persist their request, scope context, delivery and terminal
 * receipt before any remote or local side effect.
 */

import { and, asc, eq, inArray, isNull, or } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import {
  ClipIngestMalformedAckError,
  docsApi,
  normalizeClipIngestResult,
  type ClipIngestJobRequest,
  type ClipIngestJobResponse,
  type ClipIngestRequestContext,
  type ClipIngestResult,
} from "../lib/docs-api";
import {
  getConfiguredApiServerFingerprint,
  isApiConnectionError,
  isApiHttpError,
  isApiServerChangedError,
  isApiTimeoutError,
} from "../lib/api-client";
import { getToken, getTokenAuthScope } from "../lib/auth";
import { randomId } from "./outbox";

/** Compatibility retry limit for pre-journal rows. */
export const PENDING_CLIP_INGEST_MAX_RETRY = 5;
/** Explicit scope used by operations created while signed out. */
export const ANONYMOUS_AUTH_SCOPE = "anonymous";

export type ClipIngestDelivery = "remote" | "local";

export type ClipIngestOperationStatus =
  | "created"
  | "remote_ready"
  | "remote_pending"
  | "remote_unknown"
  | "remote_succeeded"
  | "remote_failed"
  | "local_pending"
  | "local_succeeded"
  | "local_failed";

export interface ClipIngestOperationIdentity {
  id: string;
  operationKey: string;
  authScope: string;
}

export interface PendingClipIngestRow {
  id: string;
  source: string;
  status: string;
  authScope: string | null;
  /** Added by v0.1.144. NULL is recovery-only/server-unknown. */
  serverFingerprint?: string | null;
  /** Added by v0.1.143; undefined is tolerated by legacy test/callers. */
  operationKey?: string | null;
  requestJson?: string | null;
  contextJson?: string | null;
  delivery?: string | null;
  remoteJobId?: string | null;
  ackJson?: string | null;
  resultJson?: string | null;
  errorJson?: string | null;
  retryCount: number;
  lastError: string | null;
  createdAt: string | null;
  updatedAt?: string | null;
  terminalAt?: string | null;
}

/** Current auth scope, following the same token-derived convention as sync. */
export async function getCurrentClipIngestAuthScope(): Promise<string> {
  return getTokenAuthScope(await getToken());
}

async function getCurrentClipIngestServerFingerprint(): Promise<string> {
  // Older rolling bundles/tests may not expose the v0.1.144 API helper.  An
  // empty value keeps those compatibility paths functional; durable rows on
  // such a bundle remain recovery-only rather than being sent unpinned.
  if (typeof getConfiguredApiServerFingerprint !== "function") return "";
  try {
    return await getConfiguredApiServerFingerprint();
  } catch {
    return "";
  }
}

function authScopeFilter(currentScope: string) {
  return eq(schema.pendingClipIngests.authScope, currentScope);
}

function errorText(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  const jsonStart = raw.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(raw.slice(jsonStart)) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      // JSONでなければ元のメッセージを使う。
    }
  }
  return raw.slice(0, 500) || "クリップ取り込みの再送に失敗しました";
}

function isConnectionResetError(error: unknown): boolean {
  return error instanceof Error
    && /connection reset|reset by peer|econnreset/i.test(error.message);
}

/** Pre-journal compatibility: classify permanent client errors. */
function isPermanentIngestError(error: unknown): error is { status: number } {
  if (!isApiHttpError(error)) return false;
  const status = (error as { status: number }).status;
  if ([401, 408, 409, 429].includes(status)) return false;
  return status >= 400 && status < 500;
}

type PendingClipIngestDbRow = typeof schema.pendingClipIngests.$inferSelect;

function toRow(row: PendingClipIngestDbRow | Record<string, unknown>): PendingClipIngestRow {
  const value = row as Record<string, unknown>;
  return {
    id: String(value.id ?? ""),
    source: String(value.source ?? ""),
    status: String(value.status ?? "queued"),
    authScope: typeof value.authScope === "string" ? value.authScope : null,
    serverFingerprint:
      typeof value.serverFingerprint === "string"
        ? value.serverFingerprint
        : null,
    operationKey: typeof value.operationKey === "string" ? value.operationKey : null,
    requestJson: typeof value.requestJson === "string" ? value.requestJson : null,
    contextJson: typeof value.contextJson === "string" ? value.contextJson : null,
    delivery: typeof value.delivery === "string" ? value.delivery : null,
    remoteJobId: typeof value.remoteJobId === "string" ? value.remoteJobId : null,
    ackJson: typeof value.ackJson === "string" ? value.ackJson : null,
    resultJson: typeof value.resultJson === "string" ? value.resultJson : null,
    errorJson: typeof value.errorJson === "string" ? value.errorJson : null,
    retryCount: typeof value.retryCount === "number" ? value.retryCount : 0,
    lastError: typeof value.lastError === "string" ? value.lastError : null,
    createdAt: typeof value.createdAt === "string" ? value.createdAt : null,
    updatedAt: typeof value.updatedAt === "string" ? value.updatedAt : null,
    terminalAt: typeof value.terminalAt === "string" ? value.terminalAt : null,
  };
}

function parseJsonRecord(value: string | null): Record<string, unknown> | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function requestForOperation(row: PendingClipIngestRow): ClipIngestJobRequest {
  const parsed = parseJsonRecord(row.requestJson ?? null);
  if (parsed && typeof parsed.source === "string") {
    return parsed as unknown as ClipIngestJobRequest;
  }
  return { source: row.source };
}

function contextForOperation(row: PendingClipIngestRow): ClipIngestRequestContext {
  const parsed = parseJsonRecord(row.contextJson ?? null);
  return {
    session_id: typeof parsed?.session_id === "string" ? parsed.session_id : null,
    project_id: typeof parsed?.project_id === "string" ? parsed.project_id : null,
  };
}

export function clipIngestResultFromOperation(row: PendingClipIngestRow): ClipIngestResult | null {
  if (!row.resultJson) return null;
  try {
    return normalizeClipIngestResult(JSON.parse(row.resultJson));
  } catch {
    return null;
  }
}

export async function createClipIngestOperation(input: {
  request: ClipIngestJobRequest;
  context?: ClipIngestRequestContext;
}): Promise<PendingClipIngestRow> {
  const id = randomId();
  const operationKey = randomId();
  const authScope = await getCurrentClipIngestAuthScope();
  const serverFingerprint = await getCurrentClipIngestServerFingerprint() || null;
  const now = new Date().toISOString();
  const context: ClipIngestRequestContext = {
    session_id: input.context?.session_id ?? null,
    project_id: input.context?.project_id ?? null,
  };
  const values = {
    id,
    source: input.request.source,
    operationKey,
    requestJson: JSON.stringify(input.request),
    contextJson: JSON.stringify(context),
    delivery: null,
    status: "created",
    authScope,
    serverFingerprint,
    remoteJobId: null,
    ackJson: null,
    resultJson: null,
    errorJson: null,
    retryCount: 0,
    lastError: null,
    createdAt: now,
    updatedAt: now,
    terminalAt: null,
  };
  // Journal creation is itself a foreground write unit.  The orchestrator
  // never starts a network/LLM side effect until this promise commits.
  await runForegroundSqliteWrite(() =>
    getDb().insert(schema.pendingClipIngests).values(values),
  );
  return toRow(values);
}

/** Compatibility export for callers that only need an id. */
export async function enqueuePendingClipIngest(source: string): Promise<string> {
  return (await createClipIngestOperation({ request: { source } })).id;
}

async function queryRows(
  where: unknown,
): Promise<PendingClipIngestRow[]> {
  const query = getDb().select().from(schema.pendingClipIngests).where(where as never);
  // Expo/Drizzle queries are awaitable directly, while older in-memory test
  // doubles expose the historical ``where(...).orderBy(...)`` shape.
  const rows = typeof (query as { orderBy?: unknown }).orderBy === "function"
    ? await (query as { orderBy: (order: unknown) => Promise<unknown[]> }).orderBy(
        asc(schema.pendingClipIngests.createdAt),
      )
    : await query;
  return (rows as Array<PendingClipIngestDbRow | Record<string, unknown>>).map(toRow);
}

async function getOperationByIdForScope(id: string, authScope: string): Promise<PendingClipIngestRow | null> {
  const rows = await queryRows(
    and(
      eq(schema.pendingClipIngests.id, id),
      authScopeFilter(authScope),
      // The JS filter below remains the source of truth for rolling upgrades
      // whose Drizzle test doubles do not yet expose operationKey.
    ),
  );
  const operationKeyColumn = (schema.pendingClipIngests as { operationKey?: unknown }).operationKey;
  const row = rows.find(
    (candidate) => operationKeyColumn == null || candidate.operationKey != null,
  );
  return row ?? null;
}

export async function getClipIngestOperation(id: string): Promise<PendingClipIngestRow | null> {
  return getOperationByIdForScope(id, await getCurrentClipIngestAuthScope());
}

export async function getClipIngestOperationByKey(
  operationKey: string,
  requestedAuthScope?: string,
): Promise<PendingClipIngestRow | null> {
  const authScope = requestedAuthScope ?? await getCurrentClipIngestAuthScope();
  const rows = await queryRows(
    and(
      eq(schema.pendingClipIngests.operationKey, operationKey),
      authScopeFilter(authScope),
    ),
  );
  return rows.find((row) => row.operationKey === operationKey) ?? null;
}

const ACTIVE_OPERATION_STATUSES = [
  "created",
  "remote_ready",
  "remote_pending",
  "remote_unknown",
  "local_pending",
] as const;
const FAILED_OPERATION_STATUSES = ["remote_failed", "local_failed"] as const;

async function listClipIngestOperationsForScope(
  authScope: string,
  statuses: readonly string[],
  serverFingerprint?: string,
): Promise<PendingClipIngestRow[]> {
  const operationKeyColumn = (schema.pendingClipIngests as { operationKey?: unknown }).operationKey;
  const serverFingerprintColumn = (
    schema.pendingClipIngests as { serverFingerprint?: unknown }
  ).serverFingerprint;
  const queryStatuses = operationKeyColumn == null
    ? [...statuses, "queued"]
    : [...statuses];
  const scopeCondition =
    serverFingerprint !== undefined && serverFingerprintColumn != null
      ? and(
          authScopeFilter(authScope),
          eq(schema.pendingClipIngests.serverFingerprint, serverFingerprint),
        )
      : authScopeFilter(authScope);
  const rows = await queryRows(
    and(
      inArray(schema.pendingClipIngests.status, queryStatuses),
      scopeCondition,
    ),
  );
  return rows
    .filter((row) =>
      operationKeyColumn == null
      || row.operationKey != null,
    )
    .sort((a, b) => String(a.createdAt ?? "").localeCompare(String(b.createdAt ?? "")));
}

export async function listPendingClipIngests(): Promise<PendingClipIngestRow[]> {
  const serverFingerprint =
    (await getCurrentClipIngestServerFingerprint()) || undefined;
  return listClipIngestOperationsForScope(
    await getCurrentClipIngestAuthScope(),
    ACTIVE_OPERATION_STATUSES,
    serverFingerprint,
  );
}

export async function listFailedClipIngests(): Promise<PendingClipIngestRow[]> {
  const serverFingerprint =
    (await getCurrentClipIngestServerFingerprint()) || undefined;
  return listClipIngestOperationsForScope(
    await getCurrentClipIngestAuthScope(),
    FAILED_OPERATION_STATUSES,
    serverFingerprint,
  );
}

const RECOVERABLE_OPERATION_STATUSES = [
  ...ACTIVE_OPERATION_STATUSES,
  "remote_succeeded",
] as const;

/**
 * User-visible recovery projection. Unlike automatic flush, this includes
 * current-account operations pinned to another/unknown server so the UI can
 * show their input and offer explicit restoration instead of sending them.
 */
export async function listRecoverableClipIngestOperations(): Promise<
  PendingClipIngestRow[]
> {
  const rows = await listClipIngestOperationsForScope(
    await getCurrentClipIngestAuthScope(),
    RECOVERABLE_OPERATION_STATUSES,
  );
  return rows.sort(
    (a, b) => String(b.createdAt ?? "").localeCompare(String(a.createdAt ?? "")),
  );
}

export async function countPendingClipIngests(): Promise<number> {
  return (await listPendingClipIngests()).length;
}

/** Recovery-only projection for NULL/anonymous legacy rows. */
export async function listLegacyPendingClipIngests(): Promise<PendingClipIngestRow[]> {
  const currentScope = await getCurrentClipIngestAuthScope();
  const rows = await queryRows(
    and(
      isNull(schema.pendingClipIngests.operationKey),
      or(
        eq(schema.pendingClipIngests.authScope, currentScope),
        eq(schema.pendingClipIngests.authScope, ANONYMOUS_AUTH_SCOPE),
        isNull(schema.pendingClipIngests.authScope),
      ),
    ),
  );
  return rows.sort((a, b) => String(a.createdAt ?? "").localeCompare(String(b.createdAt ?? "")));
}

/** Explicit user-confirmed adoption of a legacy row. */
export async function claimLegacyPendingClipIngest(id: string): Promise<PendingClipIngestRow> {
  const db = getDb();
  const currentScope = await getCurrentClipIngestAuthScope();
  const rows = await queryRows(
    and(
      eq(schema.pendingClipIngests.id, id),
      isNull(schema.pendingClipIngests.operationKey),
    ),
  );
  const legacy = rows[0];
  if (!legacy) throw new Error("Legacy ClipIngest row not found");
  if (
    legacy.authScope
    && legacy.authScope !== ANONYMOUS_AUTH_SCOPE
    && legacy.authScope !== currentScope
  ) {
    throw new Error("Legacy ClipIngest row belongs to another account");
  }
  const operationKey = randomId();
  const serverFingerprint = await getCurrentClipIngestServerFingerprint() || null;
  const now = new Date().toISOString();
  await runForegroundSqliteWrite(() =>
    db
      .update(schema.pendingClipIngests)
      .set({
        operationKey,
        authScope: currentScope,
        serverFingerprint,
        requestJson: JSON.stringify({ source: legacy.source }),
        contextJson: JSON.stringify({ session_id: null, project_id: null }),
        delivery: null,
        status: "created",
        remoteJobId: null,
        ackJson: null,
        resultJson: null,
        errorJson: null,
        retryCount: 0,
        lastError: null,
        updatedAt: now,
        terminalAt: null,
      })
      .where(
        and(
          eq(schema.pendingClipIngests.id, id),
          isNull(schema.pendingClipIngests.operationKey),
          or(
            eq(schema.pendingClipIngests.authScope, currentScope),
            eq(schema.pendingClipIngests.authScope, ANONYMOUS_AUTH_SCOPE),
            isNull(schema.pendingClipIngests.authScope),
          ),
        ),
      ),
  );
  const claimed = await getOperationByIdForScope(id, currentScope);
  if (!claimed || claimed.operationKey !== operationKey) {
    throw new Error("Legacy ClipIngest recovery was not committed");
  }
  return claimed;
}

type OperationPatch = Partial<{
  status: string;
  delivery: string | null;
  remoteJobId: string | null;
  ackJson: string | null;
  resultJson: string | null;
  errorJson: string | null;
  retryCount: number;
  lastError: string | null;
  terminalAt: string | null;
}>;

async function patchOperation(
  row: PendingClipIngestRow,
  patch: OperationPatch,
  expectedStatuses?: readonly string[],
): Promise<void> {
  if (!row.operationKey || !row.authScope) {
    throw new Error("ClipIngest operation identity is incomplete");
  }
  let predicate = and(
    eq(schema.pendingClipIngests.id, row.id),
    eq(schema.pendingClipIngests.operationKey, row.operationKey),
    eq(schema.pendingClipIngests.authScope, row.authScope),
  );
  if (expectedStatuses?.length) {
    predicate = and(
      predicate,
      inArray(schema.pendingClipIngests.status, [...expectedStatuses]),
    );
  }
  await runForegroundSqliteWrite(() =>
    getDb()
      .update(schema.pendingClipIngests)
      .set({ ...patch, updatedAt: new Date().toISOString() })
      .where(predicate),
  );
}

export async function prepareClipIngestDelivery(
  row: PendingClipIngestRow,
  delivery: ClipIngestDelivery,
): Promise<PendingClipIngestRow> {
  if (!row.operationKey || !row.authScope) {
    throw new Error("ClipIngest operation identity is incomplete");
  }
  if (row.delivery && row.delivery !== delivery) {
    throw new Error(`ClipIngest operation already selected ${row.delivery} delivery`);
  }
  if (row.delivery === delivery) return row;
  if (row.status !== "created") {
    throw new Error(`ClipIngest operation is not dispatchable: ${row.status}`);
  }
  const status = delivery === "local" ? "local_pending" : "remote_ready";
  await patchOperation(row, { delivery, status }, ["created"]);
  return { ...row, delivery, status };
}

export async function getSucceededLocalClipIngestResult(
  operation: ClipIngestOperationIdentity,
): Promise<ClipIngestResult | null> {
  const row = await getClipIngestOperationByKey(operation.operationKey, operation.authScope);
  if (
    !row
    || row.id !== operation.id
    || row.delivery !== "local"
    || row.status !== "local_succeeded"
  ) return null;
  return clipIngestResultFromOperation(row);
}

export async function completeLocalClipIngestOperation(
  operation: ClipIngestOperationIdentity,
  result: ClipIngestResult,
): Promise<ClipIngestResult> {
  const existing = await getSucceededLocalClipIngestResult(operation);
  if (existing) return existing;
  const row = await getOperationByIdForScope(operation.id, operation.authScope);
  if (!row || row.operationKey !== operation.operationKey) {
    throw new Error("ClipIngest operation no longer exists");
  }
  await patchOperation(
    row,
    {
      status: "local_succeeded",
      resultJson: JSON.stringify(result),
      errorJson: null,
      lastError: null,
      terminalAt: new Date().toISOString(),
    },
    ["local_pending"],
  );
  const durable = await getSucceededLocalClipIngestResult(operation);
  if (!durable) throw new Error("Local ClipIngest completion was not committed");
  return durable;
}

export async function deferLocalClipIngestForRemote(
  row: PendingClipIngestRow,
  error: unknown,
): Promise<void> {
  const message = errorText(error);
  await patchOperation(
    row,
    {
      delivery: null,
      status: "created",
      errorJson: JSON.stringify({ kind: "local_unavailable", message }),
      lastError: message,
      terminalAt: null,
    },
    ["local_pending"],
  );
}

export async function markLocalClipIngestFailed(
  row: PendingClipIngestRow,
  error: unknown,
): Promise<void> {
  const message = errorText(error);
  await patchOperation(
    row,
    {
      status: "local_failed",
      errorJson: JSON.stringify({ kind: "local_error", message }),
      lastError: message,
      terminalAt: new Date().toISOString(),
    },
    ["local_pending"],
  );
}

/** Legacy-only mutations retained for old UI/recovery callers. */
export async function removePendingClipIngest(id: string): Promise<void> {
  const authScope = await getCurrentClipIngestAuthScope();
  await getDb().delete(schema.pendingClipIngests).where(
    and(
      eq(schema.pendingClipIngests.id, id),
      authScopeFilter(authScope),
      isNull(schema.pendingClipIngests.operationKey),
    ),
  );
}

export async function markPendingClipIngestRetry(
  id: string,
  retryCount: number,
  error: string,
): Promise<void> {
  const authScope = await getCurrentClipIngestAuthScope();
  const next = retryCount + 1;
  await getDb().update(schema.pendingClipIngests).set({
    retryCount: next,
    lastError: error,
    status: next >= PENDING_CLIP_INGEST_MAX_RETRY ? "failed" : "queued",
    updatedAt: new Date().toISOString(),
  }).where(and(
    eq(schema.pendingClipIngests.id, id),
    eq(schema.pendingClipIngests.status, "queued"),
    authScopeFilter(authScope),
    isNull(schema.pendingClipIngests.operationKey),
  ));
}

export async function markPendingClipIngestDeferred(id: string, error: string): Promise<void> {
  const authScope = await getCurrentClipIngestAuthScope();
  await getDb().update(schema.pendingClipIngests).set({
    lastError: error,
    updatedAt: new Date().toISOString(),
  }).where(and(
    eq(schema.pendingClipIngests.id, id),
    eq(schema.pendingClipIngests.status, "queued"),
    authScopeFilter(authScope),
    isNull(schema.pendingClipIngests.operationKey),
  ));
}

export async function markPendingClipIngestFailed(id: string, error: string): Promise<void> {
  const authScope = await getCurrentClipIngestAuthScope();
  await getDb().update(schema.pendingClipIngests).set({
    status: "failed",
    lastError: error,
    updatedAt: new Date().toISOString(),
  }).where(and(
    eq(schema.pendingClipIngests.id, id),
    authScopeFilter(authScope),
    isNull(schema.pendingClipIngests.operationKey),
  ));
}

export type ClipIngestReconcileOutcome =
  | { state: "succeeded"; result: ClipIngestResult }
  | { state: "failed"; message: string }
  | { state: "pending"; unknown: boolean }
  | {
      state: "stranded";
      reason: ClipIngestStrandReason;
    };

export type ClipIngestStrandReason =
  | "auth_changed"
  | "server_changed"
  | "server_unknown"
  | "journal_invalid";

export interface ClipIngestRemoteDeps {
  currentAuthScope: () => Promise<string>;
  currentServerFingerprint: () => Promise<string>;
  hasAuth: () => Promise<boolean>;
  lookup: (
    operationKey: string,
    serverFingerprint: string,
  ) => Promise<ClipIngestJobResponse>;
  enqueue: (
    request: ClipIngestJobRequest,
    operationKey: string,
    context: ClipIngestRequestContext,
    serverFingerprint: string,
  ) => Promise<ClipIngestJobResponse>;
  persist: (row: PendingClipIngestRow, patch: OperationPatch) => Promise<void>;
}

const defaultRemoteDeps: ClipIngestRemoteDeps = {
  currentAuthScope: getCurrentClipIngestAuthScope,
  currentServerFingerprint: getCurrentClipIngestServerFingerprint,
  hasAuth: async () => Boolean(await getToken()),
  lookup: (operationKey, serverFingerprint) =>
    docsApi.findIngestJobByIdempotencyKey(operationKey, serverFingerprint),
  enqueue: (request, operationKey, context, serverFingerprint) =>
    docsApi.enqueueIngestJob(
      request,
      operationKey,
      serverFingerprint,
      context,
    ),
  persist: (row, patch) => patchOperation(row, patch),
};

async function remoteReplayStrandReason(
  row: PendingClipIngestRow,
  deps: ClipIngestRemoteDeps,
): Promise<ClipIngestStrandReason | null> {
  if (
    !row.authScope
    || row.authScope === ANONYMOUS_AUTH_SCOPE
    || !(await deps.hasAuth())
    || await deps.currentAuthScope() !== row.authScope
  ) {
    return "auth_changed";
  }
  if (!row.serverFingerprint) return "server_unknown";
  if (await deps.currentServerFingerprint() !== row.serverFingerprint) {
    return "server_changed";
  }
  return null;
}

function jsonOrNull(value: unknown): string | null {
  try { return JSON.stringify(value); } catch { return null; }
}

async function persistRemoteUnknown(
  row: PendingClipIngestRow,
  error: unknown,
  deps: ClipIngestRemoteDeps,
): Promise<ClipIngestReconcileOutcome> {
  const message = errorText(error);
  await deps.persist(row, {
    status: "remote_unknown",
    ackJson: error instanceof ClipIngestMalformedAckError
      ? jsonOrNull(error.payload)
      : row.ackJson,
    errorJson: JSON.stringify({
      kind: isApiTimeoutError(error)
        ? "timeout"
        : isApiConnectionError(error) || isConnectionResetError(error)
          ? "connection"
          : error instanceof ClipIngestMalformedAckError
            ? "malformed_ack"
            : "remote_unknown",
      message,
    }),
    lastError: message,
    retryCount: row.retryCount + 1,
    terminalAt: null,
  });
  return { state: "pending", unknown: true };
}

async function persistRemoteRejected(
  row: PendingClipIngestRow,
  error: unknown,
  deps: ClipIngestRemoteDeps,
): Promise<ClipIngestReconcileOutcome> {
  const message = errorText(error);
  await deps.persist(row, {
    status: "remote_failed",
    errorJson: JSON.stringify({
      kind: "rejected",
      message,
      status: isApiHttpError(error) ? error.status : null,
    }),
    lastError: message,
    terminalAt: new Date().toISOString(),
  });
  return { state: "failed", message };
}

async function persistRemoteJob(
  row: PendingClipIngestRow,
  job: ClipIngestJobResponse,
  deps: ClipIngestRemoteDeps,
): Promise<ClipIngestReconcileOutcome> {
  const ackJson = jsonOrNull(job.raw);
  if (job.status === "succeeded") {
    if (!job.result) {
      return persistRemoteUnknown(row, new ClipIngestMalformedAckError(job.raw), deps);
    }
    await deps.persist(row, {
      status: "remote_succeeded",
      remoteJobId: job.job_id,
      ackJson,
      resultJson: JSON.stringify(job.result),
      errorJson: null,
      lastError: null,
      terminalAt: new Date().toISOString(),
    });
    return { state: "succeeded", result: job.result };
  }
  if (job.status === "failed") {
    const message = typeof job.error.message === "string"
      ? job.error.message
      : "サーバーのClipIngest jobが失敗しました";
    await deps.persist(row, {
      status: "remote_failed",
      remoteJobId: job.job_id,
      ackJson,
      errorJson: JSON.stringify(job.error),
      lastError: message,
      terminalAt: new Date().toISOString(),
    });
    return { state: "failed", message };
  }
  await deps.persist(row, {
    status: "remote_pending",
    remoteJobId: job.job_id,
    ackJson,
    errorJson: null,
    lastError: null,
    terminalAt: null,
  });
  return { state: "pending", unknown: false };
}

function exactLookupMiss(error: unknown): boolean {
  return isApiHttpError(error) && error.status === 404;
}

function definitePostRejection(error: unknown): boolean {
  return isPermanentIngestError(error);
}

export async function reconcileClipIngestOperation(
  input: PendingClipIngestRow,
  overrides: Partial<ClipIngestRemoteDeps> = {},
): Promise<ClipIngestReconcileOutcome> {
  const deps = { ...defaultRemoteDeps, ...overrides };
  let row = input;
  if (!row.operationKey || !row.authScope) {
    return { state: "stranded", reason: "journal_invalid" };
  }
  if (row.delivery === "local") {
    return { state: "pending", unknown: false };
  }

  const initialStrand = await remoteReplayStrandReason(row, deps);
  if (initialStrand) {
    return { state: "stranded", reason: initialStrand };
  }

  if (row.status === "remote_succeeded") {
    const result = clipIngestResultFromOperation(row);
    return result ? { state: "succeeded", result } : { state: "pending", unknown: true };
  }
  if (row.status === "remote_failed") {
    return { state: "failed", message: row.lastError || "ClipIngest job failed" };
  }

  if (row.delivery === null) {
    await deps.persist(row, { delivery: "remote", status: "remote_ready" });
    row = { ...row, delivery: "remote", status: "remote_ready" };
  }
  const operationKey = row.operationKey;
  const serverFingerprint = row.serverFingerprint;
  if (!operationKey || !serverFingerprint) {
    return { state: "stranded", reason: "server_unknown" };
  }
  let job: ClipIngestJobResponse;
  try {
    job = await deps.lookup(operationKey, serverFingerprint);
  } catch (lookupError) {
    if (
      typeof isApiServerChangedError === "function"
      && isApiServerChangedError(lookupError)
    ) {
      return { state: "stranded", reason: "server_changed" };
    }
    if (!exactLookupMiss(lookupError)) return persistRemoteUnknown(row, lookupError, deps);
    const replayStrand = await remoteReplayStrandReason(row, deps);
    if (replayStrand) {
      return { state: "stranded", reason: replayStrand };
    }
    try {
      job = await deps.enqueue(
        requestForOperation(row),
        operationKey,
        contextForOperation(row),
        serverFingerprint,
      );
    } catch (postError) {
      if (
        typeof isApiServerChangedError === "function"
        && isApiServerChangedError(postError)
      ) {
        return { state: "stranded", reason: "server_changed" };
      }
      if (definitePostRejection(postError)) return persistRemoteRejected(row, postError, deps);
      return persistRemoteUnknown(row, postError, deps);
    }
  }
  return persistRemoteJob(row, job, deps);
}

export interface PendingClipIngestFlushDeps {
  currentAuthScope: () => Promise<string>;
  currentServerFingerprint: () => Promise<string>;
  hasAuth: () => Promise<boolean>;
  list: (
    authScope: string,
    serverFingerprint?: string,
  ) => Promise<PendingClipIngestRow[]>;
  reconcile: (row: PendingClipIngestRow) => Promise<ClipIngestReconcileOutcome>;
  // Legacy queue adapter retained so old tests/callers can migrate safely.
  ingest?: (source: string) => Promise<unknown>;
  remove?: (id: string) => Promise<void>;
  markFailed?: (id: string, error: string) => Promise<void>;
  markDeferred?: (id: string, error: string) => Promise<void>;
  markRetry?: (id: string, retryCount: number, error: string) => Promise<void>;
}

const defaultFlushDeps: PendingClipIngestFlushDeps = {
  currentAuthScope: getCurrentClipIngestAuthScope,
  currentServerFingerprint: getCurrentClipIngestServerFingerprint,
  hasAuth: async () => Boolean(await getToken()),
  list: (authScope, serverFingerprint) =>
    listClipIngestOperationsForScope(
      authScope,
      ACTIVE_OPERATION_STATUSES,
      serverFingerprint,
    ),
  reconcile: (row) => reconcileClipIngestOperation(row),
};

const flushInFlightByScope = new Map<string, Promise<void>>();

/** Preserve pre-journal flush semantics when an old caller injects ingest/remove deps. */
async function flushLegacyQueue(
  deps: PendingClipIngestFlushDeps,
): Promise<void> {
  const currentScope = await deps.currentAuthScope();
  if (!(await deps.hasAuth())) return;
  const rows = (await deps.list(currentScope)).filter((row) => row.authScope === currentScope);
  for (const row of rows) {
    try {
      await deps.ingest!(row.source);
      await deps.remove?.(row.id);
    } catch (error) {
      if (isApiConnectionError(error)) throw error;
      const message = errorText(error);
      if (isApiHttpError(error) && ([401, 408, 409, 429].includes(error.status) || error.status >= 500)) {
        await deps.markDeferred?.(row.id, message);
        return;
      }
      if (isPermanentIngestError(error)) {
        await deps.markFailed?.(row.id, message);
        continue;
      }
      await deps.markRetry?.(row.id, row.retryCount, message);
    }
  }
}

export async function flushPendingClipIngests(
  overrides: Partial<PendingClipIngestFlushDeps> = {},
): Promise<void> {
  const deps = { ...defaultFlushDeps, ...overrides };
  // Explicit legacy dependency injection is supported only for old callers.
  if (deps.reconcile === undefined || deps.ingest) {
    return flushLegacyQueue(deps);
  }
  const currentScope = await deps.currentAuthScope();
  if (currentScope === ANONYMOUS_AUTH_SCOPE || !(await deps.hasAuth())) return;
  const currentServerFingerprint = await deps.currentServerFingerprint();
  const flightKey = `${currentScope}|${currentServerFingerprint}`;
  const running = flushInFlightByScope.get(flightKey);
  if (running) return running;
  const flight = (async () => {
    const rows = (await deps.list(currentScope, currentServerFingerprint)).filter(
      (row) =>
        row.operationKey != null
        && row.authScope === currentScope
        && row.serverFingerprint === currentServerFingerprint,
    );
    for (const row of rows) {
      if (
        !(await deps.hasAuth())
        || await deps.currentAuthScope() !== currentScope
        || await deps.currentServerFingerprint() !== currentServerFingerprint
      ) return;
      const outcome = await deps.reconcile(row);
      if (outcome.state === "stranded") return;
      if (outcome.state === "pending" && outcome.unknown) return;
    }
  })();
  flushInFlightByScope.set(flightKey, flight);
  try {
    await flight;
  } finally {
    if (flushInFlightByScope.get(flightKey) === flight) {
      flushInFlightByScope.delete(flightKey);
    }
  }
}

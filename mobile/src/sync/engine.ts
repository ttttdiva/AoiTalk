import { and, eq, like } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { runBackgroundSqliteWrite, runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import type { SqliteHandle, SqliteWriter } from "../db/sqlite-writer";
import { rescheduleLocalTaskNotificationsFromCache } from "../lib/local-notifications";
import {
  docsSqliteAsyncAvailable,
  encodeDocsBoolean,
  encodeDocsJson,
  executeDocsPreparedBatch,
  type DocsSqliteAsyncTransaction,
  withDocsExclusiveTransaction,
} from "../db/docs-sync-async";
import {
  hasPendingOutbox,
  hasPendingOutboxInWriter,
  resolveAuthScope,
  getOutboxState,
  listPendingOutbox,
  markOutboxConflict,
  markOutboxError,
  rebaseOutboxOp,
  removeOutboxOpIfSnapshot,
  removeOutboxOpIfSnapshotInWriter,
  recordOutboxServerSnapshotInWriter,
  randomId,
  quarantineOutboxOpIfSnapshotInWriter,
  validateOutboxConflictServerScope,
} from "../repositories/outbox";
import { getSyncV3Mode, type SyncV3Mode } from "./v3/api";
import { requestV3DocsRebootstrap, runV3Pull } from "./v3/pull";
import { pushOutboxViaV3 } from "./v3/push";
import { SyncV3DisabledError } from "./v3/types";
import { captureLegacy } from "../sync-v3/legacy-migration";
import { markSyncV3RequestWriteFailed } from "../sync-v3/request-observer";
import { checkSyncV3CacheSafety, isSyncV3ManuallyStopped } from "./v3/safety";
import { rebaseDocsWriteFollowups, rebaseLegacyBlankDocsUpdate, isDocsResultOlderThanRecordedConflict, matchesDocsCreateContent, compareDocsVersions } from "../repositories/docs-create-ack";
import { isTaskCasStaleReason, matchesTaskUpdateReceipt } from "./task-update-receipt";
import {
  applyRemoteTasks,
  applyRemoteTasksInWriter,
  applyTaskRestore,
  applyTaskTombstones,
  applyTaskTombstonesInWriter,
  prepareTaskTombstoneLedgerKey,
} from "../repositories/tasks";
import {
  applyProjectTombstonesInWriter,
  applyRemoteProjects,
  applyRemoteProjectsInWriter,
} from "../repositories/projects";
import {
  applyOccurrenceTombstonesInWriter,
  applyRemoteOccurrencesInWriter,
  prepareOccurrenceApply,
} from "../repositories/occurrences";
import {
  applyConversationMessageTombstonesInWriter,
  applyConversationSessionTombstonesInWriter,
  applyRemoteConversationMessagesInWriter,
  applyRemoteConversationSessions,
  applyRemoteConversationSessionsInWriter,
  prepareRemoteConversationSessions,
  reconcileConversationSessionsWithServerInWriter,
} from "../repositories/conversations";
import {
  applyRemoteTimeEntries,
  applyRemoteTimeEntriesInWriter,
  applyTimeEntryTombstonesInWriter,
  prepareTimeEntryApply,
} from "../repositories/timeEntries";
import {
  applyRecordFieldTombstonesInWriter,
  applyRecordRowTombstonesInWriter,
  applyRecordTableTombstonesInWriter,
  applyRemoteRecordFieldsInWriter,
  applyRemoteRecordRowsInWriter,
  applyRemoteRecordTablesInWriter,
} from "../repositories/records";
import {
  applyDocsNodeTombstones,
  applyRemoteDocsEdges,
  applyRemoteDocsFieldValues,
  applyRemoteDocsFields,
  applyRemoteDocsNodeSupertags,
  applyRemoteDocsNodes,
  applyRemoteDocsNodesInWriter,
  applyRemoteDocsSupertagsInWriter,
  applyRemoteDocsNodeSupertagsInWriter,
  applyRemoteDocsFieldValuesInWriter,
  deleteLocalDocsNodeSupertagInWriter,
  deleteLocalDocsFieldValueInWriter,
  applyRemoteDocsPlacements,
  applyRemoteDocsSupertagFields,
  applyRemoteDocsSupertags,
  rollbackDocsArchiveOptimistic,
  reconcileDocsFieldsWithServer,
  reconcileDocsNodesWithServer,
  reconcileDocsSupertagsWithServer,
  promoteDocsSyncRun,
  quarantineRevokedDocsScope,
} from "../repositories/docs";
import { flushPendingConversations } from "../repositories/conversations";
import { flushPendingClipIngests } from "../repositories/pending-clip-ingest";
import { refreshClipIngestTargetsIfStale } from "../lib/clip-ingest-targets";
import { characterApi } from "../lib/character-api";
import { taskApi } from "../lib/task-api";
import type {
  ConversationMessage,
  ConversationSession,
  DocsEdge,
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsNodePlacement,
  DocsNodeSupertag,
  DocsSupertag,
  DocsSupertagField,
  Project,
  RecordField,
  RecordRow,
  RecordTable,
  Task,
  TaskOccurrence,
  TimeEntry,
} from "../types/api";
import { canAttemptAoiTalkServer } from "../stores/network";
import { useSyncStatusStore } from "../stores/sync-status";
import type {
  DocsProgressUpdate,
  OutboxSyncState,
  SyncDocsScope,
  SyncStage,
} from "../stores/sync-status";
import { getCachedToken, getToken, getTokenAuthScope } from "../lib/auth";
import {
  createApiRequestId,
  isApiConnectionError,
  isApiHttpError,
  isApiTimeoutError,
} from "../lib/api-client";
import {
  enqueueAuthScopeExclusive,
  runAuthScopeTransition as runQueuedAuthScopeTransition,
} from "../lib/auth-scope-queue";
import { registerMutationWakeListener } from "./task-mutation-wake";
import { isMobileReadOnlyDiagnosticsEnabled } from "../lib/read-only-diagnostics";
import {
  pullSync,
  pushSync,
  type DocsSyncScope,
  type SyncPullLifecycle,
  type SyncPullRequestOutcome,
  type SyncPullResponse,
  type SyncPushOperation,
  type SyncPushResult,
  type SyncTable,
} from "./api";

import {
  GENERAL_DIGEST_VERSION, generalDigestsForRequest, generalTableNeedsApply, reusableGeneralDigests,
  validGeneralDigests, validateGeneralDigestPage,
} from "./general-sync-digests";

const TABLES: SyncTable[] = [
  "projects",
  "tasks",
  "task_occurrences",
  "time_entries",
  "conversation_sessions",
  "conversation_messages",
  "record_tables",
  "record_fields",
  "record_rows",
];

const DOCS_TABLES: SyncTable[] = [
  "knowledge_nodes",
  "knowledge_supertags",
  "knowledge_node_supertags",
  "knowledge_supertag_fields",
  "knowledge_fields",
  "knowledge_field_values",
  "knowledge_node_placements",
  "knowledge_edges",
];

// Docsはページ単位で同一snapshotを検証するため、Web/PC側の編集がページ間に
// 入るとサーバーが400でcursorを無効化する。短いバックオフを挟んで再取得し、
// 一時的な競合で設定画面の強制同期が失敗しないようにする。
const DOCS_PULL_MAX_ATTEMPTS = 4;
const DOCS_PULL_RETRY_DELAY_MS = 80;
const DOCS_PULL_TRANSPORT_RETRY_DELAY_MS = 750;
/** Keep each staging write transaction small enough for production pages. */
export const DOCS_STAGING_WRITE_BATCH_SIZE = 256;

export type DocsResyncProgress = {
  phase: "queued" | "preparing" | "downloading" | "retrying" | "finalizing";
  completed: number;
  total: number | null;
  page: number;
  retryAttempt?: number;
  /** Bounded scale telemetry; UI callers may ignore these optional fields. */
  stagedRowsWritten?: number;
  stagedRowsRead?: number;
  stagedPromotionBatches?: number;
  stagedPromotionMaxBatchSize?: number;
};

export type SyncDiagnosticSkipReason =
  | "no_token"
  | "read_only_diagnostics"
  | "not_active"
  | "backend_unavailable"
  | "auth_lookup_pending"
  | "already_running"
  | "auth_scope_changed"
  | "active_scope_transition";

export type SyncDiagnosticFailureReason = "auth_lookup_failed";

export type SyncDiagnosticOutcome =
  | "running"
  | "success"
  | "partial"
  | "failed"
  | "interrupted"
  | "skipped";

export type SyncDiagnosticScope = "general" | "personal" | "shared" | "project";

export type SyncDiagnosticTable = {
  table: SyncTable;
  /** False means the response did not contain this table; zero is not inferred. */
  received: boolean;
  changes: number;
  authoritativeCount: number | null;
  hasMore: boolean | null;
};

export type DocsSyncDiagnosticStage =
  | "load_saved_scopes"
  | "load_last_pulled"
  | "load_table_digests"
  | "load_scope_digest"
  | "load_scope_revision"
  | "load_active_run"
  | "create_run"
  | "ready_resume"
  | "http_pull"
  | "reload_ready_run"
  | "promoting";

export type DocsSyncDiagnostic = {
  stage: DocsSyncDiagnosticStage;
  /** Total elapsed time since this flight's Docs watchdog budget began. */
  elapsedMs: number;
  runState: "downloading" | "ready" | null;
  pendingTables: number | null;
};

export type SyncPullDiagnostic = {
  requestId: string;
  kind: "general" | "docs";
  scope: SyncDiagnosticScope;
  startedAt: number;
  finishedAt: number | null;
  status: number | null;
  outcome: SyncPullRequestOutcome | "not_sent";
  /** Number/statuses of HTTP attempts; retries retain the same requestId. */
  attemptCount: number;
  /** Null entries are attempts that ended without an HTTP response. */
  attemptStatuses: Array<number | null>;
  hasMore: boolean | null;
  promotion: "not_applicable" | "staged" | "applied" | "promoted" | "failed" | "discarded";
  promotionGeneration: number | null;
  tables: SyncDiagnosticTable[];
};

export type SyncDiagnosticReceipt = {
  requestId: string;
  requestedAt: number;
  authGeneration: number;
  startedAt: number | null;
  finishedAt: number | null;
  outcome: SyncDiagnosticOutcome;
  skipReason: SyncDiagnosticSkipReason | null;
  failureReason: SyncDiagnosticFailureReason | null;
  /** Set for a coalesced waiter; points at the actual running receipt. */
  canonicalRequestId: string | null;
  /** Current fixed Docs child checkpoint, independent of HTTP pull receipts. */
  docsSubstage: DocsSyncDiagnostic | null;
  /** Immutable copy of the Docs checkpoint captured by the 90s watchdog. */
  docsWatchdog: DocsSyncDiagnostic | null;
  pulls: SyncPullDiagnostic[];
};

/**
 * Request-owned, read-only access to a diagnostic receipt.
 *
 * The request object remains authoritative after its bounded history entry is
 * evicted.  Consumers receive a fresh clone on every read so diagnostics
 * cannot mutate the live engine receipt.  A handle is scoped to the auth
 * generation in which it was created and becomes unusable after a transition.
 */
export type SyncDiagnosticRequestHandle = {
  requestId: string;
  authGeneration: number;
  getReceipt: () => SyncDiagnosticReceipt | null;
};

export type SyncDiagnosticSnapshot = {
  requestCount: number;
  executionCount: number;
  runningScopes: number;
  authGeneration: number;
  latest: SyncDiagnosticReceipt | null;
  recent: SyncDiagnosticReceipt[];
  /** Canonical receipts retained outside the bounded request history. */
  retained?: SyncDiagnosticReceipt[];
};

export type SyncDiagnosticListener = (snapshot: SyncDiagnosticSnapshot) => void;

type DocsResyncProgressHandler = (progress: DocsResyncProgress) => void;

type PendingOutbox = typeof schema.outbox.$inferSelect;
type PendingOutboxSnapshot = Parameters<typeof removeOutboxOpIfSnapshot>[1];
type SyncExecutionContext = {
  epoch: number;
  controller: AbortController;
  /** Present only for a normal product sync; force Docs resync has its own UI. */
  flightSequence?: number;
  lastDocsProgressKey?: string;
  docsLiveWriteState?: { started: boolean };
  docsDiagnostic?: {
    startedAt: number;
    stage: DocsSyncDiagnosticStage | null;
    runState: DocsSyncDiagnostic["runState"];
    pendingTables: number | null;
    frozen: boolean;
  };
  diagnostic?: SyncDiagnosticReceipt;
  v3PushRequestIds: Set<string>;
};
type DocsSyncRunRow = typeof schema.docsSyncRuns.$inferSelect;
type DocsSyncAuthoritative = Record<
  string,
  { ids?: string[]; scopeId?: string; digest?: string }
>;
type RunningSyncFlight = {
  epoch: number;
  promise: Promise<void>;
  mutationWakeRequested: boolean;
  diagnostic: SyncDiagnosticReceipt;
};

// Only a failed read request is automatically replayable. Local staging,
// promotion and progress observers may also throw errors mentioning timeout.
class DocsPullTransportError extends Error {
  constructor(readonly originalError: unknown) {
    super("Docs pull transport failed");
    this.name = "DocsPullTransportError";
  }
}

class SyncExecutionInterruptedError extends Error {
  constructor() {
    super("同期はアプリがactiveへ復帰するまで中断されました");
    this.name = "SyncExecutionInterruptedError";
  }
}

const completedSync = Promise.resolve();
const runningByAuthScope = new Map<string, RunningSyncFlight>();
type RunningDocsResyncFlight = {
  promise: Promise<void>;
  listeners: Set<DocsResyncProgressHandler>;
  progress: DocsResyncProgress;
};
const docsResyncByAuthScope = new Map<string, RunningDocsResyncFlight>();
let unresolvedAuthFlight: Promise<void> | null = null;
let syncRequestCount = 0;
let syncExecutionCount = 0;
let syncFlightSequence = 0;
let syncExecutionActive = true;
let syncExecutionEpoch = 0;
let syncDiagnosticGeneration = 0;
let nextSyncDiagnosticPromotionGeneration = 0;
const activeSyncContexts = new Set<SyncExecutionContext>();
let nextAuthScopeTransitionToken = 0;
const activeAuthScopeTransitions = new Set<number>();
const deferredMutationWakeScopes = new Set<string>();

const SYNC_DIAGNOSTIC_HISTORY_LIMIT = 8;
const SYNC_DIAGNOSTIC_PULL_LIMIT = 32;
const SYNC_DIAGNOSTIC_TABLE_LIMIT = 20;
const syncDiagnosticHistory: SyncDiagnosticReceipt[] = [];
const syncDiagnosticCanonicalReceipts = new Map<string, SyncDiagnosticReceipt>();
let latestSyncDiagnosticCanonical: SyncDiagnosticReceipt | null = null;
const syncDiagnosticListeners = new Set<SyncDiagnosticListener>();
let syncDiagnosticNotifyQueued = false;

function notifySyncDiagnosticListeners(): void {
  if (syncDiagnosticNotifyQueued || syncDiagnosticListeners.size === 0) return;
  syncDiagnosticNotifyQueued = true;
  queueMicrotask(() => {
    syncDiagnosticNotifyQueued = false;
    if (syncDiagnosticListeners.size === 0) return;
    const snapshot = getSyncDiagnosticSnapshot();
    for (const listener of [...syncDiagnosticListeners]) {
      try {
        listener(snapshot);
      } catch {
        // A diagnostics observer must never affect the sync flight.
      }
    }
  });
}

/** Subscribe to bounded, non-secret sync diagnostics without polling. */
export function subscribeSyncDiagnostics(listener: SyncDiagnosticListener): () => void {
  syncDiagnosticListeners.add(listener);
  return () => syncDiagnosticListeners.delete(listener);
}

function trimSyncDiagnosticHistory(): void {
  while (syncDiagnosticHistory.length > SYNC_DIAGNOSTIC_HISTORY_LIMIT) {
    // Canonical receipts are retained separately below. The request history
    // itself stays hard-bounded even while an old flight is still running.
    syncDiagnosticHistory.shift();
  }
}

function retainSyncDiagnosticCanonical(receipt: SyncDiagnosticReceipt): void {
  if (receipt.authGeneration !== syncDiagnosticGeneration) return;
  syncDiagnosticCanonicalReceipts.set(receipt.requestId, receipt);
  latestSyncDiagnosticCanonical = receipt;
  while (syncDiagnosticCanonicalReceipts.size > SYNC_DIAGNOSTIC_HISTORY_LIMIT) {
    const oldest = syncDiagnosticCanonicalReceipts.keys().next().value as string | undefined;
    if (!oldest) break;
    syncDiagnosticCanonicalReceipts.delete(oldest);
  }
  notifySyncDiagnosticListeners();
}

function getActiveSyncDiagnosticCanonicals(): SyncDiagnosticReceipt[] {
  const active = new Map<string, SyncDiagnosticReceipt>();
  for (const flight of runningByAuthScope.values()) {
    const receipt = flight.diagnostic;
    if (
      receipt.authGeneration === syncDiagnosticGeneration
      && receipt.canonicalRequestId === null
    ) {
      active.set(receipt.requestId, receipt);
    }
  }
  // A context can be active during the short hand-off before its flight is
  // published in runningByAuthScope. Keep that canonical discoverable without
  // growing the request history ring.
  for (const context of activeSyncContexts) {
    const receipt = context.diagnostic;
    if (
      receipt
      && receipt.authGeneration === syncDiagnosticGeneration
      && receipt.canonicalRequestId === null
    ) {
      active.set(receipt.requestId, receipt);
    }
  }
  return [...active.values()];
}

function createSyncDiagnosticReceipt(): SyncDiagnosticReceipt {
  const receipt: SyncDiagnosticReceipt = {
    requestId: createApiRequestId("sync-run"),
    requestedAt: Date.now(),
    authGeneration: syncDiagnosticGeneration,
    startedAt: null,
    finishedAt: null,
    outcome: "running",
    skipReason: null,
    failureReason: null,
    canonicalRequestId: null,
    docsSubstage: null,
    docsWatchdog: null,
    pulls: [],
  };
  syncDiagnosticHistory.push(receipt);
  trimSyncDiagnosticHistory();
  notifySyncDiagnosticListeners();
  return receipt;
}

function markSyncDiagnosticStarted(receipt: SyncDiagnosticReceipt): void {
  if (receipt.startedAt != null) return;
  receipt.startedAt = Date.now();
  notifySyncDiagnosticListeners();
}

function finishSyncDiagnostic(
  receipt: SyncDiagnosticReceipt,
  outcome: SyncDiagnosticOutcome,
  skipReason: SyncDiagnosticSkipReason | null = null,
  failureReason: SyncDiagnosticFailureReason | null = null,
): void {
  receipt.outcome = outcome;
  receipt.skipReason = skipReason;
  receipt.failureReason = failureReason;
  receipt.finishedAt ??= Date.now();
  trimSyncDiagnosticHistory();
  notifySyncDiagnosticListeners();
}

function diagnosticScopeFor(
  scopeId?: string,
  projectId?: string | null,
): SyncDiagnosticScope {
  if (projectId) return "project";
  if (scopeId) return "shared";
  return "personal";
}

function beginPullDiagnostic(
  receipt: SyncDiagnosticReceipt | undefined,
  kind: "general" | "docs",
  scope: SyncDiagnosticScope,
  tables: SyncTable[],
  promotion: SyncPullDiagnostic["promotion"],
  promotionGeneration: number | null = null,
): SyncPullDiagnostic | null {
  if (!receipt) return null;
  const pull: SyncPullDiagnostic = {
    requestId: createApiRequestId("sync-pull"),
    kind,
    scope,
    startedAt: Date.now(),
    finishedAt: null,
    status: null,
    outcome: "not_sent",
    attemptCount: 0,
    attemptStatuses: [],
    hasMore: null,
    promotion,
    promotionGeneration,
    tables: tables.slice(0, SYNC_DIAGNOSTIC_TABLE_LIMIT).map((table) => ({
      table,
      received: false,
      changes: 0,
      authoritativeCount: null,
      hasMore: null,
    })),
  };
  receipt.pulls.push(pull);
  while (receipt.pulls.length > SYNC_DIAGNOSTIC_PULL_LIMIT) receipt.pulls.shift();
  notifySyncDiagnosticListeners();
  return pull;
}

function pullDiagnosticLifecycle(
  pull: SyncPullDiagnostic | null,
): SyncPullLifecycle | undefined {
  if (!pull) return undefined;
  return {
    onAttempt: () => {
      pull.attemptCount += 1;
      if (pull.attemptStatuses.length < 8) pull.attemptStatuses.push(null);
      notifySyncDiagnosticListeners();
    },
    onResponse: ({ status }) => {
      pull.status = status;
      const last = pull.attemptStatuses.length - 1;
      if (last >= 0 && pull.attemptStatuses[last] == null) {
        pull.attemptStatuses[last] = status;
      } else if (pull.attemptStatuses.length < 8) {
        pull.attemptStatuses.push(status);
      }
      notifySyncDiagnosticListeners();
    },
    onFinish: ({ finishedAt, status, outcome }) => {
      pull.finishedAt = finishedAt;
      pull.status = status;
      pull.outcome = outcome;
      notifySyncDiagnosticListeners();
    },
  };
}

function recordPullResponse(
  pull: SyncPullDiagnostic | null,
  response: SyncPullResponse,
  pendingTables: SyncTable[],
  promotion: SyncPullDiagnostic["promotion"],
): void {
  if (!pull) return;
  pull.promotion = promotion;
  pull.hasMore = typeof response.has_more === "boolean"
    ? response.has_more
    : null;
  pull.tables = pendingTables.slice(0, SYNC_DIAGNOSTIC_TABLE_LIMIT).map((table) => {
    const payload = response.tables[table];
    return {
      table,
      received: Boolean(payload),
      changes: payload?.changes?.length ?? 0,
      authoritativeCount: typeof payload?.authoritative_count === "number"
        && Number.isFinite(payload.authoritative_count)
        ? Math.max(0, payload.authoritative_count)
        : null,
      hasMore: payload ? payload.cursor != null : null,
    };
  });
  if (pull.finishedAt == null) {
    pull.finishedAt = Date.now();
    pull.status ??= 200;
    pull.outcome = "success";
    pull.attemptCount = Math.max(1, pull.attemptCount);
    if (pull.attemptStatuses.length === 0 && pull.status != null) {
      pull.attemptStatuses = [pull.status];
    }
  }
  notifySyncDiagnosticListeners();
}

function finishPullDiagnosticFromError(
  pull: SyncPullDiagnostic | null,
  error: unknown,
): void {
  if (!pull || pull.finishedAt != null) return;
  pull.finishedAt = Date.now();
  pull.status = isApiHttpError(error) ? error.status : null;
  if (pull.status != null) {
    pull.attemptCount = Math.max(1, pull.attemptCount);
    if (pull.attemptStatuses.length === 0) pull.attemptStatuses = [pull.status];
  }
  pull.outcome = isApiHttpError(error)
    ? "http_error"
    : error instanceof Error && error.name === "AbortError"
      ? "cancelled"
      : isApiConnectionError(error) || isApiTimeoutError(error)
        ? "transport_error"
        : "parse_error";
  notifySyncDiagnosticListeners();
}

function markDocsPromotion(
  receipt: SyncDiagnosticReceipt | undefined,
  scope: SyncDiagnosticScope,
  promotion: "promoted" | "failed" | "discarded",
  promotionGeneration: number | null = null,
): void {
  if (!receipt) return;
  for (const pull of receipt.pulls) {
    if (
      pull.kind === "docs"
      && pull.scope === scope
      && (pull.promotion === "staged" || pull.promotion === "applied")
      && (promotionGeneration == null || pull.promotionGeneration === promotionGeneration)
    ) {
      pull.promotion = promotion;
    }
  }
  notifySyncDiagnosticListeners();
}

function markStagedDocsPromotionsFailed(receipt: SyncDiagnosticReceipt | undefined): void {
  if (!receipt) return;
  for (const pull of receipt.pulls) {
    if (
      pull.kind === "docs"
      && pull.promotion === "staged"
    ) {
      pull.promotion = "failed";
    }
  }
  notifySyncDiagnosticListeners();
}

function cloneSyncDiagnosticReceipt(
  receipt: SyncDiagnosticReceipt,
): SyncDiagnosticReceipt {
  return {
    ...receipt,
    docsSubstage: receipt.docsSubstage ? { ...receipt.docsSubstage } : null,
    docsWatchdog: receipt.docsWatchdog ? { ...receipt.docsWatchdog } : null,
    pulls: receipt.pulls.map((pull) => ({
      ...pull,
      attemptStatuses: [...pull.attemptStatuses],
      tables: pull.tables.map((table) => ({ ...table })),
    })),
  };
}

function docsRunDiagnosticFields(run: DocsSyncRunRow | null): {
  runState: DocsSyncDiagnostic["runState"];
  pendingTables: number | null;
} {
  if (!run) return { runState: null, pendingTables: null };
  const pending = parseJsonRecord<unknown>(run.pendingJson, null);
  return {
    runState: run.state === "downloading" || run.state === "ready"
      ? run.state
      : null,
    pendingTables: Array.isArray(pending) ? pending.length : null,
  };
}

function publishDocsSubstage(context: SyncExecutionContext): void {
  const runtime = context.docsDiagnostic;
  if (!runtime || runtime.frozen || !runtime.stage || !context.diagnostic) return;
  context.diagnostic.docsSubstage = {
    stage: runtime.stage,
    elapsedMs: Math.max(0, Date.now() - runtime.startedAt),
    runState: runtime.runState,
    pendingTables: runtime.pendingTables,
  };
  notifySyncDiagnosticListeners();
}

function markDocsSubstage(
  context: SyncExecutionContext,
  stage: DocsSyncDiagnosticStage,
  run?: DocsSyncRunRow | null,
): void {
  const runtime = context.docsDiagnostic;
  if (!runtime || runtime.frozen) return;
  runtime.stage = stage;
  if (run !== undefined) {
    const fields = docsRunDiagnosticFields(run);
    runtime.runState = fields.runState;
    runtime.pendingTables = fields.pendingTables;
  }
  publishDocsSubstage(context);
}

function updateDocsRunDiagnostic(
  context: SyncExecutionContext,
  run: DocsSyncRunRow | null,
): void {
  const runtime = context.docsDiagnostic;
  if (!runtime || runtime.frozen) return;
  const fields = docsRunDiagnosticFields(run);
  runtime.runState = fields.runState;
  runtime.pendingTables = fields.pendingTables;
  publishDocsSubstage(context);
}

function resetDocsRunDiagnosticFields(context: SyncExecutionContext): void {
  const runtime = context.docsDiagnostic;
  if (!runtime || runtime.frozen) return;
  runtime.runState = null;
  runtime.pendingTables = null;
}

function captureDocsWatchdog(context: SyncExecutionContext): void {
  const runtime = context.docsDiagnostic;
  if (!runtime || runtime.frozen || !runtime.stage) return;
  runtime.frozen = true;
  if (!context.diagnostic || context.diagnostic.docsWatchdog) return;
  const snapshot: DocsSyncDiagnostic = {
    stage: runtime.stage,
    elapsedMs: Math.max(0, Date.now() - runtime.startedAt),
    runState: runtime.runState,
    pendingTables: runtime.pendingTables,
  };
  context.diagnostic.docsSubstage = { ...snapshot };
  context.diagnostic.docsWatchdog = { ...snapshot };
  notifySyncDiagnosticListeners();
}

/**
 * Capture a request-owned diagnostic receipt before bounded history can evict
 * it.  The closure intentionally holds the original receipt object rather
 * than adding another global retention structure or decorating a shared
 * single-flight promise.
 */
export function getSyncDiagnosticRequestHandle(
  requestId: string | null | undefined,
  authGeneration = syncDiagnosticGeneration,
): SyncDiagnosticRequestHandle | null {
  if (!requestId) return null;
  const receipt = syncDiagnosticHistory.find((candidate) => candidate.requestId === requestId)
    ?? syncDiagnosticCanonicalReceipts.get(requestId)
    ?? getActiveSyncDiagnosticCanonicals().find((candidate) => candidate.requestId === requestId)
    ?? null;
  if (!receipt || receipt.authGeneration !== authGeneration) return null;
  return {
    requestId: receipt.requestId,
    authGeneration,
    getReceipt: () => (
      syncDiagnosticGeneration === authGeneration
      && receipt.authGeneration === authGeneration
        ? cloneSyncDiagnosticReceipt(receipt)
        : null
    ),
  };
}

export function setSyncExecutionActive(active: boolean): void {
  if (syncExecutionActive === active) return;
  syncExecutionActive = active;
  syncExecutionEpoch += 1;
  if (!active) {
    // Abort in-flight HTTP immediately.  The old epoch check still protects
    // every SQLite/write boundary, while the signal releases the auth-scope
    // queue without waiting for the 60s pull timeout or transport retries.
    for (const context of activeSyncContexts) context.controller.abort();
  }
}

function createSyncExecutionContext(
  flightSequence?: number,
  diagnostic?: SyncDiagnosticReceipt,
): SyncExecutionContext {
  const context = {
    epoch: syncExecutionEpoch,
    controller: new AbortController(),
    flightSequence,
    diagnostic,
    v3PushRequestIds: new Set<string>(),
  };
  activeSyncContexts.add(context);
  return context;
}

function releaseSyncExecutionContext(context: SyncExecutionContext): void {
  activeSyncContexts.delete(context);
}

function assertSyncExecutionActive(context: SyncExecutionContext): void {
  if (
    !syncExecutionActive
    || context.epoch !== syncExecutionEpoch
    || context.controller.signal.aborted
  ) {
    throw new SyncExecutionInterruptedError();
  }
}

function isSyncExecutionInterrupted(
  context: SyncExecutionContext,
): boolean {
  return (
    context.controller.signal.aborted
    || !syncExecutionActive
    || context.epoch !== syncExecutionEpoch
  );
}

function enqueueExclusive(operation: () => Promise<void>): Promise<void> {
  return enqueueAuthScopeExclusive(operation);
}

function wakeMutationSync(authScope: string): void {
  const start = (token: string | null): void => {
    if (!token || getTokenAuthScope(token) !== authScope) return;
    void runSyncForToken(token).catch(() => undefined);
  };
  const cached = getCachedToken();
  if (cached !== undefined) {
    start(cached);
    return;
  }
  void getToken().then(start).catch(() => undefined);
}

function requestMutationSyncForToken(authScope: string, token: string): void {
  if (isMobileReadOnlyDiagnosticsEnabled()) {
    void runSyncForToken(token).catch(() => undefined);
    return;
  }
  updateSyncStatus(() => useSyncStatusStore.getState().markOutboxQueued(authScope));
  const running = runningByAuthScope.get(authScope);
  if (running) {
    // The outbox row is durable.  A single trailing run is enough even when
    // several edits arrive during the same long general/Docs flight.
    running.mutationWakeRequested = true;
    return;
  }
  void runSyncForToken(token).catch(() => undefined);
}

function requestMutationSync(authScope: string): void {
  if (activeAuthScopeTransitions.size > 0) {
    // Keep the durable mutation's wake until the new token/scope is settled;
    // otherwise an old-scope notification could mark the new UI as pending.
    deferredMutationWakeScopes.add(authScope);
    return;
  }
  const cached = getCachedToken();
  if (cached !== undefined) {
    if (!cached || getTokenAuthScope(cached) !== authScope) return;
    requestMutationSyncForToken(authScope, cached);
    return;
  }
  const lookupTransition = nextAuthScopeTransitionToken;
  void getToken()
    .then((token) => {
      if (activeAuthScopeTransitions.size > 0) {
        deferredMutationWakeScopes.add(authScope);
        return;
      }
      // A lookup can finish after an entire account transition, not only
      // while its callback is active. Re-evaluate against the current token.
      if (lookupTransition !== nextAuthScopeTransitionToken) {
        requestMutationSync(authScope);
        return;
      }
      const currentToken = getCachedToken();
      const resolvedToken = currentToken === undefined ? token : currentToken;
      if (!resolvedToken || getTokenAuthScope(resolvedToken) !== authScope) return;
      requestMutationSyncForToken(authScope, resolvedToken);
    })
    .catch(() => undefined);
}


function getSyncStateKey(authScope: string): string {
  return `__global__:${authScope.slice("auth:".length)}`;
}

const GENERAL_SYNC_PAGINATION_VERSION = 1;
// A defensive upper bound for a corrupt/malicious server that keeps issuing
// fresh cursors forever.  Normal full pulls (including the current 1k+ message
// device dataset) complete in well under 100 pages; exceeding this bound is a
// recoverable sync failure rather than an unbounded battery/network loop.
const GENERAL_SYNC_MAX_PAGES = 2_048;

type GeneralSyncCheckpoint = {
  version: number;
  /** Missing on checkpoints written by the first v1 build; those are active. */
  state?: "in_progress" | "complete";
  since: string | null;
  serverTime: string | null;
  /** Safe time watermark; null retains a full projection, possibly digest-elided. */
  watermark: string | null;
  snapshotToken: string | null;
  pendingTables: SyncTable[];
  cursors: Record<string, string>;
  /** Terminal receipts are public only after this complete run is applied. */
  digests?: Record<string, string>;
  /** Frozen previous receipts used to validate this run, including restart. */
  requestDigests?: Record<string, string>;
  fullReplayedAt?: number;
  contentApplied?: boolean;
};

function getGeneralSyncStateKey(authScope: string): string {
  return `__general_v1__:${authScope.slice("auth:".length)}`;
}

async function getGeneralSyncCheckpoint(
  authScope: string,
): Promise<GeneralSyncCheckpoint | null> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, getGeneralSyncStateKey(authScope)));
  const raw = rows[0]?.cursor;
  if (!raw) return null;
  const parsed = parseJsonRecord<Partial<GeneralSyncCheckpoint> | null>(raw, null);
  if (!parsed || parsed.version !== GENERAL_SYNC_PAGINATION_VERSION) return null;
  if (
    parsed.state != null
    && parsed.state !== "in_progress"
    && parsed.state !== "complete"
  ) {
    return null;
  }
  if (
    !Array.isArray(parsed.pendingTables)
    || parsed.pendingTables.some(
      (table) => !TABLES.includes(table as SyncTable),
    )
  ) {
    return null;
  }
  // Completion markers written before the late-commit safeguard did not
  // record whether their wall-clock watermark was safe.  Treat those rows as
  // legacy state and bootstrap afresh rather than trusting a value that may
  // have skipped an in-flight transaction.
  if (
    parsed.state === "complete"
    && !Object.prototype.hasOwnProperty.call(parsed, "watermark")
  ) {
    return null;
  }
  const pendingTables = parsed.pendingTables as SyncTable[];
  const cursors = parsed.cursors && typeof parsed.cursors === "object"
    ? Object.fromEntries(
        Object.entries(parsed.cursors).filter(
          ([table, cursor]) =>
            TABLES.includes(table as SyncTable) && typeof cursor === "string",
        ),
      )
    : {};
  return {
    version: GENERAL_SYNC_PAGINATION_VERSION,
    state: parsed.state === "complete" ? "complete" : "in_progress",
    since: typeof parsed.since === "string" ? parsed.since : null,
    serverTime: typeof parsed.serverTime === "string" ? parsed.serverTime : null,
    watermark:
      typeof parsed.watermark === "string" ? parsed.watermark : null,
    snapshotToken:
      typeof parsed.snapshotToken === "string" ? parsed.snapshotToken : null,
    pendingTables,
    cursors,
    digests: validGeneralDigests(parsed.digests, TABLES),
    requestDigests: validGeneralDigests(parsed.requestDigests, TABLES),
    fullReplayedAt: typeof parsed.fullReplayedAt === "number" && Number.isFinite(parsed.fullReplayedAt)
      ? parsed.fullReplayedAt : undefined,
    contentApplied: typeof parsed.contentApplied === "boolean" ? parsed.contentApplied : undefined,
  };
}

async function saveGeneralSyncCheckpoint(
  authScope: string,
  checkpoint: GeneralSyncCheckpoint,
): Promise<void> {
  const db = getDb();
  const tableName = getGeneralSyncStateKey(authScope);
  await db
    .insert(schema.syncState)
    .values({
      tableName,
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: JSON.stringify(checkpoint),
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: JSON.stringify(checkpoint) },
    });
}

async function markGeneralSyncCheckpointComplete(
  authScope: string,
  serverTime: string,
  watermark: string | null,
  receipt: Pick<GeneralSyncCheckpoint, "digests" | "fullReplayedAt"> = {},
): Promise<void> {
  const db = getDb();
  const tableName = getGeneralSyncStateKey(authScope);
  // Keep a durable completion marker instead of clearing the row.  A missing
  // marker means this account may still carry a legacy one-shot watermark that
  // truncated a large pull; the first v1 run must therefore bootstrap from
  // since=null. Later runs keep the server's safe watermark (including null)
  // and may reuse only the content receipts from this completed projection.
  const marker = JSON.stringify({
    version: GENERAL_SYNC_PAGINATION_VERSION,
    state: "complete",
    since: null,
    serverTime,
    watermark,
    snapshotToken: null,
    pendingTables: [],
    cursors: {},
    ...receipt,
  });
  await db
    .insert(schema.syncState)
    .values({
      tableName,
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: marker,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: marker },
    });
}

// v2 は、旧実装が5,000件で打ち切ったまま進めたDocs同期時刻を一度だけ無効化する。
// 一般テーブルの同期時刻とは分離し、Docs全ページ完了後にだけ更新する。
function getDocsScopeSuffix(scopeId?: string): string {
  return scopeId ? `:scope:${scopeId}` : "";
}

/** Canonical Docs scope identity.  A project can share its owner's library,
 * so workspace/library id alone is never a sufficient local key. */
function getDocsScopeKey(scopeId?: string, projectId?: string | null): string {
  const library = scopeId || "personal";
  return `${library}|project:${projectId || ""}`;
}

function getDocsScopeProjectSuffix(projectId?: string | null): string {
  return projectId ? `:project:${projectId}` : "";
}

function getDocsSyncStateKey(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `__docs_v2__:${authScope.slice("auth:".length)}${getDocsScopeSuffix(scopeId)}${getDocsScopeProjectSuffix(projectId)}`;
}

// authScope 単位で保持する Docs 権威 digest の syncState.tableName プレフィクス。
function getDocsDigestPrefix(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `docs_digest:v2:${authScope.slice("auth:".length)}${getDocsScopeSuffix(scopeId)}${getDocsScopeProjectSuffix(projectId)}:`;
}

function getDocsDigestKey(
  authScope: string,
  table: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `${getDocsDigestPrefix(authScope, scopeId, projectId)}${table}`;
}

function getDocsScopeDigestKey(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `docs_scope:v2:${authScope.slice("auth:".length)}${getDocsScopeSuffix(scopeId)}${getDocsScopeProjectSuffix(projectId)}`;
}

/** Scope ACL revisions are persisted separately from row/entity digests. */
function getDocsScopeRevisionKey(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `docs_scope_revision:v2:${authScope.slice("auth:".length)}${getDocsScopeSuffix(scopeId)}${getDocsScopeProjectSuffix(projectId)}`;
}

// Docs の authoritative_scope_id（現在のワークスペース）を端末側へ保持する。
// listPages はローカル先読みを行うため、旧ワークスペースの残骸を表示しないよう
// 現在の認証スコープと組み合わせて利用する。
function getDocsWorkspaceKey(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `docs_workspace:v2:${authScope.slice("auth:".length)}${getDocsScopeSuffix(scopeId)}${getDocsScopeProjectSuffix(projectId)}`;
}

function getDocsScopesKey(authScope: string): string {
  return `docs_scopes:v2:${authScope.slice("auth:".length)}`;
}

function docsStagingAvailable(): boolean {
  return Boolean(schema.docsSyncRuns && schema.docsSyncStaging && docsSqliteAsyncAvailable());
}

function parseJsonRecord<T>(value: unknown, fallback: T): T {
  if (typeof value !== "string") return (value as T) ?? fallback;
  try {
    return (JSON.parse(value) as T) ?? fallback;
  } catch {
    return fallback;
  }
}

type RawDocsSyncRunRow = {
  run_id: string;
  auth_scope: string;
  scope_key: string;
  scope_id: string | null;
  project_id: string | null;
  snapshot_token: string | null;
  scope_revision: string | null;
  scope_digest: string | null;
  server_time: string | null;
  cursor_json: unknown;
  pending_json: unknown;
  digest_json: unknown;
  authoritative_json: unknown;
  scopes_json: unknown;
  force: unknown;
  state: string;
  created_at: string;
  updated_at: string;
};

function deserializeDocsSyncRunRow(row: RawDocsSyncRunRow): DocsSyncRunRow {
  return {
    runId: row.run_id,
    authScope: row.auth_scope,
    scopeKey: row.scope_key,
    scopeId: row.scope_id,
    projectId: row.project_id,
    snapshotToken: row.snapshot_token,
    scopeRevision: row.scope_revision,
    scopeDigest: row.scope_digest,
    serverTime: row.server_time,
    cursorJson: parseJsonRecord<Record<string, string>>(row.cursor_json, {}),
    pendingJson: parseJsonRecord<SyncTable[]>(row.pending_json, []),
    digestJson: parseJsonRecord<Record<string, string>>(row.digest_json, {}),
    authoritativeJson: parseJsonRecord<DocsSyncAuthoritative>(row.authoritative_json, {}),
    scopesJson: row.scopes_json == null
      ? null
      : parseJsonRecord<DocsSyncScope[]>(row.scopes_json, []),
    force: row.force === true || row.force === 1 || row.force === "1",
    state: row.state as DocsSyncRunRow["state"],
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

async function getDocsSyncRun(
  runId: string,
  authScope: string,
): Promise<DocsSyncRunRow | null> {
  if (!docsStagingAvailable()) return null;
  const row = await withDocsExclusiveTransaction((tx) =>
    tx.getFirstAsync<RawDocsSyncRunRow>(
      "SELECT * FROM docs_sync_runs WHERE run_id = ? AND auth_scope = ?",
      runId,
      authScope,
    ),
  );
  return row ? deserializeDocsSyncRunRow(row) : null;
}

async function getActiveDocsSyncRun(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
  force?: boolean,
): Promise<DocsSyncRunRow | null> {
  if (!docsStagingAvailable()) return null;
  const rows = await withDocsExclusiveTransaction(async (tx) => {
    const params: unknown[] = [authScope, "downloading", "ready"];
    let sql = `
      SELECT * FROM docs_sync_runs
      WHERE auth_scope = ? AND state IN (?, ?)
    `;
    if (scopeId != null) {
      sql += " AND scope_key = ?";
      params.push(getDocsScopeKey(scopeId, projectId));
    } else if (projectId == null) {
      sql += " AND project_id IS NULL";
    } else {
      sql += " AND project_id = ?";
      params.push(projectId);
    }
    if (force !== undefined) {
      sql += " AND force = ?";
      params.push(encodeDocsBoolean(force));
    }
    sql += " ORDER BY updated_at ASC";
    return tx.getAllAsync<RawDocsSyncRunRow>(sql, ...params);
  });
  const mappedRows = rows.map(deserializeDocsSyncRunRow);
  if (scopeId == null && projectId == null) {
    // A non-project shared library can coexist with the personal root.  The
    // normalized personal run carries its concrete scopeId after page one,
    // so distinguish it by the authoritative owner scope metadata instead of
    // blindly taking the first project_id=NULL run.
    return mappedRows.find((candidate) => {
      const scopes = parseJsonRecord<DocsSyncScope[]>(candidate.scopesJson, []);
      const ownerScopes = scopes.filter(
        (scope) =>
          scope.workspace_id === candidate.scopeId
          && scope.project_id == null,
      );
      // A first-page personal run starts with the historical placeholder key.
      // A run with that key is safe before the server reveals a library id;
      // once it has a concrete id, require matching personal-owner metadata so
      // a normalized shared run cannot be resumed as root.
      if (candidate.scopeKey === getDocsScopeKey(undefined, null)) {
        if (candidate.scopeId == null) return true;
        return ownerScopes.length === 1
          && ownerScopes[0].source === "personal"
          && ownerScopes[0].access === "owner";
      }
      if (!candidate.scopeId || candidate.scopeKey !== getDocsScopeKey(candidate.scopeId, null)) {
        return false;
      }
      // A complete scope set may contain a shared projection for the same
      // library id.  Resume only when that concrete candidate is uniquely
      // identified as the personal owner scope; a broad `some()` would mix a
      // shared run into root discovery after an auth/scope switch.
      return ownerScopes.length === 1
        && ownerScopes[0].source === "personal"
        && ownerScopes[0].access === "owner";
    }) ?? null;
  }
  return mappedRows[0] ?? null;
}

async function createDocsSyncRun(
  authScope: string,
  scopeId: string | undefined,
  projectId: string | null | undefined,
  force: boolean,
  assertActive?: () => void,
): Promise<DocsSyncRunRow | null> {
  if (!docsStagingAvailable()) return null;
  assertActive?.();
  const now = new Date().toISOString();
  const runId = randomId();
  const values = {
    runId,
    authScope,
    scopeKey: getDocsScopeKey(scopeId, projectId),
    scopeId: scopeId ?? null,
    projectId: projectId ?? null,
    snapshotToken: null,
    scopeRevision: null,
    scopeDigest: null,
    serverTime: null,
    cursorJson: {} as Record<string, string>,
    pendingJson: [...DOCS_TABLES],
    digestJson: {} as Record<string, string>,
    authoritativeJson: {} as DocsSyncAuthoritative,
    scopesJson: null,
    force,
    state: "downloading",
    createdAt: now,
    updatedAt: now,
  };
  await withDocsExclusiveTransaction(async (tx) => {
    assertActive?.();
    await tx.runAsync(
      `INSERT INTO docs_sync_runs(
        run_id, auth_scope, scope_key, scope_id, project_id,
        snapshot_token, scope_revision, scope_digest, server_time,
        cursor_json, pending_json, digest_json, authoritative_json, scopes_json,
        force, state, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      values.runId,
      values.authScope,
      values.scopeKey,
      values.scopeId,
      values.projectId,
      values.snapshotToken,
      values.scopeRevision,
      values.scopeDigest,
      values.serverTime,
      encodeDocsJson(values.cursorJson),
      encodeDocsJson(values.pendingJson),
      encodeDocsJson(values.digestJson),
      encodeDocsJson(values.authoritativeJson),
      encodeDocsJson(values.scopesJson),
      encodeDocsBoolean(values.force),
      values.state,
      values.createdAt,
      values.updatedAt,
    );
    assertActive?.();
  });
  return values as DocsSyncRunRow;
}

async function deleteDocsSyncRun(
  runId: string,
  authScope: string,
  assertActive?: () => void,
): Promise<void> {
  if (!docsStagingAvailable()) return;
  assertActive?.();
  await withDocsExclusiveTransaction(async (tx) => {
    assertActive?.();
    await tx.runAsync(
      "DELETE FROM docs_sync_staging WHERE run_id = ? AND auth_scope = ?",
      runId,
      authScope,
    );
    assertActive?.();
    await tx.runAsync(
      "DELETE FROM docs_sync_runs WHERE run_id = ? AND auth_scope = ?",
      runId,
      authScope,
    );
    assertActive?.();
  });
}

function docsEntityKeyForStage(
  table: SyncTable,
  value: Record<string, unknown>,
): string | null {
  if (table === "knowledge_node_supertags") {
    return value.node_id != null && value.supertag_id != null
      ? `${String(value.node_id)}:${String(value.supertag_id)}`
      : null;
  }
  if (table === "knowledge_supertag_fields") {
    return value.supertag_id != null && value.field_id != null
      ? `${String(value.supertag_id)}:${String(value.field_id)}`
      : null;
  }
  if (table === "knowledge_field_values") {
    return value.node_id != null && value.field_id != null
      ? `${String(value.node_id)}:${String(value.field_id)}`
      : null;
  }
  return value.id == null ? null : String(value.id);
}

async function persistDocsPage(
  run: DocsSyncRunRow,
  authScope: string,
  scopeId: string | undefined,
  projectId: string | null | undefined,
  response: SyncPullResponse,
  pendingTables: SyncTable[],
  nextCursors: Record<string, string>,
  assertActive?: () => void,
): Promise<number> {
  if (!docsStagingAvailable()) return 0;
  assertActive?.();
  const matchingScope = (response.docs_scopes ?? []).find(
    (scope) =>
      (scope.project_id ?? null) === (projectId ?? null)
      && (scopeId == null || scope.workspace_id === scopeId),
  )
    ?? (scopeId == null && projectId == null
      ? (response.docs_scopes ?? []).find(
          (scope) => scope.source === "personal" && scope.project_id == null,
        )
      : undefined);
  const effectiveScopeId = scopeId ?? matchingScope?.workspace_id ?? run.scopeId ?? undefined;
  const effectiveProjectId = projectId ?? matchingScope?.project_id ?? run.projectId ?? null;
  const effectiveScopeKey = getDocsScopeKey(effectiveScopeId, effectiveProjectId);
  const previousDigests = parseJsonRecord<Record<string, string>>(
    run.digestJson,
    {},
  );
  const previousAuthoritative = parseJsonRecord<DocsSyncAuthoritative>(
    run.authoritativeJson,
    {},
  );
  const digestJson = { ...previousDigests };
  const authoritativeJson = { ...previousAuthoritative };
  let snapshotToken = run.snapshotToken;
  let scopeRevision = run.scopeRevision;
  let scopeDigest = run.scopeDigest;
  let serverTime = run.serverTime;
  let scopesJson = run.scopesJson;
  if (response.docs_snapshot_token) snapshotToken = response.docs_snapshot_token;
  if (response.docs_scope_revision) scopeRevision = response.docs_scope_revision;
  if (response.docs_scope_digest) scopeDigest = response.docs_scope_digest;
  // Preserve the first page boundary across resume/pages: ACKed creates
  // newer than this snapshot must not be reconciled as server deletions.
  if (response.server_time && !serverTime) serverTime = response.server_time;
  if (response.docs_scopes?.length) scopesJson = response.docs_scopes;
  let stagedRowsWritten = 0;
  for (const table of pendingTables) {
    const payload = response.tables[table];
    if (!payload) continue;
    const tableSnapshotToken = payload.docs_snapshot_token;
    const tableScopeRevision = payload.docs_scope_revision;
    if (tableSnapshotToken) snapshotToken = snapshotToken ?? tableSnapshotToken;
    if (tableScopeRevision) scopeRevision = scopeRevision ?? tableScopeRevision;
    if (payload.docs_snapshot_revision) {
      digestJson[table] = payload.docs_snapshot_revision;
    }
    if (payload.authoritative_digest) {
      digestJson[table] = payload.authoritative_digest;
    }
    if (payload.authoritative_ids != null || payload.authoritative_scope_id) {
      authoritativeJson[table] = {
        ids: payload.authoritative_ids,
        scopeId: payload.authoritative_scope_id,
        digest: payload.authoritative_digest,
      };
    }
  }

  const stagePayloads = async (
    table: SyncTable,
    values: unknown[] | undefined,
    isTombstone: boolean,
  ): Promise<void> => {
    if (!values?.length) return;
    for (let offset = 0; offset < values.length; offset += DOCS_STAGING_WRITE_BATCH_SIZE) {
      const batch = values.slice(offset, offset + DOCS_STAGING_WRITE_BATCH_SIZE);
      const rows = batch.flatMap((value) => {
        const object = value as Record<string, unknown>;
        const entityKey = docsEntityKeyForStage(table, object);
        if (!entityKey) return [];
        return [[
          run.runId,
          authScope,
          effectiveScopeKey,
          effectiveScopeId ?? null,
          effectiveProjectId,
          table,
          entityKey,
          JSON.stringify(object),
          encodeDocsBoolean(isTombstone),
        ] as const];
      });
      if (!rows.length) continue;
      await withDocsExclusiveTransaction(async (tx: DocsSqliteAsyncTransaction) => {
        assertActive?.();
        await executeDocsPreparedBatch(
          tx,
          `INSERT INTO docs_sync_staging(
             run_id, auth_scope, scope_key, scope_id, project_id,
             table_name, entity_key, payload_json, is_tombstone
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id, table_name, entity_key) DO UPDATE SET
             payload_json = excluded.payload_json,
             is_tombstone = excluded.is_tombstone`,
          rows,
        );
        assertActive?.();
      });
      stagedRowsWritten += rows.length;
    }
  };

  // Changes are staged before tombstones, preserving the previous conflict
  // rule when a server page contains both entries for the same key.  Each
  // bounded batch commits independently; a failed batch leaves the run cursor
  // unchanged and replaying the page is idempotent through the upsert key.
  for (const table of pendingTables) {
    const payload = response.tables[table];
    if (!payload) continue;
    await stagePayloads(table, payload.changes, false);
  }
  for (const table of pendingTables) {
    const payload = response.tables[table];
    if (!payload) continue;
    await stagePayloads(table, payload.tombstones, true);
  }

  const updatedAt = new Date().toISOString();
  const nextPending = Object.keys(nextCursors);
  await withDocsExclusiveTransaction(async (tx) => {
    assertActive?.();
    await tx.runAsync(
      `UPDATE docs_sync_runs SET
         scope_key = ?, scope_id = ?, project_id = ?,
         snapshot_token = ?, scope_revision = ?, scope_digest = ?, server_time = ?,
         cursor_json = ?, pending_json = ?, state = ?,
         digest_json = ?, authoritative_json = ?, scopes_json = ?, updated_at = ?
       WHERE run_id = ? AND auth_scope = ?`,
      effectiveScopeKey,
      effectiveScopeId ?? null,
      effectiveProjectId,
      snapshotToken,
      scopeRevision,
      scopeDigest,
      serverTime,
      encodeDocsJson(nextCursors),
      encodeDocsJson(nextPending),
      nextPending.length ? "downloading" : "ready",
      encodeDocsJson(digestJson),
      encodeDocsJson(authoritativeJson),
      encodeDocsJson(scopesJson),
      updatedAt,
      run.runId,
      authScope,
    );
    assertActive?.();
  });
  // Keep the in-memory run used by the page loop in lockstep with the row we
  // just persisted.  A pull may span many pages; retaining the original
  // run object would make the next page overwrite accumulated digests and
  // authoritative IDs with only the current page's metadata.
  Object.assign(run, {
    scopeKey: effectiveScopeKey,
    scopeId: effectiveScopeId ?? null,
    projectId: effectiveProjectId,
    snapshotToken,
    scopeRevision,
    scopeDigest,
    serverTime,
    cursorJson: nextCursors,
    pendingJson: nextPending,
    state: nextPending.length ? "downloading" : "ready",
    digestJson,
    authoritativeJson,
    scopesJson,
    updatedAt,
  });
  return stagedRowsWritten;
}

async function getSavedDocsScopeDigest(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): Promise<string | null> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, getDocsScopeDigestKey(authScope, scopeId, projectId)));
  return rows[0]?.cursor ?? null;
}

async function getSavedDocsScopeRevision(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): Promise<string | null> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, getDocsScopeRevisionKey(authScope, scopeId, projectId)));
  return rows[0]?.cursor ?? null;
}

async function saveDocsScopeDigest(
  authScope: string,
  digest: string,
  scopeId?: string,
  projectId?: string | null,
  assertActive?: () => void,
): Promise<void> {
  assertActive?.();
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName: getDocsScopeDigestKey(authScope, scopeId, projectId),
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: digest,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: digest },
    });
  assertActive?.();
}

/** 保存済みの Docs digest を一括読みし、次回 pull でエコーする形へ整える。 */
async function getSavedDocsDigests(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): Promise<Record<string, string>> {
  const db = getDb();
  const prefix = getDocsDigestPrefix(authScope, scopeId, projectId);
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(like(schema.syncState.tableName, `${prefix}%`));
  const digests: Record<string, string> = {};
  for (const row of rows) {
    // drizzle の like ヘルパは ESCAPE 句を持たず、prefix 内の `_`/`%`（将来 scope に
    // 含まれ得る）が LIKE ワイルドカードとして過剰マッチしうる。JS 側で厳密な
    // 前方一致に絞り、他 scope や別種 tableName の混入を防ぐ。
    if (!row.tableName.startsWith(prefix)) continue;
    if (row.cursor) {
      digests[row.tableName.slice(prefix.length)] = row.cursor;
    }
  }
  return digests;
}

/** 応答の authoritative_digest を cursor 列へ保存する（適用完了後のみ呼ぶ）。 */
async function saveDocsDigest(
  authScope: string,
  table: string,
  digest: string | null | undefined,
  scopeId?: string,
  projectId?: string | null,
): Promise<void> {
  if (!digest) return;
  const tableName = getDocsDigestKey(authScope, table, scopeId, projectId);
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName,
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: digest,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: digest },
    });
}

async function getLastPulledAt(
  authScope: string,
  docs = false,
  docsScopeId?: string,
  docsProjectId?: string | null,
): Promise<string | null> {
  const tableName = docs
    ? getDocsSyncStateKey(authScope, docsScopeId, docsProjectId)
    : getSyncStateKey(authScope);
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, tableName));
  return rows[0]?.lastPulledAt ?? null;
}

async function setLastPulledAt(
  authScope: string,
  value: string | null,
  docs = false,
  docsScopeId?: string,
  docsProjectId?: string | null,
  assertActive?: () => void,
): Promise<void> {
  assertActive?.();
  const tableName = docs
    ? getDocsSyncStateKey(authScope, docsScopeId, docsProjectId)
    : getSyncStateKey(authScope);
  const db = getDb();
  await runBackgroundSqliteWrite(() => {
    assertActive?.();
    return db
      .insert(schema.syncState)
      .values({
        tableName,
        lastPulledAt: value,
        lastPushedAt: null,
        cursor: null,
      })
      .onConflictDoUpdate({
        target: schema.syncState.tableName,
        set: { lastPulledAt: value },
      });
  });
  assertActive?.();
}

async function applyPullResponse(
  authScope: string,
  response: SyncPullResponse,
  context: SyncExecutionContext,
  scopeId?: string,
  projectId?: string | null,
  options: { forceDocs?: boolean; stageDocs?: boolean } = {},
): Promise<void> {
  assertSyncExecutionActive(context);
  // A Docs-only page has no general writes to enqueue.
  if (TABLES.some((table) => generalTableNeedsApply(response.tables[table]))) {
  // Everything that needs a token read is prepared before the write unit.
  // The unit itself is synchronous: one transaction per table, using the
  // InWriter functions of the same repositories the async wrappers delegate to.
  const projects = response.tables.projects;
  const projectsApply = !!projects && generalTableNeedsApply(projects);

  const tasks = response.tables.tasks;
  const tasksApply = !!tasks && generalTableNeedsApply(tasks);
  const taskLedgerKey = tasksApply
    && (tasks.changes.length || tasks.tombstones.length)
    ? await prepareTaskTombstoneLedgerKey()
    : null;

  const occurrences = response.tables.task_occurrences;
  const occurrencesApply = !!occurrences && generalTableNeedsApply(occurrences);
  const occurrencePrep = occurrencesApply
    && (occurrences.changes.length || occurrences.tombstones.length)
    ? await prepareOccurrenceApply()
    : null;

  const timeEntries = response.tables.time_entries;
  const timeEntriesApply = !!timeEntries && generalTableNeedsApply(timeEntries);
  const timeEntryPrep = timeEntriesApply
    && (timeEntries.changes.length || timeEntries.tombstones.length)
    ? await prepareTimeEntryApply()
    : null;

  const sessions = response.tables.conversation_sessions;
  const sessionsApply = !!sessions && generalTableNeedsApply(sessions);
  const sessionChanges = sessionsApply
    ? sessions.changes as unknown as ConversationSession[]
    : [];
  const pendingPinSessionIds = sessionChanges.length
    ? await prepareRemoteConversationSessions(sessionChanges)
    : new Set<string>();
  const pendingPinOutboxScope = sessionChanges.length
    ? await resolveAuthScope()
    : null;

  const messages = response.tables.conversation_messages;
  const messagesApply = !!messages && generalTableNeedsApply(messages);
  const recordTables = response.tables.record_tables;
  const recordTablesApply = !!recordTables && generalTableNeedsApply(recordTables);
  const recordFields = response.tables.record_fields;
  const recordFieldsApply = !!recordFields && generalTableNeedsApply(recordFields);
  const recordRows = response.tables.record_rows;
  const recordRowsApply = !!recordRows && generalTableNeedsApply(recordRows);

  const rescheduleTasks = await runBackgroundSqliteWrite((writer) => {
    // The unit may have waited behind foreground work, and the preparation
    // awaits above ran before it was queued: re-check before the first SQL.
    assertSyncExecutionActive(context);
    let reschedule = false;
    if (projectsApply) {
      getDb().transaction((tx) => {
        applyRemoteProjectsInWriter(writer, tx, projects.changes as unknown as Project[]);
        applyProjectTombstonesInWriter(writer, tx, projects.tombstones);
      });
    }

    if (tasksApply) {
      assertSyncExecutionActive(context);
      if (taskLedgerKey !== null) {
        reschedule = getDb().transaction((tx) => {
          const applied = applyRemoteTasksInWriter(
            writer, tx, tasks.changes as unknown as Task[], taskLedgerKey);
          const tombstoned = applyTaskTombstonesInWriter(
            writer, tx, tasks.tombstones, taskLedgerKey);
          return applied || tombstoned;
        }) || reschedule;
      }
    }

    if (occurrencesApply) {
      assertSyncExecutionActive(context);
      if (occurrencePrep) {
        reschedule = getDb().transaction((tx) => {
          const applied = applyRemoteOccurrencesInWriter(
            writer, tx, occurrences.changes as unknown as TaskOccurrence[], occurrencePrep);
          const tombstoned = applyOccurrenceTombstonesInWriter(
            writer, tx, occurrences.tombstones, occurrencePrep.ledgerKey);
          return applied || tombstoned;
        }) || reschedule;
      }
    }

    if (timeEntriesApply) {
      assertSyncExecutionActive(context);
      if (timeEntryPrep) {
        getDb().transaction((tx) => {
          applyRemoteTimeEntriesInWriter(
            writer, tx, timeEntries.changes as unknown as TimeEntry[], timeEntryPrep);
          applyTimeEntryTombstonesInWriter(
            writer, tx, timeEntries.tombstones, timeEntryPrep.ledgerKey);
        });
      }
    }

    if (sessionsApply) {
      assertSyncExecutionActive(context);
      getDb().transaction((tx) => {
        applyRemoteConversationSessionsInWriter(
          writer, tx, sessionChanges, pendingPinSessionIds, pendingPinOutboxScope);
        applyConversationSessionTombstonesInWriter(writer, tx, sessions.tombstones);
        reconcileConversationSessionsWithServerInWriter(
          writer, tx, sessions.authoritative_ids);
      });
    }

    if (messagesApply) {
      assertSyncExecutionActive(context);
      getDb().transaction((tx) => {
        applyRemoteConversationMessagesInWriter(
          writer, tx, messages.changes as unknown as ConversationMessage[]);
        applyConversationMessageTombstonesInWriter(writer, tx, messages.tombstones);
      });
    }

    if (recordTablesApply) {
      assertSyncExecutionActive(context);
      getDb().transaction((tx) => {
        applyRemoteRecordTablesInWriter(
          writer, tx, recordTables.changes as unknown as RecordTable[]);
        applyRecordTableTombstonesInWriter(writer, tx, recordTables.tombstones);
      });
    }

    if (recordFieldsApply) {
      assertSyncExecutionActive(context);
      getDb().transaction((tx) => {
        applyRemoteRecordFieldsInWriter(
          writer, tx, recordFields.changes as unknown as RecordField[]);
        applyRecordFieldTombstonesInWriter(writer, tx, recordFields.tombstones);
      });
    }

    if (recordRowsApply) {
      assertSyncExecutionActive(context);
      getDb().transaction((tx) => {
        applyRemoteRecordRowsInWriter(
          writer, tx, recordRows.changes as unknown as RecordRow[]);
        applyRecordRowTombstonesInWriter(writer, tx, recordRows.tombstones);
      });
    }
    return reschedule;
  });
  if (rescheduleTasks) void rescheduleLocalTaskNotificationsFromCache();
  }

  // ---------- Docs ----------
  // A staged Docs pull is committed by promoteDocsSyncRun only after all
  // pages, cursors and ACL metadata validate.  General tables above retain
  // their existing incremental apply semantics.
  if (options.stageDocs) return;
  // digest 一致時、サーバは changes を空にし authoritative_ids / authoritative_scope_id を
  // 省略する。changes が空なら apply を、authoritative_ids が無いなら reconcile を呼ばない。
  // 各テーブルの適用と reconcile が例外なく完了した後にだけ digest を保存する。
  const docsNodes = response.tables.knowledge_nodes;
  if (docsNodes) {
    assertSyncExecutionActive(context);
    if (docsNodes.changes?.length) {
      const nodeChanges = docsNodes.changes as unknown as DocsNode[];
      if (options.forceDocs) {
        await applyRemoteDocsNodes(nodeChanges, { force: true });
      } else {
        await applyRemoteDocsNodes(nodeChanges);
      }
      assertSyncExecutionActive(context);
    }
    await applyDocsNodeTombstones(docsNodes.tombstones);
    assertSyncExecutionActive(context);
    if (docsNodes.authoritative_ids != null) {
      await reconcileDocsNodesWithServer(
        docsNodes.authoritative_ids,
        docsNodes.authoritative_scope_id,
      );
      assertSyncExecutionActive(context);
    }
    if (docsNodes.authoritative_scope_id) {
      await saveDocsWorkspaceId(
        authScope,
        docsNodes.authoritative_scope_id,
        scopeId,
        projectId,
      );
      assertSyncExecutionActive(context);
    }
    await saveDocsDigest(
      authScope,
      "knowledge_nodes",
      docsNodes.authoritative_digest,
      scopeId,
      projectId,
    );
    assertSyncExecutionActive(context);
  }

  const docsSupertags = response.tables.knowledge_supertags;
  if (docsSupertags) {
    assertSyncExecutionActive(context);
    if (docsSupertags.changes?.length) {
      await applyRemoteDocsSupertags(
        docsSupertags.changes as unknown as DocsSupertag[],
      );
    }
    assertSyncExecutionActive(context);
    await reconcileDocsSupertagsWithServer(
      docsSupertags.authoritative_ids,
      docsSupertags.authoritative_scope_id,
    );
    assertSyncExecutionActive(context);
    await saveDocsDigest(
      authScope,
      "knowledge_supertags",
      docsSupertags.authoritative_digest,
      scopeId,
      projectId,
    );
    assertSyncExecutionActive(context);
  }

  const docsFields = response.tables.knowledge_fields;
  if (docsFields) {
    assertSyncExecutionActive(context);
    if (docsFields.changes?.length) {
      await applyRemoteDocsFields(docsFields.changes as unknown as DocsField[]);
      assertSyncExecutionActive(context);
    }
    await reconcileDocsFieldsWithServer(
      docsFields.authoritative_ids,
      docsFields.authoritative_scope_id,
    );
    assertSyncExecutionActive(context);
    await saveDocsDigest(
      authScope,
      "knowledge_fields",
      docsFields.authoritative_digest,
      scopeId,
      projectId,
    );
    assertSyncExecutionActive(context);
  }

  const docsFieldValues = response.tables.knowledge_field_values;
  if (docsFieldValues) {
    assertSyncExecutionActive(context);
    if (
      docsFieldValues.changes?.length ||
      docsFieldValues.authoritative_ids != null
    ) {
      await applyRemoteDocsFieldValues(
        docsFieldValues.changes as unknown as DocsFieldValue[],
        docsFieldValues.authoritative_ids,
      );
      assertSyncExecutionActive(context);
    }
    await saveDocsDigest(
      authScope,
      "knowledge_field_values",
      docsFieldValues.authoritative_digest,
      scopeId,
      projectId,
    );
    assertSyncExecutionActive(context);
  }

  // 関連表は削除を取りこぼさないよう authoritative_ids で reconcile（省略時は据え置き）。
  const docsNodeSupertags = response.tables.knowledge_node_supertags;
  if (docsNodeSupertags) {
    assertSyncExecutionActive(context);
    if (
      docsNodeSupertags.changes?.length ||
      docsNodeSupertags.authoritative_ids != null
    ) {
      await applyRemoteDocsNodeSupertags(
        docsNodeSupertags.changes as unknown as DocsNodeSupertag[],
        docsNodeSupertags.authoritative_ids,
      );
      assertSyncExecutionActive(context);
    }
    await saveDocsDigest(
      authScope,
      "knowledge_node_supertags",
      docsNodeSupertags.authoritative_digest,
      scopeId,
      projectId,
    );
    assertSyncExecutionActive(context);
  }

  const docsSupertagFields = response.tables.knowledge_supertag_fields;
  if (docsSupertagFields) {
    assertSyncExecutionActive(context);
    if (
      docsSupertagFields.changes?.length ||
      docsSupertagFields.authoritative_ids != null
    ) {
      await applyRemoteDocsSupertagFields(
        docsSupertagFields.changes as unknown as DocsSupertagField[],
        docsSupertagFields.authoritative_ids,
      );
      assertSyncExecutionActive(context);
    }
    await saveDocsDigest(
      authScope,
      "knowledge_supertag_fields",
      docsSupertagFields.authoritative_digest,
      scopeId,
      projectId,
    );
    assertSyncExecutionActive(context);
  }

  const docsPlacements = response.tables.knowledge_node_placements;
  if (docsPlacements) {
    assertSyncExecutionActive(context);
    if (
      docsPlacements.changes?.length ||
      docsPlacements.authoritative_ids != null
    ) {
      await applyRemoteDocsPlacements(
        docsPlacements.changes as unknown as DocsNodePlacement[],
        docsPlacements.authoritative_ids,
      );
      assertSyncExecutionActive(context);
    }
    await saveDocsDigest(
      authScope,
      "knowledge_node_placements",
      docsPlacements.authoritative_digest,
      scopeId,
      projectId,
    );
    assertSyncExecutionActive(context);
  }

  const docsEdges = response.tables.knowledge_edges;
  if (docsEdges) {
    assertSyncExecutionActive(context);
    if (docsEdges.changes?.length || docsEdges.authoritative_ids != null) {
      await applyRemoteDocsEdges(
        docsEdges.changes as unknown as DocsEdge[],
        docsEdges.authoritative_ids,
      );
      assertSyncExecutionActive(context);
    }
    await saveDocsDigest(
      authScope,
      "knowledge_edges",
      docsEdges.authoritative_digest,
      scopeId,
      projectId,
    );
    assertSyncExecutionActive(context);
  }

}

function isDocsSnapshotChanged(error: unknown): boolean {
  const body = isApiHttpError(error) ? error.responseBody : "";
  return (
    isApiHttpError(error) &&
    (error.status === 400 || error.status === 409) &&
    /Docs (?:sync )?(?:snapshot|scope) (?:changed|revoked)/i.test(body)
  );
}

async function applyDocsPullOnce(
  authScope: string,
  docsSince: string | null,
  docsDigests: Record<string, string>,
  savedDocsScopeDigest: string | null,
  savedDocsScopeRevision: string | null,
  context: SyncExecutionContext,
  scopeId?: string,
  projectId?: string | null,
  options: {
    forceDocs?: boolean;
    onProgress?: DocsResyncProgressHandler;
    run?: DocsSyncRunRow | null;
    previousScopes?: DocsSyncScope[];
    diagnosticPromotionGeneration?: number;
  } = {},
): Promise<DocsSyncScope[]> {
  const persistedRun = options.run ?? null;
  let pendingTables = persistedRun
    ? parseJsonRecord<SyncTable[]>(persistedRun.pendingJson, [...DOCS_TABLES])
    : [...DOCS_TABLES];
  if (!pendingTables.length && persistedRun) pendingTables = [];
  let docsCursors = persistedRun
    ? parseJsonRecord<Record<string, string>>(persistedRun.cursorJson, {})
    : {};
  let docsServerTime: string | null = persistedRun?.serverTime ?? null;
  let responseDocsScopeDigest: string | null = persistedRun?.scopeDigest ?? null;
  let requestDocsScopeDigest = savedDocsScopeDigest;
  let snapshotToken = persistedRun?.snapshotToken ?? null;
  let scopeRevision = persistedRun?.scopeRevision ?? savedDocsScopeRevision ?? null;
  const seenCursorStates = new Set<string>();
  const authoritativeCounts = new Map<SyncTable, number>();
  const appliedCounts = new Map<SyncTable, number>();
  let stagedRowsWritten = 0;
  let discoveredScopes: DocsSyncScope[] = [];
  let page = 0;

  if (persistedRun?.state === "ready" && pendingTables.length === 0) {
    markDocsSubstage(context, "ready_resume", persistedRun);
  }

  while (pendingTables.length) {
    assertSyncExecutionActive(context);
    if (options.onProgress) {
      const hasAllCounts = DOCS_TABLES.every((table) =>
        authoritativeCounts.has(table),
      );
      const total = hasAllCounts
        ? DOCS_TABLES.reduce(
            (sum, table) => sum + (authoritativeCounts.get(table) ?? 0),
            0,
          )
        : null;
      const completed = DOCS_TABLES.reduce(
        (sum, table) => sum + (appliedCounts.get(table) ?? 0),
        0,
      );
      options.onProgress({
        phase: "downloading",
        completed: total == null ? completed : Math.min(completed, total),
        total,
        page: page + 1,
        stagedRowsWritten,
      });
    }
    const useStaging = Boolean(persistedRun && docsStagingAvailable());
    markDocsSubstage(context, "http_pull", persistedRun);
    const pullDiagnostic = beginPullDiagnostic(
      context.diagnostic,
      "docs",
      diagnosticScopeFor(scopeId, projectId),
      pendingTables,
      useStaging ? "staged" : "applied",
      options.diagnosticPromotionGeneration ?? null,
    );
    const docsResponse = await pullSync({
      since: docsSince,
      tables: pendingTables,
      ...(scopeId ? { docs_scope_id: scopeId } : {}),
      ...(projectId ? { project_id: projectId } : {}),
      docs_pagination: true,
      // Staging needs the authoritative ID set on the terminal page so clean
      // rows can be reconciled during atomic promotion.  This is intentionally
      // true even for force resync; force no longer means destructive delete.
      docs_reconcile: true,
      ...(snapshotToken ? { docs_snapshot_token: snapshotToken } : {}),
      ...(scopeRevision ? { docs_scope_revision: scopeRevision } : {}),
      ...(requestDocsScopeDigest
        ? { docs_scope_digest: requestDocsScopeDigest }
        : {}),
      ...(Object.keys(docsDigests).length
        ? { docs_digests: docsDigests }
        : {}),
      ...(Object.keys(docsCursors).length
        ? { docs_cursors: docsCursors }
        : {}),
      signal: context.controller.signal,
      ...(pullDiagnostic
        ? {
            requestId: pullDiagnostic.requestId,
            lifecycle: pullDiagnosticLifecycle(pullDiagnostic),
          }
        : {}),
    }).catch((error: unknown) => {
      finishPullDiagnosticFromError(pullDiagnostic, error);
      if (isApiConnectionError(error) || isApiTimeoutError(error)) {
        throw new DocsPullTransportError(error);
      }
      throw error;
    });
    assertSyncExecutionActive(context);
    if (docsResponse.docs_pagination_version !== 2) {
      throw new Error("サーバーがDocs分割同期に対応していません");
    }
    if (!docsResponse.docs_scope_digest) {
      throw new Error("サーバーがDocs可視範囲を返しませんでした");
    }
    if (
      snapshotToken &&
      docsResponse.docs_snapshot_token &&
      snapshotToken !== docsResponse.docs_snapshot_token
    ) {
      throw new Error("Docs同期snapshot tokenがページ間で変わりました");
    }
    if (
      scopeRevision &&
      docsResponse.docs_scope_revision &&
      scopeRevision !== docsResponse.docs_scope_revision
    ) {
      throw new Error("Docs同期scope revisionがページ間で変わりました");
    }
    snapshotToken = docsResponse.docs_snapshot_token ?? snapshotToken;
    scopeRevision = docsResponse.docs_scope_revision ?? scopeRevision;
    if (docsResponse.docs_scopes?.length) {
      discoveredScopes = docsResponse.docs_scopes;
    }
    if (
      responseDocsScopeDigest &&
      responseDocsScopeDigest !== docsResponse.docs_scope_digest
    ) {
      throw new Error("Docs可視範囲が同期中に変わりました");
    }
    responseDocsScopeDigest = docsResponse.docs_scope_digest;
    // Echo the server's immutable scope digest on continuation pages.  The
    // saved digest is only a first-page optimization; repeatedly sending a
    // stale value would force a full scan on every page.
    requestDocsScopeDigest = docsResponse.docs_scope_digest;
    docsServerTime ??= docsResponse.server_time;
    for (const table of pendingTables) {
      const count = docsResponse.tables[table]?.authoritative_count;
      if (typeof count === "number" && Number.isFinite(count)) {
        authoritativeCounts.set(table, Math.max(0, count));
      }
    }
    // A 200 response that omits a requested table is incomplete.  Do not
    // persist a ready cursor or promote staging from that payload; HTTP status
    // alone is not evidence that the Docs snapshot was applied.
    for (const table of pendingTables) {
      if (!docsResponse.tables[table]) {
        markDocsPromotion(
          context.diagnostic,
          diagnosticScopeFor(scopeId, projectId),
          "failed",
          options.diagnosticPromotionGeneration ?? null,
        );
        throw new Error(`Docs同期ページにテーブルがありません: ${table}`);
      }
    }
    // A first terminal root page with every table empty and no authoritative
    // count/IDs/digest is indistinguishable from a truncated 200 response.
    // Existing digest-resume pages are allowed to omit authority because their
    // prior validated snapshot remains the proof of membership.
    const allRequestedTablesEmpty = pendingTables.every((table) => {
      const payload = docsResponse.tables[table]!;
      return payload.changes.length === 0
        && payload.tombstones.length === 0
        && !payload.cursor;
    });
    const hasAuthoritativeMetadata = pendingTables.some((table) => {
      const payload = docsResponse.tables[table]!;
      return (typeof payload.authoritative_count === "number" && Number.isFinite(payload.authoritative_count))
        || payload.authoritative_ids != null
        || Boolean(payload.authoritative_digest)
        || Boolean(payload.docs_snapshot_revision);
    });
    if (
      page === 0
      && pendingTables.length === DOCS_TABLES.length
      && allRequestedTablesEmpty
      && !hasAuthoritativeMetadata
      && Object.keys(docsDigests).length === 0
    ) {
      markDocsPromotion(
        context.diagnostic,
        diagnosticScopeFor(scopeId, projectId),
        "failed",
        options.diagnosticPromotionGeneration ?? null,
      );
      throw new Error("Docs同期のauthoritative metadataがありません");
    }
    recordPullResponse(
      pullDiagnostic,
      docsResponse,
      pendingTables,
      useStaging ? "staged" : "applied",
    );
    const liveWriteStartedForPage = !useStaging
      && context.docsLiveWriteState
      && DOCS_TABLES.some((table) => generalTableNeedsApply(docsResponse.tables[table]));
    if (liveWriteStartedForPage) context.docsLiveWriteState!.started = true;
    try {
      await applyPullResponse(authScope, docsResponse, context, scopeId, projectId, {
        forceDocs: options.forceDocs,
        stageDocs: useStaging,
      });
    } finally {
      if (liveWriteStartedForPage) context.docsLiveWriteState!.started = false;
    }
    assertSyncExecutionActive(context);
    page += 1;
    for (const table of pendingTables) {
      const applied = docsResponse.tables[table]?.changes?.length ?? 0;
      appliedCounts.set(table, (appliedCounts.get(table) ?? 0) + applied);
    }
    const nextCursors: Record<string, string> = {};
    for (const table of pendingTables) {
      const cursor = docsResponse.tables[table]?.cursor;
      if (cursor) nextCursors[table] = cursor;
    }
    if (useStaging && persistedRun) {
      assertSyncExecutionActive(context);
      stagedRowsWritten += await persistDocsPage(
        persistedRun,
        authScope,
        scopeId,
        projectId,
        docsResponse,
        pendingTables,
        nextCursors,
        () => assertSyncExecutionActive(context),
      );
    }
    if (options.onProgress) {
      const hasAllCounts = DOCS_TABLES.every((table) =>
        authoritativeCounts.has(table),
      );
      const total = hasAllCounts
        ? DOCS_TABLES.reduce(
            (sum, table) => sum + (authoritativeCounts.get(table) ?? 0),
            0,
          )
        : null;
      const completed = DOCS_TABLES.reduce(
        (sum, table) => sum + (appliedCounts.get(table) ?? 0),
        0,
      );
      options.onProgress({
        phase: "downloading",
        completed: total == null ? completed : Math.min(completed, total),
        total,
        page,
        stagedRowsWritten,
      });
    }
    if (!Object.keys(nextCursors).length) break;

    const cursorState = JSON.stringify(nextCursors);
    if (seenCursorStates.has(cursorState)) {
      throw new Error("Docs同期cursorが進みませんでした");
    }
    seenCursorStates.add(cursorState);
    docsCursors = nextCursors;
    pendingTables = Object.keys(nextCursors) as SyncTable[];
  }

  if (docsServerTime && responseDocsScopeDigest) {
    assertSyncExecutionActive(context);
    const total = DOCS_TABLES.every((table) => authoritativeCounts.has(table))
      ? DOCS_TABLES.reduce(
          (sum, table) => sum + (authoritativeCounts.get(table) ?? 0),
          0,
        )
      : null;
    options.onProgress?.({
      phase: "finalizing",
      completed:
        total
        ?? DOCS_TABLES.reduce(
          (sum, table) => sum + (appliedCounts.get(table) ?? 0),
          0,
        ),
      total,
      page,
    });
    if (persistedRun && docsStagingAvailable()) {
      markDocsSubstage(context, "reload_ready_run", persistedRun);
      const currentRun = await getDocsSyncRun(persistedRun.runId, authScope);
      updateDocsRunDiagnostic(context, currentRun);
      assertSyncExecutionActive(context);
      if (!currentRun) throw new Error("Docs同期runが見つかりません");
      const authoritative = parseJsonRecord<DocsSyncAuthoritative>(
        currentRun.authoritativeJson,
        {},
      );
      // Only a non-empty, persisted `docs_scopes` array is authoritative.  A
      // legacy v2 response may omit the field entirely; falling back to the
      // previously saved scope set in that case would incorrectly interpret
      // every prior scope as revoked during atomic promotion.  Empty arrays
      // are likewise treated as an unknown projection (the server contract
      // promises a complete non-empty set for authenticated roots).
      const persistedScopes = parseJsonRecord<DocsSyncScope[] | null>(
        currentRun.scopesJson,
        null,
      );
      const hasAuthoritativeScopeSet = Boolean(
        Array.isArray(persistedScopes)
        && persistedScopes.length > 0
        && persistedScopes.every(
          (scope) => Boolean(
            scope
            && typeof scope.workspace_id === "string"
            && scope.workspace_id.length > 0
            && (scope.project_id == null || typeof scope.project_id === "string"),
          ),
        ),
      );
      const runScopes = hasAuthoritativeScopeSet
        ? persistedScopes!
        : discoveredScopes;
      // Root discovery pulls are keyed by the server's authoritative library,
      // not the local "personal" placeholder.  Persist that concrete
      // workspace key in the same transaction as promotion so listPages/read
      // filters never expose rows from an unrelated scope after restart.
      const personalRootScope = runScopes.find(
        (scope) =>
          (scope.project_id ?? null) === null
          && scope.source === "personal"
          && scope.access === "owner",
      );
      // A root run starts without a concrete library id.  Only the explicit
      // personal-owner scope may supply that identity; choosing the first
      // project_id=NULL shared scope would attach personal rows to a sibling
      // library.  The final fallbacks are retained only for pre-metadata
      // servers that return no docs_scopes at all.
      const finalizedWorkspaceId =
        scopeId
        ?? personalRootScope?.workspace_id
        ?? (runScopes.length
          ? undefined
          : currentRun.scopeId
            ?? authoritative.knowledge_nodes?.scopeId
            ?? authoritative.knowledge_supertags?.scopeId
            ?? authoritative.knowledge_fields?.scopeId);
      if (!finalizedWorkspaceId) {
        throw new Error("Docs personal scope is missing from authoritative projection");
      }
      const promotionScopeId = scopeId ?? currentRun.scopeId ?? finalizedWorkspaceId;
      const promotionProjectId = projectId ?? currentRun.projectId ?? null;
      // Membership/outbox isolation always uses the concrete library key.
      // Keep the legacy root digest/sync-state keys below (scopeId was
      // intentionally omitted before the composite rollout) so existing
      // devices do not re-download the same personal snapshot forever.
      const promotionScopeKey = getDocsScopeKey(
        finalizedWorkspaceId ?? promotionScopeId,
        promotionProjectId,
      );
      const stateScopeId = scopeId;
      const stateProjectId = projectId;
      let promotionTelemetry: Awaited<ReturnType<typeof promoteDocsSyncRun>>;
      try {
        markDocsSubstage(context, "promoting", currentRun);
        promotionTelemetry = await promoteDocsSyncRun({
          assertActive: () => assertSyncExecutionActive(context),
          runId: persistedRun.runId,
          authScope,
          scopeId: promotionScopeId,
          projectId: promotionProjectId,
          scopeKey: promotionScopeKey,
          snapshotServerTime: docsServerTime,
          force: options.forceDocs,
          authoritative,
          scopes: runScopes,
          finalize: {
            scopeDigest: responseDocsScopeDigest,
            serverTime: docsServerTime,
            digestByTable: parseJsonRecord<Record<string, string>>(
              currentRun.digestJson,
              {},
            ),
            scopeDigestKey: getDocsScopeDigestKey(authScope, stateScopeId, stateProjectId),
            scopeRevision,
            scopeRevisionKey: getDocsScopeRevisionKey(authScope, stateScopeId, stateProjectId),
            lastPulledKey: getDocsSyncStateKey(authScope, stateScopeId, stateProjectId),
            digestKeys: Object.fromEntries(
              Object.keys(
                parseJsonRecord<Record<string, string>>(currentRun.digestJson, {}),
              ).map((table) => [
                table,
                getDocsDigestKey(authScope, table, stateScopeId, stateProjectId),
              ]),
            ),
            scopesKey: hasAuthoritativeScopeSet
              ? getDocsScopesKey(authScope)
              : undefined,
            workspaceKey: finalizedWorkspaceId
              ? getDocsWorkspaceKey(authScope, finalizedWorkspaceId, promotionProjectId)
              : undefined,
            workspaceId: finalizedWorkspaceId,
          },
          scopeSet: options.previousScopes && hasAuthoritativeScopeSet
            ? {
                previousScopes: options.previousScopes,
                newScopes: runScopes,
              }
            : undefined,
        });
      } catch (error) {
        markDocsPromotion(
          context.diagnostic,
          diagnosticScopeFor(scopeId, projectId),
          "failed",
          options.diagnosticPromotionGeneration ?? null,
        );
        throw error;
      }
      markDocsPromotion(
        context.diagnostic,
        diagnosticScopeFor(scopeId, projectId),
        "promoted",
        options.diagnosticPromotionGeneration ?? null,
      );
      if (promotionTelemetry) {
        options.onProgress?.({
          phase: "finalizing",
          completed:
            total
            ?? DOCS_TABLES.reduce(
              (sum, table) => sum + (appliedCounts.get(table) ?? 0),
              0,
            ),
          total,
          page,
          stagedRowsWritten,
          stagedRowsRead: promotionTelemetry.rowsRead,
          stagedPromotionBatches: promotionTelemetry.batches,
          stagedPromotionMaxBatchSize: promotionTelemetry.maxBatchSize,
        });
      }
    } else {
      assertSyncExecutionActive(context);
      await saveDocsScopeDigest(
        authScope,
        responseDocsScopeDigest,
        scopeId,
        projectId,
        () => assertSyncExecutionActive(context),
      );
      if (scopeRevision) {
        assertSyncExecutionActive(context);
        await saveDocsScopeRevision(
          authScope,
          scopeRevision,
          scopeId,
          projectId,
          () => assertSyncExecutionActive(context),
        );
      }
      assertSyncExecutionActive(context);
      await setLastPulledAt(
        authScope,
        docsServerTime,
        true,
        scopeId,
        projectId,
        () => assertSyncExecutionActive(context),
      );
    }
  }
  return discoveredScopes;
}

async function resetGeneralSyncCheckpoint(authScope: string): Promise<void> {
  const db = getDb();
  const tableName = getGeneralSyncStateKey(authScope);
  await db
    .insert(schema.syncState)
    .values({
      tableName,
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: null,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: null },
    });
}

function isGeneralCheckpointRestartError(error: unknown): boolean {
  return Boolean(
    isApiHttpError(error)
    && (error.status === 400 || error.status === 409)
    && /general sync/i.test(
      `${error.message} ${error.responseBody}`,
    ),
  );
}

async function applyGeneralPullOnce(
  authScope: string,
  context: SyncExecutionContext,
): Promise<boolean> {
  assertSyncExecutionActive(context);
  const saved = await getGeneralSyncCheckpoint(authScope);
  // A terminal checkpoint means every page was committed but the final
  // lastPulledAt write was interrupted.  Finalize without issuing another
  // network request; replaying the terminal page is also safe, but avoiding it
  // keeps process-restart recovery bounded and deterministic.
  if (
    saved
    && saved.state !== "complete"
    && saved.pendingTables.length === 0
    && saved.serverTime
  ) {
    await setLastPulledAt(authScope, saved.watermark);
    await markGeneralSyncCheckpointComplete(
      authScope,
      saved.serverTime,
      saved.watermark,
      { digests: saved.digests, fullReplayedAt: saved.fullReplayedAt },
    );
    return saved.contentApplied !== false;
  }

  // Never trust the legacy global watermark until one bounded v1 run has
  // completed.  The old one-shot endpoint advanced that watermark even when
  // its 5,000-row cap truncated the response, so using it here could make
  // omitted historical rows permanently invisible.
  const since = saved?.state === "complete"
    ? await getLastPulledAt(authScope)
    : saved
      ? saved.since
      : null;
  const hasCompletedMarker = saved?.state === "complete";
  let pendingTables = !hasCompletedMarker && saved?.pendingTables.length
    ? [...saved.pendingTables]
    : [...TABLES];
  let cursors = !hasCompletedMarker ? { ...(saved?.cursors ?? {}) } : {};
  let snapshotToken = !hasCompletedMarker ? saved?.snapshotToken ?? null : null;
  let serverTime = !hasCompletedMarker ? saved?.serverTime ?? null : null;
  let watermark = !hasCompletedMarker ? saved?.watermark ?? null : null;
  let watermarkInitialized = Boolean(
    saved
    && !hasCompletedMarker
    && Object.prototype.hasOwnProperty.call(saved, "watermark"),
  );
  const now = Date.now();
  const requestDigests = since != null ? {} : hasCompletedMarker
    ? reusableGeneralDigests(saved?.digests, TABLES, saved?.fullReplayedAt, now)
    : generalDigestsForRequest(saved?.requestDigests, TABLES);
  const digests = !hasCompletedMarker ? { ...(saved?.digests ?? {}) } : {};
  const fullReplayedAt = !hasCompletedMarker && saved
    ? saved.fullReplayedAt
    : Object.keys(requestDigests).length ? saved?.fullReplayedAt : now;
  let contentApplied = !hasCompletedMarker && Boolean(saved) && saved?.contentApplied !== false;
  const seenPageStates = new Set<string>();
  let pageCount = 0;

  while (pendingTables.length) {
    assertSyncExecutionActive(context);
    pageCount += 1;
    if (pageCount > GENERAL_SYNC_MAX_PAGES) {
      throw new Error("一般テーブル同期のページ数上限を超えました");
    }
    const pageState = JSON.stringify({
      pendingTables,
      cursors,
      snapshotToken,
    });
    if (seenPageStates.has(pageState)) {
      throw new Error("一般テーブル同期cursorが進みませんでした");
    }
    seenPageStates.add(pageState);
    const pullDiagnostic = beginPullDiagnostic(
      context.diagnostic,
      "general",
      "general",
      pendingTables,
      "not_applicable",
    );
    const response = await pullSync({
      since,
      tables: pendingTables,
      general_pagination: true,
      general_digest_version: GENERAL_DIGEST_VERSION,
      ...(Object.keys(requestDigests).length ? { general_digests: requestDigests } : {}),
      ...(snapshotToken ? { general_snapshot_token: snapshotToken } : {}),
      ...(Object.keys(cursors).length ? { general_cursors: cursors } : {}),
      signal: context.controller.signal,
      ...(pullDiagnostic
        ? {
            requestId: pullDiagnostic.requestId,
            lifecycle: pullDiagnosticLifecycle(pullDiagnostic),
          }
        : {}),
    }).catch((error: unknown) => {
      finishPullDiagnosticFromError(pullDiagnostic, error);
      throw error;
    });
    assertSyncExecutionActive(context);
    recordPullResponse(
      pullDiagnostic,
      response,
      pendingTables,
      "not_applicable",
    );

    const isBoundedResponse =
      response.general_pagination_version === GENERAL_SYNC_PAGINATION_VERSION
      || Boolean(response.general_snapshot_token);
    // A response without the additive marker is unsafe: the legacy endpoint
    // may have truncated a large table at its fixed limit while still
    // returning a successful HTTP response.  Never advance lastPulledAt from
    // such a payload; a rolling deployment must be upgraded before v1 sync is
    // allowed to proceed.
    if (!isBoundedResponse) {
      throw new Error("サーバーが一般テーブル分割同期に対応していません");
    }
    if (
      response.general_pagination_version != null
      && response.general_pagination_version !== GENERAL_SYNC_PAGINATION_VERSION
    ) {
      throw new Error("サーバーが一般テーブル分割同期に対応していません");
    }
    if (
      response.general_pagination_version === GENERAL_SYNC_PAGINATION_VERSION
      && !response.general_snapshot_token
    ) {
      throw new Error("一般テーブル同期snapshot tokenがありません");
    }
    if (!Object.prototype.hasOwnProperty.call(response, "general_watermark")) {
      throw new Error("一般テーブル同期watermarkがありません");
    }
    const responseWatermark = response.general_watermark ?? null;
    if (watermarkInitialized && responseWatermark !== watermark) {
      throw new Error("一般テーブル同期watermarkがページ間で変わりました");
    }
    watermark = responseWatermark;
    watermarkInitialized = true;
    if (
      snapshotToken
      && response.general_snapshot_token
      && snapshotToken !== response.general_snapshot_token
    ) {
      throw new Error("一般テーブル同期snapshot tokenがページ間で変わりました");
    }
    snapshotToken = response.general_snapshot_token ?? snapshotToken;
    serverTime ??= response.server_time;

    for (const table of pendingTables) {
      if (!response.tables[table]) {
        throw new Error(`一般テーブル同期ページが欠落しています: ${table}`);
      }
    }
    const pageDigests = validateGeneralDigestPage(response, pendingTables, requestDigests, since);
    // Retain the existing general-completion refresh for ordinary pages.
    // Only a complete set of proven not-modified receipts suppresses it.
    const pageHasChanges = pendingTables.some((table) => !response.tables[table]?.general_not_modified);
    await applyPullResponse(authScope, response, context);
    contentApplied ||= pageHasChanges;
    Object.assign(digests, pageDigests);
    assertSyncExecutionActive(context);

    const nextCursors: Record<string, string> = {};
    for (const table of pendingTables) {
      const cursor = response.tables[table]?.cursor;
      if (cursor) nextCursors[table] = cursor;
    }
    const nextPendingTables = Object.keys(nextCursors) as SyncTable[];
    await saveGeneralSyncCheckpoint(authScope, {
      version: GENERAL_SYNC_PAGINATION_VERSION,
      state: "in_progress",
      since,
      serverTime,
      watermark,
      snapshotToken,
      pendingTables: nextPendingTables,
      cursors: nextCursors,
      digests, requestDigests, fullReplayedAt, contentApplied,
    });
    reportGeneralSyncProgress(
      authScope,
      context,
      pageCount,
      nextPendingTables.length,
    );
    pendingTables = nextPendingTables;
    cursors = nextCursors;
  }

  if (!serverTime) {
    throw new Error("一般テーブル同期server_timeがありません");
  }
  await setLastPulledAt(authScope, watermark);
  await markGeneralSyncCheckpointComplete(authScope, serverTime, watermark, { digests, fullReplayedAt });
  return contentApplied;
}

async function applyGeneralPull(
  authScope: string,
  context: SyncExecutionContext,
): Promise<boolean> {
  // ACL membership can change between pages.  The server rejects a stale
  // scope-bound snapshot; discard that checkpoint and restart once from a
  // fresh, bounded run rather than leaving the account permanently stuck on
  // the same 409 response.
  try {
    return await applyGeneralPullOnce(authScope, context);
  } catch (error) {
    if (!isGeneralCheckpointRestartError(error)) throw error;
    await resetGeneralSyncCheckpoint(authScope);
    assertSyncExecutionActive(context);
    return await applyGeneralPullOnce(authScope, context);
  }
}

async function saveDocsWorkspaceId(
  authScope: string,
  workspaceId: string,
  scopeId?: string,
  projectId?: string | null,
  assertActive?: () => void,
): Promise<void> {
  assertActive?.();
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName: getDocsWorkspaceKey(authScope, scopeId, projectId),
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: workspaceId,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: workspaceId },
    });
  assertActive?.();
}

async function getSavedDocsScopes(authScope: string): Promise<DocsSyncScope[]> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, getDocsScopesKey(authScope)));
  const value = rows[0]?.cursor;
  if (!value) return [];
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (scope): scope is DocsSyncScope =>
        Boolean(
          scope &&
            typeof scope === "object" &&
            typeof (scope as DocsSyncScope).workspace_id === "string",
        ),
    );
  } catch {
    return [];
  }
}

async function saveDocsScopes(
  authScope: string,
  scopes: DocsSyncScope[],
  assertActive?: () => void,
): Promise<void> {
  assertActive?.();
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName: getDocsScopesKey(authScope),
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: JSON.stringify(scopes),
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: JSON.stringify(scopes) },
    });
  assertActive?.();
}

async function saveDocsScopeRevision(
  authScope: string,
  revision: string,
  scopeId?: string,
  projectId?: string | null,
  assertActive?: () => void,
): Promise<void> {
  assertActive?.();
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName: getDocsScopeRevisionKey(authScope, scopeId, projectId),
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: revision,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: revision },
    });
  assertActive?.();
}

async function applyDocsPull(
  authScope: string,
  context: SyncExecutionContext,
  options: {
    forceDocs?: boolean;
    onProgress?: DocsResyncProgressHandler;
  } = {},
): Promise<void> {
  assertSyncExecutionActive(context);
  markDocsSubstage(context, "load_saved_scopes");
  const previousScopes = await getSavedDocsScopes(authScope);
  assertSyncExecutionActive(context);

  const pullScope = async (
    scopeId?: string,
    projectId?: string | null,
  ): Promise<DocsSyncScope[]> => {
    // Run metadata belongs to this exact Docs scope. A completed root/shared
    // run must not decorate the next scope's pre-run local read diagnostics.
    resetDocsRunDiagnosticFields(context);
    // Force rebuilds intentionally start without saved cursors/digests.  They
    // still stage into a new run and never clear live rows/outbox before a
    // validated promotion, but a digest-equal response must not short-circuit
    // the requested authoritative snapshot.
    let docsSince: string | null = null;
    if (!options.forceDocs) {
      markDocsSubstage(context, "load_last_pulled");
      docsSince = await getLastPulledAt(authScope, true, scopeId, projectId);
    }
    assertSyncExecutionActive(context);
    let docsDigests: Record<string, string> = {};
    if (!options.forceDocs) {
      markDocsSubstage(context, "load_table_digests");
      docsDigests = await getSavedDocsDigests(authScope, scopeId, projectId);
    }
    assertSyncExecutionActive(context);
    let savedDocsScopeDigest: string | null = null;
    if (!options.forceDocs) {
      markDocsSubstage(context, "load_scope_digest");
      savedDocsScopeDigest = await getSavedDocsScopeDigest(authScope, scopeId, projectId);
    }
    assertSyncExecutionActive(context);
    let savedDocsScopeRevision: string | null = null;
    if (!options.forceDocs) {
      markDocsSubstage(context, "load_scope_revision");
      savedDocsScopeRevision = await getSavedDocsScopeRevision(authScope, scopeId, projectId);
    }
    assertSyncExecutionActive(context);
    markDocsSubstage(context, "load_active_run");
    let run = await getActiveDocsSyncRun(authScope, scopeId, projectId, options.forceDocs);
    updateDocsRunDiagnostic(context, run);
    assertSyncExecutionActive(context);
    if (!run) {
      // A force request starts a new staged snapshot, but never clears live
      // rows/outbox.  Any abandoned normal run is staging-only and safe to
      // discard before creating the explicit force run.
      let otherRun: DocsSyncRunRow | null = null;
      if (options.forceDocs) {
        markDocsSubstage(context, "load_active_run");
        otherRun = await getActiveDocsSyncRun(authScope, scopeId, projectId, false);
        updateDocsRunDiagnostic(context, otherRun);
      }
      assertSyncExecutionActive(context);
      if (otherRun) await deleteDocsSyncRun(
        otherRun.runId,
        authScope,
        () => assertSyncExecutionActive(context),
      );
      assertSyncExecutionActive(context);
      markDocsSubstage(context, "create_run", null);
      run = await createDocsSyncRun(
        authScope,
        scopeId,
        projectId,
        Boolean(options.forceDocs),
        () => assertSyncExecutionActive(context),
      );
      updateDocsRunDiagnostic(context, run);
      assertSyncExecutionActive(context);
    }
    if (run && !options.forceDocs) {
      docsDigests = {
        ...docsDigests,
        ...parseJsonRecord<Record<string, string>>(run.digestJson, {}),
      };
    }
    const reportProgress: DocsResyncProgressHandler = (progress) => {
      reportDocsSyncProgress(
        authScope,
        context,
        scopeId,
        projectId,
        progress.page,
        progress.retryAttempt ?? null,
      );
      options.onProgress?.(progress);
    };
    let diagnosticPromotionGeneration: number | null = null;
    for (let attempt = 0; attempt < DOCS_PULL_MAX_ATTEMPTS; attempt += 1) {
      if (diagnosticPromotionGeneration == null) {
        diagnosticPromotionGeneration = ++nextSyncDiagnosticPromotionGeneration;
      }
      try {
        return await applyDocsPullOnce(
          authScope,
          docsSince,
          docsDigests,
          savedDocsScopeDigest,
          savedDocsScopeRevision,
          context,
          scopeId,
          projectId,
          {
            ...options,
            onProgress: reportProgress,
            run,
            previousScopes: scopeId == null ? previousScopes : undefined,
            diagnosticPromotionGeneration: diagnosticPromotionGeneration ?? undefined,
          },
        );
      } catch (error) {
        if (
          error instanceof DocsPullTransportError
          && attempt < DOCS_PULL_MAX_ATTEMPTS - 1
        ) {
          assertSyncExecutionActive(context);
          reportProgress({
            phase: "retrying",
            completed: 0,
            total: null,
            page: 0,
            retryAttempt: attempt + 1,
          });
          await new Promise<void>((resolve) =>
            setTimeout(resolve, DOCS_PULL_TRANSPORT_RETRY_DELAY_MS * (attempt + 1)),
          );
          assertSyncExecutionActive(context);
          continue;
        }
        if (
          isDocsSnapshotChanged(error)
          && attempt >= DOCS_PULL_MAX_ATTEMPTS - 1
        ) {
          markDocsPromotion(
            context.diagnostic,
            diagnosticScopeFor(scopeId, projectId),
            "discarded",
            diagnosticPromotionGeneration,
          );
        }
        if (
          isDocsSnapshotChanged(error) &&
          attempt < DOCS_PULL_MAX_ATTEMPTS - 1
        ) {
          assertSyncExecutionActive(context);
          markDocsPromotion(
            context.diagnostic,
            diagnosticScopeFor(scopeId, projectId),
            "discarded",
            diagnosticPromotionGeneration,
          );
          diagnosticPromotionGeneration = ++nextSyncDiagnosticPromotionGeneration;
          reportProgress({
            phase: "retrying",
            completed: 0,
            total: null,
            page: 0,
            retryAttempt: attempt + 1,
          });
          if (run) {
            // Cursor/token rejection means the staged pages belong to an
            // invalid server snapshot.  Delete only staging/run metadata;
            // live cache, outbox and conflicts remain intact.
            await deleteDocsSyncRun(
              run.runId,
              authScope,
              () => assertSyncExecutionActive(context),
            );
            assertSyncExecutionActive(context);
            markDocsSubstage(context, "create_run", null);
            run = await createDocsSyncRun(
              authScope,
              scopeId,
              projectId,
              Boolean(options.forceDocs),
              () => assertSyncExecutionActive(context),
            );
            updateDocsRunDiagnostic(context, run);
            assertSyncExecutionActive(context);
          }
          // A 400/409 snapshot change can be an ACL revision update.  Do not
          // resend the stale scope digest/revision into the fresh run; the
          // server must establish a new immutable snapshot.  Per-table
          // digests are still reloaded because already-completed tables may
          // have been applied before the failed cursor was rejected.
          savedDocsScopeDigest = null;
          savedDocsScopeRevision = null;
          markDocsSubstage(context, "load_table_digests");
          docsDigests = await getSavedDocsDigests(authScope, scopeId, projectId);
          assertSyncExecutionActive(context);
          await new Promise<void>((resolve) =>
            setTimeout(resolve, DOCS_PULL_RETRY_DELAY_MS * (attempt + 1)),
          );
          assertSyncExecutionActive(context);
          continue;
        }
        if (error instanceof DocsPullTransportError) throw error.originalError;
        throw error;
      }
    }
    return [];
  };

  const discoveredScopes = await pullScope();
  assertSyncExecutionActive(context);
  // Older servers omit docs_scopes and retain the v2 personal-only contract.
  if (!discoveredScopes.length) return;
  const activeIds = new Set(
    discoveredScopes.map((scope) => getDocsScopeKey(scope.workspace_id, scope.project_id)),
  );
  // With staged promotion the complete scope-set diff was applied inside the
  // root promotion transaction (including docs_scopes sync_state).  Keep the
  // old post-pull path only for rolling databases/test doubles without the
  // staging schema, where no atomic promotion is available.
  if (!docsStagingAvailable()) {
    for (const previous of previousScopes) {
      if (activeIds.has(getDocsScopeKey(previous.workspace_id, previous.project_id))) continue;
      if (typeof quarantineRevokedDocsScope === "function") {
        assertSyncExecutionActive(context);
        await quarantineRevokedDocsScope(
          authScope,
          previous.workspace_id,
          previous.project_id,
          getDocsScopeKey(previous.workspace_id, previous.project_id),
          "revoke",
          () => assertSyncExecutionActive(context),
        );
      }
      if (!schema.docsScopeMembership) {
        assertSyncExecutionActive(context);
        await reconcileDocsNodesWithServer([], previous.workspace_id);
        assertSyncExecutionActive(context);
        await reconcileDocsSupertagsWithServer([], previous.workspace_id);
        assertSyncExecutionActive(context);
        await reconcileDocsFieldsWithServer([], previous.workspace_id);
      }
    }
    for (const scope of discoveredScopes) {
      if (scope.source === "personal" && scope.access === "owner") continue;
      const previous = previousScopes.find(
        (item) =>
          getDocsScopeKey(item.workspace_id, item.project_id)
          === getDocsScopeKey(scope.workspace_id, scope.project_id),
      );
      if (previous && !previous.read_only && scope.read_only) {
        assertSyncExecutionActive(context);
        await quarantineRevokedDocsScope(
          authScope,
          scope.workspace_id,
          scope.project_id,
          getDocsScopeKey(scope.workspace_id, scope.project_id),
          "downgrade",
          () => assertSyncExecutionActive(context),
        );
      }
    }
    assertSyncExecutionActive(context);
    await saveDocsScopes(
      authScope,
      discoveredScopes,
      () => assertSyncExecutionActive(context),
    );
  }
  for (const scope of discoveredScopes) {
    if (scope.source === "personal" && scope.access === "owner") continue;
    assertSyncExecutionActive(context);
    await pullScope(scope.workspace_id, scope.project_id);
  }
}

function notifyDocsResyncProgress(
  listener: DocsResyncProgressHandler,
  progress: DocsResyncProgress,
): void {
  try {
    listener(progress);
  } catch (error) {
    console.warn(
      "[sync] Docs progress observer failed",
      error instanceof Error ? error.name : "UnknownError",
    );
  }
}

/**
 * Settingsから呼ぶサーバー正本のDocs再構築。
 * 認証切替・通常同期と同じexclusive queue上で実行し、live cache/outboxを
 * 先に破棄せず staging へ全量取得してから原子的に昇格する。
 */
export async function forceDocsResync(
  onProgress?: DocsResyncProgressHandler,
): Promise<void> {
  if (isMobileReadOnlyDiagnosticsEnabled()) {
    throw new Error("読み取り診断モードではDocs再構築を実行できません");
  }
  const queued: DocsResyncProgress = {
    phase: "queued",
    completed: 0,
    total: null,
    page: 0,
  };
  if (onProgress) notifyDocsResyncProgress(onProgress, queued);
  const token = await getToken();
  if (!token) {
    throw new Error("サーバーログインが必要です");
  }
  const authScope = getTokenAuthScope(token);
  // Settings can unmount and reopen while the same full pull is running.
  // Join it, even during a temporary connection failure, instead of queuing
  // another snapshot and retaining its stale screen callbacks afterward.
  const existing = docsResyncByAuthScope.get(authScope);
  if (existing) {
    if (onProgress) {
      existing.listeners.add(onProgress);
      notifyDocsResyncProgress(onProgress, existing.progress);
    }
    return existing.promise;
  }
  if (!canAttemptAoiTalkServer()) {
    throw new Error("サーバーに接続してからDocsを再構築してください");
  }
  const flight: RunningDocsResyncFlight = {
    promise: completedSync,
    listeners: new Set(onProgress ? [onProgress] : []),
    progress: queued,
  };
  const reportProgress: DocsResyncProgressHandler = (progress) => {
    flight.progress = progress;
    for (const listener of flight.listeners) notifyDocsResyncProgress(listener, progress);
  };
  flight.promise = runQueuedAuthScopeTransition(async () => {
    const context = createSyncExecutionContext();
    try {
      assertSyncExecutionActive(context);
      reportProgress({
        phase: "preparing",
        completed: 0,
        total: null,
        page: 0,
      });
      const currentToken = await getToken();
      if (!currentToken || getTokenAuthScope(currentToken) !== authScope) {
        throw new Error("認証状態が変わったため、Docs再構築を中止しました");
      }
      if (!canAttemptAoiTalkServer()) {
        throw new Error("サーバー接続が失われたため、Docs再構築を中止しました");
      }
      assertSyncExecutionActive(context);
      const legacy = await captureLegacy();
      if (legacy.state !== "migrated" && legacy.state !== "not_needed") {
        throw new Error("旧版データの移行確認後にDocs再構築を実行してください");
      }
      const v3Safety = await checkSyncV3CacheSafety(authScope);
      // An unavailable capabilities probe keeps the existing v2 rebuild, which
      // reports its own transport/HTTP failures exactly as before.
      const mode = v3Safety.safe
        ? await getSyncV3Mode(context.controller.signal, authScope).catch((): SyncV3Mode => "v2")
        : "v2";
      assertSyncExecutionActive(context);
      if (mode === "v3") {
        // Re-bootstrap every Docs scope at the next generation; live rows stay
        // until each scope's new snapshot completes.
        const v3Context = {
          signal: context.controller.signal,
          assertActive: () => assertSyncExecutionActive(context),
          onPage: (page: number) => reportProgress({ phase: "downloading", completed: 0, total: null, page }),
        };
        try {
          await requestV3DocsRebootstrap(authScope, v3Context);
          await runV3Pull(authScope, v3Context);
          reportProgress({ phase: "finalizing", completed: 0, total: null, page: 0 });
          assertSyncExecutionActive(context);
          return;
        } catch (error) {
          if (!(error instanceof SyncV3DisabledError)) throw error;
        }
      }
      await applyDocsPull(authScope, context, {
        forceDocs: true,
        onProgress: reportProgress,
      });
      assertSyncExecutionActive(context);
    } finally {
      releaseSyncExecutionContext(context);
    }
  }).finally(() => {
    flight.listeners.clear();
    if (docsResyncByAuthScope.get(authScope) === flight) {
      docsResyncByAuthScope.delete(authScope);
    }
  });
  docsResyncByAuthScope.set(authScope, flight);
  return flight.promise;
}

/**
 * knowledge_nodes の push 応答（サーバ権威）を、呼出元の write unit 内で反映する。
 * pending 判定・snapshot 記録・ACL 計算・強制適用を同じ transaction で行う。
 */
function projectDocsPushNodeInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  entity: Record<string, unknown>,
  authScope: string | null,
  preservePending: boolean,
): void {
  if (authScope && getTokenAuthScope(getCachedToken()) !== authScope) return;
  const id = String(entity.id);
  if (hasPendingOutboxInWriter(w, tx, "knowledge_nodes", id, authScope)) {
    if (!preservePending) recordOutboxServerSnapshotInWriter(w, tx, authScope, "knowledge_nodes", id, entity);
    return;
  }
  let node = entity as unknown as DocsNode;
  // Conflict receipts intentionally omit ACL fields. Preserve the
  // same actor/composite projection; absence must not revoke a locally
  // verified parent or silently invent write permission.
  const [cached] = tx.select().from(schema.knowledgeNodes).where(eq(schema.knowledgeNodes.id, id)).all();
  // A cleared live cache still has actor/composite memberships; do
  // not bypass their revocation when a delayed ACK recreates a row.
  const sameScope = !cached || (cached.workspaceId === entity.workspace_id
    && (cached.projectId ?? null) === (entity.project_id ?? null));
  const memberships = authScope && sameScope && schema.docsScopeMembership
    ? tx.select().from(schema.docsScopeMembership).where(and(
      eq(schema.docsScopeMembership.authScope, authScope),
      eq(schema.docsScopeMembership.scopeKey, `${String(entity.workspace_id)}|project:${entity.project_id ?? ""}`),
      eq(schema.docsScopeMembership.tableName, "knowledge_nodes"),
      eq(schema.docsScopeMembership.entityKey, id),
    )).all() : [];
  const membership = memberships[0];
  const writable = membership?.state === "active" && membership.readOnly === false
    && ["owner", "write"].includes(membership.access ?? "")
    && cached?.readOnly !== true && cached?.access !== "read";
  const has = (key: string) => Object.prototype.hasOwnProperty.call(entity, key);
  const knownReadOnly = sameScope && (cached?.readOnly === true || cached?.access === "read"
    || (membership != null && membership.state !== "active") || membership?.readOnly === true || membership?.access === "read");
  // A delayed write receipt cannot grant permissions revoked locally
  // while HTTP was in flight. A fresh pull owns permission upgrades.
  const access = knownReadOnly ? "read" : has("access") ? node.access : writable ? membership.access as DocsNode["access"] : "read";
  node = { ...node,
    source: has("source") ? node.source : sameScope ? cached?.source as DocsNode["source"] : undefined,
    access,
    read_only: !["owner", "write"].includes(access ?? "")
      || (has("read_only") ? entity.read_only !== false : !writable),
  };
  applyRemoteDocsNodesInWriter(w, tx, [node], null, { force: true });
}

/**
 * knowledge_supertags / node_supertags / field_values の push 応答を、呼出元の
 * write unit 内で反映する。pending 判定・snapshot 記録・強制適用を同じ transaction で行う。
 */
function projectDocsPushNonNodeInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  table: string,
  entity: Record<string, unknown>,
  authScope: string | null,
  preservePending: boolean,
): void {
  void preservePending; // 非 node は従来どおり pending 時に常に snapshot を記録する。
  if (authScope && getTokenAuthScope(getCachedToken()) !== authScope) return;
  const deleted = entity.deleted === true;
  const key = table === "knowledge_supertags"
    ? String(entity.id)
    : table === "knowledge_node_supertags"
      ? `${String(entity.node_id)}:${String(entity.supertag_id)}`
      : table === "knowledge_field_values"
        ? `${String(entity.node_id)}:${String(entity.field_id)}`
        : null;
  if (key === null) return;
  if (hasPendingOutboxInWriter(w, tx, table, key, authScope)) {
    recordOutboxServerSnapshotInWriter(w, tx, authScope, table, key, entity);
    return;
  }
  if (table === "knowledge_supertags") {
    applyRemoteDocsSupertagsInWriter(w, tx, [entity as unknown as DocsSupertag], null, { force: true });
  } else if (table === "knowledge_node_supertags") {
    if (deleted) {
      deleteLocalDocsNodeSupertagInWriter(w, tx, String(entity.node_id), String(entity.supertag_id));
    } else {
      applyRemoteDocsNodeSupertagsInWriter(w, tx, [entity as unknown as DocsNodeSupertag], undefined, null, { force: true });
    }
  } else if (deleted) {
    deleteLocalDocsFieldValueInWriter(w, tx, String(entity.node_id), String(entity.field_id));
  } else {
    applyRemoteDocsFieldValuesInWriter(w, tx, [entity as unknown as DocsFieldValue], undefined, null, { force: true });
  }
}

/**
 * Docs push 応答の entity（サーバ権威）を対応する applyRemote* で反映する。
 * 409 conflict の応答も、競合解決が適用済みと判断した場合に反映する。
 */
export async function applyDocsPushEntity(
  table: string | undefined,
  entity: Record<string, unknown> | undefined,
  authScope?: string,
  preservePending = false,
): Promise<void> {
  if (!table || !entity) return;
  switch (table) {
    case "knowledge_nodes": {
      // A foreground edit owns its live-row write and outbox merge together.
      // Keep the pending check and forced receipt projection in that same
      // coordinator, rather than clearing a newer edit after a stale read.
      const resolvedScope = await resolveAuthScope(authScope);
      await runForegroundSqliteWrite((w) =>
        getDb().transaction((tx) =>
          projectDocsPushNodeInWriter(w, tx, entity, resolvedScope, preservePending)));
      break;
    }
    case "knowledge_supertags":
    case "knowledge_node_supertags":
    case "knowledge_field_values": {
      const resolvedScope = await resolveAuthScope(authScope);
      await runForegroundSqliteWrite((w) =>
        getDb().transaction((tx) =>
          projectDocsPushNonNodeInWriter(w, tx, table, entity, resolvedScope, preservePending)));
      break;
    }
    default:
      break;
  }
}

function jsonEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

const DOCS_CAS_STALE_REASON_CODE = "docs_cas_stale";
const DOCS_BASE_MISSING_REASON_CODE = "docs_base_missing";
const LEGACY_DOCS_MISSING_BASE_REASON = "Docs base_updated_at is required for an existing entity";

function syncResultFailureReason(result: Pick<SyncPushResult, "reason" | "reason_code" | "status">): string {
  // Keep the structured wire classification in the durable lastError field.
  // A free-form reason is only a legacy fallback and is never used as an
  // automatic-resolution authorization.
  const reasonCode = typeof result.reason_code === "string" && result.reason_code.trim()
    ? result.reason_code.trim()
    : null;
  const reason = typeof result.reason === "string" && result.reason.trim()
    ? result.reason.trim()
    : null;
  if (reasonCode && reason && reason !== reasonCode) return `${reason} [${reasonCode}]`;
  return reasonCode || reason || result.status;
}

function isDocsCasStaleReason(reasonCode: unknown): boolean {
  return reasonCode === DOCS_CAS_STALE_REASON_CODE;
}

function isLegacyDocsBaseMissingResult(result: SyncPushResult): boolean {
  // The legacy blank-paragraph repair is intentionally much narrower than a
  // normal Docs merge. Require both the structured code and its exact legacy
  // message so an old/code-less or newly classified business error is held.
  return result.reason_code === DOCS_BASE_MISSING_REASON_CODE
    && result.reason === LEGACY_DOCS_MISSING_BASE_REASON;
}

function jsonObject(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function hasNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function docsEntityIdentityMatches(
  operation: PendingOutbox,
  entity: Record<string, unknown>,
): boolean {
  if (operation.tableName === "knowledge_nodes" || operation.tableName === "knowledge_supertags") {
    return entity.id === operation.entityId;
  }
  const [first, second, ...rest] = operation.entityId.split(":");
  if (rest.length || !first || !second) return false;
  if (operation.tableName === "knowledge_node_supertags") {
    return entity.node_id === first && entity.supertag_id === second;
  }
  if (operation.tableName === "knowledge_field_values") {
    return entity.node_id === first && entity.field_id === second;
  }
  return false;
}

function docsEntityScopeMatches(
  operation: PendingOutbox,
  entity: Record<string, unknown>,
): boolean {
  if (!operation.docsScopeKey) return false;
  const workspace = entity.workspace_id ?? entity.workspaceId
    ?? entity.docs_library_id ?? entity.docsLibraryId;
  const project = entity.project_id ?? entity.projectId ?? null;
  if (!hasNonEmptyString(workspace)
    || (entity.workspace_id !== undefined && entity.workspaceId !== undefined
      && String(entity.workspace_id) !== String(entity.workspaceId))
    || (entity.project_id !== undefined && entity.projectId !== undefined
      && (entity.project_id ?? null) !== (entity.projectId ?? null))) return false;
  return `${workspace}|project:${project ?? ""}` === operation.docsScopeKey;
}

async function removeAcknowledgedOutboxSnapshot(
  opId: string,
  snapshot: PendingOutboxSnapshot,
  guard?: Parameters<typeof removeOutboxOpIfSnapshot>[2],
  project?: (w: SqliteWriter, tx: SqliteHandle) => void,
  expected?: PendingOutbox,
  assertActive: () => void = () => {},
): Promise<boolean> {
  return runForegroundSqliteWrite((w) => getDb().transaction((tx) => {
    assertActive();
    if (expected?.tableName === "knowledge_nodes") {
      if (getTokenAuthScope(getCachedToken()) !== expected.authScope) return false;
      const current = tx.select().from(schema.outbox).where(eq(schema.outbox.opId, opId)).get();
      // A newer recorded conflict/block is part of the receipt snapshot too.
      // Do not erase it after waiting behind another foreground mutation.
      if (!current || current.blockedReason !== expected.blockedReason
        || current.lastError !== expected.lastError || current.retryCount !== expected.retryCount
        || !jsonEqual(current.conflictPayload, expected.conflictPayload)
        || !jsonEqual(current.basePayload, expected.basePayload)) return false;
    }
    // Carry the complete durable row through the CAS predicate.  The compact
    // operation fields alone cannot distinguish a newer conflict marker,
    // retry/error state, or base snapshot merged under the same op id.
    // A caller must pass the exact PendingOutbox row it read.  Prefer that
    // durable snapshot over any reduced operation projection supplied by an
    // older call site.
    const fullSnapshot: PendingOutboxSnapshot = expected ?? snapshot;
    const removed = removeOutboxOpIfSnapshotInWriter(w, tx, opId, fullSnapshot, guard);
    // CAS removal and baseline projection are one unit. Otherwise an edit
    // queued between them can inherit NULL even though create was confirmed.
    if (removed && project) project(w, tx);
    return removed;
  }));
}

async function getCurrentOutbox(
  operationId: string,
): Promise<PendingOutbox | null> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.outbox)
    .where(eq(schema.outbox.opId, operationId));
  return rows[0] ?? null;
}

function isOutboxSnapshotCurrent(
  current: PendingOutbox | null,
  snapshot: PendingOutbox,
): boolean {
  return Boolean(
    current &&
      current.opId === snapshot.opId &&
      current.tableName === snapshot.tableName &&
      current.action === snapshot.action &&
      current.entityId === snapshot.entityId &&
      current.payload === snapshot.payload &&
      current.baseUpdatedAt === snapshot.baseUpdatedAt &&
      current.authScope === snapshot.authScope &&
      current.docsScopeKey === snapshot.docsScopeKey &&
      current.blockedReason === snapshot.blockedReason &&
      current.retryCount === snapshot.retryCount &&
      jsonEqual(current.basePayload, snapshot.basePayload) &&
      jsonEqual(current.conflictPayload, snapshot.conflictPayload) &&
      current.lastError === snapshot.lastError,
  );
}

async function rebaseChangedOutbox(
  operation: PendingOutbox,
  serverEntity: Record<string, unknown> | undefined,
  serverUpdatedAt: string | null | undefined,
  sent?: PendingOutbox,
  assertActive: () => void = () => {},
): Promise<void> {
  if (!serverEntity) return;
  if (sent?.tableName === "knowledge_nodes" && sent.action === "create"
    && operation.action === "create" && sent.authScope) {
    // The edit can also win after the batch receipt pass but before ACK CAS.
    // Re-run the proven same-op create→update conversion for that late edit.
    await rebaseDocsWriteFollowups(sent.authScope, [sent], [{ op_id: sent.opId,
      status: "ok", entity: serverEntity, server_updated_at: serverUpdatedAt }], assertActive);
    return;
  }
  const isDocs = operation.tableName.startsWith("knowledge_");
  const isConversation = operation.tableName === "conversation_sessions";
  if (!isDocs && !isConversation) return;
  const entityVersion = isConversation
    ? (serverEntity.pin_updated_at as string | null | undefined)
      ?? (serverEntity.updated_at as string | null | undefined)
    : (serverEntity.updated_at as string | null | undefined);
  const rebase = async () => {
    assertActive();
    if (operation.tableName === "knowledge_nodes") {
      const current = await getCurrentOutbox(operation.opId);
      if (!current || current.authScope !== operation.authScope || current.docsScopeKey !== operation.docsScopeKey
        || current.blockedReason != null || !isOutboxSnapshotCurrent(current, operation)
        // The response was sent for `sent`; never let a post-send lifecycle
        // generation become the expected row for an old success ACK. The
        // create payload-only continuation is handled by the dedicated exact
        // receipt branch above before reaching this guard.
        // A knowledge-node UPDATE must always have a sent lifecycle snapshot;
        // a missing sent row is fail-closed and cannot become an expectation.
        || (sent == null || !isOutboxSnapshotCurrent(current, sent))
        || getTokenAuthScope(getCachedToken()) !== current.authScope
        || isDocsResultOlderThanRecordedConflict(current, { op_id: current.opId, status: "ok",
          entity: serverEntity, server_updated_at: serverUpdatedAt ?? entityVersion })) return;
    }
    await rebaseOutboxOp(
      operation.opId,
      serverUpdatedAt ?? entityVersion ?? null,
      serverEntity,
      operation,
    );
  };
  if (operation.tableName === "knowledge_nodes") await runForegroundSqliteWrite(rebase);
  else await rebase();
}

type DocsConflictResolution = "rebased" | "applied" | "unresolved";

/**
 * サーバー409を端末時計で解決しない。ノードの異なるフィールドだけは
 * サーバー版を新しい基準として再ベースし、同一フィールド・削除状態は
 * outboxに両方を残して明示的競合にする。
 */
async function resolveDocsConflict(
  operation: PendingOutbox,
  serverEntity: Record<string, unknown> | undefined,
  serverUpdatedAt: string | null | undefined,
  reasonCode?: string,
): Promise<DocsConflictResolution> {
  // Generic Docs resolution is authorized only by the explicit CAS-stale
  // classification.  Business rules, scope/ACL, deletion, base-missing and
  // legacy/unknown responses retain both values for user review.
  if (!serverEntity || !operation.docsScopeKey || !operation.authScope
    || !hasNonEmptyString(serverUpdatedAt)
    || serverEntity.updated_at !== serverUpdatedAt) return "unresolved";
  const payload = jsonObject(operation.payload);
  if (!payload) return "unresolved";
  const serverVersion = serverUpdatedAt;

  if (operation.tableName === "knowledge_nodes" && operation.action === "create") {
    // A create has no common base. It may still be acknowledged by the
    // existing exact create receipt when the server explicitly reports the
    // missing-base condition and the complete canonical content matches.
    const exactCreate = reasonCode === DOCS_BASE_MISSING_REASON_CODE
      && docsEntityIdentityMatches(operation, serverEntity)
      && docsEntityScopeMatches(operation, serverEntity)
      && validateOutboxConflictServerScope(operation, serverEntity).ok
      && serverEntity.created_by === operation.authScope.slice("auth:".length)
      && matchesDocsCreateContent(payload, serverEntity);
    if (!exactCreate) return "unresolved";
    return "applied";
  }
  if (!isDocsCasStaleReason(reasonCode)
    || !docsEntityIdentityMatches(operation, serverEntity)
    || !docsEntityScopeMatches(operation, serverEntity)) return "unresolved";
  if (operation.tableName === "knowledge_nodes"
    && !validateOutboxConflictServerScope(operation, serverEntity).ok) return "unresolved";
  const base = jsonObject(operation.basePayload);
  if (!hasNonEmptyString(operation.baseUpdatedAt) || !base
    || compareDocsVersions(operation.baseUpdatedAt, serverVersion) !== -1) return "unresolved";

  if (
    operation.tableName === "knowledge_nodes" &&
    operation.action === "update"
  ) {
    const changed = Object.keys(payload).filter((key) => !jsonEqual(payload[key], base[key]));
    if (changed.length && changed.every((key) => jsonEqual(serverEntity[key], payload[key]))) {
      return "applied";
    }
    const overlap = changed.some((key) => !jsonEqual(serverEntity[key], base[key]));
    if (overlap) return "unresolved";
    const rebased = await rebaseOutboxOp(operation.opId, serverVersion, serverEntity, operation);
    return rebased ? "rebased" : "unresolved";
  }

  if (operation.tableName === "knowledge_nodes" && operation.action === "delete") {
    if (serverEntity.archived_at) return "applied";
    const nodeFields = [
      "title",
      "description",
      "body_json",
      "parent_id",
      "project_id",
      "day_date",
      "sort_order",
      "archived_at",
    ];
    if (nodeFields.some((key) => !jsonEqual(serverEntity[key], base[key]))) {
      return "unresolved";
    }
    const rebased = await rebaseOutboxOp(operation.opId, serverVersion, serverEntity, operation);
    return rebased ? "rebased" : "unresolved";
  }

  if (operation.tableName === "knowledge_node_supertags") {
    const deleted = serverEntity.deleted === true;
    if ((operation.action === "create" && !deleted) || (operation.action === "delete" && deleted)) {
      return "applied";
    }
    return "unresolved";
  }

  if (operation.tableName === "knowledge_supertags" && operation.action === "create") {
    if (
      serverEntity.deleted === true ||
      !["name", "base_type", "color", "icon"]
        .every((key) => jsonEqual(serverEntity[key], payload[key]))
    ) {
      return "unresolved";
    }
    return "applied";
  }

  if (operation.tableName === "knowledge_field_values") {
    const value = payload.value;
    if (value === null || value === undefined || value === "") {
      return serverEntity.deleted === true ? "applied" : "unresolved";
    }
    const serverMatches =
      (typeof value === "number" && jsonEqual(serverEntity.value_number, value)) ||
      (typeof value === "boolean" && jsonEqual(serverEntity.value_json, { value })) ||
      (typeof value === "string" &&
        (jsonEqual(serverEntity.value_text, value) ||
          jsonEqual(serverEntity.value_datetime, value) ||
          jsonEqual(serverEntity.target_node_id, value))) ||
      jsonEqual(serverEntity.value_json, value);
    // 既に同じ値がサーバーへ適用済みなら、通信再送による409を解消できる。
    // 異なる値の場合は同一フィールドの競合なので、両方を残す。
    return serverMatches ? "applied" : "unresolved";
  }
  return "unresolved";
}

/** Apply a task push response without treating a delete acknowledgement as a
 * partial active task row.  DELETE responses intentionally contain only
 * `{id, deleted_at}`; routing them through applyRemoteTasks would otherwise
 * overwrite required fields and clear the tombstone on a later refresh.
 */
async function applyTaskPushEntity(
  operation: SyncPushOperation | undefined,
  entity: Record<string, unknown> | undefined,
  serverUpdatedAt?: string | null,
): Promise<void> {
  if (operation?.table !== "tasks") return;
  const entityId =
    typeof entity?.id === "string" && entity.id
      ? entity.id
      : operation.entity_id;
  if (!entityId) return;
  if (operation.action === "restore") {
    await applyTaskRestore({
      ...(entity ?? {}),
      id: entityId,
    });
    // Restore acknowledgements are compact batch payloads.  Rehydrate the
    // root when possible so a cache that only held a tombstone has full fields.
    if (!entity || typeof entity.title !== "string") {
      try {
        const restored = await taskApi.getTask(entityId);
        await applyTaskRestore(restored);
      } catch {
        // The next pull will deliver the active row; the explicit restore has
        // already cleared the local tombstone/ledger.
      }
    }
    return;
  }
  const deletedAt =
    typeof entity?.deleted_at === "string" && entity.deleted_at
      ? entity.deleted_at
      : operation.action === "delete"
        ? serverUpdatedAt ?? null
        : null;
  if (deletedAt != null || operation.action === "delete") {
    await applyTaskTombstones([
      { id: entityId, deleted_at: deletedAt },
    ]);
    return;
  }
  if (entity) {
    await applyRemoteTasks([entity as unknown as Task]);
  }
}

type ReorderPayload = {
  projectId: string | null;
  taskIds: string[];
};

function decodeReorderPayload(operation: PendingOutbox): ReorderPayload | null {
  let payload: unknown;
  try {
    payload = JSON.parse(operation.payload || "{}");
  } catch {
    return null;
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const value = payload as Record<string, unknown>;
  const projectId = value.project_id;
  const rawTaskIds = value.task_ids;
  if (
    projectId !== null &&
    projectId !== undefined &&
    typeof projectId !== "string"
  ) {
    return null;
  }
  if (
    !Array.isArray(rawTaskIds) ||
    rawTaskIds.length === 0 ||
    rawTaskIds.some(
      (taskId) => typeof taskId !== "string" || taskId.trim().length === 0,
    )
  ) {
    return null;
  }
  return {
    projectId: typeof projectId === "string" ? projectId : null,
    taskIds: rawTaskIds as string[],
  };
}

function isTaskPrerequisite(operation: PendingOutbox): boolean {
  return operation.tableName === "tasks" && operation.action !== "reorder";
}

/**
 * A reorder is causally dependent on any task create/update still in the
 * outbox.  Query the DB rather than only the initial list because a failed
 * response may increment retryCount (and eventually disappear from
 * listPendingOutbox), while it remains an unresolved prerequisite.
 */
async function hasPendingReorderPrerequisite(
  taskIds: string[],
  authScope: string,
  regularPending: PendingOutbox[],
  regularResultStatuses: Map<string, "ok" | "conflict" | "error">,
): Promise<boolean> {
  for (const taskId of taskIds) {
    const prerequisites = regularPending.filter(
      (operation) =>
        isTaskPrerequisite(operation) && operation.entityId === taskId,
    );
    if (
      prerequisites.some(
        (operation) => regularResultStatuses.get(operation.opId) !== "ok",
      )
    ) {
      // An explicit per-operation error/conflict (or a missing result) is not
      // an acknowledgement, regardless of what a test double or stale DB
      // query happens to report.
      return true;
    }
    if (typeof hasPendingOutbox === "function") {
      if (await hasPendingOutbox("tasks", taskId, authScope)) return true;
      continue;
    }
    // Minimal test doubles from older suites do not expose hasPendingOutbox.
    // In that case, require an explicit successful result for each matching
    // operation in this push batch before allowing the reorder through.
    // No row and no failed/missing result means the dependency was acked.
  }
  return false;
}

/** Replay canonical task ordering through the dedicated endpoint. */
async function flushReorderOutbox(
  pending: PendingOutbox[],
  context: SyncExecutionContext,
  authScope: string,
  regularPending: PendingOutbox[] = [],
  regularResultStatuses: Map<string, "ok" | "conflict" | "error"> = new Map(),
): Promise<void> {
  for (const operation of pending) {
    assertSyncExecutionActive(context);
    const decoded = decodeReorderPayload(operation);
    if (!decoded) {
      await markOutboxError(operation.opId, "reorder:invalid_payload", operation);
      continue;
    }

    if (
      await hasPendingReorderPrerequisite(
        decoded.taskIds,
        authScope,
        regularPending,
        regularResultStatuses,
      )
    ) {
      // The regular task create/update has not been acknowledged yet.  Keep
      // this reorder row intact; the next sync retries the prerequisite first.
      continue;
    }

    try {
      if (decoded.projectId) {
        await taskApi.reorderTasks(decoded.projectId, decoded.taskIds);
      } else {
        await taskApi.reorderAllTasks(decoded.taskIds);
      }
    } catch (error) {
      // Never discard a reorder merely because the endpoint returned HTTP
      // non-401.  Preserve it with an explicit classification so a 409 can
      // be reviewed/rebased and validation/permission failures are visible as
      // terminal instead of becoming silent data loss.
      if (isApiHttpError(error)) {
        const detail = error instanceof Error ? error.message : "http_error";
        if (error.status === 409) {
          await markOutboxConflict(
            operation.opId,
            `reorder:conflict:${detail}`,
            undefined,
            operation,
          );
        } else if (error.status === 401) {
          await markOutboxError(operation.opId, `reorder:auth:${detail}`, operation);
        } else if (error.status >= 400 && error.status < 500) {
          await markOutboxError(
            operation.opId,
            `reorder:terminal_http_${error.status}:${detail}`,
            operation,
          );
        } else {
          await markOutboxError(operation.opId, `reorder:http_${error.status}:${detail}`, operation);
        }
        continue;
      }
      await markOutboxError(
        operation.opId,
        error instanceof Error ? `reorder:${error.message}` : "reorder:error",
        operation,
      );
      continue;
    }

    const removed = await removeAcknowledgedOutboxSnapshot(
      operation.opId,
      operation,
      undefined,
      undefined,
      operation,
    );
    if (!removed) continue;
    // The local order is already optimistic.  A best-effort refresh reconciles
    // server canonical values without turning a refresh timeout into a blind
    // duplicate reorder.
    try {
      const refreshed = decoded.projectId
        ? await taskApi.listTasks(decoded.projectId)
        : await taskApi.listAllTasks();
      await applyRemoteTasks(refreshed);
    } catch {
      // Next regular pull will reconcile the canonical order.
    }
  }
}

async function rollbackRejectedDocsDelete(
  operation: SyncPushOperation | undefined,
  pendingOperation: PendingOutbox | undefined,
  result: { entity?: Record<string, unknown> },
): Promise<boolean> {
  if (
    operation?.table !== "knowledge_nodes"
    || operation.action !== "delete"
    || result.entity?.archived_at
  ) {
    return false;
  }
  try {
    await rollbackDocsArchiveOptimistic(
      operation.entity_id,
      pendingOperation?.payload ?? operation.payload,
    );
  } catch (error) {
    // Keep the original server error/conflict visible.  A failed rollback is
    // logged for diagnostics but must not make the sync loop crash.
    console.warn("[sync] failed to roll back rejected Docs archive", error instanceof Error ? error.name : "UnknownError");
  }
  return true;
}

async function pushOutbox(
  authScope: string,
  context: SyncExecutionContext,
  mode: SyncV3Mode = "v2",
): Promise<void> {
  assertSyncExecutionActive(context);
  const allPending = await listPendingOutbox(undefined, authScope);
  let pending = allPending.filter((op) => op.action !== "reorder");
  const reorderPending = allPending.filter((op) => op.action === "reorder");
  assertSyncExecutionActive(context);
  if (!pending.length && !reorderPending.length) return;
  if (!pending.length) {
    await flushReorderOutbox(reorderPending, context, authScope);
    return;
  }

  let operations: SyncPushOperation[] = pending.map((op) => ({
    op_id: op.opId,
    table: op.tableName,
    action: op.action as SyncPushOperation["action"],
    entity_id: op.entityId,
    payload: JSON.parse(op.payload || "{}") as Record<string, unknown>,
    base_updated_at: op.baseUpdatedAt ?? null,
  }));

  // v3 only changes the transport and the result conversion. The rows re-read
  // in the identity-allocation transaction are the snapshot for every receipt.
  let v3Quarantine = new Map<string, string>();
  let response: { results: SyncPushResult[] };
  if (mode === "v3") {
    const v3 = await pushOutboxViaV3(
      authScope,
      context.controller.signal,
      () => assertSyncExecutionActive(context),
      (requestId) => context.v3PushRequestIds.add(requestId),
    );
    pending = v3.pending;
    operations = v3.operations;
    response = { results: v3.results };
    v3Quarantine = v3.quarantine;
  } else {
    response = await pushSync(operations, context.controller.signal);
  }
  assertSyncExecutionActive(context);
  const docsCreateRebased = await rebaseDocsWriteFollowups(
    authScope, pending, response.results, () => assertSyncExecutionActive(context),
  );
  assertSyncExecutionActive(context);
  const operationsById = new Map(
    operations.map((operation) => [operation.op_id, operation]),
  );
  const pendingById = new Map(pending.map((operation) => [operation.opId, operation]));
  const regularResultStatuses = new Map<
    string,
    "ok" | "conflict" | "error"
  >();
  for (const result of response.results) {
    assertSyncExecutionActive(context);
    // This same-batch missing-base rejection was superseded by the exact
    // create receipt. Its update is durable and will retry with that CAS base.
    if (docsCreateRebased.updates.has(result.op_id)) continue;
    const operation = operationsById.get(result.op_id);
    const pendingOperation = pendingById.get(result.op_id);
    // Every receipt mutation is scoped to the exact sent outbox row. An
    // unknown/missing row has no snapshot and must not trigger a blind delete
    // or cache adoption from a delayed/partial response.
    if (!pendingOperation) continue;
    if (pendingOperation && isTaskPrerequisite(pendingOperation)) {
      regularResultStatuses.set(result.op_id, result.status);
    }
    // A DELETE acknowledgement is a tombstone even when the outbox row was
    // edited while the request was in flight.  Apply it before the snapshot
    // freshness branch; applyTaskTombstones performs the LWW guard against a
    // newer local update and never resurrects the row.
    if (
      result.status === "ok" &&
      operation?.table === "tasks" &&
      (operation.action === "delete" || operation.action === "restore")
    ) {
      await applyTaskPushEntity(
        operation,
        result.entity,
        result.server_updated_at,
      );
    }
    // push 中に同一エンティティが再編集されると、enqueueOutbox は同じ
    // operation_id の行を更新する。古い応答でその行を削除したり、サーバー値を
    // ローカルへ適用したりせず、応答時点のサーバー版を新しいoutboxの基準にする。
    if (pendingOperation) {
      const currentOperation = await getCurrentOutbox(result.op_id);
      if (currentOperation && operation?.table === "knowledge_nodes") {
        if (currentOperation.authScope !== pendingOperation.authScope
          || currentOperation.docsScopeKey !== pendingOperation.docsScopeKey
          || currentOperation.entityId !== pendingOperation.entityId
          || isDocsResultOlderThanRecordedConflict(currentOperation, result)) continue;
      }
      if (operation?.table === "knowledge_nodes" && pendingOperation.docsScopeKey && result.status === "ok"
        && !validateOutboxConflictServerScope(pendingOperation, result.entity).ok) {
        await markOutboxError(result.op_id, "docs:invalid_ack_scope", pendingOperation);
        continue;
      }
      if (!isOutboxSnapshotCurrent(currentOperation, pendingOperation)) {
        if (result.status === "ok") {
          // 送信した古い版はサーバーに適用済みなので、最新outboxだけを
          // 応答版へ再baseする。ローカル値そのものは決して上書きしない。
          await rebaseChangedOutbox(
            currentOperation ?? pendingOperation,
            result.entity,
            result.server_updated_at,
            pendingOperation,
            () => assertSyncExecutionActive(context),
          );
          if (isTaskPrerequisite(pendingOperation)) {
            // The sent snapshot was acknowledged, but a newer task mutation
            // is still pending under the same op id.
            regularResultStatuses.set(result.op_id, "error");
          }
          continue;
        }
        if (
          currentOperation &&
          result.status === "conflict" &&
          operation?.table === "conversation_sessions"
        ) {
          await rebaseChangedOutbox(
            currentOperation, result.entity, result.server_updated_at,
          );
          continue;
        }
        // A conflict receipt belongs to the exact sent lifecycle snapshot.
        // Once payload/error/retry/base state changed while HTTP was in flight,
        // the old response cannot rebase or clear the newer Docs operation.
        // ネットワーク/サーバーエラーは、後から積まれた最新操作へ
        // 古い応答のエラー状態を引き継がせない。
        continue;
      }
    }
    if (result.status === "ok") {
      if (result.entity && operation?.table === "projects") {
        await applyRemoteProjects([result.entity as unknown as Project]);
      }
      if (
        operation?.table !== "tasks" ||
        (operation.action !== "delete" && operation.action !== "restore")
      ) {
        await applyTaskPushEntity(
          operation,
          result.entity,
          result.server_updated_at,
        );
      }
      if (result.entity && operation?.table === "time_entries") {
        await applyRemoteTimeEntries([result.entity as unknown as TimeEntry]);
      }
      if (operation) {
        const removed = await removeAcknowledgedOutboxSnapshot(result.op_id,
        pendingOperation, { authScope, docsScopeKey: pendingOperation.docsScopeKey },
        operation.table === "knowledge_nodes" ? (w, tx) => { if (result.entity) projectDocsPushNodeInWriter(w, tx, result.entity, authScope, true); } : undefined, pendingOperation, () => assertSyncExecutionActive(context));
        if (!removed) {
          if (isTaskPrerequisite(pendingOperation)) {
            regularResultStatuses.set(result.op_id, "error");
          }
          const currentOperation = await getCurrentOutbox(result.op_id);
          await rebaseChangedOutbox(
            currentOperation ?? pendingOperation,
            result.entity,
            result.server_updated_at,
            pendingOperation,
            () => assertSyncExecutionActive(context),
          );
          continue;
        }
      }
      if (
        result.entity &&
        operation?.table === "conversation_sessions" &&
        operation.action !== "delete"
      ) {
        await applyRemoteConversationSessions([
          result.entity as unknown as ConversationSession,
        ]);
      }
      if (operation?.table !== "knowledge_nodes" && !docsCreateRebased.receipts.has(result.op_id)) {
        await applyDocsPushEntity(operation?.table, result.entity, authScope, true);
      }
      continue;
    }

    if (result.status === "conflict") {
      if (pendingOperation && operation?.table === "tasks" && operation.action === "update") {
        // The ledger key needs a token read, so it is prepared before the unit.
        const taskLedgerKey = await prepareTaskTombstoneLedgerKey();
        let rescheduleTaskNotifications = false;
        const acknowledged = await runForegroundSqliteWrite(async (writer) => {
          assertSyncExecutionActive(context);
          const current = await getCurrentOutbox(result.op_id);
          assertSyncExecutionActive(context);
          if (getTokenAuthScope(getCachedToken()) !== authScope
            || current?.authScope !== authScope
            || current.blockedReason != null
            || !isOutboxSnapshotCurrent(current, pendingOperation)
            || current.lastError !== pendingOperation.lastError
            || current.retryCount !== pendingOperation.retryCount
            || !jsonEqual(current.conflictPayload, pendingOperation.conflictPayload)) return false;
          if (!isTaskCasStaleReason(result.reason_code)
            || !hasNonEmptyString(pendingOperation.baseUpdatedAt)
            || !hasNonEmptyString(result.server_updated_at)
            || compareDocsVersions(pendingOperation.baseUpdatedAt, result.server_updated_at) !== -1
            || result.entity?.updated_at !== result.server_updated_at
            || !pendingOperation.authScope
            || !matchesTaskUpdateReceipt(operation.entity_id, operation.payload, result.entity)) {
            // Preserve both the exact device update and the returned server
            // entity. A later flight must not blindly replay this conflict.
            await markOutboxConflict(result.op_id, syncResultFailureReason(result),
              result.entity?.id === operation.entity_id ? result.entity
                : { id: operation.entity_id, unavailable: true }, pendingOperation);
            return false;
          }
          const removed = await removeOutboxOpIfSnapshot(
            result.op_id,
            pendingOperation,
            { authScope },
          );
          if (removed) {
            // Same non-restore decision as applyTaskPushEntity, applied in this unit.
            const entityId = typeof result.entity?.id === "string" && result.entity.id
              ? result.entity.id : operation.entity_id;
            const deletedAt = typeof result.entity?.deleted_at === "string" && result.entity.deleted_at
              ? result.entity.deleted_at : null;
            if (entityId) {
              if (deletedAt != null) {
                rescheduleTaskNotifications = getDb().transaction((tx) =>
                  applyTaskTombstonesInWriter(writer, tx, [{ id: entityId, deleted_at: deletedAt }], taskLedgerKey));
              } else if (result.entity) {
                const entity = result.entity as unknown as Task;
                rescheduleTaskNotifications = getDb().transaction((tx) =>
                  applyRemoteTasksInWriter(writer, tx, [entity], taskLedgerKey));
              }
            }
          }
          return removed;
        });
        if (rescheduleTaskNotifications) void rescheduleLocalTaskNotificationsFromCache();
        if (acknowledged) regularResultStatuses.set(result.op_id, "ok");
        continue;
      }
      if (
        pendingOperation &&
        operation?.table === "conversation_sessions" &&
        result.entity
      ) {
        await rebaseChangedOutbox(
          pendingOperation, result.entity, result.server_updated_at,
        );
        continue;
      }
      if (pendingOperation && operation?.table?.startsWith("knowledge_")) {
        if (await rollbackRejectedDocsDelete(operation, pendingOperation, result)) {
          await markOutboxConflict(
            result.op_id,
            syncResultFailureReason(result),
            result.entity,
            pendingOperation,
          );
          continue;
        }
        if (isLegacyDocsBaseMissingResult(result) && await rebaseLegacyBlankDocsUpdate(
          pendingOperation, result, authScope, () => assertSyncExecutionActive(context),
        )) continue;
        const resolution = await resolveDocsConflict(
          pendingOperation,
          result.entity,
          result.server_updated_at,
          result.reason_code,
        );
        if (resolution === "applied") {
          const removed = await removeAcknowledgedOutboxSnapshot(result.op_id,
          pendingOperation, { authScope, docsScopeKey: pendingOperation.docsScopeKey },
          operation.table === "knowledge_nodes" ? (w, tx) => { if (result.entity) projectDocsPushNodeInWriter(w, tx, result.entity, authScope, true); } : undefined, pendingOperation, () => assertSyncExecutionActive(context));
          if (!removed) continue;
          if (isTaskPrerequisite(pendingOperation)) {
            regularResultStatuses.set(result.op_id, "ok");
          }
          if (operation.table !== "knowledge_nodes") await applyDocsPushEntity(operation.table, result.entity, authScope);
          continue;
        }
        if (resolution === "rebased") continue;
        await markOutboxConflict(
          result.op_id,
          syncResultFailureReason(result),
          result.entity,
          pendingOperation,
        );
        continue;
      }
      if (result.entity && operation?.table === "projects") {
        await applyRemoteProjects([result.entity as unknown as Project]);
      }
      if (
        operation?.table !== "tasks" ||
        (operation.action !== "delete" && operation.action !== "restore")
      ) {
        await applyTaskPushEntity(
          operation,
          result.entity,
          result.server_updated_at,
        );
      }
      if (result.entity && operation?.table === "time_entries") {
        await applyRemoteTimeEntries([result.entity as unknown as TimeEntry]);
      }
      await applyDocsPushEntity(operation?.table, result.entity, authScope);
      if (!pendingOperation) continue;
      await markOutboxConflict(result.op_id, syncResultFailureReason(result), undefined, pendingOperation);
      continue;
    }

    if (result.status === "error") {
      await rollbackRejectedDocsDelete(operation, pendingOperation, result);
      if (!pendingOperation) continue;
      await markOutboxError(result.op_id, syncResultFailureReason(result), pendingOperation);
    }
  }
  // A permanently rejected v3 op_id is rejected again on every resend: block
  // exactly the snapshot markOutboxError produced (last_error kept as written).
  for (const result of response.results) {
    const code = v3Quarantine.get(result.op_id);
    const sent = pendingById.get(result.op_id);
    if (!code || !sent || result.status !== "error") continue;
    assertSyncExecutionActive(context);
    const expected = {
      ...sent,
      retryCount: sent.retryCount + 1,
      lastError: syncResultFailureReason(result),
    };
    await runForegroundSqliteWrite((w) => {
      // The unit may have waited behind other writes: re-check the run first.
      assertSyncExecutionActive(context);
      if (getTokenAuthScope(getCachedToken()) !== authScope) return false;
      return getDb().transaction((tx) =>
        quarantineOutboxOpIfSnapshotInWriter(w, tx, result.op_id, expected, `sync_v3_rejected_${code}`));
    });
    assertSyncExecutionActive(context);
  }
  await flushReorderOutbox(
    reorderPending,
    context,
    authScope,
    pending,
    regularResultStatuses,
  );
}

// Status listeners are UI observers. A throwing subscriber must not turn a
// committed pull into a failure, skip Docs, or strand the single-flight queue.
function updateSyncStatus(update: () => void): void {
  try {
    update();
  } catch (error) {
    console.warn(
      "[sync] status observer failed",
      error instanceof Error ? error.name : "UnknownError",
    );
  }
}

function reportSyncStage(
  authScope: string,
  context: SyncExecutionContext,
  stage: SyncStage,
): void {
  if (context.flightSequence == null) return;
  updateSyncStatus(() => useSyncStatusStore.getState().setStage(
    authScope,
    context.flightSequence!,
    stage,
  ));
}

function reportGeneralSyncProgress(
  authScope: string,
  context: SyncExecutionContext,
  page: number,
  pendingTables: number,
): void {
  if (context.flightSequence == null) return;
  updateSyncStatus(() => useSyncStatusStore.getState().setGeneralProgress(
    authScope,
    context.flightSequence!,
    { page, pendingTables },
  ));
}

function getDocsDiagnosticScope(
  scopeId?: string,
  projectId?: string | null,
): SyncDocsScope {
  if (projectId) return "project";
  if (scopeId) return "shared";
  return "personal";
}

function reportDocsSyncProgress(
  authScope: string,
  context: SyncExecutionContext,
  scopeId: string | undefined,
  projectId: string | null | undefined,
  page: number,
  retryAttempt: number | null = null,
): void {
  if (context.flightSequence == null) return;
  const update: DocsProgressUpdate = {
    scope: getDocsDiagnosticScope(scopeId, projectId),
    page,
    retryAttempt,
  };
  // applyDocsPullOnce reports before and after each page. Keep the product
  // observer at page granularity so a large pull cannot cause row-like UI
  // churn while retaining enough information to spot a stalled flight.
  const key = `${scopeId ?? ""}:${projectId ?? ""}:${update.scope}:${update.page}:${update.retryAttempt ?? 0}`;
  if (context.lastDocsProgressKey === key) return;
  context.lastDocsProgressKey = key;
  updateSyncStatus(() => useSyncStatusStore.getState().setDocsProgress(
    authScope,
    context.flightSequence!,
    update,
  ));
}

// Docs is first so a long general replay cannot starve it. A separate stage
// budget keeps one stalled Docs stage from holding the whole auth queue forever;
// the child signal prevents that stage from crossing another SQLite boundary
// after the budget expires. The parent flight remains valid for general sync.
const DOCS_STAGE_WATCHDOG_MS = 90_000;

class DocsStageWatchdogError extends Error {
  constructor() {
    super("Docs同期が制限時間内に完了しませんでした");
    this.name = "DocsStageWatchdogError";
  }
}

async function applyDocsPullWithinFlightBudget(
  authScope: string,
  context: SyncExecutionContext,
): Promise<void> {
  const controller = new AbortController();
  const abortChild = () => controller.abort();
  if (context.controller.signal.aborted) controller.abort();
  else context.controller.signal.addEventListener("abort", abortChild, { once: true });
  let timedOut = false;
  let liveWriteAtTimeout = false;
  let timeoutId: ReturnType<typeof setTimeout> | null = null;
  let parentAbort: (() => void) | null = null;
  const docsLiveWriteState = { started: false };
  const docsContext: SyncExecutionContext = {
    ...context,
    controller,
    docsLiveWriteState,
    docsDiagnostic: {
      startedAt: Date.now(),
      stage: null,
      runState: null,
      pendingTables: null,
      frozen: false,
    },
  };
  const docsFlight = applyDocsPull(authScope, docsContext);
  // A raced promise may settle after the stage has moved on. Consume its
  // rejection while child guards prevent any later local apply.
  void docsFlight.catch(() => undefined);
  try {
    const deadline = new Promise<never>((_, reject) => {
      timeoutId = setTimeout(() => {
        timedOut = true;
        liveWriteAtTimeout = docsLiveWriteState.started;
        captureDocsWatchdog(docsContext);
        controller.abort();
        reject(new DocsStageWatchdogError());
      }, DOCS_STAGE_WATCHDOG_MS);
    });
    const interrupted = new Promise<never>((_, reject) => {
      parentAbort = () => {
        controller.abort();
        if (!docsLiveWriteState.started) reject(new SyncExecutionInterruptedError());
      };
      if (context.controller.signal.aborted) parentAbort();
      else context.controller.signal.addEventListener("abort", parentAbort, { once: true });
    });
    await Promise.race([docsFlight, deadline, interrupted]);
  } catch (error) {
    if (timedOut && liveWriteAtTimeout) {
      // A compatibility live write owns the queue until its transaction and
      // rollback boundary settles. The child was already aborted, so its next
      // guard will reject; wait for that settlement before general sync.
      await docsFlight.catch(() => undefined);
      throw new DocsStageWatchdogError();
    }
    if (timedOut && !isSyncExecutionInterrupted(context)) {
      throw new DocsStageWatchdogError();
    }
    throw error;
  } finally {
    if (timeoutId !== null) clearTimeout(timeoutId);
    if (parentAbort) context.controller.signal.removeEventListener("abort", parentAbort);
    context.controller.signal.removeEventListener("abort", abortChild);
  }
}

async function performSync(
  authScope: string,
  context: SyncExecutionContext,
): Promise<"success" | "partial" | "interrupted"> {
  syncExecutionCount += 1;
  if (context.diagnostic) markSyncDiagnosticStarted(context.diagnostic);
  let hadFailure = false;
  // The protocol is decided per run and never persisted. 404/enabled=false/503
  // select v2 (identical to the pre-v3 flight); a transport failure is handled
  // like a failed outbox step.
  let mode: SyncV3Mode = "v2";
  if (isSyncV3ManuallyStopped()) return "interrupted";
  let legacyGate;
  try {
    assertSyncExecutionActive(context);
    legacyGate = await captureLegacy();
    assertSyncExecutionActive(context);
  } catch (error) {
    if (error instanceof SyncExecutionInterruptedError || isSyncExecutionInterrupted(context)) {
      return "interrupted";
    }
    console.warn("[sync] legacy capture failed", error instanceof Error ? error.name : "UnknownError");
    return "partial";
  }
  if (legacyGate.state !== "migrated" && legacyGate.state !== "not_needed") {
    return "partial";
  }
  const v3Safety = await checkSyncV3CacheSafety(authScope).catch(() => ({
    safe: false, unclassifiedRecords: 1, reason: "full_eviction_safety_unavailable",
  }));
  if (v3Safety.safe) {
    try {
      assertSyncExecutionActive(context);
      mode = await getSyncV3Mode(context.controller.signal, authScope);
      assertSyncExecutionActive(context);
    } catch (error) {
      if (error instanceof SyncExecutionInterruptedError || isSyncExecutionInterrupted(context)) {
        return "interrupted";
      }
      if (isApiConnectionError(error) || isApiTimeoutError(error)) {
        console.warn("[sync] outbox push failed", error instanceof Error ? error.name : "UnknownError");
        return "partial";
      }
      mode = "v2";
    }
  }
  let v3PullDisabled = false;
  const v2PullActive = () => mode === "v2" || v3PullDisabled;
  const steps: Array<[SyncStage, string, () => Promise<unknown>, boolean?]> = [
    ["outbox", "outbox push", () => pushOutbox(authScope, context, mode)],
    ...(mode === "v3"
      ? [["general", "v3 pull", async () => {
          try {
            const result = await runV3Pull(authScope, {
              signal: context.controller.signal,
              assertActive: () => assertSyncExecutionActive(context),
              onPage: (page) => reportGeneralSyncProgress(authScope, context, page, 0),
            });
            assertSyncExecutionActive(context);
            const generalChanged = [...result.appliedTables].some((table) =>
              (TABLES as string[]).includes(table));
            if (generalChanged && getTokenAuthScope(getCachedToken()) === authScope) {
              updateSyncStatus(() => useSyncStatusStore.getState().generalApplied(authScope));
            }
          } catch (error) {
            if (!(error instanceof SyncV3DisabledError)) throw error;
            v3PullDisabled = true;
          }
        }] as [SyncStage, string, () => Promise<unknown>, boolean?]]
      : []),
    // 一般tableの全件replayが長引くと、アプリがbackgroundへ移る前にDocsへ
    // 到達できない。Docsを先に一つのflightへ含め、HTTP200だけで完了扱いに
    // せず、staging/promotionまで進めてから一般tableを収束させる。
    // Docsのchildはparent abortと90秒stage期限のraceで解放し、期限後は
    // partialとしてgeneralへ進む。低レイヤーDB/SecureStore awaitがsignalを
    // 無視しても、進行中transactionは原子的に完了し、次のstaging/write
    // 境界のguardでchild処理を止める。この自然終了待ちは残る実行上の限界である。
    ["docs", "Docs pull", async () => {
      if (v2PullActive()) await applyDocsPullWithinFlightBudget(authScope, context);
    }, true],
    // Docsだけのtransport失敗でも後続の通常tableを試す。Docsの失敗は
    // partialとして記録し、outboxやgeneralの結果を成功へ畳み込まない。
    ["general", "general pull", async () => {
      if (!v2PullActive()) return;
      const changed = await applyGeneralPull(authScope, context);
      assertSyncExecutionActive(context);
      if (changed && getTokenAuthScope(getCachedToken()) === authScope) {
        updateSyncStatus(() => useSyncStatusStore.getState().generalApplied(authScope));
      }
    }],
    ["pending-conversations", "pending conversations", flushPendingConversations],
    ["pending-clips", "pending clip ingests", flushPendingClipIngests],
    // サーバー未到達時にモバイルLLMだけで取り込めるよう、到達できたときに
    // 取り込み先設定のキャッシュを更新する。TTL 内は内部でスキップされる。
    ["clip-targets", "clip ingest targets", refreshClipIngestTargetsIfStale],
    // オフライン中も最後に取得したsystem prompt等を使えるよう、
    // 接続できた同期のたびにサーバーの全プロフィールを更新する。
    // 既存の保留同期を、キャラクターAPIの一時的な断線で止めないため最後に行う。
    ["profiles", "character profiles", () => characterApi.refreshCache()],
  ];

  for (const [stage, label, operation, continueAfterConnectionFailure] of steps) {
    try {
      assertSyncExecutionActive(context);
      reportSyncStage(authScope, context, stage);
      await operation();
      assertSyncExecutionActive(context);
    } catch (error) {
      if (
        error instanceof SyncExecutionInterruptedError
        || isSyncExecutionInterrupted(context)
      ) {
        if (label === "Docs pull") {
          markStagedDocsPromotionsFailed(context.diagnostic);
        }
        return "interrupted";
      }
      hadFailure = true;
      if (label === "Docs pull") {
        markStagedDocsPromotionsFailed(context.diagnostic);
      }
      console.warn(
        "[sync] " + label + " failed",
        error instanceof Error ? error.name : "UnknownError",
      );
      if (label === "outbox push" && mode === "v3") {
        await Promise.all([...context.v3PushRequestIds].map((requestId) =>
          markSyncV3RequestWriteFailed(requestId).catch(() => undefined)));
      }
      // transport断では後続のAPIも同じ理由で失敗するため、このrunだけ打ち切る。
      // HTTP応答・認証・ローカルDB・個別機能の失敗では他領域の同期を継続する。
      if (isApiConnectionError(error) && !continueAfterConnectionFailure) {
        break;
      }
    }
  }

  return hadFailure ? "partial" : "success";
}

function runSyncForToken(
  token: string | null,
  diagnostic = createSyncDiagnosticReceipt(),
): Promise<void> {
  if (isMobileReadOnlyDiagnosticsEnabled()) {
    finishSyncDiagnostic(diagnostic, "skipped", "read_only_diagnostics");
    return completedSync;
  }
  if (!token) {
    finishSyncDiagnostic(diagnostic, "skipped", "no_token");
    return completedSync;
  }
  if (!syncExecutionActive) {
    finishSyncDiagnostic(diagnostic, "skipped", "not_active");
    return completedSync;
  }
  if (!canAttemptAoiTalkServer()) {
    finishSyncDiagnostic(diagnostic, "skipped", "backend_unavailable");
    return completedSync;
  }

  const authScope = getTokenAuthScope(token);
  const running = runningByAuthScope.get(authScope);
  if (running) {
    if (running.diagnostic.authGeneration !== syncDiagnosticGeneration) {
      return running.promise.then(() => runSyncForToken(token, diagnostic));
    }
    if (running.epoch === syncExecutionEpoch) {
      diagnostic.canonicalRequestId = running.diagnostic.requestId;
      finishSyncDiagnostic(diagnostic, "skipped", "already_running");
      return running.promise;
    }
    return running.promise.then(() => runSyncForToken(token, diagnostic));
  }

  const flightSequence = ++syncFlightSequence;
  const context = createSyncExecutionContext(flightSequence, diagnostic);
  const flightState: RunningSyncFlight = {
    epoch: context.epoch,
    promise: completedSync,
    mutationWakeRequested: false,
    diagnostic,
  };
  const flight = enqueueExclusive(async () => {
    const abortFlight = () => updateSyncStatus(() =>
      useSyncStatusStore.getState().abort(authScope, flightSequence));
    const scopeIsCurrent = () => activeAuthScopeTransitions.size === 0
      && diagnostic.authGeneration === syncDiagnosticGeneration
      && getTokenAuthScope(getCachedToken()) === authScope;
    try {
      assertSyncExecutionActive(context);
      // A queued old-scope flight must not touch data or publish UI state
      // after an account transition has been requested.
      if (!scopeIsCurrent()) {
        finishSyncDiagnostic(
          diagnostic,
          "skipped",
          activeAuthScopeTransitions.size > 0
            ? "active_scope_transition"
            : "auth_scope_changed",
        );
        return;
      }

      const initialMutationRevision = useSyncStatusStore.getState().mutationRevision;
      let initialOutboxState: OutboxSyncState = "retrying";
      try {
        initialOutboxState = await getOutboxState(authScope);
      } catch {
        // A durable summary failure cannot be represented as empty/success.
        initialOutboxState = "retrying";
      }
      if (flightState.mutationWakeRequested && initialOutboxState === "empty") {
        initialOutboxState = "pending";
      }
      assertSyncExecutionActive(context);
      if (!scopeIsCurrent()) {
        finishSyncDiagnostic(
          diagnostic,
          "skipped",
          activeAuthScopeTransitions.size > 0
            ? "active_scope_transition"
            : "auth_scope_changed",
        );
        return;
      }
      updateSyncStatus(() => useSyncStatusStore.getState().begin(authScope, flightSequence));
      updateSyncStatus(() => useSyncStatusStore.getState().setOutboxState(
        authScope,
        initialOutboxState,
        flightSequence,
        initialMutationRevision,
      ));

      const outcome = await performSync(authScope, context);
      if (outcome === "interrupted" || !scopeIsCurrent()) {
        finishSyncDiagnostic(
          diagnostic,
          outcome === "interrupted" ? "interrupted" : "skipped",
          outcome === "interrupted"
            ? null
            : activeAuthScopeTransitions.size > 0
              ? "active_scope_transition"
              : "auth_scope_changed",
        );
        abortFlight();
        return;
      }
      // A newer enqueue must win over a summary that was started earlier.
      const terminalMutationRevision = useSyncStatusStore.getState().mutationRevision;
      let outboxState: OutboxSyncState = "retrying";
      try {
        assertSyncExecutionActive(context);
        outboxState = await getOutboxState(authScope);
      } catch {
        outboxState = "retrying";
      }
      // The read itself can span lifecycle/account changes. Its success or
      // rejection never authorizes a terminal receipt for an obsolete flight.
      assertSyncExecutionActive(context);
      if (!scopeIsCurrent()) {
        finishSyncDiagnostic(
          diagnostic,
          "skipped",
          activeAuthScopeTransitions.size > 0
            ? "active_scope_transition"
            : "auth_scope_changed",
        );
        abortFlight();
        return;
      }
      if (flightState.mutationWakeRequested && outboxState === "empty") {
        outboxState = "pending";
      }
      updateSyncStatus(() => useSyncStatusStore.getState().setOutboxState(
        authScope,
        outboxState,
        flightSequence,
        terminalMutationRevision,
      ));
      const terminalState = useSyncStatusStore.getState();
      const terminalApplied = terminalState.authScope === authScope
        && terminalState.status === "syncing"
        && terminalState.flightSequence === flightSequence
        && terminalState.mutationRevision === terminalMutationRevision
        && terminalState.outboxState === outboxState;
      if (terminalApplied) {
        updateSyncStatus(() => useSyncStatusStore.getState().complete(
          authScope,
          outcome === "partial",
          flightSequence,
          terminalMutationRevision,
        ));
      }
      // CAS can lose to a later enqueue. Settle only our own flight while
      // retaining its durable pending/failure state; a canceled/offline tail
      // must not leave the UI permanently marked as actively syncing.
      abortFlight();
      // Terminal success/partial is the final diagnostic publication. Status
      // observers may synchronously trigger a lifecycle or auth transition,
      // so validate again after all tail/CAS/status work has settled.
      assertSyncExecutionActive(context);
      if (!scopeIsCurrent()) {
        finishSyncDiagnostic(
          diagnostic,
          "skipped",
          activeAuthScopeTransitions.size > 0
            ? "active_scope_transition"
            : "auth_scope_changed",
        );
        return;
      }
      finishSyncDiagnostic(diagnostic, outcome);
    } catch (error) {
      if (isSyncExecutionInterrupted(context) || !scopeIsCurrent()) {
        finishSyncDiagnostic(
          diagnostic,
          isSyncExecutionInterrupted(context) ? "interrupted" : "skipped",
          isSyncExecutionInterrupted(context)
            ? null
            : activeAuthScopeTransitions.size > 0
              ? "active_scope_transition"
              : "auth_scope_changed",
        );
        abortFlight();
        return;
      }
      finishSyncDiagnostic(diagnostic, "failed");
      updateSyncStatus(() => useSyncStatusStore.getState().fail(authScope, flightSequence));
      throw error;
    }
  }).finally(() => {
    releaseSyncExecutionContext(context);
    if (runningByAuthScope.get(authScope) === flightState) {
      runningByAuthScope.delete(authScope);
      if (
        flightState.mutationWakeRequested
        && flightState.epoch === syncExecutionEpoch
        && syncExecutionActive
      ) {
        // Do not await this from inside the auth-scope queue.  The new run is
        // enqueued after the current queue tail and therefore cannot recurse
        // into the operation that is still releasing the queue.
        queueMicrotask(() => wakeMutationSync(authScope));
      }
    }
  });
  flightState.promise = flight;
  runningByAuthScope.set(authScope, flightState);
  retainSyncDiagnosticCanonical(diagnostic);
  return flight;
}

registerMutationWakeListener(requestMutationSync);

/**
 * 同じ認証スコープの同期要求を完全なsingle-flightとしてまとめる。
 * 異なるscopeも共有SQLite/outboxを守るため共通queue上で直列実行する。
 */
export function runSync(): Promise<void> {
  syncRequestCount += 1;
  const diagnostic = createSyncDiagnosticReceipt();
  if (isMobileReadOnlyDiagnosticsEnabled()) {
    finishSyncDiagnostic(diagnostic, "skipped", "read_only_diagnostics");
    return completedSync;
  }
  if (!syncExecutionActive || !canAttemptAoiTalkServer()) {
    finishSyncDiagnostic(
      diagnostic,
      "skipped",
      !syncExecutionActive ? "not_active" : "backend_unavailable",
    );
    return completedSync;
  }

  const token = getCachedToken();
  if (token !== undefined) return runSyncForToken(token, diagnostic);
  if (unresolvedAuthFlight) {
    finishSyncDiagnostic(diagnostic, "skipped", "auth_lookup_pending");
    return unresolvedAuthFlight;
  }

  const tokenLookup = getToken();
  const flight = tokenLookup
    .then((resolvedToken) => runSyncForToken(resolvedToken, diagnostic))
    .catch((error: unknown) => {
      finishSyncDiagnostic(diagnostic, "failed", null, "auth_lookup_failed");
      throw error;
    })
    .finally(() => {
      if (unresolvedAuthFlight === flight) unresolvedAuthFlight = null;
    });
  unresolvedAuthFlight = flight;
  return flight;
}

/**
 * 進行中の同期と排他的に認証scopeを切り替える。
 * callback中に届いたrunSyncは、token/cache更新完了後まで開始されない。
 */
export function runAuthScopeTransition<T>(
  callback: () => T | PromiseLike<T>,
): Promise<T> {
  syncDiagnosticGeneration += 1;
  syncDiagnosticHistory.length = 0;
  syncDiagnosticCanonicalReceipts.clear();
  latestSyncDiagnosticCanonical = null;
  notifySyncDiagnosticListeners();
  const transitionToken = ++nextAuthScopeTransitionToken;
  activeAuthScopeTransitions.add(transitionToken);
  updateSyncStatus(() => useSyncStatusStore.getState().reset());
  return runQueuedAuthScopeTransition(callback).finally(() => {
    activeAuthScopeTransitions.delete(transitionToken);
    if (activeAuthScopeTransitions.size > 0) return;
    const scopes = [...deferredMutationWakeScopes];
    deferredMutationWakeScopes.clear();
    for (const scope of scopes) requestMutationSync(scope);
  });
}

export function getSyncDiagnostics(): {
  requestCount: number;
  executionCount: number;
  runningScopes: number;
} {
  return {
    requestCount: syncRequestCount,
    executionCount: syncExecutionCount,
    runningScopes: runningByAuthScope.size + (unresolvedAuthFlight ? 1 : 0),
  };
}

/** Read-only bounded receipts for the opt-in development diagnostics surface. */
export function getSyncDiagnosticSnapshot(): SyncDiagnosticSnapshot {
  const recent = syncDiagnosticHistory
    .filter((receipt) => receipt.authGeneration === syncDiagnosticGeneration)
    .map(cloneSyncDiagnosticReceipt);
  const activeEntries = getActiveSyncDiagnosticCanonicals();
  const retainedById = new Map(
    [...syncDiagnosticCanonicalReceipts.values()]
      .map((receipt) => [receipt.requestId, receipt] as const),
  );
  for (const receipt of activeEntries) retainedById.set(receipt.requestId, receipt);
  const retainedEntries = [...retainedById.values()]
    .filter((receipt) => receipt.authGeneration === syncDiagnosticGeneration);
  if (
    latestSyncDiagnosticCanonical
    && latestSyncDiagnosticCanonical.authGeneration === syncDiagnosticGeneration
    && !retainedEntries.some((receipt) => receipt.requestId === latestSyncDiagnosticCanonical?.requestId)
  ) {
    retainedEntries.push(latestSyncDiagnosticCanonical);
  }
  const retained = retainedEntries
    .slice(-SYNC_DIAGNOSTIC_HISTORY_LIMIT)
    .map(cloneSyncDiagnosticReceipt);
  const lookup = [...recent, ...retained];
  let latest = recent[recent.length - 1] ?? retained[retained.length - 1] ?? null;
  const canonicalIds = new Set<string>();
  while (latest?.canonicalRequestId && !canonicalIds.has(latest.requestId)) {
    canonicalIds.add(latest.requestId);
    const canonical = lookup.find((receipt) => receipt.requestId === latest?.canonicalRequestId);
    if (!canonical) break;
    latest = canonical;
  }
  return {
    requestCount: syncRequestCount,
    executionCount: syncExecutionCount,
    runningScopes: runningByAuthScope.size + (unresolvedAuthFlight ? 1 : 0),
    authGeneration: syncDiagnosticGeneration,
    latest,
    recent,
    retained,
  };
}

export function resetSyncDiagnostics(): void {
  syncRequestCount = 0;
  syncExecutionCount = 0;
  syncDiagnosticHistory.length = 0;
  syncDiagnosticCanonicalReceipts.clear();
  latestSyncDiagnosticCanonical = null;
  notifySyncDiagnosticListeners();
}

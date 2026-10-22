import { create } from "zustand";

export type SyncStatus = "idle" | "pending" | "syncing" | "success" | "partial" | "error";

/** Durable mutation state is separate from the last pull/push flight result. */
export type OutboxSyncState = "empty" | "pending" | "retrying" | "failed";

/** Internal sync stages exposed as coarse, read-only product diagnostics. */
export type SyncStage =
  | "outbox"
  | "general"
  | "docs"
  | "pending-conversations"
  | "pending-clips"
  | "clip-targets"
  | "profiles";

export type SyncOutcome = "success" | "partial" | "error" | "aborted";

export type SyncDocsScope = "personal" | "shared" | "project";

export type SyncProgressUpdate = {
  page: number;
  pendingTables: number;
};

export type DocsProgressUpdate = {
  scope: SyncDocsScope;
  page: number;
  retryAttempt: number | null;
};

type SyncStatusStore = {
  status: SyncStatus;
  authScope: string | null;
  /** A successful general pull is visible after the Docs phase has settled. */
  generalRevision: number;
  generalAuthScope: string | null;
  generalApplied: (authScope: string) => void;
  startedAt: number | null;
  finishedAt: number | null;
  /** Sequence of the currently visible flight. Never expose authScope in UI. */
  flightSequence: number | null;
  stage: SyncStage | null;
  stageStartedAt: number | null;
  generalPage: number | null;
  generalPendingTables: number | null;
  docsScope: SyncDocsScope | null;
  docsPage: number | null;
  docsRetryAttempt: number | null;
  lastOutcome: SyncOutcome | null;
  lastFinishedAt: number | null;
  lastSuccessfulAt: number | null;
  outboxState: OutboxSyncState;
  /** Monotonic local mutation generation used by terminal-flight CAS. */
  mutationRevision: number;
  begin: (authScope: string, flightSequence?: number) => number;
  markOutboxQueued: (authScope: string) => void;
  setOutboxState: (
    authScope: string,
    outboxState: OutboxSyncState,
    flightSequence?: number,
    expectedMutationRevision?: number,
  ) => boolean;
  setStage: (
    authScope: string,
    flightSequence: number,
    stage: SyncStage,
  ) => void;
  setGeneralProgress: (
    authScope: string,
    flightSequence: number,
    progress: SyncProgressUpdate,
  ) => void;
  setDocsProgress: (
    authScope: string,
    flightSequence: number,
    progress: DocsProgressUpdate,
  ) => void;
  complete: (
    authScope: string,
    partial?: boolean,
    flightSequence?: number,
    expectedMutationRevision?: number,
  ) => boolean;
  fail: (authScope: string, flightSequence?: number) => void;
  abort: (authScope: string, flightSequence?: number) => void;
  reset: () => void;
};

let nextFlightSequence = 0;
// Terminal receipts are deliberately kept out of the Zustand state shape so
// UI callers cannot accidentally render the auth scope. It only guards the
// same-scope handoff of a receipt across a background abort.
let lastReceiptAuthScope: string | null = null;

function isCurrentFlight(
  state: SyncStatusStore,
  authScope: string,
  flightSequence?: number,
): boolean {
  return state.authScope === authScope
    && state.status === "syncing"
    && (flightSequence == null || state.flightSequence === flightSequence);
}

function clearCurrentProgress() {
  return {
    stage: null,
    stageStartedAt: null,
    generalPage: null,
    generalPendingTables: null,
    docsScope: null,
    docsPage: null,
    docsRetryAttempt: null,
  };
}

export const useSyncStatusStore = create<SyncStatusStore>((set, get) => ({
  status: "idle",
  authScope: null,
  generalRevision: 0,
  generalAuthScope: null,
  generalApplied: (authScope) => {
    if (get().authScope !== authScope || get().status !== "syncing") return;
    set((state) => ({
      generalRevision: state.generalRevision + 1,
      generalAuthScope: authScope,
    }));
  },
  startedAt: null,
  finishedAt: null,
  flightSequence: null,
  stage: null,
  stageStartedAt: null,
  generalPage: null,
  generalPendingTables: null,
  docsScope: null,
  docsPage: null,
  docsRetryAttempt: null,
  lastOutcome: null,
  lastFinishedAt: null,
  lastSuccessfulAt: null,
  outboxState: "empty",
  mutationRevision: 0,
  begin: (authScope, requestedSequence) => {
    const previous = get();
    const flightSequence = requestedSequence ?? ++nextFlightSequence;
    nextFlightSequence = Math.max(nextFlightSequence, flightSequence);
    const preserveReceipt = lastReceiptAuthScope === authScope;
    const preserveScopeState = previous.authScope === authScope;
    if (!preserveReceipt) lastReceiptAuthScope = null;
    set({
      status: "syncing",
      authScope,
      startedAt: Date.now(),
      finishedAt: null,
      flightSequence,
      // Direct scope replacement must not show another account's queued work
      // or general-pull notification while its own durable state is read.
      outboxState: preserveScopeState ? previous.outboxState : "empty",
      generalAuthScope: preserveScopeState ? previous.generalAuthScope : null,
      lastOutcome: preserveReceipt ? previous.lastOutcome : null,
      lastFinishedAt: preserveReceipt ? previous.lastFinishedAt : null,
      lastSuccessfulAt: preserveReceipt ? previous.lastSuccessfulAt : null,
      ...clearCurrentProgress(),
    });
    return flightSequence;
  },
  markOutboxQueued: (authScope) => {
    const state = get();
    if (state.authScope !== null && state.authScope !== authScope) return;
    const preserveReceipt = lastReceiptAuthScope === authScope;
    if (!preserveReceipt) lastReceiptAuthScope = null;
    set({
      status: state.status === "syncing" || state.status === "error"
        ? state.status
        : "pending",
      authScope: state.authScope ?? authScope,
      // A new operation does not acknowledge older failed/retrying work.
      // Only a current durable summary may lower those states to pending/empty.
      outboxState: state.outboxState === "failed" || state.outboxState === "retrying"
        ? state.outboxState
        : "pending",
      mutationRevision: state.mutationRevision + 1,
      lastOutcome: preserveReceipt ? state.lastOutcome : null,
      lastFinishedAt: preserveReceipt ? state.lastFinishedAt : null,
      lastSuccessfulAt: preserveReceipt ? state.lastSuccessfulAt : null,
    });
  },
  setOutboxState: (
    authScope,
    outboxState,
    flightSequence,
    expectedMutationRevision,
  ) => {
    const state = get();
    if (
      state.authScope !== authScope
      || state.status !== "syncing"
      || (flightSequence != null && state.flightSequence !== flightSequence)
      || (
        expectedMutationRevision != null
        && state.mutationRevision !== expectedMutationRevision
      )
    ) return false;
    set({ outboxState });
    return true;
  },
  setStage: (authScope, flightSequence, stage) => {
    if (!isCurrentFlight(get(), authScope, flightSequence)) return;
    set({ stage, stageStartedAt: Date.now() });
  },
  setGeneralProgress: (authScope, flightSequence, progress) => {
    if (!isCurrentFlight(get(), authScope, flightSequence)) return;
    set({
      generalPage: Math.max(0, progress.page),
      generalPendingTables: Math.max(0, progress.pendingTables),
    });
  },
  setDocsProgress: (authScope, flightSequence, progress) => {
    if (!isCurrentFlight(get(), authScope, flightSequence)) return;
    set({
      docsScope: progress.scope,
      docsPage: Math.max(0, progress.page),
      docsRetryAttempt:
        progress.retryAttempt == null ? null : Math.max(1, progress.retryAttempt),
    });
  },
  complete: (
    authScope,
    partial = false,
    flightSequence,
    expectedMutationRevision,
  ) => {
    const state = get();
    if (
      !isCurrentFlight(state, authScope, flightSequence)
      || (
        expectedMutationRevision != null
        && state.mutationRevision !== expectedMutationRevision
      )
    ) return false;
    const finishedAt = Date.now();
    const outboxState = state.outboxState;
    const terminalStatus = outboxState === "failed"
      ? "error"
      : partial || outboxState !== "empty"
        ? "partial"
        : "success";
    lastReceiptAuthScope = authScope;
    set({
      status: terminalStatus,
      finishedAt,
      lastOutcome: terminalStatus,
      lastFinishedAt: finishedAt,
      lastSuccessfulAt: terminalStatus === "success"
        ? finishedAt
        : state.lastSuccessfulAt,
      ...clearCurrentProgress(),
    });
    return true;
  },
  fail: (authScope, flightSequence) => {
    if (!isCurrentFlight(get(), authScope, flightSequence)) return;
    const finishedAt = Date.now();
    lastReceiptAuthScope = authScope;
    set({
      status: "error",
      finishedAt,
      lastOutcome: "error",
      lastFinishedAt: finishedAt,
      ...clearCurrentProgress(),
    });
  },
  abort: (authScope, flightSequence) => {
    const state = get();
    if (!isCurrentFlight(state, authScope, flightSequence)) return;
    const finishedAt = Date.now();
    lastReceiptAuthScope = authScope;
    const hasOutbox = state.outboxState !== "empty";
    set({
      status: state.outboxState === "failed"
        ? "error"
        : hasOutbox
          ? "pending"
          : "idle",
      authScope: hasOutbox ? authScope : null,
      generalAuthScope: null,
      startedAt: null,
      finishedAt: null,
      flightSequence: null,
      lastOutcome: "aborted",
      lastFinishedAt: finishedAt,
      ...clearCurrentProgress(),
    });
  },
  reset: () => {
    lastReceiptAuthScope = null;
    set((state) => ({
      status: "idle",
      authScope: null,
      // Keep the counter monotonic so a reset cannot replay an old event.
      generalAuthScope: null,
      startedAt: null,
      finishedAt: null,
      flightSequence: null,
      lastOutcome: null,
      lastFinishedAt: null,
      lastSuccessfulAt: null,
      outboxState: "empty",
      mutationRevision: state.mutationRevision + 1,
      ...clearCurrentProgress(),
    }));
  },
}));

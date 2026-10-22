import type { AppRuntimeRun } from "./apps-api";

export const ACTIVE_RUNTIME_ACTION_STATUSES = new Set([
  "queued",
  "dispatching",
  "accepted",
  "starting",
  "running",
  "stopping",
  "unknown",
]);

export const PERMANENT_TERMINAL_RUNTIME_ACTION_STATUSES = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "canceled",
  "blocked",
  "rejected",
  "timed_out",
  "completed",
]);

// These responses prove that the server rejected the request before it could
// be accepted.  Other 4xx responses (for example 408/409/429) can still be
// the result of a transport or idempotency race, so their pending fence must
// remain available for an exact-key GET reconciliation.
export const DEFINITIVE_RUNTIME_ACTION_REJECT_STATUSES = new Set([
  400,
  401,
  403,
  404,
  405,
  410,
  415,
  422,
]);

export function isRuntimeActionActiveStatus(status: string): boolean {
  return ACTIVE_RUNTIME_ACTION_STATUSES.has(status.toLowerCase());
}

export function isRuntimeActionPermanentTerminalStatus(status: string): boolean {
  return PERMANENT_TERMINAL_RUNTIME_ACTION_STATUSES.has(status.toLowerCase());
}

export type RuntimeActionActivityState = {
  activeRunIds: ReadonlyMap<string, ReadonlySet<string>>;
  terminalRunIds: ReadonlySet<string>;
};

export function buildRuntimeScopeKey(
  appId: string | null | undefined,
  projectId: string | null | undefined,
  actorId: string | null | undefined,
): string {
  return `${appId ?? ""}:${projectId ?? "personal"}:${actorId ?? "anonymous"}`;
}

export type RuntimePendingAction = {
  action_id: string;
  idempotency_key: string;
};

export async function reconcileRuntimePendingActions(
  pending: readonly RuntimePendingAction[],
  read: (item: RuntimePendingAction) => Promise<readonly AppRuntimeRun[]>,
  forget: (item: RuntimePendingAction) => Promise<void>,
  shouldContinue: () => boolean,
): Promise<{ matchedRuns: AppRuntimeRun[]; unresolved: RuntimePendingAction[] } | null> {
  const matchedRuns: AppRuntimeRun[] = [];
  const unresolved: RuntimePendingAction[] = [];
  for (const item of pending) {
    if (!shouldContinue()) return null;
    const observed = await read(item);
    if (!shouldContinue()) return null;
    const matched = observed.find((run) => run.idempotency_key === item.idempotency_key);
    if (!matched) {
      unresolved.push(item);
      continue;
    }
    if (!isRuntimeActionPermanentTerminalStatus(matched.status ?? "")) {
      unresolved.push(item);
      matchedRuns.push(matched);
      continue;
    }
    try {
      await forget(item);
    } catch {
      unresolved.push(item);
    }
    if (!shouldContinue()) return null;
    matchedRuns.push(matched);
  }
  return { matchedRuns, unresolved };
}

export function mergeRuntimeActionActivity(
  state: RuntimeActionActivityState,
  runs: readonly Pick<AppRuntimeRun, "id" | "action_id" | "status">[],
): { activeRunIds: Map<string, Set<string>>; terminalRunIds: Set<string> } {
  const activeRunIds = new Map<string, Set<string>>();
  for (const [actionId, runIds] of state.activeRunIds) activeRunIds.set(actionId, new Set(runIds));
  const terminalRunIds = new Set(state.terminalRunIds);
  for (const run of runs) {
    if (!run.action_id) continue;
    if (isRuntimeActionActiveStatus(run.status)) {
      if (terminalRunIds.has(run.id)) continue;
      const runIds = activeRunIds.get(run.action_id) ?? new Set<string>();
      runIds.add(run.id);
      activeRunIds.set(run.action_id, runIds);
      continue;
    }
    if (!isRuntimeActionPermanentTerminalStatus(run.status)) continue;
    terminalRunIds.add(run.id);
    const runIds = activeRunIds.get(run.action_id);
    if (!runIds) continue;
    runIds.delete(run.id);
    if (!runIds.size) activeRunIds.delete(run.action_id);
  }
  return { activeRunIds, terminalRunIds };
}

export function collectActiveRuntimeActionIds(
  runs: readonly Pick<AppRuntimeRun, "action_id" | "status">[],
): string[] {
  return Array.from(new Set(
    runs
      .filter((run) => isRuntimeActionActiveStatus(run.status) && Boolean(run.action_id))
      .map((run) => run.action_id)
      .filter((actionId): actionId is string => typeof actionId === "string"),
  ));
}

export function shouldBlockRuntimeAction(
  actionId: string,
  activeActionIds: ReadonlySet<string>,
): boolean {
  return activeActionIds.has(actionId);
}

export function runtimeActionHttpStatus(cause: unknown): number | null {
  if (!cause || typeof cause !== "object") return null;
  const status = (cause as { status?: unknown }).status;
  return typeof status === "number" && Number.isInteger(status) ? status : null;
}

export function isDefinitiveRuntimeActionRejectStatus(status: number | null): boolean {
  return status !== null && DEFINITIVE_RUNTIME_ACTION_REJECT_STATUSES.has(status);
}

export function runtimePollBackoffMs(consecutiveFailures: number): number {
  const boundedFailures = Math.max(0, Math.min(4, Math.floor(consecutiveFailures)));
  return Math.min(8_000, 1_200 * (2 ** boundedFailures));
}

export const DOTS_DIALOGUE_PROVIDER_ID = "gpt-dots";
export const DOTS_DIALOGUE_CLIENT_MESSAGE_PREFIX = "dots-dialogue:";
export const DOTS_DIALOGUE_ROUTE_MODEL_ID = "dedicated-chat";

type Selection = { userId: string; sessionId: string; selected: boolean };
const selections = new Map<string, Selection>();
const pendingSelections = new Map<string, number | null>();

export type DotsDialogueRouteReadinessResult =
  | "ready"
  | "failed"
  | "cancelled"
  | "timeout";

type RouteReadinessStatus = "pending" | "ready" | "failed";

type RouteReadinessEntry = {
  status: RouteReadinessStatus;
  failed: boolean;
  pending: Map<symbol, number>;
  nextSequence: number;
  latestSequence: number;
  waiters: Set<(result: DotsDialogueRouteReadinessResult) => void>;
};

export type DotsDialogueRouteReadinessHandle = {
  settleHydration: (result: "ready" | "failed" | "cancelled") => void;
  beginMutation: () => symbol | null;
  settleMutation: (
    token: symbol | null,
    result: "ready" | "failed" | "cancelled",
  ) => void;
  cancel: () => void;
};

const routeReadiness = new Map<string, RouteReadinessEntry>();
const ROUTE_READINESS_TIMEOUT_MS = 2500;
const MAX_ROUTE_READINESS_ENTRIES = 64;

function key(userId: string, sessionId: string): string {
  return `${userId}\u0000${sessionId}`;
}

function normalizedScope(
  userId: string | null | undefined,
  sessionId: string | null | undefined,
): string | null {
  const owner = userId?.trim();
  const conversation = sessionId?.trim();
  return owner && conversation ? key(owner, conversation) : null;
}

function notifyRouteReadiness(
  entry: RouteReadinessEntry,
  result: DotsDialogueRouteReadinessResult,
): void {
  const waiters = [...entry.waiters];
  entry.waiters.clear();
  for (const waiter of waiters) waiter(result);
}

function updateRouteReadiness(entry: RouteReadinessEntry): void {
  if (entry.pending.size > 0) {
    entry.status = "pending";
    return;
  }
  const result: DotsDialogueRouteReadinessResult = entry.failed
    ? "failed"
    : "ready";
  entry.status = result;
  notifyRouteReadiness(entry, result);
}

function settleRouteReadinessToken(
  entry: RouteReadinessEntry,
  token: symbol | null,
  result: "ready" | "failed" | "cancelled",
): void {
  if (!token) return;
  const sequence = entry.pending.get(token);
  if (sequence === undefined) return;
  entry.pending.delete(token);
  if (sequence === entry.latestSequence) {
    if (result === "failed") entry.failed = true;
    if (result === "ready") entry.failed = false;
    if (result === "cancelled" && entry.pending.size === 0) {
      entry.failed = true;
    }
  }
  updateRouteReadiness(entry);
}

function cancelRouteReadiness(
  cacheKey: string,
  entry: RouteReadinessEntry,
): void {
  if (routeReadiness.get(cacheKey) !== entry) return;
  routeReadiness.delete(cacheKey);
  const waiters = [...entry.waiters];
  entry.waiters.clear();
  for (const waiter of waiters) waiter("cancelled");
}

/**
 * Register the current session route's hydration fence before the first paint.
 * A send waits on this fence so a stale session context cannot choose Muse/CLI
 * while the explicit persisted route is still being loaded.
 */
export function beginDotsDialogueRouteReadiness(
  userId: string | null | undefined,
  sessionId: string | null | undefined,
): DotsDialogueRouteReadinessHandle | null {
  const cacheKey = normalizedScope(userId, sessionId);
  if (!cacheKey) return null;

  const previous = routeReadiness.get(cacheKey);
  if (previous) cancelRouteReadiness(cacheKey, previous);

  const entry: RouteReadinessEntry = {
    status: "pending",
    failed: false,
    pending: new Map<symbol, number>(),
    nextSequence: 0,
    latestSequence: 0,
    waiters: new Set(),
  };
  const hydrationToken = Symbol("dots-route-hydration");
  entry.pending.set(hydrationToken, 0);
  routeReadiness.set(cacheKey, entry);
  while (routeReadiness.size > MAX_ROUTE_READINESS_ENTRIES) {
    const oldest = routeReadiness.entries().next().value as
      | [string, RouteReadinessEntry]
      | undefined;
    if (!oldest) break;
    cancelRouteReadiness(oldest[0], oldest[1]);
  }

  return {
    settleHydration: (result) => {
      if (routeReadiness.get(cacheKey) !== entry) return;
      settleRouteReadinessToken(entry, hydrationToken, result);
    },
    beginMutation: () => {
      if (routeReadiness.get(cacheKey) !== entry) return null;
      const token = Symbol("dots-route-mutation");
      const sequence = entry.nextSequence + 1;
      entry.nextSequence = sequence;
      entry.latestSequence = sequence;
      entry.failed = false;
      entry.pending.set(token, sequence);
      entry.status = "pending";
      return token;
    },
    settleMutation: (token, result) => {
      if (routeReadiness.get(cacheKey) !== entry) return;
      settleRouteReadinessToken(entry, token, result);
    },
    cancel: () => cancelRouteReadiness(cacheKey, entry),
  };
}

/**
 * Wait for the route hook's hydration and any session-settings save that
 * started after it. Missing registration is treated as ready for callers that
 * render the messaging hook without the ChatComposer; the production Chat
 * page always registers this fence in the route hook's layout effect.
 */
export async function awaitDotsDialogueRouteReady(
  userId: string | null | undefined,
  sessionId: string | null | undefined,
  timeoutMs = ROUTE_READINESS_TIMEOUT_MS,
): Promise<DotsDialogueRouteReadinessResult> {
  const cacheKey = normalizedScope(userId, sessionId);
  if (!cacheKey) return "ready";
  const entry = routeReadiness.get(cacheKey);
  if (!entry) return "ready";
  if (entry.status !== "pending") return entry.status;

  return new Promise<DotsDialogueRouteReadinessResult>((resolve) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const finish = (result: DotsDialogueRouteReadinessResult) => {
      if (settled) return;
      settled = true;
      if (timer !== null) clearTimeout(timer);
      entry.waiters.delete(finish);
      resolve(result);
    };
    entry.waiters.add(finish);
    timer = setTimeout(() => finish("timeout"), timeoutMs);
  });
}

/** Same-tab bridge between the route selector and the independent send hook. */
export function rememberDotsDialogueRoute(
  userId: string | null | undefined,
  sessionId: string | null | undefined,
  selected: boolean,
): void {
  const owner = userId?.trim();
  const conversation = sessionId?.trim();
  if (!owner || !conversation) return;
  const cacheKey = key(owner, conversation);
  selections.delete(cacheKey);
  selections.set(cacheKey, {
    userId: owner,
    sessionId: conversation,
    selected,
  });
  while (selections.size > 64) {
    const oldest = selections.keys().next().value;
    if (!oldest) break;
    selections.delete(oldest);
  }
}

/**
 * Mark an explicit GPT dots selection while its session-settings PUT is in
 * flight. The persisted session route remains the send authority; this fence
 * prevents a click that raced the PUT from dispatching through either route.
 */
export function markDotsDialogueRoutePending(
  userId: string | null | undefined,
  sessionId: string | null | undefined,
  pending: boolean,
  revision?: number,
): void {
  const owner = userId?.trim();
  const conversation = sessionId?.trim();
  if (!owner || !conversation) return;
  const cacheKey = key(owner, conversation);
  if (pending) {
    pendingSelections.set(cacheKey, revision ?? null);
  } else if (
    revision === undefined ||
    pendingSelections.get(cacheKey) === revision
  ) {
    pendingSelections.delete(cacheKey);
  }
}

export function isDotsDialogueRoutePending(
  userId: string | null | undefined,
  sessionId: string | null | undefined,
): boolean {
  const owner = userId?.trim();
  const conversation = sessionId?.trim();
  return Boolean(owner && conversation && pendingSelections.has(key(owner, conversation)));
}

/**
 * Resolve only the explicit dedicated provider selection. The persisted
 * session context covers refreshes; the keyed in-memory value covers the
 * interval between a selector change and the parent session snapshot update.
 */
export function isDotsDialogueRouteSelected(
  userId: string | null | undefined,
  sessionId: string | null | undefined,
  context?: Record<string, unknown> | null,
): boolean {
  const owner = userId?.trim();
  const conversation = sessionId?.trim();
  if (!owner || !conversation) return false;
  const cacheKey = key(owner, conversation);
  if (pendingSelections.has(cacheKey)) return false;
  const cached = selections.get(cacheKey);
  if (cached) {
    selections.delete(cacheKey);
    selections.set(cacheKey, cached);
    return cached.selected;
  }
  const settings = context?.chat_llm_settings;
  if (!settings || typeof settings !== "object" || Array.isArray(settings))
    return false;
  const route = (settings as Record<string, unknown>).main_route;
  const routeRecord =
    route && typeof route === "object" && !Array.isArray(route)
      ? (route as Record<string, unknown>)
      : null;
  return Boolean(
    routeRecord &&
      typeof routeRecord.provider === "string" &&
      routeRecord.provider.trim().toLowerCase() === DOTS_DIALOGUE_PROVIDER_ID &&
      typeof routeRecord.model === "string" &&
      routeRecord.model.trim().toLowerCase() === DOTS_DIALOGUE_ROUTE_MODEL_ID,
  );
}

export function resetDotsDialogueRouteSelectionForTests(): void {
  selections.clear();
  pendingSelections.clear();
  for (const [cacheKey, entry] of routeReadiness) {
    cancelRouteReadiness(cacheKey, entry);
  }
}

"use client";

import {
  DOCS_BOOTSTRAP_CACHE_PREFIX,
  readCachedSnapshot,
  writeCachedSnapshot,
} from "@/lib/persistent-cache";
import { EMPTY_STATE, type DocsState } from "./types";

/**
 * The full Docs editor owns mutations and focus state.  The shell only needs
 * a read-only projection of that state, so keep a tiny external store here
 * rather than mounting a second DocsWorkspace in the quick panel.
 */
export type DocsNavigationOwner = object;

export type DocsNavigationSnapshot = {
  state: DocsState;
  status: "idle" | "loading" | "ready" | "error";
  source: "publisher" | "bootstrap" | null;
  error: Error | null;
};

export type DocsBootstrapCacheEntry = {
  data: DocsState;
  etag: string | null;
};

const EMPTY_NAVIGATION_SNAPSHOT: DocsNavigationSnapshot = {
  state: EMPTY_STATE,
  status: "idle",
  source: null,
  error: null,
};

const listeners = new Set<() => void>();
const serverSnapshot = EMPTY_NAVIGATION_SNAPSHOT;
let snapshot = EMPTY_NAVIGATION_SNAPSHOT;
let canonicalOwner: DocsNavigationOwner | null = null;
let canonicalState: DocsState | null = null;
let bootstrapState: DocsState | null = null;
let bootstrapLoaded = false;
let bootstrapPromise: Promise<DocsState> | null = null;
let bootstrapFlightId = 0;
let bootstrapAbortController: AbortController | null = null;
const childrenPromises = new Map<string, Promise<Partial<DocsState>>>();
let scope = "anon";
let scopeGeneration = 0;

function isCurrentBootstrapFlight(generation: number, flightId: number) {
  return generation === scopeGeneration && flightId === bootstrapFlightId;
}

/**
 * Detach a hung or superseded bootstrap GET so the next fetchDocsBootstrap
 * can start a new request. The abandoned flight must not write snapshots
 * or clear a newer bootstrapPromise.
 */
function discardBootstrapFlight() {
  const pending = bootstrapPromise;
  const controller = bootstrapAbortController;
  bootstrapPromise = null;
  bootstrapAbortController = null;
  bootstrapFlightId += 1;
  if (pending) {
    void pending.catch(() => undefined);
  }
  try {
    controller?.abort();
  } catch {
    // ignore
  }
}

/** Drop an in-flight bootstrap so a later caller can retry. */
export function abandonDocsBootstrapInFlight() {
  discardBootstrapFlight();
}

function emit() {
  listeners.forEach((listener) => listener());
}

function setSnapshot(next: DocsNavigationSnapshot) {
  // Avoid notifying React subscribers for equivalent snapshots.  This is
  // especially important while the editor publishes on every keystroke.
  if (
    snapshot.state === next.state
    && snapshot.status === next.status
    && snapshot.source === next.source
    && snapshot.error === next.error
  ) {
    return;
  }
  snapshot = next;
  emit();
}

function isBootstrapCacheEntry(value: unknown): value is DocsBootstrapCacheEntry {
  if (!value || typeof value !== "object") return false;
  const entry = value as Partial<DocsBootstrapCacheEntry>;
  return Boolean(entry.data && Array.isArray(entry.data.nodes));
}

function isDocsState(value: unknown): value is DocsState {
  return Boolean(value && typeof value === "object" && Array.isArray((value as DocsState).nodes));
}

function publishBootstrapSnapshot(
  status: DocsNavigationSnapshot["status"],
  error: Error | null = null,
) {
  if (canonicalState) return;
  setSnapshot({
    state: bootstrapState ?? EMPTY_STATE,
    status,
    source: bootstrapState ? "bootstrap" : null,
    error,
  });
}

/** Subscribe to the read-only shell projection. */
export function subscribeDocsNavigation(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Stable client snapshot for useSyncExternalStore. */
export function getDocsNavigationSnapshot() {
  return snapshot;
}

/** Stable server snapshot for useSyncExternalStore. */
export function getDocsNavigationServerSnapshot() {
  return serverSnapshot;
}

/** Create a stable owner token for a canonical DocsWorkspace instance. */
export function createDocsNavigationOwner(): DocsNavigationOwner {
  return {};
}

/**
 * Keep the in-memory projection isolated across login/logout transitions. The
 * persistent cache itself is already user-scoped; this protects the short
 * lived module store when the shell survives a session refresh.
 */
export function configureDocsNavigationScope(userId: string | null | undefined) {
  const nextScope = userId ?? "anon";
  if (nextScope === scope) return;
  scope = nextScope;
  scopeGeneration += 1;
  canonicalOwner = null;
  canonicalState = null;
  bootstrapState = null;
  bootstrapLoaded = false;
  discardBootstrapFlight();
  childrenPromises.clear();
  setSnapshot(EMPTY_NAVIGATION_SNAPSHOT);
}

/** Publish the canonical Docs editor's state to route-independent surfaces. */
export function publishCanonicalDocsState(
  owner: DocsNavigationOwner,
  state: DocsState,
) {
  if (canonicalOwner && canonicalOwner !== owner) return;
  canonicalOwner = owner;
  canonicalState = state;
  setSnapshot({
    state,
    status: "ready",
    source: "publisher",
    error: null,
  });
}

/** Clear a publisher only if it is still the active canonical owner. */
export function clearCanonicalDocsState(owner: DocsNavigationOwner) {
  if (canonicalOwner !== owner) return;
  canonicalOwner = null;
  canonicalState = null;
  // The editor may have mutated nodes after its last bootstrap. Keep the
  // snapshot for an immediate stale view, but force the next non-Docs route to
  // revalidate instead of treating that editor-era snapshot as fresh.
  bootstrapLoaded = false;
  publishBootstrapSnapshot("idle");
}

/** Whether the canonical editor currently owns the read-only projection. */
export function hasCanonicalDocsPublisher() {
  return canonicalState !== null;
}

function stateWithNodes(current: DocsState, incoming: Partial<DocsState>): DocsState {
  const mergeById = <T extends { id: string }>(left: T[], right: T[] | undefined) => {
    if (!right) return left;
    const values = new Map(left.map((item) => [item.id, item]));
    for (const item of right) values.set(item.id, item);
    return Array.from(values.values());
  };
  const mergeByKey = <T>(left: T[], right: T[] | undefined, key: (item: T) => string) => {
    if (!right) return left;
    const values = new Map(left.map((item) => [key(item), item]));
    for (const item of right) values.set(key(item), item);
    return Array.from(values.values());
  };
  return {
    ...current,
    ...incoming,
    nodes: mergeById(current.nodes, incoming.nodes),
    placements: mergeById(current.placements, incoming.placements),
    supertags: mergeById(current.supertags, incoming.supertags),
    fields: mergeById(current.fields, incoming.fields),
    views: mergeById(current.views, incoming.views),
    projects: mergeById(current.projects, incoming.projects),
    node_supertags: mergeByKey(
      current.node_supertags,
      incoming.node_supertags,
      (item) => `${item.node_id}:${item.supertag_id}`,
    ),
    has_children_ids: Array.from(new Set([
      ...(current.has_children_ids ?? []),
      ...(incoming.has_children_ids ?? []),
    ])),
    loaded_children_parent_ids: Array.from(new Set([
      ...(current.loaded_children_parent_ids ?? []),
      ...(incoming.loaded_children_parent_ids ?? []),
    ])),
    details_loaded_ids: Array.from(new Set([
      ...(current.details_loaded_ids ?? []),
      ...(incoming.details_loaded_ids ?? []),
    ])),
    has_details_ids: Array.from(new Set([
      ...(current.has_details_ids ?? []),
      ...(incoming.has_details_ids ?? []),
    ])),
    children_next_cursor_by_parent: {
      ...(current.children_next_cursor_by_parent ?? {}),
      ...(incoming.children_next_cursor_by_parent ?? {}),
    },
    child_count_by_parent: {
      ...(current.child_count_by_parent ?? {}),
      ...(incoming.child_count_by_parent ?? {}),
    },
  };
}

/** Merge a lazy children response into the quick panel's bootstrap owner. */
export function mergeDocsNavigationState(incoming: Partial<DocsState>) {
  if (canonicalState) return canonicalState;
  bootstrapState = stateWithNodes(bootstrapState ?? EMPTY_STATE, incoming);
  bootstrapLoaded = true;
  publishBootstrapSnapshot("ready");
  return bootstrapState;
}

/**
 * Fetch the local bootstrap once per user scope. Both DocsWorkspace and the
 * quick panel call this helper, so a route transition cannot issue two GETs.
 */
export function fetchDocsBootstrap(options: {
  cached?: DocsState | null;
  etag?: string | null;
  force?: boolean;
} = {}): Promise<DocsState> {
  if (canonicalState && !options.force) return Promise.resolve(canonicalState);
  if (bootstrapPromise) return bootstrapPromise;
  if (!options.force && bootstrapLoaded && bootstrapState) return Promise.resolve(bootstrapState);
  const generation = scopeGeneration;
  const flightId = ++bootstrapFlightId;
  const controller = new AbortController();
  bootstrapAbortController = controller;
  const cachedData = options.cached && isDocsState(options.cached)
    ? options.cached
    : null;
  const cachedEtag = options.etag ?? null;
  const request = (async () => {
    let persistedData = cachedData;
    let persistedEtag = cachedEtag;
    if (!persistedData) {
      const cachedValue = await readCachedSnapshot<DocsBootstrapCacheEntry | DocsState>(
        `${DOCS_BOOTSTRAP_CACHE_PREFIX}local`,
      );
      if (isBootstrapCacheEntry(cachedValue)) {
        persistedData = cachedValue.data;
        persistedEtag = cachedValue.etag;
      } else if (isDocsState(cachedValue)) {
        persistedData = cachedValue;
      }
    }
    if (!isCurrentBootstrapFlight(generation, flightId)) return EMPTY_STATE;
    if (persistedData) {
      bootstrapState = persistedData;
      publishBootstrapSnapshot("loading");
    } else {
      publishBootstrapSnapshot("loading");
    }
    const response = await fetch("/api/docs/bootstrap", {
      credentials: "include",
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(persistedEtag ? { "If-None-Match": persistedEtag } : {}),
      },
    });
    if (!isCurrentBootstrapFlight(generation, flightId)) return EMPTY_STATE;
    let data: DocsState;
    if (response.status === 304 && persistedData) {
      data = persistedData;
    } else {
      if (!response.ok) {
        const detail = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(detail.detail || response.statusText);
      }
      const value = await response.json();
      if (!isDocsState(value)) throw new Error("Docs bootstrap response is invalid");
      data = value;
      if (!isCurrentBootstrapFlight(generation, flightId)) return EMPTY_STATE;
      const etag = response.headers.get("etag");
      void writeCachedSnapshot(`${DOCS_BOOTSTRAP_CACHE_PREFIX}local`, {
        data,
        etag,
      });
    }
    if (!isCurrentBootstrapFlight(generation, flightId)) return EMPTY_STATE;
    bootstrapState = data;
    bootstrapLoaded = true;
    publishBootstrapSnapshot("ready");
    return data;
  })()
    .catch((error) => {
      if (isCurrentBootstrapFlight(generation, flightId)) {
        bootstrapLoaded = Boolean(bootstrapState);
        publishBootstrapSnapshot("error", error instanceof Error ? error : new Error(String(error)));
      }
      throw error;
    })
    .finally(() => {
      if (!isCurrentBootstrapFlight(generation, flightId)) return;
      if (bootstrapPromise === request) bootstrapPromise = null;
      if (bootstrapAbortController === controller) bootstrapAbortController = null;
    });
  bootstrapPromise = request;
  return request;
}

/** Lazy-load a single branch without mounting the full Docs editor. */
export function fetchDocsNavigationChildren(nodeId: string, cursor?: string | null) {
  if (canonicalState) return Promise.resolve<Partial<DocsState>>(canonicalState);
  const key = `${nodeId}:${cursor ?? ""}`;
  const existing = childrenPromises.get(key);
  if (existing) return existing;
  const generation = scopeGeneration;
  const params = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  const request = fetch(`/api/docs/nodes/${encodeURIComponent(nodeId)}/children${params}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  })
    .then(async (response) => {
      if (!response.ok) {
        const detail = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(detail.detail || response.statusText);
      }
      const value = await response.json() as Partial<DocsState>;
      if (generation !== scopeGeneration) return value;
      mergeDocsNavigationState(value);
      return value;
    })
    .finally(() => {
      if (childrenPromises.get(key) === request) childrenPromises.delete(key);
    });
  childrenPromises.set(key, request);
  return request;
}

/** Reset helpers for isolated unit tests; production scopes reset on user ID. */
export function resetDocsNavigationStoreForTests() {
  scope = "anon";
  scopeGeneration += 1;
  canonicalOwner = null;
  canonicalState = null;
  bootstrapState = null;
  bootstrapLoaded = false;
  discardBootstrapFlight();
  childrenPromises.clear();
  setSnapshot(EMPTY_NAVIGATION_SNAPSHOT);
}

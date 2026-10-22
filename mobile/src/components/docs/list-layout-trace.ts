export type DocsListLayoutEvent =
  | "reset" | "focus" | "blur" | "commit" | "header-layout" | "viewport-layout"
  | "content-size" | "scroll" | "drag-begin" | "drag-end"
  | "momentum-begin" | "momentum-end" | "visible-items";

export type DocsListRestoreEvent =
  | "capture" | "arm" | "seed" | "execute" | "complete" | "drop";

type Metrics = {
  focused: boolean;
  anchorEnabled: boolean;
  clippingDisabled: boolean;
  rootCount: number;
  pullCount: number;
  canonicalPending: boolean;
  headerHeight: number;
  viewportHeight: number;
  contentHeight: number;
  offsetY: number;
  firstVisibleIndex: number;
  lastVisibleIndex: number;
  visibleCount: number;
};
type Entry = Metrics & { sequence: number; atMs: number; event: DocsListLayoutEvent };
type RestoreMetrics = {
  generation: number;
  measurementEpoch: number;
  layoutRevision: number;
  targetIndex: number;
  targetOffset: number;
  viewportY: number;
  viewportHeight: number;
  contentHeight: number;
  pending: boolean;
  ready: boolean;
  reasonCode: number;
};
type RestoreEntry = RestoreMetrics & {
  sequence: number;
  atMs: number;
  event: DocsListRestoreEvent;
};
export type DocsListLayoutSnapshot = {
  latest: Metrics;
  events: Entry[];
  restores: RestoreEntry[];
};

const initial = (): Metrics => ({
  focused: false, anchorEnabled: false, clippingDisabled: false,
  rootCount: 0, pullCount: 0, canonicalPending: false,
  headerHeight: 0, viewportHeight: 0, contentHeight: 0, offsetY: 0,
  firstVisibleIndex: -1, lastVisibleIndex: -1, visibleCount: 0,
});
const numbers = [
  "rootCount", "pullCount", "headerHeight", "viewportHeight", "contentHeight", "offsetY",
  "firstVisibleIndex", "lastVisibleIndex", "visibleCount",
] as const;
const booleans = ["focused", "anchorEnabled", "clippingDisabled", "canonicalPending"] as const;
const eventNames = new Set<DocsListLayoutEvent>([
  "reset", "focus", "blur", "commit", "header-layout", "viewport-layout",
  "content-size", "scroll", "drag-begin", "drag-end", "momentum-begin", "momentum-end", "visible-items",
]);
const restoreEventNames = new Set<DocsListRestoreEvent>([
  "capture", "arm", "seed", "execute", "complete", "drop",
]);
const restoreNumbers = [
  "generation", "measurementEpoch", "layoutRevision", "targetIndex", "targetOffset", "viewportY",
  "viewportHeight", "contentHeight", "reasonCode",
] as const;
const restoreBooleans = ["pending", "ready"] as const;

/** Memory-only bounded geometry trace. Never accept IDs, titles, text or auth data. */
export function createDocsListLayoutTrace(now: () => number = () => performance.now()) {
  let started = now();
  let latest = initial();
  let sequence = 0;
  let lastScrollAt = -Infinity;
  let events: Entry[] = [];
  let restoreSequence = 0;
  let restores: RestoreEntry[] = [];
  return {
    record(event: DocsListLayoutEvent, values: Partial<Metrics> = {}) {
      if (!eventNames.has(event)) return;
      // Runtime allowlist as well as types: never spread a caller's data into a trace.
      for (const key of numbers) {
        const value = values[key];
        if (typeof value === "number" && Number.isFinite(value)) latest[key] = value;
      }
      for (const key of booleans) {
        const value = values[key];
        if (typeof value === "boolean") latest[key] = value;
      }
      const atMs = Math.max(0, now() - started);
      if (event === "scroll" && atMs - lastScrollAt < 100) return;
      if (event === "scroll") lastScrollAt = atMs;
      events.push({ ...latest, sequence: ++sequence, atMs, event });
      if (events.length > 64) events.shift();
    },
    recordRestore(event: DocsListRestoreEvent, values: Partial<RestoreMetrics> = {}) {
      if (!restoreEventNames.has(event)) return;
      const metrics: RestoreMetrics = {
        generation: 0,
        measurementEpoch: 0,
        layoutRevision: 0,
        targetIndex: -1,
        targetOffset: 0,
        viewportY: 0,
        viewportHeight: 0,
        contentHeight: 0,
        pending: false,
        ready: false,
        reasonCode: 0,
      };
      for (const key of restoreNumbers) {
        const value = values[key];
        if (typeof value === "number" && Number.isFinite(value)) metrics[key] = value;
      }
      for (const key of restoreBooleans) {
        const value = values[key];
        if (typeof value === "boolean") metrics[key] = value;
      }
      restores.push({ ...metrics, sequence: ++restoreSequence, atMs: Math.max(0, now() - started), event });
      if (restores.length > 24) restores.shift();
    },
    reset() {
      started = now();
      latest = initial();
      sequence = 0;
      lastScrollAt = -Infinity;
      events = [];
      restoreSequence = 0;
      restores = [];
    },
    snapshot(): DocsListLayoutSnapshot {
      return {
        latest: { ...latest },
        events: events.map((entry) => ({ ...entry })),
        restores: restores.map((entry) => ({ ...entry })),
      };
    },
  };
}

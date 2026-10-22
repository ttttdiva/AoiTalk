export type OutlineInputMeasurement = {
  source: "title-tap";
  visibleRowCount: number;
  pressToInputMountMs: number;
  pressToNativeFocusMs: number;
};

/** Local opt-in QA timing only. Never accept a node ID, title or body. */
export function createOutlineInputMeasurement(
  visibleRowCount: number,
  report: (measurement: OutlineInputMeasurement) => void,
  now: () => number = () => performance.now(),
) {
  const startedAt = now();
  let mountMs: number | null = null;
  let active = true;
  const elapsed = () => Math.max(0, now() - startedAt);
  return {
    mounted() {
      if (active && mountMs === null) mountMs = elapsed();
    },
    focused() {
      if (!active || mountMs === null) return;
      active = false;
      const focusMs = elapsed();
      try {
        report({ source: "title-tap", visibleRowCount,
          pressToInputMountMs: mountMs, pressToNativeFocusMs: focusMs });
      } catch {
        // A diagnostic sink must never interrupt editing.
      }
    },
    cancel() { active = false; },
  };
}

export type OutlineEnterMeasurement = {
  source: "enter";
  sample: number;
  visibleRowCount: number;
  stage: "input-mount" | "native-focus" | "first-change" | "local-commit" | "save-error";
  elapsedMs: number;
};

/** One numeric sample per milestone; never receive document or account data. */
export function createOutlineEnterMeasurement(
  sample: number,
  visibleRowCount: number,
  report: (measurement: OutlineEnterMeasurement) => void,
  now: () => number = () => performance.now(),
) {
  const start = now();
  const seen = new Set<OutlineEnterMeasurement["stage"]>();
  return {
    mark(stage: OutlineEnterMeasurement["stage"]) {
      if (seen.has(stage)) return;
      seen.add(stage);
      try { report({ source: "enter", sample, visibleRowCount, stage, elapsedMs: Math.max(0, now() - start) }); }
      catch { /* Diagnostics must never interrupt input or persistence. */ }
    },
  };
}

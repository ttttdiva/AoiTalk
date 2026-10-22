import type { MemorySelectionObservation, MemorySelectionStage } from "@/lib/memory-selection-state";

export const MEMORY_VISUAL_STAGES: MemorySelectionStage[] = ["scope_selection", "relevance_decision", "context_budget"];
export type MemoryVisualRun = {
  key: string | null;
  phase: "idle" | "connecting" | "collecting" | "rank" | "responding";
  stage: number; played: boolean; manual: boolean; pending: boolean;
  observations: Partial<Record<MemorySelectionStage, MemorySelectionObservation>>;
};
export function newMemoryVisualRun(key: string | null): MemoryVisualRun {
  return { key, phase: "idle", stage: 0, played: false, manual: false, pending: false, observations: {} };
}
export type MemoryVisualAction =
  | { type: "observe"; key: string | null; history: readonly MemorySelectionObservation[]; live: boolean;
      readiness: "pending" | "ready" | "skip"; interrupt?: boolean }
  | { type: "elapsed"; key: string | null }
  | { type: "manual"; key: string | null; stage?: number };

/** Timers advance presentation only; all displayed statuses come from observations. */
export function memoryVisualReducer(previous: MemoryVisualRun, action: MemoryVisualAction): MemoryVisualRun {
  if (action.type !== "observe" && previous.key !== action.key) return previous;
  const state = previous.key === action.key ? previous : newMemoryVisualRun(action.key);
  if (action.type === "observe") {
    const observations = { ...state.observations };
    for (const observation of action.history) observations[observation.stage] = observation;
    if (action.interrupt) return { ...state, observations, phase: "idle", pending: false, played: state.played || state.pending };
    const requested = action.key && (action.live || state.pending) && !state.played && !state.manual && Boolean(observations.scope_selection);
    if (!requested) return { ...state, observations };
    if (action.readiness === "pending") return { ...state, observations, pending: true };
    if (action.readiness === "skip") return { ...state, observations, phase: "idle", pending: false, played: true };
    return { ...state, observations, phase: "connecting", stage: 0, played: true, pending: false };
  }
  if (action.type === "manual") return { ...state, manual: true, phase: "idle", pending: false, ...(action.stage != null ? { stage: action.stage } : {}) };
  if (state.manual) return state;
  if (state.phase === "connecting") return { ...state, phase: "collecting" };
  if (state.phase === "collecting") return { ...state, phase: "rank", stage: 0 };
  if (state.phase === "rank") {
    const next = MEMORY_VISUAL_STAGES.findIndex((stage, index) => index > state.stage && state.observations[stage]);
    if (next >= 0) return { ...state, stage: next };
    const observed = state.observations[MEMORY_VISUAL_STAGES[state.stage]];
    if (state.stage === 2 && observed && observed.status !== "running") return { ...state, phase: "responding" };
  }
  if (state.phase === "responding") return { ...state, phase: "idle" };
  return state;
}

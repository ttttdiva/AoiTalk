export type RuntimeSelectionState = {
  generation: number;
  activeRunId: string | null;
};

export type RuntimeSelectionToken = RuntimeSelectionState;

export function selectRuntimeRun(
  state: RuntimeSelectionState,
  runId: string | null,
): RuntimeSelectionToken {
  state.generation += 1;
  state.activeRunId = runId;
  return { generation: state.generation, activeRunId: runId };
}

export function captureRuntimeSelection(
  state: RuntimeSelectionState,
): RuntimeSelectionToken {
  return { generation: state.generation, activeRunId: state.activeRunId };
}

export function isCurrentRuntimeSelection(
  state: RuntimeSelectionState,
  token: RuntimeSelectionToken,
  runId: string | null,
): boolean {
  return state.generation === token.generation
    && state.activeRunId === token.activeRunId
    && state.activeRunId === runId;
}

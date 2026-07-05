const TERMINAL_AGENT_RUN_STATUSES = new Set(["succeeded", "failed", "cancelled"]);

export function isTerminalAgentRunStatus(status?: string | null): boolean {
  return Boolean(status && TERMINAL_AGENT_RUN_STATUSES.has(status));
}

export function initialAgentRunTimelineExpanded(
  live: boolean,
  runId?: string | null,
): boolean {
  return live && Boolean(runId);
}

export function shouldPollAgentRunTimeline(
  live: boolean,
  runId?: string | null,
  status?: string | null,
): boolean {
  return Boolean(live && runId && !isTerminalAgentRunStatus(status));
}

export function nextAgentRunTimelineExpanded(params: {
  runId?: string | null;
  live: boolean;
  currentExpanded: boolean;
  shouldPollLive: boolean;
  status?: string | null;
  hasRunError: boolean;
}): boolean {
  if (!params.runId) return false;
  if (!params.live) return params.currentExpanded;
  if (params.shouldPollLive) return true;
  if (
    params.status === "failed" ||
    params.status === "cancelled" ||
    params.hasRunError
  ) {
    return true;
  }
  if (params.status === "succeeded" && !params.hasRunError) {
    return false;
  }
  return params.currentExpanded;
}

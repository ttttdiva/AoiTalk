"use client";

import { useEffect, useState } from "react";
import { conversationAgentApi } from "@/lib/conversation-agent-api";
import { normalizeRuntimeProfileId } from "@/lib/agent-runtime-selection";

export type AgentRoleRuntime = {
  agentId: string;
  teamId: string | null;
  defaultProfileId: string | null;
};

/**
 * Reads the Agent's role Team and default ExecutionProfile (read-only) so the
 * runtime picker can show the role default separately from the chosen runtime.
 * Results for another Agent are discarded, so a slow response cannot leak.
 */
export function useAgentRoleRuntime(agentId: string | null | undefined): {
  runtime: AgentRoleRuntime | null;
  error: boolean;
} {
  const normalized = agentId?.trim() || null;
  const [state, setState] = useState<{
    runtime: AgentRoleRuntime | null;
    error: boolean;
  }>({ runtime: null, error: false });

  useEffect(() => {
    if (!normalized) return;
    const controller = new AbortController();
    void (async () => {
      try {
        const agent = (await conversationAgentApi.getAgent(normalized, controller.signal)).agent;
        const revisionId = agent?.latest_revision_id;
        if (!revisionId) throw new Error("revision missing");
        const revision = (
          await conversationAgentApi.getAgentRevision(revisionId, controller.signal)
        ).revision;
        if (controller.signal.aborted) return;
        setState({
          runtime: {
            agentId: normalized,
            teamId: normalizeRuntimeProfileId(revision?.agent_team_id),
            defaultProfileId: normalizeRuntimeProfileId(revision?.execution_profile_id),
          },
          error: false,
        });
      } catch {
        if (!controller.signal.aborted) setState({ runtime: null, error: true });
      }
    })();
    return () => controller.abort();
  }, [normalized]);

  return {
    runtime: state.runtime?.agentId === normalized ? state.runtime : null,
    error: Boolean(normalized) && state.error,
  };
}

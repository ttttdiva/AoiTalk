"use client";

import { useEffect, useMemo, useState } from "react";
import { chatApi } from "@/lib/chat-api";
import { conversationAgentApi } from "@/lib/conversation-agent-api";
import {
  isAgentExecutionProfileCatalog,
  type AgentExecutionProfileCatalog,
} from "@/lib/agent-runtime-selection";

export type AgentRoleRuntime = {
  agentId: string;
  agentName: string;
  revisionId: string;
  revisionVersion: number | null;
  teamId: string;
  defaultProfileId: string;
  catalog: AgentExecutionProfileCatalog;
};

export type AgentRoleRuntimeContext = {
  sessionId?: string | null;
  projectId?: string | null;
};

type RuntimeState = {
  scope: string | null;
  runtime: AgentRoleRuntime | null;
  errorMessage: string | null;
  loading: boolean;
};

const EMPTY_STATE: RuntimeState = {
  scope: null,
  runtime: null,
  errorMessage: null,
  loading: false,
};

/**
 * Reads the registered Execution Profile catalog for the exact acting role.
 * Existing chats are resolved from their immutable session binding; only a
 * new-chat draft consults the Agent's latest revision metadata.
 */
export function useAgentRoleRuntime(
  agentId: string | null | undefined,
  context: AgentRoleRuntimeContext = {},
): {
  runtime: AgentRoleRuntime | null;
  error: boolean;
  errorMessage: string | null;
  loading: boolean;
} {
  const normalizedAgentId = agentId?.trim() || null;
  const normalizedSessionId = context.sessionId?.trim() || null;
  const normalizedProjectId = context.projectId?.trim() || null;
  const scope = useMemo(
    () =>
      JSON.stringify([
        normalizedAgentId ?? "",
        normalizedSessionId ?? "",
        normalizedSessionId ? "" : normalizedProjectId ?? "",
      ]),
    [normalizedAgentId, normalizedProjectId, normalizedSessionId],
  );
  const [state, setState] = useState<RuntimeState>(EMPTY_STATE);

  useEffect(() => {
    if (!normalizedAgentId) return;
    const controller = new AbortController();
    void (async () => {
      try {
        const agentResponsePromise = conversationAgentApi.getAgent(
          normalizedAgentId,
          controller.signal,
        );
        let catalog: unknown;
        let agentResponse: Awaited<typeof agentResponsePromise>;
        if (normalizedSessionId) {
          [agentResponse, catalog] = await Promise.all([
            agentResponsePromise,
            chatApi.getAgentExecutionProfileCatalog(normalizedAgentId, {
              sessionId: normalizedSessionId,
              signal: controller.signal,
            }),
          ]);
        } else {
          agentResponse = await agentResponsePromise;
          const revisionId = agentResponse.agent?.latest_revision_id?.trim();
          if (!revisionId) throw new Error("Agent revision is missing");
          catalog = await chatApi.getAgentExecutionProfileCatalog(normalizedAgentId, {
            agentRevisionId: revisionId,
            projectId: normalizedProjectId,
            signal: controller.signal,
          });
        }
        if (controller.signal.aborted) return;
        if (!isAgentExecutionProfileCatalog(catalog)) {
          throw new Error("Execution Profile catalog is malformed");
        }
        if (catalog.agent_id !== normalizedAgentId) {
          throw new Error("Execution Profile catalog Agent mismatch");
        }
        if (
          normalizedSessionId &&
          (catalog.context_source !== "session_binding" ||
            catalog.session_id !== normalizedSessionId)
        ) {
          throw new Error("Execution Profile catalog session binding mismatch");
        }
        if (!normalizedSessionId && catalog.context_source !== "agent_revision") {
          throw new Error("Execution Profile catalog revision context mismatch");
        }
        const agent = agentResponse.agent;
        setState({
          scope,
          runtime: {
            agentId: catalog.agent_id,
            agentName: agent?.display_name?.trim() || catalog.agent_id,
            revisionId: catalog.agent_revision_id,
            revisionVersion:
              agent?.latest_revision_id === catalog.agent_revision_id &&
              typeof agent.latest_revision_version === "number"
                ? agent.latest_revision_version
                : null,
            teamId: catalog.agent_team_id,
            defaultProfileId: catalog.default_execution_profile_id,
            catalog,
          },
          errorMessage: null,
          loading: false,
        });
      } catch (error) {
        if (controller.signal.aborted) return;
        setState({
          scope,
          runtime: null,
          errorMessage:
            error instanceof Error && error.message.trim()
              ? error.message
              : "Execution Profile catalogを取得できませんでした",
          loading: false,
        });
      }
    })();
    return () => controller.abort();
  }, [normalizedAgentId, normalizedProjectId, normalizedSessionId, scope]);

  const current = state.scope === scope ? state : EMPTY_STATE;
  return {
    runtime: current.runtime,
    error: Boolean(normalizedAgentId) && Boolean(current.errorMessage),
    errorMessage: current.errorMessage,
    loading: Boolean(normalizedAgentId) && (state.scope !== scope || current.loading),
  };
}

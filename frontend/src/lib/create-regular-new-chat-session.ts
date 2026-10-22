import {
  chatApi,
  createStableClientSessionId,
  type ConversationSession,
} from "@/lib/chat-api";
import {
  fetchNewChatLlmDefaultsAfterLastUsedFlush,
  type SessionLlmSettingsResponse,
  type SessionMainRoute,
} from "@/lib/chat-llm-settings";
import { applyPendingNewChatLlmSettingsToSession } from "@/lib/new-chat-llm-settings-store";
import { hasExplicitSessionRoute } from "@/lib/chat-session-route";
import { PendingLlmHandoffError } from "@/lib/chat-session-route-handoff";

export type CreateRegularNewChatSessionInput = {
  characterName: string;
  projectId?: string;
  userId?: string | null;
  /** Optional canonical Operations Agent selected for the new session. */
  agentId?: string | null;
};

export type CreateRegularNewChatSessionResult = {
  session: ConversationSession;
  /** Agent-bound creation is authoritative on the server and has no human route handoff. */
  settings: SessionLlmSettingsResponse | null;
};

function pickExplicitLastUsedRoute(
  lastUsedMain: SessionMainRoute | undefined,
  effectiveMain: SessionMainRoute | undefined,
): SessionMainRoute | null {
  if (hasExplicitSessionRoute(effectiveMain) && effectiveMain) {
    return { ...effectiveMain };
  }
  if (hasExplicitSessionRoute(lastUsedMain) && lastUsedMain) {
    return { ...lastUsedMain };
  }
  return null;
}

/**
 * History / Quick Panel の「＋ 新規会話」。
 * Character-only Chat は last-used を GET /llm/new-chat-defaults から解決し、
 * session-settings handoff が成功するまで `/chat?s=` へ渡さない。Agent-bound Chat は
 * canonical Agent create を正本とし、human route handoff を行わない。
 */
export async function createRegularNewChatSession({
  characterName,
  projectId,
  userId,
  agentId,
}: CreateRegularNewChatSessionInput): Promise<CreateRegularNewChatSessionResult> {
  const normalizedAgentId = agentId?.trim() || null;
  if (normalizedAgentId) {
    const created = await chatApi.createSession(
      characterName,
      projectId,
      undefined,
      null,
      undefined,
      normalizedAgentId,
      createStableClientSessionId(),
    );
    return { session: created.session, settings: null };
  }

  const defaults = await fetchNewChatLlmDefaultsAfterLastUsedFlush(userId);
  const route = pickExplicitLastUsedRoute(
    defaults.last_used_main,
    defaults.effective_main,
  );
  if (!route || !hasExplicitSessionRoute(route)) {
    throw new PendingLlmHandoffError(
      "last-used の Provider / Model を確定できないため、新規会話を開きませんでした。",
    );
  }

  const created = await chatApi.createSession(
    characterName,
    projectId,
    undefined,
    null,
    route,
  );

  let applied: SessionLlmSettingsResponse | null;
  try {
    applied = await applyPendingNewChatLlmSettingsToSession(
      created.session.id,
      userId,
      route,
    );
  } catch (error) {
    if (error instanceof PendingLlmHandoffError) throw error;
    throw new PendingLlmHandoffError(
      error instanceof Error
        ? error.message
        : "last-used の Provider / Model をセッションへ確定できませんでした。",
    );
  }
  if (!applied) {
    throw new PendingLlmHandoffError(
      "last-used の Provider / Model をセッションへ確定できませんでした。",
    );
  }

  return { session: created.session, settings: applied };
}

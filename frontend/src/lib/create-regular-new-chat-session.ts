import { chatApi, type ConversationSession } from "@/lib/chat-api";
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
};

export type CreateRegularNewChatSessionResult = {
  session: ConversationSession;
  settings: SessionLlmSettingsResponse;
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
 * last-used を GET /llm/new-chat-defaults から解決し、session-settings handoff が
 * 成功するまで `/chat?s=` へ渡さない。
 */
export async function createRegularNewChatSession({
  characterName,
  projectId,
  userId,
}: CreateRegularNewChatSessionInput): Promise<CreateRegularNewChatSessionResult> {
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

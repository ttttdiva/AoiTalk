import {
  normalizeGenerationProfile,
  type GenerationProfile,
} from "@/lib/generation-profile";

const CHAT_DRAFT_HANDOFF_PREFIX = "aoitalk-chat-draft-handoff:";

type DraftStorage = {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => unknown;
  removeItem: (key: string) => unknown;
};

export type ChatDraftHandoff = {
  content: string;
  generationProfile?: GenerationProfile;
  sourceTaskId?: string;
};

function storageKey(sessionId: string) {
  return `${CHAT_DRAFT_HANDOFF_PREFIX}${sessionId}`;
}

/** Remove a pending handoff when session setup fails before navigation. */
export function clearChatDraftHandoff(storage: DraftStorage, sessionId: string) {
  try {
    storage.removeItem(storageKey(sessionId));
  } catch {
    // Storage can be disabled; cleanup is best effort and must not mask the
    // original session/reference failure.
  }
}

export function storeChatDraftHandoff(
  storage: DraftStorage,
  sessionId: string,
  draft: ChatDraftHandoff,
) {
  storage.setItem(storageKey(sessionId), JSON.stringify(draft));
}

export function takeChatDraftHandoff(
  storage: DraftStorage,
  sessionId: string,
): ChatDraftHandoff | null {
  const key = storageKey(sessionId);
  let raw: string | null;
  try {
    raw = storage.getItem(key);
  } catch {
    return null;
  }
  if (!raw) return null;

  // 削除できない場合は同じhandoffを複数回適用しない。
  try {
    if (storage.removeItem(key) === false) return null;
  } catch {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const content =
      typeof parsed.content === "string" ? parsed.content.trim() : "";
    if (!content) return null;

    const generationProfile = normalizeGenerationProfile(
      parsed.generationProfile,
    );
    const sourceTaskId =
      typeof parsed.sourceTaskId === "string" && parsed.sourceTaskId.trim()
        ? parsed.sourceTaskId.trim()
        : undefined;

    return {
      content,
      ...(generationProfile ? { generationProfile } : {}),
      ...(sourceTaskId ? { sourceTaskId } : {}),
    };
  } catch {
    return null;
  }
}

"use client";

import {
  useCallback,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { useRouter } from "next/navigation";
import { chatApi } from "@/lib/chat-api";
import type { ConversationMessage, ConversationSession } from "@/lib/chat-api";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import type { chatTimelineReducer } from "@/lib/chat-state";

type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

type UseGroupChatCreateArgs = {
  router: ReturnType<typeof useRouter>;
  activateSession: (sessionId: string) => void;
  addSession: (session: ConversationSession) => void;
  setCurrentSession: Dispatch<SetStateAction<ConversationSession | null>>;
  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
};

/**
 * グループチャット作成を担うフック。
 * `page.tsx` 由来のロジックを挙動不変で移設したもの（`use-chat-messaging` の内部から呼ぶ）。
 * 依存配列は元コードと同一に保つ。
 */
export function useGroupChatCreate({
  router,
  activateSession,
  addSession,
  setCurrentSession,
  dispatchChatTimeline,
}: UseGroupChatCreateArgs) {
  // ─── グループチャット作成 ───
  const handleCreateGroupChat = useCallback(
    async (
      characterNames: string[],
      projectId?: string,
      userIds?: string[],
      agentIds?: string[],
    ) => {
      try {
        const data = await chatApi.createGroupSession(
          characterNames,
          projectId,
          userIds,
          agentIds,
        );
        const session = data.session;
        addSession(session);
        setCurrentSession(session);

        // first_messages があれば初期メッセージとして追加
        if (data.first_messages && data.first_messages.length > 0) {
          const initialMsgs: ConversationMessage[] = data.first_messages.map(
            (fm, idx) => ({
              id: `group-init-${Date.now()}-${idx}`,
              session_id: session.id,
              role: "assistant" as const,
              content: fm.content,
              metadata: {
                character_name: fm.character_name,
                character_slug: fm.character_slug,
              },
              created_at: new Date().toISOString(),
              parent_message_id: null,
              branch_index: 0,
              is_active_branch: true,
            }),
          );
          dispatchChatTimeline({ type: "replace", messages: initialMsgs });
        } else {
          dispatchChatTimeline({ type: "clear" });
        }

        activateSession(session.id);
        const href = `/chat?s=${encodeURIComponent(session.id)}`;
        if (!navigateChatSessionInPlace(href)) {
          router.push(href);
        }
      } catch (err) {
        console.error("グループチャット作成失敗:", err);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activateSession, addSession, router],
  );

  return { handleCreateGroupChat };
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { useRouter, useSearchParams } from "next/navigation";
import type {
  ConversationMessage,
  ConversationSearchResult,
} from "@/lib/chat-api";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";

type UseConversationSearchArgs = {
  router: ReturnType<typeof useRouter>;
  searchParams: ReturnType<typeof useSearchParams>;
  activateSession: (sessionId: string) => void;
  activeSessionId: string | null;
  isLoadingMessages: boolean;
  messages: ConversationMessage[];
};

type ScrollToChatMessage = (messageId: string) => Promise<boolean>;

/**
 * 会話検索ダイアログの開閉（Ctrl+F）・検索結果選択・対象メッセージへのスクロールを担うフック。
 * `page.tsx` 由来のロジックを挙動不変で移設したもの。依存配列は元コードと同一に保つ。
 */
export function useConversationSearch({
  router,
  searchParams,
  activateSession,
  activeSessionId,
  isLoadingMessages,
  messages,
}: UseConversationSearchArgs) {
  const [conversationSearchOpen, setConversationSearchOpen] = useState(false);
  const pendingSearchTargetRef = useRef<{
    messageId: string;
    sessionId: string | null;
  } | null>(null);
  const [scrollToChatMessage, setScrollToChatMessage] = useState<ScrollToChatMessage | null>(null);
  const provideScrollToMessage = useCallback((handler: ScrollToChatMessage | null) => {
    setScrollToChatMessage(() => handler);
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (
        (event.ctrlKey || event.metaKey) &&
        !event.shiftKey &&
        !event.altKey &&
        event.key.toLowerCase() === "f"
      ) {
        event.preventDefault();
        setConversationSearchOpen(true);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  useEffect(() => {
    const pending = pendingSearchTargetRef.current;
    if (
      !pending ||
      !scrollToChatMessage ||
      isLoadingMessages ||
      activeSessionId !== pending.sessionId ||
      messages.some((message) => message.session_id !== activeSessionId)
    ) return;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      void scrollToChatMessage(pending.messageId).finally(() => {
        if (!cancelled && pendingSearchTargetRef.current === pending) {
          pendingSearchTargetRef.current = null;
        }
      });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [activeSessionId, isLoadingMessages, messages, scrollToChatMessage]);

  useEffect(() => {
    const messageId = searchParams.get("message");
    if (messageId) {
      pendingSearchTargetRef.current = {
        messageId,
        sessionId: searchParams.get("s") || activeSessionId,
      };
    }
  }, [activeSessionId, searchParams]);

  const handleSelectSearchResult = useCallback(
    (result: ConversationSearchResult) => {
      setConversationSearchOpen(false);
      pendingSearchTargetRef.current = result.message_id
        ? { messageId: result.message_id, sessionId: result.session_id }
        : null;
      activateSession(result.session_id);
      const href = `/chat?s=${encodeURIComponent(result.session_id)}`;
      if (!navigateChatSessionInPlace(href)) {
        router.push(href);
      }
    },
    [activateSession, router],
  );

  return {
    conversationSearchOpen,
    setConversationSearchOpen,
    handleSelectSearchResult,
    provideScrollToMessage,
  };
}

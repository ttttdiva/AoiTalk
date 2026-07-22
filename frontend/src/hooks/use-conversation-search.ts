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
  isLoadingMessages: boolean;
  messages: ConversationMessage[];
};

/**
 * 会話検索ダイアログの開閉（Ctrl+F）・検索結果選択・対象メッセージへのスクロールを担うフック。
 * `page.tsx` 由来のロジックを挙動不変で移設したもの。依存配列は元コードと同一に保つ。
 */
export function useConversationSearch({
  router,
  searchParams,
  activateSession,
  isLoadingMessages,
  messages,
}: UseConversationSearchArgs) {
  const [conversationSearchOpen, setConversationSearchOpen] = useState(false);
  const pendingSearchMessageIdRef = useRef<string | null>(null);

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
    const messageId = pendingSearchMessageIdRef.current;
    if (!messageId || isLoadingMessages) return;
    if (!messages.some((message) => message.id === messageId)) return;

    const timer = window.setTimeout(() => {
      const element = document.querySelector<HTMLElement>(
        `[data-chat-message-id="${messageId}"]`,
      );
      element?.scrollIntoView({ behavior: "smooth", block: "center" });
      pendingSearchMessageIdRef.current = null;
    }, 100);

    return () => window.clearTimeout(timer);
  }, [isLoadingMessages, messages]);

  useEffect(() => {
    const messageId = searchParams.get("message");
    if (messageId) pendingSearchMessageIdRef.current = messageId;
  }, [searchParams]);

  const handleSelectSearchResult = useCallback(
    (result: ConversationSearchResult) => {
      setConversationSearchOpen(false);
      pendingSearchMessageIdRef.current = result.message_id ?? null;
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
  };
}

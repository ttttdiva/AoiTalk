"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ConversationMessage } from "@/lib/chat-api";
import { CHAT_COMMANDS, commandCapabilitiesFromMessageMetadata } from "@/lib/chat-commands";
import { cn } from "@/lib/utils";

const MAX_PREVIEW_LENGTH = 120;

/**
 * Return an id that is safe to use in the DOM without relying on CSS selector
 * escaping. The original message id is deliberately not interpolated into a
 * selector; callers can always resolve this id with getElementById.
 */
export function getChatMessageDomId(messageId: string): string {
  // Hex-encode UTF-16 code units rather than replacing percent escapes so
  // different ids such as "a_b" and "a%b" cannot collapse to one DOM id.
  const encoded = messageId
    .split("")
    .map((character) => character.charCodeAt(0).toString(16).padStart(4, "0"))
    .join("");
  return `chat-message-${encoded || "empty"}`;
}

function truncatePreview(value: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= MAX_PREVIEW_LENGTH) return normalized;
  return `${normalized.slice(0, MAX_PREVIEW_LENGTH - 1)}…`;
}

function messagePreview(message: ConversationMessage): string {
  const content = truncatePreview(message.content || "");
  if (content) return content;

  const attachments = Array.isArray(message.metadata?.attachments)
    ? message.metadata.attachments.filter(
        (attachment) =>
          attachment && typeof attachment.name === "string" && attachment.name,
      )
    : [];
  if (attachments.length > 0) {
    const names = attachments.map((attachment) => attachment.name).join("、");
    return truncatePreview(`添付: ${names}`);
  }

  const capabilities = commandCapabilitiesFromMessageMetadata(message.metadata);
  if (capabilities.length > 0) {
    const labels = capabilities.map(
      (capability) =>
        CHAT_COMMANDS.find(
          (command) =>
            command.kind === "capability" && command.capability === capability,
        )?.command ?? capability,
    );
    return `コマンド: ${labels.join("、")}`;
  }

  return "（内容のないメッセージ）";
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

type ChatMessageHistoryRailProps = {
  messages: ConversationMessage[];
};

/**
 * A compact, desktop-only history rail for the user messages visible in the
 * current chat branch. It intentionally renders from the already filtered
 * visible message list so inactive branch messages never get a marker.
 */
export function ChatMessageHistoryRail({
  messages,
}: ChatMessageHistoryRailProps) {
  const userMessages = useMemo(
    () => messages.filter((message) => message.role === "user"),
    [messages],
  );
  const [hoveredMessageId, setHoveredMessageId] = useState<string | null>(
    null,
  );
  const [focusedMessageId, setFocusedMessageId] = useState<string | null>(
    null,
  );
  const [selectedMessageId, setSelectedMessageId] = useState<string | null>(
    null,
  );
  const railRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const buttonRefs = useRef(new Map<string, HTMLButtonElement>());
  const [previewTop, setPreviewTop] = useState<number | null>(null);
  const hoverIndex = useMemo(
    () =>
      hoveredMessageId === null
        ? null
        : userMessages.findIndex((message) => message.id === hoveredMessageId),
    [hoveredMessageId, userMessages],
  );
  const focusIndex = useMemo(
    () =>
      focusedMessageId === null
        ? null
        : userMessages.findIndex((message) => message.id === focusedMessageId),
    [focusedMessageId, userMessages],
  );
  // Pointer hover takes precedence, but keyboard focus is restored as soon as
  // the pointer leaves another row (including the focused row itself).
  const activeIndex =
    hoverIndex !== null && hoverIndex >= 0
      ? hoverIndex
      : focusIndex !== null && focusIndex >= 0
        ? focusIndex
        : null;

  const updatePreviewPosition = useCallback(() => {
    if (activeIndex === null) {
      setPreviewTop(null);
      return;
    }
    const message = userMessages[activeIndex];
    const rail = railRef.current;
    const button = message ? buttonRefs.current.get(message.id) : undefined;
    if (!rail || !button) return;

    const railRect = rail.getBoundingClientRect();
    const buttonRect = button.getBoundingClientRect();
    const railHeight = rail.clientHeight || railRect.height;
    const previewHeight = previewRef.current?.offsetHeight || 72;
    const halfPreview = previewHeight / 2;
    const minTop = halfPreview + 4;
    const maxTop = Math.max(minTop, railHeight - halfPreview - 4);
    const markerCenter =
      buttonRect.top + buttonRect.height / 2 - railRect.top;
    setPreviewTop(Math.min(maxTop, Math.max(minTop, markerCenter)));
  }, [activeIndex, userMessages]);

  useLayoutEffect(() => {
    // The marker and preview must be measured after commit to keep the card
    // adjacent to the active line rather than at the rail's center.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    updatePreviewPosition();
  }, [updatePreviewPosition]);

  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const handlePositionChange = () => updatePreviewPosition();
    list.addEventListener("scroll", handlePositionChange, { passive: true });
    window.addEventListener("resize", handlePositionChange);
    handlePositionChange();
    return () => {
      list.removeEventListener("scroll", handlePositionChange);
      window.removeEventListener("resize", handlePositionChange);
    };
  }, [updatePreviewPosition]);

  const scrollToMessage = useCallback((message: ConversationMessage) => {
    const target = document.getElementById(getChatMessageDomId(message.id));
    if (!target) return;

    setSelectedMessageId(message.id);
    target.scrollIntoView({
      behavior: prefersReducedMotion() ? "auto" : "smooth",
      block: "center",
    });
  }, []);

  if (userMessages.length === 0) return null;

  return (
    <aside
      className="chat-message-history-rail pointer-events-none absolute inset-y-0 left-2 z-30 w-12 items-center overflow-visible"
      aria-label="この会話の送信メッセージ履歴"
    >
      <div
        ref={railRef}
        className="pointer-events-auto relative flex max-h-[min(70vh,32rem)] w-full flex-col justify-center gap-1 overflow-visible"
      >
        <div
          ref={listRef}
          className="flex max-h-[min(70vh,32rem)] flex-col gap-1 overflow-x-hidden overflow-y-auto py-2"
          role="list"
          aria-label="送信メッセージ"
        >
          {userMessages.map((message, index) => {
            const isActive = activeIndex === index;
            const isSelected = selectedMessageId === message.id;
            const distance =
              activeIndex === null ? 0 : Math.abs(index - activeIndex);
            const lineWidth =
              activeIndex === null ? 9 : Math.max(7, 31 - distance * 7);
            const preview = messagePreview(message);
            const targetId = getChatMessageDomId(message.id);

            return (
              <div
                key={`${message.id}-${index}`}
                className="group/history-item relative flex min-h-3 shrink-0 items-center"
                role="listitem"
                onMouseEnter={() => setHoveredMessageId(message.id)}
                onMouseLeave={() => setHoveredMessageId(null)}
              >
                <button
                  ref={(element) => {
                    if (element) buttonRefs.current.set(message.id, element);
                    else buttonRefs.current.delete(message.id);
                  }}
                  type="button"
                  className={cn(
                    "relative flex h-3 w-10 items-center justify-start rounded-sm p-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 focus-visible:ring-offset-background",
                    (isActive || isSelected) && "[&>span]:bg-primary",
                  )}
                  aria-label={`メッセージ ${index + 1}: ${preview}`}
                  aria-controls={targetId}
                  aria-current={isSelected ? "location" : undefined}
                  aria-describedby={isActive ? `${targetId}-preview` : undefined}
                  data-testid="chat-message-history-item"
                  data-message-id={message.id}
                  data-distance={distance}
                  onFocus={() => setFocusedMessageId(message.id)}
                  onBlur={(event) => {
                    if (
                      !event.currentTarget.parentElement?.contains(
                        event.relatedTarget as Node | null,
                      )
                    ) {
                      setFocusedMessageId(null);
                    }
                  }}
                  onClick={() => scrollToMessage(message)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      scrollToMessage(message);
                    }
                  }}
                >
                  <span
                    aria-hidden="true"
                    className="block h-[3px] rounded-full bg-muted-foreground/60 transition-[width,background-color,transform] duration-200 ease-out motion-reduce:transition-none group-hover/history-item:bg-primary"
                    style={{ width: `${lineWidth}px` }}
                  />
                </button>
              </div>
            );
          })}
        </div>
        {activeIndex !== null && userMessages[activeIndex] && (
          <div
            ref={previewRef}
            id={`${getChatMessageDomId(userMessages[activeIndex].id)}-preview`}
            role="tooltip"
            data-anchor-message-id={userMessages[activeIndex].id}
            data-preview-top={previewTop ?? ""}
            className="pointer-events-none absolute left-11 z-40 w-56 -translate-y-1/2 rounded-lg border border-border bg-card px-3 py-2 text-left text-xs text-card-foreground shadow-lg"
            style={{ top: previewTop === null ? "50%" : `${previewTop}px` }}
          >
            <span className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              送信 {activeIndex + 1}
            </span>
            <span className="block max-h-12 overflow-hidden leading-5">
              {messagePreview(userMessages[activeIndex])}
            </span>
          </div>
        )}
      </div>
    </aside>
  );
}

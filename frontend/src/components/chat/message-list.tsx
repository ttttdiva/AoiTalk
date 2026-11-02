"use client";

import { memo, useLayoutEffect, useRef, useMemo, useState, useCallback, useEffect, type FocusEvent } from "react";
import { defaultRangeExtractor, observeElementRect, useVirtualizer } from "@tanstack/react-virtual";
import {
  Pencil,
  Check,
  X,
  Wrench,
  Loader2,
  Flag,
  Copy,
  RotateCcw,
  MoreHorizontal,
  FileText,
  Image as ImageIcon,
  AlertTriangle,
  ExternalLink,
  Search,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  GitFork,
  Bot,
} from "lucide-react";
import { MarkdownContent as SharedMarkdownContent } from "@/components/ui/markdown-content";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { AgentRunTimeline } from "@/components/chat/agent-run-timeline";
import { AgentResourceMutationList } from "@/components/chat/agent-resource-mutation-list";
import {
  ChatMessageHistoryRail,
  getChatMessageDomId,
} from "@/components/chat/chat-message-history-rail";
import type {
  ChatAttachmentMetadata,
  ChatGenerationMetrics,
  ConversationMessageHistoryIndexEntry,
  ChatToolResultMetadata,
  ChatResponseModelOption,
  ChatResponseModelSelection,
  ConversationMessage,
} from "@/lib/chat-api";
import {
  getChatScrollContentHash,
  isChatScrollPinnedToBottom,
} from "@/lib/chat-scroll";
import { getFileServeUrl, getImageThumbnailUrl } from "@/lib/explorer-serve-url";
import { getToolLabel } from "@/lib/tool-labels";
import {
  CHAT_COMMANDS,
  commandCapabilitiesFromMessageMetadata,
  type ChatCommandCapability,
} from "@/lib/chat-commands";
import { withBasePath } from "@/lib/base-path";
import { appHref, canonicalAppHref } from "@/lib/apps-link";
import { cn, formatBytes } from "@/lib/utils";
import { mediaSearchLink } from "@/lib/media-search-api";
import Link from "next/link";

// 保存確定でサーバーIDが付いても、送信済みの行とスクロール位置を維持する。
function messageRenderKey(message: ConversationMessage): string {
  const clientId = message.client_message_id || message.metadata?.client_message_id;
  return message.role === "user" && typeof clientId === "string" && clientId
    ? `user:${message.session_id}:${clientId}`
    : message.id;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const MAX_READ_ORIGINAL_LINKS = 8;
const MAX_READ_ORIGINAL_URL_LENGTH = 2048;
const SENSITIVE_ORIGINAL_QUERY_RE =
  /(?:^|[_-])(?:access[_-]?token|api[_-]?key|apikey|auth(?:orization)?|bearer|credential|password|private[_-]?key|secret|signature|sig|token)(?:$|[_-])/i;

type ChatToolResultWithName = ChatToolResultMetadata & { name?: unknown };

function isMediaReadIdentifier(value: unknown): boolean {
  if (typeof value !== "string") return false;
  const normalized = value.trim();
  if (!/^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$/u.test(normalized)) {
    return false;
  }
  return normalized.split(".").at(-1)?.toLowerCase() === "media_read";
}

function isMediaReadToolResult(result: ChatToolResultMetadata): boolean {
  const namedResult = result as ChatToolResultWithName;
  if (typeof result.tool === "string" && result.tool.trim()) {
    return isMediaReadIdentifier(result.tool);
  }
  return isMediaReadIdentifier(namedResult.name);
}

function generatedImageSrc(imageRef: string): string {
  if (!imageRef) return "";
  if (/^https?:\/\//i.test(imageRef) || imageRef.startsWith("/api/")) {
    return imageRef;
  }
  if (UUID_RE.test(imageRef)) {
    return `/api/python-proxy/api/generated-media/${imageRef}`;
  }
  return "";
}

function isDocsLink(href?: string): boolean {
  return /^\/docs\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
    href ?? "",
  );
}

/** @mention と canonical Docs参照をリンクに変換して表示 */
function MentionText({ text }: { text: string }) {
  const parts = text.split(
    /(@\[\[[^\]]+\]\]|\[\[node:[0-9a-f-]{36}\|[^\]]+\]\])/gi,
  );
  return (
    <>
      {parts.map((part, i) => {
        const docsMatch = part.match(
          /^\[\[node:([0-9a-f-]{36})\|([^\]]+)\]\]$/i,
        );
        if (docsMatch) {
          const [, id, name] = docsMatch;
          return (
            <Link
              key={i}
              href={`/docs/${id}`}
              className="inline-flex items-center gap-0.5 rounded bg-mention-docs/15 px-1 text-mention-docs underline"
            >
              {name}
            </Link>
          );
        }
        const match = part.match(
          /^@\[\[(file|task|project|app|docs|chat_session):([^:]+):([^\]]+)\]\]$/,
        );
        if (!match) return <span key={i}>{part}</span>;
        const [, type, id, name] = match;
        if (type === "file") {
          return (
            <Link
              key={i}
              href={`/filer?open=${encodeURIComponent(id)}`}
              className="inline-flex items-center gap-0.5 rounded bg-mention-file/15 px-1 text-mention-file underline"
            >
              @{name}
            </Link>
          );
        }
        if (type === "task") {
          return (
            <Link
              key={i}
              href={`/tasks/${id}`}
              className="inline-flex items-center gap-0.5 rounded bg-mention-task/15 px-1 text-mention-task underline"
            >
              @{name}
            </Link>
          );
        }
        if (type === "project") {
          return (
            <span
              key={i}
              className="inline-flex items-center gap-0.5 rounded bg-mention-project/15 px-1 text-xs font-medium text-mention-project"
            >
              @{name}
            </span>
          );
        }
        if (type === "app") {
          const href = appHref(id);
          if (!href) return <span key={i}>{part}</span>;
          return (
            <Link
              key={i}
              href={href}
              className="inline-flex items-center gap-0.5 rounded bg-mention-app/15 px-1 text-xs font-medium text-mention-app underline"
            >
              @{name}
            </Link>
          );
        }
        if (type === "docs") {
          return (
            <Link
              key={i}
              href={`/docs/${id}`}
              className="inline-flex items-center gap-0.5 rounded bg-mention-docs/15 px-1 text-xs font-medium text-mention-docs underline"
            >
              @{name}
            </Link>
          );
        }
        if (type === "chat_session") {
          return (
            <Link
              key={i}
              href={`/chat?s=${encodeURIComponent(id)}`}
              className="inline-flex items-center gap-0.5 rounded bg-mention-chat/15 px-1 text-xs font-medium text-mention-chat underline"
            >
              @{name}
            </Link>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </>
  );
}

type MessageListProps = {
  messages: ConversationMessage[];
  emptyMessage?: string;
  isStreaming: boolean;
  isWaitingResponse?: boolean;
  streamingContent?: string;
  liveToolResults?: ChatToolResultMetadata[];
  activeTool?: string | null;
  activityMessage?: string | null;
  activeAgentRunId?: string | null;
  generationKey?: string | null;
  generationStartedAt?: string | null;
  showGenerationActivity?: boolean;
  onTaskClick?: (taskId: string) => void;
  onEditMessage?: (messageId: string, newContent: string) => void;
  onForkMessage?: (message: ConversationMessage) => void | Promise<void>;
  onSwitchBranch?: (
    message: ConversationMessage,
    targetBranchIndex: number,
  ) => void | Promise<void>;
  onRerunMessage?: (
    message: ConversationMessage,
    responseModel?: ChatResponseModelSelection,
  ) => void;
  /** Retry an optimistic user turn with its original client id. */
  onRetryMessage?: (message: ConversationMessage) => void | Promise<unknown>;
  /** Retry only the durable assistant-message write for a completed report. */
  onRetryMessagePersistence?: (
    clientMessageId: string,
  ) => void | Promise<unknown>;
  /** Fetch one older page when the user reaches the top of the timeline. */
  onLoadOlderMessages?: () => boolean | void | Promise<boolean | void>;
  hasMoreMessages?: boolean;
  isLoadingOlderMessages?: boolean;
  /** Register the single history navigation path for search and deep links. */
  onProvideScrollToMessage?: (handler: ((messageId: string) => Promise<boolean>) | null) => void;
  navigationKey?: string | null;
  messageHistoryIndex?: ConversationMessageHistoryIndexEntry[];
  responseModelOptions?: ChatResponseModelOption[];
  responseModelOptionsLoading?: boolean;
};

const FEEDBACK_CATEGORIES = [
  { value: "incorrect", label: "不正確" },
  { value: "incomplete", label: "不完全" },
  { value: "slow", label: "遅い" },
  { value: "other", label: "その他" },
];

/** キャラクター名からハッシュベースの色を生成 */
function getCharacterColor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++)
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  const hue = Math.abs(hash % 360);
  return `hsl(${hue}, 70%, 60%)`;
}

function formatMessageTime(value?: string | null): string | null {
  if (!value) return null;
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return null;
  return timestamp.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function getMessageAttachments(
  message: ConversationMessage,
): ChatAttachmentMetadata[] {
  const attachments = message.metadata?.attachments;
  if (!Array.isArray(attachments)) return [];
  return attachments.filter(
    (attachment): attachment is ChatAttachmentMetadata =>
      attachment != null &&
      typeof attachment === "object" &&
      typeof (attachment as ChatAttachmentMetadata).name === "string",
  );
}

function getMessageToolResults(
  message: ConversationMessage,
): ChatToolResultMetadata[] {
  const results = message.metadata?.tool_results;
  if (!Array.isArray(results)) return [];
  return results.filter(
    (result): result is ChatToolResultMetadata =>
      result != null &&
      typeof result === "object" &&
      (typeof (result as ChatToolResultMetadata).output === "string" ||
        Array.isArray((result as ChatToolResultMetadata).urls)),
  );
}

/**
 * The server projects only verified media_read originals into `urls`.
 * Keep this renderer intentionally narrow: output JSON and assistant prose
 * are display-only and must never become citation candidates here.
 */
function readOriginalLinks(
  results?: ChatToolResultMetadata[],
): string[] {
  if (!Array.isArray(results) || typeof window === "undefined") return [];

  const links: string[] = [];
  for (const result of results) {
    if (!isMediaReadToolResult(result)) continue;
    if (!Array.isArray(result.urls)) continue;

    for (const rawUrl of result.urls) {
      if (
        typeof rawUrl !== "string" ||
        rawUrl.length === 0 ||
        rawUrl.length > MAX_READ_ORIGINAL_URL_LENGTH
      ) {
        continue;
      }
      const sanitized = mediaSearchLink(rawUrl);
      if (!sanitized) continue;

      try {
        const parsed = new URL(sanitized, window.location.origin);
        if (parsed.origin !== window.location.origin) continue;
        const hasUnsafeQuery = [...parsed.searchParams.entries()].some(
          ([key, value]) =>
            SENSITIVE_ORIGINAL_QUERY_RE.test(key) ||
            /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(value.trim()),
        );
        if (hasUnsafeQuery) {
          continue;
        }
      } catch {
        continue;
      }

      const href = withBasePath(sanitized);
      if (!links.includes(href)) links.push(href);
      if (links.length >= MAX_READ_ORIGINAL_LINKS) return links;
    }
  }
  return links;
}

function ReadOriginalLinks({
  results,
}: {
  results?: ChatToolResultMetadata[];
}) {
  const links = readOriginalLinks(results);
  if (links.length === 0) return null;

  return (
    <div
      data-testid="assistant-read-originals"
      className="flex flex-wrap items-center gap-1.5 pt-1 text-xs"
      aria-label="読んだ原本"
    >
      <span className="text-muted-foreground">読んだ原本</span>
      {links.map((href, index) => {
        const label =
          links.length === 1 ? "読んだ原本" : `読んだ原本 ${index + 1}`;
        return (
          <a
            key={href}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={label}
            className="inline-flex max-w-full items-center gap-1 rounded-full border border-primary/30 bg-primary/5 px-2 py-0.5 text-primary hover:bg-primary/10 hover:underline"
          >
            <ExternalLink className="size-3 shrink-0" aria-hidden="true" />
            <span className="truncate">
              {links.length === 1 ? "原本" : `原本 ${index + 1}`}
            </span>
          </a>
        );
      })}
    </div>
  );
}

/** capability値から /コマンド のラベル情報を逆引きする */
function commandChipInfo(
  capability: ChatCommandCapability,
): { command: string; label: string } | null {
  const match = CHAT_COMMANDS.find(
    (item) => item.kind === "capability" && item.capability === capability,
  );
  if (!match) return null;
  return { command: match.command, label: match.label };
}

/** userメッセージ本文の前に表示する、コマンド由来を示す控えめなチップ列 */
function CommandCapabilityChips({
  capabilities,
}: {
  capabilities: ChatCommandCapability[];
}) {
  if (capabilities.length === 0) return null;
  return (
    <div className="flex flex-wrap justify-end gap-1">
      {capabilities.map((capability) => {
        const info = commandChipInfo(capability);
        return (
          <span
            key={capability}
            className="inline-flex items-center rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
            title={info?.label ?? capability}
          >
            {info?.command ?? capability}
          </span>
        );
      })}
    </div>
  );
}

function toolResultsScrollKey(results: ChatToolResultMetadata[]): string {
  return results
    .map((result, index) =>
      [
        index,
        result.tool ?? "",
        result.query ?? "",
        result.output?.length ?? 0,
        result.urls?.length ?? 0,
      ].join(":"),
    )
    .join("|");
}

function metadataScrollValue(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return "";
  }
}

function getMessageGenerationMetrics(
  message: ConversationMessage,
): ChatGenerationMetrics | null {
  if (message.role !== "assistant") return null;
  const metrics = message.metadata?.generation_metrics;
  if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) {
    return null;
  }
  return metrics as ChatGenerationMetrics;
}

function getMessageResponseElapsedMs(message: ConversationMessage): number | null {
  if (message.role !== "assistant") return null;
  const value = message.metadata?.response_elapsed_ms;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return null;
  }
  return value;
}

function formatResponseDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`;
  const seconds = milliseconds / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)}秒`;
  if (seconds < 60) return `${Math.round(seconds)}秒`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.round(seconds % 60);
  return `${minutes}分${remainingSeconds.toString().padStart(2, "0")}秒`;
}

function formatTokensPerSecond(metrics: ChatGenerationMetrics | null): string | null {
  const value = metrics?.tokens_per_second;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return null;
  }
  const precision = value >= 10 ? 1 : 2;
  return `${value.toFixed(precision)} token/s`;
}

function GenerationMetricsLine({
  metrics,
  responseElapsedMs,
}: {
  metrics: ChatGenerationMetrics | null;
  responseElapsedMs: number | null;
}) {
  const durationMs = responseElapsedMs ?? metrics?.generation_ms ?? null;
  const labels = [
    typeof durationMs === "number" &&
    Number.isFinite(durationMs) &&
    durationMs >= 0
      ? `回答 ${formatResponseDuration(durationMs)}`
      : null,
    formatTokensPerSecond(metrics),
  ].filter(Boolean);

  if (labels.length === 0) return null;

  return (
    <div className="pl-1 text-[11px] leading-none text-muted-foreground/70">
      {labels.join(" / ")}
    </div>
  );
}

function ToolResultDetails({
  results,
}: {
  results?: ChatToolResultMetadata[];
}) {
  const visibleResults = (results ?? []).filter(
    (result) =>
      typeof result.output === "string" ||
      (Array.isArray(result.urls) && result.urls.length > 0),
  );
  if (visibleResults.length === 0) return null;

  return (
    <div className="mt-2 space-y-2">
      {visibleResults.map((result, index) => {
        const toolLabel = result.tool ? getToolLabel(result.tool) : "検索";
        const urls = isMediaReadToolResult(result)
          ? readOriginalLinks([result])
          : Array.isArray(result.urls)
            ? result.urls
            : [];
        return (
          <details
            key={`${result.tool ?? "tool"}-${index}`}
            className="max-w-full rounded-md border border-border/70 bg-muted/45 text-xs"
          >
            <summary className="flex min-h-9 cursor-pointer list-none items-center gap-2 px-3 py-2 text-muted-foreground [&::-webkit-details-marker]:hidden">
              <Search className="size-3.5 shrink-0" />
              <span className="min-w-0 flex-1 truncate">
                {toolLabel}
                {result.query ? `: ${result.query}` : ""}
              </span>
              {urls.length > 0 && (
                <span className="shrink-0 rounded bg-background/70 px-1.5 py-0.5 text-[11px]">
                  URL {urls.length}
                </span>
              )}
              <ChevronDown className="size-3.5 shrink-0" />
            </summary>
            <div className="space-y-2 border-t border-border/60 px-3 py-2">
              {urls.length > 0 && (
                <div className="space-y-1">
                  {urls.map((url) => (
                    <a
                      key={url}
                      href={url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex min-w-0 items-center gap-1.5 text-primary hover:underline"
                    >
                      <ExternalLink className="size-3 shrink-0" />
                      <span className="truncate">{url}</span>
                    </a>
                  ))}
                </div>
              )}
              {result.output && (
                <pre className="max-h-72 max-w-full overflow-auto whitespace-pre-wrap rounded bg-background/80 p-2 text-[11px] leading-relaxed text-foreground">
                  {result.output}
                </pre>
              )}
            </div>
          </details>
        );
      })}
    </div>
  );
}

function isImageAttachment(attachment: ChatAttachmentMetadata): boolean {
  if (attachment.mime_type?.startsWith("image/")) return true;
  return /\.(png|jpe?g|gif|webp|bmp|avif|svg)$/i.test(attachment.name);
}

function attachmentKindLabel(kind: ChatAttachmentMetadata["kind"]) {
  if (kind === "wbs") return "WBS";
  if (kind === "issue") return "課題";
  if (kind === "risk") return "リスク";
  if (kind === "request") return "依頼";
  if (kind === "attachment") return "添付";
  return null;
}

function MessageAttachments({
  attachments,
  align = "start",
}: {
  attachments: ChatAttachmentMetadata[];
  align?: "start" | "end";
}) {
  if (attachments.length === 0) return null;

  return (
    <div
      className={cn(
        "flex max-w-full flex-wrap gap-2",
        align === "end" && "justify-end",
      )}
    >
      {attachments.map((attachment, index) => {
        const isImage = isImageAttachment(attachment);
        // 共通 formatBytes は未指定/0以下を "-" で返すため、従来どおり非表示にする
        const rawSize = formatBytes(attachment.size);
        const sizeLabel = rawSize === "-" ? null : rawSize;
        const kindLabel = attachmentKindLabel(attachment.kind);
        const href = attachment.path
          ? getFileServeUrl(attachment.path)
          : undefined;
        const content = (
          <>
            {isImage && attachment.path && !attachment.upload_failed ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={getImageThumbnailUrl(attachment.path, 256)}
                alt={attachment.name}
                className="size-16 shrink-0 rounded object-cover"
                loading="lazy"
              />
            ) : (
              <span
                className={cn(
                  "flex size-10 shrink-0 items-center justify-center rounded bg-background/70",
                  attachment.upload_failed
                    ? "text-destructive"
                    : "text-muted-foreground",
                )}
              >
                {attachment.upload_failed ? (
                  <AlertTriangle className="size-4" />
                ) : isImage ? (
                  <ImageIcon className="size-4" />
                ) : (
                  <FileText className="size-4" />
                )}
              </span>
            )}
            <span className="min-w-0">
              <span className="block max-w-[220px] truncate text-xs font-medium">
                {attachment.name}
              </span>
              <span className="block text-xs text-muted-foreground">
                {attachment.upload_failed
                  ? "アップロード失敗"
                  : [kindLabel, sizeLabel].filter(Boolean).join(" / ") ||
                    "添付"}
              </span>
              {attachment.upload_failed && attachment.error ? (
                <span className="block max-w-[260px] truncate text-xs text-destructive">
                  {attachment.error}
                </span>
              ) : null}
            </span>
          </>
        );
        const className = cn(
          "inline-flex max-w-full items-center gap-2 rounded-md border bg-muted/60 p-2 text-left text-sm",
          attachment.upload_failed && "border-destructive/50 bg-destructive/10",
        );

        if (href && !attachment.upload_failed) {
          return (
            <a
              key={`${attachment.name}-${index}`}
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className={className}
              title={attachment.name}
            >
              {content}
            </a>
          );
        }

        return (
          <div
            key={`${attachment.name}-${index}`}
            className={className}
            title={attachment.error || attachment.name}
          >
            {content}
          </div>
        );
      })}
    </div>
  );
}

function ToolIndicator({ toolName }: { toolName: string }) {
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <Wrench className="size-3 animate-pulse" />
      <span>{getToolLabel(toolName)} を実行中...</span>
    </div>
  );
}

const MessageContent = memo(function MessageContent({
  content,
}: {
  content: string;
}) {
  const visibleContent = content
    .replace(/\[SCENE_DESCRIPTION:[\s\S]*?\]/g, "")
    .replace(/\[IMAGE_TRIGGER:[\s\S]*?\]/g, "")
    .trim();
  const parts = visibleContent.split(/(\[GENERATED_IMAGE:[^\]]+\])/g);
  return (
    <>
      {parts.map((part, i) => {
        const imgMatch = part.match(/^\[GENERATED_IMAGE:(.+)\]$/);
        if (imgMatch) {
          const imageRef = imgMatch[1];
          const imageSrc = generatedImageSrc(imageRef);
          if (!imageSrc) return null;
          return (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={i}
              src={imageSrc}
              alt="Generated"
              className="my-2 max-w-full rounded-lg"
              loading="lazy"
            />
          );
        }
        if (!part.trim()) return null;
        return <MarkdownContent key={i} content={part} />;
      })}
    </>
  );
});

/** Markdownを安全な外部リンクとして描画する */
function MarkdownContent({ content }: { content: string }) {
  return (
    <SharedMarkdownContent
      content={content}
      components={{
        a: ({ href, children }) => {
          const docsLink = isDocsLink(href);
          if (docsLink) {
            return (
              <Link
                href={href ?? "/docs"}
                className="my-1 inline-flex h-8 items-center gap-1.5 rounded-md border border-mention-docs/40 bg-mention-docs/15 px-3 text-xs font-medium text-mention-docs no-underline transition-colors hover:bg-mention-docs/25"
              >
                <FileText className="size-3.5" />
                Docsで開く
              </Link>
            );
          }
          const appLink = canonicalAppHref(href);
          if (appLink) {
            return (
              <Link
                href={appLink}
                className="text-primary underline [overflow-wrap:anywhere] hover:text-primary/80"
              >
                {children}
              </Link>
            );
          }
          return (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary underline [overflow-wrap:anywhere] hover:text-primary/80"
            >
              {children}
            </a>
          );
        },
      }}
    />
  );
}

/** タイピングインジケータ */
function TypingIndicator() {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:0ms]" />
      <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:150ms]" />
      <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:300ms]" />
    </span>
  );
}

/** ブランチナビゲーション */
function BranchNav({
  current,
  total,
  onPrev,
  onNext,
  disabled = false,
}: {
  current: number;
  total: number;
  onPrev: () => void;
  onNext: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
      <Button
        variant="ghost"
        size="icon-xs"
        onClick={onPrev}
        disabled={disabled || current <= 1}
        aria-label="前の分岐"
      >
        <ChevronLeft className="size-3" />
      </Button>
      <span>
        {current}/{total}
      </span>
      <Button
        variant="ghost"
        size="icon-xs"
        onClick={onNext}
        disabled={disabled || current >= total}
        aria-label="次の分岐"
      >
        <ChevronRight className="size-3" />
      </Button>
    </div>
  );
}

type BranchNavigationState = {
  index: number;
  current: number;
  total: number;
};

/**
 * Branch metadata is projected by the server.  Do not infer the number of
 * branches from the rows currently present in the active-path cache: inactive
 * siblings are intentionally not loaded by the Web client.
 */
function getBranchNavigationState(
  message: ConversationMessage,
): BranchNavigationState | null {
  const branchCount = message.branch_count;
  const total =
    typeof branchCount === "number" &&
    Number.isInteger(branchCount) &&
    branchCount > 1
      ? branchCount
      : 1;
  if (total <= 1) return null;

  const branchIndex = message.branch_index;
  const index =
    typeof branchIndex === "number" && Number.isInteger(branchIndex)
      ? Math.max(0, Math.min(branchIndex, total - 1))
      : 0;

  return { index, current: index + 1, total };
}

export function MessageList({
  messages,
  emptyMessage = "メッセージを送信して会話を開始しましょう。",
  isStreaming,
  isWaitingResponse,
  streamingContent,
  liveToolResults,
  activeTool,
  activityMessage,
  activeAgentRunId,
  generationKey,
  generationStartedAt,
  showGenerationActivity = false,
  onTaskClick,
  onEditMessage,
  onForkMessage,
  onSwitchBranch,
  onRerunMessage,
  onRetryMessage,
  onRetryMessagePersistence,
  onLoadOlderMessages,
  hasMoreMessages = false,
  isLoadingOlderMessages = false,
  onProvideScrollToMessage,
  navigationKey = null,
  messageHistoryIndex = [],
  responseModelOptions = [],
  responseModelOptionsLoading = false,
}: MessageListProps) {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const isPinnedToBottomRef = useRef(true);
  const lastBottomWriteTopRef = useRef<number | null>(null);
  const olderRequestInFlightRef = useRef(false);
  const prependReadyRef = useRef(false);
  const prependBaselineFirstIdRef = useRef<string | null>(null);
  const [prependAdjustmentVersion, setPrependAdjustmentVersion] = useState(0);
  const prependAnchorRef = useRef<{
    messageId: string | null;
    offsetTop: number;
    scrollTop: number;
    expectedScrollTop: number;
    totalSize: number;
  } | null>(null);
  const releasePrependAnchor = useCallback(() => {
    if (!prependAnchorRef.current) return;
    prependAnchorRef.current = null;
    prependReadyRef.current = false;
    prependBaselineFirstIdRef.current = null;
    const scrollContainer = scrollContainerRef.current;
    isPinnedToBottomRef.current = scrollContainer
      ? isChatScrollPinnedToBottom(scrollContainer)
      : false;
    setPrependAdjustmentVersion((version) => version + 1);
  }, []);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [openDropdownMessageId, setOpenDropdownMessageId] = useState<string | null>(null);
  const [focusedMessageId, setFocusedMessageId] = useState<string | null>(null);
  const [messageNavigationStatus, setMessageNavigationStatus] = useState<{
    navigationKey: string | null;
    text: string;
  }>({ navigationKey, text: "" });
  const [editContent, setEditContent] = useState("");
  const editTextareaRef = useRef<HTMLTextAreaElement>(null);
  const [feedbackTarget, setFeedbackTarget] =
    useState<ConversationMessage | null>(null);
  const [feedbackUserInput, setFeedbackUserInput] = useState("");
  const [feedbackCategory, setFeedbackCategory] = useState("other");
  const [feedbackComment, setFeedbackComment] = useState("");
  const [feedbackSubmitting, setFeedbackSubmitting] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [savingDocsId, setSavingDocsId] = useState<string | null>(null);
  const [savedDocsId, setSavedDocsId] = useState<string | null>(null);
  const [branchSwitchingMessageId, setBranchSwitchingMessageId] = useState<
    string | null
  >(null);
  // State updates are batched by React.  Keep a ref as the synchronous guard
  // so two rapid clicks cannot enqueue competing switch requests.
  const branchSwitchingMessageIdRef = useRef<string | null>(null);

  const resizeEditTextareaFallback = useCallback(
    (textarea: HTMLTextAreaElement) => {
      if (
        typeof CSS !== "undefined" &&
        CSS.supports?.("field-sizing", "content")
      ) {
        textarea.style.height = "";
        return;
      }

      textarea.style.height = "auto";
      const maxHeight = window.innerHeight * 0.6;
      textarea.style.height = `${Math.max(
        160,
        Math.min(textarea.scrollHeight, maxHeight),
      )}px`;
    },
    [],
  );

  const startEditing = useCallback((msg: ConversationMessage) => {
    setEditingId(msg.id);
    setEditContent(msg.content);
    setTimeout(() => {
      const textarea = editTextareaRef.current;
      if (!textarea) return;
      resizeEditTextareaFallback(textarea);
      textarea.focus();
    }, 50);
  }, [resizeEditTextareaFallback]);

  const cancelEditing = useCallback(() => {
    setEditingId(null);
    setEditContent("");
  }, []);

  const submitEdit = useCallback(() => {
    if (!editingId || !editContent.trim() || !onEditMessage) return;
    onEditMessage(editingId, editContent.trim());
    setEditingId(null);
    setEditContent("");
  }, [editingId, editContent, onEditMessage]);

  const openFeedback = useCallback(
    (message: ConversationMessage, userInput: string) => {
      setFeedbackTarget(message);
      setFeedbackUserInput(userInput);
      setFeedbackCategory("other");
      setFeedbackComment("");
    },
    [],
  );

  const submitFeedback = useCallback(async () => {
    if (!feedbackTarget) return;
    setFeedbackSubmitting(true);
    try {
      await fetch("/api/python-proxy/feedback", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: feedbackTarget.content,
          character:
            typeof feedbackTarget.metadata?.character_name === "string"
              ? feedbackTarget.metadata.character_name
              : null,
          user_input: feedbackUserInput || null,
          category: feedbackCategory,
          comment: feedbackComment.trim() || null,
          session_id: feedbackTarget.session_id,
        }),
      });
      setFeedbackTarget(null);
    } finally {
      setFeedbackSubmitting(false);
    }
  }, [feedbackCategory, feedbackComment, feedbackTarget, feedbackUserInput]);

  const copyMessage = useCallback(async (msg: ConversationMessage) => {
    await navigator.clipboard.writeText(msg.content);
    setCopiedId(msg.id);
    window.setTimeout(() => {
      setCopiedId((current) => (current === msg.id ? null : current));
    }, 1200);
  }, []);

  const saveMessageToDocs = useCallback(async (msg: ConversationMessage) => {
    if (!msg.content.trim() || savingDocsId) return;
    setSavingDocsId(msg.id);
    try {
      const todayResponse = await fetch("/api/docs/today", { credentials: "include" });
      if (!todayResponse.ok) throw new Error("Today Docsの取得に失敗しました");
      const today = await todayResponse.json() as { node?: { id?: string } };
      const parentId = today.node?.id;
      if (!parentId) throw new Error("Today Docsが見つかりません");
      const title = msg.content.trim().split(/\r?\n/)[0]?.slice(0, 120) || "Chat message";
      const response = await fetch("/api/docs", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          parent_id: parentId,
          title,
          body_text: msg.content,
          body_json: {
            source: "chat_message",
            message_id: msg.id,
            role: msg.role,
          },
          display_props: {
            source: "chat",
            chat_message_id: msg.id,
          },
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      setSavedDocsId(msg.id);
      window.setTimeout(() => {
        setSavedDocsId((current) => (current === msg.id ? null : current));
      }, 1600);
    } finally {
      setSavingDocsId(null);
    }
  }, [savingDocsId]);

  const switchBranch = useCallback(
    async (message: ConversationMessage, targetBranchIndex: number) => {
      if (!onSwitchBranch) return;
      if (isStreaming || isWaitingResponse || showGenerationActivity) return;
      if (branchSwitchingMessageIdRef.current !== null) return;

      const navigation = getBranchNavigationState(message);
      if (!navigation) return;
      if (
        !Number.isInteger(targetBranchIndex) ||
        targetBranchIndex < 0 ||
        targetBranchIndex >= navigation.total ||
        targetBranchIndex === navigation.index
      ) {
        return;
      }

      branchSwitchingMessageIdRef.current = message.id;
      setBranchSwitchingMessageId(message.id);
      try {
        const switchResult = onSwitchBranch(message, targetBranchIndex);
        // A synchronous callback has completed once it returns.  Await only
        // an actual thenable so the pending guard covers network requests
        // without unnecessarily swallowing a second click for void callers.
        if (switchResult && typeof switchResult.then === "function") {
          await switchResult;
        }
      } finally {
        if (branchSwitchingMessageIdRef.current === message.id) {
          branchSwitchingMessageIdRef.current = null;
          setBranchSwitchingMessageId(null);
        }
      }
    },
    [
      isStreaming,
      isWaitingResponse,
      onSwitchBranch,
      showGenerationActivity,
    ],
  );

  const actionButtonClass =
    "h-7 w-7 p-0 text-muted-foreground hover:text-foreground";

  const renderActions = useCallback(
    (msg: ConversationMessage, previousUserInput: string) => {
      const isPersisted = !msg.id.startsWith("temp-");
      const canRerun = Boolean(onRerunMessage && isPersisted);
      const showModelMenu = msg.role === "assistant" && canRerun;

      return (
      <div className="flex items-center gap-0.5 opacity-100 transition-opacity sm:opacity-0 sm:group-hover/msg:opacity-100 sm:group-focus-within/msg:opacity-100">
        <Button
          variant="ghost"
          size="icon"
          className={actionButtonClass}
          onClick={() => void copyMessage(msg)}
          title={copiedId === msg.id ? "コピーしました" : "コピー"}
        >
          <Copy className="size-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className={actionButtonClass}
          disabled={savingDocsId === msg.id}
          onClick={() => void saveMessageToDocs(msg)}
          title={savedDocsId === msg.id ? "Docsへ保存しました" : "Docsへ保存"}
        >
          {savingDocsId === msg.id ? <Loader2 className="size-3.5 animate-spin" /> : <FileText className="size-3.5" />}
        </Button>
        {msg.role === "user" && onEditMessage && isPersisted && (
          <Button
            variant="ghost"
            size="icon"
            className={actionButtonClass}
            onClick={() => startEditing(msg)}
            title="メッセージを編集"
          >
            <Pencil className="size-3.5" />
          </Button>
        )}
        {isPersisted && onForkMessage && (
          <Button
            variant="ghost"
            size="icon"
            className={actionButtonClass}
            onClick={() => void onForkMessage(msg)}
            title="ここから別の会話へフォーク"
          >
            <GitFork className="size-3.5" />
          </Button>
        )}
        {canRerun && (
          <Button
            variant="ghost"
            size="icon"
            className={actionButtonClass}
            onClick={() => onRerunMessage?.(msg)}
            title={msg.role === "assistant" ? "この回答を再生成" : "再実行"}
          >
            <RotateCcw className="size-3.5" />
          </Button>
        )}
        {showModelMenu && (
          <DropdownMenu
            open={openDropdownMessageId === msg.id}
            onOpenChange={(open) => setOpenDropdownMessageId(open ? msg.id : null)}
          >
            <DropdownMenuTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon"
                  className={actionButtonClass}
                  title="別モデルでこの回答を再生成"
                />
              }
            >
              <MoreHorizontal className="size-3.5" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="max-h-96 w-72 overflow-y-auto">
              {responseModelOptionsLoading && (
                <DropdownMenuItem disabled>
                  モデル一覧を読み込み中
                </DropdownMenuItem>
              )}
              {!responseModelOptionsLoading && responseModelOptions.length === 0 && (
                <DropdownMenuItem disabled>
                  再生成モデルがありません
                </DropdownMenuItem>
              )}
              {!responseModelOptionsLoading && responseModelOptions.map((option) => (
                <DropdownMenuItem
                  key={`${option.provider}:${option.model}`}
                  onClick={() => onRerunMessage?.(msg, option)}
                >
                  <span className="min-w-0 truncate">
                    {option.label} で再生成
                  </span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        {msg.role === "assistant" && (
          <Button
            variant="ghost"
            size="icon"
            className={actionButtonClass}
            onClick={() => openFeedback(msg, previousUserInput)}
            title="フィードバック"
          >
            <Flag className="size-3.5" />
          </Button>
        )}
      </div>
      );
    },
    [
      copiedId,
      copyMessage,
      savedDocsId,
      saveMessageToDocs,
      savingDocsId,
      onEditMessage,
      onForkMessage,
      onRerunMessage,
      openFeedback,
      openDropdownMessageId,
      responseModelOptions,
      responseModelOptionsLoading,
      setOpenDropdownMessageId,
      startEditing,
    ],
  );

  // Inactive siblings are normally removed by the active-path message cache.
  // Keep a small in-memory grouping guard for optimistic/transition states,
  // but never use it to determine branch counts or the selected branch.
  const branchInfo = useMemo(() => {
    const groups: Record<string, ConversationMessage[]> = {};
    for (const msg of messages) {
      const groupKey = msg.parent_message_id
        ? `parent:${msg.parent_message_id}`
        : `root:${msg.role}`;
      if (!groups[groupKey]) groups[groupKey] = [];
      groups[groupKey].push(msg);
    }
    return groups;
  }, [messages]);

  // 表示するメッセージをフィルタリング（active branch考慮）
  const visibleMessages = useMemo(() => {
    const result: ConversationMessage[] = [];
    const seen = new Set<string>();

    for (const msg of messages) {
      // A null parent is also used by legacy/optimistic linear rows.  Root
      // branch metadata is server-authoritative, so never collapse those
      // rows based on local array membership.
      const groupKey = msg.parent_message_id
        ? `parent:${msg.parent_message_id}`
        : null;
      const siblings = groupKey ? branchInfo[groupKey] : undefined;

      if (groupKey && siblings && siblings.length > 1) {
        if (seen.has(groupKey)) continue;
        seen.add(groupKey);
        const selected = siblings.find(
          (sibling) => sibling.is_active_branch !== false,
        );
        if (selected && !seen.has(selected.id)) {
          result.push(selected);
          seen.add(selected.id);
        }
        continue;
      }

      if (!seen.has(msg.id) && msg.is_active_branch !== false) {
        result.push(msg);
        seen.add(msg.id);
      }
    }
    return result;
  }, [messages, branchInfo]);
  const previousUserInputByMessageId = useMemo(() => {
    const result = new Map<string, string>();
    let previousUserInput = "";

    for (const message of visibleMessages) {
      if (message.role === "user") {
        previousUserInput = message.content || "";
      } else if (message.role === "assistant") {
        result.set(message.id, previousUserInput);
      }
    }

    return result;
  }, [visibleMessages]);
  const visibleMessageIds = useMemo(
    () => visibleMessages.map(messageRenderKey),
    [visibleMessages],
  );
  const messageIndexById = useMemo(() => {
    const indexes = new Map<string, number>();
    visibleMessages.forEach((message, index) => indexes.set(message.id, index));
    return indexes;
  }, [visibleMessages]);
  const messagePositionById = useMemo(
    () => new Map(messageHistoryIndex.map((entry) => [entry.id, entry.position])),
    [messageHistoryIndex],
  );
  const historyRailMessages = useMemo(() => {
    if (messageHistoryIndex.length === 0) return visibleMessages;
    const loadedMessagesById = new Map(visibleMessages.map((message) => [message.id, message]));
    return messageHistoryIndex.map((entry) => {
      const loaded = loadedMessagesById.get(entry.id);
      return loaded
        ? { ...entry, preview: "", content: loaded.content, metadata: loaded.metadata }
        : entry;
    });
  }, [messageHistoryIndex, visibleMessages]);
  const pinnedMessageIndexes = useMemo(() => {
    const indexes = new Set<number>();
    for (const id of [editingId, feedbackTarget?.id, openDropdownMessageId, focusedMessageId]) {
      if (!id) continue;
      const index = messageIndexById.get(id);
      if (index !== undefined) indexes.add(index);
    }
    return [...indexes].map((index) => index + (hasMoreMessages ? 1 : 0));
  }, [editingId, feedbackTarget?.id, focusedMessageId, hasMoreMessages, messageIndexById, openDropdownMessageId]);
  const messageVirtualOffset = hasMoreMessages ? 1 : 0;
  const rangeExtractor = useCallback(
    (range: Parameters<typeof defaultRangeExtractor>[0]) => {
      const indexes = new Set(defaultRangeExtractor(range));
      if (hasMoreMessages && range.count > 0) indexes.add(0);
      const nextIndex = range.endIndex + 1;
      if (nextIndex < range.count) indexes.add(nextIndex);
      for (const index of pinnedMessageIndexes) indexes.add(index);
      const prependAnchorId = prependAnchorRef.current?.messageId;
      const prependAnchorIndex = prependAnchorId
        ? messageIndexById.get(prependAnchorId)
        : undefined;
      if (prependAnchorIndex !== undefined) {
        indexes.add(prependAnchorIndex + messageVirtualOffset);
      }
      return [...indexes].sort((left, right) => left - right);
    },
    [hasMoreMessages, messageIndexById, messageVirtualOffset, pinnedMessageIndexes],
  );
  // TanStack Virtual exposes imperative methods on its virtualizer instance.
  // eslint-disable-next-line react-hooks/incompatible-library
  const rowVirtualizer = useVirtualizer({
    count: visibleMessages.length + messageVirtualOffset,
    getScrollElement: () => scrollContainerRef.current,
    getItemKey: (index) =>
      hasMoreMessages
        ? index === 0
          ? "chat-history-loader"
          : visibleMessageIds[index - 1] ?? index
        : visibleMessageIds[index] ?? index,
    estimateSize: (index) => (hasMoreMessages && index === 0 ? 56 : 220),
    overscan: 8,
    initialRect: { width: 0, height: 600 },
    rangeExtractor,
    observeElementRect: (instance, callback) =>
      observeElementRect(instance, (rect) =>
        callback(rect.height > 0 ? rect : { ...rect, height: 600 }),
      ),
  });
  const shouldAdjustScrollPositionOnItemSizeChange = useCallback(
    (item: { end: number }) => {
      const scrollContainer = scrollContainerRef.current;
      if (!scrollContainer || prependAnchorRef.current) return false;
      return (
        !isPinnedToBottomRef.current &&
        item.end <= scrollContainer.scrollTop
      );
    },
    [],
  );
  const measureVirtualizerElement = useCallback(
    (element: HTMLDivElement | null) => {
      rowVirtualizer.shouldAdjustScrollPositionOnItemSizeChange =
        shouldAdjustScrollPositionOnItemSizeChange;
      rowVirtualizer.measureElement(element);
    },
    [rowVirtualizer, shouldAdjustScrollPositionOnItemSizeChange],
  );
  useLayoutEffect(() => {
    rowVirtualizer.shouldAdjustScrollPositionOnItemSizeChange =
      shouldAdjustScrollPositionOnItemSizeChange;
  }, [rowVirtualizer, shouldAdjustScrollPositionOnItemSizeChange]);
  const virtualItems = rowVirtualizer.getVirtualItems();
  const virtualTotalSize = rowVirtualizer.getTotalSize();

  const latestMessagesRef = useRef(visibleMessages);
  const latestVisibleMessageIdsRef = useRef(visibleMessageIds);
  const latestHasMoreRef = useRef(hasMoreMessages);
  const latestLoadOlderRef = useRef(onLoadOlderMessages);
  const navigationKeyRef = useRef(navigationKey);
  latestMessagesRef.current = visibleMessages;
  latestVisibleMessageIdsRef.current = visibleMessageIds;
  latestHasMoreRef.current = hasMoreMessages;
  latestLoadOlderRef.current = onLoadOlderMessages;
  navigationKeyRef.current = navigationKey;

  const scrollToMessage = useCallback(async (messageId: string) => {
    releasePrependAnchor();
    const initialNavigationKey = navigationKeyRef.current;
    setMessageNavigationStatus({
      navigationKey: initialNavigationKey,
      text: "対象のメッセージを含む過去の履歴を検索しています…",
    });
    while (true) {
      if (navigationKeyRef.current !== initialNavigationKey) return false;
      const index = latestMessagesRef.current.findIndex(
        (message) => message.id === messageId,
      );
      if (index >= 0) {
        rowVirtualizer.scrollToIndex(
          index + (latestHasMoreRef.current ? 1 : 0),
          { align: "center", behavior: "smooth" },
        );
        setMessageNavigationStatus({ navigationKey: initialNavigationKey, text: "" });
        return true;
      }
      if (!latestHasMoreRef.current || !latestLoadOlderRef.current) break;

      const previousFirstId = latestMessagesRef.current[0]?.id ?? null;
      const loaded = await latestLoadOlderRef.current();
      if (navigationKeyRef.current !== initialNavigationKey) return false;
      if (!loaded) break;
      for (let frame = 0; frame < 3; frame += 1) {
        await new Promise<void>((resolve) => {
          if (typeof requestAnimationFrame === "function") {
            requestAnimationFrame(() => resolve());
          } else {
            window.setTimeout(resolve, 0);
          }
        });
        if (
          latestMessagesRef.current[0]?.id !== previousFirstId ||
          latestMessagesRef.current.some((message) => message.id === messageId)
        ) {
          break;
        }
      }
      if (
        latestMessagesRef.current[0]?.id === previousFirstId &&
        !latestMessagesRef.current.some((message) => message.id === messageId)
      ) {
        break;
      }
    }
    if (navigationKeyRef.current === initialNavigationKey) {
      setMessageNavigationStatus({
        navigationKey: initialNavigationKey,
        text: "対象のメッセージがこの会話に見つかりませんでした。",
      });
    }
    return false;
  }, [releasePrependAnchor, rowVirtualizer]);

  useEffect(() => {
    onProvideScrollToMessage?.(scrollToMessage);
    return () => onProvideScrollToMessage?.(null);
  }, [onProvideScrollToMessage, scrollToMessage]);

  const showEmptyState =
    visibleMessages.length === 0 &&
    !isStreaming &&
    !isWaitingResponse &&
    !showGenerationActivity;

  const updatePinnedToBottom = useCallback(() => {
    const scrollContainer = scrollContainerRef.current;
    if (!scrollContainer) return;
    if (prependAnchorRef.current) {
      isPinnedToBottomRef.current = false;
      return;
    }
    const lastBottomWriteTop = lastBottomWriteTopRef.current;
    if (
      isPinnedToBottomRef.current &&
      lastBottomWriteTop !== null &&
      Math.abs(scrollContainer.scrollTop - lastBottomWriteTop) <= 1.5
    ) {
      return;
    }
    isPinnedToBottomRef.current = isChatScrollPinnedToBottom(scrollContainer);
  }, []);

  const releasePrependAnchorForUserInput = useCallback(() => {
    if (
      !prependAnchorRef.current ||
      olderRequestInFlightRef.current ||
      prependBaselineFirstIdRef.current !== null
    ) {
      return;
    }
    releasePrependAnchor();
  }, [releasePrependAnchor]);

  const loadOlderMessages = useCallback(async () => {
    if (
      !onLoadOlderMessages ||
      !hasMoreMessages ||
      isLoadingOlderMessages ||
      olderRequestInFlightRef.current
    ) {
      return;
    }
    const scrollContainer = scrollContainerRef.current;
    if (!scrollContainer) return;

    olderRequestInFlightRef.current = true;
    prependReadyRef.current = false;
    const firstVisibleItem = rowVirtualizer
      .getVirtualItems()
      .find((item) => item.index >= messageVirtualOffset && item.end > scrollContainer.scrollTop);
    const messageId = firstVisibleItem
      ? visibleMessages[firstVisibleItem.index - messageVirtualOffset]?.id ?? null
      : null;
    const anchorElement = messageId
      ? Array.from(scrollContainer.querySelectorAll<HTMLElement>("[data-chat-message-id]")).find(
          (element) => element.dataset.chatMessageId === messageId,
        )
      : undefined;
    prependAnchorRef.current = {
      messageId,
      offsetTop: anchorElement
        ? anchorElement.getBoundingClientRect().top - scrollContainer.getBoundingClientRect().top
        : firstVisibleItem
          ? firstVisibleItem.start - scrollContainer.scrollTop
          : Number.NaN,
      scrollTop: scrollContainer.scrollTop,
      expectedScrollTop: scrollContainer.scrollTop,
      totalSize: rowVirtualizer.getTotalSize(),
    };
    prependBaselineFirstIdRef.current = visibleMessageIds[0] ?? null;
    try {
      const loaded = await onLoadOlderMessages();
      if (loaded === false) {
        releasePrependAnchor();
      } else {
        prependReadyRef.current = true;
        // The provider may dispatch before this promise continuation runs;
        // force a layout pass after the callback has definitely completed so
        // an append that arrived during the request cannot consume the anchor.
        setPrependAdjustmentVersion((version) => version + 1);
        const baselineFirstId = prependBaselineFirstIdRef.current;
        const releaseUnchangedAnchor = () => {
          if (
            prependAnchorRef.current &&
            prependReadyRef.current &&
            prependBaselineFirstIdRef.current === baselineFirstId &&
            latestVisibleMessageIdsRef.current[0] === baselineFirstId
          ) {
            releasePrependAnchor();
          }
        };
        if (typeof requestAnimationFrame === "function") {
          requestAnimationFrame(releaseUnchangedAnchor);
        } else {
          window.setTimeout(releaseUnchangedAnchor, 0);
        }
      }
    } catch {
      releasePrependAnchor();
    } finally {
      olderRequestInFlightRef.current = false;
    }
  }, [
    hasMoreMessages,
    isLoadingOlderMessages,
    onLoadOlderMessages,
    messageVirtualOffset,
    releasePrependAnchor,
    rowVirtualizer,
    visibleMessages,
    visibleMessageIds,
  ]);

  const handleScroll = useCallback(() => {
    const scrollContainer = scrollContainerRef.current;
    const anchor = prependAnchorRef.current;
    if (scrollContainer && anchor) {
      const baselineFirstId = prependBaselineFirstIdRef.current;
      const baselineHeadUnchanged =
        baselineFirstId !== null &&
        latestVisibleMessageIdsRef.current[0] === baselineFirstId;
      if (
        baselineHeadUnchanged &&
        Math.abs(scrollContainer.scrollTop - anchor.expectedScrollTop) > 1.5
      ) {
        const currentAnchorElement = anchor.messageId
          ? Array.from(scrollContainer.querySelectorAll<HTMLElement>("[data-chat-message-id]")).find(
              (element) => element.dataset.chatMessageId === anchor.messageId,
            )
          : undefined;
        if (currentAnchorElement) {
          anchor.offsetTop =
            currentAnchorElement.getBoundingClientRect().top -
            scrollContainer.getBoundingClientRect().top;
          anchor.scrollTop = scrollContainer.scrollTop;
          anchor.expectedScrollTop = scrollContainer.scrollTop;
        } else {
          const firstVisibleItem = rowVirtualizer
            .getVirtualItems()
            .find((item) => item.index >= messageVirtualOffset && item.end > scrollContainer.scrollTop);
          const messageId = firstVisibleItem
            ? visibleMessages[firstVisibleItem.index - messageVirtualOffset]?.id ?? null
            : null;
          const element = messageId
            ? Array.from(scrollContainer.querySelectorAll<HTMLElement>("[data-chat-message-id]")).find(
                (candidate) => candidate.dataset.chatMessageId === messageId,
              )
            : undefined;
          if (messageId && firstVisibleItem) {
            anchor.messageId = messageId;
            anchor.offsetTop = element
              ? element.getBoundingClientRect().top - scrollContainer.getBoundingClientRect().top
              : firstVisibleItem.start - scrollContainer.scrollTop;
            anchor.scrollTop = scrollContainer.scrollTop;
            anchor.expectedScrollTop = scrollContainer.scrollTop;
          }
        }
      } else if (
        baselineFirstId === null &&
        Math.abs(scrollContainer.scrollTop - anchor.expectedScrollTop) > 1.5
      ) {
        releasePrependAnchor();
      }
    }
    updatePinnedToBottom();
    if (scrollContainer && scrollContainer.scrollTop <= 48) {
      void loadOlderMessages();
    }
  }, [loadOlderMessages, messageVirtualOffset, releasePrependAnchor, rowVirtualizer, updatePinnedToBottom, visibleMessages]);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    if (!isPinnedToBottomRef.current) return false;
    const scrollContainer = scrollContainerRef.current;
    if (!scrollContainer) return false;
    scrollContainer.scrollTo({
      top: scrollContainer.scrollHeight,
      behavior,
    });
    lastBottomWriteTopRef.current = scrollContainer.scrollTop;
    return true;
  }, []);

  const visibleMessagesScrollKey = useMemo(
    () =>
      visibleMessages
        .map((message) => {
          const agentRunId =
            typeof message.metadata?.agent_run_id === "string"
              ? message.metadata.agent_run_id
              : "";
          const deepResearchStatus =
            typeof message.metadata?.status === "string"
              ? message.metadata.status
              : "";
          const deepResearchProgress = metadataScrollValue(
            message.metadata?.progress,
          );
          return [
            message.id,
            message.role,
            getChatScrollContentHash(message.content),
            agentRunId,
            deepResearchStatus,
            deepResearchProgress,
            toolResultsScrollKey(getMessageToolResults(message)),
          ].join(":");
        })
        .join("|"),
    [visibleMessages],
  );

  const liveToolResultsScrollKey = useMemo(
    () => toolResultsScrollKey(liveToolResults ?? []),
    [liveToolResults],
  );
  const previousVisibleMessageIdsRef = useRef<string[]>([]);

  useLayoutEffect(() => {
    const previousIds = previousVisibleMessageIdsRef.current;
    const prependAnchor = prependAnchorRef.current;
    const scrollContainer = scrollContainerRef.current;
    let adjustedPrepend = false;
    const prependFirstIdChanged =
      visibleMessageIds[0] !== prependBaselineFirstIdRef.current;
    if (
      prependAnchor &&
      prependReadyRef.current &&
      prependFirstIdChanged &&
      scrollContainer
    ) {
      const anchorIndex = prependAnchor.messageId
        ? messageIndexById.get(prependAnchor.messageId) ?? -1
        : -1;
      const anchorElement = prependAnchor.messageId
        ? Array.from(scrollContainer.querySelectorAll<HTMLElement>("[data-chat-message-id]")).find(
            (element) => element.dataset.chatMessageId === prependAnchor.messageId,
          )
        : undefined;
      if (anchorIndex >= 0 && anchorElement) {
        const anchorOffset =
          anchorElement.getBoundingClientRect().top - scrollContainer.getBoundingClientRect().top;
        scrollContainer.scrollTop = Math.max(
          0,
          scrollContainer.scrollTop + anchorOffset - prependAnchor.offsetTop,
        );
        prependAnchor.expectedScrollTop = scrollContainer.scrollTop;
        isPinnedToBottomRef.current = false;
        prependReadyRef.current = false;
        prependBaselineFirstIdRef.current = null;
        adjustedPrepend = true;
      } else {
        // A branch reset can remove the anchor row. Keep the historical
        // height-delta fallback for that explicit replacement case.
        const heightDelta =
          rowVirtualizer.getTotalSize() - prependAnchor.totalSize;
        scrollContainer.scrollTop = prependAnchor.scrollTop + heightDelta;
        releasePrependAnchor();
        isPinnedToBottomRef.current = false;
        adjustedPrepend = true;
      }
    }
    const isAppendOnlyUpdate =
      previousIds.length <= visibleMessageIds.length &&
      previousIds.every((id, index) => visibleMessageIds[index] === id);

    if (!prependAnchor && !adjustedPrepend && !isAppendOnlyUpdate) {
      isPinnedToBottomRef.current = true;
    }

    previousVisibleMessageIdsRef.current = visibleMessageIds;
  }, [messageIndexById, messageVirtualOffset, prependAdjustmentVersion, releasePrependAnchor, rowVirtualizer, visibleMessageIds]);

  useLayoutEffect(() => {
    const anchor = prependAnchorRef.current;
    const scrollContainer = scrollContainerRef.current;
    if (
      !anchor?.messageId ||
      prependReadyRef.current ||
      prependBaselineFirstIdRef.current !== null ||
      !scrollContainer
    ) {
      return;
    }
    if (Math.abs(scrollContainer.scrollTop - anchor.expectedScrollTop) > 1.5) {
      releasePrependAnchor();
      return;
    }
    const anchorIndex = messageIndexById.get(anchor.messageId);
    const anchorElement = Array.from(
      scrollContainer.querySelectorAll<HTMLElement>("[data-chat-message-id]"),
    ).find((element) => element.dataset.chatMessageId === anchor.messageId);
    if (anchorIndex === undefined || !anchorElement) return;

    const currentOffset =
      anchorElement.getBoundingClientRect().top -
      scrollContainer.getBoundingClientRect().top;
    const adjustment = currentOffset - anchor.offsetTop;
    if (Math.abs(adjustment) > 0.5) {
      scrollContainer.scrollTop = Math.max(
        0,
        scrollContainer.scrollTop + adjustment,
      );
      isPinnedToBottomRef.current = false;
    }
    anchor.expectedScrollTop = scrollContainer.scrollTop;
  }, [messageIndexById, releasePrependAnchor, virtualItems, virtualTotalSize]);

  useLayoutEffect(() => {
    const activeAnchor = prependAnchorRef.current;
    if (
      activeAnchor?.messageId &&
      !prependReadyRef.current &&
      prependBaselineFirstIdRef.current === null &&
      !messageIndexById.has(activeAnchor.messageId)
    ) {
      releasePrependAnchor();
    }
  }, [messageIndexById, releasePrependAnchor]);

  useLayoutEffect(() => {
    const scrollContainer = scrollContainerRef.current;
    if (
      prependAnchorRef.current ||
      !isPinnedToBottomRef.current ||
      !scrollContainer
    ) {
      return;
    }
    scrollContainer.scrollTop = scrollContainer.scrollHeight;
    lastBottomWriteTopRef.current = scrollContainer.scrollTop;
  }, [virtualItems, virtualTotalSize]);

  // 更新前のスクロール位置を一度描画してから移動すると、送信・streamごとに跳ねる。
  useLayoutEffect(() => {
    if (prependAnchorRef.current) return;
    scrollToBottom();
  }, [
    visibleMessagesScrollKey,
    isStreaming,
    isWaitingResponse,
    streamingContent,
    liveToolResultsScrollKey,
    activeTool,
    activityMessage,
    activeAgentRunId,
    showGenerationActivity,
    prependAdjustmentVersion,
    scrollToBottom,
  ]);

  return (
    <div className="chat-message-history-container relative min-h-0 flex-1">
      <div
        ref={scrollContainerRef}
        data-testid="chat-message-list"
        role="feed"
        aria-busy={isLoadingOlderMessages || undefined}
        className="h-full min-h-0 w-full overflow-y-auto overscroll-contain [overflow-anchor:none]"
        onWheelCapture={releasePrependAnchorForUserInput}
        onPointerDownCapture={releasePrependAnchorForUserInput}
        onTouchStartCapture={releasePrependAnchorForUserInput}
        onKeyDownCapture={(event) => {
          if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) {
            releasePrependAnchorForUserInput();
          }
        }}
        onScroll={handleScroll}
      >
        <div className="chat-viewport-center mx-auto w-full max-w-5xl px-6 py-6">
        {showEmptyState && (
          <div className="flex justify-center py-20">
            <div className="max-w-md rounded-md border border-border-subtle bg-surface-container-low px-6 py-5 text-center text-sm text-text-secondary">
              {emptyMessage}
            </div>
          </div>
        )}

        <div
          className="relative w-full"
          style={{ height: `${virtualTotalSize}px` }}
        >
        {virtualItems.map((virtualRow) => {
          if (hasMoreMessages && virtualRow.index === 0) {
            return (
              <div
                key={virtualRow.key}
                ref={measureVirtualizerElement}
                data-index={virtualRow.index}
                className="absolute left-0 top-0 flex w-full justify-center pb-3"
                style={{ transform: `translateY(${virtualRow.start}px)` }}
              >
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  data-testid="chat-load-older"
                  disabled={isLoadingOlderMessages}
                  onClick={() => void loadOlderMessages()}
                >
                  {isLoadingOlderMessages ? "過去のメッセージを読み込み中…" : "過去のメッセージを読み込む"}
                </Button>
              </div>
            );
          }
          const messageIndex = virtualRow.index - messageVirtualOffset;
          const msg = visibleMessages[messageIndex];
          if (!msg) return null;
          const rowProps = {
            ref: measureVirtualizerElement,
            "data-index": virtualRow.index,
            role: "article" as const,
            "aria-setsize": hasMoreMessages ? -1 : visibleMessages.length,
            "aria-posinset": messagePositionById.get(msg.id) ?? messageIndex + 1,
            style: {
              transform: `translateY(${virtualRow.start}px)`,
              paddingBottom: "1.5rem",
            },
            onFocusCapture: () => setFocusedMessageId(msg.id),
            onBlurCapture: (event: FocusEvent<HTMLDivElement>) => {
              if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
              setFocusedMessageId((current) => current === msg.id ? null : current);
            },
          };
          const branchNavigation = getBranchNavigationState(msg);
          const branchNavDisabled =
            branchSwitchingMessageId !== null ||
            isStreaming ||
            Boolean(isWaitingResponse) ||
            showGenerationActivity ||
            !onSwitchBranch;

          if (msg.role === "system") {
            return (
              <div
                key={messageRenderKey(msg)}
                {...rowProps}
                id={getChatMessageDomId(msg.id)}
                data-chat-message-id={msg.id}
                className="absolute left-0 top-0 w-full box-border py-2 text-center text-xs text-muted-foreground"
              >
                {msg.content}
              </div>
            );
          }

          if (msg.role === "user") {
            const isEditing = editingId === msg.id;
            const attachments = getMessageAttachments(msg);
            const senderLabel = msg.sender_display_name;
            const isImmediateInterrupt =
              msg.metadata?.delivery_mode === "immediate_interrupt";
            const interruptStatus = msg.metadata?.delivery_status;
            const interruptReceiptStatus =
              msg.metadata?.interrupt_receipt_status;
            const interruptLabel =
              interruptStatus === "failed"
                ? "割り込み失敗"
                : interruptReceiptStatus === "pending" &&
                    interruptStatus !== "sending"
                  ? "割り込み結果不明"
                : interruptStatus === "sending"
                  ? "割り込み送信中"
                  : "即時割り込み";
            const commandCapabilities =
              commandCapabilitiesFromMessageMetadata(msg.metadata);
            const deliveryState = msg.metadata?.delivery_state;
            const deliveryError =
              typeof msg.metadata?.delivery_error === "string"
                ? msg.metadata.delivery_error
                : null;
            const clientMessageId =
              typeof msg.metadata?.client_message_id === "string"
                ? msg.metadata.client_message_id
                : msg.client_message_id ?? null;
            const retryable =
              (deliveryState === "failed" || deliveryState === "uncertain") &&
              Boolean(onRetryMessage && clientMessageId);
            const attachmentReselectRequired =
              msg.metadata?.attachments_reselect_required === true;
            const messageTime = formatMessageTime(msg.created_at);
            return (
              <div
                key={messageRenderKey(msg)}
                {...rowProps}
                id={getChatMessageDomId(msg.id)}
                data-chat-message-id={msg.id}
                data-chat-message-delivery-state={
                  typeof deliveryState === "string" ? deliveryState : undefined
                }
                className="group/msg absolute left-0 top-0 flex w-full min-w-0 box-border flex-col items-end gap-1"
              >
                {isEditing ? (
                  <div className="flex min-w-0 w-full max-w-[80%] flex-col gap-2">
                    <Textarea
                      ref={editTextareaRef}
                      aria-label="メッセージ本文を編集"
                      value={editContent}
                      onChange={(e) => {
                        setEditContent(e.target.value);
                        resizeEditTextareaFallback(e.currentTarget);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          submitEdit();
                        }
                        if (e.key === "Escape") cancelEditing();
                      }}
                      className="min-h-40 max-h-[60vh] resize-y overflow-y-auto rounded-xl px-3 py-2"
                    />
                    <div className="flex justify-end gap-1.5">
                      <Button variant="ghost" size="sm" onClick={cancelEditing}>
                        <X className="size-3.5 mr-1" />
                        キャンセル
                      </Button>
                      <Button
                        size="sm"
                        onClick={submitEdit}
                        disabled={!editContent.trim()}
                      >
                        <Check className="size-3.5 mr-1" />
                        送信
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="flex min-w-0 max-w-[80%] flex-col items-end gap-1">
                    <span className="text-[11px] font-medium text-text-secondary">
                      {senderLabel || "You"}
                      {messageTime && <span className="ml-2 font-normal">{messageTime}</span>}
                    </span>
                    {isImmediateInterrupt && (
                      <span
                        className={cn(
                          "rounded-full border px-2 py-0.5 text-[10px] font-medium",
                          interruptStatus === "failed"
                            ? "border-destructive/50 bg-destructive/10 text-destructive"
                            : "border-primary/30 bg-primary/10 text-primary",
                        )}
                      >
                        {interruptLabel}
                      </span>
                    )}
                    <CommandCapabilityChips capabilities={commandCapabilities} />
                    {msg.content.trim() && (
                      <div className="min-w-0 max-w-full rounded-md rounded-br-sm border border-border-subtle bg-surface-container-low px-4 py-3 text-sm text-on-surface shadow-none whitespace-pre-wrap [overflow-wrap:anywhere]">
                        <MentionText text={msg.content} />
                      </div>
                    )}
                    <MessageAttachments attachments={attachments} align="end" />
                    {deliveryState === "pending" && (
                      <span
                        data-testid="chat-message-pending"
                        className="text-[11px] text-muted-foreground"
                      >
                        送信を確認中…
                      </span>
                    )}
                    {deliveryState === "uncertain" && (
                      <span
                        data-testid="chat-message-uncertain"
                        className="flex flex-wrap items-center justify-end gap-2 text-[11px] text-warning"
                      >
                        <span>{deliveryError || "送信結果を確認できませんでした"}</span>
                        {retryable && (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            onClick={() => void onRetryMessage?.(msg)}
                          >
                            {attachmentReselectRequired
                              ? "添付を再選択して再試行"
                              : "同じ内容で再試行"}
                          </Button>
                        )}
                      </span>
                    )}
                    {deliveryState === "failed" && (
                      <span
                        data-testid="chat-message-failed"
                        className="flex flex-wrap items-center justify-end gap-2 text-[11px] text-destructive"
                      >
                        <span>{deliveryError || "送信できませんでした"}</span>
                        {retryable && (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            onClick={() => void onRetryMessage?.(msg)}
                          >
                            {attachmentReselectRequired
                              ? "添付を再選択して再試行"
                              : "再試行"}
                          </Button>
                        )}
                      </span>
                    )}
                  </div>
                )}
                {!isEditing && renderActions(msg, "")}
                {branchNavigation && (
                  <BranchNav
                    current={branchNavigation.current}
                    total={branchNavigation.total}
                    disabled={branchNavDisabled}
                    onPrev={() =>
                      void switchBranch(msg, branchNavigation.index - 1)
                    }
                    onNext={() =>
                      void switchBranch(msg, branchNavigation.index + 1)
                    }
                  />
                )}
              </div>
            );
          }

          // assistant
          const previousUserInput =
            previousUserInputByMessageId.get(msg.id) ?? "";
          const attachments = getMessageAttachments(msg);
          const toolResults = getMessageToolResults(msg);
          const generationMetrics = getMessageGenerationMetrics(msg);
          const responseElapsedMs = getMessageResponseElapsedMs(msg);
          const generationCancelled =
            msg.metadata?.generation_status === "cancelled";
          const agentRunId =
            typeof msg.metadata?.agent_run_id === "string"
              ? msg.metadata.agent_run_id
              : null;
          const assistantSender =
            msg.sender_display_name ||
            (typeof msg.metadata?.character_name === "string"
              ? msg.metadata.character_name
              : "");
          const messageTime = formatMessageTime(msg.created_at);
          const persistenceFailed = msg.metadata?.persistence_failed === true;
          const persistenceRetryable =
            msg.metadata?.persistence_retryable !== false;
          const persistenceClientMessageId =
            typeof msg.metadata?.client_message_id === "string"
              ? msg.metadata.client_message_id
              : msg.client_message_id ?? null;
          return (
            <div
              key={messageRenderKey(msg)}
              {...rowProps}
              id={getChatMessageDomId(msg.id)}
              data-chat-message-id={msg.id}
              className="group/msg absolute left-0 top-0 flex w-full box-border justify-start gap-3"
            >
              <div className="mt-1 flex size-8 shrink-0 items-center justify-center rounded-md border border-primary/60 bg-primary-container/15 text-primary" aria-hidden="true">
                <Bot className="size-4" />
              </div>
              <div className="flex min-w-0 max-w-full flex-col gap-1">
                <span
                  className="mb-0.5 block text-[13px] font-semibold"
                  style={{
                    color: assistantSender ? getCharacterColor(assistantSender) : "var(--primary)",
                  }}
                >
                  {assistantSender || "Assistant"}
                  {messageTime && <span className="ml-2 text-[11px] font-normal text-text-secondary">{messageTime}</span>}
                </span>
                <GenerationMetricsLine
                  metrics={generationMetrics}
                  responseElapsedMs={responseElapsedMs}
                />
                {generationCancelled && (
                  <span
                    data-generation-status="cancelled"
                    className="text-xs text-warning"
                  >
                    応答生成を停止しました
                  </span>
                )}
                {agentRunId && (
                  <AgentRunTimeline
                    runId={agentRunId}
                    onContentChange={scrollToBottom}
                  />
                )}
                {msg.content.trim() && (
                  <div className="min-w-0 max-w-full overflow-hidden rounded-none border-0 bg-transparent px-0 py-0 text-[14px] leading-6 text-on-surface [overflow-wrap:anywhere] prose-sm">
                    <MessageContent content={msg.content} />
                  </div>
                )}
                <ReadOriginalLinks results={toolResults} />
                {persistenceFailed && (
                  <div
                    data-testid="assistant-persistence-warning"
                    className="flex flex-wrap items-center gap-2 text-xs text-warning"
                  >
                    <span className="inline-flex items-center gap-1">
                      <AlertTriangle className="size-3.5" aria-hidden="true" />
                      結果は表示されていますが、会話への保存に失敗しました。
                    </span>
                    {onRetryMessagePersistence &&
                      persistenceRetryable &&
                      persistenceClientMessageId && (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() =>
                            void onRetryMessagePersistence(
                              persistenceClientMessageId,
                            )
                          }
                        >
                          保存を再試行
                        </Button>
                      )}
                  </div>
                )}
                {agentRunId && (
                  <AgentResourceMutationList
                    runId={agentRunId}
                    onTaskClick={onTaskClick}
                  />
                )}
                <MessageAttachments attachments={attachments} />
                {(!agentRunId || generationCancelled) && (
                  <ToolResultDetails results={toolResults} />
                )}
                {renderActions(msg, previousUserInput)}
                {branchNavigation && (
                  <BranchNav
                    current={branchNavigation.current}
                    total={branchNavigation.total}
                    disabled={branchNavDisabled}
                    onPrev={() =>
                      void switchBranch(msg, branchNavigation.index - 1)
                    }
                    onNext={() =>
                      void switchBranch(msg, branchNavigation.index + 1)
                    }
                  />
                )}
              </div>
            </div>
          );
        })}
        </div>

        {/* 待機→stream開始→本文受信でも同じ行・実行ログを維持する。 */}
        {(isStreaming || showGenerationActivity) && (
          <div className="flex justify-start gap-3">
            <div className="mt-1 flex size-8 shrink-0 items-center justify-center rounded-md border border-primary/60 bg-primary-container/15 text-primary" aria-hidden="true">
              <Bot className="size-4" />
            </div>
            <div className="flex min-w-0 max-w-full flex-col gap-1">
              <AgentRunTimeline
                runId={activeAgentRunId}
                live
                generationKey={generationKey}
                generationStartedAt={generationStartedAt}
                activityMessage={activityMessage}
                onContentChange={scrollToBottom}
              />
              {isStreaming && streamingContent ? (
                <div className="min-w-0 max-w-full overflow-hidden rounded-none border-0 bg-transparent px-0 py-0 text-[14px] leading-6 text-on-surface [overflow-wrap:anywhere] prose-sm">
                  <MessageContent content={streamingContent} />
                  {!activeAgentRunId &&
                    !activeTool &&
                    (activityMessage ? (
                      <span className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                        <Loader2 className="size-3 animate-spin" />
                        {activityMessage}
                      </span>
                    ) : (
                      <TypingIndicator />
                    ))}
                </div>
              ) : !activeAgentRunId && (isStreaming || activeTool) ? (
                <div className="min-w-0 max-w-full overflow-hidden rounded-md border border-border-subtle bg-surface-container-low px-3 py-2.5 text-sm text-on-surface [overflow-wrap:anywhere] prose-sm">
                  {activeTool ? (
                    <ToolIndicator toolName={activeTool} />
                  ) : activityMessage ? (
                    <span className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                      <Loader2 className="size-3 animate-spin" />
                      {activityMessage}
                    </span>
                  ) : (
                    <TypingIndicator />
                  )}
                </div>
              ) : null}
              {isStreaming && streamingContent && !activeAgentRunId && (
                <ToolResultDetails results={liveToolResults} />
              )}
              {isStreaming && streamingContent && !activeAgentRunId && activeTool && (
                <ToolIndicator toolName={activeTool} />
              )}
            </div>
          </div>
        )}

        {messageNavigationStatus.navigationKey === navigationKey && messageNavigationStatus.text && (
          <div role="status" aria-live="polite" className="py-3 text-center text-sm text-muted-foreground">
            {messageNavigationStatus.text}
          </div>
        )}

        </div>
      </div>
      <ChatMessageHistoryRail
        messages={historyRailMessages}
        onScrollToMessage={scrollToMessage}
      />
      <Dialog
        open={!!feedbackTarget}
        onOpenChange={(open) => !open && setFeedbackTarget(null)}
      >
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>応答へのフィードバック</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              {FEEDBACK_CATEGORIES.map((category) => (
                <Button
                  key={category.value}
                  type="button"
                  size="sm"
                  variant={
                    feedbackCategory === category.value ? "default" : "outline"
                  }
                  onClick={() => setFeedbackCategory(category.value)}
                >
                  {category.label}
                </Button>
              ))}
            </div>
            <Textarea
              value={feedbackComment}
              onChange={(event) => setFeedbackComment(event.target.value)}
              placeholder="補足説明があれば入力してください"
              rows={5}
            />
            <div className="flex justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => setFeedbackTarget(null)}
                disabled={feedbackSubmitting}
              >
                キャンセル
              </Button>
              <Button onClick={submitFeedback} disabled={feedbackSubmitting}>
                {feedbackSubmitting && (
                  <Loader2 className="mr-1 size-3 animate-spin" />
                )}
                送信
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

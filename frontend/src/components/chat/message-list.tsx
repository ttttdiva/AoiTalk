"use client";

import { useEffect, useRef, useMemo, useState, useCallback } from "react";
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
  Download,
  ExternalLink,
  Package,
  Search,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
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
import type {
  ChatAttachmentMetadata,
  ChatGenerationMetrics,
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
import { cn, formatBytes } from "@/lib/utils";
import Link from "next/link";

const AUTO_SCROLL_BOTTOM_THRESHOLD_PX = 96;

function generatedImageSrc(imagePath: string): string {
  if (!imagePath) return "";
  if (/^https?:\/\//i.test(imagePath) || imagePath.startsWith("/api/")) {
    return imagePath;
  }
  return `/api/python-proxy/filer/image-thumbnail?path=${encodeURIComponent(imagePath)}&size=1024`;
}

function isAppFactoryDownloadLink(href?: string): boolean {
  return Boolean(
    href?.includes("/api/app-factory/artifacts/") && href.includes("/download"),
  );
}

type AppFactoryDownloadLink = {
  artifactId: string;
  title: string;
  downloadHref: string;
  previewHref?: string;
};

function extractAppFactoryDownloadLinks(content: string): AppFactoryDownloadLink[] {
  const downloadPattern =
    /\[([^\]]+)\]\(((?:\/api\/python-proxy)?\/api\/app-factory\/artifacts\/([A-Za-z0-9_.-]+)\/download)\)/g;
  const previewPattern =
    /\[[^\]]+\]\(((?:\/api\/python-proxy)?\/api\/app-factory\/artifacts\/([A-Za-z0-9_.-]+)\/preview)\)/g;
  const previewById = new Map<string, string>();
  const linksById = new Map<string, AppFactoryDownloadLink>();

  for (const match of content.matchAll(previewPattern)) {
    previewById.set(match[2], match[1]);
  }

  for (const match of content.matchAll(downloadPattern)) {
    const title = match[1].replace(/[`*_]/g, "").trim() || "app-factory.zip";
    const artifactId = match[3];
    if (!linksById.has(artifactId)) {
      linksById.set(artifactId, {
        artifactId,
        title,
        downloadHref: match[2],
        previewHref: previewById.get(artifactId),
      });
    }
  }

  return Array.from(linksById.values());
}

/** @[[type:id:name]] パターンをリンクに変換して表示 */
function MentionText({ text }: { text: string }) {
  const parts = text.split(/(@\[\[[^\]]+\]\])/g);
  return (
    <>
      {parts.map((part, i) => {
        const match = part.match(
          /^@\[\[(file|task|project):([^:]+):([^\]]+)\]\]$/,
        );
        if (!match) return <span key={i}>{part}</span>;
        const [, type, id, name] = match;
        if (type === "file") {
          return (
            <Link
              key={i}
              href={`/filer?open=${encodeURIComponent(id)}`}
              className="inline-flex items-center gap-0.5 rounded bg-primary/20 px-1 text-primary-foreground underline"
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
              className="inline-flex items-center gap-0.5 rounded bg-green-500/20 px-1 text-primary-foreground underline"
            >
              @{name}
            </Link>
          );
        }
        if (type === "project") {
          return (
            <span
              key={i}
              className="inline-flex items-center gap-0.5 rounded bg-purple-500/20 px-1 text-xs font-medium"
            >
              @{name}
            </span>
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
  onEditMessage?: (messageId: string, newContent: string) => void;
  onRerunMessage?: (
    message: ConversationMessage,
    responseModel?: ChatResponseModelSelection,
  ) => void;
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
        const urls = Array.isArray(result.urls) ? result.urls : [];
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

/** [GENERATED_IMAGE:path] タグを画像として表示し、残りをMarkdownで描画 */
function AppFactoryArtifactCards({
  links,
}: {
  links: AppFactoryDownloadLink[];
}) {
  if (links.length === 0) return null;

  return (
    <div className="mt-2 space-y-2">
      {links.map((link) => (
        <div
          key={link.artifactId}
          className="flex max-w-full flex-wrap items-center gap-2 rounded-md border border-border bg-muted/60 p-2"
        >
          <span className="flex size-9 shrink-0 items-center justify-center rounded bg-background text-muted-foreground">
            <Package className="size-4" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-xs font-semibold">
              {link.title}
            </span>
            <span className="block truncate text-[11px] text-muted-foreground">
              {link.artifactId}
            </span>
          </span>
          <span className="flex shrink-0 items-center gap-1">
            <a
              href={link.downloadHref}
              download
              className="inline-flex h-8 items-center gap-1 rounded-md border border-border bg-background px-2 text-xs font-medium hover:bg-accent hover:text-accent-foreground"
            >
              <Download className="size-3.5" />
              ZIP
            </a>
            {link.previewHref && (
              <a
                href={link.previewHref}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex h-8 items-center gap-1 rounded-md border border-border bg-background px-2 text-xs font-medium hover:bg-accent hover:text-accent-foreground"
              >
                <ExternalLink className="size-3.5" />
                Preview
              </a>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}

function MessageContent({ content }: { content: string }) {
  const visibleContent = content
    .replace(/\[SCENE_DESCRIPTION:[\s\S]*?\]/g, "")
    .replace(/\[IMAGE_TRIGGER:[\s\S]*?\]/g, "")
    .trim();
  const artifactLinks = extractAppFactoryDownloadLinks(visibleContent);
  const parts = visibleContent.split(/(\[GENERATED_IMAGE:[^\]]+\])/g);
  return (
    <>
      {parts.map((part, i) => {
        const imgMatch = part.match(/^\[GENERATED_IMAGE:(.+)\]$/);
        if (imgMatch) {
          const fullPath = imgMatch[1];
          const imageSrc = generatedImageSrc(fullPath);
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
      <AppFactoryArtifactCards links={artifactLinks} />
    </>
  );
}

/** マークダウンレンダリング */
function MarkdownContent({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        pre: ({ children }) => (
          <pre className="my-2 max-w-full overflow-x-auto rounded-md bg-black/20 p-3 text-sm">
            {children}
          </pre>
        ),
        code: ({ className, children, ...props }) => {
          const isInline = !className;
          if (isInline) {
            return (
              <code
                className="max-w-full rounded bg-black/20 px-1.5 py-0.5 text-sm [overflow-wrap:anywhere]"
                {...props}
              >
                {children}
              </code>
            );
          }
          return (
          <code className={cn("max-w-full", className)} {...props}>
            {children}
          </code>
        );
        },
        p: ({ children }) => (
          <p className="mb-2 max-w-full [overflow-wrap:anywhere] last:mb-0">
            {children}
          </p>
        ),
        ul: ({ children }) => (
          <ul className="mb-2 ml-4 list-disc space-y-1 last:mb-0">
            {children}
          </ul>
        ),
        ol: ({ children }) => (
          <ol className="mb-2 ml-4 list-decimal space-y-1 last:mb-0">
            {children}
          </ol>
        ),
        li: ({ children }) => <li className="text-sm">{children}</li>,
        h1: ({ children }) => (
          <h1 className="mb-2 text-lg font-bold">{children}</h1>
        ),
        h2: ({ children }) => (
          <h2 className="mb-2 text-base font-bold">{children}</h2>
        ),
        h3: ({ children }) => (
          <h3 className="mb-1 text-sm font-bold">{children}</h3>
        ),
        strong: ({ children }) => (
          <strong className="font-bold">{children}</strong>
        ),
        em: ({ children }) => (
          <em
            className="italic text-muted-foreground/80 not-italic"
            style={{ fontStyle: "italic" }}
          >
            {children}
          </em>
        ),
        blockquote: ({ children }) => (
          <blockquote className="my-2 border-l-2 border-muted-foreground/30 pl-3 italic">
            {children}
          </blockquote>
        ),
        table: ({ children }) => (
          <div className="my-2 max-w-full overflow-x-auto">
            <table className="min-w-full border-collapse text-sm">
              {children}
            </table>
          </div>
        ),
        th: ({ children }) => (
          <th className="border border-border bg-muted px-3 py-1.5 text-left font-semibold">
            {children}
          </th>
        ),
        td: ({ children }) => (
          <td className="border border-border px-3 py-1.5">{children}</td>
        ),
        a: ({ href, children }) => {
          const isDownloadLink = isAppFactoryDownloadLink(href);
          return (
            <a
              href={href}
              target={isDownloadLink ? undefined : "_blank"}
              rel={isDownloadLink ? undefined : "noopener noreferrer"}
              download={isDownloadLink ? true : undefined}
              className="text-primary underline [overflow-wrap:anywhere] hover:text-primary/80"
            >
              {children}
            </a>
          );
        },
        hr: () => <hr className="my-3 border-border" />,
      }}
    >
      {content}
    </ReactMarkdown>
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
}: {
  current: number;
  total: number;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
      <Button
        variant="ghost"
        size="icon-xs"
        onClick={onPrev}
        disabled={current <= 1}
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
        disabled={current >= total}
      >
        <ChevronRight className="size-3" />
      </Button>
    </div>
  );
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
  onEditMessage,
  onRerunMessage,
  responseModelOptions = [],
  responseModelOptionsLoading = false,
}: MessageListProps) {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const isPinnedToBottomRef = useRef(true);
  const [editingId, setEditingId] = useState<string | null>(null);
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

  const startEditing = useCallback((msg: ConversationMessage) => {
    setEditingId(msg.id);
    setEditContent(msg.content);
    setTimeout(() => editTextareaRef.current?.focus(), 50);
  }, []);

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
          <DropdownMenu>
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
      onRerunMessage,
      openFeedback,
      responseModelOptions,
      responseModelOptionsLoading,
      startEditing,
    ],
  );

  // ブランチ選択状態: parentId → 表示中のbranchIndex
  const [branchSelections, setBranchSelections] = useState<
    Record<string, number>
  >({});

  // ブランチ情報を計算
  const branchInfo = useMemo(() => {
    const groups: Record<string, ConversationMessage[]> = {};
    for (const msg of messages) {
      const parentKey = msg.parent_message_id || "__root__";
      if (!groups[parentKey]) groups[parentKey] = [];
      groups[parentKey].push(msg);
    }
    return groups;
  }, [messages]);

  // 表示するメッセージをフィルタリング（ブランチ考慮）
  const visibleMessages = useMemo(() => {
    const result: ConversationMessage[] = [];
    const seen = new Set<string>();

    for (const msg of messages) {
      const parentKey = msg.parent_message_id || "__root__";
      const siblings = branchInfo[parentKey];

      if (parentKey === "__root__" || !siblings || siblings.length <= 1) {
        if (!seen.has(msg.id)) {
          result.push(msg);
          seen.add(msg.id);
        }
      } else {
        if (seen.has(parentKey + "__branch__")) continue;
        seen.add(parentKey + "__branch__");

        const selectedIndex =
          branchSelections[parentKey] ??
          siblings.findIndex((s) => s.is_active_branch);
        const idx = selectedIndex >= 0 ? selectedIndex : 0;
        const selected = siblings[idx];
        if (selected && !seen.has(selected.id)) {
          result.push(selected);
          seen.add(selected.id);
        }
      }
    }
    return result;
  }, [messages, branchInfo, branchSelections]);
  const visibleMessageIds = useMemo(
    () => visibleMessages.map((message) => message.id),
    [visibleMessages],
  );

  const showEmptyState =
    visibleMessages.length === 0 && !isStreaming && !isWaitingResponse;

  const updatePinnedToBottom = useCallback(() => {
    const scrollContainer = scrollContainerRef.current;
    if (!scrollContainer) return;
    isPinnedToBottomRef.current = isChatScrollPinnedToBottom(scrollContainer);
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    if (!isPinnedToBottomRef.current) return;
    requestAnimationFrame(() => {
      if (!isPinnedToBottomRef.current) return;
      const scrollContainer = scrollContainerRef.current;
      if (!scrollContainer) return;
      scrollContainer.scrollTo({
        top: scrollContainer.scrollHeight,
        behavior,
      });
      isPinnedToBottomRef.current = true;
    });
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

  useEffect(() => {
    const previousIds = previousVisibleMessageIdsRef.current;
    const isAppendOnlyUpdate =
      previousIds.length <= visibleMessageIds.length &&
      previousIds.every((id, index) => visibleMessageIds[index] === id);

    if (!isAppendOnlyUpdate) {
      isPinnedToBottomRef.current = true;
    }

    previousVisibleMessageIdsRef.current = visibleMessageIds;
  }, [visibleMessageIds]);

  // 自動スクロール
  useEffect(() => {
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
    scrollToBottom,
  ]);

  return (
    <div
      ref={scrollContainerRef}
      data-testid="chat-message-list"
      className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
      onScroll={updatePinnedToBottom}
    >
      <div className="mx-auto flex max-w-3xl flex-col gap-4 p-4 transition-transform duration-200 ease-linear xl:translate-x-[var(--chat-viewport-offset)]">
        {showEmptyState && (
          <div className="flex justify-center py-20">
            <div className="max-w-md rounded-2xl border border-border bg-card px-6 py-5 text-center text-sm text-muted-foreground">
              {emptyMessage}
            </div>
          </div>
        )}

        {visibleMessages.map((msg, index) => {
          const parentKey = msg.parent_message_id || "__root__";
          const siblings = branchInfo[parentKey];
          // __root__グループはブランチではない（parent_message_idがnullの独立メッセージ群）
          const hasBranch =
            parentKey !== "__root__" && siblings && siblings.length > 1;
          const currentBranchIdx = hasBranch
            ? siblings.findIndex((s) => s.id === msg.id) + 1
            : 0;
          const totalBranches = hasBranch ? siblings.length : 0;

          if (msg.role === "system") {
            return (
              <div
                key={msg.id}
                data-chat-message-id={msg.id}
                className="py-2 text-center text-xs text-muted-foreground"
              >
                {msg.content}
              </div>
            );
          }

          if (msg.role === "user") {
            const isEditing = editingId === msg.id;
            const attachments = getMessageAttachments(msg);
            const senderLabel = msg.sender_display_name;
            const commandCapabilities =
              commandCapabilitiesFromMessageMetadata(msg.metadata);
            return (
              <div
                key={msg.id}
                data-chat-message-id={msg.id}
                className="group/msg flex flex-col items-end gap-1"
              >
                {isEditing ? (
                  <div className="flex max-w-[80%] flex-col gap-2">
                    <textarea
                      ref={editTextareaRef}
                      value={editContent}
                      onChange={(e) => setEditContent(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          submitEdit();
                        }
                        if (e.key === "Escape") cancelEditing();
                      }}
                      className="w-full resize-none rounded-xl border border-input bg-card px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      rows={Math.min(editContent.split("\n").length + 1, 6)}
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
                  <div className="flex max-w-[80%] flex-col items-end gap-1">
                    {senderLabel && (
                      <span className="text-xs font-medium text-muted-foreground">
                        {senderLabel}
                      </span>
                    )}
                    <CommandCapabilityChips capabilities={commandCapabilities} />
                    {msg.content.trim() && (
                      <div className="rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-sm text-primary-foreground shadow-[0_14px_34px_-28px_rgba(5,87,115,0.9)] whitespace-pre-wrap">
                        <MentionText text={msg.content} />
                      </div>
                    )}
                    <MessageAttachments attachments={attachments} align="end" />
                  </div>
                )}
                {!isEditing && renderActions(msg, "")}
                {hasBranch && (
                  <BranchNav
                    current={currentBranchIdx}
                    total={totalBranches}
                    onPrev={() =>
                      setBranchSelections((prev) => ({
                        ...prev,
                        [parentKey]: Math.max(
                          0,
                          (prev[parentKey] ??
                            siblings.findIndex((s) => s.is_active_branch)) - 1,
                        ),
                      }))
                    }
                    onNext={() =>
                      setBranchSelections((prev) => ({
                        ...prev,
                        [parentKey]: Math.min(
                          siblings.length - 1,
                          (prev[parentKey] ??
                            siblings.findIndex((s) => s.is_active_branch)) + 1,
                        ),
                      }))
                    }
                  />
                )}
              </div>
            );
          }

          // assistant
          const previousUserInput = [...visibleMessages]
            .slice(0, index)
            .reverse()
            .find((item) => item.role === "user")?.content || "";
          const attachments = getMessageAttachments(msg);
          const toolResults = getMessageToolResults(msg);
          const generationMetrics = getMessageGenerationMetrics(msg);
          const responseElapsedMs = getMessageResponseElapsedMs(msg);
          const agentRunId =
            typeof msg.metadata?.agent_run_id === "string"
              ? msg.metadata.agent_run_id
              : null;
          const assistantSender =
            msg.sender_display_name ||
            (typeof msg.metadata?.character_name === "string"
              ? msg.metadata.character_name
              : "");
          return (
            <div
              key={msg.id}
              data-chat-message-id={msg.id}
              className="group/msg flex justify-start"
            >
              <div className="flex min-w-0 max-w-full flex-col gap-1">
                {assistantSender && (
                  <span
                    className="text-xs font-medium mb-0.5 block"
                    style={{
                      color: getCharacterColor(assistantSender),
                    }}
                  >
                    {assistantSender}
                  </span>
                )}
                <GenerationMetricsLine
                  metrics={generationMetrics}
                  responseElapsedMs={responseElapsedMs}
                />
                {agentRunId && (
                  <AgentRunTimeline
                    runId={agentRunId}
                    onContentChange={scrollToBottom}
                  />
                )}
                {msg.content.trim() && (
                  <div className="min-w-0 max-w-full overflow-hidden rounded-2xl rounded-bl-md border border-border bg-card px-4 py-2.5 text-sm text-card-foreground [overflow-wrap:anywhere] prose-sm">
                    <MessageContent content={msg.content} />
                  </div>
                )}
                <MessageAttachments attachments={attachments} />
                {!agentRunId && <ToolResultDetails results={toolResults} />}
                {renderActions(msg, previousUserInput)}
                {hasBranch && (
                  <BranchNav
                    current={currentBranchIdx}
                    total={totalBranches}
                    onPrev={() =>
                      setBranchSelections((prev) => ({
                        ...prev,
                        [parentKey]: Math.max(
                          0,
                          (prev[parentKey] ??
                            siblings.findIndex((s) => s.is_active_branch)) - 1,
                        ),
                      }))
                    }
                    onNext={() =>
                      setBranchSelections((prev) => ({
                        ...prev,
                        [parentKey]: Math.min(
                          siblings.length - 1,
                          (prev[parentKey] ??
                            siblings.findIndex((s) => s.is_active_branch)) + 1,
                        ),
                      }))
                    }
                  />
                )}
              </div>
            </div>
          );
        })}

        {/* ストリーミング中のメッセージ */}
        {isStreaming && streamingContent && (
          <div className="flex justify-start">
            <div className="flex min-w-0 max-w-full flex-col gap-1">
              <AgentRunTimeline
                runId={activeAgentRunId}
                live
                onContentChange={scrollToBottom}
              />
              <div className="min-w-0 max-w-full overflow-hidden rounded-2xl rounded-bl-md border border-border bg-card px-4 py-2.5 text-sm text-card-foreground [overflow-wrap:anywhere] prose-sm">
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
              {!activeAgentRunId && (
                <ToolResultDetails results={liveToolResults} />
              )}
              {!activeAgentRunId && activeTool && (
                <ToolIndicator toolName={activeTool} />
              )}
            </div>
          </div>
        )}

        {/* ストリーミング開始直後（内容なし）またはツール実行中 */}
        {isStreaming && !streamingContent && (
          <div className="flex justify-start">
            <div className="flex min-w-0 max-w-full flex-col gap-1">
              <AgentRunTimeline
                runId={activeAgentRunId}
                live
                onContentChange={scrollToBottom}
              />
              {!activeAgentRunId && (
                <div className="min-w-0 max-w-full overflow-hidden rounded-2xl rounded-bl-md border border-border bg-card px-4 py-2.5 text-sm text-card-foreground [overflow-wrap:anywhere] prose-sm">
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
              )}
            </div>
          </div>
        )}

        {/* 応答待ち（送信済み〜stream_start受信前） */}
        {isWaitingResponse &&
          !isStreaming &&
          (activeTool || activeAgentRunId) && (
            <div className="flex justify-start">
              <div className="flex min-w-0 max-w-full flex-col gap-1">
                <AgentRunTimeline
                  runId={activeAgentRunId}
                  live
                  onContentChange={scrollToBottom}
                />
                {!activeAgentRunId && activeTool && (
                  <div className="min-w-0 max-w-full overflow-hidden rounded-2xl rounded-bl-md border border-border bg-card px-4 py-2.5 text-sm text-card-foreground [overflow-wrap:anywhere] prose-sm">
                    <ToolIndicator toolName={activeTool} />
                  </div>
                )}
              </div>
            </div>
          )}
      </div>
      <Dialog
        open={!!feedbackTarget}
        onOpenChange={(open) => !open && setFeedbackTarget(null)}
      >
        <DialogContent className="sm:max-w-lg">
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

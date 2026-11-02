"use client";

import { ConversationDelegationMap } from "@/components/chat/conversation-delegation-map";

import Link from "next/link";
import { useAoiDotsShortcut } from "./use-aoidots-shortcut";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Bot,
  ChevronDown,
  ExternalLink,
  Loader2,
  MessageCircleQuestion,
  RefreshCcw,
  Send,
  Square,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { toast } from "sonner";
import { useOptionalProject } from "@/contexts/project-context";
import {
  prepareChatAttachmentPayload,
  useWebSocket,
} from "@/hooks/use-websocket";
import { useChatWebSocketEvents } from "@/hooks/use-chat-websocket-events";
import { ChatComposer } from "@/components/chat/chat-composer";
import type { SubmittedSteeringInstruction } from "@/components/chat/chat-composer";
import { MessageList } from "@/components/chat/message-list";
import {
  AskUserQuestionDialog,
  ExternalModelPromptDialog,
  PlanApprovalDialog,
  ToolPermissionDialog,
  buildPlanApprovalResponsePayload,
  type AskUserQuestionRequest,
  type ExternalModelPromptRequest,
  type PlanApprovalRequest,
  type ToolPermissionRequest,
  type ToolPermissionScope,
} from "@/components/chat/chat-permission-dialogs";
import type {
  ConversationMessage,
  ConversationGenerationStatus,
  ChatToolResultMetadata,
} from "@/lib/chat-api";
import {
  createLocalUserMessage,
} from "@/lib/chat-local-messages";
import {
  answerAgentDialogueInput,
  aoiDotsErrorCode,
  aoiDotsHistoryFenceDeliveryBlockers,
  aoiDotsHistoryFenceErrorMessage,
  applyAoiDotsHistoryFence,
  bootstrapAoiDots,
  controlAgentDialogue,
  dispatchAoiDotsMessage,
  getAgentDialogue,
  listAoiDotsUncertainInteractiveWork,
  listAoiDotsWork,
  previewAoiDotsHistoryFence,
  type AoiDotsDialogueControlPayload,
  type AoiDotsDialogue,
  type AoiDotsHistoryFenceReview,
  type AoiDotsPendingInput,
  type AoiDotsProfile,
  type AoiDotsWorkItem,
} from "@/lib/aoidots-api";
import { chatApi } from "@/lib/chat-api";
import { conversationAgentApi, unwrapAgentRuns } from "@/lib/conversation-agent-api";
import { useShellChrome } from "@/components/layout/shell-context";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import {
  EMPTY_CHAT_COMPOSER_DRAFT,
  flushChatComposerDraftStore,
  updateChatComposerDraft,
} from "@/lib/chat-composer-draft-store";
import type { MentionItem } from "@/components/chat/mention-menu";
import type { ChatCommandCapability } from "@/lib/chat-commands";
import {
  initialChatGenerationState,
} from "@/lib/chat-generation-state";

function newClientMessageId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `aoidots-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function messageClientId(message: ConversationMessage): string | null {
  const value = message.client_message_id ?? message.metadata?.client_message_id;
  return typeof value === "string" && value.trim() ? value : null;
}

function mergeMessages(
  current: ConversationMessage[],
  incoming: ConversationMessage[],
): ConversationMessage[] {
  const byIdentity = new Map<string, ConversationMessage>();
  for (const message of current) {
    byIdentity.set(message.id, message);
    const clientId = messageClientId(message);
    if (clientId) byIdentity.set(`client:${clientId}`, message);
  }
  for (const message of incoming) {
    const clientId = messageClientId(message);
    const previous =
      byIdentity.get(message.id) ??
      (clientId ? byIdentity.get(`client:${clientId}`) : undefined);
    if (previous && previous.id !== message.id) byIdentity.delete(previous.id);
    if (previous && clientId) byIdentity.delete(`client:${clientId}`);
    byIdentity.set(message.id, message);
    if (clientId) byIdentity.set(`client:${clientId}`, message);
  }
  const unique = new Map<string, ConversationMessage>();
  for (const message of byIdentity.values()) unique.set(message.id, message);
  return [...unique.values()].sort((left, right) => {
    const leftTime = left.created_at ? Date.parse(left.created_at) : 0;
    const rightTime = right.created_at ? Date.parse(right.created_at) : 0;
    return leftTime - rightTime || left.id.localeCompare(right.id);
  });
}

function statusLabel(status: string): string {
  switch (status) {
    case "running":
    case "claimed":
      return "実行中";
    case "waiting":
    case "awaiting_input":
    case "awaiting_approval":
      return "判断待ち";
    case "succeeded":
    case "completed":
      return "完了";
    case "failed":
    case "blocked":
      return "要確認";
    case "cancelled":
      return "停止";
    default:
      return "状態を確認中";
  }
}

function statusClass(status: string): string {
  if (["failed", "blocked"].includes(status)) return "text-destructive";
  if (["running", "claimed", "waiting", "awaiting_input", "awaiting_approval"].includes(status)) {
    return "text-primary";
  }
  return "text-muted-foreground";
}

function historyFenceStatusLabel(status: string | null): string {
  switch (status) {
    case "failed": return "失敗";
    case "succeeded":
    case "completed": return "完了";
    case "running":
    case "claimed": return "実行中";
    case "waiting":
    case "pending": return "待機中";
    case "blocked": return "停止中";
    case "cancelled": return "キャンセル";
    default: return "確認できません";
  }
}

function childWorkSummary(states: string[]): string {
  if (states.length === 0) return "子Work 0件";
  const counts = new Map<string, number>();
  for (const state of states) counts.set(state, (counts.get(state) ?? 0) + 1);
  const items = [...counts.entries()].map(
    ([state, count]) => `${historyFenceStatusLabel(state)} ${count}件`,
  );
  return `子Work ${states.length}件（${items.join("・")}）`;
}

type AoiDotsHistoryFenceDisplay = AoiDotsHistoryFenceReview & {
  errorMessage?: string;
};

function shortHistoryFenceDeliveryId(id: string): string {
  return id.length > 16 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}

export function canStopAoiDotsWork(status: string): boolean {
  return ["queued", "running", "waiting", "awaiting_approval"].includes(status);
}

export function canReworkAoiDotsWork(status: string): boolean {
  return ["failed", "blocked", "cancelled", "expired"].includes(status);
}

export function encodeAoiDotsAnswer(
  freeText: string,
  choices: string[],
  allowedChoices: string[] = choices,
): string {
  const normalizedText = freeText.trim();
  const normalizedChoices = choices
    .map((choice) => choice.trim())
    .filter((choice) => Boolean(choice) && allowedChoices.includes(choice));
  if (allowedChoices.length === 0) return normalizedText;
  if (normalizedChoices.length === 0 && !normalizedText) return "";
  return JSON.stringify({
    choices: normalizedChoices,
    ...(normalizedText ? { free_text: normalizedText } : {}),
  });
}

export function canSubmitAoiDotsInput(
  freeText: string,
  selectedChoices: string[],
  allowedChoices: string[],
  allowFreeText: boolean,
): boolean {
  const hasText = freeText.trim().length > 0;
  const hasChoices = selectedChoices.some((choice) => allowedChoices.includes(choice));
  if (!allowFreeText && allowedChoices.length === 0) return false;
  return (allowFreeText && hasText) || (allowedChoices.length > 0 && hasChoices);
}

export function acceptsAoiDotsGenerationStatus(
  status: ConversationGenerationStatus,
  sessionId: string,
  activeRunId: string | null,
  activeClientMessageId: string | null,
  hasActiveLocalGeneration = Boolean(activeRunId || activeClientMessageId),
): boolean {
  if (status.session_id && status.session_id !== sessionId) return false;
  if (activeRunId && status.agent_run_id && activeRunId !== status.agent_run_id) {
    return false;
  }
  if (
    activeClientMessageId &&
    status.client_message_id &&
    activeClientMessageId !== status.client_message_id
  ) {
    return false;
  }
  if (!status.running && hasActiveLocalGeneration && !status.agent_run_id && !status.client_message_id) {
    return false;
  }
  return true;
}

export function hasAoiDotsTerminalProof(
  messages: ConversationMessage[] | null,
  runId: string | null,
  clientMessageId: string | null,
): boolean {
  return Boolean(
    messages?.some((message) => {
      if (message.role !== "assistant") return false;
      const metadata = message.metadata ?? {};
      return (
        (runId && metadata.agent_run_id === runId) ||
        (clientMessageId &&
          (metadata.client_message_id === clientMessageId ||
            message.client_message_id === clientMessageId))
      );
    }),
  );
}

export function isTerminalAoiDotsRunStatus(status: string): boolean {
  return [
    "succeeded",
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "expired",
    "error",
  ].includes(status.trim().toLowerCase());
}

export function isStaleAoiDotsError(error: unknown): boolean {
  return Boolean(
    error &&
      typeof error === "object" &&
      "status" in error &&
      Number((error as { status?: unknown }).status) === 409,
  );
}

function inputKey(dialogueId: string, inputId: string): string {
  return `${dialogueId}:${inputId}`;
}

const AOIDOTS_SHARED_EVENT_TYPES = new Set([
  "external_llm_permission_request",
  "external_model_prompt_request",
  "ask_user_question_request",
  "ask_user_question_closed",
  "plan_approval_request",
  "memory_selection_progress",
  "tool_start",
  "tool_end",
  "status_update",
  "reasoning_progress",
  "stream_start",
  "stream_cancelled",
  "stream_end",
  "response",
  "conversation_persisted",
]);

function workInput(item: AoiDotsWorkItem): AoiDotsPendingInput | null {
  return item.pending_inputs?.find((input) => input.state === "pending") ?? null;
}

export function AoiDotsToggleButton() {
  const { aoiDotsOpen, toggleAoiDots } = useShellChrome();
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            data-testid="aoi-dots-toggle"
            data-aoi-dots-open={aoiDotsOpen ? "true" : "false"}
            aria-label={aoiDotsOpen ? "AoiDotsを閉じる" : "AoiDotsを開く"}
            aria-pressed={aoiDotsOpen}
            aria-keyshortcuts="Alt+Shift+D"
            onClick={toggleAoiDots}
            className="inline-flex size-8 shrink-0 items-center justify-center rounded-md border border-border/70 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        }
      >
        <Bot className="size-4" />
      </TooltipTrigger>
      <TooltipContent side="top" sideOffset={8} positionerClassName="z-[80]">
        <span>{aoiDotsOpen ? "AoiDotsを閉じる" : "AoiDotsを開く"}</span>
        <kbd
          data-slot="kbd"
          className="rounded border border-current/30 px-1 py-0.5 font-mono text-[10px]"
        >
          Alt+Shift+D
        </kbd>
      </TooltipContent>
    </Tooltip>
  );
}

function AoiDotsWorkCard({
  item,
  dialogue,
  questionPreview,
  inputDraft,
  selectedChoices,
  onInputDraftChange,
  onSelectedChoicesChange,
  onOpenDialogue,
  onAnswer,
  onControl,
  reworkInstruction,
  onReworkInstructionChange,
  reworkPending,
}: {
  item: AoiDotsWorkItem;
  dialogue: AoiDotsDialogue | undefined;
  questionPreview?: { text: string; choices: string[]; messageId: string | null };
  inputDraft: string;
  selectedChoices: string[];
  onInputDraftChange: (value: string) => void;
  onSelectedChoicesChange: (value: string[]) => void;
  onOpenDialogue: () => void;
  onAnswer: (input: AoiDotsPendingInput) => void;
  onControl: (action: "stop" | "resume" | "rework", instruction?: string) => void;
  reworkInstruction: string;
  onReworkInstructionChange: (value: string) => void;
  reworkPending: boolean;
}) {
  const [reworkEditorOpen, setReworkEditorOpen] = useState(false);
  const pendingInput = workInput(item);
  const status = item.job_status;
  const displayItem = item;
  const displayInput = pendingInput;
  const choices = questionPreview?.choices ?? displayInput?.choices ?? [];
  const allowsMultiple = displayInput?.allow_multiple === true;
  const allowsFreeText =
    displayInput?.allow_free_text !== false &&
    displayInput?.input_type !== "choices";
  const rootAgent = displayItem.root_agent_name || "相談窓口";
  const workerAgent = displayItem.worker_agent_name || "担当Agent";
  const workerSessionId = item.refs.worker_session_id;
  const artifacts = item.artifacts ?? [];
  return (
    <article
      className="rounded-md border border-border-subtle bg-surface-container-low px-3 py-2"
      data-testid={`aoi-dots-work-${item.dialogue_id}`}
    >
      <div className="flex min-w-0 items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <span className={`text-xs font-medium ${statusClass(status)}`}>
              {displayItem.human_status || statusLabel(status)}
            </span>
            <span className="truncate text-xs font-medium text-foreground">
              {displayItem.title || displayItem.label || `Agent dialogue ${item.dialogue_id}`}
            </span>
          </div>
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
            依頼元 Agent: {rootAgent} · 実行先 Agent: {workerAgent}
          </div>
        </div>
        {workerSessionId && (
          <Link
            href={`/chat?s=${encodeURIComponent(workerSessionId)}`}
            className="inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-1 text-[11px] text-primary hover:bg-primary/10"
            title="通常Chatで子セッションを開く"
          >
            詳細
            <ExternalLink className="size-3" />
          </Link>
        )}
      </div>

      {item.safe_error && (
        <div className="mt-2 rounded border border-destructive/30 bg-destructive/5 px-2 py-1 text-[11px] text-destructive">
          {item.safe_error}
        </div>
      )}

      {artifacts.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {artifacts.map((artifact) => (
            <span
              key={`${artifact.id ?? artifact.artifact_ref ?? "artifact"}`}
              className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground"
            >
              {artifact.phase || artifact.artifact_ref || "artifact"}
            </span>
          ))}
        </div>
      )}

      {pendingInput && item.dialogue_id && (
        <div className="mt-2 rounded border border-primary/25 bg-primary/5 p-2">
          <div className="flex items-start gap-1.5 text-xs text-foreground">
            <MessageCircleQuestion className="mt-0.5 size-3.5 shrink-0 text-primary" />
            <span className="min-w-0 break-words">
              {questionPreview?.text || displayInput?.question || "Agentから判断を求められています。"}
              {questionPreview?.messageId
                ? ` 質問参照: ${questionPreview.messageId}`
                : pendingInput.question_message_id
                  ? ` 質問参照: ${pendingInput.question_message_id}`
                  : ""}
            </span>
          </div>
          {choices.length ? (
            <div className="mt-1 flex flex-wrap gap-1 text-[11px] text-muted-foreground">
              {choices.map((choice) => (
                <label key={choice} className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5">
                  <input
                    type={allowsMultiple ? "checkbox" : "radio"}
                    checked={selectedChoices.includes(choice)}
                    onChange={() => {
                      if (allowsMultiple) {
                        onSelectedChoicesChange(
                          selectedChoices.includes(choice)
                            ? selectedChoices.filter((item) => item !== choice)
                            : [...selectedChoices, choice],
                        );
                      } else {
                        onSelectedChoicesChange([choice]);
                      }
                    }}
                  />
                  {choice}
                </label>
              ))}
            </div>
          ) : null}
          {allowsFreeText && (
            <textarea
              value={inputDraft}
              onChange={(event) => onInputDraftChange(event.target.value)}
              rows={2}
              placeholder="回答・修正指示・追加相談"
              className="mt-2 w-full resize-y rounded border border-border bg-background px-2 py-1.5 text-xs outline-none focus-visible:ring-1 focus-visible:ring-primary"
              aria-label="Agentへの回答"
            />
          )}
          <div className="mt-1.5 flex items-center justify-end gap-1.5">
            <button
              type="button"
              className="rounded px-2 py-1 text-[11px] text-muted-foreground hover:bg-accent"
              onClick={onOpenDialogue}
            >
              履歴を確認
            </button>
            <button
              type="button"
              disabled={
                !canSubmitAoiDotsInput(
                  inputDraft,
                  selectedChoices,
                  choices,
                  allowsFreeText,
                )
              }
              className="inline-flex items-center gap-1 rounded bg-primary px-2 py-1 text-[11px] text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => onAnswer(pendingInput)}
            >
              <Send className="size-3" />
              回答を送る
            </button>
          </div>
        </div>
      )}

      {item.dialogue_id && (
        <div className="mt-2 flex items-center gap-1.5">
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-[11px] text-muted-foreground hover:bg-accent"
            onClick={onOpenDialogue}
          >
            <ChevronDown className="size-3" />
            Dialogue
            {dialogue ? ` · ${statusLabel(dialogue.job_status)}` : ""}
          </button>
          {((displayItem.allowed_actions?.includes("stop") ?? false) ||
            (!displayItem.allowed_actions && canStopAoiDotsWork(status))) && (
            <button
              type="button"
              className="rounded px-2 py-1 text-[11px] text-destructive hover:bg-destructive/10"
              onClick={() => onControl("stop")}
            >
              停止
            </button>
          )}
          {((displayItem.allowed_actions?.includes("rework") ?? false) ||
            (!displayItem.allowed_actions && canReworkAoiDotsWork(status))) && (
            <button
              type="button"
              className="rounded px-2 py-1 text-[11px] text-primary hover:bg-primary/10"
              onClick={() => setReworkEditorOpen(true)}
              disabled={reworkPending}
            >
              やり直す
            </button>
          )}
          {((displayItem.allowed_actions?.includes("resume") ?? false) ||
            (!displayItem.allowed_actions && status === "waiting")) && (
            <button
              type="button"
              className="rounded px-2 py-1 text-[11px] text-primary hover:bg-primary/10"
              onClick={() => onControl("resume")}
            >
              再開
            </button>
          )}
        </div>
      )}

      {reworkEditorOpen && item.dialogue_id && (
        <div className="mt-2 rounded border border-primary/25 bg-primary/5 p-2">
          <label className="text-[11px] font-medium text-foreground" htmlFor={`rework-${item.dialogue_id}`}>
            修正指示（任意）
          </label>
          <textarea
            id={`rework-${item.dialogue_id}`}
            value={reworkInstruction}
            onChange={(event) => onReworkInstructionChange(event.target.value)}
            maxLength={30_000}
            rows={3}
            placeholder="成果のどこを、どのように修正するか入力してください"
            className="mt-1 w-full resize-y rounded border border-border bg-background px-2 py-1.5 text-xs outline-none focus-visible:ring-1 focus-visible:ring-primary"
            aria-label="修正指示"
            disabled={reworkPending}
          />
          <div className="mt-1.5 flex items-center justify-end gap-1.5">
            <button
              type="button"
              className="rounded px-2 py-1 text-[11px] text-muted-foreground hover:bg-accent disabled:opacity-50"
              onClick={() => setReworkEditorOpen(false)}
              disabled={reworkPending}
            >
              キャンセル
            </button>
            <button
              type="button"
              className="rounded bg-primary px-2 py-1 text-[11px] text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => {
                setReworkEditorOpen(false);
                onControl("rework", reworkInstruction);
              }}
              disabled={reworkPending}
            >
              {reworkPending ? "送信中…" : "修正して再実行"}
            </button>
          </div>
        </div>
      )}
    </article>
  );
}

export function AoiDotsPanel() {
  const { aoiDotsOpen, setAoiDotsOpen, toggleAoiDots } = useShellChrome();
  const draftUserId = useCurrentUserId();
  const [delegationMapOpen, setDelegationMapOpen] = useState(false);
  const projectContext = useOptionalProject();
  const selectedProjectId = projectContext?.selectedProjectId ?? null;
  const includeProjectContext = Boolean(selectedProjectId);
  const projects = useMemo(
    () => projectContext?.projects ?? [],
    [projectContext?.projects],
  );
  const [profile, setProfile] = useState<AoiDotsProfile | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [workItems, setWorkItems] = useState<AoiDotsWorkItem[]>([]);
  const [dialogues, setDialogues] = useState<Record<string, AoiDotsDialogue>>({});
  const [questionPreviews, setQuestionPreviews] = useState<
    Record<string, { text: string; choices: string[]; messageId: string | null }>
  >({});
  const [inputDrafts, setInputDrafts] = useState<Record<string, string>>({});
  const [inputChoiceDrafts, setInputChoiceDrafts] = useState<Record<string, string[]>>({});
  const [reworkDrafts, setReworkDrafts] = useState<Record<string, string>>({});
  const [reworkPending, setReworkPending] = useState<Record<string, boolean>>({});
  const [streamingContent, setStreamingContent] = useState("");
  const [activeTool, setActiveTool] = useState<string | null>(null);
  const [liveToolResults, setLiveToolResults] = useState<ChatToolResultMetadata[]>([]);
  const [activeAgentRunId, setActiveAgentRunId] = useState<string | null>(null);
  const [generationStartedAt, setGenerationStartedAt] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [rootBusy, setRootBusy] = useState(false);
  const [historyFenceState, setHistoryFenceStateValue] = useState<"checking" | "clear" | "blocked" | "unavailable">("checking");
  const [historyFenceReviews, setHistoryFenceReviews] = useState<AoiDotsHistoryFenceDisplay[]>([]);
  const [historyFenceError, setHistoryFenceError] = useState<string | null>(null);
  const [historyFenceApplying, setHistoryFenceApplying] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const panelRef = useAoiDotsShortcut(
    aoiDotsOpen,
    toggleAoiDots,
    !loading && !error && Boolean(profile?.session_id),
  );
  const [refreshingWork, setRefreshingWork] = useState(false);
  const [dialogueLoading, setDialogueLoading] = useState<string | null>(null);
  const [stopPending, setStopPending] = useState(false);
  const [toolPermissionRequest, setToolPermissionRequest] =
    useState<ToolPermissionRequest | null>(null);
  const [externalModelPromptRequest, setExternalModelPromptRequest] =
    useState<ExternalModelPromptRequest | null>(null);
  const [externalModelPromptDraft, setExternalModelPromptDraft] = useState("");
  const [askUserQuestionRequest, setAskUserQuestionRequest] =
    useState<AskUserQuestionRequest | null>(null);
  const [askUserQuestionDraft, setAskUserQuestionDraft] = useState("");
  const [askUserQuestionChoices, setAskUserQuestionChoices] = useState<string[]>([]);
  const [planApprovalRequest, setPlanApprovalRequest] =
    useState<PlanApprovalRequest | null>(null);
  const [planApprovalFeedbackDraft, setPlanApprovalFeedbackDraft] = useState("");
  const [, setSteeringInstructions] = useState<SubmittedSteeringInstruction[]>([]);
  const processedEventIdsRef = useRef<Set<string>>(new Set());
  const processedLegacyMessageRef = useRef<unknown>(null);
  const streamingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const responsePollGenerationRef = useRef(0);
  const liveToolResultsRef = useRef<ChatToolResultMetadata[]>([]);
  const activeClientMessageIdRef = useRef<string | null>(null);
  const activeSubmittedDraftRef = useRef<{
    clientMessageId: string;
    content: string;
    mentions: MentionItem[];
    files: File[];
  } | null>(null);
  const activeAgentRunIdRef = useRef<string | null>(null);
  const generationEpochRef = useRef(0);
  const dispatchingRootRef = useRef(false);
  const messagesLoadGenerationRef = useRef(0);
  const generationStatusRequestRef = useRef(0);
  const bootstrapStartedRef = useRef(false);
  const unmountedRef = useRef(false);
  // 本文取得が失敗したsession。次の表示時の状態確認で一度だけ再取得する。
  const bodyFailedSessionRef = useRef<string | null>(null);
  const rootBusyRef = useRef(false);
  rootBusyRef.current = rootBusy;
  const statusInflightRef = useRef<{ sessionId: string; requestId: number; promise: Promise<void> } | null>(null);
  const workRefreshGenerationRef = useRef(0);
  const dialogueLoadGenerationRef = useRef(new Map<string, number>());
  const historyFenceGenerationRef = useRef(0);
  const historyFenceStateRef = useRef<"checking" | "clear" | "blocked" | "unavailable">("checking");
  const unresolvedInteractiveBusyRef = useRef(false);
  const setHistoryFenceState = useCallback((value: "checking" | "clear" | "blocked" | "unavailable") => {
    historyFenceStateRef.current = value;
    setHistoryFenceStateValue(value);
  }, []);

  const activeSessionId = profile?.session_id ?? null;
  const activeSessionIdRef = useRef<string | null>(null);
  activeSessionIdRef.current = activeSessionId;
  const activeGenerationKey = activeSessionId
    ? `${activeSessionId}:${generationEpochRef.current}`
    : null;
  const socket = useWebSocket(
    activeSessionId,
    activeAgentRunId,
    profile?.agent_id ?? null,
    selectedProjectId ?? null,
  );

  const loadMessages = useCallback(async (sessionId: string) => {
    const requestGeneration = ++messagesLoadGenerationRef.current;
    try {
      const result = await chatApi.getMessages(sessionId);
      if (messagesLoadGenerationRef.current !== requestGeneration) return null;
      setMessages((current) => mergeMessages(current, result.messages));
      if (bodyFailedSessionRef.current === sessionId) bodyFailedSessionRef.current = null;
      return result.messages;
    } catch (loadError) {
      if (messagesLoadGenerationRef.current !== requestGeneration) return null;
      bodyFailedSessionRef.current = sessionId;
      console.warn("AoiDots会話履歴の取得に失敗しました", loadError);
      return null;
    }
  }, []);

  const runGenerationStatus = useCallback(
    async (sessionId: string, reloadBody: boolean, retryFailedBody: boolean) => {
      const requestId = ++generationStatusRequestRef.current;
      try {
        const status = await chatApi.getGenerationStatus(sessionId);
        if (
          unmountedRef.current ||
          generationStatusRequestRef.current !== requestId ||
          activeSessionIdRef.current !== sessionId ||
          !acceptsAoiDotsGenerationStatus(
            status,
            sessionId,
            activeAgentRunIdRef.current,
            activeClientMessageIdRef.current,
            status.running
              ? undefined
              : false,
          )
        ) {
          return;
        }
        const statusRunId = status.agent_run_id ?? null;
        const statusClientId = status.client_message_id ?? null;
        if (status.running) {
          if (!activeClientMessageIdRef.current && statusClientId) {
            activeClientMessageIdRef.current = statusClientId;
            generationEpochRef.current += 1;
          }
          const runId = statusRunId ?? activeAgentRunIdRef.current;
          activeAgentRunIdRef.current = runId;
          setActiveAgentRunId(runId);
          setGenerationStartedAt(status.started_at ?? null);
          setRootBusy(true);
          setIsStreaming(true);
          return;
        }
        const hadActiveRun = Boolean(
          activeAgentRunIdRef.current || activeClientMessageIdRef.current,
        );
        const needsFinalBody =
          hadActiveRun ||
          rootBusyRef.current ||
          reloadBody ||
          (retryFailedBody && bodyFailedSessionRef.current === sessionId);
        if (
          hadActiveRun &&
          !statusRunId &&
          !statusClientId
        ) {
          const persisted = await loadMessages(sessionId);
          if (
            unmountedRef.current ||
            generationStatusRequestRef.current !== requestId ||
            activeSessionIdRef.current !== sessionId
          ) {
            return;
          }
          const runId = activeAgentRunIdRef.current;
          const clientId = activeClientMessageIdRef.current;
          const hasTerminalProof = hasAoiDotsTerminalProof(
            persisted,
            runId,
            clientId,
          );
          let proofRunId = runId;
          if (!hasTerminalProof && !proofRunId && clientId && profile?.agent_id) {
            try {
              const listed = unwrapAgentRuns(
                await conversationAgentApi.listAgentRuns(profile.agent_id, { limit: 100 }),
              ).filter(
                (candidate) =>
                  candidate.session_id === sessionId &&
                  (candidate as { client_message_id?: string | null }).client_message_id === clientId &&
                  candidate.agent_id === profile.agent_id,
              );
              if (listed.length !== 1) return;
              proofRunId = listed[0].id;
            } catch {
              return;
            }
          }
          if (!hasTerminalProof && proofRunId) {
            try {
              const runResult = await chatApi.getAgentRun(proofRunId, {
                includeEvents: false,
                includeToolCalls: false,
                includeTimeline: false,
              });
              const run = runResult.agent_run;
              if (
                generationStatusRequestRef.current !== requestId ||
                activeSessionIdRef.current !== sessionId ||
                run.id !== proofRunId ||
                run.session_id !== sessionId ||
                (profile?.agent_id && run.agent_id !== profile.agent_id) ||
                !isTerminalAoiDotsRunStatus(run.status)
              ) {
                return;
              }
            } catch {
              return;
            }
          } else if (!hasTerminalProof) {
            return;
          }
        }
        setRootBusy(false);
        setIsStreaming(false);
        setStopPending(false);
        activeClientMessageIdRef.current = null;
        activeSubmittedDraftRef.current = null;
        activeAgentRunIdRef.current = null;
        setActiveAgentRunId(null);
        // 初回hydrationで本文は取得済み。実行中だった/再接続時のみ最終本文を再取得する。
        if (needsFinalBody) await loadMessages(sessionId);
      } catch {
        // A disconnected status read is advisory; the websocket/history path
        // remains authoritative and retries on the next reconnect/display.
      }
    },
    [loadMessages, profile],
  );

  const refreshGenerationStatus = useCallback(
    (sessionId: string, options?: { reloadBody?: boolean; retryFailedBody?: boolean }) => {
      const reloadBody = options?.reloadBody ?? false;
      const retryFailedBody = options?.retryFailedBody ?? false;
      const inflight = statusInflightRef.current;
      // 送信/停止で世代が進んだ古い要求は破棄済みなので合流しない。
      if (inflight && inflight.sessionId === sessionId &&
        inflight.requestId === generationStatusRequestRef.current && !reloadBody &&
        !(retryFailedBody && bodyFailedSessionRef.current === sessionId)
      ) {
        return inflight.promise;
      }
      const promise: Promise<void> = runGenerationStatus(sessionId, reloadBody, retryFailedBody).finally(() => {
        if (statusInflightRef.current?.promise === promise) {
          statusInflightRef.current = null;
        }
      });
      statusInflightRef.current = {
        sessionId,
        requestId: generationStatusRequestRef.current,
        promise,
      };
      return promise;
    },
    [runGenerationStatus],
  );

  const aoiGenerationState = useMemo(
    () => ({
      ...initialChatGenerationState,
      lifecycle: {
        ...initialChatGenerationState.lifecycle,
        sessionId: activeSessionId,
        generationEpoch: rootBusy ? generationEpochRef.current : null,
        clientMessageId: rootBusy ? activeClientMessageIdRef.current : null,
        agentRunId: activeAgentRunId ?? activeAgentRunIdRef.current,
      },
    }),
    [activeAgentRunId, activeSessionId, rootBusy],
  );

  const permissionMessage =
    socket.lastMessage &&
    AOIDOTS_SHARED_EVENT_TYPES.has(socket.lastMessage.type)
      ? socket.lastMessage
      : null;

  useChatWebSocketEvents({
    lastMessage: permissionMessage,
    activeSessionId,
    connectionGeneration: socket.connectionGeneration,
    streamBuffer: socket.streamBuffer,
    currentSession: null,
    processedEventIdsRef,
    processedLegacyMessageRef,
    liveToolResultsRef,
    streamingIntervalRef,
    responsePollGenerationRef,
    dispatchChatTimeline: () => undefined,
    refreshPersistedMessages: loadMessages,
    bumpSessionForAssistant: () => undefined,
    maybeGenerateLoadedSessionTitle: async () => undefined,
    updateSidebarTitle: () => undefined,
    setLlmModeState: () => undefined,
    setLlmModeOptions: () => undefined,
    setLlmModeLabels: () => undefined,
    setToolPermissionRequest,
    setExternalModelPromptRequest,
    setExternalModelPromptDraft,
    sendExternalModelPromptResponse: socket.sendExternalModelPromptResponse,
    setAskUserQuestionRequest,
    setAskUserQuestionDraft,
    setAskUserQuestionChoices,
    setPlanApprovalRequest,
    setPlanApprovalFeedbackDraft,
    setSteeringInstructions,
    setStreamingContent,
    setLiveToolResults: (results) => {
      liveToolResultsRef.current = results;
      setLiveToolResults(results);
    },
    generationState: aoiGenerationState,
    dispatchGeneration: () => undefined,
    play: () => undefined,
    stopAudio: () => undefined,
    setVolume: () => undefined,
  });

  const refreshWork = useCallback(async (rootSessionId?: string | null) => {
    const requestGeneration = ++workRefreshGenerationRef.current;
    const requestedSessionId = rootSessionId?.trim() || null;
    setRefreshingWork(true);
    try {
      const result = await listAoiDotsWork(requestedSessionId);
      if (
        requestGeneration !== workRefreshGenerationRef.current ||
        (requestedSessionId !== null && activeSessionIdRef.current !== requestedSessionId)
      ) {
        return;
      }
      setWorkItems(result.items ?? []);
    } catch (workError) {
      if (requestGeneration !== workRefreshGenerationRef.current) return;
      console.warn("AoiDots workの取得に失敗しました", workError);
    } finally {
      if (requestGeneration === workRefreshGenerationRef.current) {
        setRefreshingWork(false);
      }
    }
  }, []);

  const refreshHistoryFence = useCallback(async (busyWasConfirmed = false): Promise<boolean> => {
    if (!profile?.session_id) {
      setHistoryFenceState("unavailable");
      setHistoryFenceError("AoiDots rootを確認できません。再読み込みしてください。");
      return false;
    }
    if (busyWasConfirmed) unresolvedInteractiveBusyRef.current = true;
    const requestGeneration = ++historyFenceGenerationRef.current;
    const sessionId = profile.session_id;
    historyFenceStateRef.current = "checking";
    await Promise.resolve();
    setHistoryFenceState("checking");
    setHistoryFenceError(null);
    try {
      const candidates = await listAoiDotsUncertainInteractiveWork(
        sessionId,
        profile.agent_id,
      );
      const boundedCandidates = candidates.slice(0, 50);
      const reviews = await Promise.all(boundedCandidates.map(async (candidate) => {
        try {
          return await previewAoiDotsHistoryFence(candidate, sessionId);
        } catch (reviewError) {
          const code = aoiDotsErrorCode(reviewError);
          const status =
            reviewError && typeof reviewError === "object" && "status" in reviewError
              ? Number((reviewError as { status?: unknown }).status)
              : null;
          const base: AoiDotsHistoryFenceDisplay = {
            workItemId: candidate.workItemId,
            safeErrorCode: candidate.safeErrorCode,
            status: code === "history_fence_already_releasable" ? "already_releasable" : "unavailable",
            stateDigest: null,
            runStatus: null,
            childWorkStates: [],
            deliveryBlockers: aoiDotsHistoryFenceDeliveryBlockers(reviewError),
            errorCode: code,
            errorStatus: status,
            errorMessage: aoiDotsHistoryFenceErrorMessage(reviewError),
          };
          return base;
        }
      }));
      if (
        requestGeneration !== historyFenceGenerationRef.current ||
        activeSessionIdRef.current !== sessionId
      ) return false;
      const actionable = reviews.filter(
        (review) => review.status !== "already_fenced" && review.status !== "already_releasable",
      );
      setHistoryFenceReviews(actionable);
      if (actionable.length > 0) {
        setHistoryFenceState("blocked");
        return false;
      }
      if (candidates.length > boundedCandidates.length) {
        setHistoryFenceReviews(reviews);
        setHistoryFenceState("blocked");
        setHistoryFenceError("対象Workが多いため一覧を分割して確認できません。Work一覧を整理して再確認してください。");
        return false;
      }
      if (candidates.length > 0) unresolvedInteractiveBusyRef.current = false;
      if (unresolvedInteractiveBusyRef.current) {
        setHistoryFenceState("blocked");
        setHistoryFenceError("サーバーはこの相談の継続を拒否しましたが、安全に確認できるWorkを特定できません。再確認しても解決しない場合は運用担当へ連絡してください。");
        return false;
      }
      setHistoryFenceReviews([]);
      setHistoryFenceState("clear");
      return true;
    } catch (reviewError) {
      if (requestGeneration !== historyFenceGenerationRef.current) return false;
      setHistoryFenceReviews([]);
      setHistoryFenceState(unresolvedInteractiveBusyRef.current ? "blocked" : "unavailable");
      setHistoryFenceError(aoiDotsHistoryFenceErrorMessage(
        reviewError,
        "不明状態のWorkを取得できません。送信を保留し、時間をおいて再確認してください。",
      ));
      return false;
    }
  }, [profile, setHistoryFenceState]);

  const applyHistoryFence = useCallback(async (review: AoiDotsHistoryFenceReview) => {
    if (!profile?.session_id || historyFenceApplying) return;
    setHistoryFenceApplying(review.workItemId);
    setHistoryFenceError(null);
    try {
      const result = await applyAoiDotsHistoryFence(review, profile.session_id);
      if (!new Set(["fenced", "already_fenced", "already_fenced_same_request"]).has(result.status)) {
        throw new Error("履歴区切りの結果を確認できませんでした。最新状態を再確認してください。");
      }
      unresolvedInteractiveBusyRef.current = false;
      toast.success("結果不明の履歴を再送せずに区切りました。相談の状態を確認しています。");
      await refreshHistoryFence();
    } catch (applyError) {
      const code = aoiDotsErrorCode(applyError);
      if (["history_fence_already_applied", "history_fence_already_releasable"].includes(code ?? "")) {
        unresolvedInteractiveBusyRef.current = false;
      }
      setHistoryFenceError(aoiDotsHistoryFenceErrorMessage(applyError));
      if (applyError && typeof applyError === "object" && "status" in applyError && Number((applyError as { status?: unknown }).status) === 409) {
        await refreshHistoryFence();
      }
    } finally {
      setHistoryFenceApplying(null);
    }
  }, [historyFenceApplying, profile, refreshHistoryFence]);

  useEffect(() => {
    if (!activeSessionId) return;
    unresolvedInteractiveBusyRef.current = false;
    const timerId = window.setTimeout(() => void refreshHistoryFence(), 0);
    return () => window.clearTimeout(timerId);
  }, [activeSessionId, refreshHistoryFence]);

  useEffect(() => {
    const event = socket.lastMessage;
    if (!event || !activeSessionId) return;
    if (event.__connection_session_id && event.__connection_session_id !== activeSessionId) return;
    if (
      event.__connection_generation !== undefined &&
      event.__connection_generation !== socket.connectionGeneration
    ) return;
    if (aoiDotsErrorCode(event) !== "interactive_session_busy") return;
    const eventClientId =
      typeof event.client_message_id === "string"
        ? event.client_message_id
        : typeof (event.data as { client_message_id?: unknown } | undefined)?.client_message_id === "string"
          ? (event.data as { client_message_id: string }).client_message_id
          : null;
    if (
      eventClientId &&
      activeClientMessageIdRef.current &&
      eventClientId !== activeClientMessageIdRef.current
    ) return;

    unresolvedInteractiveBusyRef.current = true;
    const rejectedDraft = activeSubmittedDraftRef.current;
    const rejectedClientId = eventClientId ?? rejectedDraft?.clientMessageId ?? activeClientMessageIdRef.current;
    if (rejectedDraft && rejectedClientId === rejectedDraft.clientMessageId) {
      updateChatComposerDraft(activeSessionId, (current) => {
        if (current.content.trim()) return current;
        return {
          ...EMPTY_CHAT_COMPOSER_DRAFT,
          content: rejectedDraft.content,
          mentions: rejectedDraft.mentions,
        };
      }, draftUserId);
      flushChatComposerDraftStore();
      setAttachedFiles((current) => current.length > 0 ? current : rejectedDraft.files);
    }
    if (rejectedClientId) {
      setMessages((current) => current.filter((message) => messageClientId(message) !== rejectedClientId));
    }
    activeSubmittedDraftRef.current = null;
    activeClientMessageIdRef.current = null;
    activeAgentRunIdRef.current = null;
    setActiveAgentRunId(null);
    setStreamingContent("");
    setIsStreaming(false);
    setRootBusy(false);
    generationStatusRequestRef.current += 1;
    toast.error("前回の実行結果を確認できないため、この相談は停止中です。入力を保持し、前回の実行は再送していません。");
    void refreshHistoryFence(true);
  }, [activeSessionId, draftUserId, refreshHistoryFence, socket.connectionGeneration, socket.lastMessage]);

  const bootstrap = useCallback(async () => {
    workRefreshGenerationRef.current += 1;
    setLoading(true);
    setError(null);
    try {
      const nextProfile = await bootstrapAoiDots(selectedProjectId ?? null);
      if (!nextProfile.session_id) {
        throw new Error("AoiDots root sessionが返されませんでした");
      }
      setProfile(nextProfile);
      activeSessionIdRef.current = nextProfile.session_id;
      await loadMessages(nextProfile.session_id);
      await refreshWork(nextProfile.session_id);
      // 生成状態の初回取得は activeSessionId effect が単独で担当する。
    } catch (bootstrapError) {
      setError(
        bootstrapError instanceof Error
          ? bootstrapError.message
          : "AoiDotsを読み込めませんでした",
      );
    } finally {
      setLoading(false);
    }
  }, [loadMessages, refreshWork, selectedProjectId]);

  useEffect(() => {
    if (bootstrapStartedRef.current) return;
    bootstrapStartedRef.current = true;
    void bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    if (!activeSessionId) return;
    const intervalId = window.setInterval(() => {
      void refreshWork(activeSessionId);
    }, 15_000);
    return () => window.clearInterval(intervalId);
  }, [activeSessionId, refreshWork]);

  useEffect(() => {
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
    };
  }, []);

  // 初回hydration（session確定）と、パネルを開いた時だけ状態を確認する。
  const statusHydratedSessionRef = useRef<string | null>(null);
  useEffect(() => {
    if (!activeSessionId) return;
    if (statusHydratedSessionRef.current !== activeSessionId) {
      statusHydratedSessionRef.current = activeSessionId;
      void refreshGenerationStatus(activeSessionId);
      return;
    }
    if (aoiDotsOpen) {
      void refreshGenerationStatus(activeSessionId, { retryFailedBody: true });
    }
  }, [activeSessionId, aoiDotsOpen, refreshGenerationStatus]);

  // 初回接続では何もせず、切断後の再接続でのみ状態と最終本文を回復する。
  const wsEverConnectedRef = useRef<string | null>(null);
  const wsWasConnectedRef = useRef(false);
  useEffect(() => {
    if (!activeSessionId) return;
    if (wsEverConnectedRef.current !== activeSessionId) {
      wsEverConnectedRef.current = null;
      wsWasConnectedRef.current = false;
    }
    if (!socket.isConnected) {
      wsWasConnectedRef.current = false;
      return;
    }
    const reconnected = wsEverConnectedRef.current === activeSessionId && !wsWasConnectedRef.current;
    wsEverConnectedRef.current = activeSessionId;
    wsWasConnectedRef.current = true;
    if (reconnected) void refreshGenerationStatus(activeSessionId, { reloadBody: true });
  }, [activeSessionId, socket.isConnected, refreshGenerationStatus]);

  useEffect(() => {
    const event = socket.lastMessage;
    if (!event || !activeSessionId) return;
    if (
      event.__connection_session_id &&
      event.__connection_session_id !== activeSessionId
    ) {
      return;
    }
    const eventSessionId =
      typeof event.session_id === "string" ? event.session_id : activeSessionId;
    if (eventSessionId !== activeSessionId) return;
    if (
      event.__connection_generation !== undefined &&
      event.__connection_generation !== socket.connectionGeneration
    ) {
      return;
    }

    const eventClientMessageId =
      typeof event.client_message_id === "string"
        ? event.client_message_id
        : typeof (event.data as { client_message_id?: unknown } | undefined)
              ?.client_message_id === "string"
          ? ((event.data as { client_message_id: string }).client_message_id)
          : null;
    if (
      eventClientMessageId &&
      activeClientMessageIdRef.current &&
      eventClientMessageId !== activeClientMessageIdRef.current
    ) {
      return;
    }

    const eventRunId =
      typeof event.agent_run_id === "string" ? event.agent_run_id : null;
    if (
      eventRunId &&
      activeAgentRunIdRef.current &&
      eventRunId !== activeAgentRunIdRef.current
    ) {
      return;
    }

    const eventType = String(event.type ?? "");
    const token = typeof event.content === "string" ? event.content : "";
    if (eventType === "stream_start" || eventType === "generation_started") {
      setIsStreaming(true);
      if (eventRunId) {
        activeAgentRunIdRef.current = eventRunId;
        setActiveAgentRunId(eventRunId);
      }
      setActiveTool(null);
      return;
    }
    if (eventType === "stream_token" || eventType === "token") {
      setIsStreaming(true);
      setStreamingContent((current) => current + token);
      if (typeof event.tool === "string") setActiveTool(event.tool);
      return;
    }
    if (eventType === "tool_start") {
      setActiveTool(typeof event.tool === "string" ? event.tool : "tool");
      return;
    }
    if (eventType === "tool_end") {
      setActiveTool(null);
      return;
    }
    if (eventType === "reasoning_progress") {
      setActiveTool("推論中");
      return;
    }
    if (eventType === "status_update") {
      if (event.status === "cancellation_pending") setActiveTool("停止処理中");
      return;
    }
    if (
      eventType === "stream_end" ||
      eventType === "response" ||
      eventType === "generation_completed"
    ) {
      setStreamingContent("");
      socket.streamBuffer.current = "";
      setActiveTool(null);
      generationStatusRequestRef.current += 1;
      setIsStreaming(false);
      setRootBusy(false);
      setStopPending(false);
      activeClientMessageIdRef.current = null;
      activeSubmittedDraftRef.current = null;
      activeAgentRunIdRef.current = null;
      setActiveAgentRunId(null);
      void loadMessages(activeSessionId);
      void refreshWork(activeSessionId);
      return;
    }
    if (
      eventType === "generation_stopped" ||
      eventType === "stop" ||
      eventType === "cancelled" ||
      eventType === "stream_cancelled"
    ) {
      if (eventType === "stream_cancelled" && event.status === "cancellation_pending") {
        setActiveTool("停止処理中");
        return;
      }
      setStreamingContent("");
      socket.streamBuffer.current = "";
      setActiveTool(null);
      generationStatusRequestRef.current += 1;
      setIsStreaming(false);
      setRootBusy(false);
      setStopPending(false);
      activeClientMessageIdRef.current = null;
      activeSubmittedDraftRef.current = null;
      activeAgentRunIdRef.current = null;
      setActiveAgentRunId(null);
      void loadMessages(activeSessionId);
      void refreshWork(activeSessionId);
      return;
    }
    if (eventType === "conversation_persisted") {
      void loadMessages(activeSessionId);
      if (event.role === "assistant") {
        generationStatusRequestRef.current += 1;
        setIsStreaming(false);
        setRootBusy(false);
        setStopPending(false);
        activeClientMessageIdRef.current = null;
        activeSubmittedDraftRef.current = null;
        activeAgentRunIdRef.current = null;
        setActiveAgentRunId(null);
      }
      return;
    }
    if (eventType === "cancellation_pending") {
      setActiveTool("停止処理中");
    }
  }, [
    activeSessionId,
    loadMessages,
    refreshWork,
    socket.connectionGeneration,
    socket.lastMessage,
    socket.streamBuffer,
    profile,
  ]);

  const handleSend = useCallback(
    async (
      content: string,
      files?: File[],
      mentions?: MentionItem[],
      _generationProfile?: string,
      commandCapabilities?: ChatCommandCapability[],
      toolsRequired?: boolean,
    ) => {
      if (
        !activeSessionId ||
        !content.trim() ||
        rootBusy ||
        dispatchingRootRef.current
      ) {
        return "failed" as const;
      }
      if (historyFenceStateRef.current !== "clear") {
        toast.error(
          historyFenceError ??
            "前回の実行結果を確認できないため、この相談は停止中です。送信前に状態を確認してください。",
        );
        return "failed" as const;
      }
      if (!(await refreshHistoryFence())) {
        toast.error(
          "前回の実行結果を確認できないため、この相談は停止中です。入力は保持し、前回の実行は再送していません。",
        );
        return "failed" as const;
      }
      dispatchingRootRef.current = true;
      generationStatusRequestRef.current += 1;
      const clientMessageId = newClientMessageId();
      setRootBusy(true);
      generationEpochRef.current += 1;
      setGenerationStartedAt(new Date().toISOString());
      activeClientMessageIdRef.current = clientMessageId;
      activeAgentRunIdRef.current = null;
      setActiveAgentRunId(null);
      setStreamingContent("");
      setActiveTool(null);
      setIsStreaming(true);
      const localMessage = createLocalUserMessage(
        activeSessionId,
        content,
        clientMessageId,
        files,
        commandCapabilities,
      );
      setMessages((current) => mergeMessages(current, [localMessage]));
      activeSubmittedDraftRef.current = {
        clientMessageId,
        content,
        mentions: mentions ?? [],
        files: files ?? [],
      };

      try {
        let accepted = false;
        if (socket.isConnected) {
          accepted = await socket.sendMessage(
            content,
            undefined,
            files,
            mentions,
            undefined,
            undefined,
            includeProjectContext,
            undefined,
            undefined,
            activeSessionId,
            clientMessageId,
            commandCapabilities,
            toolsRequired,
            null,
          );
        }
        if (!accepted) {
          const preparedAttachments = files?.length
            ? await prepareChatAttachmentPayload(content, files)
            : null;
          if (preparedAttachments?.upload_failed) {
            throw new Error("添付ファイルを保存できなかったため送信を中止しました。");
          }
          const result = await dispatchAoiDotsMessage(activeSessionId, {
            message: content,
            client_message_id: clientMessageId,
            expected_agent_id: profile?.agent_id ?? null,
            project_id: null,
            context_project_hint_id: selectedProjectId ?? null,
            include_project_context: includeProjectContext,
            command_capabilities: commandCapabilities,
            mentions,
            ...(preparedAttachments
              ? {
                  attachment_context: preparedAttachments.attachment_context,
                  attachments: preparedAttachments.attachments,
                }
              : {}),
            tools_required: toolsRequired,
          });
          const resultRecord =
            result && typeof result === "object"
              ? (result as Record<string, unknown>)
              : {};
          if (resultRecord.success !== true && resultRecord.queued !== true) {
            throw new Error(
              typeof resultRecord.message === "string"
                ? resultRecord.message
                : "AoiDotsへの送信に失敗しました",
            );
          }
          if (typeof resultRecord.agent_run_id === "string") {
            activeAgentRunIdRef.current = resultRecord.agent_run_id;
            setActiveAgentRunId(resultRecord.agent_run_id);
          }
        }
        void loadMessages(activeSessionId);
        void refreshWork(activeSessionId);
        return "accepted" as const;
      } catch (sendError) {
        setMessages((current) =>
          current.filter((message) => messageClientId(message) !== clientMessageId),
        );
        setIsStreaming(false);
        setRootBusy(false);
        activeClientMessageIdRef.current = null;
        activeSubmittedDraftRef.current = null;
        activeAgentRunIdRef.current = null;
        setActiveAgentRunId(null);
        if (aoiDotsErrorCode(sendError) === "interactive_session_busy") {
          unresolvedInteractiveBusyRef.current = true;
          setHistoryFenceState("blocked");
          await refreshHistoryFence(true);
          toast.error("前回の実行結果を確認できないため、この相談は停止中です。入力を保持し、前回の実行は再送していません。");
        } else {
          toast.error(
            sendError instanceof Error
              ? sendError.message
              : "AoiDotsへの送信に失敗しました",
          );
        }
        return "failed" as const;
      } finally {
        dispatchingRootRef.current = false;
      }
    },
    [
      activeSessionId,
      loadMessages,
      profile,
      historyFenceError,
      refreshHistoryFence,
      refreshWork,
      rootBusy,
      setHistoryFenceState,
      selectedProjectId,
      includeProjectContext,
      socket,
    ],
  );

  const handleStop = useCallback(async () => {
    if (!activeSessionId || stopPending) return;
    generationStatusRequestRef.current += 1;
    setStopPending(true);
    const targetClientId = activeClientMessageIdRef.current;
    const targetRunId = activeAgentRunIdRef.current;
    try {
      // The WS payload carries the current AgentRun id. A closed panel is never
      // a stop request, and no root stop is sent when there is no live target.
      if (!targetRunId) {
        toast.info("実行IDを確認中です。接続後に対象のrunを停止できます");
        setStopPending(false);
        return;
      }
      const sent = socket.stopGeneration(
        activeSessionId,
        targetRunId,
        targetClientId,
      );
      if (!sent) {
        throw new Error("AoiDotsの実行接続がありません。再接続後に停止してください");
      }
    } catch (stopError) {
      setStopPending(false);
      toast.error(
        stopError instanceof Error ? stopError.message : "AoiDotsの停止に失敗しました",
      );
    }
  }, [
    activeSessionId,
    socket,
    stopPending,
  ]);

  const openDialogue = useCallback(async (dialogueId: string) => {
    const requestGeneration = (dialogueLoadGenerationRef.current.get(dialogueId) ?? 0) + 1;
    dialogueLoadGenerationRef.current.set(dialogueId, requestGeneration);
    setDialogueLoading(dialogueId);
    try {
      const dialogue = await getAgentDialogue(dialogueId);
      if (dialogueLoadGenerationRef.current.get(dialogueId) !== requestGeneration) return;
      setDialogues((current) => ({ ...current, [dialogueId]: dialogue }));
      const pendingInput = dialogue.pending_inputs?.find(
        (input) => input.state === "pending",
      );
      const messageId = pendingInput?.question_message_id ?? null;
      const sessionId =
        dialogue.refs.worker_session_id ?? dialogue.refs.root_session_id;
      if (messageId && sessionId) {
        const history = await chatApi.getMessages(sessionId);
        if (dialogueLoadGenerationRef.current.get(dialogueId) !== requestGeneration) return;
        const questionMessage = history.messages.find(
          (message) => message.id === messageId,
        );
        if (questionMessage) {
          const rawChoices = questionMessage.metadata?.choices;
          setQuestionPreviews((current) => ({
            ...current,
            [inputKey(dialogueId, pendingInput?.id ?? messageId)]: {
              text: questionMessage.content,
              choices: Array.isArray(rawChoices)
                ? rawChoices.filter((choice): choice is string => typeof choice === "string")
                : [],
              messageId,
            },
          }));
        }
      }
    } catch (dialogueError) {
      if (dialogueLoadGenerationRef.current.get(dialogueId) !== requestGeneration) return;
      toast.error(
        dialogueError instanceof Error
          ? dialogueError.message
          : "Dialogue履歴を取得できませんでした",
      );
    } finally {
      if (dialogueLoadGenerationRef.current.get(dialogueId) === requestGeneration) {
        setDialogueLoading(null);
      }
    }
  }, []);

  useEffect(() => {
    for (const item of workItems) {
      const input = workInput(item);
      if (item.dialogue_id && input && !input.question?.trim()) {
        void openDialogue(item.dialogue_id);
      }
    }
  }, [openDialogue, workItems]);

  const answerInput = useCallback(
    async (item: AoiDotsWorkItem, input: AoiDotsPendingInput) => {
      if (!item.dialogue_id) return;
      const key = inputKey(item.dialogue_id, input.id);
      const selectedChoices = inputChoiceDrafts[key] ?? [];
      const answer = encodeAoiDotsAnswer(
        input.allow_free_text === false ? "" : inputDrafts[key] ?? "",
        selectedChoices.slice(0, input.allow_multiple ? undefined : 1),
        questionPreviews[key]?.choices ?? input.choices ?? [],
      );
      if (!answer) return;
      try {
        const dialogue = dialogues[item.dialogue_id];
        await answerAgentDialogueInput(item.dialogue_id, {
          input_id: input.id,
          answer,
          client_message_id: key,
          expected_control_revision: item.control_revision ??
            dialogue?.control_revision ?? input.expected_control_revision,
          expected_input_revision: input.expected_input_revision,
        });
        setInputDrafts((current) => {
          const next = { ...current };
          delete next[key];
          return next;
        });
        setInputChoiceDrafts((current) => {
          const next = { ...current };
          delete next[key];
          return next;
        });
        toast.success("Agentへ回答を送りました");
        await openDialogue(item.dialogue_id);
        await refreshWork(activeSessionId);
      } catch (answerError) {
        if (isStaleAoiDotsError(answerError)) {
          await Promise.allSettled([
            openDialogue(item.dialogue_id),
            refreshWork(activeSessionId),
          ]);
          toast.error("Workの状態が更新されたため、最新状態を読み込みました。もう一度確認してください");
          return;
        }
        toast.error(
          answerError instanceof Error
            ? answerError.message
            : "Agentへの回答に失敗しました",
        );
      }
    },
    [activeSessionId, dialogues, inputChoiceDrafts, inputDrafts, openDialogue, questionPreviews, refreshWork],
  );

  const controlDialogue = useCallback(
    async (
      item: AoiDotsWorkItem,
      action: "stop" | "resume" | "rework",
      instruction?: string,
    ) => {
      if (!item.dialogue_id) return;
      const dialogueId = item.dialogue_id;
      setReworkPending((current) => ({ ...current, [dialogueId]: true }));
      try {
        const payload: AoiDotsDialogueControlPayload = {
          action,
          expected_control_revision: item.control_revision ??
            dialogues[dialogueId]?.control_revision ?? 0,
          ...(action === "rework" && instruction?.trim()
            ? { instruction: instruction.trim() }
            : {}),
        };
        await controlAgentDialogue(dialogueId, payload);
        await openDialogue(dialogueId);
        await refreshWork(activeSessionId);
      } catch (controlError) {
        if (isStaleAoiDotsError(controlError)) {
          await Promise.allSettled([
            openDialogue(dialogueId),
            refreshWork(activeSessionId),
          ]);
          toast.error("Workの状態が更新されたため、最新状態を読み込みました。もう一度確認してください");
          return;
        }
        toast.error(
          controlError instanceof Error
            ? controlError.message
            : "Dialogue操作に失敗しました",
        );
      } finally {
        setReworkPending((current) => ({ ...current, [dialogueId]: false }));
      }
    },
    [activeSessionId, dialogues, openDialogue, refreshWork],
  );

  const handleToolPermissionDecision = useCallback(
    (approved: boolean, scope: ToolPermissionScope = "once") => {
      if (!toolPermissionRequest) return;
      socket.sendPermissionResponse(
        toolPermissionRequest.requestId,
        approved,
        approved ? scope : "once",
        toolPermissionRequest.sessionId,
      );
      setToolPermissionRequest(null);
    },
    [socket, toolPermissionRequest],
  );

  const handleExternalModelPromptDecision = useCallback(
    (approved: boolean) => {
      if (!externalModelPromptRequest) return;
      socket.sendExternalModelPromptResponse({
        requestId: externalModelPromptRequest.requestId,
        approved,
        finalPayload: approved ? externalModelPromptDraft : "",
        reviewNonce: externalModelPromptRequest.reviewNonce,
        bindingDigest: externalModelPromptRequest.bindingDigest,
        targetSessionId: externalModelPromptRequest.sessionId,
      });
      setExternalModelPromptRequest(null);
      setExternalModelPromptDraft("");
    },
    [externalModelPromptDraft, externalModelPromptRequest, socket],
  );

  const handleAskUserQuestionSubmit = useCallback(() => {
    if (!askUserQuestionRequest) return;
    const answer = askUserQuestionDraft.trim();
    if (!answer && askUserQuestionChoices.length === 0) return;
    socket.sendHumanInteractionResponse(
      askUserQuestionRequest.requestId,
      {
        action: "answer",
        answer,
        selected_choices: askUserQuestionChoices,
        revision: askUserQuestionRequest.revision,
      },
      askUserQuestionRequest.sessionId,
      "ask_user_question_response",
    );
    setAskUserQuestionRequest(null);
    setAskUserQuestionDraft("");
    setAskUserQuestionChoices([]);
  }, [askUserQuestionChoices, askUserQuestionDraft, askUserQuestionRequest, socket]);

  const handleAskUserQuestionCancel = useCallback(() => {
    if (!askUserQuestionRequest) return;
    socket.sendHumanInteractionResponse(
      askUserQuestionRequest.requestId,
      { cancelled: true, revision: askUserQuestionRequest.revision },
      askUserQuestionRequest.sessionId,
      "ask_user_question_response",
    );
    setAskUserQuestionRequest(null);
    setAskUserQuestionDraft("");
    setAskUserQuestionChoices([]);
  }, [askUserQuestionRequest, socket]);

  const handlePlanApprovalApprove = useCallback(() => {
    if (!planApprovalRequest) return;
    const payload = buildPlanApprovalResponsePayload(planApprovalRequest);
    if (!payload) {
      toast.error("承認対象を検証できないため、承認できません");
      return;
    }
    socket.sendHumanInteractionResponse(
      planApprovalRequest.requestId,
      payload,
      planApprovalRequest.sessionId,
      "plan_approval_response",
    );
    setPlanApprovalRequest(null);
  }, [planApprovalRequest, socket]);

  const handlePlanApprovalFeedback = useCallback(() => {
    if (!planApprovalRequest || !planApprovalFeedbackDraft.trim()) return;
    socket.sendHumanInteractionResponse(
      planApprovalRequest.requestId,
      {
        action: "feedback",
        feedback: planApprovalFeedbackDraft.trim(),
        plan_text: planApprovalRequest.planText,
        revision: planApprovalRequest.revision,
      },
      planApprovalRequest.sessionId,
      "plan_approval_response",
    );
    setPlanApprovalRequest(null);
    setPlanApprovalFeedbackDraft("");
  }, [planApprovalFeedbackDraft, planApprovalRequest, socket]);

  const handlePlanApprovalCancel = useCallback(() => {
    if (!planApprovalRequest) return;
    socket.sendHumanInteractionResponse(
      planApprovalRequest.requestId,
      { action: "cancel", cancelled: true, revision: planApprovalRequest.revision },
      planApprovalRequest.sessionId,
      "plan_approval_response",
    );
    setPlanApprovalRequest(null);
    setPlanApprovalFeedbackDraft("");
  }, [planApprovalRequest, socket]);

  const selectedProjectName = useMemo(
    () => projects.find((project) => project.id === selectedProjectId)?.name,
    [projects, selectedProjectId],
  );
  const pendingInputCount = workItems.reduce(
    (count, item) =>
      count +
      (item.pending_inputs?.filter((input) => input.state === "pending").length ?? 0),
    0,
  );

  return (
    <aside
      ref={panelRef}
      className={`fixed inset-y-14 right-0 z-[70] flex w-[min(44rem,100vw)] max-w-full flex-col border-l border-border bg-background shadow-2xl transition-transform duration-200 ${
        aoiDotsOpen ? "pointer-events-auto translate-x-0" : "pointer-events-none translate-x-full"
      }`}
      aria-hidden={!aoiDotsOpen}
      // The closed panel is only translated off-screen. inert keeps its
      // composer and controls out of focus and Tab order, and makes every
      // programmatic focus() inside it (autofocus, post-send refocus) a no-op,
      // so typing can never land in an invisible input.
      inert={!aoiDotsOpen}
      data-testid="aoi-dots-panel"
      data-aoi-dots-open={aoiDotsOpen ? "true" : "false"}
      data-aoi-dots-fence-state={historyFenceState}
    >
      <div className="flex min-h-12 shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        <Bot className="size-4 text-primary" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold">AoiDots</div>
          <div className="truncate text-[11px] text-muted-foreground">
            {profile?.agent_name || "owner-scoped consultation"}
            {selectedProjectName ? ` · context: ${selectedProjectName}` : ""}
          </div>
        </div>
        {isStreaming && (
          <button
            type="button"
            aria-label="AoiDotsの実行を停止"
            title="このAoiDots root runだけ停止"
            disabled={stopPending}
            onClick={() => void handleStop()}
            className="inline-flex size-7 items-center justify-center rounded border border-destructive/30 text-destructive hover:bg-destructive/10 disabled:opacity-50"
          >
            {stopPending ? <Loader2 className="size-3.5 animate-spin" /> : <Square className="size-3.5" />}
          </button>
        )}
        <button type="button" aria-expanded={delegationMapOpen && aoiDotsOpen} onClick={() => setDelegationMapOpen((value) => !value)} className="rounded px-1.5 py-1 text-xs">委任の流れ</button>
        <Link
          href="/operations"
          className="inline-flex size-7 items-center justify-center rounded text-muted-foreground hover:bg-accent hover:text-foreground"
          title="Agent identityと実行履歴"
          aria-label="Agent identityと実行履歴を開く"
        >
          <ExternalLink className="size-3.5" />
        </Link>
        <Link
          href="/settings#agents"
          className="rounded px-1.5 py-1 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          設定
        </Link>
        <button
          type="button"
          aria-label="AoiDotsを閉じる"
          onClick={() => setAoiDotsOpen(false)}
          className="inline-flex size-7 items-center justify-center rounded text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <X className="size-4" />
        </button>
      </div>

      {loading ? (
        <div className="flex flex-1 items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          AoiDotsを準備しています…
        </div>
      ) : error || !profile?.session_id ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
          <div className="text-sm text-destructive">{error || "AoiDots root sessionがありません"}</div>
          <button
            type="button"
            onClick={() => void bootstrap()}
            className="inline-flex items-center gap-1.5 rounded border border-border px-3 py-1.5 text-xs hover:bg-accent"
          >
            <RefreshCcw className="size-3.5" />
            再読み込み
          </button>
        </div>
      ) : (
        <>
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="relative min-h-0 flex-1">
              <ConversationDelegationMap presentation="message-overlay" sessionId={loading || error ? null : activeSessionId} ownerId={draftUserId} serverIdentity={typeof window === "undefined" ? "" : window.location.origin + window.location.pathname} open={aoiDotsOpen && delegationMapOpen} onClose={() => setDelegationMapOpen(false)} />
              <div className="h-full overflow-y-auto px-3 py-2">
                <MessageList
                  messages={messages}
                  emptyMessage="AoiDotsへの相談を入力してください。"
                  isStreaming={isStreaming}
                  isWaitingResponse={isStreaming && !streamingContent}
                  streamingContent={streamingContent}
                  liveToolResults={liveToolResults}
                  activeTool={activeTool}
                  activeAgentRunId={activeAgentRunId}
                  generationKey={activeGenerationKey}
                  generationStartedAt={generationStartedAt}
                  showGenerationActivity={isStreaming}
                />
              </div>
            </div>

            <div className="max-h-[36vh] shrink-0 overflow-y-auto border-t border-border-subtle px-3 py-2">
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <div className="text-xs font-semibold text-foreground">
                  進行中のWork / 判断待ち
                  {(pendingInputCount || workItems.length) > 0 && (
                    <span className="ml-1 text-[11px] font-normal text-muted-foreground">
                      {workItems.length}件 · {pendingInputCount}件待ち
                    </span>
                  )}
                </div>
                <button
                  type="button"
                  aria-label="AoiDots workを更新"
                  title="Workを更新"
                  onClick={() => void refreshWork(activeSessionId)}
                  disabled={refreshingWork}
                  className="inline-flex size-6 items-center justify-center rounded text-muted-foreground hover:bg-accent disabled:opacity-50"
                >
                  <RefreshCcw className={`size-3.5 ${refreshingWork ? "animate-spin" : ""}`} />
                </button>
              </div>
              {workItems.length === 0 ? (
                <div className="rounded border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
                  現在のWorkはありません。
                </div>
              ) : (
                <div className="space-y-2">
                  {workItems.map((item) => {
                    const dialogue = item.dialogue_id ? dialogues[item.dialogue_id] : undefined;
                    const pendingInput = workInput(item);
                    const draftKey = item.dialogue_id && pendingInput
                      ? inputKey(item.dialogue_id, pendingInput.id)
                      : "";
                    return (
                      <AoiDotsWorkCard
                        key={item.dialogue_id}
                        item={item}
                        dialogue={dialogue}
                        questionPreview={
                          draftKey ? questionPreviews[draftKey] : undefined
                        }
                        inputDraft={draftKey ? inputDrafts[draftKey] ?? "" : ""}
                        selectedChoices={draftKey ? inputChoiceDrafts[draftKey] ?? [] : []}
                        onInputDraftChange={(value) => {
                          if (!draftKey) return;
                          setInputDrafts((current) => ({ ...current, [draftKey]: value }));
                        }}
                        onSelectedChoicesChange={(value) => {
                          if (!draftKey) return;
                          setInputChoiceDrafts((current) => ({ ...current, [draftKey]: value }));
                        }}
                        onOpenDialogue={() => {
                          if (item.dialogue_id) void openDialogue(item.dialogue_id);
                        }}
                        onAnswer={(input) => void answerInput(item, input)}
                        onControl={(action, instruction) =>
                          void controlDialogue(item, action, instruction)
                        }
                        reworkInstruction={
                          item.dialogue_id ? reworkDrafts[item.dialogue_id] ?? "" : ""
                        }
                        onReworkInstructionChange={(value) => {
                          if (!item.dialogue_id) return;
                          setReworkDrafts((current) => ({
                            ...current,
                            [item.dialogue_id!]: value,
                          }));
                        }}
                        reworkPending={Boolean(
                          item.dialogue_id && reworkPending[item.dialogue_id],
                        )}
                      />
                    );
                  })}
                </div>
              )}
              {dialogueLoading && (
                <div className="mt-1 text-[11px] text-muted-foreground">Dialogueを読み込み中…</div>
              )}
            </div>

            {historyFenceState !== "clear" && (
              <section
                aria-live="polite"
                aria-busy={historyFenceState === "checking"}
                className="mb-2 max-h-56 shrink-0 space-y-2 overflow-y-auto rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-xs"
                data-testid="aoi-dots-history-fence"
                data-aoi-dots-fence-state={historyFenceState}
                role="alert"
              >
                <div className="font-semibold text-foreground">
                  {historyFenceState === "checking"
                    ? "相談の継続可否を確認しています…"
                    : "前回の実行結果を確認できないため、この相談は停止中"}
                </div>
                {historyFenceState !== "checking" && (
                  <p className="text-muted-foreground">
                    結果は不明のまま記録されます。前回の依頼は再送しません。確認内容を読んで明示操作した後に続行できます。
                  </p>
                )}
                {historyFenceError && (
                  <p className="text-destructive" data-testid="aoi-dots-history-fence-error">
                    {historyFenceError}
                  </p>
                )}
                {historyFenceReviews.map((review) => (
                  <div key={review.workItemId} className="space-y-1 rounded border border-border bg-background/70 p-2">
                    <div className="font-medium">Work {review.workItemId}</div>
                    {review.errorCode === "history_fence_child_pending" ? (
                      <div data-testid="aoi-dots-history-fence-blockers" className="space-y-1">
                        {(review.deliveryBlockers ?? []).length > 0 ? (
                          <>
                            <div className="font-medium">未完了の内部deliveryがあります</div>
                            {review.deliveryBlockers?.map((blocker) => (
                              <div key={blocker.id}>
                                {shortHistoryFenceDeliveryId(blocker.id)} · {blocker.kind} · {blocker.state}
                              </div>
                            ))}
                            {review.childWorkStates.length > 0 && (
                              <div>{childWorkSummary(review.childWorkStates)}</div>
                            )}
                          </>
                        ) : (
                          <div>未完了の内部処理があります。詳細は取得できませんでした。</div>
                        )}
                      </div>
                    ) : (
                      <>
                        <div>Run状態: {historyFenceStatusLabel(review.runStatus)}</div>
                        <div>{childWorkSummary(review.childWorkStates)}</div>
                      </>
                    )}
                    <div className="text-muted-foreground">結果不明 · providerへの再送なし · 利用量も不明のままです。</div>
                    {review.status === "dry_run_ok" && review.stateDigest ? (
                      <button
                        type="button"
                        className="mt-1 rounded border border-amber-500/50 px-2 py-1 font-medium hover:bg-amber-500/10 disabled:opacity-50"
                        disabled={historyFenceApplying !== null}
                        onClick={() => void applyHistoryFence(review)}
                      >
                        {historyFenceApplying === review.workItemId
                          ? "確認を適用中…"
                          : "結果不明のまま履歴を区切り、相談を続ける"}
                      </button>
                    ) : (
                      <p className="text-destructive">{review.errorMessage ?? "このWorkは自動で解除できません。追加確認が必要です。"}</p>
                    )}
                  </div>
                ))}
                <button
                  type="button"
                  className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 hover:bg-accent disabled:opacity-50"
                  disabled={historyFenceState === "checking"}
                  onClick={() => void refreshHistoryFence(unresolvedInteractiveBusyRef.current)}
                >
                  <RefreshCcw className="size-3" />
                  状態を再確認
                </button>
              </section>
            )}

            <ChatComposer
              presentation="aoidots"
              draftKey={profile.session_id}
              sessionId={profile.session_id}
              agentId={profile.agent_id}
              onSend={handleSend}
              disabled={false}
              busy={rootBusy}
              busySubmitBehavior="block"
              onStop={() => void handleStop()}
              attachedFiles={attachedFiles}
              onAttachedFilesChange={setAttachedFiles}
              projectId={null}
              projectScopeReady
            />
          </div>
        </>
      )}

      <ToolPermissionDialog
        request={toolPermissionRequest}
        onDecision={handleToolPermissionDecision}
      />
      <ExternalModelPromptDialog
        request={externalModelPromptRequest}
        draft={externalModelPromptDraft}
        onDraftChange={setExternalModelPromptDraft}
        onKeyDown={() => undefined}
        onDecision={handleExternalModelPromptDecision}
      />
      <AskUserQuestionDialog
        request={askUserQuestionRequest}
        draft={askUserQuestionDraft}
        selectedChoices={askUserQuestionChoices}
        onDraftChange={setAskUserQuestionDraft}
        onSelectedChoicesChange={setAskUserQuestionChoices}
        onSubmit={handleAskUserQuestionSubmit}
        onCancel={handleAskUserQuestionCancel}
      />
      <PlanApprovalDialog
        request={planApprovalRequest}
        feedbackDraft={planApprovalFeedbackDraft}
        onFeedbackDraftChange={setPlanApprovalFeedbackDraft}
        onApprove={handlePlanApprovalApprove}
        onFeedback={handlePlanApprovalFeedback}
        onCancel={handlePlanApprovalCancel}
      />
    </aside>
  );
}


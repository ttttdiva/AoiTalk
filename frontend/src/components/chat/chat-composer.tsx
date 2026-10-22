"use client";

import {
  AppSelect,
  type AppSelectChangeEvent,
  type AppSelectOpenChangeDetails,
} from "@/components/ui/app-select";
import Link from "next/link";

import {
  useState,
  useRef,
  useEffect,
  useLayoutEffect,
  useCallback,
  useMemo,
  useSyncExternalStore,
  type Dispatch,
  type KeyboardEvent as ReactKeyboardEvent,
  type SetStateAction,
} from "react";
import { useSWRConfig } from "swr";
import {
  Brain,
  Send,
  Square,
  Plus,
  Paperclip,
  X,
  FolderOpen,
  Search,
  FileText,
  CornerDownRight,
  Pencil,
  Zap,
  Gauge,
  AppWindow,
} from "lucide-react";
import { MentionMenu, type MentionItem } from "@/components/chat/mention-menu";
import { ChatIntentDialog } from "@/components/chat/chat-intent-dialog";
import { useChatIntent } from "@/hooks/use-chat-intent";
import { appendChatIntent, type ChatIntentCandidate } from "@/lib/chat-intent-api";
import { ChatComposerInput } from "@/components/chat/chat-composer-input";
import { ChatQuickPrompts } from "@/components/chat/chat-quick-prompts";
import {
  AppContextPicker,
  type ChatAppContextSelection,
} from "@/components/chat/app-context-picker";
import { GenerationProfileSelector } from "@/components/chat/generation-profile-selector";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Command,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
} from "@/components/ui/command";
import { cn, formatBytes } from "@/lib/utils";
import { resolveChatToolsRequired } from "@/lib/chat-tool-intent";
import { useMarkdownShortcuts } from "@/hooks/use-markdown-shortcuts";
import { useSnippetAutocomplete } from "@/hooks/use-snippet-autocomplete";
import { SnippetPopup } from "@/components/ui/snippet-popup";
import { VoicePanel } from "@/components/voice/voice-panel";
import { useSnippets } from "@/contexts/snippets-context";
import { useUserSettings } from "@/contexts/user-settings-context";
import type { UserSettings } from "@/lib/user-settings";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import {
  getChatComposerShortcutAction,
  resolveComposerBusyEnterAction,
} from "@/lib/chat-keyboard-shortcuts";
import { chatAttachmentError } from "@/lib/chat-attachment-validation";
import type {
  ContextRequestSnapshot,
  ContextSnapshot,
  LlmMode,
} from "@/lib/chat-api";
import { resolveMainContextSnapshot } from "@/lib/chat-api";
import {
  DEFAULT_GENERATION_PROFILE,
  getSettingsGenerationProfile,
  loadStoredGenerationProfile,
  saveStoredGenerationProfile,
  type GenerationProfile,
} from "@/lib/generation-profile";
import {
  DEFAULT_PLANNING_POLICY,
  loadStoredPlanningPolicy,
  saveStoredPlanningPolicy,
  type PlanningPolicy,
} from "@/lib/planning-policy";
import { PlanningPolicySelector } from "@/components/chat/planning-policy-selector";
import { safeLocalStorage, safeSessionStorage } from "@/lib/safe-storage";
import { takeChatDraftHandoff } from "@/lib/chat-draft-handoff";
import {
  EMPTY_CHAT_COMPOSER_DRAFT,
  NEW_CHAT_COMPOSER_DRAFT_KEY,
  type ChatComposerDispatchMode,
  type ChatComposerDraft,
  clearChatComposerDraft,
  flushChatComposerDraftStore,
  getChatComposerDraft,
  gcChatComposerDrafts,
  hydrateChatComposerDraft,
  promoteNewChatComposerDraft,
  subscribeChatComposerDraft,
  updateChatComposerDraft,
} from "@/lib/chat-composer-draft-store";
import { isChatComposerCursorInCodeBlock } from "@/lib/chat-composer-blocks";
import { formatRouteLabel } from "@/lib/chat-session-route";
import { useChatSessionRoute } from "@/hooks/use-chat-session-route";
import { useLocalRuntimeStatus } from "@/hooks/use-local-runtime-status";
import {
  completeChatCommandPrefix,
  filterChatCommands,
  filterVisibleChatSkillCommands,
  firstMatchingChatCommand,
  findChatCommand,
  isHiddenChatSkillName,
  isChatHelpCommand,
  isSlashCommandToken,
  resolveChatCommandSubmission,
  type ActiveChatCommand,
  type ChatCommandDefinition,
  type ChatCommandCapability,
} from "@/lib/chat-commands";
import {
  CHAT_SEND_RESTORE_EVENT,
  type ChatSendAttachmentMetadata,
  type ChatSendRestoreEventDetail,
} from "@/lib/chat-send-outbox";
import { toast } from "sonner";
import { createDocsNodeWikilink } from "@/lib/docs-references";
import {
  filterAvailableProviders,
  filterVisibleProviders,
  hasDeploymentMetadata,
  resolveEffectiveModelId,
  resolveEffectiveProviderId,
} from "@/lib/llm-provider-visibility";
import {
  ChatModelSettingsFields,
} from "@/components/chat/chat-model-settings-fields";
import { characterOptionLabel } from "@/lib/character-options";
import {
  useOptionalRuntimeContext,
  type RuntimeContextValue,
  type RuntimeLlmEngine,
} from "@/contexts/runtime-context";

export type ChatComposerProps = {
  onSend: (
    content: string,
    files?: File[],
    mentions?: MentionItem[],
    generationProfile?: GenerationProfile,
    commandCapabilities?: ChatCommandCapability[],
    toolsRequired?: boolean,
    appContext?: ChatAppContextSelection | null,
    options?: ChatComposerSendOptions,
  ) => void | boolean | ChatComposerSendResult | Promise<void | boolean | ChatComposerSendResult>;
  onSteer?: (content: string) => void;
  onStop?: () => void;
  disabled: boolean;
  busy?: boolean;
  /** A dedicated controller may keep the composer visible while blocking a
   * second root-lane submission instead of queueing/steering it. */
  busySubmitBehavior?: "queue" | "block";
  /** generation reducerが初回terminal遷移時だけ更新する一意キー */
  generationTerminalKey?: string | null;
  attachedFiles: File[];
  onAttachedFilesChange: (next: SetStateAction<File[]>, sessionKey?: string) => void;
  onAttachmentsPromoted?: (sessionId: string, submittedFiles: File[]) => void;
  projectContextEnabled?: boolean;
  onProjectContextToggle?: (enabled: boolean) => void;
  deepResearchEnabled?: boolean;
  onDeepResearchToggle?: (enabled: boolean) => void;
  llmMode?: LlmMode;
  llmModeOptions?: LlmMode[];
  llmModeLabels?: Record<string, string>;
  llmModeLoading?: boolean;
  llmModeError?: string | null;
  onLlmModeChange?: (mode: LlmMode) => void;
  projectId?: string | null;
  /**
   * プロジェクト選択の初期復元が完了したか。false の間はスキル一覧の取得を待つ。
   * 未指定時は true 扱い（従来どおり即時取得）。
   */
  projectScopeReady?: boolean;
  sessionId?: string | null;
  /** Durable Operations Agent binding for this Chat session, when present. */
  agentId?: string | null;
  contextSnapshot?: ContextSnapshot | null;
  contextSnapshotStatus?: string;
  appContext?: ChatAppContextSelection | null;
  onAppContextChange?: (value: ChatAppContextSelection | null) => void;
  /** S9 supplies a session for Live Voice when this composer is a new chat. */
  ensureLiveVoiceConversationSession?: () => Promise<string>;
  /** Latest durable assistant answer used by Local Pipeline TTS. */
  voiceAssistantMessage?: { id: string; text: string } | null;
  /** Keep the shared input/attachment behavior while hiding normal Chat controls. */
  presentation?: "normal" | "aoidots";
  /** Durable draft namespace for a non-route-owned controller. */
  draftKey?: string | null;
};

export type ChatComposerSendResult =
  | "accepted"
  | "pending"
  | "failed"
  /** The bubble/outbox retained the submission for an inline retry. */
  | "retained";

export type ChatComposerSendOptions = {
  /** Called only for this send's new session, immediately before activation. */
  onSessionPromoted?: (sessionId: string) => void;
  /** Reuse a durable idempotency key when restoring a failed send. */
  clientMessageId?: string;
  /** Replay the route selected when an uncertain draft was first submitted. */
  dispatchMode?: ChatComposerDispatchMode;
};

const intentAttachmentIds = new WeakMap<File, number>();
const UUID_CLIENT_MESSAGE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;
let nextIntentAttachmentId = 0;
function getIntentAttachmentId(file: File): number {
  let id = intentAttachmentIds.get(file);
  if (id === undefined) {
    id = ++nextIntentAttachmentId;
    intentAttachmentIds.set(file, id);
  }
  return id;
}

const runtimeSelectClassName =
  "h-9 min-w-0 rounded-md border border-input bg-background px-2 text-xs text-foreground outline-none transition-colors focus-visible:border-ring";

const STRICT_RUNTIME_PROVIDERS = new Set([
  "openai_compatible_local",
  "ollama",
  "sglang",
]);

type DisplayRoute = {
  provider: string;
  model: string;
};

/**
 * Runtime LLM metadata is loaded from more than one endpoint.  The engine
 * response (or a catalog response) can therefore be available before
 * `currentLlm` has been committed.  Keep that partial information useful for
 * the small, non-authoritative Provider / Model summary without changing the
 * route used for generation.
 */
function normalizeDisplayRoute(
  provider?: string | null,
  model?: string | null,
): DisplayRoute | null {
  const normalizedProvider = provider?.trim().toLowerCase() ?? "";
  const normalizedModel = model?.trim() ?? "";
  return normalizedProvider && normalizedModel
    ? { provider: normalizedProvider, model: normalizedModel }
    : null;
}

function resolveRuntimeDisplayRoute(
  runtime: RuntimeContextValue,
): DisplayRoute | null {
  const current = normalizeDisplayRoute(
    runtime.currentLlm?.provider,
    runtime.currentLlm?.model,
  );
  if (current) return current;

  const effective = normalizeDisplayRoute(
    runtime.effectiveLlm?.provider,
    runtime.effectiveLlm?.model,
  );
  if (effective) return effective;

  const persisted = normalizeDisplayRoute(
    runtime.persistedLlm?.provider,
    runtime.persistedLlm?.model,
  );
  if (persisted) return persisted;

  const catalogCurrent = normalizeDisplayRoute(
    runtime.llmCatalog?.current?.provider,
    runtime.llmCatalog?.current?.model,
  );
  if (catalogCurrent) return catalogCurrent;

  // This is intentionally provisional: the session/new-chat route remains
  // authoritative when it becomes available.  It only prevents an unrelated
  // catalog/character/features request from blanking an engine-first display.
  const firstEngine = runtime.llmEngines.find((engine) =>
    normalizeDisplayRoute(engine.provider, engine.model),
  );
  return firstEngine
    ? normalizeDisplayRoute(firstEngine.provider, firstEngine.model)
    : null;
}

function draftsMatch(a: ChatComposerDraft, b: ChatComposerDraft): boolean {
  const activeCommandMatches =
    a.activeCommand === b.activeCommand ||
    (a.activeCommand !== null &&
      b.activeCommand !== null &&
      a.activeCommand.command === b.activeCommand.command &&
      a.activeCommand.label === b.activeCommand.label &&
      a.activeCommand.description === b.activeCommand.description &&
      a.activeCommand.kind === b.activeCommand.kind &&
      a.activeCommand.capability === b.activeCommand.capability);
  return (
    a.content === b.content &&
    a.clientMessageId === b.clientMessageId &&
    a.dispatchMode === b.dispatchMode &&
    a.toolFreeMode === b.toolFreeMode &&
    activeCommandMatches &&
    a.mentions.length === b.mentions.length &&
    a.mentions.every(
      (mention, index) =>
        mention.type === b.mentions[index]?.type &&
        mention.id === b.mentions[index]?.id &&
        mention.name === b.mentions[index]?.name &&
        mention.detail === b.mentions[index]?.detail,
    )
  );
}

function createComposerClientMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `00000000-0000-4000-8000-${Math.random().toString(16).slice(2, 14).padEnd(12, "0")}`;
}

export type SubmittedSteeringInstruction = {
  id: string;
  content: string;
  createdAt: string;
  status: "sending" | "interrupting" | "queued" | "failed";
};

type QueuedChatMessage = {
  id: string;
  sessionId: string | null;
  content: string;
  // 添付は composer state から切り離して送信単位で保持する。
  // File は localStorage に保存できないため、キューが生きている間だけ
  // メモリ上の snapshot として扱い、draft storage には含めない。
  attachedFiles: File[];
  generationProfile: GenerationProfile;
  mentions: MentionItem[];
  capabilities: ChatCommandCapability[];
  toolsRequired?: boolean;
  appContext: ChatAppContextSelection | null;
  draftSnapshot: ChatComposerDraft;
  // enqueue 中も下書きを localStorage に保持するための専用スコープ。
  draftStorageKey: string;
  // session 切替後も失敗したキューを元の会話へ復元する。
  draftRestoreKey: string;
};

// crypto.randomUUID は secure context 限定のため、LAN の http 配信などでも
// 落ちないようフォールバックを用意する。
function createQueueId(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `queue-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Re-attach files from a failed/edited queue item without duplicating files
 * that are already present in the current composer.  File object identity is
 * intentional: two files with the same name are still distinct attachments.
 */
function appendQueuedAttachments(
  current: File[],
  queued: readonly File[],
): File[] {
  if (queued.length === 0) return current;
  const available = new Map<File, number>();
  for (const file of current) {
    available.set(file, (available.get(file) ?? 0) + 1);
  }
  const additions: File[] = [];
  for (const file of queued) {
    const count = available.get(file) ?? 0;
    if (count > 0) {
      available.set(file, count - 1);
    } else {
      additions.push(file);
    }
  }
  return additions.length > 0 ? [...current, ...additions] : current;
}

/** Remove only the files belonging to one queue item, preserving newer input. */
function removeQueuedAttachments(
  current: File[],
  queued: readonly File[],
): File[] {
  if (queued.length === 0 || current.length === 0) return current;
  const remaining = new Map<File, number>();
  for (const file of queued) {
    remaining.set(file, (remaining.get(file) ?? 0) + 1);
  }
  let removed = false;
  const next = current.filter((file) => {
    const count = remaining.get(file) ?? 0;
    if (count <= 0) return true;
    remaining.set(file, count - 1);
    removed = true;
    return false;
  });
  return removed ? next : current;
}

function formatTokens(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return Math.round(value).toLocaleString();
}

function resolveContextPercentage(
  snapshot?: ContextRequestSnapshot | null,
): number | null {
  if (!snapshot) return null;
  const provided = snapshot.usage_percent ?? snapshot.percentage;
  if (provided != null) return Math.max(0, Math.min(100, provided));
  if (snapshot.input_tokens != null && snapshot.context_window_tokens) {
    return Math.max(
      0,
      Math.min(
        100,
        (snapshot.input_tokens / snapshot.context_window_tokens) * 100,
      ),
    );
  }
  return null;
}

function measurementLabel(value?: string): string {
  return value === "measured"
    ? "実測"
    : value === "tokenizer_estimate" || value === "estimated"
      ? "推定"
      : value === "character_estimate" || value === "approximate"
        ? "概算"
        : "不明";
}

function ContextWindowDetails({
  snapshot,
  status,
}: {
  snapshot?: ContextSnapshot | null;
  status?: string;
}) {
  // A turn can contain retries, tool follow-ups, and specialist requests.
  // The persisted top-level snapshot (or an explicit `main` snapshot from a
  // newer backend) is the effective Main input.  The request series remains
  // diagnostic metadata and is never presented as a user-selectable total.
  const current = resolveMainContextSnapshot(snapshot);
  const requestCount =
    snapshot?.request_count ?? snapshot?.requests?.length ?? (current ? 1 : 0);
  const requestsOmitted = snapshot?.requests_omitted ?? 0;
  const percentage = resolveContextPercentage(current);
  const warning = percentage != null && percentage >= 80;
  const remaining =
    current?.remaining_tokens ??
    (current?.context_window_tokens != null && current.input_tokens != null
      ? Math.max(0, current.context_window_tokens - current.input_tokens)
      : null);
  const categories = (current?.components ?? current?.categories ?? []).filter(
    (item) =>
      item.tokens != null ||
      item.preview ||
      item.status === "deferred" ||
      item.selection_reason ||
      item.duration_ms != null ||
      item.selected_chars != null,
  );
  return (
    <div className="space-y-3" data-context-window-detail="main">
          <div>
            <div className="flex items-center justify-between gap-3">
              <span className="font-medium">
                コンテキストウィンドウ <span className="text-primary">(Main)</span>
              </span>
              <span className="text-sm font-semibold tabular-nums">
                {current
                  ? `${formatTokens(current.input_tokens)} / ${formatTokens(current.context_window_tokens)} (${percentage == null ? "—" : `${Math.round(percentage)}%`})`
                  : "—"}
              </span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
              <div
                className={cn(
                  "h-full rounded-full",
                  warning ? "bg-destructive" : "bg-primary",
                )}
                style={{ width: `${percentage ?? 0}%` }}
              />
            </div>
          </div>
          {current ? (
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
              <span className="text-muted-foreground">残りトークン</span>
              <span className="text-right tabular-nums">
                {formatTokens(remaining)}
              </span>
              <span className="text-muted-foreground">Provider</span>
              <span
                className="truncate text-right"
                title={current.provider ?? undefined}
              >
                {current.provider ?? "不明"}
              </span>
              <span className="text-muted-foreground">Model</span>
              <span
                className="truncate text-right"
                title={current.model ?? undefined}
              >
                {current.model ?? "不明"}
              </span>
              <span className="text-muted-foreground">計測</span>
              <span className="text-right">
                {measurementLabel(current.measurement)}
              </span>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              {status === "loading"
                ? "取得中…"
                : "このチャットには利用可能なSnapshotがありません。"}
            </p>
          )}
          {(requestCount > 1 || requestsOmitted > 0 || current?.request_kind) && (
            <div className="space-y-1 border-t pt-2 text-xs" data-context-diagnostics="true">
              <div className="font-medium">計測診断</div>
              <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-muted-foreground">
                <span>Main input</span>
                <span className="text-right text-foreground">1件</span>
                <span>内部リクエスト履歴</span>
                <span className="text-right text-foreground">
                  {requestCount}件{requestsOmitted > 0 ? `（${requestsOmitted}件省略）` : ""}
                </span>
                {current?.request_kind && (
                  <>
                    <span>種別</span>
                    <span className="truncate text-right text-foreground" title={current.request_kind}>
                      {current.request_kind}
                    </span>
                  </>
                )}
              </div>
              <p className="text-[10px] text-muted-foreground">
                内部履歴はMainの使用量へ合算していません。
              </p>
            </div>
          )}
          {categories.length > 0 && (
            <div className="space-y-2 border-t pt-2">
              <div className="text-xs font-medium">内訳</div>
              {categories.map((item, index) => {
                const itemPercentage =
                  item.percentage ??
                  (current?.input_tokens && item.tokens != null
                    ? (item.tokens / current.input_tokens) * 100
                    : null);
                return (
                  <div
                    key={item.id ?? item.category ?? `${item.label}-${index}`}
                    className="rounded-md border bg-muted/30 p-2 text-xs"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className="font-medium">{item.label}</span>
                      <span className="shrink-0 tabular-nums">
                        {formatTokens(item.tokens)}
                        {itemPercentage == null
                          ? ""
                          : ` · ${itemPercentage.toFixed(1)}%`}
                      </span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-2 text-[10px] text-muted-foreground">
                      <span>{item.status ?? "active"}</span>
                      <span>{measurementLabel(item.measurement)}</span>
                      {item.source && <span>source: {item.source}</span>}
                      {item.selection_reason && (
                        <span>reason: {item.selection_reason}</span>
                      )}
                      {item.duration_ms != null && (
                        <span>{item.duration_ms.toFixed(1)}ms</span>
                      )}
                      {(item.selected_chars ?? item.size_chars) != null && (
                        <span>
                          chars: {item.selected_chars ?? item.size_chars}
                          {item.retrieved_chars != null
                            ? ` / ${item.retrieved_chars}`
                            : ""}
                        </span>
                      )}
                    </div>
                    {item.preview && (
                      <p className="mt-1 line-clamp-2 break-words text-muted-foreground">
                        {item.preview}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
    </div>
  );
}

type ChatSettingsItem =
  | "provider"
  | "model"
  | "effort"
  | "agentTeam"
  | "executionProfile"
  | "character";

type ChatSettingsItemState = {
  id: ChatSettingsItem;
  disabled: boolean;
};

/**
 * The composer has one model entry point.  Provider/model, the provider's
 * reasoning/response mode, character and the Main context budget are
 * deliberately kept together so a model change cannot leave a second,
 * unrelated control stale.
 */
function ModelControl({
  runtime,
  userSettings,
  llmMode,
  llmModeOptions,
  llmModeLabels,
  llmModeLoading = false,
  llmModeError = null,
  onLlmModeChange,
  sessionId,
  userScopeKey,
  contextSnapshot,
  contextSnapshotStatus,
  open,
  onOpenChange,
  onComposerFocusRequest,
  onLlmModeRefresh,
  agentId,
}: {
  runtime: RuntimeContextValue;
  userSettings: UserSettings | null | undefined;
  llmMode?: LlmMode;
  llmModeOptions: LlmMode[];
  llmModeLabels: Record<string, string>;
  llmModeLoading?: boolean;
  llmModeError?: string | null;
  onLlmModeChange?: (mode: LlmMode) => void;
  sessionId?: string | null;
  userScopeKey?: string | null;
  contextSnapshot?: ContextSnapshot | null;
  contextSnapshotStatus?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onComposerFocusRequest?: () => void;
  onLlmModeRefresh?: () => Promise<void>;
  agentId?: string | null;
}) {
  const normalizedAgentId = agentId?.trim() || null;
  const agentBound = Boolean(normalizedAgentId);
  const routeState = useChatSessionRoute({
    runtime,
    sessionId,
    userSettings,
  });
  const {
    effectiveProvider: sessionProvider,
    effectiveModel: sessionModel,
    effectiveEffort,
    effortOptions: sessionEffortOptions,
    effortDisabled: routeEffortDisabled,
    hasSessionScopedRoute,
    agentTeamDisabled,
    executionProfileDisabled,
    providerDisabled: routeProviderDisabled,
    modelDisabled: routeModelDisabled,
    settingsLoading: routeSettingsLoading,
    sessionEffectiveMain,
    newChatEffectiveMain,
    modelDiscoveryError,
    summaryLabel,
    updateEffort,
    ...routeFields
  } = routeState;
  const [modeRefreshing] = useState(false);

  const available = filterAvailableProviders(
    runtime.llmEngines,
    runtime.llmDeployment,
    (item) => item.provider,
    (item) => item,
  );
  const visible = filterVisibleProviders(
    available,
    runtime.llmCatalog?.provider_visibility,
    [],
    (item) => item.provider,
  );
  const llmStatus =
    runtime.llmStatus ??
    (runtime.llmEngines.length > 0 || runtime.currentLlm
      ? "ready"
      : runtime.isConnected
        ? "loading"
        : "error");
  const runtimeDisplayRoute = resolveRuntimeDisplayRoute(runtime);
  const llmDataPending = llmStatus === "loading" || runtime.llmRefreshing === true;
  const currentEngine: RuntimeLlmEngine | null = runtimeDisplayRoute
    ? {
        provider: runtimeDisplayRoute.provider,
        model: runtimeDisplayRoute.model,
        label: runtimeDisplayRoute.model,
      }
    : null;
  // エンジン一覧の一時的な取得失敗中も、last-known-goodの現在Modelを選択欄に
  // 残す。候補一覧が復旧すれば通常の候補へ自然に置き換わる。
  // Never reinsert a hidden/stale current engine into the selector.  The
  // persisted route remains executable (visibility is not authorization),
  // while the UI asks the user to choose a currently available option.
  const selectableModels = visible;
  const deploymentProvider = resolveEffectiveProviderId(runtime.llmDeployment);
  const deploymentModel = resolveEffectiveModelId(runtime.llmDeployment);
  const effectiveLabel = [deploymentProvider, deploymentModel]
    .filter(Boolean)
    .join(" / ");
  const runtimeDefaultProvider =
    deploymentProvider ||
    runtime.effectiveLlm?.provider?.trim() ||
    runtime.currentLlm?.provider?.trim() ||
    "";
  const runtimeDefaultModel =
    deploymentModel ||
    runtime.effectiveLlm?.model?.trim() ||
    runtime.currentLlm?.model?.trim() ||
    "";
  const runtimeDefaultLabel =
    runtimeDefaultProvider && runtimeDefaultModel
      ? formatRouteLabel(runtimeDefaultProvider, runtimeDefaultModel)
      : "";
  // A session's effective route is authoritative for that conversation.  The
  // deployment metadata is the runtime default/constraint, not the route that
  // will be used for a session with an explicit effective route.  Keep these
  // labels separate so a Qwen session is not reported as the global Gemma
  // default in Chat settings.
  const sessionEffectiveProvider =
    sessionEffectiveMain?.provider?.trim() || sessionProvider;
  const sessionEffectiveModel =
    sessionEffectiveMain?.model?.trim() || sessionModel;
  const sessionEffectiveLabel =
    sessionEffectiveProvider && sessionEffectiveModel
      ? formatRouteLabel(sessionEffectiveProvider, sessionEffectiveModel)
      : "";
  const hasSessionEffectiveRoute = Boolean(
    sessionId && hasSessionScopedRoute && sessionEffectiveLabel,
  );
  const newChatEffectiveLabel = !sessionId &&
    ((hasSessionScopedRoute && sessionProvider && sessionModel) ||
      (newChatEffectiveMain?.provider && newChatEffectiveMain?.model))
    ? formatRouteLabel(
        newChatEffectiveMain?.provider?.trim() || sessionProvider,
        newChatEffectiveMain?.model?.trim() || sessionModel,
      )
    : "";
  const hasAuthoritativeRoute = Boolean(
    (hasSessionEffectiveRoute && sessionEffectiveLabel) || newChatEffectiveLabel,
  );
  const authoritativeRouteLabel = hasSessionEffectiveRoute
    ? sessionEffectiveLabel
    : newChatEffectiveLabel;
  const runtimeDefaultDiffers = Boolean(
    hasAuthoritativeRoute &&
      runtimeDefaultLabel &&
      runtimeDefaultLabel !== authoritativeRouteLabel,
  );
  const fixedDeployment =
    runtime.llmDeployment?.fixed === true &&
    Boolean(deploymentProvider && deploymentModel);
  const selectedRouteProvider = fixedDeployment
    ? deploymentProvider || ""
    : sessionProvider || runtimeDisplayRoute?.provider || "";
  const selectedRouteModel = fixedDeployment
    ? deploymentModel || ""
    : sessionModel || runtimeDisplayRoute?.model || "";
  const hasUsableLlmSnapshot = Boolean(
    runtime.llmEngines.length > 0 ||
      runtime.currentLlm ||
      runtime.llmCatalog?.providers?.some((provider) => {
        const models = Array.isArray(provider.chat_models)
          ? provider.chat_models
          : provider.models ?? [];
        return models.length > 0;
      }) ||
      (sessionProvider && sessionModel) ||
      (deploymentProvider && deploymentModel),
  );
  // Health is a connectivity hint, not a reason to strand controls when a
  // usable catalog/engine/session snapshot is already on screen.
  const healthUnavailable = !runtime.isConnected && !hasUsableLlmSnapshot;
  const modelBusy = runtime.llmChanging || modeRefreshing || healthUnavailable;
  const selectedEngine = [
    ...runtime.llmEngines,
    ...(currentEngine ? [currentEngine] : []),
  ].find(
    (engine) =>
      engine.provider.trim().toLowerCase() === selectedRouteProvider.trim().toLowerCase() &&
      engine.model === selectedRouteModel,
  );
  const selectedCatalogProvider = routeFields.catalogProviders?.find(
    (provider) =>
      provider.id.trim().toLowerCase() === selectedRouteProvider.trim().toLowerCase(),
  );
  const selectedCatalogModels = Array.isArray(selectedCatalogProvider?.chat_models)
    ? selectedCatalogProvider?.chat_models ?? []
    : selectedCatalogProvider &&
        !STRICT_RUNTIME_PROVIDERS.has(selectedCatalogProvider.id.trim().toLowerCase())
      ? selectedCatalogProvider.models ?? []
      : [];
  const selectedCatalogModel = selectedCatalogModels.find(
    (model) => model.id === selectedRouteModel,
  );
  const localRuntimeStatus = useLocalRuntimeStatus({
    provider: selectedRouteProvider,
    model: selectedRouteModel,
    runtime: selectedCatalogModel?.runtime,
    enabled: selectedRouteProvider.trim().toLowerCase() === "openai_compatible_local",
  });
  const localRuntimeReadiness = localRuntimeStatus.readiness;
  const effortUnsupported =
    selectedEngine?.supports_reasoning === false ||
    selectedCatalogModel?.supports_reasoning === false;
  const modeDataPending = llmModeLoading || modeRefreshing;
  const modeDataError = Boolean(llmModeError);
  const catalogEffortOptions = effortUnsupported
    ? []
    : sessionEffortOptions.length
      ? sessionEffortOptions
      : selectedEngine?.reasoning_effort_options ?? [];
  const effectiveOptions = effortUnsupported
    ? []
    : catalogEffortOptions.length
      ? catalogEffortOptions
      : llmModeOptions.length
        ? llmModeOptions
        : llmMode
          ? [llmMode]
          : [];
  const effectiveMode = effortUnsupported
    ? ""
    : effectiveEffort && effectiveOptions.includes(effectiveEffort)
      ? effectiveEffort
      : llmMode && effectiveOptions.includes(llmMode)
        ? llmMode
        : (effectiveOptions[0] ?? "");
  const modeLabel = agentBound
      ? "次の依頼から反映"
      : effortUnsupported
        ? "推論モード指定なし"
        : modeRefreshing
          ? "Syncing…"
          : effectiveMode
            ? formatLlmModeLabel(effectiveMode, llmModeLabels)
            : "—";
  const current = resolveMainContextSnapshot(contextSnapshot);
  const percentage = resolveContextPercentage(current);
  const displayScopeKey = `${userScopeKey ?? ""}:${sessionId ?? "__new_chat__"}`;
  const displayRouteLabel =
    summaryLabel ||
    (sessionProvider && sessionModel
      ? formatRouteLabel(sessionProvider, sessionModel)
      : "") ||
    (runtimeDisplayRoute
      ? formatRouteLabel(runtimeDisplayRoute.provider, runtimeDisplayRoute.model)
      : "");
  // `llmRefreshing` covers catalog/character/features refreshes as well as
  // the engine endpoint.  It must not turn a known route back into a loading
  // placeholder.  Track whether this composer has ever had a display route so
  // the loading copy is reserved for the genuinely empty first load.
  const [lastKnownDisplayRoute, setLastKnownDisplayRoute] = useState<{
    scopeKey: string;
    label: string;
  } | null>(() =>
    displayRouteLabel
      ? { scopeKey: displayScopeKey, label: displayRouteLabel }
      : null,
  );
  useEffect(() => {
    // While a session/user route request is in flight, summaryLabel may still
    // be the previous scope's value for one render.  Do not promote that
    // transient value into the new scope's last-known display snapshot.
    if (!displayRouteLabel || routeSettingsLoading) return;
    setLastKnownDisplayRoute((previous) =>
      previous?.scopeKey === displayScopeKey &&
      previous.label === displayRouteLabel
        ? previous
        : { scopeKey: displayScopeKey, label: displayRouteLabel },
    );
  }, [displayRouteLabel, displayScopeKey, routeSettingsLoading]);
  const lastKnownDisplayRouteLabelForScope =
    lastKnownDisplayRoute?.scopeKey === displayScopeKey
      ? lastKnownDisplayRoute.label
      : "";
  const lastKnownDisplayRouteLabel =
    !displayRouteLabel &&
    (llmStatus === "loading" || runtime.llmRefreshing === true)
      ? lastKnownDisplayRouteLabelForScope
      : "";
  const resolvedDisplayRouteLabel =
    displayRouteLabel || lastKnownDisplayRouteLabel;
  const showInitialModelLoading =
    !resolvedDisplayRouteLabel &&
    !lastKnownDisplayRouteLabelForScope &&
    llmStatus === "loading";
  const modelLabel = agentBound
      ? "Agent設定"
      : fixedDeployment
        ? effectiveLabel || "Enterprise"
        : resolvedDisplayRouteLabel
          ? resolvedDisplayRouteLabel
          : showInitialModelLoading
            ? "Model loading…"
            : llmStatus === "error"
              ? "Model unavailable · retry"
              : "No model available";
  const effortDisabled =
    routeEffortDisabled ||
    healthUnavailable ||
    fixedDeployment ||
    modeRefreshing ||
    (!hasSessionScopedRoute && !onLlmModeChange);
  const providerDisabled = routeProviderDisabled || modelBusy;
  const modelDisabled = routeModelDisabled || modelBusy;
  const characterStatus =
    runtime.characterStatus ??
    (runtime.characters.length > 0
      ? "ready"
      : runtime.llmRefreshing || runtime.llmStatus === "loading"
        ? "loading"
        : runtime.llmStatus === "error"
          ? "error"
          : "ready");
  const characterMetadataLoading =
    runtime.characters.length === 0 &&
    (characterStatus === "loading" || runtime.characterRefreshing === true);
  const characterMetadataError = characterStatus === "error";
  const characterMetadataStale = characterStatus === "stale";
  // Keep the picker mounted while character metadata is in flight/failed so
  // users receive an explicit status and retry action instead of a silently
  // disappearing control.  A plain empty/ready snapshot remains hidden for
  // backwards compatibility with runtimes that do not expose characters.
  const characterControlVisible =
    runtime.characters.length > 0 ||
    characterMetadataLoading ||
    characterMetadataError ||
    (characterMetadataStale && runtime.characters.length === 0) ||
    characterMetadataStale;
  const characterOptions = runtime.characters.length > 0
    ? runtime.characters
    : runtime.currentCharacter
      ? [{ slug: runtime.currentCharacter, name: runtime.currentCharacter }]
      : [];
  const characterDisabled =
    characterMetadataLoading ||
    characterMetadataError ||
    runtime.characterChanging === true ||
    (!runtime.isConnected &&
      !(
        (runtime.characterStatus === "ready" ||
          runtime.characterStatus === "stale") &&
        runtime.characters.length > 0
      ));
  const currentCharacter = runtime.characters.find(
    (character) => character.slug === runtime.currentCharacter,
  );
  const characterLabel = currentCharacter
    ? characterOptionLabel(currentCharacter, runtime.characters)
    : runtime.currentCharacter || "—";
  const contextLabel = percentage == null ? "—" : `${Math.round(percentage)}%`;
  const retryCharacters = runtime.refreshCharacters ?? runtime.refreshLlm;

  // Chat settings has two explicit keyboard layers. `outerSelection` tracks
  // the settings field selected by the user, while `innerOpen` is the one
  // AppSelect popup currently owned by that field. Never infer either state
  // from document.activeElement: Base UI moves focus through a portal when a
  // popup opens and restores it asynchronously when it closes.
  const settingsItems = useMemo<ChatSettingsItemState[]>(
    () => [
      ...(!agentBound && !fixedDeployment &&
      (routeFields.catalogProviders.length > 0 || selectableModels.length > 0)
        ? [
            { id: "provider" as const, disabled: providerDisabled },
            { id: "model" as const, disabled: modelDisabled },
          ]
        : []),
      ...(!agentBound && effectiveOptions.length > 0
        ? [
            {
              id: "effort" as const,
              disabled: effortDisabled,
            },
          ]
        : []),
      ...(!agentBound
        ? [
            { id: "agentTeam" as const, disabled: agentTeamDisabled },
            { id: "executionProfile" as const, disabled: executionProfileDisabled },
          ]
        : []),
      ...(characterControlVisible
        ? [
            {
              id: "character" as const,
              disabled: characterDisabled,
            },
          ]
        : []),
    ],
    [
      agentBound,
      agentTeamDisabled,
      characterControlVisible,
      characterDisabled,
      effectiveOptions.length,
      effortDisabled,
      executionProfileDisabled,
      fixedDeployment,
      modelDisabled,
      providerDisabled,
      routeFields.catalogProviders.length,
      selectableModels.length,
    ],
  );
  const visibleSettingsItems = useMemo(
    () => settingsItems.map((item) => item.id),
    [settingsItems],
  );
  const enabledSettingsItems = useMemo(
    () => settingsItems.filter((item) => !item.disabled).map((item) => item.id),
    [settingsItems],
  );
  const [outerSelection, setOuterSelection] = useState<ChatSettingsItem | null>(
    null,
  );
  const [innerOpen, setInnerOpen] = useState<ChatSettingsItem | null>(null);
  const settingsContentRef = useRef<HTMLDivElement>(null);
  const settingsFallbackRef = useRef<HTMLDivElement>(null);
  const selectPortalHostRef = useRef<HTMLDivElement>(null);
  const focusGenerationRef = useRef(0);
  const focusFrameRef = useRef<number | null>(null);
  const openStateRef = useRef(open);
  const outerSelectionStateRef = useRef<ChatSettingsItem | null>(
    outerSelection,
  );
  const innerOpenStateRef = useRef(innerOpen);
  const skipFocusRestoreRef = useRef(false);
  const initialFocusPendingRef = useRef(false);
  // Keep these refs in sync during render so a queued rAF sees the latest
  // state even when React has not flushed the corresponding effect yet.
  // eslint-disable-next-line react-hooks/refs -- intentional synchronous mirror for queued UI work
  openStateRef.current = open;
  // eslint-disable-next-line react-hooks/refs -- intentional synchronous mirror for queued UI work
  outerSelectionStateRef.current = outerSelection;
  // eslint-disable-next-line react-hooks/refs -- intentional synchronous mirror for queued UI work
  innerOpenStateRef.current = innerOpen;

  // Keep the outer selection ref authoritative between the trigger focus event
  // and React's next render. Base UI can dispatch the next trigger's focus and
  // its previous portal's close notification in the same turn; a following
  // Arrow key must observe that new trigger immediately.
  const updateOuterSelection = useCallback(
    (
      next:
        | ChatSettingsItem
        | null
        | ((current: ChatSettingsItem | null) => ChatSettingsItem | null),
    ) => {
      const resolved =
        typeof next === "function"
          ? next(outerSelectionStateRef.current)
          : next;
      outerSelectionStateRef.current = resolved;
      setOuterSelection(resolved);
    },
    [],
  );

  const focusComposer = useCallback(() => {
    requestAnimationFrame(() => onComposerFocusRequest?.());
  }, [onComposerFocusRequest]);

  const invalidateSettingsFocus = useCallback(() => {
    focusGenerationRef.current += 1;
    if (focusFrameRef.current !== null) {
      cancelAnimationFrame(focusFrameRef.current);
      focusFrameRef.current = null;
    }
  }, []);

  const focusSettingsTrigger = useCallback(
    (item: ChatSettingsItem | null) => {
      invalidateSettingsFocus();
      const generation = focusGenerationRef.current;
      const canApply = () =>
        generation === focusGenerationRef.current &&
        openStateRef.current &&
        innerOpenStateRef.current === null;
      const focus = () => {
        if (!canApply()) return null;
        if (!item) return false;
        const trigger =
          settingsContentRef.current?.querySelector<HTMLButtonElement>(
            `[data-chat-settings-item="${item}"]:not(:disabled)`,
          );
        if (!trigger || trigger.disabled) return false;
        trigger.focus({ preventScroll: true });
        return true;
      };

      const needsFocusRecovery = () => {
        if (typeof document === "undefined") return true;
        const active = document.activeElement;
        // A portal close can leave focus on body (or on a node that has just
        // been detached). Those are the only cases in which the deferred
        // retry is allowed to restore the settings trigger.
        if (
          !active ||
          active === document.body ||
          active === document.documentElement
        ) {
          return true;
        }
        if (!(active instanceof HTMLElement) || !active.isConnected)
          return true;
        // A Select option can remain mounted in its portal for one turn after
        // the controlled inner layer has closed. It is not a user-owned outer
        // target anymore (canApply already proves innerOpen is null), so let
        // the retry restore the outer trigger instead of leaving focus in the
        // stale popup.
        if (active.getAttribute("role") === "option") return true;
        return (
          "disabled" in active &&
          Boolean((active as HTMLButtonElement).disabled)
        );
      };

      const focused = focus();
      // Even when the trigger exists synchronously, Base UI may unmount the
      // previous Select portal later in the same turn and leave focus on a
      // detached option node. Always verify once in the next frame while the
      // generation/open/inner guards are still current.
      if (focused === null) return;
      // Popover/Select portals may finish mounting one frame after the state
      // transition. This is a focus retry, not a key suppression flag: a later
      // ArrowUp/ArrowDown is always handled from the explicit states above.
      focusFrameRef.current = requestAnimationFrame(() => {
        focusFrameRef.current = null;
        if (!canApply()) return;
        if (!needsFocusRecovery()) return;
        const focused = focus();
        if (focused === false) {
          settingsFallbackRef.current?.focus({ preventScroll: true });
        }
      });
    },
    [invalidateSettingsFocus],
  );

  const focusOuterItem = useCallback(
    (preferred: ChatSettingsItem | null) => {
      const next =
        preferred && enabledSettingsItems.includes(preferred)
          ? preferred
          : (enabledSettingsItems[0] ?? null);
      updateOuterSelection(next ?? visibleSettingsItems[0] ?? null);
      focusSettingsTrigger(next);
    },
    [
      enabledSettingsItems,
      focusSettingsTrigger,
      updateOuterSelection,
      visibleSettingsItems,
    ],
  );

  const openInnerSelectViaKeyboard = useCallback(
    (item: ChatSettingsItem, trigger: HTMLButtonElement) => {
      const itemState = settingsItems.find(
        (candidate) => candidate.id === item,
      );
      if (!itemState || itemState.disabled) return;
      // ArrowRight is not a Base UI open key. Deliver an Enter-equivalent
      // keyboard open so keyRef / openMethod stay on the keyboard path.
      // Do not set controlled `open` here and do not synthesize a pointer
      // interaction — a mouse click would leave later Arrows inert.
      trigger.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Enter",
          code: "Enter",
          bubbles: true,
          cancelable: true,
        }),
      );
      trigger.click();
    },
    [settingsItems],
  );

  const handleInnerOpenChange = useCallback(
    (
      item: ChatSettingsItem,
      nextOpen: boolean,
      details?: AppSelectOpenChangeDetails,
    ) => {
      if (nextOpen) {
        const itemState = settingsItems.find(
          (candidate) => candidate.id === item,
        );
        if (!itemState || itemState.disabled) return;
        invalidateSettingsFocus();
        innerOpenStateRef.current = item;
        skipFocusRestoreRef.current = false;
        updateOuterSelection(item);
        setInnerOpen(item);
        return;
      }

      // A stale close notification from another Select must not close or
      // refocus anything while a different Select owns the inner layer.
      if (innerOpenStateRef.current !== item) return;
      innerOpenStateRef.current = null;
      invalidateSettingsFocus();
      setInnerOpen(null);
      updateOuterSelection((current) => current ?? item);

      const reason = details?.reason;
      if (reason === "focus-out" || reason === "outside-press") {
        // Base UI has already selected the next focus target for these
        // reasons. Only close our state; never pull focus back to the trigger.
        // Base UI dispatches the focus event before this notification, so
        // synchronize the explicit outer selection from the already-focused
        // enabled trigger. This keeps an immediate ArrowUp/ArrowDown after a
        // forward Tab deterministic even though the React innerOpen state is
        // still being committed.
        const focusedTrigger =
          typeof document !== "undefined"
            ? document.activeElement?.closest<HTMLElement>(
                "[data-chat-settings-item]",
              )
            : null;
        const focusedItem = focusedTrigger?.dataset.chatSettingsItem as
          | ChatSettingsItem
          | undefined;
        if (focusedItem && enabledSettingsItems.includes(focusedItem)) {
          updateOuterSelection(focusedItem);
        } else {
          updateOuterSelection((current) => current ?? item);
        }
        skipFocusRestoreRef.current = true;
        return;
      }
      focusOuterItem(item);
    },
    [
      focusOuterItem,
      enabledSettingsItems,
      invalidateSettingsFocus,
      settingsItems,
      updateOuterSelection,
    ],
  );

  const handleInnerKeyDownCapture = useCallback(
    (item: ChatSettingsItem, event: ReactKeyboardEvent<HTMLDivElement>) => {
      if (innerOpenStateRef.current !== item || event.key !== "ArrowLeft") {
        return;
      }
      if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) {
        return;
      }

      // ArrowLeft is the explicit inner-to-outer transition. Do not let the
      // Base UI list navigation treat it as an item movement/selection.
      event.preventDefault();
      event.stopPropagation();
      invalidateSettingsFocus();
      innerOpenStateRef.current = null;
      skipFocusRestoreRef.current = false;
      setInnerOpen((current) => (current === item ? null : current));
      updateOuterSelection(item);
      focusOuterItem(item);
    },
    [focusOuterItem, invalidateSettingsFocus, updateOuterSelection],
  );

  const handleOuterTriggerFocus = useCallback(
    (item: ChatSettingsItem) => {
      // Base UI focuses the next trigger before dispatching the Select
      // focus-out close notification. Treat that managed trigger focus as an
      // explicit outer transition immediately, so a following Arrow key does
      // not observe the stale innerOpen React state. The eventual stale close
      // callback is ignored by the ref guard in handleInnerOpenChange.
      if (
        innerOpenStateRef.current !== null &&
        innerOpenStateRef.current !== item
      ) {
        invalidateSettingsFocus();
        innerOpenStateRef.current = null;
        skipFocusRestoreRef.current = true;
        setInnerOpen(null);
      }
      updateOuterSelection(item);
    },
    [invalidateSettingsFocus, updateOuterSelection],
  );

  const handleOuterKeyDownCapture = useCallback(
    (item: ChatSettingsItem, event: ReactKeyboardEvent<HTMLButtonElement>) => {
      if (
        innerOpenStateRef.current !== null ||
        outerSelectionStateRef.current !== item
      ) {
        return;
      }
      if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) {
        return;
      }

      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        event.stopPropagation();
        const currentIndex = enabledSettingsItems.indexOf(item);
        const direction = event.key === "ArrowDown" ? 1 : -1;
        const next = enabledSettingsItems[currentIndex + direction];
        focusOuterItem(next ?? item);
        return;
      }

      if (event.key === "Enter") {
        // Do not preventDefault or set controlled open here. Base UI's
        // trigger/useClick bubble path must see the real Enter so it can
        // record keyboard modality and focus the selected option.
        return;
      }

      if (event.key === "ArrowRight") {
        event.preventDefault();
        event.stopPropagation();
        openInnerSelectViaKeyboard(item, event.currentTarget);
      }
    },
    [enabledSettingsItems, focusOuterItem, openInnerSelectViaKeyboard],
  );

  useEffect(
    () => () => {
      // Do not let a retry from an unmounted settings portal focus a node in a
      // different chat/composer after navigation.
      invalidateSettingsFocus();
    },
    [invalidateSettingsFocus],
  );

  useEffect(() => {
    if (!open) {
      invalidateSettingsFocus();
      innerOpenStateRef.current = null;
      skipFocusRestoreRef.current = false;
      initialFocusPendingRef.current = false;
      if (outerSelection !== null) updateOuterSelection(null);
      if (innerOpen !== null) setInnerOpen(null);
      return;
    }
    if (routeSettingsLoading) {
      return;
    }
    const firstItem = enabledSettingsItems[0] ?? visibleSettingsItems[0];
    const firstTrigger = firstItem
      ? settingsContentRef.current?.querySelector<HTMLButtonElement>(
          `[data-chat-settings-item="${firstItem}"]`,
        )
      : null;
    if (firstTrigger?.disabled) {
      return;
    }
    if (innerOpen !== null) {
      const currentItem = settingsItems.find((item) => item.id === innerOpen);
      const trigger =
        settingsContentRef.current?.querySelector<HTMLButtonElement>(
          `[data-chat-settings-item="${innerOpen}"]`,
        );
      const staleInner =
        !currentItem || currentItem.disabled || !trigger || trigger.disabled;
      if (staleInner) {
        const next = enabledSettingsItems[0] ?? null;
        innerOpenStateRef.current = null;
        invalidateSettingsFocus();
        skipFocusRestoreRef.current = false;
        setInnerOpen(null);
        updateOuterSelection(next ?? visibleSettingsItems[0] ?? null);
        focusOuterItem(next);
      }
      return;
    }
    if (skipFocusRestoreRef.current) {
      skipFocusRestoreRef.current = false;
      return;
    }

    const next = initialFocusPendingRef.current
      ? (enabledSettingsItems[0] ?? visibleSettingsItems[0] ?? null)
      : outerSelection && enabledSettingsItems.includes(outerSelection)
        ? outerSelection
        : (enabledSettingsItems[0] ?? visibleSettingsItems[0] ?? null);
    if (initialFocusPendingRef.current) {
      initialFocusPendingRef.current = false;
    }
    if (next !== outerSelection) updateOuterSelection(next);
    focusSettingsTrigger(
      next && enabledSettingsItems.includes(next) ? next : null,
    );
  }, [
    enabledSettingsItems,
    focusSettingsTrigger,
    focusOuterItem,
    invalidateSettingsFocus,
    innerOpen,
    open,
    outerSelection,
    routeSettingsLoading,
    settingsItems,
    updateOuterSelection,
    visibleSettingsItems,
  ]);


  const handleModeChange = useCallback(
    (event: AppSelectChangeEvent) => {
      const nextMode = event.target.value as LlmMode;
      if (!nextMode || nextMode === effectiveMode) return;
      const shouldPersistEffortToRoute = sessionId
        ? hasSessionScopedRoute
        : Boolean(sessionProvider && sessionModel);
      if (shouldPersistEffortToRoute) {
        void updateEffort(nextMode);
        return;
      }
      onLlmModeChange?.(nextMode);
    },
    [
      effectiveMode,
      hasSessionScopedRoute,
      onLlmModeChange,
      sessionId,
      sessionModel,
      sessionProvider,
      updateEffort,
    ],
  );

  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) {
          // Invalidate any queued trigger-focus retry before the parent closes
          // the Popover. Base UI may unmount the portal before React flushes
          // the state update, so keep the refs authoritative in that window.
          openStateRef.current = false;
          innerOpenStateRef.current = null;
          skipFocusRestoreRef.current = false;
          initialFocusPendingRef.current = false;
          invalidateSettingsFocus();
          setInnerOpen(null);
          updateOuterSelection(null);
        } else {
          initialFocusPendingRef.current = true;
        }
        onOpenChange(nextOpen);
        if (!nextOpen) focusComposer();
      }}
    >
      <PopoverTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            className={cn(
              // 共通Buttonの固定h-8を解除して二段分だけ伸縮させつつ、
              // 横幅は既存と同じコンパクトな上限に収める。
              "h-auto min-h-8 min-w-0 max-w-[min(16rem,100%)] shrink-0 justify-start gap-1 overflow-hidden px-2 py-1 text-left",
            )}
            data-testid="chat-model-control"
            data-runtime-model-control="true"
            data-context-window-current="main"
            aria-label={
              agentBound
                ? "Agent設定・キャラクター・コンテキスト設定"
                : "モデル・推論・キャラクター・コンテキスト設定"
            }
            title={`${modelLabel} · ${modeLabel} · ${characterLabel} · Context ${contextLabel}`}
          />
        }
      >
        <span className="grid size-5 shrink-0 place-items-center rounded-full bg-muted">
          {effectiveMode ? (
            <LlmModeIcon mode={effectiveMode} className="size-3.5" />
          ) : (
            <Gauge className="size-3.5" />
          )}
        </span>
        <span
          className="min-w-0 max-w-full"
          data-model-control-summary="true"
        >
          <span
            className="flex min-w-0 max-w-full items-center gap-1"
            data-model-control-primary="true"
          >
            <span className="min-w-0 truncate text-xs font-medium">
              {modelLabel}
            </span>
            {localRuntimeReadiness && (
              <span
                className={cn(
                  "shrink-0 text-[10px]",
                  localRuntimeReadiness.state === "ready"
                    ? "text-emerald-600 dark:text-emerald-400"
                    : localRuntimeReadiness.state === "failed"
                      ? "text-destructive"
                      : "text-muted-foreground",
                )}
                data-testid="chat-local-runtime-readiness-summary"
                title={localRuntimeReadiness.detail ?? undefined}
              >
                · {localRuntimeReadiness.label}
              </span>
            )}
            <span className="shrink-0 text-[10px] text-muted-foreground">
              · {modeLabel}
            </span>
          </span>
          {characterControlVisible && (
            <span
              className="block max-w-full truncate text-[9px] text-muted-foreground/75"
              data-model-control-character="true"
              title={`Character: ${characterLabel}`}
            >
              {characterLabel}
            </span>
          )}
        </span>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        sideOffset={8}
        className="w-[min(30rem,calc(100vw-1rem))] overflow-visible p-3"
      >
        <div
          ref={settingsContentRef}
          className="max-h-[min(75vh,42rem)] space-y-3 overflow-y-auto"
        >
          <div
            ref={settingsFallbackRef}
            tabIndex={-1}
            data-chat-settings-focus-fallback="true"
            className="sr-only"
            aria-label="Chat settings focus fallback"
          />
          <div className="flex items-center justify-between gap-2 border-b pb-2">
            <div className="min-w-0">
              <div className="text-sm font-medium">Chat settings</div>
              <div className="truncate text-[10px] text-muted-foreground">
                {agentBound
                  ? "Agent設定 · Character"
                  : "provider · model · effort · team · execution profile · character"}
              </div>
            </div>
            {runtime.llmChangeError && (
              <span role="alert" className="max-w-48 text-right text-[10px] text-destructive">
                {runtime.llmChangeError}
              </span>
            )}
          </div>

          {agentBound ? (
            <div
              data-testid="agent-bound-settings-summary"
              className="space-y-2 rounded-md border border-primary/30 bg-primary/5 px-2.5 py-2 text-xs"
            >
              <p className="font-medium">このChatはAgentに紐づいています</p>
              <p className="text-muted-foreground">
                職務・モデル・Execution Team・ProfileはAgent管理が正本です。変更は次の依頼から反映されます。
              </p>
              {normalizedAgentId ? (
                <Link
                  className="inline-block text-primary underline-offset-2 hover:underline"
                  href={`/operations?tab=agents&agent=${encodeURIComponent(normalizedAgentId)}`}
                >
                  Agent管理を開く →
                </Link>
              ) : null}
            </div>
          ) : (
            <>
          {fixedDeployment ? (
            <div
              className="rounded-md border border-amber-500/50 bg-amber-50 px-2.5 py-2 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200"
              title={effectiveLabel || "Enterprise deployment"}
            >
              <div className="font-medium">Enterprise deployment</div>
              <div className="truncate">{effectiveLabel || "Fixed model"}</div>
            </div>
          ) : null}

          {!fixedDeployment &&
            runtime.llmCatalog != null &&
            !llmDataPending &&
            !modelDiscoveryError &&
            selectedRouteProvider &&
            selectedRouteModel &&
            !selectedCatalogModel && (
              <p
                role="alert"
                className="rounded-md border border-amber-500/50 bg-amber-50 px-2.5 py-2 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200"
              >
                現在のモデルはruntimeで利用できません。利用可能なモデルを再選択してください。
              </p>
            )}
          {!fixedDeployment && modelDiscoveryError && (
            <div
              role="alert"
              className="flex items-center justify-between gap-2 rounded-md border border-amber-500/50 bg-amber-50 px-2.5 py-2 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200"
            >
              <span>
                {modelDiscoveryError}。前回利用可能だったモデルを表示しています。
              </span>
              {runtime.refreshLlm && (
                <button
                  type="button"
                  className="shrink-0 text-primary underline-offset-2 hover:underline"
                  onClick={() => void runtime.refreshLlm?.()}
                >
                  再取得
                </button>
              )}
            </div>
          )}

          {localRuntimeReadiness && (
            <div
              role={localRuntimeStatus.error ? "alert" : "status"}
              aria-live="polite"
              data-testid="chat-local-runtime-readiness"
              className={cn(
                "flex items-start justify-between gap-2 rounded-md border px-2.5 py-2 text-xs",
                localRuntimeReadiness.state === "ready"
                  ? "border-emerald-500/40 bg-emerald-50 text-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-200"
                  : localRuntimeReadiness.state === "failed" || localRuntimeStatus.error
                    ? "border-destructive/40 bg-destructive/5 text-destructive"
                    : "border-border-subtle bg-muted/30 text-muted-foreground",
              )}
            >
              <span className="min-w-0">
                <span className="font-medium">ローカルモデル: {localRuntimeReadiness.label}</span>
                {localRuntimeReadiness.detail && (
                  <span className="mt-0.5 block break-words text-[10px] opacity-80">
                    {localRuntimeReadiness.detail}
                  </span>
                )}
              </span>
              <button
                type="button"
                className="shrink-0 text-primary underline-offset-2 hover:underline"
                onClick={() => void localRuntimeStatus.refresh()}
                disabled={localRuntimeStatus.loading}
              >
                再取得
              </button>
            </div>
          )}

          <ChatModelSettingsFields
            runtime={runtime}
            userSettings={userSettings}
            fixedDeployment={fixedDeployment}
            modelBusy={modelBusy}
            route={{
              agentTeamOptions: routeFields.agentTeamOptions,
              agentTeamSelectorValue: routeFields.agentTeamSelectorValue,
              agentTeamDisabled,
              executionProfileId: routeFields.executionProfileId,
              executionProfileOptions: routeFields.executionProfileOptions,
              executionProfileDisabled,
              catalogProviders: routeFields.catalogProviders,
              effectiveProvider: sessionProvider,
              effectiveModel: sessionModel,
              modelOptions: routeFields.modelOptions,
              providerDisabled: routeProviderDisabled,
              // `useChatSessionRoute` owns model availability.  Do not add a
              // second catalog-null lockout here: an engine LKG/served
              // alternative can keep the model selector usable while the
              // richer catalog is transiently unavailable.
              modelDisabled: routeModelDisabled,
              settingsLoading: routeSettingsLoading,
              updateAgentTeamValue: routeFields.updateAgentTeamValue,
              updateExecutionProfile: routeFields.updateExecutionProfile,
              updateProvider: routeFields.updateProvider,
              updateModel: routeFields.updateModel,
            }}
            innerOpen={innerOpen}
            settingsItemProps={(id) => ({
              onFocus: () => handleOuterTriggerFocus(id),
              onKeyDownCapture: (event) =>
                handleOuterKeyDownCapture(
                  id,
                  event as unknown as ReactKeyboardEvent<HTMLButtonElement>,
                ),
              onContentKeyDownCapture: (event) =>
                handleInnerKeyDownCapture(
                  id,
                  event as unknown as ReactKeyboardEvent<HTMLDivElement>,
                ),
              open: innerOpen === id,
              onOpenChange: (nextOpen, details) =>
                handleInnerOpenChange(id, nextOpen, details),
              container: selectPortalHostRef,
            })}
            effortSlot={
              (effortUnsupported ||
                effectiveOptions.length > 0 ||
                llmDataPending ||
                llmStatus === "error" ||
                modeDataPending ||
                modeDataError) ? (
                <label className="grid gap-1 text-xs text-muted-foreground">
                  <span>Effort / Reasoning / LLM mode</span>
                  {effortUnsupported ? (
                    <div
                      data-testid="chat-effort-unsupported"
                      aria-disabled="true"
                      className={cn(
                        runtimeSelectClassName,
                        "flex w-full items-center text-muted-foreground",
                      )}
                    >
                      推論モード指定なし
                    </div>
                  ) : effectiveOptions.length > 0 ? (
                    <AppSelect
                      aria-label="推論・LLMモード"
                      data-testid="chat-effort-selector"
                      data-chat-settings-item="effort"
                      value={effectiveMode}
                      onChange={handleModeChange}
                      onFocus={() => handleOuterTriggerFocus("effort")}
                      onKeyDownCapture={(event) =>
                        handleOuterKeyDownCapture("effort", event)
                      }
                      onContentKeyDownCapture={(event) =>
                        handleInnerKeyDownCapture("effort", event)
                      }
                      open={innerOpen === "effort"}
                      onOpenChange={(nextOpen, details) =>
                        handleInnerOpenChange("effort", nextOpen, details)
                      }
                      container={selectPortalHostRef}
                      disabled={effortDisabled}
                      className={cn(runtimeSelectClassName, "w-full")}
                      contentClassName="max-w-[min(28rem,calc(100vw-2rem))]"
                    >
                      {effectiveOptions.map((mode) => (
                        <option key={mode} value={mode}>
                          {formatLlmModeLabel(mode, llmModeLabels)}
                        </option>
                      ))}
                    </AppSelect>
                  ) : (
                    <div className="flex items-center justify-between gap-2 rounded-md border border-dashed px-2 py-1.5 text-[10px] text-muted-foreground">
                      <span>
                        {llmDataPending || modeDataPending
                          ? "Effort情報を読み込み中…"
                          : "Effort情報を取得できません"}
                      </span>
                      {(runtime.refreshLlm || onLlmModeRefresh) && (
                        <button
                          type="button"
                          className="text-primary underline-offset-2 hover:underline"
                          onClick={() => {
                            if (modeDataError || modeDataPending) {
                              void onLlmModeRefresh?.();
                            } else {
                              void runtime.refreshLlm?.();
                            }
                          }}
                        >
                          再取得
                        </button>
                      )}
                    </div>
                  )}
                  <span className="text-[10px] text-muted-foreground">
                    {effortUnsupported
                      ? "このモデルは推論effortの外部指定に対応していません。"
                      : effectiveMode
                        ? getLlmModeDescription(effectiveMode)
                        : "Effort options are not available yet"}
                  </span>
                </label>
              ) : null
            }
          />

          {!fixedDeployment && selectableModels.length === 0 && llmDataPending ? (
            <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
              <span>Model情報を読み込み中…</span>
              {runtime.refreshLlm && (
                <button
                  type="button"
                  className="text-primary underline-offset-2 hover:underline"
                  onClick={() => void runtime.refreshLlm?.()}
                >
                  再取得
                </button>
              )}
            </div>
          ) :
            !fixedDeployment &&
            selectableModels.length === 0 &&
            (llmStatus === "error" || llmStatus === "stale") ? (
            <div className="flex items-center justify-between gap-2 text-xs text-destructive">
              <span title={runtime.llmError ?? undefined}>Model情報を取得できません</span>
              {runtime.refreshLlm && (
                <button
                  type="button"
                  className="text-primary underline-offset-2 hover:underline"
                  onClick={() => void runtime.refreshLlm?.()}
                >
                  再取得
                </button>
              )}
            </div>
          ) : null}

          {hasSessionEffectiveRoute ? (
            <div
              className="truncate text-[10px] text-muted-foreground"
              title={sessionEffectiveLabel}
            >
              Session effective: {sessionEffectiveLabel}
            </div>
          ) : !sessionId && newChatEffectiveLabel ? (
            <div
              className="truncate text-[10px] text-muted-foreground"
              title={newChatEffectiveLabel}
            >
              New-chat effective: {newChatEffectiveLabel}
            </div>
          ) : !fixedDeployment &&
            !sessionId &&
            hasDeploymentMetadata(runtime.llmDeployment) &&
            effectiveLabel ? (
            <div
              className="truncate text-[10px] text-muted-foreground"
              title={effectiveLabel}
            >
              Effective: {effectiveLabel}
            </div>
          ) : null}
          {runtimeDefaultDiffers && (
            <div
              className="truncate text-[10px] text-muted-foreground"
              title={runtimeDefaultLabel}
            >
              Runtime default: {runtimeDefaultLabel}
            </div>
          )}

            </>
          )}

          {characterControlVisible && (
            <label className="grid gap-1 text-xs text-muted-foreground">
              <span>Character</span>
              {characterMetadataLoading && runtime.characters.length === 0 && (
                <span role="status" className="text-[10px] text-muted-foreground">
                  Character情報を読み込み中…
                </span>
              )}
              {characterMetadataError && runtime.characters.length === 0 && (
                <div className="flex items-center justify-between gap-2 text-[10px] text-destructive">
                  <span role="alert" title={runtime.characterError ?? undefined}>
                    Character情報を取得できません
                  </span>
                  {retryCharacters && (
                    <button
                      type="button"
                      className="text-primary underline-offset-2 hover:underline"
                      onClick={() => void retryCharacters()}
                    >
                      再取得
                    </button>
                  )}
                </div>
              )}
              {characterMetadataStale && (
                <div className="flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
                  <span role="status" title={runtime.characterError ?? undefined}>
                    {runtime.characters.length > 0
                      ? "Character情報の更新に失敗しました。前回の一覧を表示しています"
                      : "Character情報が古いため再取得が必要です"}
                  </span>
                  {retryCharacters && (
                    <button
                      type="button"
                      className="text-primary underline-offset-2 hover:underline"
                      onClick={() => void retryCharacters()}
                    >
                      再取得
                    </button>
                  )}
                </div>
              )}
              <AppSelect
                aria-label="キャラクター"
                data-chat-settings-item="character"
                value={runtime.currentCharacter}
                onChange={(event) =>
                  void runtime.changeCharacter(event.target.value, sessionId)
                }
                onFocus={() => handleOuterTriggerFocus("character")}
                onKeyDownCapture={(event) =>
                  handleOuterKeyDownCapture("character", event)
                }
                onContentKeyDownCapture={(event) =>
                  handleInnerKeyDownCapture("character", event)
                }
                open={innerOpen === "character"}
                onOpenChange={(nextOpen, details) =>
                  handleInnerOpenChange("character", nextOpen, details)
                }
                container={selectPortalHostRef}
                className={cn(runtimeSelectClassName, "w-full")}
                contentClassName="max-w-[min(28rem,calc(100vw-2rem))]"
                showSelectedIndicator={false}
                disabled={characterDisabled}
              >
                {characterOptions.map((character) => (
                  <option key={character.slug} value={character.slug}>
                    {characterOptionLabel(character, characterOptions)}
                  </option>
                ))}
                {characterOptions.length === 0 && (
                  <option value="" disabled>
                    {characterMetadataLoading ? "読み込み中…" : "利用可能なCharacterなし"}
                  </option>
                )}
              </AppSelect>
            </label>
          )}

          <div className="border-t pt-3">
            <ContextWindowDetails
              snapshot={contextSnapshot}
              status={contextSnapshotStatus}
            />
          </div>
        </div>
        <div
          ref={selectPortalHostRef}
          data-chat-settings-select-portal="true"
        />
      </PopoverContent>
    </Popover>
  );
}

type SkillSlashCommand = {
  command: string;
  description: string;
  usage: string;
};

type SlashMenuItem =
  | {
      kind: "chat";
      command: ChatCommandDefinition;
    }
  | {
      kind: "skill";
      command: SkillSlashCommand;
    };

type SkillApiItem = {
  name: string;
  description?: string;
  trigger_mode?: string;
};

async function fetchSkillSlashCommands(
  projectId?: string | null,
): Promise<SkillSlashCommand[]> {
  const searchParams = new URLSearchParams();
  if (projectId) searchParams.set("project_id", projectId);
  const query = searchParams.toString();
  const res = await fetch(
    `/api/python-proxy/skills${query ? `?${query}` : ""}`,
    {
      credentials: "include",
    },
  );
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
  const data: { skills?: SkillApiItem[] } = await res.json();
  return filterVisibleChatSkillCommands(
    (data.skills ?? [])
      // AUTO は LLM 自動判断専用なのでスラッシュ候補から除外する
      .filter((skill) => skill.trigger_mode !== "auto")
      .filter((skill) => !isHiddenChatSkillName(skill.name))
      .map((skill) => {
        // Keep existing display/casing for ordinary skills while stripping a
        // defensive leading slash from legacy API rows.
        const skillName = String(skill.name ?? "")
          .trim()
          .replace(/^\/+/, "");
        return {
          command: `/${skillName}`,
          description: skill.description || "スキル",
          usage: `/${skillName} [入力]`,
        };
      }),
  );
}

function isImageFile(file: File) {
  return file.type.startsWith("image/");
}

function ComposerAttachmentPreview({
  file,
  onRemove,
}: {
  file: File;
  onRemove: () => void;
}) {
  const previewUrl = useMemo(
    () => (isImageFile(file) ? URL.createObjectURL(file) : null),
    [file],
  );

  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  return (
    <div className="group relative flex max-w-full items-center gap-2 rounded-md border border-border-subtle bg-surface-slate p-2 pr-8 text-sm text-on-surface">
      {previewUrl ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={previewUrl}
          alt={file.name}
          className="size-12 shrink-0 rounded object-cover"
        />
      ) : (
        <div className="flex size-10 shrink-0 items-center justify-center rounded bg-surface-container-low text-text-secondary">
          <FileText className="size-4" />
        </div>
      )}
      <div className="min-w-0">
        <div className="max-w-[220px] truncate text-xs font-medium">
          {file.name}
        </div>
        <div className="text-xs text-muted-foreground">
          {formatBytes(file.size)}
        </div>
      </div>
      <button
        type="button"
        onClick={onRemove}
        className="absolute right-1.5 top-1.5 flex size-5 items-center justify-center rounded-full text-muted-foreground hover:bg-destructive hover:text-destructive-foreground"
        aria-label={`${file.name} を削除`}
      >
        <X className="size-3" />
      </button>
    </div>
  );
}

const LLM_MODE_DESCRIPTIONS: Record<string, string> = {
  fast: "Quick replies with lightweight reasoning.",
  thinking: "Deeper reasoning for harder prompts.",
  always: "Always thinking; this model has no non-thinking mode.",
  none: "No extra reasoning effort.",
  minimal: "Minimal reasoning effort.",
  low: "Low reasoning effort.",
  medium: "Balanced reasoning effort.",
  high: "High reasoning effort.",
  xhigh: "Very high reasoning effort.",
  max: "Maximum reasoning effort.",
};

function formatLlmModeLabel(
  mode: LlmMode,
  labels: Record<string, string>,
): string {
  const label = labels[mode]?.trim() || (mode === "always" ? "Always thinking" : "");
  if (label) return label;
  if (!mode) return "Unknown";
  return mode
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function getLlmModeDescription(mode: LlmMode): string {
  return LLM_MODE_DESCRIPTIONS[mode] ?? "Use this response mode.";
}

function isLightweightLlmMode(mode: LlmMode): boolean {
  return mode === "fast" || mode === "none" || mode === "minimal";
}

function LlmModeIcon({
  mode,
  className,
}: {
  mode: LlmMode;
  className?: string;
}) {
  if (mode === "fast") return <Zap className={className} />;
  if (isLightweightLlmMode(mode)) return <Gauge className={className} />;
  return <Brain className={className} />;
}

export function ChatComposer({
  onSend,
  onSteer,
  onStop,
  disabled,
  busy = false,
  busySubmitBehavior = "queue",
  generationTerminalKey = null,
  attachedFiles,
  onAttachedFilesChange,
  onAttachmentsPromoted,
  projectContextEnabled = false,
  onProjectContextToggle,
  deepResearchEnabled = false,
  onDeepResearchToggle,
  llmMode,
  llmModeOptions = [],
  llmModeLabels = {},
  llmModeLoading = false,
  llmModeError = null,
  onLlmModeChange,
  projectId,
  projectScopeReady = true,
  sessionId,
  agentId,
  contextSnapshot,
  contextSnapshotStatus,
  appContext = null,
  onAppContextChange,
  ensureLiveVoiceConversationSession,
  voiceAssistantMessage = null,
  presentation = "normal",
  draftKey,
}: ChatComposerProps) {
  const isAoiDotsPresentation = presentation === "aoidots";
  const runtime = useOptionalRuntimeContext();
  const { mutate: mutateSWR } = useSWRConfig();
  const refreshLlmMode = useCallback(async () => {
    await mutateSWR("chat/llm-mode");
  }, [mutateSWR]);
  // AppLayout の getSession() で確定した ID を使い、認証状態の再取得待ちで
  // 新規チャット下書きが別スコープへ移る競合を避ける。
  const draftUserId = useCurrentUserId();
  const composerDraftKey =
    draftKey ?? sessionId ?? NEW_CHAT_COMPOSER_DRAFT_KEY;
  const composerDraft = useSyncExternalStore(
    (listener) =>
      subscribeChatComposerDraft(composerDraftKey, listener, draftUserId),
    () => getChatComposerDraft(composerDraftKey, draftUserId),
    () => EMPTY_CHAT_COMPOSER_DRAFT,
  );
  const updateComposerDraft = useCallback(
    (
      updater: (
        current: typeof EMPTY_CHAT_COMPOSER_DRAFT,
      ) => typeof EMPTY_CHAT_COMPOSER_DRAFT,
    ) => updateChatComposerDraft(composerDraftKey, updater, draftUserId),
    [composerDraftKey, draftUserId],
  );
  const setValue = useCallback<Dispatch<SetStateAction<string>>>(
    (next) =>
      updateComposerDraft((current) => {
        const content = typeof next === "function" ? next(current.content) : next;
        return {
          ...current,
          content,
          ...(content !== current.content
            ? { clientMessageId: undefined, dispatchMode: undefined }
            : {}),
        };
      }),
    [updateComposerDraft],
  );
  const setMentions = useCallback<Dispatch<SetStateAction<MentionItem[]>>>(
    (next) =>
      updateComposerDraft((current) => {
        const mentions = typeof next === "function" ? next(current.mentions) : next;
        const changed =
          mentions.length !== current.mentions.length ||
          mentions.some((mention, index) => {
            const previous = current.mentions[index];
            return (
              mention.type !== previous?.type ||
              mention.id !== previous?.id ||
              mention.name !== previous?.name ||
              mention.detail !== previous?.detail
            );
          });
        return {
          ...current,
          mentions,
          ...(changed
            ? { clientMessageId: undefined, dispatchMode: undefined }
            : {}),
        };
      }),
    [updateComposerDraft],
  );
  const setActiveCommand = useCallback<
    Dispatch<SetStateAction<ActiveChatCommand | null>>
  >(
    (next) =>
      updateComposerDraft((current) => {
        const activeCommand =
          typeof next === "function" ? next(current.activeCommand) : next;
        const changed = activeCommand?.command !== current.activeCommand?.command;
        return {
          ...current,
          activeCommand,
          ...(changed
            ? { clientMessageId: undefined, dispatchMode: undefined }
            : {}),
        };
      }),
    [updateComposerDraft],
  );
  const setToolFreeMode = useCallback<Dispatch<SetStateAction<boolean>>>(
    (next) =>
      updateComposerDraft((current) => {
        const toolFreeMode =
          typeof next === "function" ? next(current.toolFreeMode) : next;
        return {
          ...current,
          toolFreeMode,
          ...(toolFreeMode !== current.toolFreeMode
            ? { clientMessageId: undefined, dispatchMode: undefined }
            : {}),
        };
      }),
    [updateComposerDraft],
  );
  const setDeepResearchMode = useCallback(
    (enabled: boolean) => {
      const dispatchMode: ChatComposerDispatchMode = enabled
        ? "deep_research"
        : "chat";
      updateComposerDraft((current) => {
        const currentMode = current.dispatchMode ??
          (deepResearchEnabled ? "deep_research" : "chat");
        return {
          ...current,
          dispatchMode,
          ...(currentMode !== dispatchMode
            ? { clientMessageId: undefined }
            : {}),
        };
      });
      onDeepResearchToggle?.(enabled);
    },
    [deepResearchEnabled, onDeepResearchToggle, updateComposerDraft],
  );
  const prepareComposerDispatch = useCallback(
    (
      submittedDraft: ChatComposerDraft | undefined,
      commandCapabilities?: ChatCommandCapability[],
    ): {
      draft: ChatComposerDraft | undefined;
      dispatchMode: ChatComposerDispatchMode;
    } => {
      const dispatchMode = commandCapabilities?.length
        ? "chat"
        : submittedDraft?.dispatchMode ??
          (submittedDraft?.clientMessageId
            ? "chat"
            : deepResearchEnabled
              ? "deep_research"
              : "chat");
      if (!submittedDraft || dispatchMode !== "deep_research") {
        return { draft: submittedDraft, dispatchMode };
      }

      const preparedDraft: ChatComposerDraft = {
        ...submittedDraft,
        clientMessageId: submittedDraft.clientMessageId &&
          UUID_CLIENT_MESSAGE_ID.test(submittedDraft.clientMessageId)
          ? submittedDraft.clientMessageId
          : createComposerClientMessageId(),
        dispatchMode,
      };
      updateComposerDraft((current) =>
        draftsMatch(current, submittedDraft) ? preparedDraft : current,
      );
      // The server may accept a request while its response is lost. Flush the
      // stable request key and route before invoking the async send handler.
      flushChatComposerDraftStore();
      return { draft: preparedDraft, dispatchMode };
    },
    [deepResearchEnabled, updateComposerDraft],
  );
  const [intentConfirmation, setIntentConfirmation] = useState({ userId: draftUserId, enabled: false });
  const intentConfirmationEnabled = intentConfirmation.userId === draftUserId && intentConfirmation.enabled;
  if (intentConfirmation.userId !== draftUserId) {
    setIntentConfirmation({ userId: draftUserId, enabled: false });
  }
  const setIntentConfirmationEnabled = (enabled: boolean) => setIntentConfirmation({ userId: draftUserId, enabled });
  const intentSubmissionRef = useRef(false);
  const restoredAttachmentMetadataRef = useRef<ChatSendAttachmentMetadata[] | null>(null);
  const restoredAttachmentBaselineRef = useRef<ChatSendAttachmentMetadata[] | null>(null);
  const latestIntentScopeRef = useRef({ key: composerDraftKey, userId: draftUserId, projectId, attachedFiles });
  useLayoutEffect(() => {
    latestIntentScopeRef.current = { key: composerDraftKey, userId: draftUserId, projectId, attachedFiles };
  }, [composerDraftKey, draftUserId, projectId, attachedFiles]);
  useEffect(() => {
    const expected = restoredAttachmentMetadataRef.current;
    if (!expected) return;
    const currentMetadata = attachedFiles.map((file) => ({
      name: file.name,
      size: file.size,
      type: file.type || "",
      lastModified: file.lastModified,
    }));
    const baseline = restoredAttachmentBaselineRef.current;
    if (
      baseline &&
      baseline.length === currentMetadata.length &&
      baseline.every(
        (attachment, index) =>
          attachment.name === currentMetadata[index]?.name &&
          attachment.size === currentMetadata[index]?.size &&
          attachment.type === currentMetadata[index]?.type &&
          attachment.lastModified === currentMetadata[index]?.lastModified,
      )
    ) {
      return;
    }
    restoredAttachmentBaselineRef.current = null;
    const matches =
      expected.length === attachedFiles.length &&
      expected.every(
        (attachment, index) => {
          const file = attachedFiles[index];
          return (
            file?.name === attachment.name &&
            file.size === attachment.size &&
            (file.type || "") === attachment.type &&
            file.lastModified === attachment.lastModified
          );
        },
      );
    if (!matches) {
      updateComposerDraft(
        (current) =>
          current.clientMessageId
            ? {
                ...current,
                clientMessageId: undefined,
                dispatchMode: undefined,
              }
            : current,
      );
      restoredAttachmentMetadataRef.current = null;
    }
  }, [attachedFiles, updateComposerDraft]);
  const value = composerDraft.content;
  const mentions = composerDraft.mentions;
  const activeCommand = composerDraft.activeCommand;
  const toolFreeMode = composerDraft.toolFreeMode;
  const [messageQueue, setMessageQueue] = useState<QueuedChatMessage[]>([]);
  const lastTerminalKeyRef = useRef(generationTerminalKey);
  const completionPendingRef = useRef(false);
  const prevSessionIdRef = useRef(sessionId ?? null);
  const previousDraftKeyRef = useRef(composerDraftKey);
  const promotedDraftRef = useRef<{
    sourceKey: string;
    targetKey: string;
    userId?: string | null;
    draft: ChatComposerDraft;
  } | null>(null);
  const [showSlashMenu, setShowSlashMenu] = useState(false);
  const [slashSelectionIndex, setSlashSelectionIndex] = useState(0);
  const [skillCommands, setSkillCommands] = useState<SkillSlashCommand[]>([]);
  const skillsFetchedRef = useRef(false);
  const skillsFetchedKeyRef = useRef<string | null>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const [showMentionMenu, setShowMentionMenu] = useState(false);
  const [mentionQuery, setMentionQuery] = useState("");
  // Keep the first render deterministic for SSR hydration.  Reading
  // localStorage in a state initializer lets the browser choose a different
  // profile than the server (for example, autonomous_work vs chat), which
  // changes GenerationProfileSelector's title/icon before hydration.  Promote
  // the persisted value only after the client has mounted.
  const [localGenerationProfile, setLocalGenerationProfile] =
    useState<GenerationProfile>(DEFAULT_GENERATION_PROFILE);
  const [generationProfileHydrated, setGenerationProfileHydrated] =
    useState(false);
  const [generationProfileChangedByUser, setGenerationProfileChangedByUser] =
    useState(false);
  const [generationProfileMenuOpen, setGenerationProfileMenuOpen] =
    useState(false);
  const [localPlanningPolicy, setLocalPlanningPolicy] =
    useState<PlanningPolicy>(DEFAULT_PLANNING_POLICY);
  const [planningPolicyHydrated, setPlanningPolicyHydrated] = useState(false);
  const [planningPolicyMenuOpen, setPlanningPolicyMenuOpen] = useState(false);
  const [modelControlOpen, setModelControlOpen] = useState(false);
  const [toolsMenuOpen, setToolsMenuOpen] = useState(false);
  const [appContextPickerOpen, setAppContextPickerOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const slashMenuRef = useRef<HTMLDivElement>(null);
  const toolsMenuButtonRef = useRef<HTMLButtonElement>(null);
  const skipToolsMenuRefocusRef = useRef(false);
  const setComposerInputRef = useCallback(
    (element: HTMLTextAreaElement | null) => {
      // ChatComposerInput の先頭（通常テキスト）textareaだけを唯一の
      // composer参照として扱う。mount/unmount の双方で同期し、古い
      // textareaへショートカットやフォーカスを送り続けないようにする。
      textareaRef.current = element;
    },
    [],
  );
  const shouldHandleComposerShortcuts = useCallback(
    (element: HTMLTextAreaElement) =>
      !isChatComposerCursorInCodeBlock(element.value, element.selectionStart),
    [],
  );
  useMarkdownShortcuts(textareaRef, {
    shouldHandle: shouldHandleComposerShortcuts,
  });
  const { snippets } = useSnippets();
  const { settings: userSettings, patch: patchUserSettings } =
    useUserSettings();
  const {
    state: snippetState,
    dismiss: dismissSnippetAutocomplete,
  } = useSnippetAutocomplete(
    textareaRef,
    snippets,
    { shouldHandle: shouldHandleComposerShortcuts },
  );
  const handleComposerCursorContextChange = useCallback(
    (_cursor: number, isCodeBlock: boolean) => {
      if (!isCodeBlock) return;
      setShowSlashMenu(false);
      setSlashSelectionIndex(0);
      setShowMentionMenu(false);
      setMentionQuery("");
      dismissSnippetAutocomplete();
    },
    [dismissSnippetAutocomplete],
  );
  const settingsGenerationProfile = useMemo(
    () => getSettingsGenerationProfile(userSettings),
    [userSettings],
  );
  const generationProfile =
    !generationProfileHydrated
      ? DEFAULT_GENERATION_PROFILE
      : generationProfileChangedByUser || !settingsGenerationProfile
        ? localGenerationProfile
        : settingsGenerationProfile;

  useEffect(() => {
    setLocalGenerationProfile(loadStoredGenerationProfile(safeLocalStorage));
    setGenerationProfileHydrated(true);
    setLocalPlanningPolicy(loadStoredPlanningPolicy(safeLocalStorage));
    setPlanningPolicyHydrated(true);
  }, []);


  const isSteeringMode = busy;
  const isEmpty =
    value.trim().length === 0 && !isSteeringMode && attachedFiles.length === 0;
  const isEditingSlashCommand = value.startsWith("/") && !/\s/.test(value);
  const slashQuery = isEditingSlashCommand ? value.trim() : "";
  const filteredChatCommands = useMemo(
    () => filterChatCommands(slashQuery),
    [slashQuery],
  );
  const visibleSkillCommands = useMemo(
    () => filterVisibleChatSkillCommands(skillCommands),
    [skillCommands],
  );
  const filteredSkillCommands = useMemo(() => {
    const normalized = slashQuery.trim().toLowerCase();
    if (!normalized || normalized === "/") return visibleSkillCommands;
    return visibleSkillCommands.filter((item) =>
      item.command.toLowerCase().startsWith(normalized),
    );
  }, [slashQuery, visibleSkillCommands]);
  const slashMenuItems = useMemo<SlashMenuItem[]>(
    () => [
      ...filteredChatCommands.map((command) => ({
        kind: "chat" as const,
        command,
      })),
      ...filteredSkillCommands.map((command) => ({
        kind: "skill" as const,
        command,
      })),
    ],
    [filteredChatCommands, filteredSkillCommands],
  );
  const selectedSlashMenuIndex =
    slashMenuItems.length > 0
      ? Math.min(slashSelectionIndex, slashMenuItems.length - 1)
      : -1;
  const composerPlaceholder = activeCommand
    ? `${activeCommand.label}で実行する内容を入力...`
    : deepResearchEnabled
      ? "Deep Researchする質問を入力..."
      : "メッセージを入力... (/ でコマンド、@ でメンション)";
  const searchCommand = useMemo(() => {
    const command = findChatCommand("/search");
    return command?.kind === "capability" ? command : null;
  }, []);
  const webSearchActive = activeCommand?.capability === "web_search";
  const toolsMenuActive =
    intentConfirmationEnabled ||
    projectContextEnabled ||
    deepResearchEnabled ||
    webSearchActive ||
    toolFreeMode ||
    Boolean(appContext);

  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!draftUserId) return;
    hydrateChatComposerDraft(composerDraftKey, draftUserId);
    gcChatComposerDrafts(draftUserId);
  }, [composerDraftKey, draftUserId]);

  // A failed send whose File bytes were not recoverable from IndexedDB can
  // still restore its text and stable client id. The user can reselect the
  // displayed attachment(s) before submitting again; never synthesize a File
  // from metadata alone.
  useEffect(() => {
    const handleRestore = (event: Event) => {
      const detail = (event as CustomEvent<ChatSendRestoreEventDetail>).detail;
      if (!detail || detail.userId !== draftUserId) return;
      const targetKey = detail.sessionId.startsWith("__new_chat_optimistic__:")
        ? NEW_CHAT_COMPOSER_DRAFT_KEY
        : detail.sessionId;
      if (targetKey !== composerDraftKey) return;
      const current = getChatComposerDraft(composerDraftKey, draftUserId);
      if (
        current.content.length > 0 &&
        current.clientMessageId !== detail.clientMessageId
      ) {
        return;
      }
      updateChatComposerDraft(
        composerDraftKey,
        () => ({
          ...current,
          content: detail.content,
          mentions: [...detail.mentions],
          clientMessageId: detail.clientMessageId,
          dispatchMode: "chat",
        }),
        draftUserId,
      );
      restoredAttachmentMetadataRef.current = [...detail.attachments];
      restoredAttachmentBaselineRef.current = attachedFiles.map((file) => ({
        name: file.name,
        size: file.size,
        type: file.type || "",
        lastModified: file.lastModified,
      }));
      if (detail.attachments.length > 0) {
        toast.info("元の添付ファイルを再選択してから送信してください");
      }
      requestAnimationFrame(() => textareaRef.current?.focus());
    };
    window.addEventListener(CHAT_SEND_RESTORE_EVENT, handleRestore);
    return () => window.removeEventListener(CHAT_SEND_RESTORE_EVENT, handleRestore);
  }, [attachedFiles, composerDraftKey, draftUserId, updateComposerDraft]);

  useEffect(() => {
    const previousDraftKey = previousDraftKeyRef.current;
    const authorizedPromotion = promotedDraftRef.current;
    // A blank Chat route can be opened beside any existing conversation. Only
    // the send path that received onSessionPromoted may move that draft to the
    // created session; ordinary navigation (for example an AoiDots worker
    // detail link) must leave __new_chat__ untouched.
    if (
      previousDraftKey === NEW_CHAT_COMPOSER_DRAFT_KEY &&
      composerDraftKey !== NEW_CHAT_COMPOSER_DRAFT_KEY &&
      authorizedPromotion?.sourceKey === previousDraftKey &&
      authorizedPromotion.targetKey === composerDraftKey &&
      authorizedPromotion.userId === draftUserId
    ) {
      const sourceDraft = getChatComposerDraft(
        NEW_CHAT_COMPOSER_DRAFT_KEY,
        draftUserId,
      );
      promoteNewChatComposerDraft(composerDraftKey, draftUserId);
      if (!draftsMatch(sourceDraft, EMPTY_CHAT_COMPOSER_DRAFT)) {
        promotedDraftRef.current = {
          sourceKey: NEW_CHAT_COMPOSER_DRAFT_KEY,
          targetKey: composerDraftKey,
          userId: draftUserId,
          draft: sourceDraft,
        };
      }
    }
    previousDraftKeyRef.current = composerDraftKey;
  }, [composerDraftKey, draftUserId]);

  useEffect(() => {
    if (!sessionId) return;
    const draft = takeChatDraftHandoff(safeSessionStorage, sessionId);
    if (!draft) return;

    setValue(draft.content);
    if (draft.generationProfile) {
      setLocalGenerationProfile(draft.generationProfile);
      setGenerationProfileChangedByUser(true);
    }
    requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.focus();
      textarea.style.height = "auto";
      const lineHeight = 24;
      const maxHeight = lineHeight * 5;
      textarea.style.height = `${Math.min(textarea.scrollHeight, maxHeight)}px`;
    });
  }, [sessionId, setValue]);

  useEffect(() => {
    if (!settingsGenerationProfile) return;
    saveStoredGenerationProfile(safeLocalStorage, settingsGenerationProfile);
  }, [settingsGenerationProfile]);

  const handleGenerationProfileChange = useCallback(
    (nextProfile: GenerationProfile) => {
      setGenerationProfileChangedByUser(true);
      setLocalGenerationProfile(nextProfile);
      saveStoredGenerationProfile(safeLocalStorage, nextProfile);
      void patchUserSettings({
        chat: {
          generation_profile: nextProfile,
        },
      }).catch((err) => {
        console.warn("生成プロファイルの保存に失敗:", err);
      });
    },
    [patchUserSettings],
  );

  const handlePlanningPolicyChange = useCallback((nextPolicy: PlanningPolicy) => {
    setLocalPlanningPolicy(nextPolicy);
    saveStoredPlanningPolicy(safeLocalStorage, nextPolicy);
    void patchUserSettings({
      chat: {
        planning_policy: nextPolicy,
      },
    }).catch((err) => {
      console.warn("計画ポリシーの保存に失敗:", err);
    });
  }, [patchUserSettings]);

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      const action = getChatComposerShortcutAction(event);
      if (!action) return;

      event.preventDefault();
      if (action === "project_context") {
        onProjectContextToggle?.(!projectContextEnabled);
        return;
      }
      if (action === "generation_profile_menu") {
        setGenerationProfileMenuOpen(true);
        return;
      }
      if (action === "llm_mode_menu") {
        setModelControlOpen(true);
        return;
      }
      if (action === "tools_menu") {
        if (!disabled && !isSteeringMode) setToolsMenuOpen(true);
        return;
      }

      if (webSearchActive) {
        setActiveCommand(null);
        toast.success("Web検索を解除しました");
      } else if (searchCommand) {
        setActiveCommand(searchCommand);
        toast.success("Web検索を次の送信に適用します");
      } else {
        toast.error("Web検索コマンドが見つかりません");
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [
    onProjectContextToggle,
    projectContextEnabled,
    searchCommand,
    disabled,
    isSteeringMode,
    webSearchActive,
    setActiveCommand,
  ]);

  // テキストエリアの高さ自動調整
  const adjustHeight = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    const lineHeight = 24;
    const maxHeight = lineHeight * 5;
    textarea.style.height = `${Math.min(textarea.scrollHeight, maxHeight)}px`;
  }, []);

  useEffect(() => {
    adjustHeight();
  }, [value, adjustHeight]);

  // WebSocket切断や送信失敗による busy=false は生成完了ではない。
  // サーバーが完了を確認した明示シグナルを受けた時だけ、キュー先頭を送る。
  useEffect(() => {
    if (
      generationTerminalKey &&
      lastTerminalKeyRef.current !== generationTerminalKey
    ) {
      lastTerminalKeyRef.current = generationTerminalKey;
      completionPendingRef.current = true;
    }
    if (!busy && completionPendingRef.current) {
      completionPendingRef.current = false;
      if (messageQueue.length === 0) return;
      const [next, ...rest] = messageQueue;
      // セッション切り替えと同一レンダーで busy が false になった場合、
      // 旧セッションのキューを現行セッションへ誤送信しないようガードする。
      // 不一致なら送らず、後続のクリア effect に破棄させる。
      if (next.sessionId !== (sessionId ?? null)) return;
      setMessageQueue(rest);
      const args = [
        next.content,
        next.attachedFiles.length > 0 ? next.attachedFiles : undefined,
        next.mentions.length ? next.mentions : undefined,
        next.generationProfile,
        next.capabilities.length ? next.capabilities : undefined,
        next.toolsRequired,
      ] as const;
      let result: ReturnType<ChatComposerProps["onSend"]>;
      try {
        result = next.appContext
          ? onSend(...args, next.appContext)
          : onSend(...args);
      } catch {
        result = false;
      }
      void Promise.resolve(result)
        .then((accepted) => {
          if (
            accepted !== false &&
            accepted !== "failed" &&
            accepted !== "pending"
          ) {
            // enqueue 時点で composer から切り離しているが、controlled
            // parent が遅延している場合に備え、当該 queue item の分だけ
            // 条件付きで掃除する。送信後に追加された新しい添付は残す。
            onAttachedFilesChange((current) =>
              removeQueuedAttachments(current, next.attachedFiles),
            );
            // キュー専用スコープは送信成功を確認してから削除する。
            clearChatComposerDraft(next.draftStorageKey, draftUserId);
            return;
          }
          // キューから取り出した直後に dispatch が失敗/保留になっても、
          // 新しい入力が始まっていない場合は元のdraftを再表示する。
          const current = getChatComposerDraft(
            next.draftRestoreKey,
            draftUserId,
          );
          const hasNewDraft =
            current.content.length > 0 ||
            current.mentions.length > 0 ||
            current.activeCommand !== null ||
            current.toolFreeMode;
          const isCurrentSession = next.sessionId === (sessionId ?? null);
          if (hasNewDraft && isCurrentSession) {
            // 別入力を上書きせず、失敗したキューを先頭へ戻して再試行可能にする。
            setMessageQueue((prev) =>
              prev.some((item) => item.id === next.id) ? prev : [next, ...prev],
            );
            return;
          }
          clearChatComposerDraft(next.draftStorageKey, draftUserId);
          if (hasNewDraft) return;
          // queue item を draft として再表示する場合、File は localStorage
          // に保存できないため、メモリ上でのみ元の添付を復元する。
          onAttachedFilesChange((current) =>
            appendQueuedAttachments(current, next.attachedFiles),
          );
          updateChatComposerDraft(
            next.draftRestoreKey,
            () => next.draftSnapshot,
            draftUserId,
          );
        })
        .catch(() => {
          const current = getChatComposerDraft(
            next.draftRestoreKey,
            draftUserId,
          );
          const hasNewDraft =
            current.content.length > 0 ||
            current.mentions.length > 0 ||
            current.activeCommand !== null ||
            current.toolFreeMode;
          const isCurrentSession = next.sessionId === (sessionId ?? null);
          if (hasNewDraft && isCurrentSession) {
            setMessageQueue((prev) =>
              prev.some((item) => item.id === next.id) ? prev : [next, ...prev],
            );
            return;
          }
          clearChatComposerDraft(next.draftStorageKey, draftUserId);
          if (hasNewDraft) return;
          onAttachedFilesChange((current) =>
            appendQueuedAttachments(current, next.attachedFiles),
          );
          updateChatComposerDraft(
            next.draftRestoreKey,
            (current) =>
              current.content || current.mentions.length > 0
                ? current
                : next.draftSnapshot,
            draftUserId,
          );
        });
    }
  }, [
    busy,
    composerDraftKey,
    draftUserId,
    generationTerminalKey,
    messageQueue,
    onAttachedFilesChange,
    onSend,
    sessionId,
  ]);

  // 新規会話の初回作成(null→ID)では作成待ち中のキューを新しいIDへ引き継ぐ。
  // それ以外のセッション切り替えでは旧セッション分だけを破棄する。
  useEffect(() => {
    const currentSessionId = sessionId ?? null;
    const previousSessionId = prevSessionIdRef.current;
    prevSessionIdRef.current = currentSessionId;
    completionPendingRef.current = false;
    lastTerminalKeyRef.current = generationTerminalKey;
    setMessageQueue((prev) => {
      const keepItem = (item: QueuedChatMessage): boolean =>
        previousSessionId === null && currentSessionId !== null
          ? item.sessionId === null || item.sessionId === currentSessionId
          : item.sessionId === currentSessionId;
      for (const item of prev) {
        if (!keepItem(item)) {
          clearChatComposerDraft(item.draftStorageKey, draftUserId);
        }
      }
      if (previousSessionId === null && currentSessionId !== null) {
        return prev
          .map((item) =>
            item.sessionId === null
              ? { ...item, sessionId: currentSessionId }
              : item,
          )
          .filter((item) => item.sessionId === currentSessionId);
      }
      return prev.filter((item) => item.sessionId === currentSessionId);
    });
    // generationTerminalKey はセッション変更時点の値だけを同期する。
    // 完了通知のたびにこのeffectを動かすと、busy解除待ちの通知を消してしまう。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftUserId, sessionId]);

  // プロジェクト復元前の projectId=undefined と復元後の projectId で
  // 二重取得しないよう、スコープ確定まで待ってから一度だけ取得する。
  useEffect(() => {
    if (!projectScopeReady) return;
    const fetchKey = projectId ?? "";
    if (skillsFetchedKeyRef.current === fetchKey) return;
    skillsFetchedKeyRef.current = fetchKey;

    let cancelled = false;
    skillsFetchedRef.current = true;
    fetchSkillSlashCommands(projectId)
      .then((commands) => {
        if (!cancelled) setSkillCommands(commands);
      })
      .catch((err) => {
        console.warn("スキル一覧の取得に失敗:", err);
        if (!cancelled) {
          skillsFetchedRef.current = false;
          // 失敗したキーは記録し直さず、次の機会に再取得できるようにする。
          if (skillsFetchedKeyRef.current === fetchKey) {
            skillsFetchedKeyRef.current = null;
          }
        }
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, projectScopeReady]);

  useEffect(() => {
    if (!showSlashMenu || selectedSlashMenuIndex < 0) return;
    const el = slashMenuRef.current?.querySelector<HTMLElement>(
      `[data-slash-command-index="${selectedSlashMenuIndex}"]`,
    );
    el?.scrollIntoView({ block: "nearest" });
  }, [selectedSlashMenuIndex, showSlashMenu]);

  const clearComposerAfterSendStart = useCallback(
    (submittedDraft?: ChatComposerDraft, intentFiles?: readonly File[], intentTargetKey?: string) => {
      const currentDraft = getChatComposerDraft(composerDraftKey, draftUserId);
      // 送信開始後に同じキーへ新しい入力が入っていたら、それを消さない。
      if (!submittedDraft || draftsMatch(currentDraft, submittedDraft)) {
        clearChatComposerDraft(composerDraftKey, draftUserId);
      }

      // null セッションの下書きが送信中に新しいセッションへ promote された
      // 場合は、送信単位で記録した target だけを確認して消す。現在の
      // composerDraftKey を無条件に消すと、切替先で入力した下書きを失う。
      const promoted = promotedDraftRef.current;
      if (
        promoted &&
        promoted.userId === draftUserId &&
        (!intentFiles || promoted.targetKey === intentTargetKey) &&
        (!submittedDraft || draftsMatch(promoted.draft, submittedDraft))
      ) {
        const promotedCurrent = getChatComposerDraft(
          promoted.targetKey,
          promoted.userId,
        );
        if (!submittedDraft || draftsMatch(promotedCurrent, submittedDraft)) {
          clearChatComposerDraft(promoted.targetKey, promoted.userId);
        }
        // promote の target 保存に失敗して source が残っている場合にも、
        // 送信対象と同じ内容だけを消す。別入力が始まっていれば保持する。
        const sourceCurrent = getChatComposerDraft(
          promoted.sourceKey,
          promoted.userId,
        );
        if (
          !submittedDraft ||
          draftsMatch(sourceCurrent, submittedDraft) ||
          draftsMatch(sourceCurrent, EMPTY_CHAT_COMPOSER_DRAFT)
        ) {
          clearChatComposerDraft(promoted.sourceKey, promoted.userId);
        }
        promotedDraftRef.current = null;
      }

      if (!intentFiles) {
        onAttachedFilesChange([]);
      } else if (latestIntentScopeRef.current.userId === draftUserId &&
          (latestIntentScopeRef.current.key === composerDraftKey ||
           latestIntentScopeRef.current.key === intentTargetKey)) {
        onAttachedFilesChange((current) => removeQueuedAttachments(current, intentFiles));
      }
      requestAnimationFrame(() => {
        if (textareaRef.current) {
          textareaRef.current.style.height = "auto";
          textareaRef.current.focus();
        }
      });
    },
    [composerDraftKey, draftUserId, onAttachedFilesChange],
  );

  const invokeSend = useCallback(
    (
      submittedDraft: ChatComposerDraft | undefined,
      clearAfterAccepted: boolean,
      ...args: Parameters<ChatComposerProps["onSend"]>
    ) => {
      const preparedDispatch = prepareComposerDispatch(
        submittedDraft,
        args[4],
      );
      submittedDraft = preparedDispatch.draft;
      let restoreDraftKey = composerDraftKey;
      let promotionSourceScope: typeof latestIntentScopeRef.current | null = null;
      const sendArgs: Parameters<ChatComposerProps["onSend"]> = [...args];
      const sendOptions: ChatComposerSendOptions = { ...args[7] };
      if (submittedDraft?.clientMessageId) {
        sendOptions.clientMessageId = submittedDraft.clientMessageId;
      }
      if (
        submittedDraft?.dispatchMode ||
        preparedDispatch.dispatchMode === "deep_research"
      ) {
        sendOptions.dispatchMode =
          submittedDraft?.dispatchMode ?? preparedDispatch.dispatchMode;
      }
      if (args[7] || Object.keys(sendOptions).length > 0) {
        sendArgs[7] = sendOptions;
      }
      const isNewChatDraftSubmission =
        Boolean(
          submittedDraft &&
            clearAfterAccepted &&
            composerDraftKey === NEW_CHAT_COMPOSER_DRAFT_KEY,
        );
      if (submittedDraft && isNewChatDraftSubmission) {
        // Persist the captured input before an async create/defaults wait. The
        // live composer is cleared synchronously below; this snapshot is used
        // only by explicit legacy failure restoration.
        flushChatComposerDraftStore();
        sendArgs[7] = {
          ...sendOptions,
          ...(submittedDraft.clientMessageId
            ? { clientMessageId: submittedDraft.clientMessageId }
            : {}),
          onSessionPromoted: (createdSessionId) => {
            const currentScope = latestIntentScopeRef.current;
            const currentDraft = getChatComposerDraft(
              composerDraftKey,
              draftUserId,
            );
            if (
              currentScope.userId === draftUserId &&
              currentScope.key === composerDraftKey &&
              (draftsMatch(currentDraft, submittedDraft) ||
                draftsMatch(currentDraft, EMPTY_CHAT_COMPOSER_DRAFT))
            ) {
              restoreDraftKey = createdSessionId;
              promotionSourceScope = currentScope;
              promotedDraftRef.current = {
                sourceKey: composerDraftKey,
                targetKey: createdSessionId,
                userId: draftUserId,
                draft: submittedDraft,
              };
            }
            args[7]?.onSessionPromoted?.(createdSessionId);
          },
        };
      }
      const submittedFiles = [...(args[1] ?? [])];
      const isDeepResearchSubmission =
        preparedDispatch.dispatchMode === "deep_research";
      const clearImmediately =
        clearAfterAccepted && !isDeepResearchSubmission;
      // Clear the captured draft before invoking the async send handler. This
      // is the boundary that prevents a slow new-chat route/defaults request
      // from making the input look stuck. The callback below restores only
      // the same snapshot after an explicit failure.
      if (clearImmediately) {
        clearComposerAfterSendStart(submittedDraft);
      }
      let result: ReturnType<ChatComposerProps["onSend"]>;
      try {
        result = onSend(...sendArgs);
      } catch {
        if (clearImmediately && submittedDraft) {
          const currentDraft = getChatComposerDraft(
            restoreDraftKey,
            draftUserId,
          );
          if (draftsMatch(currentDraft, EMPTY_CHAT_COMPOSER_DRAFT)) {
            updateChatComposerDraft(
              restoreDraftKey,
              () => submittedDraft,
              draftUserId,
            );
            flushChatComposerDraftStore();
            if (submittedFiles.length > 0) {
              onAttachedFilesChange(
                (current) => appendQueuedAttachments(current, submittedFiles),
                restoreDraftKey,
              );
            }
          }
        }
        return;
      }
      const restoreAfterFailure = () => {
        if (!clearImmediately || !submittedDraft) return;
        const currentScope = latestIntentScopeRef.current;
        if (currentScope.userId !== draftUserId ||
            (currentScope.key !== restoreDraftKey && currentScope !== promotionSourceScope)) return;
        if (currentScope.key !== restoreDraftKey &&
            !draftsMatch(getChatComposerDraft(currentScope.key, draftUserId), EMPTY_CHAT_COMPOSER_DRAFT)) return;
        const currentDraft = getChatComposerDraft(restoreDraftKey, draftUserId);
        // A failed send may return after a new message has been composed.
        // Restore only into its still-visible empty draft; never overwrite it.
        if (!draftsMatch(currentDraft, EMPTY_CHAT_COMPOSER_DRAFT)) return;
        updateChatComposerDraft(restoreDraftKey, () => submittedDraft, draftUserId);
        flushChatComposerDraftStore();
        if (submittedFiles.length > 0) {
          onAttachedFilesChange((current) => appendQueuedAttachments(current, submittedFiles), restoreDraftKey);
        }
      };
      // Preserve the historical synchronous rejection contract for callers
      // that return false/failed directly. This also makes a validation or
      // preflight rejection visibly restorable in the same event turn.
      if (result === false || result === "failed") {
        restoreAfterFailure();
      }

      void Promise.resolve(result)
        .then((accepted) => {
          if (accepted === false || accepted === "failed") {
            restoreAfterFailure();
            return;
          }
          if (accepted === "retained") {
            // The optimistic bubble/outbox owns retry recovery. Keep the
            // composer empty so retrying cannot create a second client id.
            promotedDraftRef.current = null;
            return;
          }
          // pending has not settled; do not offer a duplicate retry yet.
          if (accepted === "pending") return;
          if (clearImmediately) {
            // A promoted new-chat scope temporarily contains the snapshot so
            // that a failure can restore it. Once the send is accepted, clear
            // that exact promoted scope as well.
            if (isNewChatDraftSubmission) {
              clearComposerAfterSendStart(
                submittedDraft,
                undefined,
                restoreDraftKey,
              );
            }
            return;
          }
          // Legacy void callbacks retain their successful-send behavior.
          if (clearAfterAccepted) {
            clearComposerAfterSendStart(submittedDraft);
          }
        })
        .catch(restoreAfterFailure);
    },
    [clearComposerAfterSendStart, composerDraftKey, draftUserId,
      onAttachedFilesChange, onSend, prepareComposerDispatch],
  );

  const enqueue = useCallback((candidate?: ChatIntentCandidate): boolean => {
    const text = value.trim();
    const allowsAttachmentOnlySubmission =
      activeCommand?.capability === "work_intake" && attachedFiles.length > 0;
    if (
      !text &&
      activeCommand?.capability !== "aoitalk_help" &&
      !allowsAttachmentOnlySubmission
    ) {
      return false;
    }
    const submission = resolveChatCommandSubmission(
      value,
      activeCommand,
      attachedFiles.length > 0,
    );
    if (submission.error) {
      toast.error(submission.error);
      return false;
    }
    const id = createQueueId();
    const draftSnapshot: ChatComposerDraft = {
      content: value,
      mentions: [...mentions],
      activeCommand,
      toolFreeMode,
    };
    const draftStorageKey = `${composerDraftKey}::queued:${id}`;
    // 通常の composer draft を空にしても、送信完了までは専用スコープへ
    // snapshot を保持する。失敗時は queue effect がこの snapshot を復元する。
    updateChatComposerDraft(draftStorageKey, () => draftSnapshot, draftUserId);
    setMessageQueue((prev) => [
      ...prev,
      {
        id,
        sessionId: sessionId ?? null,
        content: candidate ? appendChatIntent(submission.content, candidate) : submission.content,
        attachedFiles: [...attachedFiles],
        generationProfile,
        mentions: [...mentions],
        capabilities: submission.capabilities ?? [],
        toolsRequired: resolveChatToolsRequired(
          submission.capabilities,
          toolFreeMode,
        ),
        appContext,
        draftSnapshot,
        draftStorageKey,
        draftRestoreKey: composerDraftKey,
      },
    ]);
    // 添付はこの送信単位へ消費する。キューに移した後も composer に残すと、
    // 次の入力へ同じ screenshot/file が意図せず再添付されてしまう。
    onAttachedFilesChange([]);
    setValue("");
    setActiveCommand(null);
    setToolFreeMode(false);
    setMentions([]);
    requestAnimationFrame(() => {
      if (textareaRef.current) {
        textareaRef.current.style.height = "auto";
        textareaRef.current.focus();
      }
    });
    return true;
  }, [
    appContext,
    attachedFiles,
    composerDraftKey,
    draftUserId,
    value,
    activeCommand,
    generationProfile,
    mentions,
    onAttachedFilesChange,
    sessionId,
    toolFreeMode,
    setActiveCommand,
    setMentions,
    setToolFreeMode,
    setValue,
  ]);

  const handleIntentSend = useCallback(async (
    candidate: ChatIntentCandidate,
    onSessionPromoted: (sessionId: string) => void,
  ): Promise<boolean> => {
    if (intentSubmissionRef.current || (disabled && !isSteeringMode) || !projectScopeReady || !draftUserId || !value.trim() || /^\/masking(?:\s|$)/i.test(value.trim())) return false;
    const submission = resolveChatCommandSubmission(value, activeCommand, attachedFiles.length > 0);
    if (submission.error) { toast.error(submission.error); return false; }
    if (submission.capabilities.includes("work_intake") && attachedFiles.length > 0 && !projectId) {
      toast.error("/inbox の添付は保存先プロジェクトを選択してから送信してください");
      return false;
    }
    intentSubmissionRef.current = true;
    if (isSteeringMode) {
      try { return enqueue(candidate); }
      finally { intentSubmissionRef.current = false; }
    }
    const currentSubmittedDraft: ChatComposerDraft = {
      content: value,
      mentions: [...mentions],
      activeCommand,
      toolFreeMode,
      clientMessageId: composerDraft.clientMessageId,
      dispatchMode: composerDraft.dispatchMode,
    };
    const preparedDispatch = prepareComposerDispatch(
      currentSubmittedDraft,
      submission.capabilities,
    );
    const submittedDraft = preparedDispatch.draft ?? currentSubmittedDraft;
    const submittedFiles = [...attachedFiles];
    let intentTargetKey: string | undefined;
    const args = [appendChatIntent(submission.content, candidate), submittedFiles.length ? submittedFiles : undefined,
      mentions.length ? mentions : undefined, generationProfile, submission.capabilities,
      resolveChatToolsRequired(submission.capabilities, toolFreeMode)] as const;
    try {
      const isNewChatIntent = composerDraftKey === NEW_CHAT_COMPOSER_DRAFT_KEY && !sessionId;
      const options: ChatComposerSendOptions = {
        ...(submittedDraft.clientMessageId
          ? { clientMessageId: submittedDraft.clientMessageId }
          : {}),
        ...(submittedDraft.dispatchMode
          ? { dispatchMode: submittedDraft.dispatchMode }
          : preparedDispatch.dispatchMode === "deep_research"
            ? { dispatchMode: preparedDispatch.dispatchMode }
            : {}),
        onSessionPromoted: (createdSessionId) => {
          const currentScope = latestIntentScopeRef.current;
          const currentDraft = getChatComposerDraft(composerDraftKey, draftUserId);
          if (composerDraftKey !== NEW_CHAT_COMPOSER_DRAFT_KEY ||
              currentScope.key !== composerDraftKey || currentScope.userId !== draftUserId ||
              currentScope.projectId !== projectId ||
              currentScope.attachedFiles.length !== submittedFiles.length ||
              currentScope.attachedFiles.some((file, index) => file !== submittedFiles[index]) ||
              (!draftsMatch(currentDraft, submittedDraft) &&
                !draftsMatch(currentDraft, EMPTY_CHAT_COMPOSER_DRAFT))) return;
          onAttachmentsPromoted?.(createdSessionId, submittedFiles);
          intentTargetKey = createdSessionId;
          promotedDraftRef.current = {
            sourceKey: composerDraftKey, targetKey: createdSessionId, userId: draftUserId, draft: submittedDraft,
          };
          onSessionPromoted(createdSessionId);
        },
      };
      let sendResult: ReturnType<ChatComposerProps["onSend"]>;
      try {
        // Invoke first so the optimistic bubble is created synchronously. The
        // returned promise is observed below; it must not delay clearing the
        // captured intent snapshot.
        sendResult = onSend(...args, appContext ?? undefined, options);
      } catch {
        return false;
      }
      const locallyAccepted = sendResult !== false && sendResult !== "failed";
      if (!locallyAccepted) return false;

      // Keep source Files available for new-chat promotion: promoteAttachedFiles
      // needs the original File identities. The source draft is still cleared
      // immediately for regular sends; Deep Research keeps its durable request
      // draft until the server confirms acceptance.
      const isDeepResearchSubmission =
        preparedDispatch.dispatchMode === "deep_research";
      if (isNewChatIntent) {
        if (!isDeepResearchSubmission) {
          clearChatComposerDraft(composerDraftKey, draftUserId);
          requestAnimationFrame(() => textareaRef.current?.focus());
        }
      } else if (!isDeepResearchSubmission) {
        clearComposerAfterSendStart(submittedDraft, submittedFiles, intentTargetKey);
      }
      const restoreIntentFailure = () => {
        const currentScope = latestIntentScopeRef.current;
        const restoreDraftKey = intentTargetKey ?? composerDraftKey;
        const sameSubmittedFiles =
          currentScope.attachedFiles.length === submittedFiles.length &&
          currentScope.attachedFiles.every(
            (file, index) => file === submittedFiles[index],
          );
        if (
          currentScope.userId !== draftUserId ||
          (!intentTargetKey && currentScope.key !== composerDraftKey) ||
          !draftsMatch(
            getChatComposerDraft(restoreDraftKey, draftUserId),
            EMPTY_CHAT_COMPOSER_DRAFT,
          )
        ) return;
        updateChatComposerDraft(
          restoreDraftKey,
          () => submittedDraft,
          draftUserId,
        );
        flushChatComposerDraftStore();
        if (
          !isNewChatIntent &&
          submittedFiles.length > 0 &&
          sameSubmittedFiles &&
          currentScope.key === restoreDraftKey
        ) {
          onAttachedFilesChange((existing) => appendQueuedAttachments(existing, submittedFiles));
        }
        toast.error("送信に失敗しました。入力を戻しました。もう一度送信してください。");
      };
      try {
        const accepted = await sendResult;
        if (accepted === false || accepted === "failed") {
          restoreIntentFailure();
          return false;
        }
        if (accepted === "retained") {
          promotedDraftRef.current = null;
          if (preparedDispatch.dispatchMode === "deep_research") return true;
        }
        clearComposerAfterSendStart(submittedDraft, submittedFiles, intentTargetKey);
        return true;
      } catch {
        restoreIntentFailure();
        return false;
      }
    } finally { intentSubmissionRef.current = false; }
  }, [activeCommand, appContext, attachedFiles, clearComposerAfterSendStart,
    composerDraft.clientMessageId, composerDraft.dispatchMode, disabled, enqueue,
    generationProfile, isSteeringMode, mentions, onAttachedFilesChange, onSend,
    onAttachmentsPromoted, prepareComposerDispatch, projectId, projectScopeReady,
    composerDraftKey, draftUserId, sessionId, toolFreeMode, value]);
  const intent = useChatIntent({
    text: value,
    draftKey: composerDraftKey,
    scopeKey: JSON.stringify([draftUserId, activeCommand, mentions, toolFreeMode, appContext,
      attachedFiles.map(getIntentAttachmentId)]),
    sessionId, projectId,
    enabled: intentConfirmationEnabled && (!disabled || isSteeringMode) && projectScopeReady && Boolean(draftUserId) && !/^\/masking(?:\s|$)/i.test(value.trim()),
    onSend: handleIntentSend,
  });

  const openIntent = intent.open;
  const handleSend = useCallback(
    (options?: { steerImmediately?: boolean }) => {
      if (intentSubmissionRef.current) return;
      if (isSteeringMode && busySubmitBehavior === "block") return;
      if (isSteeringMode) {
        const text = value.trim();
        const helpCommandSelected =
          activeCommand?.capability === "aoitalk_help";
        const helpCommandTyped = isChatHelpCommand(text);
        const helpCommandActive = helpCommandSelected || helpCommandTyped;
        if (!text && !helpCommandActive) return;
        // Help is a reserved one-turn capability.  Even while another
        // generation is active, never downgrade a menu-selected Help request
        // to the steer protocol (which carries plain text only and would
        // lose the trusted Guide grounding metadata).
        if (options?.steerImmediately && !helpCommandActive) {
          if (!text) return;
          if (!onSteer) return;
          onSteer(text);
          setValue("");
          requestAnimationFrame(() => {
            if (textareaRef.current) {
              textareaRef.current.style.height = "auto";
              textareaRef.current.focus();
            }
          });
        } else if (intentConfirmationEnabled && text && !/^\/masking(?:\s|$)/i.test(text)) {
          openIntent();
        } else {
          enqueue();
        }
        return;
      }
      const submission = resolveChatCommandSubmission(
        value,
        activeCommand,
        attachedFiles.length > 0,
      );
      if (submission.error) {
        toast.error(submission.error);
        return;
      }
      if (
        submission.capabilities.includes("work_intake") &&
        attachedFiles.length > 0 &&
        !projectId
      ) {
        toast.error(
          "/inbox の添付は保存先プロジェクトを選択してから送信してください",
        );
        return;
      }
      const emptySubmissionAllowed =
        activeCommand?.capability === "work_intake" ||
        activeCommand?.capability === "aoitalk_help";
      if ((isEmpty && !emptySubmissionAllowed) || disabled) return;
      if (intentConfirmationEnabled && value.trim() && !/^\/masking(?:\s|$)/i.test(value.trim())) {
        openIntent();
        return;
      }
      const args = [
        submission.content,
        attachedFiles.length > 0 ? attachedFiles : undefined,
        mentions.length > 0 ? mentions : undefined,
        generationProfile,
        submission.capabilities,
        resolveChatToolsRequired(submission.capabilities, toolFreeMode),
      ] as const;
      const submittedDraft: ChatComposerDraft = {
        content: value,
        mentions: [...mentions],
        activeCommand,
        toolFreeMode,
        clientMessageId: composerDraft.clientMessageId,
        dispatchMode: composerDraft.dispatchMode,
      };
      if (appContext) {
        invokeSend(submittedDraft, true, ...args, appContext);
      } else {
        invokeSend(submittedDraft, true, ...args);
      }
    },
    [
      value,
      intentConfirmationEnabled,
      openIntent,
      isEmpty,
      disabled,
      isSteeringMode,
      busySubmitBehavior,
      invokeSend,
      onSteer,
      attachedFiles,
      composerDraft.clientMessageId,
      composerDraft.dispatchMode,
      mentions,
      generationProfile,
      activeCommand,
      projectId,
      enqueue,
      toolFreeMode,
      appContext,
      setValue,
    ],
  );

  const handleQuickPromptSend = useCallback(
    (rawContent: string) => {
      const content = rawContent.trim();
      if (!content || disabled || isSteeringMode || intentSubmissionRef.current) return;

      const submission = resolveChatCommandSubmission(content, null, false);
      if (submission.error) {
        toast.error(submission.error);
        return;
      }

      const args = [
        submission.content,
        undefined,
        undefined,
        generationProfile,
        submission.capabilities,
        resolveChatToolsRequired(submission.capabilities, false),
      ] as const;
      if (appContext) {
        invokeSend(undefined, false, ...args, appContext);
      } else {
        invokeSend(undefined, false, ...args);
      }

      requestAnimationFrame(() => textareaRef.current?.focus());
    },
    [
      appContext,
      disabled,
      generationProfile,
      invokeSend,
      isSteeringMode,
    ],
  );

  const editQueuedMessage = useCallback(
    (item: QueuedChatMessage) => {
      setValue((cur) =>
        cur.trim()
          ? `${cur.replace(/\s+$/, "")}\n${item.content}`
          : item.content,
      );
      setMentions((prev) => [...prev, ...item.mentions]);
      onAttachedFilesChange((current) =>
        appendQueuedAttachments(current, item.attachedFiles),
      );
      onAppContextChange?.(item.appContext);
      setMessageQueue((prev) => prev.filter((q) => q.id !== item.id));
      requestAnimationFrame(() => {
        const textarea = textareaRef.current;
        if (!textarea) return;
        textarea.focus();
        textarea.style.height = "auto";
        const lineHeight = 24;
        const maxHeight = lineHeight * 5;
        textarea.style.height = `${Math.min(textarea.scrollHeight, maxHeight)}px`;
      });
    },
    [onAppContextChange, onAttachedFilesChange, setMentions, setValue],
  );

  const removeQueuedMessage = useCallback((id: string) => {
    setMessageQueue((prev) => prev.filter((q) => q.id !== id));
  }, []);

  const applyChatCommand = useCallback(
    (command: ChatCommandDefinition) => {
      setShowSlashMenu(false);
      setSlashSelectionIndex(0);

      // Literal commands remain in the submitted message.  In particular,
      // /masking must reach the server parser as the same raw token whether
      // the user picked it from the menu or typed it directly; it is not an
      // active LLM capability and must never be routed through one.
      if (command.kind === "literal") {
        setValue(`${command.command} `);
        setActiveCommand(null);
        setToolFreeMode(false);
        requestAnimationFrame(() => textareaRef.current?.focus());
        return;
      }

      setValue("");

      if (command.kind === "toggle") {
        if (command.target === "project_context") {
          const nextValue = !projectContextEnabled;
          onProjectContextToggle?.(nextValue);
          toast.success(`Project context: ${nextValue ? "on" : "off"}`);
        } else {
          const nextValue = !deepResearchEnabled;
          if (nextValue) setToolFreeMode(false);
          setDeepResearchMode(nextValue);
          toast.success(`Deep Research: ${nextValue ? "on" : "off"}`);
        }
        requestAnimationFrame(() => textareaRef.current?.focus());
        return;
      }

      setToolFreeMode(false);
      setActiveCommand(command);
      toast.success(`${command.label} を次の送信に適用します`);
      requestAnimationFrame(() => textareaRef.current?.focus());
    },
    [
      deepResearchEnabled,
      onProjectContextToggle,
      projectContextEnabled,
      setDeepResearchMode,
      setActiveCommand,
      setToolFreeMode,
      setValue,
    ],
  );

  const applySkillCommand = useCallback(
    (command: SkillSlashCommand) => {
      setValue(`${command.command} `);
      setShowSlashMenu(false);
      setSlashSelectionIndex(0);
      requestAnimationFrame(() => textareaRef.current?.focus());
    },
    [setValue],
  );

  const handleProjectContextMenuToggle = useCallback(
    (checked: boolean) => {
      onProjectContextToggle?.(checked);
      requestAnimationFrame(() => textareaRef.current?.focus());
    },
    [onProjectContextToggle],
  );

  const handleDeepResearchMenuToggle = useCallback(
    (checked: boolean) => {
      if (checked) setToolFreeMode(false);
      setDeepResearchMode(checked);
      requestAnimationFrame(() => textareaRef.current?.focus());
    },
    [setDeepResearchMode, setToolFreeMode],
  );

  const handleWebSearchMenuToggle = useCallback(
    (checked: boolean) => {
      if (checked) {
        if (!searchCommand) {
          toast.error("Web検索コマンドが見つかりません");
          return;
        }
        setToolFreeMode(false);
        setActiveCommand(searchCommand);
        toast.success("Web検索を次の送信に適用します");
      } else if (webSearchActive) {
        setActiveCommand(null);
        toast.success("Web検索を解除しました");
      }
      requestAnimationFrame(() => textareaRef.current?.focus());
    },
    [searchCommand, webSearchActive, setActiveCommand, setToolFreeMode],
  );

  const handleToolsMenuOpenChange = useCallback((open: boolean) => {
    setToolsMenuOpen(open);
    if (!open) {
      // App contextなど、閉じた直後に別のpopoverを開く場合はtextareaへ戻さない
      if (skipToolsMenuRefocusRef.current) {
        skipToolsMenuRefocusRef.current = false;
        return;
      }
      requestAnimationFrame(() => textareaRef.current?.focus());
    }
  }, []);

  const applySlashMenuItem = useCallback(
    (item: SlashMenuItem) => {
      if (item.kind === "chat") {
        applyChatCommand(item.command);
        return;
      }
      applySkillCommand(item.command);
    },
    [applyChatCommand, applySkillCommand],
  );

  const firstMatchingSkillCommand = useCallback(
    (token: string): SkillSlashCommand | null => {
      const normalized = token.trim().toLowerCase();
      if (!normalized || normalized === "/") return null;
      return (
        skillCommands.find((item) =>
          item.command.toLowerCase().startsWith(normalized),
        ) ?? null
      );
    },
    [skillCommands],
  );

  const completeSlashCommandPrefix = useCallback(() => {
    const token = value.trim();
    if (!showSlashMenu || !isSlashCommandToken(token)) return false;
    if (findChatCommand(token)) return false;
    if (
      skillCommands.some(
        (item) => item.command.toLowerCase() === token.toLowerCase(),
      )
    ) {
      return false;
    }

    const completion =
      completeChatCommandPrefix(token) ??
      firstMatchingSkillCommand(token)?.command ??
      null;
    if (!completion || completion.toLowerCase() === token.toLowerCase()) {
      return false;
    }

    setValue(completion);
    setShowSlashMenu(true);
    requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.focus();
      textarea.setSelectionRange(completion.length, completion.length);
    });
    return true;
  }, [
    firstMatchingSkillCommand,
    setValue,
    showSlashMenu,
    skillCommands,
    value,
  ]);

  const confirmSlashCommand = useCallback(() => {
    if (showSlashMenu && selectedSlashMenuIndex >= 0) {
      const selectedItem = slashMenuItems[selectedSlashMenuIndex];
      if (selectedItem) {
        applySlashMenuItem(selectedItem);
        return true;
      }
    }

    const token = value.trim();
    if (!isSlashCommandToken(token)) return false;

    const exactChatCommand = findChatCommand(token);
    if (exactChatCommand) {
      applyChatCommand(exactChatCommand);
      return true;
    }

    const exactSkillCommand = skillCommands.find(
      (item) => item.command.toLowerCase() === token.toLowerCase(),
    );
    if (exactSkillCommand) {
      applySkillCommand(exactSkillCommand);
      return true;
    }

    const firstChatCommand = firstMatchingChatCommand(token);
    if (firstChatCommand) {
      applyChatCommand(firstChatCommand);
      return true;
    }

    const firstSkillCommand = firstMatchingSkillCommand(token);
    if (firstSkillCommand) {
      applySkillCommand(firstSkillCommand);
      return true;
    }

    return false;
  }, [
    applyChatCommand,
    applySkillCommand,
    applySlashMenuItem,
    firstMatchingSkillCommand,
    selectedSlashMenuIndex,
    showSlashMenu,
    skillCommands,
    slashMenuItems,
    value,
  ]);

  const moveSlashSelection = useCallback(
    (direction: 1 | -1) => {
      if (slashMenuItems.length === 0) return;
      setSlashSelectionIndex((prev) => {
        const current = Math.min(prev, slashMenuItems.length - 1);
        return (
          (current + direction + slashMenuItems.length) % slashMenuItems.length
        );
      });
    },
    [slashMenuItems.length],
  );

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;

    if (showSlashMenu && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      if (slashMenuItems.length > 0) {
        e.preventDefault();
        e.stopPropagation();
        moveSlashSelection(e.key === "ArrowDown" ? 1 : -1);
      }
      return;
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      // A directly typed bare `/help` is a complete one-turn request, not a
      // request to arm a persistent command chip.  The slash menu remains
      // available for users who intentionally select Help and then type a
      // question (or submit the empty chip with the send button).
      if (value.trim().toLowerCase() === "/help") {
        setShowSlashMenu(false);
        setSlashSelectionIndex(0);
        // Help is a complete command turn.  Even while another generation is
        // running, queue it instead of sending a steer payload that would
        // lose the reserved capability metadata.
        handleSend();
        return;
      }
      if (confirmSlashCommand()) return;
      if (!showSlashMenu && !showMentionMenu) {
        handleSend({
          steerImmediately: resolveComposerBusyEnterAction(e) === "steer",
        });
      }
    }
    if (
      e.key === "Tab" ||
      (e.key === "ArrowRight" &&
        e.currentTarget.selectionStart === e.currentTarget.value.length &&
        e.currentTarget.selectionEnd === e.currentTarget.value.length)
    ) {
      if (completeSlashCommandPrefix()) {
        e.preventDefault();
        return;
      }
    }
    if (e.key === "Escape") {
      if (showSlashMenu) {
        setShowSlashMenu(false);
        setSlashSelectionIndex(0);
      }
      if (showMentionMenu) setShowMentionMenu(false);
    }
  };

  const loadSkillCommands = useCallback(() => {
    if (skillsFetchedRef.current) return;
    skillsFetchedRef.current = true;
    fetchSkillSlashCommands(projectId)
      .then(setSkillCommands)
      .catch((err) => {
        console.warn("スキル一覧の取得に失敗:", err);
        skillsFetchedRef.current = false;
      });
  }, [projectId]);

  const handleSlashSearchChange = useCallback(
    (nextValue: string) => {
      const compact = nextValue.replace(/\s+/g, "");
      const commandToken = compact.startsWith("/") ? compact : `/${compact}`;
      setValue(commandToken || "/");
      setShowSlashMenu(true);
      setSlashSelectionIndex(0);
      loadSkillCommands();
    },
    [loadSkillCommands, setValue],
  );

  const handleChange = (
    newValue: string,
    cursorPos: number,
    isCodeBlock: boolean,
  ) => {
    setValue(newValue);

    if (isCodeBlock) {
      setShowSlashMenu(false);
      setSlashSelectionIndex(0);
      setShowMentionMenu(false);
      setMentionQuery("");
      dismissSnippetAutocomplete();
      return;
    }

    // スラッシュコマンド検出
    if (newValue.startsWith("/") && !/\s/.test(newValue)) {
      setShowSlashMenu(true);
      setSlashSelectionIndex(0);
      loadSkillCommands();
    } else if (!newValue.startsWith("/") || newValue.includes(" ")) {
      setShowSlashMenu(false);
      setSlashSelectionIndex(0);
    }

    // @メンション検出
    const textBeforeCursor = newValue.slice(0, cursorPos);
    const atMatch = textBeforeCursor.match(/@([^\s@]*)$/);
    if (atMatch && !/^\/masking(?:\s|$)/i.test(newValue.trimStart())) {
      setShowMentionMenu(true);
      setMentionQuery(atMatch[1]);
    } else {
      setShowMentionMenu(false);
      setMentionQuery("");
    }
  };

  const handleMentionSelect = useCallback(
    (item: MentionItem) => {
      const cursorPos = textareaRef.current?.selectionStart || 0;
      const textBeforeCursor = value.slice(0, cursorPos);
      const atIndex = textBeforeCursor.lastIndexOf("@");

      // Docsは既存のサーバー検証済みcanonical参照を使う。これにより
      // WebSocket/RESTのどちらでも、タイトル検索へ戻らず正確なnodeを解決できる。
      const mentionToken =
        item.type === "docs"
          ? createDocsNodeWikilink(item.id, item.name)
          : `@[[${item.type}:${item.id}:${item.name}]]`;
      const newValue =
        value.slice(0, atIndex) + mentionToken + " " + value.slice(cursorPos);
      setValue(newValue);
      if (item.type !== "docs") {
        setMentions((prev) => [...prev, item]);
      }
      setShowMentionMenu(false);
      setMentionQuery("");
      textareaRef.current?.focus();
    },
    [setMentions, setValue, value],
  );

  // File selection, paste and drop all pass through this budget boundary.
  const addFiles = useCallback(
    (files: FileList | File[]) => {
      const incoming = Array.from(files);
      onAttachedFilesChange((prev) => {
        const accepted = [...prev];
        for (const file of incoming) {
          const error = chatAttachmentError([...accepted, file]);
          if (error) toast.error(`${file.name}: ${error}`);
          else accepted.push(file);
        }
        return accepted.length > prev.length ? accepted : prev;
      });
    }, [onAttachedFilesChange],
  );

  // ファイル削除
  const removeFile = useCallback(
    (index: number) => {
      onAttachedFilesChange((prev) => prev.filter((_, i) => i !== index));
    },
    [onAttachedFilesChange],
  );

  // ドラッグ&ドロップハンドラ
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragOver(false);
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        addFiles(e.dataTransfer.files);
      }
    },
    [addFiles],
  );

  // Clipboard file items share the picker/drop validation for every format.
  const handlePaste = useCallback(
    (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const items = event.clipboardData?.items;
      if (!items) return;

      const pastedFiles = Array.from(items)
        .filter(
          (item) => item.kind === "file",
        )
        .map((item) => item.getAsFile())
        .filter((file): file is File => file !== null);

      if (pastedFiles.length === 0) return;

      event.preventDefault();
      addFiles(pastedFiles);
    },
    [addFiles],
  );

  // ファイル選択ハンドラ
  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (e.target.files && e.target.files.length > 0) {
        addFiles(e.target.files);
        // inputをリセットして同じファイルを再選択可能に
        e.target.value = "";
      }
    },
    [addFiles],
  );

  const handleVoiceTranscript = useCallback(
    async (text: string) => {
      const result = await onSend(text);
      return result === "retained" ? "accepted" : result;
    },
    [onSend],
  );

  return (
    <div
      className="shrink-0 bg-background px-4 py-2"
      data-chat-composer="true"
    >
      <ChatIntentDialog intent={intent} onClosed={() => textareaRef.current?.focus()} />
      <div className="chat-viewport-center mx-auto w-full max-w-5xl space-y-2">
        {/* 添付ファイルプレビュー */}
        {attachedFiles.length > 0 && (
          <div className="flex max-h-40 flex-wrap gap-2 overflow-y-auto">
            {attachedFiles.map((file, index) => (
              <ComposerAttachmentPreview
                key={`${file.name}-${index}`}
                file={file}
                onRemove={() => removeFile(index)}
              />
            ))}
            <div className="flex w-full flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>{attachedFiles.length}/12件 · 合計{formatBytes(attachedFiles.reduce((size, file) => size + file.size, 0))}/100 MB</span>
              <Button type="button" size="xs" variant="outline" onClick={() => setValue(value.trim()
                ? `${value}\nこれらの添付を手がかりに、Docs・Knowledge・会話・資料から探してください。`
                : "これらの添付を手がかりに、Docs・Knowledge・会話・資料から探してください。")}>添付で探す</Button>
              <Button type="button" size="xs" variant="ghost" onClick={() => setValue(value.trim()
                ? `${value}\n各添付を個別の手がかりにして検索してください。`
                : "各添付を個別の手がかりにして検索してください。")}>個別に探す</Button>
            </div>
          </div>
        )}

        {messageQueue.length > 0 && (
          <div className="overflow-hidden rounded-md border border-border-subtle bg-surface-container-low text-xs shadow-none">
            <div className="flex min-h-9 items-center gap-2 border-b border-border-subtle px-3 py-2 font-medium text-text-secondary">
              <CornerDownRight className="size-3.5 shrink-0" />
              <span>送信待ち {messageQueue.length}件</span>
            </div>
            <div className="max-h-32 space-y-1 overflow-y-auto px-3 py-2">
              {messageQueue.map((item) => (
                <div
                  key={item.id}
                  className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-2"
                >
                  <span
                    className="min-w-0 truncate text-foreground/85"
                    title={item.content}
                  >
                    {item.content}
                  </span>
                  <button
                    type="button"
                    onClick={() => editQueuedMessage(item)}
                    className="flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-background/70 hover:text-foreground"
                    aria-label="この送信待ちを編集"
                    title="編集"
                  >
                    <Pencil className="size-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => removeQueuedMessage(item.id)}
                    className="flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-destructive hover:text-destructive-foreground"
                    aria-label="この送信待ちを削除"
                    title="削除"
                  >
                    <X className="size-3.5" />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="flex min-w-0 flex-wrap items-end gap-1.5 rounded-md border border-border-subtle bg-surface-charcoal p-1">
          {!isAoiDotsPresentation && (
            <>
              <GenerationProfileSelector
                value={generationProfile}
                onChange={handleGenerationProfileChange}
                open={generationProfileMenuOpen}
                onOpenChange={setGenerationProfileMenuOpen}
                onComposerFocusRequest={() => textareaRef.current?.focus()}
              />
              <PlanningPolicySelector
                value={
                  planningPolicyHydrated
                    ? localPlanningPolicy
                    : DEFAULT_PLANNING_POLICY
                }
                onChange={handlePlanningPolicyChange}
                open={planningPolicyMenuOpen}
                onOpenChange={setPlanningPolicyMenuOpen}
                onComposerFocusRequest={() => textareaRef.current?.focus()}
              />
            </>
          )}

          <DropdownMenu
            open={toolsMenuOpen}
            onOpenChange={handleToolsMenuOpenChange}
          >
            <DropdownMenuTrigger
              render={
                <Button
                  ref={toolsMenuButtonRef}
                  type="button"
                  variant={toolsMenuActive ? "secondary" : "ghost"}
                  size="icon"
                  className={cn(
                    "shrink-0",
                    toolsMenuActive &&
                      "border border-primary/40 text-primary shadow-sm",
                  )}
                  disabled={disabled || isSteeringMode}
                  title="ツール (Ctrl+.)"
                  aria-label="ツール"
                  aria-pressed={toolsMenuActive}
                />
              }
            >
              <Plus className="size-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent
              side="top"
              sideOffset={8}
              align="start"
              className="w-56"
            >
              {!isAoiDotsPresentation && (
                <>
                  <DropdownMenuCheckboxItem
                    mnemonic="I"
                    checked={intentConfirmationEnabled}
                    disabled={intent.sending}
                    onCheckedChange={(checked) => setIntentConfirmationEnabled(checked === true)}
                    className="gap-2 py-1.5"
                  >
                    <Brain className="size-4" />
                    <span>意図確認</span>
                  </DropdownMenuCheckboxItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuCheckboxItem
                    mnemonic="P"
                    checked={projectContextEnabled}
                    onCheckedChange={(checked) =>
                      handleProjectContextMenuToggle(checked === true)
                    }
                    className="gap-2 py-1.5"
                  >
                    <FolderOpen className="size-4" />
                    <span>Project context</span>
                  </DropdownMenuCheckboxItem>
                  <DropdownMenuCheckboxItem
                    mnemonic="D"
                    checked={deepResearchEnabled}
                    onCheckedChange={(checked) =>
                      handleDeepResearchMenuToggle(checked === true)
                    }
                    className="gap-2 py-1.5"
                  >
                    <Brain className="size-4" />
                    <span>Deep Research</span>
                  </DropdownMenuCheckboxItem>
                  <DropdownMenuCheckboxItem
                    mnemonic="W"
                    checked={webSearchActive}
                    onCheckedChange={(checked) =>
                      handleWebSearchMenuToggle(checked === true)
                    }
                    className="gap-2 py-1.5"
                  >
                    <Search className="size-4" />
                    <span>Web検索</span>
                  </DropdownMenuCheckboxItem>
                  <DropdownMenuCheckboxItem
                    mnemonic="N"
                    checked={toolFreeMode}
                    disabled={deepResearchEnabled || webSearchActive}
                    onCheckedChange={(checked) => setToolFreeMode(checked === true)}
                    className="gap-2 py-1.5"
                  >
                    <Gauge className="size-4" />
                    <span>ツールなし（無料枠優先）</span>
                  </DropdownMenuCheckboxItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    mnemonic="A"
                    onClick={() => {
                      skipToolsMenuRefocusRef.current = true;
                      requestAnimationFrame(() => setAppContextPickerOpen(true));
                    }}
                    className="gap-2 py-1.5"
                  >
                    <AppWindow className="size-4" />
                    <span>
                      App context
                      {appContext
                        ? `: ${appContext.appName} / ${appContext.targetKey}`
                        : "…"}
                    </span>
                  </DropdownMenuItem>
                </>
              )}
              <DropdownMenuItem
                mnemonic="F"
                onClick={() => fileInputRef.current?.click()}
                className="gap-2 py-1.5"
              >
                <Paperclip className="size-4" />
                <span>ファイル添付</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {!isAoiDotsPresentation && (
            <>
              <VoicePanel
                conversationSessionId={sessionId}
                projectId={projectId}
                includeProjectContext={projectContextEnabled}
                characterName={runtime?.currentCharacter ?? null}
                ensureConversationSession={ensureLiveVoiceConversationSession}
                assistantMessage={voiceAssistantMessage}
                chatBusy={Boolean(busy)}
                onSendTranscript={handleVoiceTranscript}
                onStopGeneration={onStop}
                disabled={disabled}
              />
              <AppContextPicker
                value={appContext}
                projectId={projectId}
                onChange={onAppContextChange ?? (() => undefined)}
                open={appContextPickerOpen}
                onOpenChange={setAppContextPickerOpen}
                anchorRef={toolsMenuButtonRef}
              />
            </>
          )}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={handleFileSelect}
          />

          <div className="relative min-w-[min(100%,16rem)] flex-1">
            {((!isSteeringMode && (Boolean(activeCommand) || toolsMenuActive)) || intentConfirmationEnabled) && (
              <div
                className="mb-1 flex min-w-0 flex-wrap items-center gap-1.5"
                data-testid="chat-composer-active-chips"
              >
                {!isSteeringMode && activeCommand && (
                  <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-primary/35 bg-primary/10 px-2 py-1 text-xs font-medium text-primary">
                    <span className="truncate">{activeCommand.label}</span>
                    <button
                      type="button"
                      className="rounded-sm text-primary/70 hover:text-primary"
                      onClick={() => setActiveCommand(null)}
                      aria-label={`${activeCommand.label} commandを解除`}
                      title={`${activeCommand.label} commandを解除`}
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                )}

                {intentConfirmationEnabled && (
                  <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-primary/35 bg-primary/10 px-2 py-1 text-xs font-medium text-primary">
                    <span className="truncate">意図確認</span>
                    <button
                      type="button"
                      className="rounded-sm text-primary/70 hover:text-primary"
                      onClick={() => setIntentConfirmationEnabled(false)}
                      disabled={intent.sending}
                      aria-label="意図確認を解除"
                      title="意図確認を解除"
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                )}

                {!isSteeringMode && projectContextEnabled && (
                  <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-primary/35 bg-primary/10 px-2 py-1 text-xs font-medium text-primary">
                    <span className="truncate">Project context</span>
                    <button
                      type="button"
                      className="rounded-sm text-primary/70 hover:text-primary"
                      onClick={() => handleProjectContextMenuToggle(false)}
                      aria-label="Project contextを解除"
                      title="Project contextを解除"
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                )}

                {!isSteeringMode && deepResearchEnabled && (
                  <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-primary/35 bg-primary/10 px-2 py-1 text-xs font-medium text-primary">
                    <span className="truncate">Deep Research</span>
                    <button
                      type="button"
                      className="rounded-sm text-primary/70 hover:text-primary"
                      onClick={() => handleDeepResearchMenuToggle(false)}
                      aria-label="Deep Researchを解除"
                      title="Deep Researchを解除"
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                )}

                {!isSteeringMode && toolFreeMode && (
                  <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-primary/35 bg-primary/10 px-2 py-1 text-xs font-medium text-primary">
                    <span className="truncate">ツールなし（無料枠優先）</span>
                    <button
                      type="button"
                      className="rounded-sm text-primary/70 hover:text-primary"
                      onClick={() => setToolFreeMode(false)}
                      aria-label="ツールなし（無料枠優先）を解除"
                      title="ツールなし（無料枠優先）を解除"
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                )}

                {!isSteeringMode && appContext && (
                  <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-primary/35 bg-primary/10 px-2 py-1 text-xs font-medium text-primary">
                    <span className="truncate">
                      App: {appContext.appName} / {appContext.targetKey}
                    </span>
                    <button
                      type="button"
                      className="rounded-sm text-primary/70 hover:text-primary"
                      onClick={() => onAppContextChange?.(null)}
                      aria-label="App contextを解除"
                      title="App contextを解除"
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                )}
              </div>
            )}

            {/* スラッシュコマンドメニュー */}
            {showSlashMenu && (
              <div
                ref={slashMenuRef}
                className="absolute bottom-full left-0 z-50 mb-2 w-64 rounded-lg border bg-popover shadow-md"
              >
                <Command shouldFilter={false}>
                  <CommandInput
                    value={slashQuery}
                    onValueChange={handleSlashSearchChange}
                    placeholder="コマンド検索..."
                    className="h-8"
                  />
                  <CommandList>
                    <CommandEmpty>コマンドが見つかりません</CommandEmpty>
                    {filteredChatCommands.length > 0 && (
                      <CommandGroup heading="コマンド">
                        {filteredChatCommands.map((cmd, index) => (
                          <CommandItem
                            key={cmd.command}
                            value={`${cmd.command} ${cmd.label}`}
                            data-slash-command-index={index}
                            className={cn(
                              index === selectedSlashMenuIndex &&
                                "bg-muted text-foreground",
                            )}
                            onMouseEnter={() => setSlashSelectionIndex(index)}
                            onSelect={() =>
                              applySlashMenuItem({
                                kind: "chat",
                                command: cmd,
                              })
                            }
                          >
                            <div className="flex flex-col">
                              <span className="font-mono text-sm">
                                {cmd.command}
                              </span>
                              {(cmd.kind === "literal" ||
                                (cmd.kind === "capability" &&
                                  cmd.capability === "aoitalk_help")) && (
                                <span className="text-sm font-medium text-foreground">
                                  {cmd.label}
                                </span>
                              )}
                              <span className="text-xs text-muted-foreground">
                                {cmd.description}
                              </span>
                            </div>
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    )}
                    {filteredSkillCommands.length > 0 && (
                      <CommandGroup heading="スキル（プロンプト）">
                        {filteredSkillCommands.map((cmd, index) => {
                          const menuIndex = filteredChatCommands.length + index;
                          return (
                            <CommandItem
                              key={cmd.command}
                              value={cmd.command}
                              data-slash-command-index={menuIndex}
                              className={cn(
                                menuIndex === selectedSlashMenuIndex &&
                                  "bg-muted text-foreground",
                              )}
                              onMouseEnter={() =>
                                setSlashSelectionIndex(menuIndex)
                              }
                              onSelect={() =>
                                applySlashMenuItem({
                                  kind: "skill",
                                  command: cmd,
                                })
                              }
                            >
                              <div className="flex flex-col">
                                <span className="font-mono text-sm">
                                  {cmd.usage}
                                </span>
                                <span className="text-xs text-muted-foreground">
                                  {cmd.description}
                                </span>
                              </div>
                            </CommandItem>
                          );
                        })}
                      </CommandGroup>
                    )}
                  </CommandList>
                </Command>
              </div>
            )}

            {/* @メンションメニュー */}
            {showMentionMenu && (
              <div className="absolute bottom-full left-0 z-50 mb-2 w-80">
                <MentionMenu
                  query={mentionQuery}
                  onSelect={handleMentionSelect}
                  onClose={() => setShowMentionMenu(false)}
                  projectId={projectId}
                  sessionId={sessionId}
                />
              </div>
            )}

            <ChatComposerInput
              value={value}
              placeholder={
                isSteeringMode
                  ? "メッセージを入力（Enter: 次に送る / Ctrl+Enter: 今の応答へ割り込む）"
                  : composerPlaceholder
              }
              onValueChange={handleChange}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              onInputRef={setComposerInputRef}
              onCursorContextChange={handleComposerCursorContextChange}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              isDragOver={isDragOver}
            />
          </div>

          {runtime && !isAoiDotsPresentation && (
            <ModelControl
              runtime={runtime}
              userSettings={userSettings}
              llmMode={llmMode}
              llmModeOptions={llmModeOptions}
              llmModeLabels={llmModeLabels}
              llmModeLoading={llmModeLoading}
              llmModeError={llmModeError}
              onLlmModeChange={onLlmModeChange}
              sessionId={sessionId}
              userScopeKey={draftUserId}
              contextSnapshot={contextSnapshot}
              contextSnapshotStatus={contextSnapshotStatus}
              open={modelControlOpen}
              onOpenChange={setModelControlOpen}
              onComposerFocusRequest={() => textareaRef.current?.focus()}
              onLlmModeRefresh={refreshLlmMode}
              agentId={agentId}
            />
          )}

          <ChatQuickPrompts
            sendDisabled={disabled || isSteeringMode}
            onSendPrompt={handleQuickPromptSend}
          />

          {isSteeringMode ? (
            <>
              <Button
                type="button"
                size="icon"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => handleSend()}
                disabled={
                  !value.trim() && activeCommand?.capability !== "aoitalk_help"
                }
                className="shrink-0"
                title="送信待ちに追加 (Enter)"
                aria-label="送信待ちに追加"
              >
                <Send className="size-4" />
              </Button>
              {onStop && (
                <Button
                  type="button"
                  variant="destructive"
                  size="icon"
                  onClick={onStop}
                  className="shrink-0"
                  title="応答生成を停止"
                  aria-label="応答生成を停止"
                >
                  <Square className="size-4" />
                </Button>
              )}
            </>
          ) : (
            <Button
              size="icon"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => handleSend()}
              disabled={
                (isEmpty &&
                  activeCommand?.capability !== "work_intake" &&
                  activeCommand?.capability !== "aoitalk_help") ||
                disabled
              }
              className="shrink-0"
              title="送信"
            >
              <Send className="size-4" />
            </Button>
          )}
        </div>
      </div>
      <SnippetPopup state={snippetState} />
    </div>
  );
}

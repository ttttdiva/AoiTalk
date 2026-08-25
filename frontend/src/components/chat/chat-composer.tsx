"use client";

import {
  AppSelect,
  type AppSelectChangeEvent,
  type AppSelectOpenChangeDetails,
} from "@/components/ui/app-select";

import {
  useState,
  useRef,
  useEffect,
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
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import {
  getChatComposerShortcutAction,
  resolveComposerBusyEnterAction,
} from "@/lib/chat-keyboard-shortcuts";
import { isOversizedMailAttachment } from "@/lib/chat-attachment-validation";
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
  type ChatComposerDraft,
  clearChatComposerDraft,
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
import {
  HIDDEN_CHAT_SKILL_NAMES,
  completeChatCommandPrefix,
  filterChatCommands,
  firstMatchingChatCommand,
  findChatCommand,
  isSlashCommandToken,
  resolveChatCommandSubmission,
  type ActiveChatCommand,
  type ChatCommandDefinition,
  type ChatCommandCapability,
} from "@/lib/chat-commands";
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
  ) => void | boolean | ChatComposerSendResult | Promise<void | boolean | ChatComposerSendResult>;
  onSteer?: (content: string) => void;
  onStop?: () => void;
  disabled: boolean;
  busy?: boolean;
  /** generation reducerが初回terminal遷移時だけ更新する一意キー */
  generationTerminalKey?: string | null;
  attachedFiles: File[];
  onAttachedFilesChange: Dispatch<SetStateAction<File[]>>;
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
  contextSnapshot?: ContextSnapshot | null;
  contextSnapshotStatus?: string;
  appContext?: ChatAppContextSelection | null;
  onAppContextChange?: (value: ChatAppContextSelection | null) => void;
  /** S9 supplies a session for Live Voice when this composer is a new chat. */
  ensureLiveVoiceConversationSession?: () => Promise<string>;
};

export type ChatComposerSendResult = "accepted" | "pending" | "failed";

const runtimeSelectClassName =
  "h-9 min-w-0 rounded-md border border-input bg-background px-2 text-xs text-foreground outline-none transition-colors focus-visible:border-ring";

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
}: {
  runtime: RuntimeContextValue;
  userSettings: Parameters<typeof filterVisibleProviders>[1];
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
}) {
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
    summaryLabel,
    updateEffort,
    ...routeFields
  } = routeState;
  const [modeRefreshing, setModeRefreshing] = useState(false);

  const available = filterAvailableProviders(
    runtime.llmEngines,
    runtime.llmDeployment,
    (item) => item.provider,
    (item) => item,
  );
  const visible = filterVisibleProviders(
    available,
    userSettings,
    [runtime.currentLlm?.provider],
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
  const modelBusy =
    runtime.llmChanging || modeRefreshing || !runtime.isConnected;
  const currentEngine: RuntimeLlmEngine | null = runtimeDisplayRoute
    ? {
        provider: runtimeDisplayRoute.provider,
        model: runtimeDisplayRoute.model,
        label: runtimeDisplayRoute.model,
      }
    : null;
  // エンジン一覧の一時的な取得失敗中も、last-known-goodの現在Modelを選択欄に
  // 残す。候補一覧が復旧すれば通常の候補へ自然に置き換わる。
  const currentVisibleEngine = currentEngine
    ? filterVisibleProviders(
        filterAvailableProviders(
          [currentEngine],
          runtime.llmDeployment,
          (item) => item.provider,
          (item) => item,
        ),
        userSettings,
        [currentEngine.provider],
        (item) => item.provider,
      )[0]
    : null;
  const currentEngineKey = currentEngine
    ? `${currentEngine.provider}::${currentEngine.model}`
    : "";
  // The backend normally completes the current option, but keep the
  // controlled select valid if a partial catalog temporarily omits it.
  const selectableModels =
    currentVisibleEngine &&
    !visible.some(
      (engine) => `${engine.provider}::${engine.model}` === currentEngineKey,
    )
      ? [currentVisibleEngine, ...visible]
      : visible.length > 0
        ? visible
        : currentVisibleEngine
          ? [currentVisibleEngine]
          : [];
  const currentValue = runtimeDisplayRoute
    ? `${runtimeDisplayRoute.provider}::${runtimeDisplayRoute.model}`
    : "";
  const deploymentProvider = resolveEffectiveProviderId(runtime.llmDeployment);
  const deploymentModel = resolveEffectiveModelId(runtime.llmDeployment);
  const effectiveLabel = [deploymentProvider, deploymentModel]
    .filter(Boolean)
    .join(" / ");
  // A session's effective route is authoritative for that conversation.  The
  // deployment metadata is the runtime default/constraint, not the route that
  // will be used for a session with an explicit effective route.  Keep these
  // labels separate so a Qwen session is not reported as the global Gemma
  // default in Chat settings.
  const sessionEffectiveLabel =
    sessionProvider && sessionModel
      ? formatRouteLabel(sessionProvider, sessionModel)
      : "";
  const hasSessionEffectiveRoute = Boolean(
    sessionId && hasSessionScopedRoute && sessionEffectiveLabel,
  );
  const runtimeDefaultDiffers = Boolean(
    hasSessionEffectiveRoute &&
      effectiveLabel &&
      effectiveLabel !== sessionEffectiveLabel,
  );
  const fixedDeployment =
    runtime.llmDeployment?.fixed === true && Boolean(deploymentProvider);
  const selectedRouteProvider = fixedDeployment
    ? deploymentProvider
    : sessionProvider || runtimeDisplayRoute?.provider || "";
  const selectedRouteModel = fixedDeployment
    ? deploymentModel
    : sessionModel || runtimeDisplayRoute?.model || "";
  const selectedEngine = [
    ...runtime.llmEngines,
    ...(currentEngine ? [currentEngine] : []),
  ].find(
    (engine) =>
      engine.provider === selectedRouteProvider &&
      engine.model === selectedRouteModel,
  );
  const selectedCatalogProvider = runtime.llmCatalog?.providers?.find(
    (provider) => provider.id === selectedRouteProvider,
  );
  const selectedCatalogModel = selectedCatalogProvider?.models?.find(
    (model) => model.id === selectedRouteModel,
  );
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
  const modeLabel = effortUnsupported
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
  const modelLabel = fixedDeployment
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
    !runtime.isConnected ||
    modeRefreshing ||
    (!hasSessionScopedRoute && !onLlmModeChange);
  const providerDisabled = routeProviderDisabled || modelBusy;
  const modelDisabled = routeModelDisabled || modelBusy;
  const currentCharacter = runtime.characters.find(
    (character) => character.slug === runtime.currentCharacter,
  );
  const characterLabel = currentCharacter
    ? characterOptionLabel(currentCharacter, runtime.characters)
    : runtime.currentCharacter || "—";
  const contextLabel = percentage == null ? "—" : `${Math.round(percentage)}%`;

  // Chat settings has two explicit keyboard layers. `outerSelection` tracks
  // the settings field selected by the user, while `innerOpen` is the one
  // AppSelect popup currently owned by that field. Never infer either state
  // from document.activeElement: Base UI moves focus through a portal when a
  // popup opens and restores it asynchronously when it closes.
  const settingsItems = useMemo<ChatSettingsItemState[]>(
    () => [
      ...(!fixedDeployment &&
      (runtime.llmCatalog?.providers?.length || selectableModels.length > 0)
        ? [
            { id: "provider" as const, disabled: providerDisabled },
            { id: "model" as const, disabled: modelDisabled },
          ]
        : []),
      ...(effectiveOptions.length > 0
        ? [
            {
              id: "effort" as const,
              disabled: effortDisabled,
            },
          ]
        : []),
      { id: "agentTeam", disabled: agentTeamDisabled },
      { id: "executionProfile", disabled: executionProfileDisabled },
      ...(runtime.characters.length > 0
        ? [
            {
              id: "character" as const,
              disabled:
                !runtime.isConnected || runtime.characterChanging === true,
            },
          ]
        : []),
    ],
    [
      agentTeamDisabled,
      effectiveOptions.length,
      effortDisabled,
      executionProfileDisabled,
      fixedDeployment,
      modeRefreshing,
      modelDisabled,
      providerDisabled,
      routeSettingsLoading,
      runtime.characterChanging,
      runtime.characters.length,
      runtime.isConnected,
      runtime.llmCatalog?.providers?.length,
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
            aria-label="モデル・推論・キャラクター・コンテキスト設定"
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
            <span className="shrink-0 text-[10px] text-muted-foreground">
              · {modeLabel}
            </span>
          </span>
          {runtime.characters.length > 0 && (
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
                provider · model · effort · team · execution profile · character
              </div>
            </div>
            {runtime.llmChangeError && (
              <span role="alert" className="max-w-48 text-right text-[10px] text-destructive">
                {runtime.llmChangeError}
              </span>
            )}
          </div>

          {fixedDeployment ? (
            <div
              className="rounded-md border border-amber-500/50 bg-amber-50 px-2.5 py-2 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200"
              title={effectiveLabel || "Enterprise deployment"}
            >
              <div className="font-medium">Enterprise deployment</div>
              <div className="truncate">{effectiveLabel || "Fixed model"}</div>
            </div>
          ) : null}

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
                      disabled={
                        !runtime.isConnected ||
                        modeRefreshing ||
                        routeSettingsLoading ||
                        effectiveOptions.length === 0 ||
                        (!hasSessionScopedRoute && !onLlmModeChange)
                      }
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
          ) : !fixedDeployment && selectableModels.length === 0 && llmStatus === "error" ? (
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

          {!fixedDeployment && (
            <>
              {hasSessionEffectiveRoute ? (
                <div
                  className="truncate text-[10px] text-muted-foreground"
                  title={sessionEffectiveLabel}
                >
                  Session effective: {sessionEffectiveLabel}
                </div>
              ) : !sessionId && hasDeploymentMetadata(runtime.llmDeployment) && effectiveLabel ? (
                <div
                  className="truncate text-[10px] text-muted-foreground"
                  title={effectiveLabel}
                >
                  Effective: {effectiveLabel}
                </div>
              ) : null}
              {hasDeploymentMetadata(runtime.llmDeployment) && runtimeDefaultDiffers && (
                <div
                  className="truncate text-[10px] text-muted-foreground"
                  title={effectiveLabel}
                >
                  Runtime default: {effectiveLabel}
                </div>
              )}
            </>
          )}

          {runtime.characters.length > 0 && (
            <label className="grid gap-1 text-xs text-muted-foreground">
              <span>Character</span>
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
                disabled={
                  !runtime.isConnected || runtime.characterChanging === true
                }
              >
                {runtime.characters.map((character) => (
                  <option key={character.slug} value={character.slug}>
                    {characterOptionLabel(character, runtime.characters)}
                  </option>
                ))}
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
  return (
    (data.skills ?? [])
      // AUTO は LLM 自動判断専用なのでスラッシュ候補から除外する
      .filter((skill) => skill.trigger_mode !== "auto")
      .filter((skill) => !HIDDEN_CHAT_SKILL_NAMES.has(skill.name))
      .map((skill) => ({
        command: `/${skill.name}`,
        description: skill.description || "スキル",
        usage: `/${skill.name} [入力]`,
      }))
  );
}

function isImageFile(file: File) {
  return file.type.startsWith("image/");
}

function isVideoFile(file: File) {
  return (
    file.type.startsWith("video/") ||
    /\.(mp4|mov|mkv)$/i.test(file.name) ||
    (/\.webm$/i.test(file.name) && !file.type.startsWith("audio/"))
  );
}

function isAudioFile(file: File) {
  return (
    !isVideoFile(file) &&
    (file.type.startsWith("audio/") ||
      /\.(wav|mp3|m4a|flac|ogg|webm)$/i.test(file.name))
  );
}

const MAX_IMAGE_ATTACHMENTS = 4;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_AUDIO_ATTACHMENTS = 1;
const MAX_AUDIO_BYTES = 25 * 1024 * 1024;
const MAX_VIDEO_ATTACHMENTS = 1;
const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;

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
  const label = labels[mode]?.trim();
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
  generationTerminalKey = null,
  attachedFiles,
  onAttachedFilesChange,
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
  contextSnapshot,
  contextSnapshotStatus,
  appContext = null,
  onAppContextChange,
  ensureLiveVoiceConversationSession,
}: ChatComposerProps) {
  const runtime = useOptionalRuntimeContext();
  const { mutate: mutateSWR } = useSWRConfig();
  const refreshLlmMode = useCallback(async () => {
    await mutateSWR("chat/llm-mode");
  }, [mutateSWR]);
  // AppLayout の getSession() で確定した ID を使い、認証状態の再取得待ちで
  // 新規チャット下書きが別スコープへ移る競合を避ける。
  const draftUserId = useCurrentUserId();
  const composerDraftKey = sessionId ?? NEW_CHAT_COMPOSER_DRAFT_KEY;
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
      updateComposerDraft((current) => ({
        ...current,
        content: typeof next === "function" ? next(current.content) : next,
      })),
    [updateComposerDraft],
  );
  const setMentions = useCallback<Dispatch<SetStateAction<MentionItem[]>>>(
    (next) =>
      updateComposerDraft((current) => ({
        ...current,
        mentions: typeof next === "function" ? next(current.mentions) : next,
      })),
    [updateComposerDraft],
  );
  const setActiveCommand = useCallback<
    Dispatch<SetStateAction<ActiveChatCommand | null>>
  >(
    (next) =>
      updateComposerDraft((current) => ({
        ...current,
        activeCommand:
          typeof next === "function" ? next(current.activeCommand) : next,
      })),
    [updateComposerDraft],
  );
  const setToolFreeMode = useCallback<Dispatch<SetStateAction<boolean>>>(
    (next) =>
      updateComposerDraft((current) => ({
        ...current,
        toolFreeMode:
          typeof next === "function" ? next(current.toolFreeMode) : next,
      })),
    [updateComposerDraft],
  );
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
  const [audioAttachmentEnabled, setAudioAttachmentEnabled] = useState(true);
  const [videoAttachmentEnabled, setVideoAttachmentEnabled] = useState(true);
  const [videoMaxBytes, setVideoMaxBytes] = useState(MAX_ATTACHMENT_BYTES);
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

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch("/api/python-proxy/settings", {
          credentials: "include",
        });
        if (!res.ok) return;
        const data = await res.json();
        const engine =
          data?.settings?.model_routing?.classes?.audio?.engine ??
          "speech_recognition";
        const mageVl = data?.settings?.mage_vl ?? {};
        const videoMode =
          data?.settings?.model_routing?.media?.video_mode ?? "auto";
        const configuredMaxBytes = Number(mageVl.max_video_bytes);
        if (!cancelled) {
          setAudioAttachmentEnabled(engine !== "off");
          setVideoAttachmentEnabled(
            mageVl.enabled !== false && videoMode !== "off",
          );
          setVideoMaxBytes(
            Number.isFinite(configuredMaxBytes) && configuredMaxBytes > 0
              ? Math.min(configuredMaxBytes, MAX_ATTACHMENT_BYTES)
              : MAX_ATTACHMENT_BYTES,
          );
        }
      } catch {
        if (!cancelled) {
          setAudioAttachmentEnabled(true);
          setVideoAttachmentEnabled(true);
          setVideoMaxBytes(MAX_ATTACHMENT_BYTES);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
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
  const filteredSkillCommands = useMemo(() => {
    const normalized = slashQuery.trim().toLowerCase();
    if (!normalized || normalized === "/") return skillCommands;
    return skillCommands.filter((item) =>
      item.command.toLowerCase().startsWith(normalized),
    );
  }, [skillCommands, slashQuery]);
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

  useEffect(() => {
    const previousDraftKey = previousDraftKeyRef.current;
    if (
      previousDraftKey === NEW_CHAT_COMPOSER_DRAFT_KEY &&
      composerDraftKey !== NEW_CHAT_COMPOSER_DRAFT_KEY
    ) {
      const promotedDraft = getChatComposerDraft(
        NEW_CHAT_COMPOSER_DRAFT_KEY,
        draftUserId,
      );
      promoteNewChatComposerDraft(composerDraftKey, draftUserId);
      if (!draftsMatch(promotedDraft, EMPTY_CHAT_COMPOSER_DRAFT)) {
        promotedDraftRef.current = {
          sourceKey: NEW_CHAT_COMPOSER_DRAFT_KEY,
          targetKey: composerDraftKey,
          userId: draftUserId,
          draft: promotedDraft,
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
        undefined,
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

  const clearComposerAfterAcceptedSend = useCallback(
    (submittedDraft?: ChatComposerDraft) => {
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

      onAttachedFilesChange([]);
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
      let result: ReturnType<ChatComposerProps["onSend"]>;
      try {
        result = onSend(...args);
      } catch {
        return;
      }
      void Promise.resolve(result)
        .then((accepted) => {
          // false/pending/failed はまだ dispatch 成功を確認できないため、
          // 下書きは残して再送できるようにする。
          // 既存の void callback は従来どおり成功扱いにする。
          if (
            accepted === false ||
            accepted === "pending" ||
            accepted === "failed"
          ) {
            return;
          }
          if (clearAfterAccepted) {
            clearComposerAfterAcceptedSend(submittedDraft);
          }
        })
        .catch(() => {
          // 送信失敗時は下書きを保持する。
        });
    },
    [clearComposerAfterAcceptedSend, onSend],
  );

  const enqueue = useCallback(() => {
    const text = value.trim();
    if (!text) return;
    const submission = resolveChatCommandSubmission(
      value,
      activeCommand,
      false,
    );
    if (submission.error) {
      toast.error(submission.error);
      return;
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
        content: submission.content,
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
  }, [
    appContext,
    composerDraftKey,
    draftUserId,
    value,
    activeCommand,
    generationProfile,
    mentions,
    sessionId,
    toolFreeMode,
    setActiveCommand,
    setMentions,
    setToolFreeMode,
    setValue,
  ]);

  const handleSend = useCallback(
    (options?: { steerImmediately?: boolean }) => {
      if (isSteeringMode) {
        const text = value.trim();
        if (!text) return;
        if (options?.steerImmediately) {
          if (!onSteer) return;
          onSteer(text);
          setValue("");
          requestAnimationFrame(() => {
            if (textareaRef.current) {
              textareaRef.current.style.height = "auto";
              textareaRef.current.focus();
            }
          });
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
      if (isEmpty || disabled) return;
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
      };
      if (appContext) {
        invokeSend(submittedDraft, true, ...args, appContext);
      } else {
        invokeSend(submittedDraft, true, ...args);
      }
    },
    [
      value,
      isEmpty,
      disabled,
      isSteeringMode,
      invokeSend,
      onSteer,
      attachedFiles,
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
      if (!content || disabled || isSteeringMode) return;

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
    [onAppContextChange, setMentions, setValue],
  );

  const removeQueuedMessage = useCallback((id: string) => {
    setMessageQueue((prev) => prev.filter((q) => q.id !== id));
  }, []);

  const applyChatCommand = useCallback(
    (command: ChatCommandDefinition) => {
      setShowSlashMenu(false);
      setSlashSelectionIndex(0);
      setValue("");

      if (command.kind === "toggle") {
        if (command.target === "project_context") {
          const nextValue = !projectContextEnabled;
          onProjectContextToggle?.(nextValue);
          toast.success(`Project context: ${nextValue ? "on" : "off"}`);
        } else {
          const nextValue = !deepResearchEnabled;
          if (nextValue) setToolFreeMode(false);
          onDeepResearchToggle?.(nextValue);
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
      onDeepResearchToggle,
      onProjectContextToggle,
      projectContextEnabled,
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
      onDeepResearchToggle?.(checked);
      requestAnimationFrame(() => textareaRef.current?.focus());
    },
    [onDeepResearchToggle, setToolFreeMode],
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
    if (atMatch) {
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

  // ファイル追加
  const addFiles = useCallback(
    (files: FileList | File[]) => {
      const incoming = Array.from(files);
      onAttachedFilesChange((prev) => {
        const accepted: File[] = [];
        let imageCount = prev.filter(isImageFile).length;
        let audioCount = prev.filter(isAudioFile).length;
        let videoCount = prev.filter(isVideoFile).length;
        for (const file of incoming) {
          if (file.size > MAX_ATTACHMENT_BYTES) {
            toast.error(
              `ファイルは 1 件 ${formatBytes(MAX_ATTACHMENT_BYTES)} までです`,
            );
            continue;
          }
          if (isImageFile(file)) {
            if (file.size > MAX_IMAGE_BYTES) {
              toast.error(`画像は1枚 ${formatBytes(MAX_IMAGE_BYTES)} までです`);
              continue;
            }
            if (imageCount >= MAX_IMAGE_ATTACHMENTS) {
              toast.error(
                `画像は最大 ${MAX_IMAGE_ATTACHMENTS} 枚まで添付できます`,
              );
              continue;
            }
            imageCount += 1;
          } else if (isVideoFile(file)) {
            if (!videoAttachmentEnabled) {
              toast.error("動画認識が無効なため動画ファイルは添付できません");
              continue;
            }
            if (file.size > videoMaxBytes) {
              toast.error(`動画は ${formatBytes(videoMaxBytes)} までです`);
              continue;
            }
            if (videoCount >= MAX_VIDEO_ATTACHMENTS) {
              toast.error("動画ファイルは1件まで添付できます");
              continue;
            }
            videoCount += 1;
          } else if (isAudioFile(file)) {
            if (!audioAttachmentEnabled) {
              toast.error("音声認識が無効なため音声ファイルは添付できません");
              continue;
            }
            if (file.size > MAX_AUDIO_BYTES) {
              toast.error(`音声は ${formatBytes(MAX_AUDIO_BYTES)} までです`);
              continue;
            }
            if (audioCount >= MAX_AUDIO_ATTACHMENTS) {
              toast.error("音声ファイルは1件まで添付できます");
              continue;
            }
            audioCount += 1;
          } else if (isOversizedMailAttachment(file)) {
            toast.error("メールファイルは 10 MB までです");
            continue;
          }
          accepted.push(file);
        }
        return accepted.length > 0 ? [...prev, ...accepted] : prev;
      });
    },
    [
      audioAttachmentEnabled,
      onAttachedFilesChange,
      videoAttachmentEnabled,
      videoMaxBytes,
    ],
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

  // クリップボード画像の貼り付け
  const handlePaste = useCallback(
    (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const items = event.clipboardData?.items;
      if (!items) return;

      const pastedImages = Array.from(items)
        .filter(
          (item) => item.kind === "file" && item.type.startsWith("image/"),
        )
        .map((item) => item.getAsFile())
        .filter((file): file is File => file !== null);

      if (pastedImages.length === 0) return;

      event.preventDefault();
      addFiles(pastedImages);
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

  return (
    <div
      className="shrink-0 border-t border-border-subtle bg-background px-4 pb-4 pt-3"
      data-chat-composer="true"
    >
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

        <ChatQuickPrompts
          sendDisabled={disabled || isSteeringMode}
          onSendPrompt={handleQuickPromptSend}
        />

        <div className="flex min-w-0 flex-wrap items-end gap-1.5 rounded-md border border-border-subtle bg-surface-charcoal p-2 xl:flex-nowrap">
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
                  // メニューが閉じてフォーカスが戻った後にpopoverを開く
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
          <VoicePanel
            conversationSessionId={sessionId}
            projectId={projectId}
            includeProjectContext={projectContextEnabled}
            characterName={runtime?.currentCharacter ?? null}
            ensureConversationSession={ensureLiveVoiceConversationSession}
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
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={handleFileSelect}
          />

          <div className="relative min-w-[min(100%,16rem)] flex-1">
            {activeCommand && !isSteeringMode && (
              <div className="mb-1 flex items-center gap-1.5">
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
              </div>
            )}
            {appContext && !isSteeringMode && (
              <div className="mb-1 flex items-center gap-1.5">
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

          {runtime && (
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
            />
          )}

          {isSteeringMode ? (
            <>
              <Button
                type="button"
                size="icon"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => handleSend()}
                disabled={!value.trim()}
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
                (isEmpty && activeCommand?.capability !== "work_intake") ||
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

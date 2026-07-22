"use client";

import {
  useState,
  useRef,
  useEffect,
  useCallback,
  useMemo,
  type Dispatch,
  type SetStateAction,
} from "react";
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
  ChevronDown,
  ChevronUp,
  CornerDownRight,
  Trash2,
  Pencil,
  Zap,
  Gauge,
} from "lucide-react";
import { MentionMenu, type MentionItem } from "@/components/chat/mention-menu";
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
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
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
import { useSnippets } from "@/contexts/snippets-context";
import { useUserSettings } from "@/contexts/user-settings-context";
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
import {
  getSettingsGenerationProfile,
  loadStoredGenerationProfile,
  saveStoredGenerationProfile,
  type GenerationProfile,
} from "@/lib/generation-profile";
import {
  HIDDEN_CHAT_SKILL_NAMES,
  completeChatCommandPrefix,
  commandCapabilitiesForActiveCommand,
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

type ChatComposerProps = {
  onSend: (
    content: string,
    files?: File[],
    mentions?: MentionItem[],
    generationProfile?: GenerationProfile,
    commandCapabilities?: ChatCommandCapability[],
    toolsRequired?: boolean,
  ) => void;
  onSteer?: (content: string) => void;
  onStop?: () => void;
  disabled: boolean;
  busy?: boolean;
  attachedFiles: File[];
  onAttachedFilesChange: Dispatch<SetStateAction<File[]>>;
  projectContextEnabled?: boolean;
  onProjectContextToggle?: (enabled: boolean) => void;
  deepResearchEnabled?: boolean;
  onDeepResearchToggle?: (enabled: boolean) => void;
  llmMode?: LlmMode;
  llmModeOptions?: LlmMode[];
  llmModeLabels?: Record<string, string>;
  onLlmModeChange?: (mode: LlmMode) => void;
  steeringInstructions?: SubmittedSteeringInstruction[];
  onClearSteeringInstructions?: () => void;
  projectId?: string | null;
  sessionId?: string | null;
  contextSnapshot?: ContextSnapshot | null;
  contextSnapshotStatus?: string;
};

export type SubmittedSteeringInstruction = {
  id: string;
  content: string;
  createdAt: string;
  status: "sending" | "queued" | "failed";
};

type QueuedChatMessage = {
  id: string;
  sessionId: string | null;
  content: string;
  generationProfile: GenerationProfile;
  mentions: MentionItem[];
  capabilities: ChatCommandCapability[];
  toolsRequired?: boolean;
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

function ContextWindowInspector({
  snapshot,
  status,
}: {
  snapshot?: ContextSnapshot | null;
  status?: string;
}) {
  const requests = snapshot?.requests?.length
    ? snapshot.requests
    : snapshot
      ? [snapshot]
      : [];
  const [selectedRequestIndex, setSelectedRequestIndex] = useState<number | null>(
    null,
  );
  const requestIndex = Math.min(
    selectedRequestIndex ?? Math.max(0, requests.length - 1),
    Math.max(0, requests.length - 1),
  );
  const current = requests[requestIndex] ?? null;
  const latest = requests[requests.length - 1] ?? null;
  const percentage = resolveContextPercentage(current);
  const latestPercentage = resolveContextPercentage(latest);
  const warning = percentage != null && percentage >= 80;
  const latestWarning = latestPercentage != null && latestPercentage >= 80;
  const remaining =
    current?.remaining_tokens ??
    (current?.context_window_tokens != null && current.input_tokens != null
      ? Math.max(0, current.context_window_tokens - current.input_tokens)
      : null);
  const categories = (current?.components ?? current?.categories ?? []).filter(
    (item) => item.tokens != null || item.preview || item.status === "deferred",
  );
  const ring =
    latestPercentage == null
      ? "conic-gradient(var(--muted-foreground) 12%, transparent 0)"
      : `conic-gradient(${latestWarning ? "var(--destructive)" : "var(--primary)"} ${latestPercentage}%, var(--muted) 0)`;

  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            className={cn(
              "h-10 shrink-0 gap-1 px-2 tabular-nums",
              latestWarning && "text-destructive",
            )}
            aria-label="コンテキストウィンドウ使用量"
            title="コンテキストウィンドウ"
          />
        }
      >
        <span
          className="grid size-5 place-items-center rounded-full"
          style={{ background: ring }}
        >
          <span className="size-3.5 rounded-full bg-card" />
        </span>
        <span className="hidden text-xs font-medium sm:inline">
          {latestPercentage == null ? "—" : `${Math.round(latestPercentage)}%`}
        </span>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="end"
        sideOffset={8}
        className="max-h-[min(70vh,36rem)] w-[min(24rem,calc(100vw-1rem))] overflow-y-auto p-3"
      >
        <div className="space-y-3">
          <div>
            <div className="flex items-center justify-between gap-3">
              <span className="font-medium">コンテキストウィンドウ</span>
              <span className="text-sm font-semibold tabular-nums">
                {current
                  ? `${formatTokens(current.input_tokens)} / ${formatTokens(current.context_window_tokens)} (${percentage == null ? "—" : `${Math.round(percentage)}%`})`
                  : "取得不能"}
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
          {requests.length > 1 && (
            <div className="flex items-center justify-between border-t pt-2 text-xs">
              <span>モデルリクエスト</span>
              <select
                className="max-w-52 rounded border bg-background px-2 py-1"
                value={requestIndex}
                onChange={(event) =>
                  setSelectedRequestIndex(Number(event.target.value))
                }
              >
                {requests.map((request, index) => (
                  <option key={request.id ?? request.request_index ?? index} value={index}>
                    {index + 1}. {request.model ?? "不明"}
                    {index === requests.length - 1 ? " (最新)" : ""}
                  </option>
                ))}
              </select>
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

function isAudioFile(file: File) {
  return (
    file.type.startsWith("audio/") ||
    /\.(wav|mp3|m4a|flac|ogg|webm)$/i.test(file.name)
  );
}

const MAX_IMAGE_ATTACHMENTS = 4;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_AUDIO_ATTACHMENTS = 1;
const MAX_AUDIO_BYTES = 25 * 1024 * 1024;

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
    <div className="group relative flex max-w-full items-center gap-2 rounded-lg border border-border bg-card p-2 pr-8 text-sm text-foreground">
      {previewUrl ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={previewUrl}
          alt={file.name}
          className="size-12 shrink-0 rounded object-cover"
        />
      ) : (
        <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-background/70 text-muted-foreground">
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

function formatInstructionTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function getInstructionStatusLabel(
  status: SubmittedSteeringInstruction["status"],
) {
  if (status === "failed") return "送信失敗";
  if (status === "sending") return "送信中";
  return "送信済み";
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

function LlmModeSelector({
  value,
  options,
  labels,
  onChange,
  open,
  onOpenChange,
  onComposerFocusRequest,
}: {
  value: LlmMode;
  options: LlmMode[];
  labels: Record<string, string>;
  onChange?: (mode: LlmMode) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onComposerFocusRequest?: () => void;
}) {
  const currentLabel = formatLlmModeLabel(value, labels);
  const disabled = !onChange || options.length <= 1;
  const lightweight = isLightweightLlmMode(value);
  const selectedItemRef = useRef<HTMLElement | null>(null);

  const focusComposer = useCallback(() => {
    requestAnimationFrame(() => onComposerFocusRequest?.());
  }, [onComposerFocusRequest]);

  useEffect(() => {
    if (!open || disabled) return;
    const timeoutId = window.setTimeout(() => {
      selectedItemRef.current?.focus();
    }, 0);
    return () => window.clearTimeout(timeoutId);
  }, [disabled, open, options, value]);

  return (
    <DropdownMenu
      open={open}
      onOpenChange={(nextOpen) => {
        onOpenChange(nextOpen);
        if (!nextOpen) focusComposer();
      }}
    >
      <DropdownMenuTrigger
        render={
          <Button
            variant={lightweight ? "outline" : "secondary"}
            size="default"
            className={cn(
              "min-w-[6.75rem] max-w-[8.5rem] justify-start px-2 text-xs",
              !lightweight && "border-primary/40 text-primary shadow-sm",
            )}
            disabled={disabled}
            title={`LLM mode: ${currentLabel} (Ctrl+Shift+M)`}
            aria-label="LLM mode"
          />
        }
      >
        <LlmModeIcon mode={value} className="size-4" />
        <span className="min-w-0 flex-1 truncate text-left">
          {currentLabel}
        </span>
        <ChevronUp className="size-3 text-muted-foreground" />
      </DropdownMenuTrigger>

      <DropdownMenuContent
        side="top"
        sideOffset={8}
        align="start"
        className="w-64"
      >
        <DropdownMenuRadioGroup
          value={value}
          onValueChange={(nextMode) => {
            if (nextMode !== value) onChange?.(nextMode);
            onOpenChange(false);
            focusComposer();
          }}
        >
          {options.map((mode) => {
            const modeLightweight = isLightweightLlmMode(mode);
            return (
              <DropdownMenuRadioItem
                key={mode}
                ref={(node) => {
                  if (mode === value) selectedItemRef.current = node;
                }}
                value={mode}
                closeOnClick
                onClick={focusComposer}
                className="items-start gap-2 py-1.5"
              >
                <LlmModeIcon
                  mode={mode}
                  className={cn(
                    "mt-0.5 size-4 shrink-0",
                    !modeLightweight && "text-primary",
                  )}
                />
                <div className="flex min-w-0 flex-col">
                  <span className="truncate text-sm font-medium">
                    {formatLlmModeLabel(mode, labels)}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {getLlmModeDescription(mode)}
                  </span>
                </div>
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function SteeringInstructionPreview({
  instructions,
  onClear,
}: {
  instructions: SubmittedSteeringInstruction[];
  onClear?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const latest = instructions.at(-1);
  if (!latest) return null;

  const countLabel =
    instructions.length > 1 ? `追加指示 ${instructions.length}件` : "追加指示";

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-muted text-xs shadow-sm">
      <div className="flex min-h-9 items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left text-muted-foreground transition-colors hover:text-foreground"
          aria-expanded={expanded}
          title={latest.content}
        >
          <CornerDownRight className="size-3.5 shrink-0" />
          <span className="shrink-0 font-medium">{countLabel}</span>
          <span className="min-w-0 flex-1 truncate text-foreground/85">
            {latest.content}
          </span>
          {expanded ? (
            <ChevronDown className="size-3.5 shrink-0" />
          ) : (
            <ChevronUp className="size-3.5 shrink-0" />
          )}
        </button>
        {onClear && (
          <button
            type="button"
            onClick={onClear}
            className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-background/70 hover:text-foreground"
            aria-label="追加指示の表示を消す"
            title="追加指示の表示を消す"
          >
            <Trash2 className="size-3.5" />
          </button>
        )}
      </div>

      {expanded && (
        <div className="max-h-32 space-y-1 overflow-y-auto border-t border-border/60 px-3 py-2">
          {instructions.map((item) => (
            <div
              key={item.id}
              className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-2"
            >
              <CornerDownRight className="mt-0.5 size-3 shrink-0 text-muted-foreground" />
              <div className="whitespace-pre-wrap break-words text-foreground/85">
                {item.content}
              </div>
              <div className="shrink-0 text-[10px] text-muted-foreground">
                {formatInstructionTime(item.createdAt)}
                <span className="ml-1">
                  {getInstructionStatusLabel(item.status)}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ChatComposer({
  onSend,
  onSteer,
  onStop,
  disabled,
  busy = false,
  attachedFiles,
  onAttachedFilesChange,
  projectContextEnabled = false,
  onProjectContextToggle,
  deepResearchEnabled = false,
  onDeepResearchToggle,
  llmMode,
  llmModeOptions = [],
  llmModeLabels = {},
  onLlmModeChange,
  steeringInstructions = [],
  onClearSteeringInstructions,
  projectId,
  sessionId,
  contextSnapshot,
  contextSnapshotStatus,
}: ChatComposerProps) {
  const [value, setValue] = useState("");
  const [messageQueue, setMessageQueue] = useState<QueuedChatMessage[]>([]);
  const queueSuppressedRef = useRef(false);
  const prevBusyRef = useRef(busy);
  const [showSlashMenu, setShowSlashMenu] = useState(false);
  const [slashSelectionIndex, setSlashSelectionIndex] = useState(0);
  const [skillCommands, setSkillCommands] = useState<SkillSlashCommand[]>([]);
  const [activeCommand, setActiveCommand] = useState<ActiveChatCommand | null>(
    null,
  );
  const skillsFetchedRef = useRef(false);
  const [isDragOver, setIsDragOver] = useState(false);
  const [showMentionMenu, setShowMentionMenu] = useState(false);
  const [mentionQuery, setMentionQuery] = useState("");
  const [mentions, setMentions] = useState<MentionItem[]>([]);
  const [localGenerationProfile, setLocalGenerationProfile] =
    useState<GenerationProfile>(() =>
      loadStoredGenerationProfile(
        typeof window === "undefined" ? null : window.localStorage,
      ),
    );
  const [generationProfileChangedByUser, setGenerationProfileChangedByUser] =
    useState(false);
  const [generationProfileMenuOpen, setGenerationProfileMenuOpen] =
    useState(false);
  const [llmModeMenuOpen, setLlmModeMenuOpen] = useState(false);
  const [toolsMenuOpen, setToolsMenuOpen] = useState(false);
  const [toolFreeMode, setToolFreeMode] = useState(false);
  const [audioAttachmentEnabled, setAudioAttachmentEnabled] = useState(true);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const slashMenuRef = useRef<HTMLDivElement>(null);
  useMarkdownShortcuts(textareaRef);
  const { snippets } = useSnippets();
  const { settings: userSettings, patch: patchUserSettings } =
    useUserSettings();
  const { state: snippetState } = useSnippetAutocomplete(textareaRef, snippets);
  const settingsGenerationProfile = useMemo(
    () => getSettingsGenerationProfile(userSettings),
    [userSettings],
  );
  const generationProfile =
    generationProfileChangedByUser || !settingsGenerationProfile
      ? localGenerationProfile
      : settingsGenerationProfile;

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
        if (!cancelled) setAudioAttachmentEnabled(engine !== "off");
      } catch {
        if (!cancelled) setAudioAttachmentEnabled(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const isSteeringMode = busy;
  const isEmpty =
    value.trim().length === 0 && !isSteeringMode && attachedFiles.length === 0;
  const effectiveLlmModeOptions =
    llmModeOptions.length > 0 ? llmModeOptions : llmMode ? [llmMode] : [];
  const effectiveLlmMode =
    llmMode && effectiveLlmModeOptions.includes(llmMode)
      ? llmMode
      : (effectiveLlmModeOptions[0] ?? "");
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
    projectContextEnabled || deepResearchEnabled || webSearchActive || toolFreeMode;

  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!settingsGenerationProfile) return;
    saveStoredGenerationProfile(window.localStorage, settingsGenerationProfile);
  }, [settingsGenerationProfile]);

  const handleGenerationProfileChange = useCallback(
    (nextProfile: GenerationProfile) => {
      setGenerationProfileChangedByUser(true);
      setLocalGenerationProfile(nextProfile);
      saveStoredGenerationProfile(window.localStorage, nextProfile);
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
        setLlmModeMenuOpen(true);
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

  // 生成完了(busy true→false)でキュー先頭を1件ずつ通常送信する
  useEffect(() => {
    const wasBusy = prevBusyRef.current;
    prevBusyRef.current = busy;
    if (
      wasBusy &&
      !busy &&
      !queueSuppressedRef.current &&
      messageQueue.length > 0
    ) {
      const [next, ...rest] = messageQueue;
      // セッション切り替えと同一レンダーで busy が false になった場合、
      // 旧セッションのキューを現行セッションへ誤送信しないようガードする。
      // 不一致なら送らず、後続のクリア effect に破棄させる。
      if (next.sessionId !== (sessionId ?? null)) return;
      queueMicrotask(() => {
        setMessageQueue(rest);
        onSend(
          next.content,
          undefined,
          next.mentions.length ? next.mentions : undefined,
          next.generationProfile,
          next.capabilities.length ? next.capabilities : undefined,
          next.toolsRequired,
        );
      });
    }
  }, [busy, messageQueue, onSend, sessionId]);

  // セッション切り替えでキューを破棄する（value はクリアしない）
  useEffect(() => {
    const clearTimer = window.setTimeout(() => setMessageQueue([]), 0);
    return () => window.clearTimeout(clearTimer);
  }, [sessionId]);

  useEffect(() => {
    let cancelled = false;
    skillsFetchedRef.current = true;
    fetchSkillSlashCommands(projectId)
      .then((commands) => {
        if (!cancelled) setSkillCommands(commands);
      })
      .catch((err) => {
        console.warn("スキル一覧の取得に失敗:", err);
        if (!cancelled) skillsFetchedRef.current = false;
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  useEffect(() => {
    if (!showSlashMenu || selectedSlashMenuIndex < 0) return;
    const el = slashMenuRef.current?.querySelector<HTMLElement>(
      `[data-slash-command-index="${selectedSlashMenuIndex}"]`,
    );
    el?.scrollIntoView({ block: "nearest" });
  }, [selectedSlashMenuIndex, showSlashMenu]);

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
    setMessageQueue((prev) => [
      ...prev,
      {
        id: createQueueId(),
        sessionId: sessionId ?? null,
        content: submission.content,
        generationProfile,
        mentions,
        capabilities: submission.capabilities ?? [],
        toolsRequired: resolveChatToolsRequired(
          submission.capabilities,
          toolFreeMode,
        ),
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
  }, [value, activeCommand, generationProfile, mentions, sessionId, toolFreeMode]);

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
      if (isEmpty || disabled) return;
      queueSuppressedRef.current = false;
      onSend(
        submission.content,
        attachedFiles.length > 0 ? attachedFiles : undefined,
        mentions.length > 0 ? mentions : undefined,
        generationProfile,
        submission.capabilities,
        resolveChatToolsRequired(submission.capabilities, toolFreeMode),
      );
      setValue("");
      setActiveCommand(null);
      setToolFreeMode(false);
      onAttachedFilesChange([]);
      setMentions([]);
      requestAnimationFrame(() => {
        if (textareaRef.current) {
          textareaRef.current.style.height = "auto";
          textareaRef.current.focus();
        }
      });
    },
    [
      value,
      isEmpty,
      disabled,
      isSteeringMode,
      onSend,
      onSteer,
      attachedFiles,
      mentions,
      generationProfile,
      activeCommand,
      onAttachedFilesChange,
      enqueue,
      toolFreeMode,
    ],
  );

  const editQueuedMessage = useCallback((item: QueuedChatMessage) => {
    setValue((cur) =>
      cur.trim() ? `${cur.replace(/\s+$/, "")}\n${item.content}` : item.content,
    );
    setMentions((prev) => [...prev, ...item.mentions]);
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
  }, []);

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
    ],
  );

  const applySkillCommand = useCallback((command: SkillSlashCommand) => {
    setValue(`${command.command} `);
    setShowSlashMenu(false);
    setSlashSelectionIndex(0);
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, []);

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
    [onDeepResearchToggle],
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
    [searchCommand, webSearchActive],
  );

  const handleToolsMenuOpenChange = useCallback((open: boolean) => {
    setToolsMenuOpen(open);
    if (!open) requestAnimationFrame(() => textareaRef.current?.focus());
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
  }, [firstMatchingSkillCommand, showSlashMenu, skillCommands, value]);

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
        e.currentTarget.selectionStart === value.length &&
        e.currentTarget.selectionEnd === value.length)
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
    [loadSkillCommands],
  );

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const newValue = e.target.value;
    setValue(newValue);

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
    const cursorPos = e.target.selectionStart;
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

      // @[[type:id:name]] 形式に置換
      const mentionToken = `@[[${item.type}:${item.id}:${item.name}]]`;
      const newValue =
        value.slice(0, atIndex) + mentionToken + " " + value.slice(cursorPos);
      setValue(newValue);
      setMentions((prev) => [...prev, item]);
      setShowMentionMenu(false);
      setMentionQuery("");
      textareaRef.current?.focus();
    },
    [value],
  );

  // ファイル追加
  const addFiles = useCallback(
    (files: FileList | File[]) => {
      const incoming = Array.from(files);
      onAttachedFilesChange((prev) => {
        const accepted: File[] = [];
        let imageCount = prev.filter(isImageFile).length;
        let audioCount = prev.filter(isAudioFile).length;
        for (const file of incoming) {
          if (isImageFile(file)) {
            if (file.size > MAX_IMAGE_BYTES) {
              toast.error(
                `画像は1枚 ${formatBytes(MAX_IMAGE_BYTES)} までです`,
              );
              continue;
            }
            if (imageCount >= MAX_IMAGE_ATTACHMENTS) {
              toast.error(
                `画像は最大 ${MAX_IMAGE_ATTACHMENTS} 枚まで添付できます`,
              );
              continue;
            }
            imageCount += 1;
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
    [audioAttachmentEnabled, onAttachedFilesChange],
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
    <div className="border-t border-border bg-card p-4">
      <div className="mx-auto max-w-3xl space-y-2 transition-transform duration-200 ease-linear xl:translate-x-[var(--chat-viewport-offset)]">
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

        {steeringInstructions.length > 0 && (
          <SteeringInstructionPreview
            instructions={steeringInstructions}
            onClear={onClearSteeringInstructions}
          />
        )}

        {messageQueue.length > 0 && (
          <div className="overflow-hidden rounded-xl border border-border bg-muted text-xs shadow-sm">
            <div className="flex min-h-9 items-center gap-2 border-b border-border/60 px-3 py-2 font-medium text-muted-foreground">
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

        <div className="flex items-end gap-2">
          <GenerationProfileSelector
            value={generationProfile}
            onChange={handleGenerationProfileChange}
            open={generationProfileMenuOpen}
            onOpenChange={setGenerationProfileMenuOpen}
            onComposerFocusRequest={() => textareaRef.current?.focus()}
          />

          {effectiveLlmModeOptions.length > 0 && (
            <LlmModeSelector
              value={effectiveLlmMode}
              options={effectiveLlmModeOptions}
              labels={llmModeLabels}
              onChange={onLlmModeChange}
              open={llmModeMenuOpen}
              onOpenChange={setLlmModeMenuOpen}
              onComposerFocusRequest={() => textareaRef.current?.focus()}
            />
          )}

          <DropdownMenu
            open={toolsMenuOpen}
            onOpenChange={handleToolsMenuOpenChange}
          >
            <DropdownMenuTrigger
              render={
                <Button
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
                onCheckedChange={(checked) =>
                  setToolFreeMode(checked === true)
                }
                className="gap-2 py-1.5"
              >
                <Gauge className="size-4" />
                <span>ツールなし（無料枠優先）</span>
              </DropdownMenuCheckboxItem>
              <DropdownMenuSeparator />
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
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept="image/*,audio/*,.txt,.md,.markdown,.csv,.tsv,.json,.yaml,.yml,.xml,.log,.msg,.eml,application/vnd.ms-outlook,message/rfc822"
            className="hidden"
            onChange={handleFileSelect}
          />

          <div className="relative flex-1">
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
                />
              </div>
            )}

            <textarea
              ref={textareaRef}
              value={value}
              onChange={handleChange}
              onKeyDown={handleKeyDown}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              placeholder={
                isSteeringMode
                  ? "生成中に追加する指示を入力..."
                  : composerPlaceholder
              }
              rows={1}
              className={cn(
                "flex w-full resize-none rounded-xl border border-input bg-card px-3 py-2.5 text-sm text-foreground transition-colors outline-none",
                "placeholder:text-muted-foreground",
                "focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
                isDragOver && "border-primary ring-3 ring-primary/50",
              )}
              style={{ height: "auto", minHeight: "40px", maxHeight: "120px" }}
            />
          </div>

          <ContextWindowInspector
            key={contextSnapshot?.message_id ?? contextSnapshot?.captured_at ?? "context-unavailable"}
            snapshot={contextSnapshot}
            status={contextSnapshotStatus}
          />

          {isSteeringMode && onStop ? (
            <Button
              type="button"
              variant="destructive"
              size="icon"
              onClick={() => {
                queueSuppressedRef.current = true;
                onStop();
              }}
              className="shrink-0"
              title="応答生成を停止"
            >
              <Square className="size-4" />
            </Button>
          ) : (
            <Button
              size="icon"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => handleSend()}
              disabled={
                (isEmpty &&
                  activeCommand?.capability !== "docs_ingest" &&
                  activeCommand?.capability !== "work_intake") ||
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

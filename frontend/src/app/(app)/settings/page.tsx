"use client";

import {
  useState,
  useEffect,
  useCallback,
  useRef,
  type ReactNode,
} from "react";
import useSWR from "swr";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/hooks/use-confirm";
import {
  Bell,
  AudioLines,
  Bug,
  CalendarDays,
  ChevronDown,
  ChevronUp,
  CircleHelp,
  Database,
  KeyRound,
  Keyboard,
  Loader2,
  Lock,
  MessageSquareText,
  Music,
  Plug,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Smartphone,
  UserCog,
} from "lucide-react";
import { LoginHistorySection } from "@/components/settings/login-history-section";
import { FeedbackSection } from "@/components/settings/feedback-section";
import { SkillsSection } from "@/components/settings/skills-section";
import { KnowledgeSourcesSection } from "@/components/settings/knowledge-sources-section";
import { ClipIngestTargetsSection } from "@/components/settings/clip-ingest-targets-section";
import { UserExportSection } from "@/components/settings/user-export-section";
import { CharactersSection } from "@/components/settings/characters-section";
import { McpSection } from "@/components/settings/mcp-section";
import { CostDashboardSection } from "@/components/settings/cost-dashboard-section";
import { MemorySection } from "@/components/settings/memory-section";
import { ComfyUISection } from "@/components/settings/comfyui-section";
import { SnippetsSection } from "@/components/settings/snippets-section";
import { GoogleCalendarSection } from "@/components/settings/google-calendar-section";
import { RemoteServerSection } from "@/components/settings/remote-server-section";
import { UserManagementConsole } from "@/components/settings/user-management-console";
import { HeartbeatsSection } from "@/components/settings/heartbeats-section";
import { LlmModelSection } from "@/components/settings/llm-model-section";
import { SearchSettingsSection } from "@/components/settings/search-settings-section";
import { SpotifySection } from "@/components/settings/spotify-section";
import { EditorSettingsSection } from "@/components/settings/editor-settings-section";
import { YomiLinterSection } from "@/components/settings/yomi-linter-section";
import { NavigationTabsSection } from "@/components/settings/navigation-tabs-section";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";
import { getTaskNotificationsDefaultEnabled } from "@/lib/user-settings";
import { useUserSettings } from "@/contexts/user-settings-context";
import { serializeAudioPlayerSettings } from "@/lib/audio-player-settings";

async function pyFetch<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
  return res.json();
}

async function apiFetch<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

interface SettingsResponse {
  agents?: Record<string, { enabled: boolean; description?: string }>;
  [key: string]: unknown;
}

interface CrawlerInfo {
  name: string;
  status: string;
  type: string;
  is_alive: boolean;
  error?: string;
}

interface CrawlerStatusResponse {
  crawlers: CrawlerInfo[];
}

interface MobileCommand {
  id: string;
  label: string;
  hint?: string;
  icon?: string;
  accent?: string;
  requires_confirmation?: boolean;
}

interface MobileCommandsResponse {
  enabled: boolean;
  commands: MobileCommand[];
}

interface AuthStatus {
  authenticated: boolean;
  user?: {
    id: string;
    username: string;
    role: string | null;
    display_name: string | null;
    password_reset_required?: boolean | null;
    user_settings?: Record<string, unknown>;
  };
}

const AGENT_LABELS: Record<string, string> = {
  filesystem: "ファイル直接ツール",
  project_management: "プロジェクトDB直接ツール",
};

const AGENT_DESCRIPTIONS: Record<string, string> = {
  filesystem: "メインassistantがワークスペースの検索・読取・編集ツールを直接使う権限です。",
  project_management: "メインassistantが案件情報Docs・WBS・タスク系の直接ツールを使う権限です。",
};

type QuickSettingId =
  | "task-notifications"
  | "web-search"
  | "user-memory"
  | "google-calendar"
  | "snippets"
  | "integrations"
  | "custom-instructions"
  | "characters"
  | "llm-model"
  | "embed-card"
  | "mobile-commands"
  | "knowledge-sources"
  | "crawler-status"
  | "tool-permissions"
  | "account-security";

type QuickSettingDefinition = {
  id: QuickSettingId;
  href: string;
  label: string;
  icon: typeof Bell;
};

const QUICK_SETTING_REGISTRY: QuickSettingDefinition[] = [
  { id: "task-notifications", href: "#notifications", label: "タスク通知", icon: Bell },
  { id: "web-search", href: "#knowledge", label: "検索プロバイダ", icon: Search },
  { id: "user-memory", href: "#conversation", label: "Dreamingメモリ", icon: Database },
  { id: "google-calendar", href: "#notifications", label: "Google Calendar", icon: CalendarDays },
  { id: "snippets", href: "#input", label: "スニペット", icon: Keyboard },
  { id: "integrations", href: "#integrations", label: "外部連携", icon: Plug },
  { id: "custom-instructions", href: "#conversation", label: "会話カスタム指示", icon: MessageSquareText },
  { id: "characters", href: "#conversation", label: "キャラクター", icon: MessageSquareText },
  { id: "llm-model", href: "#conversation", label: "言語モデル", icon: MessageSquareText },
  { id: "embed-card", href: "#input", label: "埋め込みカード", icon: Keyboard },
  { id: "mobile-commands", href: "#input", label: "モバイルコマンド", icon: Smartphone },
  { id: "knowledge-sources", href: "#knowledge", label: "ナレッジソース", icon: Database },
  { id: "crawler-status", href: "#knowledge", label: "クローラーステータス", icon: Bug },
  { id: "tool-permissions", href: "#tool-permissions", label: "ツール権限", icon: ShieldCheck },
  { id: "account-security", href: "#account", label: "アカウント", icon: KeyRound },
];

const DEFAULT_QUICK_SETTING_IDS: QuickSettingId[] = [
  "task-notifications",
  "web-search",
  "user-memory",
  "google-calendar",
  "snippets",
  "integrations",
];

function getQuickSettingDefinition(id: QuickSettingId) {
  return QUICK_SETTING_REGISTRY.find((item) => item.id === id);
}

function normalizeQuickSettingIds(value: unknown): QuickSettingId[] {
  if (!Array.isArray(value)) return DEFAULT_QUICK_SETTING_IDS;
  const validIds = new Set(QUICK_SETTING_REGISTRY.map((item) => item.id));
  const normalized: QuickSettingId[] = [];
  for (const id of value) {
    if (
      typeof id === "string" &&
      validIds.has(id as QuickSettingId) &&
      !normalized.includes(id as QuickSettingId)
    ) {
      normalized.push(id as QuickSettingId);
    }
  }
  return normalized;
}

function getQuickSettingIds(settings: Record<string, unknown>): QuickSettingId[] {
  const quickSettings = settings.quick_settings;
  if (
    typeof quickSettings !== "object" ||
    quickSettings === null ||
    Array.isArray(quickSettings)
  ) {
    return DEFAULT_QUICK_SETTING_IDS;
  }
  return normalizeQuickSettingIds(
    (quickSettings as Record<string, unknown>).pins,
  );
}

function SettingsGroup({
  id,
  title,
  icon,
  children,
}: {
  id: string;
  title: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-4 space-y-3">
      <div className="flex items-center gap-2 border-b pb-2">
        {icon}
        <h2 className="text-sm font-semibold">{title}</h2>
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

function QuickSettings() {
  const { settings, patch } = useUserSettings();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const selectedIds = getQuickSettingIds(settings);
  const selectedItems = selectedIds
    .map(getQuickSettingDefinition)
    .filter((item): item is QuickSettingDefinition => Boolean(item));

  const saveSelectedIds = async (nextIds: QuickSettingId[]) => {
    setSaving(true);
    try {
      await patch({ quick_settings: { pins: nextIds } });
    } finally {
      setSaving(false);
    }
  };

  const addItem = (id: QuickSettingId) => {
    if (selectedIds.includes(id)) return;
    void saveSelectedIds([...selectedIds, id]);
  };

  const removeItem = (id: QuickSettingId) => {
    void saveSelectedIds(selectedIds.filter((value) => value !== id));
  };

  const moveItem = (id: QuickSettingId, direction: -1 | 1) => {
    const index = selectedIds.indexOf(id);
    const nextIndex = index + direction;
    if (index < 0 || nextIndex < 0 || nextIndex >= selectedIds.length) return;
    const next = [...selectedIds];
    [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    void saveSelectedIds(next);
  };

  const availableItems = QUICK_SETTING_REGISTRY.filter(
    (item) => !selectedIds.includes(item.id),
  );

  return (
    <SettingsDisclosure title="よく使う設定">
        <div className="flex justify-end">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setEditing((value) => !value)}
          >
            <SlidersHorizontal className="mr-1 size-3" />
            {editing ? "完了" : "編集"}
          </Button>
        </div>
        {selectedItems.length > 0 ? (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {selectedItems.map(({ id, href, label, icon: Icon }) => (
              <a
                key={id}
                href={href}
                className="inline-flex h-7 items-center justify-start gap-1 rounded-md border bg-background px-2.5 text-[0.8rem] font-medium transition-colors hover:bg-muted"
              >
                <Icon className="size-4" />
                {label}
              </a>
            ))}
          </div>
        ) : (
          <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
            よく使う設定は未選択です。
          </p>
        )}
        {editing && (
          <div className="space-y-3 rounded-md border p-3">
            <div className="space-y-2">
              <p className="text-xs font-medium text-muted-foreground">
                表示中
              </p>
              {selectedItems.length > 0 ? (
                <div className="space-y-2">
                  {selectedItems.map(({ id, label, icon: Icon }, index) => (
                    <div
                      key={id}
                      className="flex items-center justify-between gap-2 rounded border px-2 py-1.5"
                    >
                      <span className="flex min-w-0 items-center gap-2 text-sm">
                        <Icon className="size-4 shrink-0" />
                        <span className="truncate">{label}</span>
                      </span>
                      <div className="flex shrink-0 items-center gap-1">
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-xs"
                          disabled={saving || index === 0}
                          onClick={() => moveItem(id, -1)}
                          title="上へ"
                        >
                          <ChevronUp className="size-3" />
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-xs"
                          disabled={saving || index === selectedItems.length - 1}
                          onClick={() => moveItem(id, 1)}
                          title="下へ"
                        >
                          <ChevronDown className="size-3" />
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="xs"
                          disabled={saving}
                          onClick={() => removeItem(id)}
                        >
                          外す
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  表示中の項目はありません。
                </p>
              )}
            </div>
            <div className="space-y-2">
              <p className="text-xs font-medium text-muted-foreground">
                追加できる項目
              </p>
              {availableItems.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {availableItems.map(({ id, label, icon: Icon }) => (
                    <Button
                      key={id}
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={saving}
                      onClick={() => addItem(id)}
                    >
                      <Icon className="mr-1 size-3" />
                      {label}
                    </Button>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  追加できる項目はありません。
                </p>
              )}
            </div>
          </div>
        )}
    </SettingsDisclosure>
  );
}

function ToolPermissionsCard({
  loading,
  settings,
  onToggle,
}: {
  loading: boolean;
  settings: SettingsResponse | null;
  onToggle: (agentKey: string, enabled: boolean) => void;
}) {
  const agents = settings?.agents;

  return (
    <SettingsDisclosure title="ツール権限" icon={<ShieldCheck className="size-4" />}>
        {loading ? (
          <div className="space-y-3">
            {Array.from({ length: 2 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full rounded" />
            ))}
          </div>
        ) : agents ? (
          <div className="space-y-3">
            {Object.entries(AGENT_LABELS).map(([key, label]) => {
              const agent = agents[key];
              const enabled = agent?.enabled === true;
              return (
                <div key={key}>
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex min-w-0 items-center gap-3">
                      <Checkbox
                        checked={enabled}
                        onCheckedChange={(checked) =>
                          onToggle(key, checked === true)
                        }
                      />
                      <div className="min-w-0">
                        <p className="text-sm font-medium">{label}</p>
                        <p className="text-xs text-muted-foreground">
                          {agent?.description || AGENT_DESCRIPTIONS[key]}
                        </p>
                      </div>
                    </div>
                    <Badge variant={enabled ? "default" : "secondary"}>
                      {enabled ? "許可" : "未許可"}
                    </Badge>
                  </div>
                  <Separator className="mt-3" />
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            ツール権限を取得できませんでした
          </p>
        )}
    </SettingsDisclosure>
  );
}

function McpEnableCard({
  loading,
  enabled,
  onToggle,
}: {
  loading: boolean;
  enabled: boolean | null;
  onToggle: (enabled: boolean) => void;
}) {
  return (
    <SettingsDisclosure
      title="MCP"
      icon={<Plug className="size-4" />}
      summary={
          <Badge variant={enabled ? "default" : "secondary"}>
            {enabled === null ? "未取得" : enabled ? "ON" : "OFF"}
          </Badge>
      }
    >
        {loading ? (
          <Skeleton className="h-10 w-full rounded" />
        ) : (
          <div className="flex items-start justify-between gap-3 rounded border p-3">
            <div className="space-y-1">
              <Label htmlFor="mcp-enabled" className="text-sm font-medium">
                MCPを有効化
              </Label>
              <p className="text-xs text-muted-foreground">
                MCP連携と登録済みMCPサーバーの利用を切り替えます。
              </p>
            </div>
            <Checkbox
              id="mcp-enabled"
              checked={enabled === true}
              onCheckedChange={(checked) => onToggle(checked === true)}
            />
          </div>
        )}
    </SettingsDisclosure>
  );
}

function AudioPlayerSettingsCard() {
  const { audioPlayerSettings, patch } = useUserSettings();
  const [saving, setSaving] = useState(false);
  const scopeLabel =
    audioPlayerSettings.playbackScope === "global_next"
      ? "フォルダ跨ぎ"
      : "同一フォルダ";
  const flags = [
    audioPlayerSettings.shuffle ? "シャッフル" : null,
    audioPlayerSettings.repeatOne ? "1曲リピート" : null,
  ].filter(Boolean);

  const save = async (patchValue: Partial<typeof audioPlayerSettings>) => {
    setSaving(true);
    try {
      const next = { ...audioPlayerSettings, ...patchValue };
      await patch({ audio_player: serializeAudioPlayerSettings(next) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <SettingsDisclosure
      title="音楽プレイヤー"
      icon={<Music className="size-4" />}
      summary={
        <Badge variant="secondary">
          {flags.length > 0
            ? `${scopeLabel} / ${flags.join(" / ")}`
            : scopeLabel}
        </Badge>
      }
    >
      <div className="space-y-2">
        <Label className="text-xs text-muted-foreground">再生範囲</Label>
        <div className="grid gap-2 sm:grid-cols-2">
          <Button
            type="button"
            variant={
              audioPlayerSettings.playbackScope === "folder_loop"
                ? "default"
                : "outline"
            }
            disabled={saving}
            onClick={() => save({ playbackScope: "folder_loop" })}
          >
            同じフォルダでループ
          </Button>
          <Button
            type="button"
            variant={
              audioPlayerSettings.playbackScope === "global_next"
                ? "default"
                : "outline"
            }
            disabled={saving}
            onClick={() => save({ playbackScope: "global_next" })}
          >
            フォルダを跨いで続ける
          </Button>
        </div>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="flex items-start gap-3 rounded border p-3">
          <Checkbox
            checked={audioPlayerSettings.shuffle}
            disabled={saving}
            onCheckedChange={(checked) => save({ shuffle: checked === true })}
          />
          <span>
            <span className="block text-sm font-medium">シャッフル</span>
            <span className="text-xs text-muted-foreground">
              次曲を再生範囲内からランダムに選びます。
            </span>
          </span>
        </label>
        <label className="flex items-start gap-3 rounded border p-3">
          <Checkbox
            checked={audioPlayerSettings.repeatOne}
            disabled={saving}
            onCheckedChange={(checked) => save({ repeatOne: checked === true })}
          />
          <span>
            <span className="block text-sm font-medium">1曲リピート</span>
            <span className="text-xs text-muted-foreground">
              曲終了時に同じ曲をもう一度再生します。
            </span>
          </span>
        </label>
      </div>
    </SettingsDisclosure>
  );
}

// SWR共通オプション。取得タイミングは従来どおり呼び出し側（fetchAll / fetchCrawlers /
// 各操作後）で駆動するため、自動 revalidation は全て無効化する。
const SETTINGS_SWR_OPTIONS = {
  revalidateOnMount: false,
  revalidateOnFocus: false,
  revalidateOnReconnect: false,
  revalidateIfStale: false,
  keepPreviousData: true,
  dedupingInterval: 0,
} as const;

export default function SettingsPage() {
  const confirm = useConfirm();
  const [loading, setLoading] = useState(true);
  // 独立したサーバー状態（settings / mobileCommands / crawlers）は SWR で管理する。
  // currentUser は編集ドラフトの初期値を供給し、各保存操作で楽観的に patch される
  // ハイブリッド状態のため従来の useState のまま扱う。
  const settingsRef = useRef<SettingsResponse | null>(null);
  const { data: settings = null, mutate: mutateSettings } = useSWR<SettingsResponse | null>(
    "settings/page-settings",
    async () => {
      try {
        const raw = (await pyFetch<SettingsResponse>("/settings")) as Record<
          string,
          unknown
        >;
        return (raw.settings ?? raw) as SettingsResponse;
      } catch {
        return settingsRef.current;
      }
    },
    SETTINGS_SWR_OPTIONS,
  );
  settingsRef.current = settings;
  const { data: mobileCommands = null, mutate: mutateMobileCommands } =
    useSWR<MobileCommandsResponse | null>(
      "settings/mobile-commands",
      async () => {
        try {
          return await pyFetch<MobileCommandsResponse>("/mobile/commands");
        } catch {
          return null;
        }
      },
      SETTINGS_SWR_OPTIONS,
    );
  const { data: crawlers = null, mutate: mutateCrawlers } = useSWR<
    CrawlerInfo[] | null
  >(
    "settings/crawler-status",
    async () => {
      try {
        return (await pyFetch<CrawlerStatusResponse>("/crawler/status")).crawlers;
      } catch {
        return [];
      }
    },
    SETTINGS_SWR_OPTIONS,
  );
  const [runningCommandId, setRunningCommandId] = useState<string | null>(null);
  const [currentUser, setCurrentUser] = useState<AuthStatus["user"] | null>(
    null,
  );

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordLoading, setPasswordLoading] = useState(false);
  const [passwordMessage, setPasswordMessage] = useState("");
  const [passwordError, setPasswordError] = useState("");

  const [restartShortcutEnabled, setRestartShortcutEnabled] = useState(false);
  const [
    taskNotificationsDefaultEnabled,
    setTaskNotificationsDefaultEnabled,
  ] = useState(true);
  const [taskNotificationMinutesBefore, setTaskNotificationMinutesBefore] =
    useState("5");
  const [taskNotificationSaving, setTaskNotificationSaving] = useState(false);
  const [customInstructions, setCustomInstructions] = useState("");
  const [customInstructionsSaving, setCustomInstructionsSaving] =
    useState(false);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      // settings / mobileCommands は SWR fetcher が取得と失敗時フォールバックを担う。
      // authStatus は currentUser とドラフト初期値の供給のため従来どおり直接取得する。
      const results = await Promise.allSettled([
        mutateSettings(),
        mutateMobileCommands(),
        apiFetch<AuthStatus>("/api/auth/status"),
      ]);

      if (results[2].status === "fulfilled" && results[2].value.authenticated) {
        const u = results[2].value.user || null;
        setCurrentUser(u);
        if (u?.user_settings) {
          setRestartShortcutEnabled(
            u.user_settings.restart_shortcut_enabled === true,
          );
          setTaskNotificationsDefaultEnabled(
            getTaskNotificationsDefaultEnabled(u.user_settings),
          );
          const minutes = u.user_settings.task_notification_minutes_before;
          setTaskNotificationMinutesBefore(
            typeof minutes === "number" || typeof minutes === "string"
              ? String(minutes)
              : "5",
          );
          setCustomInstructions(
            typeof u.user_settings.custom_instructions === "string"
              ? u.user_settings.custom_instructions
              : "",
          );
        }
      }
    } catch (err) {
      console.error("設定取得失敗:", err);
    } finally {
      setLoading(false);
    }
  }, [mutateSettings, mutateMobileCommands]);

  const fetchCrawlers = useCallback(async () => {
    // 取得前に一旦未取得状態（null）へ戻してから再取得する（従来挙動を維持）。
    await mutateCrawlers(null, { revalidate: false });
    await mutateCrawlers();
  }, [mutateCrawlers]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const handleRunMobileCommand = useCallback(
    async (commandId: string, requiresConfirmation?: boolean) => {
      if (
        requiresConfirmation &&
        !(await confirm({
          description: `コマンド「${commandId}」を実行しますか？`,
        }))
      ) {
        return;
      }
      setRunningCommandId(commandId);
      try {
        await pyFetch("/mobile/commands/run", {
          method: "POST",
          body: JSON.stringify({ command_id: commandId }),
        });
      } catch (err) {
        console.error("コマンド実行失敗:", err);
      } finally {
        setRunningCommandId(null);
      }
    },
    [confirm],
  );

  const handleAgentToggle = useCallback(
    async (agentKey: string, enabled: boolean) => {
      const settingKey =
        agentKey === "mcp" ? "mcp_enabled" : `agents.${agentKey}.enabled`;
      try {
        await pyFetch("/settings", {
          method: "PATCH",
          body: JSON.stringify({
            key: settingKey,
            value: enabled,
          }),
        });
        // 楽観的更新：保存成功後は再取得せずローカルキャッシュの agents を更新する。
        await mutateSettings((prev) => {
          if (!prev?.agents) return prev;
          return {
            ...prev,
            agents: {
              ...prev.agents,
              [agentKey]: { ...prev.agents[agentKey], enabled },
            },
          };
        }, { revalidate: false });
      } catch (err) {
        console.error("エージェント設定変更失敗:", err);
      }
    },
    [mutateSettings],
  );

  const handleTaskNotificationMinutesSave = useCallback(async () => {
    const minutes = Number(taskNotificationMinutesBefore);
    if (!Number.isFinite(minutes) || minutes < 0) return;
    setTaskNotificationSaving(true);
    try {
      await apiFetch("/api/users/me/settings", {
        method: "PATCH",
        body: JSON.stringify({
          task_notification_minutes_before: Math.floor(minutes),
        }),
      });
      setCurrentUser((prev) =>
        prev
          ? {
              ...prev,
              user_settings: {
                ...(prev.user_settings || {}),
                task_notification_minutes_before: Math.floor(minutes),
              },
            }
          : prev,
      );
      setTaskNotificationMinutesBefore(String(Math.floor(minutes)));
    } finally {
      setTaskNotificationSaving(false);
    }
  }, [taskNotificationMinutesBefore]);

  const handleTaskNotificationsDefaultToggle = useCallback(
    async (enabled: boolean) => {
      setTaskNotificationsDefaultEnabled(enabled);
      try {
        await apiFetch("/api/users/me/settings", {
          method: "PATCH",
          body: JSON.stringify({
            task_notifications_default_enabled: enabled,
          }),
        });
        setCurrentUser((prev) =>
          prev
            ? {
                ...prev,
                user_settings: {
                  ...(prev.user_settings || {}),
                  task_notifications_default_enabled: enabled,
                },
              }
            : prev,
        );
      } catch {
        setTaskNotificationsDefaultEnabled(!enabled);
      }
    },
    [],
  );

  const handleCustomInstructionsSave = useCallback(async () => {
    setCustomInstructionsSaving(true);
    try {
      await apiFetch("/api/users/me/settings", {
        method: "PATCH",
        body: JSON.stringify({
          custom_instructions: customInstructions.trim(),
        }),
      });
      setCurrentUser((prev) =>
        prev
          ? {
              ...prev,
              user_settings: {
                ...(prev.user_settings || {}),
                custom_instructions: customInstructions.trim(),
              },
            }
          : prev,
      );
    } finally {
      setCustomInstructionsSaving(false);
    }
  }, [customInstructions]);

  const handleChangePassword = useCallback(async () => {
    setPasswordError("");
    setPasswordMessage("");

    if (!newPassword) {
      setPasswordError("新しいパスワードを入力してください");
      return;
    }
    if (newPassword.length < 6) {
      setPasswordError("新しいパスワードは6文字以上必要です");
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError("新しいパスワードが一致しません");
      return;
    }

    setPasswordLoading(true);
    try {
      await apiFetch("/api/auth/change-password", {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword || undefined,
          new_password: newPassword,
        }),
      });
      setPasswordMessage("パスワードを変更しました");
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      setPasswordError(
        err instanceof Error ? err.message : "変更に失敗しました",
      );
    } finally {
      setPasswordLoading(false);
    }
  }, [currentPassword, newPassword, confirmPassword]);

  return (
    <div className="mx-auto w-full max-w-5xl space-y-5 p-4 pb-8">
      <div>
        <h1 className="text-lg font-bold">設定</h1>
        <p className="mt-1 text-xs text-muted-foreground">
          よく使う項目から、会話、操作、通知、ナレッジ、連携、権限、管理へ順番に整理しています。
        </p>
      </div>

      <QuickSettings />

      <SettingsGroup
        id="conversation"
        title="会話・AI応答"
        icon={<MessageSquareText className="size-4" />}
      >
        <SettingsDisclosure
          title="会話カスタム指示"
          icon={<MessageSquareText className="size-4" />}
          summary={
            customInstructions.trim() ? (
              <Badge variant="secondary">設定済み</Badge>
            ) : undefined
          }
        >
          <div className="space-y-2">
            <Label htmlFor="custom-instructions" className="text-xs">
              すべての会話に追加するユーザー別指示
            </Label>
            <LongTextEditor
              id="custom-instructions"
              value={customInstructions}
              onChange={setCustomInstructions}
              minHeight={140}
              maxHeight={360}
              fontSize={12}
              placeholder="例: 返答は簡潔に。重要な判断は理由も書く。"
            />
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">
                LLMの会話プロンプトへ毎回追加されます。
              </p>
              <Button
                size="sm"
                onClick={handleCustomInstructionsSave}
                disabled={customInstructionsSaving}
              >
                {customInstructionsSaving ? "保存中..." : "保存"}
              </Button>
            </div>
          </div>
        </SettingsDisclosure>
        <MemorySection />
        <CharactersSection />
        <LlmModelSection />
      </SettingsGroup>

      <SettingsGroup
        id="tts-yomi"
        title="音声・読み"
        icon={<AudioLines className="size-4" />}
      >
        <YomiLinterSection />
      </SettingsGroup>

      <SettingsGroup
        id="input"
        title="入力・操作"
        icon={<Keyboard className="size-4" />}
      >
        <NavigationTabsSection />
        <EditorSettingsSection />
        <AudioPlayerSettingsCard />
        <SnippetsSection />
        <SettingsDisclosure
          title="モバイルコマンド"
          icon={<Smartphone className="size-4" />}
          summary={
            mobileCommands?.enabled && mobileCommands.commands.length > 0 ? (
              <Badge variant="secondary">
                {mobileCommands.commands.length}件
              </Badge>
            ) : undefined
          }
        >
          <p className="text-xs text-muted-foreground">
            端末操作や外部スクリプトを起動するため、必要な時だけ開いて実行します。
          </p>
          {mobileCommands ? (
            mobileCommands.enabled ? (
              mobileCommands.commands.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {mobileCommands.commands.map((cmd) => (
                    <Button
                      key={cmd.id}
                      variant="outline"
                      size="sm"
                      disabled={runningCommandId === cmd.id}
                      onClick={() =>
                        handleRunMobileCommand(cmd.id, cmd.requires_confirmation)
                      }
                      title={cmd.hint || undefined}
                    >
                      {runningCommandId === cmd.id ? (
                        <Loader2 className="mr-1 size-3 animate-spin" />
                      ) : cmd.icon ? (
                        <span className="mr-1">{cmd.icon}</span>
                      ) : null}
                      {cmd.label}
                    </Button>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  コマンドは登録されていません
                </p>
              )
            ) : (
              <p className="text-sm text-muted-foreground">
                モバイルコマンドは無効です
              </p>
            )
          ) : (
            <p className="text-sm text-muted-foreground">
              取得できませんでした
            </p>
          )}
        </SettingsDisclosure>
        {currentUser?.role === "admin" && (
          <SettingsDisclosure
            title="ショートカット"
            icon={<Keyboard className="size-4" />}
          >
              <div className="flex items-center gap-2">
                <Checkbox
                  id="restart-shortcut"
                  checked={restartShortcutEnabled}
                  onCheckedChange={async (checked) => {
                    const val = checked === true;
                    setRestartShortcutEnabled(val);
                    try {
                      await apiFetch("/api/users/me/settings", {
                        method: "PATCH",
                        body: JSON.stringify({
                          restart_shortcut_enabled: val,
                        }),
                      });
                    } catch {
                      setRestartShortcutEnabled(!val);
                    }
                  }}
                />
                <Label htmlFor="restart-shortcut" className="text-xs">
                  Alt+Shift+R でバックエンドを即時再起動
                </Label>
              </div>
          </SettingsDisclosure>
        )}
      </SettingsGroup>

      <SettingsGroup
        id="notifications"
        title="通知・予定"
        icon={<Bell className="size-4" />}
      >
        <SettingsDisclosure
          title="タスク通知"
          icon={<Bell className="size-4" />}
          summary={
            <Badge variant={taskNotificationsDefaultEnabled ? "default" : "secondary"}>
              {taskNotificationsDefaultEnabled ? "ON" : "OFF"}
            </Badge>
          }
        >
            <div className="flex items-center justify-between gap-3 rounded-md border px-3 py-2">
              <div className="space-y-1">
                <Label
                  htmlFor="task-notifications-default"
                  className="text-xs font-medium"
                >
                  新規タスクの通知をデフォルトONにする
                </Label>
                <p className="text-xs text-muted-foreground">
                  OFFにすると、新しく作るタスクは通知オフで開始します。
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Checkbox
                  id="task-notifications-default"
                  checked={taskNotificationsDefaultEnabled}
                  onCheckedChange={(checked) =>
                    handleTaskNotificationsDefaultToggle(checked === true)
                  }
                />
                <Badge
                  variant={
                    taskNotificationsDefaultEnabled ? "default" : "secondary"
                  }
                >
                  {taskNotificationsDefaultEnabled ? "ON" : "OFF"}
                </Badge>
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="task-notification-minutes" className="text-xs">
                Start Date の何分前に通知するか
              </Label>
              <div className="flex items-center gap-2">
                <Input
                  id="task-notification-minutes"
                  type="number"
                  min={0}
                  step={1}
                  value={taskNotificationMinutesBefore}
                  onChange={(e) =>
                    setTaskNotificationMinutesBefore(e.target.value)
                  }
                  className="h-8 w-28"
                />
                <Button
                  size="sm"
                  onClick={handleTaskNotificationMinutesSave}
                  disabled={
                    taskNotificationSaving ||
                    !Number.isFinite(Number(taskNotificationMinutesBefore)) ||
                    Number(taskNotificationMinutesBefore) < 0
                  }
                >
                  {taskNotificationSaving ? "保存中..." : "保存"}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                個別タスクで通知オフにしていない場合の既定値です。
              </p>
            </div>
        </SettingsDisclosure>
        <GoogleCalendarSection />
        <RemoteServerSection />
      </SettingsGroup>

      <SettingsGroup
        id="knowledge"
        title="ナレッジ・検索"
        icon={<Search className="size-4" />}
      >
        <SearchSettingsSection />
        <ClipIngestTargetsSection />
        <KnowledgeSourcesSection />
        <SettingsDisclosure
          title="クローラーステータス"
          icon={<Bug className="size-4" />}
          summary={
            crawlers && crawlers.length > 0 ? (
              <Badge variant="secondary">{crawlers.length}件</Badge>
            ) : undefined
          }
        >
          <Button variant="outline" size="sm" onClick={fetchCrawlers}>
            更新
          </Button>
          {crawlers === null ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              必要な時に更新してください
            </div>
          ) : crawlers.length > 0 ? (
            <div className="space-y-3">
              {crawlers.map((crawler) => (
                <div key={crawler.name}>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <span
                        className={`inline-block size-2.5 rounded-full ${
                          crawler.is_alive
                            ? "bg-green-500 shadow-[0_0_6px_rgba(34,197,94,0.5)]"
                            : "bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.5)]"
                        }`}
                      />
                      <div>
                        <p className="text-sm font-medium">{crawler.name}</p>
                        <p className="text-xs text-muted-foreground">
                          {crawler.type} - {crawler.status}
                        </p>
                      </div>
                    </div>
                    <Badge variant={crawler.is_alive ? "default" : "secondary"}>
                      {crawler.is_alive ? "稼働中" : "停止"}
                    </Badge>
                  </div>
                  {crawler.error && (
                    <p className="mt-1 ml-5.5 text-xs text-destructive">
                      {crawler.error}
                    </p>
                  )}
                  <Separator className="mt-3" />
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              クローラーは登録されていません
            </p>
          )}
        </SettingsDisclosure>
      </SettingsGroup>

      <SettingsGroup
        id="integrations"
        title="外部連携"
        icon={<Plug className="size-4" />}
      >
        <SpotifySection />
        <ComfyUISection />
        <McpEnableCard
          loading={loading}
          enabled={settings?.agents?.mcp?.enabled ?? null}
          onToggle={(enabled) => handleAgentToggle("mcp", enabled)}
        />
        <McpSection />
      </SettingsGroup>

      <SettingsGroup
        id="tool-permissions"
        title="ツール権限・実行環境"
        icon={<ShieldCheck className="size-4" />}
      >
        <ToolPermissionsCard
          loading={loading}
          settings={settings}
          onToggle={handleAgentToggle}
        />
      </SettingsGroup>

      <SettingsGroup
        id="account"
        title="アカウント・セキュリティ"
        icon={<KeyRound className="size-4" />}
      >
        <SettingsDisclosure
          title="パスワード変更"
          icon={<Lock className="size-4" />}
        >
          <div className="space-y-1">
            <Label className="text-xs">現在のパスワード</Label>
            <Input
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              placeholder="現在のパスワード"
              className="max-w-sm"
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">新しいパスワード</Label>
            <Input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="6文字以上"
              className="max-w-sm"
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">新しいパスワード（確認）</Label>
            <Input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="もう一度入力"
              className="max-w-sm"
            />
          </div>
          <Button
            size="sm"
            onClick={handleChangePassword}
            disabled={passwordLoading}
          >
            {passwordLoading && <Loader2 className="mr-1 size-3 animate-spin" />}
            変更
          </Button>
          {passwordMessage && (
            <p className="text-xs text-green-600 dark:text-green-400">
              {passwordMessage}
            </p>
          )}
          {passwordError && (
            <p className="text-xs text-destructive">{passwordError}</p>
          )}
        </SettingsDisclosure>
        {currentUser && (
          <LoginHistorySection isAdmin={currentUser.role === "admin"} />
        )}
        {currentUser && (
          <CostDashboardSection isAdmin={currentUser.role === "admin"} />
        )}
      </SettingsGroup>

      {currentUser?.role === "admin" && (
        <SettingsGroup
          id="admin"
          title="管理・運用"
          icon={<UserCog className="size-4" />}
        >
          <UserManagementConsole currentUser={currentUser} />
          <UserExportSection />
          <SkillsSection />
          <HeartbeatsSection />
        </SettingsGroup>
      )}

      {currentUser?.role === "admin" && (
        <SettingsGroup
          id="support"
          title="サポート"
          icon={<CircleHelp className="size-4" />}
        >
          <FeedbackSection />
        </SettingsGroup>
      )}
    </div>
  );
}

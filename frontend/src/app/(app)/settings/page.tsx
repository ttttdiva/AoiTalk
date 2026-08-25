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
  ChevronDown,
  ChevronUp,
  CircleHelp,
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
import { WebexSection } from "@/components/settings/webex-section";
import { RemoteServerSection } from "@/components/settings/remote-server-section";
import { UserManagementConsole } from "@/components/settings/user-management-console";
import { HeartbeatsSection } from "@/components/settings/heartbeats-section";
import { LlmModelSection } from "@/components/settings/llm-model-section";
import { AutonomousTaskExecutionSection } from "@/components/settings/autonomous-task-execution-section";
import { SearchSettingsSection } from "@/components/settings/search-settings-section";
import { SpotifySection } from "@/components/settings/spotify-section";
import { EditorSettingsSection } from "@/components/settings/editor-settings-section";
import { YomiLinterSection } from "@/components/settings/yomi-linter-section";
import { NavigationTabsSection } from "@/components/settings/navigation-tabs-section";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";
import {
  SettingsCategoryNavigation,
  type SettingsCategoryId,
} from "@/components/settings/settings-category-navigation";
import {
  QUICK_SETTING_REGISTRY,
  getSettingsTarget,
  isSettingsCategoryId,
  type SettingsTargetId,
} from "@/components/settings/settings-target-registry";
import { SettingsOverview } from "@/components/settings/settings-overview";
import { HydrusSettingsSection } from "@/components/settings/hydrus-settings-section";
import { CookieManagementSection } from "@/components/settings/cookie-management-section";
import { openSettingsTarget } from "@/components/settings/settings-target-navigation";
import {
  getSettingsScrollContainer,
  pushSettingsCategoryHash,
  scrollSettingsCategory,
  type SettingsScrollBehavior,
} from "@/components/settings/settings-scroll-navigation";
import { getTaskNotificationsDefaultEnabled } from "@/lib/user-settings";
import { useUserSettings } from "@/contexts/user-settings-context";
import { serializeAudioPlayerSettings } from "@/lib/audio-player-settings";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";

function requestSettingsFrame(callback: () => void): number {
  if (typeof window === "undefined") return -1;
  if (typeof window.requestAnimationFrame === "function") {
    return window.requestAnimationFrame(callback);
  }
  return window.setTimeout(callback, 0);
}

function cancelSettingsFrame(frame: number) {
  if (typeof window === "undefined" || frame < 0) return;
  if (typeof window.cancelAnimationFrame === "function") {
    window.cancelAnimationFrame(frame);
  } else {
    window.clearTimeout(frame);
  }
}

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
    avatar_url: string | null;
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

type QuickSettingId = SettingsTargetId;
type QuickSettingDefinition = (typeof QUICK_SETTING_REGISTRY)[number];

const DEFAULT_QUICK_SETTING_IDS: QuickSettingId[] = [
  "task-notifications",
  "web-search",
  "user-memory",
  "google-calendar",
  "webex",
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
    <section id={id} className="scroll-mt-5 space-y-3" data-settings-group={id}>
      <div className="flex items-center gap-2 border-b border-border-subtle pb-2">
        <span className="text-primary">{icon}</span>
        <h2 tabIndex={-1} className="text-sm font-semibold tracking-tight">
          {title}
        </h2>
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

/**
 * Keeps a stable target around legacy section components that do not accept an
 * id prop yet. `contents` preserves their layout while hash navigation can
 * still locate and open the first disclosure in the section.
 */
function SettingsTargetFrame({
  targetId,
  children,
}: {
  targetId: SettingsTargetId;
  children: ReactNode;
}) {
  return (
    <div
      data-settings-target={targetId}
      className="contents"
    >
      {children}
    </div>
  );
}

function isVisibleSettingsTarget(targetId: string, isAdmin: boolean) {
  const target = getSettingsTarget(targetId);
  return target && (!target.adminOnly || isAdmin) ? target : null;
}

function QuickSettings() {
  const { settings, patch } = useUserSettings();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const selectedIds = getQuickSettingIds(settings);
  const selectedItems = selectedIds
    .map(getQuickSettingDefinition)
    .filter((item): item is QuickSettingDefinition => Boolean(item));

  const saveSelectedIds = async (nextIds: QuickSettingId[]) => {
    setSaving(true);
    setFeedback(null);
    try {
      await patch({ quick_settings: { pins: nextIds } });
      setFeedback("よく使う設定を保存しました。");
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "保存に失敗しました。");
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
        {feedback && (
          <p role="status" className="text-xs text-muted-foreground">
            {feedback}
          </p>
        )}
        {selectedItems.length > 0 ? (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {selectedItems.map(({ id, targetId, label, icon: Icon }) => (
              <a
                key={id}
                href={`#${targetId}`}
                data-settings-target-link={targetId}
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
  error,
  onRetry,
  onToggle,
}: {
  loading: boolean;
  settings: SettingsResponse | null;
  error?: Error;
  onRetry?: () => void;
  onToggle: (agentKey: string, enabled: boolean) => void;
}) {
  const agents = settings?.agents;

  return (
    <SettingsDisclosure
      title="ツール権限"
      icon={<ShieldCheck className="size-4" />}
      id="tool-permissions-card"
      targetId="tool-permissions"
    >
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
        ) : error ? (
          <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>ツール権限を取得できませんでした。{error.message}</p>
            {onRetry && (
              <Button type="button" variant="outline" size="sm" onClick={onRetry}>
                再試行
              </Button>
            )}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            ツール権限を取得できませんでした
          </p>
        )}
    </SettingsDisclosure>
  );
}

function AudioPlayerSettingsCard() {
  const { audioPlayerSettings, patch } = useUserSettings();
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
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
    setFeedback(null);
    try {
      const next = { ...audioPlayerSettings, ...patchValue };
      await patch({ audio_player: serializeAudioPlayerSettings(next) });
      setFeedback("音楽プレイヤー設定を保存しました。");
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "保存に失敗しました。");
    } finally {
      setSaving(false);
    }
  };

  return (
    <SettingsDisclosure
      title="音楽プレイヤー"
      icon={<Music className="size-4" />}
      id="audio-player-card"
      targetId="audio-player"
      summary={
        <Badge variant="secondary">
          {flags.length > 0
            ? `${scopeLabel} / ${flags.join(" / ")}`
            : scopeLabel}
        </Badge>
      }
    >
      {feedback && <p role="status" className="text-xs text-muted-foreground">{feedback}</p>}
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
  const [currentUser, setCurrentUser] = useState<AuthStatus["user"] | null>(
    null,
  );
  const [authStatusLoaded, setAuthStatusLoaded] = useState(false);
  const isAdmin = currentUser?.role === "admin";
  // 独立したサーバー状態（settings / mobileCommands / crawlers）は SWR で管理する。
  // currentUser は編集ドラフトの初期値を供給し、各保存操作で楽観的に patch される
  // ハイブリッド状態のため従来の useState のまま扱う。
  const { data: settings = null, error: settingsError, mutate: mutateSettings } = useSWR<SettingsResponse | null>(
    "settings/page-settings",
    async () => {
      const raw = (await pyFetch<SettingsResponse>("/settings")) as Record<
        string,
        unknown
      >;
      return (raw.settings ?? raw) as SettingsResponse;
    },
    SETTINGS_SWR_OPTIONS,
  );
  const { data: mobileCommands = null, error: mobileCommandsError, mutate: mutateMobileCommands } =
    useSWR<MobileCommandsResponse | null>(
      authStatusLoaded && isAdmin ? "settings/mobile-commands" : null,
      async () => {
        return await pyFetch<MobileCommandsResponse>("/mobile/commands");
      },
      SETTINGS_SWR_OPTIONS,
    );
  const { data: crawlers = null, error: crawlersError, mutate: mutateCrawlers } = useSWR<
    CrawlerInfo[] | null
  >(
    "settings/crawler-status",
    async () => {
      return (await pyFetch<CrawlerStatusResponse>("/crawler/status")).crawlers;
    },
    SETTINGS_SWR_OPTIONS,
  );
  const [runningCommandId, setRunningCommandId] = useState<string | null>(null);

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
  const [activeCategory, setActiveCategory] =
    useState<SettingsCategoryId>("overview");
  const categoryScrollFrameRef = useRef<number | null>(null);
  const directTargetFrameRef = useRef<number | null>(null);
  const navigationGenerationRef = useRef(0);
  const categoryScrollObserverRef = useRef<ResizeObserver | null>(null);
  const categoryScrollWatchTimeoutRef = useRef<number | null>(null);
  const lastHashSyncKeyRef = useRef<string | null>(null);
  const [actionFeedback, setActionFeedback] = useState<{
    kind: "success" | "error";
    message: string;
  } | null>(null);

  /**
   * Wait for the shell/page paint before measuring a category.  This avoids
   * racing the initial async settings render while still issuing exactly one
   * scroll once the target exists.  If a target is temporarily absent (for
   * example while a gated section mounts), retry a bounded number of frames.
   */
  const cancelCategoryScroll = useCallback(() => {
    if (categoryScrollFrameRef.current !== null) {
      cancelSettingsFrame(categoryScrollFrameRef.current);
      categoryScrollFrameRef.current = null;
    }
    categoryScrollObserverRef.current?.disconnect();
    categoryScrollObserverRef.current = null;
    if (categoryScrollWatchTimeoutRef.current !== null) {
      window.clearTimeout(categoryScrollWatchTimeoutRef.current);
      categoryScrollWatchTimeoutRef.current = null;
    }
  }, []);

  const scheduleCategoryScroll = useCallback(
    (category: SettingsCategoryId, behavior: SettingsScrollBehavior) => {
      if (typeof window === "undefined") return;
      cancelCategoryScroll();
      const navigationGeneration = navigationGenerationRef.current;
      const expectedHash = `#${category}`;

      let attempts = 0;
      const measure = () => {
        const didScroll = scrollSettingsCategory(category, {
          behavior,
          focus: true,
        });
        if (!didScroll && attempts < 3) {
          attempts += 1;
          categoryScrollFrameRef.current = requestSettingsFrame(measure);
          return;
        }
        categoryScrollFrameRef.current = null;

        // Account's history/cost cards mount after auth and can change the
        // scrollHeight after the first paint.  Watch that short settling
        // window and remeasure only when the page actually grew, avoiding a
        // second scroll for an unchanged layout.
        if (didScroll && typeof ResizeObserver !== "undefined") {
          const page = document.querySelector<HTMLElement>("[data-settings-page]");
          const container = getSettingsScrollContainer();
          if (page && container) {
            let lastScrollHeight = container.scrollHeight;
            const observer = new ResizeObserver(() => {
              if (
                navigationGeneration !== navigationGenerationRef.current ||
                window.location.hash !== expectedHash
              ) {
                observer.disconnect();
                if (categoryScrollObserverRef.current === observer) {
                  categoryScrollObserverRef.current = null;
                }
                return;
              }
              const nextScrollHeight = container.scrollHeight;
              if (nextScrollHeight === lastScrollHeight) return;
              lastScrollHeight = nextScrollHeight;
              scrollSettingsCategory(category, {
                behavior: "auto",
                focus: false,
              });
            });
            observer.observe(page);
            categoryScrollObserverRef.current = observer;
            categoryScrollWatchTimeoutRef.current = window.setTimeout(() => {
              observer.disconnect();
              if (categoryScrollObserverRef.current === observer) {
                categoryScrollObserverRef.current = null;
              }
              categoryScrollWatchTimeoutRef.current = null;
            }, 1_500);
          }
        }
      };

      // A single RAF is enough for normal clicks; retries only run when the
      // target has not mounted yet, so a category never receives duplicate
      // smooth scrolls from this handler.
      categoryScrollFrameRef.current = requestSettingsFrame(measure);
    },
    [cancelCategoryScroll],
  );

  useEffect(
    () => () => {
      cancelCategoryScroll();
      if (directTargetFrameRef.current !== null) {
        cancelSettingsFrame(directTargetFrameRef.current);
        directTargetFrameRef.current = null;
      }
    },
    [cancelCategoryScroll],
  );

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      // settings は SWR fetcher が取得と失敗時フォールバックを担う。
      // mobile commands は admin 確定後に別 effect で取得する。
      // authStatus は currentUser とドラフト初期値の供給のため従来どおり直接取得する。
      const results = await Promise.allSettled([
        mutateSettings(),
        apiFetch<AuthStatus>("/api/auth/status"),
      ]);

      if (results[1].status === "fulfilled" && results[1].value.authenticated) {
        const u = results[1].value.user || null;
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
      setAuthStatusLoaded(true);
      setLoading(false);
    }
  }, [mutateSettings]);

  useEffect(() => {
    if (!authStatusLoaded || !isAdmin) return;
    void mutateMobileCommands();
  }, [authStatusLoaded, isAdmin, mutateMobileCommands]);

  const fetchCrawlers = useCallback(async () => {
    // 取得前に一旦未取得状態（null）へ戻してから再取得する（従来挙動を維持）。
    try {
      await mutateCrawlers(null, { revalidate: false });
      await mutateCrawlers();
    } catch {
      // SWR exposes the error so the section can render stale/error + retry.
    }
  }, [mutateCrawlers]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // Hash links are the stable contract between the Settings category
  // navigation and section owners. Validate every incoming hash so an old or
  // unauthorized deep link never renders a blank page; direct targets also
  // open their disclosure and move keyboard focus to its trigger.
  useEffect(() => {
    const syncHash = () => {
      // Wait for auth resolution before the first hash movement.  Account is
      // public to authenticated users, but admin/support visibility is not;
      // deferring all initial movement prevents a pending→loaded transition
      // from scrolling the same target twice while still revalidating ACLs
      // once authStatusLoaded flips true.
      if (!authStatusLoaded) return;
      const rawHash = window.location.hash.replace(/^#/, "");
      // Browsers commonly emit both popstate and hashchange when traversing
      // hash history.  Treat that pair as one navigation so the same section
      // cannot receive two smooth-scroll requests.
      const syncKey = `${rawHash}|${authStatusLoaded ? "loaded" : "pending"}|${isAdmin ? "admin" : "user"}`;
      if (lastHashSyncKeyRef.current === syncKey) return;
      lastHashSyncKeyRef.current = syncKey;
      cancelCategoryScroll();
      navigationGenerationRef.current += 1;
      if (directTargetFrameRef.current !== null) {
        cancelSettingsFrame(directTargetFrameRef.current);
        directTargetFrameRef.current = null;
      }
      if (!rawHash) {
        setActiveCategory("overview");
        return;
      }
      const target = isVisibleSettingsTarget(rawHash, isAdmin);
      if (target) {
        setActiveCategory(target.category);
        if (target.openDisclosure === false) {
          scheduleCategoryScroll(target.category, "auto");
          return;
        }
        const generation = navigationGenerationRef.current;
        const targetHash = `#${target.targetId}`;
        let directTargetFrameRan = false;
        const directTargetFrame = requestSettingsFrame(() => {
          directTargetFrameRan = true;
          directTargetFrameRef.current = null;
          if (
            generation !== navigationGenerationRef.current ||
            window.location.hash !== targetHash
          ) {
            return;
          }
          openSettingsTarget(target.targetId, {
            isCurrent: () =>
              generation === navigationGenerationRef.current &&
              window.location.hash === targetHash,
          });
        });
        if (!directTargetFrameRan) {
          directTargetFrameRef.current = directTargetFrame;
        }
        return;
      }
      if (isSettingsCategoryId(rawHash)) {
        // Category registry ids are always valid except admin/support for a
        // non-admin user. Keep those paths safe and visibly recoverable.
        if ((rawHash === "admin" || rawHash === "support") && !isAdmin) {
          setActiveCategory("overview");
          window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#overview`);
          scheduleCategoryScroll("overview", "auto");
          return;
        }
        setActiveCategory(rawHash);
        scheduleCategoryScroll(rawHash, "auto");
        return;
      }
      setActiveCategory("overview");
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#overview`);
      scheduleCategoryScroll("overview", "auto");
    };
    syncHash();
    window.addEventListener("hashchange", syncHash);
    window.addEventListener("popstate", syncHash);
    return () => {
      window.removeEventListener("hashchange", syncHash);
      window.removeEventListener("popstate", syncHash);
    };
  }, [authStatusLoaded, cancelCategoryScroll, isAdmin, scheduleCategoryScroll]);

  const handleCategorySelect = useCallback((category: SettingsCategoryId) => {
    setActiveCategory(category);
    cancelCategoryScroll();
    navigationGenerationRef.current += 1;
    if (directTargetFrameRef.current !== null) {
      cancelSettingsFrame(directTargetFrameRef.current);
      directTargetFrameRef.current = null;
    }
    // Category anchors keep their href for copy/link semantics, but prevent
    // native scrolling so this is the only history + scroll path.
    pushSettingsCategoryHash(category);
    if (!authStatusLoaded) return;
    lastHashSyncKeyRef.current = `${category}|${authStatusLoaded ? "loaded" : "pending"}|${isAdmin ? "admin" : "user"}`;
    scheduleCategoryScroll(category, "smooth");
  }, [authStatusLoaded, cancelCategoryScroll, isAdmin, scheduleCategoryScroll]);

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
        setActionFeedback({ kind: "success", message: `コマンド「${commandId}」を実行しました。` });
      } catch (err) {
        console.error("コマンド実行失敗:", err);
        setActionFeedback({
          kind: "error",
          message: err instanceof Error ? err.message : "コマンドの実行に失敗しました。",
        });
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
        setActionFeedback({ kind: "success", message: "ツール権限を更新しました。" });
      } catch (err) {
        console.error("エージェント設定変更失敗:", err);
        setActionFeedback({
          kind: "error",
          message: err instanceof Error ? err.message : "ツール権限の更新に失敗しました。",
        });
      }
    },
    [mutateSettings],
  );

  const handleTaskNotificationMinutesSave = useCallback(async () => {
    const minutes = Number(taskNotificationMinutesBefore);
    if (!Number.isFinite(minutes) || minutes < 0) {
      setActionFeedback({ kind: "error", message: "通知時間は0以上の数値で入力してください。" });
      return;
    }
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
      setActionFeedback({ kind: "success", message: "タスク通知の設定を保存しました。" });
    } catch (error) {
      setActionFeedback({
        kind: "error",
        message: error instanceof Error ? error.message : "タスク通知の保存に失敗しました。",
      });
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
        setActionFeedback({ kind: "success", message: "タスク通知の既定値を更新しました。" });
      } catch (error) {
        setTaskNotificationsDefaultEnabled(!enabled);
        setActionFeedback({
          kind: "error",
          message: error instanceof Error ? error.message : "タスク通知の更新に失敗しました。",
        });
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
      setActionFeedback({ kind: "success", message: "カスタム指示を保存しました。" });
    } catch (error) {
      setActionFeedback({
        kind: "error",
        message: error instanceof Error ? error.message : "カスタム指示の保存に失敗しました。",
      });
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

  const settingsNavigation = (
    <aside
      className="ao-workspace-nav-panel bg-surface-charcoal"
      data-shell-slot="workspace-navigation"
      data-workspace="settings"
    >
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        <div className="border-b border-border-subtle px-1 pb-3 pt-2">
          <h2 className="text-base font-semibold tracking-tight">Settings</h2>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            項目をカテゴリから開きます
          </p>
        </div>
        <div className="pt-2">
          <SettingsCategoryNavigation
            activeCategory={activeCategory}
            isAdmin={isAdmin}
            onSelect={handleCategorySelect}
          />
        </div>
      </div>
    </aside>
  );

  useWorkspaceShellRegistration({
    id: "settings-workspace",
    workspaceNavigation: settingsNavigation,
    priority: 30,
  });

  const showMobileCommandsError = Boolean(isAdmin && mobileCommandsError);

  return (
    <div className="settings-page w-full space-y-6 px-6 py-5 pb-10" data-settings-page>
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-border-subtle pb-4">
        <div>
        <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">AoiTalk Workspace</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">設定</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          よく使う項目から、会話、操作、通知、ナレッジ、連携、権限、管理へ順番に整理しています。
        </p>
        </div>
        <div className="rounded-sm border border-border-subtle bg-card px-2.5 py-1.5 text-[11px] text-muted-foreground dark:bg-surface-charcoal">
          Settings / {currentUser?.role === "admin" ? "Admin" : "Workspace"}
        </div>
      </div>

      {(settingsError || showMobileCommandsError || crawlersError || actionFeedback) && (
        <div
          role={settingsError || showMobileCommandsError || crawlersError || actionFeedback?.kind === "error" ? "alert" : "status"}
          aria-live="polite"
          className={
            settingsError || showMobileCommandsError || crawlersError || actionFeedback?.kind === "error"
              ? "rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
              : "rounded-md border border-border bg-muted/30 px-3 py-2 text-sm text-muted-foreground"
          }
        >
          {settingsError && (
            <p className="flex flex-wrap items-center justify-between gap-2">
              <span>設定を取得できませんでした。{settings ? "前回の値を表示しています。" : ""}</span>
              <Button type="button" variant="outline" size="sm" onClick={() => void mutateSettings().catch(() => undefined)}>
                再試行
              </Button>
            </p>
          )}
          {showMobileCommandsError && (
            <p className="mt-1 flex flex-wrap items-center justify-between gap-2">
              <span>モバイルコマンドを取得できませんでした。</span>
              <Button type="button" variant="outline" size="sm" onClick={() => void mutateMobileCommands().catch(() => undefined)}>
                再試行
              </Button>
            </p>
          )}
          {crawlersError && (
            <p className="mt-1 flex flex-wrap items-center justify-between gap-2">
              <span>クローラー状態を取得できませんでした。</span>
              <Button type="button" variant="outline" size="sm" onClick={() => void fetchCrawlers()}>
                再試行
              </Button>
            </p>
          )}
          {actionFeedback && <p className="mt-1">{actionFeedback.message}</p>}
        </div>
      )}

      <SettingsOverview
        isAdmin={isAdmin}
        onSelectCategory={handleCategorySelect}
        quickSettings={<QuickSettings />}
      />

      <SettingsGroup
        id="conversation"
        title="会話・AI応答"
        icon={<MessageSquareText className="size-4" />}
      >
        <SettingsDisclosure
          title="会話カスタム指示"
          icon={<MessageSquareText className="size-4" />}
          id="custom-instructions-card"
          targetId="custom-instructions"
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
        <SettingsTargetFrame targetId="memory"><MemorySection /></SettingsTargetFrame>
        <SettingsTargetFrame targetId="characters"><CharactersSection /></SettingsTargetFrame>
        <SettingsTargetFrame targetId="llm-model"><LlmModelSection /></SettingsTargetFrame>
      </SettingsGroup>

      <SettingsGroup
        id="tts-yomi"
        title="音声・読み"
        icon={<AudioLines className="size-4" />}
      >
        <SettingsTargetFrame targetId="yomi-linter"><YomiLinterSection /></SettingsTargetFrame>
      </SettingsGroup>

      <SettingsGroup
        id="input"
        title="入力・操作"
        icon={<Keyboard className="size-4" />}
      >
        <SettingsTargetFrame targetId="navigation-tabs"><NavigationTabsSection /></SettingsTargetFrame>
        <SettingsTargetFrame targetId="editor-settings"><EditorSettingsSection /></SettingsTargetFrame>
        <SettingsTargetFrame targetId="audio-player"><AudioPlayerSettingsCard /></SettingsTargetFrame>
        <SettingsTargetFrame targetId="snippets"><SnippetsSection /></SettingsTargetFrame>
        {isAdmin && (
          <SettingsDisclosure
          title="モバイルコマンド"
          icon={<Smartphone className="size-4" />}
          id="mobile-commands"
          targetId="mobile-commands"
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
          {mobileCommandsError ? (
            <div role="alert" className="space-y-2 text-sm text-destructive">
              <p>モバイルコマンドを取得できませんでした。</p>
              <Button type="button" variant="outline" size="sm" onClick={() => void mutateMobileCommands().catch(() => undefined)}>
                再試行
              </Button>
            </div>
          ) : mobileCommands ? (
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
          ) : loading ? (
            <p className="text-sm text-muted-foreground">取得中...</p>
          ) : (
            <p className="text-sm text-muted-foreground">
              取得できませんでした
            </p>
          )}
          </SettingsDisclosure>
        )}
        {currentUser?.role === "admin" && (
          <SettingsDisclosure
            title="ショートカット"
            icon={<Keyboard className="size-4" />}
            id="restart-shortcut-card"
            targetId="restart-shortcut"
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
                      setActionFeedback({ kind: "success", message: "再起動ショートカットを更新しました。" });
                    } catch (error) {
                      setRestartShortcutEnabled(!val);
                      setActionFeedback({
                        kind: "error",
                        message: error instanceof Error ? error.message : "ショートカットの更新に失敗しました。",
                      });
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
          id="task-notifications"
          targetId="task-notifications"
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
        <SettingsTargetFrame targetId="google-calendar"><GoogleCalendarSection /></SettingsTargetFrame>
        <SettingsTargetFrame targetId="remote-server"><RemoteServerSection /></SettingsTargetFrame>
      </SettingsGroup>

      <SettingsGroup
        id="knowledge"
        title="ナレッジ・検索"
        icon={<Search className="size-4" />}
      >
        <SettingsTargetFrame targetId="search-provider"><SearchSettingsSection /></SettingsTargetFrame>
        <SettingsTargetFrame targetId="clip-ingest"><ClipIngestTargetsSection /></SettingsTargetFrame>
        <SettingsTargetFrame targetId="knowledge-sources"><KnowledgeSourcesSection /></SettingsTargetFrame>
        <SettingsDisclosure
          title="クローラーステータス"
          icon={<Bug className="size-4" />}
          id="crawler-status"
          targetId="crawler-status"
          summary={
            crawlers && crawlers.length > 0 ? (
              <Badge variant="secondary">{crawlers.length}件</Badge>
            ) : undefined
          }
        >
          <Button variant="outline" size="sm" onClick={fetchCrawlers}>
            更新
          </Button>
          {crawlersError ? (
            <div role="alert" className="space-y-2 text-sm text-destructive">
              <p>クローラー状態を取得できませんでした。</p>
              <Button type="button" variant="outline" size="sm" onClick={() => void fetchCrawlers()}>
                再試行
              </Button>
            </div>
          ) : crawlers === null ? (
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
        <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-3" data-settings-integrations-grid>
          <SettingsTargetFrame targetId="webex"><WebexSection /></SettingsTargetFrame>
          <SettingsTargetFrame targetId="spotify"><SpotifySection /></SettingsTargetFrame>
          <SettingsTargetFrame targetId="hydrus"><HydrusSettingsSection /></SettingsTargetFrame>
          <SettingsTargetFrame targetId="comfyui"><ComfyUISection /></SettingsTargetFrame>
          <SettingsTargetFrame targetId="cookie-management"><CookieManagementSection /></SettingsTargetFrame>
          <SettingsTargetFrame targetId="mcp"><McpSection
              loading={loading}
              enabled={settings?.agents?.mcp?.enabled ?? null}
              onToggle={(enabled) => handleAgentToggle("mcp", enabled)}
            /></SettingsTargetFrame>
        </div>
      </SettingsGroup>

      <SettingsGroup
        id="tool-permissions"
        title="ツール権限・実行環境"
        icon={<ShieldCheck className="size-4" />}
      >
        <ToolPermissionsCard
          loading={loading}
          settings={settings}
          error={settingsError}
          onRetry={() => void mutateSettings().catch(() => undefined)}
          onToggle={handleAgentToggle}
        />
        <AutonomousTaskExecutionSection />
      </SettingsGroup>

      <SettingsGroup
        id="account"
        title="アカウント・セキュリティ"
        icon={<KeyRound className="size-4" />}
      >
        <SettingsDisclosure
          title="パスワード変更"
          icon={<Lock className="size-4" />}
          id="password"
          targetId="password"
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
        {isAdmin && currentUser && (
          <SettingsTargetFrame targetId="login-history"><LoginHistorySection isAdmin /></SettingsTargetFrame>
        )}
        {currentUser && (
          <SettingsTargetFrame targetId="cost-dashboard"><CostDashboardSection isAdmin={currentUser.role === "admin"} /></SettingsTargetFrame>
        )}
      </SettingsGroup>

      {currentUser?.role === "admin" && (
        <SettingsGroup
          id="admin"
          title="管理・運用"
          icon={<UserCog className="size-4" />}
        >
          <SettingsTargetFrame targetId="user-management"><UserManagementConsole currentUser={currentUser} /></SettingsTargetFrame>
          <SettingsTargetFrame targetId="user-export"><UserExportSection /></SettingsTargetFrame>
          <SettingsTargetFrame targetId="skills"><SkillsSection /></SettingsTargetFrame>
          <SettingsTargetFrame targetId="heartbeats"><HeartbeatsSection /></SettingsTargetFrame>
        </SettingsGroup>
      )}

      {currentUser?.role === "admin" && (
        <SettingsGroup
          id="support"
          title="サポート"
          icon={<CircleHelp className="size-4" />}
        >
          <SettingsTargetFrame targetId="feedback"><FeedbackSection /></SettingsTargetFrame>
        </SettingsGroup>
      )}
    </div>
  );
}

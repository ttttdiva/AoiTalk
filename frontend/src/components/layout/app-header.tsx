"use client";

import { AppSelect } from "@/components/ui/app-select";

import { useEffect, useState, useCallback, useRef } from "react";
import useSWR from "swr";
import { usePathname, useRouter } from "next/navigation";
import {
  LogOut,
  FolderOpen,
  Square,
  Timer,
  Layers,
  RotateCcw,
  HelpCircle,
  Camera,
  Moon,
  Sun,
  Loader2,
  Search,
  Home,
  Settings,
} from "lucide-react";
import { useProject } from "@/contexts/project-context";
import { useTheme } from "@/contexts/theme-context";
import { taskApi, type TimeEntry } from "@/lib/task-api";
import { toast } from "sonner";
import { formatTimerClock, getElapsedTimerSeconds } from "@/lib/task-time";
import { performAdminRestart } from "@/components/layout/global-admin-restart";
import {
  clearPersistentCache,
  discardPendingPersistentWrites,
} from "@/lib/persistent-cache";
import { resetChatMessageCacheMemory } from "@/lib/chat-message-cache";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Separator } from "@/components/ui/separator";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { RuntimeUtilityPanel } from "@/components/layout/runtime-utility-panel";
import { NotificationBellPopover } from "@/components/layout/sidebar/notification-panel";
import { useShellChrome } from "@/components/layout/shell-context";
import { useRuntimeContext } from "@/contexts/runtime-context";
import { ResourceColorDot } from "@/components/projects/resource-color-picker";

type AuthStatus = {
  authenticated?: boolean;
  user?: {
    id?: string;
    username?: string | null;
    display_name?: string | null;
    role?: string | null;
    avatar_url?: string | null;
  } | null;
};

export const USER_SETTINGS_HREF = "/settings#account";


function useActiveTimer() {
  const [activeEntry, setActiveEntry] = useState<TimeEntry | null>(null);
  const [elapsedValue, setElapsedValue] = useState("00:00:00");
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const originalTitleRef = useRef<string | null>(null);
  const lastTimerEventAtRef = useRef(0);

  // アクティブタイマーをポーリング（10秒間隔）+ timer-changed イベント購読
  useEffect(() => {
    let mounted = true;

    const poll = async () => {
      // タブ非表示中はタイマー同期ポーリングを行わない（低帯域配慮）。
      if (typeof document !== "undefined" && document.hidden) return;
      const requestedAt = Date.now();
      try {
        const entry = await taskApi.getActiveTimeEntry();
        if (mounted && requestedAt >= lastTimerEventAtRef.current) {
          setActiveEntry(entry ?? null);
        }
      } catch {
        if (mounted && requestedAt >= lastTimerEventAtRef.current) {
          setActiveEntry(null);
        }
      }
    };

    poll();
    const interval = setInterval(poll, 10000);
    const onVisibility = () => {
      if (typeof document !== "undefined" && !document.hidden) void poll();
    };
    document.addEventListener("visibilitychange", onVisibility);
    const onChange = (event: Event) => {
      const detail = (event as CustomEvent<{ activeEntry?: TimeEntry | null }>)
        .detail;
      if (detail && typeof detail === "object" && "activeEntry" in detail) {
        lastTimerEventAtRef.current = Date.now();
        setActiveEntry(detail.activeEntry ?? null);
        return;
      }
      void poll();
    };
    window.addEventListener("timer-changed", onChange);
    return () => {
      mounted = false;
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("timer-changed", onChange);
    };
  }, []);

  // 経過時間を毎秒更新 + ブラウザタブタイトル更新
  useEffect(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }

    if (!activeEntry?.started_at) {
      // タイマー停止時はタイトル復元
      if (
        typeof document !== "undefined" &&
        originalTitleRef.current !== null
      ) {
        document.title = originalTitleRef.current;
        originalTitleRef.current = null;
      }
      return;
    }

    if (typeof document !== "undefined" && originalTitleRef.current === null) {
      originalTitleRef.current = document.title;
    }

    const update = () => {
      const display = formatTimerClock(
        getElapsedTimerSeconds(activeEntry.started_at),
      );
      setElapsedValue(display);
      if (typeof document !== "undefined") {
        const taskPart = activeEntry.task_title
          ? ` - ${activeEntry.task_title}`
          : "";
        const base = originalTitleRef.current ?? "AoiTalk";
        document.title = `⏱ ${display}${taskPart} | ${base}`;
      }
    };

    update();
    timerRef.current = setInterval(update, 1000);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [activeEntry]);

  const stopTimer = useCallback(async () => {
    if (!activeEntry) return;
    try {
      const taskId =
        (activeEntry as { task_id?: string | null } | null)?.task_id ?? null;
      await taskApi.stopTimer(activeEntry.id);
      setActiveEntry(null);
      window.dispatchEvent(
        new CustomEvent("timer-changed", {
          detail: { activeEntry: null, taskId },
        }),
      );
    } catch (err) {
      console.error("タイマー停止失敗:", err);
    }
  }, [activeEntry]);

  useEffect(() => {
    const handler = () => {
      void stopTimer();
    };
    window.addEventListener("global-stop-timer", handler);
    return () => window.removeEventListener("global-stop-timer", handler);
  }, [stopTimer]);

  return {
    activeEntry,
    elapsed: activeEntry?.started_at ? elapsedValue || "00:00:00" : "",
    stopTimer,
  };
}

const selectClassName =
  "h-7 rounded border border-input bg-surface-charcoal px-2 text-xs text-foreground outline-none transition-colors focus-visible:border-ring";

function getWorkspaceTitle(pathname: string | null): string {
  if (!pathname) return "Workspace";
  if (pathname.startsWith("/chat")) return "チャット";
  if (pathname.startsWith("/tasks")) return "タスク";
  if (pathname.startsWith("/calendar")) return "カレンダー";
  if (pathname.startsWith("/docs")) return "Docs";
  if (pathname.startsWith("/filer")) return "Files";
  if (pathname.startsWith("/reports")) return "レポート";
  if (pathname.startsWith("/projects")) return "プロジェクト";
  if (pathname.startsWith("/scenarios")) return "Story";
  if (pathname.startsWith("/trpg")) return "TRPG";
  if (pathname.startsWith("/apps")) return "Apps";
  if (pathname.startsWith("/settings")) return "設定";
  return "Workspace";
}

export function AppHeader() {
  const router = useRouter();
  const pathname = usePathname();
  const { resolvedTheme, setTheme } = useTheme();
  const {
    runtimePanelOpen,
    setRuntimePanelOpen,
    toggleRuntimePanel,
    setContextRailOpen,
    setNotificationPanelOpen,
  } = useShellChrome();
  const {
    isConnected: pythonConnected,
    runtimeFeatures,
    changeRuntimeFeature,
    changeRuntimeFeatures,
    voiceStatus,
  } = useRuntimeContext();
  const { activeEntry, elapsed, stopTimer } = useActiveTimer();
  const {
    spaces,
    selectedSpaceId,
    setSelectedSpaceId,
    projects,
    selectedProjectId,
    setSelectedProjectId,
    remoteErrors,
  } = useProject();

  const [restarting, setRestarting] = useState(false);
  const [avatarUploading, setAvatarUploading] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [themeMounted, setThemeMounted] = useState(false);
  const avatarInputRef = useRef<HTMLInputElement>(null);
  const visibleResolvedTheme = themeMounted ? resolvedTheme : "light";
  const nextThemeLabel = visibleResolvedTheme === "dark" ? "ライト" : "ダーク";
  const selectedSpace = spaces.find((space) => space.id === selectedSpaceId);
  const selectedProject = projects.find(
    (project) => project.id === selectedProjectId,
  );
  const toggleTheme = useCallback(() => {
    setTheme(resolvedTheme === "dark" ? "light" : "dark");
  }, [resolvedTheme, setTheme]);

  useEffect(() => {
    setThemeMounted(true);
  }, []);


  // 認証状態の取得を SWR に委譲（マウント時取得のみ・自動 revalidation なし）。
  // アバターもサーバー側のユーザープロフィールを唯一のソースとして扱う。
  const { data: authStatus, mutate: mutateAuthStatus } = useSWR<AuthStatus>(
    "auth/status",
    async () =>
      (await fetch("/api/auth/status", { credentials: "include" })).json(),
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      shouldRetryOnError: false,
    },
  );

  const handleAvatarChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // 同じファイルを続けて選択できるよう、処理開始時に入力をリセットする。
    e.target.value = "";
    if (!file) return;

    if (!file.type.startsWith("image/")) {
      toast.error("画像ファイルを選択してください");
      return;
    }
    // サーバー側の制限に加えて、明らかに大きすぎるファイルは送信前に弾く。
    if (file.size > 5 * 1024 * 1024) {
      toast.error("画像ファイルは5MB以下にしてください");
      return;
    }

    setAvatarUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await fetch("/api/users/me/avatar", {
        method: "POST",
        credentials: "include",
        body: formData,
      });
      const data = (await res.json().catch(() => ({}))) as {
        avatar_url?: string | null;
        detail?: string;
        message?: string;
      };
      if (!res.ok) {
        throw new Error(
          data.detail || data.message || "アイコン画像の更新に失敗しました",
        );
      }

      // 応答をそのまま SWR キャッシュへ反映し、再フェッチを待たずにヘッダーを更新する。
      await mutateAuthStatus(
        (current) =>
          current
            ? {
                ...current,
                user: current.user
                  ? { ...current.user, avatar_url: data.avatar_url ?? null }
                  : current.user,
              }
            : current,
        { revalidate: false },
      );
      toast.success("アイコン画像を更新しました");
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "アイコン画像の更新に失敗しました",
      );
    } finally {
      setAvatarUploading(false);
    }
  };

  const isAdmin =
    authStatus?.authenticated === true && authStatus.user?.role === "admin";
  const avatarUrl = authStatus?.user?.avatar_url || undefined;
  const avatarFallback = (
    authStatus?.user?.display_name?.trim() ||
    authStatus?.user?.username?.trim() ||
    "U"
  ).charAt(0).toUpperCase();

  const handleRestart = async () => {
    setRestarting(true);
    await performAdminRestart();
  };

  const handleLogout = async () => {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      const response = await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "include",
      });
      let result: {
        detail?: string;
        local_session_cleared?: boolean;
        global_revocation_required?: boolean;
        global_revocation?: boolean;
      } | null = null;
      try {
        result = (await response.json()) as {
          detail?: string;
          local_session_cleared?: boolean;
          global_revocation_required?: boolean;
          global_revocation?: boolean;
        };
      } catch {
        // A malformed response is not proof that the server revoked the session.
      }
      const localSessionCleared =
        result?.local_session_cleared === true ||
        (result?.local_session_cleared === undefined &&
          response.ok &&
          result?.global_revocation === true);
      if (!localSessionCleared) {
        throw new Error(
          result?.detail ||
            "ログアウトに失敗しました。時間をおいて再試行してください",
        );
      }

      // 別ユーザーへ情報が残らないよう、永続キャッシュ（SWR/チャット/Docs）を破棄する。
      try {
        await discardPendingPersistentWrites();
        await clearPersistentCache();
        resetChatMessageCacheMemory();
      } catch (error) {
        console.error("Failed to clear local caches after logout", error);
      }
      if (
        !response.ok ||
        (result?.global_revocation_required !== false &&
          result?.global_revocation !== true)
      ) {
        toast.warning(
          result?.detail ||
            "ローカルではログアウトしましたが、全セッションの失効を確認できませんでした",
        );
      }
      router.replace("/login");
      router.refresh();
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : "ログアウトに失敗しました。時間をおいて再試行してください",
      );
    } finally {
      setLoggingOut(false);
    }
  };

  return (
    <div className="relative shrink-0" data-shell-region="global-context-shell">
      <header
        className="ao-global-context flex min-h-14 h-14 items-center gap-2 border-b border-border bg-background px-2 sm:px-3 md:gap-3 md:px-4"
        data-shell-region="global-context"
      >
          <SidebarTrigger
            className="size-8 shrink-0"
            aria-label="Workspace Navigationを切り替え"
            onClick={() => {
              // A local drawer is the only secondary surface when it opens;
              // close shell overlays first to avoid stacked scrims at compact
              // desktop/mobile breakpoints.
              setRuntimePanelOpen(false);
              setContextRailOpen(false);
              setNotificationPanelOpen(false);
            }}
          />
        <Separator orientation="vertical" className="hidden h-5 md:block" />

        <div className="min-w-0 flex-1" data-shell-region="workspace-title">
          <div className="flex min-w-0 items-center gap-1.5 text-xs">
            <span className="hidden shrink-0 font-semibold text-foreground sm:inline">
              AoiTalk
            </span>
            <span className="hidden text-muted-foreground sm:inline">/</span>
            <span className="truncate font-medium text-foreground">
              {getWorkspaceTitle(pathname)}
            </span>
          </div>
        </div>

        <div
          className="flex min-w-0 shrink-0 items-center gap-1.5"
          data-shell-region="context-switchers"
        >
          {spaces.length > 0 && (
            <div className="flex min-w-0 items-center gap-1">
              <Layers
                className="size-3.5 shrink-0 text-muted-foreground"
                aria-hidden="true"
                data-testid="header-space-icon"
                style={selectedSpace?.color ? { color: selectedSpace.color } : undefined}
              />
              <AppSelect
                aria-label="スペース選択"
                value={selectedSpaceId ?? ""}
                onChange={(event) => setSelectedSpaceId(event.target.value)}
                triggerContent={
                  <span className="truncate">
                    {selectedSpace?.source === "remote" ? "[EP] " : ""}
                    {selectedSpace?.name ?? ""}
                  </span>
                }
                className={`${selectClassName} max-w-[7rem] md:max-w-[10rem]`}
              >
                {spaces.map((space) => (
                  <option key={space.id} value={space.id}>
                    <span className="inline-flex items-center gap-1.5">
                      <ResourceColorDot color={space.color} />
                      <span>
                        {space.source === "remote" ? "[EP] " : ""}
                        {space.name}
                      </span>
                    </span>
                  </option>
                ))}
              </AppSelect>
            </div>
          )}
          {projects.length > 0 && (
            <div className="hidden min-w-0 items-center gap-1 md:flex">
              <FolderOpen
                className="size-3.5 shrink-0 text-muted-foreground"
                aria-hidden="true"
                data-testid="header-project-icon"
                style={
                  selectedProject?.color
                    ? { color: selectedProject.color }
                    : undefined
                }
              />
              <AppSelect
                aria-label="プロジェクト選択"
                value={selectedProjectId ?? ""}
                onChange={(event) => setSelectedProjectId(event.target.value)}
                triggerContent={
                  <span className="truncate">
                    {selectedProject?.source === "remote" ? "[EP] " : ""}
                    {selectedProject?.name ?? ""}
                  </span>
                }
                className={`${selectClassName} max-w-[11rem]`}
              >
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    <span className="inline-flex items-center gap-1.5">
                      <ResourceColorDot color={project.color} />
                      <span>
                        {project.source === "remote" ? "[EP] " : ""}
                        {project.name}
                      </span>
                    </span>
                  </option>
                ))}
              </AppSelect>
              {remoteErrors.length > 0 && (
                <span
                  className="max-w-28 truncate text-[10px] text-amber-600"
                  title={remoteErrors.join("\n")}
                  role="status"
                >
                  Remote接続エラー
                </span>
              )}
            </div>
          )}
        </div>

        <Button
          type="button"
          variant="ghost"
          size="default"
          className="ao-search-trigger h-8 w-8 justify-start overflow-hidden rounded border border-border bg-surface-charcoal px-2 text-muted-foreground hover:bg-surface-container hover:text-foreground md:w-64"
          aria-label="検索・コマンドパレットを開く"
          title="検索・コマンド (Ctrl/Cmd+K)"
          onClick={() => window.dispatchEvent(new Event("global-command-palette"))}
        >
          <Search className="size-4" />
          <span className="hidden min-w-0 flex-1 truncate text-left text-xs font-normal md:inline">
            Search workspace…
          </span>
          <kbd className="hidden shrink-0 rounded border border-border bg-surface-container px-1 font-mono text-[10px] text-muted-foreground md:inline">
            ⌘K
          </kbd>
        </Button>

        {activeEntry && (
          <div
            className="flex max-w-[7.5rem] shrink-0 items-center gap-0.5 rounded-md border border-input bg-card px-1 py-1 md:max-w-[13rem] md:gap-1 md:px-1.5"
            data-shell-region="timer"
          >
            <Timer className="size-3.5 shrink-0 animate-pulse text-yellow-500" />
            <button
              type="button"
              className="flex min-w-0 items-center gap-1 rounded px-0.5 text-left hover:bg-accent/75 md:px-1"
              onClick={() => {
                if (activeEntry.task_id) router.push(`/tasks/${activeEntry.task_id}`);
              }}
              title="タスクを開く"
            >
              <span className="hidden max-w-[7rem] truncate text-xs font-medium lg:inline">
                {activeEntry.task_title || "タスク"}
              </span>
              <span className="text-xs tabular-nums text-muted-foreground">{elapsed}</span>
            </button>
            <Button
              variant="ghost"
              size="icon-sm"
              className="size-6 shrink-0"
              onClick={(event) => {
                event.stopPropagation();
                void stopTimer();
              }}
              title="タイマー停止"
              aria-label="タイマー停止"
            >
              <Square className="size-3 fill-current text-red-500" />
            </Button>
          </div>
        )}

        <button
          type="button"
          data-runtime-panel-trigger="true"
          className="hidden size-8 shrink-0 items-center justify-center rounded-md border border-border/70 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring md:inline-flex"
          aria-label="Runtime設定を開く"
          title="Runtime設定"
          onClick={toggleRuntimePanel}
        >
          <span
            className={`size-2.5 rounded-full ${pythonConnected ? "bg-emerald-500" : "bg-muted-foreground/40"}`}
          />
        </button>

        <div className="shrink-0 md:hidden">
          <NotificationBellPopover
            mobileOnly
            triggerClassName="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            onOpenChange={(open) => {
              if (open) {
                window.dispatchEvent(new Event("global-close-workspace-navigation"));
              }
            }}
          />
        </div>

        <input
          ref={avatarInputRef}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={handleAvatarChange}
        />
        <Button
          variant="ghost"
          size="icon-sm"
          className="hidden size-8 shrink-0 rounded-md text-muted-foreground hover:bg-accent hover:text-foreground md:inline-flex"
          title="ショートカット一覧 (?)"
          aria-label="ショートカット一覧"
          onClick={() => window.dispatchEvent(new Event("global-shortcuts-help"))}
        >
          <HelpCircle className="size-4" />
        </Button>

        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label="ユーザーメニューを開く"
            className="shrink-0 rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Avatar className="size-8 ring-1 ring-border">
              {avatarUrl && (
                <AvatarImage
                  src={avatarUrl}
                  alt={`${authStatus?.user?.display_name || authStatus?.user?.username || "ユーザー"}のアイコン`}
                />
              )}
              <AvatarFallback className="text-xs">{avatarFallback}</AvatarFallback>
            </Avatar>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem
              className="md:hidden"
              onClick={() => window.dispatchEvent(new Event("global-open-home"))}
            >
              <Home className="mr-2 size-4" />
              Todayを開く
            </DropdownMenuItem>
            <DropdownMenuItem className="md:hidden" onClick={toggleRuntimePanel}>
              <span
                className={`mr-2 size-2.5 rounded-full ${pythonConnected ? "bg-emerald-500" : "bg-muted-foreground/40"}`}
              />
              Runtime設定を開く
            </DropdownMenuItem>
            <DropdownMenuItem className="md:hidden" onClick={toggleTheme}>
              {visibleResolvedTheme === "dark" ? (
                <Moon className="mr-2 size-4" />
              ) : (
                <Sun className="mr-2 size-4" />
              )}
              テーマを{nextThemeLabel}に切り替え
            </DropdownMenuItem>
            <DropdownMenuSeparator className="md:hidden" />
            <DropdownMenuItem
              mnemonic="I"
              disabled={avatarUploading}
              onClick={() => avatarInputRef.current?.click()}
            >
              {avatarUploading ? (
                <Loader2 className="mr-2 size-4 animate-spin" />
              ) : (
                <Camera className="mr-2 size-4" />
              )}
              {avatarUploading ? "アップロード中..." : "アイコン画像を変更"}
            </DropdownMenuItem>
            <DropdownMenuItem
              mnemonic="S"
              onClick={() => router.push(USER_SETTINGS_HREF)}
            >
              <Settings className="mr-2 size-4" />
              ユーザー設定
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            {isAdmin && (
              <DropdownMenuItem mnemonic="R" onClick={handleRestart} disabled={restarting}>
                <RotateCcw className={`mr-2 size-4 ${restarting ? "animate-spin" : ""}`} />
                {restarting ? "再起動中..." : "再起動"}
              </DropdownMenuItem>
            )}
            <DropdownMenuItem
              mnemonic="L"
              onClick={handleLogout}
              disabled={loggingOut}
            >
              {loggingOut ? (
                <Loader2 className="mr-2 size-4 animate-spin" />
              ) : (
                <LogOut className="mr-2 size-4" />
              )}
              {loggingOut ? "ログアウト中..." : "ログアウト"}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      <RuntimeUtilityPanel
        open={runtimePanelOpen}
        onClose={() => setRuntimePanelOpen(false)}
        pythonConnected={pythonConnected}
        runtimeFeatures={runtimeFeatures}
        changeRuntimeFeature={changeRuntimeFeature}
        changeRuntimeFeatures={changeRuntimeFeatures}
        voiceStatus={voiceStatus}
      />

    </div>
  );
}

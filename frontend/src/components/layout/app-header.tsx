"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import {
  LogOut,
  FolderOpen,
  Square,
  Timer,
  Layers,
  RotateCcw,
  HelpCircle,
  Camera,
  ChevronLeft,
  ChevronRight,
  Bot as BotIcon,
  Mic,
  Volume2,
  Moon,
  Sun,
} from "lucide-react";
import { useProject } from "@/contexts/project-context";
import { useTheme } from "@/contexts/theme-context";
import { taskApi, type TimeEntry } from "@/lib/task-api";
import { APP_VIEW_TABS } from "@/lib/app-navigation";
import { formatTimerClock, getElapsedTimerSeconds } from "@/lib/task-time";
import { performAdminRestart } from "@/components/layout/global-admin-restart";
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
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const AVATAR_STORAGE_KEY = "user-avatar-image";

type LlmEngine = { provider: string; model: string; label: string };

type VoiceStatus = {
  ready: boolean;
  rms: number;
  recording: boolean;
};

type RuntimeFeatures = {
  features: Record<string, boolean>;
  discord_bot_service?: {
    state?: "stopped" | "starting" | "running" | "stopping" | "failed";
    user?: string | null;
    guild_count?: number;
    task_running?: boolean;
    last_error?: string | null;
  };
};

function useVoiceStatus(pythonConnected: boolean) {
  const [status, setStatus] = useState<VoiceStatus | null>(null);

  useEffect(() => {
    if (!pythonConnected) return;

    let mounted = true;

    const poll = async () => {
      try {
        const res = await fetch("/api/python-proxy/voice_status", {
          credentials: "include",
          signal: AbortSignal.timeout(3000),
        });
        if (res.ok && mounted) {
          setStatus(await res.json());
        }
      } catch {
        if (mounted) setStatus(null);
      }
    };

    poll();
    const interval = setInterval(poll, 3000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [pythonConnected]);

  return pythonConnected ? status : null;
}

function usePythonApi() {
  const [isConnected, setIsConnected] = useState(false);
  const [characters, setCharacters] = useState<string[]>([]);
  const [currentCharacter, setCurrentCharacter] = useState("");
  const [llmEngines, setLlmEngines] = useState<LlmEngine[]>([]);
  const [currentLlm, setCurrentLlm] = useState<{
    provider: string;
    model: string;
  } | null>(null);
  const [runtimeFeatures, setRuntimeFeatures] =
    useState<RuntimeFeatures | null>(null);

  const refreshRuntimeFeatures = useCallback(async () => {
    try {
      const res = await fetch("/api/python-proxy/runtime/features", {
        credentials: "include",
        signal: AbortSignal.timeout(3000),
      });
      if (!res.ok) return null;
      const data = await res.json();
      setRuntimeFeatures(data);
      return data as RuntimeFeatures;
    } catch {
      return null;
    }
  }, []);

  const fetchExtras = useCallback(async () => {
    try {
      const [charRes, llmRes, runtimeRes] = await Promise.all([
        fetch("/api/python-proxy/characters", {
          credentials: "include",
          signal: AbortSignal.timeout(3000),
        }),
        fetch("/api/python-proxy/llm/engine", {
          credentials: "include",
          signal: AbortSignal.timeout(3000),
        }),
        fetch("/api/python-proxy/runtime/features", {
          credentials: "include",
          signal: AbortSignal.timeout(3000),
        }),
      ]);
      if (charRes.ok) {
        const data = await charRes.json();
        setCharacters(data.characters ?? []);
        setCurrentCharacter(data.current ?? "");
      }
      if (llmRes.ok) {
        const data = await llmRes.json();
        setLlmEngines(data.available ?? []);
        setCurrentLlm({ provider: data.provider, model: data.model });
      }
      if (runtimeRes.ok) {
        const data = await runtimeRes.json();
        setRuntimeFeatures(data);
      }
    } catch {
      // エラー時は何もしない
    }
  }, []);

  useEffect(() => {
    let mounted = true;

    const check = async () => {
      try {
        const res = await fetch("/api/python-proxy/health", {
          signal: AbortSignal.timeout(3000),
        });
        const ok = res.ok;
        if (mounted) {
          setIsConnected(ok);
          if (ok) fetchExtras();
        }
      } catch {
        if (mounted) setIsConnected(false);
      }
    };

    check();
    const interval = setInterval(check, 15000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [fetchExtras]);

  const changeCharacter = useCallback(async (name: string) => {
    try {
      const res = await fetch(
        `/api/python-proxy/character/${encodeURIComponent(name)}`,
        {
          method: "POST",
          credentials: "include",
        },
      );
      if (res.ok) setCurrentCharacter(name);
    } catch (err) {
      console.error("キャラクター変更失敗:", err);
    }
  }, []);

  const changeLlmEngine = useCallback(
    async (provider: string, model: string) => {
      try {
        const res = await fetch("/api/python-proxy/llm/engine", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider, model }),
        });
        if (res.ok) setCurrentLlm({ provider, model });
      } catch (err) {
        console.error("LLMエンジン変更失敗:", err);
      }
    },
    [],
  );

  const changeRuntimeFeature = useCallback(
    async (feature: string, enabled: boolean) => {
      try {
        const res = await fetch("/api/python-proxy/runtime/features", {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ feature, enabled }),
        });
        if (res.ok) {
          setRuntimeFeatures(await res.json());
          setTimeout(() => {
            void refreshRuntimeFeatures();
          }, 1500);
        }
      } catch (err) {
        console.error("ランタイム機能変更失敗:", err);
      }
    },
    [refreshRuntimeFeatures],
  );

  const changeRuntimeFeatures = useCallback(
    async (features: Record<string, boolean>) => {
      let latest: RuntimeFeatures | null = null;
      try {
        for (const [feature, enabled] of Object.entries(features)) {
          const res = await fetch("/api/python-proxy/runtime/features", {
            method: "PATCH",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ feature, enabled }),
          });
          if (res.ok) latest = await res.json();
        }
        if (latest) setRuntimeFeatures(latest);
        setTimeout(() => {
          void refreshRuntimeFeatures();
        }, 1500);
      } catch (err) {
        console.error("ランタイム機能変更失敗:", err);
      }
    },
    [refreshRuntimeFeatures],
  );

  return {
    isConnected,
    characters,
    currentCharacter,
    changeCharacter,
    llmEngines,
    currentLlm,
    changeLlmEngine,
    runtimeFeatures,
    changeRuntimeFeature,
    changeRuntimeFeatures,
  };
}

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
      await taskApi.stopTimer(activeEntry.id);
      setActiveEntry(null);
      window.dispatchEvent(
        new CustomEvent("timer-changed", { detail: { activeEntry: null } }),
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
  "h-7 rounded-lg border border-input bg-white/45 px-2 text-xs text-foreground shadow-[inset_0_1px_rgba(255,255,255,0.65)] outline-none backdrop-blur-xl transition-colors focus-visible:border-ring dark:bg-white/10 dark:shadow-[inset_0_1px_rgba(255,255,255,0.12)]";

export function AppHeader() {
  const router = useRouter();
  const pathname = usePathname();
  const { resolvedTheme, setTheme } = useTheme();
  const {
    isConnected: pythonConnected,
    characters,
    currentCharacter,
    changeCharacter,
    llmEngines,
    currentLlm,
    changeLlmEngine,
    runtimeFeatures,
    changeRuntimeFeature,
    changeRuntimeFeatures,
  } = usePythonApi();
  const voiceStatus = useVoiceStatus(pythonConnected);
  const { activeEntry, elapsed, stopTimer } = useActiveTimer();
  const {
    spaces,
    selectedSpaceId,
    setSelectedSpaceId,
    projects,
    selectedProjectId,
    setSelectedProjectId,
  } = useProject();

  const [isAdmin, setIsAdmin] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [avatarSrc, setAvatarSrc] = useState<string>("");
  const avatarInputRef = useRef<HTMLInputElement>(null);

  const tabsScrollRef = useRef<HTMLDivElement>(null);
  const [canScrollTabsLeft, setCanScrollTabsLeft] = useState(false);
  const [canScrollTabsRight, setCanScrollTabsRight] = useState(false);
  const hasAutoScrolledTabsRef = useRef(false);
  const runtimeFeatureFlags = runtimeFeatures?.features ?? {};
  const discordBotService = runtimeFeatures?.discord_bot_service;
  const discordBotState = discordBotService?.state ?? "stopped";
  const discordBotTitle =
    discordBotState === "running"
      ? `Discord Bot: 稼働中${discordBotService?.user ? ` (${discordBotService.user})` : ""}`
      : discordBotState === "starting"
        ? "Discord Bot: 起動中"
        : discordBotState === "stopping"
          ? "Discord Bot: 停止中"
          : discordBotState === "failed"
            ? `Discord Bot: 起動失敗${discordBotService?.last_error ? ` - ${discordBotService.last_error}` : ""}`
            : "Discord Bot/VC";
  const nextThemeLabel = resolvedTheme === "dark" ? "ライト" : "ダーク";
  const toggleTheme = useCallback(() => {
    setTheme(resolvedTheme === "dark" ? "light" : "dark");
  }, [resolvedTheme, setTheme]);

  const updateTabScrollState = useCallback(() => {
    const el = tabsScrollRef.current;
    if (!el) return;
    setCanScrollTabsLeft(el.scrollLeft > 0);
    setCanScrollTabsRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 1);
  }, []);

  useEffect(() => {
    const el = tabsScrollRef.current;
    if (!el) return;
    updateTabScrollState();
    el.addEventListener("scroll", updateTabScrollState, { passive: true });
    const resizeObs = new ResizeObserver(updateTabScrollState);
    resizeObs.observe(el);
    return () => {
      el.removeEventListener("scroll", updateTabScrollState);
      resizeObs.disconnect();
    };
  }, [updateTabScrollState]);

  useEffect(() => {
    const el = tabsScrollRef.current;
    if (!el) return;
    const activeLink = el.querySelector<HTMLElement>(
      '[data-tab-active="true"]',
    );
    if (activeLink) {
      activeLink.scrollIntoView({
        block: "nearest",
        inline: "nearest",
        behavior: hasAutoScrolledTabsRef.current ? "smooth" : "auto",
      });
    }
    hasAutoScrolledTabsRef.current = true;
  }, [pathname]);

  const scrollTabsBy = (direction: 1 | -1) => {
    const el = tabsScrollRef.current;
    if (!el) return;
    el.scrollBy({
      left: direction * Math.max(120, el.clientWidth * 0.6),
      behavior: "smooth",
    });
  };

  useEffect(() => {
    const stored = localStorage.getItem(AVATAR_STORAGE_KEY);
    if (stored) setAvatarSrc(stored);
  }, []);

  const handleAvatarChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      const dataUrl = ev.target?.result as string;
      localStorage.setItem(AVATAR_STORAGE_KEY, dataUrl);
      setAvatarSrc(dataUrl);
    };
    reader.readAsDataURL(file);
    e.target.value = "";
  };

  useEffect(() => {
    fetch("/api/auth/status", { credentials: "include" })
      .then((r) => r.json())
      .then((d) => {
        if (d.authenticated && d.user?.role === "admin") setIsAdmin(true);
      })
      .catch(() => {});
  }, []);

  const handleRestart = async () => {
    setRestarting(true);
    await performAdminRestart();
  };

  const handleLogout = async () => {
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "include",
      });
    } finally {
      window.location.href = "/login";
    }
  };

  return (
    <div className="relative shrink-0">
      <header className="ao-topbar flex h-16 items-center gap-2 px-2 md:px-4">
        {/* 左: SidebarTrigger + ビュー切替タブ */}
        <SidebarTrigger className="-ml-1 shrink-0" />
        <Separator
          orientation="vertical"
          className="mr-2 h-4 hidden md:block"
        />

        <nav className="flex min-w-0 flex-1 items-center md:flex-initial">
          <button
            type="button"
            onClick={() => scrollTabsBy(-1)}
            tabIndex={canScrollTabsLeft ? 0 : -1}
            aria-label="タブを左にスクロール"
            aria-hidden={!canScrollTabsLeft}
            className={`shrink-0 rounded-lg p-0.5 text-muted-foreground transition-opacity hover:bg-white/55 hover:text-foreground dark:hover:bg-white/12 ${
              canScrollTabsLeft
                ? "opacity-100"
                : "pointer-events-none opacity-0"
            }`}
          >
            <ChevronLeft className="size-4" />
          </button>
          <div
            ref={tabsScrollRef}
            className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto whitespace-nowrap [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          >
            {APP_VIEW_TABS.map((tab) => {
              const isActive = pathname.startsWith(tab.href);
              return (
                <Link
                  key={tab.href}
                  href={tab.href}
                  data-tab-active={isActive}
                  className={`shrink-0 rounded-xl px-3 py-1.5 text-xs font-semibold transition-all ${
                    isActive
                      ? "bg-primary text-primary-foreground shadow-[inset_0_1px_rgba(255,255,255,0.28),0_14px_26px_-22px_rgba(0,90,120,0.95)] ring-1 ring-white/55"
                      : "border border-white/50 bg-white/36 text-muted-foreground shadow-[inset_0_1px_rgba(255,255,255,0.62)] hover:bg-white/66 hover:text-foreground dark:border-white/12 dark:bg-white/8 dark:shadow-[inset_0_1px_rgba(255,255,255,0.12)] dark:hover:bg-white/14"
                  }`}
                >
                  {tab.title}
                </Link>
              );
            })}
          </div>
          <button
            type="button"
            onClick={() => scrollTabsBy(1)}
            tabIndex={canScrollTabsRight ? 0 : -1}
            aria-label="タブを右にスクロール"
            aria-hidden={!canScrollTabsRight}
            className={`shrink-0 rounded-lg p-0.5 text-muted-foreground transition-opacity hover:bg-white/55 hover:text-foreground dark:hover:bg-white/12 ${
              canScrollTabsRight
                ? "opacity-100"
                : "pointer-events-none opacity-0"
            }`}
          >
            <ChevronRight className="size-4" />
          </button>
        </nav>

        <Separator
          orientation="vertical"
          className="mx-1 h-4 hidden md:block"
        />

        {/* スペース選択 */}
        {spaces.length > 0 && (
          <div className="hidden md:flex shrink-0 items-center gap-1.5">
            <Layers className="size-3.5 text-muted-foreground" />
            <select
              value={selectedSpaceId ?? ""}
              onChange={(e) => setSelectedSpaceId(e.target.value)}
              className={selectClassName}
            >
              {spaces.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* プロジェクト選択 */}
        {projects.length > 0 && (
          <div className="hidden md:flex shrink-0 items-center gap-1.5">
            <FolderOpen className="size-3.5 text-muted-foreground" />
            <select
              value={selectedProjectId ?? ""}
              onChange={(e) => setSelectedProjectId(e.target.value)}
              className={selectClassName}
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
        )}

        {pythonConnected && (
          <div className="hidden md:flex shrink-0 items-center gap-2">
            {runtimeFeatures && (
              <div className="flex items-center gap-1">
                {[
                  { key: "local_mic", icon: Mic, title: "ローカルマイク入力" },
                  { key: "tts", icon: Volume2, title: "読み上げ" },
                  { key: "discord", icon: BotIcon, title: discordBotTitle },
                ].map(({ key, icon: Icon, title }) => {
                  const enabled =
                    key === "discord"
                      ? !!(
                          runtimeFeatureFlags.discord_bot &&
                          runtimeFeatureFlags.discord_text &&
                          runtimeFeatureFlags.discord_vc_input &&
                          runtimeFeatureFlags.discord_vc_output
                        )
                      : !!runtimeFeatureFlags[key];
                  const discordFailed =
                    key === "discord" &&
                    enabled &&
                    discordBotState === "failed";
                  const discordChanging =
                    key === "discord" &&
                    enabled &&
                    (discordBotState === "starting" ||
                      discordBotState === "stopping");
                  const discordRunning =
                    key === "discord" &&
                    enabled &&
                    discordBotState === "running";
                  const enabledClass =
                    key === "discord"
                      ? discordFailed
                        ? "border-destructive/60 bg-destructive text-destructive-foreground shadow-sm shadow-red-900/15"
                        : discordChanging
                          ? "border-amber-500/70 bg-amber-500 text-white shadow-sm shadow-amber-900/15"
                          : discordRunning
                            ? "border-emerald-500/70 bg-emerald-500 text-white shadow-sm shadow-emerald-900/15"
                            : "border-primary/50 bg-primary text-primary-foreground shadow-sm shadow-cyan-800/15"
                      : "border-primary/50 bg-primary text-primary-foreground shadow-sm shadow-cyan-800/15";
                  return (
                    <button
                      key={key}
                      type="button"
                      onClick={() => {
                        if (key === "discord") {
                          const nextEnabled = !enabled;
                          changeRuntimeFeatures({
                            discord_bot: nextEnabled,
                            discord_text: nextEnabled,
                            discord_vc_input: nextEnabled,
                            discord_vc_output: nextEnabled,
                            tts: nextEnabled || !!runtimeFeatureFlags.local_speaker,
                          });
                          return;
                        }
                        changeRuntimeFeature(key, !enabled);
                      }}
                      title={title}
                      aria-label={title}
                      className={`relative inline-flex size-7 items-center justify-center rounded border text-xs transition-colors ${
                        enabled
                          ? enabledClass
                          : "border-input bg-white/35 text-muted-foreground backdrop-blur-xl hover:bg-accent/75 hover:text-foreground dark:bg-white/8 dark:hover:bg-white/14"
                      }`}
                    >
                      <Icon className="size-3.5" />
                      {key === "discord" && enabled && (
                        <span
                          className={`absolute -right-0.5 -top-0.5 size-2 rounded-full border border-background ${
                            discordFailed
                              ? "bg-destructive"
                              : discordChanging
                                ? "bg-amber-300"
                                : discordRunning
                                  ? "bg-emerald-300"
                                  : "bg-primary-foreground"
                          }`}
                        />
                      )}
                    </button>
                  );
                })}
              </div>
            )}
            {/* キャラクター */}
            {characters.length > 0 && (
              <select
                value={currentCharacter}
                onChange={(e) => changeCharacter(e.target.value)}
                className={`${selectClassName} max-w-[140px]`}
              >
                {characters.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            )}
            {/* LLMエンジン */}
            {llmEngines.length > 0 && currentLlm && (
              <select
                value={`${currentLlm.provider}::${currentLlm.model}`}
                onChange={(e) => {
                  const [provider, model] = e.target.value.split("::");
                  changeLlmEngine(provider, model);
                }}
                className={`${selectClassName} max-w-[140px]`}
              >
                {llmEngines.map((eng) => (
                  <option
                    key={`${eng.provider}::${eng.model}`}
                    value={`${eng.provider}::${eng.model}`}
                  >
                    {eng.label}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        {/* スペーサー（md以上でのみタブが縮まないよう伸長） */}
        <div className="hidden md:block md:flex-1" />

        {/* アクティブタイマー */}
        {activeEntry && (
          <div className="mr-1 flex shrink-0 items-center gap-1.5 rounded-lg border border-input bg-white/45 px-2 py-1 shadow-[inset_0_1px_rgba(255,255,255,0.68)] backdrop-blur-xl dark:bg-white/10 dark:shadow-[inset_0_1px_rgba(255,255,255,0.12)]">
            <Timer className="size-3.5 text-yellow-500 animate-pulse" />
            <button
              type="button"
              className="flex min-w-0 items-center gap-1.5 rounded-md px-1 py-0.5 text-left hover:bg-accent/75"
              onClick={() => {
                if (activeEntry.task_id) {
                  router.push(`/tasks/${activeEntry.task_id}`);
                }
              }}
              title="タスクを開く"
            >
              <span className="hidden md:inline max-w-[120px] truncate text-xs font-medium">
                {activeEntry.task_title || "タスク"}
              </span>
              <span className="text-xs tabular-nums text-muted-foreground">
                {elapsed}
              </span>
            </button>
            <Button
              variant="ghost"
              size="icon"
              className="size-5"
              onClick={(e) => {
                e.stopPropagation();
                void stopTimer();
              }}
              title="タイマー停止"
            >
              <Square className="size-3 fill-current text-red-500" />
            </Button>
          </div>
        )}

        {/* API/WS ステータス + ユーザーアバター */}
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger className="flex shrink-0 items-center gap-1.5 mr-2 cursor-default">
              <span
                className={`inline-block size-2.5 rounded-full ${
                  pythonConnected
                    ? "bg-green-500 shadow-[0_0_6px_rgba(34,197,94,0.5)]"
                    : "bg-muted-foreground/40"
                }`}
              />
              {!pythonConnected && (
                <span className="text-xs text-muted-foreground">
                  オフライン
                </span>
              )}
            </TooltipTrigger>
            <TooltipContent>
              {pythonConnected ? "Python API: 接続中" : "Python API: 未接続"}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <input
          ref={avatarInputRef}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={handleAvatarChange}
        />

        <button
          type="button"
          className="inline-flex size-7 items-center justify-center rounded-full text-muted-foreground outline-none transition-colors hover:bg-white/55 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring dark:hover:bg-white/12"
          title={`テーマを${nextThemeLabel}に切り替え`}
          aria-label={`テーマを${nextThemeLabel}に切り替え`}
          onClick={toggleTheme}
        >
          {resolvedTheme === "dark" ? (
            <Moon className="size-4" />
          ) : (
            <Sun className="size-4" />
          )}
        </button>

        <Button
          variant="ghost"
          size="icon"
          className="hidden md:inline-flex size-7 rounded-full text-muted-foreground hover:bg-white/55 hover:text-foreground dark:hover:bg-white/12"
          title="ショートカット一覧 (?)"
          onClick={() =>
            window.dispatchEvent(new Event("global-shortcuts-help"))
          }
        >
          <HelpCircle className="size-4" />
        </Button>

        <DropdownMenu>
          <DropdownMenuTrigger className="shrink-0 rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <Avatar className="size-7 ring-1 ring-border/70 shadow-[inset_0_1px_rgba(255,255,255,0.7)]">
              {avatarSrc && (
                <AvatarImage src={avatarSrc} alt="ユーザーアイコン" />
              )}
              <AvatarFallback className="text-xs">U</AvatarFallback>
            </Avatar>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={() => avatarInputRef.current?.click()}>
              <Camera className="mr-2 size-4" />
              アイコン画像を変更
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            {isAdmin && (
              <DropdownMenuItem onClick={handleRestart} disabled={restarting}>
                <RotateCcw
                  className={`mr-2 size-4 ${restarting ? "animate-spin" : ""}`}
                />
                {restarting ? "再起動中..." : "再起動"}
              </DropdownMenuItem>
            )}
            <DropdownMenuItem onClick={handleLogout}>
              <LogOut className="mr-2 size-4" />
              ログアウト
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      {/* 音声ステータス: voice_chatモード時のみヘッダー下にオーバーレイ */}
      {pythonConnected && voiceStatus && voiceStatus.ready && (
        <div className="absolute left-1/2 top-16 z-50 flex -translate-x-1/2 items-center gap-2 rounded-b-lg border border-t-0 border-border/70 bg-background/82 px-4 py-1.5 shadow-md backdrop-blur-2xl">
          <span
            className={`inline-block size-2 rounded-full ${
              voiceStatus.recording
                ? "bg-red-500 animate-pulse"
                : voiceStatus.ready
                  ? "bg-green-500"
                  : "bg-muted-foreground/40"
            }`}
          />
          <span className="text-xs text-muted-foreground">
            {voiceStatus.recording
              ? "録音中"
              : voiceStatus.ready
                ? "待機"
                : "停止"}
          </span>
          <div className="w-[80px] h-[3px] rounded-full bg-muted overflow-hidden">
            <div
              className="h-full bg-blue-500 transition-all duration-200"
              style={{ width: `${Math.min(voiceStatus.rms * 100, 100)}%` }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

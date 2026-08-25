"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import {
  BookOpen,
  Download,
  Eye,
  FileText,
  GitBranch,
  Library,
  Loader2,
  MessagesSquare,
  PencilLine,
  Search,
  Settings2,
  Users,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { chatApi } from "@/lib/chat-api";
import { storyApi, type StorySearchHit } from "@/lib/story/api";
import { normalizeEpisode, normalizeWork, type StoryWorkView } from "@/lib/story/view-model";
import { EMPTY_WORK } from "@/components/story/hooks/use-story-data";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { saveStatusLabel, type StorySaveState } from "@/components/story/shell/save-status";
import {
  navigateAfterFlush as navigateAfterStoryFlush,
  navigateStoryWorkspaceSegment,
} from "@/components/story/shell/story-navigation";
import {
  applyWritingBusyAfterStartChatWriting,
  executeStartChatWriting,
  StoryWorkspaceLeavingLock,
} from "@/components/story/shell/story-chat-writing";
import {
  STORY_WORKSPACE_SHELL_CONTENT_CLASS,
  STORY_WORKSPACE_SHELL_HEADER_CLASS,
  STORY_WORKSPACE_SHELL_OUTER_CLASS,
  storyWorkspaceShellBusyModifier,
} from "@/components/story/shell/story-workspace-layout";

export type { StorySaveState };

export type StorySaveScope = {
  id: string;
  isDirty: () => boolean;
  isSaving?: () => boolean;
  isFailed?: () => boolean;
  flush: () => Promise<boolean>;
};

type StoryWorkContextValue = {
  work: StoryWorkView;
  /** overview の取得が完了したか。未完了の間は work の各カウントが既定値のままになる。 */
  isLoaded: boolean;
  saveWork: (patch: Record<string, unknown>) => Promise<void>;
  saveState: StorySaveState;
  markDirty: () => void;
  registerSaveScope: (scope: StorySaveScope) => () => void;
  flushAllScopes: () => Promise<boolean>;
};

const StoryWorkContext = createContext<StoryWorkContextValue | null>(null);

export function useStoryWorkContext(): StoryWorkContextValue {
  const value = useContext(StoryWorkContext);
  if (!value) throw new Error("StoryWorkContext がありません");
  return value;
}

const navItems = [
  { segment: "settings", label: "作品設定", icon: Settings2, count: (work: StoryWorkView) => work.notesCount },
  { segment: "cast", label: "登場人物", icon: Users, count: (work: StoryWorkView) => work.charactersCount },
  { segment: "rules", label: "ルールブック", icon: BookOpen, count: (work: StoryWorkView) => work.rulebooksCount },
  { segment: "manuscript", label: "章と本文", icon: FileText, count: (work: StoryWorkView) => work.episodeCount },
  { segment: "map", label: "分岐マップ", icon: GitBranch, count: (work: StoryWorkView) => work.branchCount },
  { segment: "review", label: "通し読み", icon: Eye, count: () => null },
];

/** 作品詳細の Workspace Navigation。Shared Shell の左スロットへ登録する。 */
function StoryWorkspaceNavigation({
  work,
  activeSegment,
  onNavigate,
  writingBusy,
}: {
  work: StoryWorkView;
  activeSegment: string;
  onNavigate: (segment: string) => void;
  writingBusy: boolean;
}) {
  return (
    <aside
      className={`relative flex h-full min-h-0 w-full flex-col bg-surface-charcoal text-on-surface${writingBusy ? " pointer-events-none select-none" : ""}`}
      data-testid="story-workspace-navigation"
      data-shell-workspace="story"
      aria-busy={writingBusy}
    >
      <div className="border-b border-border-subtle px-4 py-4">
        <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-on-surface-variant">
          Story Studio
        </div>
        <div className="mt-1 truncate text-sm font-medium" title={work.title}>
          {work.title || "無題の作品"}
        </div>
      </div>
      <Tabs
        orientation="vertical"
        value={activeSegment}
        onValueChange={onNavigate}
          className="min-h-0 flex-1 overflow-y-auto"
      >
        <TabsList
          variant="line"
          className="flex h-auto w-full flex-col gap-0.5 rounded-none p-3"
        >
          {navItems.map(({ segment, label, icon: Icon, count }) => {
            const badge = count(work);
            return (
              <TabsTrigger
                key={segment}
                value={segment}
                className="min-w-0 justify-start rounded-sm border-l-2 border-transparent px-2 py-1.5 text-[13px] text-on-surface-variant transition-colors hover:bg-surface-container-high hover:text-on-surface data-[state=active]:border-primary data-[state=active]:bg-surface-container-highest data-[state=active]:text-primary"
              >
                <Icon className="size-4" />
                <span className="truncate">{label}</span>
                {typeof badge === "number" && badge > 0 && (
                  <span
                    className="ml-auto rounded-sm bg-surface-container-high px-1.5 py-0.5 text-[10px] text-on-surface-variant"
                    data-testid={`story-nav-badge-${segment}`}
                  >
                    {badge}
                  </span>
                )}
              </TabsTrigger>
            );
          })}
        </TabsList>
      </Tabs>
      {writingBusy && <StoryWorkspaceLeavingLock testId="story-workspace-nav-leaving-lock" />}
    </aside>
  );
}

export function StoryWorkspaceShell({ workId, children }: { workId: string; children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { data, error, mutate } = useSWR(`story-work-shell:${workId}`, () => storyApi.getOverview(workId));
  const work = useMemo(() => data ? normalizeWork(data) : { ...EMPTY_WORK, id: workId }, [data, workId]);
  const [saveState, setSaveState] = useState<StorySaveState>("saved");
  const [scopeSnapshot, setScopeSnapshot] = useState(0);
  const [scopeFailed, setScopeFailed] = useState(false);
  const scopesRef = useRef<Map<string, StorySaveScope>>(new Map());
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [writingBusy, setWritingBusy] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<StorySearchHit[]>([]);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchBusy, setSearchBusy] = useState(false);

  const registerSaveScope = useCallback((scope: StorySaveScope) => {
    scopesRef.current.set(scope.id, scope);
    setScopeSnapshot((value) => value + 1);
    return () => {
      scopesRef.current.delete(scope.id);
      setScopeSnapshot((value) => value + 1);
    };
  }, []);

  const flushAllScopes = useCallback(async (): Promise<boolean> => {
    const scopes = Array.from(scopesRef.current.values());
    let ok = true;
    for (const scope of scopes) {
      if (!scope.isDirty()) continue;
      const flushed = await scope.flush();
      if (!flushed) ok = false;
    }
    setScopeFailed(!ok);
    return ok;
  }, []);

  const navigateAfterFlush = useCallback(
    (href: string) => navigateAfterStoryFlush(flushAllScopes, router.push, href),
    [flushAllScopes, router],
  );

  const aggregatedSaveState = useMemo<StorySaveState>(() => {
    const scopes = Array.from(scopesRef.current.values());
    if (saveState === "saving" || scopes.some((scope) => scope.isSaving?.())) return "saving";
    if (scopeFailed || scopes.some((scope) => scope.isFailed?.())) return "failed";
    if (saveState === "dirty" || scopes.some((scope) => scope.isDirty())) return "dirty";
    return "saved";
  }, [saveState, scopeFailed, scopeSnapshot]);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      const scopes = Array.from(scopesRef.current.values());
      const hasUnsaved =
        saveState === "dirty" || scopes.some((scope) => scope.isDirty());
      const isSaving =
        saveState === "saving" || scopes.some((scope) => scope.isSaving?.());
      if (!hasUnsaved && !isSaving) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [saveState, scopeSnapshot]);

  const markDirty = useCallback(() => {
    setScopeFailed(false);
    setSaveState("dirty");
  }, []);

  const saveWork = useCallback(async (patch: Record<string, unknown>) => {
    setSaveState("saving");
    try {
      await storyApi.updateWork(workId, patch);
      await mutate();
      setSaveState("saved");
      setScopeFailed(false);
    } catch (saveError) {
      setSaveState("dirty");
      setScopeFailed(true);
      toast.error(saveError instanceof Error ? saveError.message : "作品を保存できませんでした");
      throw saveError;
    }
  }, [mutate, workId]);

  const startTitleEdit = () => {
    setTitleDraft(work.title);
    setEditingTitle(true);
  };

  const commitTitle = useCallback(async (): Promise<boolean> => {
    const nextTitle = titleDraft.trim();
    if (!nextTitle || nextTitle === work.title) {
      setEditingTitle(false);
      return true;
    }
    try {
      await saveWork({ title: nextTitle });
      setEditingTitle(false);
      return true;
    } catch {
      return false;
    }
  }, [saveWork, titleDraft, work.title]);

  const isTitleDirty = useCallback(() => {
    if (!editingTitle) return false;
    const nextTitle = titleDraft.trim();
    return Boolean(nextTitle) && nextTitle !== work.title;
  }, [editingTitle, titleDraft, work.title]);

  useEffect(() => {
    return registerSaveScope({
      id: "header-title",
      isDirty: isTitleDirty,
      isSaving: () => saveState === "saving",
      flush: commitTitle,
    });
  }, [commitTitle, isTitleDirty, registerSaveScope, saveState]);

  /**
   * §4.12 チャット連携の入口。会話セッションを作ってから `story_writing_sessions` を
   * 作成し、返ってきた会話セッションIDでチャット画面へ遷移する。
   * 対象章は執筆ビューで開いている章（`?episode=`）、無ければ作品の開始章。
   */
  const startChatWriting = async () => {
    if (writingBusy) return;
    setWritingBusy(true);
    const succeeded = await executeStartChatWriting({
      flushAllScopes,
      push: router.push,
      prepareChatHref: async () => {
        const queryEpisodeId = new URLSearchParams(window.location.search).get("episode");
        const targetEpisodeId = queryEpisodeId || work.startEpisodeId || null;
        let episodeTitle = "";
        if (targetEpisodeId) {
          // 章名はチャットのセッション名に使うだけなので、取得失敗でも執筆開始は止めない。
          episodeTitle = await storyApi.getEpisode(targetEpisodeId).then((value) => normalizeEpisode(value).title).catch(() => "");
        }
        const characterName = await chatApi.getCurrentCharacterName();
        const created = await chatApi.createSession(characterName);
        await chatApi.updateSessionTitle(created.session.id, `[執筆] ${work.title}${episodeTitle ? ` / ${episodeTitle}` : ""}`);
        const writing = await storyApi.startWriting(workId, {
          conversation_session_id: created.session.id,
          ...(targetEpisodeId ? { episode_id: targetEpisodeId } : {}),
        });
        return `/chat?s=${encodeURIComponent(writing?.conversation_session_id || created.session.id)}`;
      },
    });
    // 成功時は router.push 後もアンマウントまでロックを維持する（finally で解除しない）。
    applyWritingBusyAfterStartChatWriting(succeeded, setWritingBusy);
  };

  const exportWork = async () => {
    try {
      const blob = await storyApi.exportWork(workId, "route");
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${work.title || "story"}.txt`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (exportError) {
      toast.error(exportError instanceof Error ? exportError.message : "書き出しに失敗しました");
    }
  };

  useEffect(() => {
    const trimmed = searchQuery.trim();
    if (!trimmed) {
      setSearchResults([]);
      setSearchBusy(false);
      return;
    }
    setSearchBusy(true);
    const timer = window.setTimeout(() => {
      void storyApi.searchWork(workId, trimmed)
        .then((response) => setSearchResults(response.results ?? []))
        .catch(() => setSearchResults([]))
        .finally(() => setSearchBusy(false));
    }, 300);
    return () => window.clearTimeout(timer);
  }, [searchQuery, workId]);

  const openEpisodeFromSearch = useCallback(
    async (episodeId: string) => {
      const ok = await navigateAfterFlush(
        `/scenarios/${encodeURIComponent(workId)}/manuscript?episode=${encodeURIComponent(episodeId)}`,
      );
      if (!ok) return;
      setSearchOpen(false);
    },
    [navigateAfterFlush, workId],
  );

  const activeSegment = navItems.find((item) => pathname?.includes(`/scenarios/${workId}/${item.segment}`))?.segment ?? "manuscript";

  const handleNavigate = useCallback(
    async (segment: string) => {
      await navigateStoryWorkspaceSegment({
        writingBusy,
        segment,
        activeSegment,
        workId,
        navigateAfterFlush,
      });
    },
    [activeSegment, navigateAfterFlush, workId, writingBusy],
  );

  const contextValue = useMemo<StoryWorkContextValue>(
    () => ({
      work,
      isLoaded: Boolean(data),
      saveWork,
      saveState: aggregatedSaveState,
      markDirty,
      registerSaveScope,
      flushAllScopes,
    }),
    [aggregatedSaveState, data, flushAllScopes, markDirty, registerSaveScope, saveWork, work],
  );
  const storyNavigation = useMemo(
    () => (
      <StoryWorkspaceNavigation
        work={work}
        activeSegment={activeSegment}
        onNavigate={(segment) => void handleNavigate(segment)}
        writingBusy={writingBusy}
      />
    ),
    [activeSegment, handleNavigate, work, writingBusy],
  );
  const storyContextRail = useMemo(
    () => (
      <aside
        className={`relative flex min-h-0 flex-1 flex-col bg-surface-charcoal text-on-surface${writingBusy ? " pointer-events-none select-none" : ""}`}
        data-testid="story-context-rail"
        data-shell-workspace="story"
        aria-busy={writingBusy}
      >
        <div className="border-b border-border-subtle px-4 py-3">
          <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-on-surface-variant">
            Story Context
          </div>
          <div className="mt-1 text-sm font-medium">作品コンテキスト</div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <div className="rounded-sm border border-border-subtle bg-surface-container-lowest p-3 text-xs">
            <div className="flex items-center justify-between gap-2">
              <span className="text-on-surface-variant">保存状態</span>
              <span className="font-medium text-primary" data-testid="story-save-state">
                {saveStatusLabel(aggregatedSaveState)}
              </span>
            </div>
          </div>
          <div className="mt-4 space-y-2 text-xs text-on-surface-variant">
            <div className="flex items-center justify-between gap-2">
              <span>章</span>
              <span>{work.episodeCount}</span>
            </div>
            <div className="flex items-center justify-between gap-2">
              <span>分岐</span>
              <span>{work.branchCount}</span>
            </div>
            <div className="flex items-center justify-between gap-2">
              <span>文字数</span>
              <span>{work.totalChars.toLocaleString("ja-JP")}</span>
            </div>
          </div>
        </div>
        {writingBusy && <StoryWorkspaceLeavingLock testId="story-context-rail-leaving-lock" />}
      </aside>
    ),
    [aggregatedSaveState, work, writingBusy],
  );
  useWorkspaceShellRegistration({
    workspaceNavigation: storyNavigation,
    contextRail: storyContextRail,
    priority: 20,
    id: `story-workspace-${workId}`,
  });

  const shellBusyModifier = storyWorkspaceShellBusyModifier(writingBusy);

  return (
    <StoryWorkContext.Provider value={contextValue}>
      <div
        className={`${STORY_WORKSPACE_SHELL_OUTER_CLASS}${shellBusyModifier ? ` ${shellBusyModifier}` : ""}`}
        data-testid="story-workspace-shell"
        aria-busy={writingBusy}
        inert={writingBusy ? true : undefined}
      >
        <header className={STORY_WORKSPACE_SHELL_HEADER_CLASS}>
          <Button variant="link" size="sm" className="h-auto px-0 text-xs text-on-surface-variant hover:text-primary" onClick={() => void navigateAfterFlush("/scenarios")}>← 作品一覧</Button>
          <span className="h-4 w-px bg-border-subtle" aria-hidden="true" />
          {editingTitle ? (
            <form className="flex min-w-48 flex-1 items-center gap-2" onSubmit={(event) => { event.preventDefault(); void commitTitle(); }}>
              <Input autoFocus value={titleDraft} onChange={(event) => setTitleDraft(event.target.value)} onBlur={() => void commitTitle()} className="h-8 max-w-md" aria-label="作品名" />
            </form>
          ) : (
            <button type="button" onClick={startTitleEdit} className="group flex min-w-0 items-center gap-2 text-left" aria-label="作品名を編集">
              <h1 className="truncate text-[16px] font-semibold tracking-tight text-on-surface">{work.title}</h1>
              <PencilLine className="size-3.5 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
            </button>
          )}
          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground" aria-live="polite">
            {aggregatedSaveState === "saving" && <Loader2 className="size-3.5 animate-spin" />}
            {saveStatusLabel(aggregatedSaveState)}
          </span>
          <Popover open={searchOpen} onOpenChange={setSearchOpen}>
            <PopoverTrigger
              render={
                <div className="relative min-w-40 max-w-xs flex-1">
                  <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={searchQuery}
                    onChange={(event) => setSearchQuery(event.target.value)}
                    onFocus={() => setSearchOpen(true)}
                    placeholder="作品内を検索"
                    className="h-8 pl-8 text-xs"
                    aria-label="作品内を検索"
                    data-testid="story-work-search"
                  />
                </div>
              }
            />
            <PopoverContent className="w-[min(24rem,calc(100vw-2rem))] p-2" align="start">
              {searchBusy ? (
                <div className="flex items-center gap-2 px-2 py-1 text-xs text-muted-foreground">
                  <Loader2 className="size-3.5 animate-spin" /> 検索中…
                </div>
              ) : searchQuery.trim() && searchResults.length === 0 ? (
                <div className="px-2 py-1 text-xs text-muted-foreground">一致する章がありません</div>
              ) : (
                <ul className="max-h-64 space-y-1 overflow-y-auto">
                  {searchResults.map((hit) => (
                    <li key={hit.episode_id}>
                      <button
                        type="button"
                        className="w-full rounded-sm px-2 py-1.5 text-left hover:bg-surface-container-high"
                        onClick={() => void openEpisodeFromSearch(hit.episode_id)}
                      >
                        <div className="truncate text-xs font-medium text-on-surface">{hit.title}</div>
                        <div className="truncate text-[11px] text-muted-foreground">{hit.snippet}</div>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </PopoverContent>
          </Popover>
          {error && <span className="text-xs text-destructive">作品情報を読み込めません</span>}
          <div className="ml-auto flex items-center gap-2">
            <Button variant="outline" size="sm" className="h-7 rounded-sm border-outline-variant bg-transparent text-xs text-on-surface hover:bg-surface-container-high" onClick={() => void startChatWriting()} disabled={writingBusy || !data} data-testid="story-start-chat-writing">
              {writingBusy ? <Loader2 className="size-3.5 animate-spin" /> : <MessagesSquare className="size-3.5" />} チャットで執筆
            </Button>
            <Button variant="outline" size="sm" className="h-7 rounded-sm border-outline-variant bg-transparent text-xs text-on-surface hover:bg-surface-container-high" onClick={() => void exportWork()}><Download className="size-3.5" /> TXT書き出し</Button>
            <Button variant="outline" size="sm" className="hidden h-7 rounded-sm border-outline-variant bg-transparent text-xs text-on-surface hover:bg-surface-container-high md:inline-flex" onClick={() => void navigateAfterFlush("/scenarios/library")}><Library className="size-3.5" />共有ライブラリ</Button>
          </div>
        </header>
        <div className={STORY_WORKSPACE_SHELL_CONTENT_CLASS}>{children}</div>
        {writingBusy && <StoryWorkspaceLeavingLock />}
      </div>
    </StoryWorkContext.Provider>
  );
}

"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { defaultFilter } from "cmdk";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Checkbox } from "@/components/ui/checkbox";
import { Plus, CheckSquare, FileText, Layers, FolderOpen } from "lucide-react";
import { taskApi, type Task } from "@/lib/task-api";
import { useProject } from "@/contexts/project-context";
import { APP_VIEW_TABS } from "@/lib/app-navigation";
import { DocsCommandItems } from "@/components/docs/docs-dialogs";
import {
  DOCS_COMMAND_OPEN_EVENT,
  useDocsCommandContext,
} from "@/components/docs/hooks/use-docs-command-palette";
import type { DocsCommandMode } from "@/components/docs/docs-workspace-shared";
import { useDocsPaletteSearch } from "./use-docs-palette-search";

const COMMANDS = [
  { id: "new-chat", label: "新規会話", icon: Plus, action: "/chat?new=1" },
  {
    id: "new-task",
    label: "新規タスク",
    icon: Plus,
    action: "__create-task__",
    keywords: "t task タスク作成",
  },
  ...APP_VIEW_TABS.map((tab) => ({
    id: tab.href.replace(/^\//, "") || "home",
    label: tab.title,
    icon: tab.icon,
    action: tab.href,
    keywords: `${tab.title} navigation`,
  })),
];

type SearchScope = {
  navigation: boolean;
  spaces: boolean;
  projects: boolean;
  tasks: boolean;
  docs: boolean;
};

type PendingPaletteAction = {
  event: Event;
  /** イベントを積んだ時点のpathname。画面遷移後は破棄する。 */
  pathname: string;
  /** task detailのように特定画面だけで処理するイベントの遷移先。 */
  targetPathname?: string;
};

const DEFAULT_SCOPE: SearchScope = {
  navigation: true,
  spaces: true,
  projects: true,
  tasks: true,
  docs: true,
};

const SCOPE_STORAGE_KEY = "command-palette-scope";

function filterPaletteItem(value: string, search: string, keywords?: string[]) {
  // Docs results are already filtered/ranked by the server, including body and
  // #tag matches. Preserve that order without changing other command filtering.
  if (value.startsWith("__docs_result__:")) return 1;
  if (value === "__docs_search_more__") return 0.5;
  return defaultFilter(value, search, keywords);
}

function readSearchScope(): SearchScope {
  if (typeof window === "undefined") return DEFAULT_SCOPE;
  try {
    const saved = localStorage.getItem(SCOPE_STORAGE_KEY);
    if (!saved) return DEFAULT_SCOPE;
    return { ...DEFAULT_SCOPE, ...JSON.parse(saved) };
  } catch {
    return DEFAULT_SCOPE;
  }
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [scope, setScope] = useState<SearchScope>(() => readSearchScope());
  const [docsMode, setDocsMode] = useState<DocsCommandMode>({ kind: "root" });
  const [query, setQuery] = useState("");
  const router = useRouter();
  const pathname = usePathname();
  const {
    spaces,
    participatingProjects: globalSelectableProjects,
    projects: selectedSpaceProjects,
    allProjects,
    selectedProjectId,
    setSelectedSpaceId,
    setSelectedProjectId,
  } = useProject();
  // Project navigation is global (the selected Space changes together with a
  // chosen Project), so use the participant-only projection rather than the
  // currently selected Space's narrower `projects` list.  Keep the fallback
  // for isolated consumers/tests that provide only `allProjects`.
  const selectableProjects =
    globalSelectableProjects ?? selectedSpaceProjects ?? allProjects;
  const docsContext = useDocsCommandContext();
  const pendingActionRef = useRef<PendingPaletteAction | null>(null);
  const docsSubmode = Boolean(docsContext && docsMode.kind !== "root");
  const docsSearchResult = useDocsPaletteSearch(query, open && !docsSubmode && scope.docs);
  const resetDocsSearch = docsSearchResult.reset;

  const updateScope = (key: keyof SearchScope, value: boolean) => {
    if (key === "docs") {
      resetDocsSearch();
    }
    setScope((prev) => {
      const next = { ...prev, [key]: value };
      try {
        localStorage.setItem(SCOPE_STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((prev) => {
          setDocsMode({ kind: "root" });
          setQuery("");
          resetDocsSearch();
          return !prev;
        });
      }
    };
    const handleDocsCommandOpen = (event: Event) => {
      const mode = (event as CustomEvent<DocsCommandMode>).detail ?? {
        kind: "root" as const,
      };
      setDocsMode(mode);
      setQuery("");
      resetDocsSearch();
      setOpen(true);
    };
    const handleGlobalCommandOpen = () => {
      setDocsMode({ kind: "root" });
      setQuery("");
      resetDocsSearch();
      setOpen(true);
    };
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener(DOCS_COMMAND_OPEN_EVENT, handleDocsCommandOpen);
    window.addEventListener("global-command-palette", handleGlobalCommandOpen);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener(
        DOCS_COMMAND_OPEN_EVENT,
        handleDocsCommandOpen,
      );
      window.removeEventListener("global-command-palette", handleGlobalCommandOpen);
    };
  }, [resetDocsSearch]);

  useEffect(() => {
    if (!open || !scope.tasks) return;
    taskApi
      .listTasks()
      .then(setTasks)
      .catch(() => {});
  }, [open, scope.tasks]);

  // CommandDialog のclose commit後に、次のrAFで後続ダイアログを開く。
  // 同じtickに同期dispatchすると、close側のfocus復帰と次Dialogのopenが
  // 競合するため、イベントはrefへ積んでからcloseし、open=false後に予約する。
  useEffect(() => {
    if (open || !pendingActionRef.current) return;

    const pendingAction = pendingActionRef.current;
    pendingActionRef.current = null;
    if (
      pendingAction.pathname !== pathname ||
      (pendingAction.targetPathname &&
        pendingAction.targetPathname !== pathname)
    ) {
      return;
    }

    let cancelled = false;
    let dispatched = false;
    let frameId: number | null = null;
    let timeoutId: number | null = null;
    const dispatch = () => {
      if (cancelled || dispatched) return;
      dispatched = true;
      frameId = null;
      timeoutId = null;
      if (
        pendingAction.pathname !== pathname ||
        (pendingAction.targetPathname &&
          pendingAction.targetPathname !== pathname)
      ) {
        return;
      }
      window.dispatchEvent(pendingAction.event);
    };

    if (typeof window.requestAnimationFrame === "function") {
      frameId = window.requestAnimationFrame(dispatch);
    } else {
      // jsdomなどrAFが未提供の環境でも、close後の別tickに揃える。
      timeoutId = window.setTimeout(dispatch, 0);
    }

    return () => {
      cancelled = true;
      if (
        frameId !== null &&
        typeof window.cancelAnimationFrame === "function"
      ) {
        window.cancelAnimationFrame(frameId);
        frameId = null;
      }
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
        timeoutId = null;
      }
    };
  }, [open, pathname]);

  useEffect(() => {
    return () => {
      pendingActionRef.current = null;
    };
  }, []);

  const closePalette = () => {
    setOpen(false);
    setDocsMode({ kind: "root" });
    setQuery("");
    resetDocsSearch();
  };

  const handleOpenChange = (nextOpen: boolean) => {
    setOpen(nextOpen);
    setDocsMode({ kind: "root" });
    setQuery("");
    resetDocsSearch();
  };

  const setDocsModeAndResetQuery = (mode: DocsCommandMode) => {
    setDocsMode(mode);
    setQuery("");
    resetDocsSearch();
  };

  const docsNodes =
    open && scope.docs && docsSearchResult.query === query.trim()
      ? docsSearchResult.nodes
      : [];
  const handleQueryChange = (nextQuery: string) => {
    setQuery(nextQuery);
    resetDocsSearch();
  };
  const inputPlaceholder = docsSubmode
    ? docsMode.kind === "move"
      ? docsMode.leaveReference
        ? "参照を残して移動するノードを検索..."
        : "移動先のノードを検索..."
      : docsMode.kind === "tag"
        ? "追加するタグを検索..."
        : docsMode.kind === "view"
          ? "表示形式を検索..."
          : "設定するフィールド値を検索..."
    : "コマンド・Docsを検索...（#タグで絞り込み）";

  const queueAfterPaletteClose = (event: Event, targetPathname?: string) => {
    pendingActionRef.current = {
      event,
      pathname,
      targetPathname,
    };
    closePalette();
  };

  const handleSelect = (action: string) => {
    const taskDetailMatch = action.match(/^\/tasks\?detail=([^&]+)/);
    if (taskDetailMatch && pathname === "/tasks") {
      queueAfterPaletteClose(
        new CustomEvent("task-detail-open", {
          detail: { taskId: decodeURIComponent(taskDetailMatch[1]) },
        }),
        "/tasks",
      );
      return;
    }
    if (action === "__create-task__") {
      // プロジェクト未選択時は候補自体をdisabledにしているが、
      // キーボード/プログラム経由の呼び出しでも無効なイベントを送らない。
      if (!selectedProjectId) {
        closePalette();
        return;
      }
      queueAfterPaletteClose(new Event("global-create-task"));
      return;
    }
    closePalette();
    if (action.startsWith("__select-space__:")) {
      const spaceId = action.split(":")[1];
      if (!spaceId) return;
      setSelectedSpaceId(spaceId);
      router.push("/tasks");
      return;
    }
    if (action.startsWith("__select-project__:")) {
      const projectId = action.split(":")[1];
      if (!projectId) return;
      const project = selectableProjects.find((p) => p.id === projectId);
      if (project?.space_id) setSelectedSpaceId(project.space_id);
      setSelectedProjectId(projectId);
      router.push("/tasks");
      return;
    }
    router.push(action);
  };

  return (
    <CommandDialog
      open={open}
      onOpenChange={handleOpenChange}
      title="コマンドパレット"
      description="コマンドを検索して実行します"
    >
      <Command filter={filterPaletteItem}>
        <CommandInput
          value={query}
          onValueChange={handleQueryChange}
          placeholder={inputPlaceholder}
        />
        <div className="flex flex-wrap items-center gap-3 border-b px-3 py-1.5 text-[11px] text-muted-foreground">
          <span className="font-medium">検索対象:</span>
          <label className="flex cursor-pointer select-none items-center gap-1">
            <Checkbox
              checked={scope.navigation}
              onCheckedChange={(v) => updateScope("navigation", !!v)}
              className="size-3.5"
            />
            ナビゲーション
          </label>
          <label className="flex cursor-pointer select-none items-center gap-1">
            <Checkbox
              checked={scope.spaces}
              onCheckedChange={(v) => updateScope("spaces", !!v)}
              className="size-3.5"
            />
            スペース
          </label>
          <label className="flex cursor-pointer select-none items-center gap-1">
            <Checkbox
              checked={scope.projects}
              onCheckedChange={(v) => updateScope("projects", !!v)}
              className="size-3.5"
            />
            プロジェクト
          </label>
          <label className="flex cursor-pointer select-none items-center gap-1">
            <Checkbox
              checked={scope.tasks}
              onCheckedChange={(v) => updateScope("tasks", !!v)}
              className="size-3.5"
            />
            タスク
          </label>
          <label className="flex cursor-pointer select-none items-center gap-1">
            <Checkbox
              checked={scope.docs}
              onCheckedChange={(v) => updateScope("docs", !!v)}
              className="size-3.5"
            />
            Docs
          </label>
        </div>
        {!docsSubmode && scope.docs ? (
          <p className="px-3 py-1 text-[11px] text-muted-foreground">
            Docs: 本文・タグ・別名・IDを検索 / #タグ / #&quot;複数語のタグ&quot;
          </p>
        ) : null}
        <CommandList>
          <CommandEmpty>
            {docsNodes.length > 0 || docsSearchResult.loading || docsSearchResult.error ? null : "見つかりません"}
          </CommandEmpty>
          {docsSubmode && docsContext ? (
            <DocsCommandItems
              context={docsContext}
              mode={docsMode}
              setMode={setDocsModeAndResetQuery}
              onClose={closePalette}
            />
          ) : (
            <>
              {scope.navigation && (
                <CommandGroup heading="ナビゲーション">
                  {COMMANDS.map((cmd) => (
                    <CommandItem
                      key={cmd.id}
                      disabled={
                        cmd.action === "__create-task__" && !selectedProjectId
                      }
                      onSelect={() => handleSelect(cmd.action)}
                      keywords={cmd.keywords ? [cmd.keywords] : undefined}
                    >
                      <cmd.icon className="mr-2 size-4" />
                      {cmd.label}
                      {cmd.action === "__create-task__" &&
                        !selectedProjectId && (
                          <span className="ml-auto text-xs text-muted-foreground">
                            プロジェクトを選択してください
                          </span>
                        )}
                    </CommandItem>
                  ))}
                </CommandGroup>
              )}
              {scope.spaces && spaces.length > 0 && (
                <CommandGroup heading="スペース">
                  {spaces.map((space) => (
                    <CommandItem
                      key={space.id}
                      value={`${space.name}__space__${space.id}`}
                      keywords={[`space スペース ${space.name}`]}
                      onSelect={() =>
                        handleSelect(`__select-space__:${space.id}`)
                      }
                    >
                      <Layers className="mr-2 size-4" />
                      {space.name}
                    </CommandItem>
                  ))}
                </CommandGroup>
              )}
              {scope.projects && selectableProjects.length > 0 && (
                <CommandGroup heading="プロジェクト">
                  {selectableProjects.map((project) => {
                    const space = spaces.find((s) => s.id === project.space_id);
                    return (
                      <CommandItem
                        key={project.id}
                        value={`${project.name}__project__${project.id}`}
                        keywords={[
                          `project プロジェクト ${project.name} ${space?.name ?? ""}`,
                        ]}
                        onSelect={() =>
                          handleSelect(`__select-project__:${project.id}`)
                        }
                      >
                        <FolderOpen className="mr-2 size-4" />
                        <span>{project.name}</span>
                        {space && (
                          <span className="ml-auto text-xs text-muted-foreground">
                            {space.name}
                          </span>
                        )}
                      </CommandItem>
                    );
                  })}
                </CommandGroup>
              )}
              {scope.tasks && tasks.length > 0 && (
                <CommandGroup heading="タスク">
                  {tasks.map((task) => {
                    const project = allProjects.find(
                      (p) => p.id === task.project_id,
                    );
                    return (
                      <CommandItem
                        key={task.id}
                        value={`${task.title}__task__${task.id}`}
                        keywords={[task.title, project?.name ?? "", task.id]}
                        onSelect={() =>
                          handleSelect(`/tasks?detail=${task.id}`)
                        }
                      >
                        <CheckSquare className="mr-2 size-4" />
                        <span>{task.title}</span>
                        {project && (
                          <span className="ml-auto text-xs text-muted-foreground">
                            {project.name}
                          </span>
                        )}
                      </CommandItem>
                    );
                  })}
                </CommandGroup>
              )}
              {scope.docs && docsNodes.length > 0 && (
                <CommandGroup heading="Docs">
                  {docsNodes.map((node) => (
                    <CommandItem
                      key={node.id}
                      value={`__docs_result__:${node.id}`}
                      keywords={[
                        node.title,
                        node.id,
                        ...(node.aliases ?? []),
                        docsSearchResult.query,
                      ]}
                      onSelect={() => {
                        closePalette();
                        if (
                          pathname?.startsWith("/docs") &&
                          docsContext?.onOpenNode
                        ) {
                          docsContext.onOpenNode(node.id);
                          return;
                        }
                        router.push(`/docs/${node.id}`);
                      }}
                      className="items-start"
                    >
                      <FileText className="mt-0.5 size-4 text-muted-foreground" />
                      <span className="min-w-0 flex-1 space-y-1">
                        <span className="block truncate font-medium">{node.title}</span>
                        {node.breadcrumb && node.breadcrumb.length > 0 ? (
                          <span className="block truncate text-xs text-muted-foreground" title={node.breadcrumb.join(" / ")}>
                            {node.breadcrumb.join(" / ")}
                          </span>
                        ) : null}
                        {node.snippet ? (
                          <span className="line-clamp-2 text-xs text-muted-foreground">{node.snippet}</span>
                        ) : null}
                        {node.tags && node.tags.length > 0 ? (
                          <span className="flex flex-wrap gap-1">
                            {node.tags.map((tag) => (
                              <span key={tag} className="rounded bg-muted px-1 text-[11px] text-muted-foreground">#{tag}</span>
                            ))}
                          </span>
                        ) : null}
                      </span>
                    </CommandItem>
                  ))}
                </CommandGroup>
              )}
              {scope.docs && docsSearchResult.loading ? (
                <div role="status" className="px-3 py-2 text-xs text-muted-foreground">Docsを検索中...</div>
              ) : null}
              {scope.docs && (docsSearchResult.nextCursor || docsSearchResult.error) ? (
                <CommandGroup>
                  <CommandItem
                    value="__docs_search_more__"
                    keywords={[docsSearchResult.query]}
                    disabled={docsSearchResult.loading}
                    onSelect={docsSearchResult.error ? docsSearchResult.retry : docsSearchResult.loadMore}
                  >
                    {docsSearchResult.error ? "Docs検索に失敗しました。再試行" : "Docsをさらに表示"}
                  </CommandItem>
                </CommandGroup>
              ) : null}
              {docsContext ? (
                <DocsCommandItems
                  context={docsContext}
                  mode={docsMode}
                  setMode={setDocsModeAndResetQuery}
                  onClose={closePalette}
                />
              ) : null}
            </>
          )}
        </CommandList>
      </Command>
    </CommandDialog>
  );
}

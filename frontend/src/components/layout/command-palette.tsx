"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
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
import {
  Plus,
  CheckSquare,
  Layers,
  FolderOpen,
} from "lucide-react";
import { taskApi, type Task } from "@/lib/task-api";
import { useProject } from "@/contexts/project-context";
import { APP_VIEW_TABS } from "@/lib/app-navigation";

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
};

const DEFAULT_SCOPE: SearchScope = {
  navigation: true,
  spaces: true,
  projects: true,
  tasks: true,
};

const SCOPE_STORAGE_KEY = "command-palette-scope";

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
  const router = useRouter();
  const pathname = usePathname();
  const { spaces, allProjects, setSelectedSpaceId, setSelectedProjectId } =
    useProject();

  const updateScope = (key: keyof SearchScope, value: boolean) => {
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
        setOpen((prev) => !prev);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  useEffect(() => {
    if (!open || !scope.tasks) return;
    taskApi
      .listTasks()
      .then(setTasks)
      .catch(() => {});
  }, [open, scope.tasks]);

  const handleSelect = (action: string) => {
    setOpen(false);
    const taskDetailMatch = action.match(/^\/tasks\?detail=([^&]+)/);
    if (taskDetailMatch && pathname === "/tasks") {
      window.dispatchEvent(
        new CustomEvent("task-detail-open", {
          detail: { taskId: decodeURIComponent(taskDetailMatch[1]) },
        }),
      );
      return;
    }
    if (action === "__create-task__") {
      window.dispatchEvent(new Event("global-create-task"));
      return;
    }
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
      const project = allProjects.find((p) => p.id === projectId);
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
      onOpenChange={setOpen}
      title="コマンドパレット"
      description="コマンドを検索して実行します"
    >
      <Command>
        <CommandInput placeholder="コマンドを検索..." />
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
        </div>
        <CommandList>
          <CommandEmpty>見つかりません</CommandEmpty>
          {scope.navigation && (
            <CommandGroup heading="ナビゲーション">
              {COMMANDS.map((cmd) => (
                <CommandItem
                  key={cmd.id}
                  onSelect={() => handleSelect(cmd.action)}
                  keywords={cmd.keywords ? [cmd.keywords] : undefined}
                >
                  <cmd.icon className="mr-2 size-4" />
                  {cmd.label}
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
                  onSelect={() => handleSelect(`__select-space__:${space.id}`)}
                >
                  <Layers className="mr-2 size-4" />
                  {space.name}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {scope.projects && allProjects.length > 0 && (
            <CommandGroup heading="プロジェクト">
              {allProjects.map((project) => {
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
                    keywords={[task.title, project?.name ?? ""]}
                    onSelect={() => handleSelect(`/tasks?detail=${task.id}`)}
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
        </CommandList>
      </Command>
    </CommandDialog>
  );
}

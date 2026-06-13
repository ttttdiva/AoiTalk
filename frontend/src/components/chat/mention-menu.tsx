"use client";

import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { File, CheckSquare, FolderKanban } from "lucide-react";
import { explorerSearch, type SearchResult } from "@/lib/explorer-api";
import { useProject } from "@/contexts/project-context";
import { TASK_STATUS_LABELS } from "@/lib/task-status";

export type MentionType = "file" | "task" | "project";

export interface MentionItem {
  type: MentionType;
  id: string;
  name: string;
  detail?: string;
}

interface MentionMenuProps {
  query: string;
  onSelect: (item: MentionItem) => void;
  onClose: () => void;
}

type TaskMentionCandidate = {
  id: string;
  title: string;
  status: string;
  project_name?: string | null;
};

function normalizeSearchText(value: string) {
  return value.toLowerCase().replace(/-/g, "");
}

function getTaskDetail(task: TaskMentionCandidate) {
  const statusLabel = TASK_STATUS_LABELS[task.status] ?? task.status;
  const shortId = task.id.slice(0, 8);
  return [statusLabel, task.project_name, shortId].filter(Boolean).join(" / ");
}

export function MentionMenu({ query, onSelect, onClose }: MentionMenuProps) {
  const { projects, selectedProjectId } = useProject();
  const [fileResults, setFileResults] = useState<SearchResult[]>([]);
  const [taskResults, setTaskResults] = useState<TaskMentionCandidate[]>([]);
  const [selectionState, setSelectionState] = useState({ query: "", index: 0 });
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const listRef = useRef<HTMLDivElement>(null);
  const hasQuery = query.length > 0;

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);

    debounceRef.current = setTimeout(async () => {
      const root = selectedProjectId
        ? `_projects/project_${selectedProjectId}`
        : "";

      if (hasQuery) {
        try {
          const res = await explorerSearch(query, root, 8);
          if (res.success) setFileResults(res.results);
        } catch {
          // ignore
        }
      } else {
        setFileResults([]);
      }

      try {
        const params = new URLSearchParams();
        if (selectedProjectId) params.set("project_id", selectedProjectId);
        const qs = params.toString();
        const res = await fetch(`/api/tasks${qs ? `?${qs}` : ""}`);
        if (res.ok) {
          const data = await res.json();
          const normalizedQuery = normalizeSearchText(query);
          const tasks = (data.tasks || data || [])
            .filter((task: TaskMentionCandidate) => task.status !== "closed")
            .filter((task: TaskMentionCandidate) => {
              if (!hasQuery) return true;
              const title = task.title.toLowerCase();
              const id = normalizeSearchText(task.id);
              const projectName = task.project_name?.toLowerCase() ?? "";
              return (
                title.includes(query.toLowerCase()) ||
                id.includes(normalizedQuery) ||
                projectName.includes(query.toLowerCase())
              );
            })
            .slice(0, 8);
          setTaskResults(tasks);
        }
      } catch {
        // ignore
      }
    }, 300);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [hasQuery, query, selectedProjectId]);

  const filteredProjects = useMemo(
    () =>
      (
        hasQuery
          ? projects.filter((project) =>
              project.name.toLowerCase().includes(query.toLowerCase()),
            )
          : projects
      ).slice(0, 5),
    [hasQuery, projects, query],
  );

  const allItems = useMemo<MentionItem[]>(
    () => [
      ...(hasQuery ? fileResults : []).map((file) => ({
        type: "file" as const,
        id: file.path,
        name: file.name,
        detail: file.path,
      })),
      ...taskResults.map((task) => ({
        type: "task" as const,
        id: task.id,
        name: task.title,
        detail: getTaskDetail(task),
      })),
      ...filteredProjects.map((project) => ({
        type: "project" as const,
        id: project.id,
        name: project.name,
      })),
    ],
    [fileResults, filteredProjects, hasQuery, taskResults],
  );

  const selectedIndex =
    selectionState.query === query
      ? Math.min(selectionState.index, Math.max(allItems.length - 1, 0))
      : 0;

  const setSelectedIndex = useCallback(
    (next: number | ((prev: number) => number)) => {
      setSelectionState((prev) => {
        const prevIndex = prev.query === query ? prev.index : 0;
        const nextIndex = typeof next === "function" ? next(prevIndex) : next;
        return { query, index: nextIndex };
      });
    },
    [query]
  );

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (!allItems.length) return;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((prev) => Math.min(prev + 1, allItems.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((prev) => Math.max(prev - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (allItems[selectedIndex]) {
          onSelect(allItems[selectedIndex]);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    },
    [allItems, onClose, onSelect, selectedIndex, setSelectedIndex]
  );

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  useEffect(() => {
    const el = listRef.current?.children[selectedIndex] as
      | HTMLElement
      | undefined;
    el?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex]);

  if (allItems.length === 0 && query.length > 0) {
    return (
      <div className="rounded-lg border bg-popover p-3 shadow-md">
        <p className="text-xs text-muted-foreground">
          候補が見つかりません
        </p>
      </div>
    );
  }

  if (allItems.length === 0) return null;

  const iconMap = {
    file: <File className="size-3.5 text-blue-400" />,
    task: <CheckSquare className="size-3.5 text-green-400" />,
    project: <FolderKanban className="size-3.5 text-purple-400" />,
  };

  const labelMap = {
    file: "ファイル",
    task: "タスク",
    project: "プロジェクト",
  };

  return (
    <div
      className="max-h-64 overflow-auto rounded-lg border bg-popover shadow-md"
      ref={listRef}
    >
      {(["file", "task", "project"] as const).map((type) => {
        const items = allItems.filter((item) => item.type === type);
        if (items.length === 0) return null;

        return (
          <div key={type}>
            <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              {labelMap[type]}
            </div>
            {items.map((item) => {
              const idx = allItems.indexOf(item);
              return (
                <button
                  key={`${item.type}-${item.id}`}
                  className={`flex w-full items-center gap-2 px-3 py-1.5 text-sm hover:bg-accent/50 ${
                    idx === selectedIndex ? "bg-accent" : ""
                  }`}
                  onClick={() => onSelect(item)}
                  onMouseEnter={() => setSelectedIndex(idx)}
                >
                  {iconMap[item.type]}
                  <span className="truncate">{item.name}</span>
                  {item.detail && (
                    <span className="ml-auto max-w-[120px] truncate text-[10px] text-muted-foreground">
                      {item.detail}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

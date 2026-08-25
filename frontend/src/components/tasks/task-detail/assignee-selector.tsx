"use client";

import { useEffect, useMemo, useState } from "react";
import { Check, Loader2, X } from "lucide-react";

import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  taskApi,
  type TaskAssignee,
  type TaskAssigneeCandidate,
} from "@/lib/task-api";
import { cn } from "@/lib/utils";

function assigneeName(assignee: TaskAssignee | TaskAssigneeCandidate): string {
  return assignee.display_name || assignee.username || assignee.user_id;
}

export function AssigneeSelector({
  projectId,
  assignees,
  disabled = false,
  onChange,
}: {
  projectId: string;
  assignees: TaskAssignee[];
  disabled?: boolean;
  onChange: (assigneeIds: string[]) => Promise<unknown> | void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [candidates, setCandidates] = useState<TaskAssigneeCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!projectId || disabled) {
      setCandidates([]);
      setLoadFailed(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadFailed(false);
    void taskApi
      .listAssigneeCandidates(projectId)
      .then((members) => {
        if (!cancelled) setCandidates(members);
      })
      .catch(() => {
        if (!cancelled) {
          setCandidates([]);
          setLoadFailed(true);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [disabled, projectId]);

  const selectedIds = useMemo(
    () => new Set(assignees.map((assignee) => assignee.user_id)),
    [assignees],
  );
  const normalizedSearch = search.trim().toLowerCase();
  const filteredCandidates = normalizedSearch
    ? candidates.filter((candidate) =>
        assigneeName(candidate).toLowerCase().includes(normalizedSearch),
      )
    : candidates;

  const save = async (nextIds: string[]) => {
    if (saving || disabled) return;
    setSaving(true);
    try {
      await onChange(nextIds);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        if (disabled) return;
        setOpen(nextOpen);
        if (!nextOpen) setSearch("");
      }}
    >
      <PopoverTrigger
        nativeButton={false}
        render={
          <div
            role="button"
            tabIndex={disabled ? -1 : 0}
            aria-label="担当者を変更"
            aria-disabled={disabled}
            className={cn(
              "flex min-h-6 items-center gap-1.5 rounded px-1 -mx-1 text-xs transition-colors",
              disabled
                ? "cursor-not-allowed opacity-60"
                : "cursor-pointer hover:bg-muted/50",
            )}
          >
            {assignees.length > 0 ? (
              <span className="flex flex-wrap gap-x-1.5 gap-y-1">
                {assignees.map((assignee) => (
                  <span key={assignee.id}>{assigneeName(assignee)}</span>
                ))}
              </span>
            ) : (
              <span className="text-muted-foreground">Empty</span>
            )}
            {saving ? (
              <Loader2 className="ml-auto size-3 animate-spin" />
            ) : assignees.length > 0 && !disabled ? (
              <button
                type="button"
                className="ml-auto shrink-0 text-muted-foreground hover:text-foreground"
                aria-label="担当者をすべて外す"
                onClick={(event) => {
                  event.stopPropagation();
                  void save([]);
                }}
              >
                <X className="size-3.5" />
              </button>
            ) : null}
          </div>
        }
      />
      <PopoverContent className="w-72 p-0" align="start">
        <div className="border-b p-2">
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="担当者を検索"
            className="h-7 text-xs"
            autoFocus
          />
        </div>
        <div className="max-h-56 overflow-y-auto p-1">
          <button
            type="button"
            className={cn(
              "flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent",
              selectedIds.size === 0 && "bg-accent/50",
            )}
            disabled={saving}
            onClick={() => void save([])}
          >
            <span className="flex size-4 items-center justify-center">
              {selectedIds.size === 0 ? <Check className="size-3.5" /> : null}
            </span>
            未設定
          </button>
          {loading ? (
            <div className="flex items-center gap-2 px-2 py-3 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" />
              担当者候補を取得中...
            </div>
          ) : loadFailed ? (
            <p className="px-2 py-3 text-xs text-destructive">
              担当者候補を取得できません
            </p>
          ) : filteredCandidates.length === 0 ? (
            <p className="px-2 py-3 text-xs text-muted-foreground">
              該当する担当者はいません
            </p>
          ) : (
            filteredCandidates.map((candidate) => {
              const selected = selectedIds.has(candidate.user_id);
              return (
                <button
                  key={candidate.user_id}
                  type="button"
                  className={cn(
                    "flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent",
                    selected && "bg-accent/50",
                  )}
                  disabled={saving}
                  onClick={() =>
                    void save(
                      selected
                        ? [...selectedIds].filter(
                            (userId) => userId !== candidate.user_id,
                          )
                        : [...selectedIds, candidate.user_id],
                    )
                  }
                >
                  <span className="flex size-4 items-center justify-center">
                    {selected ? <Check className="size-3.5" /> : null}
                  </span>
                  {assigneeName(candidate)}
                </button>
              );
            })
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

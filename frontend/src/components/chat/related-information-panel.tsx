"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  CheckSquare,
  CircleSlash2,
  ExternalLink,
  Flag,
  FileText,
  FolderOpen,
  Image as ImageIcon,
  Loader2,
  MessageSquareText,
  Paperclip,
  RefreshCcw,
  X,
  XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useAgentRun } from "@/hooks/use-agent-run";
import {
  extractSubagentSummaries,
  type SubagentRunStatus,
} from "@/lib/agent-run-timeline-rows";
import type { ConversationMessage, ConversationSession } from "@/lib/chat-api";
import {
  normalizeChatResources,
  type ChatResource,
} from "@/lib/chat-resource-normalizer";
import { getFileServeUrl, getImageThumbnailUrl } from "@/lib/explorer-serve-url";

export type RelatedTaskSummary = {
  id: string;
  title: string;
  status: string;
  priority: string;
  project_id: string;
  project_name?: string | null;
  updated_at?: string | null;
};

const SUBAGENT_STATUS_LABELS: Record<SubagentRunStatus, string> = {
  running: "実行中",
  succeeded: "完了",
  failed: "失敗",
  cancelled: "キャンセル",
};

type RelatedTaskRequest = {
  sessionId: string;
  controller: AbortController;
  promise: Promise<void>;
};

export type RelatedInformationSection = "all" | "context" | "execution";

class RelatedTasksRequestError extends Error {
  constructor(readonly status: number) {
    super(`related tasks: ${status}`);
  }
}

function relatedTasksErrorMessage(error: unknown): string {
  if (error instanceof RelatedTasksRequestError) {
    if (error.status === 401) {
      return "関連タスクを取得できませんでした。ログイン状態を確認してください。";
    }
    if (error.status === 403) {
      return "関連タスクを表示する権限がありません。";
    }
    if (error.status === 404) {
      return "関連タスクの取得先が見つかりませんでした。";
    }
    return `関連タスクを取得できませんでした（${error.status}）。`;
  }
  return "関連タスクを取得できませんでした。ネットワーク接続を確認して再試行してください。";
}

function SubagentStatusIcon({ status }: { status: SubagentRunStatus }) {
  if (status === "running") {
    return <Loader2 className="size-4 shrink-0 animate-spin text-primary" />;
  }
  if (status === "succeeded") {
    return <CheckCircle2 className="size-4 shrink-0 text-emerald-600" />;
  }
  if (status === "failed") {
    return <XCircle className="size-4 shrink-0 text-destructive" />;
  }
  return <CircleSlash2 className="size-4 shrink-0 text-amber-600" />;
}

export function RelatedInformationPanel({
  sessionId,
  agentRunId,
  onTaskClick,
  onTasksChange,
  onClose,
  hideHeader = false,
  messages = [],
  currentSession = null,
  projectName = null,
  section = "all",
  active = true,
  scrollable = true,
}: {
  sessionId: string | null;
  agentRunId?: string | null;
  onTaskClick: (taskId: string) => void;
  onTasksChange?: (tasks: RelatedTaskSummary[]) => void;
  onClose?: () => void;
  hideHeader?: boolean;
  messages?: ConversationMessage[];
  currentSession?: ConversationSession | null;
  projectName?: string | null;
  /** Render only the context or execution slice when embedded in tabs. */
  section?: RelatedInformationSection;
  /** Inactive tab content must not start network polling. */
  active?: boolean;
  /** Set false when a parent tabpanel owns the single scroll container. */
  scrollable?: boolean;
}) {
  const showContext = section !== "execution";
  const showExecution = section !== "context";
  const subscribedAgentRunId = showExecution && active ? agentRunId : null;
  const [tasks, setTasks] = useState<RelatedTaskSummary[]>([]);
  const [tasksLoading, setTasksLoading] = useState(Boolean(sessionId));
  const [tasksError, setTasksError] = useState<string | null>(null);
  const [tasksSessionId, setTasksSessionId] = useState<string | null>(sessionId);
  const tasksRequestRef = useRef<RelatedTaskRequest | null>(null);
  const tasksSessionIdRef = useRef<string | null>(sessionId);
  const activeSessionIdRef = useRef(sessionId);
  activeSessionIdRef.current = sessionId;
  const {
    run: agentRun,
    error: agentRunError,
    loading: agentRunLoading,
    refresh: refreshAgentRun,
  } = useAgentRun(subscribedAgentRunId, {
    // AgentRunTimelineと同じRunストアを使い、実行中だけ2.5秒間隔で更新する。
    poll: Boolean(agentRunId) && showExecution && active,
  });

  const refreshTasks = useCallback(async () => {
    if (tasksRequestRef.current?.sessionId === sessionId) {
      return tasksRequestRef.current.promise;
    }

    if (!sessionId) {
      tasksRequestRef.current?.controller.abort();
      tasksRequestRef.current = null;
      setTasks([]);
      setTasksError(null);
      setTasksSessionId(null);
      tasksSessionIdRef.current = null;
      onTasksChange?.([]);
      setTasksLoading(false);
      return;
    }

    if (tasksRequestRef.current) {
      tasksRequestRef.current.controller.abort();
      tasksRequestRef.current = null;
    }
    if (tasksSessionIdRef.current !== sessionId) {
      tasksSessionIdRef.current = sessionId;
      setTasksSessionId(sessionId);
      setTasks([]);
      setTasksError(null);
      onTasksChange?.([]);
    }
    setTasksLoading(true);
    const controller = new AbortController();
    const requestState: RelatedTaskRequest = {
      sessionId,
      controller,
      promise: Promise.resolve(),
    };
    const request = (async () => {
      const isCurrentRequest = () =>
        activeSessionIdRef.current === requestState.sessionId &&
        tasksRequestRef.current === requestState;
      try {
        const response = await fetch(
          `/api/conversations/${encodeURIComponent(sessionId)}/related-tasks`,
          {
            credentials: "include",
            cache: "no-store",
            signal: controller.signal,
          },
        );
        if (!response.ok) throw new RelatedTasksRequestError(response.status);
        const data = (await response.json()) as {
          tasks?: RelatedTaskSummary[];
        };
        if (!isCurrentRequest()) return;
        const next = Array.isArray(data.tasks) ? data.tasks : [];
        setTasksError(null);
        setTasks(next);
        onTasksChange?.(next);
      } catch (error) {
        if (!isCurrentRequest()) return;
        console.warn("関連タスク取得に失敗しました", error);
        setTasksError(relatedTasksErrorMessage(error));
      } finally {
        if (tasksRequestRef.current === requestState) {
          setTasksLoading(false);
          tasksRequestRef.current = null;
        }
      }
    })();
    requestState.promise = request;
    tasksRequestRef.current = requestState;
    return request;
  }, [onTasksChange, sessionId]);

  const refreshAll = useCallback(async () => {
    await Promise.all([
      showContext ? refreshTasks() : Promise.resolve(),
      showExecution && active ? refreshAgentRun() : Promise.resolve(),
    ]);
  }, [active, refreshAgentRun, refreshTasks, showContext, showExecution]);

  useEffect(() => {
    if (!active) return;
    void refreshAll();
    const interval = showContext
      ? window.setInterval(() => void refreshTasks(), 5000)
      : null;
    const handleTaskEvent = () => void refreshAll();
    if (showContext) {
      window.addEventListener("aoitalk-task-updated", handleTaskEvent);
      window.addEventListener("aoitalk-task-created", handleTaskEvent);
    }
    return () => {
      if (interval !== null) window.clearInterval(interval);
      if (showContext) {
        window.removeEventListener("aoitalk-task-updated", handleTaskEvent);
        window.removeEventListener("aoitalk-task-created", handleTaskEvent);
      }
      // Invalidate the request identity before an unmount/session switch so a
      // late response cannot update the disposed panel or its badge callback.
      const activeRequest = tasksRequestRef.current;
      tasksRequestRef.current = null;
      activeRequest?.controller.abort();
    };
  }, [active, refreshAll, refreshTasks, showContext]);

  const subagents = useMemo(
    () => extractSubagentSummaries(agentRun?.timeline ?? []),
    [agentRun?.timeline],
  );
  const currentTasks = useMemo(
    () => (tasksSessionId === sessionId ? tasks : []),
    [sessionId, tasks, tasksSessionId],
  );
  const tasksPending =
    Boolean(sessionId) && (tasksLoading || tasksSessionId !== sessionId);
  const refreshing = tasksLoading || agentRunLoading;
  const resources = useMemo(
    () =>
      normalizeChatResources({
        session: currentSession,
        projectName,
        messages,
        relatedTasks: currentTasks,
        agentRun,
      }),
    [agentRun, currentSession, currentTasks, messages, projectName],
  );
  const taskResources = resources.filter((resource) => resource.kind === "task");
  const linkedResources = resources.filter((resource) => resource.kind !== "task");

  const statusLabel = (status: string | null | undefined): string => {
    const normalized = String(status ?? "").trim().toLowerCase();
    const labels: Record<string, string> = {
      running: "実行中",
      pending: "待機中",
      queued: "待機中",
      succeeded: "完了",
      completed: "完了",
      done: "完了",
      failed: "失敗",
      cancelled: "キャンセル",
      canceled: "キャンセル",
      blocked: "保留",
      in_progress: "進行中",
    };
    return labels[normalized] ?? (status || "状態不明");
  };

  const resourceLabel = (resource: ChatResource): string => {
    switch (resource.kind) {
      case "attachment":
        return "添付ファイル";
      case "file":
        return "ファイル";
      case "docs":
        return "Docs";
      case "chat_session":
        return "チャット";
      case "project":
        return "プロジェクト";
      case "app":
        return "App";
      default:
        return "リソース";
    }
  };

  const resourceIcon = (resource: ChatResource) => {
    if (resource.kind === "attachment" || resource.kind === "file") {
      return resource.mimeType?.startsWith("image/") ? (
        <ImageIcon className="size-4 shrink-0 text-primary" />
      ) : (
        <Paperclip className="size-4 shrink-0 text-primary" />
      );
    }
    if (resource.kind === "docs") return <FileText className="size-4 shrink-0 text-primary" />;
    if (resource.kind === "chat_session") {
      return <MessageSquareText className="size-4 shrink-0 text-primary" />;
    }
    return <FolderOpen className="size-4 shrink-0 text-primary" />;
  };

  const resourceCard = (resource: ChatResource) => {
    const body = (
      <>
        {resourceIcon(resource)}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs font-medium">{resource.name}</span>
          <span className="mt-0.5 block truncate text-[10px] text-text-secondary">
            {resourceLabel(resource)}
            {resource.operation ? ` · ${resource.operation}` : ""}
            {resource.path ? ` · ${resource.path}` : ""}
          </span>
        </span>
        {resource.href && <ExternalLink className="size-3.5 shrink-0 text-text-secondary" />}
      </>
    );
    const className =
      "flex w-full items-center gap-2 rounded-md border border-border-subtle bg-surface-slate px-3 py-2 text-left transition-colors hover:border-primary/60 hover:bg-surface-container-high";
    if (resource.kind === "attachment" && resource.path) {
      const href = getFileServeUrl(resource.path);
      const isImage = resource.mimeType?.startsWith("image/") === true;
      return (
        <a key={resource.key} href={href} target="_blank" rel="noreferrer" className={className}>
          {isImage && (
            <img
              src={getImageThumbnailUrl(resource.path, 96)}
              alt=""
              className="size-8 shrink-0 rounded object-cover"
            />
          )}
          {body}
        </a>
      );
    }
    if (resource.kind === "task" && resource.id) {
      return (
        <button
          key={resource.key}
          type="button"
          className={className}
          onClick={() => onTaskClick(resource.id!)}
        >
          {body}
        </button>
      );
    }
    if (resource.href) {
      return (
        <a key={resource.key} href={resource.href} className={className}>
          {body}
        </a>
      );
    }
    return (
      <div key={resource.key} className={className}>
        {body}
      </div>
    );
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-surface-charcoal text-on-surface">
      {!hideHeader && (
        <div className="flex h-14 items-center justify-between gap-2 border-b border-border-subtle px-4">
          <h2 className="text-base font-semibold">関連情報</h2>
          <div className="flex items-center gap-1">
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="text-text-secondary hover:bg-surface-slate hover:text-primary"
              onClick={() => void refreshAll()}
              disabled={refreshing}
              title="関連情報を再取得"
              aria-label="関連情報を再取得"
            >
              <RefreshCcw className={refreshing ? "size-3.5 animate-spin" : "size-3.5"} />
            </Button>
            {onClose && (
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="text-text-secondary hover:bg-surface-slate hover:text-primary"
                onClick={onClose}
                title="関連情報を閉じる"
                aria-label="関連情報を閉じる"
              >
                <X className="size-3.5" />
              </Button>
            )}
          </div>
        </div>
      )}

      <div className={`min-h-0 flex-1 ${scrollable ? "overflow-auto" : "overflow-visible"}`}>
        {showExecution && (
        <section className="space-y-3 border-b border-border-subtle px-4 py-4">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-text-secondary">
              サブエージェント
              {subagents.length > 0 && (
                <span className="ml-1 font-normal">({subagents.length})</span>
              )}
            </h3>
            {agentRun?.status && (
              <span className="text-[10px] text-text-secondary">
                状態: {statusLabel(agentRun.status)}
              </span>
            )}
          </div>

          {agentRunError ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive">
              <div className="flex items-start gap-2">
                <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
                <div className="min-w-0 flex-1">
                  <p>サブエージェント情報を取得できませんでした</p>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="mt-1 h-6 px-1.5 text-xs text-destructive hover:text-destructive"
                    onClick={() => void refreshAgentRun()}
                    disabled={agentRunLoading}
                  >
                    再取得
                  </Button>
                </div>
              </div>
            </div>
          ) : !agentRunId ? (
            <p className="py-2 text-xs text-text-secondary">
              実行中のサブエージェントはいません
            </p>
          ) : agentRunLoading && !agentRun ? (
            <p className="flex items-center gap-2 py-2 text-xs text-text-secondary">
              <Loader2 className="size-3.5 animate-spin" />
              サブエージェント情報を読み込み中…
            </p>
          ) : subagents.length === 0 ? (
            <p className="py-2 text-xs text-text-secondary">
              この実行ではサブエージェントを使用していません
            </p>
          ) : (
            <div className="space-y-2">
              {subagents.map((subagent) => {
                const providerModel = [subagent.provider, subagent.model]
                  .filter(Boolean)
                  .join(" / ");
                return (
                  <div
                    key={subagent.key}
                    data-subagent-status={subagent.status}
                    className="rounded-md border border-border-subtle bg-surface-slate px-3 py-2.5"
                  >
                    <div className="flex min-w-0 items-start gap-2">
                      <SubagentStatusIcon status={subagent.status} />
                      <div className="min-w-0 flex-1">
                        <div className="flex min-w-0 items-center justify-between gap-2">
                          <span className="min-w-0 truncate text-xs font-medium">
                            {subagent.name}
                          </span>
                          <span
                            className="shrink-0 text-[10px] text-text-secondary"
                            title={subagent.status}
                          >
                            {SUBAGENT_STATUS_LABELS[subagent.status]}
                          </span>
                        </div>
                        <p className="mt-1 text-[11px] leading-relaxed text-text-secondary [overflow-wrap:anywhere]">
                          {subagent.action}
                        </p>
                        {providerModel && (
                          <p className="mt-1 truncate text-[10px] text-text-secondary/70">
                            {providerModel}
                          </p>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>
        )}

        {showContext && (
        <section className="space-y-3 px-4 py-4" aria-busy={tasksPending}>
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-text-secondary">
             関連タスク <span className="ml-1 rounded bg-surface-slate px-1.5 py-0.5 font-mono text-[10px] font-normal">{taskResources.length}</span>
          </h3>
          {tasksError && (
            <div
              role="alert"
              className="rounded-md border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive"
            >
              <div className="flex items-start gap-2">
                <AlertCircle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                <div className="min-w-0 flex-1">
                  <p>{tasksError}</p>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="mt-1 h-6 px-1.5 text-xs text-destructive hover:text-destructive"
                    onClick={() => {
                      if (!tasksLoading) void refreshTasks();
                    }}
                    aria-disabled={tasksLoading}
                    aria-label="関連タスクを再取得"
                  >
                    再取得
                  </Button>
                </div>
              </div>
            </div>
          )}
          {tasksPending && (
            <p
              role="status"
              className="flex items-center gap-2 px-2 py-2 text-xs text-text-secondary"
            >
              <Loader2 aria-hidden="true" className="size-3.5 animate-spin" />
              {taskResources.length > 0
                ? "関連タスクを更新中…"
                : "関連タスクを読み込み中…"}
            </p>
          )}
          {taskResources.length === 0 && !tasksPending && !tasksError ? (
            <p className="px-2 py-1 text-xs text-text-secondary">
              このチャットに関連するタスクはありません
            </p>
          ) : taskResources.length > 0 ? (
            <div className="space-y-1">
              {taskResources.map((resource) => {
                const task = currentTasks.find((item) => item.id === resource.id);
                return (
                <button
                  key={resource.key}
                  type="button"
                  className="group relative w-full rounded-md border border-border-subtle bg-surface-slate p-3 text-left transition-colors hover:border-primary/60 hover:bg-surface-container-high"
                  onClick={() => resource.id && onTaskClick(resource.id)}
                >
                  <div className="flex items-start gap-2">
                    <span className="absolute inset-y-0 left-0 w-0.5 rounded-l bg-transparent transition-colors group-hover:bg-primary" />
                    <CheckSquare className="mt-0.5 size-4 shrink-0 text-primary" />
                    <span className="min-w-0 flex-1 truncate text-[13px] font-medium">
                      {resource.name}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-x-2 gap-y-1 text-[11px] text-text-secondary">
                    <span>{statusLabel(task?.status)}</span>
                    <span>
                      <Flag className="mr-0.5 inline size-3" />
                      {task?.priority || "優先度未設定"}
                    </span>
                    {(task?.project_name || resource.projectName) && (
                      <span>
                        <FolderOpen className="mr-0.5 inline size-3" />
                        {task?.project_name || resource.projectName}
                      </span>
                    )}
                  </div>
                </button>
                );
              })}
            </div>
          ) : null}
        </section>
        )}

        {showContext && (
        <section className="space-y-3 border-t border-border-subtle px-4 py-4">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-text-secondary">
            セッション内リソース
            <span className="ml-1 rounded bg-surface-slate px-1.5 py-0.5 font-mono text-[10px] font-normal">
              {linkedResources.length}
            </span>
          </h3>
          {linkedResources.length === 0 ? (
            <p className="px-2 py-1 text-xs text-text-secondary">
              明示的に関連付けられたリソースはありません
            </p>
          ) : (
            <div className="space-y-1">{linkedResources.map(resourceCard)}</div>
          )}
        </section>
        )}
      </div>
    </div>
  );
}

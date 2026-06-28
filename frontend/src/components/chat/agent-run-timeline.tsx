"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Loader2,
  PlayCircle,
  UserRound,
  Wrench,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  chatApi,
  type AgentRun,
  type AgentRunTimelineColumn,
  type AgentRunTimelineItem,
} from "@/lib/chat-api";
import { cn } from "@/lib/utils";

type AgentRunTimelineProps = {
  runId?: string | null;
  live?: boolean;
  onContentChange?: () => void;
};

const STATUS_LABELS: Record<string, string> = {
  queued: "待機中",
  running: "実行中",
  started: "開始",
  recorded: "記録",
  succeeded: "完了",
  failed: "失敗",
  cancelled: "停止",
  tool: "ツール",
};

const TERMINAL_RUN_STATUSES = new Set(["succeeded", "failed", "cancelled"]);

function statusLabel(status?: string | null) {
  if (!status) return "記録";
  return STATUS_LABELS[status] ?? status;
}

function isTerminalRunStatus(status?: string | null) {
  return Boolean(status && TERMINAL_RUN_STATUSES.has(status));
}

function statusTone(status?: string | null) {
  if (status === "succeeded") return "text-emerald-600 dark:text-emerald-400";
  if (status === "failed" || status === "cancelled") {
    return "text-destructive";
  }
  if (status === "recorded") return "text-muted-foreground";
  if (status === "running" || status === "queued" || status === "tool") {
    return "text-sky-600 dark:text-sky-400";
  }
  if (status === "started") return "text-sky-600 dark:text-sky-400";
  return "text-muted-foreground";
}

function StatusIcon({ status }: { status?: string | null }) {
  if (status === "succeeded") return <CheckCircle2 className="size-3.5" />;
  if (status === "failed" || status === "cancelled") {
    return <XCircle className="size-3.5" />;
  }
  if (status === "running" || status === "queued" || status === "tool") {
    return <Loader2 className="size-3.5 animate-spin" />;
  }
  if (status === "started") return <PlayCircle className="size-3.5" />;
  return <Clock3 className="size-3.5" />;
}

function actorIcon(item: AgentRunTimelineItem) {
  if (item.source === "tool_call" || item.actor_type === "tool") {
    return <Wrench className="size-3.5" />;
  }
  if (item.actor_type === "system") return <PlayCircle className="size-3.5" />;
  return <UserRound className="size-3.5" />;
}

function formatTime(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatDuration(value?: number | null) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return null;
  }
  if (value < 1000) return `${Math.round(value)}ms`;
  return `${(value / 1000).toFixed(1)}秒`;
}

function compactJson(value?: Record<string, unknown>) {
  if (!value || Object.keys(value).length === 0) return "";
  try {
    const text = JSON.stringify(value, null, 2);
    return text.length > 900 ? `${text.slice(0, 900).trimEnd()}\n...` : text;
  } catch {
    return "";
  }
}

function displayModel(model?: string | null) {
  const value = String(model ?? "").trim();
  if (!value || value.toLowerCase() === "default") return "";
  return value;
}

function providerModelLabel(provider?: string | null, model?: string | null) {
  const providerText = String(provider ?? "").trim();
  const modelText = displayModel(model);
  if (providerText && modelText) return `${providerText} / ${modelText}`;
  return modelText || providerText;
}

function actorDisplayLabel(item: AgentRunTimelineItem) {
  const label = item.actor_label || "エージェント";
  return label;
}

function timelineSummary(items: AgentRunTimelineItem[]) {
  const agents = new Set(
    items
      .map((item) => actorDisplayLabel(item))
      .filter((label): label is string => Boolean(label)),
  );
  const toolCount = items.filter((item) => item.source === "tool_call").length;
  const eventCount = items.length;
  return [
    `${eventCount}件`,
    agents.size > 0 ? `担当 ${agents.size}` : null,
    toolCount > 0 ? `ツール ${toolCount}` : null,
  ]
    .filter(Boolean)
    .join(" / ");
}

function TimelineRow({ item }: { item: AgentRunTimelineItem }) {
  const duration = formatDuration(item.duration_ms);
  const argumentsText = compactJson(item.arguments);
  const detailText = item.result_preview || argumentsText;
  const displayStatus = item.display_status ?? item.status;
  const modelText = providerModelLabel(item.provider, item.model);

  return (
    <li className="relative pl-5">
      <span className="absolute left-0 top-1.5 size-2 rounded-full bg-border" />
      <div className="min-w-0 rounded-md border border-border/60 bg-background/60 px-2.5 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
            {formatTime(item.created_at)}
          </span>
          <span className="inline-flex min-w-0 items-center gap-1 font-medium">
            {actorIcon(item)}
            <span className="min-w-0 [overflow-wrap:anywhere]">
              {actorDisplayLabel(item)}
            </span>
          </span>
          {modelText && (
            <span className="min-w-0 text-[11px] text-muted-foreground [overflow-wrap:anywhere]">
              {modelText}
            </span>
          )}
          <span
            className={cn(
              "inline-flex items-center gap-1 text-[11px]",
              statusTone(displayStatus),
            )}
          >
            <StatusIcon status={displayStatus} />
            {statusLabel(displayStatus)}
          </span>
          {item.tool_name && (
            <Badge variant="secondary" className="h-5 max-w-[180px] truncate">
              {item.tool_name}
            </Badge>
          )}
          {duration && (
            <span className="text-[11px] text-muted-foreground">{duration}</span>
          )}
        </div>
        <div className="mt-1 min-w-0 text-xs leading-relaxed text-foreground">
          {item.action}
          {item.message && item.message !== item.tool_name ? (
            <span className="text-muted-foreground"> / {item.message}</span>
          ) : null}
        </div>
        {detailText && (
          <details className="mt-1">
            <summary className="cursor-pointer list-none text-[11px] text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden">
              詳細
            </summary>
            <pre className="mt-1 max-w-full overflow-x-auto whitespace-pre-wrap rounded bg-muted/60 p-2 text-[11px] leading-relaxed">
              {detailText}
            </pre>
          </details>
        )}
      </div>
    </li>
  );
}

function TimelineList({ items }: { items: AgentRunTimelineItem[] }) {
  return (
    <ol className="space-y-2 border-l border-border/70 pl-3">
      {items.map((item) => (
        <TimelineRow key={item.id} item={item} />
      ))}
    </ol>
  );
}

function TimelineColumn({ column }: { column: AgentRunTimelineColumn }) {
  const modelText = providerModelLabel(column.provider, column.model);
  return (
    <section className="min-w-0 rounded-md border border-border/70 bg-background/45 p-2">
      <div className="mb-2 min-w-0">
        <div className="flex min-w-0 items-center gap-1.5 text-xs font-medium">
          <UserRound className="size-3.5 shrink-0" />
          <span className="min-w-0 [overflow-wrap:anywhere]">{column.label}</span>
        </div>
        {modelText && (
          <div className="mt-0.5 text-[11px] leading-snug text-muted-foreground [overflow-wrap:anywhere]">
            {modelText}
          </div>
        )}
      </div>
      <TimelineList items={column.items} />
    </section>
  );
}

export function AgentRunTimeline({
  runId,
  live = false,
  onContentChange,
}: AgentRunTimelineProps) {
  const [expanded, setExpanded] = useState(live ? Boolean(runId) : false);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollBottomRef = useRef<HTMLDivElement>(null);
  const shouldPollLive = Boolean(
    live && runId && !isTerminalRunStatus(run?.status),
  );
  const hasRunError = Boolean(run?.error);
  const hasLoadedRun = run !== null;

  useEffect(() => {
    setRun(null);
    setError(null);
    setLoading(false);
    setExpanded(Boolean(runId));
  }, [live, runId]);

  useEffect(() => {
    if (!runId) return;
    if (
      shouldPollLive ||
      run?.status === "failed" ||
      run?.status === "cancelled" ||
      hasRunError
    ) {
      setExpanded(true);
      return;
    }
    if (run?.status === "succeeded" && !hasRunError) {
      setExpanded(false);
    }
  }, [hasRunError, run?.status, runId, shouldPollLive]);

  const loadRun = useCallback(
    async (isCancelled: () => boolean) => {
      if (!runId) return;
      setLoading(true);
      setError(null);
      try {
        const response = await chatApi.getAgentRun(runId);
        if (!isCancelled()) {
          setRun(response.agent_run);
        }
      } catch (err) {
        if (!isCancelled()) {
          setError(
            err instanceof Error
              ? err.message
              : "実行タイムラインを取得できませんでした",
          );
        }
      } finally {
        if (!isCancelled()) setLoading(false);
      }
    },
    [runId],
  );

  useEffect(() => {
    if (!runId || live || hasLoadedRun) return;
    let cancelled = false;

    void loadRun(() => cancelled);

    return () => {
      cancelled = true;
    };
  }, [hasLoadedRun, live, loadRun, runId]);

  useEffect(() => {
    if ((!expanded && !shouldPollLive) || !runId) return;
    let cancelled = false;
    let intervalId: number | undefined;

    void loadRun(() => cancelled);
    if (shouldPollLive) {
      intervalId = window.setInterval(
        () => void loadRun(() => cancelled),
        2500,
      );
    }

    return () => {
      cancelled = true;
      if (intervalId) window.clearInterval(intervalId);
    };
  }, [expanded, loadRun, runId, shouldPollLive]);

  const items = useMemo(() => run?.timeline ?? [], [run]);
  const columns = useMemo(() => run?.timeline_columns ?? [], [run]);
  const hasColumnLayout = columns.length > 1;

  useEffect(() => {
    if (!expanded) return;
    onContentChange?.();
    scrollBottomRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [expanded, items.length, columns, loading, error, onContentChange]);

  if (!runId) return null;

  return (
    <div className="mt-2 max-w-full rounded-md border border-border/70 bg-muted/30 text-xs">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-auto w-full justify-start gap-2 rounded-none px-3 py-2 text-xs"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <ChevronDown
          className={cn(
            "size-3.5 shrink-0 transition-transform",
            expanded && "rotate-180",
          )}
        />
        <span className="min-w-0 flex-1 truncate text-left">
          実行タイムライン
          {run ? `: ${timelineSummary(items)}` : ""}
        </span>
        {shouldPollLive && (
          <span className="inline-flex items-center gap-1 text-[11px] text-sky-600 dark:text-sky-400">
            <Loader2 className="size-3 animate-spin" />
            更新中
          </span>
        )}
        {run?.status && (
          <Badge variant="outline" className="shrink-0">
            {statusLabel(run.status)}
          </Badge>
        )}
      </Button>

      {expanded && (
        <div className="border-t border-border/60 px-3 py-2">
          {loading && !run && (
            <div className="flex items-center gap-2 text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" />
              読み込み中
            </div>
          )}
          {error && (
            <div className="flex items-start gap-2 rounded border border-destructive/30 bg-destructive/10 p-2 text-destructive">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span className="min-w-0 [overflow-wrap:anywhere]">{error}</span>
            </div>
          )}
          {!loading && !error && items.length === 0 && (
            <div className="text-muted-foreground">記録された実行イベントはありません。</div>
          )}
          {items.length > 0 && hasColumnLayout && (
            <div
              className="grid gap-3"
              style={{
                gridTemplateColumns:
                  "repeat(auto-fit, minmax(min(18rem, 100%), 1fr))",
              }}
            >
              {columns.map((column) => (
                <TimelineColumn key={column.key} column={column} />
              ))}
            </div>
          )}
          {items.length > 0 && !hasColumnLayout && (
            <TimelineList items={items} />
          )}
          <div ref={scrollBottomRef} />
        </div>
      )}
    </div>
  );
}

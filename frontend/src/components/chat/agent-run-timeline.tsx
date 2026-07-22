"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Ban,
  Check,
  ChevronDown,
  Circle,
  FilePenLine,
  Loader2,
  RefreshCw,
  Search,
  Terminal,
  Users,
  Wrench,
  X,
} from "lucide-react";
import {
  chatApi,
  type AgentRun,
  type AgentRunTimelineItem,
} from "@/lib/chat-api";
import {
  isTerminalAgentRunStatus,
  initialAgentRunTimelineExpanded,
  nextAgentRunTimelineExpanded,
  shouldPollAgentRunTimeline,
} from "@/lib/agent-run-timeline-state";
import { cn } from "@/lib/utils";

type AgentRunTimelineProps = {
  runId?: string | null;
  live?: boolean;
  onContentChange?: () => boolean | void;
};

const HISTORICAL_PENDING_POLL_TIMEOUT_MS = 30_000;

// ─── 表示対象の判定 ───
// Codex CLI 風に、意味のある動作行だけを 1 列で並べる。
// run.* / stream.stream_start / stream.stream_end / reasoning_progress /
// steering_update / 記録などのライフサイクル行は一切表示しない。

/** ツール実行行（検索・URL 取得・コマンド実行など） */
function isToolRow(item: AgentRunTimelineItem): boolean {
  return item.source === "tool_call" || item.event_type === "tool_operation";
}

/** サブエージェント行（agent_team.instance_started/succeeded/failed） */
function isAgentTeamRow(item: AgentRunTimelineItem): boolean {
  if (item.source !== "event") return false;
  const eventType = item.event_type ?? "";
  return (
    eventType === "agent_operation" ||
    eventType.startsWith("agent_team.") ||
    item.actor_type === "agent_team"
  );
}

/** 検証ループ行（agentic_review / 進捗検証系の status_update） */
function isReviewRow(item: AgentRunTimelineItem): boolean {
  if (item.source !== "event") return false;
  const eventType = item.event_type ?? "";
  if (eventType === "stream.agentic_review") return true;
  if (eventType === "stream.status_update") {
    const status = String(item.payload?.status ?? "").toLowerCase();
    return status === "agentic_review" || status === "agentic_continue";
  }
  return false;
}

type TimelineRowKind = "tool" | "agent" | "review";

function timelineRowKind(item: AgentRunTimelineItem): TimelineRowKind | null {
  if (isToolRow(item)) return "tool";
  if (isAgentTeamRow(item)) return "agent";
  if (isReviewRow(item)) return "review";
  return null;
}

function isDisplayableTimelineItem(item: AgentRunTimelineItem): boolean {
  return timelineRowKind(item) !== null;
}

/** 秒数を「12秒」「1分05秒」形式へ */
function formatSeconds(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds));
  if (safe < 60) return `${safe}秒`;
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${minutes}分${seconds.toString().padStart(2, "0")}秒`;
}

// ─── ツール引数の 1 行要約 ───

const ARGUMENT_PRIORITY_KEYS = [
  "query",
  "q",
  "search",
  "url",
  "uri",
  "command",
  "cmd",
  "path",
  "file_path",
  "pattern",
  "input",
  "text",
  "prompt",
  "name",
];

function toolArgumentSummary(args?: Record<string, unknown>): string {
  if (!args) return "";
  for (const key of ARGUMENT_PRIORITY_KEYS) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number") return String(value);
  }
  for (const value of Object.values(args)) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function displayModel(model?: string | null): string {
  const value = String(model ?? "").trim();
  if (!value || value.toLowerCase() === "default") return "";
  return value;
}

function formatDuration(durationMs?: number | null): string {
  if (typeof durationMs !== "number" || durationMs < 0) return "";
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  if (durationMs < 10_000) return `${(durationMs / 1000).toFixed(1)}秒`;
  return formatSeconds(durationMs / 1000);
}

function operationMeta(item: AgentRunTimelineItem): string {
  const groupId = String(item.group_id ?? payloadValue(item, "group_id") ?? "").trim();
  const groupLabel = groupId === "heavy" ? "高負荷" : groupId === "light" ? "軽量" : groupId;
  const provider = String(item.provider ?? payloadValue(item, "provider") ?? "").trim();
  const providerModel = [provider, displayModel(item.model)].filter(Boolean).join("/");
  const mode = String(item.mode ?? "").trim();
  const pool = String(item.pool ?? payloadValue(item, "pool", "pool_id") ?? "").trim();
  const candidate = String(item.candidate ?? payloadValue(item, "candidate", "candidate_id") ?? "").trim();
  const credential = String(item.credential_profile ?? payloadValue(item, "credential_profile", "credential_profile_id") ?? "").trim();
  const fallbackCount = Number(item.fallback_count ?? payloadValue(item, "fallback_count") ?? 0);
  return [
    formatDuration(item.duration_ms),
    groupLabel,
    pool ? `pool:${pool}` : "",
    providerModel,
    mode,
    candidate ? `candidate:${candidate}` : "",
    credential ? `credential:${credential}` : "",
    fallbackCount > 0 ? `fallback:${fallbackCount}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

function payloadValue(item: AgentRunTimelineItem, ...keys: string[]): unknown {
  for (const key of keys) {
    const value = item.payload?.[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return undefined;
}

function operationCommand(item: AgentRunTimelineItem): string {
  const command = item.arguments?.command ?? item.arguments?.cmd;
  return typeof command === "string" ? command.trim() : "";
}

function operationPaths(item: AgentRunTimelineItem): string[] {
  const values: string[] = [];
  for (const value of [item.arguments?.file_path, item.arguments?.path]) {
    if (typeof value === "string" && value.trim()) values.push(value.trim());
  }
  for (const key of ["paths", "files"] as const) {
    const paths = item.arguments?.[key];
    if (Array.isArray(paths)) {
      for (const path of paths) {
        if (typeof path === "string" && path.trim()) values.push(path.trim());
      }
    }
  }
  const changes = item.arguments?.changes;
  if (Array.isArray(changes)) {
    for (const change of changes) {
      if (change && typeof change === "object") {
        const path = (change as { path?: unknown }).path;
        if (typeof path === "string" && path.trim()) values.push(path.trim());
      }
    }
  }
  return [...new Set(values)];
}

function operationPath(item: AgentRunTimelineItem): string {
  return operationPaths(item)[0] ?? "";
}

function isFileEdit(item: AgentRunTimelineItem): boolean {
  const toolName = String(item.tool_name ?? "").toLowerCase();
  return ["write_file", "edit_file", "apply_patch"].includes(toolName);
}

function hasMeaningfulDetails(item: AgentRunTimelineItem): boolean {
  if (item.error) return true;
  if (operationCommand(item) || operationPath(item)) return true;
  if (Object.keys(item.arguments ?? {}).length > 0) {
    const simpleToolNames = new Set([
      "get_current_time",
      "get_weather",
      "calculate",
    ]);
    if (!simpleToolNames.has(String(item.tool_name ?? ""))) return true;
  }
  if (item.event_type === "agent_operation" && item.result) return true;
  if (String(item.result ?? "").includes("\n")) return true;
  return ["exit_code", "exit", "stdout", "stderr", "diff", "patch"].some(
    (key) => payloadValue(item, key) !== undefined,
  );
}

function toolRowSummary(item: AgentRunTimelineItem): string {
  const input = toolArgumentSummary(item.arguments);
  const result = String(item.result_preview ?? item.result ?? "").trim();
  const simpleToolNames = new Set([
    "get_current_time",
    "get_weather",
    "calculate",
  ]);
  if (simpleToolNames.has(String(item.tool_name ?? "")) && result) {
    return input ? `${input} → ${result}` : result;
  }
  return input || result;
}

function operationIcon(item: AgentRunTimelineItem) {
  const running =
    item.display_status === "started" || item.status === "running";
  const cancelled = item.status === "cancelled";
  const failed =
    item.success === false || item.status === "failed" || Boolean(item.error);
  if (running) return <Loader2 className="size-3.5 animate-spin" />;
  if (cancelled) return <Ban className="size-3.5" />;
  if (failed) return <X className="size-3.5" />;
  if (item.success === true || item.status === "succeeded") {
    return <Check className="size-3.5" />;
  }
  return <Circle className="size-3.5" />;
}

function operationStatusLabel(item: AgentRunTimelineItem): string {
  if (item.display_status === "started" || item.status === "running")
    return "実行中";
  if (item.status === "cancelled") return "キャンセル";
  if (item.success === false || item.status === "failed" || item.error)
    return "失敗";
  if (item.success === true || item.status === "succeeded") return "成功";
  return "記録済み";
}

function operationTypeIcon(item: AgentRunTimelineItem) {
  if (isReviewRow(item)) return <RefreshCw className="size-3.5" />;
  if (item.event_type === "agent_operation")
    return <Users className="size-3.5" />;
  if (operationCommand(item)) return <Terminal className="size-3.5" />;
  if (isFileEdit(item)) return <FilePenLine className="size-3.5" />;
  if (
    String(item.tool_name ?? "")
      .toLowerCase()
      .includes("search")
  ) {
    return <Search className="size-3.5" />;
  }
  return <Wrench className="size-3.5" />;
}

function DetailBlock({ label, value }: { label: string; value: string }) {
  if (!value.trim()) return null;
  return (
    <div className="space-y-1">
      <div className="font-medium text-foreground/80">{label}</div>
      <pre className="max-w-full overflow-x-auto whitespace-pre-wrap rounded-md bg-muted/60 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-foreground [overflow-wrap:normal]">
        {value}
      </pre>
    </div>
  );
}

function OperationDetails({ item }: { item: AgentRunTimelineItem }) {
  const command = operationCommand(item);
  const paths = operationPaths(item);
  const result = String(item.result ?? item.result_preview ?? "");
  const error = String(item.error ?? payloadValue(item, "stderr") ?? "");
  const stdout = String(payloadValue(item, "stdout") ?? "");
  const diff = String(payloadValue(item, "diff", "patch") ?? "");
  const exitCode = payloadValue(item, "exit_code", "exit");
  const urls = Array.isArray(item.payload?.urls)
    ? item.payload.urls.filter(
        (url): url is string =>
          typeof url === "string" && /^https?:\/\//i.test(url),
      )
    : [];
  const remainingArguments = Object.fromEntries(
    Object.entries(item.arguments ?? {}).filter(
      ([key]) => !["command", "cmd", "path", "file_path"].includes(key),
    ),
  );
  return (
    <div className="space-y-2.5 border-l border-border/70 py-1 pl-3 text-[11px] text-muted-foreground">
      {paths.length > 0 && (
        <div className="whitespace-pre-wrap font-mono text-foreground [overflow-wrap:anywhere]">
          {paths.join("\n")}
        </div>
      )}
      <DetailBlock
        label="実行したコマンド"
        value={command ? `$ ${command}` : ""}
      />
      {Object.keys(remainingArguments).length > 0 && (
        <DetailBlock
          label="入力"
          value={JSON.stringify(remainingArguments, null, 2)}
        />
      )}
      {exitCode !== undefined && (
        <div className="font-mono text-foreground">exit={String(exitCode)}</div>
      )}
      <DetailBlock label="標準出力" value={stdout} />
      <DetailBlock label="結果" value={result} />
      <DetailBlock label="エラー" value={error} />
      <DetailBlock label="差分" value={diff} />
      {urls.length > 0 && (
        <div className="space-y-1">
          <div className="font-medium text-foreground/80">参照URL</div>
          {urls.map((url) => (
            <a
              key={url}
              href={url}
              target="_blank"
              rel="noreferrer"
              className="block truncate text-primary underline underline-offset-2"
            >
              {url}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── 1 論理作業の描画 ───

function TimelineRow({
  item,
  onContentChange,
}: {
  item: AgentRunTimelineItem;
  onContentChange?: () => boolean | void;
}) {
  const kind = timelineRowKind(item);
  if (!kind) return null;

  const failed =
    item.success === false || item.status === "failed" || Boolean(item.error);
  const title =
    kind === "review"
      ? item.message || item.action || "結果を検証"
      : item.action || item.actor_label || item.tool_name || "処理を実行";
  const summary = kind === "tool" ? toolRowSummary(item) : "";
  const meta = operationMeta(item);
  const details = hasMeaningfulDetails(item);
  const row = (
    <div
      className={cn(
        "flex min-w-0 items-center gap-2 py-1 text-xs",
        failed ? "text-destructive" : "text-muted-foreground",
      )}
    >
      <span className="shrink-0" title={operationStatusLabel(item)}>
        {operationIcon(item)}
        <span className="sr-only">{operationStatusLabel(item)}</span>
      </span>
      <span className="shrink-0 text-muted-foreground/75">
        {operationTypeIcon(item)}
      </span>
      <span
        className="min-w-0 flex-1 truncate font-medium text-foreground"
        title={title}
      >
        {title}
      </span>
      {summary && (
        <span className="min-w-0 max-w-[35%] truncate" title={summary}>
          {summary}
        </span>
      )}
      {meta && (
        <span
          className="min-w-0 max-w-[45%] truncate whitespace-nowrap text-[11px] text-muted-foreground/75"
          title={meta}
        >
          {meta}
        </span>
      )}
      {details && (
        <ChevronDown className="size-3.5 shrink-0 transition-transform group-open:rotate-180" />
      )}
    </div>
  );
  return (
    <li className="min-w-0">
      {details ? (
        <details
          className="group min-w-0"
          onToggle={(event) => {
            if (!event.currentTarget.open) return;
            requestAnimationFrame(() => onContentChange?.());
          }}
        >
          <summary className="cursor-pointer list-none">{row}</summary>
          <OperationDetails item={item} />
        </details>
      ) : (
        row
      )}
    </li>
  );
}

function workSummary(items: AgentRunTimelineItem[], live: boolean): string {
  if (items.length === 0) return live ? "実行を準備中" : "実行ログ";
  const commandCount = items.filter((item) =>
    Boolean(operationCommand(item)),
  ).length;
  const editItems = items.filter(isFileEdit);
  const editedFiles = new Set(
    editItems.flatMap(operationPaths).filter(Boolean),
  );
  const editedFileCount = editedFiles.size || editItems.length;
  const agentCount = items.filter(
    (item) => item.event_type === "agent_operation",
  ).length;
  const otherCount = items.filter(
    (item) =>
      !operationCommand(item) &&
      !isFileEdit(item) &&
      item.event_type !== "agent_operation",
  ).length;
  const parts: string[] = [];
  if (commandCount) parts.push(`${commandCount}件のコマンド`);
  if (editedFileCount) parts.push(`${editedFileCount}個のファイル`);
  if (agentCount) parts.push(`${agentCount}件のエージェント作業`);
  if (otherCount || !parts.length)
    parts.push(`${otherCount || items.length}件の操作`);
  return `${live ? "実行中" : "実行済み"} ${parts.join("、")}`;
}

export function AgentRunTimeline({
  runId,
  live = false,
  onContentChange,
}: AgentRunTimelineProps) {
  const [expanded, setExpanded] = useState(
    initialAgentRunTimelineExpanded(live, runId),
  );
  const [run, setRun] = useState<AgentRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const scrollBottomRef = useRef<HTMLDivElement>(null);
  const historicalPendingPollStartedAtRef = useRef<number | null>(null);

  const shouldPollLive = shouldPollAgentRunTimeline(live, runId, run?.status);
  const hasRunError = Boolean(run?.error);
  const hasLoadedRun = run !== null;
  const shouldPollHistoricalPending = Boolean(
    !live &&
    runId &&
    hasLoadedRun &&
    !hasRunError &&
    !isTerminalAgentRunStatus(run?.status),
  );
  const shouldPollRun = shouldPollLive || shouldPollHistoricalPending;

  // runId / live 変更時にローカル状態をリセットする（prop 変化に対する正当なリセット）
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- prop 変化に伴う状態リセット
    setRun(null);
    setError(null);
    setExpanded(initialAgentRunTimelineExpanded(live, runId));
    historicalPendingPollStartedAtRef.current = null;
  }, [live, runId]);

  useEffect(() => {
    if (!runId) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 実行状態に応じた展開遷移
    setExpanded((currentExpanded) =>
      nextAgentRunTimelineExpanded({
        runId,
        live,
        currentExpanded,
        shouldPollLive,
        status: run?.status,
        hasRunError,
      }),
    );
  }, [hasRunError, live, run?.status, runId, shouldPollLive]);

  const loadRun = useCallback(
    async (isCancelled: () => boolean) => {
      if (!runId) return;
      // setState は await 後（非同期継続）でのみ行い、effect 内の同期 setState を避ける
      try {
        const response = await chatApi.getAgentRun(runId);
        if (!isCancelled()) {
          setRun(response.agent_run);
          setError(null);
        }
      } catch (err) {
        if (!isCancelled()) {
          setError(
            err instanceof Error
              ? err.message
              : "実行ログを取得できませんでした",
          );
        }
      }
    },
    [runId],
  );

  // 保存済み（非 live）の初回ロード
  useEffect(() => {
    if (!runId || live || hasLoadedRun) return;
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 初回データ取得
    void loadRun(() => cancelled);
    return () => {
      cancelled = true;
    };
  }, [hasLoadedRun, live, loadRun, runId]);

  // ポーリング（2500ms 固定・既存挙動を踏襲）
  useEffect(() => {
    if ((!expanded && !shouldPollRun) || !runId) return;
    let cancelled = false;
    let intervalId: number | undefined;
    if (
      shouldPollHistoricalPending &&
      historicalPendingPollStartedAtRef.current === null
    ) {
      historicalPendingPollStartedAtRef.current = Date.now();
    }
    const pollTimeoutAt =
      shouldPollHistoricalPending && historicalPendingPollStartedAtRef.current
        ? historicalPendingPollStartedAtRef.current +
          HISTORICAL_PENDING_POLL_TIMEOUT_MS
        : null;

    // eslint-disable-next-line react-hooks/set-state-in-effect -- ポーリングによるデータ取得
    void loadRun(() => cancelled);
    if (shouldPollRun) {
      intervalId = window.setInterval(() => {
        if (pollTimeoutAt !== null && Date.now() > pollTimeoutAt) {
          if (intervalId) window.clearInterval(intervalId);
          return;
        }
        void loadRun(() => cancelled);
      }, 2500);
    }

    return () => {
      cancelled = true;
      if (intervalId) window.clearInterval(intervalId);
    };
  }, [expanded, loadRun, runId, shouldPollHistoricalPending, shouldPollRun]);

  const items = useMemo(() => run?.timeline ?? [], [run]);
  const displayItems = useMemo(
    () => items.filter(isDisplayableTimelineItem),
    [items],
  );

  useEffect(() => {
    if (!expanded) return;
    if (onContentChange?.() === false) return;
    scrollBottomRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [expanded, displayItems.length, error, onContentChange]);

  if (!runId) return null;

  const failedRun =
    run?.status === "failed" || run?.status === "cancelled" || hasRunError;

  // 表示可能アイテムが 0 件で終了済みの run はブロック自体を出さない。
  // live の間はヘッダだけでも出す。保存済みでロード前も出さない。
  if (!shouldPollLive) {
    if (!hasLoadedRun) return null;
    if (displayItems.length === 0 && !failedRun) return null;
  }

  const headerLabel = failedRun
    ? run?.status === "cancelled"
      ? "実行を停止"
      : "実行に失敗"
    : workSummary(displayItems, shouldPollLive);

  if (
    displayItems.length === 1 &&
    !hasMeaningfulDetails(displayItems[0]) &&
    !failedRun
  ) {
    return (
      <div
        className="mb-1.5 max-w-full text-xs"
        data-testid="agent-run-work-log"
      >
        <ul>
          <TimelineRow
            item={displayItems[0]}
            onContentChange={onContentChange}
          />
        </ul>
      </div>
    );
  }

  return (
    <div className="mb-1.5 max-w-full text-xs" data-testid="agent-run-work-log">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        className="flex w-full min-w-0 items-center gap-1.5 rounded-md py-0.5 text-left text-[11px] text-muted-foreground transition-colors hover:text-foreground"
      >
        {shouldPollLive ? (
          <Loader2 className="size-3.5 shrink-0 animate-spin" />
        ) : (
          <ChevronDown
            className={cn(
              "size-3.5 shrink-0 transition-transform",
              expanded && "rotate-180",
            )}
          />
        )}
        <span
          className={cn(
            "min-w-0 flex-1 truncate font-medium",
            failedRun && "text-destructive",
          )}
        >
          {headerLabel}
        </span>
      </button>

      {expanded && (
        <div className="mt-1 border-l border-border/60 pl-3">
          {failedRun && run?.error && (
            <div className="mb-1 flex items-start gap-1.5 text-destructive">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span className="min-w-0 [overflow-wrap:anywhere]">
                {run.error}
              </span>
            </div>
          )}
          {error && (
            <div className="mb-1 flex items-start gap-1.5 text-destructive">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span className="min-w-0 [overflow-wrap:anywhere]">{error}</span>
            </div>
          )}
          {displayItems.length > 0 ? (
            <ul className="min-w-0 space-y-0">
              {displayItems.map((item) => (
                <TimelineRow
                  key={item.id}
                  item={item}
                  onContentChange={onContentChange}
                />
              ))}
            </ul>
          ) : (
            shouldPollLive && (
              <div className="flex items-center gap-1.5 py-0.5 text-muted-foreground">
                <Loader2 className="size-3 animate-spin" />
                準備中
              </div>
            )
          )}
          <div ref={scrollBottomRef} />
        </div>
      )}
    </div>
  );
}

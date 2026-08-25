"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Brain,
  ChevronDown,
  Loader2,
  PanelRight,
} from "lucide-react";
import type { Components } from "react-markdown";
import type { AgentRunTimelineItem } from "@/lib/chat-api";
import { useAgentRun } from "@/hooks/use-agent-run";
import {
  initialAgentRunTimelineExpanded,
  nextAgentRunTimelineExpanded,
  shouldPollAgentRunTimeline,
} from "@/lib/agent-run-timeline-state";
import {
  hasMeaningfulDetails,
  agentRunElapsedMs,
  agentRunUsage,
  formatAgentRunTokens,
  formatSeconds,
  isFileEdit,
  operationCommand,
  operationMeta,
  operationPaths,
  operationStatusLabel,
  operationUrls,
  payloadValue,
  toolRowSummary,
} from "@/lib/agent-run-timeline-format";
import {
  collapseAgentLifecycleRows,
  isDisplayableTimelineItem,
  isOperationRow,
  liveTimelineActivityLabel,
  resolveChildRunId,
  thinkingRowLabel,
  timelineItemTitle,
  timelineRowKind,
  timelineDisplayTextContent,
} from "@/lib/agent-run-timeline-rows";
import {
  operationIcon,
  operationTypeIcon,
} from "@/components/chat/agent-run-timeline-icons";
import {
  AgentRunDetailSheet,
  type AgentRunDetailView,
} from "@/components/chat/agent-run-detail-sheet";
import { BorderBeam } from "@/components/magicui/border-beam";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { cn } from "@/lib/utils";

type AgentRunTimelineProps = {
  runId?: string | null;
  live?: boolean;
  generationKey?: string | null;
  generationStartedAt?: string | null;
  activityMessage?: string | null;
  onContentChange?: () => boolean | void;
};

/** 途中経過テキストはタイムライン内の小さめ文字に合わせて描画する */
const TIMELINE_TEXT_MARKDOWN: Partial<Components> = {
  p: ({ children }) => (
    <p className="mb-1.5 max-w-full [overflow-wrap:anywhere] last:mb-0">
      {children}
    </p>
  ),
  li: ({ children }) => <li className="text-xs">{children}</li>,
  pre: ({ children }) => (
    <pre className="my-1.5 max-w-full overflow-x-auto rounded-md bg-black/20 p-2 text-[11px]">
      {children}
    </pre>
  ),
  code: ({ className, children, ...props }) => {
    const isInline = !className;
    if (isInline) {
      return (
        <code
          className="rounded bg-black/20 px-1 py-0.5 text-[11px] [overflow-wrap:anywhere]"
          {...props}
        >
          {children}
        </code>
      );
    }
    return (
      <code className={cn("max-w-full", className)} {...props}>
        {children}
      </code>
    );
  },
};

const SAFE_AGENT_FAILURE_MESSAGE =
  "応答の生成に失敗しました。もう一度お試しください。";
const SAFE_OPERATION_FAILURE_MESSAGE =
  "操作に失敗しました。もう一度お試しください。";
const SAFE_LOG_FAILURE_MESSAGE = "実行ログを取得できませんでした。";

/**
 * Technical provider/stack details belong in the audit sheet only.  The
 * compact timeline is a user-facing status surface, so redact known internal
 * errors (and multiline stack traces) before rendering them there.
 */
function containsTechnicalFailureDetail(value: string): boolean {
  return (
    /assistant generation returned no response/i.test(value) ||
    /(?:^|\n)\s*(?:traceback|at\s+[^\n(]+\(|caused by:)/i.test(value) ||
    /(?:^|\n)\s*File\s+".+",\s*line\s+\d+/i.test(value) ||
    /(?:^|\n)\s*at\s+\S+.*:\d+:\d+/i.test(value) ||
    /(?:^|\n)\s*(?:Error|Exception):/i.test(value) ||
    /\b(?:stack trace|internal server error|exception)\b/i.test(value)
  );
}

function safeTimelineOperationError(value: string): string {
  const normalized = value.trim();
  if (!normalized) return "";
  if (containsTechnicalFailureDetail(normalized)) {
    return SAFE_OPERATION_FAILURE_MESSAGE;
  }
  return normalized;
}

function DetailButton({
  label,
  onClick,
  className,
}: {
  label: string;
  onClick: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onClick();
      }}
      className={cn(
        "shrink-0 rounded p-0.5 text-muted-foreground/60 transition hover:text-foreground focus-visible:opacity-100",
        className,
      )}
    >
      <PanelRight className="size-3.5" />
    </button>
  );
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
  const error = safeTimelineOperationError(
    String(item.error ?? payloadValue(item, "stderr") ?? ""),
  );
  const stdout = String(payloadValue(item, "stdout") ?? "");
  const diff = String(payloadValue(item, "diff", "patch") ?? "");
  const exitCode = payloadValue(item, "exit_code", "exit");
  const urls = operationUrls(item);
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

// ─── 途中経過テキスト（地の文） ───

function AssistantTextRow({ item }: { item: AgentRunTimelineItem }) {
  const text = timelineDisplayTextContent(item);
  if (!text) return null;
  return (
    <li className="min-w-0 py-1 text-xs leading-relaxed text-foreground/90">
      <MarkdownContent
        content={text}
        components={TIMELINE_TEXT_MARKDOWN}
        breaks
      />
    </li>
  );
}

// ─── 思考（グレーの折りたたみ） ───

function ThinkingRow({
  item,
  onContentChange,
}: {
  item: AgentRunTimelineItem;
  onContentChange?: () => boolean | void;
}) {
  const text = timelineDisplayTextContent(item);
  if (!text) return null;
  return (
    <li className="min-w-0">
      <details
        className="group min-w-0"
        onToggle={(event) => {
          if (!event.currentTarget.open) return;
          requestAnimationFrame(() => onContentChange?.());
        }}
      >
        <summary className="flex min-w-0 cursor-pointer list-none items-center gap-1.5 py-1 text-[11px] text-muted-foreground/70 transition-colors hover:text-muted-foreground">
          <Brain className="size-3.5 shrink-0" />
          <span className="min-w-0 flex-1 truncate">
            {thinkingRowLabel(item)}
          </span>
          <ChevronDown className="size-3.5 shrink-0 transition-transform group-open:rotate-180" />
        </summary>
        <div className="whitespace-pre-wrap border-l border-border/50 py-1 pl-3 text-[11px] leading-relaxed text-muted-foreground/70 [overflow-wrap:anywhere]">
          {text}
        </div>
      </details>
    </li>
  );
}

// ─── 1 論理作業の描画 ───

function TimelineRow({
  item,
  onContentChange,
  onOpenDetail,
}: {
  item: AgentRunTimelineItem;
  onContentChange?: () => boolean | void;
  onOpenDetail?: (item: AgentRunTimelineItem) => void;
}) {
  const kind = timelineRowKind(item);
  if (!kind) return null;
  if (kind === "text") return <AssistantTextRow item={item} />;
  if (kind === "thinking") {
    return <ThinkingRow item={item} onContentChange={onContentChange} />;
  }

  const failed =
    item.success === false || item.status === "failed" || Boolean(item.error);
  const title = timelineItemTitle(item);
  const summary = kind === "tool" ? toolRowSummary(item) : "";
  const meta = operationMeta(item);
  const details = hasMeaningfulDetails(item);
  const detailLabel = resolveChildRunId(item)
    ? "サブエージェントの実行ログを開く"
    : "詳細を開く";
  const row = (
    <div
      className={cn(
        "group/row flex min-w-0 items-center gap-2 py-1 text-xs",
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
      {onOpenDetail && (
        <DetailButton
          label={detailLabel}
          onClick={() => onOpenDetail(item)}
          className="opacity-0 group-hover/row:opacity-100 focus:opacity-100"
        />
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

function workSummary(
  items: AgentRunTimelineItem[],
  live: boolean,
  hasDisplayItems: boolean,
  liveActivityLabel: string | null,
): string {
  if (items.length === 0) {
    if (liveActivityLabel) return liveActivityLabel;
    if (live && hasDisplayItems) return "実行中";
    return live ? "実行を準備中" : "実行ログ";
  }
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

function timelineRunFailureMessage(
  status: string | undefined,
  hasRunError: boolean,
): string {
  if (status === "cancelled") return "実行を停止しました。";
  if (status === "failed" || hasRunError) return SAFE_AGENT_FAILURE_MESSAGE;
  return SAFE_LOG_FAILURE_MESSAGE;
}

export function AgentRunTimeline({
  runId,
  live = false,
  generationKey,
  generationStartedAt,
  activityMessage,
  onContentChange,
}: AgentRunTimelineProps) {
  const parsedGenerationStart = generationStartedAt
    ? Date.parse(generationStartedAt)
    : NaN;
  const [expanded, setExpanded] = useState(
    initialAgentRunTimelineExpanded(live, runId),
  );
  const [detailViews, setDetailViews] = useState<AgentRunDetailView[]>([]);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailKey, setDetailKey] = useState(0);
  const [clientStartMs, setClientStartMs] = useState(() =>
    Number.isFinite(parsedGenerationStart) ? parsedGenerationStart : Date.now(),
  );
  const [nowMs, setNowMs] = useState(() => Date.now());
  const scrollBottomRef = useRef<HTMLDivElement>(null);

  const {
    run,
    error,
    refresh: refreshRun,
  } = useAgentRun(runId, {
    // 同じRunを表示する右パネルと取得を共有し、terminalになるまでだけ更新する。
    poll: Boolean(runId),
    pollTimeoutMs: live ? null : 30_000,
  });

  const shouldPollLive = shouldPollAgentRunTimeline(live, runId, run?.status);
  const liveClockActive = live && (!runId || shouldPollLive);
  const hasRunError = Boolean(run?.error);
  const hasLoadedRun = run !== null;

  const lifecycleKey = generationKey ?? runId ?? null;
  const lifecycleResetRunId = generationKey ? null : runId;
  // generation単位でのみ開始時刻をリセットする。run idの後着では継続する。
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- prop 変化に伴う状態リセット
    setExpanded(initialAgentRunTimelineExpanded(live, lifecycleResetRunId));
    setDetailOpen(false);
    const parsedStart = generationStartedAt
      ? Date.parse(generationStartedAt)
      : NaN;
    setClientStartMs(Number.isFinite(parsedStart) ? parsedStart : Date.now());
    setNowMs(Date.now());
  }, [generationStartedAt, lifecycleKey, lifecycleResetRunId, live]);

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

  // runId / live変更時の初回ロード。取得結果は本文タイムラインと右パネルで共有する。
  useEffect(() => {
    if (!runId) return;
    void refreshRun();
  }, [live, refreshRun, runId]);

  // 経過時間表示だけを1秒ごとに更新し、既存のAPIポーリング周期は変えない。
  useEffect(() => {
    if (!liveClockActive) return;
    const updateNow = () => setNowMs(Date.now());
    updateNow();
    const intervalId = window.setInterval(updateNow, 1000);
    return () => window.clearInterval(intervalId);
  }, [lifecycleKey, liveClockActive]);

  const items = useMemo(() => run?.timeline ?? [], [run]);
  const displayItems = useMemo(
    () => collapseAgentLifecycleRows(items.filter(isDisplayableTimelineItem)),
    [items],
  );
  const liveActivityLabel = useMemo(
    () => (shouldPollLive ? liveTimelineActivityLabel(items) : null),
    [items, shouldPollLive],
  );
  // 「N件の操作」はツール・エージェント・検証行だけを数える
  const operationItems = useMemo(
    () => displayItems.filter(isOperationRow),
    [displayItems],
  );
  const usage = useMemo(() => agentRunUsage(run, items), [items, run]);

  const openDetail = useCallback((views: AgentRunDetailView[]) => {
    setDetailViews(views);
    setDetailKey((value) => value + 1);
    setDetailOpen(true);
  }, []);

  const openRunDetail = useCallback(() => {
    if (!runId) return;
    openDetail([{ kind: "run", runId }]);
  }, [openDetail, runId]);

  const openItemDetail = useCallback(
    (item: AgentRunTimelineItem) => {
      if (!runId) return;
      const childRunId = resolveChildRunId(item);
      openDetail([
        { kind: "run", runId },
        childRunId
          ? { kind: "run", runId: childRunId }
          : { kind: "item", runId, itemId: item.id },
      ]);
    },
    [openDetail, runId],
  );

  useEffect(() => {
    if (!expanded) return;
    if (onContentChange?.() === false) return;
    scrollBottomRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [expanded, displayItems.length, error, onContentChange]);

  if (!runId) {
    if (!live) return null;
    const elapsed = Math.max(0, nowMs - clientStartMs);
    return (
      <div
        className="relative mb-1.5 max-w-full overflow-hidden rounded-md border border-primary/20 bg-primary/[0.025]"
        data-testid="agent-run-work-log"
        data-agent-run-active="true"
        data-generation-key={generationKey ?? undefined}
      >
        <BorderBeam
          size={56}
          duration={10}
          borderWidth={1}
          colorFrom="var(--primary)"
          colorTo="var(--chart-2)"
          className="pointer-events-none"
        />
        <div className="relative z-10 flex min-w-0 items-center gap-2 px-2 py-1 text-xs text-muted-foreground">
          <Loader2 className="size-3 animate-spin" />
          <span className="min-w-0 truncate">{activityMessage || "応答を準備しています"}</span>
          <span className="shrink-0">{formatSeconds(elapsed / 1000)}</span>
        </div>
      </div>
    );
  }

  const failedRun =
    run?.status === "failed" || run?.status === "cancelled" || hasRunError;
  const elapsedMs = agentRunElapsedMs(
    run,
    items,
    nowMs,
    clientStartMs,
    shouldPollLive,
  );
  const hasRunMetrics = Boolean(usage) || elapsedMs !== null;

  // 表示可能アイテムが 0 件で終了済みの run はブロック自体を出さない。
  // live の間はヘッダだけでも出す。保存済みでロード前も出さない。
  if (!shouldPollLive) {
    if (!hasLoadedRun) return null;
    if (displayItems.length === 0 && !failedRun && !hasRunMetrics) return null;
  }

  const detailSheet =
    detailViews.length > 0 ? (
      <AgentRunDetailSheet
        key={detailKey}
        open={detailOpen}
        onOpenChange={setDetailOpen}
        views={detailViews}
        fallbackRun={run}
      />
    ) : null;

  const headerSummary = failedRun
    ? run?.status === "cancelled"
      ? "実行を停止"
      : "実行に失敗"
    : workSummary(
        operationItems,
        shouldPollLive,
        displayItems.length > 0,
        liveActivityLabel,
      );
  const headerDetails = [
    elapsedMs !== null ? formatSeconds(elapsedMs / 1000) : "",
    formatAgentRunTokens(usage),
  ].filter(Boolean);
  const headerLabel = [headerSummary, ...headerDetails].join("  ");

  if (
    displayItems.length === 1 &&
    operationItems.length === 1 &&
    !hasMeaningfulDetails(displayItems[0]) &&
    !failedRun &&
    !shouldPollLive &&
    !hasRunMetrics
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
            onOpenDetail={openItemDetail}
          />
        </ul>
        {detailSheet}
      </div>
    );
  }

  const timelineBody = (
    <>
      <div className="flex w-full min-w-0 items-center gap-1">
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md py-0.5 text-left text-[11px] text-muted-foreground transition-colors hover:text-foreground"
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
        <DetailButton label="詳細・監査ログを開く" onClick={openRunDetail} />
      </div>

      {expanded && (
        <div className="mt-1 border-l border-border/60 pl-3">
          {failedRun && (
            <div className="mb-1 flex items-start gap-1.5 text-destructive">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span className="min-w-0 [overflow-wrap:anywhere]">
                {timelineRunFailureMessage(run?.status, hasRunError)}
              </span>
            </div>
          )}
          {error && (
            <div className="mb-1 flex items-start gap-1.5 text-destructive">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span className="min-w-0 [overflow-wrap:anywhere]">
                {SAFE_LOG_FAILURE_MESSAGE}
              </span>
            </div>
          )}
          {displayItems.length > 0 ? (
            <ul className="min-w-0 space-y-0">
              {displayItems.map((item) => (
                <TimelineRow
                  key={item.id}
                  item={item}
                  onContentChange={onContentChange}
                  onOpenDetail={openItemDetail}
                />
              ))}
            </ul>
          ) : (
            shouldPollLive && (
              <div className="flex items-center gap-1.5 py-0.5 text-muted-foreground">
                <Loader2 className="size-3 animate-spin" />
                {liveActivityLabel ?? "準備中"}
              </div>
            )
          )}
          <div ref={scrollBottomRef} />
        </div>
      )}
      {detailSheet}
    </>
  );

  return (
    <div
      className={cn(
        "relative mb-1.5 max-w-full text-xs",
        shouldPollLive &&
          "overflow-hidden rounded-md border border-primary/20 bg-primary/[0.025]",
      )}
      data-testid="agent-run-work-log"
      data-agent-run-active={shouldPollLive ? "true" : undefined}
    >
      {shouldPollLive && (
        <BorderBeam
          size={72}
          duration={10}
          borderWidth={1}
          colorFrom="var(--primary)"
          colorTo="var(--chart-2)"
          className="pointer-events-none"
        />
      )}
      <div className={cn("relative z-10", shouldPollLive && "px-1.5 py-1")}>
        {timelineBody}
      </div>
    </div>
  );
}

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, ChevronRight, Loader2, Users } from "lucide-react";
import {
  chatApi,
  type AgentRun,
  type AgentRunEvent,
  type AgentRunTimelineItem,
} from "@/lib/chat-api";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  agentRunDurationMs,
  agentRunStatusLabel,
  formatDuration,
  formatTimestamp,
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
  findRawToolCall,
  isDisplayableTimelineItem,
  resolveChildRunId,
  timelineItemTitle,
  timelineRowKind,
  timelineTextContent,
} from "@/lib/agent-run-timeline-rows";
import {
  operationIcon,
  operationTypeIcon,
} from "@/components/chat/agent-run-timeline-icons";
import { cn } from "@/lib/utils";

/** サイドバーが表示する 1 画面ぶんの対象 */
export type AgentRunDetailView =
  | { kind: "run"; runId: string }
  | { kind: "item"; runId: string; itemId: string };

type AgentRunDetailSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 初期スタック。先頭が最上位（戻る先）、末尾が最初に表示する画面 */
  views: AgentRunDetailView[];
  /** 親タイムラインが既に保持している run（fetch 前のフォールバック） */
  fallbackRun?: AgentRun | null;
};

function isEmptyRecord(value?: Record<string, unknown> | null): boolean {
  return !value || Object.keys(value).length === 0;
}

function DetailSection({
  label,
  value,
  mono = true,
}: {
  label: string;
  value?: string | null;
  mono?: boolean;
}) {
  const text = String(value ?? "");
  if (!text.trim()) return null;
  return (
    <section className="space-y-1">
      <h4 className="text-[11px] font-medium text-muted-foreground">{label}</h4>
      <pre
        className={cn(
          "max-w-full overflow-x-auto whitespace-pre-wrap rounded-md bg-muted/50 px-3 py-2 text-[11px] leading-relaxed text-foreground [overflow-wrap:anywhere]",
          mono ? "font-mono" : "font-sans",
        )}
      >
        {text}
      </pre>
    </section>
  );
}

function MetaRow({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  return (
    <div className="flex gap-2 text-[11px]">
      <span className="w-20 shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 flex-1 text-foreground [overflow-wrap:anywhere]">
        {value}
      </span>
    </div>
  );
}

/** サイドバー内のタイムライン一覧（クリックで項目詳細・子 run へ） */
function TimelineList({
  items,
  onSelect,
}: {
  items: AgentRunTimelineItem[];
  onSelect: (item: AgentRunTimelineItem) => void;
}) {
  if (items.length === 0) {
    return (
      <p className="text-[11px] text-muted-foreground">
        表示できる操作はありません。
      </p>
    );
  }
  return (
    <ul className="space-y-0.5">
      {items.map((item) => {
        const kind = timelineRowKind(item);
        const failed =
          item.success === false ||
          item.status === "failed" ||
          Boolean(item.error);
        const summary =
          kind === "tool"
            ? toolRowSummary(item)
            : kind === "text" || kind === "thinking"
              ? timelineTextContent(item)
              : "";
        return (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => onSelect(item)}
              className="flex w-full min-w-0 items-center gap-2 rounded-md px-1.5 py-1 text-left text-[11px] transition-colors hover:bg-muted/60"
            >
              <span
                className={cn(
                  "shrink-0",
                  failed ? "text-destructive" : "text-muted-foreground",
                )}
              >
                {operationIcon(item)}
              </span>
              <span className="shrink-0 text-muted-foreground/75">
                {operationTypeIcon(item)}
              </span>
              <span className="min-w-0 flex-1 truncate font-medium text-foreground">
                {timelineItemTitle(item)}
              </span>
              {summary && (
                <span className="min-w-0 max-w-[45%] truncate text-muted-foreground">
                  {summary}
                </span>
              )}
              <ChevronRight className="size-3.5 shrink-0 text-muted-foreground/60" />
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function AuditEventRow({ event }: { event: AgentRunEvent }) {
  const payload = event.payload ?? {};
  return (
    <details className="rounded-md border border-border/50 bg-muted/20 px-2 py-1.5">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-[11px]">
        <span className="min-w-0 flex-1 truncate font-medium text-foreground">
          {event.event_type}
        </span>
        {event.status && (
          <span className="shrink-0 text-muted-foreground">{event.status}</span>
        )}
        <span className="shrink-0 text-muted-foreground/70">
          {formatTimestamp(event.created_at)}
        </span>
      </summary>
      <div className="mt-2 space-y-2 border-l border-border/60 pl-2 text-[11px]">
        <MetaRow label="event ID" value={event.id} />
        <MetaRow label="run ID" value={event.run_id} />
        <MetaRow
          label="sequence"
          value={event.sequence !== null && event.sequence !== undefined
            ? String(event.sequence)
            : null}
        />
        {event.message && (
          <div className="whitespace-pre-wrap text-foreground [overflow-wrap:anywhere]">
            {event.message}
          </div>
        )}
        {!isEmptyRecord(payload) && (
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-muted/60 p-2 font-mono text-[10px] leading-relaxed [overflow-wrap:anywhere]">
            {JSON.stringify(payload, null, 2)}
          </pre>
        )}
      </div>
    </details>
  );
}

function AuditTrail({ run }: { run: AgentRun }) {
  const events = run.events ?? [];
  const toolCalls = run.tool_calls ?? [];
  if (events.length === 0 && toolCalls.length === 0) return null;

  return (
    <details
      className="rounded-md border border-border/60"
      data-testid="agent-run-audit-log"
    >
      <summary className="cursor-pointer list-none px-2.5 py-2 text-[11px] font-medium text-foreground">
        詳細・監査ログ（{events.length}イベント / {toolCalls.length}ツール）
      </summary>
      <div className="space-y-3 border-t border-border/60 p-2.5">
        {events.length > 0 && (
          <section className="space-y-1.5">
            <h5 className="text-[11px] font-medium text-muted-foreground">
              生イベント
            </h5>
            <div className="space-y-1">
              {events.map((event) => (
                <AuditEventRow key={event.id} event={event} />
              ))}
            </div>
          </section>
        )}
        {toolCalls.length > 0 && (
          <section className="space-y-1.5">
            <h5 className="text-[11px] font-medium text-muted-foreground">
              ツール監査
            </h5>
            <div className="space-y-1">
              {toolCalls.map((call) => (
                <details
                  key={call.id}
                  className="rounded-md border border-border/50 bg-muted/20 px-2 py-1.5"
                >
                  <summary className="flex cursor-pointer list-none items-center gap-2 text-[11px]">
                    <span className="min-w-0 flex-1 truncate font-medium text-foreground">
                      {call.tool_name}
                    </span>
                    <span className="shrink-0 text-muted-foreground">
                      {call.success ? "成功" : "失敗"}
                    </span>
                  </summary>
                  <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-muted/60 p-2 font-mono text-[10px] leading-relaxed [overflow-wrap:anywhere]">
                    {JSON.stringify(call, null, 2)}
                  </pre>
                </details>
              ))}
            </div>
          </section>
        )}
      </div>
    </details>
  );
}

/** run 全体ビュー */
function RunView({
  run,
  onSelectItem,
}: {
  run: AgentRun;
  onSelectItem: (item: AgentRunTimelineItem) => void;
}) {
  const items = collapseAgentLifecycleRows(
    (run.timeline ?? []).filter(isDisplayableTimelineItem),
  );
  const duration = agentRunDurationMs(run);
  const providerModel = [run.provider, run.model].filter(Boolean).join("/");
  return (
    <div className="space-y-4">
      <div className="space-y-1 rounded-md border border-border/60 p-2.5">
        <MetaRow label="ステータス" value={agentRunStatusLabel(run.status)} />
        <MetaRow label="種別" value={run.run_type} />
        <MetaRow label="モデル" value={providerModel || null} />
        <MetaRow
          label="生成profile"
          value={run.generation_profile ?? null}
        />
        <MetaRow
          label="所要時間"
          value={duration !== null ? formatDuration(duration) : null}
        />
        <MetaRow label="開始" value={formatTimestamp(run.started_at) || null} />
        <MetaRow label="終了" value={formatTimestamp(run.ended_at) || null} />
        <MetaRow label="run ID" value={run.id} />
        <MetaRow label="親 run" value={run.parent_run_id ?? null} />
      </div>
      {run.error && (
        <div className="flex items-start gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-2 text-[11px] text-destructive">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span className="min-w-0 [overflow-wrap:anywhere]">{run.error}</span>
        </div>
      )}
      <DetailSection label="依頼内容" value={run.objective} mono={false} />
      {!isEmptyRecord(run.result) && (
        <DetailSection
          label="実行結果"
          value={JSON.stringify(run.result, null, 2)}
        />
      )}
      {!isEmptyRecord(run.validation) && (
        <DetailSection
          label="検証"
          value={JSON.stringify(run.validation, null, 2)}
        />
      )}
      {!isEmptyRecord(run.metadata) && (
        <DetailSection
          label="メタデータ"
          value={JSON.stringify(run.metadata, null, 2)}
        />
      )}
      <section className="space-y-1.5">
        <h4 className="text-[11px] font-medium text-muted-foreground">
          タイムライン（{items.length}件）
        </h4>
        <TimelineList items={items} onSelect={onSelectItem} />
      </section>
      <AuditTrail run={run} />
    </div>
  );
}

/** 項目詳細ビュー */
function ItemView({
  item,
  run,
  onOpenChildRun,
}: {
  item: AgentRunTimelineItem;
  run: AgentRun | null;
  onOpenChildRun: (childRunId: string) => void;
}) {
  const kind = timelineRowKind(item);
  const rawToolCall = findRawToolCall(run, item);
  const childRunId = resolveChildRunId(item);
  const text = timelineTextContent(item);
  const command = operationCommand(item);
  const paths = operationPaths(item);
  const args = rawToolCall?.arguments ?? item.arguments ?? {};
  const result = String(rawToolCall?.result ?? item.result ?? item.result_preview ?? "");
  const stdout = String(payloadValue(item, "stdout") ?? "");
  const stderr = String(payloadValue(item, "stderr") ?? "");
  const diff = String(payloadValue(item, "diff", "patch") ?? "");
  const exitCode = payloadValue(item, "exit_code", "exit");
  const urls = operationUrls(item);
  const meta = operationMeta(item);

  if (kind === "text" || kind === "thinking") {
    return (
      <div className="space-y-3">
        <div className="space-y-1 rounded-md border border-border/60 p-2.5">
          <MetaRow
            label="種別"
            value={kind === "text" ? "途中経過テキスト" : "思考"}
          />
          <MetaRow label="時刻" value={formatTimestamp(item.created_at) || null} />
        </div>
        <DetailSection label="本文" value={text} mono={false} />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="space-y-1 rounded-md border border-border/60 p-2.5">
        <MetaRow label="状態" value={operationStatusLabel(item)} />
        <MetaRow
          label="ツール"
          value={item.tool_name ?? item.raw_tool_name ?? null}
        />
        <MetaRow
          label="raw名"
          value={
            item.raw_tool_name && item.raw_tool_name !== item.tool_name
              ? item.raw_tool_name
              : null
          }
        />
        <MetaRow label="担当" value={item.actor_label ?? null} />
        <MetaRow label="開始" value={formatTimestamp(item.started_at) || null} />
        <MetaRow label="終了" value={formatTimestamp(item.ended_at) || null} />
        <MetaRow
          label="所要時間"
          value={formatDuration(item.duration_ms ?? rawToolCall?.duration_ms) || null}
        />
        <MetaRow label="属性" value={meta || null} />
        <MetaRow
          label="exit code"
          value={exitCode !== undefined ? String(exitCode) : null}
        />
      </div>
      {childRunId && (
        <button
          type="button"
          onClick={() => onOpenChildRun(childRunId)}
          className="flex w-full items-center gap-2 rounded-md border border-border/60 px-2.5 py-2 text-left text-[11px] text-foreground transition-colors hover:bg-muted/60"
        >
          <Users className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="min-w-0 flex-1 truncate">
            サブエージェントの実行ログを開く
          </span>
          <ChevronRight className="size-3.5 shrink-0 text-muted-foreground/60" />
        </button>
      )}
      {paths.length > 0 && (
        <DetailSection label="対象ファイル" value={paths.join("\n")} />
      )}
      <DetailSection label="実行したコマンド" value={command ? `$ ${command}` : ""} />
      {Object.keys(args).length > 0 && (
        <DetailSection label="引数" value={JSON.stringify(args, null, 2)} />
      )}
      <DetailSection label="標準出力" value={stdout} />
      <DetailSection label="結果" value={result} />
      <DetailSection label="エラー" value={item.error ?? stderr} />
      <DetailSection label="差分" value={diff} />
      {urls.length > 0 && (
        <section className="space-y-1">
          <h4 className="text-[11px] font-medium text-muted-foreground">
            参照URL
          </h4>
          {urls.map((url) => (
            <a
              key={url}
              href={url}
              target="_blank"
              rel="noreferrer"
              className="block truncate text-[11px] text-primary underline underline-offset-2"
            >
              {url}
            </a>
          ))}
        </section>
      )}
      {!isEmptyRecord(item.payload) && (
        <DetailSection
          label="生ペイロード"
          value={JSON.stringify(item.payload, null, 2)}
        />
      )}
    </div>
  );
}

export function AgentRunDetailSheet({
  open,
  onOpenChange,
  views,
  fallbackRun,
}: AgentRunDetailSheetProps) {
  const [stack, setStack] = useState<AgentRunDetailView[]>(views);
  const [runs, setRuns] = useState<Record<string, AgentRun>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const requestedRef = useRef<Set<string>>(new Set());
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const current = stack.length > 0 ? stack[stack.length - 1] : null;
  const currentRunId = current?.runId ?? null;

  const loadRun = useCallback(async (runId: string) => {
    try {
      const response = await chatApi.getAgentRun(runId, {
        includeEvents: true,
        includeToolCalls: true,
        includeTimeline: true,
      });
      if (!mountedRef.current) return;
      setRuns((previous) => ({ ...previous, [runId]: response.agent_run }));
    } catch (err) {
      if (!mountedRef.current) return;
      setErrors((previous) => ({
        ...previous,
        [runId]:
          err instanceof Error ? err.message : "詳細を取得できませんでした",
      }));
    }
  }, []);

  useEffect(() => {
    if (!open || !currentRunId) return;
    if (requestedRef.current.has(currentRunId)) return;
    requestedRef.current.add(currentRunId);
    void loadRun(currentRunId);
  }, [currentRunId, loadRun, open]);

  const currentRun = useMemo(() => {
    if (!currentRunId) return null;
    const fetched = runs[currentRunId];
    if (fetched) return fetched;
    if (fallbackRun && fallbackRun.id === currentRunId) return fallbackRun;
    return null;
  }, [currentRunId, fallbackRun, runs]);

  const currentItem = useMemo(() => {
    if (!current || current.kind !== "item") return null;
    const items = collapseAgentLifecycleRows(
      (currentRun?.timeline ?? []).filter(isDisplayableTimelineItem),
    );
    return items.find((item) => item.id === current.itemId) ?? null;
  }, [current, currentRun]);

  const pushView = useCallback((view: AgentRunDetailView) => {
    setStack((previous) => [...previous, view]);
  }, []);

  const popView = useCallback(() => {
    setStack((previous) =>
      previous.length > 1 ? previous.slice(0, -1) : previous,
    );
  }, []);

  const handleSelectItem = useCallback(
    (item: AgentRunTimelineItem) => {
      if (!currentRunId) return;
      const childRunId = resolveChildRunId(item);
      if (childRunId) {
        pushView({ kind: "run", runId: childRunId });
        return;
      }
      pushView({ kind: "item", runId: currentRunId, itemId: item.id });
    },
    [currentRunId, pushView],
  );

  const handleOpenChildRun = useCallback(
    (childRunId: string) => {
      pushView({ kind: "run", runId: childRunId });
    },
    [pushView],
  );

  const error = currentRunId ? errors[currentRunId] : undefined;
  const loading = Boolean(currentRunId) && !currentRun && !error;
  const title =
    current?.kind === "item"
      ? currentItem
        ? timelineItemTitle(currentItem)
        : "操作の詳細"
      : currentRun?.title || "実行ログの詳細";
  const description =
    current?.kind === "item"
      ? currentRun?.title || currentRunId || ""
      : agentRunStatusLabel(currentRun?.status);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="flex w-[min(94vw,40rem)] flex-col gap-0 p-0 sm:max-w-xl"
      >
        <SheetHeader className="border-b px-4 py-3 pr-12">
          {stack.length > 1 && (
            <button
              type="button"
              onClick={popView}
              className="mb-1 flex items-center gap-1 self-start text-[11px] text-muted-foreground transition-colors hover:text-foreground"
            >
              <ArrowLeft className="size-3.5" />
              戻る
            </button>
          )}
          <SheetTitle className="truncate text-sm">{title}</SheetTitle>
          <SheetDescription className="truncate text-[11px]">
            {description}
          </SheetDescription>
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 text-xs">
          {error && (
            <div className="flex items-start gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-2 text-[11px] text-destructive">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span className="min-w-0 [overflow-wrap:anywhere]">{error}</span>
            </div>
          )}
          {loading && (
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" />
              読み込み中
            </div>
          )}
          {!loading && !error && currentRun && current?.kind === "run" && (
            <RunView run={currentRun} onSelectItem={handleSelectItem} />
          )}
          {!loading && !error && current?.kind === "item" && (
            currentItem ? (
              <ItemView
                item={currentItem}
                run={currentRun}
                onOpenChildRun={handleOpenChildRun}
              />
            ) : (
              <p className="text-[11px] text-muted-foreground">
                この操作の詳細は見つかりませんでした。
              </p>
            )
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

"use client";

import { useCallback, useMemo, useState, type KeyboardEvent } from "react";
import {
  Activity,
  CheckCircle2,
  Info,
  Mic,
  RefreshCcw,
  X,
  WifiOff,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { AgentRunTimeline } from "@/components/chat/agent-run-timeline";
import {
  RelatedInformationPanel,
  type RelatedInformationSection,
  type RelatedTaskSummary,
} from "@/components/chat/related-information-panel";
import type {
  ContextSnapshot,
  ConversationMessage,
  ConversationSession,
} from "@/lib/chat-api";
import { resolveMainContextSnapshot } from "@/lib/chat-api";
import { normalizeVoiceRms } from "@/lib/voice-level";
import { useContextRailVoice } from "@/hooks/use-context-rail-voice";

type RailTab = "context" | "execution";

function VoiceStatus({ enabled }: { enabled: boolean }) {
  const { connected, status, config, loading, error, refresh } =
    useContextRailVoice(enabled);
  const level = normalizeVoiceRms(status?.rms);

  const label = !connected
    ? "接続なし"
    : loading && !status
      ? "音声状態を確認中"
      : error && !status
        ? "音声状態を取得できません"
        : !status?.ready
          ? "音声入力を利用できません"
          : status.recording
            ? "録音中"
            : "入力待機";
  const tone = status?.recording
    ? "bg-red-500"
    : status?.ready
      ? "bg-primary"
      : "bg-muted-foreground/40";

  return (
    <section
      className="shrink-0 border-b border-border-subtle bg-surface-charcoal px-4 py-3"
      aria-label="音声入力ステータス"
      data-testid="chat-voice-status"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          {connected ? (
            <Mic className="size-4 shrink-0 text-primary" aria-hidden="true" />
          ) : (
            <WifiOff className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          )}
          <span className="truncate text-xs font-medium">音声入力</span>
          <span className="truncate text-[11px] text-text-secondary">{label}</span>
        </div>
        {error && (
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            className="size-6 shrink-0 text-text-secondary"
            onClick={() => void refresh()}
            aria-label="音声入力状態を再取得"
            title="再取得"
          >
            <RefreshCcw className="size-3" />
          </Button>
        )}
      </div>
      <div
        className="mt-2 h-2 w-full overflow-hidden rounded-full bg-surface-slate"
        role="meter"
        aria-label="音声入力レベル"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(level * 100)}
      >
        <div
          // CSS interpolation provides a short attack/decay trail without a
          // render loop when the rail is idle or the tab is hidden.
          className={`h-full rounded-full transition-[width] duration-200 ease-out ${tone}`}
          style={{ width: `${Math.round(level * 100)}%` }}
        />
      </div>
      {(config?.engine || config?.model) && (
        <p className="mt-1 truncate text-[10px] text-text-secondary" title={[config.engine, config.model].filter(Boolean).join(" / ")}>
          ASR {[config.engine, config.model].filter(Boolean).join(" / ")}
        </p>
      )}
    </section>
  );
}

function ContextSnapshotSummary({
  snapshot,
  status,
}: {
  snapshot?: ContextSnapshot | null;
  status?: string;
}) {
  const main = resolveMainContextSnapshot(snapshot);
  if (!main) {
    if (status === "loading") {
      return <p className="px-1 py-1 text-[11px] text-text-secondary">コンテキスト使用量を確認中…</p>;
    }
    return null;
  }
  const measurement = main.measurement;
  const numericAllowed = measurement === "measured" || measurement === "tokenizer_estimate" || measurement === "character_estimate" || measurement === "estimated" || measurement === "approximate";
  const estimated = numericAllowed && measurement !== "measured";
  const used = numericAllowed ? main.input_tokens : null;
  const limit = numericAllowed ? main.context_window_tokens : null;
  const usage = numericAllowed ? main.usage_percent ?? main.percentage : null;
  const categories = (main.categories ?? main.components ?? [])
    .filter((category) => {
      const categoryNumericAllowed = category.measurement === "measured" || category.measurement === "tokenizer_estimate" || category.measurement === "character_estimate" || category.measurement === "estimated" || category.measurement === "approximate";
      return category.status !== "deferred" && category.label && categoryNumericAllowed && (category.tokens != null || category.percentage != null);
    })
    .slice(0, 3);
  if (used == null && limit == null && usage == null && categories.length === 0) return null;
  return (
    <section className="rounded-md border border-border-subtle bg-surface-slate/60 px-3 py-2" aria-label="コンテキスト使用量">
      <div className="flex items-center gap-1.5 text-[11px] font-medium">
        <Info className="size-3.5 text-primary" aria-hidden="true" />
        コンテキスト使用量{estimated && <span className="text-[10px] text-text-secondary">（推定）</span>}
        {usage != null && <span className="ml-auto font-mono text-text-secondary">{Math.round(usage)}%</span>}
      </div>
      {(used != null || limit != null) && (
        <p className="mt-1 text-[10px] text-text-secondary">
          {[used != null ? `${used.toLocaleString()} tokens` : "", limit != null ? `/ ${limit.toLocaleString()}` : ""].join(" ")}
        </p>
      )}
      {categories.length > 0 && (
        <ul className="mt-1.5 space-y-0.5 text-[10px] text-text-secondary">
          {categories.map((category) => (
            <li key={`${category.id ?? category.label}-${category.source ?? ""}`} className="flex justify-between gap-2">
              <span className="truncate">{category.label}</span>
              <span className="shrink-0 font-mono">
                {category.measurement !== "measured" && "≈"}
                {category.tokens != null ? `${category.tokens.toLocaleString()}t` : `${Math.round(category.percentage ?? 0)}%`}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Chat's right Context Rail: voice status first, then context/execution tabs. */
export function ChatContextRail({
  sessionId,
  agentRunId,
  generationKey,
  generationStartedAt,
  activityMessage,
  generationLive,
  onTaskClick,
  onTasksChange,
  onClose,
  messages,
  currentSession,
  projectName,
  contextSnapshot,
  contextSnapshotStatus,
  persistent = false,
}: {
  sessionId: string | null;
  agentRunId?: string | null;
  generationKey?: string | null;
  generationStartedAt?: string | null;
  activityMessage?: string | null;
  generationLive?: boolean;
  onTaskClick: (taskId: string) => void;
  onTasksChange?: (tasks: RelatedTaskSummary[]) => void;
  onClose?: () => void;
  messages?: ConversationMessage[];
  currentSession?: ConversationSession | null;
  projectName?: string | null;
  contextSnapshot?: ContextSnapshot | null;
  contextSnapshotStatus?: string;
  persistent?: boolean;
}) {
  const [activeTab, setActiveTab] = useState<RailTab>(generationLive ? "execution" : "context");
  // A persisted run id is useful history, but does not mean work is active.
  // Keep the badge tied to the page-owned generation state.
  const executionLive = Boolean(generationLive);
  const hasExecution = Boolean(generationLive || agentRunId);
  const [taskCount, setTaskCount] = useState(0);
  const handleTasksChange = useCallback(
    (tasks: RelatedTaskSummary[]) => {
      setTaskCount(tasks.length);
      onTasksChange?.(tasks);
    },
    [onTasksChange],
  );

  const tabs = useMemo(
    () => [
      { id: "context" as const, label: "コンテキスト", badge: taskCount > 0 ? taskCount : null },
      { id: "execution" as const, label: "実行", badge: executionLive ? "●" : null },
    ],
    [executionLive, taskCount],
  );
  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = tabs.findIndex((tab) => tab.id === activeTab);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    setActiveTab(tabs[next].id);
    document.getElementById(`chat-context-rail-tab-${tabs[next].id}`)?.focus();
  };

  const renderSection = (section: RelatedInformationSection) => (
    <RelatedInformationPanel
      sessionId={sessionId}
      agentRunId={agentRunId}
      onTaskClick={onTaskClick}
      onTasksChange={handleTasksChange}
      messages={messages}
      currentSession={currentSession}
      projectName={projectName}
      section={section}
      active={activeTab === section}
      scrollable={false}
      hideHeader
    />
  );

  return (
    <div
      className="flex min-h-0 flex-1 flex-col bg-surface-charcoal text-on-surface"
      data-testid="chat-context-rail"
      data-shell-workspace="chat"
      data-context-rail-persistent={persistent ? "true" : "false"}
    >
      <div className="flex h-10 shrink-0 items-center justify-between border-b border-border-subtle px-3">
        <span className="text-xs font-semibold">Context Rail</span>
        {onClose && !persistent && (
          <Button type="button" variant="ghost" size="icon-sm" aria-label="Context Railを閉じる" title="Context Railを閉じる" onClick={onClose}>
            <X className="size-3.5" />
          </Button>
        )}
      </div>

      <VoiceStatus enabled />

      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex shrink-0 border-b border-border-subtle px-2" role="tablist" aria-label="Context Rail表示切替">
          {tabs.map((tab) => {
            const selected = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                id={`chat-context-rail-tab-${tab.id}`}
                type="button"
                role="tab"
                aria-selected={selected}
                aria-controls={`chat-context-rail-panel-${tab.id}`}
                tabIndex={selected ? 0 : -1}
                onClick={() => setActiveTab(tab.id)}
                onKeyDown={handleTabKeyDown}
                className={`relative flex min-w-0 flex-1 items-center justify-center gap-1 px-2 py-2 text-[11px] font-medium transition-colors ${selected ? "text-primary" : "text-text-secondary hover:text-on-surface"}`}
              >
                {tab.id === "execution" ? <Activity className="size-3.5" aria-hidden="true" /> : <CheckCircle2 className="size-3.5" aria-hidden="true" />}
                <span className="truncate">{tab.label}</span>
                {tab.badge != null && <span className="rounded-full bg-primary/15 px-1.5 font-mono text-[9px] text-primary">{tab.badge}</span>}
                {selected && <span className="absolute inset-x-2 bottom-0 h-0.5 rounded-full bg-primary" />}
              </button>
            );
          })}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <div id="chat-context-rail-panel-context" role="tabpanel" aria-labelledby="chat-context-rail-tab-context" hidden={activeTab !== "context"} className="min-h-full">
            <ContextSnapshotSummary snapshot={contextSnapshot} status={contextSnapshotStatus} />
            {activeTab === "context" && renderSection("context")}
          </div>
          <div id="chat-context-rail-panel-execution" role="tabpanel" aria-labelledby="chat-context-rail-tab-execution" hidden={activeTab !== "execution"} className="min-h-full">
            <section className="border-b border-border-subtle px-3 py-3" data-testid="chat-generation-rail">
              <div className="mb-2 flex items-center justify-between gap-2 text-[11px]">
                <span className="flex items-center gap-1.5 font-semibold text-text-secondary"><Activity className="size-3.5 text-primary" aria-hidden="true" />実行ログ</span>
                <span className="inline-flex items-center gap-1 text-text-secondary"><span className={generationLive ? "size-1.5 animate-pulse rounded-full bg-primary" : "size-1.5 rounded-full bg-runtime-idle"} />{generationLive ? "実行中" : "待機"}</span>
              </div>
              {activeTab === "execution" && hasExecution ? (
                <AgentRunTimeline runId={agentRunId} live={generationLive} generationKey={generationKey} generationStartedAt={generationStartedAt} activityMessage={activityMessage} />
              ) : activeTab === "execution" ? (
                <p className="flex items-center gap-1.5 text-[11px] text-text-secondary"><CheckCircle2 className="size-3.5 text-primary" aria-hidden="true" />実行ログはありません</p>
              ) : null}
            </section>
            {activeTab === "execution" && renderSection("execution")}
          </div>
        </div>
      </div>
    </div>
  );
}

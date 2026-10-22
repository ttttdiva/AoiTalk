"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useId,
  type ReactNode,
  useRef,
  useState,
} from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Info,
  Clock3,
  Loader2,
  RefreshCw,
  Sparkles,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

import { ProjectOverviewGraph } from "./project-overview-graph";
import {
  normalizeProjectOverviewResponse,
  projectOverviewDisplayStatus,
  projectOverviewLayoutHasContent,
  type ProjectOverviewMemoryRef,
  type ProjectOverviewResponse,
  type ProjectOverviewSection,
  type ProjectOverviewStatus,
} from "./project-overview-model";

const POLL_INTERVAL_MS = 1500;
const MAX_POLL_ATTEMPTS = 20;

const STATUS_LABELS: Record<ProjectOverviewStatus, string> = {
  empty: "Empty",
  pending: "Pending",
  building: "Building",
  fresh: "Fresh",
  failed: "Failed",
};

const ERROR_LABELS: Record<string, string> = {
  overview_generation_timeout: "生成がタイムアウトしました。",
  overview_provider_error: "Overview生成モデルを利用できませんでした。",
  overview_invalid_json: "生成結果を解釈できませんでした。",
  overview_layout_validation_failed:
    "生成結果がOverview形式に適合しませんでした。",
  overview_layout_invalid: "保存済みOverview形式を安全に表示できません。",
  overview_response_invalid: "Overview応答を安全に表示できません。",
  overview_refresh_failed: "Overview更新に失敗しました。",
  overview_refresh_requeue_failed: "Overview更新の再予約に失敗しました。",
  overview_refresh_invalid_state: "Overview更新状態が不正です。",
  overview_route_invalid: "Overviewのモデルルート設定を確認してください。",
  missing_provider: "Overview用LLM providerが未設定です。",
  missing_model: "Overview用LLM modelが未設定です。",
  unsupported_provider: "Overview用LLM providerが未対応です。",
  client_creation_failed: "Overview生成モデルの初期化に失敗しました。",
};

const EMPHASIS_CLASS: Record<ProjectOverviewSection["emphasis"], string> = {
  normal: "border-border bg-card",
  primary: "border-primary/40 bg-primary/5",
  warning: "border-amber-500/40 bg-amber-500/5",
  critical: "border-destructive/40 bg-destructive/5",
};

function errorDetail(body: unknown, fallback: string): string {
  if (
    body &&
    typeof body === "object" &&
    "detail" in body &&
    typeof (body as { detail?: unknown }).detail === "string"
  ) {
    const detail = (body as { detail: string }).detail.trim();
    if (detail) return detail;
  }
  return fallback;
}

async function responseError(
  response: Response,
  fallback: string,
): Promise<string> {
  return errorDetail(await response.json().catch(() => null), fallback);
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "未生成";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("ja-JP");
}

function statusIcon(status: ProjectOverviewStatus) {
  switch (status) {
    case "fresh":
      return <CheckCircle2 className="size-4" />;
    case "failed":
      return <AlertCircle className="size-4" />;
    case "building":
    case "pending":
      return <Loader2 className="size-4 animate-spin" />;
    default:
      return <Clock3 className="size-4" />;
  }
}

function knownSafeError(value: string | null): string | null {
  if (!value) return null;
  return ERROR_LABELS[value] ?? "Overview生成に失敗しました。";
}

function isTransitionalStatus(status: ProjectOverviewStatus): boolean {
  return status === "pending" || status === "building";
}

function sameText(left: string | null, right: string): boolean {
  return (
    Boolean(left?.trim()) &&
    left!.replace(/\s+/g, " ").trim() === right.replace(/\s+/g, " ").trim()
  );
}

function MemoryDetails({ memory }: { memory: ProjectOverviewMemoryRef }) {
  return (
    <div className="min-w-0 space-y-2 text-xs">
      {!sameText(memory.title, memory.content) && memory.content ? (
        <p className="whitespace-pre-wrap leading-6 [overflow-wrap:anywhere]">
          {memory.content}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-muted-foreground">
        <span>{memory.memory_type}</span>
        {memory.is_pinned ? <span>固定済み</span> : null}
        {memory.updated_at ? (
          <span>メモリ更新 {formatTimestamp(memory.updated_at)}</span>
        ) : null}
      </div>
    </div>
  );
}

function MemoryRecord({ memory }: { memory: ProjectOverviewMemoryRef }) {
  return (
    <details className="group min-w-0 rounded-md border border-border/70 bg-background/60 open:bg-background">
      <summary className="flex min-w-0 cursor-pointer list-none items-start gap-2 px-3 py-2.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-start gap-2">
            <span className="min-w-0 flex-1 line-clamp-2 text-sm font-medium leading-5 [overflow-wrap:anywhere] group-open:line-clamp-none">
              {memory.title || "メモリの記録"}
            </span>
            <Badge
              variant="outline"
              className="max-w-24 shrink-0 truncate text-[10px]"
            >
              {memory.memory_type}
            </Badge>
          </div>
          {memory.content && !sameText(memory.title, memory.content) ? (
            <p className="mt-1 line-clamp-2 whitespace-pre-wrap text-xs leading-5 text-muted-foreground [overflow-wrap:anywhere] group-open:hidden">
              {memory.content}
            </p>
          ) : null}
        </div>
        <ChevronDown
          aria-hidden="true"
          className="mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180"
        />
      </summary>
      <div className="min-w-0 border-t border-border/60 px-3 py-2.5">
        <MemoryDetails memory={memory} />
      </div>
    </details>
  );
}

function OverviewSection({
  section,
  memoryRefs,
  id,
}: {
  section: ProjectOverviewSection;
  memoryRefs: Record<string, ProjectOverviewMemoryRef>;
  id: string;
}) {
  return (
    <section
      id={id}
      tabIndex={-1}
      className={cn(
        "min-w-0 scroll-mt-3 rounded-lg border p-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        EMPHASIS_CLASS[section.emphasis],
      )}
    >
      <div className="mb-2 flex min-w-0 items-start justify-between gap-2">
        <h3 className="min-w-0 text-sm font-semibold [overflow-wrap:anywhere]">
          {section.title}
        </h3>
        <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
          参照 {section.memory_ids.length}件
        </span>
      </div>
      {section.memory_ids.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          表示対象のProject Memoryはありません。
        </p>
      ) : (
        <div
          className={cn(
            "grid min-w-0 grid-cols-1 gap-2",
            section.columns === 2 && "md:grid-cols-2",
            section.kind === "timeline" && "border-l-2 border-border pl-3",
          )}
        >
          {section.memory_ids.map((memoryId) => {
            const memory = memoryRefs[memoryId];
            return memory ? (
              <MemoryRecord key={memoryId} memory={memory} />
            ) : (
              <p
                key={memoryId}
                className="min-w-0 rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground"
              >
                この参照メモリは現在activeではありません。
              </p>
            );
          })}
        </div>
      )}
    </section>
  );
}

export function ProjectOverviewPanel({
  projectId,
  canManageSettings,
  taskSummary,
  onOpenMemory,
}: {
  projectId: string;
  canManageSettings: boolean;
  taskSummary?: ReactNode;
  onOpenMemory?: () => void;
}) {
  const sectionPrefix = useId();
  const [overview, setOverview] = useState<ProjectOverviewResponse | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedMemoryIds, setSelectedMemoryIds] = useState<string[]>([]);

  const requestGenerationRef = useRef(0);
  const requestControllerRef = useRef<AbortController | null>(null);
  const refreshControllerRef = useRef<AbortController | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollGenerationRef = useRef(0);

  const clearPoll = useCallback(() => {
    pollGenerationRef.current += 1;
    if (pollTimerRef.current !== null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const load = useCallback(
    async ({
      quiet = false,
    }: {
      quiet?: boolean;
    } = {}): Promise<ProjectOverviewResponse | null> => {
      const generation = ++requestGenerationRef.current;
      requestControllerRef.current?.abort();
      const controller = new AbortController();
      requestControllerRef.current = controller;
      if (!quiet) setLoading(true);
      setError(null);

      try {
        const response = await fetch(
          `/api/projects/${encodeURIComponent(projectId)}/overview`,
          {
            credentials: "include",
            signal: controller.signal,
          },
        );
        if (!response.ok) {
          throw new Error(
            await responseError(
              response,
              "Project Overviewを取得できませんでした",
            ),
          );
        }
        const normalized = normalizeProjectOverviewResponse(
          await response.json(),
        );
        if (
          controller.signal.aborted ||
          generation !== requestGenerationRef.current
        ) {
          return null;
        }
        setOverview(normalized);
        if (!normalized.layoutValid) {
          setError("Overview形式を安全に表示できません。");
        }
        return normalized;
      } catch (cause) {
        if (
          controller.signal.aborted ||
          generation !== requestGenerationRef.current
        ) {
          return null;
        }
        setError(
          cause instanceof Error
            ? cause.message
            : "Project Overviewを取得できませんでした",
        );
        return null;
      } finally {
        if (
          !controller.signal.aborted &&
          generation === requestGenerationRef.current &&
          !quiet
        ) {
          setLoading(false);
        }
      }
    },
    [projectId],
  );

  const beginPolling = useCallback(() => {
    clearPoll();
    const generation = pollGenerationRef.current;
    let attempts = 0;

    const poll = () => {
      if (
        generation !== pollGenerationRef.current ||
        attempts >= MAX_POLL_ATTEMPTS
      ) {
        setRefreshing(false);
        return;
      }
      pollTimerRef.current = setTimeout(async () => {
        pollTimerRef.current = null;
        if (generation !== pollGenerationRef.current) return;
        attempts += 1;
        const next = await load({ quiet: true });
        if (
          next &&
          isTransitionalStatus(next.status) &&
          attempts < MAX_POLL_ATTEMPTS
        ) {
          poll();
          return;
        }
        setRefreshing(false);
      }, POLL_INTERVAL_MS);
    };

    poll();
  }, [clearPoll, load]);

  useEffect(() => {
    clearPoll();
    requestGenerationRef.current += 1;
    requestControllerRef.current?.abort();
    refreshControllerRef.current?.abort();
    setOverview(null);
    setError(null);
    setSelectedMemoryIds([]);
    setRefreshing(false);
    setLoading(true);

    void load().then((next) => {
      if (next && isTransitionalStatus(next.status)) {
        beginPolling();
      }
    });

    return () => {
      requestGenerationRef.current += 1;
      requestControllerRef.current?.abort();
      refreshControllerRef.current?.abort();
      clearPoll();
    };
  }, [beginPolling, clearPoll, load]);

  const refresh = useCallback(async () => {
    if (!canManageSettings || refreshing || refreshControllerRef.current)
      return;
    clearPoll();
    const controller = new AbortController();
    refreshControllerRef.current = controller;
    setRefreshing(true);
    setError(null);

    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/overview/refresh`,
        {
          method: "POST",
          credentials: "include",
          signal: controller.signal,
        },
      );
      if (controller.signal.aborted) return;
      if (!response.ok && response.status !== 202) {
        throw new Error(
          await responseError(
            response,
            "Project Overviewを更新できませんでした",
          ),
        );
      }
      setOverview((current) =>
        current ? { ...current, status: "pending" } : current,
      );
      const next = await load({ quiet: true });
      if (controller.signal.aborted) return;
      if (next && isTransitionalStatus(next.status)) {
        beginPolling();
      } else {
        setRefreshing(false);
      }
    } catch (cause) {
      if (controller.signal.aborted) return;
      setRefreshing(false);
      setError(
        cause instanceof Error
          ? cause.message
          : "Project Overviewを更新できませんでした",
      );
    } finally {
      if (refreshControllerRef.current === controller) {
        refreshControllerRef.current = null;
      }
    }
  }, [beginPolling, canManageSettings, clearPoll, load, projectId, refreshing]);

  const status = projectOverviewDisplayStatus(overview, refreshing);
  const selectedMemories = useMemo(
    () =>
      selectedMemoryIds
        .map((id) => overview?.memory_refs[id])
        .filter((item): item is ProjectOverviewMemoryRef => Boolean(item)),
    [overview?.memory_refs, selectedMemoryIds],
  );
  const hasContent = overview
    ? projectOverviewLayoutHasContent(overview.layout)
    : false;
  const safeGenerationError = knownSafeError(overview?.error_message ?? null);

  return (
    <Card
      className="min-w-0 gap-3 rounded-none border-0 bg-transparent py-0 shadow-none"
      data-testid="project-overview-panel"
    >
      <CardHeader className="min-w-0 rounded-lg border border-border bg-card px-3 py-2.5">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
            <CardTitle className="flex items-center gap-2 text-sm font-semibold">
              <Sparkles className="size-4" />
              メモリの記録
            </CardTitle>
            <Badge
              variant={status === "fresh" ? "secondary" : "outline"}
              data-testid="project-overview-status"
            >
              <span className="mr-1 inline-flex">{statusIcon(status)}</span>
              {STATUS_LABELS[status]}
            </Badge>
            <span
              className="text-xs text-muted-foreground"
              data-testid="project-overview-generated-at"
            >
              構成更新 {formatTimestamp(overview?.generated_at)}
            </span>
          </div>
          {canManageSettings ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={refreshing}
              onClick={() => void refresh()}
            >
              {refreshing || isTransitionalStatus(status) ? (
                <Loader2 className="mr-2 size-3.5 animate-spin" />
              ) : (
                <RefreshCw className="mr-2 size-3.5" />
              )}
              更新
            </Button>
          ) : null}
        </div>
        <details className="min-w-0 text-xs text-muted-foreground">
          <summary className="mt-1 flex w-fit cursor-pointer list-none items-center gap-1.5 rounded-sm py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <Info className="size-3.5" />
            生成情報
          </summary>
          <div className="mt-2 space-y-2 border-t border-border pt-2">
            <p>
              Active Project
              Memoryをもとにした表示構成です。記録中のタスク集計は、現在のタスク状態を示すものではありません。
            </p>
            <dl className="grid min-w-0 gap-2 sm:grid-cols-2">
              <div>
                <dt>構成生成日時</dt>
                <dd>{formatTimestamp(overview?.generated_at)}</dd>
              </div>
              <div>
                <dt>生成バージョン</dt>
                <dd>{overview?.generation_version ?? "—"}</dd>
              </div>
              <div className="min-w-0 sm:col-span-2">
                <dt>ソースダイジェスト</dt>
                <dd className="font-mono text-[11px] [overflow-wrap:anywhere]">
                  {overview?.source_digest || "—"}
                </dd>
              </div>
            </dl>
            {!canManageSettings ? (
              <p>更新にはProjectのmanage_settings権限が必要です。</p>
            ) : null}
          </div>
        </details>
      </CardHeader>

      <CardContent className="min-w-0 space-y-3 px-0">
        {error ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm [overflow-wrap:anywhere]"
          >
            {error}
          </div>
        ) : null}
        {status === "failed" && safeGenerationError ? (
          <div
            role="status"
            className="rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-sm"
          >
            {safeGenerationError}
            {hasContent ? (
              <span className="ml-1 text-muted-foreground">
                最後に成功したOverviewを表示しています。
              </span>
            ) : null}
          </div>
        ) : null}
        <div
          className={cn(
            "grid min-w-0 grid-cols-1 items-start gap-3",
            taskSummary && "xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]",
          )}
        >
          <div className="min-w-0 space-y-3">
            {loading && !overview ? (
              <div className="flex items-center gap-2 rounded-lg border bg-card p-4 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                Project Overviewを読み込み中…
              </div>
            ) : !overview ? null : !hasContent ? (
              <div className="rounded-lg border border-dashed border-border px-3 py-4">
                <p className="text-sm font-medium">Overviewはまだありません</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  Active Project Memoryが追加されると自動更新されます。
                </p>
              </div>
            ) : (
              <>
                {selectedMemories.length > 0 ? (
                  <section
                    aria-label="選択中のProject Memory"
                    className="min-w-0 rounded-lg border border-primary/40 bg-card p-3"
                  >
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <h3 className="text-sm font-semibold">根拠のメモリ</h3>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="h-7 px-2 text-xs"
                        onClick={() => setSelectedMemoryIds([])}
                      >
                        選択解除
                      </Button>
                    </div>
                    <div className="min-w-0 space-y-3">
                      {selectedMemories.map((memory) => (
                        <article key={memory.id} className="min-w-0">
                          <h4 className="mb-1 text-sm font-medium [overflow-wrap:anywhere]">
                            {memory.title || "メモリの記録"}
                          </h4>
                          <MemoryDetails memory={memory} />
                        </article>
                      ))}
                    </div>
                  </section>
                ) : null}
                {overview.layout.graph.nodes.length > 0 ? (
                  <div className="min-w-0 space-y-2">
                    <h3 className="text-sm font-semibold [overflow-wrap:anywhere]">
                      {overview.layout.graph.title || "メモリに基づく関係図"}
                    </h3>
                    <ProjectOverviewGraph
                      graph={overview.layout.graph}
                      compact
                      onSelectMemoryIds={setSelectedMemoryIds}
                    />
                  </div>
                ) : null}
                {overview.layout.sections.map((section, index) => (
                  <OverviewSection
                    key={
                      section.id || `${section.kind}-${index}-${section.title}`
                    }
                    id={`${sectionPrefix}-section-${index}`}
                    section={section}
                    memoryRefs={overview.memory_refs}
                  />
                ))}
              </>
            )}
            {hasContent ? (
              <p className="text-[11px] leading-5 text-muted-foreground">
                本文は現在activeな参照メモリを最大2,000文字まで表示します。元の全文はメモリタブで確認できます。
              </p>
            ) : null}
          </div>
          <aside className="order-first min-w-0 space-y-3 xl:order-last">
            {taskSummary}
            {overview && overview.layout.sections.length > 1 ? (
              <nav
                aria-label="参照セクション"
                className="min-w-0 rounded-lg border border-border bg-card p-3"
              >
                <h3 className="mb-2 text-sm font-semibold">参照セクション</h3>
                <ul className="divide-y divide-border">
                  {overview.layout.sections.map((section, index) => (
                    <li key={section.id || index}>
                      <button
                        type="button"
                        className="flex w-full min-w-0 items-center gap-2 rounded-sm py-2 text-left text-xs hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        onClick={() => {
                          const target = document.getElementById(
                            `${sectionPrefix}-section-${index}`,
                          );
                          target?.scrollIntoView({ block: "start" });
                          target?.focus({ preventScroll: true });
                        }}
                      >
                        <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
                          {section.title}
                        </span>
                        <ChevronRight className="size-3.5 shrink-0" />
                      </button>
                    </li>
                  ))}
                </ul>
              </nav>
            ) : null}
            {onOpenMemory ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="w-full"
                onClick={onOpenMemory}
              >
                元のメモリを開く
                <ChevronRight className="ml-1 size-3.5" />
              </Button>
            ) : null}
          </aside>
        </div>
      </CardContent>
    </Card>
  );
}

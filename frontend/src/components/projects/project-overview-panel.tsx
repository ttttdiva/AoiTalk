"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  Loader2,
  RefreshCw,
  Sparkles,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
  overview_layout_validation_failed: "生成結果がOverview形式に適合しませんでした。",
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

const EMPHASIS_CLASS: Record<
  ProjectOverviewSection["emphasis"],
  string
> = {
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
  return errorDetail(
    await response.json().catch(() => null),
    fallback,
  );
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "未生成";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : date.toLocaleString("ja-JP");
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

function isTransitionalStatus(
  status: ProjectOverviewStatus,
): boolean {
  return status === "pending" || status === "building";
}

function MemoryEvidenceButton({
  memory,
  selected,
  onSelect,
}: {
  memory: ProjectOverviewMemoryRef;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      className={cn(
        "w-full rounded-md border px-3 py-2 text-left transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        selected
          ? "border-primary bg-primary/10"
          : "border-border bg-background hover:border-primary/50",
      )}
      onClick={onSelect}
    >
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="min-w-0 truncate text-sm font-medium">
          {memory.title || memory.content || "Project Memory"}
        </span>
        <Badge variant="outline" className="shrink-0 text-[10px]">
          {memory.memory_type}
        </Badge>
      </div>
      {memory.content ? (
        <p className="mt-1 line-clamp-3 whitespace-pre-wrap break-words text-xs leading-5 text-muted-foreground">
          {memory.content}
        </p>
      ) : null}
    </button>
  );
}

function OverviewSection({
  section,
  memoryRefs,
  selectedMemoryIds,
  onSelectMemoryIds,
}: {
  section: ProjectOverviewSection;
  memoryRefs: Record<string, ProjectOverviewMemoryRef>;
  selectedMemoryIds: Set<string>;
  onSelectMemoryIds: (ids: string[]) => void;
}) {
  return (
    <section
      className={cn(
        "rounded-lg border",
        section.density === "compact" ? "p-3" : "p-4",
        EMPHASIS_CLASS[section.emphasis],
      )}
    >
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{section.title}</h3>
        <Badge variant="secondary" className="text-[10px]">
          {section.kind}
        </Badge>
      </div>
      {section.memory_ids.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          表示対象のProject Memoryはありません。
        </p>
      ) : (
        <div
          className={cn(
            "grid gap-2",
            section.columns === 2 && "md:grid-cols-2",
          )}
        >
          {section.memory_ids.map((memoryId) => {
            const memory = memoryRefs[memoryId];
            if (!memory) {
              return (
                <div
                  key={memoryId}
                  className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground"
                >
                  この参照メモリは現在activeではありません。
                </div>
              );
            }
            return (
              <MemoryEvidenceButton
                key={memoryId}
                memory={memory}
                selected={selectedMemoryIds.has(memoryId)}
                onSelect={() => onSelectMemoryIds([memoryId])}
              />
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
}: {
  projectId: string;
  canManageSettings: boolean;
}) {
  const [overview, setOverview] =
    useState<ProjectOverviewResponse | null>(null);
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
          return normalized;
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
    if (!canManageSettings || refreshing) return;
    clearPoll();
    refreshControllerRef.current?.abort();
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
      if (!response.ok && response.status !== 202) {
        throw new Error(
          await responseError(
            response,
            "Project Overviewを更新できませんでした",
          ),
        );
      }
      setOverview((current) =>
        current
          ? { ...current, status: "pending" }
          : current,
      );
      const next = await load({ quiet: true });
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
  }, [
    beginPolling,
    canManageSettings,
    clearPoll,
    load,
    projectId,
    refreshing,
  ]);

  const status = projectOverviewDisplayStatus(overview, refreshing);
  const selectedSet = useMemo(
    () => new Set(selectedMemoryIds),
    [selectedMemoryIds],
  );
  const selectedMemories = useMemo(
    () =>
      selectedMemoryIds
        .map((id) => overview?.memory_refs[id])
        .filter(
          (item): item is ProjectOverviewMemoryRef =>
            Boolean(item),
        ),
    [overview?.memory_refs, selectedMemoryIds],
  );
  const hasContent = overview
    ? projectOverviewLayoutHasContent(overview.layout)
    : false;
  const safeGenerationError = knownSafeError(
    overview?.error_message ?? null,
  );

  return (
    <Card
      className="border-border bg-card shadow-none"
      data-testid="project-overview-panel"
    >
      <CardHeader className="border-b border-border">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-base font-semibold">
              <Sparkles className="size-4" />
              Project Overview
            </CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              Active Project Memory から生成された固定レイアウトです。
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Badge
              variant={status === "fresh" ? "secondary" : "outline"}
              data-testid="project-overview-status"
            >
              <span className="mr-1 inline-flex">
                {statusIcon(status)}
              </span>
              {STATUS_LABELS[status]}
            </Badge>
            {canManageSettings ? (
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={refreshing}
                onClick={() => void refresh()}
              >
                {refreshing ? (
                  <Loader2 className="mr-2 size-4 animate-spin" />
                ) : (
                  <RefreshCw className="mr-2 size-4" />
                )}
                更新
              </Button>
            ) : null}
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-4 pt-5">
        <dl className="grid gap-3 text-xs text-muted-foreground sm:grid-cols-3">
          <div>
            <dt>generated_at</dt>
            <dd
              className="mt-1 text-sm text-foreground"
              data-testid="project-overview-generated-at"
            >
              {formatTimestamp(overview?.generated_at)}
            </dd>
          </div>
          <div>
            <dt>generation_version</dt>
            <dd className="mt-1 text-sm text-foreground">
              {overview?.generation_version ?? 1}
            </dd>
          </div>
          <div>
            <dt>source_digest</dt>
            <dd className="mt-1 truncate font-mono text-[11px] text-foreground">
              {overview?.source_digest || "—"}
            </dd>
          </div>
        </dl>

        {!canManageSettings ? (
          <p className="text-xs text-muted-foreground">
            更新にはProjectのmanage_settings権限が必要です。
          </p>
        ) : null}

        {error ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm"
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

        {loading && !overview ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Project Overviewを読み込み中…
          </div>
        ) : !overview ? null : !hasContent ? (
          <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center">
            <p className="text-sm font-medium">
              Overviewはまだありません
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              Active Project Memoryが追加されると自動更新されます。
            </p>
          </div>
        ) : (
          <>
            {overview.layout.sections.length > 0 ? (
              <div className="grid gap-3">
                {overview.layout.sections.map((section, index) => (
                  <OverviewSection
                    key={
                      section.id ||
                      `${section.kind}-${index}-${section.title}`
                    }
                    section={section}
                    memoryRefs={overview.memory_refs}
                    selectedMemoryIds={selectedSet}
                    onSelectMemoryIds={setSelectedMemoryIds}
                  />
                ))}
              </div>
            ) : null}

            {overview.layout.graph.title ? (
              <h3 className="text-sm font-semibold">
                {overview.layout.graph.title}
              </h3>
            ) : null}

            <ProjectOverviewGraph
              graph={overview.layout.graph}
              onSelectMemoryIds={setSelectedMemoryIds}
            />
          </>
        )}

        {selectedMemories.length > 0 ? (
          <section
            aria-label="選択中のProject Memory"
            className="rounded-lg border border-border bg-surface-container-low p-3"
          >
            <div className="mb-2 flex items-center justify-between gap-2">
              <h3 className="text-sm font-semibold">
                Evidence
              </h3>
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
            <div className="grid gap-2 md:grid-cols-2">
              {selectedMemories.map((memory) => (
                <MemoryEvidenceButton
                  key={memory.id}
                  memory={memory}
                  selected
                  onSelect={() =>
                    setSelectedMemoryIds([memory.id])
                  }
                />
              ))}
            </div>
          </section>
        ) : null}
      </CardContent>
    </Card>
  );
}

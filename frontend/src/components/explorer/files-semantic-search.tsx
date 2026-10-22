"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ExternalLink,
  FileAudio,
  FileIcon,
  FileImage,
  FileSpreadsheet,
  FileText,
  FileVideo,
  FolderOpen,
  Loader2,
  RefreshCw,
  Settings2,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import type { FilesSemanticSearchController } from "@/hooks/use-files-semantic-search";
import {
  filesExcludedReasonLabel,
  filesIndexApi,
  filesIndexCoverage,
  formatIndexTimestamp,
  parseLines,
  type FilesFileKind,
  type FilesIndexEntry,
  type FilesIndexOwnerRules,
  type FilesIndexSettings,
  type FilesIndexStatus,
  type FilesSemanticOutcome,
  type FilesSemanticResult,
  type FilesSemanticScope,
} from "@/lib/explorer/files-semantic-search";
import { cn } from "@/lib/utils";

const KIND_ICONS: Record<FilesFileKind, typeof FileIcon> = {
  pdf: FileText,
  audio: FileAudio,
  video: FileVideo,
  image: FileImage,
  office: FileSpreadsheet,
  text: FileText,
  other: FileIcon,
};

const KIND_LABELS: Record<FilesFileKind, string> = {
  pdf: "PDF",
  audio: "音声",
  video: "動画",
  image: "画像",
  office: "Office",
  text: "テキスト",
  other: "ファイル",
};

export function filesScopeLabel(scope: FilesSemanticScope): string {
  return scope.scope === "project" ? "Project Files" : "User Files";
}

// ─── 索引状況（パネル内のコンパクト表示） ───

export function FilesIndexStatusBar({
  status,
  error,
  loading,
  onOpenDetails,
  onRefresh,
}: {
  status: FilesIndexStatus | null;
  error: string | null;
  loading: boolean;
  onOpenDetails: () => void;
  onRefresh: () => void;
}) {
  const coverage = status ? filesIndexCoverage(status) : null;
  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
      data-testid="files-index-status"
      aria-live="polite"
    >
      {error ? (
        <span className="text-destructive">{error}</span>
      ) : !status || !coverage ? (
        <span>{loading ? "索引状況を確認中..." : "索引状況を取得していません"}</span>
      ) : (
        <>
          {!status.enabled && <span className="font-medium text-amber-500">索引は無効</span>}
          {!status.semantic_available && (
            <span className="font-medium text-amber-500">意味検索は準備中</span>
          )}
          <span>
            索引済み{" "}
            <span className="font-medium text-foreground">
              {coverage.percent != null ? `${coverage.percent}%` : "—"}
            </span>{" "}
            ({coverage.ready}/{coverage.indexable}件)
          </span>
          <span>待機 {coverage.waiting}</span>
          <span className={cn(coverage.failed > 0 && "text-destructive")}>失敗 {coverage.failed}</span>
          <span>除外 {coverage.excluded}</span>
          <span>最終更新 {formatIndexTimestamp(status.last_indexed_at ?? status.last_scan_at)}</span>
          {coverage.inProgress && (
            <span className="inline-flex items-center gap-1 text-primary">
              <Loader2 className="size-3 animate-spin" />
              {status.scanner_running ? "スキャン中" : "索引中"}
            </span>
          )}
        </>
      )}
      <span className="ml-auto inline-flex items-center gap-1">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          onClick={onRefresh}
          aria-label="索引状況を更新"
          title="索引状況を更新"
        >
          <RefreshCw className={cn("size-3", loading && "animate-spin")} />
        </Button>
        <Button type="button" variant="ghost" size="xs" onClick={onOpenDetails}>
          <Settings2 className="size-3" />
          詳細・設定
        </Button>
      </span>
    </div>
  );
}

// ─── 結果リスト ───

function ScoreBadge({ score }: { score: number }) {
  return (
    <span
      className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
      title="意味的な近さ（コサイン類似度）"
    >
      {score.toFixed(2)}
    </span>
  );
}

export function FilesSemanticResults({
  controller,
  scope,
  onOpenLocation,
  onSwitchToString,
}: {
  controller: FilesSemanticSearchController;
  scope: FilesSemanticScope;
  onOpenLocation: (result: FilesSemanticResult) => void;
  onSwitchToString: () => void;
}) {
  const { search, outcome } = controller;
  if (search.phase === "idle") {
    return (
      <p className="py-2 text-xs text-muted-foreground">
        自然な言葉で内容を説明すると、{filesScopeLabel(scope)}{" "}
        内の文書・PDF・音声・動画から意味の近い箇所を探します。Enterで検索します。
      </p>
    );
  }
  if (search.phase === "loading") {
    return (
      <p role="status" className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
        <Loader2 className="size-3.5 animate-spin" />
        内容を検索中...
      </p>
    );
  }
  if (search.phase === "error") {
    return (
      <div role="alert" className="flex flex-wrap items-center gap-2 py-2 text-xs text-destructive">
        {search.error}
        <Button type="button" size="xs" variant="outline" onClick={onSwitchToString}>
          文字列検索で探す
        </Button>
      </div>
    );
  }
  return (
    <div className="flex min-h-0 flex-col gap-1" data-testid="files-semantic-results">
      {outcome && <OutcomeNotice outcome={outcome} onSwitchToString={onSwitchToString} />}
      {search.results.length > 0 && (
        <ul className="max-h-[50vh] divide-y divide-border overflow-y-auto rounded border border-border bg-background">
          {search.results.map((result) => (
            <SemanticResultItem key={result.key} result={result} onOpenLocation={onOpenLocation} />
          ))}
        </ul>
      )}
    </div>
  );
}

function OutcomeNotice({
  outcome,
  onSwitchToString,
}: {
  outcome: FilesSemanticOutcome;
  onSwitchToString: () => void;
}) {
  if (outcome.kind === "results") {
    return (
      <p role="status" className="text-[11px] text-muted-foreground" data-outcome={outcome.kind}>
        <span className="font-medium text-foreground">{outcome.title}</span>
        {outcome.detail && <span className="ml-2">{outcome.detail}</span>}
      </p>
    );
  }
  const warning = outcome.kind !== "no_match";
  return (
    <div
      role="status"
      data-testid="files-semantic-outcome"
      data-outcome={outcome.kind}
      className={cn(
        "flex flex-col gap-1 rounded border px-3 py-2 text-xs",
        warning ? "border-amber-400/30 bg-amber-400/10" : "border-border bg-card/40",
      )}
    >
      <span className="flex items-center gap-1.5 font-medium text-foreground">
        {warning && <AlertTriangle className="size-3.5 text-amber-500" />}
        {outcome.title}
      </span>
      {outcome.detail && <span className="text-muted-foreground">{outcome.detail}</span>}
      <span>
        <Button
          type="button"
          size="xs"
          variant={outcome.suggestStringSearch ? "outline" : "ghost"}
          onClick={onSwitchToString}
        >
          名前・本文の文字列検索に切り替える
        </Button>
      </span>
    </div>
  );
}

function SemanticResultItem({
  result,
  onOpenLocation,
}: {
  result: FilesSemanticResult;
  onOpenLocation: (result: FilesSemanticResult) => void;
}) {
  const Icon = KIND_ICONS[result.kind];
  const [expanded, setExpanded] = useState(false);
  return (
    <li className="flex gap-2 px-2 py-2" data-files-semantic-result={result.filePath}>
      <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-label={KIND_LABELS[result.kind]} />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[13px] font-medium text-foreground" title={result.name}>
            {result.name}
          </span>
          {result.best.label && (
            <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
              {result.best.label}
            </span>
          )}
          <ScoreBadge score={result.best.score} />
        </div>
        <div className="truncate text-[11px] text-muted-foreground" title={result.folderLabel || "ルート"}>
          {result.folderLabel ? `/${result.folderLabel}` : "/（ルート）"}
        </div>
        {result.best.snippet && (
          <p className="mt-1 line-clamp-3 break-words text-xs text-foreground/90">{result.best.snippet}</p>
        )}
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <Button type="button" size="xs" variant="outline" onClick={() => onOpenLocation(result)}>
            <FolderOpen className="size-3" />
            Filesで場所を開く
          </Button>
          {result.best.originalUrl && (
            <a
              href={result.best.originalUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex h-6 items-center gap-1 rounded px-2 text-xs text-muted-foreground underline-offset-2 hover:bg-muted hover:text-foreground hover:underline"
            >
              <ExternalLink className="size-3" />
              原本を開く{result.best.label ? `（${result.best.label}）` : ""}
            </a>
          )}
          {result.morePositions.length > 0 && (
            <button
              type="button"
              className="h-6 rounded px-2 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
              aria-expanded={expanded}
              onClick={() => setExpanded((value) => !value)}
            >
              他の該当箇所 {result.morePositions.length}件
            </button>
          )}
        </div>
        {expanded && result.morePositions.length > 0 && (
          <ul className="mt-1 space-y-1 border-l border-border pl-2">
            {result.morePositions.map((position) => (
              <li key={position.key} className="text-xs">
                <span className="flex items-center gap-2">
                  {position.label && <span className="text-[10px] text-primary">{position.label}</span>}
                  <ScoreBadge score={position.score} />
                  {position.originalUrl && (
                    <a
                      href={position.originalUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-[11px] text-muted-foreground underline"
                    >
                      原本を開く
                    </a>
                  )}
                </span>
                {position.snippet && (
                  <span className="line-clamp-2 break-words text-muted-foreground">{position.snippet}</span>
                )}
              </li>
            ))}
          </ul>
        )}
        {result.otherLocations.length > 0 && (
          <p className="mt-1 truncate text-[10px] text-muted-foreground" title={result.otherLocations.join("、")}>
            同じ内容: {result.otherLocations.join("、")}
          </p>
        )}
      </div>
    </li>
  );
}

// ─── 詳細ダイアログ ───

type EntryFilter = "failed" | "excluded" | "unsupported";
const ENTRY_FILTER_LABELS: Record<EntryFilter, string> = {
  failed: "失敗",
  excluded: "除外",
  unsupported: "非対応",
};
const ENTRY_PAGE_SIZE = 50;

export function FilesIndexDialog({
  open,
  onOpenChange,
  scope,
  status,
  statusError,
  onRefreshStatus,
  onOpenPath,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  scope: FilesSemanticScope;
  status: FilesIndexStatus | null;
  statusError: string | null;
  onRefreshStatus: () => void;
  onOpenPath: (path: string) => void;
}) {
  const [tab, setTab] = useState("status");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="2xl" className="max-h-[85dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>内容検索の索引 — {filesScopeLabel(scope)}</DialogTitle>
          <DialogDescription>
            索引済みのファイルだけが内容（意味）検索の対象です。失敗・除外の理由確認、再スキャン、対象の設定ができます。
          </DialogDescription>
        </DialogHeader>
        <Tabs value={tab} onValueChange={(value) => setTab(String(value))}>
          <TabsList aria-label="索引の詳細">
            <TabsTrigger value="status">状況</TabsTrigger>
            <TabsTrigger value="entries">失敗・除外</TabsTrigger>
            <TabsTrigger value="settings">設定</TabsTrigger>
          </TabsList>
          <TabsContent value="status">
            <IndexStatusDetails
              scope={scope}
              status={status}
              error={statusError}
              onRefreshStatus={onRefreshStatus}
            />
          </TabsContent>
          <TabsContent value="entries">
            {open && tab === "entries" && (
              <IndexEntries
                scope={scope}
                onOpenPath={(path) => {
                  onOpenChange(false);
                  onOpenPath(path);
                }}
              />
            )}
          </TabsContent>
          <TabsContent value="settings">
            {open && tab === "settings" && (
              <IndexSettings key={scope.rootPath} scope={scope} onSaved={onRefreshStatus} />
            )}
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

function IndexStatusDetails({
  scope,
  status,
  error,
  onRefreshStatus,
}: {
  scope: FilesSemanticScope;
  status: FilesIndexStatus | null;
  error: string | null;
  onRefreshStatus: () => void;
}) {
  const [busy, setBusy] = useState<"rescan" | "retry" | null>(null);
  const coverage = status ? filesIndexCoverage(status) : null;

  const rescan = async (retryFailed: boolean) => {
    setBusy(retryFailed ? "retry" : "rescan");
    try {
      const result = await filesIndexApi.rescan(scope, retryFailed);
      toast.success(
        retryFailed
          ? `再スキャンを開始し、失敗した${result.retried}件を再試行します`
          : "再スキャンを開始しました",
      );
      onRefreshStatus();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "再スキャンを開始できませんでした");
    } finally {
      setBusy(null);
    }
  };

  const rows: [string, string | number][] = coverage && status
    ? [
        ["対象ファイル", coverage.total],
        ["索引できるファイル", coverage.indexable],
        ["索引済み", `${coverage.ready}${coverage.percent != null ? `（${coverage.percent}%）` : ""}`],
        ["待機・処理中", coverage.waiting],
        ["キュー待ち", status.queue_waiting],
        ["失敗", coverage.failed],
        ["除外・非対応", coverage.excluded],
        ["最終スキャン", formatIndexTimestamp(status.last_scan_at)],
        ["最終索引", formatIndexTimestamp(status.last_indexed_at)],
      ]
    : [];

  return (
    <div className="flex flex-col gap-3 pt-2 text-sm" data-testid="files-index-details">
      {error && <p className="text-xs text-destructive">{error}</p>}
      {status && (
        <div className="flex flex-wrap gap-2 text-xs">
          <span className={cn("rounded px-2 py-0.5", status.enabled ? "bg-primary/10 text-primary" : "bg-muted")}>
            索引 {status.enabled ? "有効" : "無効"}
          </span>
          <span
            className={cn(
              "rounded px-2 py-0.5",
              status.semantic_available ? "bg-primary/10 text-primary" : "bg-amber-400/10 text-amber-600",
            )}
          >
            意味検索 {status.semantic_available ? "利用可能" : "準備中（文字列検索のみ）"}
          </span>
          {status.scanner_running && (
            <span className="inline-flex items-center gap-1 rounded bg-muted px-2 py-0.5">
              <Loader2 className="size-3 animate-spin" />
              スキャン中
            </span>
          )}
        </div>
      )}
      {status?.scanner_error && (
        <p className="rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          スキャンエラー: {status.scanner_error}
        </p>
      )}
      {rows.length > 0 && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
          {rows.map(([label, value]) => (
            <div key={label} className="flex justify-between gap-2 border-b border-border/60 py-1">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="font-medium">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {status && Object.keys(status.excluded_reasons ?? {}).length > 0 && (
        <div className="text-xs">
          <p className="mb-1 text-muted-foreground">除外理由</p>
          <ul className="flex flex-wrap gap-1.5">
            {Object.entries(status.excluded_reasons ?? {}).map(([reason, count]) => (
              <li key={reason} className="rounded bg-muted px-2 py-0.5">
                {filesExcludedReasonLabel(reason)}: {count}
              </li>
            ))}
          </ul>
        </div>
      )}
      {status && (status.roots?.length ?? 0) > 1 && (
        <div className="text-xs">
          <p className="mb-1 text-muted-foreground">ルート別</p>
          <ul className="space-y-1">
            {(status.roots ?? []).map((root) => {
              const rootCoverage = filesIndexCoverage({ counts: root.counts, queue_waiting: 0, scanner_running: false });
              return (
                <li key={`${root.scope}:${root.project_id ?? ""}`} className="flex flex-wrap gap-2">
                  <span className="font-medium">
                    {root.scope === "project" ? root.project_name || "Project Files" : "User Files"}
                  </span>
                  <span>
                    {rootCoverage.ready}/{rootCoverage.indexable}件
                  </span>
                  {!root.enabled && <span className="text-amber-600">無効</span>}
                  {root.root_available === false && <span className="text-destructive">ルートを読めません</span>}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="outline" disabled={busy !== null} onClick={() => void rescan(false)}>
          <RefreshCw className={cn("size-3.5", busy === "rescan" && "animate-spin")} />
          再スキャン
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy !== null || !coverage || coverage.failed === 0}
          onClick={() => void rescan(true)}
        >
          失敗を再試行{coverage && coverage.failed > 0 ? `（${coverage.failed}件）` : ""}
        </Button>
      </div>
    </div>
  );
}

function IndexEntries({
  scope,
  onOpenPath,
}: {
  scope: FilesSemanticScope;
  onOpenPath: (path: string) => void;
}) {
  const [filter, setFilter] = useState<EntryFilter>("failed");
  const [state, setState] = useState<{
    key: string;
    entries: FilesIndexEntry[];
    total: number;
    loading: boolean;
    error: string | null;
  } | null>(null);
  const key = `${scope.scope}:${scope.projectId ?? ""}:${scope.rootPath}:${filter}`;
  const requestGenerationRef = useRef(0);

  const load = useCallback(
    async (offset: number, signal?: AbortSignal) => {
      const requestGeneration = ++requestGenerationRef.current;
      setState((prev) => ({
        key,
        entries: offset > 0 && prev?.key === key ? prev.entries : [],
        total: prev?.key === key ? prev.total : 0,
        loading: true,
        error: null,
      }));
      try {
        const result = await filesIndexApi.entries(scope, { states: [filter], offset, limit: ENTRY_PAGE_SIZE }, signal);
        if (signal?.aborted || requestGeneration !== requestGenerationRef.current) return;
        setState((prev) => ({
          key,
          entries: offset > 0 && prev?.key === key ? [...prev.entries, ...result.entries] : result.entries,
          total: result.total,
          loading: false,
          error: null,
        }));
      } catch (error) {
        if (signal?.aborted || requestGeneration !== requestGenerationRef.current) return;
        setState((prev) => ({
          key,
          entries: prev?.key === key ? prev.entries : [],
          total: prev?.key === key ? prev.total : 0,
          loading: false,
          error: error instanceof Error ? error.message : "一覧を取得できません",
        }));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- key は scope と filter から導出する
    [key],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(0, controller.signal);
    return () => {
      controller.abort();
      requestGenerationRef.current += 1;
    };
  }, [load]);

  const current = state?.key === key ? state : null;
  return (
    <div className="flex flex-col gap-2 pt-2" data-testid="files-index-entries">
      <div className="flex items-center gap-2">
        <AppSelect
          aria-label="表示する状態"
          size="sm"
          value={filter}
          onValueChange={(value) => setFilter(value as EntryFilter)}
        >
          {(Object.keys(ENTRY_FILTER_LABELS) as EntryFilter[]).map((value) => (
            <option key={value} value={value}>
              {ENTRY_FILTER_LABELS[value]}
            </option>
          ))}
        </AppSelect>
        <span className="text-xs text-muted-foreground">
          {current ? `${current.total}件` : ""}
        </span>
      </div>
      {current?.error && <p className="text-xs text-destructive">{current.error}</p>}
      {current && !current.loading && current.entries.length === 0 && !current.error && (
        <p className="py-4 text-center text-xs text-muted-foreground">
          {ENTRY_FILTER_LABELS[filter]}のファイルはありません
        </p>
      )}
      {current && current.entries.length > 0 && (
        <ul className="max-h-[45dvh] divide-y divide-border overflow-y-auto rounded border border-border">
          {current.entries.map((entry) => {
            const slash = entry.rel_path.lastIndexOf("/");
            return (
              <li key={entry.id} className="flex items-start gap-2 px-2 py-1.5 text-xs">
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium" title={entry.rel_path}>
                    {entry.name}
                  </p>
                  <p className="truncate text-[11px] text-muted-foreground">
                    {slash >= 0 ? `/${entry.rel_path.slice(0, slash)}` : "/（ルート）"}
                  </p>
                  <p className={cn("break-words", entry.index_state === "failed" ? "text-destructive" : "text-muted-foreground")}>
                    {entry.index_state === "failed"
                      ? entry.error || "索引に失敗しました"
                      : filesExcludedReasonLabel(entry.excluded_reason)}
                  </p>
                </div>
                <Button
                  type="button"
                  size="xs"
                  variant="ghost"
                  onClick={() => onOpenPath(entry.files_location.path)}
                >
                  場所を開く
                </Button>
              </li>
            );
          })}
        </ul>
      )}
      {current?.loading && (
        <p className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="size-3 animate-spin" />
          読み込み中...
        </p>
      )}
      {current && !current.loading && current.entries.length < current.total && (
        <Button type="button" size="xs" variant="outline" onClick={() => void load(current.entries.length)}>
          さらに表示
        </Button>
      )}
    </div>
  );
}

type TriState = "inherit" | "on" | "off";

function triState(value: boolean | null | undefined): TriState {
  return value == null ? "inherit" : value ? "on" : "off";
}

interface OwnerForm {
  enabled: TriState;
  includeRoots: string;
  excludePatterns: string;
  includePatterns: string;
}

function ownerForm(rules: FilesIndexOwnerRules | null | undefined): OwnerForm {
  return {
    enabled: triState(rules?.enabled),
    includeRoots: (rules?.include_roots ?? []).join("\n"),
    excludePatterns: (rules?.exclude_patterns ?? []).join("\n"),
    includePatterns: (rules?.include_patterns ?? []).join("\n"),
  };
}

/** 空欄は「既定に従う」（null）として送る。 */
export function ownerFormToRules(form: OwnerForm): Record<string, unknown> {
  const rules: Record<string, unknown> = {};
  if (form.enabled !== "inherit") rules.enabled = form.enabled === "on";
  const includeRoots = parseLines(form.includeRoots);
  const excludePatterns = parseLines(form.excludePatterns);
  const includePatterns = parseLines(form.includePatterns);
  if (includeRoots.length) rules.include_roots = includeRoots;
  if (excludePatterns.length) rules.exclude_patterns = excludePatterns;
  if (includePatterns.length) rules.include_patterns = includePatterns;
  return rules;
}

function IndexSettings({ scope, onSaved }: { scope: FilesSemanticScope; onSaved: () => void }) {
  const [settings, setSettings] = useState<FilesIndexSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [owner, setOwner] = useState<OwnerForm>(ownerForm(null));
  const [globalForm, setGlobalForm] = useState({
    enabled: true,
    user: true,
    project: true,
    includeRoots: "",
    excludePatterns: "",
  });

  const apply = useCallback(
    (value: FilesIndexSettings) => {
      setSettings(value);
      setOwner(ownerForm(scope.scope === "project" ? value.project_override : value.user_override));
      setGlobalForm({
        enabled: value.global_settings.enabled,
        user: value.global_settings.scopes.user ?? true,
        project: value.global_settings.scopes.project ?? true,
        includeRoots: value.global_settings.include_roots.join("\n"),
        excludePatterns: value.global_settings.exclude_patterns.join("\n"),
      });
    },
    [scope.scope],
  );

  useEffect(() => {
    const controller = new AbortController();
    filesIndexApi
      .settings(scope.projectId, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) apply(value);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "設定を取得できません");
      });
    return () => controller.abort();
  }, [apply, scope.projectId]);

  const canEditOwner = scope.scope === "user" || Boolean(settings?.can_edit_project);
  const canEditGlobal = Boolean(settings?.can_edit_global);
  const ownerTarget = scope.scope === "project" ? "project" : "user";

  const save = async (target: "global" | "user" | "project", body: Record<string, unknown>, label: string) => {
    setSaving(label);
    setError(null);
    try {
      const value = await filesIndexApi.updateSettings(target, body, scope.projectId);
      apply(value);
      toast.success("索引設定を保存しました。再スキャンを開始します");
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "設定を保存できませんでした");
    } finally {
      setSaving(null);
    }
  };

  const globalSummary = useMemo(() => {
    if (!settings) return null;
    const g = settings.global_settings;
    return [
      `索引: ${g.enabled ? "有効" : "無効"}`,
      `User Files: ${g.scopes.user ? "対象" : "対象外"}`,
      `Project Files: ${g.scopes.project ? "対象" : "対象外"}`,
    ].join(" / ");
  }, [settings]);

  if (!settings) {
    return (
      <div className="pt-2 text-xs text-muted-foreground" data-testid="files-index-settings">
        {error ? <span className="text-destructive">{error}</span> : "設定を読み込み中..."}
      </div>
    );
  }

  const ownerTitle = scope.scope === "project" ? "このプロジェクトの設定" : "自分の User Files の設定";
  return (
    <div className="flex flex-col gap-4 pt-2 text-xs" data-testid="files-index-settings">
      {error && <p className="text-destructive">{error}</p>}
      <section className="flex flex-col gap-2" aria-label={ownerTitle}>
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-medium">{ownerTitle}</h3>
          {!canEditOwner && (
            <span className="rounded bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
              編集権限がありません（プロジェクトの設定管理権限が必要）
            </span>
          )}
        </div>
        <p className="text-muted-foreground">空欄の項目は全体の既定に従います。</p>
        <label className="flex flex-col gap-1">
          <span>索引</span>
          <AppSelect
            aria-label={`${ownerTitle}: 索引の有効・無効`}
            size="sm"
            value={owner.enabled}
            disabled={!canEditOwner}
            onValueChange={(value) => setOwner((prev) => ({ ...prev, enabled: value as TriState }))}
          >
            <option value="inherit">既定に従う</option>
            <option value="on">有効</option>
            <option value="off">無効</option>
          </AppSelect>
        </label>
        <PatternField
          label="対象フォルダ（1行に1つ、空欄はルート全体）"
          placeholder={"例: 資料\n議事録/2026"}
          value={owner.includeRoots}
          disabled={!canEditOwner}
          onChange={(value) => setOwner((prev) => ({ ...prev, includeRoots: value }))}
        />
        <PatternField
          label="追加の除外パターン（例: node_modules/ ・ *.log ・ 下書き/*.tmp）"
          value={owner.excludePatterns}
          disabled={!canEditOwner}
          onChange={(value) => setOwner((prev) => ({ ...prev, excludePatterns: value }))}
        />
        <PatternField
          label="除外から戻すパターン"
          value={owner.includePatterns}
          disabled={!canEditOwner}
          onChange={(value) => setOwner((prev) => ({ ...prev, includePatterns: value }))}
        />
        {canEditOwner && (
          <div className="flex gap-2">
            <Button
              type="button"
              size="sm"
              disabled={saving !== null}
              onClick={() => void save(ownerTarget, ownerFormToRules(owner), "owner")}
            >
              {saving === "owner" ? "保存中..." : "保存"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={saving !== null}
              onClick={() => void save(ownerTarget, {}, "owner-clear")}
            >
              既定に戻す
            </Button>
          </div>
        )}
      </section>

      <section className="flex flex-col gap-2 border-t border-border pt-3" aria-label="全体の既定">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-medium">全体の既定</h3>
          {!canEditGlobal && (
            <span className="rounded bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
              管理者のみ編集できます
            </span>
          )}
        </div>
        {canEditGlobal ? (
          <>
            <div className="flex flex-wrap gap-3">
              {(
                [
                  ["enabled", "索引を有効にする"],
                  ["user", "User Files を対象"],
                  ["project", "Project Files を対象"],
                ] as const
              ).map(([field, label]) => (
                <label key={field} className="flex items-center gap-1.5">
                  <Checkbox
                    checked={globalForm[field]}
                    onCheckedChange={(checked) =>
                      setGlobalForm((prev) => ({ ...prev, [field]: checked === true }))
                    }
                  />
                  {label}
                </label>
              ))}
            </div>
            <PatternField
              label="対象フォルダ（全ルート共通、空欄はルート全体）"
              value={globalForm.includeRoots}
              onChange={(value) => setGlobalForm((prev) => ({ ...prev, includeRoots: value }))}
            />
            <PatternField
              label="除外パターン"
              value={globalForm.excludePatterns}
              onChange={(value) => setGlobalForm((prev) => ({ ...prev, excludePatterns: value }))}
            />
            <div>
              <Button
                type="button"
                size="sm"
                disabled={saving !== null}
                onClick={() =>
                  void save(
                    "global",
                    {
                      enabled: globalForm.enabled,
                      scopes: { user: globalForm.user, project: globalForm.project },
                      include_roots: parseLines(globalForm.includeRoots),
                      exclude_patterns: parseLines(globalForm.excludePatterns),
                    },
                    "global",
                  )
                }
              >
                {saving === "global" ? "保存中..." : "全体の既定を保存"}
              </Button>
            </div>
          </>
        ) : (
          <div className="flex flex-col gap-1 text-muted-foreground">
            <p>{globalSummary}</p>
            <p>
              対象フォルダ: {settings.global_settings.include_roots.length
                ? settings.global_settings.include_roots.join("、")
                : "ルート全体"}
            </p>
            <p className="break-words">
              除外パターン: {settings.global_settings.exclude_patterns.join("、") || "なし"}
            </p>
          </div>
        )}
      </section>
    </div>
  );
}

function PatternField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span>{label}</span>
      <Textarea
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-14 font-mono text-xs md:text-xs"
        rows={3}
      />
    </label>
  );
}

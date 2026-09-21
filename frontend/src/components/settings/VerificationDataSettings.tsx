"use client";

import { useCallback, useMemo, useState } from "react";
import useSWR from "swr";
import { CheckCircle2, Loader2, RefreshCw, ShieldAlert, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";

export const VERIFICATION_CLEANUP_CONFIRMATION = "DELETE VERIFIED TEST DATA" as const;
const LEGACY_SELECTOR_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const AGGREGATE_COUNT_KEYS = new Set(["total", "entities", "artifacts", "runs"]);

export type VerificationSelectorType = "legacy_manifest" | "verification_run";

/**
 * A selector is deliberately an opaque provenance pointer.  The UI never
 * derives eligibility from names, timestamps, status, or row counts; only the
 * backend's explicit category A marker is allowed to reach the cleanup form.
 */
export type VerificationSelector = {
  type: VerificationSelectorType;
  id: string;
  category?: string | null;
  classification?: string | null;
  disposition?: string | null;
  label?: string | null;
  source?: string | null;
  harness?: string | null;
  workstream?: string | null;
  created_at?: string | null;
  status?: string | null;
  reason?: string | null;
  entity_count?: number | null;
  count?: number | null;
  counts?: Record<string, number> | null;
};

export type VerificationPreview = {
  preview_digest: string;
  selectors?: VerificationSelector[] | null;
  /** Some backend revisions expose the already-filtered A list separately. */
  a_selectors?: VerificationSelector[] | null;
  counts?: Record<string, number> | null;
  total_artifacts?: number | null;
  generated_at?: string | null;
  expires_at?: string | null;
};

type CleanupResult = {
  status?: string;
  removed_count?: number;
  already_removed_count?: number;
  failed_count?: number;
  counts?: Record<string, number> | null;
  deleted?: unknown[] | null;
  message?: string;
  [key: string]: unknown;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function selectorType(value: unknown): value is VerificationSelectorType {
  return value === "legacy_manifest" || value === "verification_run";
}

function normalizeSelector(value: unknown): VerificationSelector | null {
  if (!isRecord(value) || !selectorType(value.type) || typeof value.id !== "string") {
    return null;
  }
  const id = value.id.trim();
  if (!id || id.length > 256) return null;
  if (value.type === "legacy_manifest" && !LEGACY_SELECTOR_RE.test(id)) return null;
  const result: VerificationSelector = {
    type: value.type,
    id,
  };
  for (const field of [
    "category",
    "classification",
    "disposition",
    "label",
    "source",
    "harness",
    "workstream",
    "created_at",
    "status",
    "reason",
  ] as const) {
    const fieldValue = value[field];
    if (typeof fieldValue === "string") result[field] = fieldValue;
    else if (fieldValue === null) result[field] = null;
  }
  for (const field of ["entity_count", "count"] as const) {
    const fieldValue = value[field];
    if (typeof fieldValue === "number" && Number.isFinite(fieldValue) && fieldValue >= 0) {
      result[field] = Math.floor(fieldValue);
    }
  }
  if (isRecord(value.counts)) {
    const counts: Record<string, number> = {};
    for (const [key, raw] of Object.entries(value.counts)) {
      if (typeof raw === "number" && Number.isFinite(raw) && raw >= 0) {
        counts[key] = Math.floor(raw);
      }
    }
    result.counts = counts;
  }
  return result;
}

function normalizePreview(value: unknown): VerificationPreview | null {
  if (!isRecord(value) || typeof value.preview_digest !== "string") return null;
  const digest = value.preview_digest.trim();
  if (!digest) return null;
  const runSelectors: unknown[] = Array.isArray(value.runs)
    ? value.runs
        .map((run) => {
          if (!isRecord(run)) return null;
          const rawId = run.run_id ?? run.id;
          return typeof rawId === "string"
            ? { ...run, type: "verification_run", id: rawId }
            : null;
        })
        .filter((run) => run !== null)
    : [];
  // Keep the complete selector projection for digest-bound cleanup while
  // accepting older responses that put the A subset in a separate field (or
  // expose only run rows).  De-duplicate by opaque type/id identity so a
  // selector repeated in both projections cannot render twice.
  const rawSelectors = [
    ...(Array.isArray(value.selectors) ? value.selectors : []),
    ...(Array.isArray(value.a_selectors) ? value.a_selectors : []),
    ...runSelectors,
  ];
  const selectorMap = new Map<string, VerificationSelector>();
  for (const rawSelector of rawSelectors) {
    const selector = normalizeSelector(rawSelector);
    if (!selector) continue;
    const key = selectorKey(selector);
    const existing = selectorMap.get(key);
    if (!existing || (!isCategoryA(existing) && isCategoryA(selector))) {
      selectorMap.set(key, selector);
    }
  }
  const selectors = [...selectorMap.values()];
  const counts = isRecord(value.counts)
    ? Object.fromEntries(
        Object.entries(value.counts).filter(
          ([, raw]) => typeof raw === "number" && Number.isFinite(raw) && raw >= 0,
        ).map(([key, raw]) => [key, Math.floor(raw as number)]),
      )
    : null;
  return {
    preview_digest: digest,
    selectors,
    a_selectors: Array.isArray(value.a_selectors)
      ? value.a_selectors
          .map(normalizeSelector)
          .filter((selector): selector is VerificationSelector => selector !== null)
      : undefined,
    counts,
    total_artifacts:
      typeof value.total_artifacts === "number" && Number.isFinite(value.total_artifacts) && value.total_artifacts >= 0
        ? Math.floor(value.total_artifacts)
        : null,
    generated_at: typeof value.generated_at === "string" ? value.generated_at : null,
    expires_at: typeof value.expires_at === "string" ? value.expires_at : null,
  };
}

function isCategoryA(selector: VerificationSelector): boolean {
  // Do not infer A from a selector's shape.  Require one explicit
  // classification marker from the backend's manifest response.  The API
  // normalises markers to uppercase, but accepting surrounding whitespace and
  // lowercase here keeps the read-only UI compatible with older projections.
  return [selector.category, selector.classification, selector.disposition].some(
    (marker) => typeof marker === "string" && marker.trim().toUpperCase() === "A",
  );
}

function selectorKey(selector: Pick<VerificationSelector, "type" | "id">): string {
  return `${selector.type}:${selector.id}`;
}

function selectorLabel(selector: VerificationSelector): string {
  return selector.label?.trim() || `${selector.type} / ${selector.id}`;
}

function selectorCount(selector: VerificationSelector): number | null {
  if (typeof selector.entity_count === "number") return selector.entity_count;
  if (typeof selector.count === "number") return selector.count;
  const total = selector.counts?.total ?? selector.counts?.entities;
  return typeof total === "number" ? total : null;
}

function formatTimestamp(value: string | null | undefined): string | null {
  if (!value?.trim()) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function selectorCountEntries(selector: VerificationSelector): Array<[string, number]> {
  if (!selector.counts) return [];
  return Object.entries(selector.counts)
    .filter(([kind]) => !AGGREGATE_COUNT_KEYS.has(kind.toLowerCase()))
    .filter(([, count]) => Number.isFinite(count) && count >= 0)
    .map(([kind, count]) => [kind, Math.floor(count)] as [string, number])
    .sort(([left], [right]) => left.localeCompare(right));
}

async function readJson<T>(response: Response): Promise<T | null> {
  return response.json().catch(() => null) as Promise<T | null>;
}

function errorMessage(value: unknown, fallback: string): string {
  if (isRecord(value) && typeof value.detail === "string" && value.detail.trim()) {
    return value.detail;
  }
  if (isRecord(value) && typeof value.message === "string" && value.message.trim()) {
    return value.message;
  }
  return fallback;
}

export function VerificationDataSettings() {
  const [expanded, setExpanded] = useState(false);
  const fetchPreview = useCallback(async (): Promise<VerificationPreview> => {
    const response = await fetch("/api/admin/verification-data", {
      credentials: "include",
      cache: "no-store",
    });
    const body = await readJson<unknown>(response);
    if (!response.ok) {
      throw new Error(errorMessage(body, `プレビュー取得に失敗しました（${response.status}）`));
    }
    const nextPreview = normalizePreview(body);
    if (!nextPreview) throw new Error("プレビュー応答の形式が不正です。");
    return nextPreview;
  }, []);
  const {
    data: preview = null,
    error: previewError,
    isLoading,
    mutate: mutatePreview,
  } = useSWR<VerificationPreview>(
    expanded ? "settings/verification-data-preview" : null,
    fetchPreview,
    {
      revalidateOnMount: true,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      dedupingInterval: 0,
    },
  );
  const [selectedKeysOverride, setSelectedKeysOverride] = useState<Set<string> | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [cleaning, setCleaning] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "success" | "error"; message: string } | null>(null);

  const loadPreview = useCallback(async (options?: { clearFeedback?: boolean }) => {
    if (options?.clearFeedback !== false) setFeedback(null);
    setSelectedKeysOverride(null);
    setConfirmation("");
    await mutatePreview().catch(() => undefined);
  }, [mutatePreview]);

  const aSelectors = useMemo(
    () => (preview?.selectors ?? []).filter(isCategoryA),
    [preview],
  );
  const selectedKeys = useMemo(
    () => selectedKeysOverride ?? new Set(aSelectors.map(selectorKey)),
    [aSelectors, selectedKeysOverride],
  );
  const selectedSelectors = useMemo(
    () => aSelectors.filter((selector) => selectedKeys.has(selectorKey(selector))),
    [aSelectors, selectedKeys],
  );
  const aEntityCounts = useMemo(() => {
    const totals = new Map<string, number>();
    for (const selector of aSelectors) {
      const entries = selectorCountEntries(selector);
      if (entries.length > 0) {
        for (const [kind, count] of entries) {
          totals.set(kind, (totals.get(kind) ?? 0) + count);
        }
        continue;
      }
      const count = selectorCount(selector);
      if (count !== null) {
        totals.set("entities", (totals.get("entities") ?? 0) + count);
      }
    }
    // A few older projections provide one aggregate count map instead of
    // per-selector counts.  Use it only when every returned selector is A;
    // otherwise a top-level aggregate could accidentally include hidden B/C
    // records.
    const allSelectors = preview?.selectors ?? [];
    if (totals.size === 0 && aSelectors.length > 0 && allSelectors.length === aSelectors.length) {
      const aggregate = Object.entries(preview?.counts ?? {})
        .filter(([kind, count]) => !AGGREGATE_COUNT_KEYS.has(kind.toLowerCase()) && Number.isFinite(count) && count >= 0)
        .map(([kind, count]) => [kind, Math.floor(count)] as [string, number]);
      if (aggregate.length > 0) {
        for (const [kind, count] of aggregate) totals.set(kind, count);
      } else {
        const artifactCount = preview?.counts?.artifacts ?? preview?.total_artifacts;
        if (typeof artifactCount === "number" && Number.isFinite(artifactCount) && artifactCount >= 0) {
          totals.set("entities", Math.floor(artifactCount));
        }
      }
    }
    return [...totals.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [aSelectors, preview]);
  const aEntityTotal = useMemo(
    () => aEntityCounts.reduce((total, [, count]) => total + count, 0),
    [aEntityCounts],
  );
  const allSelected = aSelectors.length > 0 && selectedSelectors.length === aSelectors.length;
  const confirmationMatches = confirmation === VERIFICATION_CLEANUP_CONFIRMATION;

  const toggleSelector = (selector: VerificationSelector, checked: boolean) => {
    const key = selectorKey(selector);
    setSelectedKeysOverride((previous) => {
      const effective = previous ?? new Set(aSelectors.map(selectorKey));
      const next = new Set(effective);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
  };

  const toggleAll = (checked: boolean) => {
    setSelectedKeysOverride(checked ? new Set(aSelectors.map(selectorKey)) : new Set());
  };

  const cleanup = async () => {
    if (!preview || selectedSelectors.length === 0 || !confirmationMatches) return;
    setCleaning(true);
    setFeedback(null);
    try {
      const response = await fetch("/api/admin/verification-data", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          selectors: selectedSelectors.map(({ type, id }) => ({ type, id })),
          preview_digest: preview.preview_digest,
          confirmation: VERIFICATION_CLEANUP_CONFIRMATION,
        }),
      });
      const body = await readJson<CleanupResult>(response);
      if (!response.ok) throw new Error(errorMessage(body, `削除に失敗しました（${response.status}）`));
      const removed = typeof body?.removed_count === "number" ? body.removed_count : null;
      const alreadyRemoved = typeof body?.already_removed_count === "number" ? body.already_removed_count : null;
      const deletedRuns = Array.isArray(body?.deleted) ? body.deleted.length : null;
      const reportedRuns = typeof body?.counts?.runs === "number" ? body.counts.runs : null;
      const suffixParts = [
        removed !== null || alreadyRemoved !== null
          ? `削除済み ${removed ?? 0}件、既削除 ${alreadyRemoved ?? 0}件`
          : null,
        deletedRuns !== null
          ? `処理 run ${deletedRuns}件`
          : reportedRuns !== null
            ? `処理 run ${reportedRuns}件`
            : null,
      ].filter((part): part is string => part !== null);
      const suffix = suffixParts.length > 0 ? `（${suffixParts.join("、")}）` : "";
      setFeedback({ kind: "success", message: `検証データの削除処理が完了しました。${suffix}` });
      setSelectedKeysOverride(null);
      await loadPreview({ clearFeedback: false });
    } catch (cause) {
      setFeedback({
        kind: "error",
        message: cause instanceof Error ? cause.message : "削除に失敗しました。プレビューを再取得してください。",
      });
    } finally {
      setCleaning(false);
    }
  };

  return (
    <SettingsDisclosure
      title="Verification / Test Data"
      icon={<ShieldAlert className="size-4" />}
      id="verification-data-card"
      targetId="verification-data"
      onOpenChange={setExpanded}
      summary={<Badge variant="outline">admin only</Badge>}
    >
      <div className="space-y-3" data-verification-data-settings>
        <p className="text-xs text-muted-foreground">
          明示的な provenance があり、A 判定になった検証データだけを対象にします。B/C 判定や判定不能のデータは表示・操作しません。
        </p>

        {isLoading && !preview ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
            <Loader2 className="size-4 animate-spin" />
            プレビューを取得しています…
          </div>
        ) : previewError ? (
          <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive" role="alert">
            <p>{previewError instanceof Error ? previewError.message : "プレビュー取得に失敗しました。"}</p>
            <Button type="button" variant="outline" size="sm" onClick={() => void loadPreview()} disabled={isLoading}>
              <RefreshCw className="size-3.5" /> 再試行
            </Button>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/20 px-3 py-2 text-xs">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span>
                  A 判定の削除対象（検証 run） <strong>{aSelectors.length}</strong> 件
                </span>
                <span>
                  A 判定のエンティティ <strong>{aEntityTotal}</strong> 件
                </span>
                {aEntityCounts.length > 0 ? (
                  <span className="text-muted-foreground">
                    （{aEntityCounts.map(([kind, count]) => `${kind}: ${count}`).join(" / ")}）
                  </span>
                ) : null}
              </div>
              <Button type="button" variant="outline" size="sm" onClick={() => void loadPreview()} disabled={isLoading || cleaning}>
                <RefreshCw className={isLoading ? "size-3.5 animate-spin" : "size-3.5"} />
                プレビューを更新
              </Button>
            </div>

            {preview ? (
              <p className="break-all text-[11px] text-muted-foreground">
                Preview digest: <code data-testid="verification-preview-digest">{preview.preview_digest}</code>
              </p>
            ) : null}

            {aSelectors.length === 0 ? (
              <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
                現在、明示的に検証済みと判定された削除対象（A）はありません。
              </p>
            ) : (
              <div className="space-y-2" aria-label="A判定の検証データ一覧">
                <label className="flex items-center gap-2 rounded-md border bg-background px-3 py-2 text-xs font-medium">
                  <Checkbox checked={allSelected} onCheckedChange={(value) => toggleAll(value === true)} disabled={cleaning} />
                  A 判定をすべて選択
                </label>
                {aSelectors.map((selector) => {
                  const key = selectorKey(selector);
                  const count = selectorCount(selector);
                  return (
                    <label key={key} className="flex items-start gap-2 rounded-md border bg-background px-3 py-2">
                      <Checkbox
                        checked={selectedKeys.has(key)}
                        onCheckedChange={(value) => toggleSelector(selector, value === true)}
                        disabled={cleaning}
                        aria-label={`${selectorLabel(selector)}を選択`}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
                          <span className="truncate">{selectorLabel(selector)}</span>
                          <Badge variant="destructive" className="text-[10px]">A</Badge>
                          <Badge variant="outline" className="text-[10px]">{selector.type}</Badge>
                        </span>
                        <span className="mt-0.5 block break-all text-[11px] text-muted-foreground">{selector.id}</span>
                        <span className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
                          <span>判定根拠: explicit A provenance</span>
                          {selector.source ? <span>source: {selector.source}</span> : null}
                          {selector.harness ? <span>harness: {selector.harness}</span> : null}
                          {selector.workstream ? <span>workstream: {selector.workstream}</span> : null}
                          {selector.reason ? <span>reason: {selector.reason}</span> : null}
                          {selector.status ? <span>status: {selector.status}</span> : null}
                          {formatTimestamp(selector.created_at) ? <span>created: {formatTimestamp(selector.created_at)}</span> : null}
                        </span>
                        {selectorCountEntries(selector).length > 0 ? (
                          <span className="mt-0.5 block text-[11px] text-muted-foreground">
                            対象内訳: {selectorCountEntries(selector).map(([kind, value]) => `${kind} ${value}`).join(" / ")}
                          </span>
                        ) : count !== null ? (
                          <span className="text-[11px] text-muted-foreground">対象エンティティ: {count}</span>
                        ) : null}
                      </span>
                    </label>
                  );
                })}
              </div>
            )}

            <Card size="sm" className="border-destructive/40 bg-destructive/5">
              <CardContent className="space-y-2 p-3">
                <div className="flex items-start gap-2 text-xs text-destructive">
                  <Trash2 className="mt-0.5 size-4 shrink-0" />
                  <p>削除は不可逆です。プレビューの digest をサーバー側で再検証し、A 判定の provenance と一致する場合だけ実行されます。</p>
                </div>
                <div className="space-y-1">
                  <Label htmlFor="verification-cleanup-confirmation" className="text-xs font-medium">
                    続行するには <code>{VERIFICATION_CLEANUP_CONFIRMATION}</code> と入力
                  </Label>
                  <Input
                    id="verification-cleanup-confirmation"
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                    placeholder={VERIFICATION_CLEANUP_CONFIRMATION}
                    autoComplete="off"
                    disabled={cleaning}
                    aria-describedby="verification-cleanup-help"
                  />
                  <p id="verification-cleanup-help" className="text-[11px] text-muted-foreground">
                    B/C 判定・判定不能データはこの操作から除外されます。
                  </p>
                </div>
                <Button
                  type="button"
                  variant="destructive"
                  size="sm"
                  onClick={() => void cleanup()}
                  disabled={cleaning || selectedSelectors.length === 0 || !confirmationMatches}
                >
                  {cleaning ? <Loader2 className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />}
                  {cleaning ? "削除中…" : "選択した検証データを削除"}
                </Button>
              </CardContent>
            </Card>

            {feedback ? (
              <p className={feedback.kind === "error" ? "text-sm text-destructive" : "flex items-center gap-1 text-sm text-green-600 dark:text-green-400"} role={feedback.kind === "error" ? "alert" : "status"}>
                {feedback.kind === "success" ? <CheckCircle2 className="size-4" /> : null}
                {feedback.message}
              </p>
            ) : null}
          </>
        )}
      </div>
    </SettingsDisclosure>
  );
}

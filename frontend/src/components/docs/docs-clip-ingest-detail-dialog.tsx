"use client";

import type { DialogRoot } from "@base-ui/react/dialog";
import { AlertTriangle, CheckCircle2, CircleHelp, ExternalLink, Loader2 } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  CLIP_INGEST_ACTION_LABELS,
  type DocsClipIngestResult,
} from "./docs-clip-ingest-dialog";
import type { DocsClipIngestJob } from "./docs-clip-ingest-panel";

/**
 * ClipIngest receipt list DTO projected into the UI.
 *
 * The server is the source of truth.  The normalizer intentionally accepts
 * the snake_case DTO as well as the camelCase form used by a few test and
 * compatibility adapters, but it never reconstructs a receipt from a Docs
 * node body or from the local history cache.
 */
export type DocsClipIngestReceiptSummary = {
  id: string;
  /** Canonical list DTO field. */
  topic_node_id: string | null;
  action: DocsClipIngestResult["action"] | null;
  open_node_id: string | null;
  open_node_title: string | null;
  node_id: string | null;
  node_title: string | null;
  created_at: string | null;
  source_preview: string;
  title: string | null;
  source_type: string | null;
};

export type DocsClipIngestReceiptProvenance = {
  id: string;
  kind: string | null;
  label: string;
  url: string | null;
  detail: string | null;
};

export type DocsClipIngestReceiptDetail = DocsClipIngestReceiptSummary & {
  /** Canonical detail DTO field; this is never rebuilt from local history. */
  source_text: string;
  source_sha256: string | null;
  /** Compatibility spelling retained for the pre-receipt detail renderer. */
  original_source: string;
  provenance: DocsClipIngestReceiptProvenance[];
  result: DocsClipIngestResult | null;
  target_id: string | null;
  target_label: string | null;
  status: string | null;
  error: string | null;
};

type RecordLike = Record<string, unknown>;

function asRecord(value: unknown): RecordLike | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as RecordLike
    : null;
}

function firstString(record: RecordLike, keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

/** Read a text field without trimming its content. */
function firstRawString(record: RecordLike, keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string") return value;
  }
  return null;
}

function nullableString(record: RecordLike, keys: string[]): string | null {
  const value = firstString(record, keys);
  return value || null;
}

function sourcePreview(source: string): string {
  const compact = source.replace(/\s+/g, " ").trim();
  return compact.length > 180 ? `${compact.slice(0, 177)}…` : compact;
}

function unwrapReceipt(value: unknown): RecordLike | null {
  const record = asRecord(value);
  if (!record) return null;
  const nested = asRecord(record.receipt ?? record.detail ?? record.item ?? record.data);
  return nested ?? record;
}

function normalizeResult(value: unknown): DocsClipIngestResult | null {
  const record = asRecord(value);
  if (!record) return null;
  const action = record.action;
  const targetId = firstString(record, ["target_id", "targetId"]);
  const targetLabel = firstString(record, ["target_label", "targetLabel"]);
  const openNodeId = firstString(record, ["open_node_id", "openNodeId"]);
  const openNodeTitle = firstString(record, ["open_node_title", "openNodeTitle"]);
  const changedNodeId = firstString(record, ["changed_node_id", "changedNodeId"]);
  const changedNodeTitle = firstString(record, ["changed_node_title", "changedNodeTitle"]);
  if (
    !targetId
    || !targetLabel
    || (action !== "create" && action !== "append" && action !== "duplicate_skip")
    || !openNodeId
    || !openNodeTitle
  ) return null;
  const stringArray = (raw: unknown): string[] => Array.isArray(raw)
    ? raw.filter((item): item is string => typeof item === "string")
    : [];
  const failedUrlsRaw = record.failed_urls ?? record.failedUrls;
  const failedUrls = Array.isArray(failedUrlsRaw)
    ? failedUrlsRaw.flatMap((item): Array<{ url?: string; error?: string; acquisition_status?: string }> => {
        const failed = asRecord(item);
        if (!failed) return [];
        return [{
          ...(typeof failed.url === "string" ? { url: failed.url } : {}),
          ...(typeof failed.error === "string" ? { error: failed.error } : {}),
          ...(typeof (failed.acquisition_status ?? failed.acquisitionStatus) === "string"
            ? { acquisition_status: (failed.acquisition_status ?? failed.acquisitionStatus) as string }
            : {}),
        }];
      })
    : [];
  return {
    target_id: targetId,
    target_label: targetLabel,
    action,
    changed_node_id: changedNodeId,
    changed_node_title: changedNodeTitle,
    open_node_id: openNodeId,
    open_node_title: openNodeTitle,
    direct_urls: stringArray(record.direct_urls ?? record.directUrls),
    supplemental_urls: stringArray(record.supplemental_urls ?? record.supplementalUrls),
    failed_urls: failedUrls,
    used_urls: stringArray(record.used_urls ?? record.usedUrls),
    unconfirmed: stringArray(record.unconfirmed),
  };
}

function normalizeSource(value: RecordLike): string {
  return firstRawString(value, [
    "source_text",
    "sourceText",
    "original_source",
    "originalSource",
    "original_text",
    "originalText",
    "source",
    "input",
    "raw_source",
    "rawSource",
    "verbatim_source",
    "verbatimSource",
  ]) ?? "";
}

function normalizeProvenance(value: RecordLike): DocsClipIngestReceiptProvenance[] {
  const raw = value.provenance
    ?? value.provenance_items
    ?? value.source_refs
    ?? value.source_references
    ?? value.sources
    ?? value.refs;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item, index): DocsClipIngestReceiptProvenance[] => {
    if (typeof item === "string" && item.trim()) {
      return [{ id: `provenance-${index}`, kind: null, label: item.trim(), url: /^https?:\/\//i.test(item.trim()) ? item.trim() : null, detail: null }];
    }
    const record = asRecord(item);
    if (!record) return [];
    const url = firstString(record, ["url", "source_url", "href", "final_url"]);
    const label = firstString(record, ["label", "title", "name", "url", "source_url", "ref"]) ?? "出典";
    const detail = firstString(record, ["detail", "description", "snippet", "text", "note", "error"]);
    return [{
      id: firstString(record, ["id", "ref_id", "source_id"]) ?? `provenance-${index}`,
      kind: nullableString(record, ["kind", "type", "source_type"]),
      label,
      url,
      detail,
    }];
  });
}

function summaryFromRecord(value: unknown): DocsClipIngestReceiptSummary | null {
  const record = unwrapReceipt(value);
  if (!record) return null;
  const id = firstString(record, ["id", "receipt_id", "receiptId"]);
  if (!id) return null;
  const source = normalizeSource(record);
  const topicNodeId = nullableString(record, [
    "topic_node_id",
    "topicNodeId",
    "topic_id",
    "topicId",
    "node_id",
    "nodeId",
    "target_node_id",
  ]);
  const openNodeId = nullableString(record, ["open_node_id", "openNodeId"]);
  const openNodeTitle = nullableString(record, ["open_node_title", "openNodeTitle"]);
  const nodeTitle = nullableString(record, [
    "node_title",
    "nodeTitle",
    "topic_title",
    "topicTitle",
  ]) ?? openNodeTitle;
  const action = record.action === "create"
    || record.action === "append"
    || record.action === "duplicate_skip"
    ? record.action
    : null;
  return {
    id,
    topic_node_id: topicNodeId,
    action,
    open_node_id: openNodeId,
    open_node_title: openNodeTitle ?? nodeTitle,
    // Keep the old aliases populated for callers that still consume them.
    node_id: topicNodeId,
    node_title: nodeTitle,
    created_at: nullableString(record, ["created_at", "createdAt", "captured_at", "capturedAt", "timestamp"]),
    source_preview: firstString(record, ["source_preview", "sourcePreview", "preview", "summary"]) ?? sourcePreview(source),
    title: nullableString(record, ["title", "receipt_title", "receiptTitle", "topic_title", "topicTitle"]),
    source_type: nullableString(record, ["source_type", "sourceType", "kind"]),
  };
}

function compareReceiptSummaries(
  left: DocsClipIngestReceiptSummary,
  right: DocsClipIngestReceiptSummary,
): number {
  const leftTime = left.created_at ? Date.parse(left.created_at) : Number.NaN;
  const rightTime = right.created_at ? Date.parse(right.created_at) : Number.NaN;
  if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) {
    return rightTime - leftTime;
  }
  if (Number.isFinite(leftTime) !== Number.isFinite(rightTime)) {
    return Number.isFinite(rightTime) ? 1 : -1;
  }
  return right.id.localeCompare(left.id);
}

/** Return a newest-first copy for the compact receipt picker. */
export function sortDocsClipIngestReceiptSummaries(
  receipts: DocsClipIngestReceiptSummary[],
): DocsClipIngestReceiptSummary[] {
  return receipts.slice().sort(compareReceiptSummaries);
}

/** Normalize one item from the server receipt-list DTO. */
export function normalizeDocsClipIngestReceiptSummary(value: unknown): DocsClipIngestReceiptSummary | null {
  return summaryFromRecord(value);
}

/** Normalize a list response without assuming the exact envelope key. */
export function normalizeDocsClipIngestReceiptList(value: unknown): DocsClipIngestReceiptSummary[] {
  let summaries: DocsClipIngestReceiptSummary[] = [];
  if (Array.isArray(value)) {
    summaries = value.flatMap((item) => {
      const summary = summaryFromRecord(item);
      return summary ? [summary] : [];
    });
    return sortDocsClipIngestReceiptSummaries(summaries);
  }
  const record = asRecord(value);
  if (!record) return [];
  const raw = record.receipts
    ?? record.items
    ?? record.receipt_summaries
    ?? record.receiptSummaries
    ?? record.data;
  summaries = Array.isArray(raw)
    ? raw.flatMap((item) => {
        const summary = summaryFromRecord(item);
        return summary ? [summary] : [];
      })
    : [];
  return sortDocsClipIngestReceiptSummaries(summaries);
}

/** Normalize the server receipt-detail DTO. */
export function normalizeDocsClipIngestReceiptDetail(value: unknown): DocsClipIngestReceiptDetail | null {
  const record = unwrapReceipt(value);
  const summary = summaryFromRecord(record);
  if (!record || !summary) return null;
  const source = normalizeSource(record);
  const result = normalizeResult(record.result ?? record.ingest_result ?? record.ingestResult);
  const sourceSha256 = nullableString(record, [
    "source_sha256",
    "sourceSha256",
    "source_hash",
    "sourceHash",
    "original_sha256",
    "originalSha256",
    "sha256",
  ]);
  const recordOpenNodeId = nullableString(record, ["open_node_id", "openNodeId"]);
  const recordOpenNodeTitle = nullableString(record, ["open_node_title", "openNodeTitle"]);
  return {
    ...summary,
    action: summary.action ?? result?.action ?? null,
    open_node_id: recordOpenNodeId ?? result?.open_node_id ?? summary.open_node_id,
    open_node_title: recordOpenNodeTitle ?? result?.open_node_title ?? summary.open_node_title,
    source_text: source,
    source_sha256: sourceSha256,
    original_source: source,
    provenance: normalizeProvenance(record),
    result,
    target_id: nullableString(record, ["target_id", "targetId"]) ?? result?.target_id ?? null,
    target_label: nullableString(record, ["target_label", "targetLabel"]) ?? result?.target_label ?? null,
    status: nullableString(record, ["status"]),
    error: nullableString(record, ["error", "error_message", "errorMessage"]),
  };
}

export const DOCS_CLIP_INGEST_RECEIPT_LIST_PATH = "/api/docs/nodes";
export const DOCS_CLIP_INGEST_RECEIPT_DETAIL_PATH = "/api/docs/clip-ingest-receipts";
/** @deprecated Use the explicit list/detail path constants instead. */
export const DOCS_CLIP_INGEST_RECEIPTS_PATH = DOCS_CLIP_INGEST_RECEIPT_DETAIL_PATH;

export function docsClipIngestReceiptListPath(nodeId: string): string {
  return `${DOCS_CLIP_INGEST_RECEIPT_LIST_PATH}/${encodeURIComponent(nodeId)}/clip-ingest-receipts`;
}

export function docsClipIngestReceiptDetailPath(receiptId: string): string {
  return `${DOCS_CLIP_INGEST_RECEIPT_DETAIL_PATH}/${encodeURIComponent(receiptId)}`;
}

function legacyDialogTitle(job: DocsClipIngestJob): string {
  if (job.status === "success") return "取り込みが完了しました";
  if (job.status === "failure") return "取り込みに失敗しました";
  if (job.status === "interrupted") return "取り込み結果が不明です";
  return "取り込みを再試行しています";
}

function statusIcon(status: DocsClipIngestJob["status"]) {
  if (status === "success") return <CheckCircle2 className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />;
  if (status === "failure") return <AlertTriangle className="size-4 shrink-0 text-destructive" />;
  if (status === "interrupted") return <CircleHelp className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />;
  return <Loader2 className="size-4 shrink-0 animate-spin" />;
}

export type DocsClipIngestDetailDialogProps = {
  open: boolean;
  legacyJob?: DocsClipIngestJob | null;
  receipt?: DocsClipIngestReceiptDetail | null;
  receiptLoading?: boolean;
  receiptError?: string | null;
  receiptPicker?: DocsClipIngestReceiptSummary[] | null;
  onSelectReceipt?: (receipt: DocsClipIngestReceiptSummary) => void;
  onOpenChange: (open: boolean) => void;
  onRetry?: (jobId: string) => void;
  onOpenNode: (nodeId: string) => void | Promise<void>;
};

/**
 * One Dialog used by both the history Sheet and the document provenance
 * field.  A picker is deliberately rendered in this component as well, so a
 * receipt selected from a topic and a receipt opened from history share the
 * same focus, close, and detail rendering rules.
 */
export function DocsClipIngestDetailDialog({
  open,
  legacyJob = null,
  receipt = null,
  receiptLoading = false,
  receiptError = null,
  receiptPicker = null,
  onSelectReceipt,
  onOpenChange,
  onRetry,
  onOpenNode,
}: DocsClipIngestDetailDialogProps) {
  const pickerOpen = Boolean(receiptPicker && receiptPicker.length > 1 && !legacyJob && !receipt && !receiptLoading && !receiptError);
  const title = legacyJob
    ? legacyDialogTitle(legacyJob)
    : pickerOpen
      ? "取り込み記録を選択"
      : "取り込み記録の詳細";
  const result = receipt ? receipt.result : legacyJob?.result ?? null;
  const openNodeId = receipt?.open_node_id ?? result?.open_node_id ?? null;
  const openNodeTitle = receipt?.open_node_title ?? result?.open_node_title ?? null;
  const originalSource = receipt
    ? receipt.source_text
    : legacyJob
      ? legacyJob.source || ((legacyJob.upload_ids?.length ?? 0) > 0 ? `${legacyJob.upload_ids?.length}件のファイル` : "")
      : "";

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen, eventDetails?: DialogRoot.ChangeEventDetails) => {
        // The history Sheet and this Dialog use separate portals.  A focus
        // transition between them is not a user close action.
        if (!nextOpen && eventDetails?.reason === "focus-out") {
          eventDetails.cancel?.();
          return;
        }
        onOpenChange(nextOpen);
      }}
    >
      <DialogContent showCloseButton className="max-h-[min(90vh,48rem)] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>

        {pickerOpen ? (
          <div className="space-y-2" aria-label="取り込み記録一覧">
            <p className="text-sm text-muted-foreground">表示する記録を選択してください。</p>
            {receiptPicker?.map((item) => (
              <button
                key={item.id}
                type="button"
                className="flex w-full min-w-0 items-start gap-3 rounded-lg border bg-background p-3 text-left transition-colors hover:bg-accent"
                onClick={() => onSelectReceipt?.(item)}
              >
                <span className="mt-0.5 text-primary"><CheckCircle2 className="size-4" /></span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">
                    {item.title || item.open_node_title || item.node_title || item.source_preview || "ClipIngest取り込み"}
                  </span>
                  {item.source_preview ? (
                    <span className="mt-1 block whitespace-pre-wrap break-words text-xs text-muted-foreground">{item.source_preview}</span>
                  ) : null}
                  {item.created_at ? (
                    <span className="mt-1 block text-[11px] text-muted-foreground">{new Date(item.created_at).toLocaleString("ja-JP")}</span>
                  ) : null}
                </span>
              </button>
            ))}
          </div>
        ) : null}

        {receiptLoading ? (
          <div role="status" aria-live="polite" className="flex items-center gap-2 rounded-lg border bg-muted/30 p-3 text-sm">
            <Loader2 className="size-4 animate-spin" />
            <span>取り込み記録を読み込み中…</span>
          </div>
        ) : null}

        {receiptError ? (
          <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
            <div className="whitespace-pre-wrap">{receiptError}</div>
          </div>
        ) : null}

        {legacyJob?.status === "queued" || legacyJob?.status === "running" ? (
          <div role="status" aria-live="polite" className="flex items-center gap-2 rounded-lg border border-blue-500/40 bg-blue-500/10 p-3 text-sm">
            {statusIcon(legacyJob.status)}
            <span>再試行中です。画面を閉じても処理は継続し、完了後に履歴から結果を確認できます。</span>
          </div>
        ) : null}

        {legacyJob?.status === "failure" ? (
          <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
            <div className="whitespace-pre-wrap">{legacyJob.error}</div>
          </div>
        ) : null}

        {legacyJob?.status === "interrupted" ? (
          <div role="status" className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
            <div className="flex items-start gap-2 text-amber-900 dark:text-amber-100">
              <CircleHelp className="mt-0.5 size-4 shrink-0" />
              <div>
                <div className="whitespace-pre-wrap">{legacyJob.error}</div>
                <div className="mt-1 text-xs text-muted-foreground">取り込みが完了したかどうかは不明です。Docsで結果を確認してから、必要に応じて再試行してください。</div>
              </div>
            </div>
          </div>
        ) : null}

        {receipt && !receiptLoading && !receiptError ? (
          <div className="space-y-3 text-sm">
            <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
              {receipt.target_label ? <><dt className="text-muted-foreground">保存先</dt><dd>{receipt.target_label}</dd></> : null}
              {receipt.result ? <><dt className="text-muted-foreground">処理</dt><dd>{CLIP_INGEST_ACTION_LABELS[receipt.result.action]}</dd></> : null}
              {openNodeTitle ? <><dt className="text-muted-foreground">ノード</dt><dd>{openNodeTitle}</dd></> : null}
              {receipt.created_at ? <><dt className="text-muted-foreground">取り込み日時</dt><dd>{new Date(receipt.created_at).toLocaleString("ja-JP")}</dd></> : null}
              {receipt.source_sha256 ? <><dt className="text-muted-foreground">原文SHA-256</dt><dd className="break-all font-mono text-xs">{receipt.source_sha256}</dd></> : null}
            </dl>
            {receipt.provenance.length > 0 ? (
              <div>
                <div className="text-xs font-medium text-muted-foreground">保存根拠</div>
                <ul className="mt-1 space-y-1.5 text-xs">
                  {receipt.provenance.map((item) => (
                    <li key={item.id} className="rounded border bg-muted/20 p-2">
                      {item.url ? (
                        <a className="inline-flex max-w-full items-center gap-1 break-all text-primary underline underline-offset-2" href={item.url} target="_blank" rel="noreferrer">
                          <span>{item.label}</span><ExternalLink className="size-3 shrink-0" />
                        </a>
                      ) : <span className="whitespace-pre-wrap break-words">{item.label}</span>}
                      {item.kind ? <span className="ml-1 text-muted-foreground">({item.kind})</span> : null}
                      {item.detail ? <div className="mt-1 whitespace-pre-wrap break-words text-muted-foreground">{item.detail}</div> : null}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {receipt.result?.failed_urls.length ? (
              <div role="status" className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs">
                <div className="font-medium">元URLの直接取得をWeb検索で補完しました</div>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-muted-foreground">
                  {receipt.result.failed_urls.map((item, index) => <li key={`${item.url ?? "unknown"}-${index}`} className="break-all">{item.url || "URL不明"}{item.error ? `: ${item.error}` : ""}</li>)}
                </ul>
              </div>
            ) : null}
            {receipt.result?.unconfirmed.length ? (
              <div>
                <div className="text-xs font-medium text-muted-foreground">未確認事項</div>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-xs">{receipt.result.unconfirmed.map((item) => <li key={item}>{item}</li>)}</ul>
              </div>
            ) : null}
          </div>
        ) : null}

        {!receipt && legacyJob?.status === "success" && legacyJob.result ? (
          <div className="space-y-3 text-sm">
            <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
              <dt className="text-muted-foreground">保存先</dt><dd>{legacyJob.result.target_label}</dd>
              <dt className="text-muted-foreground">処理</dt><dd>{CLIP_INGEST_ACTION_LABELS[legacyJob.result.action]}</dd>
              <dt className="text-muted-foreground">ノード</dt><dd>{legacyJob.result.open_node_title}</dd>
            </dl>
            {legacyJob.result.failed_urls.length > 0 ? (
              <div role="status" className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs">
                <div className="font-medium">元URLの直接取得をWeb検索で補完しました</div>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-muted-foreground">{legacyJob.result.failed_urls.map((item, index) => <li key={`${item.url ?? "unknown"}-${index}`} className="break-all">{item.url || "URL不明"}{item.error ? `: ${item.error}` : ""}</li>)}</ul>
              </div>
            ) : null}
            {legacyJob.result.used_urls.length > 0 ? (
              <div><div className="text-xs font-medium text-muted-foreground">保存根拠URL</div><ul className="mt-1 list-disc space-y-1 pl-5 text-xs">{legacyJob.result.used_urls.map((url) => <li key={url} className="break-all">{url}</li>)}</ul></div>
            ) : null}
            {legacyJob.result.unconfirmed.length > 0 ? (
              <div><div className="text-xs font-medium text-muted-foreground">未確認事項</div><ul className="mt-1 list-disc space-y-1 pl-5 text-xs">{legacyJob.result.unconfirmed.map((item) => <li key={item}>{item}</li>)}</ul></div>
            ) : null}
          </div>
        ) : null}

        {legacyJob || receipt ? (
          <div className="rounded-lg border bg-muted/40 p-3 text-xs whitespace-pre-wrap break-words text-muted-foreground">
            {originalSource}
            {legacyJob?.skip_image_recognition ? "\n画像認識を実施しない" : ""}
          </div>
        ) : null}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>閉じる</Button>
          {legacyJob && (legacyJob.status === "failure" || legacyJob.status === "interrupted" || legacyJob.status === "queued" || legacyJob.status === "running") && legacyJob.retryable !== false && onRetry ? (
            <Button type="button" disabled={legacyJob.status === "queued" || legacyJob.status === "running"} onClick={() => onRetry(legacyJob.id)}>
              {legacyJob.status === "failure" || legacyJob.status === "interrupted" ? "再試行" : "再試行中…"}
            </Button>
          ) : null}
          {openNodeId ? (
            <Button type="button" onClick={() => { onOpenChange(false); void onOpenNode(openNodeId); }}>Docsで開く</Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

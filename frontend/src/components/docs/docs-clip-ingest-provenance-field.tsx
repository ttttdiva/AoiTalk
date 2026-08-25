"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./docs-utils";
import {
  docsClipIngestReceiptListPath,
  normalizeDocsClipIngestReceiptList,
  type DocsClipIngestReceiptSummary,
} from "./docs-clip-ingest-detail-dialog";
import { useDocsClipIngest } from "./docs-clip-ingest-provider";

function statusOf(value: unknown): number | null {
  if (!value || typeof value !== "object") return null;
  const status = (value as { status?: unknown }).status;
  return typeof status === "number" ? status : null;
}

function messageFor(value: unknown): string {
  if (statusOf(value) === 404) {
    return "このDocsノードの取り込み記録を表示する権限がありません。ノードの共有範囲と現在のアカウントを確認してください。";
  }
  return value instanceof Error
    ? value.message
    : "取り込み記録を読み込めませんでした。時間をおいて再試行してください。";
}

/**
 * The receipt field is intentionally an optional system field.  A successful
 * empty list (and the request while it is pending) renders no row at all;
 * this avoids presenting a label or an empty value for every ordinary Docs
 * node.  Only a server-confirmed receipt count can make the row visible.
 */
export function DocsClipIngestProvenanceField({ nodeId }: { nodeId: string }) {
  const { openReceipt, openReceiptPicker } = useDocsClipIngest();
  const [receipts, setReceipts] = useState<DocsClipIngestReceiptSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadedNodeId, setLoadedNodeId] = useState<string | null>(null);
  const requestGenerationRef = useRef(0);

  useEffect(() => {
    const generation = requestGenerationRef.current + 1;
    requestGenerationRef.current = generation;
    const normalizedNodeId = nodeId.trim();
    if (!normalizedNodeId) {
      // This reset invalidates the prior node before the next render.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setReceipts([]);
      setError(null);
      setLoadedNodeId(null);
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    // Invalidate the previous node before starting this request.  The render
    // guard below therefore cannot flash the previous node's receipt count.
    setReceipts([]);
    setError(null);
    setLoadedNodeId(null);
    setLoading(true);

    void apiFetch<unknown>(docsClipIngestReceiptListPath(normalizedNodeId), {
      signal: controller.signal,
    })
      .then((response) => {
        if (controller.signal.aborted || requestGenerationRef.current !== generation) return;
        setReceipts(normalizeDocsClipIngestReceiptList(response));
        setError(null);
        setLoadedNodeId(normalizedNodeId);
      })
      .catch((requestError: unknown) => {
        if (controller.signal.aborted || requestGenerationRef.current !== generation) return;
        setReceipts([]);
        setError(messageFor(requestError));
        setLoadedNodeId(normalizedNodeId);
      })
      .finally(() => {
        if (controller.signal.aborted || requestGenerationRef.current !== generation) return;
        setLoading(false);
      });

    return () => {
      controller.abort();
    };
  }, [nodeId]);

  const normalizedNodeId = nodeId.trim();
  // Do not render loading or stale data as a field.  A node with zero receipts
  // must have no system-field label, placeholder, or empty-state text.
  if (!normalizedNodeId || loadedNodeId !== normalizedNodeId || loading) return null;

  if (error) {
    return (
      <div
        data-testid="docs-clip-ingest-provenance-field"
        className="grid max-w-2xl grid-cols-[minmax(8rem,11rem)_minmax(12rem,32rem)] items-start gap-2 text-xs"
      >
        <span className="truncate px-1 py-2 text-muted-foreground">&gt;クリップ取り込み</span>
        <div role="alert" className="whitespace-pre-wrap break-words px-1 py-1.5 text-destructive">
          {error}
        </div>
      </div>
    );
  }

  if (receipts.length === 0) return null;

  const countLabel = `クリップ取り込み: ${receipts.length}件`;
  return (
    <div
      data-testid="docs-clip-ingest-provenance-field"
      className="grid max-w-2xl grid-cols-[minmax(8rem,11rem)_minmax(12rem,32rem)] items-start gap-2 text-xs"
    >
      <span className="truncate px-1 py-2 text-muted-foreground">&gt;クリップ取り込み</span>
      <button
        type="button"
        data-docs-field-control
        className="flex h-7 min-w-0 w-full items-center rounded-md border-0 bg-transparent px-1 text-left text-sm shadow-none outline-none hover:bg-accent/40 focus-visible:ring-1 focus-visible:ring-ring/50"
        onClick={() => {
          if (receipts.length === 1) openReceipt(receipts[0]);
          else openReceiptPicker(receipts);
        }}
        aria-label={countLabel}
      >
        {countLabel}
      </button>
    </div>
  );
}

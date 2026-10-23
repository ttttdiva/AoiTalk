"use client";

import type { ReceiptSource, ReceiptSourcePreviewOut } from "@/components/personal-finance/personal-finance-api";
import { maskByteSize, maskDigest, maskReference } from "./finance-receipt-dto";

export type FinanceReceiptSourceProps = Readonly<{
  source: ReceiptSource;
  preview: ReceiptSourcePreviewOut | null;
  previewError: string | null;
}>;

export function FinanceReceiptSource({ source, preview, previewError }: FinanceReceiptSourceProps) {
  return (
    <section aria-label="選択した資料" data-testid="receipt-source" className="rounded-md border p-3 text-sm">
      <h3 className="font-medium">資料</h3>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt>資料ID</dt><dd data-testid="source-ref">{maskReference(source.source_ref)}</dd>
        <dt>種別</dt><dd>{source.mime_type}</dd>
        <dt>サイズ</dt><dd>{maskByteSize(source.byte_size)}</dd>
        <dt>照合値</dt><dd data-testid="source-digest">{maskDigest(source.source_sha256)}</dd>
      </dl>
      {previewError && <p role="alert" className="mt-2 text-rose-700">{previewError}</p>}
      {preview && (
        <div className="mt-3">
          <p className="text-xs text-muted-foreground">サーバーで抽出した根拠文面（リンク・HTMLとして扱いません）</p>
          <pre data-testid="source-preview-text" className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-2 text-xs">
            {preview.text}
          </pre>
          <ul className="mt-2 space-y-1 text-xs" data-testid="source-evidence">
            {preview.evidence.map((item) => (
              <li key={`${item.span_start}:${item.span_end}`}>
                根拠 {maskReference(item.source_ref)}・{item.page === null ? "ページ指定なし" : `${item.page}ページ`}・{item.span_start}〜{item.span_end}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

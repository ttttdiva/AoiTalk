"use client";

// 資料（source）レビューの分離コンポーネント。
// ここに来る値は信頼済みアダプターが検証済みで、本文は常にプレーンテキストとして描画する。
// dangerouslySetInnerHTML も <a href> の生成も行わない（モデル供給のリンク/HTMLを描画しない）。

import type {
  EvidenceReference,
  ReceiptSource,
  ReceiptSourcePreview,
} from "./finance-receipt-dto";
import { maskByteSize, maskDigest, maskReference } from "./finance-receipt-dto";

export type FinanceReceiptSourceProps = Readonly<{
  source: ReceiptSource;
  preview: ReceiptSourcePreview | null;
  previewError: string | null;
}>;

function evidenceLabel(reference: EvidenceReference): string {
  const page = reference.page === null ? "ページ指定なし" : `${reference.page}ページ`;
  return `根拠 ${maskReference(reference.source_ref)}・${page}・${reference.span_start}〜${reference.span_end}`;
}

export function FinanceReceiptSource({
  source,
  preview,
  previewError,
}: FinanceReceiptSourceProps) {
  return (
    <section
      aria-label="選択した資料"
      data-testid="receipt-source"
      className="rounded-md border border-slate-200 p-3 text-sm"
    >
      <h3 className="text-sm font-medium">資料</h3>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-slate-500">資料ID</dt>
        <dd data-testid="source-ref">{maskReference(source.source_ref)}</dd>
        <dt className="text-slate-500">種別</dt>
        <dd>{source.mime_type}</dd>
        <dt className="text-slate-500">サイズ</dt>
        <dd>{maskByteSize(source.byte_size)}</dd>
        <dt className="text-slate-500">照合値</dt>
        <dd data-testid="source-digest">{maskDigest(source.source_sha256)}</dd>
      </dl>

      {previewError === null ? null : (
        <p role="alert" className="mt-2 text-xs text-rose-700">
          {previewError}
        </p>
      )}

      {preview === null ? null : (
        <div className="mt-3">
          <p className="text-xs text-slate-500">
            根拠（信頼済みアダプターが抽出した範囲・プレーンテキスト）
          </p>
          <pre
            data-testid="source-preview-text"
            className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-2 text-xs"
          >
            {preview.text}
          </pre>
          <ul className="mt-2 space-y-1 text-xs" data-testid="source-evidence">
            {preview.evidence.map((reference) => (
              <li
                key={`${reference.source_ref}:${reference.page ?? "none"}:${reference.span_start}:${reference.span_end}`}
              >
                {evidenceLabel(reference)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

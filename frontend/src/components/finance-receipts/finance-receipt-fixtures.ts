// 合成フィクスチャ専用モジュール。実データ・実API・実フィードには一切触れない。
// バックエンド（受領書normalizer / personal_money）は未接続のため、
// ここでは整数の最小単位金額だけを持つ合成候補と、注入アダプターの合成実装を提供する。

import type {
  FinanceRow,
  ReceiptCandidate,
  ReceiptConfirmationRequest,
  ReceiptSource,
  ReceiptSourcePreview,
} from "./finance-receipt-dto";
import type { ReceiptReviewAdapter } from "./finance-receipt-review";

export const SYNTHETIC_OWNER_KEY = "synthetic-owner-scope";
export const SYNTHETIC_SOURCE_REF = "11111111-1111-4111-8111-111111111111";
export const SYNTHETIC_SOURCE_SHA = "a".repeat(64);
export const SYNTHETIC_OTHER_SOURCE_REF = "11111111-1111-4111-8111-111111111112";
export const SYNTHETIC_OTHER_SOURCE_SHA = "c".repeat(64);
export const SYNTHETIC_CANDIDATE_REF = "33333333-3333-4333-8333-333333333301";
export const SYNTHETIC_OTHER_CANDIDATE_REF = "33333333-3333-4333-8333-333333333302";
export const SYNTHETIC_DIGEST = "b".repeat(64);
export const SYNTHETIC_OTHER_DIGEST = "d".repeat(64);
export const SYNTHETIC_ENTRY_REF = "44444444-4444-4444-8444-444444444401";

export function syntheticSource(
  overrides: Partial<ReceiptSource> = {},
): ReceiptSource {
  return {
    source_ref: SYNTHETIC_SOURCE_REF,
    source_sha256: SYNTHETIC_SOURCE_SHA,
    mime_type: "image/png",
    byte_size: 248_320,
    ...overrides,
  };
}

export function syntheticOtherSource(): ReceiptSource {
  return syntheticSource({
    source_ref: SYNTHETIC_OTHER_SOURCE_REF,
    source_sha256: SYNTHETIC_OTHER_SOURCE_SHA,
    mime_type: "application/pdf",
    byte_size: 1_024,
  });
}

export function syntheticRow(overrides: Partial<FinanceRow> = {}): FinanceRow {
  return {
    row_ref: "22222222-2222-4222-8222-222222222201",
    occurred_on: "2026-10-22",
    kind: "expense",
    amount_minor: 128_000,
    currency: "JPY",
    account_alias: "household-bank-main",
    category: "食費",
    ...overrides,
  };
}

export function syntheticCandidate(
  overrides: Partial<ReceiptCandidate> = {},
): ReceiptCandidate {
  const source = overrides.source ?? syntheticSource();
  return {
    candidate_ref: SYNTHETIC_CANDIDATE_REF,
    revision: 1,
    confirmed_digest: SYNTHETIC_DIGEST,
    source,
    rows: [
      syntheticRow(),
      syntheticRow({
        row_ref: "22222222-2222-4222-8222-222222222202",
        kind: "income",
        amount_minor: 25_000,
        account_alias: "wallet-cash",
        category: null,
      }),
    ],
    needs_review: [],
    possible_duplicate: false,
    status: "pending",
    ...overrides,
  };
}

export function syntheticPreview(
  source: ReceiptSource = syntheticSource(),
  text = "2026-10-22 スーパー 1,280 JPY / 端数なし",
): ReceiptSourcePreview {
  return {
    text,
    evidence: [
      {
        source_ref: source.source_ref,
        source_sha256: source.source_sha256,
        page: 1,
        span_start: 0,
        span_end: 32,
      },
    ],
  };
}

export function syntheticAcceptance(request: ReceiptConfirmationRequest) {
  return {
    candidate_ref: request.candidate_ref,
    revision: request.revision,
    confirmed_digest: request.confirmed_digest,
    status: "accepted" as const,
    entry_refs: [SYNTHETIC_ENTRY_REF],
  };
}

export type SyntheticAdapterOptions = Readonly<{
  sources?: readonly ReceiptSource[];
  candidateFor?: (sourceRef: string) => unknown;
  previewFor?: (source: ReceiptSource) => unknown;
  onConfirm?: (
    request: ReceiptConfirmationRequest,
    attempt: number,
  ) => Promise<unknown>;
  keys?: readonly string[];
}>;

export type SyntheticAdapterHandle = Readonly<{
  adapter: ReceiptReviewAdapter;
  confirmations: ReceiptConfirmationRequest[];
}>;

/**
 * 合成アダプター。ネットワークもfetchも使わず、記録した要求だけを返す。
 * これは実APIの代用ではなく、注入境界の検証用スタブである。
 */
export function createSyntheticReceiptAdapter(
  options: SyntheticAdapterOptions = {},
): SyntheticAdapterHandle {
  const sources = options.sources ?? [syntheticSource(), syntheticOtherSource()];
  const keys = options.keys ?? [
    "idem-synthetic-1",
    "idem-synthetic-2",
    "idem-synthetic-3",
  ];
  const confirmations: ReceiptConfirmationRequest[] = [];
  let keyIndex = 0;

  const adapter: ReceiptReviewAdapter = {
    listSources: async () => sources,
    loadCandidate: async (sourceRef: string) => {
      if (options.candidateFor) return options.candidateFor(sourceRef);
      const source =
        sources.find((item) => item.source_ref === sourceRef) ?? syntheticSource();
      return syntheticCandidate({ source });
    },
    loadSourcePreview: async (source: ReceiptSource) => {
      if (options.previewFor) return options.previewFor(source);
      return syntheticPreview(source);
    },
    submitConfirmation: async (request: ReceiptConfirmationRequest) => {
      confirmations.push(request);
      if (options.onConfirm) return options.onConfirm(request, confirmations.length);
      return syntheticAcceptance(request);
    },
    newIdempotencyKey: () => {
      const key = keys[keyIndex] ?? `idem-synthetic-${keyIndex + 1}`;
      keyIndex += 1;
      return key;
    },
  };

  return { adapter, confirmations };
}

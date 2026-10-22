// 領収書レビューUIの固定DTO v1（finance-receipt review レーンの独立境界）。
//
// 重要:
// - これは実行時APIの契約ではない。バックエンド（personal_agent_tools / receipt normalizer /
//   personal_money）は未接続で、UIは注入されたアダプター経由でのみ動作する。
//   このモジュールは fetch も APIクライアントも生成 API型も持たない。
// - 金額の解析・整形は src/services/personal_money.py（Strata pending）の責務。
//   ここでは通貨指数の解釈を一切行わず、最小単位の整数をそのまま表示するだけに留める。
// - 共有契約（openapi / generated types / server 登録）は統合ownerの担当。このファイルは
//   その手前の lane-local 境界であり、他laneのDTOを拡張しない。

export const RECEIPT_MAX_BYTE_SIZE = 10_485_760;
export const RECEIPT_MAX_ROWS = 100;
export const RECEIPT_MAX_AMOUNT_MINOR = 9_007_199_254_740_991;
export const RECEIPT_MAX_CATEGORY_CHARS = 80;
export const RECEIPT_MAX_PREVIEW_CHARS = 4_000;
export const RECEIPT_MAX_EVIDENCE_REFS = 20;
export const RECEIPT_MAX_NEEDS_REVIEW = 20;

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const CURRENCY_PATTERN = /^[A-Z]{3}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const MIME_PATTERN = /^[a-z0-9!#$&^_.+-]{1,64}\/[a-z0-9!#$&^_.+-]{1,64}$/;
// 参照・プレビューにパス/URL/制御文字を混ぜない。
const UNSAFE_REFERENCE_PATTERN = /(:\/\/|\\|\/|\.\.|[\u0000-\u001f\u007f])/;

/** 資料の固定DTO。source_ref はUUID、source_sha256 は小文字64桁hex。 */
export type ReceiptSource = Readonly<{
  source_ref: string;
  source_sha256: string;
  mime_type: string;
  byte_size: number;
}>;

/** 根拠参照。パスもURLも持たず、資料ID/照合値と有界のページ・範囲だけを持つ。 */
export type EvidenceReference = Readonly<{
  source_ref: string;
  source_sha256: string;
  page: number | null;
  span_start: number;
  span_end: number;
}>;

export type FinanceRowKind = "income" | "expense" | "balance";

/** FinanceRow（docs の FinanceRow 準拠）。金額は最小単位の整数のみ。 */
export type FinanceRow = Readonly<{
  row_ref: string;
  occurred_on: string;
  kind: FinanceRowKind;
  amount_minor: number;
  currency: string;
  account_alias: string;
  category: string | null;
}>;

export type ReceiptCandidateStatus = "pending" | "accepted" | "rejected";

/** Candidate の固定DTO。owner / session / model確認はここに入らない。 */
export type ReceiptCandidate = Readonly<{
  candidate_ref: string;
  revision: number;
  confirmed_digest: string;
  source: ReceiptSource;
  rows: readonly FinanceRow[];
  needs_review: readonly string[];
  possible_duplicate: boolean;
  status: ReceiptCandidateStatus;
}>;

/** 受理応答の固定DTO。 */
export type ReceiptAcceptanceResponse = Readonly<{
  candidate_ref: string;
  revision: number;
  confirmed_digest: string;
  status: "accepted";
  entry_refs: readonly string[];
}>;

/** 確定要求の固定DTO。owner_confirmed は常に true、重複時のみ duplicate_confirmed。 */
export type ReceiptConfirmationRequest = Readonly<{
  candidate_ref: string;
  revision: number;
  confirmed_digest: string;
  idempotency_key: string;
  owner_confirmed: true;
  duplicate_confirmed?: true;
}>;

/** 信頼済みアダプターが返す資料プレビュー（本文はプレーンテキストのみ）。 */
export type ReceiptSourcePreview = Readonly<{
  text: string;
  evidence: readonly EvidenceReference[];
}>;

export type ParseResult<T> =
  | Readonly<{ ok: true; value: T }>
  | Readonly<{ ok: false; reason: string }>;

function ok<T>(value: T): ParseResult<T> {
  return { ok: true, value };
}

function fail<T>(reason: string): ParseResult<T> {
  return { ok: false, reason };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isIntegerBetween(
  value: unknown,
  minimum: number,
  maximum: number,
): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= minimum &&
    value <= maximum
  );
}

function isCalendarDate(value: string): boolean {
  if (!DATE_PATTERN.test(value)) return false;
  const [year, month, day] = value.split("-").map((part) => Number(part));
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}

/** 参照文字列にパス/URL/制御文字が混ざっていないことを確認する。 */
export function isBoundedReferenceText(value: string): boolean {
  if (value.length === 0 || value.length > 200) return false;
  return !UNSAFE_REFERENCE_PATTERN.test(value);
}

export function parseReceiptSource(value: unknown): ParseResult<ReceiptSource> {
  if (!isRecord(value)) return fail("資料の形式が不正です");
  const { source_ref, source_sha256, mime_type, byte_size } = value;
  if (typeof source_ref !== "string" || !UUID_PATTERN.test(source_ref)) {
    return fail("資料IDが不正です");
  }
  if (typeof source_sha256 !== "string" || !SHA256_PATTERN.test(source_sha256)) {
    return fail("資料の照合値が不正です");
  }
  if (typeof mime_type !== "string" || !MIME_PATTERN.test(mime_type)) {
    return fail("資料の種別が不正です");
  }
  if (!isIntegerBetween(byte_size, 1, RECEIPT_MAX_BYTE_SIZE)) {
    return fail("資料のサイズが不正です");
  }
  return ok({ source_ref, source_sha256, mime_type, byte_size });
}

export function parseEvidenceReference(
  value: unknown,
): ParseResult<EvidenceReference> {
  if (!isRecord(value)) return fail("根拠参照の形式が不正です");
  const { source_ref, source_sha256, page, span_start, span_end } = value;
  if (typeof source_ref !== "string" || !UUID_PATTERN.test(source_ref)) {
    return fail("根拠参照の資料IDが不正です");
  }
  if (typeof source_sha256 !== "string" || !SHA256_PATTERN.test(source_sha256)) {
    return fail("根拠参照の照合値が不正です");
  }
  if (page !== null && !isIntegerBetween(page, 0, 10_000)) {
    return fail("根拠参照のページが不正です");
  }
  if (!isIntegerBetween(span_start, 0, 20_000)) {
    return fail("根拠参照の範囲が不正です");
  }
  if (!isIntegerBetween(span_end, 0, 40_000) || span_end <= span_start) {
    return fail("根拠参照の範囲が不正です");
  }
  if (span_end - span_start > 20_000) {
    return fail("根拠参照の範囲が広すぎます");
  }
  return ok({
    source_ref,
    source_sha256,
    page: page === null ? null : page,
    span_start,
    span_end,
  });
}

export function evidenceMatchesSource(
  reference: EvidenceReference,
  source: ReceiptSource,
): boolean {
  return (
    reference.source_ref === source.source_ref &&
    reference.source_sha256 === source.source_sha256
  );
}

export function parseFinanceRow(value: unknown): ParseResult<FinanceRow> {
  if (!isRecord(value)) return fail("明細の形式が不正です");
  const {
    row_ref,
    occurred_on,
    kind,
    amount_minor,
    currency,
    account_alias,
    category,
  } = value;
  if (typeof row_ref !== "string" || !UUID_PATTERN.test(row_ref)) {
    return fail("明細IDが不正です");
  }
  if (typeof occurred_on !== "string" || !isCalendarDate(occurred_on)) {
    return fail("日付は YYYY-MM-DD の実在日付である必要があります");
  }
  if (kind !== "income" && kind !== "expense" && kind !== "balance") {
    return fail("明細の区分が不正です");
  }
  if (!isIntegerBetween(amount_minor, 0, RECEIPT_MAX_AMOUNT_MINOR)) {
    return fail("金額は最小単位の整数である必要があります");
  }
  if (typeof currency !== "string" || !CURRENCY_PATTERN.test(currency)) {
    return fail("通貨は大文字3文字である必要があります");
  }
  if (
    typeof account_alias !== "string" ||
    account_alias.length === 0 ||
    account_alias.length > 64 ||
    /[\u0000-\u001f\u007f]/.test(account_alias)
  ) {
    return fail("口座の別名が不正です");
  }
  if (
    category !== null &&
    (typeof category !== "string" ||
      category.length === 0 ||
      category.length > RECEIPT_MAX_CATEGORY_CHARS)
  ) {
    return fail("分類が不正です");
  }
  return ok({
    row_ref,
    occurred_on,
    kind,
    amount_minor,
    currency,
    account_alias,
    category: category === null ? null : category,
  });
}

function parseNeedsReview(value: unknown): ParseResult<readonly string[]> {
  if (!Array.isArray(value)) return fail("確認事項の形式が不正です");
  if (value.length > RECEIPT_MAX_NEEDS_REVIEW) {
    return fail("確認事項が多すぎます");
  }
  const items: string[] = [];
  for (const item of value) {
    if (
      typeof item !== "string" ||
      item.trim().length === 0 ||
      item.length > 200 ||
      /[\u0000-\u001f\u007f]/.test(item)
    ) {
      return fail("確認事項の内容が不正です");
    }
    items.push(item);
  }
  return ok(items);
}

export function parseReceiptCandidate(
  value: unknown,
): ParseResult<ReceiptCandidate> {
  if (!isRecord(value)) return fail("候補の形式が不正です");
  const { candidate_ref, revision, confirmed_digest, rows, status } = value;
  if (typeof candidate_ref !== "string" || !UUID_PATTERN.test(candidate_ref)) {
    return fail("候補IDが不正です");
  }
  if (!isIntegerBetween(revision, 1, 1_000_000)) {
    return fail("候補の改訂番号が不正です");
  }
  if (
    typeof confirmed_digest !== "string" ||
    !SHA256_PATTERN.test(confirmed_digest)
  ) {
    return fail("候補の照合値が不正です");
  }
  const parsedSource = parseReceiptSource(value.source);
  if (!parsedSource.ok) return fail(parsedSource.reason);
  if (!Array.isArray(rows) || rows.length > RECEIPT_MAX_ROWS) {
    return fail("明細の件数が不正です");
  }
  const parsedRows: FinanceRow[] = [];
  for (const row of rows) {
    const parsedRow = parseFinanceRow(row);
    if (!parsedRow.ok) return fail(parsedRow.reason);
    parsedRows.push(parsedRow.value);
  }
  const parsedNeedsReview = parseNeedsReview(value.needs_review);
  if (!parsedNeedsReview.ok) return fail(parsedNeedsReview.reason);
  if (typeof value.possible_duplicate !== "boolean") {
    return fail("重複の可能性の形式が不正です");
  }
  if (status !== "pending" && status !== "accepted" && status !== "rejected") {
    return fail("候補の状態が不正です");
  }
  return ok({
    candidate_ref,
    revision,
    confirmed_digest,
    source: parsedSource.value,
    rows: parsedRows,
    needs_review: parsedNeedsReview.value,
    possible_duplicate: value.possible_duplicate,
    status,
  });
}

export function parseSourceList(
  value: unknown,
): ParseResult<readonly ReceiptSource[]> {
  if (!Array.isArray(value)) return fail("資料一覧の形式が不正です");
  if (value.length > 50) return fail("資料一覧が多すぎます");
  const sources: ReceiptSource[] = [];
  for (const item of value) {
    const parsed = parseReceiptSource(item);
    if (!parsed.ok) return fail(parsed.reason);
    sources.push(parsed.value);
  }
  return ok(sources);
}

function stripPreviewControls(value: string): string {
  // 制御文字（改行・タブ以外）を落とし、長すぎる本文は有界に切り詰める。
  const withoutControls = value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "");
  if (withoutControls.length <= RECEIPT_MAX_PREVIEW_CHARS) return withoutControls;
  return `${withoutControls.slice(0, RECEIPT_MAX_PREVIEW_CHARS)}…`;
}

export function parseSourcePreview(
  value: unknown,
  source: ReceiptSource,
): ParseResult<ReceiptSourcePreview> {
  if (!isRecord(value)) return fail("資料プレビューの形式が不正です");
  if (typeof value.text !== "string") {
    return fail("資料プレビュー本文の形式が不正です");
  }
  if (!Array.isArray(value.evidence)) {
    return fail("根拠参照の一覧が不正です");
  }
  if (value.evidence.length > RECEIPT_MAX_EVIDENCE_REFS) {
    return fail("根拠参照が多すぎます");
  }
  const evidence: EvidenceReference[] = [];
  for (const item of value.evidence) {
    const parsed = parseEvidenceReference(item);
    if (!parsed.ok) return fail(parsed.reason);
    if (!evidenceMatchesSource(parsed.value, source)) {
      return fail("根拠が選択した資料と一致しません");
    }
    if (!isBoundedReferenceText(parsed.value.source_ref)) {
      return fail("根拠参照の資料IDが不正です");
    }
    evidence.push(parsed.value);
  }
  return ok({ text: stripPreviewControls(value.text), evidence });
}

export function parseReceiptAcceptance(
  value: unknown,
  request: ReceiptConfirmationRequest,
): ParseResult<ReceiptAcceptanceResponse> {
  if (!isRecord(value)) return fail("確定結果の形式が不正です");
  if (value.status !== "accepted") return fail("確定結果の状態が一致しません");
  if (value.candidate_ref !== request.candidate_ref) {
    return fail("確定結果の候補が要求と一致しません");
  }
  if (value.revision !== request.revision) {
    return fail("確定結果の改訂番号が要求と一致しません");
  }
  if (value.confirmed_digest !== request.confirmed_digest) {
    return fail("確定結果の照合値が要求と一致しません");
  }
  if (!Array.isArray(value.entry_refs) || value.entry_refs.length > RECEIPT_MAX_ROWS) {
    return fail("確定結果の台帳参照が不正です");
  }
  const entryRefs: string[] = [];
  for (const entry of value.entry_refs) {
    if (typeof entry !== "string" || !UUID_PATTERN.test(entry)) {
      return fail("確定結果の台帳参照が不正です");
    }
    entryRefs.push(entry);
  }
  return ok({
    candidate_ref: request.candidate_ref,
    revision: request.revision,
    confirmed_digest: request.confirmed_digest,
    status: "accepted",
    entry_refs: entryRefs,
  });
}

export type ConfirmationGateOptions = Readonly<{
  duplicateConfirmed: boolean;
}>;

/**
 * 確定可否を判定する。空配列 = 確定可能。
 * needs_review / 根拠欠落・不一致 / 重複の明示確認なし は必ず塞ぐ。
 */
export function receiptConfirmationBlockers(
  candidate: ReceiptCandidate,
  evidence: readonly EvidenceReference[] | null,
  options: ConfirmationGateOptions,
): readonly string[] {
  const blockers: string[] = [];
  if (candidate.status !== "pending") {
    blockers.push("この候補は既に確定または却下されています");
  }
  if (candidate.rows.length === 0) {
    blockers.push("明細が1件もない候補は確定できません");
  }
  if (candidate.needs_review.length > 0) {
    blockers.push(`確認が必要な項目があります: ${candidate.needs_review.join(", ")}`);
  }
  if (evidence === null || evidence.length === 0) {
    blockers.push("資料の根拠参照がないため確定できません");
  } else if (
    !evidence.every((reference) => evidenceMatchesSource(reference, candidate.source))
  ) {
    blockers.push("根拠が選択した資料と一致しません");
  }
  if (candidate.possible_duplicate && !options.duplicateConfirmed) {
    blockers.push("重複の可能性があるため、明示的な確認が必要です");
  }
  return blockers;
}

/** 4xx（408/425/429 を除く）は「確定していない」ことが確定する失敗として扱う。 */
export function isDefinitiveConfirmationFailure(error: unknown): boolean {
  if (!isRecord(error) && typeof error !== "object") return false;
  const status = (error as { status?: unknown } | null)?.status;
  if (typeof status !== "number" || !Number.isInteger(status)) return false;
  if (status >= 500 || status < 400) return false;
  if (status === 408 || status === 425 || status === 429) return false;
  return true;
}

export class ReceiptRequestError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ReceiptRequestError";
    this.status = status;
  }
}

/** 表示専用のフォールバック。通貨指数の解釈はしない（personal_money 到着後に差し替える）。 */
export type MinorUnitFormatter = (minorUnits: number, currency: string) => string;

export const displayMinorUnitsRaw: MinorUnitFormatter = (minorUnits, currency) =>
  `${minorUnits} ${currency}（最小単位）`;

// --- 表示用マスキング -------------------------------------------------------

export function maskReference(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length === 0) return "…";
  if (trimmed.length <= 4) return "•".repeat(trimmed.length);
  return `…${trimmed.slice(-4)}`;
}

export function maskAccountAlias(alias: string): string {
  const chars = Array.from(alias.trim());
  if (chars.length === 0) return "…";
  if (chars.length <= 3) return "•".repeat(chars.length);
  const hidden = "•".repeat(Math.max(chars.length - 3, 3));
  return `${chars[0]}${chars[1]}${hidden}${chars[chars.length - 1]}`;
}

export function maskDigest(digest: string): string {
  if (!SHA256_PATTERN.test(digest)) return "…";
  return `${digest.slice(0, 8)}…${digest.slice(-4)}`;
}

export function maskByteSize(byteSize: number): string {
  if (byteSize < 1024) return `${byteSize} B`;
  if (byteSize < 1024 * 1024) return `${(byteSize / 1024).toFixed(1)} KB`;
  return `${(byteSize / (1024 * 1024)).toFixed(1)} MB`;
}

function randomKey(): string {
  const cryptoApi = globalThis.crypto as Crypto | undefined;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
    return `rcpt-${cryptoApi.randomUUID()}`;
  }
  if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    return `rcpt-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
  }
  return `rcpt-${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}`;
}

/** 冪等キーの生成。テストではアダプター側の生成器を注入して固定できる。 */
export function createIdempotencyKey(): string {
  return randomKey();
}

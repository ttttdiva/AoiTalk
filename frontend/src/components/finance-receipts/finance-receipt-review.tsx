"use client";

// 領収書候補のレビューUI（LANE4 / synthetic only）。
//
// このコンポーネントはバックエンド未接続の分離UIである:
// - ネットワーク呼び出しは持たず、注入された ReceiptReviewAdapter だけを使う。
// - owner / session は信頼済み呼び出し元が prop で渡す。候補DTOに owner を混ぜない。
// - 確定は本人の明示的な操作（資料の明示選択 → 内容確認 → 確定）でのみ行い、自動確定しない。
// - needs_review / 根拠欠落 / 重複の可能性（明示確認なし）は確定させない。
// - 応答不明時は同じ要求・同じ冪等キーを保持し、新しい操作を作らない。
// - owner / 資料 / 候補が変わった後に届いた遅延応答は、表示中の状態へ反映しない。

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  createIdempotencyKey,
  displayMinorUnitsRaw,
  isDefinitiveConfirmationFailure,
  maskAccountAlias,
  maskByteSize,
  maskDigest,
  maskReference,
  parseReceiptAcceptance,
  parseReceiptCandidate,
  parseSourceList,
  parseSourcePreview,
  receiptConfirmationBlockers,
  type EvidenceReference,
  type FinanceRowKind,
  type MinorUnitFormatter,
  type ReceiptCandidate,
  type ReceiptConfirmationRequest,
  type ReceiptSource,
  type ReceiptSourcePreview,
} from "./finance-receipt-dto";
import { FinanceReceiptSource } from "./finance-receipt-source";

/**
 * 注入アダプター。実APIの実装は統合ownerが用意する（このレーンでは未接続）。
 * 戻り値は unknown として受け取り、UI側で必ず検証する。
 */
export type ReceiptReviewAdapter = Readonly<{
  listSources: () => Promise<unknown>;
  loadCandidate: (sourceRef: string) => Promise<unknown>;
  loadSourcePreview: (source: ReceiptSource) => Promise<unknown>;
  submitConfirmation: (request: ReceiptConfirmationRequest) => Promise<unknown>;
  newIdempotencyKey?: () => string;
}>;

export type FinanceReceiptReviewProps = Readonly<{
  /** 信頼済み呼び出し元が渡す不透明なスコープ識別子。確定要求には含めない。 */
  ownerKey: string;
  adapter: ReceiptReviewAdapter;
  /**
   * 金額表示の注入点。既定は最小単位の整数表示のみ（通貨指数の解釈はしない）。
   * personal_money.format_minor_units の到着後に統合ownerが差し替える。
   */
  formatAmount?: MinorUnitFormatter;
}>;

type ReviewState =
  | Readonly<{ kind: "idle" }>
  | Readonly<{ kind: "loading"; sourceRef: string }>
  | Readonly<{ kind: "empty"; sourceRef: string }>
  | Readonly<{ kind: "unavailable"; sourceRef: string; message: string }>
  | Readonly<{
      kind: "ready";
      sourceRef: string;
      candidate: ReceiptCandidate;
      preview: ReceiptSourcePreview | null;
      previewError: string | null;
    }>;

type PendingOperation = Readonly<{
  request: ReceiptConfirmationRequest;
  candidateRef: string;
  revision: number;
  digest: string;
  sourceRef: string;
  ownerKey: string;
}>;

type AcceptedScope = Readonly<{
  ownerKey: string;
  sourceRef: string;
  candidateRef: string;
  revision: number;
  digest: string;
  entryRefs: readonly string[];
}>;

const ROW_KIND_LABEL: Readonly<Record<FinanceRowKind, string>> = {
  income: "収入",
  expense: "支出",
  balance: "残高",
};

const STATUS_LABEL: Readonly<Record<ReceiptCandidate["status"], string>> = {
  pending: "未確定の候補",
  accepted: "確定済みの候補",
  rejected: "却下済みの候補",
};

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.length > 0) return error.message;
  return fallback;
}

export function FinanceReceiptReview({
  ownerKey,
  adapter,
  formatAmount = displayMinorUnitsRaw,
}: FinanceReceiptReviewProps) {
  const titleId = useId();
  const radioName = useId();
  const confirmId = useId();
  const duplicateId = useId();

  const [sources, setSources] = useState<readonly ReceiptSource[] | null>(null);
  const [sourcesMessage, setSourcesMessage] = useState<string | null>(null);
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [review, setReview] = useState<ReviewState>({ kind: "idle" });
  const [confirmed, setConfirmed] = useState(false);
  const [duplicateConfirmed, setDuplicateConfirmed] = useState(false);
  const [pending, setPending] = useState<PendingOperation | null>(null);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState<AcceptedScope | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const epochRef = useRef(0);
  const scopeRef = useRef<{ ownerKey: string; sourceRef: string | null }>({
    ownerKey,
    sourceRef: null,
  });
  const pendingRef = useRef<PendingOperation | null>(null);
  const ownerRef = useRef(ownerKey);
  const adapterRef = useRef(adapter);

  // アダプターは呼び出し元が安定した同一インスタンスを渡す前提。
  // ここで参照を最新化しておき、遅延応答側でも最新の実装を使う。
  useEffect(() => {
    adapterRef.current = adapter;
  }, [adapter]);

  // owner スコープ変更: 選択・確認・確定結果を破棄する。
  // 保留中の操作は「照合すべき不変の記録」なので意図的に保持する。
  useEffect(() => {
    if (ownerRef.current === ownerKey) return;
    ownerRef.current = ownerKey;
    epochRef.current += 1;
    scopeRef.current = { ownerKey, sourceRef: null };
    setSelectedRef(null);
    setReview({ kind: "idle" });
    setConfirmed(false);
    setDuplicateConfirmed(false);
    setAccepted(null);
    setNotice(null);
  }, [ownerKey]);

  useEffect(() => {
    let cancelled = false;
    const epoch = epochRef.current;
    // 一覧は ownerKey 変更ごとに外部アダプターから再取得するため、取得前に初期化する。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSources(null);
    setSourcesMessage(null);
    void adapterRef
      .current
      .listSources()
      .then((raw: unknown) => {
        if (cancelled || epoch !== epochRef.current) return;
        const parsed = parseSourceList(raw);
        if (!parsed.ok) {
          setSourcesMessage(parsed.reason);
          return;
        }
        setSources(parsed.value);
        if (parsed.value.length === 0) {
          setSourcesMessage("確認できる資料がありません");
        }
      })
      .catch((error: unknown) => {
        if (cancelled || epoch !== epochRef.current) return;
        setSourcesMessage(errorMessage(error, "資料一覧を取得できませんでした"));
      });
    return () => {
      cancelled = true;
    };
  }, [ownerKey]);

  async function loadCandidateFor(source: ReceiptSource, epoch: number) {
    let raw: unknown;
    try {
      raw = await adapterRef.current.loadCandidate(source.source_ref);
    } catch (error: unknown) {
      if (epoch !== epochRef.current) return;
      setReview({
        kind: "unavailable",
        sourceRef: source.source_ref,
        message: errorMessage(error, "候補を取得できませんでした"),
      });
      return;
    }
    // 資料・owner が変わった後の遅延応答は表示中の状態へ反映しない。
    if (epoch !== epochRef.current) return;
    if (raw === null || raw === undefined) {
      setReview({ kind: "empty", sourceRef: source.source_ref });
      return;
    }
    const parsedCandidate = parseReceiptCandidate(raw);
    if (!parsedCandidate.ok) {
      setReview({
        kind: "unavailable",
        sourceRef: source.source_ref,
        message: parsedCandidate.reason,
      });
      return;
    }
    const candidate = parsedCandidate.value;
    if (
      candidate.source.source_ref !== source.source_ref ||
      candidate.source.source_sha256 !== source.source_sha256
    ) {
      setReview({
        kind: "unavailable",
        sourceRef: source.source_ref,
        message: "候補の資料が選択した資料と一致しません",
      });
      return;
    }

    let preview: ReceiptSourcePreview | null = null;
    let previewError: string | null = null;
    try {
      const rawPreview = await adapterRef.current.loadSourcePreview(candidate.source);
      if (epoch !== epochRef.current) return;
      const parsedPreview = parseSourcePreview(rawPreview, candidate.source);
      if (parsedPreview.ok) preview = parsedPreview.value;
      else previewError = parsedPreview.reason;
    } catch (error: unknown) {
      if (epoch !== epochRef.current) return;
      previewError = errorMessage(error, "資料の根拠を取得できませんでした");
    }
    if (epoch !== epochRef.current) return;
    setReview({
      kind: "ready",
      sourceRef: source.source_ref,
      candidate,
      preview,
      previewError,
    });
  }

  function selectSource(source: ReceiptSource) {
    // 保留中の確定がある間は競合する編集を受け付けない。
    if (pendingRef.current !== null) return;
    epochRef.current += 1;
    const epoch = epochRef.current;
    scopeRef.current = { ownerKey: ownerRef.current, sourceRef: source.source_ref };
    setSelectedRef(source.source_ref);
    setConfirmed(false);
    setDuplicateConfirmed(false);
    setAccepted(null);
    setNotice(null);
    setReview({ kind: "loading", sourceRef: source.source_ref });
    void loadCandidateFor(source, epoch);
  }

  async function runConfirmation(operation: PendingOperation) {
    setBusy(true);
    let raw: unknown;
    try {
      raw = await adapterRef.current.submitConfirmation(operation.request);
    } catch (error: unknown) {
      if (pendingRef.current !== operation) return;
      setBusy(false);
      if (isDefinitiveConfirmationFailure(error)) {
        pendingRef.current = null;
        setPending(null);
        setConfirmed(false);
        setNotice(
          "確定は受理されませんでした。候補を再取得して確認し直してください。",
        );
      } else {
        // 結果不明: 要求はそのまま保持し、同じ冪等キーで照合に回す。
        setNotice(
          "確定結果を確認できませんでした。同じ内容・同じ冪等キーで照合します。",
        );
      }
      return;
    }
    if (pendingRef.current !== operation) return;
    setBusy(false);

    const parsed = parseReceiptAcceptance(raw, operation.request);
    if (!parsed.ok) {
      // 応答を検証できない = 結果不明。操作は保持したまま照合に回す。
      setNotice(
        `確定結果を照合できませんでした（${parsed.reason}）。同じ内容・同じ冪等キーで照合します。`,
      );
      return;
    }

    const sameScope =
      scopeRef.current.ownerKey === operation.ownerKey &&
      scopeRef.current.sourceRef === operation.sourceRef;
    pendingRef.current = null;
    setPending(null);
    setConfirmed(false);
    setDuplicateConfirmed(false);
    if (!sameScope) {
      setNotice(
        "保留していた確定結果を照合しました。表示中の資料には反映していません。",
      );
      return;
    }
    setAccepted({
      ownerKey: operation.ownerKey,
      sourceRef: operation.sourceRef,
      candidateRef: parsed.value.candidate_ref,
      revision: parsed.value.revision,
      digest: parsed.value.confirmed_digest,
      entryRefs: parsed.value.entry_refs,
    });
    setNotice(null);
  }

  const evidence: readonly EvidenceReference[] | null =
    review.kind === "ready" ? review.preview?.evidence ?? null : null;
  const blockers = useMemo<readonly string[]>(
    () =>
      review.kind === "ready"
        ? receiptConfirmationBlockers(review.candidate, evidence, {
            duplicateConfirmed,
          })
        : [],
    [review, evidence, duplicateConfirmed],
  );

  const locked = pending !== null || busy;
  const activeCandidate = review.kind === "ready" ? review.candidate : null;
  const canConfirm =
    activeCandidate !== null &&
    activeCandidate.status === "pending" &&
    !locked &&
    confirmed &&
    blockers.length === 0;

  function confirmCandidate() {
    if (review.kind !== "ready") return;
    if (pendingRef.current !== null) return;
    if (blockers.length > 0) {
      setNotice(blockers[0]);
      return;
    }
    if (!confirmed) {
      setNotice("内容を確認してから確定してください。");
      return;
    }
    const candidate = review.candidate;
    const base = {
      candidate_ref: candidate.candidate_ref,
      revision: candidate.revision,
      confirmed_digest: candidate.confirmed_digest,
      idempotency_key: (adapterRef.current.newIdempotencyKey ?? createIdempotencyKey)(),
      owner_confirmed: true as const,
    };
    const request: ReceiptConfirmationRequest = Object.freeze(
      candidate.possible_duplicate && duplicateConfirmed
        ? { ...base, duplicate_confirmed: true as const }
        : base,
    );
    // 一度作った要求は凍結して保持し、再送でも同じ内容・同じ冪等キーを使う。
    const operation: PendingOperation = Object.freeze({
      request,
      candidateRef: candidate.candidate_ref,
      revision: candidate.revision,
      digest: candidate.confirmed_digest,
      sourceRef: review.sourceRef,
      ownerKey: scopeRef.current.ownerKey,
    });
    pendingRef.current = operation;
    setPending(operation);
    setNotice(null);
    void runConfirmation(operation);
  }

  function retryPending() {
    const operation = pendingRef.current;
    if (operation === null || busy) return;
    void runConfirmation(operation);
  }

  // 候補の再取得（候補が変わった場合の遅延応答は epoch で棄却される）。
  function reloadCandidate() {
    if (pendingRef.current !== null) return;
    const source = (sources ?? []).find(
      (item) => item.source_ref === selectedRef,
    );
    if (!source) return;
    selectSource(source);
  }

  return (
    <section
      aria-labelledby={titleId}
      data-testid="finance-receipt-review"
      className="space-y-4 rounded-lg border border-slate-200 p-4"
    >
      <header className="space-y-1">
        <h2 id={titleId} className="text-base font-semibold">
          領収書候補の確認
        </h2>
        <p className="text-xs text-slate-500">
          合成データ専用の画面です。バックエンドは未接続で、注入されたアダプター経由でのみ動作します。
        </p>
      </header>

      <fieldset className="space-y-2" disabled={locked}>
        <legend className="text-sm font-medium">
          資料を選択（自動選択はしません）
        </legend>
        {sources === null ? (
          <p className="text-xs text-slate-500" data-testid="sources-loading">
            資料一覧を読み込み中…
          </p>
        ) : null}
        {sourcesMessage === null ? null : (
          <p role="alert" className="text-xs text-rose-700">
            {sourcesMessage}
          </p>
        )}
        <ul className="space-y-1">
          {(sources ?? []).map((source, index) => {
            const optionId = `${radioName}-${index}`;
            return (
              <li key={source.source_ref}>
                <label htmlFor={optionId} className="flex items-center gap-2 text-sm">
                  <input
                    id={optionId}
                    type="radio"
                    name={radioName}
                    value={source.source_ref}
                    checked={selectedRef === source.source_ref}
                    onChange={() => selectSource(source)}
                  />
                  <span data-testid={`source-option-${index}`}>
                    資料{index + 1}・{maskReference(source.source_ref)}・
                    {source.mime_type}・{maskByteSize(source.byte_size)}
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
      </fieldset>

      {review.kind === "loading" ? (
        <p className="text-xs text-slate-500" data-testid="candidate-loading">
          候補を読み込み中…
        </p>
      ) : null}
      {review.kind === "empty" ? (
        <p className="text-sm" data-testid="candidate-empty">
          この資料に対応する候補はありません。
        </p>
      ) : null}
      {review.kind === "unavailable" ? (
        <p role="alert" className="text-sm text-rose-700" data-testid="candidate-error">
          {review.message}
        </p>
      ) : null}

      {review.kind === "ready" ? (
        <div className="space-y-3">
          <FinanceReceiptSource
            source={review.candidate.source}
            preview={review.preview}
            previewError={review.previewError}
          />

          <article
            aria-label="選択した候補"
            data-testid="receipt-candidate"
            className="space-y-3 rounded-md border border-slate-200 p-3"
          >
            <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
              <h3 className="text-sm font-medium">候補の内容</h3>
              <span data-testid="candidate-status">
                {STATUS_LABEL[review.candidate.status]}
              </span>
              <span>revision {review.candidate.revision}</span>
              <span data-testid="candidate-digest">
                照合値 {maskDigest(review.candidate.confirmed_digest)}
              </span>
            </header>

            {review.candidate.possible_duplicate ? (
              <p
                data-testid="duplicate-warning"
                className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900"
              >
                重複の可能性がある候補です。明示的に確認しない限り確定できません。
              </p>
            ) : null}

            {review.candidate.needs_review.length > 0 ? (
              <div data-testid="needs-review" className="text-xs text-amber-900">
                <p className="font-medium">確認が必要な項目</p>
                <ul className="list-disc space-y-1 pl-5">
                  {review.candidate.needs_review.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            <table className="w-full text-xs" data-testid="receipt-rows">
              <caption className="sr-only">候補の明細</caption>
              <thead>
                <tr className="text-left text-slate-500">
                  <th scope="col">日付</th>
                  <th scope="col">区分</th>
                  <th scope="col">金額</th>
                  <th scope="col">口座</th>
                  <th scope="col">分類</th>
                </tr>
              </thead>
              <tbody>
                {review.candidate.rows.map((row, index) => (
                  <tr key={row.row_ref}>
                    <td>{row.occurred_on}</td>
                    <td>{ROW_KIND_LABEL[row.kind]}</td>
                    <td data-testid={`row-amount-${index}`}>
                      {formatAmount(row.amount_minor, row.currency)}
                    </td>
                    <td data-testid={`row-account-${index}`}>
                      {maskAccountAlias(row.account_alias)}
                    </td>
                    <td>{row.category ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="space-y-2">
              <div className="flex items-start gap-2 text-sm">
                <Checkbox
                  id={confirmId}
                  aria-label="候補の内容を確認しました"
                  aria-disabled={locked || blockers.length > 0}
                  checked={confirmed}
                  disabled={locked || blockers.length > 0}
                  onCheckedChange={(value) => setConfirmed(value === true)}
                />
                <span>
                  選択した資料と候補（revision {review.candidate.revision} / 照合値{" "}
                  {maskDigest(review.candidate.confirmed_digest)}）を確認しました
                </span>
              </div>

              {review.candidate.possible_duplicate ? (
                <div className="flex items-start gap-2 text-sm">
                  <Checkbox
                    id={duplicateId}
                    aria-label="重複の可能性を確認しました"
                    aria-disabled={locked}
                    checked={duplicateConfirmed}
                    disabled={locked}
                    onCheckedChange={(value) =>
                      setDuplicateConfirmed(value === true)
                    }
                  />
                  <span>重複の可能性を確認しました（明示的に続行します）</span>
                </div>
              ) : null}
            </div>

            {blockers.length > 0 ? (
              <ul
                role="alert"
                data-testid="confirm-blockers"
                className="list-disc space-y-1 pl-5 text-xs text-amber-800"
              >
                {blockers.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            ) : null}

            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                className="min-h-11"
                data-testid="confirm-candidate"
                disabled={!canConfirm}
                onClick={confirmCandidate}
              >
                この候補を確定
              </Button>
              <Button
                type="button"
                className="min-h-11"
                disabled={locked}
                onClick={reloadCandidate}
              >
                候補を再取得
              </Button>
              <span className="text-xs text-slate-500">
                本人の明示的な確認なしに自動確定はしません。
              </span>
            </div>
          </article>
        </div>
      ) : null}

      {pending === null ? null : (
        <section
          aria-label="保留中の確認"
          data-testid="pending-confirmation"
          className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm"
        >
          <p>
            確定結果を確認できていません。新しい操作は作成せず、同じ内容・同じ冪等キーで照合します。
          </p>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
            <dt className="text-slate-500">候補</dt>
            <dd>
              {maskReference(pending.candidateRef)}（revision {pending.revision}）
            </dd>
            <dt className="text-slate-500">照合値</dt>
            <dd>{maskDigest(pending.digest)}</dd>
            <dt className="text-slate-500">冪等キー</dt>
            <dd data-testid="pending-idempotency-key">
              {maskReference(pending.request.idempotency_key)}
            </dd>
            <dt className="text-slate-500">スコープ</dt>
            <dd>
              {pending.ownerKey === ownerKey
                ? "表示中のスコープ"
                : "別スコープ（表示には反映しません）"}
            </dd>
          </dl>
          <Button
            type="button"
            className="mt-2 min-h-11"
            disabled={busy}
            onClick={retryPending}
          >
            同じ操作で照合
          </Button>
        </section>
      )}

      {accepted !== null &&
      accepted.ownerKey === ownerKey &&
      accepted.sourceRef === selectedRef ? (
        <section
          aria-label="確定結果"
          data-testid="receipt-accepted"
          className="rounded-md border border-emerald-300 bg-emerald-50 p-3 text-sm"
        >
          <p>
            確定しました。台帳項目 {accepted.entryRefs.length} 件を受理しました。
          </p>
          <ul
            className="mt-1 space-y-1 text-xs"
            data-testid="receipt-entry-refs"
          >
            {accepted.entryRefs.map((reference) => (
              <li key={reference}>{maskReference(reference)}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {notice === null ? null : (
        <p role="status" data-testid="receipt-notice" className="text-xs text-slate-700">
          {notice}
        </p>
      )}
    </section>
  );
}

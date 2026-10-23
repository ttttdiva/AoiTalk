"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import {
  isAmbiguousFinanceWrite,
  personalFinanceApi,
  type ReceiptCandidate,
  type ReceiptCandidateConfirmation,
  type ReceiptCandidateProposal,
  type ReceiptSource,
  type ReceiptSourcePreviewOut,
  type ReceiptConfirmationPreviewOut,
} from "@/components/personal-finance/personal-finance-api";
import { FinanceReceiptSource } from "./finance-receipt-source";
import { displayMinorUnitsRaw, maskDigest, maskReference } from "./finance-receipt-dto";

type CandidateState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "empty" }
  | { kind: "unavailable"; message: string }
  | { kind: "ready"; candidate: ReceiptCandidate; preview: ReceiptSourcePreviewOut | null; previewError: string | null };

type ReceiptRowInput = Readonly<{
  occurred_on: string;
  kind: "income" | "expense";
  amount_minor: number;
  currency: string;
  account_alias: string;
  category: string | null;
}>;

type PendingProposal = Readonly<{
  ownerKey: string;
  source: ReceiptSource;
  body: ReceiptCandidateProposal;
}>;

type PendingConfirmation = Readonly<{
  ownerKey: string;
  candidateRef: string;
  sourceRef: string;
  rowKey: string;
  body: ReceiptCandidateConfirmation;
}>;

const MAX_AMOUNT_MINOR = 9_007_199_254_740_991;

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "操作を完了できませんでした";
}

function fieldValue(candidate: ReceiptCandidate, name: string): unknown {
  const field = candidate.fields[name];
  return field && typeof field === "object" ? (field as Record<string, unknown>).value : undefined;
}

function initialRow(candidate: ReceiptCandidate): ReceiptRowInput {
  const amount = fieldValue(candidate, "amount_minor");
  return {
    occurred_on: typeof fieldValue(candidate, "occurred_on") === "string" ? String(fieldValue(candidate, "occurred_on")) : "",
    kind: "expense",
    amount_minor: typeof amount === "number" && Number.isSafeInteger(amount) ? amount : 0,
    currency: typeof fieldValue(candidate, "currency") === "string" ? String(fieldValue(candidate, "currency")) : "JPY",
    account_alias: "",
    category: null,
  };
}

function rowKey(row: ReceiptRowInput): string {
  return JSON.stringify(row);
}

function isValidRow(row: ReceiptRowInput): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(row.occurred_on)
    && (row.kind === "income" || row.kind === "expense")
    && Number.isSafeInteger(row.amount_minor) && row.amount_minor > 0 && row.amount_minor <= MAX_AMOUNT_MINOR
    && /^[A-Z]{3}$/.test(row.currency)
    && row.account_alias.trim().length > 0 && row.account_alias.trim().length <= 64
    && (row.category === null || row.category.length <= 80);
}

export function FinanceReceiptReview() {
  const ownerId = useCurrentUserId();
  const ownerKey = ownerId?.trim() || "";
  const [sources, setSources] = useState<ReceiptSource[] | null>(null);
  const [selectedRef, setSelectedRef] = useState("");
  const [review, setReview] = useState<CandidateState>({ kind: "idle" });
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingProposal, setPendingProposal] = useState<PendingProposal | null>(null);
  const [pendingConfirmation, setPendingConfirmation] = useState<PendingConfirmation | null>(null);
  const [row, setRow] = useState<ReceiptRowInput | null>(null);
  const [confirmationPreview, setConfirmationPreview] = useState<ReceiptConfirmationPreviewOut | null>(null);
  const [previewRowKey, setPreviewRowKey] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [duplicateConfirmed, setDuplicateConfirmed] = useState(false);
  const epoch = useRef(0);
  const inFlight = useRef(false);
  const ownerRef = useRef(ownerKey);
  const pendingProposalRef = useRef<PendingProposal | null>(null);
  const pendingConfirmationRef = useRef<PendingConfirmation | null>(null);
  const radioName = useId();
  const checkboxId = useId();
  const duplicateCheckboxId = useId();

  useLayoutEffect(() => {
    if (ownerRef.current !== ownerKey) {
      ownerRef.current = ownerKey;
      epoch.current += 1;
      setSources(null);
      setSelectedRef("");
      setReview({ kind: "idle" });
      setRow(null);
      setConfirmationPreview(null);
      setPreviewRowKey("");
      setConfirmed(false);
      setDuplicateConfirmed(false);
      setNotice("");
    }
  }, [ownerKey]);

  useEffect(() => {
    const requestEpoch = ++epoch.current;
    const controller = new AbortController();
    if (!ownerKey) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- an unauthenticated owner has no source list to retain.
      setSources([]);
      setNotice("ログイン状態を確認してください");
      return () => controller.abort();
    }
    setSources(null);
    setNotice("");
    void personalFinanceApi.receiptSources(controller.signal).then((items) => {
      if (requestEpoch !== epoch.current || ownerRef.current !== ownerKey) return;
      setSources(items);
      if (items.length === 0) setNotice("本人用の画像資料がありません");
    }).catch((error: unknown) => {
      if (controller.signal.aborted || requestEpoch !== epoch.current || ownerRef.current !== ownerKey) return;
      setSources([]);
      setNotice(errorMessage(error));
    });
    return () => controller.abort();
  }, [ownerKey]);

  async function loadCandidate(source: ReceiptSource, requestEpoch: number) {
    try {
      const candidates = await personalFinanceApi.receiptCandidates(source.source_ref);
      if (requestEpoch !== epoch.current || ownerRef.current !== ownerKey) return;
      const candidate = candidates[0];
      if (!candidate) {
        setReview({ kind: "empty" });
        setRow(null);
        return;
      }
      if (candidate.source.source_ref !== source.source_ref || candidate.source.source_sha256 !== source.source_sha256) {
        setReview({ kind: "unavailable", message: "候補の資料が選択した資料と一致しません" });
        return;
      }
      const fresh = await personalFinanceApi.receiptCandidate(candidate.candidate_ref);
      if (requestEpoch !== epoch.current || ownerRef.current !== ownerKey) return;
      let preview: ReceiptSourcePreviewOut | null = null;
      let previewError: string | null = null;
      try {
        preview = await personalFinanceApi.receiptPreview(candidate.candidate_ref);
      } catch (error) {
        previewError = errorMessage(error);
      }
      if (requestEpoch !== epoch.current || ownerRef.current !== ownerKey) return;
      if (fresh.source.source_ref !== source.source_ref || fresh.source.source_sha256 !== source.source_sha256) {
        setReview({ kind: "unavailable", message: "候補の資料が選択した資料と一致しません" });
        return;
      }
      setReview({ kind: "ready", candidate: fresh, preview, previewError });
      setRow(initialRow(fresh));
      setConfirmationPreview(null);
      setPreviewRowKey("");
      setConfirmed(false);
      setDuplicateConfirmed(false);
    } catch (error) {
      if (requestEpoch !== epoch.current || ownerRef.current !== ownerKey) return;
      setReview({ kind: "unavailable", message: errorMessage(error) });
    }
  }

  function selectSource(source: ReceiptSource) {
    if (pendingProposalRef.current || pendingConfirmationRef.current) return;
    const requestEpoch = ++epoch.current;
    setSelectedRef(source.source_ref);
    setNotice("");
    setReview({ kind: "loading" });
    setRow(null);
    setConfirmationPreview(null);
    setPreviewRowKey("");
    setConfirmed(false);
    setDuplicateConfirmed(false);
    void loadCandidate(source, requestEpoch);
  }

  async function runProposal(operation: PendingProposal) {
    if (busy || inFlight.current || ownerRef.current !== operation.ownerKey) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const result = await personalFinanceApi.proposeReceipt(operation.body);
      if (ownerRef.current !== operation.ownerKey || pendingProposalRef.current !== operation) return;
      pendingProposalRef.current = null;
      setPendingProposal(null);
      if (result.status !== "candidate_needs_review" || !result.candidate_ref) {
        setNotice(result.status === "processor_unavailable"
          ? "ローカルOCR処理が未配備のため、候補は作成されていません。"
          : result.status === "no_candidate"
            ? "資料から候補を作れませんでした。本人入力で記録できます。"
            : `候補を作成できませんでした（${result.status}）。`);
        return;
      }
      const requestEpoch = ++epoch.current;
      setReview({ kind: "loading" });
      await loadCandidate(operation.source, requestEpoch);
    } catch (error) {
      if (ownerRef.current !== operation.ownerKey || pendingProposalRef.current !== operation) return;
      if (isAmbiguousFinanceWrite(error)) {
        setNotice("候補作成の結果が不明です。元の資料と同じ冪等キーで照合してください。");
      } else {
        pendingProposalRef.current = null;
        setPendingProposal(null);
        setNotice(errorMessage(error));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function proposeCandidate() {
    if (!ownerKey || !selectedRef || busy || pendingProposalRef.current) return;
    const source = sources?.find((item) => item.source_ref === selectedRef);
    if (!source) return;
    const operation: PendingProposal = Object.freeze({
      ownerKey,
      source,
      body: Object.freeze({
        source_ref: source.source_ref,
        source_sha256: source.source_sha256,
        idempotency_key: crypto.randomUUID(),
      }),
    });
    pendingProposalRef.current = operation;
    setPendingProposal(operation);
    void runProposal(operation);
  }

  async function retryProposal() {
    const operation = pendingProposalRef.current;
    if (!operation || operation.ownerKey !== ownerKey || busy) return;
    await runProposal(operation);
  }

  function changeRow(patch: Partial<ReceiptRowInput>) {
    setRow((previous) => previous ? { ...previous, ...patch } : previous);
    setConfirmationPreview(null);
    setPreviewRowKey("");
    setConfirmed(false);
    setDuplicateConfirmed(false);
  }

  async function prepareConfirmation() {
    if (review.kind !== "ready" || !row || !isValidRow(row) || busy || pendingConfirmationRef.current) return;
    setBusy(true);
    const rowIdentity = rowKey(row);
    try {
      const result = await personalFinanceApi.previewReceiptConfirmation(review.candidate.candidate_ref, {
        revision: review.candidate.revision,
        candidate_digest: review.candidate.confirmed_digest,
        row,
      });
      if (ownerRef.current !== ownerKey || selectedRef !== review.candidate.source.source_ref) return;
      setConfirmationPreview(result);
      setPreviewRowKey(rowIdentity);
      setConfirmed(false);
      setDuplicateConfirmed(false);
      setNotice("");
    } catch (error) {
      if (ownerRef.current === ownerKey) setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function runConfirmation(operation: PendingConfirmation) {
    if (busy || inFlight.current || ownerRef.current !== operation.ownerKey) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const result = await personalFinanceApi.confirmReceipt(operation.candidateRef, operation.body);
      if (ownerRef.current !== operation.ownerKey || pendingConfirmationRef.current !== operation) return;
      if (result.candidate_ref !== operation.candidateRef || result.status !== "accepted") {
        throw new Error("確定結果を照合できませんでした");
      }
      pendingConfirmationRef.current = null;
      setPendingConfirmation(null);
      setConfirmed(false);
      setDuplicateConfirmed(false);
      setNotice(result.replayed ? "同じ操作の確定済み結果を照合しました。" : "本人確認を受け付けて finance entry に記録しました。");
      const source = sources?.find((item) => item.source_ref === operation.sourceRef);
      if (source) {
        const requestEpoch = ++epoch.current;
        await loadCandidate(source, requestEpoch);
      }
    } catch (error) {
      if (ownerRef.current !== operation.ownerKey || pendingConfirmationRef.current !== operation) return;
      if (isAmbiguousFinanceWrite(error)) {
        setNotice("確定結果が不明です。同じ内容・同じ冪等キーで照合してください。");
      } else {
        pendingConfirmationRef.current = null;
        setPendingConfirmation(null);
        setConfirmed(false);
        setNotice(errorMessage(error));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function confirmCandidate() {
    if (review.kind !== "ready" || !row || !confirmationPreview || busy || pendingConfirmationRef.current) return;
    if (previewRowKey !== rowKey(row) || !confirmed) return;
    if (confirmationPreview.possible_duplicate && !duplicateConfirmed) return;
    const body: ReceiptCandidateConfirmation = Object.freeze({
      revision: review.candidate.revision,
      candidate_digest: review.candidate.confirmed_digest,
      row,
      confirmed_digest: confirmationPreview.confirmed_digest,
      duplicate_confirmed: duplicateConfirmed,
      owner_confirmed: true,
      idempotency_key: crypto.randomUUID(),
    });
    const operation: PendingConfirmation = Object.freeze({
      ownerKey,
      candidateRef: review.candidate.candidate_ref,
      sourceRef: review.candidate.source.source_ref,
      rowKey: rowKey(row),
      body,
    });
    pendingConfirmationRef.current = operation;
    setPendingConfirmation(operation);
    setNotice("");
    void runConfirmation(operation);
  }

  async function retryConfirmation() {
    const operation = pendingConfirmationRef.current;
    if (!operation || operation.ownerKey !== ownerKey || busy) return;
    await runConfirmation(operation);
  }

  const locked = busy || pendingProposal !== null || pendingConfirmation !== null;
  const selectedSource = sources?.find((item) => item.source_ref === selectedRef) ?? null;
  const usablePreview = !!row && !!confirmationPreview && previewRowKey === rowKey(row);

  return (
    <section data-testid="finance-receipt-review" className="space-y-4 rounded-lg border p-4">
      <header>
        <h2 className="text-base font-semibold">領収書候補の確認</h2>
        <p className="text-xs text-muted-foreground">候補の作成と finance entry の記帳は分かれています。記帳前に内容を確認してください。</p>
      </header>

      {!ownerKey && <p role="alert">ログイン状態を確認してください。</p>}
      <fieldset className="space-y-2" disabled={locked || !ownerKey}>
        <legend className="text-sm font-medium">本人用の資料を選択</legend>
        {sources === null ? <p data-testid="sources-loading">資料一覧を読み込み中…</p> : null}
        {sources && sources.length === 0 ? <p>確認できる画像資料がありません。</p> : null}
        <ul className="space-y-1">
          {(sources ?? []).map((source, index) => (
            <li key={source.source_ref}>
              <label className="flex min-h-10 items-center gap-2">
                <input
                  type="radio" name={radioName} value={source.source_ref}
                  checked={selectedRef === source.source_ref}
                  onChange={() => selectSource(source)}
                />
                <span data-testid={`source-option-${index}`}>資料{index + 1}・{maskReference(source.source_ref)}・{source.mime_type}</span>
              </label>
            </li>
          ))}
        </ul>
        {selectedRef && (
          <Button type="button" variant="outline" disabled={locked} onClick={() => {
            epoch.current += 1;
            setSelectedRef("");
            setReview({ kind: "idle" });
            setRow(null);
            setConfirmationPreview(null);
            setPreviewRowKey("");
            setConfirmed(false);
            setDuplicateConfirmed(false);
            setNotice("");
          }}>資料選択を取り消す</Button>
        )}
      </fieldset>

      {notice && <p role="status" aria-live="polite">{notice}</p>}
      {review.kind === "loading" && <p data-testid="candidate-loading">候補を取得中…</p>}
      {review.kind === "empty" && selectedSource && (
        <section className="space-y-2 rounded border p-3">
          <p data-testid="candidate-empty">この資料の候補はありません。</p>
          <Button type="button" disabled={locked} onClick={proposeCandidate}>この資料から候補を作る</Button>
        </section>
      )}
      {review.kind === "unavailable" && <p role="alert" data-testid="candidate-error">{review.message}</p>}
      {pendingProposal && (
        <section aria-label="候補作成結果の照合" className="rounded border border-amber-300 p-3">
          <p>{pendingProposal.ownerKey === ownerKey ? "候補作成の結果が不明です。" : "別のログインスコープで保留中の候補作成があります。元のアカウントに戻ると同じ操作で照合できます。"}</p>
          <Button type="button" disabled={busy || pendingProposal.ownerKey !== ownerKey} onClick={() => void retryProposal()}>
            同じ資料・同じキーで照合
          </Button>
        </section>
      )}

      {review.kind === "ready" && (
        <div className="space-y-3">
          <FinanceReceiptSource source={review.candidate.source} preview={review.preview} previewError={review.previewError} />
          <article aria-label="選択した候補" className="space-y-3 rounded border p-3">
            <header className="flex flex-wrap items-center gap-2">
              <h3 className="font-semibold">候補の内容</h3>
              <span data-testid="candidate-status">{review.candidate.status === "accepted" ? "finance entry 記帳済み" : "本人確認待ち"}</span>
              <span>revision {review.candidate.revision}</span>
              <span data-testid="candidate-digest">照合値 {maskDigest(review.candidate.confirmed_digest)}</span>
            </header>
            {review.candidate.needs_review.length > 0 && (
              <div data-testid="needs-review" className="rounded bg-amber-50 p-2 text-sm">
                <p className="font-medium">確認事項</p>
                <ul className="list-disc pl-5">{review.candidate.needs_review.map((item) => <li key={item}>{item}</li>)}</ul>
                <p>曖昧な候補はこの操作から記帳できません。個人財務の本人入力を使ってください。</p>
              </div>
            )}
            {review.candidate.status === "accepted" && review.candidate.accepted_row ? (
              <p data-testid="receipt-accepted">
                {review.candidate.accepted_row.occurred_on}・{review.candidate.accepted_row.kind === "income" ? "収入" : "支出"}・
                {displayMinorUnitsRaw(review.candidate.accepted_row.amount_minor, review.candidate.accepted_row.currency)}・
                {review.candidate.accepted_row.account_alias}
              </p>
            ) : review.candidate.status === "pending" && row ? (
              <div className="space-y-3">
                <fieldset className="grid gap-3 sm:grid-cols-2" disabled={locked}>
                  <label className="grid gap-1">日付
                    <input aria-label="日付" type="date" value={row.occurred_on} onChange={(event) => changeRow({ occurred_on: event.target.value })} />
                  </label>
                  <label className="grid gap-1">区分
                    {/* eslint-disable-next-line no-restricted-syntax -- preserve the compact native expense/income selector contract. */}
                    <select aria-label="区分" value={row.kind} onChange={(event) => changeRow({ kind: event.target.value as "income" | "expense" })}>
                      <option value="expense">支出</option><option value="income">収入</option>
                    </select>
                  </label>
                  <label className="grid gap-1">金額（最小単位の整数）
                    <input aria-label="金額（最小単位の整数）" inputMode="numeric" value={String(row.amount_minor || "")} onChange={(event) => changeRow({ amount_minor: /^\d+$/.test(event.target.value) ? Number(event.target.value) : 0 })} />
                  </label>
                  <label className="grid gap-1">通貨
                    <input aria-label="通貨" maxLength={3} value={row.currency} onChange={(event) => changeRow({ currency: event.target.value.toUpperCase() })} />
                  </label>
                  <label className="grid gap-1">口座の別名
                    <input aria-label="口座の別名" maxLength={64} value={row.account_alias} onChange={(event) => changeRow({ account_alias: event.target.value })} />
                  </label>
                  <label className="grid gap-1">分類
                    <input aria-label="分類" maxLength={80} value={row.category ?? ""} onChange={(event) => changeRow({ category: event.target.value || null })} />
                  </label>
                </fieldset>
                <Button type="button" variant="outline" disabled={locked || !isValidRow(row) || review.candidate.needs_review.length > 0} onClick={() => void prepareConfirmation()}>
                  候補・重複・資料状態を再確認
                </Button>
                {usablePreview && confirmationPreview && (
                  <section className="space-y-2 rounded border p-3" data-testid="confirmation-preview">
                    <p>本人確認する記帳内容</p>
                    <p>{confirmationPreview.row.occurred_on}・{confirmationPreview.row.kind === "income" ? "収入" : "支出"}・{displayMinorUnitsRaw(confirmationPreview.row.amount_minor, confirmationPreview.row.currency)}・{confirmationPreview.row.account_alias}・{confirmationPreview.row.category ?? "分類なし"}</p>
                    <p>確認digest: {maskDigest(confirmationPreview.confirmed_digest)}</p>
                    {confirmationPreview.possible_duplicate && (
                      <div className="space-y-2 rounded bg-amber-50 p-2" data-testid="duplicate-warning">
                        <p>既存の明細と重複する可能性があります。続行する場合は明示してください。</p>
                        {confirmationPreview.duplicate_scan_incomplete && <p>検索上限に達したため、重複を確認しきれていません。</p>}
                        <label htmlFor={duplicateCheckboxId} className="flex items-center gap-2">
                          {/* eslint-disable-next-line no-restricted-syntax -- preserve the native duplicate acknowledgement control and label contract. */}
                          <input id={duplicateCheckboxId} type="checkbox" checked={duplicateConfirmed} onChange={(event) => setDuplicateConfirmed(event.target.checked)} />
                          重複の可能性を確認して続行する
                        </label>
                      </div>
                    )}
                    <label htmlFor={checkboxId} className="flex items-center gap-2">
                      {/* eslint-disable-next-line no-restricted-syntax -- preserve the native owner confirmation control and label contract. */}
                      <input id={checkboxId} type="checkbox" checked={confirmed} disabled={locked} onChange={(event) => setConfirmed(event.target.checked)} />
                      資料と表示中の全項目を確認しました
                    </label>
                    <Button type="button" data-testid="confirm-candidate" disabled={locked || !confirmed || (confirmationPreview.possible_duplicate && !duplicateConfirmed)} onClick={confirmCandidate}>
                      finance entry に記帳する
                    </Button>
                  </section>
                )}
              </div>
            ) : null}
          </article>
        </div>
      )}

      {pendingConfirmation && (
        <section aria-label="記帳結果の照合" data-testid="pending-confirmation" className="rounded border border-amber-300 p-3">
          <p>{pendingConfirmation.ownerKey === ownerKey ? "記帳結果が不明です。新しいキーを作らず同じ操作を再送してください。" : "別のログインスコープで記帳結果の照合が保留中です。元のアカウントに戻ると再照合できます。"}</p>
          {pendingConfirmation.ownerKey === ownerKey && (
            <p>候補 {maskReference(pendingConfirmation.candidateRef)}・キー {maskReference(pendingConfirmation.body.idempotency_key)}</p>
          )}
          <Button type="button" disabled={busy || pendingConfirmation.ownerKey !== ownerKey} onClick={() => void retryConfirmation()}>
            同じ内容・同じ冪等キーで照合
          </Button>
        </section>
      )}
    </section>
  );
}

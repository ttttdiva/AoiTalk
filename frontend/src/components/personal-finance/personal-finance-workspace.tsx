"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { FinanceReceiptReview } from "@/components/finance-receipts/finance-receipt-review";
import { PersonalAgentInvocationPanel } from "./personal-agent-invocation-panel";
import {
  isAmbiguousFinanceWrite,
  newFinanceIdempotencyKey,
  personalFinanceApi,
  type FinanceDatasetAccept,
  type FinanceDatasetOut,
  type FinanceEntry,
  type FinanceEntryCreate,
  type FinanceEntryPreviewResponse,
  type FinanceSummaryResponse,
} from "./personal-finance-api";

type EntryDraft = Readonly<{
  row_ref: string;
  occurred_on: string;
  kind: "income" | "expense";
  amount_minor: number;
  currency: string;
  account_alias: string;
  category: string | null;
}>;

type EntryOperation = Readonly<{
  ownerKey: string;
  body: FinanceEntryCreate;
}>;

type DatasetOperation = Readonly<{
  ownerKey: string;
  body: FinanceDatasetAccept;
}>;

const MAX_AMOUNT_MINOR = 9_007_199_254_740_991;

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "操作を完了できませんでした";
}

function validDraft(row: EntryDraft): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(row.occurred_on)
    && (row.kind === "income" || row.kind === "expense")
    && Number.isSafeInteger(row.amount_minor) && row.amount_minor > 0 && row.amount_minor <= MAX_AMOUNT_MINOR
    && /^[A-Z]{3}$/.test(row.currency)
    && row.account_alias.trim().length > 0 && row.account_alias.trim().length <= 64
    && (row.category === null || row.category.length <= 80);
}

export function PersonalFinanceWorkspace() {
  const currentUserId = useCurrentUserId();
  const ownerKey = currentUserId?.trim() || "";
  const [entries, setEntries] = useState<FinanceEntry[]>([]);
  const [datasets, setDatasets] = useState<FinanceDatasetOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [draft, setDraft] = useState<EntryDraft>({
    row_ref: "", occurred_on: "", kind: "expense", amount_minor: 0,
    currency: "JPY", account_alias: "", category: null,
  });
  const [preview, setPreview] = useState<FinanceEntryPreviewResponse | null>(null);
  const [previewRowKey, setPreviewRowKey] = useState("");
  const [entryConfirmed, setEntryConfirmed] = useState(false);
  const [pendingEntry, setPendingEntry] = useState<EntryOperation | null>(null);
  const [selectedRows, setSelectedRows] = useState<string[]>([]);
  const [datasetConfirmed, setDatasetConfirmed] = useState(false);
  const [pendingDataset, setPendingDataset] = useState<DatasetOperation | null>(null);
  const [selectedDatasetRef, setSelectedDatasetRef] = useState("");
  const [month, setMonth] = useState("");
  const [summary, setSummary] = useState<FinanceSummaryResponse | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const entryPendingRef = useRef<EntryOperation | null>(null);
  const datasetPendingRef = useRef<DatasetOperation | null>(null);
  const listEpoch = useRef(0);
  const summaryEpoch = useRef(0);
  const inFlight = useRef(false);
  const ownerRef = useRef(ownerKey);
  const entryConfirmId = useId();
  const datasetConfirmId = useId();

  async function reloadLists(scope: string, epoch: number, signal?: AbortSignal) {
    if (!scope) return;
    const [entryPage, datasetPage] = await Promise.all([
      personalFinanceApi.entries(signal),
      personalFinanceApi.datasets(signal),
    ]);
    if (epoch !== listEpoch.current || ownerRef.current !== scope) return;
    setEntries(entryPage.entries);
    setDatasets(datasetPage.datasets);
  }

  // Clear owner-scoped private state before the owner transition can paint.
  useLayoutEffect(() => {
    if (ownerRef.current !== ownerKey) {
      ownerRef.current = ownerKey;
      setEntries([]);
      setDatasets([]);
      setSelectedRows([]);
      setSelectedDatasetRef("");
      setSummary(null);
      setDraft({
        row_ref: "", occurred_on: "", kind: "expense", amount_minor: 0,
        currency: "JPY", account_alias: "", category: null,
      });
      setMonth("");
      setPreview(null);
      setPreviewRowKey("");
      setEntryConfirmed(false);
      setDatasetConfirmed(false);
      setMessage("");
    }
    const epoch = ++listEpoch.current;
    const controller = new AbortController();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- owner changes must clear the old list before the new request resolves.
    setLoading(true);
    if (!ownerKey) {
      setLoading(false);
      setMessage("ログイン状態を確認してください");
      return () => controller.abort();
    }
    void reloadLists(ownerKey, epoch, controller.signal).catch((error: unknown) => {
      if (controller.signal.aborted || epoch !== listEpoch.current || ownerRef.current !== ownerKey) return;
      setMessage(errorMessage(error));
    }).finally(() => {
      if (!controller.signal.aborted && epoch === listEpoch.current && ownerRef.current === ownerKey) setLoading(false);
    });
    return () => controller.abort();
  }, [ownerKey]);

  useEffect(() => {
    const epoch = ++summaryEpoch.current;
    const controller = new AbortController();
    if (!ownerKey || !selectedDatasetRef || !/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- changing the selected dataset/month invalidates the prior summary immediately.
      setSummary(null);
      setSummaryLoading(false);
      return () => controller.abort();
    }
    setSummaryLoading(true);
    void personalFinanceApi.summary(selectedDatasetRef, month, controller.signal).then((result) => {
      if (!controller.signal.aborted && epoch === summaryEpoch.current && ownerRef.current === ownerKey) setSummary(result);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted && epoch === summaryEpoch.current && ownerRef.current === ownerKey) {
        setSummary(null);
        setMessage(errorMessage(error));
      }
    }).finally(() => {
      if (!controller.signal.aborted && epoch === summaryEpoch.current && ownerRef.current === ownerKey) setSummaryLoading(false);
    });
    return () => controller.abort();
  }, [ownerKey, selectedDatasetRef, month]);

  function changeDraft(patch: Partial<EntryDraft>) {
    setDraft((previous) => ({ ...previous, ...patch }));
    setPreview(null);
    setPreviewRowKey("");
    setEntryConfirmed(false);
  }

  async function prepareEntry() {
    if (!ownerKey || busy || !validDraft(draft)) return;
    const row = { ...draft, row_ref: draft.row_ref || crypto.randomUUID() };
    setDraft(row);
    setBusy(true);
    try {
      const result = await personalFinanceApi.previewEntry({ row, source: null });
      if (ownerRef.current !== ownerKey) return;
      setPreview(result);
      setPreviewRowKey(JSON.stringify(row));
      setEntryConfirmed(false);
      setMessage("");
    } catch (error) {
      if (ownerRef.current === ownerKey) setMessage(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function runEntry(operation: EntryOperation) {
    if (busy || inFlight.current || ownerRef.current !== operation.ownerKey) return;
    inFlight.current = true;
    setBusy(true);
    try {
      await personalFinanceApi.createEntry(operation.body);
      if (ownerRef.current !== operation.ownerKey || entryPendingRef.current !== operation) return;
      entryPendingRef.current = null;
      setPendingEntry(null);
      setMessage("本人確認を受け付け、finance entry に記録しました。");
      setDraft({ row_ref: "", occurred_on: "", kind: "expense", amount_minor: 0, currency: "JPY", account_alias: "", category: null });
      setPreview(null);
      setPreviewRowKey("");
      setEntryConfirmed(false);
      await reloadLists(operation.ownerKey, ++listEpoch.current);
    } catch (error) {
      if (ownerRef.current !== operation.ownerKey || entryPendingRef.current !== operation) return;
      if (isAmbiguousFinanceWrite(error)) setMessage("明細の記録結果が不明です。同じ内容・同じキーで照合してください。");
      else {
        entryPendingRef.current = null;
        setPendingEntry(null);
        setPreview(null);
        setEntryConfirmed(false);
        setMessage(errorMessage(error));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function submitEntry() {
    if (!ownerKey || !preview || !entryConfirmed || busy || entryPendingRef.current || previewRowKey !== JSON.stringify(draft)) return;
    const operation: EntryOperation = Object.freeze({
      ownerKey,
      body: Object.freeze({
        row: preview.row,
        source: null,
        confirmed_digest: preview.confirmed_digest,
        owner_confirmed: true,
        idempotency_key: newFinanceIdempotencyKey(),
      }),
    });
    entryPendingRef.current = operation;
    setPendingEntry(operation);
    void runEntry(operation);
  }

  async function retryEntry() {
    const operation = entryPendingRef.current;
    if (!operation || operation.ownerKey !== ownerKey || busy) return;
    await runEntry(operation);
  }

  function toggleRow(rowRef: string) {
    if (busy || pendingDataset) return;
    setSelectedRows((previous) => previous.includes(rowRef) ? previous.filter((item) => item !== rowRef) : [...previous, rowRef]);
    setDatasetConfirmed(false);
  }

  async function runDataset(operation: DatasetOperation) {
    if (busy || inFlight.current || ownerRef.current !== operation.ownerKey) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const accepted = await personalFinanceApi.acceptDataset(operation.body);
      if (ownerRef.current !== operation.ownerKey || datasetPendingRef.current !== operation) return;
      datasetPendingRef.current = null;
      setPendingDataset(null);
      setSelectedDatasetRef(accepted.dataset_ref);
      setDatasetConfirmed(false);
      setSelectedRows([]);
      setMessage(accepted.complete ? "選択した明細を accepted dataset に固定しました。" : "accepted dataset を作成しました（全明細の一部です）。");
      await reloadLists(operation.ownerKey, ++listEpoch.current);
    } catch (error) {
      if (ownerRef.current !== operation.ownerKey || datasetPendingRef.current !== operation) return;
      if (isAmbiguousFinanceWrite(error)) setMessage("dataset の確定結果が不明です。同じ選択・同じキーで照合してください。");
      else {
        datasetPendingRef.current = null;
        setPendingDataset(null);
        setDatasetConfirmed(false);
        setMessage(errorMessage(error));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function acceptSelectedDataset() {
    if (!ownerKey || !selectedRows.length || !datasetConfirmed || busy || datasetPendingRef.current) return;
    const body: FinanceDatasetAccept = Object.freeze({
      row_refs: [...selectedRows].sort(),
      owner_confirmed: true,
      idempotency_key: newFinanceIdempotencyKey(),
    });
    const operation: DatasetOperation = Object.freeze({ ownerKey, body });
    datasetPendingRef.current = operation;
    setPendingDataset(operation);
    void runDataset(operation);
  }

  async function retryDataset() {
    const operation = datasetPendingRef.current;
    if (!operation || operation.ownerKey !== ownerKey || busy) return;
    await runDataset(operation);
  }

  const selectedRowsCount = selectedRows.length;
  const entryPreviewCurrent = !!preview && previewRowKey === JSON.stringify(draft);

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-4 text-sm" data-testid="personal-finance-workspace">
      <header><h1 className="text-xl font-semibold">個人財務</h1></header>
      {message && <p role="status" aria-live="polite">{message}</p>}

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="finance-entry-title">
        <h2 id="finance-entry-title" className="font-semibold">本人確認済み明細</h2>
        <fieldset className="grid gap-3 sm:grid-cols-2" disabled={busy || !ownerKey || pendingEntry !== null}>
          <label className="grid gap-1">日付
            <input type="date" aria-label="明細の日付" value={draft.occurred_on} onChange={(event) => changeDraft({ occurred_on: event.target.value })} />
          </label>
          <label className="grid gap-1">区分
            {/* eslint-disable-next-line no-restricted-syntax -- the compact owner-only entry form preserves native keyboard/form behavior. */}
            <select aria-label="明細の区分" value={draft.kind} onChange={(event) => changeDraft({ kind: event.target.value as EntryDraft["kind"] })}>
              <option value="expense">支出</option><option value="income">収入</option>
            </select>
          </label>
          <label className="grid gap-1">金額（最小単位の整数）
            <input aria-label="明細の金額" inputMode="numeric" value={draft.amount_minor ? String(draft.amount_minor) : ""} onChange={(event) => changeDraft({ amount_minor: /^\d+$/.test(event.target.value) ? Number(event.target.value) : 0 })} />
          </label>
          <label className="grid gap-1">通貨
            <input aria-label="明細の通貨" maxLength={3} value={draft.currency} onChange={(event) => changeDraft({ currency: event.target.value.toUpperCase() })} />
          </label>
          <label className="grid gap-1">口座の別名
            <input aria-label="明細の口座別名" maxLength={64} value={draft.account_alias} onChange={(event) => changeDraft({ account_alias: event.target.value })} />
          </label>
          <label className="grid gap-1">分類
            <input aria-label="明細の分類" maxLength={80} value={draft.category ?? ""} onChange={(event) => changeDraft({ category: event.target.value || null })} />
          </label>
        </fieldset>
        <Button type="button" variant="outline" disabled={busy || !validDraft(draft) || pendingEntry !== null} onClick={() => void prepareEntry()}>
          入力内容のdigestを確認
        </Button>
        {entryPreviewCurrent && preview && (
          <section className="space-y-2 rounded border p-3" data-testid="finance-entry-preview">
            <p>{preview.row.occurred_on as string}・{preview.row.kind as string}・{preview.row.amount_minor as number} {preview.row.currency as string}・{preview.row.account_alias as string}</p>
            <p className="break-all font-mono">確認digest: {preview.confirmed_digest}</p>
            <label htmlFor={entryConfirmId} className="flex items-center gap-2">
              {/* eslint-disable-next-line no-restricted-syntax -- existing compact confirmation field; preserve its stable label contract. */}
              <input id={entryConfirmId} type="checkbox" checked={entryConfirmed} disabled={busy} onChange={(event) => setEntryConfirmed(event.target.checked)} />
              表示内容を本人が確認しました
            </label>
            <Button type="button" disabled={busy || !entryConfirmed} onClick={submitEntry}>この明細を記録</Button>
          </section>
        )}
      </section>

      {pendingEntry && (
        <section aria-label="明細結果の照合" className="rounded border border-amber-300 p-3">
          <p>{pendingEntry.ownerKey === ownerKey ? "明細の記録結果が不明です。" : "別ログインで保留中です。元のアカウントに戻ると同じ操作で照合できます。"}</p>
          <Button type="button" disabled={busy || pendingEntry.ownerKey !== ownerKey} onClick={() => void retryEntry()}>同じ内容・同じキーで照合</Button>
        </section>
      )}

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="finance-entries-title">
        <h2 id="finance-entries-title" className="font-semibold">記録済み明細</h2>
        {loading && <p>読み込み中…</p>}
        {!loading && entries.length === 0 && <p>明細はありません。</p>}
        <ul className="space-y-2">
          {entries.map((entry) => (
            <li key={entry.row_ref} className="flex items-start gap-2 rounded border p-2">
              {/* eslint-disable-next-line no-restricted-syntax -- existing row selection field; preserve its stable label contract. */}
              <input aria-label={`${entry.occurred_on} ${entry.amount_minor}をdatasetに選択`} type="checkbox" checked={selectedRows.includes(entry.row_ref)} disabled={busy || pendingDataset !== null} onChange={() => toggleRow(entry.row_ref)} />
              <span>{entry.occurred_on}・{entry.kind === "income" ? "収入" : "支出"}・{entry.amount_minor} {entry.currency}・{entry.account_alias}{entry.category ? `・${entry.category}` : ""}</span>
            </li>
          ))}
        </ul>
        {selectedRowsCount > 0 && (
          <div className="space-y-2 rounded border p-3">
            <p>{selectedRowsCount}件を accepted dataset として固定します。</p>
            <label htmlFor={datasetConfirmId} className="flex items-center gap-2">
              {/* eslint-disable-next-line no-restricted-syntax -- existing compact confirmation field; preserve its stable label contract. */}
              <input id={datasetConfirmId} type="checkbox" checked={datasetConfirmed} disabled={busy} onChange={(event) => setDatasetConfirmed(event.target.checked)} />
              選択した明細を確認しました
            </label>
            <Button type="button" disabled={busy || !datasetConfirmed} onClick={acceptSelectedDataset}>この選択をdatasetに固定</Button>
          </div>
        )}
      </section>

      {pendingDataset && (
        <section aria-label="dataset結果の照合" className="rounded border border-amber-300 p-3">
          <p>{pendingDataset.ownerKey === ownerKey ? "dataset の結果が不明です。" : "別ログインで保留中です。元のアカウントに戻ると同じ選択で照合できます。"}</p>
          <Button type="button" disabled={busy || pendingDataset.ownerKey !== ownerKey} onClick={() => void retryDataset()}>同じ選択・同じキーで照合</Button>
        </section>
      )}

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="finance-summary-title">
        <h2 id="finance-summary-title" className="font-semibold">accepted dataset のsummary</h2>
        <label className="grid max-w-xs gap-1">dataset
          {/* eslint-disable-next-line no-restricted-syntax -- the compact owner-only summary picker preserves native keyboard/form behavior. */}
          <select aria-label="summary対象dataset" value={selectedDatasetRef} disabled={busy || pendingDataset !== null} onChange={(event) => setSelectedDatasetRef(event.target.value)}>
            <option value="">選択してください</option>
            {datasets.map((dataset) => <option key={dataset.dataset_ref} value={dataset.dataset_ref}>{dataset.member_count}件・{dataset.complete ? "全明細" : "一部"}・{dataset.dataset_ref.slice(-6)}</option>)}
          </select>
        </label>
        <label className="grid max-w-xs gap-1">対象月
          <input aria-label="summary対象月" type="month" value={month} onChange={(event) => setMonth(event.target.value)} />
        </label>
        {summaryLoading && <p>summary を計算中…</p>}
        {summary && (
          <div data-testid="finance-summary">
            <p className="break-all font-mono">snapshot digest: {summary.snapshot_digest}</p>
            <ul>{summary.totals.map((total) => <li key={total.currency}>{total.currency}・収入 {total.income_minor}・支出 {total.expense_minor}・{total.record_count}件</li>)}</ul>
            {summary.duplicate_candidates.length > 0 && <p>重複候補: {summary.duplicate_candidates.join("、")}</p>}
            {summary.needs_review.map((reason) => <p key={reason}>要確認: {reason}</p>)}
          </div>
        )}
      </section>

      <PersonalAgentInvocationPanel
        title="財務Agentで本人が選んだ明細を読む"
        profileId="personal-finance-reviewer"
        operation="finance.read_entry"
        actionLabel="選択した明細をpersonal Agent adapterで確認"
        invoke={personalFinanceApi.invokePersonalAgent}
        getArguments={() => ({ row_ref: selectedRows[0] })}
        canInvoke={selectedRows.length === 1}
        disabledReason={selectedRows.length === 1 ? undefined : "先に記録済み明細を1件だけ選択してください。"}
      />

      <PersonalAgentInvocationPanel
        title="財務Agentでaccepted datasetのsummaryを読む"
        profileId="personal-finance-reviewer"
        operation="finance.summary"
        actionLabel="選択したdatasetのsummaryをpersonal Agent adapterで確認"
        invoke={personalFinanceApi.invokePersonalAgent}
        getArguments={() => ({ accepted_dataset_ref: selectedDatasetRef, month })}
        canInvoke={Boolean(selectedDatasetRef && /^\d{4}-(0[1-9]|1[0-2])$/.test(month))}
        disabledReason={selectedDatasetRef && /^\d{4}-(0[1-9]|1[0-2])$/.test(month) ? undefined : "accepted dataset と対象月を選択してください。"}
      />

      <FinanceReceiptReview />
    </div>
  );
}

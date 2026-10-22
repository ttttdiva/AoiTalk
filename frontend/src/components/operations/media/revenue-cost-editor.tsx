"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  mediaOperationsApi,
  MediaOperationsApiError,
  type CharacterDashboardRevenueItem,
  type RevenueCostRecord,
  type RevenueCostRecordHistory,
  type RevenueCostRecordInput,
} from "@/lib/media-operations-api";
import { REVENUE_COST_SCOPE_NOTE, revenueCurrency } from "@/lib/media-revenue-display";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { RevenueCostSummary } from "./revenue-cost-summary";
import { RevenueCostFields } from "./revenue-cost-fields";
import { COST_FIELDS, type CostForm, formFromCostRecord, costFieldsInput, newCostKey, costErrorText } from "@/lib/media-cost-input";

function formFromRecord(record: RevenueCostRecord | null, event: CharacterDashboardRevenueItem): CostForm {
  return formFromCostRecord(record, event.settlement_status === "settled" ? "settled" : "estimated");
}

function costInput(form: CostForm, event: CharacterDashboardRevenueItem, effective: RevenueCostRecord | null): RevenueCostRecordInput {
  const currency = revenueCurrency(event.currency);
  if (!currency) throw new Error("売上の通貨が不明なため、費用を記録できません");
  return { currency, ...costFieldsInput(form, effective?.evidence ?? [], { kind: "revenue_event", ref: event.id }, event.event_type !== "sale"), correction_of_id: effective?.id ?? null };
}

export function RevenueCostEditor({ event, onClose, onReadback }: {
  event: CharacterDashboardRevenueItem;
  onClose: () => void;
  onReadback: (record: RevenueCostRecord | null) => void;
}) {
  const [history, setHistory] = useState<RevenueCostRecordHistory | null>(null);
  const [form, setForm] = useState(() => formFromRecord(null, event));
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [readbackPending, setReadbackPending] = useState(false);
  const initialEvent = useRef(event).current;
  const mounted = useRef(false);
  const busy = useRef(false);
  const retry = useRef<{ body: string; key: string } | null>(null);
  const onReadbackRef = useRef(onReadback);
  useEffect(() => { onReadbackRef.current = onReadback; }, [onReadback]);

  useEffect(() => {
    mounted.current = true;
    let active = true;
    void mediaOperationsApi.getRevenueCostRecords(initialEvent.id).then((result) => {
      if (!active) return;
      setHistory(result);
      setForm(formFromRecord(result.effective, initialEvent));
      onReadbackRef.current(result.effective);
    }).catch((loadError) => {
      if (active) setError(costErrorText(loadError));
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; mounted.current = false; };
    // Parent keys the editor by event id, so another event starts a new session.
  }, [initialEvent]);

  async function reload() {
    if (busy.current) return;
    busy.current = true;
    setLoading(true);
    setError(null);
    try {
      const result = await mediaOperationsApi.getRevenueCostRecords(event.id);
      if (!mounted.current) return;
      setHistory(result);
      setForm(formFromRecord(result.effective, event));
      setConflict(false);
      setReadbackPending(false);
      retry.current = null;
      onReadbackRef.current(result.effective);
      setNotice("サーバーから費用記録を再読込しました");
    } catch (loadError) {
      if (mounted.current) setError(costErrorText(loadError));
    } finally {
      busy.current = false;
      if (mounted.current) setLoading(false);
    }
  }

  async function submit(submitEvent: FormEvent<HTMLFormElement>) {
    submitEvent.preventDefault();
    if (busy.current || !history || forbidden || conflict || readbackPending || history.effective?.cost_entry_id) return;
    let input: RevenueCostRecordInput;
    try { input = costInput(form, event, history.effective); }
    catch (validationError) { setError(costErrorText(validationError)); return; }
    busy.current = true;
    setSaving(true);
    setError(null);
    setNotice(null);
    const body = JSON.stringify(input);
    const pending = retry.current?.body === body ? retry.current : { body, key: newCostKey() };
    retry.current = pending;
    let saved = false;
    try {
      const created = await mediaOperationsApi.createRevenueCostRecord(event.id, input, pending.key);
      saved = true;
      const result = await mediaOperationsApi.getRevenueCostRecords(event.id);
      if (!mounted.current) return;
      if (!result.items.some((record) => record.id === created.id)) {
        throw new Error("保存した版が履歴にまだ見つかりません。再読込で確認してください");
      }
      setHistory(result);
      setForm(formFromRecord(result.effective, event));
      setReadbackPending(false);
      retry.current = null;
      onReadbackRef.current(result.effective);
      setNotice(`保存後の再読込を確認しました（保存版 ${created.revision}）`);
    } catch (saveError) {
      if (!mounted.current) return;
      if (saved) {
        setReadbackPending(true);
        setError(`保存は受け付けられましたが、再読込の確認に失敗しました。再読込してください。${costErrorText(saveError)}`);
      } else if (saveError instanceof MediaOperationsApiError && saveError.status === 409) {
        setConflict(true);
        setError("別の費用記録が先に保存されました。最新の履歴を再読込し、内容を確認して入力し直してください。現在の入力は再読込するまで保持されます。");
      } else if (saveError instanceof MediaOperationsApiError && [401, 403].includes(saveError.status)) {
        setForbidden(true);
        setError("費用を保存する権限がありません。ログイン状態・プロジェクト権限を確認してください。");
      } else {
        setError(`保存を確認できませんでした。同じ入力での再試行は同じ送信キーを使います。${costErrorText(saveError)}`);
      }
    } finally {
      busy.current = false;
      if (mounted.current) setSaving(false);
    }
  }

  const effective = history?.effective ?? null;
  const disabled = loading || saving || forbidden || conflict || readbackPending || !history || !!effective?.cost_entry_id || !revenueCurrency(event.currency);
  return (
    <section className="space-y-3 rounded-lg border border-primary/30 p-3" aria-label="売上ごとの費用記録">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h6 className="inline-flex items-center gap-1 text-sm font-semibold">費用記録 · {event.id}<span className="text-xs font-normal text-muted-foreground">対象日時: {event.event_at ?? "日時不明"}</span><HelpPopover label="費用記録"><p>直接帰属する費用だけを記録します。期間全体の費用は按分しません。</p><p>{REVENUE_COST_SCOPE_NOTE}</p></HelpPopover></h6>
        <div className="flex gap-2">
          <Button type="button" variant="outline" size="sm" disabled={loading || saving} onClick={() => void reload()}>最新の履歴を再読込</Button>
          <Button type="button" variant="ghost" size="sm" disabled={saving} onClick={onClose}>閉じる・キャンセル</Button>
        </div>
      </div>
      {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
      {notice ? <p role="status" className="text-sm">{notice}</p> : null}
      {loading ? <p role="status">費用記録を読み込み中…</p> : null}
      {history ? <>
        <div className="rounded-md bg-muted/30 p-2" data-testid="cost-readback">
          <p className="text-xs font-semibold">保存済みの現行版: {effective ? `第${effective.revision}版` : "記録なし"}</p>
          <RevenueCostSummary item={effective ?? {}} currency={event.currency} revenueAuthority={event.revenue_authority} />
        </div>
        {effective?.cost_entry_id ? <p className="text-xs">この費用は独立費用から配賦済みです。手入力での訂正・移動には対応していません。</p> : null}
        <form onSubmit={(e) => void submit(e)} aria-label="費用記録フォーム" className="space-y-3">
          <fieldset disabled={disabled} className="space-y-3">
            <legend className="text-xs">{effective ? "訂正を新しい版として追加" : "最初の費用記録を追加"} · 通貨 {revenueCurrency(event.currency) ?? "不明"}</legend>
            <RevenueCostFields form={form} setForm={setForm} allowNegative={event.event_type !== "sale"} lockSource={!!effective} />
            <Button type="submit" size="sm" disabled={disabled}>{saving ? "保存・再読込中…" : effective ? "訂正版を追加保存" : "費用記録を保存"}</Button>
          </fieldset>
        </form>
        <details><summary className="cursor-pointer text-xs">保存履歴（{history.items.length}件・過去の版は保持）</summary>
          {history.has_more ? <p className="text-xs">この一覧には古い履歴がさらにあります。</p> : null}
          <ol className="mt-2 space-y-2">{history.items.map((record) => <li key={record.id} className="rounded-md border p-2 text-xs">
            <p>第{record.revision}版 · {record.created_at} · {record.id === effective?.id ? "現行版" : "過去の版"}</p>
            <RevenueCostSummary item={record} currency={record.currency} revenueAuthority={event.revenue_authority} />
            <p>入力範囲: {record.coverage_complete ? "確認済み" : "未確認"} · {record.coverage_note}</p>
            {Object.entries(record.missing_reasons ?? {}).map(([key, reason]) => <p key={key}>{COST_FIELDS.find((field) => field.key === key)?.label ?? key} 未入力: {reason}</p>)}
            <p>証拠 {record.evidence.length}件 · {record.cost_hash}</p>
            {record.evidence.map((evidence, index) => <p key={index}>{evidence.label ?? evidence.kind}: {evidence.url ?? evidence.ref} {evidence.note}</p>)}
          </li>)}</ol>
        </details>
      </> : null}
    </section>
  );
}

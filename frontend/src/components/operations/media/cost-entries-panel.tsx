"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  mediaOperationsApi, MediaOperationsApiError,
  type CharacterDashboardRevenueItem, type MediaCharacterDashboard,
  type MediaCostEntry, type MediaCostEntryHistory, type MediaCostEntryInput,
  type RevenueCostRecord, type RevenueCostRecordHistory,
} from "@/lib/media-operations-api";
import { costErrorText, costFieldsInput, formFromCostRecord, newCostKey, COST_FIELDS } from "@/lib/media-cost-input";
import { revenueAmountText, revenueCurrency, revenueEvidenceClass } from "@/lib/media-revenue-display";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AppSelect } from "@/components/ui/app-select";
import { RevenueCostSummary } from "./revenue-cost-summary";
import { RevenueCostFields } from "./revenue-cost-fields";
import { HelpPopover } from "@/components/ui/help-popover";

type Props = {
  dashboard: MediaCharacterDashboard;
  revenueItems: CharacterDashboardRevenueItem[];
  onAllocated: (eventId: string, record: RevenueCostRecord | null) => void;
};
type EntryScope = { currency: string; start: string; end: string; contentRef: string };
const emptyScope = (): EntryScope => ({ currency: "", start: "", end: "", contentRef: "" });

function utcTime(value: string): number {
  // The backend serializes UTC-naive datetimes. Never interpret those as
  // the browser's local timezone when displaying or comparing cost windows.
  const normalized = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/u.test(value)
    ? `${value}Z` : value;
  return new Date(normalized).getTime();
}

function dateInput(value: string): string {
  const date = new Date(utcTime(value));
  return Number.isFinite(date.getTime()) ? date.toISOString().slice(0, 16) : "";
}
function utcDate(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/u.test(value)) throw new Error("費用対象期間をUTCで入力してください");
  const date = new Date(`${value}:00Z`);
  if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 16) !== value) throw new Error("費用対象期間を確認してください");
  return date.toISOString();
}
function eligible(entry: MediaCostEntry, event: CharacterDashboardRevenueItem): boolean {
  const eventTime = utcTime(event.event_at ?? "");
  return revenueCurrency(event.currency) === entry.currency &&
    event.settlement_status === entry.basis &&
    event.persona_ref === entry.persona_ref &&
    (!entry.content_ref || event.content_ref === entry.content_ref) &&
    Number.isFinite(eventTime) && eventTime >= utcTime(entry.window_start) && eventTime <= utcTime(entry.window_end);
}

function EntryReadback({ entry }: { entry: MediaCostEntry }) {
  return <div className="space-y-1 text-xs" data-testid={`cost-entry-readback-${entry.id}`}>
    <p className="font-medium">{entry.currency} · {entry.basis === "settled" ? "確定" : "推計"} · {entry.source === "manual" ? "自己申告" : "取込記録"} · 第{entry.revision}版</p>
    <p>{entry.window_start} 〜 {entry.window_end} · {entry.content_ref ? `コンテンツ ${entry.content_ref}` : "コンテンツ指定なし"}</p>
    <p>手数料 {revenueAmountText(entry.fee_amount)} · 制作原価 {revenueAmountText(entry.production_cost_amount)} · 人的費用 {revenueAmountText(entry.human_cost_amount)} · 作業時間 {revenueAmountText(entry.human_minutes)} 分</p>
    <p>未配賦残: 手数料 {revenueAmountText(entry.unallocated_fee_amount)} · 制作原価 {revenueAmountText(entry.unallocated_production_cost_amount)} · 人的費用 {revenueAmountText(entry.unallocated_human_cost_amount)} · 作業時間 {revenueAmountText(entry.unallocated_human_minutes)} 分</p>
    <p>{entry.allocation_status === "allocated" ? `配賦済み · 売上 ${entry.allocated_revenue_event_id}` : "未配賦"} · 入力範囲 {entry.coverage_complete ? "確認済み" : "未確認"}</p>
    <p>{entry.coverage_note}</p>
    {Object.entries(entry.missing_reasons ?? {}).map(([key, reason]) => <p key={key}>{COST_FIELDS.find((field) => field.key === key)?.label ?? key} 未入力: {reason}</p>)}
  </div>;
}

export function CostEntriesPanel(props: Props) {
  const [open, setOpen] = useState(false);
  const [working, setWorking] = useState(false);
  return <section className="mt-3 space-y-2 rounded-md border border-border/60 p-2.5" aria-label="売上前・未配賦費用">
    <div className="flex items-center justify-between gap-2"><h6 className="inline-flex items-center gap-1 text-xs font-semibold">売上前・未配賦費用<HelpPopover label="売上前・未配賦費用">売上がない段階の費用も保存できます。未配賦費用は売上の入力範囲の利益に含まれません。全額を1件の売上へ配賦でき、分割配賦には対応していません。全額配賦後の未配賦残は0ですが、元の費用の未入力は不明のまま保持されます。</HelpPopover></h6>
      <Button type="button" variant="outline" size="sm" disabled={working} onClick={() => setOpen(!open)}>{open ? "未配賦費用を閉じる" : "未配賦費用を開く"}</Button>
    </div>
    {open ? <CostEntriesWorkspace {...props} onBusyChange={setWorking} /> : null}
  </section>;
}

function CostEntriesWorkspace({ dashboard, revenueItems, onAllocated, onBusyChange }: Props & { onBusyChange: (busy: boolean) => void }) {
  const character = useRef(dashboard.character).current;
  const [entries, setEntries] = useState<MediaCostEntry[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [history, setHistory] = useState<MediaCostEntryHistory | null>(null);
  const [form, setForm] = useState(() => formFromCostRecord(null));
  const [scope, setScope] = useState(emptyScope);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [busyState, setBusyState] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [needsReload, setNeedsReload] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [allocationEntry, setAllocationEntry] = useState<MediaCostEntry | null>(null);
  const [allocationTarget, setAllocationTarget] = useState<{ event: CharacterDashboardRevenueItem; costs: RevenueCostRecordHistory } | null>(null);
  const alive = useRef(false);
  const busy = useRef(false);
  const pendingEntry = useRef<string | null>(null);
  const pendingAllocation = useRef<{ eventId: string; recordId: string } | null>(null);
  const retry = useRef<{ body: string; key: string } | null>(null);
  const effective = history?.effective ?? null;

  useEffect(() => {
    alive.current = true;
    let active = true;
    void mediaOperationsApi.listCostEntries(character.id, character.project_id).then((items) => {
      if (!active) return;
      setEntries(items); setHasMore(items.length === 100); setLoaded(true);
    }).catch((e) => { if (active) setError(costErrorText(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; alive.current = false; };
  }, [character]);

  function applyHistory(next: MediaCostEntryHistory) {
    setHistory(next);
    const entry = next.effective;
    setForm(formFromCostRecord(entry));
    setScope(entry ? { currency: entry.currency, start: dateInput(entry.window_start), end: dateInput(entry.window_end), contentRef: entry.content_ref ?? "" } : emptyScope());
  }
  function beginOperation(write = false) {
    if (busy.current) return false;
    busy.current = true; setBusyState(true); onBusyChange(write); setError(null); setNotice(null);
    return true;
  }
  function finishOperation() {
    busy.current = false;
    if (alive.current) { setBusyState(false); setLoading(false); onBusyChange(false); }
  }
  function operationError(e: unknown, accepted: boolean) {
    if (!alive.current) return;
    if (accepted) {
      setNeedsReload(true);
      setError(`保存は受け付けられましたが、再読込確認に失敗しました。一覧を再読込してください。${costErrorText(e)}`);
    } else if (e instanceof MediaOperationsApiError && e.status === 409) {
      setNeedsReload(true);
      setError("費用または売上の最新版が変わりました。一覧を再読込し、最新の内容を確認してください。");
    } else if (e instanceof MediaOperationsApiError && [401, 403].includes(e.status)) {
      setForbidden(true); setError("この費用を変更する権限がありません。");
    } else setError(`保存を確認できませんでした。同じ内容の再試行には同じ送信キーを使います。${costErrorText(e)}`);
  }
  function keyFor(body: string): string {
    const pending = retry.current?.body === body ? retry.current : { body, key: newCostKey() };
    retry.current = pending;
    return pending.key;
  }

  async function reload() {
    if (!beginOperation()) return;
    setLoading(true);
    try {
      const items = await mediaOperationsApi.listCostEntries(character.id, character.project_id);
      const entryId = pendingEntry.current ?? effective?.entry_id;
      const latest = entryId ? await mediaOperationsApi.getCostEntryHistory(entryId) : null;
      const allocation = pendingAllocation.current;
      const revenue = allocation ? await mediaOperationsApi.getRevenueCostRecords(allocation.eventId) : null;
      if (allocation && !revenue?.items.some((record) => record.id === allocation.recordId)) throw new Error("配賦した売上費用の再読込を確認できません");
      if (!alive.current) return;
      setEntries(items); setHasMore(items.length === 100); setLoaded(true);
      if (latest) applyHistory(latest);
      if (allocation && revenue) onAllocated(allocation.eventId, revenue.effective);
      pendingAllocation.current = null;
      setAllocationEntry(null); setAllocationTarget(null); setNeedsReload(false); retry.current = null; pendingEntry.current = null;
      setNotice("費用一覧をサーバーから再読込しました");
    } catch (e) { if (alive.current) setError(costErrorText(e)); }
    finally { finishOperation(); }
  }
  async function loadMore() {
    if (!beginOperation()) return;
    try {
      const items = await mediaOperationsApi.listCostEntries(character.id, character.project_id, entries.length);
      if (!alive.current) return;
      setEntries((current) => Array.from(new Map([...current, ...items].map((entry) => [entry.entry_id, entry])).values()));
      setHasMore(items.length === 100);
    } catch (e) { if (alive.current) setError(costErrorText(e)); }
    finally { finishOperation(); }
  }
  async function selectEntry(entry: MediaCostEntry) {
    if (!beginOperation()) return;
    try {
      const next = await mediaOperationsApi.getCostEntryHistory(entry.entry_id);
      if (!alive.current) return;
      applyHistory(next); pendingEntry.current = null; retry.current = null; setAllocationEntry(null); setAllocationTarget(null);
    } catch (e) { if (alive.current) setError(costErrorText(e)); }
    finally { finishOperation(); }
  }
  function beginNew() {
    if (busy.current || needsReload) return;
    setHistory(null); setForm(formFromCostRecord(null)); setScope(emptyScope()); setError(null); setNotice(null);
    setAllocationEntry(null); setAllocationTarget(null); retry.current = null; pendingEntry.current = null;
  }

  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!loaded || busy.current || needsReload || forbidden || effective?.allocation_status === "allocated") return;
    let input: MediaCostEntryInput;
    try {
      const currency = effective?.currency ?? revenueCurrency(scope.currency);
      if (!currency) throw new Error("通貨はUSD・JPYなどの3文字で入力してください");
      const start = effective?.window_start ?? utcDate(scope.start);
      const end = effective?.window_end ?? utcDate(scope.end);
      if (utcTime(end) < utcTime(start)) throw new Error("費用対象期間の終了は開始以降にしてください");
      input = {
        ...costFieldsInput(form, effective?.evidence ?? [], { kind: "record", ref: character.id }, true),
        currency, persona_ref: character.id, project_id: character.project_id ?? null,
        content_ref: effective ? effective.content_ref : scope.contentRef.trim() || null,
        window_start: start, window_end: end, correction_of_id: effective?.id ?? null,
      };
    } catch (e) { setError(costErrorText(e)); return; }
    if (!beginOperation(true)) return;
    let accepted = false;
    try {
      const created = await mediaOperationsApi.createCostEntry(input, keyFor(JSON.stringify(input)));
      accepted = true; pendingEntry.current = created.entry_id;
      const latest = await mediaOperationsApi.getCostEntryHistory(created.entry_id);
      const items = await mediaOperationsApi.listCostEntries(character.id, character.project_id);
      if (!alive.current) return;
      if (!latest.items.some((entry) => entry.id === created.id)) throw new Error("保存版がまだ履歴にありません");
      applyHistory(latest); setEntries(items); setHasMore(items.length === 100);
      setAllocationEntry(null); setAllocationTarget(null);
      retry.current = null; pendingEntry.current = null;
      setNotice("未配賦費用の保存後の再読込を確認しました");
    } catch (e) { operationError(e, accepted); }
    finally { finishOperation(); }
  }

  async function selectAllocationTarget(event: CharacterDashboardRevenueItem) {
    if (!allocationEntry || !beginOperation()) return;
    try {
      const costs = await mediaOperationsApi.getRevenueCostRecords(event.id);
      if (!alive.current) return;
      setAllocationTarget({ event, costs });
    } catch (e) { if (alive.current) setError(costErrorText(e)); }
    finally { finishOperation(); }
  }
  async function allocate() {
    if (!allocationEntry || !allocationTarget || needsReload || forbidden || allocationEntry.allocation_status === "allocated" || allocationTarget.costs.effective?.cost_entry_id || !beginOperation(true)) return;
    const entry = allocationEntry;
    const target = allocationTarget;
    const input = { correction_of_id: target.costs.effective?.id ?? null };
    let accepted = false;
    try {
      const saved = await mediaOperationsApi.allocateCostEntry(target.event.id, entry.id, input, keyFor(JSON.stringify({ entry: entry.id, event: target.event.id, ...input })));
      accepted = true; pendingEntry.current = entry.entry_id;
      pendingAllocation.current = { eventId: target.event.id, recordId: saved.id };
      const entryHistory = await mediaOperationsApi.getCostEntryHistory(entry.entry_id);
      const revenueHistory = await mediaOperationsApi.getRevenueCostRecords(target.event.id);
      const items = await mediaOperationsApi.listCostEntries(character.id, character.project_id);
      if (!alive.current) return;
      if (!revenueHistory.items.some((record) => record.id === saved.id) || entryHistory.effective?.allocation_status !== "allocated") throw new Error("配賦結果の反映を再確認してください");
      setEntries(items); setHasMore(items.length === 100); applyHistory(entryHistory);
      onAllocated(target.event.id, revenueHistory.effective);
      pendingAllocation.current = null;
      setAllocationEntry(null); setAllocationTarget(null); retry.current = null; pendingEntry.current = null;
      setNotice("全額配賦後の費用・売上を再読込して確認しました");
    } catch (e) { operationError(e, accepted); }
    finally { finishOperation(); }
  }

  const disabled = loading || busyState || needsReload || forbidden || !loaded;
  const variants = dashboard.variants?.items ?? [];
  return <div className="space-y-3">
    <div className="flex gap-2"><Button type="button" size="sm" variant="outline" disabled={loading || busyState} onClick={() => void reload()}>費用一覧を再読込</Button><Button type="button" size="sm" disabled={disabled} onClick={beginNew}>新しい未配賦費用</Button></div>
    {loading ? <p role="status">費用一覧を読み込み中…</p> : null}
    {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
    {notice ? <p role="status" className="text-sm">{notice}</p> : null}
    <ul className="space-y-2">{entries.map((entry) => <li key={entry.entry_id} className="space-y-2 rounded-md border p-2">
      <EntryReadback entry={entry} />
      <div className="flex gap-2"><Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => void selectEntry(entry)} aria-label={`費用 ${entry.entry_id} の詳細・訂正`}>詳細・訂正履歴</Button>
        <Button type="button" size="sm" variant="outline" disabled={disabled || entry.allocation_status === "allocated"} onClick={() => { setAllocationEntry(entry); setAllocationTarget(null); }} aria-label={`費用 ${entry.entry_id} の配賦先を選ぶ`}>売上へ全額配賦</Button></div>
    </li>)}</ul>
    {loaded && !entries.length ? <p className="text-xs">売上前・未配賦費用の記録はまだありません。</p> : null}
    {hasMore ? <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => void loadMore()}>費用をさらに読み込む</Button> : null}
    {allocationEntry ? <section className="space-y-2 rounded-md border border-primary/30 p-2" aria-label="費用の配賦先">
      <p className="text-xs">対象費用 第{allocationEntry.revision}版を全額配賦します。同じ通貨・推計/確定・Persona・コンテンツ・対象期間に一致する表示中の売上だけを選べます。</p>
      {revenueItems.filter((event) => eligible(allocationEntry, event)).map((event) => {
        const classification = revenueEvidenceClass(event.revenue_authority, event.event_type);
        return <Button key={event.id} type="button" size="sm" variant="outline" disabled={disabled} onClick={() => void selectAllocationTarget(event)} aria-label={`売上 ${event.id} を配賦先に選択`}>{event.event_at} · {event.platform ?? "媒体未設定"} · {classification.eventLabel} · {classification.authorityLabel} · 手取り {revenueAmountText(event.net_amount)} {event.currency} · {event.id}</Button>;
      })}
      {!revenueItems.some((event) => eligible(allocationEntry, event)) ? <p className="text-xs">条件に合う売上は表示範囲にありません。未配賦のまま保存されます。</p> : null}
      {allocationTarget ? <div className="space-y-2 text-xs">
        <p>配賦先: {allocationTarget.event.id} · 既存の売上費用 {allocationTarget.costs.effective ? `第${allocationTarget.costs.effective.revision}版` : "なし"}</p>
        {allocationTarget.costs.effective ? <RevenueCostSummary item={allocationTarget.costs.effective} currency={allocationTarget.event.currency} revenueAuthority={allocationTarget.event.revenue_authority} /> : null}
        <p>既存の手入力費用がある場合は、この独立費用の全額で置き換えます。既存額への加算はしません。配賦後の費用訂正・別売上への移動は初期版ではできません。</p>
        {allocationTarget.costs.effective?.cost_entry_id ? <p>この売上には独立費用が既に配賦されています。</p> : null}
        <Button type="button" size="sm" disabled={disabled || !!allocationTarget.costs.effective?.cost_entry_id} onClick={() => void allocate()}>この売上に全額配賦する</Button>
      </div> : null}
      <Button type="button" variant="ghost" size="sm" disabled={busyState} onClick={() => { setAllocationEntry(null); setAllocationTarget(null); }}>配賦をキャンセル</Button>
    </section> : null}
    <form onSubmit={(e) => void save(e)} aria-label="未配賦費用フォーム" className="space-y-3 rounded-md border p-3">
      <fieldset disabled={disabled || effective?.allocation_status === "allocated"} className="space-y-3">
        <legend className="text-xs font-semibold">{effective ? `費用 第${effective.revision}版を訂正` : "売上前・未配賦費用を追加"}</legend>
        <fieldset disabled={!!effective} className="grid gap-2 sm:grid-cols-2">
          <label className="text-xs">通貨<Input aria-label="未配賦費用の通貨" maxLength={3} value={scope.currency} onChange={(e) => setScope({ ...scope, currency: e.target.value.toUpperCase() })} /></label>
          <label className="text-xs">コンテンツ<AppSelect aria-label="未配賦費用のコンテンツ" value={scope.contentRef} onValueChange={(contentRef) => setScope({ ...scope, contentRef })}><option value="">指定なし</option>{variants.map((variant) => <option key={variant.id} value={variant.id}>{variant.name ?? variant.title ?? variant.id}</option>)}</AppSelect></label>
          <label className="text-xs">対象開始（UTC）<Input aria-label="費用対象開始UTC" type="datetime-local" value={scope.start} onChange={(e) => setScope({ ...scope, start: e.target.value })} /></label>
          <label className="text-xs">対象終了（UTC）<Input aria-label="費用対象終了UTC" type="datetime-local" value={scope.end} onChange={(e) => setScope({ ...scope, end: e.target.value })} /></label>
        </fieldset>
        <RevenueCostFields form={form} setForm={setForm} allowNegative lockSource={!!effective} fixedBasis={!!effective} basisNote="記録済みの区分で固定" scopeLabel="この費用対象" />
        <Button type="submit" size="sm" disabled={disabled || effective?.allocation_status === "allocated"}>{busyState ? "保存・再読込中…" : effective ? "未配賦費用の訂正版を保存" : "未配賦費用を保存"}</Button>
      </fieldset>
      {effective?.allocation_status === "allocated" ? <p className="text-xs">配賦済み費用は訂正・移動できません。保存履歴を確認できます。</p> : null}
    </form>
    {history ? <details><summary className="cursor-pointer text-xs">独立費用の保存履歴（{history.items.length}件）</summary>{history.has_more ? <p className="text-xs">表示範囲より古い履歴があります。</p> : null}<ol className="space-y-2">{history.items.map((entry) => <li key={entry.id} className="rounded-md border p-2"><EntryReadback entry={entry} /><p className="text-xs">証拠 {entry.evidence.length}件 · {entry.entry_hash}</p>{entry.evidence.map((item, index) => <p key={index} className="text-xs">{item.url ?? item.ref} {item.note}</p>)}</li>)}</ol></details> : null}
  </div>;
}

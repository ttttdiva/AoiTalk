"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { HelpPopover } from "@/components/ui/help-popover";
import {
  ambiguousDraftFailure, draftWrite, personalDraftApi,
  type DraftDecisionResult, type DraftOperation, type DraftWrite, type PersonalDraft,
} from "./personal-draft-api";

const STATUS: Record<string, string> = {
  awaiting_user_review: "本人の受理待ち",
  accepted: "受理済み・台帳反映済み",
  materialized_requires_verification: "期限候補へ反映済み",
  discarded: "破棄済み",
};
const FIELD: Record<string, string> = {
  subject_key: "対象", due_date: "日付", label: "名称", entry_type: "記録の種類",
  quantity: "数量", unit: "単位", occurred_on: "記録日", note: "メモ",
};
const ENTRY: Record<string, string> = { opening_balance: "初期残数", purchase: "購入", consumption: "消費" };
type Pending = { write: DraftWrite; ambiguousSeen: boolean; subject: string };
type Observation = Pick<PersonalDraft, "draft_ref" | "confirmed_digest" | "proposal" | "status" | "target_id">;
type Terminal = {
  draftRef: string; digest: string; kind: PersonalDraft["proposal"]["kind"];
  status: "accepted" | "materialized_requires_verification" | "discarded"; targetId: string | null;
};
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function terminalOf(draft: Observation): Terminal | undefined {
  const { status, target_id: targetId, proposal: { kind } } = draft;
  if (status === "discarded" && targetId === null ||
    typeof targetId === "string" && UUID_PATTERN.test(targetId) &&
    (status === "accepted" && kind === "inventory" || status === "materialized_requires_verification" && kind === "deadline")) {
    return { draftRef: draft.draft_ref, digest: draft.confirmed_digest, kind,
      status: status as Terminal["status"], targetId: targetId ?? null };
  }
}
const identity = (draft?: PersonalDraft) => draft ? `${draft.draft_ref}:${draft.confirmed_digest}:${draft.status}` : "";
const reason = (error: unknown) => error instanceof Error ? error.message : "操作できませんでした";

function DraftReview({ draft, locked, onDecision }: {
  draft: PersonalDraft; locked: boolean; onDecision: (draft: PersonalDraft, operation: DraftOperation) => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const checkId = useId();
  const awaiting = draft.status === "awaiting_user_review";
  const source = typeof draft.evidence.source_text === "string" ? draft.evidence.source_text : "";
  // Missing source evidence cannot be confirmed as a usable candidate.
  const reviewable = !!source && /^[a-f0-9]{64}$/.test(draft.confirmed_digest);
  return (
    <article aria-label="選択した候補" className="min-w-0 space-y-3 rounded border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">{draft.proposal.kind === "deadline" ? "期限の候補" : "在庫の記帳候補"}</h3>
        <span data-testid="draft-status">{STATUS[draft.status] ?? "状態を確認できません"}</span>
      </div>
      <p className="text-muted-foreground">提案元: {draft.proposed_by === "agent" ? "Agent" : "本人"}</p>
      <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1">
        {Object.entries(draft.proposal.data).map(([key, value]) => (
          <div key={key} className="contents">
            <dt>{FIELD[key] ?? key}</dt>
            <dd className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]">
              {key === "entry_type" ? ENTRY[String(value)] ?? String(value) : String(value)}
            </dd>
          </div>
        ))}
      </dl>
      <section aria-label="候補の根拠" className="min-w-0 space-y-1">
        <h4 className="font-medium">根拠の文面</h4>
        <pre tabIndex={0} aria-label="根拠の文面" className="max-h-64 overflow-y-auto whitespace-pre-wrap rounded bg-muted p-2 font-sans focus-visible:outline-2 [overflow-wrap:anywhere]">{source || "根拠の文面を取得できませんでした"}</pre>
        <details className="min-w-0 text-muted-foreground">
          <summary className="cursor-pointer">根拠の記録情報</summary>
          <dl className="space-y-1 pt-2 [overflow-wrap:anywhere]">
            <dt>抽出方法</dt><dd>{String(draft.evidence.extractor_version ?? "不明")}</dd>
            <dt>資料の指紋</dt><dd>{String(draft.evidence.source_sha256 ?? "不明")}</dd>
            <dt>値の文字範囲</dt><dd><pre className="whitespace-pre-wrap [overflow-wrap:anywhere]">{JSON.stringify(draft.evidence.field_spans ?? {}, null, 2)}</pre></dd>
          </dl>
        </details>
      </section>
      {awaiting && (
        <>
          <p>{draft.proposal.kind === "inventory" ? "受理すると、この数量を台帳の残数に反映します。" : "受理すると期限候補を台帳に追加します。日付の本人確認は別の操作です。"}</p>
          <label htmlFor={checkId} className="flex min-h-11 cursor-pointer items-start gap-2 py-2">
            <Checkbox id={checkId} className="mt-1" checked={confirmed}
              disabled={locked || !reviewable} onCheckedChange={(value) => setConfirmed(value === true)} />
            <span>元の文面と、この候補の内容を確認しました</span>
          </label>
          <div className="flex flex-wrap gap-2">
            <Button className="min-h-11" disabled={locked || !confirmed || !reviewable}
              onClick={() => onDecision(draft, "accept")}>この候補を受理</Button>
            <Button className="min-h-11" variant="outline" disabled={locked}
              onClick={() => onDecision(draft, "discard")}>候補を破棄</Button>
          </div>
        </>
      )}
      {draft.status === "materialized_requires_verification" && <p>期限の確認・完了の状態は台帳で確認してください。</p>}
    </article>
  );
}

export function PersonalDraftPanel({ onMaterialized }: { onMaterialized?: () => void | Promise<void> }) {
  const [drafts, setDrafts] = useState<PersonalDraft[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const pageRead = useRef<object | null>(null);
  const cursorRef = useRef<string | null>(null);
  const extended = useRef(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const [reviewReset, setReviewReset] = useState(0);
  const draftsRef = useRef(drafts);
  const selectedRef = useRef(selected);
  const pendingRef = useRef<Pending | null>(null);
  const inFlight = useRef(false);
  const reads = useRef(0);
  const reviewEpoch = useRef(0);
  const terminals = useRef(new Map<string, Terminal>());
  const resolvedWrites = useRef(new Set<string>());
  const notifiedMaterializations = useRef(new Set<string>());
  const materializedCallback = useRef(onMaterialized);
  const mounted = useRef(true);
  const headingId = useId();

  useEffect(() => { materializedCallback.current = onMaterialized; }, [onMaterialized]);

  const retain = useCallback((value: Pending | null) => {
    pendingRef.current = value;
    setPending(value);
  }, []);

  const resolvePending = useCallback((terminal: Terminal) => {
    const request = pendingRef.current;
    if (!request || request.write.draftRef !== terminal.draftRef ||
      request.write.body.confirmed_digest !== terminal.digest || request.write.expectedKind !== terminal.kind) return false;
    // A terminal event is immutable. An exact authoritative GET resolves the
    // candidate outcome even if the original POST response/key receipt was lost.
    resolvedWrites.current.add(request.write.body.idempotency_key);
    retain(null);
    setReviewReset((n) => n + 1);
    setMessage(terminal.status === "discarded"
      ? request.write.operation === "accept"
        ? "候補は別の操作で破棄済みです。元の受理操作は成立していません。"
        : "候補が破棄済みであることを確認しました。"
      : request.write.operation === "discard"
        ? "候補は別の操作で台帳に反映済みです。元の破棄操作は成立していません。"
        : `候補が台帳に反映済みであることを確認しました。${terminal.kind === "deadline" ? "期限の確認・完了は台帳で確認してください。" : ""}`);
    return true;
  }, [retain]);

  const reconcileTerminal = useCallback(<T extends Observation,>(draft: T): T => {
    const known = terminals.current.get(draft.draft_ref);
    const merged = known?.digest === draft.confirmed_digest && known.kind === draft.proposal.kind
      ? { ...draft, status: known.status, target_id: known.targetId } : draft;
    const terminal = terminalOf(merged);
    if (terminal) {
      terminals.current.set(draft.draft_ref, terminal);
      resolvePending(terminal);
    }
    return merged;
  }, [resolvePending]);

  const notifyMaterialized = useCallback(async (observed: Observation[]) => {
    const refresh = materializedCallback.current;
    if (!refresh) return;
    const keys = observed.map(terminalOf).filter((terminal): terminal is Terminal =>
      !!terminal && terminal.status !== "discarded").map((terminal) =>
      `${terminal.draftRef}:${terminal.digest}:${terminal.targetId}`
    ).filter((key) => !notifiedMaterializations.current.has(key));
    if (!keys.length) return;
    // Reserve before awaiting: a GET, a POST and a changing parent callback must
    // not produce duplicate refreshes or a render/refetch loop.
    keys.forEach((key) => notifiedMaterializations.current.add(key));
    try { await refresh(); }
    catch {
      keys.forEach((key) => notifiedMaterializations.current.delete(key));
      if (mounted.current) setMessage("台帳への反映を確認しました。台帳の再読み込みを行ってください。");
    }
  }, []);

  const apply = useCallback((next: PersonalDraft[]) => {
    const current = draftsRef.current.find((d) => d.draft_ref === selectedRef.current);
    const updated = next.find((d) => d.draft_ref === selectedRef.current);
    if (identity(current) !== identity(updated)) {
      reviewEpoch.current++;
      setReviewReset((n) => n + 1);
    }
    draftsRef.current = next;
    setDrafts(next);
    if (!updated) {
      selectedRef.current = next[0]?.draft_ref ?? "";
      setSelected(selectedRef.current);
    }
  }, []);

  const reload = useCallback(async () => {
    const generation = ++reads.current;
    pageRead.current = null;
    setLoadingMore(false);
    setLoading(true);
    try {
      const page = await personalDraftApi.list();
      if (!mounted.current || generation !== reads.current) return;
      const reconciled = page.items.map(reconcileTerminal);
      const loaded = draftsRef.current;
      const refs = new Set(loaded.map((draft) => draft.draft_ref));
      if (extended.current && page.next_cursor !== null && reconciled.some((draft) => refs.has(draft.draft_ref))) {
        // The newest page overlaps what is already shown, so the union is still one
        // contiguous run: refresh it and keep the older pages and their cursor.
        const fresh = new Set(reconciled.map((draft) => draft.draft_ref));
        apply([...reconciled, ...loaded.filter((draft) => !fresh.has(draft.draft_ref))]);
      } else {
        // Nothing older is shown, or more than a page of newer drafts arrived and a
        // gap could hide older ones: restart from the newest page.
        extended.current = false;
        cursorRef.current = page.next_cursor;
        setNextCursor(page.next_cursor);
        apply(reconciled);
      }
      void notifyMaterialized(reconciled);
    } catch (error) {
      if (mounted.current && generation === reads.current) setMessage(`候補一覧を更新できませんでした。${reason(error)}`);
    } finally {
      if (mounted.current && generation === reads.current) setLoading(false);
    }
  }, [apply, reconcileTerminal, notifyMaterialized]);

  async function loadMore() {
    const cursor = cursorRef.current;
    if (loading || !cursor || pageRead.current) return;
    const token = {};
    pageRead.current = token;
    const generation = reads.current;
    setLoadingMore(true);
    try {
      const page = await personalDraftApi.list(cursor);
      if (!mounted.current || generation !== reads.current || pageRead.current !== token) return;
      const observed = page.items.map(reconcileTerminal);
      // Update overlaps in place; append only new identities. Never capture the
      // selected candidate before an await or replace the existing page set.
      const merged = new Map(draftsRef.current.map((draft) => [draft.draft_ref, draft]));
      observed.forEach((draft) => merged.set(draft.draft_ref, draft));
      apply([...merged.values()]);
      extended.current = true;
      cursorRef.current = page.next_cursor;
      setNextCursor(page.next_cursor);
      void notifyMaterialized(observed);
    } catch (error) {
      if (mounted.current && generation === reads.current && pageRead.current === token) {
        setMessage(`候補の続きを取得できませんでした。${reason(error)}`);
      }
    } finally {
      if (mounted.current && pageRead.current === token) {
        pageRead.current = null;
        setLoadingMore(false);
      }
    }
  }

  useEffect(() => {
    mounted.current = true;
    void reload();
    return () => { mounted.current = false; };
  }, [reload]);

  function select(id: string) {
    if (selectedRef.current !== id) {
      reviewEpoch.current++;
      setReviewReset((n) => n + 1);
      selectedRef.current = id;
      setSelected(id);
    }
  }

  async function send(request: Pending) {
    let result: DraftDecisionResult;
    try {
      result = await personalDraftApi.send(request.write);
    } catch (error) {
      if (!mounted.current || resolvedWrites.current.has(request.write.body.idempotency_key)) return;
      if (request.ambiguousSeen || ambiguousDraftFailure(error)) {
        retain({ ...request, ambiguousSeen: true });
        const known = terminals.current.get(request.write.draftRef);
        if (!known || !resolvePending(known)) {
          setMessage(`操作結果の応答を照合できていません。${reason(error)}。元の送信内容を保持しています。`);
        }
      } else {
        retain(null);
        setReviewReset((n) => n + 1);
        setMessage(reason(error));
      }
      return;
    }
    if (!mounted.current || resolvedWrites.current.has(request.write.body.idempotency_key)) return;
    const observed: Observation = {
      draft_ref: result.draft_id, confirmed_digest: request.write.body.confirmed_digest,
      status: result.status, target_id: result.target_id,
      proposal: { kind: request.write.expectedKind, data: {} },
    };
    const reconciled = reconcileTerminal(observed);
    if (reconciled.status !== result.status || reconciled.target_id !== result.target_id) {
      // An authoritative terminal already observed through GET wins over a
      // contradictory late response. Do not claim that this request succeeded.
      retain({ ...request, ambiguousSeen: true });
      const known = terminals.current.get(request.write.draftRef);
      if (known) resolvePending(known);
      return;
    }
    retain(null);
    reads.current++; // An older in-flight list response must not undo a successful decision.
    apply(draftsRef.current.map((d) => d.draft_ref === result.draft_id && d.confirmed_digest === request.write.body.confirmed_digest
      ? { ...d, status: result.status, target_id: result.target_id } : d));
    setMessage(result.status === "discarded" ? "候補を破棄しました" : result.status === "materialized_requires_verification"
      ? "期限候補を台帳に追加しました。日付の本人確認は台帳で行ってください。" : "受理して台帳に反映しました");
    void reload(); // Refresh failures never become ambiguous POST failures.
    await notifyMaterialized([observed]);
  }

  async function decide(draft: PersonalDraft, operation: DraftOperation) {
    if (inFlight.current || pendingRef.current || selectedRef.current !== draft.draft_ref) return;
    inFlight.current = true;
    setBusy(true);
    setMessage("");
    const epoch = reviewEpoch.current;
    try {
      // Revalidate exactly what was displayed/confirmed before creating an operation key.
      const fetched = await personalDraftApi.get(draft.draft_ref);
      if (!mounted.current) return;
      if (fetched.draft_ref !== draft.draft_ref) throw new Error("候補の照合に失敗しました");
      const current = reconcileTerminal(fetched);
      void notifyMaterialized([current]);
      if (epoch !== reviewEpoch.current || selectedRef.current !== draft.draft_ref) {
        setMessage("確認対象が変わりました。表示内容を確認し直してください。");
        return;
      }
      apply(draftsRef.current.map((d) => d.draft_ref === current.draft_ref ? current : d));
      if (current.confirmed_digest !== draft.confirmed_digest || current.status !== "awaiting_user_review") {
        setReviewReset((n) => n + 1);
        setMessage("候補の内容または状態が変わりました。表示内容を確認し直してください。");
        return;
      }
      await send({ write: draftWrite(current, operation), ambiguousSeen: false,
        subject: String(current.proposal.data.subject_key ?? "候補") });
    } catch (error) {
      if (mounted.current) { setMessage(reason(error)); setReviewReset((n) => n + 1); }
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  async function retry() {
    if (inFlight.current || !pendingRef.current) return;
    inFlight.current = true;
    setBusy(true);
    try { await send(pendingRef.current); }
    finally { inFlight.current = false; if (mounted.current) setBusy(false); }
  }

  async function checkPending() {
    if (inFlight.current || !pendingRef.current) return;
    const request = pendingRef.current;
    inFlight.current = true;
    setBusy(true);
    try {
      const fetched = await personalDraftApi.get(request.write.draftRef);
      if (!mounted.current || resolvedWrites.current.has(request.write.body.idempotency_key)) return;
      if (fetched.draft_ref !== request.write.draftRef) throw new Error("候補の照合に失敗しました");
      const current = reconcileTerminal(fetched);
      apply(draftsRef.current.map((draft) => draft.draft_ref === current.draft_ref ? current : draft));
      void notifyMaterialized([current]);
      if (pendingRef.current) setMessage("保留中の操作はまだ解決していません。元の送信内容を保持しています。");
    } catch (error) {
      if (mounted.current && !resolvedWrites.current.has(request.write.body.idempotency_key)) {
        setMessage(`保留中の候補を照合できませんでした。${reason(error)}。元の送信内容を保持しています。`);
      }
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  const draft = drafts.find((d) => d.draft_ref === selected);
  return (
    <section aria-labelledby={headingId} className="min-w-0 space-y-3 rounded border p-3" data-testid="personal-drafts">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h2 id={headingId} className="font-semibold">候補の確認</h2>
          <HelpPopover label="候補の確認">本人専用の候補です。根拠と内容を確認して受理すると台帳へ反映します。期限の日付の確認や完了は、台帳で別に操作します。</HelpPopover>
        </div>
        <Button variant="outline" className="min-h-11" onClick={() => void reload()}>候補を再取得</Button>
      </header>
      {message && <p role="status" className="[overflow-wrap:anywhere]">{message}</p>}
      {pending && <div role="alert" className="space-y-2 rounded border p-3">
        <p className="[overflow-wrap:anywhere]">対象: {pending.subject}。応答待ちの{pending.write.operation === "accept" ? "受理" : "破棄"}操作があります。対象を切り替えても、元の操作として照合します。</p>
        <Button className="min-h-11" disabled={busy} onClick={() => void retry()}>同じ操作で結果を照合</Button>
        <Button className="min-h-11" variant="outline" disabled={busy} onClick={() => void checkPending()}>保留中の候補を照合</Button>
      </div>}
      {loading && <p aria-live="polite">候補を取得中…</p>}
      {!loading && !drafts.length && <p>表示できる候補はありません</p>}
      <ul aria-label="候補一覧" className="grid min-w-0 gap-2 sm:grid-cols-2">
        {drafts.map((item) => <li key={item.draft_ref} className="min-w-0">
          <button type="button" aria-pressed={selected === item.draft_ref}
            className="min-h-11 w-full min-w-0 rounded border p-2 text-left focus-visible:outline-2 focus-visible:outline-offset-2 aria-pressed:border-primary"
            onClick={() => select(item.draft_ref)}>
            <span className="block [overflow-wrap:anywhere]">{String(item.proposal.data.subject_key ?? "候補")} · {item.proposal.kind === "deadline" ? "期限" : "在庫"}</span>
            <span className="block text-xs text-muted-foreground">{STATUS[item.status] ?? "状態を確認できません"}</span>
          </button>
        </li>)}
      </ul>
      {nextCursor && <Button variant="outline" className="min-h-11" disabled={loading || loadingMore}
        onClick={() => void loadMore()}>{loadingMore ? "続きを取得中…" : "候補をさらに表示"}</Button>}
      {draft && <DraftReview key={`${identity(draft)}:${reviewReset}`} draft={draft}
        locked={busy || pending !== null} onDecision={(value, operation) => void decide(value, operation)} />}
    </section>
  );
}

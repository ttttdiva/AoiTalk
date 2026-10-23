"use client";

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import {
  isAmbiguousWriteFailure,
  newIdempotencyKey,
  ledgerApi,
  reminderApi,
  type LedgerDeadline,
  type ReminderActionResponse,
  type ReminderItem,
  type LedgerState,
} from "./personal-ledger-api";

type ProposalBody = Readonly<{
  candidate_entry_id: string;
  confirmed_digest: string;
  remind_at: string;
  time_zone: string;
  idempotency_key: string;
}>;

type PendingProposal = Readonly<{
  ownerKey: string;
  selection: ProposalBody;
  proposal: ProposalBody;
}>;

type PendingLifecycle = Readonly<{
  ownerKey: string;
  draftRef: string;
  kind: "schedule" | "cancel" | "ack";
  body: { confirmed_digest?: string; idempotency_key: string };
}>;

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "リマインダー操作を完了できませんでした";
}

function isEligible(deadline: LedgerDeadline): boolean {
  return deadline.status === "verified"
    && deadline.completed_on === null
    && deadline.needs_review.length === 0
    && Boolean(deadline.verified_entry_id);
}

function initialState(): LedgerState {
  return { deadlines: [], inventory: [] };
}

export function PersonalReminderPanel() {
  const currentUserId = useCurrentUserId();
  const ownerKey = currentUserId?.trim() || "";
  const [ledger, setLedger] = useState<LedgerState>(initialState);
  const [reminders, setReminders] = useState<ReminderItem[]>([]);
  const [deliveryAvailable, setDeliveryAvailable] = useState(false);
  const [reminderUnavailable, setReminderUnavailable] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [selectedCandidateId, setSelectedCandidateId] = useState("");
  const [remindAt, setRemindAt] = useState("");
  const [timeZone, setTimeZone] = useState("");
  const [draft, setDraft] = useState<ReminderItem | null>(null);
  const [draftReviewed, setDraftReviewed] = useState(false);
  const [pendingProposal, setPendingProposal] = useState<PendingProposal | null>(null);
  const [pendingLifecycle, setPendingLifecycle] = useState<PendingLifecycle | null>(null);
  const ownerRef = useRef(ownerKey);
  const requestEpoch = useRef(0);
  const ownerEpoch = useRef(0);
  const proposalRef = useRef<PendingProposal | null>(null);
  const lifecycleRef = useRef<PendingLifecycle | null>(null);
  const inFlight = useRef(false);

  const selectedDeadline = useMemo(
    () => ledger.deadlines.find((item) => item.candidate_entry_id === selectedCandidateId) ?? null,
    [ledger.deadlines, selectedCandidateId],
  );

  // Clear owner-scoped private state before the owner transition can paint.
  useLayoutEffect(() => {
    if (ownerRef.current === ownerKey) return;
    ownerRef.current = ownerKey;
    ownerEpoch.current += 1;
    requestEpoch.current += 1;
    setLoading(false);
    setLedger(initialState());
    setReminders([]);
    setDeliveryAvailable(false);
    setReminderUnavailable(false);
    setLoaded(false);
    setSelectedCandidateId("");
    setRemindAt("");
    setTimeZone("");
    setDraft(null);
    setDraftReviewed(false);
    setMessage("");
    // Keep pending operations visible and owner-bound so a late/unknown
    // result cannot be mistaken for the new owner's state.
  }, [ownerKey]);

  async function refresh() {
    if (!ownerKey || loading) return;
    const epoch = ++requestEpoch.current;
    const scope = ownerKey;
    setLoading(true);
    setMessage("");
    try {
      const [ledgerResult, reminderResult] = await Promise.allSettled([
        ledgerApi.state(),
        reminderApi.list(),
      ]);
      if (epoch !== requestEpoch.current || ownerRef.current !== scope) return;
      const ledgerAvailable = ledgerResult.status === "fulfilled";
      if (ledgerAvailable) {
        setLedger(ledgerResult.value);
      }
      setLoaded(ledgerAvailable);
      if (reminderResult.status === "fulfilled") {
        setReminders(reminderResult.value.items);
        setDeliveryAvailable(reminderResult.value.delivery_available);
        setReminderUnavailable(false);
        setDraft((current) => current
          ? reminderResult.value.items.find((item) => item.draft_ref === current.draft_ref) ?? current
          : current);
      } else {
        setReminders([]);
        setDeliveryAvailable(false);
        setReminderUnavailable(true);
        setDraft(null);
        setDraftReviewed(false);
      }
      if (!ledgerAvailable && reminderResult.status === "rejected") {
        setMessage(`個人台帳とprivate reminderを読み込めませんでした（${errorMessage(ledgerResult.reason)}）。`);
      } else if (!ledgerAvailable) {
        setMessage(`個人台帳を読み込めませんでした（${errorMessage(ledgerResult.reason)}）。`);
      } else if (reminderResult.status === "rejected") {
        setMessage("個人台帳は表示できますが、private reminder APIは利用できません。scheduleは無効です。");
      }
    } finally {
      if (epoch === requestEpoch.current && ownerRef.current === scope) setLoading(false);
    }
  }

  async function runProposal(operation: PendingProposal) {
    if (inFlight.current || ownerRef.current !== operation.ownerKey) return;
    const operationOwnerEpoch = ownerEpoch.current;
    const isCurrentOperation = () => ownerRef.current === operation.ownerKey
      && ownerEpoch.current === operationOwnerEpoch
      && proposalRef.current === operation;
    inFlight.current = true;
    setBusy(true);
    setMessage("");
    try {
      await reminderApi.selectTime(operation.selection);
      if (!isCurrentOperation()) return;
      const response = await reminderApi.propose(operation.proposal);
      if (!isCurrentOperation()) return;
      const nextDraft = await reminderApi.get(response.draft_ref);
      if (!isCurrentOperation()) return;
      proposalRef.current = null;
      setPendingProposal(null);
      setDraft(nextDraft);
      setDraftReviewed(false);
      setMessage("本人が選んだ日時で reminder draft を作成しました。表示内容を確認してから schedule してください。");
      await refresh();
    } catch (error) {
      if (!isCurrentOperation()) return;
      if (isAmbiguousWriteFailure(error)) {
        setMessage("reminder draft の結果が不明です。同じ内容・同じキーで照合してください。");
      } else {
        proposalRef.current = null;
        setPendingProposal(null);
        setMessage(errorMessage(error));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function propose() {
    if (!ownerKey || !selectedDeadline || !isEligible(selectedDeadline) || !remindAt || !timeZone || busy || proposalRef.current) return;
    const selectionKey = newIdempotencyKey();
    const proposalKey = newIdempotencyKey();
    const fields = {
      candidate_entry_id: selectedDeadline.candidate_entry_id,
      confirmed_digest: selectedDeadline.confirm_digest,
      remind_at: remindAt.trim(),
      time_zone: timeZone.trim(),
    };
    const operation: PendingProposal = Object.freeze({
      ownerKey,
      selection: Object.freeze({ ...fields, idempotency_key: selectionKey }),
      proposal: Object.freeze({ ...fields, idempotency_key: proposalKey }),
    });
    proposalRef.current = operation;
    setPendingProposal(operation);
    void runProposal(operation);
  }

  async function runLifecycle(operation: PendingLifecycle) {
    if (inFlight.current || ownerRef.current !== operation.ownerKey) return;
    inFlight.current = true;
    setBusy(true);
    setMessage("");
    try {
      let result: ReminderActionResponse;
      if (operation.kind === "schedule") {
        result = await reminderApi.schedule(operation.draftRef, {
          confirmed_digest: operation.body.confirmed_digest ?? "",
          idempotency_key: operation.body.idempotency_key,
        });
      } else if (operation.kind === "cancel") {
        result = await reminderApi.cancel(operation.draftRef, { idempotency_key: operation.body.idempotency_key });
      } else {
        result = await reminderApi.acknowledge(operation.draftRef, { idempotency_key: operation.body.idempotency_key });
      }
      if (ownerRef.current !== operation.ownerKey || lifecycleRef.current !== operation) return;
      lifecycleRef.current = null;
      setPendingLifecycle(null);
      setDraftReviewed(false);
      setMessage(`reminder を ${result.status} に更新しました。`);
      await refresh();
    } catch (error) {
      if (ownerRef.current !== operation.ownerKey || lifecycleRef.current !== operation) return;
      if (isAmbiguousWriteFailure(error)) {
        setMessage("reminder の結果が不明です。同じ内容・同じキーで照合してください。");
      } else {
        lifecycleRef.current = null;
        setPendingLifecycle(null);
        setMessage(errorMessage(error));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function startLifecycle(kind: PendingLifecycle["kind"]) {
    if (!ownerKey || !draft || busy || lifecycleRef.current) return;
    if (kind === "schedule" && (!draftReviewed || !draft.confirmation_digest || !draft.delivery_available)) return;
    const operation: PendingLifecycle = Object.freeze({
      ownerKey,
      draftRef: draft.draft_ref,
      kind,
      body: Object.freeze({
        ...(kind === "schedule" ? { confirmed_digest: draft.confirmation_digest ?? "" } : {}),
        idempotency_key: newIdempotencyKey(),
      }),
    });
    lifecycleRef.current = operation;
    setPendingLifecycle(operation);
    void runLifecycle(operation);
  }

  const eligibleDeadlines = ledger.deadlines.filter(isEligible);
  const canPropose = Boolean(ownerKey && selectedDeadline && isEligible(selectedDeadline) && remindAt.trim() && timeZone.trim());

  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="personal-reminder-title" data-testid="personal-reminder-panel">
      <div>
        <h2 id="personal-reminder-title" className="font-semibold">期限と本人用 private inbox</h2>
        <p className="text-xs text-muted-foreground">
          法定期限や更新日を自動計算しません。本人が選んだ日時だけで、確認済み・未完了の個人台帳から下書きを作成します。
        </p>
      </div>
      {!ownerKey && <p role="alert">ログイン状態を確認してください。</p>}
      <Button type="button" variant="outline" disabled={!ownerKey || loading || busy} onClick={() => void refresh()}>
        {loading ? "台帳とinboxを読み込み中…" : "個人台帳とprivate inboxを読み込む"}
      </Button>
      {message && <p role="status" aria-live="polite">{message}</p>}
      {loaded && (
        <>
          {reminderUnavailable && <p role="status">private reminder APIのschema/workerが利用できないため、期限は表示できますがreminder操作は停止しています。</p>}
          {!deliveryAvailable && <p role="status">worker未配備のため、未承認のdraftをscheduled / deliveredとは表示しません。</p>}
          <div className="space-y-2 rounded border p-3">
            <h3 className="font-medium">本人が選ぶ期限と通知時刻</h3>
            <label className="grid gap-1">
              確認済み・未完了の期限
              {/* eslint-disable-next-line no-restricted-syntax -- this owner-only deadline picker keeps native keyboard/form behavior. */}
              <select
                aria-label="リマインダー対象の期限"
                value={selectedCandidateId}
                disabled={busy || eligibleDeadlines.length === 0}
                onChange={(event) => {
                  setSelectedCandidateId(event.target.value);
                  setDraft(null);
                  setDraftReviewed(false);
                }}
              >
                <option value="">選択してください</option>
                {ledger.deadlines.map((deadline) => (
                  <option key={deadline.candidate_entry_id} value={deadline.candidate_entry_id} disabled={!isEligible(deadline)}>
                    {deadline.label || deadline.subject_key}・期限 {deadline.due_date}・{deadline.status === "verified" ? "確認済み" : "未確認"}
                  </option>
                ))}
              </select>
            </label>
            {selectedDeadline && !isEligible(selectedDeadline) && (
              <p role="status">この期限は未確認・完了済み・要確認のため、reminder draft の対象にできません。</p>
            )}
            <label className="grid gap-1">
              本人が選んだ通知日時（RFC3339 offset付き）
              <input aria-label="リマインダーの日時" placeholder="2027-03-01T09:00:00+09:00" value={remindAt} disabled={busy} onChange={(event) => { setRemindAt(event.target.value); setDraft(null); setDraftReviewed(false); }} />
            </label>
            <label className="grid gap-1">
              IANAタイムゾーン
              <input aria-label="リマインダーのタイムゾーン" placeholder="Asia/Tokyo" value={timeZone} disabled={busy} onChange={(event) => { setTimeZone(event.target.value); setDraft(null); setDraftReviewed(false); }} />
            </label>
            <Button type="button" disabled={busy || !canPropose || Boolean(pendingProposal)} onClick={propose}>
              本人が選んだ日時でreminder draftを作成
            </Button>
          </div>

          {pendingProposal && (
            <div className="rounded border border-amber-300 p-3" aria-label="reminder draft結果の照合">
              <p>{pendingProposal.ownerKey === ownerKey ? "reminder draft の結果が不明です。" : "別ログインで保留中です。元のアカウントに戻ると同じ内容・同じキーで照合できます。"}</p>
              <Button type="button" disabled={busy || pendingProposal.ownerKey !== ownerKey} onClick={() => void runProposal(pendingProposal)}>
                同じ内容・同じキーでreminder draftを照合
              </Button>
            </div>
          )}

          {draft && (
            <article className="space-y-2 rounded border p-3" aria-label="reminder draft内容確認" data-testid="reminder-draft-review">
              <h3 className="font-medium">reminder draft の内容確認</h3>
              <p>{draft.title || draft.label || draft.subject_key || "期限"}</p>
              <p>期限: {draft.due_date || "不明"}・通知: {draft.remind_at || "未設定"}・{draft.time_zone || "未設定"}</p>
              <p>配信: owner_private_in_app・status: {draft.status}</p>
              <p>{draft.delivery_available ? "workerが利用可能です。" : "worker未配備のため、scheduled / delivered とは表示しません。"}</p>
              <label className="flex items-center gap-2">
                {/* eslint-disable-next-line no-restricted-syntax -- the confirmation is a native form field with an explicit accessible label. */}
                <input type="checkbox" aria-label="reminder draftの表示内容を確認しました" checked={draftReviewed} disabled={busy} onChange={(event) => setDraftReviewed(event.target.checked)} />
                表示された内容を本人が確認しました
              </label>
              <div className="flex flex-wrap gap-2">
                <Button type="button" disabled={busy || !draftReviewed || !draft.confirmation_digest || !draft.delivery_available || Boolean(pendingLifecycle)} onClick={() => startLifecycle("schedule")}>
                  確認した内容をschedule
                </Button>
                {(draft.status === "awaiting_user_review" || draft.status === "scheduled") && (
                  <Button type="button" variant="outline" disabled={busy || Boolean(pendingLifecycle)} onClick={() => startLifecycle("cancel")}>
                    reminderをcancel
                  </Button>
                )}
                {draft.status === "delivered" && (
                  <Button type="button" variant="outline" disabled={busy || Boolean(pendingLifecycle)} onClick={() => startLifecycle("ack")}>
                    private inboxでack
                  </Button>
                )}
              </div>
            </article>
          )}

          {pendingLifecycle && (
            <div className="rounded border border-amber-300 p-3" aria-label="reminder lifecycle結果の照合">
              <p>{pendingLifecycle.ownerKey === ownerKey ? "reminder操作の結果が不明です。" : "別ログインで保留中です。元のアカウントに戻ると同じキーで照合できます。"}</p>
              <Button type="button" disabled={busy || pendingLifecycle.ownerKey !== ownerKey} onClick={() => void runLifecycle(pendingLifecycle)}>
                同じ内容・同じキーでreminder操作を照合
              </Button>
            </div>
          )}

          <div className="space-y-2 rounded border p-3" aria-label="private reminder inbox">
            <h3 className="font-medium">private inbox</h3>
            {!reminders.length && <p>reminderはありません。</p>}
            <ul className="space-y-2">
              {reminders.map((item) => (
                <li key={item.draft_ref} className="rounded border p-2">
                  <button type="button" className="text-left underline" onClick={() => { setDraft(item); setDraftReviewed(false); }}>
                    {item.title || item.label || item.subject_key || "期限"}・{item.status}・{item.remind_at || "時刻未設定"}
                  </button>
                  <p className="text-xs">delivery: owner_private_in_app・scheduled: {item.scheduled ? "true" : "false"}</p>
                </li>
              ))}
            </ul>
          </div>
        </>
      )}
    </section>
  );
}

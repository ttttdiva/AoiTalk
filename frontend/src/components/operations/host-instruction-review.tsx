"use client";
import { useEffect, useLayoutEffect, useId, useRef, useState } from "react";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { operationsLocalTasksApi as api, LocalTaskApiError, type InstructionReview, type LocalTask, type LocalTaskBody } from "@/lib/operations-local-tasks-api";

type Props = { owner: string; disabled: boolean; canPreview: boolean; body: () => LocalTaskBody;
  onLock: (locked: boolean) => void; onAccepted: (task: LocalTask) => Promise<void>; onSessionInvalid?: () => void; isParentLiveScope?: () => boolean };
const pointerKey = (owner: string) => `aoitalk:host-instruction-review:${owner}`;
type LookupPointer = { nonce: string; task_id: string };
async function lookup(pointer: LookupPointer, verifyOwner: () => Promise<boolean>) {
  try { return (await api.instructionReviewStatus(pointer.nonce)).task; }
  catch (e) {
    if (!(e instanceof LocalTaskApiError) || e.status !== 404) throw e;
    if (!await verifyOwner()) return null;
    // A restart removes challenges. The reserved durable task remains owner-only.
    try { return (await api.get(pointer.task_id)).task; }
    catch (taskError) { if (taskError instanceof LocalTaskApiError && taskError.status === 404) return null; throw taskError; }
  }
}
export function HostInstructionReview(props: Props) {
  const sessionOwner = useCurrentUserId();
  const scope = `${props.owner}:${sessionOwner ?? (sessionOwner === null ? "anonymous" : "standalone")}`;
  const liveScope = useRef(scope);
  useLayoutEffect(() => { liveScope.current = scope; }, [scope]);
  // Render-time gating also removes private text before effects run.
  if (sessionOwner !== undefined && sessionOwner !== props.owner) return null;
  return <ScopedInstructionReview key={scope} {...props} isLiveScope={() => liveScope.current === scope && (props.isParentLiveScope?.() ?? true)} />;
}
function ScopedInstructionReview({ owner, disabled, canPreview, body, onLock, onAccepted, onSessionInvalid, isLiveScope }: Props & { isLiveScope: () => boolean }) {
  const fieldId = useId();
  const [review, setReview] = useState<InstructionReview | null>(null);
  const [transmit, setTransmit] = useState(false);
  const [trusted, setTrusted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [recovery, setRecovery] = useState<LookupPointer | null>(null);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now());
  const lifetime = useRef({ active: true, session: 0, request: 0 });
  const inFlight = useRef(false);
  const callbacks = useRef({ onLock, onAccepted, onSessionInvalid, isLiveScope });
  callbacks.current = { onLock, onAccepted, onSessionInvalid, isLiveScope };
  type Ticket = { session: number; request: number };
  function current(ticket: Ticket) {
    return lifetime.current.active && callbacks.current.isLiveScope()
      && ticket.session === lifetime.current.session && ticket.request === lifetime.current.request;
  }
  function clearPrivate() {
    setReview(null); setTransmit(false); setTrusted(false); setRecovery(null); setUncertain(false);
  }
  function invalidateSession() {
    lifetime.current.session++; lifetime.current.request++; inFlight.current = false;
    localStorage.removeItem(pointerKey(owner)); clearPrivate(); setBusy(false);
    callbacks.current.onLock(false); callbacks.current.onSessionInvalid?.();
    setError("ログイン中の所有者を確認できません。ページを再読込してください");
  }
  async function checkOwner(ticket: Ticket) {
    if (!current(ticket)) return false;
    try {
      const session = await api.owner();
      if (!current(ticket)) return false;
      if (session.authenticated && session.user?.id === owner && session.user.role === "admin") return true;
    } catch { if (!current(ticket)) return false; }
    invalidateSession(); return false;
  }
  function begin() {
    const ticket = { session: lifetime.current.session, request: ++lifetime.current.request };
    inFlight.current = true; setBusy(true); setError(""); return ticket;
  }
  function finish(ticket: Ticket) {
    if (!current(ticket)) return;
    inFlight.current = false; setBusy(false);
  }
  useLayoutEffect(() => {
    const state = lifetime.current; state.active = true;
    return () => { state.active = false; state.session++; state.request++; };
  }, []);
  useEffect(() => {
    const raw = localStorage.getItem(pointerKey(owner));
    if (raw) {
      const ticket = begin();
      void (async () => {
        try {
          if (!await checkOwner(ticket)) return;
          const pointer = JSON.parse(raw) as LookupPointer;
          if (!/^[a-f0-9]{32}$/u.test(pointer.nonce) || !/^[a-f0-9]{32}$/u.test(pointer.task_id)) {
            localStorage.removeItem(pointerKey(owner)); throw new Error("前回レビューの受付情報を確認できませんでした");
          }
          setRecovery(pointer); callbacks.current.onLock(true);
          const task = await lookup(pointer, () => checkOwner(ticket));
          if (!await checkOwner(ticket)) return;
          localStorage.removeItem(pointerKey(owner)); setRecovery(null); callbacks.current.onLock(false);
          if (task) await callbacks.current.onAccepted(task);
          else setError("前のレビューは採用されていません。全文を確認し直してください。");
        } catch (e) { if (await checkOwner(ticket)) setError(e instanceof Error ? e.message : "受付を確認できませんでした"); }
        finally { finish(ticket); }
      })();
    }
    const revalidate = () => {
      const ticket = { session: lifetime.current.session, request: lifetime.current.request };
      void checkOwner(ticket);
    };
    window.addEventListener("focus", revalidate);
    window.addEventListener("storage", revalidate);
    document.addEventListener("visibilitychange", revalidate);
    return () => {
      inFlight.current = false; callbacks.current.onLock(false);
      if (!callbacks.current.isLiveScope()) localStorage.removeItem(pointerKey(owner));
      window.removeEventListener("focus", revalidate); window.removeEventListener("storage", revalidate);
      document.removeEventListener("visibilitychange", revalidate);
    };
  // The keyed owner scope owns this lifetime; request functions use refs for live callbacks.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner]);
  useEffect(() => {
    if (!review) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [review]);
  const expired = review !== null && now >= review.expires_at * 1000;
  async function preview() {
    if (disabled || !canPreview || recovery || inFlight.current) return;
    const ticket = begin(); onLock(true);
    try {
      if (!await checkOwner(ticket)) return;
      const result = await api.previewInstructions(body());
      if (!await checkOwner(ticket)) return;
      if (result.owner !== owner) { invalidateSession(); return; }
      setReview(result); setTransmit(false); setTrusted(false); setUncertain(false); setNow(Date.now());
    } catch (e) {
      if (await checkOwner(ticket)) { setError(e instanceof Error ? e.message : "指示を確認できませんでした"); onLock(false); }
    } finally { finish(ticket); }
  }
  async function accept() {
    if (!review || !transmit || !trusted || expired || disabled || inFlight.current || uncertain) return;
    const ticket = begin();
    try {
      if (!await checkOwner(ticket)) return;
      // Only a lookup pointer survives reload; no text or approval grant is stored.
      localStorage.setItem(pointerKey(owner), JSON.stringify({ nonce: review.nonce, task_id: review.task_id }));
      setUncertain(true);
      const result = await api.approveInstructions(review.nonce, { task_fingerprint: review.task_fingerprint,
        review_digest: review.review_digest, accept_openai_transmission: true, accept_trusted_host_assumption: true });
      if (!await checkOwner(ticket)) return;
      localStorage.removeItem(pointerKey(owner)); clearPrivate(); onLock(false);
      await callbacks.current.onAccepted(result.task);
    } catch (e) { if (await checkOwner(ticket)) setError(e instanceof Error ? e.message : "採用の受付を確認できませんでした"); }
    finally { finish(ticket); }
  }
  async function reconcile() {
    if ((!review && !recovery) || inFlight.current) return;
    const ticket = begin();
    try {
      if (!await checkOwner(ticket)) return;
      const pointer = recovery ?? { nonce: review!.nonce, task_id: review!.task_id };
      const task = await lookup(pointer, () => checkOwner(ticket));
      if (!await checkOwner(ticket)) return;
      if (task) {
        localStorage.removeItem(pointerKey(owner)); clearPrivate(); onLock(false);
        await callbacks.current.onAccepted(task);
      } else {
        localStorage.removeItem(pointerKey(owner)); setRecovery(null); setUncertain(false);
        if (!review) onLock(false);
        setError("採用は未受付です。全文と期限を確認してから採用の操作をやり直してください。");
      }
    } catch (e) { if (await checkOwner(ticket)) setError(e instanceof Error ? e.message : "受付を確認できませんでした"); }
    finally { finish(ticket); }
  }
  if (!canPreview && !review && !recovery && !error) return null;
  return <section aria-label="Codexホスト指示の確認" className="space-y-3 rounded border p-3">
    <p className="text-sm">Codex が読み込むホスト指示を、このタスクに採用する前に確認できます。通常の実行は未採用の指示を拒否します。</p>
    {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
    {recovery ? <><p role="status">前回の採用の受付を確認中です。新しいタスクは作りません。</p><Button type="button" variant="outline" disabled={busy} onClick={() => void reconcile()}>採用の受付結果を確認</Button></> : null}
    {!review && canPreview ? <Button type="button" variant="outline" disabled={disabled || busy || recovery !== null} onClick={() => void preview()}>ホスト指示の全文を確認</Button> : review ? <>
      <p className="text-sm">本人: {review.owner} · CLI: {review.cli_version}</p>
      <p className="break-all text-xs">Task ID: {review.task_id}<br />Task fingerprint: {review.task_fingerprint}<br />Review digest: {review.review_digest}<br />Nonce: {review.nonce}<br />確認期限: {new Date(review.expires_at * 1000).toISOString()}<br />採用後の実行期限: {new Date(review.execution_expires_at * 1000).toISOString()}</p>
      {review.sources.map(source => <div key={source.path} className="space-y-1 rounded border p-2">
        <p className="break-all text-xs">{source.path} · {source.selected ? "適用対象" : "変更監視対象"} · {source.size_bytes} bytes<br />SHA-256: {source.sha256 ?? "未存在（追加時に失効）"}</p>
        {source.exists ? <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words text-xs">{source.text}</pre> : null}
      </div>)}
      <p className="text-sm">適用対象の本文はタスクと共に OpenAI に送信されます。同時に外部プロセスが Codex home を変更しない、信頼済みホストでの利用が前提です。候補の追加・削除・本文変更をモデルとツールの実行境界で検出すると停止します。公式 CLI は消費した本文の bytes を証明しないため、検査の間の変更を完全には検出できません。</p>
      <p className="text-sm">この採用は表示した一つのタスクだけに有効です。読み取り専用・コマンド禁止と既存のツール制約を維持し、再起動または実行期限で失効します。</p>
      <label htmlFor={`${fieldId}-transmit`} className="flex gap-2 text-sm"><Checkbox id={`${fieldId}-transmit`} checked={transmit} disabled={busy || uncertain} onCheckedChange={setTransmit} />全文を読み、このタスクで適用対象の本文を OpenAI に送信することを採用する</label>
      <label htmlFor={`${fieldId}-trusted`} className="flex gap-2 text-sm"><Checkbox id={`${fieldId}-trusted`} checked={trusted} disabled={busy || uncertain} onCheckedChange={setTrusted} />信頼済みホストの前提と、消費本文の厳密な同一性を証明しない範囲を確認した</label>
      {expired ? <p role="alert">確認期限が切れました。確認を閉じて全文を読み直してください。</p> : null}
      {uncertain ? <p role="status">採用の受付結果が未確認です。受付結果の確認は新しいタスクを作りません。</p> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="button" disabled={disabled || busy || !transmit || !trusted || expired || uncertain} onClick={() => void accept()}>この内容をこのタスクに採用して実行</Button>
        {uncertain ? <Button type="button" variant="outline" disabled={busy} onClick={() => void reconcile()}>採用の受付結果を確認</Button> : <Button type="button" variant="outline" disabled={busy} onClick={() => { setReview(null); setTransmit(false); setTrusted(false); onLock(false); }}>確認を閉じて入力を編集</Button>}
      </div>
    </> : null}
  </section>;
}

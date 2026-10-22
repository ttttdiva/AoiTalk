"use client";
import { useId, useLayoutEffect, useRef, useState } from "react";
import { Checkbox } from "@/components/ui/checkbox";
import type { components } from "@/lib/api-types.gen";
import { codexProfileApi as api, operationsLocalTasksApi } from "@/lib/operations-local-tasks-api";

type Handoff = components["schemas"]["CodexProfileHandoff"];
type Login = components["schemas"]["CodexProfileLoginHandoff"];
export function CodexAppConnection({ owner, isCurrent, onSessionInvalid, onConnection }: { owner: string; isCurrent: () => boolean; onSessionInvalid: () => void; onConnection?: (connected: boolean) => void }) {
  const [handoff, setHandoff] = useState<Handoff | null>(null);
  const [login, setLogin] = useState<Login | null>(null);
  const [checks, setChecks] = useState([false, false, false, false]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [state, setState] = useState("");
  const [modelReady, setModelReady] = useState(false);
  const fieldId = useId();
  const life = useRef({ active: true, sequence: 0 });
  const working = useRef(false);
  useLayoutEffect(() => {
    const currentLife = life.current; currentLife.active = true;
    return () => { currentLife.active = false; currentLife.sequence++; };
  }, []);
  function current(sequence: number) { return life.current.active && sequence === life.current.sequence && isCurrent(); }
  function clear() { setHandoff(null); setLogin(null); setChecks([false, false, false, false]); setState(""); setModelReady(false); onConnection?.(false); }
  async function checkOwner(sequence: number) {
    if (!current(sequence)) return false;
    const session = await operationsLocalTasksApi.owner().catch(() => null);
    if (!current(sequence)) return false;
    if (session?.authenticated && session.user?.id === owner && session.user.role === "admin") return true;
    clear(); setError("ログイン中の所有者を確認できません。ページを再読込してください");
    life.current.sequence++; working.current = false; setBusy(false); onSessionInvalid(); return false;
  }
  async function action(run: (sequence: number) => Promise<void>) {
    if (working.current) return;
    const sequence = ++life.current.sequence;
    working.current = true; setBusy(true); setError("");
    try { if (await checkOwner(sequence)) await run(sequence); }
    catch (e) { if (await checkOwner(sequence)) { setError(e instanceof Error ? e.message : "接続状態を確認できませんでした"); setLogin(null); setChecks([false, false, false, false]); onConnection?.(false); } }
    finally { if (current(sequence)) { working.current = false; setBusy(false); } }
  }
  const review = () => action(async sequence => {
    clear();
    const status = await api.status();
    if (!await checkOwner(sequence)) return;
    if (!status.connector_available) throw new Error("公式接続の配備が未設定です");
    if (status.connected) { setState("connected"); setModelReady(status.model_ready); onConnection?.(true); return; }
    const value = await api.handoff();
    if (!await checkOwner(sequence)) return;
    if (!value.disclosure.official_connector_available) throw new Error("公式接続の配備が未設定です");
    setHandoff(value);
  });
  const start = () => action(async sequence => {
    if (!handoff || !checks.every(Boolean)) return;
    const receipt = await api.approve({ nonce: handoff.nonce, spec_digest: handoff.spec_digest,
      accept_storage: true, accept_scopes: true, accept_use_and_disconnect: true, accept_official_login: true });
    if (!await checkOwner(sequence)) return;
    const value = await api.login(receipt.approval_receipt);
    if (!await checkOwner(sequence)) return;
    const url = new URL(value.auth_url);
    if (url.protocol !== "https:" || url.hostname !== "auth.openai.com" || url.pathname !== "/oauth/authorize" || url.username || url.password || url.port || url.hash) throw new Error("公式ログイン先を確認できませんでした");
    setHandoff(null); setChecks([false, false, false, false]); setLogin(value); setState("logging_in");
  });
  const poll = () => action(async sequence => {
    const value = await api.poll();
    if (!await checkOwner(sequence)) return;
    setState(value.state);
    setModelReady(value.model_ready);
    onConnection?.(value.connected);
    if (value.connected || value.state !== "logging_in") setLogin(null);
  });
  const disconnect = () => action(async sequence => {
    const value = await api.disconnect();
    if (await checkOwner(sequence)) { clear(); setState(value.state); }
  });
  return <div className="space-y-3">
    <div className="flex flex-wrap gap-2"><button type="button" disabled={busy || !!handoff || !!login} onClick={() => void review()}>接続条件を確認</button><button type="button" disabled={busy} onClick={() => void disconnect()}>切断・承認待ちを破棄</button></div>
    {handoff && <div className="space-y-2" aria-label="専用Codex接続の本人承認">
      <p className="break-all">この手続の専用保存先: {handoff.disclosure.storage_destination}</p>
      <p className="break-all">接続するアカウント・workspace: {String(handoff.disclosure.connector_spec?.expected_account ?? "未設定")}</p>
      <p>全認証範囲: <code className="break-words">{handoff.oauth_scopes.join(" ")}</code></p>
      <p>Windows の OS keyring 鍵と専用保存先の暗号化ファイルに保存します。固定失効日は保証されません。アプリ切断・再起動で接続状態は失効し、認証情報削除・提供元取消は別途必要です。読み取り専用の用途で利用量を所有者に記録します。</p>
      <p>条件確認は5分、本人承認の確認結果は60秒、公式ログイン待ちは5分で失効します。</p>
      {["上記の保存先・保存方法・保持を承認する", "表示された全OAuth範囲を承認する", "用途・利用記録・切断と別途取消の違いを承認する", "本人として公式ログインを開始する"].map((label, index) => <label key={label} htmlFor={`${fieldId}-${index}`} className="flex items-start gap-2"><Checkbox id={`${fieldId}-${index}`} checked={checks[index]} disabled={busy} onCheckedChange={checked => setChecks(previous => previous.map((value, at) => at === index ? checked : value))} />{label}</label>)}
      <button type="button" disabled={busy || !checks.every(Boolean)} onClick={() => void start()}>承認して公式ログインを準備</button>
    </div>}
    {login && <div className="space-y-2"><p>本人が公式ページでログインしてください。5分以内に確認が必要です。</p><a href={login.auth_url} target="_blank" rel="noreferrer">公式ログインページを開く</a><button type="button" disabled={busy} onClick={() => void poll()}>ログイン完了を確認</button></div>}
    {state === "connected" && <p role="status">公式接続を確認しました。モデル利用とタスク権限は別の確認が必要です。</p>}
    {state === "connected" && <p>{modelReady ? "モデル接続の契約を確認済みです。" : "モデル接続の契約は最初の読み取りタスクで確認します。"}</p>}
    {state && state !== "connected" && state !== "logging_in" && <p role="status">接続状態: {state}</p>}
    {error && <p role="alert">{error} 状態が不明な場合は、承認待ちを破棄して条件を再確認してください。</p>}
  </div>;
}

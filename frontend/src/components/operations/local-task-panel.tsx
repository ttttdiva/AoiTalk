"use client";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { AppSelect } from "@/components/ui/app-select";
import { HostInstructionReview } from "./host-instruction-review";
import { CodexAppProfileNotice } from "./codex-app-profile-notice";
import { operationsLocalTasksApi as api, codexProfileApi, codexTaskConnectionKey, LocalTaskApiError, readingTaskBody, parsePendingLocalTask, pendingLocalTaskKey, type LocalTask, type LocalTaskStatus, type PendingLocalTask } from "@/lib/operations-local-tasks-api";
const labels: Record<string, string> = { queued: "待機中", running: "実行中", completed: "完了", failed: "失敗", cancelled: "中止" };
function message(error: unknown) { return error instanceof Error ? error.message : "処理を確認できませんでした"; }
function readable(value: unknown) { return typeof value === "string" ? value : value == null ? "" : JSON.stringify(value, null, 2); }
export function OperationsLocalTaskPanel() {
  const sessionOwner = useCurrentUserId();
  const scope = sessionOwner ?? (sessionOwner === null ? "anonymous" : "standalone");
  const liveScope = useRef(scope);
  useLayoutEffect(() => { liveScope.current = scope; }, [scope]);
  if (sessionOwner === null) return null;
  return <ScopedLocalTaskPanel key={scope} sessionOwner={sessionOwner} isLiveScope={() => liveScope.current === scope} />;
}
function ScopedLocalTaskPanel({ sessionOwner, isLiveScope }: { sessionOwner: string | undefined; isLiveScope: () => boolean }) {
  const [owner, setOwner] = useState<string | null>(null);
  const [status, setStatus] = useState<LocalTaskStatus | null>(null);
  const [tasks, setTasks] = useState<LocalTask[]>([]);
  const [selected, setSelected] = useState<LocalTask | null>(null);
  const [events, setEvents] = useState<unknown[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [reviewLocked, setReviewLocked] = useState(false);
  const submitting = useRef(false);
  const readSequence = useRef(0);
  const selectedTaskId = useRef<string | null>(null);
  const [invalidPending, setInvalidPending] = useState(false);
  const [pending, setPending] = useState<PendingLocalTask | null>(null);
  const [goal, setGoal] = useState(""); const [workspace, setWorkspace] = useState("");
  const [criteria, setCriteria] = useState(""); const [rounds, setRounds] = useState("4"); const [timeout, setTimeout] = useState("120");
  const [provider, setProvider] = useState("openai_compatible_local"); const [model, setModel] = useState(""); const [effort, setEffort] = useState("");
  const [profileConnected, setProfileConnected] = useState(false);
  const [codexConnection, setCodexConnection] = useState<"host" | "app_profile">("host");
  const connectionChanged = useCallback((connected: boolean) => {
    setProfileConnected(connected);
  }, []);
  const lifetime = useRef({ active: true, generation: 0 });
  const ownerRef = useRef<string | null>(null);
  const liveScopeRef = useRef(isLiveScope); liveScopeRef.current = isLiveScope;
  const current = useCallback((generation: number) => lifetime.current.active
    && lifetime.current.generation === generation && liveScopeRef.current(), []);
  const invalidateSession = useCallback(() => {
    lifetime.current.generation++; ownerRef.current = null; readSequence.current++;
    selectedTaskId.current = null; submitting.current = false;
    setOwner(null); setStatus(null); setTasks([]); setSelected(null); setEvents([]); setNotice("");
    setPending(null); setInvalidPending(false); setReviewLocked(false); setBusy(false);
    setProfileConnected(false); setCodexConnection("host");
    setError("ログイン中の所有者を確認できません。ページを再読込してください");
  }, []);
  const validateSession = useCallback(async (generation: number) => {
    if (!current(generation)) return false;
    try {
      const session = await api.owner();
      if (!current(generation)) return false;
      if (session.authenticated && session.user?.id === ownerRef.current && session.user.role === "admin") return true;
    } catch { if (!current(generation)) return false; }
    invalidateSession(); return false;
  }, [current, invalidateSession]);
  const refresh = useCallback(async () => {
    const generation = lifetime.current.generation;
    try {
      const [nextStatus, list] = await Promise.all([api.status(), api.list()]);
      if (await validateSession(generation)) { setStatus(nextStatus); setTasks(list.tasks); }
      return nextStatus;
    } catch (e) { if (await validateSession(generation)) throw e; return undefined; }
  }, [validateSession]);
  useLayoutEffect(() => {
    const state = lifetime.current; state.active = true;
    return () => { state.active = false; state.generation++; readSequence.current++; };
  }, []);
  useEffect(() => {
    const generation = lifetime.current.generation;
    void api.owner().then(async session => {
      if (!current(generation)) return;
      if (!session.authenticated || session.user?.role !== "admin" || !session.user.id
        || (sessionOwner !== undefined && sessionOwner !== session.user.id)) { invalidateSession(); return; }
      const id = session.user.id; ownerRef.current = id; setOwner(id);
      let saved: PendingLocalTask | null = null;
      let restoredConnection: "host" | "app_profile" = "host";
      try {
        saved = parsePendingLocalTask(localStorage.getItem(pendingLocalTaskKey(id)), id);
        const preference = localStorage.getItem(codexTaskConnectionKey(id));
        if (preference !== null && preference !== "host" && preference !== "app_profile") throw new Error("保存されたCodex接続先を確認できません");
        restoredConnection = saved ? saved.body.codex_connection ?? "host" : preference === "app_profile" ? "app_profile" : "host";
        setCodexConnection(restoredConnection);
      }
      catch (e) { setInvalidPending(true); setError(message(e)); }
      if (saved) { setPending(saved); setGoal(saved.input.goal); setWorkspace(saved.input.workspace); setCriteria(saved.input.completion_criteria.join("\n")); setRounds(String(saved.input.max_tool_rounds)); setTimeout(String(saved.input.timeout_sec)); }
      const next = await refresh();
      if (!current(generation) || !next) return;
      const route = saved?.input.route ?? next.default_route;
      setProvider(restoredConnection === "app_profile" ? "codex-cli" : route?.provider ?? "openai_compatible_local");
      setModel(restoredConnection === "app_profile" && route?.provider !== "codex-cli" ? "" : route?.model ?? next.model);
      setEffort(restoredConnection === "app_profile" && route?.provider !== "codex-cli" ? "" : route?.effort ?? "");
    }).catch(e => { if (current(generation)) { invalidateSession(); setError(message(e)); } });
  }, [refresh, current, invalidateSession, sessionOwner]);
  const readTask = useCallback(async (id: string, select = true) => {
    if (select) selectedTaskId.current = id;
    if (selectedTaskId.current !== id) return;
    const generation = lifetime.current.generation;
    const sequence = ++readSequence.current;
    try {
      const [result, tail] = await Promise.all([api.get(id), api.tail(id)]);
      if (!await validateSession(generation) || sequence !== readSequence.current || selectedTaskId.current !== id) return;
      setSelected(result.task); setEvents(tail.events);
      setTasks(previous => previous.map(task => task.task_id === id ? result.task : task));
    } catch (e) { if (await validateSession(generation)) throw e; }
  }, [validateSession]);
  const selectedId = selected?.task_id;
  const activeTask = selected?.status === "queued" || selected?.status === "running";
  useEffect(() => {
    if (!selectedId || !activeTask) return;
    let stopped = false; let timer: number;
    const poll = async () => {
      try { await readTask(selectedId, false); } catch (e) { if (!stopped) setError(message(e)); }
      if (!stopped) timer = window.setTimeout(() => void poll(), 2000);
    };
    timer = window.setTimeout(() => void poll(), 2000);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [selectedId, activeTask, readTask]);
  async function submit() {
    if (!owner || !status?.enabled || !status.running || invalidPending || reviewLocked || submitting.current) return;
    const profileRequest = pending ? pending.body.codex_connection === "app_profile" : provider === "codex-cli" && codexConnection === "app_profile";
    if (profileRequest && (!profileConnected || !status.supports_task_routes
      || status.codex_app_profile_connection_enabled !== true || status.codex_app_profile_connector_available !== true)) return;
    const generation = lifetime.current.generation;
    submitting.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const session = await api.owner();
      if (!current(generation)) return;
      if (!session.authenticated || session.user?.id !== owner || session.user.role !== "admin") { invalidateSession(); return; }
      if (profileRequest) {
        // Confirmation and route choice are independent. A disconnected or
        // unknown profile can never turn this request into a host request.
        setProfileConnected(false);
        const connection = await codexProfileApi.status();
        if (!await validateSession(generation)) return;
        if (!connection.connected || !connection.connector_available) throw new Error("専用Codex接続を確認してから実行してください");
        setProfileConnected(true);
      }
      const input = { goal, workspace, completion_criteria: criteria.split(/\n/u).map(x => x.trim()).filter(Boolean), max_tool_rounds: Number(rounds), timeout_sec: Number(timeout), ...(status.supports_task_routes ? { route: { provider, model, ...(effort ? { effort } : {}) } } : {}), ...(provider === "codex-cli" && codexConnection === "app_profile" ? { codex_connection: "app_profile" as const } : {}) };
      const intent = pending ?? { owner, key: crypto.randomUUID(), input, body: readingTaskBody(input) };
      localStorage.setItem(pendingLocalTaskKey(owner), JSON.stringify(intent)); setPending(intent);
      let result;
      try { result = await api.create(intent.body, intent.key); }
      catch (e) {
        if (!current(generation)) return;
        if (!pending && e instanceof LocalTaskApiError && [400, 422].includes(e.status)) { localStorage.removeItem(pendingLocalTaskKey(owner)); setPending(null); }
        throw e;
      }
      if (!await validateSession(generation)) return;
      if (!/^[a-f0-9]{32}$/u.test(result.task.task_id)) throw new Error("受付済みタスクIDを確認できませんでした。同じキーで再照合してください");
      localStorage.removeItem(pendingLocalTaskKey(owner)); setPending(null); readSequence.current += 1; selectedTaskId.current = result.task.task_id; setSelected(result.task);
      setNotice(result.created ? "タスクを受け付けました。実行状態を確認しています。" : "同じ受付キーの保存済みタスクを読み込みました。");
      await refresh(); await readTask(result.task.task_id);
    } catch (e) {
      if (await validateSession(generation)) setError(message(e));
    } finally { if (current(generation)) { submitting.current = false; setBusy(false); } }
  }
  async function cancelTask(id: string) {
    const generation = lifetime.current.generation;
    try {
      if (!await validateSession(generation)) return;
      await api.cancel(id);
      if (current(generation)) setProfileConnected(false);
      if (!await validateSession(generation)) return;
      await refresh();
      if (selectedTaskId.current === id) await readTask(id, false);
    } catch (e) { if (await validateSession(generation)) setError(message(e)); }
  }
  function draftBody() {
    return readingTaskBody({ goal, workspace, completion_criteria: criteria.split(/\n/u).map(x => x.trim()).filter(Boolean),
      max_tool_rounds: Number(rounds), timeout_sec: Number(timeout), route: { provider, model, ...(effort ? { effort } : {}) } });
  }
  async function acceptedReviewTask(task: LocalTask) {
    const generation = lifetime.current.generation;
    if (!await validateSession(generation)) return;
    readSequence.current += 1; selectedTaskId.current = task.task_id; setSelected(task);
    setNotice("明示採用したタスクの受付を確認しました。");
    await refresh();
    if (current(generation)) await readTask(task.task_id);
  }
  const locked = busy || pending !== null || invalidPending || reviewLocked;
  const profileRequest = pending ? pending.body.codex_connection === "app_profile" : provider === "codex-cli" && codexConnection === "app_profile";
  const profileBlocked = profileRequest && (!profileConnected || !status?.supports_task_routes
    || status.codex_app_profile_connection_enabled !== true || status.codex_app_profile_connector_available !== true);
  function chooseConnection(value: string) {
    if (!owner || !current(lifetime.current.generation) || (value !== "host" && value !== "app_profile")) return;
    setCodexConnection(value);
    try { localStorage.setItem(codexTaskConnectionKey(owner), value); }
    catch { setInvalidPending(true); setError("Codex接続先の選択を保存できません。ページを再読込して確認してください"); }
  }
  return <Card data-testid="operations-local-tasks"><CardHeader><CardTitle className="inline-flex items-center gap-1">ローカル読み取りタスク<HelpPopover label="ローカル読み取りタスク">指定した作業フォルダーを調べ、結果を保存します。既存のローカル実行キューと上限を使います。</HelpPopover></CardTitle></CardHeader><CardContent className="space-y-4">
    <CodexAppProfileNotice owner={owner} enabled={status?.codex_app_profile_connection_enabled === true && status?.codex_app_profile_connector_available === true} onSessionInvalid={invalidateSession} onConnection={connectionChanged} />
    {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
    {notice ? <p role="status" className="text-sm">{notice}</p> : null}
    {owner && status ? <>
      <div className="rounded border p-3 text-sm"><p>現在のモデル: {status.model || "未設定"}</p><p>同時実行上限: {status.max_concurrency} · 待機: {status.queued} · {status.enabled ? status.running ? "ワーカー稼働中" : "ワーカー停止中" : "無効"}</p><p>許可フォルダー: {status.allowed_roots.join(" / ")}</p></div>
      {pending ? <p className="text-sm">受付結果が未確認です。入力を固定し、同じキーで再照合します。ページを開き直しても受付情報を引き継ぎます。</p> : null}
      <form className="space-y-3" onSubmit={event => { event.preventDefault(); void submit(); }} aria-label="ローカル読み取りタスクを作成">
        <label className="block space-y-1 text-sm">作業フォルダー<Input value={workspace} onChange={e => setWorkspace(e.target.value)} required maxLength={1024} disabled={locked} /></label>
        <label className="block space-y-1 text-sm">目的<Textarea value={goal} onChange={e => setGoal(e.target.value)} required maxLength={8000} disabled={locked} /></label>
        <label className="block space-y-1 text-sm">完了条件（1行1件）<Textarea value={criteria} onChange={e => setCriteria(e.target.value)} maxLength={10000} disabled={locked} /></label>
        {status.supports_task_routes ? <div className="grid gap-3 sm:grid-cols-3">
          <label className="text-sm">実行provider<AppSelect aria-label="実行provider" value={provider} onChange={e => { setProvider(e.target.value); setEffort(""); }} disabled={locked} className="w-full">{(status.supported_providers ?? []).filter(p => ["openai_compatible_local", "codex-cli"].includes(p)).map(p => <option key={p} value={p}>{p}</option>)}</AppSelect></label>
          <label className="text-sm">モデルID<Input value={model} onChange={e => setModel(e.target.value)} required maxLength={255} disabled={locked} /></label>
          <label className="text-sm">推論設定<AppSelect aria-label="推論設定" value={effort} onChange={e => setEffort(e.target.value)} disabled={locked} className="w-full"><option value="">モデル既定</option>{(provider === "codex-cli" ? ["low", "medium", "high", "xhigh"] : ["fast", "thinking"]).map(e => <option key={e} value={e}>{e}</option>)}</AppSelect></label>
        </div> : <p className="text-sm text-muted-foreground">現在のサーバー設定を使います。この接続ではタスクごとのモデル変更を受け付けません。</p>}
        {status.supports_task_routes && provider === "codex-cli" && (profileConnected || codexConnection === "app_profile") ? <label className="block text-sm">Codex接続先<AppSelect aria-label="Codex接続先" value={codexConnection} onChange={e => chooseConnection(e.target.value)} disabled={locked} className="w-full"><option value="host">既存のホスト接続</option><option value="app_profile" disabled={!profileConnected}>本人が接続した専用profile</option></AppSelect></label> : null}
        {profileBlocked ? <p role="status" className="text-sm">専用profileの選択を保持しています。接続条件を確認してから実行してください。ホスト接続への変更は接続先で選択できます。</p> : null}
        <div className="grid gap-3 sm:grid-cols-2"><label className="text-sm">Tool回数上限<Input type="number" value={rounds} onChange={e => setRounds(e.target.value)} min={1} max={status.limits.max_max_tool_rounds ?? 1000} disabled={locked} /></label><label className="text-sm">時間上限（秒）<Input type="number" value={timeout} onChange={e => setTimeout(e.target.value)} min={30} max={status.limits.max_timeout_sec ?? 86400} disabled={locked} /></label></div>
        <p className="text-xs text-muted-foreground">読み取り専用。ファイル編集・コマンド・adb・Git操作は実行できません。</p>
        <Button type="submit" disabled={busy || reviewLocked || !status.enabled || !status.running || invalidPending || profileBlocked}>{pending ? "同じ受付キーで再照合" : "タスクを保存して実行"}</Button>
      </form>
      <HostInstructionReview owner={owner} canPreview={provider === "codex-cli" && codexConnection === "host"} body={draftBody} onLock={setReviewLocked} onAccepted={acceptedReviewTask} onSessionInvalid={invalidateSession} isParentLiveScope={isLiveScope}
        disabled={busy || pending !== null || invalidPending || !status.enabled || !status.running} />
      <div className="flex items-center justify-between"><h3 className="font-semibold">保存済みタスク</h3><Button type="button" variant="outline" onClick={() => void refresh().catch(e => setError(message(e)))}>一覧を再読込</Button></div>
      <div className="space-y-2">{tasks.map(task => <button key={task.task_id} type="button" className="w-full rounded border p-3 text-left text-sm" onClick={() => void readTask(task.task_id).catch(e => setError(message(e)))} aria-pressed={selectedId === task.task_id}><span className="font-medium">{labels[task.status] ?? task.status}</span> · {task.goal}<span className="block text-xs text-muted-foreground">{task.task_id}</span></button>)}{tasks.length === 0 ? <p className="text-sm">保存済みタスクはありません。</p> : null}</div>
    </> : !error ? <p>管理者セッションと実行キューを確認中…</p> : null}
    {selected ? <section aria-label="ローカルタスクの結果" className="space-y-3 rounded border p-3"><h3 className="font-semibold">実行状態: {labels[selected.status] ?? selected.status}</h3><p className="text-xs">Task ID: {selected.task_id}</p><p className="text-sm">{selected.latest_activity || selected.current_step}</p>
      <p className="text-sm">実行経路: {selected.llm?.provider ?? "未記録"} / {selected.llm?.model ?? "未記録"} · effort: {selected.llm?.effort ?? "既定・未記録"}</p>
      <p className="text-sm">LLM呼出: {selected.counters?.llm_calls ?? 0} · Tool実行: {selected.counters?.tool_calls ?? 0} · 変更ファイル: {selected.changed_files?.length ?? 0}</p>
      {selected.failure_reason ? <p role="alert" className="text-sm text-destructive">停止理由: {selected.failure_reason}</p> : null}
      {selected.stalled ? <p role="alert">進捗を確認してください。worker応答: {String(selected.worker_alive)}</p> : null}
      <pre className="whitespace-pre-wrap break-words text-sm">{selected.summary_text || readable(selected.completion_summary) || "報告はまだありません。"}</pre>
      {selected.result ? <details><summary>結果の詳細</summary><pre className="whitespace-pre-wrap break-words text-xs">{readable(selected.result)}</pre></details> : null}
      <details><summary>実行ログ（直近30件）</summary><pre className="whitespace-pre-wrap break-words text-xs">{events.map(readable).join("\n")}</pre></details>
      <div className="flex gap-2"><Button variant="outline" onClick={() => void readTask(selected.task_id).catch(e => setError(message(e)))}>結果を再読込</Button>{activeTask ? <Button variant="outline" disabled={selected.cancel_requested} onClick={() => void cancelTask(selected.task_id)}>このタスクを中止</Button> : null}</div>
    </section> : null}
  </CardContent></Card>;
}

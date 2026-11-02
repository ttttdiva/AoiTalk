"use client";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { employeeApi, employeeKey, EmployeeApiError, type Employee, type EmployeeRevision } from "@/lib/agent-employees-api";
import { emptyGraph, teamGraphApi, type DefaultGraphRef, type GraphDefinition, type TeamGraph } from "@/lib/agent-team-graph-api";
import { boundedGraphRead, classifyGraphReadback } from "@/lib/graph-update-recovery";
import { employeeHref } from "./employee-workspace";
import { TeamGraphCanvas } from "./team-graph-canvas";
import { EmployeeRole } from "./employee-role";
import { EmployeeNotice } from "./employee-fields";
import { AoiDotsApiError, aoiDotsErrorCode, bootstrapAoiDots, dispatchAoiDotsMessage, listAoiDotsUncertainInteractiveWork, type AoiDotsDispatchPayload } from "@/lib/aoidots-api";
import { chatApi } from "@/lib/chat-api";
import { useOptionalProject } from "@/contexts/project-context";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { useShellChrome } from "@/components/layout/shell-context";

type RootIntent = { key: string; session: string; graphId: string; payload: AoiDotsDispatchPayload; state: "unknown" | "rejected" };
type UpdateIntent = { base: TeamGraph; request: { title: string; project_id: string | null; definition: GraphDefinition; expected_revision: number } };
type CreateIntent = { title: string; project_id: string | null; definition: GraphDefinition; idempotency_key: string };
export function TeamGraphWorkspace({ agents, onBack, mapMode = false, onDirtyChange }: { agents: Employee[]; onBack: () => void; mapMode?: boolean; onDirtyChange?: (dirty: boolean) => void }) {
 const router = useRouter();
 const { setAoiDotsOpen } = useShellChrome();
 const userId = useCurrentUserId();
 const projectContext = useOptionalProject();
 const selectedProjectId = projectContext?.selectedProjectId ?? null;
 const draftStorageKey = userId ? `aoitalk:graph-root-request:${userId}` : null;
 const [restoredEpoch, setRestoredEpoch] = useState(-1);
 const owner = useRef(userId);
 const ownerEpoch = useRef(0);
 // Invalidate old async completions immediately, before passive hydration effects run.
 // eslint-disable-next-line react-hooks/refs
 if (owner.current !== userId) { owner.current = userId; ownerEpoch.current++; }
 const list = useSWR(userId ? ["team-graphs", userId] : null, () => teamGraphApi.list(), { shouldRetryOnError: false });
 const defaults = useSWR(userId ? ["team-graph-default", userId] : null, () => teamGraphApi.getDefault(), { shouldRetryOnError: false, revalidateOnFocus: false, revalidateOnReconnect: false });
 const [defaultNeedsRefresh, setDefaultNeedsRefresh] = useState(false);
 const [defaultMessage, setDefaultMessage] = useState<string | null>(null);
 const [graph, setGraph] = useState<TeamGraph | null>(null);
 const [title, setTitle] = useState("新しい Agent Team");
 const [definition, setDefinition] = useState<GraphDefinition>(emptyGraph);
 const [baseline, setBaseline] = useState(() => JSON.stringify(["新しい Agent Team", emptyGraph()]));
 const [error, setError] = useState<unknown>(null);
 const [busy, setBusy] = useState(false);
 const [goal, setGoal] = useState("");
 const [groupKey, setGroupKey] = useState("");
 const [rootIntent, setRootIntent] = useState<RootIntent | null>(null);
 const requestUncertain = rootIntent?.state === "unknown";
 const targetProjectId = rootIntent?.payload?.context_project_hint_id ?? graph?.project_id ?? selectedProjectId;
 const targetProjectName = projectContext?.projects?.find(project => project.id === targetProjectId)?.name ?? targetProjectId;
 const requestKey = useRef<string | null>(null);
 const requestSession = useRef<string | null>(null);
 const [intent, setIntent] = useState<CreateIntent | null>(null);
 const [updateIntent, setUpdateIntent] = useState<UpdateIntent | null>(null);
 const [revisionRefresh, setRevisionRefresh] = useState(0);
 const [settings, setSettings] = useState<{ agentId: string; nodeKey: string } | null>(null);
 const [templateOpen, setTemplateOpen] = useState(false);
 const [revisions, setRevisions] = useState<Record<string, EmployeeRevision | undefined>>({});
 const lock = useRef(false);
 const generation = useRef(0);
 useEffect(() => { const epoch = generation; const invalidate = () => { epoch.current++; lock.current = false; setBusy(false); }; window.addEventListener("popstate", invalidate); return () => { epoch.current++; window.removeEventListener("popstate", invalidate); }; }, []);
 const dirty = JSON.stringify([title, definition]) !== baseline;
 useEffect(() => { onDirtyChange?.(dirty || !!intent || !!updateIntent || !!rootIntent || busy); }, [dirty, intent, updateIntent, rootIntent, busy, onDirtyChange]);
 useEffect(() => {
  generation.current++; lock.current = false;
  const next = emptyGraph();
  // eslint-disable-next-line react-hooks/set-state-in-effect
  setGraph(null); setTitle("新しい Agent Team"); setDefinition(next); setBaseline(JSON.stringify(["新しい Agent Team", next]));
  setDefaultNeedsRefresh(false); setDefaultMessage(null);
  setIntent(null); setUpdateIntent(null); setGoal(""); setGroupKey(""); setRootIntent(null); setError(null); setBusy(false); setSettings(null); setTemplateOpen(false);
  requestKey.current = null; requestSession.current = null;
  if (!draftStorageKey) { setRestoredEpoch(ownerEpoch.current); return; }
  try {
   const raw = sessionStorage.getItem(draftStorageKey);
   if (raw) {
    const draft = JSON.parse(raw);
    // Restore a previously persisted user-scoped draft after hydration.
    if (typeof draft.title !== "string" || !draft.definition || typeof draft.baseline !== "string") throw new Error("Invalid draft");
    setGraph(draft.graph ?? null); setTitle(draft.title); setDefinition(draft.definition); setBaseline(draft.baseline); setIntent(draft.intent ?? null); setUpdateIntent(draft.updateIntent ?? null);
    requestKey.current = draft.clientMessageId ?? null; requestSession.current = draft.requestSession ?? null;
    setGoal(draft.goal ?? ""); setGroupKey(draft.groupKey ?? ""); setRootIntent(draft.rootIntent ?? (draft.requestUncertain && draft.clientMessageId && draft.requestSession ? { key: draft.clientMessageId, session: draft.requestSession, graphId: draft.graph?.id ?? "", payload: null, state: "unknown" } : null));
   }
  } catch { /* inaccessible or invalid storage */ }
  setRestoredEpoch(ownerEpoch.current);
 }, [draftStorageKey]);
 useLayoutEffect(() => {
  if (!draftStorageKey || restoredEpoch !== ownerEpoch.current) return;
  try { sessionStorage.setItem(draftStorageKey, JSON.stringify({ graph, title, definition, baseline, intent, updateIntent, goal, groupKey, rootIntent, clientMessageId: requestKey.current, requestSession: requestSession.current })); } catch { /* storage unavailable */ }
 }, [draftStorageKey, restoredEpoch, graph, title, definition, baseline, intent, updateIntent, goal, groupKey, rootIntent]);
 useEffect(() => { const handler = (event: BeforeUnloadEvent) => { if (dirty || intent || updateIntent || rootIntent || busy) event.preventDefault(); }; window.addEventListener("beforeunload", handler); return () => window.removeEventListener("beforeunload", handler); }, [dirty, intent, updateIntent, rootIntent, busy]);
 useEffect(() => { let active = true; void Promise.all(agents.map(async agent => { try { const result = await employeeApi.revisions(agent.id); const sorted = result.revisions.sort((a, b) => b.version - a.version); return [[agent.id, sorted[0]], ...sorted.map(revision => [revision.id, revision])] as [string, EmployeeRevision | undefined][]; } catch { return [[agent.id, undefined]] as [string, EmployeeRevision | undefined][]; } })).then(items => { if (active) setRevisions(Object.fromEntries(items.flat())); }); return () => { active = false; }; }, [agents, revisionRefresh]);
 useEffect(() => { const refresh = () => setRevisionRefresh(value => value + 1); window.addEventListener("focus", refresh); window.addEventListener("popstate", refresh); return () => { window.removeEventListener("focus", refresh); window.removeEventListener("popstate", refresh); }; }, []);
 const accept = useCallback((value: TeamGraph) => { setGraph(value); setTitle(value.title); setDefinition(value.definition); setBaseline(JSON.stringify([value.title, value.definition])); setIntent(null); setUpdateIntent(null); setError(null); }, []);
 const leave = () => !(dirty || intent || updateIntent || rootIntent) || window.confirm("未保存の変更があります。破棄しますか？");
 const load = async (id: string) => { if (!leave()) return; const token = ++generation.current; const ownerSnapshot = userId; const ownerVersion = ownerEpoch.current; const current = () => token === generation.current && owner.current === ownerSnapshot && ownerEpoch.current === ownerVersion; setBusy(true); setError(null); try { const value = (await teamGraphApi.get(id)).graph; if (current()) accept(value); } catch (e) { if (current()) setError(e); } finally { if (current()) setBusy(false); } };
 const reset = () => { if (!leave()) return; generation.current++; const next = emptyGraph(); setGraph(null); setTitle("新しい Agent Team"); setDefinition(next); setBaseline(JSON.stringify(["新しい Agent Team", next])); setIntent(null); setUpdateIntent(null); setError(null); };
 // eslint-disable-next-line react-hooks/refs
 const canTemplate = !graph && !busy && !intent && !updateIntent && !rootIntent && !!draftStorageKey && restoredEpoch === ownerEpoch.current && !definition.root_key && !definition.nodes.length && !definition.edges.length && !definition.groups.length;
 const editRejectedRequest = () => {
  if (busy || lock.current || rootIntent?.state !== "rejected") return;
  requestKey.current = null; requestSession.current = null; setRootIntent(null); setError(null);
 };
 const checkRequest = async () => {
  if (lock.current || !requestKey.current || !requestSession.current) return;
  const token = generation.current; const ownerSnapshot = userId; const ownerVersion = ownerEpoch.current; const current = () => token === generation.current && owner.current === ownerSnapshot && ownerEpoch.current === ownerVersion; const messageKey = requestKey.current; const session = requestSession.current; lock.current = true; setBusy(true); setError(null);
  try {
   const history = await chatApi.getMessages(session);
   if (!current()) return;
   if (!history.messages?.some(message => (message.client_message_id ?? message.metadata?.client_message_id) === messageKey)) throw new Error("送信結果をまだ確認できません。自動再送はしません。");
   requestKey.current = null; requestSession.current = null; setRootIntent(null); setGoal(""); setAoiDotsOpen(true);
  } catch (e) { if (current()) setError(e); } finally { if (current()) { lock.current = false; setBusy(false); } }
 };
 const requestRoot = async () => {
  if (lock.current || !graph || dirty || intent || updateIntent || !goal.trim() || !groupKey || requestUncertain || (rootIntent && rootIntent.graphId !== graph.id)) return;
  if (!targetProjectId) return;
  const root = graph.definition.nodes.find(node => node.key === graph.definition.root_key);
  if (!root || !graph.definition.groups.some(group => group.key === groupKey)) { setError(new Error("保存済みGraphのRootとグループを確認してください。")); return; }
  lock.current = true; const token = generation.current; setBusy(true); setError(null);
  const ownerSnapshot = userId; const ownerVersion = ownerEpoch.current;
  const current = () => token === generation.current && owner.current === ownerSnapshot && ownerEpoch.current === ownerVersion;
  let dispatched = false;
  try {
   const profile = await bootstrapAoiDots(targetProjectId);
   if (!current()) return;
   if (root.agent_id !== profile.agent_id) throw new Error(`GraphのRoot (${root.agent_id}) と現在のRoot (${profile.agent_id}) が一致しません。`);
   const uncertain = await listAoiDotsUncertainInteractiveWork(profile.session_id, profile.agent_id);
   if (!current()) return;
   if (uncertain.length) throw new Error("前回の実行結果を確認できません。AoiDotsで状態を確認してください。");
   const message = `${goal.trim()}\n\n保存済みAgent Teamを利用してください。team_graph_ref: ${JSON.stringify({ graph_id: graph.id, revision_id: graph.revision_id, group_key: groupKey })}\n保存済みrevision v${graph.version} content_hash: ${graph.content_hash}`;
   const pending: RootIntent = rootIntent ?? { key: employeeKey(), session: profile.session_id, graphId: graph.id, payload: { message, client_message_id: "", expected_agent_id: profile.agent_id, project_id: null, context_project_hint_id: targetProjectId, include_project_context: Boolean(targetProjectId) }, state: "rejected" };
   if (pending.session !== profile.session_id || pending.payload.expected_agent_id !== profile.agent_id) throw new Error("Rootの相談が変わったため、元の依頼を再送できません。AoiDotsで状態を確認してください。");
   const sending = { ...pending, payload: { ...pending.payload, client_message_id: pending.key }, state: "unknown" as const };
   if (!draftStorageKey) throw new Error("ユーザーを確認できないため送信できません。");
   sessionStorage.setItem(draftStorageKey, JSON.stringify({ graph, title, definition, baseline, intent, updateIntent, goal, groupKey, rootIntent: sending, clientMessageId: pending.key, requestSession: pending.session }));
   requestKey.current = pending.key; requestSession.current = pending.session; setRootIntent(sending);
   dispatched = true;
   const result = await dispatchAoiDotsMessage(sending.session, sending.payload);
   if (!current()) return;
   const response = result && typeof result === "object" ? result as Record<string, unknown> : {};
   if (response.success !== true && response.queued !== true) throw new Error("送信結果を確認できません。AoiDotsで状態を確認してください。");
   requestKey.current = null; requestSession.current = null; setRootIntent(null); setGoal(""); setAoiDotsOpen(true);
  } catch (e) { if (current()) { if (dispatched && e instanceof AoiDotsApiError && e.status === 409 && aoiDotsErrorCode(e) === "interactive_session_busy") { setRootIntent(previous => previous ? { ...previous, state: "rejected" } : previous); setError(new Error("前の実行が進行中です。この依頼は受け付けられていません。終了後、同じ依頼を再試行できます。")); } else { if (dispatched && e instanceof AoiDotsApiError && e.status >= 400 && e.status < 500 && e.status !== 409) { requestKey.current = null; requestSession.current = null; setRootIntent(null); } setError(e); } } } finally { if (current()) { lock.current = false; setBusy(false); } }
 };
 const save = async () => {
  if (lock.current || !title.trim() || intent || updateIntent || !draftStorageKey || restoredEpoch !== ownerEpoch.current) return;
  lock.current = true; const token = generation.current; setBusy(true); setError(null);
  const ownerSnapshot = userId; const ownerVersion = ownerEpoch.current;
  const current = () => token === generation.current && owner.current === ownerSnapshot && ownerEpoch.current === ownerVersion;
  const sameOwner = () => owner.current === ownerSnapshot && ownerEpoch.current === ownerVersion;
  try {
   let saved: TeamGraph;
   if (graph) {
    const request = { title: title.trim(), project_id: graph.project_id, definition, expected_revision: graph.version };
    sessionStorage.setItem(draftStorageKey, JSON.stringify({ graph, title, definition, baseline, intent, updateIntent: { base: graph, request }, goal, groupKey, rootIntent, clientMessageId: requestKey.current, requestSession: requestSession.current }));
    setUpdateIntent({ base: graph, request });
    try {
     const updated = (await teamGraphApi.update(graph.id, request)).graph;
     if (classifyGraphReadback(graph, request, updated) !== "confirmed") throw new EmployeeApiError(409);
     saved = updated;
    } catch (e) {
     if (!sameOwner()) return;
     let observed: TeamGraph;
     try { observed = await boundedGraphRead(() => teamGraphApi.get(graph.id)); }
     catch { if (current()) setError(e instanceof EmployeeApiError && e.status === 409 ? e : new Error("保存結果を確認できません。未保存の変更を保持しています。")); return; }
     const outcome = classifyGraphReadback(graph, request, observed);
     if (outcome === "confirmed") saved = observed;
     else { if (current()) setError(outcome === "conflict" || (e instanceof EmployeeApiError && e.status === 409) ? new EmployeeApiError(409) : new Error("保存結果を確認できません。サーバーには更新前の状態が表示されています。未保存の変更を保持しています。")); return; }
    }
   } else {
    const request = intent ?? { title: title.trim(), project_id: selectedProjectId, definition, idempotency_key: employeeKey() };
    sessionStorage.setItem(draftStorageKey, JSON.stringify({ graph, title, definition, baseline, intent: request, updateIntent, goal, groupKey, rootIntent, clientMessageId: requestKey.current, requestSession: requestSession.current }));
    setIntent(request);
    const id = (await teamGraphApi.create(request)).graph.id;
    if (!sameOwner()) return;
    saved = (await teamGraphApi.get(id)).graph;
   }
   if (current()) { accept(saved); await list.mutate(); }
  } catch (e) { if (current()) setError(e); } finally { if (current()) { lock.current = false; setBusy(false); } }
 };
 const checkSave = async () => {
  if (lock.current) return;
  const token = generation.current; const ownerSnapshot = userId; const ownerVersion = ownerEpoch.current;
  const current = () => token === generation.current && owner.current === ownerSnapshot && ownerEpoch.current === ownerVersion;
  lock.current = true; setBusy(true); setError(null);
  try {
   if (updateIntent) {
    const observed = await boundedGraphRead(() => teamGraphApi.get(updateIntent.base.id));
    if (!current()) return;
    const outcome = classifyGraphReadback(updateIntent.base, updateIntent.request, observed);
    if (outcome === "confirmed") { accept(observed); await list.mutate(); }
    else setError(new Error(outcome === "conflict" ? "保存結果が競合しています。下書きを保持しています。" : "保存結果をまだ確認できません。自動再送はしません。"));
   } else if (intent) {
    await list.mutate();
    if (!current()) return;
    // The server binds this exact body to the owner and key and returns the existing graph on replay.
    const created = (await teamGraphApi.create(intent)).graph;
    if (!current()) return;
    const observed = await boundedGraphRead(() => teamGraphApi.get(created.id));
    if (!current()) return;
    accept(observed); await list.mutate();
   }
  } catch (e) { if (current()) setError(e); } finally { if (current()) { lock.current = false; setBusy(false); } }
 };
 const refreshDefault = async () => {
  if (lock.current || !userId) return;
  const token = generation.current; const ownerSnapshot = userId; const ownerVersion = ownerEpoch.current;
  const current = () => token === generation.current && owner.current === ownerSnapshot && ownerEpoch.current === ownerVersion;
  lock.current = true; setBusy(true); setDefaultNeedsRefresh(true); setDefaultMessage(null);
  try { const observed = await teamGraphApi.getDefault(); if (!current()) return; await defaults.mutate(observed, { revalidate: false }); if (current()) setDefaultNeedsRefresh(false); }
  catch { if (current()) setDefaultMessage("既定の取得に失敗しました。再読込してください。"); }
  finally { if (current()) { lock.current = false; setBusy(false); } }
 };
 const setDefault = async () => {
  if (lock.current || !userId || !graph || dirty || intent || updateIntent || rootIntent || defaultNeedsRefresh || defaults.error || !defaults.data) return;
  const token = generation.current; const ownerSnapshot = userId; const ownerVersion = ownerEpoch.current;
  const current = () => token === generation.current && owner.current === ownerSnapshot && ownerEpoch.current === ownerVersion;
  const target: DefaultGraphRef = { graph_id: graph.id, revision_id: graph.revision_id, content_hash: graph.content_hash };
  const previous = defaults.data.configuration;
  const expected = previous ? { graph_id: previous.graph_id, revision_id: previous.revision_id, content_hash: previous.content_hash } : null;
  lock.current = true; setBusy(true); setDefaultNeedsRefresh(true); setDefaultMessage(null);
  try {
   await teamGraphApi.setDefault({ graph_id: target.graph_id, revision_id: target.revision_id, expected });
   if (!current()) return;
   const observed = await teamGraphApi.getDefault();
   if (!current()) return;
   const ref = observed.configuration;
   if (!observed.available || !ref || ref.graph_id !== target.graph_id || ref.revision_id !== target.revision_id || ref.content_hash !== target.content_hash) throw new Error();
   await defaults.mutate(observed, { revalidate: false });
   if (current()) { setDefaultNeedsRefresh(false); setDefaultMessage("AoiDotsの既定を確認しました。"); }
  } catch { if (current()) setDefaultMessage("既定の変更結果を確認できません。現在の既定を再読込してから再試行してください。"); }
  finally { if (current()) { lock.current = false; setBusy(false); } }
 };
 // eslint-disable-next-line react-hooks/refs
 const defaultDisabled = busy || !userId || restoredEpoch !== ownerEpoch.current || !graph || dirty || !!intent || !!updateIntent || !!rootIntent || defaultNeedsRefresh || !!defaults.error || !defaults.data;
 // eslint-disable-next-line react-hooks/refs
 const rootDisabledReason = busy ? "処理中です。" : restoredEpoch !== ownerEpoch.current || !draftStorageKey ? "ユーザー情報を確認中です。" : requestUncertain ? "送信結果を確認してください。" : !graph ? "チームを選択または保存してください。" : dirty || intent || updateIntent ? "チームの変更を保存してください。" : rootIntent && rootIntent.graphId !== graph.id ? "未受付の依頼を編集してください。" : !goal.trim() || !groupKey ? "依頼内容とグループを入力してください。" : !targetProjectId ? "再利用チームの対象Projectを選択してください。" : null;
 return <section className="flex min-h-0 flex-1 flex-col gap-3" data-testid="team-graph-workspace">
  <div className="flex flex-wrap items-center gap-2"><Button variant="ghost" size="sm" disabled={busy} onClick={() => { if (mapMode ? (!(dirty || intent) || window.confirm("未保存の下書きを保持して実行状況に戻りますか？")) : leave()) { generation.current++; onBack(); } }}>{mapMode ? "← 実行状況" : "← Agent一覧"}</Button><h2 className="text-lg font-semibold">Agent Team</h2><Button variant="secondary" size="sm" onClick={reset} disabled={busy || !!intent || !!updateIntent}>新規</Button><AppSelect aria-label="保存済みチーム" className="rounded-md border bg-background p-2 text-sm" value={graph?.id ?? ""} disabled={busy || !!intent || !!updateIntent || !!rootIntent} onChange={e => { if (e.target.value) void load(e.target.value); }}><option value="">チームを選択</option>{list.data?.graphs.map(g => <option key={g.id} value={g.id}>{g.title}</option>)}</AppSelect>{graph && <Button variant="ghost" size="sm" disabled={busy || !!intent || !!updateIntent || !!rootIntent} onClick={() => void load(graph.id)}>再読込</Button>}<input aria-label="チーム名" className="rounded-md border bg-background p-2 text-sm" maxLength={160} value={title} disabled={busy || !!intent || !!updateIntent} onChange={e => setTitle(e.target.value)} /><Button size="sm" disabled={busy || !!intent || !!updateIntent || !title.trim()} onClick={() => void save()}>{busy ? "保存・読込中…" : "保存"}</Button><span className="text-sm text-muted-foreground">{dirty ? "未保存" : graph ? `保存済み v${graph.version}` : "新規"}</span></div>
  {(error || list.error) && <p role="alert" className="text-sm text-destructive">{error instanceof Error ? error.message : list.error instanceof Error ? list.error.message : "通信に失敗しました。"}</p>}
  {(intent || updateIntent) && <p role="status" className="text-sm">保存結果を確認できません。 <Button size="sm" variant="secondary" disabled={busy} onClick={() => void checkSave()}>保存状態を確認</Button> <Button size="sm" variant="ghost" disabled={busy} onClick={() => { if (window.confirm("保存結果は未確定です。保留を解除し、一覧から確認しますか？")) { setIntent(null); setUpdateIntent(null); } }}>保留を解除</Button></p>}
  <div className="flex flex-wrap items-center gap-2"><Button variant="outline" size="sm" disabled={defaultDisabled} onClick={() => void setDefault()}>AoiDotsの既定にする</Button><Button variant="ghost" size="sm" disabled={busy || !userId} onClick={() => void refreshDefault()}>現在の既定を再読込</Button>
   {defaults.data?.available && defaults.data.configuration && <span className="text-sm">現在の既定: {defaults.data.configuration.graph_id} / {defaults.data.configuration.revision_id} / {defaults.data.configuration.content_hash}</span>}
   {(defaultMessage || defaults.error || (defaults.data && !defaults.data.available)) && <span role="status" className="text-sm">{defaultMessage ?? (defaults.error ? "既定の取得に失敗しました。再読込してください。" : "現在有効な既定はありません。保存済みGraphから設定できます。")}</span>}</div>
  <div><Button variant="outline" size="sm" disabled={!canTemplate} onClick={() => setTemplateOpen(true)}>複数チーム構造テンプレート</Button></div>
  <Dialog open={templateOpen && canTemplate} onOpenChange={open => setTemplateOpen(open)}><DialogContent size="2xl" className="max-h-[90vh] overflow-y-auto"><DialogTitle>複数チーム構造テンプレート</DialogTitle>{templateOpen && canTemplate && <TeamStructureTemplate agents={agents} revisions={revisions} onCancel={() => setTemplateOpen(false)} onApply={value => { if (!canTemplate) return; setDefinition(value); setTemplateOpen(false); }} />}</DialogContent></Dialog>
  <div className="flex flex-wrap items-end gap-2">
   <label className="text-sm">依頼内容<input aria-label="依頼内容" className="ml-2 rounded-md border bg-background p-2" value={goal} onChange={e => setGoal(e.target.value)} disabled={busy || !!rootIntent} /></label>
   <label className="text-sm">グループ<AppSelect aria-label="依頼するグループ" className="ml-2 rounded-md border bg-background p-2" value={groupKey} onChange={e => setGroupKey(e.target.value)} disabled={busy || !!rootIntent}><option value="">選択</option>{graph?.definition.groups.map(group => <option key={group.key} value={group.key}>{group.title}</option>)}</AppSelect></label>
   <Button size="sm" disabled={!!rootDisabledReason} onClick={() => void requestRoot()}>Rootに依頼</Button>
  </div>
  {rootDisabledReason && <p className="text-sm text-muted-foreground">{rootDisabledReason}</p>}
  {graph && !graph.project_id && <p className="text-sm text-muted-foreground">再利用チーム · 対象Project: {targetProjectName ?? "未選択"}</p>}
  {rootIntent?.state === "rejected" && <p role="status" className="text-sm">前の実行の終了後、同じ依頼を再試行できます。 <Button size="sm" variant="secondary" disabled={busy} onClick={editRejectedRequest}>未受付の依頼を編集</Button> <Button size="sm" variant="ghost" onClick={() => setAoiDotsOpen(true)}>AoiDotsを開く</Button></p>}
  {requestUncertain && <div role="status" className="text-sm">送信結果を確認できません。再送せず状態を確認してください。 <Button size="sm" variant="secondary" disabled={busy} onClick={() => void checkRequest()}>送信状態を確認</Button> <Button size="sm" variant="ghost" onClick={() => setAoiDotsOpen(true)}>AoiDotsを開く</Button></div>}
  <p role="status" className="text-sm text-muted-foreground">保存済みのrevisionをRootから利用できます。保存だけでは開始しません。{graph?.diagnostics.length ? ` 準備状態: ${graph.diagnostics.join("、")}` : graph ? " 準備診断の指摘なし。" : ""}</p>
  <div inert={busy || !!intent || !!updateIntent} className={busy || intent || updateIntent ? "opacity-60" : ""}><TeamGraphCanvas definition={definition} onChange={setDefinition} agents={agents} revisions={revisions} onAgentSettings={(id, nodeKey) => { if (mapMode) setSettings({ agentId: id, nodeKey }); else if (leave()) { generation.current++; onBack(); router.push(employeeHref(id, "role")); } }} /></div>
  <Dialog open={!!settings} onOpenChange={open => { if (!open) setSettings(null); }}><DialogContent size="4xl" className="max-h-[90vh] overflow-y-auto"><DialogTitle>Agent・モデル設定{settings && ` [${settings.agentId.slice(0, 8)}]`}</DialogTitle>{settings && <GraphAgentSettings key={`${settings.agentId}:${settings.nodeKey}`} agentId={settings.agentId} onPin={revision => { setRevisions(previous => ({ ...previous, [revision.id]: revision, [revision.agent_id]: revision })); setDefinition(previous => ({ ...previous, nodes: previous.nodes.map(node => node.key === settings.nodeKey && node.agent_id === revision.agent_id ? { ...node, revision_id: revision.id } : node) })); setSettings(null); }} onRefresh={() => setRevisionRefresh(value => value + 1)} />}</DialogContent></Dialog>
 </section>;
}

const templateSlots = ["統括（AoiDots など）", "チーム1 リーダー", "チーム1 作業者", "チーム1 レビュアー", "チーム2 リーダー", "チーム2 作業者", "チーム2 レビュアー"] as const;

function TeamStructureTemplate({ agents, revisions, onApply, onCancel }: { agents: Employee[]; revisions: Record<string, EmployeeRevision | undefined>; onApply: (definition: GraphDefinition) => void; onCancel: () => void }) {
 const [selected, setSelected] = useState<string[]>(() => templateSlots.map(() => ""));
 const valid = selected.every(id => !id || agents.some(agent => agent.id === id));
 const ready = valid && !!selected[0] && !!selected[1] && !!selected[4] && selected[0] !== selected[1] && selected[0] !== selected[4] && (!selected[2] || !selected[3] || selected[2] !== selected[3]) && (!selected[5] || !selected[6] || selected[5] !== selected[6]);
 const apply = () => {
  if (!ready) return;
  const definition = emptyGraph();
  const keys = selected.map(id => id ? employeeKey() : "");
  selected.forEach((id, index) => {
   if (!id) return;
   const revision = revisions[id];
   definition.nodes.push({ key: keys[index], agent_id: id, revision_id: revision?.agent_id === id ? revision.id : null, role: index === 0 || index === 1 || index === 4 ? "coordinator" : index === 3 || index === 6 ? "reviewer" : "worker", x: [420, 105, 0, 210, 735, 630, 840][index], y: index === 0 ? 20 : index === 1 || index === 4 ? 170 : 320, title: null });
  });
  definition.root_key = keys[0];
  for (const [leader, worker, reviewer, number] of [[1, 2, 3, 1], [4, 5, 6, 2]]) {
   definition.groups.push({ key: employeeKey(), title: `チーム${number}`, purpose: "", node_keys: [keys[leader], keys[worker], keys[reviewer]].filter(Boolean), leader_key: keys[leader] });
   const edge = (source: string, target: string, kind: "supervises" | "assigns" | "reviews") => definition.edges.push({ key: employeeKey(), source, target, kind, output_key: null });
   edge(keys[0], keys[leader], "supervises");
   if (keys[worker]) edge(keys[leader], keys[worker], "assigns");
   if (keys[worker] && keys[reviewer]) edge(keys[reviewer], keys[worker], "reviews");
  }
  onApply(definition);
 };
 return <div className="space-y-3"><div className="grid gap-2 sm:grid-cols-2">{templateSlots.map((label, index) => <label key={label} className="space-y-1 text-sm"><span>{label}{index === 0 || index === 1 || index === 4 ? "（必須）" : "（任意）"}</span><AppSelect aria-label={label} className="w-full rounded-md border bg-background p-2" value={selected[index]} onChange={event => setSelected(previous => previous.map((value, at) => at === index ? event.target.value : value))}><option value="">Agent を選択</option>{agents.map(agent => <option key={agent.id} value={agent.id}>{agent.display_name} [{agent.id.slice(0, 8)}]{agent.slug?.trim() ? ` · ${agent.slug.trim()}` : ""}</option>)}</AppSelect></label>)}</div>
  {!ready && <p role="status" className="text-sm">統括と2人のリーダーを選択してください。統括とリーダー、同じチームの作業者とレビュアーには別の Agent が必要です。</p>}
  {ready && [2, 3, 5, 6].some(index => !selected[index]) && <p role="status" className="text-sm">未選択のメンバーは作成されません。実行前に各チームの作業者とレビュアーを追加してください。</p>}
  <div className="flex gap-2"><Button type="button" disabled={!ready} onClick={apply}>下書きに作成</Button><Button type="button" variant="outline" onClick={onCancel}>キャンセル</Button></div>
 </div>;
}

function GraphAgentSettings({ agentId, onPin, onRefresh }: { agentId: string; onPin: (revision: EmployeeRevision) => void; onRefresh: () => void }) {
 const agent = useSWR(["graph-employee", agentId], () => employeeApi.get(agentId), { shouldRetryOnError: false });
 const catalog = useSWR("employee-catalog", employeeApi.catalog, { shouldRetryOnError: false });
 const revisions = useSWR(["graph-employee-revisions", agentId], () => employeeApi.revisions(agentId), { shouldRetryOnError: false, revalidateOnFocus: false });
 const [saved, setSaved] = useState<EmployeeRevision | null>(null);
 const reload = () => { void agent.mutate(); void catalog.mutate(); void revisions.mutate(); onRefresh(); };
 const all = saved ? [saved, ...(revisions.data?.revisions ?? []).filter(item => item.id !== saved.id)] : revisions.data?.revisions ?? [];
 const current = [...all].sort((a, b) => b.version - a.version)[0];
 return <div className="space-y-4"><EmployeeNotice error={agent.error || catalog.error || revisions.error} reload={reload} />
  {saved && <div role="status" className="rounded-md border p-3">v{saved.version} を保存しました。<Button className="ml-2" size="sm" onClick={() => onPin(saved)}>このノードに v{saved.version} を固定</Button></div>}
  {agent.data && catalog.data && revisions.data && <EmployeeRole key={current?.id ?? "first"} agent={agent.data.agent} catalog={catalog.data} revisions={all} onSaved={revision => { if (revision) setSaved(revision); reload(); }} />}
 </div>;
}

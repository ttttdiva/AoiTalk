import React, { useCallback, useRef, useState } from "react";
import { Alert, ScrollView, StyleSheet, View } from "react-native";
import { useFocusEffect, useLocalSearchParams, useNavigation, useRouter } from "expo-router";
import { Button, Text, TextInput, useTheme } from "react-native-paper";
import { usePreventRemove } from "@react-navigation/native";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import { ScreenHeader } from "../../../components/screen-header";
import { TeamGraphEditor } from "../../../features/operations/team-graph-editor";
import { getConfiguredApiServerFingerprint, isApiHttpError } from "../../../lib/api-client";
import { employeeApi } from "../../../lib/agent-employees-api";
import { readGraphJournal, writeGraphJournal, clearGraphJournal, graphContentHash } from "../../../lib/team-graph-journal";
import { graphError, graphIdentity, sameGraphContent, teamGraphApi, type GraphContent, type TeamGraph } from "../../../lib/agent-team-graphs-api";

type PendingWrite = { server: string; identity: string; body: GraphContent | null; version: number; revisionId: string; contentHash: string; graph: TeamGraph };
const pendingWrites = new Map<string, PendingWrite>();
type AgentOption = { id: string; name: string; revision_id?: string | null };
export default function TeamGraphScreen() {
  const theme = useTheme(); const router = useRouter();
  const { graphId } = useLocalSearchParams<{ graphId: string }>(); const navigation = useNavigation();
  const { user } = useAuth(); const { selectedProjectId, selectedSpaceId } = useProject();
  const scope = `${user?.user_id}:${user?.role}:${selectedProjectId}:${selectedSpaceId}:${graphId}`;
  const [graph, setGraph] = useState<TeamGraph | null>(null); const [content, setContent] = useState<GraphContent | null>(null);
  const [agents, setAgents] = useState<AgentOption[]>([]); const [error, setError] = useState<string | null>(null);
  const [canRelease, setCanRelease] = useState(false);
  const [revisionRefreshKey, setRevisionRefreshKey] = useState(false);
  const [busy, setBusy] = useState(false); const [uncertain, setUncertain] = useState(false); const [conflict, setConflict] = useState(false);
  const active = useRef(false); const epoch = useRef(0); const scopeRef = useRef(scope);
  const loadedServer = useRef<string | null>(null);
  const loaded = useRef(false); const loadedIdentity = useRef<string | null>(null);
  const inFlight = useRef<PendingWrite | null>(null);
  const dirty = !!graph && !!content && !sameGraphContent(graph, content);
  usePreventRemove(dirty || uncertain, ({ data }) => {
    Alert.alert("未保存の変更", "変更を破棄して離れますか？", [{ text: "編集を続ける", style: "cancel" }, { text: "離れる", style: "destructive", onPress: () => navigation.dispatch(data.action) }]);
  });
  const load = useCallback(async () => {
    const token = ++epoch.current; setBusy(true); setError(null);
    try {
      const server = await getConfiguredApiServerFingerprint(); const identity = await graphIdentity();
      if (!active.current || token !== epoch.current) return;
      const record = await readGraphJournal(identity, scope, graphId);
      if (record && record.kind !== "update") throw new Error("invalid journal");
      const memoryPending = pendingWrites.get(`${identity}:${scope}`);
      const [result, employees] = await Promise.all([teamGraphApi.get(server, graphId), employeeApi.list(server)]);
      if (!active.current || token !== epoch.current || server !== await getConfiguredApiServerFingerprint() || identity !== await graphIdentity()) return;
      if (!active.current || token !== epoch.current) return;
      loadedServer.current = server; loadedIdentity.current = identity; loaded.current = true;
      const pending = memoryPending ?? (record ? { server, identity, body: null, version: record.version, revisionId: record.revisionId, contentHash: record.contentHash, graph: result.graph } : null);
      const shown = pending?.graph ?? result.graph;
      setGraph(shown); setContent(pending?.body ?? { title: shown.title, project_id: shown.project_id, definition: shown.definition });
      setAgents(employees.agents.filter(a => a.created_by === user?.user_id).map(a => ({ id: a.id, name: a.display_name })));
      inFlight.current = pending ?? null; setUncertain(!!pending); setConflict(false);
    } catch (e) { if (active.current && token === epoch.current) setError(graphError(e)); }
    finally { if (active.current && token === epoch.current) setBusy(false); }
  }, [graphId, scope, user?.user_id]);
  useFocusEffect(useCallback(() => {
    active.current = true;
    if (scopeRef.current !== scope) { scopeRef.current = scope; loaded.current = false; inFlight.current = null; setGraph(null); setContent(null); setAgents([]); setUncertain(false); setConflict(false); void load(); }
    else if (!loaded.current) void load();
    else { void (async () => {
      const token = ++epoch.current; setBusy(true);
      try {
        const identity = await graphIdentity();
        if (!active.current || token !== epoch.current) return;
        if (identity !== loadedIdentity.current) { loaded.current = false; loadedIdentity.current = null; inFlight.current = null; setGraph(null); setContent(null); setAgents([]); setUncertain(false); setConflict(false); void load(); return; }
        if (inFlight.current) setUncertain(true);
        setRevisionRefreshKey(key => !key);
      } catch { if (active.current && token === epoch.current) { loaded.current = false; setGraph(null); setContent(null); setError("接続先とアカウントを確認してください。"); } }
      finally { if (active.current && token === epoch.current) setBusy(false); }
    })(); }
    return () => { active.current = false; ++epoch.current; };
  // Focus lifecycle intentionally does not rerun on edits.
  }, [scope, load]));
  const loadRevisions = useCallback(async (agentId: string, signal: AbortSignal) => {
    const token = epoch.current;
    const server = await getConfiguredApiServerFingerprint();
    const identity = await graphIdentity();
    if (!active.current || token !== epoch.current || identity !== loadedIdentity.current || server !== loadedServer.current || scopeRef.current !== scope || signal.aborted) throw new Error("scope changed");
    const result = await employeeApi.revisions(server, agentId, signal);
    if (!active.current || token !== epoch.current || scopeRef.current !== scope || signal.aborted || server !== await getConfiguredApiServerFingerprint() || identity !== await graphIdentity()) throw new Error("scope changed");
    return result.revisions;
  }, [scope]);
  const save = async () => {
    if (!graph || !content || busy || uncertain || conflict || !dirty || !content.title.trim()) return;
    const token = ++epoch.current; setBusy(true); setError(null);
    try {
      const server = await getConfiguredApiServerFingerprint(); const identity = await graphIdentity();
      if (!active.current || token !== epoch.current) return;
      if (identity !== loadedIdentity.current) throw new Error("identity changed");
      const body = { ...content, title: content.title.trim() };
      const contentHash = await graphContentHash(body);
      inFlight.current = { server, identity, body, version: graph.version, revisionId: graph.revision_id, contentHash, graph };
      try { await writeGraphJournal(identity, scope, graph.id, { kind: "update", version: graph.version, revisionId: graph.revision_id, contentHash }); }
      catch (e) { inFlight.current = null; throw e; }
      pendingWrites.set(`${identity}:${scope}`, inFlight.current);
      if (!active.current || token !== epoch.current || identity !== await graphIdentity()) return;
      if (!active.current || token !== epoch.current) return;
      const result = await teamGraphApi.update(server, graph.id, { ...body, expected_revision: graph.version });
      if (!active.current || token !== epoch.current || server !== await getConfiguredApiServerFingerprint() || identity !== await graphIdentity()) return;
      if (!active.current || token !== epoch.current) return;
      await clearGraphJournal(identity, scope, graph.id, { kind: "update", version: graph.version, revisionId: graph.revision_id, contentHash });
      if (!active.current || token !== epoch.current) return;
      pendingWrites.delete(`${identity}:${scope}`); inFlight.current = null; setGraph(result.graph); setContent({ title: result.graph.title, project_id: result.graph.project_id, definition: result.graph.definition });
    } catch (e) {
      if (!active.current || token !== epoch.current) return;
      if (isApiHttpError(e) && e.status >= 400 && e.status < 500 && e.status !== 408 && inFlight.current) {
        try {
          await clearGraphJournal(inFlight.current.identity, scope, graph.id, { kind: "update", version: inFlight.current.version, revisionId: inFlight.current.revisionId, contentHash: inFlight.current.contentHash });
          if (!active.current || token !== epoch.current || !inFlight.current) return;
          pendingWrites.delete(`${inFlight.current.identity}:${scope}`); inFlight.current = null;
          if (e.status === 409) setConflict(true);
          setError(graphError(e));
        } catch { if (!active.current || token !== epoch.current) return; setUncertain(true); setError("保存記録を確認できません。保存状態を照合してください。"); }
      } else if (inFlight.current) { setUncertain(true); setError("保存結果が不明です。保存状態を照合してください。"); }
      else setError("保存記録または接続先を確認できません。再読み込みしてください。");
    } finally { if (active.current && token === epoch.current) setBusy(false); }
  };
  const reconcile = async () => {
    const pending = inFlight.current; if (!pending || !graph || busy) return;
    const token = ++epoch.current; setBusy(true); setError(null);
    try {
      if (pending.server !== await getConfiguredApiServerFingerprint() || loadedIdentity.current !== await graphIdentity()) throw new Error("identity changed");
      const result = await teamGraphApi.get(pending.server, graph.id);
      if (!active.current || token !== epoch.current || pending.server !== await getConfiguredApiServerFingerprint() || loadedIdentity.current !== await graphIdentity()) return;
      if (!active.current || token !== epoch.current) return;
      const resultHash = await graphContentHash({ title: result.graph.title, project_id: result.graph.project_id, definition: result.graph.definition });
      if (!active.current || token !== epoch.current || pending.identity !== await graphIdentity()) return;
      if (!active.current || token !== epoch.current) return;
      if (result.graph.version === pending.version + 1 && resultHash === pending.contentHash) {
        await clearGraphJournal(pending.identity, scope, graph.id, { kind: "update", version: pending.version, revisionId: pending.revisionId, contentHash: pending.contentHash });
        if (!active.current || token !== epoch.current) return;
        setGraph(result.graph); setContent({ title: result.graph.title, project_id: result.graph.project_id, definition: result.graph.definition }); setUncertain(false); pendingWrites.delete(`${pending.identity}:${scope}`); inFlight.current = null;
      } else if (result.graph.version === pending.version && result.graph.revision_id === pending.revisionId) {
        setCanRelease(true); setError("現在の版には反映されていません。処理が続いている可能性があります。再照合するか、確認を解除して最新版から編集し直せます。");
      } else { await clearGraphJournal(pending.identity, scope, graph.id, { kind: "update", version: pending.version, revisionId: pending.revisionId, contentHash: pending.contentHash }); if (!active.current || token !== epoch.current) return; setUncertain(false); pendingWrites.delete(`${pending.identity}:${scope}`); inFlight.current = null; setConflict(true); setError("保存状態が変更されています。変更を保持しています。再読み込みして確認してください。"); }
    } catch { if (active.current && token === epoch.current) setError("保存状態を照合できません。接続を確認して再試行してください。"); }
    finally { if (active.current && token === epoch.current) setBusy(false); }
  };
  const releaseUncertain = () => {
    const pending = inFlight.current; if (!pending || !graph || !canRelease || busy) return;
    Alert.alert("保存状態の確認を解除", "先の更新が後から反映される可能性があります。最新版を再読み込みし、変更を確認して編集し直しますか？", [
      { text: "キャンセル", style: "cancel" },
      { text: "解除して再編集", onPress: () => { void (async () => {
        const token = ++epoch.current; setBusy(true);
        try {
          if (pending.identity !== await graphIdentity()) throw new Error("identity changed");
          await clearGraphJournal(pending.identity, scope, graph.id, { kind: "update", version: pending.version, revisionId: pending.revisionId, contentHash: pending.contentHash });
          if (!active.current || token !== epoch.current) return;
          pendingWrites.delete(`${pending.identity}:${scope}`); inFlight.current = null; setUncertain(false); setCanRelease(false); setConflict(true);
          setError("再読み込みして最新版を確認し、変更を編集し直してください。");
        } catch { if (active.current && token === epoch.current) setError("保存記録を解除できません。接続と端末の状態を確認してください。"); }
        finally { if (active.current && token === epoch.current) setBusy(false); }
      })(); } },
    ]);
  };
  const reload = () => {
    if (dirty || uncertain) Alert.alert("変更の破棄", "現在の編集を破棄してサーバーから再読み込みしますか？", [{ text: "キャンセル", style: "cancel" }, { text: "再読み込み", style: "destructive", onPress: () => void load() }]);
    else void load();
  };
  return <View style={[styles.screen, { backgroundColor: theme.colors.background }]}><ScreenHeader title="Team編集" /><ScrollView keyboardShouldPersistTaps="handled" contentContainerStyle={styles.content}>
    <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>保存するのはTeam定義です。実際の仕事は開始されません。</Text>
    {error ? <Text accessibilityRole="alert" style={[styles.warning, { color: theme.colors.error }]}>{error}</Text> : null}
    {busy ? <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>通信中…</Text> : null}
    {uncertain ? <Button onPress={() => void reconcile()} disabled={busy}>保存状態を照合</Button> : null}
    {uncertain && canRelease ? <Button onPress={releaseUncertain} disabled={busy}>確認を解除して再編集</Button> : null}
    <Button onPress={reload} disabled={busy || uncertain}>再読み込み</Button>
    {graph && content ? <>
      <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>保存版 v{graph.version} · {dirty ? "未保存の変更あり" : "保存済み"}</Text>
      <TextInput label="Team名" value={content.title} onChangeText={title => setContent({ ...content, title })} maxLength={160} disabled={busy || uncertain} />
      <Button mode="contained" onPress={() => void save()} disabled={busy || uncertain || conflict || !dirty || !content.title.trim()}>保存</Button>
      {conflict ? <Text style={[styles.warning, { color: theme.colors.error }]}>再読み込み後に変更をやり直してください。</Text> : null}
      <TeamGraphEditor definition={content.definition} onChange={definition => setContent({ ...content, definition })} agents={agents} onOpenAgent={agentId => router.push({ pathname: "/operations/agents/[agentId]", params: { agentId } })} loadRevisions={loadRevisions} revisionRefreshKey={revisionRefreshKey} disabled={busy || uncertain} />
    </> : null}
  </ScrollView></View>;
}
const styles = StyleSheet.create({ screen: { flex: 1,  }, content: { padding: 16, gap: 12 }, muted: {}, warning: {} });

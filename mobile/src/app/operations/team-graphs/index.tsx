import React, { useCallback, useRef, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { Button, Text, TextInput, useTheme } from "react-native-paper";
import * as Crypto from "expo-crypto";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import { ScreenHeader } from "../../../components/screen-header";
import { useOperations } from "../../../features/operations/use-operations";
import { getConfiguredApiServerFingerprint, isApiHttpError } from "../../../lib/api-client";
import { readGraphJournal, writeGraphJournal, clearGraphJournal } from "../../../lib/team-graph-journal";
import { emptyGraph, graphError, graphIdentity, teamGraphApi, type GraphContent } from "../../../lib/agent-team-graphs-api";

const pendingCreates = new Map<string, { body: GraphContent & { idempotency_key: string }; identity: string }>();

export default function TeamGraphsScreen() {
  const theme = useTheme();
  const router = useRouter(); const { user } = useAuth(); const { selectedProjectId, selectedSpaceId } = useProject();
  const scope = `${user?.user_id}:${user?.role}:${selectedProjectId}:${selectedSpaceId}`;
  const loader = useCallback(async (server: string, signal: AbortSignal) => { const identity = await graphIdentity(); const result = await teamGraphApi.list(server, signal); if (identity !== await graphIdentity()) throw new Error("identity changed"); return result; }, []);
  const { data, error, loading, refresh } = useOperations(scope, loader);
  const [journalReady, setJournalReady] = useState(false);
  const [title, setTitle] = useState(""); const [busy, setBusy] = useState(false); const [failure, setFailure] = useState<string | null>(null);
  const previousScope = useRef(scope); const pendingIdentity = useRef<string | null>(null);
  const pending = useRef<(GraphContent & { idempotency_key: string }) | null>(null); const epoch = useRef(0); const active = useRef(false);
  useFocusEffect(useCallback(() => {
    active.current = true; const token = ++epoch.current;
    if (previousScope.current !== scope) { previousScope.current = scope; setTitle(""); }
    pending.current = null; pendingIdentity.current = null; setFailure(null); setJournalReady(false); setBusy(true);
    void (async () => {
      try {
        const identity = await graphIdentity();
        if (!active.current || token !== epoch.current) return;
        const recorded = await readGraphJournal(identity, scope, "create");
        if (!active.current || token !== epoch.current) return;
        if (recorded && recorded.kind !== "create") throw new Error("invalid journal");
        const saved = pendingCreates.get(`${identity}:${scope}`) ?? (recorded ? { body: recorded.body, identity } : null);
        setJournalReady(true);
        if (saved) { pending.current = saved.body; pendingIdentity.current = saved.identity; setTitle(saved.body.title); setFailure("作成結果を確認できません。同じ内容・同じキーで再試行します。"); }
      } catch { if (active.current && token === epoch.current) setFailure("接続先とアカウントを確認してください。"); }
      finally { if (active.current && token === epoch.current) setBusy(false); }
    })();
    return () => { active.current = false; ++epoch.current; };
  }, [scope]));
  const create = async () => {
    if (busy || !journalReady || (!pending.current && !title.trim())) return;
    const token = epoch.current; setBusy(true); setFailure(null);
    const body = pending.current ?? { title: title.trim(), project_id: selectedProjectId ?? null, definition: emptyGraph(), idempotency_key: Crypto.randomUUID() };
    pending.current = body;
    try {
      const server = await getConfiguredApiServerFingerprint(); const identity = await graphIdentity();
      if (!active.current || token !== epoch.current) return;
      if (pendingIdentity.current && pendingIdentity.current !== identity) throw new Error("identity changed");
      pendingIdentity.current = identity;
      if (!pendingCreates.has(`${identity}:${scope}`)) pendingCreates.set(`${identity}:${scope}`, { body, identity });
      await writeGraphJournal(identity, scope, "create", { kind: "create", body });
      if (!active.current || token !== epoch.current || identity !== await graphIdentity()) return;
      if (!active.current || token !== epoch.current) return;
      const result = await teamGraphApi.create(server, body);
      if (!active.current || token !== epoch.current || server !== await getConfiguredApiServerFingerprint() || identity !== await graphIdentity()) return;
      if (!active.current || token !== epoch.current) return;
      await clearGraphJournal(identity, scope, "create", { kind: "create", body });
      if (!active.current || token !== epoch.current) return;
      pendingCreates.delete(`${identity}:${scope}`); pending.current = null; pendingIdentity.current = null; setTitle(""); router.push({ pathname: "/operations/team-graphs/[graphId]", params: { graphId: result.graph.id } });
    } catch (e) {
      if (!active.current || token !== epoch.current) return;
      if (isApiHttpError(e) && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429 && pendingIdentity.current) { try { await clearGraphJournal(pendingIdentity.current, scope, "create", { kind: "create", body }); if (!active.current || token !== epoch.current) return; pendingCreates.delete(`${pendingIdentity.current}:${scope}`); pending.current = null; pendingIdentity.current = null; } catch { if (!active.current || token !== epoch.current) return; setJournalReady(false); } }
      setFailure(pending.current ? "作成結果を確認できません。同じ内容・同じキーで再試行します。" : graphError(e));
    } finally { if (active.current && token === epoch.current) setBusy(false); }
  };
  return <View style={[styles.screen, { backgroundColor: theme.colors.background }]}><ScreenHeader title="Team" /><ScrollView keyboardShouldPersistTaps="handled" contentContainerStyle={styles.content}>
    <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>保存されたTeam定義です。ここから仕事は実行されません。</Text>
    <TextInput label="新しいTeam名" value={title} onChangeText={setTitle} disabled={!!pending.current || busy} maxLength={160} />
    <Button mode="contained" disabled={busy || !journalReady || (!pending.current && !title.trim())} onPress={() => void create()}>{pending.current ? "同じ作成を再試行" : "Teamを作成"}</Button>
    {failure ? <Text accessibilityRole="alert" style={[styles.warning, { color: theme.colors.error }]}>{failure}</Text> : null}
    {error ? <><Text accessibilityRole="alert" style={[styles.warning, { color: theme.colors.error }]}>{error}</Text><Button onPress={() => void refresh()}>再読み込み</Button></> : null}
    {loading ? <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>読み込み中…</Text> : null}
    {data?.graphs.map(graph => <Button key={graph.id} mode="outlined" onPress={() => router.push({ pathname: "/operations/team-graphs/[graphId]", params: { graphId: graph.id } })}>{graph.title} · v{graph.version}</Button>)}
    {data && !data.graphs.length ? <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>Teamはまだありません。</Text> : null}
  </ScrollView></View>;
}
const styles = StyleSheet.create({ screen: { flex: 1,  }, content: { padding: 16, gap: 12 }, muted: {}, warning: {} });

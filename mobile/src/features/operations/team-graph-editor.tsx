import React, { useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import { useTheme } from "react-native-paper";
import type { GraphDefinition, GraphNode, GraphEdge, GraphGroup } from "../../lib/agent-team-graphs-api";
import { TeamGraphCanvas } from "./team-graph-canvas";
export type { GraphDefinition, GraphNode } from "../../lib/agent-team-graphs-api";

type Agent = { id: string; name: string; revision_id?: string | null };
type Revision = { id: string; version: number; display_name: string };
type Props = { revisionRefreshKey?: boolean; loadRevisions?: (agentId: string, signal: AbortSignal) => Promise<Revision[]>; definition: GraphDefinition; onChange: (definition: GraphDefinition) => void; agents: Agent[]; disabled?: boolean; onOpenAgent?: (agentId: string) => void };
const makeKey = (prefix: string) => `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

function Choice({ label, onPress, active, disabled }: { label: string; onPress: () => void; active?: boolean; disabled?: boolean }) {
  const theme = useTheme();
  return <Pressable accessibilityRole="button" accessibilityState={{ selected: !!active, disabled: !!disabled }} disabled={disabled} onPress={onPress} style={[styles.choice, { backgroundColor: theme.colors.surfaceVariant }, active && [styles.active, { borderColor: theme.colors.primary }], disabled && styles.disabled]}><Text style={{ color: theme.colors.onSurface }}>{label}</Text></Pressable>;
}
function Field({ label, value, onChangeText, disabled, multiline }: { label: string; value: string; onChangeText: (value: string) => void; disabled?: boolean; multiline?: boolean }) {
  const theme = useTheme();
  return <View><Text style={[styles.label, { color: theme.colors.onSurfaceVariant }]}>{label}</Text><TextInput accessibilityLabel={label} editable={!disabled} value={value} onChangeText={onChangeText} multiline={multiline} style={[styles.input, { color: theme.colors.onSurface, borderColor: theme.colors.outline }]} /></View>;
}

export function TeamGraphEditor({ definition, onChange, agents, disabled, loadRevisions, onOpenAgent, revisionRefreshKey }: Props) {
  const theme = useTheme();
  const [tab, setTab] = useState<"canvas" | "inspector">("canvas");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [kind, setKind] = useState<GraphEdge["kind"]>("supervises");
  const [groupKey, setGroupKey] = useState<string | null>(null);
  const selected = definition.nodes.find(node => node.key === selectedKey);
  const [revisions, setRevisions] = useState<{ agentId: string; items: Revision[]; error?: boolean } | null>(null);
  const revisionAgent = selected && agents.some(agent => agent.id === selected.agent_id) ? selected.agent_id : null;
  useEffect(() => {
    setRevisions(null);
    if (!revisionAgent || !loadRevisions) return;
    const controller = new AbortController();
    loadRevisions(revisionAgent, controller.signal).then(items => {
      if (!controller.signal.aborted) setRevisions({ agentId: revisionAgent, items });
    }).catch(() => { if (!controller.signal.aborted) setRevisions({ agentId: revisionAgent, items: [], error: true }); });
    return () => controller.abort();
  }, [revisionAgent, loadRevisions, revisionRefreshKey]);
  const group = definition.groups.find(item => item.key === groupKey);
  const updateNode = (changes: Partial<GraphNode>) => {
    if (!selected || disabled) return;
    onChange({ ...definition, nodes: definition.nodes.map(node => node.key === selected.key ? { ...node, ...changes } : node) });
  };
  const updateGroup = (changes: Partial<GraphGroup>) => {
    if (!group || disabled) return;
    onChange({ ...definition, groups: definition.groups.map(item => item.key === group.key ? { ...item, ...changes } : item) });
  };
  const addNode = (agent: Agent) => {
    const key = makeKey("node");
    onChange({ ...definition, nodes: [...definition.nodes, { key, agent_id: agent.id, revision_id: null, role: "worker", x: definition.nodes.length * 30, y: definition.nodes.length * 30, title: agent.name }] });
    setSelectedKey(key); setTab("inspector");
  };
  const deleteNode = () => {
    if (!selected) return;
    onChange({ ...definition, root_key: definition.root_key === selected.key ? null : definition.root_key,
      nodes: definition.nodes.filter(node => node.key !== selected.key), edges: definition.edges.filter(edge => edge.source !== selected.key && edge.target !== selected.key),
      groups: definition.groups.map(item => ({ ...item, node_keys: item.node_keys.filter(key => key !== selected.key), leader_key: item.leader_key === selected.key ? null : item.leader_key })) });
    setSelectedKey(null);
  };
  return <View>
    <View style={styles.row}><Choice label="キャンバス" active={tab === "canvas"} onPress={() => setTab("canvas")} /><Choice label="ノード・接続・グループ" active={tab === "inspector"} onPress={() => setTab("inspector")} /></View>
    {tab === "canvas" ? <><TeamGraphCanvas definition={definition} selectedKey={selectedKey} onSelect={key => { setSelectedKey(key); }} onMove={(key, x, y) => { if (!disabled) onChange({ ...definition, nodes: definition.nodes.map(node => node.key === key ? { ...node, x, y } : node) }); }} disabled={disabled} />{selected ? <View style={styles.selection}><Text style={{ color: theme.colors.onSurface }}>選択中: {selected.title || selected.key}</Text><Choice label="選択したノードを編集" onPress={() => setTab("inspector")} /></View> : <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>ノードを選択すると編集できます</Text>}</> : <ScrollView nestedScrollEnabled style={styles.inspector} keyboardShouldPersistTaps="handled">
      <Text style={[styles.heading, { color: theme.colors.onSurface }]}>Agent を追加</Text>
      {agents.length ? agents.map(agent => <Choice key={agent.id} label={`${agent.name} を追加`} disabled={disabled} onPress={() => addNode(agent)} />) : <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>利用できる Agent がありません</Text>}
      <Text style={[styles.heading, { color: theme.colors.onSurface }]}>ノードを選択</Text>
      <View style={styles.row}>{definition.nodes.map(node => <Choice key={node.key} label={node.title || node.key} active={selectedKey === node.key} onPress={() => setSelectedKey(node.key)} />)}</View>
      {selected ? <>
        <Text style={[styles.heading, { color: theme.colors.onSurface }]}>選択中: {selected.title || selected.key}</Text>
        {onOpenAgent && revisionAgent ? <Choice label="Agent の役割・モデルを開く" disabled={disabled} onPress={() => onOpenAgent(revisionAgent)} /> : null}
        {selected.revision_id ? <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>固定ノードに Agent の変更を反映するには、新しい revision を選択してください。</Text> : null}
        <Text style={[styles.label, { color: theme.colors.onSurfaceVariant }]}>参照: {selected.revision_id ? `固定 revision ${selected.revision_id}` : "最新版"}</Text>
        <Field label="表示名" value={selected.title || ""} onChangeText={value => updateNode({ title: value || null })} disabled={disabled} />
        <Text style={[styles.label, { color: theme.colors.onSurfaceVariant }]}>参照 Agent</Text><View style={styles.row}>{agents.map(agent => <Choice key={agent.id} label={agent.name} active={selected.agent_id === agent.id} disabled={disabled} onPress={() => { if (agent.id !== selected.agent_id) updateNode({ agent_id: agent.id, revision_id: null }); }} />)}</View>
        <Text style={[styles.label, { color: theme.colors.onSurfaceVariant }]}>revision</Text><View style={styles.row}>
          <Choice label="最新版" active={!selected.revision_id} disabled={disabled} onPress={() => updateNode({ revision_id: null })} />
          {revisions?.agentId === selected.agent_id && revisions.items.map(revision => <Choice key={revision.id} label={`v${revision.version}・${revision.display_name}`} active={selected.revision_id === revision.id} disabled={disabled} onPress={() => updateNode({ revision_id: revision.id })} />)}
        </View>
        {revisionAgent && loadRevisions && !revisions ? <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>revision を読み込み中…</Text> : null}
        {revisions?.error ? <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>revision を読み込めません。既存の固定は保持されます。</Text> : null}
        <Text style={[styles.label, { color: theme.colors.onSurfaceVariant }]}>役割</Text><View style={styles.row}>{(["coordinator", "worker", "reviewer"] as const).map(role => <Choice key={role} label={role} active={selected.role === role} disabled={disabled} onPress={() => updateNode({ role })} />)}</View>
        <Choice label={definition.root_key === selected.key ? "ルートに選択中" : "ルートにする"} active={definition.root_key === selected.key} disabled={disabled} onPress={() => onChange({ ...definition, root_key: selected.key })} />
        {definition.root_key === selected.key ? <Choice label="ルートを解除" disabled={disabled} onPress={() => onChange({ ...definition, root_key: null })} /> : null}
        <Choice label="ノードを削除" disabled={disabled} onPress={deleteNode} />
        <Text style={[styles.heading, { color: theme.colors.onSurface }]}>接続先と種類</Text>
        <View style={styles.row}>{definition.nodes.filter(node => node.key !== selected.key).map(node => <Choice key={node.key} label={node.title || node.key} active={target === node.key} onPress={() => setTarget(node.key)} />)}</View>
        <View style={styles.row}>{(["supervises", "assigns", "reviews", "handoff"] as const).map(value => <Choice key={value} label={value} active={kind === value} onPress={() => setKind(value)} />)}</View>
        <Choice label="接続を追加" disabled={disabled || !target || !definition.nodes.some(node => node.key === target && node.key !== selected.key)} onPress={() => { if (target) onChange({ ...definition, edges: [...definition.edges, { key: makeKey("edge"), source: selected.key, target, kind, output_key: null }] }); }} />
        {definition.edges.filter(edge => edge.source === selected.key || edge.target === selected.key).map(edge => <View key={edge.key} style={[styles.block, { borderColor: theme.colors.outline }]}>
          <Text style={[styles.text, { color: theme.colors.onSurface }]}>{edge.source} → {edge.target} · {edge.kind}</Text>
          <Text style={[styles.label, { color: theme.colors.onSurfaceVariant }]}>接続の種類</Text><View style={styles.row}>{(["supervises", "assigns", "reviews", "handoff"] as const).map(value => <Choice key={value} label={value} active={edge.kind === value} disabled={disabled} onPress={() => onChange({ ...definition, edges: definition.edges.map(item => item.key === edge.key ? { ...item, kind: value } : item) })} />)}</View>
          <Field label="出力キー" value={edge.output_key || ""} disabled={disabled} onChangeText={value => onChange({ ...definition, edges: definition.edges.map(item => item.key === edge.key ? { ...item, output_key: value || null } : item) })} />
          <Choice label="接続を解除" disabled={disabled} onPress={() => onChange({ ...definition, edges: definition.edges.filter(item => item.key !== edge.key) })} />
        </View>)}
      </> : <Text style={[styles.muted, { color: theme.colors.onSurfaceVariant }]}>キャンバスまたは上の一覧からノードを選択してください</Text>}
      <Text style={[styles.heading, { color: theme.colors.onSurface }]}>グループ</Text>
      <Choice label="グループを作成" disabled={disabled} onPress={() => { const key = makeKey("group"); onChange({ ...definition, groups: [...definition.groups, { key, title: "新しいグループ", purpose: "", node_keys: [], leader_key: null }] }); setGroupKey(key); }} />
      <View style={styles.row}>{definition.groups.map(item => <Choice key={item.key} label={item.title} active={groupKey === item.key} onPress={() => setGroupKey(item.key)} />)}</View>
      {group ? <>
        <Field label="グループ名" value={group.title} disabled={disabled} onChangeText={value => updateGroup({ title: value })} />
        <Field label="目的" value={group.purpose} disabled={disabled} multiline onChangeText={value => updateGroup({ purpose: value })} />
        <Text style={[styles.label, { color: theme.colors.onSurfaceVariant }]}>メンバー（タップで切替）</Text><View style={styles.row}>{definition.nodes.map(node => <Choice key={node.key} label={node.title || node.key} active={group.node_keys.includes(node.key)} disabled={disabled} onPress={() => { const included = group.node_keys.includes(node.key); updateGroup({ node_keys: included ? group.node_keys.filter(key => key !== node.key) : [...group.node_keys, node.key], leader_key: included && group.leader_key === node.key ? null : group.leader_key }); }} />)}</View>
        <Text style={[styles.label, { color: theme.colors.onSurfaceVariant }]}>リーダー</Text><View style={styles.row}><Choice label="未設定" active={!group.leader_key} disabled={disabled} onPress={() => updateGroup({ leader_key: null })} />{group.node_keys.map(key => <Choice key={key} label={definition.nodes.find(node => node.key === key)?.title || key} active={group.leader_key === key} disabled={disabled} onPress={() => updateGroup({ leader_key: key })} />)}</View>
        <Choice label="グループを解除" disabled={disabled} onPress={() => { onChange({ ...definition, groups: definition.groups.filter(item => item.key !== group.key) }); setGroupKey(null); }} />
      </> : null}
    </ScrollView>}
  </View>;
}
const styles = StyleSheet.create({
  selection: { flexDirection: "row", alignItems: "center", flexWrap: "wrap", marginTop: 8 },
  row: { flexDirection: "row", flexWrap: "wrap" }, choice: { padding: 9, margin: 3, borderRadius: 5 }, active: { borderWidth: 1 }, disabled: { opacity: 0.45 },
  text: {}, muted: { marginVertical: 8 }, heading: {  fontSize: 17, fontWeight: "600", marginTop: 18, marginBottom: 5 },
  label: { marginTop: 10 }, input: {  borderWidth: 1, borderRadius: 4, padding: 8, minHeight: 40 }, inspector: { maxHeight: 600 }, block: { padding: 8, marginVertical: 4, borderWidth: 1 },
});

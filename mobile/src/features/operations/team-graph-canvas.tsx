import React, { useMemo, useRef, useState } from "react";
import { useTheme } from "react-native-paper";
import { PanResponder, Pressable, StyleSheet, Text, View } from "react-native";
import type { GraphDefinition, GraphNode } from "./team-graph-editor";

export function screenDeltaToWorld(delta: number, zoom: number): number {
  return delta / zoom;
}

const NODE_WIDTH = 150;
const NODE_HEIGHT = 68;

type Props = {
  definition: GraphDefinition;
  selectedKey: string | null;
  onSelect: (key: string) => void;
  onMove: (key: string, x: number, y: number) => void;
  disabled?: boolean;
};

function CanvasNode({ node, zoom, left, top, selected, onSelect, onMove, disabled }: {
  node: GraphNode; zoom: number; left: number; top: number; selected: boolean;
  onSelect: () => void; onMove: (x: number, y: number) => void; disabled?: boolean;
}) {
  const latest = useRef({ node, zoom, onMove, onSelect, disabled });
  latest.current = { node, zoom, onMove, onSelect, disabled };
  const start = useRef({ x: node.x, y: node.y });
  const responder = useMemo(() => PanResponder.create({
    onStartShouldSetPanResponder: () => !latest.current.disabled,
    onMoveShouldSetPanResponder: () => !latest.current.disabled,
    onPanResponderTerminationRequest: () => false,
    onPanResponderGrant: () => { start.current = { x: latest.current.node.x, y: latest.current.node.y }; latest.current.onSelect(); },
    onPanResponderMove: (_, gesture) => latest.current.onMove(start.current.x + screenDeltaToWorld(gesture.dx, latest.current.zoom), start.current.y + screenDeltaToWorld(gesture.dy, latest.current.zoom)),
  }), []);
  return <View {...responder.panHandlers} accessible accessibilityRole="button" accessibilityLabel={`${node.title || node.key} を選択して移動`} accessibilityActions={[{ name: "activate", label: "選択" }, { name: "increment", label: "右へ移動" }, { name: "decrement", label: "左へ移動" }, { name: "moveUp", label: "上へ移動" }, { name: "moveDown", label: "下へ移動" }]} onAccessibilityAction={event => {
    onSelect();
    if (disabled) return;
    const action = event.nativeEvent.actionName;
    if (action === "increment") onMove(node.x + 20, node.y);
    if (action === "decrement") onMove(node.x - 20, node.y);
    if (action === "moveUp") onMove(node.x, node.y - 20);
    if (action === "moveDown") onMove(node.x, node.y + 20);
  }} style={[styles.node, { left, top, width: NODE_WIDTH * zoom, height: NODE_HEIGHT * zoom }, selected && styles.selected]}>
    <Text numberOfLines={1} style={[styles.nodeText, { fontSize: Math.max(10, 13 * zoom) }]}>{node.title || node.key}</Text>
    <Text numberOfLines={1} style={[styles.muted, { fontSize: Math.max(9, 11 * zoom) }]}>{node.revision_id ? "固定 revision" : "最新版"}</Text>
  </View>;
}

export function TeamGraphCanvas({ definition, selectedKey, onSelect, onMove, disabled }: Props) {
  const theme = useTheme();
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 70, y: 70 });
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const fitGraph = () => {
    if (!definition.nodes.length || viewport.width <= 24 || viewport.height <= 24) return;
    const minX = Math.min(...definition.nodes.map(node => node.x)) - 20;
    const minY = Math.min(...definition.nodes.map(node => node.y)) - 30;
    const maxX = Math.max(...definition.nodes.map(node => node.x)) + NODE_WIDTH + 20;
    const maxY = Math.max(...definition.nodes.map(node => node.y)) + NODE_HEIGHT + 20;
    const nextZoom = Math.min(1, (viewport.width - 24) / (maxX - minX), (viewport.height - 24) / (maxY - minY));
    const scale = nextZoom;
    setZoom(scale);
    setPan({ x: (viewport.width - (maxX - minX) * scale) / 2 - minX * scale, y: (viewport.height - (maxY - minY) * scale) / 2 - minY * scale });
  };
  const panRef = useRef(pan); panRef.current = pan;
  const origin = useRef(pan);
  const responder = useMemo(() => PanResponder.create({
    onStartShouldSetPanResponder: () => true,
    onMoveShouldSetPanResponder: () => false,
    onPanResponderTerminationRequest: () => false,
    onPanResponderGrant: () => { origin.current = panRef.current; },
    onPanResponderMove: (_, gesture) => setPan({ x: origin.current.x + gesture.dx, y: origin.current.y + gesture.dy }),
  }), []);
  const byKey = new Map(definition.nodes.map(node => [node.key, node]));
  return <View>
    <View style={styles.toolbar}>
      <Pressable style={styles.controlTarget} accessibilityRole="button" accessibilityLabel="縮小" onPress={() => setZoom(value => value <= 0.4 ? value / 2 : Math.max(0.4, Math.round((value - 0.2) * 10) / 10))}><Text style={[styles.control, { color: theme.colors.primary }]}>−</Text></Pressable>
      <Text style={{ color: theme.colors.onSurfaceVariant }}>{Math.round(zoom * 100)}%</Text>
      <Pressable style={styles.controlTarget} accessibilityRole="button" accessibilityLabel="拡大" onPress={() => setZoom(value => value < 0.4 ? Math.min(0.4, value * 2) : Math.min(2, Math.round((value + 0.2) * 10) / 10))}><Text style={[styles.control, { color: theme.colors.primary }]}>＋</Text></Pressable>
      <Pressable style={styles.controlTarget} accessibilityRole="button" accessibilityLabel="全体を表示" onPress={fitGraph}><Text style={[styles.fitControl, { color: theme.colors.primary }]}>全体表示</Text></Pressable>
    </View>
    <Text style={[styles.hint, { color: theme.colors.onSurfaceVariant }]}>背景をドラッグして移動</Text>
    <View style={styles.viewport} onLayout={event => setViewport(event.nativeEvent.layout)} {...responder.panHandlers} accessibilityLabel="チーム構成キャンバス">
      {definition.groups.map(group => {
        const members = group.node_keys.map(key => byKey.get(key)).filter((node): node is GraphNode => !!node);
        if (!members.length) return null;
        const minX = Math.min(...members.map(node => node.x)) - 20; const minY = Math.min(...members.map(node => node.y)) - 30;
        const maxX = Math.max(...members.map(node => node.x)) + NODE_WIDTH + 20; const maxY = Math.max(...members.map(node => node.y)) + NODE_HEIGHT + 20;
        return <View pointerEvents="none" key={group.key} style={[styles.group, { left: pan.x + minX * zoom, top: pan.y + minY * zoom, width: (maxX - minX) * zoom, height: (maxY - minY) * zoom }]}><Text style={styles.groupText}>{group.title}</Text></View>;
      })}
      {definition.edges.map(edge => {
        const from = byKey.get(edge.source); const to = byKey.get(edge.target); if (!from || !to) return null;
        const x1 = pan.x + (from.x + NODE_WIDTH / 2) * zoom; const y1 = pan.y + (from.y + NODE_HEIGHT / 2) * zoom;
        const x2 = pan.x + (to.x + NODE_WIDTH / 2) * zoom; const y2 = pan.y + (to.y + NODE_HEIGHT / 2) * zoom;
        const length = Math.hypot(x2 - x1, y2 - y1);
        return <View pointerEvents="none" key={edge.key} style={[styles.line, { left: (x1 + x2 - length) / 2, top: (y1 + y2) / 2, width: length, transform: [{ rotate: `${Math.atan2(y2 - y1, x2 - x1)}rad` }] }]} />;
      })}
      {definition.nodes.map(node => <CanvasNode key={node.key} node={node} zoom={zoom} left={pan.x + node.x * zoom} top={pan.y + node.y * zoom} selected={selectedKey === node.key} onSelect={() => onSelect(node.key)} onMove={(x, y) => onMove(node.key, x, y)} disabled={disabled} />)}
    </View>
  </View>;
}

const styles = StyleSheet.create({
  viewport: { height: 400, overflow: "hidden", backgroundColor: "#181825", borderWidth: 1, borderColor: "#45475a" },
  toolbar: { flexDirection: "row", alignItems: "center", gap: 8, paddingTop: 8 },
  controlTarget: { minWidth: 44, minHeight: 44, alignItems: "center", justifyContent: "center", paddingHorizontal: 10 }, control: { fontSize: 24 }, fitControl: { fontSize: 14 }, hint: { fontSize: 13, marginBottom: 8 }, muted: { color: "#a6adc8" },
  node: { position: "absolute", backgroundColor: "#313244", borderColor: "#89b4fa", borderWidth: 1, borderRadius: 6, padding: 6, justifyContent: "center" },
  selected: { borderColor: "#f9e2af", borderWidth: 2 }, nodeText: { color: "#cdd6f4" },
  group: { position: "absolute", borderWidth: 1, borderColor: "#a6e3a1", backgroundColor: "rgba(166,227,161,0.06)" },
  groupText: { color: "#a6e3a1", fontSize: 11 }, line: { position: "absolute", height: 2, backgroundColor: "#89b4fa" },
});

import React from "react";
import { StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";
import { activityTime, duration, type WorkExecution } from "../../lib/operations-api";

type RunRow = {
  run: WorkExecution["runs"][number];
  depth: number;
};

function preorderRuns(runs: WorkExecution["runs"]): RunRow[] {
  const byParent = new Map<string, WorkExecution["runs"]>();
  const ids = new Set(runs.map((run) => run.id));
  for (const run of runs) {
    const parent = run.parent_run_id && ids.has(run.parent_run_id)
      ? run.parent_run_id
      : "root";
    const children = byParent.get(parent) ?? [];
    children.push(run);
    byParent.set(parent, children);
  }
  const visited = new Set<string>();
  const rows: RunRow[] = [];
  const visit = (run: WorkExecution["runs"][number], depth: number) => {
    if (visited.has(run.id)) return;
    visited.add(run.id);
    rows.push({ run, depth });
    for (const child of byParent.get(run.id) ?? []) visit(child, depth + 1);
  };
  for (const root of byParent.get("root") ?? []) visit(root, 0);
  // Keep malformed cycles and page fragments visible once, without recursion.
  for (const run of runs) visit(run, 0);
  return rows;
}

export function OperationsExecutionTree({ data }: { data: WorkExecution }) {
  const ids = new Set(data.runs.map((run) => run.id));
  const rows = preorderRuns(data.runs);
  return (
    <View accessibilityLabel="Agent Execution Tree">
      {rows.map(({ run, depth }) => (
        <View
          key={run.id}
          style={[styles.run, { marginLeft: Math.min(depth, 6) * 6 }]}
        >
          <Text variant="titleMedium" style={styles.title}>
            {run.acting_subagent_id || (run.parent_run_id ? "Subagent" : "Main agent")}
          </Text>
          <Text style={styles.text}>
            Run {run.id} · 仕事 {run.work_item_id || "未記録"} · {depth ? `Level ${depth} · ` : ""}{run.status} · Attempt {run.work_item_attempt ?? "未記録"}
          </Text>
          <View style={styles.currentPosition}>
            <Text style={styles.text}>現在の工程: {run.phase ?? "未記録"}</Text>
            <Text style={styles.text}>現在のTool: {run.tool ?? "未記録"}</Text>
          </View>
          <Text style={styles.small}>開始 {activityTime(run.started_at)}</Text>
          <Text style={styles.small}>終了 {activityTime(run.ended_at)}</Text>
          <Text style={styles.small}>経過 {duration(run.elapsed_seconds)}</Text>
          <Text style={styles.small}>最終活動 {activityTime(run.last_event_at)}</Text>
          {run.error_code ? <Text style={styles.error}>{run.error_code}</Text> : null}
          {run.parent_run_id && !ids.has(run.parent_run_id) ? (
            <Text style={styles.error}>上位実行はこのページに未取得です</Text>
          ) : null}
          <View style={styles.leaves}>
            {data.events
              .filter((event) => event.run_id === run.id && event.phase)
              .sort((a, b) => a.sequence - b.sequence)
              .map((event) => (
                <View key={event.id} style={styles.leaf}>
                  <Text style={styles.text}>Phase: {event.phase}</Text>
                  <Text style={styles.small}>
                    {event.status || event.event_type} · {activityTime(event.created_at)}
                  </Text>
                  {event.error_code ? <Text style={styles.error}>{event.error_code}</Text> : null}
                </View>
              ))}
            {data.tool_calls
              .filter((tool) => tool.run_id === run.id)
              .map((tool) => (
                <View key={tool.id} style={styles.leaf}>
                  <Text style={styles.text}>Tool: {tool.tool_name}</Text>
                  <Text style={styles.text}>{tool.status}</Text>
                  <Text style={styles.small}>開始 {activityTime(tool.started_at)}</Text>
                  <Text style={styles.small}>終了 {activityTime(tool.ended_at)}</Text>
                  <Text style={styles.small}>
                    経過 {duration(tool.duration_ms == null ? null : tool.duration_ms / 1000)}
                  </Text>
                  {tool.error_code ? <Text style={styles.error}>{tool.error_code}</Text> : null}
                </View>
              ))}
          </View>
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  run: {
    padding: 12,
    borderColor: "#45475a",
    borderWidth: 1,
    borderRadius: 12,
    marginBottom: 12,
    backgroundColor: "#1e1e2e",
  },
  title: { color: "#cdd6f4", marginBottom: 6 },
  text: { color: "#bac2de", fontSize: 12, lineHeight: 19 },
  small: { color: "#a6adc8", fontSize: 11, lineHeight: 18 },
  error: { color: "#f9e2af", fontSize: 12, marginVertical: 4 },
  leaves: {
    borderLeftColor: "#585b70",
    borderLeftWidth: 2,
    paddingLeft: 8,
    marginTop: 10,
  },
  currentPosition: {
    borderColor: "#89b4fa",
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 6,
    padding: 6,
    marginTop: 6,
    marginBottom: 4,
  },
  leaf: { paddingVertical: 6 },
});

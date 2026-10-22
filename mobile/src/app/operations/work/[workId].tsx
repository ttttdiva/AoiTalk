import React, { useCallback, useEffect, useRef, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Button, IconButton, Text } from "react-native-paper";
import { ScreenHeader } from "../../../components/screen-header";
import { useAuth } from "../../../contexts/AuthContext";
import { OperationsExecutionTree } from "../../../features/operations/operations-execution-tree";
import { useOperations } from "../../../features/operations/use-operations";
import { activityTime, attentionLabel, duration, getWorkExecution, type WorkExecution, workConversationReference } from "../../../lib/operations-api";

const merge = <T extends { id: string }>(a: T[], b: T[]) => Array.from(new Map([...a,...b].map(item => [item.id,item])).values());
const pageKinds = ["runs", "events", "tools"] as const;
export default function OperationsWorkScreen() {
  const router = useRouter(); const insets = useSafeAreaInsets(); const { user } = useAuth();
  const params = useLocalSearchParams<{ workId: string }>();
  const workId = typeof params.workId === "string" ? params.workId : "";
  const [page, setPage] = useState<{ kind: "runs" | "events" | "tools"; cursor: string } | null>(null);
  const accumulated = useRef<WorkExecution | null>(null);
  const [data, setData] = useState<WorkExecution | null>(null);
  const loader = useCallback((server: string, signal: AbortSignal) => getWorkExecution(server, workId, signal, page?.kind, page?.cursor), [workId, page]);
  const request = useOperations(`${user?.user_id}:${workId}`, loader);
  useFocusEffect(useCallback(() => {
    accumulated.current = null; setData(null); setPage(null);
    return () => { accumulated.current = null; setData(null); };
  }, [user?.user_id, workId]));
  useEffect(() => { accumulated.current = null; setData(null); setPage(null); }, [request.sourceKey]);
  useEffect(() => {
    if (!request.data) return;
    const next = request.data; const previous = accumulated.current;
    const combined = page && previous ? { ...previous, work: next.work, observed_at: next.observed_at,
      runs: merge(previous.runs,next.runs), events: merge(previous.events,next.events), tool_calls: merge(previous.tool_calls,next.tool_calls),
      [`${page.kind}_page`]: next[`${page.kind}_page`], } : next;
    accumulated.current = combined; setData(combined);
  }, [request.data, page]);
  const refresh = () => { accumulated.current = null; setData(null); if (page) setPage(null); else void request.refresh(); };
  const hasTruncatedHistory = Boolean(data && pageKinds.some(kind => data[`${kind}_page`].truncated));
  const hasAdditionalHistory = Boolean(data && pageKinds.some(kind => data[`${kind}_page`].has_more && data[`${kind}_page`].next_cursor));
  const conversationReference = data ? workConversationReference(data.work) : null;
  return <View style={styles.screen}>
    <ScreenHeader
      title="実行の詳細"
      onBack={() => router.replace("/operations")}
      right={
        <IconButton
          icon="refresh"
          iconColor="#89b4fa"
          disabled={request.loading}
          accessibilityLabel="実行状況を更新"
          onPress={refresh}
          style={styles.headerActionButton}
        />
      }
    />
    <ScrollView contentContainerStyle={[styles.content,{paddingBottom: insets.bottom+24}]}>
      <Text variant="titleLarge" style={styles.title}>Agent Execution Tree</Text>
      <Text style={styles.muted}>状態確認専用です。承認・再試行は正本で行います。</Text>
      {request.error ? <Text accessibilityRole="alert" style={styles.warning}>{request.error}{data ? " 表示中の記録は最新と確認できていません。" : ""}</Text> : null}
      {request.loading ? <Text style={styles.muted}>読み込み中…</Text> : null}
      {data ? <>
        <View style={styles.summary}>
          <Text style={styles.title}>Work {data.work.id}</Text><Text style={styles.text}>{data.work.state}</Text>
          <Text style={styles.text}>Attempt {data.work.execution_summary.attempt_count}/{data.work.execution_summary.max_attempts}</Text>
          <Text style={styles.text}>Phase: {data.work.execution_summary.phase ?? "未記録"}</Text>
          <Text style={styles.text}>Tool: {data.work.execution_summary.tool ?? "未記録"}</Text>
          <Text style={styles.text}>経過: {duration(data.work.execution_summary.elapsed_seconds)}</Text>
          <Text style={styles.text}>最終活動: {activityTime(data.work.execution_summary.last_activity_at)}</Text>
          <Text style={styles.text}>Heartbeat: {activityTime(data.work.execution_summary.heartbeat_at)}</Text>
          <Text style={styles.text}>Lease: {data.work.execution_summary.lease_health}</Text>
          <Text style={styles.warning}>{data.work.execution_summary.attention_reasons.map(attentionLabel).join(" / ")}</Text>
          {data.work.execution_summary.blocker_code || data.work.execution_summary.safe_error_code ? <Text style={styles.warning}>{data.work.execution_summary.blocker_code || data.work.execution_summary.safe_error_code}</Text> : null}
          {data.work.execution_summary.observation_status !== "available" ? <Text style={styles.warning}>{data.work.execution_summary.observation_status === "partial" ? "正本の実行記録は一部だけ確認できています" : "正本の実行記録を確認できていません"}</Text> : null}
          {data.work.task_id ? <Button onPress={() => router.push({pathname:"/(tabs)/tasks/[taskId]",params:{taskId:data.work.task_id!}})}>正本のタスクを開く</Button> : data.work.source_href ? <Text style={styles.muted}>正本の操作はWeb版Operationsで確認できます</Text> : null}
          {conversationReference ? <Button icon="chat-outline" onPress={() => router.push({ pathname: "/(tabs)/chat/[sessionId]", params: { sessionId: conversationReference.id } })}>関連Chatを開く</Button> : null}
        </View>
        {hasTruncatedHistory ? <Text accessibilityRole="alert" style={styles.warning}>実行が多いため一部の履歴を省略しています。現在の実行を優先して表示しています。</Text> : null}
        {data.runs.length ? <OperationsExecutionTree data={data} /> : <Text style={styles.muted}>AgentRunはまだ記録されていません</Text>}
        {pageKinds.map(kind => data[`${kind}_page`].has_more && data[`${kind}_page`].next_cursor ? <Button key={kind} disabled={request.loading} onPress={() => { setData(null); setPage({kind,cursor:data[`${kind}_page`].next_cursor!}); }}>{({runs:"実行",events:"工程",tools:"ツール"})[kind]}をさらに読む</Button> : null)}
        <Text style={styles.muted}>最終確認 {activityTime(data.observed_at)} · 件数上限付きの記録です{hasAdditionalHistory ? "。追加で読める履歴があります" : ""}。</Text>
      </> : null}
    </ScrollView>
  </View>;
}
const styles = StyleSheet.create({ screen:{flex:1,backgroundColor:"#11111b"},headerActionButton:{width:44,height:44,minWidth:44,minHeight:44,margin:0},content:{padding:16,gap:12},title:{color:"#cdd6f4",flexShrink:1},text:{color:"#bac2de",fontSize:12,lineHeight:20},muted:{color:"#a6adc8",fontSize:12,lineHeight:18},warning:{color:"#f9e2af",fontSize:12,lineHeight:18},summary:{padding:14,borderWidth:1,borderColor:"#45475a",borderRadius:12,gap:5}});



import React, { useCallback, useEffect, useRef, useState } from "react";
import { Modal, ScrollView, StyleSheet, View } from "react-native";
import { ActivityIndicator, Button, IconButton, Text } from "react-native-paper";
import { getOutboxDiagnostics, type OutboxDiagnostic } from "../repositories/outbox";
import {
  useSyncStatusStore,
  type OutboxSyncState,
  type SyncDocsScope,
  type SyncOutcome,
  type SyncStage,
  type SyncStatus,
} from "../stores/sync-status";
import { useNetworkStore } from "../stores/network";
import { runSync } from "../sync/engine";

const OUTBOX_LABELS: Record<OutboxSyncState, string> = {
  empty: "なし", pending: "未送信の更新あり", retrying: "再試行待ち", failed: "確認が必要な更新あり",
};
const STATUS_LABELS: Record<SyncStatus, string> = {
  idle: "待機中", pending: "送信待ち", syncing: "同期中", success: "完了", partial: "一部失敗", error: "失敗",
};
const STAGE_LABELS: Record<SyncStage, string> = {
  outbox: "未送信の更新",
  general: "一般データ",
  docs: "Docs",
  "pending-conversations": "保留中の会話",
  "pending-clips": "保留中のクリップ",
  "clip-targets": "クリップ対象",
  profiles: "プロファイル",
};
const DOCS_SCOPE_LABELS: Record<SyncDocsScope, string> = {
  personal: "個人",
  shared: "共有",
  project: "プロジェクト",
};
const OUTCOME_LABELS: Record<SyncOutcome, string> = {
  success: "完了",
  partial: "一部失敗",
  error: "失敗",
  aborted: "中断",
};
const REASONS: Record<OutboxDiagnostic["errorKind"], string> = {
  conflict: "サーバー上の変更と競合しています。採用する変更の確認が必要です。",
  missing_entity: "サーバーに対象が見つかりません。端末の更新は保持しています。",
  scope: "保存先または権限を確認してください。",
  missing_base: "サーバーの更新版を確認する必要があります。",
  server_error: "サーバーが操作を受理できませんでした。",
  pending: "送信を待っています。",
};

function indicator(status: SyncStatus, outbox: OutboxSyncState) {
  if (status === "error" || outbox === "failed") return {
    label: outbox === "empty" ? "同期失敗" : "同期失敗（送信待ちあり）", icon: "alert-circle-outline", color: "#f38ba8",
  };
  if (outbox === "retrying") return { label: "再試行待ち", icon: "clock-alert-outline", color: "#f9e2af" };
  if (status === "syncing") return { label: "同期中…", icon: "sync", color: "#89b4fa" };
  if (outbox === "pending" || status === "pending") return { label: "送信待ち", icon: "cloud-upload-outline", color: "#89b4fa" };
  if (status === "partial") return { label: "一部失敗", icon: "alert-circle-outline", color: "#f9e2af" };
  return { label: status === "success" ? "同期済" : "同期待機中", icon: status === "success" ? "cloud-check-outline" : "cloud-outline", color: "#a6adc8" };
}

function areaLabel(table: string) {
  if (table.startsWith("knowledge_")) return "Docs";
  return ({ tasks: "タスク", projects: "プロジェクト", time_entries: "時間記録",
    conversation_sessions: "会話" } as Record<string, string>)[table] ?? "保存データ";
}

function formatNumber(value: number | null): string {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.round(value).toLocaleString("ja-JP")
    : "未取得";
}

function formatElapsed(startedAt: number | null, now: number): string {
  if (
    typeof startedAt !== "number"
    || !Number.isFinite(startedAt)
    || !Number.isFinite(now)
  ) return "未取得";
  const elapsedSeconds = (now - startedAt) / 1000;
  if (!Number.isFinite(elapsedSeconds)) return "未取得";
  const seconds = Math.max(0, Math.round(elapsedSeconds));
  if (!Number.isFinite(seconds)) return "未取得";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes > 0 ? `${minutes}分${rest}秒` : `${rest}秒`;
}

function formatFinishedAt(value: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未取得";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("ja-JP") : "未取得";
}

/** The header stays one fixed-size control; queue reads require opening details. */
export function SyncStatusIndicator() {
  const syncStatus = useSyncStatusStore();
  const {
    status,
    outboxState: outbox,
    authScope: scope,
    startedAt,
    flightSequence,
    stage,
    stageStartedAt,
    generalPage,
    generalPendingTables,
    docsScope,
    docsPage,
    docsRetryAttempt,
    lastOutcome,
    lastFinishedAt,
  } = syncStatus;
  const online = useNetworkStore((state) => state.connected ?? state.online);
  const [visible, setVisible] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [result, setResult] = useState<{ scope: string; rows: OutboxDiagnostic[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const retryBusy = useRef(false);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const currentVisible = useRef(visible);
  currentVisible.current = visible;
  const state = indicator(status, outbox);
  const rows = result?.scope === scope ? result.rows : null;
  const currentStage = status === "syncing" && flightSequence != null ? stage : null;

  const load = useCallback(async () => {
    const request = ++generation.current;
    setResult(null); setError(null);
    if (!scope?.startsWith("auth:")) { setLoading(false); return; }
    const active = () => generation.current === request && currentScope.current === scope;
    setLoading(true);
    try {
      const next = await getOutboxDiagnostics(scope);
      if (active()) setResult({ scope, rows: next });
    } catch {
      if (active()) setError("保留理由を取得できませんでした。更新は保持しています。");
    } finally {
      if (active()) setLoading(false);
    }
  }, [scope]);

  useEffect(() => {
    if (visible) void load();
    else { ++generation.current; setResult(null); setError(null); setLoading(false); }
    return () => { ++generation.current; };
  }, [visible, load]);

  useEffect(() => {
    if (!visible || status !== "syncing") return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [visible, status, scope, startedAt, stageStartedAt]);

  const retry = async () => {
    if (retryBusy.current || !online || !scope?.startsWith("auth:")) return;
    const expectedScope = scope;
    const active = () => currentScope.current === expectedScope && currentVisible.current;
    retryBusy.current = true; setRetrying(true); setError(null);
    try {
      if (!active()) return;
      await runSync();
      if (active()) await load();
    }
    catch { if (active()) setError("再同期できませんでした。接続やログイン状態を確認してください。更新は保持しています。"); }
    finally { retryBusy.current = false; setRetrying(false); }
  };

  return <>
    <IconButton
      testID="sync-status-indicator"
      icon={state.label === "同期中…" ? () => <ActivityIndicator testID="sync-status-spinner" accessible={false} size={18} color={state.color} /> : state.icon}
      size={18}
      iconColor={state.color}
      style={styles.indicator}
      accessibilityLabel={state.label}
      accessibilityHint="同期の詳細と再試行を開きます"
      onPress={() => setVisible(true)}
    />
    {visible ? <Modal transparent visible onRequestClose={() => setVisible(false)} animationType="fade">
      <View style={styles.backdrop}>
        <View style={styles.panel} accessibilityViewIsModal testID="sync-status-details">
          <View style={styles.heading}>
            <Text accessibilityRole="header" style={styles.title}>同期の詳細</Text>
            <IconButton icon="close" size={20} style={styles.indicator} iconColor="#cdd6f4"
              accessibilityLabel="同期の詳細を閉じる" onPress={() => setVisible(false)} />
          </View>
          <ScrollView style={styles.body} contentContainerStyle={styles.content}>
            <Text style={styles.text}>現在の同期: {STATUS_LABELS[status]}</Text>
            <Text style={styles.text}>送信待ち: {OUTBOX_LABELS[outbox]}</Text>
            <View style={styles.progress}>
              <Text style={styles.text}>全体同期の現在工程: {currentStage ? STAGE_LABELS[currentStage] : "未取得"}</Text>
              <Text style={styles.text}>工程経過: {currentStage ? formatElapsed(stageStartedAt, now) : "未取得"}</Text>
              <Text style={styles.text}>開始からの経過: {status === "syncing" ? formatElapsed(startedAt, now) : "未取得"}</Text>
              {currentStage === "general" ? <>
                <Text style={styles.text}>一般データのページ: {formatNumber(generalPage)}</Text>
                <Text style={styles.text}>残りテーブル: {formatNumber(generalPendingTables)}</Text>
              </> : null}
              {currentStage === "docs" ? <>
                <Text style={styles.text}>Docsの対象: {docsScope ? DOCS_SCOPE_LABELS[docsScope] : "未取得"}</Text>
                <Text style={styles.text}>Docsのページ: {formatNumber(docsPage)}</Text>
                <Text style={styles.text}>Docsの再試行: {formatNumber(docsRetryAttempt)}</Text>
              </> : null}
              <Text style={styles.text}>全体同期の直近終了結果: {lastOutcome ? OUTCOME_LABELS[lastOutcome] : "未取得"}</Text>
              <Text style={styles.text}>全体同期の直近終了時刻: {formatFinishedAt(lastFinishedAt)}</Text>
            </View>
            {loading ? <ActivityIndicator accessibilityLabel="保留理由を確認中" /> : null}
            {rows ? <>
              <Text style={styles.text}>確認時点の未送信: {rows.length}件</Text>
              {rows.slice(0, 20).map((row) => <View key={row.opId} style={styles.queueRow}>
                <Text style={styles.text}>{areaLabel(row.tableName)}・{row.action === "create" ? "新規保存" : row.action === "delete" || row.action === "archive" ? "削除" : "更新"}</Text>
                <Text style={styles.text}>{REASONS[row.errorKind]}</Text>
              </View>)}
              {rows.length > 20 ? <Text style={styles.text}>最初の20件を表示しています。</Text> : null}
            </> : null}
            {!scope?.startsWith("auth:") ? <Text style={styles.text}>ログイン状態を確認してください。</Text> : null}
            {!online ? <Text style={styles.text}>接続が戻ってから再同期できます。</Text> : null}
            {error ? <Text accessibilityRole="alert" style={styles.error}>{error}</Text> : null}
          </ScrollView>
          <Button mode="outlined" style={styles.retry} loading={retrying}
            disabled={!online || !scope?.startsWith("auth:") || retrying || status === "syncing"}
            onPress={() => { void retry(); }}>再同期</Button>
        </View>
      </View>
    </Modal> : null}
  </>;
}

const styles = StyleSheet.create({
  indicator: { width: 44, height: 44, margin: 0, flexShrink: 0 },
  backdrop: { flex: 1, justifyContent: "center", alignItems: "center", padding: 24, backgroundColor: "#00000088" },
  panel: { width: "100%", maxHeight: "85%", borderRadius: 16, padding: 16, backgroundColor: "#1e1e2e" },
  heading: { flexDirection: "row", alignItems: "center", gap: 8 },
  title: { flex: 1, color: "#cdd6f4", fontSize: 20, fontWeight: "bold" },
  body: { flexShrink: 1 },
  content: { gap: 12, paddingVertical: 12 },
  text: { color: "#cdd6f4", fontSize: 14 },
  progress: { gap: 6 },
  error: { color: "#f38ba8", fontSize: 14 },
  queueRow: { gap: 4, paddingTop: 10, borderTopWidth: 1, borderTopColor: "#343149" },
  retry: { minHeight: 44, marginTop: 8 },
});

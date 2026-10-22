import React, { useEffect, useState, useRef, useCallback } from "react";
import { Alert, StyleSheet, View } from "react-native";
import { useFocusEffect } from "expo-router";
import { Button, ProgressBar, Text } from "react-native-paper";
import { ScreenShell } from "../../../components/screen-primitives";
import { useAuth } from "../../../contexts/AuthContext";
import { isApiConnectionError, isApiTimeoutError } from "../../../lib/api-client";
import { forceDocsResync, type DocsResyncProgress } from "../../../sync/engine";

function formatError(error: unknown) {
  if (error instanceof Error && error.message.includes("Docs sync snapshot changed")) {
    return "サーバー側でDocsが更新されたため、再同期を完了できませんでした。少し待ってからもう一度実行してください。";
  }
  if (isApiConnectionError(error) || isApiTimeoutError(error)) {
    return "通信が途中で切れました。自動再試行でも復旧できませんでした。サーバー接続を確認して、もう一度実行してください。";
  }
  return error instanceof Error ? error.message : "Docsの再取得に失敗しました。";
}

function formatElapsed(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes > 0 ? `${minutes}分${rest}秒` : `${rest}秒`;
}

function formatProgress(progress: DocsResyncProgress | null): string {
  if (!progress) return "同期処理を開始しています…";
  if (progress.phase === "queued") return "他の同期処理の完了を待っています…";
  if (progress.phase === "preparing") return "サーバー接続と同期状態を確認しています…";
  if (progress.phase === "retrying") {
    return `通信が切れたため自動再試行中（${progress.retryAttempt ?? 1}回目）…`;
  }
  if (progress.phase === "finalizing") return "取得したDocsを端末へ反映しています…";
  if (progress.total != null) {
    return `${progress.completed.toLocaleString()} / ${progress.total.toLocaleString()}件 ・ ページ${Math.max(1, progress.page)}`;
  }
  return `ページ${Math.max(1, progress.page)}を取得しています…`;
}

export default function DocsResyncSettingsScreen() {
  const { isAuthenticated, user } = useAuth();
  const authScope = isAuthenticated ? `auth:${user?.user_id ?? "unknown"}` : null;
  const authScopeRef = useRef(authScope);
  authScopeRef.current = authScope;
  const runGeneration = useRef(0);
  const runningRef = useRef(false);
  const focusedRef = useRef(false);
  const mountedRef = useRef(false);
  const [progress, setProgress] = useState<DocsResyncProgress | null>(null);
  const [running, setRunning] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  useEffect(() => {
    mountedRef.current = true;
    runningRef.current = false;
    setRunning(false);
    setProgress(null);
    return () => {
      mountedRef.current = false;
      ++runGeneration.current;
    };
  }, [authScope]);

  useFocusEffect(useCallback(() => {
    focusedRef.current = true;
    return () => {
      // Navigation does not cancel the engine operation. Keep its progress and
      // busy state until the actual promise settles, including on return.
      focusedRef.current = false;
    };
  }, []));

  useEffect(() => {
    if (!running) {
      setElapsedSeconds(0);
      return;
    }
    const startedAt = Date.now();
    setElapsedSeconds(0);
    const timer = setInterval(() => {
      setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => clearInterval(timer);
  }, [running]);

  const start = () => {
    if (!authScope || runningRef.current) return;
    const requestedScope = authScope;
    Alert.alert(
      "Docs再同期",
      "端末のDocs同期状態を再構築し、サーバーから最新データを再取得します。未送信の編集とサーバー上のデータは削除しません。",
      [
        { text: "キャンセル", style: "cancel" },
        {
          text: "再同期",
          style: "destructive",
          onPress: () => {
            if (!focusedRef.current || runningRef.current ||
                authScopeRef.current !== requestedScope) return;
            const generation = ++runGeneration.current;
            const isCurrent = () => mountedRef.current &&
              generation === runGeneration.current &&
              authScopeRef.current === requestedScope;
            runningRef.current = true;
            setProgress(null);
            setRunning(true);
            void forceDocsResync((next) => {
              if (isCurrent()) setProgress(next);
            })
              .then(() => {
                if (isCurrent() && focusedRef.current) Alert.alert("Docs再同期完了", "サーバーからDocsを再取得しました。");
              })
              .catch((error) => {
                if (isCurrent() && focusedRef.current) Alert.alert("Docs再同期失敗", formatError(error));
              })
              .finally(() => {
                if (!isCurrent()) return;
                runningRef.current = false;
                setRunning(false);
                setProgress(null);
              });
          },
        },
      ],
    );
  };

  const progressValue = progress?.total != null
    ? progress.total === 0 ? 1 : Math.min(1, progress.completed / progress.total)
    : 0;

  return (
    <ScreenShell scroll style={styles.container} contentContainerStyle={styles.content}>
      <Text style={styles.description}>端末側のDocs同期状態をリセットし、サーバーから最新データを再取得します。</Text>
      <Button mode="outlined" textColor="#f38ba8" style={styles.button} loading={running} disabled={!isAuthenticated || running} onPress={start}>
        Docsを再同期
      </Button>
      {!isAuthenticated ? <Text style={styles.note}>サーバーログイン中のみ利用できます。</Text> : null}
      {running ? (
        <View style={styles.progress}>
          <ProgressBar
            progress={progressValue}
            indeterminate={progress?.total == null || progress.phase === "retrying"}
            color="#f38ba8"
          />
          <View style={styles.progressMeta}>
            <Text style={styles.progressText}>{formatProgress(progress)}</Text>
            <Text style={styles.elapsedText}>経過 {formatElapsed(elapsedSeconds)}</Text>
          </View>
        </View>
      ) : null}
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#10101b" },
  content: { padding: 16, gap: 12 },
  description: { color: "#a9a7c5", fontSize: 13, lineHeight: 19 },
  button: { alignSelf: "flex-start", borderColor: "#f38ba8" },
  note: { color: "#f9e2af", fontSize: 12 },
  progress: { gap: 8, marginTop: 6 },
  progressMeta: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
  },
  progressText: { color: "#cdd6f4", fontSize: 12, flex: 1 },
  elapsedText: { color: "#7f7d99", fontSize: 11, flexShrink: 0 },
});

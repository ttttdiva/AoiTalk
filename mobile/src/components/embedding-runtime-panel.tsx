import React, { useCallback, useEffect, useRef, useState } from "react";
import { View } from "react-native";
import { Button, Surface, Text } from "react-native-paper";
import { embeddingRuntimeApi, type EmbeddingRuntimeStatus } from "../lib/embedding-runtime-api";

const LABELS: Record<EmbeddingRuntimeStatus["state"], string> = {
  unloaded: "未ロード", loading: "読み込み中", loaded: "待機中", busy: "処理中",
  releasing: "解放中", failed: "エラー", closed: "終了",
};

export function EmbeddingRuntimePanel() {
  const [status, setStatus] = useState<EmbeddingRuntimeStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [pending, setPending] = useState<"status" | "release" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const active = useRef(false);
  const request = useRef<AbortController | null>(null);
  const run = useCallback(async (kind: "status" | "release") => {
    if (!active.current || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setPending(kind);
    setError(null);
    setMessage(null);
    try {
      if (kind === "status") {
        const result = await embeddingRuntimeApi.status(controller.signal);
        if (!active.current || request.current !== controller) return;
        setStatus(result.status);
      } else {
        const result = await embeddingRuntimeApi.release(controller.signal);
        if (!active.current || request.current !== controller) return;
        setStatus(result.status);
        if (result.accepted) setMessage(result.released ? "メモリを解放しました。次の推論時に再ロードします。" : "モデルは既に解放されています。");
        else setError(result.reason === "busy" ? "処理中または待機中のリクエストがあります。完了後に状態を更新してください。" : "解放できませんでした。状態を更新して確認してください。");
      }
      setLoaded(true);
    } catch {
      if (active.current && request.current === controller) setError(kind === "release"
        ? "解放結果を確認できませんでした。状態を更新してください。"
        : "状態を取得できませんでした。サーバー接続と管理者権限を確認してください。");
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (active.current) setPending(null);
      }
    }
  }, []);
  useEffect(() => {
    active.current = true;
    queueMicrotask(() => { void run("status"); });
    return () => { active.current = false; request.current?.abort(); request.current = null; };
  }, [run]);
  const busy = Boolean(status && (status.pending_requests > 0 || ["busy", "loading", "releasing", "closed"].includes(status.state)));
  const canRelease = loaded && status?.process_running && !busy && !pending && !error;
  return <Surface elevation={0} style={{ padding: 16, gap: 10, borderRadius: 12 }}>
    <Text variant="titleMedium">サーバーのEmbeddingGemma 2</Text>
    <Text>{loaded ? status ? LABELS[status.state] : "未起動" : "確認中"}</Text>
    <Text variant="bodySmall">検索・索引作成で共有するモデルです。状態確認では読み込みません。解放後は次の推論で再ロードします。</Text>
    {status && <>
      <Text>実行 / 待機: {status.active_requests} / {status.queued_requests}</Text>
      <Text>配置 / 精度: {status.effective_device ?? status.configured_device} / {status.precision ?? "未ロード"}</Text>
      <Text>自動解放: {status.idle_timeout_seconds === 0 ? "OFF" : `${status.idle_timeout_seconds} 秒`} / PID: {status.pid ?? "なし"}</Text>
      {status.metrics_source === "last_worker_response" && typeof status.metrics.rss_bytes === "number" && <Text variant="bodySmall">メモリ: {(status.metrics.rss_bytes / 1024 / 1024).toFixed(1)} MiB（直近の推論応答時点）</Text>}
    </>}
    {status?.last_error && <Text accessibilityRole="alert">{status.last_error}</Text>}
    {error && <Text accessibilityRole="alert">{error}</Text>}
    {message && <Text accessibilityLiveRegion="polite">{message}</Text>}
    <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
      <Button mode="outlined" disabled={Boolean(pending)} onPress={() => void run("status")}>状態を更新</Button>
      <Button mode="outlined" disabled={!canRelease} loading={pending === "release"} onPress={() => void run("release")}>メモリを解放</Button>
    </View>
  </Surface>;
}

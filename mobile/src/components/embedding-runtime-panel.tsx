import React, { useCallback, useEffect, useRef, useState } from "react";
import { AppState, View } from "react-native";
import { Button, Surface, Switch, Text } from "react-native-paper";
import { embeddingRuntimeApi, type EmbeddingRuntimeStatus } from "../lib/embedding-runtime-api";
import { apiHttpErrorDetail, embeddingMasterPhase } from "../lib/embedding-master-setting";

const LABELS: Record<EmbeddingRuntimeStatus["state"], string> = {
  unloaded: "未ロード", loading: "読み込み中", loaded: "待機中", busy: "処理中",
  releasing: "解放中", failed: "エラー", closed: "終了",
};
const SAVED_MESSAGES = {
  on: "有効にしました。モデルは必要になった時点で読み込み、停止していた索引作成も再開します。",
  off: "無効にしました。進行中の推論は完了を待ち、OFF中の索引更新は保留します。ONに戻すと再開します。",
};
const POLL_INTERVAL_MS = 5000;

export function EmbeddingRuntimePanel({ isAdmin = false }: { isAdmin?: boolean } = {}) {
  const canManage = isAdmin === true;
  const [status, setStatus] = useState<EmbeddingRuntimeStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [pending, setPending] = useState<"status" | "release" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  // null は未取得または不正値。確認できない間は ON と推測しない。
  const [enabled, setEnabledState] = useState<boolean | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const active = useRef(false);
  const manageAllowed = useRef(false);
  const request = useRef<AbortController | null>(null);
  const settingsRequest = useRef<AbortController | null>(null);
  const saveRequest = useRef<AbortController | null>(null);
  const enabledRef = useRef<boolean | null>(null);
  enabledRef.current = enabled;

  const run = useCallback(async (kind: "status" | "release") => {
    // 保存中は runtime 要求を出さない（保存後の確認だけが有効な観測になる）。
    if (!active.current || !manageAllowed.current || request.current || saveRequest.current) return;
    const controller = new AbortController();
    request.current = controller;
    setPending(kind);
    setError(null);
    setMessage(null);
    try {
      if (kind === "status") {
        const result = await embeddingRuntimeApi.status(controller.signal);
        if (!active.current || !manageAllowed.current || request.current !== controller) return;
        setStatus(result.status);
      } else {
        const result = await embeddingRuntimeApi.release(controller.signal);
        if (!active.current || !manageAllowed.current || request.current !== controller) return;
        setStatus(result.status);
        if (result.accepted) setMessage(!result.released ? "モデルは既に解放されています。"
          : enabledRef.current === true ? "メモリを解放しました。次の推論時に再ロードします。"
            : enabledRef.current === false ? "メモリを解放しました。ONに戻すまでモデルは再ロードされません。"
              : "メモリを解放しました。");
        else setError(result.reason === "busy" ? "処理中または待機中のリクエストがあります。完了後に状態を更新してください。" : "解放できませんでした。状態を更新して確認してください。");
        // 拒否（accepted:false）で status が null なのは不在の確認ではない。受理された not_loaded の null だけが確定した不在。
        setLoaded(result.accepted === true || result.status != null);
        return;
      }
      setLoaded(true);
    } catch {
      // 取得に失敗した観測は確認済みとして扱わない（最後の詳細は残すが、解放済みの確定表示はしない）。
      if (active.current && manageAllowed.current && request.current === controller) setLoaded(false);
      if (active.current && manageAllowed.current && request.current === controller) setError(kind === "release"
        ? "解放結果を確認できませんでした。状態を更新してください。"
        : "状態を取得できませんでした。サーバー接続と管理者権限を確認してください。");
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (active.current && manageAllowed.current) setPending(null);
      }
    }
  }, []);

  // マスタースイッチの現在値を読む。保存開始時は進行中の読み取りを中断し、古い値で上書きさせない。
  const loadSettings = useCallback(async () => {
    if (!active.current || settingsRequest.current || saveRequest.current) return;
    const controller = new AbortController();
    settingsRequest.current = controller;
    try {
      const value = await embeddingRuntimeApi.getEnabled(controller.signal);
      if (!active.current || settingsRequest.current !== controller) return;
      setEnabledState(value);
      setSettingsError(null);
    } catch (cause) {
      if (!active.current || settingsRequest.current !== controller) return;
      setEnabledState(null);
      setSettingsError(apiHttpErrorDetail(cause) ?? (cause instanceof Error && cause.message
        ? cause.message : "設定を取得できませんでした。"));
    } finally {
      if (settingsRequest.current === controller) settingsRequest.current = null;
    }
  }, []);

  const setEnabled = useCallback(async (next: boolean) => {
    if (!active.current || !manageAllowed.current || saveRequest.current) return;
    const controller = new AbortController();
    saveRequest.current = controller;
    settingsRequest.current?.abort();
    settingsRequest.current = null;
    // 保存前に開始した runtime 要求は破棄し、確認済みの状態も無効化する。
    request.current?.abort();
    request.current = null;
    setPending(null);
    setLoaded(false);
    setError(null);
    setMessage(null);
    setSaving(true);
    setSaveError(null);
    setSaveMessage(null);
    let reload = false;
    try {
      const saved = await embeddingRuntimeApi.setEnabled(next, controller.signal);
      if (!active.current || !manageAllowed.current || saveRequest.current !== controller) return;
      setEnabledState(saved);
      setSettingsError(null);
      setSaveMessage(saved ? SAVED_MESSAGES.on : SAVED_MESSAGES.off);
    } catch (cause) {
      if (!active.current || !manageAllowed.current || saveRequest.current !== controller) return;
      reload = true;
      const detail = apiHttpErrorDetail(cause);
      setSaveError(detail
        ? `${detail}（現在の設定を再読み込みしました。）`
        : "保存結果を確認できませんでした。現在の設定を再読み込みしました。");
    } finally {
      if (saveRequest.current === controller) {
        saveRequest.current = null;
        if (active.current && manageAllowed.current) {
          setSaving(false);
          // 結果が不確かな保存では実際の永続値を読み直す。どちらも保存後の runtime 状態を新たに観測する。
          if (reload) void loadSettings();
          void run("status");
        }
      }
    }
  }, [loadSettings, run]);

  // 設定の読み取りは全ロールで行い、サーバーのマスター状態を表示する。
  useEffect(() => {
    active.current = true;
    queueMicrotask(() => { void loadSettings(); });
    return () => {
      active.current = false;
      settingsRequest.current?.abort();
      settingsRequest.current = null;
    };
  }, [loadSettings]);

  // 特権（runtime 状態・解放・マスター保存・ポーリング）は管理者の間だけ。
  // true→false / unmount では進行中を中断し、遅れて届く応答が状態を更新しないようにする。
  useEffect(() => {
    if (!canManage) return;
    manageAllowed.current = true;
    queueMicrotask(() => { void run("status"); });
    const timer = setInterval(() => {
      if (AppState.currentState === "active") void run("status");
    }, POLL_INTERVAL_MS);
    const subscription = AppState.addEventListener("change", (next) => {
      if (next === "active") void run("status");
    });
    return () => {
      manageAllowed.current = false;
      clearInterval(timer);
      subscription.remove();
      request.current?.abort();
      request.current = null;
      saveRequest.current?.abort();
      saveRequest.current = null;
      if (active.current) {
        setStatus(null); setLoaded(false); setPending(null); setError(null); setMessage(null);
        setSaving(false); setSaveError(null); setSaveMessage(null);
      }
    };
  }, [canManage, run]);

  const busy = Boolean(status && (status.pending_requests > 0 || ["busy", "loading", "releasing", "closed"].includes(status.state)));
  const canRelease = loaded && status?.process_running && !busy && !pending && !error;
  const phase = embeddingMasterPhase(enabled, status, loaded);
  const switchLabel = enabled === null ? "確認中" : enabled ? "ON" : "OFF";
  return <Surface elevation={0} style={{ padding: 16, gap: 10, borderRadius: 12 }}>
    <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
      <Text variant="titleMedium">サーバーのEmbeddingGemma 2</Text>
      <Button compact mode="text" accessibilityLabel="EmbeddingGemma 2 の説明" onPress={() => setHelpOpen((open) => !open)}>{helpOpen ? "閉じる" : "説明"}</Button>
    </View>
    {canManage && <Text>{loaded ? status ? LABELS[status.state] : "未起動" : "確認中"}</Text>}
    {helpOpen && <>
      <Text variant="bodySmall">検索・索引作成で共有するモデルです。状態確認では読み込みません。</Text>
      <Text variant="bodySmall">OFFは新しい意味検索・索引作成の受付を止めます（進行中の推論は完了を待ち、キーワード検索は使えます）。「メモリを解放」は一度だけの操作で、ONのままなら次の推論で再ロードします。</Text>
    </>}
    <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
      <Text>意味検索・索引作成</Text>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <Text variant="bodySmall">{saving ? "保存中…" : switchLabel}</Text>
        <Switch
          accessibilityLabel="意味検索・索引作成"
          value={enabled === true}
          disabled={!canManage || enabled === null || saving}
          onValueChange={(next) => { if (canManage && enabled !== null) void setEnabled(next); }}
        />
      </View>
    </View>
    {!canManage && <Text variant="bodySmall" accessibilityLiveRegion="polite">この設定の変更には管理者権限が必要です。</Text>}
    {settingsError && <Text accessibilityRole="alert">{settingsError}</Text>}
    {canManage && saveError && <Text accessibilityRole="alert">{saveError}</Text>}
    {canManage && saveMessage && <Text accessibilityLiveRegion="polite">{saveMessage}</Text>}
    {canManage && phase === "off_checking" && <Text accessibilityLiveRegion="polite">OFF: モデルの状態を確認しています。</Text>}
    {canManage && phase === "off_draining" && status && <Text accessibilityLiveRegion="polite">OFF: 受付済みの推論が完了するまで待っています（実行 {status.active_requests} / 待機 {status.queued_requests}）。完了後にモデルを解放します。</Text>}
    {canManage && phase === "off_unloading" && <Text accessibilityLiveRegion="polite">OFF: モデルの解放を待っています。解放を確認できるまで、読み込み済みの状態が表示されます。</Text>}
    {canManage && phase === "off_released" && <Text accessibilityLiveRegion="polite">OFF: モデルは解放済みです。意味検索と索引作成は停止し、キーワード検索のみ動作します。</Text>}
    {canManage && status && <>
      <Text>実行 / 待機: {status.active_requests} / {status.queued_requests}</Text>
      <Text>配置 / 精度: {status.effective_device ?? status.configured_device} / {status.precision ?? "未ロード"}</Text>
      <Text>自動解放: {status.idle_timeout_seconds === 0 ? "OFF" : `${status.idle_timeout_seconds} 秒`} / PID: {status.pid ?? "なし"}</Text>
      {status.metrics_source === "last_worker_response" && typeof status.metrics.rss_bytes === "number" && <Text variant="bodySmall">メモリ: {(status.metrics.rss_bytes / 1024 / 1024).toFixed(1)} MiB（直近の推論応答時点）</Text>}
    </>}
    {canManage && status?.last_error && <Text accessibilityRole="alert">{status.last_error}</Text>}
    {canManage && error && <Text accessibilityRole="alert">{error}</Text>}
    {canManage && message && <Text accessibilityLiveRegion="polite">{message}</Text>}
    {canManage && <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
      <Button mode="outlined" disabled={Boolean(pending) || saving} onPress={() => { void run("status"); void loadSettings(); }}>状態を更新</Button>
      <Button mode="outlined" disabled={!canRelease || saving} loading={pending === "release"} onPress={() => void run("release")}>メモリを解放</Button>
    </View>}
  </Surface>;
}

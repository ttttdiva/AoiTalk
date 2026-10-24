import { useCallback, useEffect, useRef, useState } from "react";
import type { components } from "@/lib/api-types.gen";
import {
  EMBEDDING_ENABLED_SETTING_KEY, apiErrorDetail, isSavedEmbeddingEnabled, readEmbeddingEnabled,
} from "@/lib/embedding-master-setting";

export type EmbeddingRuntimeStatus = components["schemas"]["EmbeddingRuntimeStatus"];
type RuntimeView = components["schemas"]["EmbeddingRuntimeView"];
type ReleaseResult = components["schemas"]["EmbeddingRuntimeReleaseResult"];

const RELEASE_MESSAGES: Record<ReleaseResult["reason"], string> = {
  manual: "メモリを解放しました。",
  not_loaded: "モデルは既に解放されています。",
  busy: "処理中または待機中のリクエストがあります。完了後に再度解放してください。",
  closed: "ランタイムは終了しています。",
  error: "解放できませんでした。状態を確認して再試行してください。",
};

// 手動解放の成功文言。マスター値で再ロードの可否が変わるため、現在値に合わせて案内する（未確認は断定しない）。
const manualReleaseMessage = (enabled: boolean | null) => enabled === true
  ? "メモリを解放しました。次の推論時に必要なモデルを読み込みます。"
  : enabled === false
    ? "メモリを解放しました。ONに戻すまでモデルは再読み込みされません。"
    : RELEASE_MESSAGES.manual;

const SETTINGS_PATH = "/api/python-proxy/settings";
const SAVED_MESSAGES = {
  on: "有効にしました。モデルは必要になった時点で読み込み、停止していた索引作成も再開します。",
  off: "無効にしました。進行中の推論は完了を待ち、OFF中の索引更新は保留します。ONに戻すと再開します。下の状態でモデル解放を確認できます。",
};

export interface EmbeddingRuntimeOptions {
  /** 管理者のみ true。false/未確定の間は設定の読み取り以外の特権リクエストを一切送らない。 */
  canManage?: boolean;
}

export function useEmbeddingRuntime({ canManage = false }: EmbeddingRuntimeOptions = {}) {
  const [status, setStatus] = useState<EmbeddingRuntimeStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [pending, setPending] = useState<"status" | "release" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  // null は未取得または不正値。キー欠落（既定 true）とは区別し、確認できない間は ON と推測しない。
  const [enabled, setEnabledState] = useState<boolean | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const active = useRef(false);
  const enabledRef = useRef<boolean | null>(null);
  useEffect(() => {
    enabledRef.current = enabled;
  }, [enabled]);
  const manageAllowed = useRef(false);
  const request = useRef<{ controller: AbortController; kind: "status" | "release" } | null>(null);
  const settingsRequest = useRef<AbortController | null>(null);
  const saveRequest = useRef<AbortController | null>(null);

  const run = useCallback(async (kind: "status" | "release") => {
    // 保存中は保存前の観測を新しい状態として扱わないため、状態取得も解放も送らない。
    if (!active.current || !manageAllowed.current || request.current || saveRequest.current) return;
    const controller = new AbortController();
    request.current = { controller, kind };
    setPending(kind);
    setError(null);
    if (kind === "release") setMessage(null);
    const timeout = window.setTimeout(() => controller.abort(), kind === "release" ? 30000 : 5000);
    try {
      const response = await fetch(`/api/python-proxy/runtime/embedding${kind === "release" ? "/release" : ""}`, {
        method: kind === "release" ? "POST" : "GET",
        credentials: "include", cache: "no-store", signal: controller.signal,
      });
      const body = await response.json() as RuntimeView & Partial<ReleaseResult> & { detail?: string };
      if (!active.current || !manageAllowed.current || request.current?.controller !== controller) return;
      if (kind === "release" && body.reason && body.reason in RELEASE_MESSAGES) {
        setStatus(body.status);
        // 拒否（accepted:false）で status が null なのは不在の確認ではない。受理された not_loaded の null だけが確定した不在。
        setLoaded(body.accepted === true || body.status != null);
        const note = body.reason === "manual" ? manualReleaseMessage(enabledRef.current) : RELEASE_MESSAGES[body.reason];
        if (body.accepted) setMessage(note);
        else setError(note);
        return;
      }
      if (!response.ok) throw new Error(body.detail || `状態を取得できませんでした (${response.status})`);
      setStatus(body.status);
      setLoaded(true);
    } catch (cause) {
      if (!active.current || !manageAllowed.current || request.current?.controller !== controller) return;
      // 取得できなかった状態を直前の有効な観測のまま見せない（未確認として扱う）。
      setStatus(null);
      setLoaded(false);
      setError(controller.signal.aborted
        ? kind === "release" ? "解放結果を確認できませんでした。状態を更新してください。" : "状態取得がタイムアウトしました。"
        : cause instanceof Error ? cause.message : "状態を取得できませんでした。");
    } finally {
      window.clearTimeout(timeout);
      if (request.current?.controller === controller) {
        request.current = null;
        if (active.current && manageAllowed.current) setPending(null);
      }
    }
  }, []);

  // マスタースイッチの現在値を読む。保存中の PATCH より前に発行した読み取りは
  // 古い値で上書きしないよう、保存開始時に中断して無効化する。
  const loadSettings = useCallback(async () => {
    if (!active.current || settingsRequest.current || saveRequest.current) return;
    const controller = new AbortController();
    settingsRequest.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(SETTINGS_PATH, {
        method: "GET", credentials: "include", cache: "no-store", signal: controller.signal,
      });
      const body: unknown = await response.json().catch(() => null);
      if (!active.current || settingsRequest.current !== controller) return;
      if (!response.ok) throw new Error(apiErrorDetail(body) ?? `設定を取得できませんでした (${response.status})`);
      const read = readEmbeddingEnabled(body);
      if (!read.ok) {
        setEnabledState(null);
        setSettingsError("設定値を確認できませんでした（応答が不正です）。");
        return;
      }
      setEnabledState(read.enabled);
      setSettingsError(null);
    } catch (cause) {
      if (!active.current || settingsRequest.current !== controller) return;
      setEnabledState(null);
      setSettingsError(controller.signal.aborted
        ? "設定の取得がタイムアウトしました。"
        : cause instanceof Error ? cause.message : "設定を取得できませんでした。");
    } finally {
      window.clearTimeout(timeout);
      if (settingsRequest.current === controller) settingsRequest.current = null;
    }
  }, []);

  const setEnabled = useCallback(async (next: boolean) => {
    if (!active.current || !manageAllowed.current || saveRequest.current) return;
    const controller = new AbortController();
    saveRequest.current = controller;
    settingsRequest.current?.abort();
    settingsRequest.current = null;
    // 保存前に発行した runtime 要求は無効化する。遅れて届いても反映せず、古い要求の後始末も新しい pending を消さない。
    if (request.current) {
      request.current.controller.abort();
      request.current = null;
      setPending(null);
    }
    // HTTP 失敗は保存済みを否定しない（commit 後の 500 があり得る）。保存開始から、新しく取得した状態を確認するまで未確認にする。
    setStatus(null);
    setLoaded(false);
    setSaving(true);
    setSaveError(null);
    setSaveMessage(null);
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    let reload = false;
    try {
      const response = await fetch(SETTINGS_PATH, {
        method: "PATCH", credentials: "include", cache: "no-store", signal: controller.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: EMBEDDING_ENABLED_SETTING_KEY, value: next, persist: true }),
      });
      const body: unknown = await response.json().catch(() => null);
      if (!active.current || !manageAllowed.current || saveRequest.current !== controller) return;
      if (!response.ok) {
        throw new Error(apiErrorDetail(body) ?? `保存できませんでした (${response.status})`);
      }
      if (!isSavedEmbeddingEnabled(body, next)) {
        reload = true;
        setSaveError("保存結果を確認できませんでした。現在の設定を再読み込みしました。");
        return;
      }
      setEnabledState(next);
      setSettingsError(null);
      setSaveMessage(next ? SAVED_MESSAGES.on : SAVED_MESSAGES.off);
    } catch (cause) {
      if (!active.current || !manageAllowed.current || saveRequest.current !== controller) return;
      reload = true;
      setSaveError(controller.signal.aborted
        ? "保存結果を確認できませんでした。現在の設定を再読み込みしました。"
        : cause instanceof Error ? cause.message : "保存できませんでした。");
    } finally {
      window.clearTimeout(timeout);
      if (saveRequest.current === controller) {
        saveRequest.current = null;
        if (active.current && manageAllowed.current) {
          setSaving(false);
          // どの結果でも runtime の実状態が変わり得るため、成功した保存後の観測まで未確認にする。
          setStatus(null); setLoaded(false);
          // 結果が不確かな保存では実際の永続値を読み直す。どの結果でも保存後に新しい状態を取得する。
          if (reload) void loadSettings();
          void run("status");
        }
      }
    }
  }, [loadSettings, run]);

  useEffect(() => {
    active.current = true;
    queueMicrotask(() => { void loadSettings(); });
    return () => {
      active.current = false;
      settingsRequest.current?.abort();
      settingsRequest.current = null;
    };
  }, [loadSettings]);

  // 特権（runtime 状態・解放・マスター保存）は canManage の間だけ。
  // true→false / unmount では進行中を中断し、遅れて届く応答が状態を更新しないようにする。
  useEffect(() => {
    if (!canManage) return;
    manageAllowed.current = true;
    queueMicrotask(() => { void run("status"); });
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void run("status");
    }, 5000);
    return () => {
      manageAllowed.current = false;
      window.clearInterval(timer);
      request.current?.controller.abort();
      request.current = null;
      saveRequest.current?.abort();
      saveRequest.current = null;
      if (active.current) {
        setStatus(null); setLoaded(false); setPending(null); setError(null); setMessage(null);
        setSaving(false); setSaveError(null); setSaveMessage(null);
      }
    };
  }, [canManage, run]);

  return { status, loaded, pending, error, message,
    enabled, settingsError, saving, saveError, saveMessage,
    refresh: () => Promise.all([run("status"), loadSettings()]).then(() => undefined),
    release: () => run("release"), setEnabled };
}

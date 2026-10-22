import { useCallback, useRef, useState } from "react";
import { AppState } from "react-native";
import { useFocusEffect } from "expo-router";
import {
  getConfiguredApiServerFingerprint,
  isApiConnectionError,
  isApiHttpError,
  isApiTimeoutError,
  isGatewayReauthRequiredError,
} from "../../lib/api-client";

function isOperationsForbiddenError(error: unknown): boolean {
  return isApiHttpError(error) && error.status === 403;
}

function isOperationsUnauthorizedError(error: unknown): boolean {
  if (isApiHttpError(error) && error.status === 401) return true;
  return error instanceof Error && error.name === "AuthError";
}

/** Keep read failures actionable without exposing raw server response bodies. */
export function operationsErrorMessage(error: unknown): string {
  if (isApiTimeoutError(error)) {
    return "Operationsの読み取りが30秒以内に完了しませんでした。処理が続いている可能性があるため、少し待ってから再読み込みしてください。";
  }
  if (isGatewayReauthRequiredError(error)) {
    return "Gatewayの認証を確認してから、Operationsを再読み込みしてください。";
  }
  if (isOperationsForbiddenError(error)) {
    return "Operationsの表示権限を確認できません。表示範囲・接続先・アカウント権限を確認して再読み込みしてください。";
  }
  if (isOperationsUnauthorizedError(error)) {
    return "Operationsの認証が必要です。ログイン状態を確認して再読み込みしてください。";
  }
  if (isApiConnectionError(error)) {
    return "AoiTalkサーバーに接続できません。ネットワークとAPI URLを確認して再読み込みしてください。";
  }
  return "Operationsの最新状況を取得できませんでした。サーバーの応答を確認して再読み込みしてください。";
}

/** Ephemeral only: identity/scope changes and blur invalidate every response. */
export function useOperations<T>(scopeKey: string, loader: (server: string, signal: AbortSignal) => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [sourceKey, setSourceKey] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);
  const version = useRef(0);
  const abort = useRef<AbortController | null>(null);
  const active = useRef(false);
  const invalidate = useCallback(() => { ++version.current; abort.current?.abort(); }, []);
  const refresh = useCallback(async () => {
    if (!active.current) return;
    invalidate(); const revision = version.current;
    const request = new AbortController(); abort.current = request;
    // A new read has no verified relationship to the previous response until
    // its server fingerprint and auth scope complete. Keep the UI fail-closed
    // during that gap so a logout/server switch cannot look healthy.
    setData(null); setCheckedAt(null); setLoading(true); setError(null);
    try {
      const server = await getConfiguredApiServerFingerprint();
      if (request.signal.aborted) return;
      setSourceKey(`${scopeKey}:${server}`);
      const result = await loader(server, request.signal);
      if (revision !== version.current || !active.current || request.signal.aborted) return;
      if (server !== await getConfiguredApiServerFingerprint()) return;
      if (revision !== version.current || !active.current) return;
      setData(result); setCheckedAt(new Date().toISOString());
    } catch (error) {
      if (revision !== version.current || !active.current || request.signal.aborted) return;
      setData(null); setError(operationsErrorMessage(error));
    } finally { if (revision === version.current && active.current) setLoading(false); }
  }, [loader, invalidate, scopeKey]);
  useFocusEffect(useCallback(() => {
    active.current = true; setData(null); setCheckedAt(null); void refresh();
    const subscription = AppState.addEventListener("change", state => {
      if (state === "active") { active.current = true; void refresh(); }
      else { active.current = false; invalidate(); setData(null); }
    });
    return () => { active.current = false; invalidate(); subscription.remove(); };
  }, [scopeKey, refresh, invalidate]));
  return { data, error, loading, checkedAt, sourceKey, refresh };
}



import { useCallback, useEffect, useRef, useState } from "react";
import type { components } from "@/lib/api-types.gen";

export type EmbeddingRuntimeStatus = components["schemas"]["EmbeddingRuntimeStatus"];
type RuntimeView = components["schemas"]["EmbeddingRuntimeView"];
type ReleaseResult = components["schemas"]["EmbeddingRuntimeReleaseResult"];

const RELEASE_MESSAGES: Record<ReleaseResult["reason"], string> = {
  manual: "メモリを解放しました。次の推論時に必要なモデルを読み込みます。",
  not_loaded: "モデルは既に解放されています。",
  busy: "処理中または待機中のリクエストがあります。完了後に再度解放してください。",
  closed: "ランタイムは終了しています。",
  error: "解放できませんでした。状態を確認して再試行してください。",
};

export function useEmbeddingRuntime() {
  const [status, setStatus] = useState<EmbeddingRuntimeStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [pending, setPending] = useState<"status" | "release" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const active = useRef(false);
  const request = useRef<{ controller: AbortController; kind: "status" | "release" } | null>(null);

  const run = useCallback(async (kind: "status" | "release") => {
    if (!active.current || request.current) return;
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
      if (!active.current || request.current?.controller !== controller) return;
      if (kind === "release" && body.reason && body.reason in RELEASE_MESSAGES) {
        setStatus(body.status);
        setLoaded(true);
        const note = RELEASE_MESSAGES[body.reason];
        if (body.accepted) setMessage(note);
        else setError(note);
        return;
      }
      if (!response.ok) throw new Error(body.detail || `状態を取得できませんでした (${response.status})`);
      setStatus(body.status);
      setLoaded(true);
    } catch (cause) {
      if (!active.current || request.current?.controller !== controller) return;
      setError(controller.signal.aborted
        ? kind === "release" ? "解放結果を確認できませんでした。状態を更新してください。" : "状態取得がタイムアウトしました。"
        : cause instanceof Error ? cause.message : "状態を取得できませんでした。");
    } finally {
      window.clearTimeout(timeout);
      if (request.current?.controller === controller) {
        request.current = null;
        if (active.current) setPending(null);
      }
    }
  }, []);

  useEffect(() => {
    active.current = true;
    queueMicrotask(() => { void run("status"); });
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void run("status");
    }, 5000);
    return () => {
      active.current = false;
      window.clearInterval(timer);
      request.current?.controller.abort();
      request.current = null;
    };
  }, [run]);

  return { status, loaded, pending, error, message,
    refresh: () => run("status"), release: () => run("release") };
}

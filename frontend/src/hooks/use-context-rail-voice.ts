"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useOptionalRuntimeContext,
  type RuntimeVoiceStatus,
} from "@/contexts/runtime-context";

/** Fast enough to track speech, but only armed while the backend is recording. */
const VOICE_RECORDING_POLL_INTERVAL_MS = 400;
/** Waiting/ready checks are intentionally sparse to avoid idle traffic. */
const VOICE_READY_POLL_INTERVAL_MS = 3000;
/** Avoid leaving a meter request pending forever when the proxy is unavailable. */
const VOICE_REQUEST_TIMEOUT_MS = 3000;

export type ContextRailVoiceConfig = {
  engine: string | null;
  model: string | null;
};

export type ContextRailVoiceState = {
  connected: boolean;
  status: RuntimeVoiceStatus | null;
  config: ContextRailVoiceConfig | null;
  loading: boolean;
  error: boolean;
  refresh: () => Promise<void>;
};

type VoiceConfigResponse = {
  asr_engine?: unknown;
  asr_model?: unknown;
};

function cleanConfigValue(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if (!normalized || normalized.toLowerCase() === "unknown") return null;
  return normalized;
}

/**
 * Rail-scoped voice status. RuntimeProvider remains the app-wide fallback
 * (15s health poll), while this hook adds a conditional meter poll only when
 * the rail is visible, local mic is enabled, and the backend is recording.
 */
export function useContextRailVoice(enabled = true): ContextRailVoiceState {
  const runtime = useOptionalRuntimeContext();
  const connected = Boolean(runtime?.isConnected);
  const localMicEnabled = runtime?.runtimeFeatures?.features?.local_mic === true;
  const runtimeVoiceStatus = runtime?.voiceStatus ?? null;
  const [status, setStatus] = useState<RuntimeVoiceStatus | null>(runtimeVoiceStatus);
  const [config, setConfig] = useState<ContextRailVoiceConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const statusRef = useRef<RuntimeVoiceStatus | null>(runtimeVoiceStatus);
  const requestRef = useRef<AbortController | null>(null);
  const [visible, setVisible] = useState(
    () => typeof document === "undefined" || !document.hidden,
  );

  const updateStatus = useCallback((next: RuntimeVoiceStatus | null) => {
    statusRef.current = next;
    setStatus(next);
  }, []);
  const refresh = useCallback(async () => {
    if (
      !enabled ||
      !connected ||
      !localMicEnabled ||
      !visible
    ) {
      return;
    }
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    const timeoutId = window.setTimeout(
      () => controller.abort(),
      VOICE_REQUEST_TIMEOUT_MS,
    );
    try {
      const response = await fetch("/api/python-proxy/voice_status", {
        credentials: "include",
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`voice status: ${response.status}`);
      const next = (await response.json()) as RuntimeVoiceStatus;
      if (controller.signal.aborted) return;
      updateStatus(next);
      setError(false);
    } catch {
      if (controller.signal.aborted) return;
      updateStatus(null);
      setError(true);
    } finally {
      window.clearTimeout(timeoutId);
      if (requestRef.current === controller) {
        requestRef.current = null;
        setLoading(false);
      }
    }
  }, [connected, enabled, localMicEnabled, updateStatus, visible]);

  // Keep the RuntimeProvider's ready/recording transitions visible immediately
  // without replacing fresh RMS samples from this hook.
  useEffect(() => {
    if (!runtimeVoiceStatus) return;
    const current = statusRef.current;
    if (!current || current.ready !== runtimeVoiceStatus.ready || current.recording !== runtimeVoiceStatus.recording) {
      updateStatus(runtimeVoiceStatus);
    }
  }, [runtimeVoiceStatus, updateStatus]);

  useEffect(() => {
    if (!enabled || !connected || !localMicEnabled) {
      requestRef.current?.abort();
      updateStatus(null);
      setError(false);
      setLoading(false);
      return;
    }

    const onVisibilityChange = () => {
      setVisible(!document.hidden);
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    void refresh();
    return () => {
      requestRef.current?.abort();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [connected, enabled, localMicEnabled, refresh, updateStatus]);

  // Do not poll when the backend says voice is unavailable. The provider's
  // existing 15s status check (and visibility refresh above) will notice when
  // readiness changes; once ready, this rail owns the meter cadence.
  useEffect(() => {
    if (!enabled || !connected || !localMicEnabled || !visible || !status?.ready) {
      return;
    }
    let disposed = false;
    let timer: number | null = null;
    const schedule = () => {
      if (disposed || !visible || !statusRef.current?.ready) return;
      const interval = statusRef.current.recording
        ? VOICE_RECORDING_POLL_INTERVAL_MS
        : VOICE_READY_POLL_INTERVAL_MS;
      timer = window.setTimeout(() => {
        timer = null;
        void refresh().then(schedule);
      }, interval);
    };
    schedule();
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [connected, enabled, localMicEnabled, refresh, status?.ready, status?.recording, visible]);

  useEffect(() => {
    if (!enabled || !connected || !localMicEnabled || !visible) {
      setConfig(null);
      return;
    }
    const controller = new AbortController();
    const timeoutId = window.setTimeout(
      () => controller.abort(),
      VOICE_REQUEST_TIMEOUT_MS,
    );
    void fetch("/api/python-proxy/config", {
      credentials: "include",
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`config: ${response.status}`);
        return (await response.json()) as VoiceConfigResponse;
      })
      .then((data) => {
        if (controller.signal.aborted) return;
        const engine = cleanConfigValue(data.asr_engine);
        const model = cleanConfigValue(data.asr_model);
        setConfig(engine || model ? { engine, model } : null);
      })
      .catch(() => {
        if (!controller.signal.aborted) setConfig(null);
      })
      .finally(() => window.clearTimeout(timeoutId));
    return () => {
      window.clearTimeout(timeoutId);
      controller.abort();
    };
  }, [connected, enabled, localMicEnabled, visible]);

  return useMemo(
    () => ({ connected, status, config, loading, error, refresh }),
    [connected, config, error, loading, refresh, status],
  );
}

export {
  VOICE_READY_POLL_INTERVAL_MS,
  VOICE_RECORDING_POLL_INTERVAL_MS,
  VOICE_REQUEST_TIMEOUT_MS,
};

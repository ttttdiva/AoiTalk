"use client";

import { useCallback, useEffect, useRef } from "react";
import useSWR from "swr";
import { getLlmMode, setLlmMode, type LlmMode } from "@/lib/chat-api";
import { toast } from "sonner";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";

// SWR キャッシュキー。チャット画面で一意なので固定文字列を使う。
const LLM_MODE_SWR_KEY = "chat/llm-mode";

type LlmModeData = {
  mode: LlmMode;
  options: LlmMode[];
  labels: Record<string, string>;
};

const EMPTY_OPTIONS: LlmMode[] = [];
const EMPTY_LABELS: Record<string, string> = {};
const ERROR_DETAIL_MAX_LENGTH = 120;

function sanitizeErrorDetail(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized) return null;
  return normalized.length > ERROR_DETAIL_MAX_LENGTH
    ? `${normalized.slice(0, ERROR_DETAIL_MAX_LENGTH - 1)}…`
    : normalized;
}

function apiErrorDetail(error: unknown): string | null {
  if (error && typeof error === "object") {
    const record = error as Record<string, unknown>;
    const detail = sanitizeErrorDetail(record.detail);
    if (detail) return detail;

    const rawMessage =
      typeof record.message === "string" ? record.message : null;
    if (!rawMessage) return null;

    const responseBody = rawMessage.match(/\s-\s(\{[\s\S]*\})$/)?.[1];
    if (responseBody) {
      try {
        const parsed = JSON.parse(responseBody) as Record<string, unknown>;
        return (
          sanitizeErrorDetail(parsed.detail) ??
          sanitizeErrorDetail(parsed.message) ??
          sanitizeErrorDetail(rawMessage)
        );
      } catch {
        // JSONでないエラー本文は、長さと制御文字を制限したmessageを使う。
      }
    }
    return sanitizeErrorDetail(rawMessage);
  }
  return sanitizeErrorDetail(error);
}

// API レスポンスを内部データ形へ正規化する（旧実装の setState 群と同じ導出）。
function normalizeLlmMode(result: {
  mode: LlmMode;
  available_modes?: LlmMode[];
  labels?: Record<string, string>;
}): LlmModeData {
  return {
    mode: result.mode,
    options: result.available_modes?.length
      ? result.available_modes
      : [result.mode],
    labels: result.labels ?? {},
  };
}

/**
 * LLM モードの取得・切り替えを担うフック。
 *
 * 取得・キャッシュ・重複排除は SWR に委譲する。マウント時に一度取得し、その後の
 * 自動 revalidation（フォーカス/再接続/stale）は行わない。WebSocket 経由の
 * `llm_mode_change` イベントでも状態を更新するため、SWR キャッシュを直接書き換える
 * setter（setLlmModeState / setLlmModeOptions / setLlmModeLabels）も併せて公開する。
 * 公開 API と表示挙動は従来の useState 実装と互換。
 */
export function useChatLlmMode() {
  const runtime = useOptionalRuntimeContext();
  const { data, error, isLoading, mutate } = useSWR<LlmModeData>(
    LLM_MODE_SWR_KEY,
    async () => normalizeLlmMode(await getLlmMode()),
    {
      revalidateOnMount: true,
      revalidateOnFocus: false,
      // 一時的なmode API失敗はRuntime再接続時に自然復旧させる。SWRの
      // keepPreviousDataと併用し、既に取得済みのeffortを空へ戻さない。
      revalidateOnReconnect: true,
      revalidateIfStale: true,
      shouldRetryOnError: true,
      errorRetryCount: 3,
      errorRetryInterval: 1500,
      keepPreviousData: true,
      onError: (err) => {
        console.warn("LLMモード取得に失敗:", err);
        toast.error("LLMモードの取得に失敗しました");
      },
    },
  );

  const wasConnectedRef = useRef(false);
  useEffect(() => {
    const connected = runtime?.isConnected === true;
    if (connected && !wasConnectedRef.current) {
      void mutate();
    }
    wasConnectedRef.current = connected;
  }, [mutate, runtime?.isConnected]);

  const llmMode = data?.mode ?? "";
  const llmModeOptions = data?.options ?? EMPTY_OPTIONS;
  const llmModeLabels = data?.labels ?? EMPTY_LABELS;

  // WebSocket イベントからの部分更新用 setter。SWR キャッシュのみ更新（revalidate 無効）。
  const setLlmModeState = useCallback(
    (mode: LlmMode) => {
      void mutate(
        (prev) => ({
          mode,
          options: prev?.options ?? EMPTY_OPTIONS,
          labels: prev?.labels ?? EMPTY_LABELS,
        }),
        { revalidate: false },
      );
    },
    [mutate],
  );

  const setLlmModeOptions = useCallback(
    (options: LlmMode[]) => {
      void mutate(
        (prev) => ({
          mode: prev?.mode ?? "",
          options,
          labels: prev?.labels ?? EMPTY_LABELS,
        }),
        { revalidate: false },
      );
    },
    [mutate],
  );

  const setLlmModeLabels = useCallback(
    (labels: Record<string, string>) => {
      void mutate(
        (prev) => ({
          mode: prev?.mode ?? "",
          options: prev?.options ?? EMPTY_OPTIONS,
          labels,
        }),
        { revalidate: false },
      );
    },
    [mutate],
  );

  const handleLlmModeChange = useCallback(
    async (nextMode: LlmMode) => {
      try {
        const result = await setLlmMode(nextMode);
        await mutate(normalizeLlmMode(result), { revalidate: false });
        toast.success(
          `LLM mode: ${result.labels?.[result.mode] ?? result.mode}`,
        );
      } catch (err) {
        console.error("LLMモード切り替えに失敗:", err);
        const detail = apiErrorDetail(err);
        toast.error(
          detail
            ? `LLMモードの切り替えに失敗しました: ${detail}`
            : "LLMモードの切り替えに失敗しました",
        );
        try {
          // POSTが設定保存後のhot applyで失敗する場合があるため、server stateを正とする。
          await mutate();
        } catch (syncError) {
          console.warn("LLMモード切り替え失敗後の再同期に失敗:", syncError);
        }
      }
    },
    [mutate],
  );

  return {
    llmMode,
    llmModeOptions,
    llmModeLabels,
    setLlmModeState,
    setLlmModeOptions,
    setLlmModeLabels,
    handleLlmModeChange,
    llmModeError: error ?? null,
    llmModeLoading: isLoading,
    refreshLlmMode: mutate,
  };
}

"use client";

import { useCallback } from "react";
import useSWR from "swr";
import { getLlmMode, setLlmMode, type LlmMode } from "@/lib/chat-api";
import { toast } from "sonner";

// SWR キャッシュキー。チャット画面で一意なので固定文字列を使う。
const LLM_MODE_SWR_KEY = "chat/llm-mode";

type LlmModeData = {
  mode: LlmMode;
  options: LlmMode[];
  labels: Record<string, string>;
};

const EMPTY_OPTIONS: LlmMode[] = [];
const EMPTY_LABELS: Record<string, string> = {};

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
  const { data, mutate } = useSWR<LlmModeData>(
    LLM_MODE_SWR_KEY,
    async () => normalizeLlmMode(await getLlmMode()),
    {
      revalidateOnMount: true,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      shouldRetryOnError: false,
      onError: (err) => {
        console.warn("LLMモード取得に失敗:", err);
        toast.error("LLMモードの取得に失敗しました");
      },
    },
  );

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
        toast.error("LLMモードの切り替えに失敗しました");
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
  };
}

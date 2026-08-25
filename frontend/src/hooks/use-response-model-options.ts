"use client";

import { useMemo } from "react";
import useSWR from "swr";
import type { UserSettings } from "@/lib/user-settings";
import {
  getLlmModelCatalog,
  type LlmDeploymentMetadata,
  type ChatResponseModelOption,
  type LlmCatalogModelOption,
  type LlmCatalogProvider,
  type LlmModelCatalogResponse,
} from "@/lib/chat-api";
import {
  filterAvailableProviders,
  isProviderAvailable,
} from "@/lib/llm-provider-visibility";

// SWR キャッシュキー。チャット画面で一意なので固定文字列を使う。
const RESPONSE_MODEL_OPTIONS_SWR_KEY = "chat/response-model-options";

const EMPTY_OPTIONS: ChatResponseModelOption[] = [];

const API_KEY_REQUIRED_PROVIDERS = new Set(["openai", "gemini", "openrouter", "deepseek", "deepinfra", "kimi"]);

function modelLabel(
  model: LlmCatalogModelOption | undefined,
  fallback: string,
) {
  const label = model?.label?.trim();
  return label || fallback;
}

/**
 * 再生成モデル一覧を LLM カタログから構築する。
 * `page.tsx` の同名関数を移設したもの（挙動不変）。
 */
export function buildResponseModelOptions(
  catalog: LlmModelCatalogResponse,
  _settings?: UserSettings | null,
): ChatResponseModelOption[] {
  // User-level hidden-provider settings intentionally do not affect rerun
  // options; backend deployment availability is the authoritative filter.
  void _settings;
  const currentProvider = catalog.current.provider;
  const currentModel = catalog.current.model;
  const availableProviders = filterAvailableProviders(
    catalog.providers,
    catalog.deployment as LlmDeploymentMetadata | null | undefined,
    (provider) => provider.id,
    (provider) => provider,
  );
  const providers = new Map(
    availableProviders.map((provider) => [provider.id, provider]),
  );
  const result: ChatResponseModelOption[] = [];
  const seen = new Set<string>();

  const addOption = (
    provider: LlmCatalogProvider | undefined,
    modelId: string | undefined,
    model: LlmCatalogModelOption | undefined,
  ) => {
    const normalizedProvider = provider?.id?.trim();
    const normalizedModel = modelId?.trim();
    if (!normalizedProvider || !normalizedModel) return;
    const key = `${normalizedProvider}:${normalizedModel}`;
    if (seen.has(key)) return;

    seen.add(key);
    const providerLabel = provider?.label || normalizedProvider;
    const displayModel = modelLabel(model, normalizedModel);
    const isCurrent =
      normalizedProvider === currentProvider &&
      normalizedModel === currentModel;
    result.push({
      provider: normalizedProvider,
      model: normalizedModel,
      providerLabel,
      modelLabel: displayModel,
      label: isCurrent
        ? `${providerLabel} / ${displayModel} (現在)`
        : `${providerLabel} / ${displayModel}`,
      isCurrent,
    });
  };

  const persistedCurrentProvider = catalog.providers.find(
    (provider) => provider.id === currentProvider,
  );
  const currentCatalogProvider = providers.get(currentProvider);
  if (
    currentCatalogProvider &&
    isProviderAvailable(
      currentProvider,
      catalog.deployment as LlmDeploymentMetadata | null | undefined,
      persistedCurrentProvider,
    )
  ) {
    const currentCatalogModel = currentCatalogProvider.models.find(
      (model) => model.id === currentModel,
    );
    addOption(currentCatalogProvider, currentModel, currentCatalogModel);
  } else if (
    !catalog.deployment &&
    isProviderAvailable(currentProvider, undefined, persistedCurrentProvider)
  ) {
    // Older personal responses may omit the provider from the catalog while
    // still exposing it as the current selection. Keep the old fallback.
    addOption(
      {
        id: currentProvider,
        label: currentProvider,
        models: [],
      },
      currentModel,
      undefined,
    );
  }

  for (const provider of availableProviders) {
    if (
      API_KEY_REQUIRED_PROVIDERS.has(provider.id) &&
      provider.settings?.api_key_configured === false &&
      provider.id !== currentProvider
    ) {
      continue;
    }

    const configuredModel = provider.configured_model?.trim();
    if (configuredModel) {
      addOption(
        provider,
        configuredModel,
        provider.models.find((model) => model.id === configuredModel),
      );
    }

    for (const model of provider.models) {
      addOption(provider, model.id, model);
    }
  }

  return result;
}

/**
 * 再生成モデル選択肢を LLM カタログから読み込むフック。
 *
 * 取得・キャッシュ・重複排除は SWR に委譲する。マウント時に必ずカタログ取得を
 * 開始し、取得完了までは loading=true とする見え方は従来（旧: useState 初期値 true +
 * effect 内 fetch）と不変。フォーカス/再接続などの自動 revalidation は無効化する。
 */
export function useResponseModelOptions() {
  const { data: catalog, isLoading } = useSWR<LlmModelCatalogResponse>(
    RESPONSE_MODEL_OPTIONS_SWR_KEY,
    async () => {
      return getLlmModelCatalog();
    },
    {
      // 従来どおりマウント時に必ず取得する（初期 loading=true → 取得完了で false）。
      revalidateOnMount: true,
      // フォーカス/再接続/stale 時の自動再取得や失敗時リトライは行わない（従来挙動）。
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      shouldRetryOnError: false,
      onError: (err) => console.warn("再生成モデル一覧の取得に失敗:", err),
    },
  );
  const responseModelOptions = useMemo(
    () => (catalog ? buildResponseModelOptions(catalog) : EMPTY_OPTIONS),
    [catalog],
  );

  return {
    // 取得失敗時は data が undefined のままとなり、従来同様に空配列を返す。
    responseModelOptions,
    responseModelOptionsLoading: isLoading,
  };
}

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
  filterVisibleProviders,
  normalizeHiddenProviderIds,
} from "@/lib/llm-provider-visibility";

// SWR キャッシュキー。チャット画面で一意なので固定文字列を使う。
const RESPONSE_MODEL_OPTIONS_SWR_KEY = "chat/response-model-options";

const EMPTY_OPTIONS: ChatResponseModelOption[] = [];

const API_KEY_REQUIRED_PROVIDERS = new Set(["openai", "gemini", "openrouter", "deepseek", "deepinfra", "kimi"]);
const STRICT_RUNTIME_PROVIDERS = new Set(["openai_compatible_local", "ollama", "sglang"]);

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
  const hiddenProviderIds = new Set(
    normalizeHiddenProviderIds(catalog.provider_visibility),
  );
  const availableProviders = filterVisibleProviders(
    filterAvailableProviders(
      catalog.providers,
      catalog.deployment as LlmDeploymentMetadata | null | undefined,
      (provider) => provider.id,
      (provider) => provider,
    ),
    catalog.provider_visibility,
    [],
    (provider) => provider.id,
  );
  /*
   * ``models`` remains the settings catalog.  A backend that predates the
   * split has no ``chat_models`` field, so retain a compatibility fallback;
   * an explicit empty chat_models list is authoritative and must stay empty.
   */
  const chatModelsForProvider = (provider: LlmCatalogProvider) =>
    Array.isArray(provider.chat_models)
      ? provider.chat_models
      : STRICT_RUNTIME_PROVIDERS.has(provider.id.trim().toLowerCase())
        ? []
        : provider.models;
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
    const currentCatalogModel = chatModelsForProvider(currentCatalogProvider).find(
      (model) => model.id === currentModel,
    );
    if (currentCatalogModel) {
      addOption(currentCatalogProvider, currentModel, currentCatalogModel);
    }
  } else if (
    !catalog.deployment &&
    !hiddenProviderIds.has(currentProvider.trim().toLowerCase()) &&
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

    const chatModels = chatModelsForProvider(provider);
    const configuredModel = provider.configured_model?.trim();
    if (configuredModel && chatModels.some((model) => model.id === configuredModel)) {
      addOption(
        provider,
        configuredModel,
        chatModels.find((model) => model.id === configuredModel),
      );
    }

    for (const model of chatModels) {
      addOption(provider, model.id, model);
    }
  }

  return result;
}

export type UseResponseModelOptionsInput = {
  /** Catalog owned by RuntimeProvider. Null means it is still loading. */
  catalog?: LlmModelCatalogResponse | null;
  /** Skip the hook's standalone request and use the shared catalog. */
  shared?: boolean;
};

/**
 * 再生成モデル選択肢を LLM カタログから読み込むフック。
 *
 * 既定では取得・キャッシュ・重複排除を SWR に委譲する。RuntimeProvider
 * の共有カタログを渡された場合は独立した /llm/models リクエストを省き、
 * その値を直接再利用する。フォーカス/再接続などの自動 revalidation は無効化する。
 */
export function useResponseModelOptions(
  input?: UseResponseModelOptionsInput,
) {
  // Passing a catalog object opts into the shared-runtime path by default;
  // callers may explicitly set shared=false when they only want to seed
  // optional metadata while retaining the standalone fetch.
  const sharedCatalog = input !== undefined && input.shared !== false;
  const { data: catalog, isLoading } = useSWR<LlmModelCatalogResponse>(
    sharedCatalog ? null : RESPONSE_MODEL_OPTIONS_SWR_KEY,
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
  const resolvedCatalog = sharedCatalog ? input?.catalog ?? null : catalog;
  const responseModelOptions = useMemo(
    () =>
      resolvedCatalog
        ? buildResponseModelOptions(resolvedCatalog)
        : EMPTY_OPTIONS,
    [resolvedCatalog],
  );

  return {
    // Shared runtime catalog is already loading/owned elsewhere, so expose a
    // settled false loading flag and let the runtime controls render their own
    // loading/error state. Standalone callers retain the historical SWR flag.
    responseModelOptions,
    // A shared RuntimeProvider still owns the request, but a null shared
    // catalog means that request is pending. Preserve the loading signal so
    // rerun menus do not report a false permanent empty state during a
    // transient metadata timeout.
    responseModelOptionsLoading: sharedCatalog
      ? input?.catalog == null
      : isLoading,
  };
}

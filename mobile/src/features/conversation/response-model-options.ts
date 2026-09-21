import type {
  ChatResponseModelOption,
  LlmCatalogModelOption,
  LlmCatalogProvider,
  LlmModelCatalogResponse,
} from "../../types/api";
import {
  filterProvidersByDeployment,
  filterVisibleProviders,
  hasDeploymentProviderRestrictions,
  isProviderAvailableForDeployment,
  normalizeHiddenProviderIds,
  normalizeProviderId,
  resolveEffectiveModelId,
  resolveEffectiveProviderId,
} from "../../lib/llm-provider-visibility";
import { isForbiddenModelId } from "../../lib/cloud-model-catalog";

const API_KEY_REQUIRED_PROVIDERS = new Set([
  "openai",
  "gemini",
  "deepseek",
  "deepinfra",
  "kimi",
  "openrouter",
]);
const STRICT_RUNTIME_PROVIDERS = new Set([
  "openai_compatible_local",
  "ollama",
  "sglang",
]);

function modelLabel(model: LlmCatalogModelOption | undefined, fallback: string) {
  const label = model?.label?.trim();
  return label || fallback;
}

export function buildResponseModelOptions(
  catalog: LlmModelCatalogResponse,
): ChatResponseModelOption[] {
  // The server's global catalog metadata is the presentation authority used by
  // chat. Per-user visibility is legacy state and must not gate this projection.
  const deployment = catalog.deployment;
  const deploymentHasRestrictions = hasDeploymentProviderRestrictions(deployment);
  const currentProvider = catalog.current.provider.trim();
  const currentModel = catalog.current.model.trim();
  const providers = new Map(
    catalog.providers.map((provider) => [normalizeProviderId(provider.id), provider]),
  );

  // A fixed deployment's effective selection is authoritative over a stale
  // persisted mobile selection.  Without explicit deployment metadata, keep
  // the legacy catalog.current behaviour unchanged.
  const effectiveProvider = resolveEffectiveProviderId(deployment);
  const effectiveModel = resolveEffectiveModelId(deployment);
  const targetProviderId =
    deployment?.fixed === true && effectiveProvider
      ? effectiveProvider
      : normalizeProviderId(currentProvider) ?? currentProvider;
  const targetProvider = providers.get(targetProviderId);
  const targetModel =
    deployment?.fixed === true && effectiveProvider && effectiveModel
      ? effectiveModel
      : currentModel;
  const result: ChatResponseModelOption[] = [];
  const seen = new Set<string>();

  const addOption = (
    provider: LlmCatalogProvider | undefined,
    modelId: string | undefined,
    model: LlmCatalogModelOption | undefined,
  ) => {
    const normalizedProvider = provider?.id?.trim();
    const normalizedModel = modelId?.trim();
    if (
      !normalizedProvider ||
      !normalizedModel ||
      isForbiddenModelId(normalizedModel)
    ) return;
    const key = `${normalizedProvider}:${normalizedModel}`;
    if (seen.has(key)) return;

    seen.add(key);
    const providerLabel = provider?.label || normalizedProvider;
    const displayModel = modelLabel(model, normalizedModel);
    const isCurrent =
      normalizeProviderId(normalizedProvider) === targetProviderId &&
      normalizedModel === targetModel;
    result.push({
      provider: normalizedProvider,
      model: normalizedModel,
      providerLabel,
      modelLabel: displayModel,
      ...(Array.isArray(model?.reasoning_effort_options) ? {
        reasoningEffortOptions: [...model.reasoning_effort_options],
        reasoningEffortDefault: model.reasoning_effort_default,
        reasoningEffortKind: model.reasoning_effort_kind,
      } : {}),
      label: isCurrent
        ? `${providerLabel} / ${displayModel} (現在)`
        : `${providerLabel} / ${displayModel}`,
      isCurrent,
    });
  };

  const targetProviderAvailable = isProviderAvailableForDeployment(
    targetProviderId,
    deployment,
    targetProvider,
  );
  // Preserve the old fallback for a catalog that omits catalog.current.  For
  // a fixed deployment, only a provider present in the effective catalog is
  // surfaced; inventing an unavailable Enterprise provider would make it look
  // like an ordinary selectable server choice.
  const canSurfaceTarget =
    !normalizeHiddenProviderIds(catalog.provider_visibility).includes(targetProviderId) &&
    targetProviderAvailable &&
    (!deploymentHasRestrictions || targetProvider !== undefined);
  if (canSurfaceTarget && targetProviderId && targetModel) {
    const currentCatalogProvider = targetProvider ?? {
      id: targetProviderId,
      label: targetProviderId,
      models: [],
    };
    const currentCatalogModels = Array.isArray(currentCatalogProvider.chat_models)
      ? currentCatalogProvider.chat_models
      : STRICT_RUNTIME_PROVIDERS.has(
          currentCatalogProvider.id.trim().toLowerCase(),
        )
        ? []
        : currentCatalogProvider.models;
    const currentCatalogModel = currentCatalogModels.find(
      (model) => model.id === targetModel,
    );
    if (currentCatalogModel) {
      addOption(currentCatalogProvider, targetModel, currentCatalogModel);
    }
  }

  const deploymentProviders = filterProvidersByDeployment(
    catalog.providers,
    deployment,
  );
  const visibleProviders = filterVisibleProviders(
    deploymentProviders,
    catalog.provider_visibility,
    [],
  );
  for (const provider of visibleProviders) {
    const normalizedProviderId = normalizeProviderId(provider.id);
    const isTargetProvider = normalizedProviderId === targetProviderId;
    if (
      normalizedProviderId &&
      API_KEY_REQUIRED_PROVIDERS.has(normalizedProviderId) &&
      provider.settings?.api_key_configured === false &&
      !isTargetProvider
    ) {
      continue;
    }

    const chatModels = Array.isArray(provider.chat_models)
      ? provider.chat_models
      : STRICT_RUNTIME_PROVIDERS.has(provider.id.trim().toLowerCase())
        ? []
        : provider.models;
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

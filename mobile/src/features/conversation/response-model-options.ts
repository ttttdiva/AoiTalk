import type {
  ChatResponseModelOption,
  LlmCatalogModelOption,
  LlmCatalogProvider,
  LlmModelCatalogResponse,
  UserSettings,
} from "../../types/api";
import {
  filterProvidersByDeployment,
  filterVisibleProviders,
  hasDeploymentProviderRestrictions,
  isProviderAvailableForDeployment,
  normalizeProviderId,
  resolveEffectiveModelId,
  resolveEffectiveProviderId,
} from "../../lib/llm-provider-visibility";

const API_KEY_REQUIRED_PROVIDERS = new Set([
  "openai",
  "gemini",
  "deepseek",
  "deepinfra",
  "kimi",
  "openrouter",
]);

function modelLabel(model: LlmCatalogModelOption | undefined, fallback: string) {
  const label = model?.label?.trim();
  return label || fallback;
}

export function buildResponseModelOptions(
  catalog: LlmModelCatalogResponse,
  settings?: UserSettings | null,
): ChatResponseModelOption[] {
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
    if (!normalizedProvider || !normalizedModel) return;
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
    targetProviderAvailable &&
    (!deploymentHasRestrictions || targetProvider !== undefined);
  if (canSurfaceTarget && targetProviderId && targetModel) {
    const currentCatalogProvider = targetProvider ?? {
      id: targetProviderId,
      label: targetProviderId,
      models: [],
    };
    const currentCatalogModel = currentCatalogProvider.models.find(
      (model) => model.id === targetModel,
    );
    addOption(currentCatalogProvider, targetModel, currentCatalogModel);
  }

  const deploymentProviders = filterProvidersByDeployment(
    catalog.providers,
    deployment,
  );
  const visibleProviders = filterVisibleProviders(
    deploymentProviders,
    settings,
    canSurfaceTarget ? [targetProviderId] : [],
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

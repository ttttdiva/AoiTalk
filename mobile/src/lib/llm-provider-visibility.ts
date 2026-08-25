import type { LlmDeploymentMetadata, UserSettings } from "../types/api";

export function normalizeProviderId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  return normalized || null;
}

function normalizeProviderIdList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;

  const seen = new Set<string>();
  for (const rawId of value) {
    const providerId = normalizeProviderId(rawId);
    if (providerId) seen.add(providerId);
  }
  return Array.from(seen);
}

/** ユーザー設定から非表示プロバイダーIDを安全に正規化する。 */
export function normalizeHiddenProviderIds(
  settings: UserSettings | null | undefined,
): string[] {
  const rawIds = settings?.llm_provider_visibility?.hidden_provider_ids;
  if (!Array.isArray(rawIds)) return [];

  const seen = new Set<string>();
  for (const rawId of rawIds) {
    const providerId = normalizeProviderId(rawId);
    if (providerId) seen.add(providerId);
  }
  return Array.from(seen);
}

/**
 * Enterprise deployment metadata may restrict which server providers can be
 * selected.  Keep this check independent from user-hidden provider settings:
 * direct-mobile providers never pass through this helper.
 */
export function isProviderAvailableForDeployment(
  providerId: unknown,
  deployment: LlmDeploymentMetadata | null | undefined,
  providerAvailable: unknown = true,
): boolean {
  // A provider-level availability flag is authoritative when explicitly
  // false.  Missing/unknown values retain the legacy behaviour.
  if (
    providerAvailable === false ||
    (providerAvailable && typeof providerAvailable === "object" &&
      ((providerAvailable as { available?: unknown }).available === false ||
        (providerAvailable as { disabled?: unknown }).disabled === true ||
        (providerAvailable as { unavailable?: unknown }).unavailable === true))
  ) {
    return false;
  }

  const normalizedProviderId = normalizeProviderId(providerId);
  if (!normalizedProviderId || !deployment) return true;

  const unavailableIds = new Set(
    normalizeProviderIdList(deployment.unavailable_provider_ids) ?? [],
  );
  if (unavailableIds.has(normalizedProviderId)) return false;

  const allowedIds = normalizeProviderIdList(deployment.allowed_provider_ids);
  if (allowedIds && !allowedIds.includes(normalizedProviderId)) return false;

  const effectiveProviderId = resolveEffectiveProviderId(deployment);
  if (
    deployment.fixed === true &&
    effectiveProviderId &&
    normalizedProviderId !== effectiveProviderId
  ) {
    return false;
  }

  return true;
}

/** Resolve an effective provider while accepting legacy aliases. */
export function resolveEffectiveProviderId(
  deployment: LlmDeploymentMetadata | null | undefined,
): string | null {
  return normalizeProviderId(
    deployment?.effective_provider ??
      deployment?.fixed_provider ??
      deployment?.effective?.provider,
  );
}

/** Resolve an effective model while accepting legacy aliases. */
export function resolveEffectiveModelId(
  deployment: LlmDeploymentMetadata | null | undefined,
): string | null {
  if (!deployment) return null;
  const value =
    deployment.effective_model ??
    deployment.fixed_model ??
    deployment.effective?.model;
  return typeof value === "string" ? value.trim() || null : null;
}

/** True when deployment metadata explicitly constrains provider selection. */
export function hasDeploymentProviderRestrictions(
  deployment: LlmDeploymentMetadata | null | undefined,
): boolean {
  if (!deployment) return false;
  return (
    deployment.fixed === true ||
    resolveEffectiveProviderId(deployment) !== null ||
    Array.isArray(deployment.allowed_provider_ids) ||
    Array.isArray(deployment.unavailable_provider_ids)
  );
}

/** Apply effective Enterprise deployment availability to a provider list. */
export function filterProvidersByDeployment<T>(
  providers: readonly T[] | null | undefined,
  deployment: LlmDeploymentMetadata | null | undefined,
  getProviderId: (provider: T) => unknown = (provider) =>
    (provider as { id?: unknown }).id,
  getProviderAvailable: (provider: T) => unknown = (provider) =>
    provider,
): T[] {
  return (providers ?? []).filter((provider) =>
    isProviderAvailableForDeployment(
      getProviderId(provider),
      deployment,
      getProviderAvailable(provider),
    ),
  );
}

/** 非表示設定を適用しつつ、現在使用中のプロバイダーは候補に残す。 */
export function filterVisibleProviders<T>(
  providers: readonly T[] | null | undefined,
  settings: UserSettings | null | undefined,
  preserveProviderIds: Iterable<unknown> = [],
  getProviderId: (provider: T) => unknown = (provider) =>
    (provider as { id?: unknown }).id,
): T[] {
  const hiddenIds = new Set(normalizeHiddenProviderIds(settings));
  if (!hiddenIds.size) return Array.from(providers ?? []);

  const preservedIds = new Set<string>();
  for (const rawId of preserveProviderIds) {
    const providerId = normalizeProviderId(rawId);
    if (providerId) preservedIds.add(providerId);
  }

  return (providers ?? []).filter((provider) => {
    const providerId = normalizeProviderId(getProviderId(provider));
    return !providerId || !hiddenIds.has(providerId) || preservedIds.has(providerId);
  });
}

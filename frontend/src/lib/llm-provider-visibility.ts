import type { UserSettings } from "./user-settings";

export const LLM_PROVIDER_VISIBILITY_KEY = "llm_provider_visibility";

/**
 * Backend-owned deployment contract used to constrain provider selection.
 *
 * All fields are optional on purpose: personal deployments and older
 * backends do not send this object, in which case the UI keeps its historical
 * provider list behaviour.  The flat effective/persisted fields are the
 * canonical wire format; the nested aliases make the client tolerant of
 * diagnostics responses that use an explicit object shape.
 */
export type LlmDeploymentMetadata = {
  backend?: string | null;
  transport?: string | null;
  fixed?: boolean | null;
  ready?: boolean | null;
  effective_provider?: string | null;
  effective_model?: string | null;
  fixed_provider?: string | null;
  fixed_model?: string | null;
  allowed_provider_ids?: unknown;
  unavailable_provider_ids?: unknown;
  reason?: string | null;
  persisted?: {
    provider?: string | null;
    model?: string | null;
    base_url?: string | null;
  } | null;
  effective?: {
    provider?: string | null;
    model?: string | null;
    base_url?: string | null;
    server_profile?: string | null;
    tool_capability?: string | null;
  } | null;
};

type ProviderVisibilitySettings = {
  hidden_provider_ids?: unknown;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Normalize a provider id without accepting malformed settings values. */
export function normalizeProviderId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  return normalized || null;
}

/** Read and normalize the user's hidden provider list. */
export function normalizeHiddenProviderIds(
  settings: UserSettings | null | undefined,
): string[] {
  const rawVisibility = settings?.[LLM_PROVIDER_VISIBILITY_KEY];
  if (!isRecord(rawVisibility)) return [];

  const rawIds = (rawVisibility as ProviderVisibilitySettings).hidden_provider_ids;
  if (!Array.isArray(rawIds)) return [];

  const seen = new Set<string>();
  for (const rawId of rawIds) {
    const providerId = normalizeProviderId(rawId);
    if (providerId) seen.add(providerId);
  }
  return Array.from(seen);
}

/**
 * Filter provider-like options while retaining explicitly selected values.
 * The accessor keeps the helper usable for both `{ id }` catalogs and
 * header options shaped as `{ provider }`.
 */
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

export function isProviderHidden(
  providerId: unknown,
  settings: UserSettings | null | undefined,
): boolean {
  const normalizedId = normalizeProviderId(providerId);
  return normalizedId
    ? normalizeHiddenProviderIds(settings).includes(normalizedId)
    : false;
}

type ProviderAvailabilityFields = {
  available?: unknown;
  disabled?: unknown;
  unavailable?: unknown;
};

function normalizedProviderIdSet(value: unknown): Set<string> {
  if (!Array.isArray(value)) return new Set();
  const result = new Set<string>();
  for (const item of value) {
    const normalized = normalizeProviderId(item);
    if (normalized) result.add(normalized);
  }
  return result;
}

/** Resolve the effective provider/model while accepting legacy nested fields. */
export function resolveEffectiveProviderId(
  deployment: LlmDeploymentMetadata | null | undefined,
): string | null {
  return normalizeProviderId(
    deployment?.effective_provider ??
      deployment?.fixed_provider ??
      deployment?.effective?.provider,
  );
}

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

/** True when the backend supplied a deployment constraint/status object. */
export function hasDeploymentMetadata(
  deployment: LlmDeploymentMetadata | null | undefined,
): boolean {
  if (!deployment || typeof deployment !== "object") return false;
  return Boolean(
    deployment.backend ||
      deployment.transport ||
      deployment.fixed != null ||
      deployment.ready != null ||
      resolveEffectiveProviderId(deployment) ||
      resolveEffectiveModelId(deployment) ||
      normalizedProviderIdSet(deployment.allowed_provider_ids).size ||
      normalizedProviderIdSet(deployment.unavailable_provider_ids).size ||
      deployment.reason,
  );
}

/**
 * Return whether a provider is available for normal UI selection.
 *
 * Provider-local `available:false`/`disabled:true` is authoritative even when
 * deployment metadata is absent.  Deployment allow/deny lists are enforced
 * only when non-empty, preserving personal/legacy behaviour when omitted.
 */
export function isProviderAvailable(
  providerId: unknown,
  deployment?: LlmDeploymentMetadata | null,
  provider?: ProviderAvailabilityFields | null,
): boolean {
  const normalizedId = normalizeProviderId(providerId);
  if (!normalizedId) return false;

  if (provider?.available === false || provider?.disabled === true || provider?.unavailable === true) {
    return false;
  }

  const allowed = normalizedProviderIdSet(deployment?.allowed_provider_ids);
  if (allowed.size && !allowed.has(normalizedId)) return false;

  const unavailable = normalizedProviderIdSet(
    deployment?.unavailable_provider_ids,
  );
  return !unavailable.has(normalizedId);
}

/** Filter backend-marked unavailable provider choices. */
export function filterAvailableProviders<T>(
  providers: readonly T[] | null | undefined,
  deployment?: LlmDeploymentMetadata | null,
  getProviderId: (provider: T) => unknown = (provider) =>
    (provider as { id?: unknown }).id,
  getAvailability: (provider: T) => ProviderAvailabilityFields | null | undefined =
    (provider) => provider as ProviderAvailabilityFields,
): T[] {
  return (providers ?? []).filter((provider) =>
    isProviderAvailable(
      getProviderId(provider),
      deployment,
      getAvailability(provider),
    ),
  );
}

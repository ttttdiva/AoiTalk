import type { LlmModelCatalogResponse } from "@/lib/chat-api";
import { defaultModeForOptions } from "@/components/settings/llm-model-section-types";
import type { SessionMainRoute } from "@/lib/chat-llm-settings";

export type RouteFragment = {
  provider?: string;
  model?: string;
  effort?: string;
};

export type ResolvedUnderlyingRoute = {
  provider: string;
  model: string;
  effort: string;
};

type NullableRouteFields = {
  provider?: string | null;
  model?: string | null;
  effort?: string | null;
};

export function normalizeRouteFragment(
  route: SessionMainRoute | RouteFragment | NullableRouteFields | null | undefined,
): RouteFragment {
  const raw = route ?? {};
  return {
    provider: typeof raw.provider === "string" ? raw.provider.trim().toLowerCase() : "",
    model: typeof raw.model === "string" ? raw.model.trim() : "",
    effort: typeof raw.effort === "string" ? raw.effort.trim() : "",
  };
}

export function getModelEffortOptionsFromCatalog(
  catalog: LlmModelCatalogResponse | null | undefined,
  provider: string,
  model: string,
): string[] {
  const normalizedProvider = provider.trim().toLowerCase();
  const normalizedModel = model.trim();
  if (!normalizedProvider || !normalizedModel) return [];
  const providerEntry = catalog?.providers?.find((item) => item.id === normalizedProvider);
  const modelEntry = providerEntry?.models?.find((item) => item.id === normalizedModel);
  if (normalizedProvider === "openai_compatible_local") {
    const modelHasProfileContract = Boolean(
      modelEntry?.reasoning_effort_default && modelEntry?.reasoning_effort_wire,
    );
    if (modelHasProfileContract) return modelEntry?.reasoning_effort_options ?? [];
    const settings = providerEntry?.settings;
    const settingsHaveProfileContract = Boolean(
      settings?.reasoning_effort_default && settings?.reasoning_effort_wire,
    );
    return settingsHaveProfileContract ? settings?.reasoning_effort_options ?? [] : [];
  }
  return modelEntry?.reasoning_effort_options ?? [];
}

export function hasExplicitSessionRoute(route: SessionMainRoute | RouteFragment | null | undefined): boolean {
  const normalized = normalizeRouteFragment(route);
  return Boolean(normalized.provider && normalized.model);
}

export type UnderlyingRouteContext = {
  sessionId?: string | null;
  mainRoute?: SessionMainRoute | RouteFragment | NullableRouteFields | null;
  newChatEffectiveMain?: NullableRouteFields | null;
  sessionEffectiveMain?: NullableRouteFields | null;
  runtimeProvider?: string | null;
  runtimeModel?: string | null;
};

function resolveRouteFromAuthoritativeSources(
  context: UnderlyingRouteContext,
): ResolvedUnderlyingRoute | null {
  const routeFromSettings = normalizeRouteFragment(context.mainRoute);
  const isNewChat = !context.sessionId;
  const effectiveMain = normalizeRouteFragment(
    isNewChat ? context.newChatEffectiveMain : context.sessionEffectiveMain,
  );
  // Do not synthesize a generation route by combining a partial desired
  // provider/model with fields from an older effective route.  A complete
  // explicit route wins; otherwise wait for a complete authoritative
  // session/new-chat effective route.  The display resolver may still show a
  // field-level provisional fallback, but generation must fail closed here.
  if (routeFromSettings.provider && routeFromSettings.model) {
    return {
      provider: routeFromSettings.provider,
      model: routeFromSettings.model,
      // Keep the authoritative source atomic.  An explicit provider/model
      // route must not inherit an effort value from an older effective route.
      effort: routeFromSettings.effort || "",
    };
  }
  if (effectiveMain.provider && effectiveMain.model) {
    return {
      provider: effectiveMain.provider,
      model: effectiveMain.model,
      effort: effectiveMain.effort || "",
    };
  }
  return null;
}

/**
 * Provider / Model の表示用 route。
 *
 * session/new-chat の effective route がまだ届いていない間は runtime current を
 * provisional fallback として返す。これは UI 表示専用で、generation の authority
 * 判定には使わないこと。
 */
export function resolveDisplayedRoute(
  context: UnderlyingRouteContext,
): ResolvedUnderlyingRoute {
  const routeFromAuthoritativeSources = resolveRouteFromAuthoritativeSources(context);
  if (routeFromAuthoritativeSources) return routeFromAuthoritativeSources;

  const routeFromSettings = normalizeRouteFragment(context.mainRoute);
  const fallbackProvider = context.runtimeProvider?.trim().toLowerCase() ?? "";
  const fallbackModel = context.runtimeModel?.trim() ?? "";
  const isNewChat = !context.sessionId;
  const effectiveMain = normalizeRouteFragment(
    isNewChat ? context.newChatEffectiveMain : context.sessionEffectiveMain,
  );

  return {
    provider: routeFromSettings.provider || effectiveMain.provider || fallbackProvider,
    model: routeFromSettings.model || effectiveMain.model || fallbackModel,
    effort: routeFromSettings.effort || effectiveMain.effort || "",
  };
}

/**
 * Generation 開始に利用できる authoritative route。
 * runtime current は backend の session/new-chat settings を確認していないため、
 * provisional display には使えても generation authority には昇格させない。
 */
export function resolveGenerationReadyRoute(
  context: UnderlyingRouteContext,
): ResolvedUnderlyingRoute | null {
  return resolveRouteFromAuthoritativeSources(context);
}

/** Display and update handlers share this resolver so new-chat last-used fallbacks stay aligned. */
export function resolveUnderlyingRoute(context: UnderlyingRouteContext): ResolvedUnderlyingRoute {
  const route = resolveDisplayedRoute(context);
  // Keep the legacy helper's effort contract for update handlers. Displayed
  // effort is resolved separately by resolveDisplayedEffort (which can include
  // the effective settings envelope).
  return {
    ...route,
    effort: normalizeRouteFragment(context.mainRoute).effort ?? "",
  };
}

export function resolveDisplayedEffort(
  context: UnderlyingRouteContext & { freeTeamActive?: boolean },
): string {
  if (context.freeTeamActive) return "";
  const routeEffort = normalizeRouteFragment(context.mainRoute).effort;
  if (routeEffort) return routeEffort;
  if (context.sessionId) {
    return context.sessionEffectiveMain?.effort?.trim() ?? "";
  }
  const newChatMain = context.newChatEffectiveMain;
  if (newChatMain?.provider?.trim() || newChatMain?.model?.trim()) {
    return newChatMain.effort?.trim() ?? "";
  }
  return "";
}

export function formatRouteLabel(provider: string, model: string): string {
  if (!provider && !model) return "";
  if (!provider) return model;
  if (!model) return provider;
  return `${provider} / ${model}`;
}

export function resolveEffortForModel(
  catalog: LlmModelCatalogResponse | null | undefined,
  provider: string,
  model: string,
  previousEffort: string | undefined,
): string | undefined {
  const options = getModelEffortOptionsFromCatalog(catalog, provider, model);
  if (options.length === 0) return undefined;
  const providerEntry = catalog?.providers?.find((item) => item.id === provider.trim().toLowerCase());
  const modelEntry = providerEntry?.models?.find((item) => item.id === model.trim());
  const preferred = previousEffort?.trim()
    || modelEntry?.reasoning_effort_default
    || providerEntry?.settings?.reasoning_effort_default
    || "medium";
  return defaultModeForOptions(options, preferred);
}

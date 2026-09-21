"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import type { RuntimeContextValue } from "@/contexts/runtime-context";
import type { LlmCatalogModelOption, LlmCatalogProvider } from "@/lib/chat-api";
import {
  fetchAgentTeamOptions,
  fetchNewChatLlmDefaultsAfterLastUsedFlush,
  fetchSessionLlmSettings,
  FREE_TEAM_ROUTING_PROFILE_ID,
  recordLastUsedLlmRoute,
  saveSessionLlmSettings,
  type AgentTeamExecutionProfileOption,
  type AgentTeamOption,
  type SessionLlmSettings,
  type SessionLlmSettingsResponse,
  type SessionMainRoute,
} from "@/lib/chat-llm-settings";
import {
  filterAvailableProviders,
  filterVisibleProviders,
} from "@/lib/llm-provider-visibility";
import {
  formatRouteLabel,
  getModelEffortOptionsFromCatalog,
  hasExplicitSessionRoute,
  resolveDisplayedEffort,
  resolveEffortForModel,
  resolveGenerationReadyRoute,
  resolveUnderlyingRoute,
  type ResolvedUnderlyingRoute,
} from "@/lib/chat-session-route";
import {
  getPendingNewChatLlmSettings,
  hydratePendingNewChatLlmSettings,
  setPendingNewChatLlmSettings,
} from "@/lib/new-chat-llm-settings-store";
import {
  awaitSessionLlmSettingsReady,
  enqueueSessionLlmSettingsSave,
} from "@/lib/session-llm-settings-save-queue";
import type { UserSettings } from "@/lib/user-settings";

const STRICT_RUNTIME_PROVIDERS = new Set([
  "openai_compatible_local",
  "ollama",
  "sglang",
]);

function normalizedProviderId(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function isStrictRuntimeProvider(value: unknown): boolean {
  return STRICT_RUNTIME_PROVIDERS.has(normalizedProviderId(value));
}

function uniqueModelOptions(
  options: readonly LlmCatalogModelOption[],
): LlmCatalogModelOption[] {
  const result: LlmCatalogModelOption[] = [];
  const seen = new Set<string>();
  for (const option of options) {
    const id = typeof option?.id === "string" ? option.id.trim() : "";
    if (!id || seen.has(id)) continue;
    seen.add(id);
    result.push(option);
  }
  return result;
}

/**
 * Resolve the model projection that is safe to expose in Chat settings.
 *
 * ``models`` is a settings/catalog projection.  For strict runtime providers
 * only ``chat_models`` (or the legacy ``available_models`` alias) proves that
 * a model is served or verified by the backend as locally auto-startable.
 * A missing projection can be recovered from
 * the engine endpoint's last-known-good candidates; an explicit empty
 * projection stays empty so stale models are never fabricated.
 */
function servedModelsForProvider(
  provider: LlmCatalogProvider | null | undefined,
  runtimeEngines: readonly RuntimeContextValue["llmEngines"][number][],
): LlmCatalogModelOption[] {
  const providerId = normalizedProviderId(provider?.id);
  const runtimeModels = runtimeEngines
    .filter((engine) => normalizedProviderId(engine.provider) === providerId)
    .filter((engine) => engine.available !== false && engine.disabled !== true && engine.unavailable !== true)
    .map((engine) => ({
      id: engine.model,
      label: engine.label || engine.model,
      reasoning_effort_options: engine.reasoning_effort_options,
      context_window_tokens: engine.context_window_tokens,
      supports_reasoning: engine.supports_reasoning,
    }));

  if (!provider) return uniqueModelOptions(runtimeModels);
  if (!isStrictRuntimeProvider(provider.id)) {
    return uniqueModelOptions(
      Array.isArray(provider.chat_models) ? provider.chat_models : provider.models ?? [],
    );
  }

  // A strict discovery error should retain last-known-good served candidates
  // when the backend preserved them (or when the engine endpoint supplied an
  // LKG).  It must not synthesize a model from the static settings catalog;
  // with no LKG, the selector remains empty and the retry path is explicit.
  if (provider.availability?.state === "error") {
    if (Array.isArray(provider.chat_models) && provider.chat_models.length > 0) {
      return uniqueModelOptions(provider.chat_models);
    }
    if (
      Array.isArray(provider.available_models) &&
      provider.available_models.length > 0
    ) {
      return uniqueModelOptions(provider.available_models);
    }
    return uniqueModelOptions(runtimeModels);
  }

  if (Array.isArray(provider.chat_models)) {
    // A non-empty discovery response is authoritative.  An explicit empty
    // response is also authoritative unless the backend marked it as an
    // error, in which case an engine LKG is safer than blanking controls.
    if (provider.chat_models.length > 0) {
      return uniqueModelOptions(provider.chat_models);
    }
    return [];
  }
  if (Array.isArray(provider.available_models)) {
    return uniqueModelOptions(provider.available_models);
  }
  return uniqueModelOptions(runtimeModels);
}

export const AGENT_TEAM_SELECTOR_AUTO = "__auto__";
export const AGENT_TEAM_SELECTOR_FREE_TEAM = "__free_team__";
export const AGENT_TEAM_VALUE_AUTO = AGENT_TEAM_SELECTOR_AUTO;
export const AGENT_TEAM_VALUE_FREE_TEAM = AGENT_TEAM_SELECTOR_FREE_TEAM;

let displayedNewChatMainRoute: SessionMainRoute | null = null;
let generationReadyNewChatMainRoute: SessionMainRoute | null = null;

function toDisplayedMainRoute(
  route: ResolvedUnderlyingRoute,
  effort?: string,
): SessionMainRoute {
  const next: SessionMainRoute = {};
  if (route.provider) next.provider = route.provider;
  if (route.model) next.model = route.model;
  const trimmedEffort = effort?.trim();
  if (trimmedEffort) next.effort = trimmedEffort;
  return next;
}

function toGenerationMainRoute(route: ResolvedUnderlyingRoute): SessionMainRoute {
  const next: SessionMainRoute = {};
  if (route.provider) next.provider = route.provider;
  if (route.model) next.model = route.model;
  const trimmedEffort = route.effort.trim();
  if (trimmedEffort) next.effort = trimmedEffort;
  return next;
}

/** このタブの新規チャットが表示している解決済み route。別タブの last-used では変わらない。 */
export function getDisplayedNewChatMainRoute(): SessionMainRoute | null {
  return displayedNewChatMainRoute;
}

/**
 * 新規チャットの generation に渡してよい authoritative route。
 * runtime current の provisional 表示はここへ昇格させない。
 */
export function getGenerationReadyNewChatMainRoute(): SessionMainRoute | null {
  return generationReadyNewChatMainRoute;
}

export function resetDisplayedNewChatMainRoute(): void {
  displayedNewChatMainRoute = null;
  generationReadyNewChatMainRoute = null;
}

/**
 * 新規チャットの表示 route snapshot は commit 後の layout effect でのみ書く。
 * render 本体から module-global を触らないので、commit されなかった render が
 * 表示中 A を壊さない。layout effect は paint 前・click より先に走る。
 */
export function useSyncDisplayedNewChatMainRoute(
  sessionId: string | null | undefined,
  route: ResolvedUnderlyingRoute,
  effort?: string,
  generationReadyRoute: ResolvedUnderlyingRoute | null = route,
): void {
  // Route objects are derived during render; primitive fields intentionally
  // form the dependency key so a fresh object does not re-run this snapshot
  // effect on every render.
  const routeProvider = route.provider;
  const routeModel = route.model;
  const generationProvider = generationReadyRoute?.provider ?? "";
  const generationModel = generationReadyRoute?.model ?? "";
  const generationEffort = generationReadyRoute?.effort ?? "";
  useLayoutEffect(() => {
    if (sessionId) return;
    displayedNewChatMainRoute = toDisplayedMainRoute(
      { provider: routeProvider, model: routeModel, effort: effort ?? "" },
      effort,
    );
    generationReadyNewChatMainRoute = generationProvider && generationModel
      ? toGenerationMainRoute({
          provider: generationProvider,
          model: generationModel,
          effort: generationEffort,
        })
      : null;
  }, [
    sessionId,
    routeProvider,
    routeModel,
    effort,
    generationProvider,
    generationModel,
    generationEffort,
  ]);
}

const defaultSessionSettings = (): SessionLlmSettings => ({
  agent_team_selection: {
    mode: "auto",
    team_id: "",
    loaded_team_ids: [],
  },
  main_route: {},
  special_routing: {},
  execution_profile_id: "",
});

function normalizeExecutionProfileId(value?: string | null): string {
  return typeof value === "string" ? value.trim() : "";
}

function cloneSettings(settings: SessionLlmSettings): SessionLlmSettings {
  return {
    agent_team_selection: {
      mode: settings.agent_team_selection.mode,
      team_id: settings.agent_team_selection.team_id,
      loaded_team_ids: [...settings.agent_team_selection.loaded_team_ids],
    },
    main_route: settings.main_route ? { ...settings.main_route } : {},
    special_routing: settings.special_routing ? { ...settings.special_routing } : {},
    execution_profile_id: normalizeExecutionProfileId(settings.execution_profile_id),
  };
}

function executionProfilesForTeam(
  teamId: string,
  teams: AgentTeamOption[],
): AgentTeamExecutionProfileOption[] {
  return teams.find((team) => team.team_id === teamId)?.execution_profiles ?? [];
}

function resolveExecutionProfileForTeamChange(
  selectorValue: string,
  currentProfileId: string | undefined,
  teams: AgentTeamOption[],
): string {
  if (
    selectorValue === AGENT_TEAM_SELECTOR_AUTO ||
    selectorValue === AGENT_TEAM_SELECTOR_FREE_TEAM
  ) {
    return "";
  }
  const current = normalizeExecutionProfileId(currentProfileId);
  if (!current) return "";
  return executionProfilesForTeam(selectorValue, teams).some(
    (profile) => profile.profile_id === current,
  )
    ? current
    : "";
}

function resolveAgentTeamSelectorValue(settings: SessionLlmSettings): string {
  if (settings.special_routing?.routing_profile_id === FREE_TEAM_ROUTING_PROFILE_ID) {
    return AGENT_TEAM_SELECTOR_FREE_TEAM;
  }
  const team = settings.agent_team_selection;
  if (team.mode === "fixed" && team.team_id.trim()) {
    return team.team_id;
  }
  return AGENT_TEAM_SELECTOR_AUTO;
}

function isFreeTeamActive(settings: SessionLlmSettings): boolean {
  return settings.special_routing?.routing_profile_id === FREE_TEAM_ROUTING_PROFILE_ID;
}

type UseChatSessionRouteArgs = {
  sessionId?: string | null;
  runtime: RuntimeContextValue;
  userSettings?: UserSettings | null;
};

export function useChatSessionRoute({
  sessionId,
  runtime,
  userSettings,
}: UseChatSessionRouteArgs) {
  // User-scoped visibility is legacy state. Provider visibility is now read
  // from the DB-backed global catalog metadata below.
  void userSettings;
  const userId = useCurrentUserId();
  const routeScopeKey = `${userId ?? "__anonymous__"}\u0000${sessionId ?? "__new_chat__"}`;
  const [desiredSettings, setDesiredSettings] = useState<SessionLlmSettings>(
    defaultSessionSettings(),
  );
  const [sessionEffectiveMain, setSessionEffectiveMain] = useState<
    SessionLlmSettingsResponse["effective_main"] | null
  >(null);
  const [newChatEffectiveMain, setNewChatEffectiveMain] = useState<
    SessionLlmSettingsResponse["effective_main"] | null
  >(null);
  const [routeLoading, setRouteLoading] = useState(false);
  const [agentTeamOptions, setAgentTeamOptions] = useState<AgentTeamOption[]>([]);
  const [teamsLoading, setTeamsLoading] = useState(false);
  const [resolvedRouteScopeKey, setResolvedRouteScopeKey] = useState<string | null>(null);
  const settingsRevisionRef = useRef(0);
  const desiredSettingsRef = useRef(desiredSettings);

  // New-chat pending settings are synchronous (memory/localStorage) and must be
  // considered before the async defaults request. In particular, do not let a
  // previous session's desiredSettings become the new-chat route for one render.
  const pendingSettings = sessionId
    ? defaultSessionSettings()
    : getPendingNewChatLlmSettings(userId);
  const routeSettings = sessionId
    ? resolvedRouteScopeKey === routeScopeKey
      ? desiredSettings
      : defaultSessionSettings()
    : pendingSettings;
  desiredSettingsRef.current = routeSettings;

  useEffect(() => {
    hydratePendingNewChatLlmSettings(userId);
  }, [userId]);

  useEffect(() => {
    if (sessionId) return;
    setDesiredSettings(cloneSettings(getPendingNewChatLlmSettings(userId)));
  }, [sessionId, userId]);

  useEffect(() => {
    let cancelled = false;

    const loadTeams = async () => {
      setTeamsLoading(true);
      try {
        const teams = await fetchAgentTeamOptions();
        if (!cancelled) setAgentTeamOptions(teams);
      } catch {
        if (!cancelled) setAgentTeamOptions([]);
      } finally {
        if (!cancelled) setTeamsLoading(false);
      }
    };

    void loadTeams();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      setRouteLoading(true);
      // An effective route is scoped to the current session/user. Clear the
      // previous response while the next one is in flight so it cannot become
      // generation authority during a session A/B or user switch.
      setSessionEffectiveMain(null);
      setNewChatEffectiveMain(null);
      try {
        if (sessionId) {
          // A selector change persists asynchronously through the module-level
          // per-session PUT queue. If this hook is unmounted for another app
          // route and immediately mounted again, a GET must not overtake the
          // already-queued PUT and freeze this mount on stale server state.
          try {
            await awaitSessionLlmSettingsReady(sessionId);
          } catch {
            // Preference-save failure must not prevent loading the canonical
            // server state, matching the existing new-chat flush contract.
          }
          if (cancelled) return;

          const sessionEnvelope = await fetchSessionLlmSettings(sessionId);
          if (!cancelled) {
            setDesiredSettings(cloneSettings(sessionEnvelope.settings));
            setSessionEffectiveMain(sessionEnvelope.effective_main ?? null);
            setNewChatEffectiveMain(null);
            setResolvedRouteScopeKey(routeScopeKey);
          }
        } else {
          const defaultsEnvelope = await fetchNewChatLlmDefaultsAfterLastUsedFlush(userId);
          if (cancelled) return;

          const pending = getPendingNewChatLlmSettings(userId);
          setDesiredSettings(cloneSettings(pending));
          setSessionEffectiveMain(null);
          setNewChatEffectiveMain(defaultsEnvelope.effective_main ?? null);
          setResolvedRouteScopeKey(routeScopeKey);
        }
      } catch {
        if (!cancelled) {
          setDesiredSettings(
            sessionId
              ? defaultSessionSettings()
              : cloneSettings(getPendingNewChatLlmSettings(userId)),
          );
          setSessionEffectiveMain(null);
          setNewChatEffectiveMain(null);
          setResolvedRouteScopeKey(routeScopeKey);
        }
      } finally {
        if (!cancelled) setRouteLoading(false);
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [routeScopeKey, sessionId, userId]);

  const freeTeamActive = isFreeTeamActive(routeSettings);
  const agentTeamSelectionValue = resolveAgentTeamSelectorValue(routeSettings);

  const routeContext = {
    sessionId,
    mainRoute: routeSettings.main_route,
    newChatEffectiveMain,
    sessionEffectiveMain,
    runtimeProvider: runtime.currentLlm?.provider,
    runtimeModel: runtime.currentLlm?.model,
  };
  const displayedRoute = resolveUnderlyingRoute(routeContext);
  const generationReadyRoute = resolveGenerationReadyRoute(routeContext);
  const authoritativeRouteProvider = generationReadyRoute?.provider ?? "";
  const underlyingProvider = displayedRoute.provider;
  const underlyingModel = displayedRoute.model;

  const effectiveProvider = underlyingProvider;
  const effectiveModel = underlyingModel;

  const effectiveEffort = resolveDisplayedEffort({
    ...routeContext,
    freeTeamActive,
  });

  const catalogProviders = useMemo(() => {
    const persistedProviders = runtime.llmCatalog?.providers ?? [];
    const runtimeByProvider = new Map<
      string,
      RuntimeContextValue["llmEngines"]
    >();
    for (const engine of runtime.llmEngines) {
      const providerId = normalizedProviderId(engine.provider);
      if (!providerId || !engine.model?.trim()) continue;
      const current = runtimeByProvider.get(providerId) ?? [];
      current.push(engine);
      runtimeByProvider.set(providerId, current);
    }

    // The engine endpoint is the LLM last-known-good (LKG) source while the
    // richer catalog is still loading or recovering from an error.  Merge
    // only strict-runtime served candidates here; cloud providers retain the
    // catalog's broader settings list.
    const providers = persistedProviders.map((provider) => {
      if (!isStrictRuntimeProvider(provider.id)) return provider;
      const runtimeModels = runtimeByProvider.get(normalizedProviderId(provider.id)) ?? [];
      if (runtimeModels.length === 0) return provider;
      const existing = Array.isArray(provider.chat_models)
        ? provider.chat_models
        : Array.isArray(provider.available_models)
          ? provider.available_models
          : [];
      const runtimeOptions = runtimeModels.map((engine) => ({
        id: engine.model,
        label: engine.label || engine.model,
        reasoning_effort_options: engine.reasoning_effort_options,
        context_window_tokens: engine.context_window_tokens,
        supports_reasoning: engine.supports_reasoning,
      }));
      return {
        ...provider,
        // Preserve an explicit successful non-empty discovery response as the
        // authority.  Runtime engines fill only a missing/error projection;
        // this prevents stale strict models from being revived after an
        // explicit empty response.
        chat_models:
          existing.length > 0
            ? provider.chat_models ?? provider.available_models
            : !Array.isArray(provider.chat_models) &&
                provider.availability?.state !== "empty"
              ? uniqueModelOptions(runtimeOptions)
              : provider.chat_models,
      };
    });

    // If /llm/models is unavailable altogether, still expose the engine LKG
    // as a provider/model selector.  These entries are runtime candidates,
    // not fabricated static settings models.
    const knownProviders = new Set(
      providers.map((provider) => normalizedProviderId(provider.id)),
    );
    for (const [providerId, engines] of runtimeByProvider) {
      if (knownProviders.has(providerId)) continue;
      const first = engines[0];
      if (!first) continue;
      const options = engines
        .filter(
          (engine) =>
            engine.available !== false &&
            engine.disabled !== true &&
            engine.unavailable !== true,
        )
        .map((engine) => ({
          id: engine.model,
          label: engine.label || engine.model,
          reasoning_effort_options: engine.reasoning_effort_options,
          context_window_tokens: engine.context_window_tokens,
          supports_reasoning: engine.supports_reasoning,
        }));
      if (options.length === 0) continue;
      providers.push({
        id: first.provider,
        label: first.label || first.provider,
        models: [],
        chat_models: uniqueModelOptions(options),
      });
    }

    // The engine endpoint can legitimately succeed while the richer catalog
    // is still loading or has timed out. Preserve a non-strict effective
    // route as a selectable LKG candidate in that window. Strict runtimes
    // require an actually advertised engine (handled above), so a stale local
    // model is never fabricated from the route alone.
    const runtimeRoute = runtime.effectiveLlm ?? runtime.currentLlm;
    const runtimeProviderId = normalizedProviderId(runtimeRoute?.provider);
    const runtimeModelId = runtimeRoute?.model?.trim() ?? "";
    const runtimeHasProof =
      Boolean(runtimeProviderId && runtimeModelId) &&
      (!isStrictRuntimeProvider(runtimeProviderId) ||
        runtime.llmEngines.some(
          (engine) =>
            normalizedProviderId(engine.provider) === runtimeProviderId &&
            engine.model.trim() === runtimeModelId &&
            engine.available !== false &&
            engine.disabled !== true &&
            engine.unavailable !== true,
        ));
    if (
      runtimeHasProof &&
      runtimeRoute
    ) {
      const existingIndex = providers.findIndex(
        (provider) => normalizedProviderId(provider.id) === runtimeProviderId,
      );
      const existing = existingIndex >= 0 ? providers[existingIndex] : undefined;
      if (existing && !isStrictRuntimeProvider(runtimeProviderId)) {
        const existingModels = Array.isArray(existing.chat_models)
          ? existing.chat_models
          : existing.models;
        if (!existingModels.some((model) => model.id === runtimeModelId)) {
          providers[existingIndex] = {
            ...existing,
            chat_models: uniqueModelOptions([
              ...existingModels,
              { id: runtimeModelId, label: runtimeModelId },
            ]),
          };
        }
      } else if (!existing) {
        providers.push({
          id: runtimeRoute.provider,
          label: runtimeRoute.provider,
          models: [],
          chat_models: [{ id: runtimeModelId, label: runtimeModelId }],
        });
      }
    }

    // Keep a complete authoritative session/new-chat provider visible even
    // when a partial catalog omitted its entry.  The synthetic entry carries
    // no model list, so it cannot fabricate a stale model; the selected route
    // remains diagnostic until a served alternative is advertised.
    if (
      authoritativeRouteProvider &&
      !providers.some(
        (provider) =>
          normalizedProviderId(provider.id) ===
          normalizedProviderId(authoritativeRouteProvider),
      )
    ) {
      providers.push({
        id: authoritativeRouteProvider,
        label: authoritativeRouteProvider,
        models: [],
        chat_models: [],
      });
    }

    const available = filterAvailableProviders(
      providers,
      runtime.llmDeployment,
      (item) => item.id,
      (item) => item,
    );

    // Deployment availability describes the global runtime. A complete
    // session route is independently authoritative and may intentionally
    // differ from that runtime default. Preserve only that currently selected
    // provider so the controlled selector can represent the server state
    // without broadly bypassing deployment filtering.
    const authoritativeProvider = authoritativeRouteProvider
      ? providers.find(
          (item) =>
            normalizedProviderId(item.id) ===
            normalizedProviderId(authoritativeRouteProvider),
        ) ?? null
      : null;
    const authoritativeIsAvailable = Boolean(
      authoritativeProvider &&
        filterAvailableProviders(
          [authoritativeProvider],
          runtime.llmDeployment,
          (item) => item.id,
          (item) => item,
        ).length > 0,
    );
    const candidates =
      authoritativeProvider &&
      authoritativeIsAvailable &&
      !available.some(
        (item) =>
          normalizedProviderId(item.id) ===
          normalizedProviderId(authoritativeRouteProvider),
      )
        ? [authoritativeProvider, ...available]
        : available;

    return filterVisibleProviders(
      candidates,
      runtime.llmCatalog?.provider_visibility,
      [],
      (item) => item.id,
    ).filter(
      (item) =>
        normalizedProviderId(item.id) ===
          normalizedProviderId(authoritativeRouteProvider) ||
        (item.available !== false && item.disabled !== true),
    );
  }, [
    authoritativeRouteProvider,
    runtime.llmEngines,
    runtime.llmCatalog?.providers,
    runtime.llmCatalog?.provider_visibility,
    runtime.llmDeployment,
    runtime.currentLlm,
    runtime.effectiveLlm,
  ]);

  useSyncDisplayedNewChatMainRoute(
    sessionId,
    displayedRoute,
    effectiveEffort,
    generationReadyRoute,
  );

  const effortOptions = useMemo(() => {
    if (freeTeamActive) return [];
    const fromCatalog = getModelEffortOptionsFromCatalog(
      runtime.llmCatalog,
      effectiveProvider,
      effectiveModel,
    );
    const fromEngine = runtime.llmEngines.find(
      (engine) =>
        normalizedProviderId(engine.provider) ===
          normalizedProviderId(effectiveProvider) &&
        engine.model === effectiveModel,
    );
    const options =
      fromCatalog.length > 0
        ? fromCatalog
        : fromEngine?.reasoning_effort_options ?? [];
    const authoritativeEffort = sessionId ? effectiveEffort.trim() : "";
    return authoritativeEffort && !options.includes(authoritativeEffort)
      ? [authoritativeEffort, ...options]
      : options;
  }, [
    effectiveEffort,
    effectiveModel,
    effectiveProvider,
    freeTeamActive,
    runtime.llmCatalog,
    runtime.llmEngines,
    sessionId,
  ]);

  const selectedProvider =
    catalogProviders.find(
      (item) =>
        normalizedProviderId(item.id) === normalizedProviderId(effectiveProvider),
    ) ?? null;
  const providerModels = servedModelsForProvider(
    selectedProvider,
    runtime.llmEngines,
  );
  // ``effectiveModel`` is intentionally not prepended when it disappeared
  // from the runtime projection.  The persisted/session route remains
  // untouched for execution compatibility, while the selector exposes only
  // models the runtime actually advertised.
  const modelOptions = providerModels;
  const modelDiscoveryError =
    selectedProvider?.availability?.state === "error"
      ? selectedProvider.availability.error?.trim() ||
        "現在のruntimeモデル一覧を取得できませんでした"
      : runtime.llmStatus === "stale" && runtime.llmCatalog == null
        ? "Model情報を一時取得できませんでした"
      : null;

  const hasSessionScopedRoute = sessionId
    ? hasExplicitSessionRoute(routeSettings.main_route) ||
      Boolean(sessionEffectiveMain?.provider && sessionEffectiveMain?.model) ||
      freeTeamActive
    : hasExplicitSessionRoute(routeSettings.main_route) || freeTeamActive;

  const persistDesiredSettings = useCallback(
    async (nextSettings: SessionLlmSettings): Promise<SessionLlmSettingsResponse | null> => {
      const snapshot = cloneSettings(nextSettings);
      if (!snapshot.special_routing?.routing_profile_id?.trim()) {
        delete snapshot.special_routing;
      }
      snapshot.execution_profile_id = normalizeExecutionProfileId(
        snapshot.execution_profile_id,
      );
      const revision = settingsRevisionRef.current + 1;
      settingsRevisionRef.current = revision;

      if (sessionId) {
        const saved = await enqueueSessionLlmSettingsSave(sessionId, () =>
          saveSessionLlmSettings(sessionId, snapshot),
        );
        if (settingsRevisionRef.current === revision) {
          setDesiredSettings(cloneSettings(saved.settings));
          setSessionEffectiveMain(saved.effective_main ?? null);
          setResolvedRouteScopeKey(routeScopeKey);
        }
        return saved;
      }

      if (!userId) return null;
      setPendingNewChatLlmSettings(snapshot, userId);
      if (settingsRevisionRef.current === revision) {
        setDesiredSettings(snapshot);
      }
      if (hasExplicitSessionRoute(snapshot.main_route)) {
        const defaults = await recordLastUsedLlmRoute(snapshot.main_route, {
          userId,
        });
        if (
          settingsRevisionRef.current === revision &&
          (defaults.effective_main?.provider || defaults.effective_main?.model)
        ) {
          setNewChatEffectiveMain(defaults.effective_main ?? null);
        }
      }
      return null;
    },
    [routeScopeKey, sessionId, userId],
  );

  const applyDesiredSettings = useCallback(
    (updater: (current: SessionLlmSettings) => SessionLlmSettings) => {
      const next = cloneSettings(updater(cloneSettings(desiredSettingsRef.current)));
      setDesiredSettings(next);
      void persistDesiredSettings(next);
      return next;
    },
    [persistDesiredSettings],
  );

  const updateProvider = useCallback(
    (nextProvider: string) => {
      const provider = catalogProviders.find(
        (item) =>
          normalizedProviderId(item.id) === normalizedProviderId(nextProvider),
      );
      const availableModels = servedModelsForProvider(
        provider,
        runtime.llmEngines,
      );
      const firstModel = availableModels[0]?.id ?? "";
      const configuredModel = provider?.configured_model?.trim();
      const nextModel =
        configuredModel &&
        availableModels.some((item) => item.id === configuredModel)
          ? configuredModel
          : firstModel;
      if (!nextProvider) return;
      if (!nextModel) {
        toast.error(
          normalizedProviderId(nextProvider) === "openai_compatible_local"
            ? "利用可能なローカルモデルがありません。設定のモデル画面でGGUFの保存先とllama.cppの設定を確認してください。"
            : provider?.availability?.error || "利用可能なモデルがありません。モデル一覧を更新してください。",
        );
        return;
      }

      applyDesiredSettings((current) => {
        const previousEffort = current.main_route?.effort;
        const nextEffort = resolveEffortForModel(
          runtime.llmCatalog,
          nextProvider,
          nextModel,
          previousEffort,
        );
        const mainRoute: NonNullable<SessionLlmSettings["main_route"]> = {
          provider: nextProvider,
          model: nextModel,
        };
        if (nextEffort) {
          mainRoute.effort = nextEffort;
        }
        return {
          agent_team_selection: current.agent_team_selection,
          main_route: mainRoute,
          special_routing: {},
          execution_profile_id: current.execution_profile_id ?? "",
        };
      });
    },
    [applyDesiredSettings, catalogProviders, runtime.llmCatalog, runtime.llmEngines],
  );

  const updateModel = useCallback(
    (nextModel: string) => {
      applyDesiredSettings((current) => {
        const { provider } = resolveUnderlyingRoute({
          sessionId,
          mainRoute: current.main_route,
          newChatEffectiveMain,
          sessionEffectiveMain,
          runtimeProvider: runtime.currentLlm?.provider,
          runtimeModel: runtime.currentLlm?.model,
        });
        if (!provider || !nextModel) return current;

        const providerEntry = catalogProviders.find(
          (item) =>
            normalizedProviderId(item.id) === normalizedProviderId(provider),
        );
        const availableModels = servedModelsForProvider(
          providerEntry,
          runtime.llmEngines,
        );
        if (!providerEntry || !availableModels.some((item) => item.id === nextModel)) {
          return current;
        }

        const nextEffort = resolveEffortForModel(
          runtime.llmCatalog,
          provider,
          nextModel,
          current.main_route?.effort,
        );
        const mainRoute: NonNullable<SessionLlmSettings["main_route"]> = {
          provider,
          model: nextModel,
        };
        if (nextEffort) {
          mainRoute.effort = nextEffort;
        }
        return {
          agent_team_selection: current.agent_team_selection,
          main_route: mainRoute,
          special_routing: {},
          execution_profile_id: current.execution_profile_id ?? "",
        };
      });
    },
    [
      applyDesiredSettings,
      newChatEffectiveMain,
      runtime.currentLlm?.model,
      runtime.currentLlm?.provider,
      runtime.llmCatalog,
      runtime.llmEngines,
      catalogProviders,
      sessionEffectiveMain,
      sessionId,
    ],
  );

  const updateEffort = useCallback(
    (nextEffort: string) => {
      const normalizedEffort = nextEffort.trim();
      if (!normalizedEffort) return;

      applyDesiredSettings((current) => {
        if (isFreeTeamActive(current)) return current;
        const { provider, model } = resolveUnderlyingRoute({
          sessionId,
          mainRoute: current.main_route,
          newChatEffectiveMain,
          sessionEffectiveMain,
          runtimeProvider: runtime.currentLlm?.provider,
          runtimeModel: runtime.currentLlm?.model,
        });
        if (!provider || !model) return current;

        return {
          agent_team_selection: current.agent_team_selection,
          main_route: {
            provider,
            model,
            effort: normalizedEffort,
          },
          special_routing: current.special_routing,
          execution_profile_id: current.execution_profile_id ?? "",
        };
      });
    },
    [
      applyDesiredSettings,
      newChatEffectiveMain,
      runtime.currentLlm?.model,
      runtime.currentLlm?.provider,
      sessionEffectiveMain,
      sessionId,
    ],
  );

  const updateAgentTeamSelection = useCallback(
    (selectorValue: string) => {
      if (!userId && !sessionId) return;

      applyDesiredSettings((current) => {
        if (selectorValue === AGENT_TEAM_SELECTOR_AUTO) {
          return {
            agent_team_selection: {
              mode: "auto",
              team_id: "",
              loaded_team_ids: current.agent_team_selection.loaded_team_ids,
            },
            main_route: current.main_route ? { ...current.main_route } : {},
            special_routing: {},
            execution_profile_id: "",
          };
        }

        if (selectorValue === AGENT_TEAM_SELECTOR_FREE_TEAM) {
          return {
            agent_team_selection: {
              mode: "auto",
              team_id: "",
              loaded_team_ids: [],
            },
            main_route: current.main_route ? { ...current.main_route } : {},
            special_routing: {
              routing_profile_id: FREE_TEAM_ROUTING_PROFILE_ID,
            },
            execution_profile_id: "",
          };
        }

        return {
          agent_team_selection: {
            mode: "fixed",
            team_id: selectorValue,
            loaded_team_ids: [],
          },
          main_route: current.main_route ? { ...current.main_route } : {},
          special_routing: {},
          execution_profile_id: resolveExecutionProfileForTeamChange(
            selectorValue,
            current.execution_profile_id,
            agentTeamOptions,
          ),
        };
      });
    },
    [agentTeamOptions, applyDesiredSettings, sessionId, userId],
  );

  const updateExecutionProfile = useCallback(
    (profileId: string) => {
      if (!userId && !sessionId) return;
      applyDesiredSettings((current) => ({
        agent_team_selection: current.agent_team_selection,
        main_route: current.main_route ? { ...current.main_route } : {},
        special_routing: current.special_routing
          ? { ...current.special_routing }
          : {},
        execution_profile_id: normalizeExecutionProfileId(profileId),
      }));
    },
    [applyDesiredSettings, sessionId, userId],
  );

  const flushPendingSave = useCallback(async () => {
    if (!sessionId) return;
    await awaitSessionLlmSettingsReady(sessionId);
  }, [sessionId]);

  const settingsLoading = routeLoading || teamsLoading;
  const summaryLabel = freeTeamActive
    ? "Free Team"
    : underlyingProvider && underlyingModel
      ? formatRouteLabel(underlyingProvider, underlyingModel)
      : "";

  const executionProfileOptions = useMemo(() => {
    if (
      agentTeamSelectionValue === AGENT_TEAM_SELECTOR_AUTO ||
      agentTeamSelectionValue === AGENT_TEAM_SELECTOR_FREE_TEAM
    ) {
      return [] as AgentTeamExecutionProfileOption[];
    }
    const current = normalizeExecutionProfileId(desiredSettings.execution_profile_id);
    const profiles = executionProfilesForTeam(agentTeamSelectionValue, agentTeamOptions);
    const enabled = profiles.filter((profile) => profile.enabled !== false);
    if (current && !enabled.some((profile) => profile.profile_id === current)) {
      const selected = profiles.find((profile) => profile.profile_id === current);
      if (selected) return [...enabled, selected];
    }
    return enabled;
  }, [agentTeamOptions, agentTeamSelectionValue, desiredSettings.execution_profile_id]);

  return {
    catalogProviders,
    effectiveProvider,
    effectiveModel,
    effectiveEffort,
    effortOptions,
    modelOptions,
    modelDiscoveryError,
    hasSessionScopedRoute,
    sessionEffectiveMain,
    newChatEffectiveMain,
    generationReadyRoute,
    isLoading: settingsLoading,
    routeLoading: settingsLoading,
    settingsLoading,
    teamsLoading,
    agentTeamOptions,
    agentTeamSelectorValue: agentTeamSelectionValue,
    agentTeamSelectionValue,
    agentTeamDisabled: settingsLoading,
    executionProfileId: normalizeExecutionProfileId(desiredSettings.execution_profile_id),
    executionProfileOptions,
    executionProfileDisabled: settingsLoading,
    updateExecutionProfile,
    providerDisabled: settingsLoading,
    // A provider can remain selected while its current model is stale or the
    // catalog is temporarily unavailable.  Keep the model control interactive
    // whenever at least one served/LKG alternative is advertised; an explicit
    // empty/error projection with no alternatives is the only empty lockout.
    modelDisabled: settingsLoading || !effectiveProvider || modelOptions.length === 0,
    effortDisabled: settingsLoading || effortOptions.length === 0 || freeTeamActive,
    freeTeamActive,
    summaryLabel,
    desiredSettings,
    sessionSettings: sessionId ? desiredSettings : null,
    pendingSettings,
    updateProvider,
    updateModel,
    updateEffort,
    updateAgentTeamValue: updateAgentTeamSelection,
    updateAgentTeamSelection,
    flushPendingSave,
    persistSettings: persistDesiredSettings,
  };
}

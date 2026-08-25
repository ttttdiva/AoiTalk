"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import type { RuntimeContextValue } from "@/contexts/runtime-context";
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

  const catalogProviders = useMemo(() => {
    const providers = runtime.llmCatalog?.providers ?? [];
    const available = filterAvailableProviders(
      providers,
      runtime.llmDeployment,
      (item) => item.id,
      (item) => item,
    );
    return filterVisibleProviders(
      available,
      userSettings,
      [runtime.currentLlm?.provider],
      (item) => item.id,
    ).filter((item) => item.available !== false && item.disabled !== true);
  }, [runtime.currentLlm?.provider, runtime.llmCatalog?.providers, runtime.llmDeployment, userSettings]);

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
  const underlyingProvider = displayedRoute.provider;
  const underlyingModel = displayedRoute.model;

  const effectiveProvider = underlyingProvider;
  const effectiveModel = underlyingModel;

  const effectiveEffort = resolveDisplayedEffort({
    ...routeContext,
    freeTeamActive,
  });

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
    if (fromCatalog.length > 0) return fromCatalog;
    const fromEngine = runtime.llmEngines.find(
      (engine) =>
        engine.provider === effectiveProvider && engine.model === effectiveModel,
    );
    return fromEngine?.reasoning_effort_options ?? [];
  }, [effectiveModel, effectiveProvider, freeTeamActive, runtime.llmCatalog, runtime.llmEngines]);

  const selectedProvider =
    catalogProviders.find((item) => item.id === effectiveProvider) ??
    catalogProviders[0] ??
    null;
  const providerModels = selectedProvider?.models ?? [];
  const modelOptions =
    effectiveModel &&
    !providerModels.some((item) => item.id === effectiveModel) &&
    effectiveProvider
      ? [{ id: effectiveModel, label: effectiveModel }, ...providerModels]
      : providerModels;

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
      const provider = catalogProviders.find((item) => item.id === nextProvider);
      const firstModel = provider?.models?.[0]?.id ?? "";
      const configuredModel = provider?.configured_model?.trim();
      const nextModel =
        configuredModel &&
        provider?.models?.some((item) => item.id === configuredModel)
          ? configuredModel
          : firstModel;
      if (!nextProvider || !nextModel) return;

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
    [applyDesiredSettings, catalogProviders, runtime.llmCatalog],
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
    hasSessionScopedRoute,
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
    modelDisabled: settingsLoading || !effectiveProvider,
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

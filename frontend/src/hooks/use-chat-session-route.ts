"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { useDotsDialogueAvailability } from "@/hooks/use-dots-dialogue-chat";
import type { RuntimeContextValue } from "@/contexts/runtime-context";
import type { LlmCatalogModelOption, LlmCatalogProvider } from "@/lib/chat-api";
import {
  DOTS_DIALOGUE_PROVIDER_ID,
  DOTS_DIALOGUE_ROUTE_MODEL_ID,
  beginDotsDialogueRouteReadiness,
  markDotsDialogueRoutePending,
  rememberDotsDialogueRoute,
  type DotsDialogueRouteReadinessHandle,
} from "@/lib/dots-dialogue-chat-route";
import {
  fetchNewChatLlmDefaultsAfterLastUsedFlush,
  fetchSessionLlmSettings,
  recordLastUsedLlmRoute,
  saveSessionLlmSettings,
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
  normalizeAlwaysThinkingRoute,
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

/**
 * A new Chat may start before the defaults endpoint has produced its envelope.
 * A strict local route can still be authoritative when the runtime has both a
 * ready status and a served/catalog model proof. Keep this proof narrow so a
 * stale display-only runtime value cannot bypass the normal handoff guard.
 */
function getReadyRuntimeRouteProof(
  runtime: RuntimeContextValue,
): { provider: string; model: string } | null {
  if (runtime.llmStatus !== "ready") return null;
  const route = runtime.effectiveLlm ?? runtime.currentLlm;
  const provider = normalizedProviderId(route?.provider);
  const model = route?.model?.trim() ?? "";
  if (!provider || !model || !isStrictRuntimeProvider(provider)) return null;

  if (
    runtime.llmEngines.some(
      (engine) =>
        normalizedProviderId(engine.provider) === provider &&
        engine.model.trim() === model &&
        engine.available !== false &&
        engine.disabled !== true &&
        engine.unavailable !== true,
    )
  ) {
    return { provider, model };
  }

  const catalogProvider = runtime.llmCatalog?.providers?.find(
    (entry) => normalizedProviderId(entry.id) === provider,
  );
  if (!catalogProvider || catalogProvider.availability?.state === "error") {
    return null;
  }
  const advertisedModels = Array.isArray(catalogProvider.chat_models)
    ? catalogProvider.chat_models
    : catalogProvider.available_models;
  return advertisedModels?.some((entry) => entry.id === model)
    ? { provider, model }
    : null;
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

let displayedNewChatMainRoute: SessionMainRoute | null = null;
let generationReadyNewChatMainRoute: SessionMainRoute | null = null;
let newChatRouteSnapshotScopeKey: string | null = null;
let newChatRouteSnapshotOwner: symbol | null = null;

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
export function getDisplayedNewChatMainRoute(scopeKey?: string): SessionMainRoute | null {
  if (scopeKey !== undefined && newChatRouteSnapshotScopeKey !== scopeKey) {
    return null;
  }
  return displayedNewChatMainRoute;
}

/**
 * 新規チャットの generation に渡してよい authoritative route。
 * runtime current の provisional 表示はここへ昇格させない。
 */
export function getGenerationReadyNewChatMainRoute(scopeKey?: string): SessionMainRoute | null {
  if (scopeKey !== undefined && newChatRouteSnapshotScopeKey !== scopeKey) {
    return null;
  }
  return generationReadyNewChatMainRoute;
}

export function resetDisplayedNewChatMainRoute(): void {
  displayedNewChatMainRoute = null;
  generationReadyNewChatMainRoute = null;
  newChatRouteSnapshotScopeKey = null;
  newChatRouteSnapshotOwner = null;
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
  scopeKey?: string,
): void {
  // Route objects are derived during render; primitive fields intentionally
  // form the dependency key so a fresh object does not re-run this snapshot
  // effect on every render.
  const routeProvider = route.provider;
  const routeModel = route.model;
  const generationProvider = generationReadyRoute?.provider ?? "";
  const generationModel = generationReadyRoute?.model ?? "";
  const generationEffort = generationReadyRoute?.effort ?? "";
  const snapshotScopeKey = scopeKey ?? "__legacy__";
  const snapshotOwnerRef = useRef<symbol | null>(null);
  if (snapshotOwnerRef.current === null) {
    snapshotOwnerRef.current = Symbol("new-chat-route-snapshot");
  }
  const snapshotOwner = snapshotOwnerRef.current;
  useLayoutEffect(() => {
    if (sessionId) {
      // Clear a committed new-chat snapshot as soon as this route scope moves
      // to a session. Without this fence, a late old draft snapshot can be
      // observed by another consumer during the session transition.
      if (
        newChatRouteSnapshotScopeKey !== null &&
        newChatRouteSnapshotScopeKey !== snapshotScopeKey &&
        newChatRouteSnapshotOwner === snapshotOwner
      ) {
        resetDisplayedNewChatMainRoute();
      }
      return;
    }
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
    newChatRouteSnapshotScopeKey = snapshotScopeKey;
    newChatRouteSnapshotOwner = snapshotOwner;
    return () => {
      if (
        newChatRouteSnapshotScopeKey === snapshotScopeKey &&
        newChatRouteSnapshotOwner === snapshotOwner
      ) {
        resetDisplayedNewChatMainRoute();
      }
    };
  }, [
    sessionId,
    routeProvider,
    routeModel,
    effort,
    generationProvider,
    generationModel,
    generationEffort,
    snapshotScopeKey,
    snapshotOwner,
  ]);
}

const defaultSessionSettings = (): SessionLlmSettings => ({
  main_route: {},
});

function cloneSettings(settings: SessionLlmSettings): SessionLlmSettings {
  return {
    main_route: settings.main_route ? { ...settings.main_route } : {},
    ...(settings.legacy ? { legacy: { ...settings.legacy } } : {}),
  };
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
  const dotsDialogueAvailability = useDotsDialogueAvailability(sessionId, userId);
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
  const [resolvedRouteScopeKey, setResolvedRouteScopeKey] = useState<string | null>(null);
  const settingsRevisionRef = useRef(0);
  const desiredSettingsRef = useRef(desiredSettings);
  const routeReadinessRef = useRef<DotsDialogueRouteReadinessHandle | null>(null);

  useLayoutEffect(() => {
    const readiness = beginDotsDialogueRouteReadiness(userId, sessionId);
    routeReadinessRef.current = readiness;
    return () => {
      if (routeReadinessRef.current === readiness) {
        routeReadinessRef.current = null;
      }
      readiness?.cancel();
    };
  }, [routeScopeKey, sessionId, userId]);

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
  const scopedSessionEffectiveMain =
    sessionId && resolvedRouteScopeKey === routeScopeKey
      ? sessionEffectiveMain
      : null;
  const scopedNewChatEffectiveMain =
    !sessionId && resolvedRouteScopeKey === routeScopeKey
      ? newChatEffectiveMain
      : null;

  useEffect(() => {
    hydratePendingNewChatLlmSettings(userId);
  }, [userId]);

  useEffect(() => {
    if (sessionId) return;
    setDesiredSettings(cloneSettings(getPendingNewChatLlmSettings(userId)));
  }, [sessionId, userId]);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      setRouteLoading(true);
      const loadSettingsRevision = settingsRevisionRef.current;
      const readiness = routeReadinessRef.current;
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
            // A selector PUT started after this GET must remain authoritative;
            // never hydrate a stale route over its pending selection fence.
            if (settingsRevisionRef.current !== loadSettingsRevision) {
              readiness?.settleHydration("cancelled");
              return;
            }
            const persistedMainRoute = sessionEnvelope.settings.main_route;
            const persistedDotsRoute =
              normalizedProviderId(persistedMainRoute?.provider) ===
                DOTS_DIALOGUE_PROVIDER_ID &&
              normalizedProviderId(persistedMainRoute?.model) ===
                DOTS_DIALOGUE_ROUTE_MODEL_ID;
            markDotsDialogueRoutePending(userId, sessionId, false);
            rememberDotsDialogueRoute(userId, sessionId, persistedDotsRoute);
            setDesiredSettings(cloneSettings(sessionEnvelope.settings));
            setSessionEffectiveMain(sessionEnvelope.effective_main ?? null);
            setNewChatEffectiveMain(null);
            setResolvedRouteScopeKey(routeScopeKey);
            readiness?.settleHydration("ready");
          }
        } else {
          const defaultsEnvelope = await fetchNewChatLlmDefaultsAfterLastUsedFlush(userId);
          if (cancelled) return;

          const pending = getPendingNewChatLlmSettings(userId);
          const pendingDotsRoute =
            normalizedProviderId(pending.main_route?.provider) ===
              DOTS_DIALOGUE_PROVIDER_ID &&
            normalizedProviderId(pending.main_route?.model) ===
              DOTS_DIALOGUE_ROUTE_MODEL_ID;
          const inheritedDotsRoute =
            normalizedProviderId(defaultsEnvelope.effective_main?.provider) ===
              DOTS_DIALOGUE_PROVIDER_ID;
          setDesiredSettings(cloneSettings(pending));
          setSessionEffectiveMain(null);
          // An inherited GPT dots effective route is not an explicit draft
          // selection. Keep the selector unselected until this owner chooses
          // the dedicated provider in the current new-chat draft.
          setNewChatEffectiveMain(
            inheritedDotsRoute && !pendingDotsRoute
              ? null
              : defaultsEnvelope.effective_main ?? null,
          );
          setResolvedRouteScopeKey(routeScopeKey);
        }
      } catch {
        if (
          !cancelled &&
          settingsRevisionRef.current === loadSettingsRevision
        ) {
          setDesiredSettings(
            sessionId
              ? defaultSessionSettings()
              : cloneSettings(getPendingNewChatLlmSettings(userId)),
          );
          setSessionEffectiveMain(null);
          setNewChatEffectiveMain(null);
          setResolvedRouteScopeKey(routeScopeKey);
          readiness?.settleHydration("failed");
        } else if (settingsRevisionRef.current !== loadSettingsRevision) {
          // A newer selector save owns the route now. Do not let a failed
          // stale hydrate replace its settings or leave the send fence stuck.
          readiness?.settleHydration("cancelled");
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

  const runtimeRouteProof = !sessionId
    ? getReadyRuntimeRouteProof(runtime)
    : null;
  const routeContext = {
    sessionId,
    mainRoute: routeSettings.main_route,
    newChatEffectiveMain: scopedNewChatEffectiveMain,
    sessionEffectiveMain: scopedSessionEffectiveMain,
    runtimeProvider:
      runtimeRouteProof?.provider ?? runtime.currentLlm?.provider,
    runtimeModel: runtimeRouteProof?.model ?? runtime.currentLlm?.model,
    runtimeRouteReady: runtimeRouteProof !== null,
  };
  const displayedRoute = resolveUnderlyingRoute(routeContext);
  const rawGenerationReadyRoute = resolveGenerationReadyRoute(routeContext);
  const generationReadyRoute = normalizeAlwaysThinkingRoute(
    runtime.llmCatalog,
    rawGenerationReadyRoute,
  );
  const authoritativeRouteProvider = generationReadyRoute?.provider ?? "";
  const underlyingProvider = displayedRoute.provider;
  const underlyingModel = displayedRoute.model;

  const effectiveProvider = underlyingProvider;
  const effectiveModel = underlyingModel;

  const effectiveEffort = normalizeAlwaysThinkingRoute(
    runtime.llmCatalog,
    {
      ...displayedRoute,
      effort: resolveDisplayedEffort({
        ...routeContext,
      }),
    },
  )?.effort ?? "";

  const catalogProviders = useMemo(() => {
    const persistedProviders = runtime.llmCatalog?.providers ?? [];
    const chatStatusProviders = runtime.llmCatalog?.chat_providers ?? [];
    const chatStatusProviderIds = new Set(
      chatStatusProviders
        .filter((provider) => provider.chat_status?.selectable === false)
        .map((provider) => normalizedProviderId(provider.id))
        .filter((providerId): providerId is string => Boolean(providerId)),
    );
    const dotsStatusProvider = chatStatusProviders.find(
      (provider) => normalizedProviderId(provider.id) === "gpt-dots",
    );
    const dotsSessionSelectable = Boolean(
      sessionId &&
        dotsDialogueAvailability.selectable &&
        dotsDialogueAvailability.conversationId === sessionId,
    );
    const dotsDraftSelectable = Boolean(
      !sessionId &&
        userId &&
        dotsDialogueAvailability.selectable &&
        dotsDialogueAvailability.state === "connection_ready",
    );
    const dotsSelectable = dotsSessionSelectable || dotsDraftSelectable;
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
      if (
        knownProviders.has(providerId) ||
        chatStatusProviderIds.has(providerId)
      ) {
        continue;
      }
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

    if (dotsSelectable) {
      const dotsProvider =
        dotsStatusProvider ??
        ({
          id: DOTS_DIALOGUE_PROVIDER_ID,
          label: "GPT dots",
          models: [],
          chat_models: [],
        } satisfies LlmCatalogProvider);
      const connectedDotsProvider: LlmCatalogProvider = {
        ...dotsProvider,
        available: true,
        disabled: false,
        unavailable: false,
        availability: { state: "available", error: null, message: null },
        models: [],
        chat_models: [{ id: DOTS_DIALOGUE_ROUTE_MODEL_ID, label: "GPT dots" }],
        available_models: [{ id: DOTS_DIALOGUE_ROUTE_MODEL_ID, label: "GPT dots" }],
        chat_status: {
          ...dotsProvider.chat_status,
          state: "available",
          label: "専用接続",
          selectable: true,
          message: dotsDialogueAvailability.message,
          latest_request_id: dotsDialogueAvailability.latestRequestId ?? undefined,
          latest_request_state: dotsDialogueAvailability.latestRequestState ?? undefined,
        },
      };
      const dotsIndex = providers.findIndex(
        (provider) => normalizedProviderId(provider.id) === "gpt-dots",
      );
      if (dotsIndex >= 0) providers[dotsIndex] = connectedDotsProvider;
      else providers.push(connectedDotsProvider);
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
      runtimeRoute &&
      runtimeProviderId !== null &&
      !chatStatusProviderIds.has(runtimeProviderId)
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
      !chatStatusProviderIds.has(
        normalizedProviderId(authoritativeRouteProvider) ?? "",
      ) &&
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

    const visibleCandidates = filterVisibleProviders(
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
    const unavailableChatStatuses = chatStatusProviders
      .filter((provider) => normalizedProviderId(provider.id) !== "gpt-dots" || !dotsSelectable)
      .map((provider) =>
        normalizedProviderId(provider.id) === "gpt-dots"
          ? {
              ...provider,
              chat_status: {
                ...provider.chat_status,
                state: dotsDialogueAvailability.state,
                label: dotsDialogueAvailability.selectable ? "専用接続" : "利用不可",
                selectable: false,
                message: dotsDialogueAvailability.message,
                ...(normalizedProviderId(provider.id) === "gpt-dots"
                  ? {
                      latest_request_id: dotsDialogueAvailability.latestRequestId ?? undefined,
                      latest_request_state: dotsDialogueAvailability.latestRequestState ?? undefined,
                    }
                  : {}),
              },
            }
          : provider,
      );
    const visibleChatStatuses = filterVisibleProviders(
      unavailableChatStatuses.filter(
        (provider) => provider.chat_status?.selectable === false,
      ),
      runtime.llmCatalog?.provider_visibility,
      [],
      (item) => item.id,
    );
    return [...visibleCandidates, ...visibleChatStatuses];
  }, [
    authoritativeRouteProvider,
    runtime.llmEngines,
    runtime.llmCatalog?.providers,
    runtime.llmCatalog?.chat_providers,
    runtime.llmCatalog?.provider_visibility,
    runtime.llmDeployment,
    runtime.currentLlm,
    runtime.effectiveLlm,
    dotsDialogueAvailability,
    sessionId,
    userId,
  ]);

  useSyncDisplayedNewChatMainRoute(
    sessionId,
    displayedRoute,
    effectiveEffort,
    generationReadyRoute,
    routeScopeKey,
  );

  const effortOptions = useMemo(() => {
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
      Boolean(scopedSessionEffectiveMain?.provider && scopedSessionEffectiveMain?.model)
    : hasExplicitSessionRoute(routeSettings.main_route);

  const persistDesiredSettings = useCallback(
    async (nextSettings: SessionLlmSettings): Promise<SessionLlmSettingsResponse | null> => {
      const snapshot = cloneSettings(nextSettings);
      const revision = settingsRevisionRef.current + 1;
      settingsRevisionRef.current = revision;

      if (sessionId) {
        const readiness = routeReadinessRef.current;
        const mutationToken = readiness?.beginMutation() ?? null;
        try {
          const saved = await enqueueSessionLlmSettingsSave(sessionId, () =>
            saveSessionLlmSettings(sessionId, snapshot),
          );
          readiness?.settleMutation(mutationToken, "ready");
          if (settingsRevisionRef.current === revision) {
            setDesiredSettings(cloneSettings(saved.settings));
            setSessionEffectiveMain(saved.effective_main ?? null);
            setResolvedRouteScopeKey(routeScopeKey);
          }
          return saved;
        } catch (error) {
          readiness?.settleMutation(mutationToken, "failed");
          throw error;
        }
      }

      if (!userId) return null;
      setPendingNewChatLlmSettings(snapshot, userId);
      if (settingsRevisionRef.current === revision) {
        setDesiredSettings(snapshot);
      }
      // GPT dots is an owner-scoped pending draft route. Selecting it must
      // not rewrite the account's inherited last-used route before a real
      // Chat UUID exists; the handoff is persisted and verified after create.
      if (
        hasExplicitSessionRoute(snapshot.main_route) &&
        normalizedProviderId(snapshot.main_route?.provider) !==
          DOTS_DIALOGUE_PROVIDER_ID
      ) {
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
      const providerId = normalizedProviderId(nextProvider);
      if (
        providerId === "gpt-dots" &&
        (!userId ||
          (sessionId
            ? !dotsDialogueAvailability.selectable ||
              dotsDialogueAvailability.conversationId !== sessionId
            : !dotsDialogueAvailability.selectable ||
              dotsDialogueAvailability.state !== "connection_ready"))
      ) {
        toast.error(
          sessionId
            ? "GPT dotsは承認済みの既存Chatで、有効なホスト購読がある場合だけ選択できます。"
            : "GPT dotsは認証済みの新規Chat下書きで選択できます。",
        );
        return;
      }
      const provider = catalogProviders.find(
        (item) =>
          normalizedProviderId(item.id) === normalizedProviderId(nextProvider),
      );
      if (provider?.chat_status?.selectable === false) return;
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

      const buildNextSettings = (current: SessionLlmSettings): SessionLlmSettings => {
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
        return { main_route: mainRoute };
      };

      if (providerId === "gpt-dots") {
        if (!userId) return;
        if (!sessionId) {
          if (
            !dotsDialogueAvailability.selectable ||
            dotsDialogueAvailability.state !== "connection_ready"
          ) {
            toast.error("GPT dots接続を確認できないため、新規Chatでは選択できません。");
            return;
          }
          applyDesiredSettings((current) =>
            buildNextSettings(cloneSettings(current)),
          );
          const pending = getPendingNewChatLlmSettings(userId);
          if (
            normalizedProviderId(pending.main_route?.provider) !==
              DOTS_DIALOGUE_PROVIDER_ID ||
            normalizedProviderId(pending.main_route?.model) !==
              DOTS_DIALOGUE_ROUTE_MODEL_ID
          ) {
            toast.error("GPT dotsの新規Chat選択を保存できませんでした。");
            return;
          }
          return;
        }
        return (async () => {
          const requestedRevision = settingsRevisionRef.current + 1;
          // The backend gates DOTS sends on the persisted session route. Keep
          // this route fenced while its PUT is in flight so a fast composer
          // click cannot use the previous persisted route by accident.
          markDotsDialogueRoutePending(
            userId,
            sessionId,
            true,
            requestedRevision,
          );
          rememberDotsDialogueRoute(userId, sessionId, false);
          try {
            const saved = await persistDesiredSettings(
              buildNextSettings(cloneSettings(desiredSettingsRef.current)),
            );
            if (settingsRevisionRef.current !== requestedRevision) {
              // A later non-DOTS settings save may supersede this PUT. Clear
              // only this attempt's fence; a newer DOTS attempt owns its own
              // revision and remains pending.
              markDotsDialogueRoutePending(
                userId,
                sessionId,
                false,
                requestedRevision,
              );
              return;
            }
            const persistedProvider = normalizedProviderId(
              saved?.settings.main_route?.provider,
            );
            const persistedModel = normalizedProviderId(
              saved?.settings.main_route?.model,
            );
            if (
              persistedProvider !== "gpt-dots" ||
              persistedModel !== DOTS_DIALOGUE_ROUTE_MODEL_ID
            ) {
              markDotsDialogueRoutePending(
                userId,
                sessionId,
                false,
                requestedRevision,
              );
              rememberDotsDialogueRoute(userId, sessionId, false);
              toast.error("GPT dotsのProvider設定を保存できなかったため、選択を反映しませんでした。");
              return;
            }
            markDotsDialogueRoutePending(
              userId,
              sessionId,
              false,
              requestedRevision,
            );
            rememberDotsDialogueRoute(userId, sessionId, true);
          } catch {
            if (settingsRevisionRef.current !== requestedRevision) {
              markDotsDialogueRoutePending(
                userId,
                sessionId,
                false,
                requestedRevision,
              );
              return;
            }
            markDotsDialogueRoutePending(
              userId,
              sessionId,
              false,
              requestedRevision,
            );
            rememberDotsDialogueRoute(userId, sessionId, false);
            toast.error("GPT dotsのProvider設定を保存できなかったため、選択を反映しませんでした。");
          }
        })();
      }

      if (sessionId) {
        markDotsDialogueRoutePending(userId, sessionId, false);
        rememberDotsDialogueRoute(userId, sessionId, false);
      }
      applyDesiredSettings((current) => buildNextSettings(current));
    },
    [
      applyDesiredSettings,
      catalogProviders,
      dotsDialogueAvailability,
      desiredSettingsRef,
      persistDesiredSettings,
      runtime.llmCatalog,
      runtime.llmEngines,
      sessionId,
      userId,
    ],
  );

  const updateModel = useCallback(
    (nextModel: string) => {
      applyDesiredSettings((current) => {
        const { provider } = resolveUnderlyingRoute({
          sessionId,
          mainRoute: current.main_route,
          newChatEffectiveMain: scopedNewChatEffectiveMain,
          sessionEffectiveMain: scopedSessionEffectiveMain,
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
        return { main_route: mainRoute };
      });
    },
    [
      applyDesiredSettings,
      scopedNewChatEffectiveMain,
      runtime.currentLlm?.model,
      runtime.currentLlm?.provider,
      runtime.llmCatalog,
      runtime.llmEngines,
      catalogProviders,
      scopedSessionEffectiveMain,
      sessionId,
    ],
  );

  const updateEffort = useCallback(
    (nextEffort: string) => {
      const normalizedEffort = nextEffort.trim();
      if (!normalizedEffort) return;

      applyDesiredSettings((current) => {
        const { provider, model } = resolveUnderlyingRoute({
          sessionId,
          mainRoute: current.main_route,
          newChatEffectiveMain: scopedNewChatEffectiveMain,
          sessionEffectiveMain: scopedSessionEffectiveMain,
          runtimeProvider: runtime.currentLlm?.provider,
          runtimeModel: runtime.currentLlm?.model,
        });
        if (!provider || !model) return current;

        return {
          main_route: { provider, model, effort: normalizedEffort },
        };
      });
    },
    [
      applyDesiredSettings,
      scopedNewChatEffectiveMain,
      runtime.currentLlm?.model,
      runtime.currentLlm?.provider,
      scopedSessionEffectiveMain,
      sessionId,
    ],
  );

  const flushPendingSave = useCallback(async () => {
    if (!sessionId) return;
    await awaitSessionLlmSettingsReady(sessionId);
  }, [sessionId]);

  const settingsLoading = routeLoading;
  const summaryLabel = underlyingProvider && underlyingModel
    ? formatRouteLabel(underlyingProvider, underlyingModel)
    : "";

  return {
    catalogProviders,
    effectiveProvider,
    effectiveModel,
    effectiveEffort,
    effortOptions,
    modelOptions,
    modelDiscoveryError,
    hasSessionScopedRoute,
    sessionEffectiveMain: scopedSessionEffectiveMain,
    newChatEffectiveMain: scopedNewChatEffectiveMain,
    generationReadyRoute,
    isLoading: settingsLoading,
    routeLoading: settingsLoading,
    settingsLoading,
    providerDisabled: settingsLoading,
    // A provider can remain selected while its current model is stale or the
    // catalog is temporarily unavailable.  Keep the model control interactive
    // whenever at least one served/LKG alternative is advertised; an explicit
    // empty/error projection with no alternatives is the only empty lockout.
    modelDisabled: settingsLoading || !effectiveProvider || modelOptions.length === 0,
    effortDisabled: settingsLoading || effortOptions.length === 0,
    summaryLabel,
    desiredSettings,
    sessionSettings: sessionId ? desiredSettings : null,
    pendingSettings,
    updateProvider,
    updateModel,
    updateEffort,
    flushPendingSave,
    persistSettings: persistDesiredSettings,
  };
}

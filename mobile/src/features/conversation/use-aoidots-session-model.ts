import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatResponseModelOption, LlmModelCatalogResponse } from "../../types/api";
import { getCredentialIdentityRevision } from "../../lib/api-client";
import {
  getSessionLlmSettings,
  normalizeSessionMainRoute,
  putSessionMainRoute,
  type SessionLlmSettingsEnvelope,
  type SessionMainRoute,
} from "../../lib/session-llm-settings-api";
import { getCachedLlmModelCatalog, refreshLlmModelCatalog } from "./llm-meta-cache";
import { buildResponseModelOptions } from "./response-model-options";

type ModelState = {
  mainRoute: SessionMainRoute | null;
  effectiveRoute: SessionMainRoute | null;
  options: ChatResponseModelOption[];
  settingsReady: boolean;
  settingsLoading: boolean;
  catalogLoading: boolean;
  saving: boolean;
  error: string | null;
};

const EMPTY_STATE: ModelState = {
  mainRoute: null,
  effectiveRoute: null,
  options: [],
  settingsReady: false,
  settingsLoading: false,
  catalogLoading: false,
  saving: false,
  error: null,
};

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function hasCatalogShape(value: unknown): value is LlmModelCatalogResponse {
  const catalog = record(value);
  const current = record(catalog?.current);
  return Boolean(
    catalog && Array.isArray(catalog.providers) &&
    typeof current?.provider === "string" &&
    typeof current.model === "string",
  );
}

function readEnvelope(value: SessionLlmSettingsEnvelope): {
  mainRoute: SessionMainRoute | null;
  effectiveRoute: SessionMainRoute | null;
} {
  const savedSettings = record(value.saved_settings) ?? record(value.settings);
  if (!savedSettings) throw new Error("セッションのモデル設定を読み取れませんでした。");
  return {
    mainRoute: normalizeSessionMainRoute(savedSettings.main_route),
    effectiveRoute: normalizeSessionMainRoute(value.effective_main),
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : "モデル設定を取得できませんでした。再取得してください。";
}

function sameModel(left: SessionMainRoute | null, right: SessionMainRoute): boolean {
  return Boolean(
    left &&
    left.provider.trim().toLowerCase() === right.provider.trim().toLowerCase() &&
    left.model.trim() === right.model.trim(),
  );
}

function sameRoute(left: SessionMainRoute | null, right: SessionMainRoute): boolean {
  if (!sameModel(left, right)) return false;
  return (left?.effort?.trim().toLowerCase() || "") === (right.effort?.trim().toLowerCase() || "");
}

function routeForOption(
  option: ChatResponseModelOption,
  currentRoute: SessionMainRoute | null,
  effectiveRoute: SessionMainRoute | null,
): SessionMainRoute {
  const route: SessionMainRoute = {
    provider: option.provider.trim(),
    model: option.model.trim(),
  };
  const efforts = (option.reasoningEffortOptions ?? [])
    .map((value) => value.trim())
    .filter(Boolean);
  const previousEffort = sameModel(currentRoute, route)
    ? currentRoute?.effort
    : sameModel(effectiveRoute, route)
      ? effectiveRoute?.effort
      : undefined;
  const effort = previousEffort && efforts.includes(previousEffort)
    ? previousEffort
    : option.reasoningEffortDefault && efforts.includes(option.reasoningEffortDefault)
      ? option.reasoningEffortDefault
      : undefined;
  return effort ? { ...route, effort } : route;
}

function optionsForRoute(
  catalog: LlmModelCatalogResponse,
  mainRoute: SessionMainRoute | null,
  effectiveRoute: SessionMainRoute | null,
): ChatResponseModelOption[] {
  const current = mainRoute ?? effectiveRoute;
  return buildResponseModelOptions(catalog, current
    ? { provider: current.provider, model: current.model }
    : null);
}

function isPersistedRoute(
  value: SessionLlmSettingsEnvelope,
  wanted: SessionMainRoute,
): boolean {
  const readback = readEnvelope(value);
  return sameRoute(readback.mainRoute, wanted);
}

export function useAoiDotsSessionModel({
  sessionId,
  enabled,
  blocked,
  authScopeHint,
}: {
  sessionId: string;
  enabled: boolean;
  blocked: boolean;
  authScopeHint?: string | null;
}) {
  const [state, setState] = useState<ModelState>(EMPTY_STATE);
  const stateRef = useRef(state);
  const operationRef = useRef(0);
  const savingRef = useRef(false);
  const savingOwnerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const sessionIdRef = useRef(sessionId);
  const enabledRef = useRef(enabled);
  const blockedRef = useRef(blocked);
  const catalogRef = useRef<LlmModelCatalogResponse | null>(null);

  stateRef.current = state;
  sessionIdRef.current = sessionId;
  enabledRef.current = enabled;
  blockedRef.current = blocked;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      operationRef.current += 1;
      savingOwnerRef.current = null;
      savingRef.current = false;
    };
  }, []);

  const load = useCallback(async (
    includeCatalog: boolean,
    refreshCatalog = false,
  ): Promise<boolean> => {
    if (!enabled || !sessionId || savingRef.current) return false;
    const requestId = ++operationRef.current;
    const requestSessionId = sessionId;
    const isUiCurrent = () => mountedRef.current &&
      enabledRef.current &&
      sessionIdRef.current === requestSessionId &&
      operationRef.current === requestId;
    setState((previous) => ({
      ...previous,
      settingsLoading: true,
      catalogLoading: includeCatalog,
      options: includeCatalog ? [] : previous.options,
      error: null,
    }));

    let identity: string;
    try {
      identity = await getCredentialIdentityRevision();
    } catch (error) {
      if (isUiCurrent()) setState((previous) => ({
        ...previous,
        settingsLoading: false,
        catalogLoading: false,
        error: errorMessage(error),
      }));
      return false;
    }
    if (!isUiCurrent()) return false;

    const [settingsResult, catalogResult] = await Promise.allSettled([
      getSessionLlmSettings(requestSessionId),
      includeCatalog
        ? refreshCatalog ? refreshLlmModelCatalog() : getCachedLlmModelCatalog()
        : Promise.resolve(null),
    ]);
    if (!isUiCurrent()) return false;
    let currentIdentity: string;
    try {
      currentIdentity = await getCredentialIdentityRevision();
    } catch {
      currentIdentity = "";
    }
    if (!isUiCurrent()) return false;
    if (currentIdentity !== identity) {
      setState({
        ...EMPTY_STATE,
        error: "認証または接続先が切り替わりました。現在のセッション設定を再取得してください。",
      });
      return false;
    }

    let routes: ReturnType<typeof readEnvelope> | null = null;
    let settingsError: string | null = null;
    if (settingsResult.status === "fulfilled") {
      try {
        routes = readEnvelope(settingsResult.value);
      } catch (error) {
        settingsError = errorMessage(error);
      }
    } else {
      settingsError = errorMessage(settingsResult.reason);
    }

    let nextOptions = stateRef.current.options;
    let catalogError: string | null = null;
    if (includeCatalog) {
      if (catalogResult.status === "fulfilled" && hasCatalogShape(catalogResult.value)) {
        catalogRef.current = catalogResult.value;
        nextOptions = routes
          ? optionsForRoute(catalogResult.value, routes.mainRoute, routes.effectiveRoute)
          : [];
      } else if (catalogResult.status === "fulfilled") {
        catalogError = "モデル候補の応答を読み取れませんでした。再取得してください。";
        catalogRef.current = null;
        nextOptions = [];
      } else if (catalogResult.status === "rejected") {
        catalogError = errorMessage(catalogResult.reason);
        catalogRef.current = null;
        nextOptions = [];
      }
    } else if (catalogRef.current && routes) {
      nextOptions = optionsForRoute(
        catalogRef.current,
        routes.mainRoute,
        routes.effectiveRoute,
      );
    }

    const error = settingsError ?? catalogError;
    setState({
      mainRoute: routes?.mainRoute ?? null,
      effectiveRoute: routes?.effectiveRoute ?? null,
      options: nextOptions,
      settingsReady: Boolean(routes),
      settingsLoading: false,
      catalogLoading: false,
      saving: false,
      error,
    });
    return !error;
  }, [enabled, sessionId]);

  const open = useCallback(() => load(true), [load]);
  const refresh = useCallback(
    (includeCatalog = true) => load(includeCatalog, includeCatalog),
    [load],
  );
  const isSaving = useCallback(() => savingRef.current, []);

  useEffect(() => {
    operationRef.current += 1;
    savingOwnerRef.current = null;
    savingRef.current = false;
    catalogRef.current = null;
    setState(EMPTY_STATE);
    if (!enabled || !sessionId) return;
    void load(false);
    return () => {
      operationRef.current += 1;
    };
  }, [authScopeHint, enabled, load, sessionId]);

  const select = useCallback(async (option: ChatResponseModelOption): Promise<boolean> => {
    if (
      !enabledRef.current ||
      blockedRef.current ||
      savingRef.current ||
      !stateRef.current.settingsReady
    ) return false;
    const currentOption = stateRef.current.options.find(
      (candidate) => candidate.provider === option.provider && candidate.model === option.model,
    );
    const requestSessionId = sessionIdRef.current;
    if (!currentOption || !requestSessionId) return false;

    savingRef.current = true;
    const requestId = ++operationRef.current;
    savingOwnerRef.current = requestId;
    const releaseSaving = () => {
      if (savingOwnerRef.current !== requestId) return;
      savingOwnerRef.current = null;
      savingRef.current = false;
    };
    const previousState = stateRef.current;
    const wanted = routeForOption(
      currentOption,
      previousState.mainRoute,
      previousState.effectiveRoute,
    );
    const isUiCurrent = () => mountedRef.current &&
      enabledRef.current &&
      sessionIdRef.current === requestSessionId &&
      operationRef.current === requestId;
    setState((previous) => ({ ...previous, saving: true, error: null }));

    let identity: string;
    try {
      identity = await getCredentialIdentityRevision();
    } catch (error) {
      if (isUiCurrent()) setState((previous) => ({
        ...previous,
        saving: false,
        error: errorMessage(error),
      }));
      releaseSaving();
      return false;
    }
    if (!isUiCurrent()) {
      releaseSaving();
      return false;
    }

    let putResult: SessionLlmSettingsEnvelope | null = null;
    let putError: unknown = null;
    try {
      putResult = await putSessionMainRoute(requestSessionId, wanted);
    } catch (error) {
      putError = error;
    }
    if (!isUiCurrent()) {
      releaseSaving();
      return false;
    }

    let currentIdentity: string;
    try {
      currentIdentity = await getCredentialIdentityRevision();
    } catch {
      currentIdentity = "";
    }
    if (!isUiCurrent()) {
      releaseSaving();
      return false;
    }
    if (currentIdentity !== identity) {
      releaseSaving();
      setState({
        ...EMPTY_STATE,
        error: "認証または接続先が切り替わりました。保存結果を再取得してください。",
      });
      return false;
    }

    let verified: SessionLlmSettingsEnvelope | null = null;
    if (putResult) {
      try {
        if (isPersistedRoute(putResult, wanted)) verified = putResult;
      } catch {
        // An incomplete response is reconciled through GET below.
      }
    }
    if (!verified) {
      try {
        const readback = await getSessionLlmSettings(requestSessionId);
        if (!isUiCurrent()) {
          releaseSaving();
          return false;
        }
        let readbackIdentity: string;
        try {
          readbackIdentity = await getCredentialIdentityRevision();
        } catch {
          readbackIdentity = "";
        }
        if (!isUiCurrent()) {
          releaseSaving();
          return false;
        }
        if (readbackIdentity !== identity) {
          releaseSaving();
          setState({
            ...EMPTY_STATE,
            error: "認証または接続先が切り替わりました。保存結果を再取得してください。",
          });
          return false;
        }
        if (isPersistedRoute(readback, wanted)) verified = readback;
        else {
          const routes = readEnvelope(readback);
          setState((previous) => ({
            ...previous,
            mainRoute: routes.mainRoute,
            effectiveRoute: routes.effectiveRoute,
            options: catalogRef.current
              ? optionsForRoute(catalogRef.current, routes.mainRoute, routes.effectiveRoute)
              : previous.options,
            saving: false,
            error: putError
              ? `モデル設定を保存できませんでした。現在の設定を再取得しました。${errorMessage(putError)}`
              : "選択したモデルが保存されていることを確認できませんでした。再取得してから選び直してください。",
          }));
          releaseSaving();
          return false;
        }
      } catch (readbackError) {
        if (isUiCurrent()) setState((previous) => ({
          ...previous,
          saving: false,
          error: `保存結果を確認できませんでした。再取得して状態を確認してください。${putError ? ` ${errorMessage(putError)}` : ` ${errorMessage(readbackError)}`}`,
        }));
        releaseSaving();
        return false;
      }
    }

    if (!verified || !isUiCurrent()) {
      releaseSaving();
      return false;
    }
    let routes: ReturnType<typeof readEnvelope>;
    try {
      routes = readEnvelope(verified);
    } catch (error) {
      setState((previous) => ({ ...previous, saving: false, error: errorMessage(error) }));
      releaseSaving();
      return false;
    }
    setState((previous) => ({
      ...previous,
      mainRoute: routes.mainRoute,
      effectiveRoute: routes.effectiveRoute,
      options: catalogRef.current
        ? optionsForRoute(catalogRef.current, routes.mainRoute, routes.effectiveRoute)
        : previous.options,
      saving: false,
      error: null,
    }));
    releaseSaving();
    return true;
  }, []);

  const displayedRoute = state.effectiveRoute ?? state.mainRoute;
  const selectedRoute = state.mainRoute ?? state.effectiveRoute;
  const currentModelLabel = displayedRoute
    ? `${displayedRoute.provider} / ${displayedRoute.model}`
    : state.settingsLoading
      ? "AoiDotsモデルを確認中…"
      : "AoiDotsモデル未確認";

  return {
    ...state,
    currentModelLabel,
    selectedKey: selectedRoute
      ? `${selectedRoute.provider.trim().toLowerCase()}:${selectedRoute.model.trim()}`
      : null,
    isSaving,
    open,
    refresh,
    select,
  };
}

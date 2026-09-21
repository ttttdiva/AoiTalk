"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useChatSessions } from "@/contexts/chat-session-context";
import { toast } from "sonner";
import {
  normalizeCharacterOptions,
  resolveCurrentCharacterSlug,
  type CharacterOption,
} from "@/lib/character-options";
import {
  resolveEffectiveModelId,
  resolveEffectiveProviderId,
  type LlmDeploymentMetadata,
} from "@/lib/llm-provider-visibility";
import type { LlmModelCatalogResponse } from "@/lib/chat-api";

export type RuntimeLlmEngine = {
  provider: string;
  model: string;
  label: string;
  available?: boolean;
  disabled?: boolean;
  unavailable?: boolean;
  availability_reason?: string | null;
  reasoning_effort_options?: string[];
  context_window_tokens?: number | null;
  supports_reasoning?: boolean;
};

const STRICT_RUNTIME_PROVIDERS = new Set([
  "openai_compatible_local",
  "ollama",
  "sglang",
]);

const API_KEY_GATED_PROVIDERS = new Set([
  "openai",
  "gemini",
  "openrouter",
  "deepseek",
  "deepinfra",
  "kimi",
]);

export type RuntimeFeatureState = {
  features: Record<string, boolean>;
  application_features?: Record<string, boolean>;
  discord_bot_service?: {
    state?: "stopped" | "starting" | "running" | "stopping" | "failed";
    user?: string | null;
    guild_count?: number;
    task_running?: boolean;
    last_error?: string | null;
  };
};

export type RuntimeLlmRoute = {
  provider: string;
  model: string;
};

export type RuntimeMetadataStatus = "loading" | "ready" | "stale" | "error";

export type RuntimeVoiceStatus = {
  ready: boolean;
  rms: number;
  recording: boolean;
};

export type RuntimeContextValue = {
  isConnected: boolean;
  characters: CharacterOption[];
  currentCharacter: string;
  changeCharacter: (slug: string, sessionId?: string | null) => Promise<boolean>;
  characterChanging?: boolean;
  characterStatus?: RuntimeMetadataStatus;
  characterError?: string | null;
  characterRefreshing?: boolean;
  refreshCharacters?: (sessionId?: string | null) => Promise<void>;
  llmEngines: RuntimeLlmEngine[];
  persistedLlm?: RuntimeLlmRoute | null;
  effectiveLlm?: RuntimeLlmRoute | null;
  currentLlm: RuntimeLlmRoute | null;
  /** LLM情報の取得状態。ready は engine/catalog の少なくとも一方が正常応答した状態。 */
  llmStatus?: RuntimeMetadataStatus;
  llmError?: string | null;
  llmRefreshing?: boolean;
  /** LLM情報だけを再取得する導線（文字・featureの状態は変更しない）。 */
  refreshLlm?: () => Promise<void>;
  changeLlmEngine: (provider: string, model: string) => Promise<boolean>;
  llmCatalog: LlmModelCatalogResponse | null;
  runtimeFeatures: RuntimeFeatureState | null;
  runtimeFeatureStatus?: RuntimeMetadataStatus;
  runtimeFeatureError?: string | null;
  runtimeFeaturesRefreshing?: boolean;
  refreshRuntimeFeatures?: () => Promise<RuntimeFeatureState | null>;
  changeRuntimeFeature: (feature: string, enabled: boolean) => Promise<boolean>;
  changeRuntimeFeatures: (features: Record<string, boolean>) => Promise<boolean>;
  llmDeployment: LlmDeploymentMetadata | null;
  llmChangeError: string | null;
  llmChanging: boolean;
  voiceStatus: RuntimeVoiceStatus | null;
};

type LlmEngineResponse = {
  success?: boolean;
  provider?: string;
  model?: string;
  persisted_provider?: string;
  persisted_model?: string;
  effective_provider?: string;
  effective_model?: string;
  effective_main?: {
    provider?: string | null;
    model?: string | null;
    effort?: string | null;
  } | null;
  execution_profile?: {
    effective_main?: {
      provider?: string | null;
      model?: string | null;
      effort?: string | null;
      reasoning_effort?: string | null;
    } | null;
  } | null;
  available?: RuntimeLlmEngine[];
  available_included?: boolean;
  deployment?: LlmDeploymentMetadata | null;
  current?: { provider?: string | null; model?: string | null } | null;
  detail?: unknown;
  message?: unknown;
  error?: unknown;
};

const RuntimeContext = createContext<RuntimeContextValue | null>(null);

function timeoutSignal(ms: number): AbortSignal | undefined {
  if (typeof AbortSignal !== "undefined" && "timeout" in AbortSignal) {
    return AbortSignal.timeout(ms);
  }
  return undefined;
}

function sessionIdFromLocation(): string {
  if (typeof window === "undefined") return "";
  const params = new URLSearchParams(window.location.search);
  // The chat route uses ``s`` while API callers and deep links sometimes use
  // the explicit ``session_id`` spelling.  Prefer the explicit API spelling
  // when both are present, but accept either as the active session.
  return (
    params.get("session_id")?.trim() || params.get("s")?.trim() || ""
  );
}

async function responseErrorMessage(
  response: Response,
  fallback: string,
): Promise<string> {
  const data = (await response.json().catch(() => null)) as
    | { detail?: unknown; message?: unknown; error?: unknown }
    | null;
  for (const value of [data?.detail, data?.message, data?.error]) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return `${fallback} (${response.status})`;
}

type RouteLike = {
  provider?: unknown;
  model?: unknown;
};

function runtimeLlmRoute(
  provider: unknown,
  model: unknown,
): RuntimeLlmRoute | null {
  const normalizedProvider =
    typeof provider === "string" ? provider.trim().toLowerCase() : "";
  const normalizedModel = typeof model === "string" ? model.trim() : "";
  if (!normalizedProvider || !normalizedModel) return null;
  return { provider: normalizedProvider, model: normalizedModel };
}

function normalizeLlmRoute(value: unknown): RuntimeLlmRoute | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const route = value as RouteLike;
  return runtimeLlmRoute(route.provider, route.model);
}

function deploymentEffectiveRoute(
  deployment: LlmDeploymentMetadata | null | undefined,
): RuntimeLlmRoute | null {
  return runtimeLlmRoute(
    resolveEffectiveProviderId(deployment),
    resolveEffectiveModelId(deployment),
  );
}

function enginePersistedRoute(parsed: LlmEngineResponse): RuntimeLlmRoute | null {
  return (
    runtimeLlmRoute(parsed.persisted_provider, parsed.persisted_model) ??
    runtimeLlmRoute(parsed.provider, parsed.model)
  );
}

function engineEffectiveRoute(parsed: LlmEngineResponse): RuntimeLlmRoute | null {
  return (
    runtimeLlmRoute(parsed.effective_main?.provider, parsed.effective_main?.model) ??
    runtimeLlmRoute(
      parsed.execution_profile?.effective_main?.provider,
      parsed.execution_profile?.effective_main?.model,
    ) ??
    runtimeLlmRoute(parsed.effective_provider, parsed.effective_model) ??
    deploymentEffectiveRoute(parsed.deployment) ??
    enginePersistedRoute(parsed)
  );
}

function catalogPersistedRoute(
  catalog: LlmModelCatalogResponse | null | undefined,
): RuntimeLlmRoute | null {
  return runtimeLlmRoute(catalog?.current?.provider, catalog?.current?.model);
}

function catalogEffectiveRoute(
  catalog: LlmModelCatalogResponse | null | undefined,
): RuntimeLlmRoute | null {
  return deploymentEffectiveRoute(catalog?.deployment) ?? catalogPersistedRoute(catalog);
}

function providerId(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function providerModels(
  provider: LlmModelCatalogResponse["providers"][number],
): LlmModelCatalogResponse["providers"][number]["models"] {
  // For strict runtimes, only backend discovery (`chat_models`) proves that
  // a model is actually served.  Static `models` are intentionally ignored.
  if (STRICT_RUNTIME_PROVIDERS.has(providerId(provider.id))) {
    return Array.isArray(provider.chat_models) ? provider.chat_models : [];
  }
  return Array.isArray(provider.chat_models)
    ? provider.chat_models
    : provider.models ?? [];
}

function providerDiscoveryState(
  provider: LlmModelCatalogResponse["providers"][number],
): string {
  return typeof provider.availability?.state === "string"
    ? provider.availability.state.trim().toLowerCase()
    : "";
}

function engineMetadata(
  provider: LlmModelCatalogResponse["providers"][number],
  model: LlmModelCatalogResponse["providers"][number]["models"][number],
): Partial<RuntimeLlmEngine> {
  return {
    label: model.label || `${provider.label} / ${model.id}`,
    available: provider.available,
    disabled: provider.disabled,
    unavailable: provider.unavailable,
    availability_reason: provider.availability_reason,
    reasoning_effort_options: model.reasoning_effort_options,
    context_window_tokens: model.context_window_tokens,
    supports_reasoning: model.supports_reasoning,
  };
}

function mergeEngineCatalog(
  engines: RuntimeLlmEngine[],
  catalog: LlmModelCatalogResponse | null,
): RuntimeLlmEngine[] {
  if (!catalog?.providers?.length) return engines;
  const metadataByKey = new Map<string, Partial<RuntimeLlmEngine>>();
  for (const provider of catalog.providers) {
    for (const model of providerModels(provider)) {
      metadataByKey.set(
        `${provider.id}::${model.id}`,
        engineMetadata(provider, model),
      );
    }
  }
  return engines.map((engine) => {
    const metadata = metadataByKey.get(`${engine.provider}::${engine.model}`);
    if (!metadata) return engine;
    return {
      ...metadata,
      ...engine,
      reasoning_effort_options:
        metadata.reasoning_effort_options ?? engine.reasoning_effort_options,
      context_window_tokens:
        metadata.context_window_tokens ?? engine.context_window_tokens,
      supports_reasoning:
        metadata.supports_reasoning ?? engine.supports_reasoning,
    };
  });
}

/** Build the compact one-model-per-provider runtime projection from catalog. */
function compactEnginesFromCatalog(
  catalog: LlmModelCatalogResponse,
): RuntimeLlmEngine[] {
  const result: RuntimeLlmEngine[] = [];
  for (const provider of catalog.providers) {
    const id = providerId(provider.id);
    if (
      provider.available === false ||
      provider.disabled === true ||
      provider.unavailable === true ||
      (API_KEY_GATED_PROVIDERS.has(id) &&
        provider.settings?.api_key_configured === false) ||
      (STRICT_RUNTIME_PROVIDERS.has(id) &&
        providerDiscoveryState(provider) === "error")
    ) {
      continue;
    }
    const models = providerModels(provider);
    if (models.length === 0) continue;
    const configuredModel = provider.configured_model?.trim() ?? "";
    // Do not silently substitute a served model for a stale persisted model.
    // The full selector receives the complete served `chat_models` list and
    // can make recovery an explicit user action.
    const selected = configuredModel
      ? models.find((model) => model.id.trim() === configuredModel) ?? null
      : models[0] ?? null;
    if (!selected) continue;
    result.push({
      provider: provider.id,
      model: selected.id,
      ...engineMetadata(provider, selected),
      label: selected.label || `${provider.label} / ${selected.id}`,
    });
  }
  return result;
}

function strictRuntimeDiscoveryErrorForRoute(
  catalog: LlmModelCatalogResponse | null,
  route: RuntimeLlmRoute | null,
): string | null {
  if (!catalog || !route || !STRICT_RUNTIME_PROVIDERS.has(providerId(route.provider))) {
    return null;
  }
  const provider = catalog.providers.find(
    (item) => providerId(item.id) === providerId(route.provider),
  );
  if (!provider || providerDiscoveryState(provider) !== "error") return null;
  const message =
    typeof provider.availability?.error === "string"
      ? provider.availability.error.trim()
      : "";
  return message || `${route.provider} runtimeのモデル一覧を取得できませんでした`;
}

function catalogHasUsableData(catalog: LlmModelCatalogResponse | null): boolean {
  if (!catalog) return false;
  const current = normalizeLlmRoute(catalog.current);
  return Boolean(current) || catalog.providers.some((provider) => {
    const models = providerModels(provider);
    return models.length > 0 ||
      (!STRICT_RUNTIME_PROVIDERS.has(providerId(provider.id)) &&
        (provider.models ?? []).length > 0);
  });
}

function hasUsableLlmSnapshot(
  current: RuntimeLlmRoute | null,
  engines: RuntimeLlmEngine[],
  catalog: LlmModelCatalogResponse | null,
): boolean {
  return Boolean(current) || engines.length > 0 || catalogHasUsableData(catalog);
}

function mergeStrictRuntimeCatalogLkg(
  previous: LlmModelCatalogResponse | null,
  next: LlmModelCatalogResponse,
): LlmModelCatalogResponse {
  if (!previous?.providers?.length) return next;
  const providers = next.providers.map((provider) => {
    if (
      !STRICT_RUNTIME_PROVIDERS.has(providerId(provider.id)) ||
      providerDiscoveryState(provider) !== "error"
    ) {
      return provider;
    }
    const old = previous.providers.find(
      (candidate) => providerId(candidate.id) === providerId(provider.id),
    );
    const oldChatModels = old?.chat_models ?? old?.available_models;
    if (!Array.isArray(oldChatModels) || oldChatModels.length === 0) {
      return provider;
    }
    // Discovery `error` means the runtime could not answer. Retain the last
    // served strict candidates rather than presenting a transient outage as
    // an authoritative empty result. Keep the error state for diagnostics.
    return {
      ...provider,
      chat_models: oldChatModels,
      available_models: oldChatModels,
    };
  });
  return { ...next, providers };
}

function useVoiceStatus(pythonConnected: boolean): RuntimeVoiceStatus | null {
  const [status, setStatus] = useState<RuntimeVoiceStatus | null>(null);

  useEffect(() => {
    if (!pythonConnected) return;

    let mounted = true;
    const poll = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      try {
        const response = await fetch("/api/python-proxy/voice_status", {
          credentials: "include",
          signal: timeoutSignal(3000),
        });
        if (response.ok && mounted) {
          setStatus((await response.json()) as RuntimeVoiceStatus);
        }
      } catch {
        if (mounted) setStatus(null);
      }
    };

    void poll();
    const interval = setInterval(() => void poll(), 15000);
    const onVisibility = () => {
      if (typeof document !== "undefined" && !document.hidden) void poll();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      mounted = false;
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [pythonConnected]);

  return pythonConnected ? status : null;
}

export function RuntimeProvider({
  children,
}: {
  children: ReactNode;
}) {
  const { updateSession } = useChatSessions();

  const [isConnected, setIsConnected] =
    useState(false);

  const [characters, setCharacters] =
    useState<CharacterOption[]>([]);
  const [currentCharacter, setCurrentCharacter] =
    useState("");
  const [characterStatus, setCharacterStatus] =
    useState<RuntimeMetadataStatus>("loading");
  const [characterError, setCharacterError] =
    useState<string | null>(null);
  const [
    characterRefreshing,
    setCharacterRefreshing,
  ] = useState(false);
  const [
    characterChanging,
    setCharacterChanging,
  ] = useState(false);

  const [llmEngines, setLlmEngines] =
    useState<RuntimeLlmEngine[]>([]);

  const [currentLlm, setCurrentLlm] =
    useState<RuntimeLlmRoute | null>(null);
  const [persistedLlm, setPersistedLlm] =
    useState<RuntimeLlmRoute | null>(null);
  const [effectiveLlm, setEffectiveLlm] =
    useState<RuntimeLlmRoute | null>(null);

  const [llmStatus, setLlmStatus] =
    useState<RuntimeMetadataStatus>("loading");
  const [llmError, setLlmError] =
    useState<string | null>(null);
  const [llmRefreshing, setLlmRefreshing] =
    useState(false);

  const [llmCatalog, setLlmCatalog] =
    useState<LlmModelCatalogResponse | null>(
      null,
    );

  const [llmDeployment, setLlmDeployment] =
    useState<LlmDeploymentMetadata | null>(
      null,
    );

  const [llmChangeError, setLlmChangeError] =
    useState<string | null>(null);
  const [llmChanging, setLlmChanging] =
    useState(false);

  const [
    runtimeFeatures,
    setRuntimeFeatures,
  ] = useState<RuntimeFeatureState | null>(
    null,
  );

  const characterMutationRef = useRef(false);
  const runtimeFeatureMutationRef =
    useRef(false);

  const characterSnapshotRef =
    useRef(false);
  const characterNeedsRefreshRef =
    useRef(true);
  const characterEpochRef = useRef(0);
  const currentCharacterRef = useRef("");

  const characterRefreshPromiseRef =
    useRef<Promise<void> | null>(null);
  const characterRefreshKeyRef =
    useRef<string | null>(null);

  const runtimeFeatureRefreshPromiseRef =
    useRef<
      Promise<RuntimeFeatureState | null> | null
    >(null);
  const runtimeFeatureNeedsRefreshRef =
    useRef(true);

  const llmRefreshPromiseRef =
    useRef<Promise<void> | null>(null);
  const llmNeedsRefreshRef =
    useRef(true);

  const llmRequestSeqRef = useRef(0);

  /**
   * Mutations advance the epoch after a POST is accepted.
   *
   * Every GET captures the epoch at request start, therefore an older
   * response cannot undo a successful engine switch.
   */
  const llmEpochRef = useRef(0);

  const llmEnginesRef =
    useRef<RuntimeLlmEngine[]>([]);
  const currentLlmRef =
    useRef<RuntimeLlmRoute | null>(null);
  const persistedLlmRef =
    useRef<RuntimeLlmRoute | null>(null);
  const effectiveLlmRef =
    useRef<RuntimeLlmRoute | null>(null);
  const llmCatalogRef =
    useRef<LlmModelCatalogResponse | null>(
      null,
    );
  const llmDeploymentRef =
    useRef<LlmDeploymentMetadata | null>(null);

  const updateLlmEngines = useCallback(
    (next: RuntimeLlmEngine[]) => {
      llmEnginesRef.current = next;
      setLlmEngines(next);
    },
    [],
  );

  const updateCurrentLlm = useCallback(
    (next: RuntimeLlmRoute | null) => {
      currentLlmRef.current = next;
      setCurrentLlm(next);
    },
    [],
  );

  const updatePersistedLlm = useCallback(
    (next: RuntimeLlmRoute | null) => {
      persistedLlmRef.current = next;
      setPersistedLlm(next);
    },
    [],
  );

  const updateEffectiveLlm = useCallback(
    (next: RuntimeLlmRoute | null) => {
      effectiveLlmRef.current = next;
      setEffectiveLlm(next);

      // currentLlm remains the backward-compatible effective route.
      updateCurrentLlm(next);
    },
    [updateCurrentLlm],
  );

  const updateLlmDeployment = useCallback(
    (next: LlmDeploymentMetadata | null) => {
      llmDeploymentRef.current = next;
      setLlmDeployment(next);
    },
    [],
  );

  const refreshRuntimeFeatures =
    useCallback(
      (): Promise<RuntimeFeatureState | null> => {
        const inFlight =
          runtimeFeatureRefreshPromiseRef.current;

        if (inFlight) {
          return inFlight;
        }

        const task = (async () => {
          try {
            const response = await fetch(
              "/api/python-proxy/runtime/features",
              {
                credentials: "include",
                signal: timeoutSignal(3000),
              },
            );

            if (!response.ok) {
              throw new Error(
                await responseErrorMessage(
                  response,
                  "Runtime feature取得に失敗しました",
                ),
              );
            }

            const data =
              (await response.json()) as RuntimeFeatureState;

            runtimeFeatureNeedsRefreshRef.current = false;
            setRuntimeFeatures(data);
            return data;
          } catch {
            runtimeFeatureNeedsRefreshRef.current = true;
            return null;
          }
        })();

        runtimeFeatureRefreshPromiseRef.current =
          task;

        void task.then(
          () => {
            if (runtimeFeatureRefreshPromiseRef.current === task) {
              runtimeFeatureRefreshPromiseRef.current = null;
            }
          },
          () => {
            if (runtimeFeatureRefreshPromiseRef.current === task) {
              runtimeFeatureRefreshPromiseRef.current = null;
            }
          },
        );

        return task;
      },
      [],
    );

  const refreshCharacters = useCallback(
    (requestedSessionId?: string | null): Promise<void> => {
      // Character metadata is session-scoped. Coalesce requests for the same
      // session, but supersede an in-flight request when navigation switches
      // to another session so the new chat is never left with the old value.
      const activeSessionId =
        requestedSessionId === undefined
          ? sessionIdFromLocation()
          : requestedSessionId?.trim() || "";
      const requestKey = activeSessionId;
      const inFlight =
        characterRefreshPromiseRef.current;

      if (
        inFlight &&
        characterRefreshKeyRef.current === requestKey
      ) {
        return inFlight;
      }
      if (inFlight) {
        characterEpochRef.current += 1;
      }

      const epoch = characterEpochRef.current;
      const hadSnapshot =
        characterSnapshotRef.current;

      setCharacterRefreshing(true);

      if (!hadSnapshot) {
        setCharacterStatus("loading");
      }

      const task = (async () => {
        const query = activeSessionId
          ? `?session_id=${encodeURIComponent(
              activeSessionId,
            )}`
          : "";

        try {
          const response = await fetch(
            `/api/python-proxy/characters${query}`,
            {
              credentials: "include",
              signal: timeoutSignal(3000),
            },
          );

          if (!response.ok) {
            throw new Error(
              await responseErrorMessage(
                response,
                "キャラクター取得に失敗しました",
              ),
            );
          }

          const data = await response.json();

          if (
            epoch !== characterEpochRef.current
          ) {
            return;
          }

          const options =
            normalizeCharacterOptions(data);

          // A successful [] is authoritative.
          characterSnapshotRef.current = true;
          characterNeedsRefreshRef.current = false;
          const nextCurrent = resolveCurrentCharacterSlug(
            options,
            data.current,
          );
          currentCharacterRef.current = nextCurrent;
          setCharacters(options);
          setCurrentCharacter(
            nextCurrent,
          );

          setCharacterError(null);
          setCharacterStatus("ready");
        } catch (error) {
          if (
            epoch !== characterEpochRef.current
          ) {
            return;
          }

          characterNeedsRefreshRef.current = true;

          setCharacterError(
            error instanceof Error
              ? error.message
              : "キャラクター情報を取得できませんでした",
          );

          setCharacterStatus(
            characterSnapshotRef.current
              ? "stale"
              : "error",
          );

          // Never replace an LKG snapshot with [] because of a timeout/error.
        } finally {
          if (
            epoch === characterEpochRef.current
          ) {
            setCharacterRefreshing(false);
          }
        }
      })();

      characterRefreshPromiseRef.current = task;
      characterRefreshKeyRef.current = requestKey;

      void task.then(
        () => {
          if (characterRefreshPromiseRef.current === task) {
            characterRefreshPromiseRef.current = null;
            characterRefreshKeyRef.current = null;
          }
        },
        () => {
          if (characterRefreshPromiseRef.current === task) {
            characterRefreshPromiseRef.current = null;
            characterRefreshKeyRef.current = null;
          }
        },
      );

      return task;
    },
    [],
  );

  const refreshLlmMetadata = useCallback(
    (): Promise<void> => {
      const inFlight =
        llmRefreshPromiseRef.current;

      if (inFlight) {
        return inFlight;
      }

      const task = (async () => {
        const requestId =
          ++llmRequestSeqRef.current;
        const epoch = llmEpochRef.current;

        const isLatestRequest = () =>
          requestId ===
            llmRequestSeqRef.current &&
          epoch === llmEpochRef.current;

        type EndpointStatus =
          | "pending"
          | "success"
          | "error";

        const attempt = {
          engine: {
            status:
              "pending" as EndpointStatus,
            hasModels: false,
            availableIncluded: false,
            persisted:
              null as RuntimeLlmRoute | null,
            effective:
              null as RuntimeLlmRoute | null,
            available: [] as RuntimeLlmEngine[],
          },
          catalog: {
            status:
              "pending" as EndpointStatus,
            hasModels: false,
            persisted:
              null as RuntimeLlmRoute | null,
            effective:
              null as RuntimeLlmRoute | null,
          },
          errors: [] as string[],
        };

        const hadSnapshot =
          hasUsableLlmSnapshot(
            currentLlmRef.current,
            llmEnginesRef.current,
            llmCatalogRef.current,
          );

        setLlmRefreshing(true);

        if (!hadSnapshot) {
          setLlmStatus("loading");
        }

        const commitReady = () => {
          if (!isLatestRequest()) {
            return;
          }

          if (
            hasUsableLlmSnapshot(
              currentLlmRef.current,
              llmEnginesRef.current,
              llmCatalogRef.current,
            )
          ) {
            // Do not clear the error here. The other endpoint may still fail,
            // in which case the final state must become `stale`.
            setLlmStatus("ready");
          }
        };

        const runIndependent = async (
          label: string,
          action: () => Promise<void>,
          onError: () => void,
        ) => {
          try {
            await action();
          } catch (error) {
            if (!isLatestRequest()) {
              return;
            }

            attempt.errors.push(
              error instanceof Error
                ? error.message
                : `${label}取得に失敗しました`,
            );

            onError();
          }
        };

        const engineTask =
          runIndependent(
            "LLM engine",
            async () => {
              const response = await fetch(
                "/api/python-proxy/llm/engine?include_available=false",
                {
                  credentials: "include",
                  signal: timeoutSignal(3000),
                },
              );

              if (!response.ok) {
                throw new Error(
                  `LLM engine取得に失敗しました (${response.status})`,
                );
              }

              const parsed =
                (await response.json()) as LlmEngineResponse;

              if (
                !parsed ||
                typeof parsed !== "object"
              ) {
                throw new Error(
                  "LLM engine取得に失敗しました (invalid response)",
                );
              }

              if (!isLatestRequest()) {
                return;
              }

              const persisted =
                enginePersistedRoute(parsed);
              const effective =
                engineEffectiveRoute(parsed);

              const available =
                Array.isArray(parsed.available)
                  ? parsed.available
                  : [];

              const availableIncluded =
                parsed.available_included !==
                false;

              attempt.engine.status =
                "success";
              attempt.engine.persisted =
                persisted;
              attempt.engine.effective =
                effective;
              attempt.engine.availableIncluded =
                availableIncluded;
              attempt.engine.hasModels =
                availableIncluded &&
                available.length > 0;
              attempt.engine.available = available;

              if (persisted) {
                updatePersistedLlm(
                  persisted,
                );
              }

              if (effective) {
                updateEffectiveLlm(
                  effective,
                );
              }

              /**
               * Compatibility with an older backend which does not understand
               * include_available=false. New backend responses set
               * available_included=false and the catalog becomes the sole
               * candidate/discovery source.
               */
              if (
                availableIncluded &&
                available.length > 0
              ) {
                updateLlmEngines(
                  mergeEngineCatalog(
                    available,
                    llmCatalogRef.current,
                  ),
                );
              }

              if (
                parsed.deployment !==
                undefined
              ) {
                updateLlmDeployment(
                  parsed.deployment ??
                    null,
                );
              }

              commitReady();
            },
            () => {
              attempt.engine.status =
                "error";
            },
          );

        const catalogTask =
          runIndependent(
            "LLM catalog",
            async () => {
              const response = await fetch(
                "/api/python-proxy/llm/models",
                {
                  credentials: "include",
                  signal: timeoutSignal(3000),
                },
              );

              if (!response.ok) {
                throw new Error(
                  `LLM catalog取得に失敗しました (${response.status})`,
                );
              }

              const parsed =
                (await response.json()) as LlmModelCatalogResponse;

              if (
                !parsed ||
                !Array.isArray(
                  parsed.providers,
                )
              ) {
                throw new Error(
                  "LLM catalog取得に失敗しました (invalid response)",
                );
              }

              if (!isLatestRequest()) {
                return;
              }

              const persisted =
                catalogPersistedRoute(
                  parsed,
                );
              const effective =
                catalogEffectiveRoute(
                  parsed,
                );

              attempt.catalog.status =
                "success";
              attempt.catalog.persisted =
                persisted;
              attempt.catalog.effective =
                effective;

              // This is the incoming authoritative discovery, not the merged
              // LKG projection.
              attempt.catalog.hasModels =
                parsed.providers.some(
                  (provider) => {
                    const models =
                      Array.isArray(
                        provider.chat_models,
                      )
                        ? provider.chat_models
                        : STRICT_RUNTIME_PROVIDERS.has(
                              provider.id
                                .trim()
                                .toLowerCase(),
                            )
                          ? []
                          : provider.models ??
                            [];

                    return (
                      models.length > 0
                    );
                  },
                );

              const mergedCatalog =
                mergeStrictRuntimeCatalogLkg(
                  llmCatalogRef.current,
                  parsed,
                );

              llmCatalogRef.current =
                mergedCatalog;
              setLlmCatalog(
                mergedCatalog,
              );

              if (
                attempt.engine.status !==
                "success"
              ) {
                if (persisted) {
                  updatePersistedLlm(
                    persisted,
                  );
                }

                if (effective) {
                  updateEffectiveLlm(
                    effective,
                  );
                }
              }

              /**
               * Selectability comes from the latest catalog availability
               * state. Because mergeStrictRuntimeCatalogLkg leaves
               * availability="error" intact, stale strict-runtime LKG models
               * are never reintroduced here.
               */
              const compactEngines =
                attempt.engine.status === "success" &&
                attempt.engine.availableIncluded &&
                attempt.engine.available.length > 0
                  ? mergeEngineCatalog(
                      attempt.engine.available,
                      mergedCatalog,
                    )
                  : compactEnginesFromCatalog(mergedCatalog);

              const preserveLkgAfterEngineFailure =
                attempt.engine.status === "error" &&
                llmEnginesRef.current.length > 0 &&
                !parsed.providers.some((provider) =>
                  STRICT_RUNTIME_PROVIDERS.has(
                    provider.id.trim().toLowerCase(),
                  ),
                );
              const preserveLkgAfterEngineEmpty =
                attempt.engine.status === "success" &&
                attempt.engine.availableIncluded &&
                attempt.engine.available.length === 0 &&
                llmEnginesRef.current.length > 0 &&
                !parsed.providers.some((provider) =>
                  STRICT_RUNTIME_PROVIDERS.has(
                    provider.id.trim().toLowerCase(),
                  ),
                );
              const preserveLkg =
                preserveLkgAfterEngineFailure ||
                preserveLkgAfterEngineEmpty;

              if (
                !preserveLkg &&
                (compactEngines.length > 0 || parsed.providers.length > 0)
              ) {
                updateLlmEngines(
                  compactEngines,
                );
              } else if (preserveLkg) {
                updateLlmEngines(
                  mergeEngineCatalog(
                    llmEnginesRef.current,
                    mergedCatalog,
                  ),
                );
              }

              if (
                parsed.deployment !==
                undefined
              ) {
                updateLlmDeployment(
                  parsed.deployment ??
                    null,
                );
              }

              commitReady();
            },
            () => {
              attempt.catalog.status =
                "error";
            },
          );

        await Promise.allSettled([
          engineTask,
          catalogTask,
        ]);

        if (!isLatestRequest()) {
          return;
        }

        const confirmedEmpty =
          attempt.engine.status ===
            "success" &&
          attempt.catalog.status ===
            "success" &&
          !attempt.engine.hasModels &&
          !attempt.catalog.hasModels &&
          !attempt.engine.effective &&
          !attempt.catalog.effective;

        if (confirmedEmpty) {
          updateLlmEngines([]);
          updatePersistedLlm(null);
          updateEffectiveLlm(null);

          llmNeedsRefreshRef.current = false;
          setLlmError(null);
          setLlmStatus("ready");
          setLlmRefreshing(false);
          return;
        }

        const strictDiscoveryError =
          strictRuntimeDiscoveryErrorForRoute(
            llmCatalogRef.current,
            effectiveLlmRef.current,
          );

        const errors = [
          ...attempt.errors,
        ];

        if (
          strictDiscoveryError &&
          !errors.includes(
            strictDiscoveryError,
          )
        ) {
          errors.push(
            strictDiscoveryError,
          );
        }

        if (
          hasUsableLlmSnapshot(
            currentLlmRef.current,
            llmEnginesRef.current,
            llmCatalogRef.current,
          )
        ) {
          if (errors.length > 0) {
            llmNeedsRefreshRef.current = true;
            setLlmError(
              errors.join(" / "),
            );
            setLlmStatus("stale");
          } else {
            llmNeedsRefreshRef.current = false;
            setLlmError(null);
            setLlmStatus("ready");
          }
        } else {
          llmNeedsRefreshRef.current = true;
          setLlmError(
            errors.join(" / ") ||
              "LLM情報を取得できませんでした",
          );
          setLlmStatus("error");
        }

        setLlmRefreshing(false);
      })();

      llmRefreshPromiseRef.current =
        task;

      void task.then(
        () => {
          if (llmRefreshPromiseRef.current === task) {
            llmRefreshPromiseRef.current = null;
          }
        },
        () => {
          if (llmRefreshPromiseRef.current === task) {
            llmRefreshPromiseRef.current = null;
          }
        },
      );

      return task;
    },
    [
      updateEffectiveLlm,
      updateLlmDeployment,
      updateLlmEngines,
      updatePersistedLlm,
    ],
  );

  const refreshLlm = useCallback(
    async () => {
      await refreshLlmMetadata();
    },
    [refreshLlmMetadata],
  );

  /**
   * Bootstrap metadata exactly once.
   *
   * Health remains independent: a transient health failure must not serialize
   * the initial LLM/character display, but health polling also must not become
   * a 15-second metadata polling loop.
   */
  useEffect(() => {
    void refreshCharacters();
    void refreshRuntimeFeatures();
    void refreshLlmMetadata();
  }, [
    refreshCharacters,
    refreshLlmMetadata,
    refreshRuntimeFeatures,
  ]);

  useEffect(() => {
    let mounted = true;
    let healthRetryCount = 0;
    let healthRetryTimer:
      number | null = null;
    let healthCheckInFlight = false;
    let previousConnected:
      boolean | null = null;
    let hasEstablishedConnection = false;

    const maxHealthRetries = 2;

    const clearHealthRetry = () => {
      if (
        healthRetryTimer !== null
      ) {
        window.clearTimeout(
          healthRetryTimer,
        );
        healthRetryTimer = null;
      }
    };

    const scheduleHealthRetry = () => {
      if (
        healthRetryCount >=
        maxHealthRetries
      ) {
        return;
      }

      clearHealthRetry();

      const delay =
        250 * 2 ** healthRetryCount;

      healthRetryCount += 1;

      healthRetryTimer =
        window.setTimeout(
          () => void check(),
          delay,
        );
    };

    const check = async () => {
      if (
        typeof document !==
          "undefined" &&
        document.hidden
      ) {
        return;
      }

      if (healthCheckInFlight) {
        return;
      }

      healthCheckInFlight = true;

      try {
        const response = await fetch(
          "/api/python-proxy/health",
          {
            credentials: "include",
            signal: timeoutSignal(
              3000,
            ),
          },
        );

        const connected =
          response.ok;

        // Finish the response before reporting connectivity. Leaving this
        // body unread makes Chromium abort the successful health request when
        // its timeout fires, and hides failures while receiving the body.
        await response.text();

        if (!mounted) {
          return;
        }

        const wasConnected =
          previousConnected;

        previousConnected =
          connected;
        setIsConnected(connected);

        if (connected) {
          healthRetryCount = 0;
          clearHealthRetry();

          /**
           * A healthy poll is intentionally cheap: it does not refresh
           * metadata merely because the interval fired.  On a real
           * disconnected -> connected transition refresh all three snapshots;
           * while the initial bootstrap is settling (or after a transient
           * metadata failure), retry only the endpoint that still needs a
           * snapshot.  The refresh helpers dedupe requests already in flight.
           */
          const reconnecting =
            wasConnected === false &&
            hasEstablishedConnection;

          if (
            reconnecting ||
            characterNeedsRefreshRef.current
          ) {
            void refreshCharacters();
          }
          if (
            reconnecting ||
            runtimeFeatureNeedsRefreshRef.current
          ) {
            void refreshRuntimeFeatures();
          }
          if (
            reconnecting ||
            llmNeedsRefreshRef.current
          ) {
            void refreshLlmMetadata();
          }
          hasEstablishedConnection = true;
        } else {
          scheduleHealthRetry();
        }
      } catch {
        if (!mounted) {
          return;
        }

        previousConnected = false;
        setIsConnected(false);
        scheduleHealthRetry();
      } finally {
        healthCheckInFlight =
          false;
      }
    };

    void check();

    const interval =
      window.setInterval(
        () => void check(),
        15000,
      );

    const onVisibility = () => {
      if (
        typeof document !==
          "undefined" &&
        !document.hidden
      ) {
        void check();
      }
    };

    document.addEventListener(
      "visibilitychange",
      onVisibility,
    );

    return () => {
      mounted = false;
      window.clearInterval(
        interval,
      );
      clearHealthRetry();
      document.removeEventListener(
        "visibilitychange",
        onVisibility,
      );
    };
  }, [
    refreshCharacters,
    refreshLlmMetadata,
    refreshRuntimeFeatures,
  ]);

  const changeCharacter =
    useCallback(
      async (
        slug: string,
        sessionId?: string | null,
      ) => {
        if (
          characterMutationRef.current
        ) {
          return false;
        }

        characterMutationRef.current =
          true;
        setCharacterChanging(true);

        const activeSessionId =
          sessionId ||
          sessionIdFromLocation();

        const query =
          activeSessionId
            ? `?session_id=${encodeURIComponent(
                activeSessionId,
              )}`
            : "";

        try {
          const response =
            await fetch(
              `/api/python-proxy/character/${encodeURIComponent(
                slug,
              )}${query}`,
              {
                method: "POST",
                credentials:
                  "include",
              },
            );

          if (!response.ok) {
            toast.error(
              await responseErrorMessage(
                response,
                "キャラクター変更に失敗しました",
              ),
            );
            return false;
          }

          const data =
            await response
              .json()
              .catch(() => ({}));

          const selected =
            typeof data?.character_slug ===
              "string" &&
            data.character_slug.trim()
              ? data.character_slug.trim()
              : slug;

          /**
           * The POST is authoritative for the selected character. Invalidate
           * an older GET before publishing it so its late response cannot
           * restore the previous selection.
           */
          characterEpochRef.current += 1;
          setCharacterRefreshing(false);
          setCharacterError(null);
          setCharacterStatus("ready");
          currentCharacterRef.current = selected;
          setCurrentCharacter(selected);

          if (activeSessionId) {
            updateSession(
              activeSessionId,
              (session) => ({
                ...session,
                character_name:
                  selected,
              }),
            );
          }

          return true;
        } catch (error) {
          console.error(
            "キャラクター変更失敗:",
            error,
          );

          toast.error(
            error instanceof Error
              ? error.message
              : "キャラクター変更に失敗しました",
          );

          return false;
        } finally {
          characterMutationRef.current =
            false;
          setCharacterChanging(false);
        }
      },
      [updateSession],
    );

  const changeLlmEngine =
    useCallback(
      async (
        provider: string,
        model: string,
      ) => {
        if (
          !provider ||
          !model ||
          llmChanging
        ) {
          return false;
        }

        setLlmChanging(true);
        setLlmChangeError(null);

        try {
          const response =
            await fetch(
              "/api/python-proxy/llm/engine",
              {
                method: "POST",
                credentials:
                  "include",
                headers: {
                  "Content-Type":
                    "application/json",
                },
                body: JSON.stringify({
                  provider,
                  model,
                }),
              },
            );

          const data =
            (await response
              .json()
              .catch(
                () => ({}),
              )) as LlmEngineResponse;

          if (
            !response.ok ||
            data.success === false
          ) {
            const detail =
              typeof data.detail ===
              "string"
                ? data.detail
                : typeof data.message ===
                    "string"
                  ? data.message
                  : typeof data.error ===
                      "string"
                    ? data.error
                    : `LLMエンジン変更に失敗しました (${response.status})`;

            setLlmChangeError(
              detail,
            );
            toast.error(detail);
            return false;
          }

          /**
           * A successful mutation is authoritative. Invalidate every GET
           * started before it before publishing the new route.
           */
          llmEpochRef.current += 1;
          llmRequestSeqRef.current +=
            1;

          const selected =
            runtimeLlmRoute(
              data.provider,
              data.model,
            ) ??
            runtimeLlmRoute(
              provider,
              model,
            );

          if (!selected) {
            throw new Error(
              "LLMエンジン変更応答に有効なprovider/modelがありません",
            );
          }

          const nextPersisted =
            enginePersistedRoute(
              data,
            ) ?? selected;

          const nextEffective =
            engineEffectiveRoute(
              data,
            ) ?? selected;

          updatePersistedLlm(
            nextPersisted,
          );
          updateEffectiveLlm(
            nextEffective,
          );

          const selectedProviderCatalog =
            llmCatalogRef.current?.providers.find(
              (item) =>
                item.id
                  .trim()
                  .toLowerCase() ===
                nextEffective.provider
                  .trim()
                  .toLowerCase(),
            );

          const servedModels =
            selectedProviderCatalog?.chat_models;

          const selectedModelIsRuntimeServed =
            Array.isArray(
              servedModels,
            ) &&
            servedModels.some(
              (item) =>
                item.id ===
                nextEffective.model,
            );

          if (
            !STRICT_RUNTIME_PROVIDERS.has(
              nextEffective.provider
                .trim()
                .toLowerCase(),
            ) ||
            selectedModelIsRuntimeServed
          ) {
            if (
              !llmEnginesRef.current.some(
                (engine) =>
                  engine.provider ===
                    nextEffective.provider &&
                  engine.model ===
                    nextEffective.model,
              )
            ) {
              updateLlmEngines([
                ...llmEnginesRef.current,
                {
                  provider:
                    nextEffective.provider,
                  model:
                    nextEffective.model,
                  label: `${nextEffective.provider} / ${nextEffective.model}`,
                },
              ]);
            }
          } else {
            /**
             * A strict-provider POST proves that the persisted/config
             * transition was accepted; it does not prove /models discovery.
             * Never manufacture a selectable strict engine here.
             */
            updateLlmEngines(
              llmEnginesRef.current.filter(
                (engine) =>
                  !(
                    engine.provider ===
                      nextEffective.provider &&
                    engine.model ===
                      nextEffective.model
                  ),
              ),
            );
          }

          if (
            "deployment" in data
          ) {
            updateLlmDeployment(
              data.deployment ?? null,
            );
          }

          setLlmError(null);
          setLlmStatus("ready");
          llmNeedsRefreshRef.current = false;
          setLlmRefreshing(false);

          return true;
        } catch (error) {
          const message =
            error instanceof Error
              ? error.message
              : "LLMエンジン変更に失敗しました";

          setLlmChangeError(
            message,
          );
          toast.error(message);
          return false;
        } finally {
          setLlmChanging(false);
        }
      },
      [
        llmChanging,
        updateEffectiveLlm,
        updateLlmDeployment,
        updateLlmEngines,
        updatePersistedLlm,
      ],
    );

  const changeRuntimeFeature =
    useCallback(
      async (
        feature: string,
        enabled: boolean,
      ) => {
        if (
          runtimeFeatureMutationRef.current
        ) {
          return false;
        }

        runtimeFeatureMutationRef.current =
          true;

        try {
          const response =
            await fetch(
              "/api/python-proxy/runtime/features",
              {
                method: "PATCH",
                credentials:
                  "include",
                headers: {
                  "Content-Type":
                    "application/json",
                },
                body: JSON.stringify({
                  feature,
                  enabled,
                }),
              },
            );

          if (!response.ok) {
            toast.error(
              await responseErrorMessage(
                response,
                "ランタイム機能変更に失敗しました",
              ),
            );
            return false;
          }

          setRuntimeFeatures(
            await response.json(),
          );

          window.setTimeout(
            () =>
              void refreshRuntimeFeatures(),
            1500,
          );

          return true;
        } catch (error) {
          console.error(
            "ランタイム機能変更失敗:",
            error,
          );

          toast.error(
            error instanceof Error
              ? error.message
              : "ランタイム機能変更に失敗しました",
          );

          return false;
        } finally {
          runtimeFeatureMutationRef.current =
            false;
        }
      },
      [refreshRuntimeFeatures],
    );

  const changeRuntimeFeatures =
    useCallback(
      async (
        features: Record<
          string,
          boolean
        >,
      ) => {
        if (
          runtimeFeatureMutationRef.current
        ) {
          return false;
        }

        runtimeFeatureMutationRef.current =
          true;

        try {
          const response =
            await fetch(
              "/api/python-proxy/runtime/features",
              {
                method: "PATCH",
                credentials:
                  "include",
                headers: {
                  "Content-Type":
                    "application/json",
                },
                body: JSON.stringify({
                  features,
                }),
              },
            );

          if (!response.ok) {
            toast.error(
              await responseErrorMessage(
                response,
                "ランタイム機能変更に失敗しました",
              ),
            );
            return false;
          }

          setRuntimeFeatures(
            await response.json(),
          );

          window.setTimeout(
            () =>
              void refreshRuntimeFeatures(),
            1500,
          );

          return true;
        } catch (error) {
          console.error(
            "ランタイム機能変更失敗:",
            error,
          );

          toast.error(
            error instanceof Error
              ? error.message
              : "ランタイム機能変更に失敗しました",
          );

          return false;
        } finally {
          runtimeFeatureMutationRef.current =
            false;
        }
      },
      [refreshRuntimeFeatures],
    );

  const voiceStatus =
    useVoiceStatus(
      isConnected &&
        runtimeFeatures?.features
          ?.local_mic === true,
    );

  const value =
    useMemo<RuntimeContextValue>(
      () => ({
        isConnected,

        characters,
        currentCharacter,
        characterStatus,
        characterError,
        characterRefreshing,
        refreshCharacters,
        changeCharacter,
        characterChanging,

        llmEngines,
        currentLlm,
        persistedLlm,
        effectiveLlm,
        llmStatus,
        llmError,
        llmRefreshing,
        refreshLlm,
        changeLlmEngine,
        llmCatalog,

        runtimeFeatures,
        changeRuntimeFeature,
        changeRuntimeFeatures,

        llmDeployment,
        llmChangeError,
        llmChanging,

        voiceStatus,
      }),
      [
        isConnected,

        characters,
        currentCharacter,
        characterStatus,
        characterError,
        characterRefreshing,
        refreshCharacters,
        changeCharacter,
        characterChanging,

        llmEngines,
        currentLlm,
        persistedLlm,
        effectiveLlm,
        llmStatus,
        llmError,
        llmRefreshing,
        refreshLlm,
        changeLlmEngine,
        llmCatalog,

        runtimeFeatures,
        changeRuntimeFeature,
        changeRuntimeFeatures,

        llmDeployment,
        llmChangeError,
        llmChanging,

        voiceStatus,
      ],
    );

  return (
    <RuntimeContext.Provider
      value={value}
    >
      {children}
    </RuntimeContext.Provider>
  );
}

export function useRuntimeContext(): RuntimeContextValue {
  const value = useContext(RuntimeContext);
  if (!value) {
    throw new Error("useRuntimeContext must be used within RuntimeProvider");
  }
  return value;
}

export function useOptionalRuntimeContext(): RuntimeContextValue | null {
  return useContext(RuntimeContext);
}

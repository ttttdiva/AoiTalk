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
import type { LlmDeploymentMetadata } from "@/lib/llm-provider-visibility";
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

export type RuntimeFeatureState = {
  features: Record<string, boolean>;
  discord_bot_service?: {
    state?: "stopped" | "starting" | "running" | "stopping" | "failed";
    user?: string | null;
    guild_count?: number;
    task_running?: boolean;
    last_error?: string | null;
  };
};

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
  llmEngines: RuntimeLlmEngine[];
  currentLlm: { provider: string; model: string } | null;
  /** LLM情報の取得状態。ready は engine/catalog の少なくとも一方が正常応答した状態。 */
  llmStatus?: "loading" | "ready" | "error";
  llmError?: string | null;
  llmRefreshing?: boolean;
  /** LLM情報だけを再取得する導線（文字・featureの状態は変更しない）。 */
  refreshLlm?: () => Promise<void>;
  changeLlmEngine: (provider: string, model: string) => Promise<boolean>;
  llmCatalog: LlmModelCatalogResponse | null;
  runtimeFeatures: RuntimeFeatureState | null;
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
  available?: RuntimeLlmEngine[];
  deployment?: LlmDeploymentMetadata | null;
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
  return new URLSearchParams(window.location.search).get("s") || "";
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

function mergeEngineCatalog(
  engines: RuntimeLlmEngine[],
  catalog: LlmModelCatalogResponse | null,
): RuntimeLlmEngine[] {
  if (!catalog?.providers?.length) return engines;
  const metadataByKey = new Map<string, Partial<RuntimeLlmEngine>>();
  for (const provider of catalog.providers) {
    for (const model of provider.models ?? []) {
      const key = `${provider.id}::${model.id}`;
      metadataByKey.set(key, {
        label: model.label || `${provider.label} / ${model.id}`,
        available: provider.available,
        disabled: provider.disabled,
        unavailable: provider.unavailable,
        availability_reason: provider.availability_reason,
        reasoning_effort_options: model.reasoning_effort_options,
        context_window_tokens: model.context_window_tokens,
        supports_reasoning: model.supports_reasoning,
      });
    }
  }
  // /llm/engine が返すcompact候補だけを選択肢とし、catalogは一致する
  // provider/modelの付加情報だけを補う。設定画面用の全catalogをここで
  // 新規候補化すると、チャットのモデル一覧に未選択モデルが大量表示される。
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

function currentEngineFallback(
  current: { provider: string; model: string } | null,
  engines: RuntimeLlmEngine[],
  catalog: LlmModelCatalogResponse | null,
): RuntimeLlmEngine[] {
  if (!current) return [];
  const existing = engines.find(
    (engine) =>
      engine.provider === current.provider && engine.model === current.model,
  );
  return mergeEngineCatalog(
    [
      existing ?? {
        provider: current.provider,
        model: current.model,
        label: `${current.provider} / ${current.model}`,
      },
    ],
    catalog,
  );
}

function lastKnownEnginesFallback(
  current: { provider: string; model: string } | null,
  engines: RuntimeLlmEngine[],
  catalog: LlmModelCatalogResponse | null,
): RuntimeLlmEngine[] {
  if (engines.length > 0) return mergeEngineCatalog(engines, catalog);
  return currentEngineFallback(current, engines, catalog);
}

function catalogHasUsableData(catalog: LlmModelCatalogResponse | null): boolean {
  if (!catalog) return false;
  const current = catalog.current;
  return Boolean(
    (typeof current?.provider === "string" && current.provider) &&
      (typeof current?.model === "string" && current.model),
  ) || catalog.providers.some((provider) => (provider.models ?? []).length > 0);
}

function hasUsableLlmSnapshot(
  current: { provider: string; model: string } | null,
  engines: RuntimeLlmEngine[],
  catalog: LlmModelCatalogResponse | null,
): boolean {
  return Boolean(current) || engines.length > 0 || catalogHasUsableData(catalog);
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

export function RuntimeProvider({ children }: { children: ReactNode }) {
  const { updateSession } = useChatSessions();
  const [isConnected, setIsConnected] = useState(false);
  const [characters, setCharacters] = useState<CharacterOption[]>([]);
  const [currentCharacter, setCurrentCharacter] = useState("");
  const [characterChanging, setCharacterChanging] = useState(false);
  const [llmEngines, setLlmEngines] = useState<RuntimeLlmEngine[]>([]);
  const [currentLlm, setCurrentLlm] = useState<{
    provider: string;
    model: string;
  } | null>(null);
  const [llmStatus, setLlmStatus] = useState<
    "loading" | "ready" | "error"
  >("loading");
  const [llmError, setLlmError] = useState<string | null>(null);
  const [llmRefreshing, setLlmRefreshing] = useState(false);
  const [llmCatalog, setLlmCatalog] = useState<LlmModelCatalogResponse | null>(
    null,
  );
  const [llmDeployment, setLlmDeployment] =
    useState<LlmDeploymentMetadata | null>(null);
  const [llmChangeError, setLlmChangeError] = useState<string | null>(null);
  const [llmChanging, setLlmChanging] = useState(false);
  const [runtimeFeatures, setRuntimeFeatures] =
    useState<RuntimeFeatureState | null>(null);
  const characterMutationRef = useRef(false);
  const runtimeFeatureMutationRef = useRef(false);
  const llmRequestSeqRef = useRef(0);
  // Mutations advance the epoch after the POST is accepted.  Every GET
  // captures the epoch it started in, so an older response cannot undo a
  // successful engine switch even if its request sequence is still current.
  const llmEpochRef = useRef(0);
  const llmEnginesRef = useRef<RuntimeLlmEngine[]>([]);
  const currentLlmRef = useRef<{ provider: string; model: string } | null>(null);
  const llmCatalogRef = useRef<LlmModelCatalogResponse | null>(null);

  const updateLlmEngines = useCallback((next: RuntimeLlmEngine[]) => {
    llmEnginesRef.current = next;
    setLlmEngines(next);
  }, []);

  const updateCurrentLlm = useCallback(
    (next: { provider: string; model: string } | null) => {
      currentLlmRef.current = next;
      setCurrentLlm(next);
    },
    [],
  );

  const refreshRuntimeFeatures = useCallback(async () => {
    try {
      const response = await fetch("/api/python-proxy/runtime/features", {
        credentials: "include",
        signal: timeoutSignal(3000),
      });
      if (!response.ok) return null;
      const data = (await response.json()) as RuntimeFeatureState;
      setRuntimeFeatures(data);
      return data;
    } catch {
      return null;
    }
  }, []);

  const fetchExtras = useCallback(async () => {
    // Characters/features/catalog are auxiliary sources.  Do not make the
    // fast LLM path wait for their Promise.allSettled completion: current and
    // compact engines become visible as soon as /llm/engine is usable.
    const requestId = ++llmRequestSeqRef.current;
    const epoch = llmEpochRef.current;
    const isLatestRequest = () =>
      requestId === llmRequestSeqRef.current && epoch === llmEpochRef.current;
    type EndpointStatus = "pending" | "success" | "error";
    const attempt = {
      engine: {
        status: "pending" as EndpointStatus,
        hasModels: false,
        current: null as { provider: string; model: string } | null,
      },
      catalog: {
        status: "pending" as EndpointStatus,
        hasModels: false,
        current: null as { provider: string; model: string } | null,
      },
      errors: [] as string[],
    };
    setLlmRefreshing(true);

    const commitReady = () => {
      if (!isLatestRequest()) return;
      if (
        hasUsableLlmSnapshot(
          currentLlmRef.current,
          llmEnginesRef.current,
          llmCatalogRef.current,
        )
      ) {
        setLlmError(null);
        setLlmStatus("ready");
      }
    };

    const runIndependent = async (
      label: string,
      action: () => Promise<void>,
      onError: () => void,
      recordError = true,
    ) => {
      try {
        await action();
      } catch (error) {
        if (isLatestRequest()) {
          if (recordError) {
            attempt.errors.push(
              error instanceof Error ? error.message : `${label}取得に失敗しました`,
            );
          }
          onError();
        }
      }
    };

    const charactersTask = runIndependent(
      "キャラクター",
      async () => {
        const response = await fetch("/api/python-proxy/characters", {
          credentials: "include",
          signal: timeoutSignal(3000),
        });
        if (!response.ok) throw new Error(`キャラクター取得に失敗しました (${response.status})`);
        const data = await response.json();
        if (!isLatestRequest()) return;
        const options = normalizeCharacterOptions(data);
        setCharacters(options);
        setCurrentCharacter(resolveCurrentCharacterSlug(options, data.current));
      },
      () => undefined,
      false,
    );

    const runtimeTask = runIndependent(
      "Runtime feature",
      async () => {
        const response = await fetch("/api/python-proxy/runtime/features", {
          credentials: "include",
          signal: timeoutSignal(3000),
        });
        if (!response.ok) throw new Error(`Runtime feature取得に失敗しました (${response.status})`);
        const data = (await response.json()) as RuntimeFeatureState;
        if (!isLatestRequest()) return;
        setRuntimeFeatures(data);
      },
      () => undefined,
      false,
    );

    const engineTask = runIndependent(
      "LLM engine",
      async () => {
        const response = await fetch("/api/python-proxy/llm/engine", {
          credentials: "include",
          signal: timeoutSignal(3000),
        });
        if (!response.ok) throw new Error(`LLM engine取得に失敗しました (${response.status})`);
        const parsed = (await response.json()) as LlmEngineResponse;
        if (!parsed || typeof parsed !== "object") {
          throw new Error("LLM engine取得に失敗しました (invalid response)");
        }
        if (!isLatestRequest()) return;
        const available = Array.isArray(parsed.available) ? parsed.available : [];
        attempt.engine.status = "success";
        attempt.engine.hasModels = available.length > 0;
        attempt.engine.current =
          typeof parsed.provider === "string" && parsed.provider &&
          typeof parsed.model === "string" && parsed.model
            ? { provider: parsed.provider, model: parsed.model }
            : null;
        if (available.length > 0) {
          updateLlmEngines(mergeEngineCatalog(available, llmCatalogRef.current));
        } else if (attempt.engine.current) {
          updateLlmEngines(
            lastKnownEnginesFallback(
              attempt.engine.current,
              llmEnginesRef.current,
              llmCatalogRef.current,
            ),
          );
        }
        if (attempt.engine.current) updateCurrentLlm(attempt.engine.current);
        if (parsed.deployment !== undefined) setLlmDeployment(parsed.deployment ?? null);
        // This is intentionally before characters/features/catalog settle.
        commitReady();
      },
      () => {
        attempt.engine.status = "error";
      },
    );

    const catalogTask = runIndependent(
      "LLM catalog",
      async () => {
        const response = await fetch("/api/python-proxy/llm/models", {
          credentials: "include",
          signal: timeoutSignal(3000),
        });
        if (!response.ok) throw new Error(`LLM catalog取得に失敗しました (${response.status})`);
        const parsed = (await response.json()) as LlmModelCatalogResponse;
        if (!parsed || !Array.isArray(parsed.providers)) {
          throw new Error("LLM catalog取得に失敗しました (invalid response)");
        }
        if (!isLatestRequest()) return;
        attempt.catalog.status = "success";
        attempt.catalog.hasModels = parsed.providers.some(
          (provider) => (provider.models ?? []).length > 0,
        );
        attempt.catalog.current =
          typeof parsed.current?.provider === "string" && parsed.current.provider &&
          typeof parsed.current?.model === "string" && parsed.current.model
            ? parsed.current
            : null;
        llmCatalogRef.current = parsed;
        setLlmCatalog(parsed);
        if (attempt.engine.status !== "success" && attempt.catalog.current) {
          updateCurrentLlm(attempt.catalog.current);
        }
        if (llmEnginesRef.current.length > 0) {
          updateLlmEngines(mergeEngineCatalog(llmEnginesRef.current, parsed));
        } else if (attempt.catalog.current) {
          updateLlmEngines(
            currentEngineFallback(
              attempt.catalog.current,
              llmEnginesRef.current,
              parsed,
            ),
          );
        }
        if (parsed.deployment !== undefined) setLlmDeployment(parsed.deployment ?? null);
        commitReady();
      },
      () => {
        attempt.catalog.status = "error";
      },
    );

    await Promise.allSettled([charactersTask, engineTask, runtimeTask, catalogTask]);
    if (!isLatestRequest()) return;

    const confirmedEmpty =
      attempt.engine.status === "success" &&
      attempt.catalog.status === "success" &&
      !attempt.engine.hasModels &&
      !attempt.catalog.hasModels &&
      !attempt.engine.current &&
      !attempt.catalog.current;
    if (confirmedEmpty) {
      // Only two valid, explicit empty responses may clear LKG data.  A
      // timeout/500 on either side is never evidence that the collection is
      // empty.
      updateLlmEngines([]);
      updateCurrentLlm(null);
      setLlmError(null);
      setLlmStatus("ready");
    } else if (
      hasUsableLlmSnapshot(
        currentLlmRef.current,
        llmEnginesRef.current,
        llmCatalogRef.current,
      )
    ) {
      setLlmError(null);
      setLlmStatus("ready");
    } else {
      setLlmError(attempt.errors.join(" / ") || "LLM情報を取得できませんでした");
      setLlmStatus("error");
    }
    setLlmRefreshing(false);
  }, [updateCurrentLlm, updateLlmEngines]);

  const refreshLlm = useCallback(async () => {
    await fetchExtras();
  }, [fetchExtras]);

  useEffect(() => {
    let mounted = true;
    let initialCheck = true;
    let healthRetryCount = 0;
    let healthRetryTimer: number | null = null;
    const maxHealthRetries = 2;
    const check = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      const bootstrap = initialCheck;
      initialCheck = false;
      // Start the LLM path concurrently with health.  A slow/unavailable
      // health probe must not serialize model-name rendering.
      if (bootstrap) void fetchExtras();
      try {
        const response = await fetch("/api/python-proxy/health", {
          credentials: "include",
          signal: timeoutSignal(3000),
        });
        const connected = response.ok;
        if (!mounted) return;
        setIsConnected(connected);
        if (connected) {
          healthRetryCount = 0;
          // Health is a connectivity indicator, not an LLM bootstrap gate.
          // The initial LLM request is also started when health is down.
          if (!bootstrap) void fetchExtras();
        } else if (healthRetryCount < maxHealthRetries) {
          const delay = 250 * 2 ** healthRetryCount;
          healthRetryCount += 1;
          healthRetryTimer = window.setTimeout(() => void check(), delay);
        }
      } catch {
        if (!mounted) return;
        setIsConnected(false);
        if (healthRetryCount < maxHealthRetries) {
          const delay = 250 * 2 ** healthRetryCount;
          healthRetryCount += 1;
          healthRetryTimer = window.setTimeout(() => void check(), delay);
        }
      }
    };
    void check();
    const interval = setInterval(() => void check(), 15000);
    const onVisibility = () => {
      if (typeof document !== "undefined" && !document.hidden) void check();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      mounted = false;
      clearInterval(interval);
      if (healthRetryTimer !== null) window.clearTimeout(healthRetryTimer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [fetchExtras]);

  const changeCharacter = useCallback(
    async (slug: string, sessionId?: string | null) => {
      if (characterMutationRef.current) return false;
      characterMutationRef.current = true;
      setCharacterChanging(true);
      const activeSessionId = sessionId || sessionIdFromLocation();
      const query = activeSessionId
        ? `?session_id=${encodeURIComponent(activeSessionId)}`
        : "";
      try {
        const response = await fetch(
          `/api/python-proxy/character/${encodeURIComponent(slug)}${query}`,
          { method: "POST", credentials: "include" },
        );
        if (!response.ok) {
          toast.error(
            await responseErrorMessage(response, "キャラクター変更に失敗しました"),
          );
          return false;
        }
        const data = await response.json().catch(() => ({}));
        const selected =
          typeof data?.character_slug === "string" && data.character_slug.trim()
            ? data.character_slug.trim()
            : slug;
        setCurrentCharacter(selected);
        if (activeSessionId) {
          updateSession(activeSessionId, (session) => ({
            ...session,
            character_name: selected,
          }));
        }
        return true;
      } catch (error) {
        console.error("キャラクター変更失敗:", error);
        toast.error(
          error instanceof Error
            ? error.message
            : "キャラクター変更に失敗しました",
        );
        return false;
      } finally {
        characterMutationRef.current = false;
        setCharacterChanging(false);
      }
    },
    [updateSession],
  );

  const changeLlmEngine = useCallback(
    async (provider: string, model: string) => {
      if (!provider || !model || llmChanging) return false;
      setLlmChanging(true);
      setLlmChangeError(null);
      try {
        const response = await fetch("/api/python-proxy/llm/engine", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider, model }),
        });
        const data = (await response.json().catch(() => ({}))) as LlmEngineResponse;
        if (!response.ok || data.success === false) {
          const detail =
            typeof data.detail === "string"
              ? data.detail
              : typeof data.message === "string"
                ? data.message
                : typeof data.error === "string"
                  ? data.error
                  : `LLMエンジン変更に失敗しました (${response.status})`;
          setLlmChangeError(detail);
          toast.error(detail);
          return false;
        }
        // A successful mutation is authoritative.  Invalidate every GET that
        // started before the POST completed before publishing the new current
        // selection, preventing an old response from rolling it back.
        llmEpochRef.current += 1;
        llmRequestSeqRef.current += 1;
        const selected = {
          provider: typeof data.provider === "string" ? data.provider : provider,
          model: typeof data.model === "string" ? data.model : model,
        };
        updateCurrentLlm(selected);
        if (
          !llmEnginesRef.current.some(
            (engine) =>
              engine.provider === selected.provider && engine.model === selected.model,
          )
        ) {
          updateLlmEngines([
            ...llmEnginesRef.current,
            {
              provider: selected.provider,
              model: selected.model,
              label: `${selected.provider} / ${selected.model}`,
            },
          ]);
        }
        if ("deployment" in data) setLlmDeployment(data.deployment ?? null);
        setLlmError(null);
        setLlmStatus("ready");
        setLlmRefreshing(false);
        return true;
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "LLMエンジン変更に失敗しました";
        setLlmChangeError(message);
        toast.error(message);
        return false;
      } finally {
        setLlmChanging(false);
      }
    },
    [llmChanging, updateCurrentLlm, updateLlmEngines],
  );

  const changeRuntimeFeature = useCallback(
    async (feature: string, enabled: boolean) => {
      if (runtimeFeatureMutationRef.current) return false;
      runtimeFeatureMutationRef.current = true;
      try {
        const response = await fetch("/api/python-proxy/runtime/features", {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ feature, enabled }),
        });
        if (!response.ok) {
          toast.error(
            await responseErrorMessage(
              response,
              "ランタイム機能変更に失敗しました",
            ),
          );
          return false;
        }
        setRuntimeFeatures(await response.json());
        window.setTimeout(() => void refreshRuntimeFeatures(), 1500);
        return true;
      } catch (error) {
        console.error("ランタイム機能変更失敗:", error);
        toast.error(
          error instanceof Error
            ? error.message
            : "ランタイム機能変更に失敗しました",
        );
        return false;
      } finally {
        runtimeFeatureMutationRef.current = false;
      }
    },
    [refreshRuntimeFeatures],
  );

  const changeRuntimeFeatures = useCallback(
    async (features: Record<string, boolean>) => {
      if (runtimeFeatureMutationRef.current) return false;
      runtimeFeatureMutationRef.current = true;
      try {
        const response = await fetch("/api/python-proxy/runtime/features", {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ features }),
        });
        if (!response.ok) {
          toast.error(
            await responseErrorMessage(
              response,
              "ランタイム機能変更に失敗しました",
            ),
          );
          return false;
        }
        setRuntimeFeatures(await response.json());
        window.setTimeout(() => void refreshRuntimeFeatures(), 1500);
        return true;
      } catch (error) {
        console.error("ランタイム機能変更失敗:", error);
        toast.error(
          error instanceof Error
            ? error.message
            : "ランタイム機能変更に失敗しました",
        );
        return false;
      } finally {
        runtimeFeatureMutationRef.current = false;
      }
    },
    [refreshRuntimeFeatures],
  );

  // Voice status is only useful (and safe to poll) when the local microphone
  // feature is explicitly enabled. RuntimeUtilityPanel still receives the
  // latest status for diagnostics, but disabled/unknown feature state stops
  // the global 15s poll entirely.
  const voiceStatus = useVoiceStatus(
    isConnected && runtimeFeatures?.features?.local_mic === true,
  );
  const value = useMemo<RuntimeContextValue>(
    () => ({
      isConnected,
      characters,
      currentCharacter,
      changeCharacter,
      characterChanging,
      llmEngines,
      currentLlm,
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
      changeCharacter,
      characterChanging,
      llmEngines,
      currentLlm,
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

  return <RuntimeContext.Provider value={value}>{children}</RuntimeContext.Provider>;
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

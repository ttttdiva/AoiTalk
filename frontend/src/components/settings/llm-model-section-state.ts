"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { useConfirm } from "@/hooks/use-confirm";
import {
  filterAvailableProviders,
  isProviderAvailable,
  resolveEffectiveProviderId,
} from "@/lib/llm-provider-visibility";
import {
  buildClassDraft, buildProjectAutomationDraft, canonicalAgentTeamConfig, defaultModeForOptions, modelOptionSettings,
  providerSelection, pyFetch, reasoningEffortOptionsForModel, MODEL_PAGE_SIZE,
  CONNECTION_SETTINGS_PROVIDERS, REASONING_EFFORT_PROVIDERS,
  type LlmEngineResponse, type LlmModelCatalogResponse,
  type AgentTeamConfig,
  type LlmProviderCatalog, type ModelClassDraft, type ModelRouteSettings,
  type OllamaDeleteResponse, type OllamaPullTask, type ProviderDraft, type ProviderSettingsDraft,
  type SettingsPayload, type SpeechRecognitionSettings, type MageVLSettings,
  type ExternalModelPrivacySettings,
  type CloudAdvisorSettings,
  type LlamaCppSettingsDraft,
  type LlamaCppModelRootState,
  type LlamaCppRuntimeProfile,
  type LocalRuntimeStatus, type LocalRuntimeTask,
  llamaCppDraftFromSettings, llamaCppPayloadFromDraft, llamaCppBaseUrlFromPayload,
  llamaCppRuntimeProfileForModel, managedLocalRuntimeForSelection,
  cloudAdvisorProviderOptions,
} from "./llm-model-section-types";

const UNSUPPORTED_CLIP_INGEST_PROVIDERS = new Set(["claude", "grok"]);

export type RoutingSaveScope =
  | "all"
  | "vision"
  | "audio"
  | "video"
  | "clip_ingest"
  | "project_automation"
  | "agent";

function settingsReasoningEffortOptions(
  provider: LlmProviderCatalog | null | undefined,
  modelId: string,
): string[] {
  // The generic local-model endpoint still uses its fast/thinking response
  // mode.  Only profile metadata (model or provider settings) opts a local
  // model into the managed Qwen3.8 effort selector and save payload.
  if (provider?.id === "openai_compatible_local") {
    const model = provider.models.find((item) => item.id === modelId);
    const modelHasProfileContract = Boolean(
      model?.reasoning_effort_default && model?.reasoning_effort_wire,
    );
    if (modelHasProfileContract && model?.reasoning_effort_options?.length) {
      return model.reasoning_effort_options;
    }
    const settings = provider.settings;
    const settingsHaveProfileContract = Boolean(
      settings?.reasoning_effort_default && settings?.reasoning_effort_wire,
    );
    return settingsHaveProfileContract ? settings?.reasoning_effort_options ?? [] : [];
  }
  return reasoningEffortOptionsForModel(provider, modelId);
}

export function buildClipIngestDraft(
  route: (ModelRouteSettings & { base_url?: string; api_key?: string }) | undefined,
  providers: LlmProviderCatalog[] | undefined,
): ModelClassDraft {
  if (UNSUPPORTED_CLIP_INGEST_PROVIDERS.has(route?.provider ?? "")) {
    return { ...buildClassDraft(undefined, providers), inherit: true };
  }
  return {
    ...buildClassDraft(route, providers),
    inherit: route?.inherit ?? !(route?.provider || route?.model),
  };
}

export function useLlmModelSection() {
  const confirm = useConfirm();
  const [expanded, setExpanded] = useState(false);
  const [catalog, setCatalog] = useState<LlmModelCatalogResponse | null>(null);
  const [provider, setProvider] = useState("gemini");
  const [model, setModel] = useState("");
  const [customModel, setCustomModel] = useState("");
  const [providerDrafts, setProviderDrafts] = useState<Record<string, ProviderDraft>>({});
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [engineChangeError, setEngineChangeError] = useState<string | null>(null);
  const [localRuntimeStatus, setLocalRuntimeStatus] = useState<LocalRuntimeStatus | null>(null);
  const [localRuntimeTask, setLocalRuntimeTask] = useState<LocalRuntimeTask | null>(null);
  const [localRuntimeLoading, setLocalRuntimeLoading] = useState(false);
  const [localRuntimePreparing, setLocalRuntimePreparing] = useState(false);
  const localRuntimePollSeqRef = useRef(0);
  const localRuntimePrepareRef = useRef<number | null>(null);
  const localRuntimeActivationRef = useRef<string | null>(null);
  const [pulling, setPulling] = useState(false);
  const [pullInput, setPullInput] = useState("gpt-oss:20b");
  const [task, setTask] = useState<OllamaPullTask | null>(null);
  const [deletingModel, setDeletingModel] = useState<string | null>(null);
  const [modelSearch, setModelSearch] = useState("");
  const [modelPage, setModelPage] = useState(1);
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState("medium");
  const [llamaCppDraft, setLlamaCppDraft] = useState<LlamaCppSettingsDraft>(() =>
    llamaCppDraftFromSettings(),
  );
  const [llamaCppError, setLlamaCppError] = useState<string | null>(null);
  const [llamaCppModelRootState, setLlamaCppModelRootState] = useState<LlamaCppModelRootState | null>(null);
  const [llamaCppModelRootLoading, setLlamaCppModelRootLoading] = useState(false);
  const [llamaCppModelRootSaving, setLlamaCppModelRootSaving] = useState(false);
  const [llamaCppModelRootError, setLlamaCppModelRootError] = useState<string | null>(null);
  const [delegationEnabled, setDelegationEnabled] = useState(false);
  const [orchestrationMode, setOrchestrationMode] = useState<"standard" | "director">("standard");
  const [chatgptWeb, setChatgptWeb] = useState({
    profile_dir: "",
    response_timeout_seconds: 900,
    max_rounds_per_turn: 20,
  });
  const [externalPrivacy, setExternalPrivacy] = useState<ExternalModelPrivacySettings>({
    mode: "direct", review_policy: "high_risk", notify: true, semantic_redaction_enabled: true,
    local_provider: "openai_compatible_local", local_model: "", redaction_terms: [],
    trusted_local_hosts: [], raw_media_policy: "block", cache_enabled: true,
  });
  const [cloudAdvisor, setCloudAdvisor] = useState<CloudAdvisorSettings>({
    mode: "disabled",
    provider: "openai",
    model: "",
    reasoning_effort: "high",
  });
  const [cloudAdvisorProviderIds, setCloudAdvisorProviderIds] = useState<string[]>([
    "openai",
    "deepinfra",
  ]);
  const [imageMode, setImageMode] = useState<"auto" | "always" | "off">("auto");
  const [videoMode, setVideoMode] = useState<"auto" | "off">("auto");
  const [routingDetailsOpen, setRoutingDetailsOpen] = useState(false);
  const [modelTab, setModelTab] = useState<"language" | "vision" | "audio" | "video" | "clip_ingest" | "project_automation">("language");
  const [speechRecognition, setSpeechRecognition] = useState<SpeechRecognitionSettings>({});
  const [mageVl, setMageVl] = useState<MageVLSettings>({
    enabled: true,
    managed: true,
    preload_on_start: false,
    model: "microsoft/Mage-VL",
    base_url: "http://127.0.0.1:30000/v1",
    max_video_bytes: 50 * 1024 * 1024,
    max_video_duration_seconds: 300,
    video_backend: "frames",
    codec_engine: "traditional",
    num_frames: 32,
    max_pixels: 150000,
    max_new_tokens: 256,
  });
  const [classDrafts, setClassDrafts] = useState<Record<"vision" | "audio" | "video" | "clip_ingest", ModelClassDraft>>({
    vision: { ...buildClassDraft(undefined, undefined), inherit: true },
    audio: { ...buildClassDraft(undefined, undefined), engine: "speech_recognition" },
    video: { ...buildClassDraft(undefined, undefined), provider: "mage_vl", model: "microsoft/Mage-VL", inherit: false },
    clip_ingest: { ...buildClassDraft(undefined, undefined), inherit: true },
  });
  const [projectAutomationDraft, setProjectAutomationDraft] = useState<ModelClassDraft>(
    () => buildProjectAutomationDraft(undefined, undefined),
  );
  const [projectAutomationProviderIds, setProjectAutomationProviderIds] = useState<string[] | null>(null);
  const [projectAutomationPersistedProvider, setProjectAutomationPersistedProvider] = useState("");
  const [savingRouting, setSavingRouting] = useState(false);
  const [agentTeamConfig, setAgentTeamConfig] = useState<AgentTeamConfig | null>(null);

  const providerOptions = useMemo(
    () =>
      filterAvailableProviders(
        catalog?.providers,
        catalog?.deployment,
        (item) => item.id,
        (item) => item,
      ),
    [catalog],
  );
  const selectedProvider = useMemo(
    () => providerOptions.find((item) => item.id === provider) ?? null,
    [providerOptions, provider],
  );
  const selectedModelId = customModel.trim() || model;
  const selectedModel = useMemo(
    () => selectedProvider?.models.find((item) => item.id === selectedModelId) ?? null,
    [selectedModelId, selectedProvider],
  );
  const selectedManagedRuntime = useMemo(
    () => managedLocalRuntimeForSelection(selectedProvider, selectedModel, selectedModelId),
    [selectedModel, selectedModelId, selectedProvider],
  );
  const selectedRuntimeProfile: LlamaCppRuntimeProfile | null = useMemo(
    () => {
      const providerSettings = selectedProvider?.settings;
      const runtimeSettings = providerSettings?.llama_cpp
        ?? providerSettings?.runtime_settings
        ?? (providerSettings?.runtime_profile
          ? { runtime_profile: providerSettings.runtime_profile }
          : undefined);
      return llamaCppRuntimeProfileForModel(
        selectedModel,
        runtimeSettings,
        selectedModelId,
      );
    },
    [selectedModel, selectedModelId, selectedProvider],
  );
  const current = catalog?.current;
  const visionProvider = useMemo(
    () => providerOptions.find((item) => item.id === classDrafts.vision.provider),
    [providerOptions, classDrafts.vision.provider],
  );
  const audioProvider = useMemo(
    () => providerOptions.find((item) => item.id === classDrafts.audio.provider),
    [providerOptions, classDrafts.audio.provider],
  );
  const clipIngestProvider = useMemo(
    () => providerOptions.find((item) => item.id === classDrafts.clip_ingest.provider),
    [providerOptions, classDrafts.clip_ingest.provider],
  );
  // backendの専用client factoryが生成できるproviderだけを候補にする。
  const clipIngestProviders = useMemo(
    () =>
      providerOptions.filter(
        (item) =>
          item.selection_kind !== "routing_profile" &&
          !UNSUPPORTED_CLIP_INGEST_PROVIDERS.has(item.id),
      ),
    [providerOptions],
  );
  const projectAutomationProviders = useMemo(() => {
    const allowed = projectAutomationProviderIds === null
      ? null
      : new Set(projectAutomationProviderIds);
    return providerOptions.filter(
      (item) =>
        item.selection_kind !== "routing_profile" &&
        (allowed === null || allowed.has(item.id)),
    );
  }, [projectAutomationProviderIds, providerOptions]);
  const speechEngine = speechRecognition.current_engine || "whisper";
  const speechModel = speechRecognition.engines?.[speechEngine]?.model || speechEngine;
  const audioSource = classDrafts.audio.engine === "speech_recognition"
    ? "local-stt"
    : classDrafts.audio.engine === "off"
      ? "off"
      : classDrafts.audio.inherit
        ? "inherit"
        : classDrafts.audio.provider;
  const mediaProviders = useCallback(
    (kind: "image" | "audio") =>
      providerOptions.filter((providerItem) =>
        providerItem.models.some((item) => item.media?.[kind]),
      ),
    [providerOptions],
  );

  const providerModels = useMemo(
    () => selectedProvider?.models ?? [],
    [selectedProvider],
  );
  const filteredModels = useMemo(() => {
    const query = modelSearch.trim().toLowerCase();
    if (!query) return providerModels;

    return providerModels.filter((item) => {
      const searchable = [
        item.id,
        item.label,
        item.description,
        item.source,
        item.source_label,
        item.base_url,
        item.server_label,
        item.details?.family,
        item.details?.parameter_size,
        item.details?.quantization_level,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return searchable.includes(query);
    });
  }, [modelSearch, providerModels]);
  const totalModelPages = Math.max(1, Math.ceil(filteredModels.length / MODEL_PAGE_SIZE));
  const currentModelPage = Math.min(modelPage, totalModelPages);
  const modelPageStart = (currentModelPage - 1) * MODEL_PAGE_SIZE;
  const visibleModels = filteredModels.slice(modelPageStart, modelPageStart + MODEL_PAGE_SIZE);

  const loadCatalog = useCallback(async (refresh = false, refreshProvider?: string) => {
    if (refresh) setRefreshing(true);
    else setLoading(true);
    try {
      const params = new URLSearchParams();
      if (refresh) params.set("refresh", "true");
      if (refreshProvider) params.set("provider", refreshProvider);
      const query = params.toString();
      const data = await pyFetch<LlmModelCatalogResponse>(
        `/llm/models${query ? `?${query}` : ""}`,
      );
      setCatalog(data);
      const effectiveProvider = resolveEffectiveProviderId(data.deployment);
      const preferredProvider =
        effectiveProvider &&
        isProviderAvailable(
          effectiveProvider,
          data.deployment,
          data.providers.find((item) => item.id === effectiveProvider),
        )
          ? effectiveProvider
          : data.current.provider;
      setProvider((current) => {
        if (refreshProvider) return current;
        if (catalog) return current;
        return preferredProvider || current;
      });
      if (!refreshProvider) {
        const currentProvider = data.providers.find(
          (item) => item.id === preferredProvider,
        );
        const selection = providerSelection(currentProvider);
        setModel(selection.model);
        setCustomModel(selection.customModel);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "モデル一覧を取得できませんでした");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [catalog]);

  useEffect(() => {
    if (expanded && !catalog) void loadCatalog(false);
  }, [catalog, expanded, loadCatalog]);

  // A fixed Enterprise deployment may intentionally leave the persisted
  // provider unavailable. Select the effective provider instead of leaving a
  // controlled select with a value that is not present in its options.
  useEffect(() => {
    if (!catalog || providerOptions.length === 0) return;
    if (providerOptions.some((item) => item.id === provider)) return;

    const effectiveProvider = resolveEffectiveProviderId(catalog.deployment);
    const fallback =
      (effectiveProvider && providerOptions.some((item) => item.id === effectiveProvider)
        ? effectiveProvider
        : providerOptions[0]?.id) || "";
    if (!fallback || fallback === provider) return;
    const selection = providerSelection(
      providerOptions.find((item) => item.id === fallback),
    );
    setProvider(fallback);
    setModel(selection.model);
    setCustomModel(selection.customModel);
  }, [catalog, provider, providerOptions]);

  useEffect(() => {
    setModelPage(1);
  }, [provider, modelSearch]);

  useEffect(() => {
    if (modelPage > totalModelPages) setModelPage(totalModelPages);
  }, [modelPage, totalModelPages]);

  useEffect(() => {
    const settings = selectedProvider?.settings;
    const effortOptions = settingsReasoningEffortOptions(selectedProvider, selectedModelId);
    const selectedOption = selectedProvider?.models.find((item) => item.id === selectedModelId);
    const configuredEffort = settings?.reasoning_effort
      ?? selectedOption?.reasoning_effort_default
      ?? settings?.reasoning_effort_default
      ?? "medium";
    setBaseUrl(settings?.base_url ?? "");
    setApiKey("");
    setReasoningEffort(
      effortOptions.length
        ? defaultModeForOptions(effortOptions, configuredEffort)
        : configuredEffort,
    );
    if (provider === "openai_compatible_local") {
      const nextDraft = llamaCppDraftFromSettings(
        settings?.llama_cpp ?? settings?.runtime_settings,
        selectedModelId,
        selectedRuntimeProfile,
      );
      // The dedicated resolver is the source of truth for the editable
      // override.  Do not hydrate an environment-derived effective path into
      // the input, otherwise saving an unrelated runtime field would silently
      // persist a machine-specific path.
      if (llamaCppModelRootState) {
        nextDraft.model_root = llamaCppModelRootState.model_root_override ?? "";
      }
      setLlamaCppDraft(
        nextDraft,
      );
      setLlamaCppError(null);
    }
  }, [llamaCppModelRootState, provider, selectedModelId, selectedProvider, selectedRuntimeProfile]);

  useEffect(() => {
    if (!expanded || provider !== "openai_compatible_local") return;
    let active = true;
    setLlamaCppModelRootLoading(true);
    setLlamaCppModelRootError(null);
    void pyFetch<LlamaCppModelRootState>("/llm/llama-cpp/model-root")
      .then((state) => {
        if (!active) return;
        setLlamaCppModelRootState(state);
        setLlamaCppDraft((current) => ({
          ...current,
          model_root: state.model_root_override ?? "",
        }));
      })
      .catch((error) => {
        if (!active) return;
        setLlamaCppDraft((current) => ({
          ...current,
          model_root: "",
        }));
        const message = error instanceof Error
          ? error.message
          : "llama.cpp model rootを取得できませんでした";
        setLlamaCppModelRootError(message);
      })
      .finally(() => {
        if (active) setLlamaCppModelRootLoading(false);
      });
    return () => {
      active = false;
    };
  }, [expanded, provider]);

  useEffect(() => {
    if (provider === "openai_compatible_local" && selectedModel?.base_url) {
      setBaseUrl((current) => current || selectedModel.base_url || "");
    }
  }, [provider, selectedModel?.base_url]);

  useEffect(() => {
    if (!selectedProvider || selectedProvider.models.some((item) => item.id === model)) {
      return;
    }
    if (
      selectedProvider.configured_model?.trim() === model ||
      (current?.provider === provider && current.model === model)
    ) {
      return;
    }
    setModel(selectedProvider.models[0]?.id ?? "");
  }, [current, model, provider, selectedProvider]);

  useEffect(() => {
    const sequence = ++localRuntimePollSeqRef.current;
    localRuntimePrepareRef.current = null;
    localRuntimeActivationRef.current = null;
    setLocalRuntimeTask(null);
    setLocalRuntimeStatus(null);
    setLocalRuntimePreparing(false);

    if (!selectedManagedRuntime || !selectedModelId.trim()) {
      setLocalRuntimeStatus(null);
      setLocalRuntimeLoading(false);
      return;
    }

    setLocalRuntimeLoading(true);
    const params = new URLSearchParams({ model: selectedModelId.trim() });
    void pyFetch<LocalRuntimeStatus>(`/llm/local-runtime/status?${params.toString()}`)
      .then((status) => {
        if (localRuntimePollSeqRef.current !== sequence) return;
        setLocalRuntimeStatus(status);
      })
      .catch((error) => {
        if (localRuntimePollSeqRef.current !== sequence) return;
        const message = error instanceof Error
          ? error.message
          : "ローカルruntimeの状態を取得できませんでした";
        setLocalRuntimeStatus(null);
        setEngineChangeError(message);
        toast.error(message);
      })
      .finally(() => {
        if (localRuntimePollSeqRef.current === sequence) {
          setLocalRuntimeLoading(false);
        }
      });

    return () => {
      if (localRuntimePollSeqRef.current === sequence) {
        localRuntimePollSeqRef.current += 1;
      }
    };
  }, [selectedManagedRuntime, selectedModelId]);

  useEffect(() => {
    if (!task || task.done) return;
    const interval = window.setInterval(async () => {
      try {
        const next = await pyFetch<OllamaPullTask>(
          `/ollama/pull/${encodeURIComponent(task.task_id)}`,
        );
        setTask(next);
        if (next.done) {
          setPulling(false);
          if (next.error) toast.error(next.error);
          else {
            toast.success(`${next.model} をダウンロードしました`);
            void loadCatalog(false);
          }
        }
      } catch (error) {
        setPulling(false);
        toast.error(error instanceof Error ? error.message : "Ollama pull 状態を取得できませんでした");
      }
    }, 1000);
    return () => window.clearInterval(interval);
  }, [loadCatalog, task]);

  const saveModelSelection = useCallback(async (
    nextProvider: string,
    nextModel: string,
    settings?: ProviderSettingsDraft,
  ) => {
    const trimmedModel = nextModel.trim();
    if (!nextProvider || !trimmedModel) return;
    setSaving(true);
    setEngineChangeError(null);
    try {
      const payload: Record<string, unknown> = {
        provider: nextProvider,
        model: trimmedModel,
      };
      if (settings) {
        if (settings.base_url !== undefined) payload.base_url = settings.base_url;
        if (settings.api_key?.trim()) payload.api_key = settings.api_key.trim();
        if (settings.reasoning_effort !== undefined) {
          payload.reasoning_effort = settings.reasoning_effort;
        }
        if (settings.llama_cpp !== undefined) {
          payload.llama_cpp = settings.llama_cpp;
        }
      }
      const data = await pyFetch<LlmEngineResponse>("/llm/engine", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setProvider(data.provider);
      setModel(data.model);
      setCustomModel("");
      setProviderDrafts((current) => ({
        ...current,
        [data.provider]: { model: data.model, customModel: "" },
      }));
      setCatalog((currentCatalog) => currentCatalog
        ? {
          ...currentCatalog,
          current: { provider: data.provider, model: data.model },
          ...(Object.prototype.hasOwnProperty.call(data, "deployment")
            ? { deployment: data.deployment ?? null }
            : {}),
          providers: currentCatalog.providers.map((item) => item.id === data.provider
            ? {
              ...item,
              settings: settings
                ? {
                  ...item.settings,
                  ...(settings.reasoning_effort !== undefined
                    ? { reasoning_effort: settings.reasoning_effort }
                    : {}),
                  ...(settings.base_url !== undefined
                    ? { base_url: settings.base_url }
                    : {}),
                  ...(settings.llama_cpp !== undefined
                    ? {
                      llama_cpp: {
                        ...item.settings?.llama_cpp,
                        ...settings.llama_cpp,
                        ...(data.llama_cpp ?? {}),
                        // The API persists the canonical readiness_timeout
                        // key while the catalog exposes both spellings.
                        ...(settings.llama_cpp.readiness_timeout !== undefined
                          ? { readiness_timeout_seconds: settings.llama_cpp.readiness_timeout }
                          : {}),
                      },
                    }
                    : {}),
                }
                : item.settings,
              configured_model: data.model,
              models: item.models.some((option) => option.id === data.model)
                ? item.models
                : [
                  {
                    id: data.model,
                    label: data.model,
                    custom_current: true,
                    source: "provider-configured",
                    source_label: "現在の設定",
                  },
                  ...item.models,
                ],
            }
            : item),
        }
        : currentCatalog);
      toast.success(data.message || "言語モデルを保存しました");
    } catch (error) {
      const message = error instanceof Error ? error.message : "言語モデルを保存できませんでした";
      setEngineChangeError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, []);

  useEffect(() => {
    const targetModel = selectedModelId.trim();
    const statusRuntime = String(localRuntimeStatus?.runtime ?? "").trim().toLowerCase();
    const targetKey = selectedManagedRuntime && targetModel
      ? `${selectedManagedRuntime}:${targetModel}`
      : "";
    const statusMatchesSelection = Boolean(
      targetKey
        && localRuntimeStatus?.prepared
        && localRuntimeStatus.model.trim() === targetModel
        && statusRuntime === selectedManagedRuntime,
    );
    if (!statusMatchesSelection) return;

    if (
      current?.provider === "openai_compatible_local"
      && current.model === targetModel
    ) {
      localRuntimeActivationRef.current = null;
      return;
    }
    if (
      saving
      || localRuntimePreparing
      || localRuntimeActivationRef.current === targetKey
    ) {
      return;
    }

    localRuntimeActivationRef.current = targetKey;
    const targetOption = selectedProvider?.models.find((item) => item.id === targetModel);
    const effortOptions = settingsReasoningEffortOptions(selectedProvider, targetModel);
    const selectedEffort = effortOptions.length
      ? defaultModeForOptions(
        effortOptions,
        targetOption?.reasoning_effort_default
          ?? selectedProvider?.settings?.reasoning_effort_default
          ?? reasoningEffort,
      )
      : "";
    void saveModelSelection("openai_compatible_local", targetModel, {
      ...(modelOptionSettings(targetOption) ?? {}),
      ...(selectedEffort ? { reasoning_effort: selectedEffort } : {}),
    });
  }, [
    current,
    localRuntimePreparing,
    localRuntimeStatus,
    reasoningEffort,
    saveModelSelection,
    saving,
    selectedManagedRuntime,
    selectedModelId,
    selectedProvider,
  ]);

  const startLocalRuntimePrepare = useCallback(async () => {
    const targetModel = selectedModelId.trim();
    const runtime = selectedManagedRuntime;
    if (
      !targetModel
      || !runtime
      || saving
      || localRuntimePrepareRef.current !== null
    ) return;

    if (
      localRuntimeStatus?.prepared
      && localRuntimeStatus.model.trim() === targetModel
      && String(localRuntimeStatus.runtime ?? "").trim().toLowerCase() === runtime
    ) {
      localRuntimeActivationRef.current = null;
      setLocalRuntimeStatus({ ...localRuntimeStatus });
      return;
    }

    const sequence = ++localRuntimePollSeqRef.current;
    localRuntimePrepareRef.current = sequence;
    setLocalRuntimePreparing(true);
    setLocalRuntimeLoading(false);
    setEngineChangeError(null);

    const statusPath = `/llm/local-runtime/status?${new URLSearchParams({
      model: targetModel,
    }).toString()}`;
    const isCurrentSequence = () => localRuntimePollSeqRef.current === sequence;

    try {
      let status = localRuntimeStatus;
      if (
        !status
        || status.model !== targetModel
        || status.runtime !== runtime
      ) {
        status = await pyFetch<LocalRuntimeStatus>(statusPath);
        if (!isCurrentSequence()) return;
        setLocalRuntimeStatus(status);
      }

      if (!status.prepared) {
        if (!(status.actions.prepare || status.actions.retry)) {
          throw new Error(status.reason || "このモデルは自動準備できません");
        }
        let nextTask = await pyFetch<LocalRuntimeTask>("/llm/local-runtime/prepare", {
          method: "POST",
          body: JSON.stringify({ model: targetModel, runtime }),
        });
        if (!isCurrentSequence()) return;
        setLocalRuntimeTask(nextTask);

        while (!nextTask.done) {
          if (!nextTask.task_id) {
            throw new Error("ローカルruntime準備タスクIDを取得できませんでした");
          }
          await new Promise<void>((resolve) => window.setTimeout(resolve, 1000));
          if (!isCurrentSequence()) return;
          nextTask = await pyFetch<LocalRuntimeTask>(
            `/llm/local-runtime/tasks/${encodeURIComponent(nextTask.task_id)}`,
          );
          if (!isCurrentSequence()) return;
          setLocalRuntimeTask(nextTask);
        }

        if (nextTask.error || nextTask.status === "failed") {
          throw new Error(
            nextTask.reason
              || nextTask.error
              || "ローカルruntimeの準備に失敗しました",
          );
        }

        status = await pyFetch<LocalRuntimeStatus>(statusPath);
        if (!isCurrentSequence()) return;
        setLocalRuntimeStatus(status);
      }

      if (!status.prepared) {
        throw new Error(status.reason || "ローカルruntimeの準備が完了していません");
      }
    } catch (error) {
      if (!isCurrentSequence()) return;
      const message = error instanceof Error
        ? error.message
        : "ローカルruntimeの準備に失敗しました";
      setEngineChangeError(message);
      toast.error(message);
    } finally {
      if (localRuntimePrepareRef.current === sequence) {
        localRuntimePrepareRef.current = null;
        setLocalRuntimePreparing(false);
      }
    }
  }, [
    localRuntimeStatus,
    saving,
    selectedManagedRuntime,
    selectedModelId,
  ]);

  const handleProviderChange = useCallback(
    (nextProvider: string) => {
      setProvider(nextProvider);
      const next = providerOptions.find((item) => item.id === nextProvider);
      const selection = providerDrafts[nextProvider] ?? providerSelection(next);
      const nextModel = selection.customModel.trim() || selection.model;
      const nextOption = selection.customModel.trim()
        ? null
        : next?.models.find((item) => item.id === selection.model);
      const nextModelId = selection.customModel.trim() || selection.model;
      const nextEffortOptions = settingsReasoningEffortOptions(next, nextModelId);
      const nextEffort = nextEffortOptions.length
        ? defaultModeForOptions(
          nextEffortOptions,
          nextOption?.reasoning_effort_default
            ?? next?.settings?.reasoning_effort_default
            ?? next?.settings?.reasoning_effort
            ?? "medium",
        )
        : "";
      const nextSettings = {
        ...(modelOptionSettings(nextOption) ?? {}),
        ...(nextEffort ? { reasoning_effort: nextEffort } : {}),
      };
      if (nextSettings?.base_url) setBaseUrl(nextSettings.base_url);
      if (nextEffort) setReasoningEffort(nextEffort);
      setModel(selection.model);
      setCustomModel(selection.customModel);
      if (managedLocalRuntimeForSelection(next, nextOption, nextModelId)) {
        setEngineChangeError(null);
        return;
      }
      void saveModelSelection(nextProvider, nextModel, nextSettings);
    },
    [providerDrafts, providerOptions, saveModelSelection],
  );

  const handleModelChange = useCallback(
    (nextModel: string) => {
      setModel(nextModel);
      setCustomModel("");
      setProviderDrafts((current) => ({
        ...current,
        [provider]: { model: nextModel, customModel: "" },
      }));
      const nextOption = selectedProvider?.models.find((item) => item.id === nextModel);
      const nextEffortOptions = settingsReasoningEffortOptions(selectedProvider, nextModel);
      const nextOptionDefault = selectedProvider?.models.find((item) => item.id === nextModel)
        ?.reasoning_effort_default
        ?? selectedProvider?.settings?.reasoning_effort_default;
      const nextEffort = nextEffortOptions.length
        ? defaultModeForOptions(nextEffortOptions, nextOptionDefault ?? reasoningEffort)
        : "";
      const nextSettings = {
        ...(modelOptionSettings(nextOption) ?? {}),
        ...(nextEffort ? { reasoning_effort: nextEffort } : {}),
      };
      if (nextSettings?.base_url) setBaseUrl(nextSettings.base_url);
      if (nextEffort) setReasoningEffort(nextEffort);
      if (managedLocalRuntimeForSelection(selectedProvider, nextOption, nextModel)) {
        setEngineChangeError(null);
        return;
      }
      void saveModelSelection(provider, nextModel, nextSettings);
    },
    [provider, reasoningEffort, saveModelSelection, selectedProvider],
  );

  const handleCustomModelChange = useCallback(
    (nextModel: string) => {
      setCustomModel(nextModel);
      setProviderDrafts((current) => ({
        ...current,
        [provider]: { model, customModel: nextModel },
      }));
    },
    [model, provider],
  );

  const handleCustomModelConfirm = useCallback(() => {
    const nextModel = customModel.trim();
    if (!nextModel) return;
    const nextOption = selectedProvider?.models.find((item) => item.id === nextModel);
    if (managedLocalRuntimeForSelection(selectedProvider, nextOption, nextModel)) {
      setEngineChangeError(null);
      return;
    }
    void saveModelSelection(provider, nextModel);
  }, [customModel, provider, saveModelSelection, selectedProvider]);

  const handleProviderSettingsSave = useCallback(() => {
    void saveModelSelection(provider, selectedModelId, {
      base_url: baseUrl.trim(),
      api_key: apiKey,
      reasoning_effort: reasoningEffort,
    });
  }, [
    apiKey,
    baseUrl,
    provider,
    reasoningEffort,
    saveModelSelection,
    selectedModelId,
  ]);

  const handleLlamaCppSettingsSave = useCallback(() => {
    try {
      const profileAlias = selectedRuntimeProfile?.served_alias?.trim() || "";
      const aliasLocked = selectedRuntimeProfile?.alias_locked === true && Boolean(profileAlias);
      const draftForSave = {
        ...(aliasLocked
          ? { ...llamaCppDraft, model_alias: profileAlias }
          : llamaCppDraft),
        // MTP is profile-owned.  Never send stale MTP controls for an
        // external/local model or a legacy profile without an MTP contract.
        ...(selectedRuntimeProfile?.mtp ? {} : { mtp_enabled: undefined }),
      };
      const llamaCpp = llamaCppPayloadFromDraft(draftForSave);
      const selectedModelKey = selectedModelId.trim().toLowerCase();
      const managedRuntime = Boolean(llamaCpp.model_path)
        && (llamaCpp.auto_start || llamaCpp.model_alias.trim().toLowerCase() === selectedModelKey);
      if (
        managedRuntime
        && !aliasLocked
        && llamaCpp.model_alias.trim().toLowerCase() !== selectedModelKey
      ) {
        throw new Error("managed llama.cppではmodel aliasを選択中モデルと一致させてください");
      }
      const profileRuntime = String(selectedRuntimeProfile?.runtime ?? "")
        .trim()
        .toLowerCase()
        .replace(".", "_") === "llama_cpp";
      const shouldPersistRuntime = profileRuntime || managedRuntime;
      setLlamaCppError(null);
      const settings: ProviderSettingsDraft = {
        // llama.cpp owns its endpoint: keep the existing connection controls
        // intact for other providers, but derive this payload's URL from the
        // same host/port sent to the runtime so the UI/backend round-trip is
        // immediately consistent (including IPv6 bracket notation).
        base_url: shouldPersistRuntime
          ? llamaCppBaseUrlFromPayload(llamaCpp)
          : baseUrl.trim(),
        api_key: apiKey,
        reasoning_effort: reasoningEffort,
      };
      // Do not declare a llama.cpp runtime for an external local-model that
      // has no GGUF path. This keeps its existing custom Base URL and avoids
      // the backend interpreting default runtime settings as a managed launch.
      if (shouldPersistRuntime) settings.llama_cpp = llamaCpp;
      void saveModelSelection(provider, selectedModelId, settings);
    } catch (error) {
      const message = error instanceof Error ? error.message : "llama.cpp設定を確認してください";
      setLlamaCppError(message);
      toast.error(message);
    }
  }, [
    apiKey,
    baseUrl,
    llamaCppDraft,
    provider,
    reasoningEffort,
    saveModelSelection,
    selectedModelId,
    selectedRuntimeProfile,
  ]);

  const saveLlamaCppModelRoot = useCallback(async (nextRoot?: string) => {
    const modelRoot = (nextRoot ?? llamaCppDraft.model_root ?? "").trim();
    setLlamaCppModelRootSaving(true);
    setLlamaCppModelRootError(null);
    try {
      const state = await pyFetch<LlamaCppModelRootState>("/llm/llama-cpp/model-root", {
        method: "PUT",
        body: JSON.stringify({ model_root: modelRoot }),
      });
      setLlamaCppModelRootState(state);
      setLlamaCppDraft((current) => ({
        ...current,
        model_root: state.model_root_override ?? "",
      }));
      toast.success(modelRoot ? "llama.cpp model rootを保存しました" : "llama.cpp model rootを既定値へ戻しました");
    } catch (error) {
      const message = error instanceof Error
        ? error.message
        : "llama.cpp model rootを保存できませんでした";
      setLlamaCppModelRootError(message);
      toast.error(message);
    } finally {
      setLlamaCppModelRootSaving(false);
    }
  }, [llamaCppDraft.model_root]);

  const startPull = useCallback(async () => {
    const nextModel = pullInput.trim();
    if (!nextModel) return;
    setPulling(true);
    try {
      const started = await pyFetch<OllamaPullTask>("/ollama/pull", {
        method: "POST",
        body: JSON.stringify({ model: nextModel }),
      });
      setTask(started);
      toast.success(`${nextModel} のダウンロードを開始しました`);
    } catch (error) {
      setPulling(false);
      toast.error(error instanceof Error ? error.message : "Ollama pull を開始できませんでした");
    }
  }, [pullInput]);

  const percent = Math.max(0, Math.min(100, task?.percent ?? 0));
  const hasProviderSettings = Boolean(selectedProvider?.settings && Object.keys(selectedProvider.settings).length > 0);
  const showConnectionSettings = CONNECTION_SETTINGS_PROVIDERS.has(provider);
  const selectedReasoningEffortOptions = settingsReasoningEffortOptions(
    selectedProvider,
    selectedModelId,
  );
  // Managed Qwen3.8 local profiles opt into the catalog-driven effort
  // selector.  Generic local-model remains on its existing fast/thinking
  // response-mode control because it has no catalog effort options.
  const showReasoningEffort = REASONING_EFFORT_PROVIDERS.has(provider)
    || (provider === "openai_compatible_local" && selectedReasoningEffortOptions.length > 0);

  const loadAgentTeamSettings = useCallback(async () => {
    try {
      const [data, teamResponse] = await Promise.all([
        pyFetch<SettingsPayload>("/settings"),
        pyFetch<{ agent_team?: AgentTeamConfig }>("/agent-team/config"),
      ]);
      const team = teamResponse.agent_team ?? (
        data.settings?.agent_team?.schema_version === 3
          ? data.settings.agent_team as AgentTeamConfig
          : undefined
      );
      const routing = data.settings?.model_routing;
      const projectAutomationRoute = routing?.classes?.project_automation;
      const projectAutomationProviderSchema =
        data.schema?.["model_routing.classes.project_automation.provider"];
      setProjectAutomationProviderIds(
        Array.isArray(projectAutomationProviderSchema?.values)
          ? projectAutomationProviderSchema.values
            .map((item) => String(item || "").trim())
            .filter(Boolean)
          : null,
      );
      setOrchestrationMode(team?.orchestration_mode ?? "standard");
      setChatgptWeb({
        profile_dir: data.settings?.chatgpt_web?.profile_dir ?? "",
        response_timeout_seconds: data.settings?.chatgpt_web?.response_timeout_seconds ?? 900,
        max_rounds_per_turn: data.settings?.chatgpt_web?.max_rounds_per_turn ?? 20,
      });
      setDelegationEnabled(team?.delegation_enabled ?? false);
      if (team?.schema_version === 3) setAgentTeamConfig(canonicalAgentTeamConfig(team));
      setExternalPrivacy(data.settings?.external_model_privacy ?? {});
      const loadedCloudAdvisor = data.settings?.cloud_advisor ?? {};
      const loadedCloudAdvisorProviders = cloudAdvisorProviderOptions(
        loadedCloudAdvisor,
        data.schema?.["cloud_advisor.provider"],
      );
      const loadedCloudAdvisorProvider = String(loadedCloudAdvisor.provider ?? "")
        .trim()
        .toLowerCase();
      const normalizedCloudAdvisorProvider = loadedCloudAdvisorProviders.some(
        (item) => item.id === loadedCloudAdvisorProvider,
      )
        ? loadedCloudAdvisorProvider
        : loadedCloudAdvisorProviders[0]?.id ?? "openai";
      setCloudAdvisor((current) => ({
        ...current,
        ...loadedCloudAdvisor,
        mode: loadedCloudAdvisor.mode ?? current.mode ?? "disabled",
        provider: normalizedCloudAdvisorProvider,
        model: loadedCloudAdvisor.model ?? current.model ?? "",
        reasoning_effort: loadedCloudAdvisor.reasoning_effort
          ?? current.reasoning_effort
          ?? "high",
      }));
      setCloudAdvisorProviderIds(loadedCloudAdvisorProviders.map((item) => item.id));
      setImageMode(routing?.media?.image_mode ?? "auto");
      setVideoMode(routing?.media?.video_mode ?? "auto");
      const loadedMageVl = data.settings?.mage_vl ?? {};
      setMageVl((current) => ({ ...current, ...loadedMageVl, api_key: "" }));
      setSpeechRecognition(data.settings?.speech_recognition ?? {});
      setClassDrafts({
        vision: {
          ...buildClassDraft(routing?.classes?.vision, providerOptions),
          inherit: routing?.classes?.vision?.inherit ?? !(
            routing?.classes?.vision?.provider || routing?.classes?.vision?.model
          ),
        },
        audio: {
          ...buildClassDraft(routing?.classes?.audio, providerOptions),
          engine: routing?.classes?.audio?.engine ?? "speech_recognition",
        },
        video: {
          ...buildClassDraft(routing?.classes?.video, providerOptions),
          provider: routing?.classes?.video?.provider || "mage_vl",
          model: routing?.classes?.video?.model || loadedMageVl.model || "microsoft/Mage-VL",
          baseUrl: routing?.classes?.video?.base_url || loadedMageVl.base_url || "",
          inherit: false,
        },
        clip_ingest: buildClipIngestDraft(
          routing?.classes?.clip_ingest,
          providerOptions,
        ),
      });
      setProjectAutomationDraft(
        buildProjectAutomationDraft(
          projectAutomationRoute,
          providerOptions,
        ),
      );
      setProjectAutomationPersistedProvider(
        projectAutomationRoute?.provider || "",
      );
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Agent Team設定を取得できませんでした");
    }
  }, [providerOptions]);

  useEffect(() => {
    if (expanded) void loadAgentTeamSettings();
  }, [expanded, loadAgentTeamSettings]);

  const deleteOllamaModel = useCallback(async (modelId: string) => {
    if (!modelId || deletingModel) return;
    const isCurrent = current?.provider === "ollama" && current.model === modelId;
    const message = isCurrent
      ? `${modelId} is the current Ollama model. Delete it anyway?`
      : `Delete ${modelId} from Ollama?`;
    if (!(await confirm({ description: message, destructive: true }))) return;

    setDeletingModel(modelId);
    try {
      await pyFetch<OllamaDeleteResponse>("/ollama/models", {
        method: "DELETE",
        body: JSON.stringify({ model: modelId }),
      });
      toast.success(`${modelId} deleted`);
      if (model === modelId) setModel("");
      await loadCatalog(false, "ollama");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to delete Ollama model");
    } finally {
      setDeletingModel(null);
    }
  }, [current, deletingModel, loadCatalog, model, confirm]);

  const saveExternalPrivacySettings = useCallback(async () => {
    setSavingRouting(true);
    try {
      for (const [field, value] of Object.entries(externalPrivacy)) {
        await pyFetch("/settings", {
          method: "PATCH",
          body: JSON.stringify({
            key: `external_model_privacy.${field}`,
            value,
          }),
        });
      }
      toast.success("外部送信設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "外部送信設定を保存できませんでした");
    } finally {
      setSavingRouting(false);
    }
  }, [externalPrivacy]);

  const saveCloudAdvisorSettings = useCallback(async () => {
    setSavingRouting(true);
    try {
      const saveSetting = async (key: string, value: unknown) => {
        await pyFetch("/settings", {
          method: "PATCH",
          body: JSON.stringify({ key, value }),
        });
      };
      await saveSetting("cloud_advisor.mode", cloudAdvisor.mode ?? "disabled");
      await saveSetting("cloud_advisor.provider", cloudAdvisor.provider ?? "openai");
      await saveSetting("cloud_advisor.model", cloudAdvisor.model?.trim() ?? "");
      await saveSetting(
        "cloud_advisor.reasoning_effort",
        cloudAdvisor.reasoning_effort ?? "high",
      );
      toast.success("Cloud Advisor設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Cloud Advisor設定を保存できませんでした");
    } finally {
      setSavingRouting(false);
    }
  }, [cloudAdvisor]);

  const saveRoutingSettings = useCallback(async (scope: RoutingSaveScope = "all") => {
    setSavingRouting(true);
    try {
      const saveSetting = async (key: string, value: unknown) => {
        await pyFetch("/settings", {
          method: "PATCH",
          body: JSON.stringify({ key, value }),
        });
      };
      const saveClass = async (classKey: "vision" | "audio" | "clip_ingest") => {
        const draft = classDrafts[classKey];
        const targetModel = (draft.customModel.trim() || draft.model).trim();
        await saveSetting(
          `model_routing.classes.${classKey}.inherit`,
          draft.inherit ?? false,
        );
        if (classKey === "audio") {
          await saveSetting(
            "model_routing.classes.audio.engine",
            draft.engine ?? "speech_recognition",
          );
        }
        const effortFields: (readonly [string, string])[] = [
          ["reasoning_effort", draft.mode],
          ["mode", draft.mode],
        ];
        for (const [field, value] of [
          ["provider", draft.provider],
          ["model", targetModel],
          ["base_url", draft.baseUrl.trim()],
          ...effortFields,
        ] as const) {
          await saveSetting(`model_routing.classes.${classKey}.${field}`, value);
        }
        if (draft.apiKey.trim()) {
          await saveSetting(
            `model_routing.classes.${classKey}.api_key`,
            draft.apiKey.trim(),
          );
        }
      };

      const saveProjectAutomationRoute = async () => {
        const prefix = "model_routing.classes.project_automation";
        const draft = projectAutomationDraft;
        const inherit = draft.inherit ?? true;
        const targetModel = (draft.customModel.trim() || draft.model).trim();
        const providerChanged =
          draft.provider !== projectAutomationPersistedProvider;

        if (!inherit) {
          if (providerChanged) {
            // A provider switch must not leave a transient mixed route such as
            // new-provider + old-model/base-url. Move execution to the inherited
            // Base Model before changing any provider-owned dedicated fields.
            await saveSetting(`${prefix}.mode`, "");
            await saveSetting(`${prefix}.inherit`, true);

            // A route-level credential belongs to the persisted provider.
            // Clear it only after the old dedicated route is no longer active.
            await saveSetting(`${prefix}.api_key`, "");
          }

          // While the current route is still inherited, dedicated fields can
          // be populated without changing effective routing. Populate them
          // first so flipping inherit=false never exposes a partial target.
          await saveSetting(`${prefix}.provider`, draft.provider);
          await saveSetting(`${prefix}.model`, targetModel);
          await saveSetting(`${prefix}.base_url`, draft.baseUrl.trim());
          await saveSetting(`${prefix}.reasoning_effort`, draft.mode.trim());
          if (draft.apiKey.trim()) {
            await saveSetting(`${prefix}.api_key`, draft.apiKey.trim());
          }
        }

        // The backend accepts an empty mode and then derives inherit/dedicated
        // from the boolean. Neutralize a previously explicit mode before
        // changing inherit, avoiding an invalid intermediate mode/inherit pair.
        await saveSetting(`${prefix}.mode`, "");
        await saveSetting(`${prefix}.inherit`, inherit);
        if (!inherit) {
          setProjectAutomationPersistedProvider(draft.provider);
        }
      };

      if (scope === "all" || scope === "vision") {
        await saveSetting("model_routing.media.image_mode", imageMode);
        await saveClass("vision");
      }
      if (scope === "all" || scope === "audio") {
        await saveClass("audio");
      }
      if (scope === "all" || scope === "clip_ingest") {
        await saveClass("clip_ingest");
      }
      if (scope === "all" || scope === "project_automation") {
        await saveProjectAutomationRoute();
      }

      if (scope === "all" || scope === "video") {
        const videoDraft = classDrafts.video;
        const videoModel = (videoDraft.customModel.trim() || videoDraft.model).trim();
        for (const [field, value] of [
          ["inherit", false],
          ["provider", videoDraft.provider || "mage_vl"],
          ["model", videoModel || mageVl.model || "microsoft/Mage-VL"],
          ["base_url", videoDraft.baseUrl.trim() || mageVl.base_url || ""],
          ["mode", videoDraft.mode],
        ] as const) {
          await saveSetting(`model_routing.classes.video.${field}`, value);
        }
        if (videoDraft.apiKey.trim()) {
          await saveSetting(
            "model_routing.classes.video.api_key",
            videoDraft.apiKey.trim(),
          );
        }
        const mageFields: Array<[string, unknown]> = [
          ["enabled", mageVl.enabled !== false],
          ["managed", mageVl.managed !== false],
          ["preload_on_start", mageVl.preload_on_start === true],
          ["model", videoModel || mageVl.model || "microsoft/Mage-VL"],
          ["base_url", videoDraft.baseUrl.trim() || mageVl.base_url || ""],
          ["startup_timeout_seconds", mageVl.startup_timeout_seconds ?? 300],
          ["max_video_bytes", mageVl.max_video_bytes ?? 50 * 1024 * 1024],
          ["max_video_duration_seconds", mageVl.max_video_duration_seconds ?? 300],
          ["video_backend", "frames"],
          ["codec_engine", mageVl.codec_engine ?? "traditional"],
          ["num_frames", mageVl.num_frames ?? 32],
          ["max_pixels", mageVl.max_pixels ?? 150000],
          ["max_new_tokens", mageVl.max_new_tokens ?? 256],
        ];
        for (const [field, value] of mageFields) {
          await saveSetting(`mage_vl.${field}`, value);
        }
        await saveSetting("mage_vl.server_command", mageVl.server_command?.trim() ?? "");
        await saveSetting("model_routing.media.video_mode", videoMode);
      }

      if (scope === "all" || scope === "agent") {
        for (const [key, value] of Object.entries(chatgptWeb)) {
          await saveSetting(`chatgpt_web.${key}`, value);
        }
      }
      toast.success("モデル・メディア設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Agent Team設定を保存できませんでした");
    } finally {
      setSavingRouting(false);
    }
  }, [
    chatgptWeb,
    classDrafts,
    imageMode,
    mageVl,
    projectAutomationDraft,
    projectAutomationPersistedProvider,
    videoMode,
  ]);

  return {
    expanded, setExpanded, catalog, setCatalog, provider, setProvider, model, setModel,
    customModel, setCustomModel, providerDrafts, setProviderDrafts, loading, setLoading,
    refreshing, setRefreshing, saving, setSaving, pulling, setPulling, pullInput, setPullInput,
    engineChangeError,
    localRuntimeStatus, localRuntimeTask, localRuntimeLoading, localRuntimePreparing,
    startLocalRuntimePrepare,
    task, setTask, deletingModel, setDeletingModel, modelSearch, setModelSearch, modelPage, setModelPage,
    baseUrl, setBaseUrl, apiKey, setApiKey, reasoningEffort, setReasoningEffort,
    llamaCppDraft, setLlamaCppDraft, llamaCppError,
    llamaCppModelRootState, llamaCppModelRootLoading, llamaCppModelRootSaving,
    llamaCppModelRootError, saveLlamaCppModelRoot,
    delegationEnabled, setDelegationEnabled, orchestrationMode, setOrchestrationMode,
    chatgptWeb, setChatgptWeb,
    externalPrivacy, setExternalPrivacy,
    cloudAdvisor, setCloudAdvisor, cloudAdvisorProviderIds,
    imageMode, setImageMode, videoMode, setVideoMode, mageVl, setMageVl, routingDetailsOpen, setRoutingDetailsOpen, modelTab, setModelTab,
    speechRecognition, setSpeechRecognition, classDrafts, setClassDrafts,
    projectAutomationDraft, setProjectAutomationDraft, projectAutomationProviders,
    agentTeamConfig, setAgentTeamConfig,
    savingRouting, setSavingRouting, selectedProvider, selectedModelId, selectedModel, current,
    providerOptions,
    visionProvider, audioProvider, clipIngestProvider, clipIngestProviders,
    speechEngine, speechModel, audioSource, mediaProviders,
    providerModels, filteredModels, totalModelPages, currentModelPage, modelPageStart, visibleModels,
    loadCatalog, saveModelSelection, handleProviderChange, handleModelChange, handleCustomModelChange,
    handleCustomModelConfirm, handleProviderSettingsSave, handleLlamaCppSettingsSave, startPull, percent, hasProviderSettings,
    showConnectionSettings, showReasoningEffort,
    loadAgentTeamSettings,
    deleteOllamaModel, saveRoutingSettings, saveExternalPrivacySettings,
    saveCloudAdvisorSettings,
  };
}

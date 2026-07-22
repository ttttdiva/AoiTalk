"use client";

import { useCallback, useEffect, useMemo, useState, type DragEvent } from "react";
import { toast } from "sonner";
import { useConfirm } from "@/hooks/use-confirm";
import {
  buildClassDraft, buildRouteDrafts, defaultModeForOptions, modelOptionSettings,
  providerSelection, pyFetch, routeSelection, MODEL_PAGE_SIZE, MODEL_ROUTE_DEFINITIONS,
  CONNECTION_SETTINGS_PROVIDERS, REASONING_EFFORT_PROVIDERS,
  type AgentTeamModelGroup, type LlmEngineResponse, type LlmModelCatalogResponse,
  type ModelClassDraft, type ModelRouteDefinition, type ModelRouteDraft, type ModelRouteKey,
  type OllamaDeleteResponse, type OllamaPullTask, type ProviderDraft, type ProviderSettingsDraft,
  type SettingsPayload, type SpeechRecognitionSettings,
} from "./llm-model-section-types";

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
  const [pulling, setPulling] = useState(false);
  const [pullInput, setPullInput] = useState("gpt-oss:20b");
  const [task, setTask] = useState<OllamaPullTask | null>(null);
  const [deletingModel, setDeletingModel] = useState<string | null>(null);
  const [modelSearch, setModelSearch] = useState("");
  const [modelPage, setModelPage] = useState(1);
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState("medium");
  const [delegationEnabled, setDelegationEnabled] = useState(false);
  const [routingConfirmPrompt, setRoutingConfirmPrompt] = useState(true);
  const [routingNotify, setRoutingNotify] = useState(true);
  const [agentTeamRedactionText, setAgentTeamRedactionText] = useState("");
  const [imageMode, setImageMode] = useState<"auto" | "always" | "off">("auto");
  const [routingDetailsOpen, setRoutingDetailsOpen] = useState(false);
  const [modelTab, setModelTab] = useState<"language" | "vision" | "audio">("language");
  const [speechRecognition, setSpeechRecognition] = useState<SpeechRecognitionSettings>({});
  const [classDrafts, setClassDrafts] = useState<Record<"vision" | "audio", ModelClassDraft>>({
    vision: { ...buildClassDraft(undefined, undefined), inherit: true },
    audio: { ...buildClassDraft(undefined, undefined), engine: "speech_recognition" },
  });
  const [routingDrafts, setRoutingDrafts] = useState<Record<ModelRouteKey, ModelRouteDraft>>(() =>
    buildRouteDrafts(undefined, undefined),
  );
  const [modelGroups, setModelGroups] = useState<Record<string, AgentTeamModelGroup>>({
    heavy: { name: "高負荷", provider: "", model: "", effort_policy: "same", effort: "" },
    light: { name: "軽量", provider: "", model: "", effort_policy: "lower", effort: "" },
  });
  const [memberSettingsInitialized, setMemberSettingsInitialized] = useState(false);
  const [savingRouting, setSavingRouting] = useState(false);

  const selectedProvider = useMemo(
    () => catalog?.providers.find((item) => item.id === provider) ?? null,
    [catalog, provider],
  );

  const selectedModelId = customModel.trim() || model;
  const selectedModel = useMemo(
    () => selectedProvider?.models.find((item) => item.id === selectedModelId) ?? null,
    [selectedModelId, selectedProvider],
  );
  const current = catalog?.current;
  const visionProvider = useMemo(
    () => catalog?.providers.find((item) => item.id === classDrafts.vision.provider),
    [catalog, classDrafts.vision.provider],
  );
  const audioProvider = useMemo(
    () => catalog?.providers.find((item) => item.id === classDrafts.audio.provider),
    [catalog, classDrafts.audio.provider],
  );
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
    (kind: "image" | "audio") => (catalog?.providers ?? []).filter(
      (providerItem) => providerItem.models.some((item) => item.media?.[kind]),
    ),
    [catalog],
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
      setProvider((current) => {
        if (refreshProvider) return current;
        if (catalog) return current;
        return data.current.provider || current;
      });
      if (!refreshProvider) {
        const currentProvider = data.providers.find((item) => item.id === data.current.provider);
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

  useEffect(() => {
    setModelPage(1);
  }, [provider, modelSearch]);

  useEffect(() => {
    if (modelPage > totalModelPages) setModelPage(totalModelPages);
  }, [modelPage, totalModelPages]);

  useEffect(() => {
    const settings = selectedProvider?.settings;
    setBaseUrl(settings?.base_url ?? "");
    setApiKey("");
    setReasoningEffort(settings?.reasoning_effort ?? "medium");
  }, [selectedProvider]);

  useEffect(() => {
    if (provider === "openai_compatible_local" && selectedModel?.base_url) {
      setBaseUrl(selectedModel.base_url);
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
      toast.error(error instanceof Error ? error.message : "言語モデルを保存できませんでした");
    } finally {
      setSaving(false);
    }
  }, []);

  const handleProviderChange = useCallback(
    (nextProvider: string) => {
      setProvider(nextProvider);
      const next = catalog?.providers.find((item) => item.id === nextProvider);
      const selection = providerDrafts[nextProvider] ?? providerSelection(next);
      const nextModel = selection.customModel.trim() || selection.model;
      const nextOption = selection.customModel.trim()
        ? null
        : next?.models.find((item) => item.id === selection.model);
      const nextSettings = modelOptionSettings(nextOption);
      if (nextSettings?.base_url) setBaseUrl(nextSettings.base_url);
      setModel(selection.model);
      setCustomModel(selection.customModel);
      void saveModelSelection(nextProvider, nextModel, nextSettings);
    },
    [catalog, providerDrafts, saveModelSelection],
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
      const nextSettings = modelOptionSettings(nextOption);
      if (nextSettings?.base_url) setBaseUrl(nextSettings.base_url);
      void saveModelSelection(provider, nextModel, nextSettings);
    },
    [provider, saveModelSelection, selectedProvider],
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
    void saveModelSelection(provider, nextModel);
  }, [customModel, provider, saveModelSelection]);

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
  const showReasoningEffort = REASONING_EFFORT_PROVIDERS.has(provider);

  const updateRoutingDraft = useCallback(
    (key: ModelRouteKey, patch: Partial<ModelRouteDraft>) => {
      setRoutingDrafts((current) => ({
        ...current,
        [key]: { ...current[key], ...patch },
      }));
    },
    [],
  );

  const handleModelDragStart = useCallback(
    (event: DragEvent<HTMLElement>, modelId: string) => {
      event.dataTransfer.setData(
        "application/json",
        JSON.stringify({ provider, model: modelId }),
      );
      event.dataTransfer.effectAllowed = "copy";
    },
    [provider],
  );

  const routeProviderOptions = useCallback(
    (definition: ModelRouteDefinition) => {
      const providers = (catalog?.providers ?? []).filter(
        (item) => item.selection_kind !== "routing_profile",
      );
      if (!definition.allowedProviders) return providers;
      return providers.filter((item) => definition.allowedProviders?.includes(item.id));
    },
    [catalog],
  );

  const handleMemberDrop = useCallback(
    (event: DragEvent<HTMLElement>, definition: ModelRouteDefinition) => {
      event.preventDefault();
      const raw = event.dataTransfer.getData("application/json");
      if (!raw) return;
      let payload: { provider?: string; model?: string };
      try {
        payload = JSON.parse(raw) as { provider?: string; model?: string };
      } catch {
        return;
      }
      const droppedProvider = String(payload.provider || "");
      const droppedModel = String(payload.model || "");
      if (!droppedProvider || !droppedModel) return;
      if (
        definition.allowedProviders?.length &&
        !definition.allowedProviders.includes(droppedProvider)
      ) {
        toast.error(`${definition.label} にはこの provider を割り当てられません`);
        return;
      }
      const providerCatalog = catalog?.providers.find((item) => item.id === droppedProvider);
      const droppedOption = providerCatalog?.models.find((item) => item.id === droppedModel);
      updateRoutingDraft(definition.key, {
        enabled: true,
        provider: droppedProvider,
        model: droppedModel,
        customModel: "",
        mode: defaultModeForOptions(
          droppedOption?.reasoning_effort_options,
          routingDrafts[definition.key]?.mode ?? "medium",
        ),
      });
    },
    [catalog, routingDrafts, updateRoutingDraft],
  );

  const loadAgentTeamSettings = useCallback(async () => {
    try {
      const data = await pyFetch<SettingsPayload>("/settings");
      const team = data.settings?.agent_team;
      const routing = data.settings?.model_routing;
      setDelegationEnabled(team?.delegation_enabled ?? false);
      setMemberSettingsInitialized(team?.member_settings_initialized ?? false);
      setRoutingConfirmPrompt(team?.confirm_prompt ?? true);
      setRoutingNotify(team?.notify ?? true);
      setAgentTeamRedactionText((team?.redaction_terms ?? []).join(", "));
      setImageMode(routing?.media?.image_mode ?? "auto");
      setSpeechRecognition(data.settings?.speech_recognition ?? {});
      setClassDrafts({
        vision: {
          ...buildClassDraft(routing?.classes?.vision, catalog?.providers),
          inherit: routing?.classes?.vision?.inherit ?? !(
            routing?.classes?.vision?.provider || routing?.classes?.vision?.model
          ),
        },
        audio: {
          ...buildClassDraft(routing?.classes?.audio, catalog?.providers),
          engine: routing?.classes?.audio?.engine ?? "speech_recognition",
        },
      });
      setModelGroups({
        heavy: { name: "高負荷", provider: "", model: "", effort_policy: "same", effort: "" },
        light: { name: "軽量", provider: "", model: "", effort_policy: "lower", effort: "" },
        ...(team?.model_groups ?? {}),
      });
      setRoutingDrafts(buildRouteDrafts(team?.members, catalog?.providers, team?.roster));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Agent Team設定を取得できませんでした");
    }
  }, [catalog]);

  const changeDelegationEnabled = useCallback(async (enabled: boolean) => {
    const previous = delegationEnabled;
    setDelegationEnabled(enabled);
    setSavingRouting(true);
    try {
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "agent_team.delegation_enabled", value: enabled }),
      });
      await loadAgentTeamSettings();
    } catch (error) {
      setDelegationEnabled(previous);
      toast.error(error instanceof Error ? error.message : "Agent Teamの状態を変更できませんでした");
    } finally {
      setSavingRouting(false);
    }
  }, [delegationEnabled, loadAgentTeamSettings]);

  useEffect(() => {
    if (expanded) void loadAgentTeamSettings();
  }, [expanded, loadAgentTeamSettings]);

  useEffect(() => {
    if (!catalog) return;
    setRoutingDrafts((current) => {
      let changed = false;
      const next = { ...current };
      for (const definition of MODEL_ROUTE_DEFINITIONS) {
        const draft = current[definition.key];
        if (!draft || draft.customModel.trim()) continue;
        const providerCatalog = catalog.providers.find((item) => item.id === draft.provider);
        if (!providerCatalog) continue;
        if (providerCatalog.models.some((item) => item.id === draft.model)) continue;
        const selection = routeSelection(providerCatalog, draft.model || definition.defaultModel);
        next[definition.key] = {
          ...draft,
          model: selection.model,
          customModel: selection.customModel,
        };
        changed = true;
      }
      return changed ? next : current;
    });
  }, [catalog]);

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

  const saveRoutingSettings = useCallback(async () => {
    setSavingRouting(true);
    try {
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "agent_team.delegation_enabled", value: delegationEnabled }),
      });

      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "agent_team.model_groups", value: modelGroups }),
      });

      const members = Object.fromEntries(MODEL_ROUTE_DEFINITIONS.map((definition) => {
        const draft = routingDrafts[definition.key];
        const targetModel = (draft.customModel.trim() || draft.model).trim();
        const override = {
          ...(draft.provider && targetModel
            ? { provider: draft.provider, model: targetModel }
            : {}),
          ...(draft.effortPolicy === "inherit"
            ? {}
            : {
                effort_policy: draft.effortPolicy,
                ...(draft.effortPolicy === "explicit" ? { effort: draft.mode } : {}),
              }),
          ...(draft.runner ? { runner: draft.runner } : {}),
        };
        return [definition.key, {
          enabled: draft.enabled,
          group_id: draft.groupId || "",
          override,
          default_instances: draft.defaultInstances,
          max_instances: draft.maxInstances,
        }];
      }));
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "agent_team.members", value: members }),
      });
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "agent_team.confirm_prompt", value: routingConfirmPrompt }),
      });
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "agent_team.notify", value: routingNotify }),
      });
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({
          key: "agent_team.redaction_terms",
          value: agentTeamRedactionText
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean),
        }),
      });
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "model_routing.media.image_mode", value: imageMode }),
      });

      for (const classKey of ["vision", "audio"] as const) {
        const draft = classDrafts[classKey];
        const targetModel = (draft.customModel.trim() || draft.model).trim();
        if (classKey === "vision" || classKey === "audio") {
          await pyFetch("/settings", {
            method: "PATCH",
            body: JSON.stringify({
              key: `model_routing.classes.${classKey}.inherit`,
              value: draft.inherit ?? false,
            }),
          });
        }
        if (classKey === "audio") {
          await pyFetch("/settings", {
            method: "PATCH",
            body: JSON.stringify({
              key: "model_routing.classes.audio.engine",
              value: draft.engine ?? "speech_recognition",
            }),
          });
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
          await pyFetch("/settings", {
            method: "PATCH",
            body: JSON.stringify({
              key: `model_routing.classes.${classKey}.${field}`,
              value,
            }),
          });
        }
        if (draft.apiKey.trim()) {
          await pyFetch("/settings", {
            method: "PATCH",
            body: JSON.stringify({
              key: `model_routing.classes.${classKey}.api_key`,
              value: draft.apiKey.trim(),
            }),
          });
        }
      }

      toast.success("Agent Team設定を保存しました");
      setMemberSettingsInitialized(true);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Agent Team設定を保存できませんでした");
    } finally {
      setSavingRouting(false);
    }
  }, [agentTeamRedactionText, classDrafts, delegationEnabled, imageMode, modelGroups, routingConfirmPrompt, routingDrafts, routingNotify]);

  return {
    expanded, setExpanded, catalog, setCatalog, provider, setProvider, model, setModel,
    customModel, setCustomModel, providerDrafts, setProviderDrafts, loading, setLoading,
    refreshing, setRefreshing, saving, setSaving, pulling, setPulling, pullInput, setPullInput,
    task, setTask, deletingModel, setDeletingModel, modelSearch, setModelSearch, modelPage, setModelPage,
    baseUrl, setBaseUrl, apiKey, setApiKey, reasoningEffort, setReasoningEffort,
    delegationEnabled, setDelegationEnabled, routingConfirmPrompt, setRoutingConfirmPrompt,
    routingNotify, setRoutingNotify, agentTeamRedactionText, setAgentTeamRedactionText,
    imageMode, setImageMode, routingDetailsOpen, setRoutingDetailsOpen, modelTab, setModelTab,
    speechRecognition, setSpeechRecognition, classDrafts, setClassDrafts, routingDrafts, setRoutingDrafts,
    modelGroups, setModelGroups, memberSettingsInitialized, setMemberSettingsInitialized,
    savingRouting, setSavingRouting, selectedProvider, selectedModelId, selectedModel, current,
    visionProvider, audioProvider, speechEngine, speechModel, audioSource, mediaProviders,
    providerModels, filteredModels, totalModelPages, currentModelPage, modelPageStart, visibleModels,
    loadCatalog, saveModelSelection, handleProviderChange, handleModelChange, handleCustomModelChange,
    handleCustomModelConfirm, handleProviderSettingsSave, startPull, percent, hasProviderSettings,
    showConnectionSettings, showReasoningEffort, updateRoutingDraft, handleModelDragStart,
    routeProviderOptions, handleMemberDrop, loadAgentTeamSettings, changeDelegationEnabled,
    deleteOllamaModel, saveRoutingSettings,
  };
}

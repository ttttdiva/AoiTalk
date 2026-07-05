"use client";

import { useCallback, useEffect, useMemo, useState, type DragEvent } from "react";
import {
  Bot,
  ChevronDown,
  ChevronUp,
  Download,
  Loader2,
  RefreshCcw,
  Save,
  Search,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";

interface LlmModelOption {
  id: string;
  label: string;
  description?: string;
  installed?: boolean;
  source?: string;
  source_label?: string;
  base_url?: string;
  server?: string;
  server_label?: string;
  size?: number;
  details?: {
    parameter_size?: string;
    quantization_level?: string;
    family?: string;
  };
  context_length?: number;
  reasoning_effort_options?: string[];
  custom_current?: boolean;
}

interface LlmProviderCatalog {
  id: string;
  label: string;
  models: LlmModelOption[];
  configured_model?: string;
  supports_custom_model: boolean;
  capabilities?: {
    supports_stream?: boolean;
    supports_tools?: boolean;
    supports_response_format?: boolean;
    supports_model_pull?: boolean;
    supports_model_delete?: boolean;
    supports_extra_body?: boolean;
  };
  settings?: {
    base_url?: string;
    api_key_configured?: boolean;
    api_key_placeholder?: string;
    reasoning_effort?: string;
    reasoning_effort_options?: string[];
  };
  source: string;
  refreshed?: boolean;
  cached_at?: string | null;
  error?: string | null;
}

interface LlmModelCatalogResponse {
  current: {
    provider: string;
    model: string;
  };
  providers: LlmProviderCatalog[];
}

interface LlmEngineResponse {
  success?: boolean;
  provider: string;
  model: string;
  message?: string;
}

interface SettingsPayload {
  settings?: {
    agent_team?: {
      confirm_prompt?: boolean;
      notify?: boolean;
      redaction_terms?: string[];
      strategy?: string;
      roster?: ModelRouteSettings[];
    };
    model_routing?: ModelRoutingSettings;
  };
}

interface ModelRoutingSettings {
  classes?: {
    heavy?: ModelRouteSettings;
    light?: ModelRouteSettings;
    vision?: ModelRouteSettings & { base_url?: string; api_key?: string };
    audio?: ModelRouteSettings & {
      engine?: "speech_recognition" | "llm" | "off";
      base_url?: string;
      api_key?: string;
    };
  };
  media?: {
    image_mode?: "auto" | "always" | "off";
  };
  overrides?: Record<string, ModelRouteSettings>;
}

interface ModelRouteSettings {
  enabled?: boolean;
  provider?: string;
  model?: string;
  mode?: string;
  reasoning_effort?: string;
  external?: boolean;
  label?: string;
  role?: string;
  runner?: string;
  scalable?: boolean;
  default_instances?: number;
  max_instances?: number;
  tools?: string[];
}

interface OllamaPullTask {
  task_id: string;
  model: string;
  status: string;
  message?: string;
  completed?: number;
  total?: number;
  percent?: number;
  done: boolean;
  error?: string | null;
}

interface OllamaDeleteResponse {
  success: boolean;
  model: string;
}

type ProviderDraft = {
  model: string;
  customModel: string;
};

type ProviderSettingsDraft = {
  base_url?: string;
  api_key?: string;
  reasoning_effort?: string;
};

type ModelRouteKey =
  | "advanced_reasoning"
  | "architect"
  | "explorer"
  | "implementer"
  | "reviewer"
  | "utility"
  | "media"
  | "spotify"
  | "scenario"
  | "writing"
  | "import"
  | "agent_harness";

type ModelRouteDefinition = {
  key: ModelRouteKey;
  label: string;
  defaultProvider: string;
  defaultModel: string;
  allowedProviders?: string[];
  scalable?: boolean;
  defaultMaxInstances?: number;
};

type ModelRouteDraft = {
  enabled: boolean;
  provider: string;
  model: string;
  customModel: string;
  mode: string;
  scalable: boolean;
  defaultInstances: number;
  maxInstances: number;
  runner: string;
};

type ModelClassDraft = {
  provider: string;
  model: string;
  customModel: string;
  mode: string;
  baseUrl: string;
  apiKey: string;
  engine?: "speech_recognition" | "llm" | "off";
};

const MODEL_ROUTE_DEFINITIONS: ModelRouteDefinition[] = [
  {
    key: "advanced_reasoning",
    label: "高度推論",
    defaultProvider: "openai",
    defaultModel: "gpt-4o",
  },
  {
    key: "architect",
    label: "設計",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
    scalable: true,
    defaultMaxInstances: 2,
  },
  {
    key: "explorer",
    label: "調査",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
    scalable: true,
    defaultMaxInstances: 6,
  },
  {
    key: "implementer",
    label: "実装",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
    scalable: true,
    defaultMaxInstances: 4,
  },
  {
    key: "reviewer",
    label: "レビュー",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
    scalable: true,
    defaultMaxInstances: 4,
  },
  {
    key: "utility",
    label: "ユーティリティ",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "media",
    label: "メディア",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "spotify",
    label: "Spotify",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "scenario",
    label: "TRPG_GM",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "writing",
    label: "執筆",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "import",
    label: "シナリオ素材取り込み",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "agent_harness",
    label: "作業エージェント",
    defaultProvider: "codex-cli",
    defaultModel: "gpt-5-codex",
    allowedProviders: ["codex-cli", "claude-cli"],
  },
];

const EXTERNAL_AGENT_PROVIDERS = new Set([
  "openai",
  "openrouter",
  "gemini",
  "antigravity-cli",
  "claude-cli",
  "codex-cli",
]);

const MODEL_PAGE_SIZE = 24;

async function pyFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `API Error: ${res.status}`);
  }
  return res.json();
}

function formatBytes(value?: number): string {
  if (!value || value <= 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function providerHint(providerId: string): string {
  switch (providerId) {
    case "codex-cli":
      return "Codex CLI は --model を受け付けます。候補はCLIから取得した一覧ではなく、未掲載モデルは直接入力してください。";
    case "claude-cli":
      return "Claude Code は alias とフルモデル名を受け付けます。候補はCLIから取得した一覧ではありません。";
    case "antigravity-cli":
      return "Antigravity CLI は --model でモデルを指定します。候補は agy models から取得した一覧ではありません。";
    case "sglang":
      return "SGLang は Hugging Face の model path または /v1/models のIDを使います。";
    case "openai_compatible_local":
      return "llama-server、exo、MLX LM などの /v1/chat/completions 互換APIを指定します。候補にBase URLがある場合は自動で反映します。";
    case "openrouter":
      return "OpenRouter は公開 Models API から候補を取得します。";
    case "ollama":
      return "Ollama はインストール済みモデルと Pull 候補を分けて表示します。";
    default:
      return "プロバイダーが受け付けるモデルIDを指定します。";
  }
}

function modelSourceLabel(item: LlmModelOption): string | null {
  if (item.source_label) return item.source_label;
  if (item.installed) return "インストール済み";
  if (item.custom_current) return "現在の設定";
  return null;
}

function modelSummary(item: LlmModelOption): string {
  if (item.description) return item.description;
  if (item.server_label && item.base_url) return `${item.server_label} ${item.base_url}`;
  if (item.context_length) return `context ${item.context_length}`;
  if (item.details) {
    return `${item.details.parameter_size || "-"} / ${item.details.quantization_level || "-"} / ${formatBytes(item.size)}`;
  }
  return item.id;
}

function providerSourceLabel(source: string): string {
  switch (source) {
    case "remote":
      return "API取得";
    case "cached":
      return "前回取得";
    case "installed":
      return "インストール確認済み";
    case "cli-suggested":
      return "CLI候補";
    case "platform-suggested":
      return "OS候補";
    case "static-suggested":
      return "静的候補";
    case "static":
      return "静的候補";
    default:
      return source || "候補";
  }
}

function providerSelection(provider: LlmProviderCatalog | null | undefined): ProviderDraft {
  const firstModel = provider?.models[0]?.id ?? "";
  const configuredModel = provider?.configured_model?.trim();
  if (!configuredModel) {
    return { model: firstModel, customModel: "" };
  }
  if (provider?.models.some((item) => item.id === configuredModel)) {
    return { model: configuredModel, customModel: "" };
  }
  return { model: firstModel, customModel: configuredModel };
}

function modelOptionSettings(option: LlmModelOption | null | undefined): ProviderSettingsDraft | undefined {
  if (!option?.base_url) return undefined;
  return { base_url: option.base_url };
}

function defaultModeForOptions(options: string[] | undefined, preferred = "medium"): string {
  const values = options ?? [];
  if (!values.length) return preferred;
  if (values.includes(preferred)) return preferred;
  if (values.includes("fast")) return "fast";
  if (values.includes("medium")) return "medium";
  return values[0];
}

function routeSelection(
  provider: LlmProviderCatalog | null | undefined,
  modelId: string,
): ProviderDraft {
  if (!provider) return { model: modelId, customModel: "" };
  return providerSelection({ ...provider, configured_model: modelId });
}

function buildRouteDrafts(
  routes: Record<string, ModelRouteSettings> | undefined,
  providers: LlmProviderCatalog[] | undefined,
): Record<ModelRouteKey, ModelRouteDraft> {
  return Object.fromEntries(
    MODEL_ROUTE_DEFINITIONS.map((definition) => {
      const route = routes?.[definition.key] ?? {};
      const routeProvider = route.provider || definition.defaultProvider;
      const routeModel = route.model || definition.defaultModel;
      const providerCatalog = providers?.find((item) => item.id === routeProvider);
      const selection = routeSelection(providerCatalog, routeModel);
      return [
        definition.key,
        {
          enabled: Boolean(route.provider && route.model),
          provider: routeProvider,
          model: selection.model,
          customModel: selection.customModel,
          mode: route.mode || route.reasoning_effort || "medium",
          scalable: route.scalable ?? definition.scalable ?? false,
          defaultInstances: route.default_instances ?? 1,
          maxInstances: route.max_instances ?? definition.defaultMaxInstances ?? 1,
          runner: route.runner ?? "",
        },
      ];
    }),
  ) as Record<ModelRouteKey, ModelRouteDraft>;
}

function suggestedLightModel(providerId: string, currentModelId: string): string {
  if (providerId === "openai") return "gpt-4o-mini";
  if (providerId === "gemini") return "gemini-2.5-flash";
  if (providerId === "openrouter") return "openai/gpt-4o-mini";
  return currentModelId;
}

function buildClassDraft(
  route: (ModelRouteSettings & { base_url?: string; api_key?: string; engine?: "speech_recognition" | "llm" | "off" }) | undefined,
  providers: LlmProviderCatalog[] | undefined,
): ModelClassDraft {
  const routeProvider = route?.provider || "";
  const routeModel = route?.model || "";
  const providerCatalog = providers?.find((item) => item.id === routeProvider);
  const selection = routeProvider && routeModel
    ? routeSelection(providerCatalog, routeModel)
    : { model: "", customModel: "" };
  return {
    provider: routeProvider,
    model: selection.model,
    customModel: selection.customModel || (!providerCatalog && routeModel ? routeModel : ""),
    mode: route?.mode || route?.reasoning_effort || "",
    baseUrl: route?.base_url || "",
    apiKey: "",
    engine: route?.engine,
  };
}

export function LlmModelSection() {
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
  const [routingConfirmPrompt, setRoutingConfirmPrompt] = useState(true);
  const [routingNotify, setRoutingNotify] = useState(true);
  const [agentTeamRedactionText, setAgentTeamRedactionText] = useState("");
  const [imageMode, setImageMode] = useState<"auto" | "always" | "off">("auto");
  const [routingDetailsOpen, setRoutingDetailsOpen] = useState(false);
  const [classDrafts, setClassDrafts] = useState<Record<"heavy" | "light" | "vision" | "audio", ModelClassDraft>>({
    heavy: buildClassDraft(undefined, undefined),
    light: buildClassDraft(undefined, undefined),
    vision: buildClassDraft(undefined, undefined),
    audio: { ...buildClassDraft(undefined, undefined), engine: "speech_recognition" },
  });
  const [routingDrafts, setRoutingDrafts] = useState<Record<ModelRouteKey, ModelRouteDraft>>(() =>
    buildRouteDrafts(undefined, undefined),
  );
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
  const showConnectionSettings = provider === "ollama" ||
    provider === "openai_compatible_local" ||
    provider === "openrouter" ||
    provider === "sglang";
  const showReasoningEffort = provider === "codex-cli" || provider === "claude-cli";

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
      const providers = catalog?.providers ?? [];
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
      setRoutingConfirmPrompt(team?.confirm_prompt ?? true);
      setRoutingNotify(team?.notify ?? true);
      setAgentTeamRedactionText((team?.redaction_terms ?? []).join(", "));
      setImageMode(routing?.media?.image_mode ?? "auto");
      setClassDrafts({
        heavy: buildClassDraft(routing?.classes?.heavy, catalog?.providers),
        light: buildClassDraft(routing?.classes?.light, catalog?.providers),
        vision: buildClassDraft(routing?.classes?.vision, catalog?.providers),
        audio: {
          ...buildClassDraft(routing?.classes?.audio, catalog?.providers),
          engine: routing?.classes?.audio?.engine ?? "speech_recognition",
        },
      });
      setRoutingDrafts(buildRouteDrafts(routing?.overrides, catalog?.providers));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "モデルルーティング設定を取得できませんでした");
    }
  }, [catalog]);

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
    if (!window.confirm(message)) return;

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
  }, [current, deletingModel, loadCatalog, model]);

  const saveRoutingSettings = useCallback(async () => {
    setSavingRouting(true);
    try {
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

      for (const classKey of ["heavy", "light", "vision", "audio"] as const) {
        const draft = classDrafts[classKey];
        const targetModel = (draft.customModel.trim() || draft.model).trim();
        if (classKey === "audio") {
          await pyFetch("/settings", {
            method: "PATCH",
            body: JSON.stringify({
              key: "model_routing.classes.audio.engine",
              value: draft.engine ?? "speech_recognition",
            }),
          });
        }
        for (const [field, value] of [
          ["provider", draft.provider],
          ["model", targetModel],
          ["base_url", draft.baseUrl.trim()],
          ["reasoning_effort", draft.mode],
          ["mode", draft.mode],
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

      for (const definition of MODEL_ROUTE_DEFINITIONS) {
        const draft = routingDrafts[definition.key];
        const targetModel = (draft.customModel.trim() || draft.model).trim();
        const providerCatalog = catalog?.providers.find((item) => item.id === draft.provider);
        const selectedRouteModel = providerCatalog?.models.find((item) => item.id === targetModel);
        const routeMode = defaultModeForOptions(
          selectedRouteModel?.reasoning_effort_options,
          draft.mode,
        );
        if (draft.enabled && (!draft.provider || !targetModel)) {
          throw new Error(`${definition.label} の provider/model を指定してください`);
        }
        await pyFetch("/settings", {
          method: "PATCH",
          body: JSON.stringify({
            key: `model_routing.overrides.${definition.key}.provider`,
            value: draft.enabled ? draft.provider : "",
          }),
        });
        await pyFetch("/settings", {
          method: "PATCH",
          body: JSON.stringify({
            key: `model_routing.overrides.${definition.key}.model`,
            value: draft.enabled ? targetModel : "",
          }),
        });
        if (routeMode) {
          await pyFetch("/settings", {
            method: "PATCH",
            body: JSON.stringify({
              key: `model_routing.overrides.${definition.key}.mode`,
              value: draft.enabled ? routeMode : "",
            }),
          });
          await pyFetch("/settings", {
            method: "PATCH",
            body: JSON.stringify({
              key: `model_routing.overrides.${definition.key}.reasoning_effort`,
              value: draft.enabled ? routeMode : "",
            }),
          });
        }
        await pyFetch("/settings", {
          method: "PATCH",
          body: JSON.stringify({
            key: `model_routing.overrides.${definition.key}.default_instances`,
            value: draft.enabled ? draft.defaultInstances : 1,
          }),
        });
        await pyFetch("/settings", {
          method: "PATCH",
          body: JSON.stringify({
            key: `model_routing.overrides.${definition.key}.max_instances`,
            value: draft.enabled ? draft.maxInstances : definition.defaultMaxInstances ?? 1,
          }),
        });
        if (definition.key === "agent_harness" || draft.runner) {
          await pyFetch("/settings", {
            method: "PATCH",
            body: JSON.stringify({
              key: `model_routing.overrides.${definition.key}.runner`,
              value: draft.enabled ? draft.runner || "codex_exec" : "",
            }),
          });
        }
      }
      toast.success("モデルルーティング設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "モデルルーティング設定を保存できませんでした");
    } finally {
      setSavingRouting(false);
    }
  }, [agentTeamRedactionText, catalog, classDrafts, imageMode, routingConfirmPrompt, routingDrafts, routingNotify]);

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={() => setExpanded((value) => !value)}
      >
        <CardTitle className="flex items-center justify-between gap-3 text-sm">
          <span className="flex min-w-0 items-center gap-2">
            <Bot className="size-4" />
            <span>言語モデル</span>
            {current && (
              <Badge variant="secondary" className="max-w-[260px] truncate">
                {current.provider} / {current.model}
              </Badge>
            )}
          </span>
          {expanded ? (
            <ChevronUp className="size-4 shrink-0" />
          ) : (
            <ChevronDown className="size-4 shrink-0" />
          )}
        </CardTitle>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-4">
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              取得中...
            </div>
          ) : catalog ? (
            <>
              <div className="grid gap-3 md:grid-cols-[220px_1fr]">
                <div className="space-y-1">
                  <Label className="text-xs">プロバイダー</Label>
                  <select
                    value={provider}
                    onChange={(event) => handleProviderChange(event.target.value)}
                    disabled={saving}
                    className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                  >
                    {catalog.providers.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">モデル</Label>
                  <select
                    value={model}
                    onChange={(event) => handleModelChange(event.target.value)}
                    disabled={saving}
                    className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                  >
                    {(selectedProvider?.models ?? []).map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}{modelSourceLabel(item) ? ` (${modelSourceLabel(item)})` : ""}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div className="space-y-1">
                <Label className="text-xs">カスタムモデルID</Label>
                <Input
                  value={customModel}
                  onChange={(event) => handleCustomModelChange(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      handleCustomModelConfirm();
                    }
                  }}
                  placeholder="候補にないモデルIDを直接入力"
                  disabled={saving}
                  className="h-8"
                />
                <p className="text-[10px] text-muted-foreground">
                  {providerHint(provider)}
                </p>
                {selectedModel?.source === "cli-suggested" && (
                  <p className="text-[10px] text-amber-600">
                    この候補はCLIから取得した一覧ではありません。必要ならCLI側の最新モデル名をカスタムモデルIDに直接入力してください。
                  </p>
                )}
                {selectedModel?.source === "pull-suggested" && (
                  <p className="text-[10px] text-amber-600">
                    このOllama候補は未インストールの可能性があります。使用前に Pull してください。
                  </p>
                )}
              </div>

              {hasProviderSettings && (
                  <div className="space-y-3 rounded-md border p-3">
                    <div className="grid gap-3 md:grid-cols-2">
                      {(["heavy", "light"] as const).map((classKey) => {
                        const draft = classDrafts[classKey];
                        const providerCatalog = catalog.providers.find((item) => item.id === draft.provider);
                        return (
                          <div key={classKey} className="space-y-2 rounded border p-2">
                            <div className="flex items-center justify-between gap-2">
                              <div className="text-xs font-medium">
                                {classKey === "heavy" ? "高負荷推論" : "軽量・大量呼び出し"}
                              </div>
                              {classKey === "light" && (
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="ghost"
                                  className="h-7 px-2 text-[11px]"
                                  disabled={savingRouting}
                                  onClick={() => {
                                    const nextModel = suggestedLightModel(provider, selectedModelId);
                                    setClassDrafts((current) => ({
                                      ...current,
                                      light: {
                                        ...current.light,
                                        provider,
                                        model: nextModel,
                                        customModel: catalog.providers.find((item) => item.id === provider)?.models.some((item) => item.id === nextModel) ? "" : nextModel,
                                      },
                                    }));
                                  }}
                                >
                                  推奨を適用
                                </Button>
                              )}
                            </div>
                            <select
                              value={draft.provider}
                              onChange={(event) => {
                                const nextProvider = event.target.value;
                                const next = catalog.providers.find((item) => item.id === nextProvider);
                                const selection = nextProvider ? providerSelection(next) : { model: "", customModel: "" };
                                setClassDrafts((current) => ({
                                  ...current,
                                  [classKey]: { ...current[classKey], provider: nextProvider, ...selection },
                                }));
                              }}
                              disabled={savingRouting}
                              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
                            >
                              <option value="">メインを継承</option>
                              {catalog.providers
                                .filter((item) => !item.id.endsWith("-cli") && item.id !== "claude" && item.id !== "grok")
                                .map((item) => (
                                  <option key={item.id} value={item.id}>
                                    {item.label}
                                  </option>
                                ))}
                            </select>
                            {draft.provider && (
                              <div className="grid gap-2 md:grid-cols-2">
                                <select
                                  value={draft.model}
                                  onChange={(event) =>
                                    setClassDrafts((current) => ({
                                      ...current,
                                      [classKey]: { ...current[classKey], model: event.target.value, customModel: "" },
                                    }))
                                  }
                                  disabled={savingRouting}
                                  className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
                                >
                                  {(providerCatalog?.models ?? []).map((item) => (
                                    <option key={item.id} value={item.id}>
                                      {item.label}
                                    </option>
                                  ))}
                                </select>
                                <Input
                                  value={draft.customModel}
                                  onChange={(event) =>
                                    setClassDrafts((current) => ({
                                      ...current,
                                      [classKey]: { ...current[classKey], customModel: event.target.value },
                                    }))
                                  }
                                  placeholder="カスタムID"
                                  disabled={savingRouting}
                                  className="h-8"
                                />
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>

                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-2 rounded border p-2">
                        <div className="flex items-center justify-between gap-2">
                          <div className="text-xs font-medium">画像認識</div>
                          <select
                            value={imageMode}
                            onChange={(event) => setImageMode(event.target.value as "auto" | "always" | "off")}
                            disabled={savingRouting}
                            className="h-8 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
                          >
                            <option value="auto">auto</option>
                            <option value="always">always</option>
                            <option value="off">off</option>
                          </select>
                        </div>
                        <div className="grid gap-2 md:grid-cols-2">
                          <select
                            value={classDrafts.vision.provider}
                            onChange={(event) => {
                              const nextProvider = event.target.value;
                              const next = catalog.providers.find((item) => item.id === nextProvider);
                              const selection = nextProvider ? providerSelection(next) : { model: "", customModel: "" };
                              setClassDrafts((current) => ({
                                ...current,
                                vision: { ...current.vision, provider: nextProvider, ...selection },
                              }));
                            }}
                            disabled={savingRouting || imageMode === "off"}
                            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
                          >
                            <option value="">未設定</option>
                            {catalog.providers.filter((item) => !item.id.endsWith("-cli")).map((item) => (
                              <option key={item.id} value={item.id}>{item.label}</option>
                            ))}
                          </select>
                          <Input
                            value={classDrafts.vision.customModel || classDrafts.vision.model}
                            onChange={(event) =>
                              setClassDrafts((current) => ({
                                ...current,
                                vision: { ...current.vision, customModel: event.target.value, model: "" },
                              }))
                            }
                            placeholder="モデルID"
                            disabled={savingRouting || imageMode === "off"}
                            className="h-8"
                          />
                        </div>
                        <div className="grid gap-2 md:grid-cols-2">
                          <Input
                            value={classDrafts.vision.baseUrl}
                            onChange={(event) =>
                              setClassDrafts((current) => ({
                                ...current,
                                vision: { ...current.vision, baseUrl: event.target.value },
                              }))
                            }
                            placeholder="Base URL"
                            disabled={savingRouting || imageMode === "off"}
                            className="h-8"
                          />
                          <Input
                            value={classDrafts.vision.apiKey}
                            onChange={(event) =>
                              setClassDrafts((current) => ({
                                ...current,
                                vision: { ...current.vision, apiKey: event.target.value },
                              }))
                            }
                            placeholder="API key（空なら維持）"
                            disabled={savingRouting || imageMode === "off"}
                            className="h-8"
                          />
                        </div>
                      </div>

                      <div className="space-y-2 rounded border p-2">
                        <div className="text-xs font-medium">音声認識</div>
                        <select
                          value={classDrafts.audio.engine ?? "speech_recognition"}
                          onChange={(event) =>
                            setClassDrafts((current) => ({
                              ...current,
                              audio: { ...current.audio, engine: event.target.value as "speech_recognition" | "llm" | "off" },
                            }))
                          }
                          disabled={savingRouting}
                          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
                        >
                          <option value="speech_recognition">既存STT</option>
                          <option value="llm">LLM</option>
                          <option value="off">無効</option>
                        </select>
                        {classDrafts.audio.engine === "llm" && (
                          <div className="grid gap-2 md:grid-cols-2">
                            <select
                              value={classDrafts.audio.provider}
                              onChange={(event) => {
                                const nextProvider = event.target.value;
                                const next = catalog.providers.find((item) => item.id === nextProvider);
                                const selection = nextProvider ? providerSelection(next) : { model: "", customModel: "" };
                                setClassDrafts((current) => ({
                                  ...current,
                                  audio: { ...current.audio, provider: nextProvider, ...selection },
                                }));
                              }}
                              disabled={savingRouting}
                              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
                            >
                              <option value="">未設定</option>
                              {catalog.providers
                                .filter((item) => ["openai", "gemini", "openrouter", "openai_compatible_local", "sglang", "ollama"].includes(item.id))
                                .map((item) => (
                                  <option key={item.id} value={item.id}>{item.label}</option>
                                ))}
                            </select>
                            <Input
                              value={classDrafts.audio.customModel || classDrafts.audio.model}
                              onChange={(event) =>
                                setClassDrafts((current) => ({
                                  ...current,
                                  audio: { ...current.audio, customModel: event.target.value, model: "" },
                                }))
                              }
                              placeholder="モデルID"
                              disabled={savingRouting}
                              className="h-8"
                            />
                          </div>
                        )}
                        {classDrafts.audio.engine === "llm" && (
                          <div className="grid gap-2 md:grid-cols-2">
                            <Input
                              value={classDrafts.audio.baseUrl}
                              onChange={(event) =>
                                setClassDrafts((current) => ({
                                  ...current,
                                  audio: { ...current.audio, baseUrl: event.target.value },
                                }))
                              }
                              placeholder="Base URL"
                              disabled={savingRouting}
                              className="h-8"
                            />
                            <Input
                              value={classDrafts.audio.apiKey}
                              onChange={(event) =>
                                setClassDrafts((current) => ({
                                  ...current,
                                  audio: { ...current.audio, apiKey: event.target.value },
                                }))
                              }
                              placeholder="API key（空なら維持）"
                              disabled={savingRouting}
                              className="h-8"
                            />
                          </div>
                        )}
                      </div>
                    </div>
                  {showConnectionSettings && (
                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-1">
                        <Label className="text-xs">Base URL</Label>
                        <Input
                          value={baseUrl}
                          onChange={(event) => setBaseUrl(event.target.value)}
                          placeholder="http://127.0.0.1:8080/v1"
                          disabled={saving}
                          className="h-8"
                        />
                      </div>
                      <div className="space-y-1">
                        <Label className="text-xs">API key</Label>
                        <Input
                          value={apiKey}
                          onChange={(event) => setApiKey(event.target.value)}
                          placeholder={
                            selectedProvider?.settings?.api_key_configured
                              ? "設定済み。空なら維持"
                              : selectedProvider?.settings?.api_key_placeholder || "dummy"
                          }
                          disabled={saving}
                          className="h-8"
                        />
                      </div>
                    </div>
                  )}

                  {showReasoningEffort && (
                    <div className="max-w-xs space-y-1">
                      <Label className="text-xs">Effort</Label>
                      <select
                        value={reasoningEffort}
                        onChange={(event) => setReasoningEffort(event.target.value)}
                        disabled={saving}
                        className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                      >
                        {(selectedProvider?.settings?.reasoning_effort_options ?? ["medium"]).map(
                          (item) => (
                            <option key={item} value={item}>
                              {item}
                            </option>
                          ),
                        )}
                      </select>
                    </div>
                  )}

                  <div className="flex flex-wrap items-center gap-4">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={handleProviderSettingsSave}
                      disabled={saving || !selectedModelId.trim()}
                    >
                      設定を保存
                    </Button>
                  </div>
                </div>
              )}

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => loadCatalog(true, provider)}
                  disabled={refreshing || saving}
                >
                  <RefreshCcw className={`mr-1 size-3 ${refreshing ? "animate-spin" : ""}`} />
                  選択中の候補を更新
                </Button>
                {saving && (
                  <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                    <Loader2 className="size-3 animate-spin" />
                    保存中...
                  </span>
                )}
                {selectedProvider?.error && (
                  <span className="text-xs text-amber-600">
                    動的取得失敗: {selectedProvider.error}
                  </span>
                )}
                {selectedProvider && !selectedProvider.refreshed && (
                  <span className="text-xs text-muted-foreground">
                    表示中: {providerSourceLabel(selectedProvider.source)}
                  </span>
                )}
              </div>

              {provider === "ollama" && (
                <div className="space-y-3 rounded-md border p-3">
                  <div className="flex flex-wrap items-end gap-2">
                    <div className="min-w-64 flex-1 space-y-1">
                      <Label className="text-xs">Ollama model tag</Label>
                      <Input
                        value={pullInput}
                        onChange={(event) => setPullInput(event.target.value)}
                        placeholder="llama3.2:3b"
                        className="h-8"
                      />
                    </div>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={startPull}
                      disabled={pulling || !pullInput.trim()}
                    >
                      {pulling ? (
                        <Loader2 className="mr-1 size-3 animate-spin" />
                      ) : (
                        <Download className="mr-1 size-3" />
                      )}
                      Pull
                    </Button>
                  </div>

                  {task && (
                    <div className="space-y-2 rounded border p-3">
                      <div className="flex items-center justify-between gap-3 text-xs">
                        <span className="font-medium">{task.model}</span>
                        <Badge variant={task.error ? "destructive" : "secondary"}>
                          {task.status}
                        </Badge>
                      </div>
                      <div className="h-2 overflow-hidden rounded bg-muted">
                        <div
                          className="h-full bg-primary transition-all"
                          style={{ width: `${percent}%` }}
                        />
                      </div>
                      <div className="flex justify-between gap-3 text-xs text-muted-foreground">
                        <span>{task.message || task.status}</span>
                        <span>
                          {percent}% {formatBytes(task.completed)} / {formatBytes(task.total)}
                        </span>
                      </div>
                      {task.error && <p className="text-xs text-destructive">{task.error}</p>}
                    </div>
                  )}
                </div>
              )}

              <Separator />

              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs font-medium">
                    {selectedProvider?.label ?? provider} の候補
                  </p>
                  <Badge variant="secondary">
                    {selectedProvider?.models.length ?? 0}件
                  </Badge>
                </div>
                <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
                  <div className="relative md:max-w-sm md:flex-1">
                    <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                    <Input
                      value={modelSearch}
                      onChange={(event) => setModelSearch(event.target.value)}
                      placeholder="候補を検索"
                      className="h-8 pl-8"
                    />
                  </div>
                  <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground md:justify-end">
                    <span>
                      {filteredModels.length
                        ? `${modelPageStart + 1}-${modelPageStart + visibleModels.length} / ${filteredModels.length}件`
                        : "0件"}
                    </span>
                    <div className="flex items-center gap-1">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="h-8 px-2 text-xs"
                        onClick={() => setModelPage((page) => Math.max(1, page - 1))}
                        disabled={currentModelPage <= 1}
                      >
                        前へ
                      </Button>
                      <span className="min-w-14 text-center">
                        {currentModelPage} / {totalModelPages}
                      </span>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="h-8 px-2 text-xs"
                        onClick={() => setModelPage((page) => Math.min(totalModelPages, page + 1))}
                        disabled={currentModelPage >= totalModelPages}
                      >
                        次へ
                      </Button>
                    </div>
                  </div>
                </div>
                <div className="grid gap-2 md:grid-cols-2">
                  {visibleModels.map((item) => {
                    const canDelete = provider === "ollama" && item.installed;
                    const isDeleting = deletingModel === item.id;
                    return (
                      <div
                        key={item.id}
                        draggable
                        onDragStart={(event) => handleModelDragStart(event, item.id)}
                        className={`flex min-h-14 items-center gap-2 rounded border px-3 py-2 transition-colors hover:bg-accent/50 ${
                          selectedModelId === item.id ? "border-primary bg-accent/30" : ""
                        }`}
                      >
                        <button
                          type="button"
                          onClick={() => handleModelChange(item.id)}
                          disabled={saving}
                          className="min-w-0 flex-1 text-left"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <p className="truncate text-xs font-medium">{item.label}</p>
                            {modelSourceLabel(item) && (
                              <Badge
                                variant={item.source === "pull-suggested" ? "secondary" : "default"}
                                className="shrink-0 text-[10px]"
                              >
                                {modelSourceLabel(item)}
                              </Badge>
                            )}
                          </div>
                          <p className="mt-1 truncate text-[10px] text-muted-foreground">
                            {modelSummary(item)}
                          </p>
                        </button>
                        {canDelete && (
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            className="size-8 shrink-0 text-muted-foreground hover:text-destructive"
                            onClick={() => void deleteOllamaModel(item.id)}
                            disabled={Boolean(deletingModel)}
                            title={`Delete ${item.id}`}
                            aria-label={`Delete ${item.id}`}
                          >
                            {isDeleting ? (
                              <Loader2 className="size-3 animate-spin" />
                            ) : (
                              <Trash2 className="size-3" />
                            )}
                          </Button>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>

              <Separator />

              <div className="space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-xs font-medium">モデルルーティング</div>
                    <p className="text-[11px] text-muted-foreground">
                      未設定の行はメインモデルを継承します。
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={saveRoutingSettings}
                    disabled={savingRouting}
                  >
                    {savingRouting ? (
                      <Loader2 className="mr-1 size-3 animate-spin" />
                    ) : (
                      <Save className="mr-1 size-3" />
                    )}
                    保存
                  </Button>
                </div>

                <div className="space-y-3 rounded-md border p-3">
                    <div className="flex flex-wrap items-center gap-4">
                      <label className="flex items-center gap-2 text-xs">
                        <Checkbox
                          checked={routingConfirmPrompt}
                          onCheckedChange={(checked) => setRoutingConfirmPrompt(checked === true)}
                          disabled={savingRouting}
                        />
                        外部送信前に確認する
                      </label>
                      <label className="flex items-center gap-2 text-xs">
                        <Checkbox
                          checked={routingNotify}
                          onCheckedChange={(checked) => setRoutingNotify(checked === true)}
                          disabled={savingRouting}
                        />
                        確認時に通知する
                      </label>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={saveRoutingSettings}
                        disabled={savingRouting}
                      >
                        {savingRouting ? (
                          <Loader2 className="mr-1 size-3 animate-spin" />
                        ) : (
                          <Save className="mr-1 size-3" />
                        )}
                        保存
                      </Button>
                    </div>

                    <div className="space-y-1">
                      <Label className="text-xs">外部モデル送信時に追加でマスクする語句</Label>
                      <Input
                        value={agentTeamRedactionText}
                        onChange={(event) => setAgentTeamRedactionText(event.target.value)}
                        placeholder="顧客名, 案件名, 社内コード"
                        disabled={savingRouting}
                        className="h-8"
                      />
                    </div>

                    <div className="space-y-2">
                      <div className="flex items-center justify-between gap-3 rounded border p-2">
                        <span className="text-xs font-medium">メインエージェント</span>
                        <Badge variant="secondary" className="max-w-[360px] truncate">
                          {current?.provider ?? "-"} / {current?.model ?? "-"}
                        </Badge>
                      </div>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="h-8 justify-start px-2 text-xs"
                        onClick={() => setRoutingDetailsOpen((value) => !value)}
                      >
                        {routingDetailsOpen ? (
                          <ChevronUp className="mr-1 size-3" />
                        ) : (
                          <ChevronDown className="mr-1 size-3" />
                        )}
                        用途別の詳細設定
                      </Button>
                      {routingDetailsOpen && MODEL_ROUTE_DEFINITIONS.map((definition) => {
                        const draft = routingDrafts[definition.key];
                        const providers = routeProviderOptions(definition);
                        const providerCatalog = catalog.providers.find((item) => item.id === draft.provider);
                        const selectedRouteModelId = draft.customModel.trim() || draft.model;
                        const selectedRouteModel = providerCatalog?.models.find(
                          (item) => item.id === selectedRouteModelId,
                        );
                        const modeOptions = selectedRouteModel?.reasoning_effort_options ?? [];
                        const routeMode = modeOptions.includes(draft.mode)
                          ? draft.mode
                          : defaultModeForOptions(modeOptions, draft.mode);
                        return (
                          <div
                            key={definition.key}
                            onDragOver={(event) => event.preventDefault()}
                            onDrop={(event) => handleMemberDrop(event, definition)}
                            className="grid gap-2 rounded border p-2 transition-colors hover:bg-accent/40 md:grid-cols-[170px_170px_1fr_110px_150px]"
                          >
                            <label className="flex items-center gap-2 text-xs font-medium">
                              <Checkbox
                                checked={draft.enabled}
                                onCheckedChange={(checked) =>
                                  updateRoutingDraft(definition.key, { enabled: checked === true })
                                }
                                disabled={savingRouting}
                              />
                              <span className="min-w-0 truncate">{definition.label}</span>
                              {draft.enabled && EXTERNAL_AGENT_PROVIDERS.has(draft.provider) && (
                                <Badge variant="secondary" className="shrink-0 text-[10px]">
                                  外部送信
                                </Badge>
                              )}
                            </label>
                            <select
                              value={draft.provider}
                              onChange={(event) => {
                                const nextProvider = event.target.value;
                                const next = providers.find((item) => item.id === nextProvider);
                                const selection = providerSelection(next);
                                const nextModelId = selection.customModel.trim() || selection.model;
                                const nextModel = next?.models.find((item) => item.id === nextModelId);
                                updateRoutingDraft(definition.key, {
                                  provider: nextProvider,
                                  model: selection.model,
                                  customModel: selection.customModel,
                                  mode: defaultModeForOptions(
                                    nextModel?.reasoning_effort_options,
                                    next?.settings?.reasoning_effort ?? draft.mode,
                                  ),
                                  runner: definition.key === "agent_harness" && nextProvider === "claude-cli"
                                    ? "claude_code"
                                    : definition.key === "agent_harness" && nextProvider === "codex-cli"
                                      ? "codex_exec"
                                      : draft.runner,
                                });
                              }}
                              disabled={savingRouting || Boolean(definition.allowedProviders?.length === 1)}
                              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                            >
                              {providers.map((item) => (
                                <option key={item.id} value={item.id}>
                                  {item.label}
                                </option>
                              ))}
                            </select>
                            <div className="grid gap-2 md:grid-cols-2">
                              <select
                                value={draft.model}
                                onChange={(event) => {
                                  const nextModel = event.target.value;
                                  const nextOption = providerCatalog?.models.find((item) => item.id === nextModel);
                                  updateRoutingDraft(definition.key, {
                                    model: nextModel,
                                    customModel: "",
                                    mode: defaultModeForOptions(
                                      nextOption?.reasoning_effort_options,
                                      draft.mode,
                                    ),
                                  });
                                }}
                                disabled={savingRouting}
                                className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                              >
                                {(providerCatalog?.models ?? []).map((item) => (
                                  <option key={item.id} value={item.id}>
                                    {item.label}
                                  </option>
                                ))}
                              </select>
                              <Input
                                value={draft.customModel}
                                onChange={(event) =>
                                  updateRoutingDraft(definition.key, { customModel: event.target.value })
                                }
                                placeholder="カスタムID"
                                disabled={savingRouting}
                                className="h-8"
                              />
                            </div>
                            {modeOptions.length > 0 ? (
                              <select
                                value={routeMode}
                                onChange={(event) =>
                                  updateRoutingDraft(definition.key, { mode: event.target.value })
                                }
                                disabled={savingRouting}
                                className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                              >
                                {modeOptions.map((item) => (
                                  <option key={item} value={item}>
                                    {item}
                                  </option>
                                ))}
                              </select>
                            ) : (
                              <span className="flex h-8 items-center text-xs text-muted-foreground">
                                モードなし
                              </span>
                            )}
                            <div className="grid gap-2">
                              {definition.scalable ? (
                                <div className="grid grid-cols-[1fr_64px] items-center gap-2">
                                  <span className="text-xs text-muted-foreground">自動増員</span>
                                  <Input
                                    type="number"
                                    min={1}
                                    max={32}
                                    value={draft.maxInstances}
                                    onChange={(event) => {
                                      const next = Math.max(1, Math.min(32, Number(event.target.value) || 1));
                                      updateRoutingDraft(definition.key, {
                                        scalable: true,
                                        maxInstances: next,
                                        defaultInstances: Math.min(draft.defaultInstances || 1, next),
                                      });
                                    }}
                                    disabled={savingRouting}
                                    className="h-8"
                                  />
                                </div>
                              ) : (
                                <span className="flex h-8 items-center text-xs text-muted-foreground">
                                  単体
                                </span>
                              )}
                              {definition.key === "agent_harness" && (
                                <select
                                  value={draft.runner || "codex_exec"}
                                  onChange={(event) => {
                                    const nextRunner = event.target.value;
                                    const nextProvider =
                                      nextRunner === "claude_code"
                                        ? "claude-cli"
                                        : nextRunner === "codex_exec"
                                          ? "codex-cli"
                                          : draft.provider;
                                    const next = providers.find((item) => item.id === nextProvider);
                                    const selection =
                                      nextProvider === draft.provider
                                        ? { model: draft.model, customModel: draft.customModel }
                                        : providerSelection(next);
                                    const nextModelId = selection.customModel.trim() || selection.model;
                                    const nextModel = next?.models.find((item) => item.id === nextModelId);
                                    updateRoutingDraft(definition.key, {
                                      runner: nextRunner,
                                      provider: nextProvider,
                                      model: selection.model,
                                      customModel: selection.customModel,
                                      mode: defaultModeForOptions(
                                        nextModel?.reasoning_effort_options,
                                        next?.settings?.reasoning_effort ?? draft.mode,
                                      ),
                                    });
                                  }}
                                  disabled={savingRouting}
                                  className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                                >
                                  <option value="codex_exec">Codex CLI</option>
                                  <option value="claude_code">Claude Code</option>
                                  <option value="custom_command">Custom</option>
                                </select>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
              </div>
            </>
          ) : (
            <Button size="sm" variant="outline" onClick={() => loadCatalog(true)}>
              取得
            </Button>
          )}
        </CardContent>
      )}
    </Card>
  );
}

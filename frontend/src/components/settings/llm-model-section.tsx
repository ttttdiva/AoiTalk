"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
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
  size?: number;
  details?: {
    parameter_size?: string;
    quantization_level?: string;
    family?: string;
  };
  context_length?: number;
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
    model_sharing?: {
      enabled?: boolean;
      confirm_prompt?: boolean;
      notify?: boolean;
      provider?: string;
      model?: string;
    };
  };
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
    case "gemini-cli":
      return "Gemini CLI は -m でモデルを指定します。候補はCLIから取得した一覧ではありません。";
    case "sglang":
      return "SGLang は Hugging Face の model path または /v1/models のIDを使います。";
    case "openai_compatible_local":
      return "llama-server などの /v1/chat/completions 互換APIを指定します。DFlash対応モデルはここで接続します。";
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
  const [delegationEnabled, setDelegationEnabled] = useState(false);
  const [delegationConfirmPrompt, setDelegationConfirmPrompt] = useState(true);
  const [delegationNotify, setDelegationNotify] = useState(true);
  const [delegationProvider, setDelegationProvider] = useState("openai");
  const [delegationModel, setDelegationModel] = useState("gpt-4o");
  const [delegationCustomModel, setDelegationCustomModel] = useState("");
  const [savingDelegation, setSavingDelegation] = useState(false);

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
      setModel(selection.model);
      setCustomModel(selection.customModel);
      void saveModelSelection(nextProvider, nextModel);
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
      void saveModelSelection(provider, nextModel);
    },
    [provider, saveModelSelection],
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
  const selectedDelegationProvider = useMemo(
    () => catalog?.providers.find((item) => item.id === delegationProvider) ?? null,
    [catalog, delegationProvider],
  );
  const delegationSelectedModelId = delegationCustomModel.trim() || delegationModel;

  const loadDelegationSettings = useCallback(async () => {
    try {
      const data = await pyFetch<SettingsPayload>("/settings");
      const settings = data.settings?.model_sharing ?? {};
      const nextProvider = settings.provider || "openai";
      const nextModel = settings.model || "gpt-4o";
      setDelegationEnabled(settings.enabled ?? false);
      setDelegationConfirmPrompt(settings.confirm_prompt ?? true);
      setDelegationNotify(settings.notify ?? true);
      setDelegationProvider(nextProvider);
      setDelegationModel(nextModel);
      setDelegationCustomModel("");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "モデル分担設定を取得できませんでした");
    }
  }, []);

  useEffect(() => {
    if (expanded) void loadDelegationSettings();
  }, [expanded, loadDelegationSettings]);

  useEffect(() => {
    if (!selectedDelegationProvider) return;
    if (delegationCustomModel.trim()) return;
    const selection = providerSelection({
      ...selectedDelegationProvider,
      configured_model: delegationModel,
    });
    setDelegationModel(selection.model);
    setDelegationCustomModel(selection.customModel);
  }, [delegationCustomModel, delegationModel, selectedDelegationProvider]);

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

  const saveDelegationSettings = useCallback(async () => {
    const targetModel = (delegationCustomModel.trim() || delegationModel).trim();
    if (delegationEnabled && (!delegationProvider || !targetModel)) return;

    setSavingDelegation(true);
    try {
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "model_sharing.enabled", value: delegationEnabled }),
      });
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "model_sharing.confirm_prompt", value: delegationConfirmPrompt }),
      });
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "model_sharing.notify", value: delegationNotify }),
      });
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "model_sharing.provider", value: delegationProvider }),
      });
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "model_sharing.model", value: targetModel }),
      });
      toast.success("モデル分担設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "モデル分担設定を保存できませんでした");
    } finally {
      setSavingDelegation(false);
    }
  }, [
    delegationConfirmPrompt,
    delegationCustomModel,
    delegationEnabled,
    delegationModel,
    delegationNotify,
    delegationProvider,
  ]);

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
                            {item.description ||
                              item.context_length && `context ${item.context_length}` ||
                              item.details && `${item.details.parameter_size || "-"} / ${item.details.quantization_level || "-"} / ${formatBytes(item.size)}` ||
                              item.id}
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
                  <label className="flex items-center gap-2 text-xs">
                    <Checkbox
                      checked={delegationEnabled}
                      onCheckedChange={(checked) => setDelegationEnabled(checked === true)}
                      disabled={savingDelegation}
                    />
                    外部モデル分担を使う
                  </label>
                  {!delegationEnabled && (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={saveDelegationSettings}
                      disabled={savingDelegation}
                    >
                      {savingDelegation ? (
                        <Loader2 className="mr-1 size-3 animate-spin" />
                      ) : (
                        <Save className="mr-1 size-3" />
                      )}
                      保存
                    </Button>
                  )}
                </div>

                {delegationEnabled && (
                  <div className="space-y-3 rounded-md border p-3">
                    <div className="grid gap-3 md:grid-cols-[220px_1fr]">
                      <div className="space-y-1">
                        <Label className="text-xs">外部モデルプロバイダー</Label>
                        <select
                          value={delegationProvider}
                          onChange={(event) => {
                            const nextProvider = event.target.value;
                            const next = catalog.providers.find((item) => item.id === nextProvider);
                            const selection = providerSelection(next);
                            setDelegationProvider(nextProvider);
                            setDelegationModel(selection.model);
                            setDelegationCustomModel(selection.customModel);
                          }}
                          disabled={savingDelegation}
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
                        <Label className="text-xs">外部モデル</Label>
                        <select
                          value={delegationModel}
                          onChange={(event) => {
                            setDelegationModel(event.target.value);
                            setDelegationCustomModel("");
                          }}
                          disabled={savingDelegation}
                          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                        >
                          {(selectedDelegationProvider?.models ?? []).map((item) => (
                            <option key={item.id} value={item.id}>
                              {item.label}{modelSourceLabel(item) ? ` (${modelSourceLabel(item)})` : ""}
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>

                    <div className="space-y-1">
                      <Label className="text-xs">外部カスタムモデルID</Label>
                      <Input
                        value={delegationCustomModel}
                        onChange={(event) => setDelegationCustomModel(event.target.value)}
                        placeholder="候補にないモデルIDを直接入力"
                        disabled={savingDelegation}
                        className="h-8"
                      />
                      <p className="text-[10px] text-muted-foreground">
                        {providerHint(delegationProvider)}
                      </p>
                    </div>

                    <div className="flex flex-wrap items-center gap-4">
                      <label className="flex items-center gap-2 text-xs">
                        <Checkbox
                          checked={delegationConfirmPrompt}
                          onCheckedChange={(checked) => setDelegationConfirmPrompt(checked === true)}
                          disabled={savingDelegation}
                        />
                        送信前に確認する
                      </label>
                      <label className="flex items-center gap-2 text-xs">
                        <Checkbox
                          checked={delegationNotify}
                          onCheckedChange={(checked) => setDelegationNotify(checked === true)}
                          disabled={savingDelegation}
                        />
                        確認時に通知する
                      </label>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={saveDelegationSettings}
                        disabled={savingDelegation || !delegationSelectedModelId.trim()}
                      >
                        {savingDelegation ? (
                          <Loader2 className="mr-1 size-3 animate-spin" />
                        ) : (
                          <Save className="mr-1 size-3" />
                        )}
                        保存
                      </Button>
                    </div>
                  </div>
                )}
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

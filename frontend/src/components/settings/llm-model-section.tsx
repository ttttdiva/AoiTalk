"use client";

import { AppSelect } from "@/components/ui/app-select";
import { useEffect, useState, type KeyboardEvent } from "react";

import {
  Bot,
  ChevronDown,
  ChevronUp,
  Download,
  Loader2,
  RefreshCcw,
  Search,
  Trash2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { formatBytes } from "@/lib/utils";
import { LlmMediaModelTabs } from "./llm-media-model-tabs";
import { LlmAgentTeamRouting } from "./llm-agent-team-routing";
import { LlmModelGroupsPanel } from "./llm-model-groups-panel";
import { LlmProviderVisibilitySettings } from "./llm-provider-visibility-settings";
import { OpenRouterProviderRouting } from "./openrouter-provider-routing";
import { FreeTeamSettingsPanel } from "./free-team-settings-panel";
import { LlmExternalPrivacySettings } from "./llm-external-privacy-settings";
import { LlamaCppRuntimePanel } from "./llama-cpp-runtime-panel";
import { useLlmModelSection } from "./llm-model-section-state";
import {
  hasDeploymentMetadata,
  resolveEffectiveModelId,
  resolveEffectiveProviderId,
} from "@/lib/llm-provider-visibility";
import {
  modelSourceLabel,
  modelSummary,
  providerHint,
  providerSourceLabel,
  llamaCppRuntimeProfileForModel,
  shouldShowLlamaCppRuntimePanel,
} from "./llm-model-section-types";

export function LlmModelSection() {
  const [activeSection, setActiveSection] = useState<"base" | "routing" | "agent" | "privacy">("base");
  const {
    expanded, setExpanded, catalog, provider, model, customModel, loading, refreshing, saving,
    pulling, pullInput, setPullInput, task, deletingModel, modelSearch, setModelSearch, setModelPage,
    baseUrl, setBaseUrl, apiKey, setApiKey, reasoningEffort, setReasoningEffort,
    llamaCppDraft, setLlamaCppDraft, llamaCppError,
    delegationEnabled, setDelegationEnabled,
    orchestrationMode, setOrchestrationMode, chatgptWeb, setChatgptWeb,
    externalPrivacy, setExternalPrivacy, imageMode,
    setImageMode, videoMode, setVideoMode, mageVl, setMageVl, modelTab, setModelTab,
    classDrafts, setClassDrafts, savingRouting,
    selectedProvider, selectedModelId, selectedModel, current, providerOptions,
    visionProvider, audioProvider,
    clipIngestProvider, clipIngestProviders,
    speechEngine, speechModel, audioSource, mediaProviders, filteredModels, totalModelPages,
    currentModelPage, modelPageStart, visibleModels, loadCatalog, handleModelChange,
    handleCustomModelChange, handleCustomModelConfirm, handleProviderChange,
    handleProviderSettingsSave, handleLlamaCppSettingsSave, startPull, percent, hasProviderSettings, showConnectionSettings,
    showReasoningEffort, deleteOllamaModel, saveRoutingSettings, saveExternalPrivacySettings, engineChangeError,
    agentTeamConfig, setAgentTeamConfig,
  } = useLlmModelSection();

  useEffect(() => {
    const handleTargetOpen = (event: Event) => {
      if ((event as CustomEvent<string>).detail === "llm-model") {
        setExpanded(true);
      }
    };
    window.addEventListener("settings:open-target", handleTargetOpen);
    return () => window.removeEventListener("settings:open-target", handleTargetOpen);
  }, [setExpanded]);

  const effectiveProvider = resolveEffectiveProviderId(catalog?.deployment);
  const effectiveModel = resolveEffectiveModelId(catalog?.deployment);
  const effectiveLabel = [effectiveProvider, effectiveModel].filter(Boolean).join(" / ");
  const persistedLabel = [current?.provider, current?.model].filter(Boolean).join(" / ");
  const deploymentHasMetadata = hasDeploymentMetadata(catalog?.deployment);
  const deploymentIsFixed = catalog?.deployment?.fixed === true && Boolean(effectiveProvider);
  // Child panels use the same filtered provider list as the main selector.  This
  // prevents a deployment-denied provider from resurfacing in media/routing
  // controls while preserving the original catalog metadata for diagnostics.
  const selectionCatalog = catalog
    ? { ...catalog, providers: providerOptions }
    : null;
  const localPrivacyModelOptions = (catalog?.providers ?? [])
    .filter((item) =>
      ["ollama", "sglang", "openai_compatible_local"].includes(item.id)
      && item.available !== false
      && item.disabled !== true,
    )
    .flatMap((item) =>
      (item.models ?? []).map((entry) => ({
        id: entry.id,
        label: entry.label || entry.id,
        provider: item.id,
      })),
    );
  const llamaCppRuntimeSettings = selectedProvider?.settings?.llama_cpp
    ?? selectedProvider?.settings?.runtime_settings
    ?? (selectedProvider?.settings?.runtime_profile
      ? { runtime_profile: selectedProvider.settings.runtime_profile }
      : null)
    ?? null;
  const llamaCppRuntimeProfile = llamaCppRuntimeProfileForModel(
    selectedModel,
    llamaCppRuntimeSettings,
    selectedModelId,
  );
  const showLlamaCppPanel = provider === "openai_compatible_local"
    && shouldShowLlamaCppRuntimePanel(
      selectedModelId,
      selectedModel,
      llamaCppRuntimeSettings,
    );
  const sectionItems = [
    { id: "base" as const, label: "Base Model", description: "Providerとモデル" },
    { id: "routing" as const, label: "Routing", description: "用途別の経路" },
    { id: "agent" as const, label: "Agent Team", description: "Team構成" },
    { id: "privacy" as const, label: "Privacy & Advanced", description: "保護とRuntime" },
  ];

  const openSection = (section: typeof activeSection) => {
    setActiveSection(section);
    if (section === "base") setModelTab("language");
    if (section === "routing" && modelTab === "language") setModelTab("vision");
  };

  const handleSectionKeyDown = (event: KeyboardEvent<HTMLButtonElement>, section: typeof activeSection) => {
    const currentIndex = sectionItems.findIndex((item) => item.id === section);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % sectionItems.length;
    else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + sectionItems.length) % sectionItems.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = sectionItems.length - 1;
    else return;
    event.preventDefault();
    const next = sectionItems[nextIndex].id;
    openSection(next);
    window.requestAnimationFrame(() => document.getElementById(`llm-local-tab-${next}`)?.focus());
  };

  return (
    <Card
      size="sm"
      data-settings-surface="language-models"
      data-settings-disclosure="true"
      data-settings-target="llm-model"
      className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0"
    >
      <CardHeader
        className="cursor-pointer select-none border-b border-border dark:border-[#333335] px-3 py-3 transition-colors hover:bg-muted dark:bg-[#242426]"
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        aria-controls="llm-model-content"
        onClick={() => setExpanded((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setExpanded((value) => !value);
          }
        }}
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
        <CardContent id="llm-model-content" className="space-y-4 px-3 py-4">
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              取得中...
            </div>
          ) : catalog ? (
            <>
              <div
                role="tablist"
                aria-label="言語モデル設定"
                className="sticky top-0 z-10 grid grid-cols-2 gap-1 rounded-md border border-border bg-card/95 p-1 shadow-sm backdrop-blur md:grid-cols-4"
              >
                {sectionItems.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    role="tab"
                    aria-selected={activeSection === item.id}
                    aria-controls={`llm-local-panel-${item.id}`}
                    id={`llm-local-tab-${item.id}`}
                    tabIndex={activeSection === item.id ? 0 : -1}
                    onClick={() => openSection(item.id)}
                    onKeyDown={(event) => handleSectionKeyDown(event, item.id)}
                    className={`rounded-sm border-l-2 px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                      activeSection === item.id
                        ? "border-l-primary bg-primary/10 text-foreground"
                        : "border-l-transparent text-muted-foreground hover:bg-muted hover:text-foreground"
                    }`}
                  >
                    <span className="block text-xs font-medium">{item.label}</span>
                    <span className="hidden text-[10px] text-muted-foreground sm:block">{item.description}</span>
                  </button>
                ))}
              </div>

              <div
                id={`llm-local-panel-${activeSection}`}
                role="tabpanel"
                aria-labelledby={`llm-local-tab-${activeSection}`}
                className="space-y-4"
              >

              {activeSection === "base" && deploymentHasMetadata && (
                <div
                  role="status"
                  aria-live="polite"
                  aria-label={`LLM deployment状態: ${effectiveLabel || "未指定"}`}
                  className="space-y-1 rounded-md border border-primary/40 bg-primary/5 p-3 text-xs"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={deploymentIsFixed ? "default" : "secondary"}>
                      {deploymentIsFixed ? "固定deployment" : "deployment"}
                    </Badge>
                    {effectiveLabel && (
                      <span className="font-medium">有効: {effectiveLabel}</span>
                    )}
                    {catalog.deployment?.backend && (
                      <span className="text-muted-foreground">
                        backend: {catalog.deployment.backend}
                      </span>
                    )}
                    {catalog.deployment?.transport && (
                      <span className="text-muted-foreground">
                        transport: {catalog.deployment.transport}
                      </span>
                    )}
                  </div>
                  {persistedLabel && persistedLabel !== effectiveLabel && (
                    <p className="text-muted-foreground">
                      保存済み設定: {persistedLabel}（deploymentの有効設定とは異なります）
                    </p>
                  )}
                  {catalog.deployment?.reason && (
                    <p className="text-muted-foreground">理由: {catalog.deployment.reason}</p>
                  )}
                </div>
              )}
              {activeSection === "base" && engineChangeError && (
                <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
                  LLMエンジン変更に失敗しました: {engineChangeError}
                </p>
              )}
              <Tabs value={modelTab} onValueChange={(value) => setModelTab(value as "language" | "vision" | "audio" | "video" | "clip_ingest")}>
                {activeSection === "routing" && (
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4" aria-label="用途別Routing一覧">
                    {[
                      {
                        id: "vision" as const,
                        label: "Image Recognition",
                        route: imageMode === "off" ? "Off" : classDrafts.vision.inherit ? "Base Modelを継承" : `${classDrafts.vision.provider} / ${classDrafts.vision.customModel || classDrafts.vision.model}`,
                      },
                      {
                        id: "audio" as const,
                        label: "Audio Recognition",
                        route: audioSource === "local-stt" ? `Local STT · ${speechEngine} / ${speechModel}` : audioSource === "inherit" ? "Base Modelを継承" : audioSource === "off" ? "Off" : `${classDrafts.audio.provider} / ${classDrafts.audio.customModel || classDrafts.audio.model}`,
                      },
                      {
                        id: "video" as const,
                        label: "Video Recognition",
                        route: videoMode === "off"
                          ? "Route: Off"
                          : `Route: Auto · Runtime: ${mageVl.enabled === false ? "Disabled" : "Enabled"} · ${classDrafts.video.customModel || classDrafts.video.model || mageVl.model}`,
                      },
                      {
                        id: "clip_ingest" as const,
                        label: "Clip Ingest",
                        route: classDrafts.clip_ingest.inherit ? "Base Modelを継承" : `${classDrafts.clip_ingest.provider} / ${classDrafts.clip_ingest.customModel || classDrafts.clip_ingest.model}`,
                      },
                    ].map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => setModelTab(item.id)}
                        aria-pressed={modelTab === item.id}
                        className={`min-h-16 rounded-sm border border-l-2 px-3 py-2 text-left transition-colors ${modelTab === item.id ? "border-l-primary bg-primary/10" : "border-l-transparent bg-muted/35 hover:bg-muted"}`}
                      >
                        <span className="block text-xs font-medium">{item.label}</span>
                        <span className="mt-1 block truncate text-[10px] text-muted-foreground">{item.route}</span>
                      </button>
                    ))}
                  </div>
                )}
                 <TabsList variant="line" className={`w-full justify-start gap-1 rounded-none border-b border-border p-0 ${activeSection === "agent" || activeSection === "privacy" ? "hidden" : ""}`}>
                  {activeSection === "base" && <TabsTrigger value="language">基本モデル</TabsTrigger>}
                  {activeSection === "routing" && <TabsTrigger value="vision">画像認識</TabsTrigger>}
                  {activeSection === "routing" && <TabsTrigger value="audio">音声認識</TabsTrigger>}
                  {activeSection === "routing" && <TabsTrigger value="video">動画認識</TabsTrigger>}
                  {activeSection === "routing" && <TabsTrigger value="clip_ingest">クリップ取り込み</TabsTrigger>}
                </TabsList>

                {activeSection === "routing" && <LlmMediaModelTabs
                  catalog={selectionCatalog ?? catalog}
                  classDrafts={classDrafts}
                  setClassDrafts={setClassDrafts}
                  savingRouting={savingRouting}
                  imageMode={imageMode}
                  setImageMode={setImageMode}
                  videoMode={videoMode}
                  setVideoMode={setVideoMode}
                  mageVl={mageVl}
                  setMageVl={setMageVl}
                  visionProvider={visionProvider}
                  audioProvider={audioProvider}
                  clipIngestProvider={clipIngestProvider}
                  clipIngestProviders={clipIngestProviders}
                  audioSource={audioSource}
                  speechEngine={speechEngine}
                  speechModel={speechModel}
                  mediaProviders={mediaProviders}
                  saveRoutingSettings={saveRoutingSettings}
                />}
                <TabsContent value="language" className={activeSection === "base" ? "mt-3 space-y-3" : "hidden"}>
              <div className={modelTab === "language" ? "grid gap-3 md:grid-cols-[220px_1fr]" : "hidden"}>
                <div className="space-y-1">
                  <Label className="text-xs">プロバイダー</Label>
                  <AppSelect
                    aria-label="LLMプロバイダー"
                    value={provider}
                    onChange={(event) => handleProviderChange(event.target.value)}
                    disabled={saving}
                    className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                  >
                    {providerOptions.map((item) => (
                      <option key={item.id} value={item.id} disabled={item.disabled}>
                        {item.label}
                      </option>
                    ))}
                  </AppSelect>
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">モデル</Label>
                  <AppSelect
                    aria-label="LLMモデル"
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
                  </AppSelect>
                </div>
              </div>

              {selectedProvider?.selection_kind !== "routing_profile" && <div className={modelTab === "language" ? "space-y-1" : "hidden"}>
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
                {provider === "openai_compatible_local"
                  && selectedModelId.trim()
                  && !showLlamaCppPanel && (
                    <p className="text-[10px] text-muted-foreground">
                      この候補は既存の専用local launcherが管理するため、generic llama.cpp設定は適用しません。
                    </p>
                  )}
              </div>}
                </TabsContent>
              </Tabs>

              <div className={activeSection === "privacy" ? "space-y-4" : "hidden"} aria-hidden={activeSection !== "privacy"}>
                  <div>
                    <h3 className="text-sm font-medium">Privacy & Advanced</h3>
                    <p className="text-[11px] text-muted-foreground">外部送信の保護、Provider表示、固有Routing、ローカルRuntimeを管理します。</p>
                  </div>
                  <LlmProviderVisibilitySettings providers={providerOptions} />

                  {provider === "openrouter" && (
                    <OpenRouterProviderRouting model={model} />
                  )}

                  {showLlamaCppPanel && (
                    <LlamaCppRuntimePanel
                      selectedModelId={selectedModelId}
                      runtimeProfile={llamaCppRuntimeProfile}
                      runtimeSettings={llamaCppRuntimeSettings}
                      draft={llamaCppDraft}
                      setDraft={setLlamaCppDraft}
                      saving={saving}
                      error={llamaCppError}
                      onSave={handleLlamaCppSettingsSave}
                    />
                  )}

                  {selectedProvider?.selection_kind === "routing_profile" && (
                    <FreeTeamSettingsPanel />
                  )}

                  <LlmExternalPrivacySettings
                    value={externalPrivacy}
                    onChange={setExternalPrivacy}
                    onSave={saveExternalPrivacySettings}
                    saving={savingRouting}
                    localModelOptions={localPrivacyModelOptions}
                  />
              </div>

              <div className={activeSection === "base" ? "block" : "hidden"} aria-hidden={activeSection !== "base"}>
              <LlmModelGroupsPanel
                catalog={selectionCatalog ?? catalog}
                imageMode={imageMode}
                setImageMode={setImageMode}
                classDrafts={classDrafts}
                setClassDrafts={setClassDrafts}
                savingRouting={savingRouting}
                connection={{
                  selectedModelId,
                  selectedProvider,
                  baseUrl,
                  setBaseUrl,
                  apiKey,
                  setApiKey,
                  reasoningEffort,
                  setReasoningEffort,
                  saving,
                  hasProviderSettings,
                  showConnectionSettings,
                  showReasoningEffort,
                  handleProviderSettingsSave,
                }}
              />
              </div>

              {activeSection === "base" && <div className="flex flex-wrap items-center gap-2">
                {selectedProvider?.selection_kind !== "routing_profile" && <Button
                  size="sm"
                  variant="outline"
                  onClick={() => loadCatalog(true, provider)}
                  disabled={refreshing || saving}
                >
                  <RefreshCcw className={`mr-1 size-3 ${refreshing ? "animate-spin" : ""}`} />
                  選択中の候補を更新
                </Button>}
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
              </div>}

              {activeSection === "base" && provider === "ollama" && (
                  <div className="space-y-3 rounded-md border border-border dark:border-[#333335] bg-muted/45 dark:bg-[#242426]/45 p-3">
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

              {activeSection === "base" && <Separator />}

              {activeSection === "base" && <div className="space-y-2">
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
                       className={`flex min-h-14 items-center gap-2 rounded-sm border border-border dark:border-[#333335] px-3 py-2 transition-colors hover:bg-muted dark:bg-[#242426] ${
                           selectedModelId === item.id ? "border-primary bg-primary/10" : "bg-background dark:bg-[#131313]"
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
              </div>}

              {activeSection === "base" && <Separator />}

              <div className={activeSection === "agent" ? "block" : "hidden"} aria-hidden={activeSection !== "agent"}>
              <LlmAgentTeamRouting
                catalog={selectionCatalog ?? catalog}
                provider={provider}
                selectedModelId={selectedModelId}
                delegationEnabled={delegationEnabled}
                setDelegationEnabled={setDelegationEnabled}
                orchestrationMode={orchestrationMode}
                setOrchestrationMode={setOrchestrationMode}
                chatgptWeb={chatgptWeb}
                setChatgptWeb={setChatgptWeb}
                savingRouting={savingRouting}
                saveRoutingSettings={() => saveRoutingSettings("agent")}
                agentTeamConfig={agentTeamConfig}
                setAgentTeamConfig={setAgentTeamConfig}
              />
              </div>
              </div>
              {sectionItems
                .filter((item) => item.id !== activeSection)
                .map((item) => (
                  <div
                    key={item.id}
                    id={`llm-local-panel-${item.id}`}
                    role="tabpanel"
                    aria-labelledby={`llm-local-tab-${item.id}`}
                    hidden
                  />
                ))}
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

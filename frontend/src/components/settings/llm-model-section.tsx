"use client";

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
import { FreeTeamSettingsPanel } from "./free-team-settings-panel";
import { useLlmModelSection } from "./llm-model-section-state";
import {
  modelSourceLabel,
  modelSummary,
  providerHint,
  providerSourceLabel,
} from "./llm-model-section-types";

export function LlmModelSection() {
  const {
    expanded, setExpanded, catalog, provider, model, customModel, loading, refreshing, saving,
    pulling, pullInput, setPullInput, task, deletingModel, modelSearch, setModelSearch, setModelPage,
    baseUrl, setBaseUrl, apiKey, setApiKey, reasoningEffort, setReasoningEffort,
    delegationEnabled, routingConfirmPrompt, setRoutingConfirmPrompt, routingNotify,
    setRoutingNotify, agentTeamRedactionText, setAgentTeamRedactionText, imageMode,
    setImageMode, routingDetailsOpen, setRoutingDetailsOpen, modelTab, setModelTab,
    classDrafts, setClassDrafts, routingDrafts, setRoutingDrafts, modelGroups, setModelGroups, savingRouting,
    selectedProvider, selectedModelId, selectedModel, current, visionProvider, audioProvider,
    speechEngine, speechModel, audioSource, mediaProviders, filteredModels, totalModelPages,
    currentModelPage, modelPageStart, visibleModels, loadCatalog, handleModelChange,
    handleCustomModelChange, handleCustomModelConfirm, handleProviderChange,
    handleProviderSettingsSave, startPull, percent, hasProviderSettings, showConnectionSettings,
    showReasoningEffort, updateRoutingDraft, handleModelDragStart, routeProviderOptions,
    handleMemberDrop, changeDelegationEnabled, deleteOllamaModel, saveRoutingSettings,
  } = useLlmModelSection();

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
              <Tabs value={modelTab} onValueChange={(value) => setModelTab(value as "language" | "vision" | "audio")}>
                <TabsList>
                  <TabsTrigger value="language">言語</TabsTrigger>
                  <TabsTrigger value="vision">画像認識</TabsTrigger>
                  <TabsTrigger value="audio">音声認識</TabsTrigger>
                </TabsList>

                <LlmMediaModelTabs
                  catalog={catalog}
                  classDrafts={classDrafts}
                  setClassDrafts={setClassDrafts}
                  savingRouting={savingRouting}
                  imageMode={imageMode}
                  setImageMode={setImageMode}
                  visionProvider={visionProvider}
                  audioProvider={audioProvider}
                  audioSource={audioSource}
                  speechEngine={speechEngine}
                  speechModel={speechModel}
                  mediaProviders={mediaProviders}
                  saveRoutingSettings={saveRoutingSettings}
                />
                <TabsContent value="language" className="mt-3 space-y-3">
              <div className={modelTab === "language" ? "grid gap-3 md:grid-cols-[220px_1fr]" : "hidden"}>
                <div className="space-y-1">
                  <Label className="text-xs">プロバイダー</Label>
                  <select
                    value={provider}
                    onChange={(event) => handleProviderChange(event.target.value)}
                    disabled={saving}
                    className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                  >
                    {catalog.providers.map((item) => (
                      <option key={item.id} value={item.id} disabled={item.disabled}>
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
              </div>}
                </TabsContent>
              </Tabs>

              <LlmModelGroupsPanel
                catalog={catalog}
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

              {selectedProvider?.selection_kind === "routing_profile" && (
                <FreeTeamSettingsPanel />
              )}

              <div className="flex flex-wrap items-center gap-2">
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
                         draggable={selectedProvider?.selection_kind !== "routing_profile"}
                         onDragStart={(event) => {
                           if (selectedProvider?.selection_kind !== "routing_profile") {
                             handleModelDragStart(event, item.id);
                           }
                         }}
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

              {selectedProvider?.selection_kind !== "routing_profile" && <LlmAgentTeamRouting
                catalog={catalog}
                provider={provider}
                selectedModelId={selectedModelId}
                current={current}
                modelGroups={modelGroups}
                setModelGroups={setModelGroups}
                setRoutingDrafts={setRoutingDrafts}
                routingDrafts={routingDrafts}
                updateRoutingDraft={updateRoutingDraft}
                routeProviderOptions={routeProviderOptions}
                handleMemberDrop={handleMemberDrop}
                delegationEnabled={delegationEnabled}
                changeDelegationEnabled={changeDelegationEnabled}
                routingConfirmPrompt={routingConfirmPrompt}
                setRoutingConfirmPrompt={setRoutingConfirmPrompt}
                routingNotify={routingNotify}
                setRoutingNotify={setRoutingNotify}
                agentTeamRedactionText={agentTeamRedactionText}
                setAgentTeamRedactionText={setAgentTeamRedactionText}
                routingDetailsOpen={routingDetailsOpen}
                setRoutingDetailsOpen={setRoutingDetailsOpen}
                savingRouting={savingRouting}
                saveRoutingSettings={saveRoutingSettings}
              />}
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

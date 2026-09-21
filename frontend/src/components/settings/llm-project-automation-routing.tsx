"use client";

import { type Dispatch, type SetStateAction } from "react";

import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TabsContent } from "@/components/ui/tabs";
import {
  defaultModeForOptions,
  providerSelection,
  reasoningEffortOptionsForModel,
  type LlmProviderCatalog,
  type ModelClassDraft,
} from "./llm-model-section-types";

type LlmProjectAutomationRoutingProps = {
  draft: ModelClassDraft;
  setDraft: Dispatch<SetStateAction<ModelClassDraft>>;
  providers: LlmProviderCatalog[];
  saving: boolean;
  onSave: () => void | Promise<void>;
};

export function LlmProjectAutomationRouting({
  draft,
  setDraft,
  providers,
  saving,
  onSave,
}: LlmProjectAutomationRoutingProps) {
  const inherit = draft.inherit ?? true;
  const selectedProvider = providers.find((item) => item.id === draft.provider);
  const selectedModelId = draft.customModel.trim() || draft.model;
  const effortOptions = reasoningEffortOptionsForModel(
    selectedProvider,
    selectedModelId,
  );

  const selectProvider = (providerId: string) => {
    if (!providerId) {
      setDraft((current) => ({
        ...current,
        provider: "",
        model: "",
        customModel: "",
        mode: "",
        baseUrl: "",
        apiKey: "",
      }));
      return;
    }

    const provider = providers.find((item) => item.id === providerId);
    const selection = providerSelection(provider);
    const modelId = selection.customModel.trim() || selection.model;
    const nextEffortOptions = reasoningEffortOptionsForModel(provider, modelId);

    setDraft((current) => ({
      ...current,
      inherit: false,
      provider: providerId,
      ...selection,
      mode: nextEffortOptions.length
        ? defaultModeForOptions(nextEffortOptions, current.mode || "medium")
        : "",
      baseUrl: "",
      apiKey: "",
    }));
  };

  const selectModel = (modelId: string) => {
    const nextEffortOptions = reasoningEffortOptionsForModel(
      selectedProvider,
      modelId,
    );
    setDraft((current) => ({
      ...current,
      model: modelId,
      customModel: "",
      mode: nextEffortOptions.length
        ? defaultModeForOptions(nextEffortOptions, current.mode || "medium")
        : "",
    }));
  };

  return (
    <TabsContent
      value="project_automation"
      className="mt-3 space-y-3"
      data-testid="project-automation-routing-panel"
    >
      <p className="text-xs text-muted-foreground">
        Project Overview / Project Steward の自動化処理に使うモデルです。既定では Base Model を継承します。
      </p>

      <div className="max-w-xs space-y-1">
        <Label className="text-xs">Route</Label>
        <AppSelect
          aria-label="Project Automation route"
          value={inherit ? "inherit" : "dedicated"}
          onChange={(event) => {
            if (event.target.value === "inherit") {
              setDraft((current) => ({ ...current, inherit: true }));
              return;
            }

            if (draft.provider || providers.length === 0) {
              setDraft((current) => ({ ...current, inherit: false }));
              return;
            }

            selectProvider(providers[0].id);
          }}
          disabled={saving}
          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
        >
          <option value="inherit">Base Modelを継承</option>
          <option value="dedicated">Dedicated model</option>
        </AppSelect>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-1">
          <Label className="text-xs">プロバイダー</Label>
          <AppSelect
            aria-label="Project Automation provider"
            value={draft.provider}
            onChange={(event) => selectProvider(event.target.value)}
            disabled={saving || inherit}
            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
          >
            <option value="">プロバイダーを選択</option>
            {providers.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </AppSelect>
        </div>

        <div className="space-y-1">
          <Label className="text-xs">モデル</Label>
          <AppSelect
            aria-label="Project Automation model"
            value={draft.model}
            onChange={(event) => selectModel(event.target.value)}
            disabled={saving || inherit || !selectedProvider}
            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
          >
            {(selectedProvider?.models ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </AppSelect>
        </div>
      </div>

      {!inherit && (
        <>
          <div className="space-y-1">
            <Label className="text-xs">カスタムモデルID</Label>
            <Input
              aria-label="Project Automation custom model"
              value={draft.customModel}
              onChange={(event) => {
                const customModel = event.target.value;
                const modelId = customModel.trim() || draft.model;
                const nextEffortOptions = reasoningEffortOptionsForModel(
                  selectedProvider,
                  modelId,
                );
                setDraft((current) => ({
                  ...current,
                  customModel,
                  mode: nextEffortOptions.length
                    ? defaultModeForOptions(
                      nextEffortOptions,
                      current.mode || "medium",
                    )
                    : "",
                }));
              }}
              placeholder="候補にないモデルIDを直接入力"
              disabled={saving}
              className="h-8"
            />
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1">
              <Label className="text-xs">Base URL（任意）</Label>
              <Input
                aria-label="Project Automation base URL"
                value={draft.baseUrl}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    baseUrl: event.target.value,
                  }))
                }
                disabled={saving}
                className="h-8"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">APIキー（任意）</Label>
              <Input
                aria-label="Project Automation API key"
                type="password"
                value={draft.apiKey}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    apiKey: event.target.value,
                  }))
                }
                placeholder="空欄なら既存値を変更しない"
                disabled={saving}
                className="h-8"
              />
            </div>
          </div>

          {effortOptions.length > 0 && (
            <div className="max-w-xs space-y-1">
              <Label className="text-xs">Effort</Label>
              <AppSelect
                aria-label="Project Automation effort"
                value={draft.mode || defaultModeForOptions(effortOptions)}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    mode: event.target.value,
                  }))
                }
                disabled={saving}
                className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
              >
                {effortOptions.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </AppSelect>
            </div>
          )}
        </>
      )}

      <div>
        <Button size="sm" onClick={() => void onSave()} disabled={saving}>
          {saving ? "保存中..." : "Project Automation設定を保存"}
        </Button>
      </div>
    </TabsContent>
  );
}

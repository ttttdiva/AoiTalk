"use client";

import { AppSelect } from "@/components/ui/app-select";

import type { Dispatch, SetStateAction } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  providerSelection,
  reasoningEffortOptionsForModel,
  type LlmModelCatalogResponse,
  type ModelClassDraft,
} from "./llm-model-section-types";

type ClassDraftMap = Record<"vision" | "audio" | "video" | "clip_ingest", ModelClassDraft>;

export type ConnectionSettingsProps = {
  selectedModelId: string;
  selectedProvider: LlmModelCatalogResponse["providers"][number] | null;
  baseUrl: string;
  setBaseUrl: Dispatch<SetStateAction<string>>;
  apiKey: string;
  setApiKey: Dispatch<SetStateAction<string>>;
  reasoningEffort: string;
  setReasoningEffort: Dispatch<SetStateAction<string>>;
  saving: boolean;
  hasProviderSettings: boolean;
  showConnectionSettings: boolean;
  showReasoningEffort: boolean;
  handleProviderSettingsSave: () => void;
};

export function LlmModelGroupsPanel({
  catalog,
  imageMode,
  setImageMode,
  classDrafts,
  setClassDrafts,
  savingRouting,
  connection,
}: {
  catalog: LlmModelCatalogResponse;
  imageMode: "auto" | "always" | "off";
  setImageMode: Dispatch<SetStateAction<"auto" | "always" | "off">>;
  classDrafts: ClassDraftMap;
  setClassDrafts: Dispatch<SetStateAction<ClassDraftMap>>;
  savingRouting: boolean;
  connection: ConnectionSettingsProps;
}) {
  const {
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
  } = connection;
  const reasoningEffortOptions = reasoningEffortOptionsForModel(
    selectedProvider,
    selectedModelId,
  );
  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="hidden">
        <div className="space-y-2 rounded border p-2">
          <div className="flex items-center justify-between gap-2">
            <div className="text-xs font-medium">画像認識</div>
            <AppSelect
              value={imageMode}
              onChange={(event) => setImageMode(event.target.value as "auto" | "always" | "off")}
              disabled={savingRouting}
              className="h-8 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
            >
              <option value="auto">auto</option>
              <option value="always">always</option>
              <option value="off">off</option>
            </AppSelect>
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            <AppSelect
              value={classDrafts.vision.provider}
              onChange={(event) => {
                const nextProvider = event.target.value;
                const next = (catalog?.providers ?? []).find((item) => item.id === nextProvider);
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
              {(catalog?.providers ?? []).filter((item) => !item.id.endsWith("-cli") && item.id !== "deepseek").map((item) => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </AppSelect>
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
          <AppSelect
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
          </AppSelect>
          {classDrafts.audio.engine === "llm" && (
            <div className="grid gap-2 md:grid-cols-2">
              <AppSelect
                value={classDrafts.audio.provider}
                onChange={(event) => {
                  const nextProvider = event.target.value;
                  const next = (catalog?.providers ?? []).find((item) => item.id === nextProvider);
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
                {(catalog?.providers ?? [])
                  .filter((item) => ["openai", "gemini", "openrouter", "openai_compatible_local", "sglang", "ollama"].includes(item.id))
                  .map((item) => (
                    <option key={item.id} value={item.id}>{item.label}</option>
                  ))}
              </AppSelect>
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
          <AppSelect
            value={reasoningEffort}
            onChange={(event) => setReasoningEffort(event.target.value)}
            disabled={saving}
            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
          >
            {(reasoningEffortOptions.length ? reasoningEffortOptions : ["medium"]).map(
              (item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ),
            )}
          </AppSelect>
        </div>
      )}

      {(hasProviderSettings || showConnectionSettings || showReasoningEffort) && (
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
      )}
    </div>
  );
}

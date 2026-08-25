"use client";

import { AppSelect } from "@/components/ui/app-select";

import { useState, type Dispatch, type SetStateAction } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  defaultModeForOptions,
  providerSelection,
  type LlmModelCatalogResponse,
  type LlmProviderCatalog,
  type MageVLSettings,
  type ModelClassDraft,
} from "./llm-model-section-types";

type ClassDraftMap = Record<"vision" | "audio" | "video" | "clip_ingest", ModelClassDraft>;
type MediaRoutingSaveScope = "vision" | "audio" | "video" | "clip_ingest";

const PROVIDER_WIDE_EFFORT_OPTIONS = new Set(["codex-cli", "claude-cli"]);

export function clipIngestReasoningEffortOptions(
  provider: LlmProviderCatalog | undefined,
  modelId: string,
): string[] {
  const modelOptions = provider?.models.find(
    (item) => item.id === modelId,
  )?.reasoning_effort_options;
  if (modelOptions?.length) return modelOptions;
  if (provider && PROVIDER_WIDE_EFFORT_OPTIONS.has(provider.id)) {
    return provider.settings?.reasoning_effort_options ?? [];
  }
  return [];
}

type MediaModelTabsProps = {
  catalog: LlmModelCatalogResponse;
  classDrafts: ClassDraftMap;
  setClassDrafts: Dispatch<SetStateAction<ClassDraftMap>>;
  savingRouting: boolean;
  imageMode: "auto" | "always" | "off";
  setImageMode: Dispatch<SetStateAction<"auto" | "always" | "off">>;
  videoMode: "auto" | "off";
  setVideoMode: Dispatch<SetStateAction<"auto" | "off">>;
  mageVl: MageVLSettings;
  setMageVl: Dispatch<SetStateAction<MageVLSettings>>;
  visionProvider: LlmProviderCatalog | undefined;
  audioProvider: LlmProviderCatalog | undefined;
  clipIngestProvider: LlmProviderCatalog | undefined;
  clipIngestProviders: LlmProviderCatalog[];
  audioSource: string;
  speechEngine: string;
  speechModel: string;
  mediaProviders: (kind: "image" | "audio") => LlmProviderCatalog[];
  saveRoutingSettings: (scope: MediaRoutingSaveScope) => void | Promise<void>;
};

export function LlmMediaModelTabs({
  catalog,
  classDrafts,
  setClassDrafts,
  savingRouting,
  imageMode,
  setImageMode,
  videoMode,
  setVideoMode,
  mageVl,
  setMageVl,
  visionProvider,
  audioProvider,
  clipIngestProvider,
  clipIngestProviders,
  audioSource,
  speechEngine,
  speechModel,
  mediaProviders,
  saveRoutingSettings,
}: MediaModelTabsProps) {
  const [videoAdvancedOpen, setVideoAdvancedOpen] = useState(false);
  const videoDraft = classDrafts.video;
  const clipIngestDraft = classDrafts.clip_ingest;
  const clipIngestInherit = clipIngestDraft.inherit ?? true;
  const clipIngestModelId =
    clipIngestDraft.customModel.trim() || clipIngestDraft.model;
  const clipIngestEffortOptions = clipIngestReasoningEffortOptions(
    clipIngestProvider,
    clipIngestModelId,
  );
  const showClipIngestEffort =
    !clipIngestInherit && clipIngestEffortOptions.length > 0;

  return (
    <>
      <TabsContent value="vision" className="mt-3 space-y-3">
        <p className="text-xs text-muted-foreground">既定では言語モデルを使います。対応しないモデルの場合のみ、画像対応モデルを指定してください。</p>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <Label className="text-xs">プロバイダー</Label>
            <AppSelect
              value={classDrafts.vision.inherit ? "inherit" : classDrafts.vision.provider}
              onChange={(event) => {
                const nextProvider = event.target.value;
                if (nextProvider === "inherit") {
                  setClassDrafts((current) => ({ ...current, vision: { ...current.vision, inherit: true, provider: "", model: "", customModel: "" } }));
                  return;
                }
                const selection = providerSelection(catalog.providers.find((item) => item.id === nextProvider));
                setClassDrafts((current) => ({ ...current, vision: { ...current.vision, inherit: false, provider: nextProvider, ...selection } }));
              }}
              disabled={savingRouting}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
            >
              <option value="inherit">言語モデルと同じ</option>
              {mediaProviders("image").map((item) => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </AppSelect>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">モデル</Label>
            <AppSelect
              value={classDrafts.vision.model}
              onChange={(event) => setClassDrafts((current) => ({ ...current, vision: { ...current.vision, model: event.target.value, customModel: "" } }))}
              disabled={savingRouting || classDrafts.vision.inherit}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
            >
              {(visionProvider?.models ?? []).filter((item) => item.media?.image).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </AppSelect>
          </div>
        </div>
        <div className="flex items-center justify-between gap-2">
          <Label className="text-xs">画像の送信方法</Label>
          <AppSelect value={imageMode} onChange={(event) => setImageMode(event.target.value as "auto" | "always" | "off")} disabled={savingRouting} className="h-8 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30">
            <option value="auto">自動</option><option value="always">常に認識モデルを使用</option><option value="off">無効</option>
          </AppSelect>
        </div>
        <div><Button size="sm" onClick={() => void saveRoutingSettings("vision")} disabled={savingRouting}>{savingRouting ? "保存中..." : "画像認識設定を保存"}</Button></div>
      </TabsContent>

      <TabsContent value="audio" className="mt-3 space-y-3">
        <p className="text-xs text-muted-foreground">音声対応が確認できるモデルだけを選べます。ローカルSTTは設定済みの実モデル名を表示します。</p>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <Label className="text-xs">プロバイダー</Label>
            <AppSelect
              value={audioSource}
              onChange={(event) => {
                const next = event.target.value;
                if (next === "local-stt") setClassDrafts((current) => ({ ...current, audio: { ...current.audio, engine: "speech_recognition", inherit: false, provider: "", model: "", customModel: "" } }));
                else if (next === "inherit") setClassDrafts((current) => ({ ...current, audio: { ...current.audio, engine: "llm", inherit: true, provider: "", model: "", customModel: "" } }));
                else if (next === "off") setClassDrafts((current) => ({ ...current, audio: { ...current.audio, engine: "off", inherit: false } }));
                else {
                  const selection = providerSelection(catalog.providers.find((item) => item.id === next));
                  setClassDrafts((current) => ({ ...current, audio: { ...current.audio, engine: "llm", inherit: false, provider: next, ...selection } }));
                }
              }}
              disabled={savingRouting}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
            >
              <option value="local-stt">ローカルSTT</option>
              <option value="inherit">言語モデルと同じ</option>
              {mediaProviders("audio").map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              <option value="off">無効</option>
            </AppSelect>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">モデル</Label>
            {audioSource === "local-stt" ? (
              <div className="flex h-8 items-center rounded-lg border border-input px-2.5 text-sm">{speechEngine} / {speechModel}</div>
            ) : (
              <AppSelect value={classDrafts.audio.model} onChange={(event) => setClassDrafts((current) => ({ ...current, audio: { ...current.audio, model: event.target.value, customModel: "" } }))} disabled={savingRouting || audioSource === "inherit" || audioSource === "off"} className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30">
                {(audioProvider?.models ?? []).filter((item) => item.media?.audio).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </AppSelect>
            )}
          </div>
        </div>
        <div><Button size="sm" onClick={() => void saveRoutingSettings("audio")} disabled={savingRouting}>{savingRouting ? "保存中..." : "音声認識設定を保存"}</Button></div>
      </TabsContent>

      <TabsContent value="video" className="mt-3 space-y-3">
        <p className="text-xs text-muted-foreground">
          Microsoft Mage-VLをSGLangのOpenAI互換サーバーとして使います。動画は保存済みパスから等間隔フレームを抽出し、1回の認識リクエストにまとめて送信します。
        </p>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <Label className="text-xs">動画認識</Label>
            <AppSelect
              value={videoMode}
              onChange={(event) => setVideoMode(event.target.value as "auto" | "off")}
              disabled={savingRouting}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
            >
              <option value="auto">自動（Mage-VL）</option>
              <option value="off">無効</option>
            </AppSelect>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">プロバイダー</Label>
            <div className="flex h-8 items-center rounded-lg border border-input px-2.5 text-sm">
              mage_vl（Microsoft Mage-VL）
            </div>
          </div>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <Label className="text-xs">モデルID</Label>
            <Input
              value={videoDraft.customModel || videoDraft.model || mageVl.model || "microsoft/Mage-VL"}
              onChange={(event) => setClassDrafts((current) => ({
                ...current,
                video: { ...current.video, provider: "mage_vl", model: "", customModel: event.target.value, inherit: false },
              }))}
              disabled={savingRouting || videoMode === "off"}
              className="h-8"
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">SGLang Base URL</Label>
            <Input
              value={videoDraft.baseUrl || mageVl.base_url || ""}
              onChange={(event) => setClassDrafts((current) => ({ ...current, video: { ...current.video, baseUrl: event.target.value } }))}
              placeholder="http://127.0.0.1:30000/v1"
              disabled={savingRouting}
              className="h-8"
            />
          </div>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">APIキー（任意・空欄ならdummy）</Label>
          <Input
            type="password"
            value={videoDraft.apiKey}
            onChange={(event) => setClassDrafts((current) => ({ ...current, video: { ...current.video, apiKey: event.target.value } }))}
            disabled={savingRouting}
            className="h-8"
            placeholder={mageVl.api_key_configured ? "設定済み（変更時のみ入力）" : "dummy"}
          />
        </div>
        <div className="flex items-center gap-2">
          <Checkbox id="mage-vl-enabled" checked={mageVl.enabled !== false} onCheckedChange={(checked) => setMageVl((current) => ({ ...current, enabled: checked === true }))} disabled={savingRouting} />
          <Label htmlFor="mage-vl-enabled" className="cursor-pointer text-xs font-normal">Mage-VLを有効にする</Label>
        </div>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-8 justify-start px-2 text-xs"
          onClick={() => setVideoAdvancedOpen((value) => !value)}
          aria-expanded={videoAdvancedOpen}
          aria-controls="video-runtime-advanced"
        >
          {videoAdvancedOpen ? <ChevronUp className="mr-1 size-3" /> : <ChevronDown className="mr-1 size-3" />}
          Runtime Configuration / Advanced
        </Button>
        {videoAdvancedOpen && <div id="video-runtime-advanced" className="space-y-3 rounded-md border bg-muted/35 p-3">
        <div className="grid gap-3 md:grid-cols-2">
          <div className="flex items-center gap-2">
            <Checkbox id="mage-vl-managed" checked={mageVl.managed !== false} onCheckedChange={(checked) => setMageVl((current) => ({ ...current, managed: checked === true }))} disabled={savingRouting} />
            <Label htmlFor="mage-vl-managed" className="cursor-pointer text-xs font-normal">AoiTalkがSGLangを管理</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox id="mage-vl-preload" checked={mageVl.preload_on_start === true} onCheckedChange={(checked) => setMageVl((current) => ({ ...current, preload_on_start: checked === true }))} disabled={savingRouting} />
            <Label htmlFor="mage-vl-preload" className="cursor-pointer text-xs font-normal">起動時に事前ロード</Label>
          </div>
        </div>
        <div className="grid gap-3 md:grid-cols-4">
          <div className="space-y-1">
            <Label className="text-xs">フレーム数</Label>
            <Input type="number" min={1} max={128} value={mageVl.num_frames ?? 32} onChange={(event) => setMageVl((current) => ({ ...current, num_frames: Number(event.target.value) }))} disabled={savingRouting} className="h-8" />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">最大動画サイズ（bytes）</Label>
            <Input type="number" min={1} value={mageVl.max_video_bytes ?? 52428800} onChange={(event) => setMageVl((current) => ({ ...current, max_video_bytes: Number(event.target.value) }))} disabled={savingRouting} className="h-8" />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">最大長（秒）</Label>
            <Input type="number" min={0} value={mageVl.max_video_duration_seconds ?? 300} onChange={(event) => setMageVl((current) => ({ ...current, max_video_duration_seconds: Number(event.target.value) }))} disabled={savingRouting} className="h-8" />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">起動待ち（秒）</Label>
            <Input type="number" min={1} value={mageVl.startup_timeout_seconds ?? 300} onChange={(event) => setMageVl((current) => ({ ...current, startup_timeout_seconds: Number(event.target.value) }))} disabled={savingRouting} className="h-8" />
          </div>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">外部SGLangを使う場合の起動コマンド（任意）</Label>
          <Input
            value={mageVl.server_command ?? ""}
            onChange={(event) => setMageVl((current) => ({ ...current, server_command: event.target.value }))}
            disabled={savingRouting}
            className="h-8"
            placeholder="python -m sglang.launch_server --model-path microsoft/Mage-VL ..."
          />
        </div>
        <p className="text-xs text-muted-foreground">
          状態: {mageVl.state?.state ?? "unloaded"}{mageVl.state?.error ? ` / ${mageVl.state.error}` : ""}
        </p>
        </div>}
        <div><Button size="sm" onClick={() => void saveRoutingSettings("video")} disabled={savingRouting}>{savingRouting ? "保存中..." : "動画認識設定を保存"}</Button></div>
      </TabsContent>

      <TabsContent value="clip_ingest" className="mt-3 space-y-3">
        <p className="text-xs text-muted-foreground">クリップ取り込み（Docsへの保存計画の生成）に使うモデルです。既定では言語モデルを使います。</p>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <Label className="text-xs">プロバイダー</Label>
            <AppSelect
              value={clipIngestInherit ? "inherit" : clipIngestDraft.provider}
              onChange={(event) => {
                const nextProvider = event.target.value;
                if (nextProvider === "inherit") {
                  setClassDrafts((current) => ({ ...current, clip_ingest: { ...current.clip_ingest, inherit: true, provider: "", model: "", customModel: "" } }));
                  return;
                }
                const nextProviderCatalog = catalog.providers.find((item) => item.id === nextProvider);
                const selection = providerSelection(nextProviderCatalog);
                const selectedModelId = selection.customModel || selection.model;
                const effortOptions = clipIngestReasoningEffortOptions(
                  nextProviderCatalog,
                  selectedModelId,
                );
                setClassDrafts((current) => ({
                  ...current,
                  clip_ingest: {
                    ...current.clip_ingest,
                    inherit: false,
                    provider: nextProvider,
                    ...selection,
                    mode: effortOptions.length
                      ? defaultModeForOptions(
                        effortOptions,
                        current.clip_ingest.mode || "medium",
                      )
                      : "",
                  },
                }));
              }}
              disabled={savingRouting}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
            >
              <option value="inherit">言語モデルと同じ</option>
              {clipIngestProviders.map((item) => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </AppSelect>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">モデル</Label>
            <AppSelect
              value={clipIngestDraft.model}
              onChange={(event) => {
                const nextModel = event.target.value;
                const effortOptions = clipIngestReasoningEffortOptions(
                  clipIngestProvider,
                  nextModel,
                );
                setClassDrafts((current) => ({
                  ...current,
                  clip_ingest: {
                    ...current.clip_ingest,
                    model: nextModel,
                    customModel: "",
                    mode: effortOptions.length
                      ? defaultModeForOptions(
                        effortOptions,
                        current.clip_ingest.mode || "medium",
                      )
                      : "",
                  },
                }));
              }}
              disabled={savingRouting || clipIngestInherit}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
            >
              {(clipIngestProvider?.models ?? []).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </AppSelect>
          </div>
        </div>
        {!clipIngestInherit && (
          <div className="space-y-1">
            <Label className="text-xs">カスタムモデルID</Label>
            <Input
              value={clipIngestDraft.customModel}
              onChange={(event) => {
                const customModel = event.target.value;
                const effectiveModel = customModel.trim() || clipIngestDraft.model;
                const effortOptions = clipIngestReasoningEffortOptions(
                  clipIngestProvider,
                  effectiveModel,
                );
                setClassDrafts((current) => ({
                  ...current,
                  clip_ingest: {
                    ...current.clip_ingest,
                    customModel,
                    mode: effortOptions.length
                      ? defaultModeForOptions(
                        effortOptions,
                        current.clip_ingest.mode || "medium",
                      )
                      : "",
                  },
                }));
              }}
              placeholder="候補にないモデルIDを直接入力"
              disabled={savingRouting}
              className="h-8"
            />
          </div>
        )}
        {showClipIngestEffort && (
          <div className="max-w-xs space-y-1">
            <Label className="text-xs">Effort</Label>
            <AppSelect
              value={clipIngestDraft.mode || "medium"}
              onChange={(event) => setClassDrafts((current) => ({ ...current, clip_ingest: { ...current.clip_ingest, mode: event.target.value } }))}
              disabled={savingRouting}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
            >
              {clipIngestEffortOptions.map((item) => <option key={item} value={item}>{item}</option>)}
            </AppSelect>
          </div>
        )}
        <div><Button size="sm" onClick={() => void saveRoutingSettings("clip_ingest")} disabled={savingRouting}>{savingRouting ? "保存中..." : "クリップ取り込み設定を保存"}</Button></div>
      </TabsContent>
    </>
  );
}

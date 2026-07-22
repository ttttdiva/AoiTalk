"use client";

import type { Dispatch, SetStateAction } from "react";
import { TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  providerSelection,
  type LlmModelCatalogResponse,
  type LlmProviderCatalog,
  type ModelClassDraft,
} from "./llm-model-section-types";

type ClassDraftMap = Record<"vision" | "audio", ModelClassDraft>;

type MediaModelTabsProps = {
  catalog: LlmModelCatalogResponse;
  classDrafts: ClassDraftMap;
  setClassDrafts: Dispatch<SetStateAction<ClassDraftMap>>;
  savingRouting: boolean;
  imageMode: "auto" | "always" | "off";
  setImageMode: Dispatch<SetStateAction<"auto" | "always" | "off">>;
  visionProvider: LlmProviderCatalog | undefined;
  audioProvider: LlmProviderCatalog | undefined;
  audioSource: string;
  speechEngine: string;
  speechModel: string;
  mediaProviders: (kind: "image" | "audio") => LlmProviderCatalog[];
  saveRoutingSettings: () => void;
};

export function LlmMediaModelTabs({
  catalog,
  classDrafts,
  setClassDrafts,
  savingRouting,
  imageMode,
  setImageMode,
  visionProvider,
  audioProvider,
  audioSource,
  speechEngine,
  speechModel,
  mediaProviders,
  saveRoutingSettings,
}: MediaModelTabsProps) {
  return (
    <>
      <TabsContent value="vision" className="mt-3 space-y-3">
        <p className="text-xs text-muted-foreground">既定では言語モデルを使います。対応しないモデルの場合のみ、画像対応モデルを指定してください。</p>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <Label className="text-xs">プロバイダー</Label>
            <select
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
            </select>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">モデル</Label>
            <select
              value={classDrafts.vision.model}
              onChange={(event) => setClassDrafts((current) => ({ ...current, vision: { ...current.vision, model: event.target.value, customModel: "" } }))}
              disabled={savingRouting || classDrafts.vision.inherit}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
            >
              {(visionProvider?.models ?? []).filter((item) => item.media?.image).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
          </div>
        </div>
        <div className="flex items-center justify-between gap-2">
          <Label className="text-xs">画像の送信方法</Label>
          <select value={imageMode} onChange={(event) => setImageMode(event.target.value as "auto" | "always" | "off")} disabled={savingRouting} className="h-8 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30">
            <option value="auto">自動</option><option value="always">常に認識モデルを使用</option><option value="off">無効</option>
          </select>
        </div>
        <div><Button size="sm" onClick={saveRoutingSettings} disabled={savingRouting}>{savingRouting ? "保存中..." : "画像認識設定を保存"}</Button></div>
      </TabsContent>

      <TabsContent value="audio" className="mt-3 space-y-3">
        <p className="text-xs text-muted-foreground">音声対応が確認できるモデルだけを選べます。ローカルSTTは設定済みの実モデル名を表示します。</p>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <Label className="text-xs">プロバイダー</Label>
            <select
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
            </select>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">モデル</Label>
            {audioSource === "local-stt" ? (
              <div className="flex h-8 items-center rounded-lg border border-input px-2.5 text-sm">{speechEngine} / {speechModel}</div>
            ) : (
              <select value={classDrafts.audio.model} onChange={(event) => setClassDrafts((current) => ({ ...current, audio: { ...current.audio, model: event.target.value, customModel: "" } }))} disabled={savingRouting || audioSource === "inherit" || audioSource === "off"} className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30">
                {(audioProvider?.models ?? []).filter((item) => item.media?.audio).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
            )}
          </div>
        </div>
        <div><Button size="sm" onClick={saveRoutingSettings} disabled={savingRouting}>{savingRouting ? "保存中..." : "音声認識設定を保存"}</Button></div>
      </TabsContent>
    </>
  );
}

"use client";

import { AudioLines, CircleAlert, Loader2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { HelpPopover } from "@/components/ui/help-popover";
import type { components } from "@/lib/api-types.gen";

const API_PREFIX = "/api/python-proxy";
const CUSTOM_VOICE = "__custom__";

type GeminiTTSCatalog = components["schemas"]["GeminiTTSCatalogResponse"];
type GeminiTTSPreviewRequest = components["schemas"]["GeminiTTSPreviewRequest"];

// Used until the server catalog arrives (and if it is unreachable) so the
// selector never renders empty.  The server remains the source of truth.
const FALLBACK_CATALOG: GeminiTTSCatalog = {
  engine: "gemini_tts",
  available: false,
  api_key_configured: false,
  default_model: "gemini-3.8-flash-lite-tts",
  default_voice: "Kore",
  models: [
    {
      id: "gemini-3.8-flash-lite-tts",
      label: "Gemini 3.8 Flash-Lite TTS",
      description: "低遅延・低コスト。通常会話と音声会話の既定値",
    },
    {
      id: "gemini-3.8-flash-tts",
      label: "Gemini 3.8 Flash TTS",
      description: "高表現力。演技・感情表現・キャラクター性を重視する場合",
    },
  ],
  voices: [],
};

const LANGUAGE_OPTIONS = [
  { value: "", label: "自動判定" },
  { value: "ja", label: "日本語 (ja)" },
  { value: "en", label: "英語 (en)" },
  { value: "zh", label: "中国語 (zh)" },
  { value: "ko", label: "韓国語 (ko)" },
];

const GENDER_LABELS: Record<string, string> = {
  female: "女性",
  male: "男性",
};

type Props = {
  voiceId: string;
  voiceParameters: Record<string, unknown>;
  onVoiceIdChange: (value: string) => void;
  onParametersChange: (value: Record<string, unknown>) => void;
};

function stringParam(params: Record<string, unknown>, key: string): string {
  const value = params[key];
  return typeof value === "string" ? value : "";
}

export function GeminiVoiceSettings({
  voiceId,
  voiceParameters,
  onVoiceIdChange,
  onParametersChange,
}: Props) {
  const [catalog, setCatalog] = useState<GeminiTTSCatalog>(FALLBACK_CATALOG);
  const [catalogLoaded, setCatalogLoaded] = useState(false);
  const [customMode, setCustomMode] = useState(false);
  const [previewText, setPreviewText] = useState(
    "こんにちは。Gemini TTSの試聴です。",
  );
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const previewUrlRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_PREFIX}/tts/gemini/catalog`, { credentials: "include" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`API Error: ${response.status}`);
        return (await response.json()) as GeminiTTSCatalog;
      })
      .then((data) => {
        if (!cancelled) setCatalog(data);
      })
      .catch(() => {
        // Keep the fallback catalog; the voice can still be typed in.
      })
      .finally(() => {
        if (!cancelled) setCatalogLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(
    () => () => {
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    },
    [],
  );

  const model = stringParam(voiceParameters, "model");
  const style = stringParam(voiceParameters, "style");
  const languageCode = stringParam(voiceParameters, "language_code");
  const knownVoice = catalog.voices.some((voice) => voice.id === voiceId);
  const showCustom =
    customMode || (voiceId !== "" && !knownVoice && catalogLoaded);
  const selectedVoice = showCustom ? CUSTOM_VOICE : voiceId;
  const defaultModelLabel =
    catalog.models.find((option) => option.id === catalog.default_model)
      ?.label ?? catalog.default_model;

  const setParam = useCallback(
    (key: string, value: string) => {
      const next = { ...voiceParameters };
      if (value) next[key] = value;
      else delete next[key];
      onParametersChange(next);
    },
    [onParametersChange, voiceParameters],
  );

  const generatePreview = useCallback(async () => {
    const text = previewText.trim();
    if (!text) {
      setError("試聴テキストを入力してください");
      return;
    }
    setPreviewLoading(true);
    setError(null);
    try {
      // Preview the unsaved form values; nothing is persisted here.
      const body: GeminiTTSPreviewRequest = { text };
      if (model) body.model = model;
      if (voiceId) body.voice = voiceId;
      if (style) body.style = style;
      if (languageCode) body.language_code = languageCode;
      const response = await fetch(`${API_PREFIX}/tts/gemini/preview`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        let message = `API Error: ${response.status}`;
        try {
          const payload = (await response.json()) as { detail?: unknown };
          if (typeof payload.detail === "string") message = payload.detail;
        } catch {
          // Keep the status-based message.
        }
        throw new Error(message);
      }
      const blob = await response.blob();
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
      const url = URL.createObjectURL(blob);
      previewUrlRef.current = url;
      setPreviewUrl(url);
    } catch (previewError) {
      setError(
        previewError instanceof Error
          ? previewError.message
          : "試聴音声の生成に失敗しました",
      );
    } finally {
      setPreviewLoading(false);
    }
  }, [languageCode, model, previewText, style, voiceId]);

  return (
    <div className="space-y-3 rounded-md border border-primary/20 bg-primary/5 p-3">
      <div className="flex items-start gap-2">
        <AudioLines
          className="mt-0.5 size-4 shrink-0 text-primary"
          aria-hidden="true"
        />
        <div>
          <p className="inline-flex items-center gap-1 text-xs font-medium">
            Gemini TTS 設定
            <HelpPopover label="Gemini TTS 設定">ボイスで「誰の声か」、読み上げスタイルで「どう話すか」を指定します。通常チャット・音声会話・モバイルで共通です。</HelpPopover>
          </p>
        </div>
      </div>

      {catalogLoaded && !catalog.api_key_configured && (
        <p
          role="status"
          className="flex items-start gap-1.5 text-[11px] text-amber-600 dark:text-amber-400"
        >
          <CircleAlert
            className="mt-0.5 size-3.5 shrink-0"
            aria-hidden="true"
          />
          サーバーに GEMINI_API_KEY が設定されていません。.env
          に設定すると利用できます。
        </p>
      )}

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <Label htmlFor="gemini-tts-model" className="text-xs">
            モデル
          </Label>
          <AppSelect
            id="gemini-tts-model"
            aria-label="Gemini TTSモデル"
            value={model}
            onChange={(event) => setParam("model", event.target.value)}
            className="h-8 text-xs"
          >
            <option value="">サーバー既定（{defaultModelLabel}）</option>
            {catalog.models.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </AppSelect>
        </div>
        <div className="space-y-1">
          <Label htmlFor="gemini-tts-voice" className="text-xs">
            ボイス
          </Label>
          <AppSelect
            id="gemini-tts-voice"
            aria-label="Gemini TTSボイス"
            value={selectedVoice}
            onChange={(event) => {
              const value = event.target.value;
              if (value === CUSTOM_VOICE) {
                setCustomMode(true);
                if (knownVoice) onVoiceIdChange("");
                return;
              }
              setCustomMode(false);
              onVoiceIdChange(value);
            }}
            className="h-8 text-xs"
          >
            <option value="">サーバー既定（{catalog.default_voice}）</option>
            {catalog.voices.map((voice) => (
              <option key={voice.id} value={voice.id}>
                {voice.id}（{voice.tone}・
                {GENDER_LABELS[voice.gender] ?? voice.gender}）
              </option>
            ))}
            <option value={CUSTOM_VOICE}>カスタム（Voice Design ID）</option>
          </AppSelect>
        </div>
      </div>

      {showCustom && (
        <div className="space-y-1">
          <div className="inline-flex items-center gap-1">
            <Label htmlFor="gemini-tts-custom-voice" className="text-xs">
              カスタムボイスID
            </Label>
            <HelpPopover label="カスタムボイスID">Gemini API の Voice Design / Voices API で作成した voice_… のIDを指定します。</HelpPopover>
          </div>
          <Input
            id="gemini-tts-custom-voice"
            value={voiceId}
            onChange={(event) => onVoiceIdChange(event.target.value.trim())}
            placeholder="voice_xxxxxxxx"
          />
        </div>
      )}

      <div className="space-y-1">
        <div className="inline-flex items-center gap-1">
          <Label htmlFor="gemini-tts-style" className="text-xs">
            読み上げスタイル
          </Label>
          <HelpPopover label="読み上げスタイル">話し方・感情・テンポを自然文で指定します。本文として読み上げられることはありません。</HelpPopover>
        </div>
        <Textarea
          id="gemini-tts-style"
          value={style}
          onChange={(event) => setParam("style", event.target.value)}
          placeholder="例: 明るく親しみやすく、少しゆっくり。語尾はやわらかく。"
          rows={2}
          className="text-xs"
        />
      </div>

      <div className="space-y-1">
        <div className="inline-flex items-center gap-1">
          <Label htmlFor="gemini-tts-language" className="text-xs">
            言語
          </Label>
          <HelpPopover label="Gemini TTS言語">Gemini 3.8 TTSは本文から言語を自動判定します。選択値は設定互換性のため保存されます。</HelpPopover>
        </div>
        <AppSelect
          id="gemini-tts-language"
          aria-label="Gemini TTS言語"
          value={languageCode}
          onChange={(event) => setParam("language_code", event.target.value)}
          className="h-8 text-xs"
        >
          {LANGUAGE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </AppSelect>
      </div>

      <div className="space-y-2 rounded-md border bg-background/50 p-2.5">
        <div>
          <p className="text-xs font-medium">試聴</p>
          <p className="text-[10px] text-muted-foreground">
            保存前の設定で音声を生成します（Gemini APIの利用料金が発生します）。
          </p>
        </div>
        <Input
          aria-label="Gemini TTS試聴テキスト"
          value={previewText}
          onChange={(event) => setPreviewText(event.target.value)}
          placeholder="試聴する文章を入力"
          maxLength={300}
          disabled={previewLoading}
        />
        <div className="flex items-center gap-2">
          <Button
            type="button"
            size="sm"
            onClick={() => void generatePreview()}
            disabled={previewLoading}
          >
            {previewLoading && <Loader2 className="size-3 animate-spin" />}
            {previewLoading ? "生成中..." : "生成して試聴"}
          </Button>
          {previewUrl && (
            <audio
              controls
              autoPlay
              preload="none"
              src={previewUrl}
              className="h-8 min-w-0 flex-1"
              aria-label="Gemini TTS試聴結果"
            />
          )}
        </div>
      </div>

      {error && (
        <p
          role="alert"
          className="flex items-start gap-1.5 text-[11px] text-destructive"
        >
          <CircleAlert
            className="mt-0.5 size-3.5 shrink-0"
            aria-hidden="true"
          />
          {error}
        </p>
      )}
    </div>
  );
}

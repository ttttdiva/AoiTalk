"use client";

import {
  ArrowDown,
  ArrowUp,
  AudioLines,
  CircleAlert,
  FileAudio,
  Loader2,
  MonitorSpeaker,
  Play,
  RefreshCw,
  Square,
  Trash2,
  UploadCloud,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

const API_PREFIX = "/api/python-proxy";
const DEFAULT_MAX_DURATION_SECONDS = 120;
const ALLOWED_AUDIO_MIME_TYPES = new Set([
  "audio/wav",
  "audio/wave",
  "audio/x-wav",
  "audio/vnd.wave",
  "audio/flac",
  "audio/x-flac",
  "audio/ogg",
  "application/ogg",
]);

export type IrodoriVoiceParameters = Record<string, unknown>;

/**
 * Logical Irodori model choices persisted with a character's voice
 * parameters.  Keep the checkpoint resolution in the backend/runtime layer;
 * the settings UI only exposes stable, human-readable selector values.
 */
export const IRODORI_MODEL_DEFAULT = "v4.1-small" as const;
export const IRODORI_MODEL_OPTIONS = [
  { value: IRODORI_MODEL_DEFAULT, label: "Irodori-TTS v4.1 Small" },
  { value: "v3-voice-design", label: "Irodori-TTS v3 VoiceDesign" },
] as const;
export type IrodoriModel = (typeof IRODORI_MODEL_OPTIONS)[number]["value"];

function isIrodoriModel(value: unknown): value is IrodoriModel {
  return IRODORI_MODEL_OPTIONS.some((option) => option.value === value);
}

export function normalizeIrodoriModel(value: unknown): IrodoriModel {
  return isIrodoriModel(value) ? value : IRODORI_MODEL_DEFAULT;
}

export interface IrodoriVoiceSettingsProps {
  /** The persisted character id. Empty means this is a new, unsaved character. */
  characterId: string;
  voiceParameters: IrodoriVoiceParameters;
  onChange: (value: IrodoriVoiceParameters) => void;
}

interface VoiceAsset {
  id: string;
  name: string;
  durationSeconds: number;
  sizeBytes?: number;
  metadata: Record<string, unknown>;
}

interface CaptureDevice {
  id: string;
  name: string;
  index?: number;
}

interface ApiError extends Error {
  status?: number;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function asNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

function normalizeAsset(value: unknown, index: number): VoiceAsset | null {
  const raw = asRecord(value);
  const id = asString(raw.id) || asString(raw.asset_id) || asString(raw.voice_asset_id);
  if (!id) return null;
  return {
    id,
    name:
      asString(raw.filename) ||
      asString(raw.name) ||
      asString(raw.original_filename) ||
      asString(raw.display_name) ||
      `参照音声 ${index + 1}`,
    durationSeconds:
      asNumber(raw.duration_seconds) ||
      asNumber(raw.duration) ||
      asNumber(raw.length_seconds) ||
      0,
    sizeBytes:
      asNumber(raw.size_bytes) || asNumber(raw.file_size) || asNumber(raw.size),
    metadata: raw,
  };
}

function normalizeDevice(value: unknown, index: number): CaptureDevice | null {
  const raw = asRecord(value);
  const deviceIndex = asNumber(raw.index);
  const id =
    asString(raw.id) ||
    asString(raw.device_id) ||
    (deviceIndex !== undefined ? String(deviceIndex) : undefined) ||
    asString(raw.name);
  if (!id) return null;
  return {
    id,
    name:
      asString(raw.label) ||
      asString(raw.name) ||
      asString(raw.display_name) ||
      `スピーカー ${index + 1}`,
    index: deviceIndex,
  };
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0:00";
  const wholeSeconds = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(wholeSeconds / 60);
  return `${minutes}:${String(wholeSeconds % 60).padStart(2, "0")}`;
}

function formatBytes(bytes?: number): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function sameStringArray(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function referenceId(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  const raw = asRecord(value);
  return asString(raw.id) || asString(raw.asset_id) || asString(raw.voice_asset_id);
}

async function requestJson<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (
    init?.body !== undefined &&
    !(init.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    credentials: "include",
    headers,
  });
  if (!response.ok) {
    const error: ApiError = new Error(`API Error: ${response.status}`);
    error.status = response.status;
    try {
      const detail = (await response.text()).trim();
      if (detail) error.message = detail;
    } catch {
      // Keep the status-based message when the response has no readable body.
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

async function requestBlob(path: string, init?: RequestInit): Promise<Blob> {
  const headers = new Headers(init?.headers);
  if (
    init?.body !== undefined &&
    !(init.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    credentials: "include",
    headers,
  });
  if (!response.ok) {
    const error: ApiError = new Error(`API Error: ${response.status}`);
    error.status = response.status;
    try {
      const detail = (await response.text()).trim();
      if (detail) error.message = detail;
    } catch {
      // Keep the status-based message when the response has no readable body.
    }
    throw error;
  }
  return response.blob();
}

function assetPath(characterId: string, suffix = "voice-assets"): string {
  return `/characters/manage/${encodeURIComponent(characterId)}/${suffix}`;
}

function getCaptureId(payload: unknown): string | undefined {
  const raw = asRecord(payload);
  const capture = asRecord(raw.capture);
  return (
    asString(raw.capture_id) ||
    asString(raw.id) ||
    asString(capture.capture_id) ||
    asString(capture.id)
  );
}

function isTerminalCaptureStatus(status: string): boolean {
  return [
    "completed",
    "complete",
    "stopped",
    "finished",
    "done",
    "failed",
    "error",
    "cancelled",
    "canceled",
  ].includes(status.toLowerCase());
}

export function IrodoriVoiceSettings({
  characterId,
  voiceParameters,
  onChange,
}: IrodoriVoiceSettingsProps) {
  const [assets, setAssets] = useState<VoiceAsset[]>([]);
  const [totalDuration, setTotalDuration] = useState(0);
  const [maxDuration, setMaxDuration] = useState(DEFAULT_MAX_DURATION_SECONDS);
  const [assetsLoaded, setAssetsLoaded] = useState(false);
  const [assetsLoading, setAssetsLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [ordering, setOrdering] = useState(false);
  const [deletingAssetId, setDeletingAssetId] = useState<string | null>(null);
  const [playbackAssetId, setPlaybackAssetId] = useState<string | null>(null);
  const [playbackLoadingId, setPlaybackLoadingId] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [devices, setDevices] = useState<CaptureDevice[]>([]);
  const [devicesLoading, setDevicesLoading] = useState(false);
  const [selectedDeviceId, setSelectedDeviceId] = useState("");
  const [captureId, setCaptureId] = useState<string | null>(null);
  const [captureStatus, setCaptureStatus] = useState("");
  const [stoppingCapture, setStoppingCapture] = useState(false);
  const [previewText, setPreviewText] = useState("こんにちは、これはIrodori-TTSの試聴です。");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const playbackAudioRef = useRef<HTMLAudioElement | null>(null);
  const playbackUrlRef = useRef<string | null>(null);
  const previewUrlRef = useRef<string | null>(null);

  const hasCharacter = Boolean(characterId.trim());
  const selectedModel = normalizeIrodoriModel(voiceParameters.irodori_model);
  // An omitted selector is intentionally kept omitted for preview requests.
  // This preserves legacy characters that rely on an explicit hf_checkpoint
  // (or another backend/global fallback) until the user chooses a model in
  // this editor.  Selecting either option writes the logical selector into
  // voice_parameters, after which it is forwarded below.
  const previewModel = isIrodoriModel(voiceParameters.irodori_model)
    ? voiceParameters.irodori_model
    : undefined;
  const selectedModelLabel =
    IRODORI_MODEL_OPTIONS.find((option) => option.value === selectedModel)?.label ??
    IRODORI_MODEL_OPTIONS[0].label;
  const caption = typeof voiceParameters.caption === "string" ? voiceParameters.caption : "";
  const referenceAssetRecords = useMemo(
    () =>
      Array.isArray(voiceParameters.irodori_reference_assets)
        ? voiceParameters.irodori_reference_assets
        : [],
    [voiceParameters.irodori_reference_assets],
  );
  const referenceAssetIds = useMemo(
    () => referenceAssetRecords.map(referenceId).filter((id): id is string => Boolean(id)),
    [referenceAssetRecords],
  );

  const setVoiceParameter = useCallback(
    (key: string, value: unknown) => {
      onChange({ ...voiceParameters, [key]: value });
    },
    [onChange, voiceParameters],
  );

  const loadAssets = useCallback(async () => {
    if (!hasCharacter) return;
    setAssetsLoading(true);
    try {
      const payload = await requestJson<{
        assets?: unknown[];
        total_duration_seconds?: unknown;
        max_duration_seconds?: unknown;
      }>(assetPath(characterId));
      const normalized = (payload.assets || [])
        .map((asset, index) => normalizeAsset(asset, index))
        .filter((asset): asset is VoiceAsset => asset !== null);
      const total =
        asNumber(payload.total_duration_seconds) ??
        normalized.reduce((sum, asset) => sum + asset.durationSeconds, 0);
      setAssets(normalized);
      setTotalDuration(Math.max(0, total));
      setMaxDuration(
        Math.max(
          1,
          asNumber(payload.max_duration_seconds) || DEFAULT_MAX_DURATION_SECONDS,
        ),
      );
      setAssetsLoaded(true);
      setError(null);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "参照音声の取得に失敗しました");
    } finally {
      setAssetsLoading(false);
    }
  }, [characterId, hasCharacter]);

  const loadDevices = useCallback(async () => {
    if (!hasCharacter) return;
    setDevicesLoading(true);
    try {
      const payload = await requestJson<{ devices?: unknown[] }>(
        assetPath(characterId, "voice-assets/devices"),
      );
      const normalized = (payload.devices || [])
        .map((device, index) => normalizeDevice(device, index))
        .filter((device): device is CaptureDevice => device !== null);
      setDevices(normalized);
      setSelectedDeviceId((current) =>
        current && !normalized.some((device) => device.id === current) ? "" : current,
      );
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "スピーカー一覧の取得に失敗しました");
    } finally {
      setDevicesLoading(false);
    }
  }, [characterId, hasCharacter]);

  useEffect(() => {
    setAssets([]);
    setTotalDuration(0);
    setMaxDuration(DEFAULT_MAX_DURATION_SECONDS);
    setAssetsLoaded(false);
    setError(null);
    setNotice(null);
    if (hasCharacter) {
      void loadAssets();
      void loadDevices();
    } else {
      setDevices([]);
      setSelectedDeviceId("");
    }
  }, [characterId, hasCharacter, loadAssets, loadDevices]);

  useEffect(() => {
    if (!hasCharacter || !assetsLoaded) return;
    const ids = assets.map((asset) => asset.id);
    const recordsAreMetadata = referenceAssetRecords.every(
      (record) => typeof record === "object" && record !== null,
    );
    if (!sameStringArray(ids, referenceAssetIds) || !recordsAreMetadata) {
      const metadata = assets.map((asset) => ({ ...asset.metadata, id: asset.id }));
      const preserveIdList =
        referenceAssetRecords.length > 0 &&
        referenceAssetRecords.every((record) => typeof record === "string");
      onChange({
        ...voiceParameters,
        irodori_reference_assets: preserveIdList ? ids : metadata,
      });
    }
  }, [
    assets,
    assetsLoaded,
    hasCharacter,
    onChange,
    referenceAssetIds,
    referenceAssetRecords,
    voiceParameters,
  ]);

  useEffect(() => {
    if (!captureId) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const payload = await requestJson<Record<string, unknown>>(
          assetPath(characterId, `voice-assets/capture/${encodeURIComponent(captureId)}`),
        );
        if (disposed) return;
        const raw = asRecord(payload);
        const capture = asRecord(raw.capture);
        const status = String(
          raw.status || raw.state || capture.status || capture.state || "recording",
        );
        setCaptureStatus(status);
        if (isTerminalCaptureStatus(status) || raw.asset || raw.voice_asset || raw.asset_id) {
          setCaptureId(null);
          setStoppingCapture(false);
          if (["failed", "error", "cancelled", "canceled"].includes(status.toLowerCase())) {
            setError("PCスピーカー録音に失敗しました");
          } else {
            await loadAssets();
            setNotice("録音した音声を参照音声に追加しました");
          }
          return;
        }
        timer = setTimeout(() => void poll(), 1000);
      } catch (pollError) {
        if (disposed) return;
        // A completed capture may disappear immediately after stop. Refresh the
        // asset list before surfacing an error so that the successful recording
        // is still visible when the backend has already finalized it.
        if (stoppingCapture) {
          try {
            await loadAssets();
            setCaptureId(null);
            setStoppingCapture(false);
            setNotice("録音した音声を参照音声に追加しました");
            return;
          } catch {
            // Fall through to the original polling error.
          }
        }
        setCaptureId(null);
        setStoppingCapture(false);
        setError(pollError instanceof Error ? pollError.message : "録音状態の取得に失敗しました");
      }
    };

    void poll();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  }, [captureId, characterId, loadAssets, stoppingCapture]);

  const stopPlayback = useCallback(() => {
    if (playbackAudioRef.current) {
      playbackAudioRef.current.pause();
      playbackAudioRef.current.removeAttribute("src");
      playbackAudioRef.current.load();
    }
    if (playbackUrlRef.current) {
      URL.revokeObjectURL(playbackUrlRef.current);
      playbackUrlRef.current = null;
    }
    setPlaybackAssetId(null);
    setPlaybackLoadingId(null);
  }, []);

  useEffect(() => {
    const playbackAudio = playbackAudioRef.current;
    return () => {
      if (playbackUrlRef.current) URL.revokeObjectURL(playbackUrlRef.current);
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
      playbackAudio?.pause();
    };
  }, []);

  const handleAssetPlayback = useCallback(
    async (asset: VoiceAsset) => {
      if (!hasCharacter) return;
      if (playbackAssetId === asset.id) {
        stopPlayback();
        return;
      }
      stopPlayback();
      setPlaybackLoadingId(asset.id);
      try {
        let blob: Blob;
        try {
          blob = await requestBlob(
            assetPath(
              characterId,
              `voice-assets/${encodeURIComponent(asset.id)}/content`,
            ),
          );
        } catch (contentError) {
          // Older development servers exposed the asset itself rather than
          // /content. Keep playback compatible while the v4 API rolls out.
          if ((contentError as ApiError).status !== 404) throw contentError;
          blob = await requestBlob(
            assetPath(characterId, `voice-assets/${encodeURIComponent(asset.id)}`),
          );
        }
        const url = URL.createObjectURL(blob);
        playbackUrlRef.current = url;
        const audio = playbackAudioRef.current;
        if (!audio) {
          stopPlayback();
          return;
        }
        audio.src = url;
        audio.onended = stopPlayback;
        audio.onerror = stopPlayback;
        await audio.play();
        setPlaybackAssetId(asset.id);
      } catch (playbackError) {
        setError(
          playbackError instanceof Error ? playbackError.message : "参照音声を再生できませんでした",
        );
      } finally {
        setPlaybackLoadingId(null);
      }
    },
    [characterId, hasCharacter, playbackAssetId, stopPlayback],
  );

  const uploadFiles = useCallback(
    async (files: File[]) => {
      if (!hasCharacter) {
        setNotice("参照音声を登録するには、先にキャラクターを保存してください。");
        return;
      }
      const audioFiles = files.filter((file) => {
        const extensionAllowed = /\.(wav|wave|ogg|flac)$/i.test(file.name);
        const mimeAllowed = ALLOWED_AUDIO_MIME_TYPES.has(file.type.toLowerCase());
        return extensionAllowed || mimeAllowed;
      });
      if (audioFiles.length === 0) {
        setError("音声ファイル（WAV / FLAC / OGG）を選択してください");
        return;
      }
      setUploading(true);
      setError(null);
      setNotice(null);
      let uploadedCount = 0;
      const failures: string[] = [];
      for (const file of audioFiles) {
        try {
          const formData = new FormData();
          formData.append("file", file);
          await requestJson(
            assetPath(characterId),
            { method: "POST", body: formData },
          );
          uploadedCount += 1;
        } catch (uploadError) {
          const detail = uploadError instanceof Error
            ? uploadError.message
            : "参照音声の追加に失敗しました";
          failures.push(`${file.name}: ${detail}`);
        }
      }

      try {
        // Each POST commits independently. Always refresh after attempting the
        // batch so successful earlier files are visible even when a later file
        // exceeds the duration limit or cannot be decoded.
        await loadAssets();
        if (failures.length === 0) {
          setNotice(`${uploadedCount}件の参照音声を追加しました`);
        } else if (uploadedCount > 0) {
          setNotice(`${uploadedCount}件の参照音声を追加しました。${failures.length}件は追加できませんでした。`);
          setError(failures.join("\n"));
        } else {
          setError(failures.join("\n"));
        }
      } finally {
        setUploading(false);
      }
    },
    [characterId, hasCharacter, loadAssets],
  );

  const handleFileInput = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files || []);
      event.target.value = "";
      if (files.length) void uploadFiles(files);
    },
    [uploadFiles],
  );

  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragActive(false);
      if (event.dataTransfer.files.length) {
        void uploadFiles(Array.from(event.dataTransfer.files));
      }
    },
    [uploadFiles],
  );

  const handleDelete = useCallback(
    async (asset: VoiceAsset) => {
      if (!hasCharacter) return;
      if (!window.confirm(`参照音声「${asset.name}」を削除しますか？`)) return;
      setDeletingAssetId(asset.id);
      try {
        await requestJson(
          assetPath(characterId, `voice-assets/${encodeURIComponent(asset.id)}`),
          { method: "DELETE" },
        );
        await loadAssets();
      } catch (deleteError) {
        setError(deleteError instanceof Error ? deleteError.message : "参照音声の削除に失敗しました");
      } finally {
        setDeletingAssetId(null);
      }
    },
    [characterId, hasCharacter, loadAssets],
  );

  const moveAsset = useCallback(
    async (index: number, offset: number) => {
      const targetIndex = index + offset;
      if (
        !hasCharacter ||
        targetIndex < 0 ||
        targetIndex >= assets.length ||
        ordering
      ) {
        return;
      }
      const reordered = [...assets];
      const [moved] = reordered.splice(index, 1);
      reordered.splice(targetIndex, 0, moved);
      setAssets(reordered);
      setOrdering(true);
      try {
        await requestJson(
          assetPath(characterId, "voice-assets/order"),
          {
            method: "PATCH",
            body: JSON.stringify({ asset_ids: reordered.map((asset) => asset.id) }),
          },
        );
      } catch (orderError) {
        setError(orderError instanceof Error ? orderError.message : "参照音声の順序変更に失敗しました");
        await loadAssets();
      } finally {
        setOrdering(false);
      }
    },
    [assets, characterId, hasCharacter, loadAssets, ordering],
  );

  const startCapture = useCallback(async () => {
    if (!hasCharacter || captureId) return;
    setError(null);
    setNotice(null);
    try {
      const payload = await requestJson<Record<string, unknown>>(
        assetPath(characterId, "voice-assets/capture/start"),
        {
          method: "POST",
          body: JSON.stringify(selectedDeviceId ? { device_id: selectedDeviceId } : {}),
        },
      );
      const nextCaptureId = getCaptureId(payload);
      if (!nextCaptureId) throw new Error("録音IDを取得できませんでした");
      const raw = asRecord(payload);
      const capture = asRecord(raw.capture);
      setCaptureStatus(String(raw.status || raw.state || capture.status || capture.state || "recording"));
      setCaptureId(nextCaptureId);
    } catch (captureError) {
      setError(captureError instanceof Error ? captureError.message : "PCスピーカー録音を開始できませんでした");
    }
  }, [captureId, characterId, hasCharacter, selectedDeviceId]);

  const stopCapture = useCallback(async () => {
    if (!captureId) return;
    setStoppingCapture(true);
    setCaptureStatus("停止処理中");
    try {
      await requestJson(
        assetPath(characterId, `voice-assets/capture/${encodeURIComponent(captureId)}/stop`),
        { method: "POST" },
      );
    } catch (captureError) {
      setStoppingCapture(false);
      setError(captureError instanceof Error ? captureError.message : "PCスピーカー録音を停止できませんでした");
    }
  }, [captureId, characterId]);

  const generatePreview = useCallback(async () => {
    if (!hasCharacter) {
      setNotice("試聴するには、先にキャラクターを保存してください。");
      return;
    }
    const text = previewText.trim();
    if (!text) {
      setError("試聴テキストを入力してください");
      return;
    }
    setPreviewLoading(true);
    setError(null);
    try {
      const previewBody: {
        text: string;
        caption: string;
        irodori_model?: IrodoriModel;
      } = { text, caption: caption.trim() };
      if (previewModel) previewBody.irodori_model = previewModel;
      const blob = await requestBlob(assetPath(characterId, "voice-assets/preview"), {
        method: "POST",
        // Send the currently edited logical model when one is explicit,
        // rather than injecting the display-only default into legacy preview
        // requests.  Checkpoint resolution remains a backend concern.
        body: JSON.stringify(previewBody),
      });
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
      const url = URL.createObjectURL(blob);
      previewUrlRef.current = url;
      setPreviewUrl(url);
    } catch (previewError) {
      setError(previewError instanceof Error ? previewError.message : "試聴音声の生成に失敗しました");
    } finally {
      setPreviewLoading(false);
    }
  }, [caption, characterId, hasCharacter, previewModel, previewText]);

  const remainingSeconds = Math.max(0, maxDuration - totalDuration);
  const progressPercent = Math.min(100, (totalDuration / maxDuration) * 100);

  return (
    <div className="space-y-3 rounded-md border border-primary/20 bg-primary/5 p-3">
      <div className="flex items-start gap-2">
        <AudioLines className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
        <div>
          <p className="text-xs font-medium">{selectedModelLabel} 設定</p>
          <p className="text-[11px] text-muted-foreground">
            参照音声は「誰の声か」、captionは「どう話すか」を指定します。両方を同時に利用できます。
          </p>
        </div>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="irodori-model" className="text-xs">Irodori-TTSモデル</Label>
        <AppSelect
          id="irodori-model"
          aria-label="Irodori-TTSモデル"
          value={selectedModel}
          onChange={(event) => setVoiceParameter("irodori_model", event.target.value)}
          className="h-8 text-xs"
        >
          {IRODORI_MODEL_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </AppSelect>
        <p className="text-[10px] text-muted-foreground">
          キャラクターごとに使用するIrodori-TTSモデルを選択します。未指定の場合はv4.1 Smallを使用します。
        </p>
      </div>

      {!hasCharacter && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-300">
          <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          <span>参照音声の追加・録音・試聴は、キャラクターを一度保存してから利用できます。</span>
        </div>
      )}

      <div className="space-y-1.5">
        <Label htmlFor="irodori-caption" className="text-xs">読み上げ方（caption）</Label>
        <Textarea
          id="irodori-caption"
          value={caption}
          onChange={(event) => setVoiceParameter("caption", event.target.value)}
          placeholder="明るく親しみやすく、少し嬉しそうに話す"
          className="min-h-20 text-xs"
        />
        <p className="text-[10px] text-muted-foreground">
          参照音声と矛盾する指定（例：子どもの声に低い男性声）は品質が不安定になることがあります。
        </p>
      </div>

      <div className="space-y-2 rounded-md border bg-background/50 p-2.5">
        <div className="flex items-center justify-between gap-2">
          <div>
            <p className="text-xs font-medium">参照音声</p>
            <p className="text-[10px] text-muted-foreground">合計約30秒が目安、上限は120秒です。</p>
          </div>
          <div className="text-right text-[11px] tabular-nums text-muted-foreground">
            <div>{formatDuration(totalDuration)} / {formatDuration(maxDuration)}</div>
            <div>残り {formatDuration(remainingSeconds)}</div>
          </div>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
          <div
            className={`h-full rounded-full transition-all ${progressPercent >= 100 ? "bg-destructive" : "bg-primary"}`}
            style={{ width: `${progressPercent}%` }}
          />
        </div>

        <div
          role="button"
          tabIndex={hasCharacter ? 0 : -1}
          aria-disabled={!hasCharacter || uploading || remainingSeconds <= 0}
          aria-label="音声ファイルをドロップして参照音声に追加"
          onClick={() => hasCharacter && remainingSeconds > 0 && fileInputRef.current?.click()}
          onKeyDown={(event) => {
            if ((event.key === "Enter" || event.key === " ") && hasCharacter && remainingSeconds > 0) {
              event.preventDefault();
              fileInputRef.current?.click();
            }
          }}
          onDragEnter={(event) => {
            event.preventDefault();
            if (hasCharacter) setDragActive(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            if (event.currentTarget === event.target) setDragActive(false);
          }}
          onDrop={handleDrop}
          className={`flex min-h-16 cursor-pointer flex-col items-center justify-center gap-1 rounded-md border border-dashed px-3 py-2 text-center text-[11px] transition-colors ${
            dragActive ? "border-primary bg-primary/10" : "border-muted-foreground/30 hover:border-primary/60 hover:bg-muted/40"
          } ${!hasCharacter || remainingSeconds <= 0 ? "cursor-not-allowed opacity-60" : ""}`}
        >
          {uploading ? <Loader2 className="size-4 animate-spin text-primary" aria-hidden="true" /> : <UploadCloud className="size-4 text-muted-foreground" aria-hidden="true" />}
          <span>{remainingSeconds <= 0 ? "上限に達しています" : "音声ファイルをここにドロップ（複数可）またはクリックして選択"}</span>
          <input
            ref={fileInputRef}
            type="file"
            accept=".wav,.wave,.ogg,.flac,audio/wav,audio/ogg,audio/flac"
            multiple
            className="sr-only"
            disabled={!hasCharacter || uploading || remainingSeconds <= 0}
            onChange={handleFileInput}
            tabIndex={-1}
          />
        </div>

        {assetsLoading ? (
          <div className="flex items-center gap-1.5 py-1 text-[11px] text-muted-foreground">
            <Loader2 className="size-3 animate-spin" aria-hidden="true" /> 参照音声を読み込み中...
          </div>
        ) : assets.length === 0 ? (
          <p className="py-1 text-[11px] text-muted-foreground">参照音声はまだ登録されていません（captionのみでも利用できます）。</p>
        ) : (
          <ul className="space-y-1" aria-label="登録済み参照音声一覧">
            {assets.map((asset, index) => (
              <li key={asset.id} className="flex items-center gap-1.5 rounded-md border px-2 py-1.5">
                <FileAudio className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate text-[11px]" title={asset.name}>{asset.name}</span>
                <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                  {formatDuration(asset.durationSeconds)}{formatBytes(asset.sizeBytes) ? ` / ${formatBytes(asset.sizeBytes)}` : ""}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={playbackAssetId === asset.id ? `${asset.name}の再生を停止` : `${asset.name}を再生`}
                  title={playbackAssetId === asset.id ? "停止" : "再生"}
                  disabled={playbackLoadingId !== null && playbackLoadingId !== asset.id}
                  onClick={() => void handleAssetPlayback(asset)}
                >
                  {playbackLoadingId === asset.id ? <Loader2 className="size-3 animate-spin" /> : playbackAssetId === asset.id ? <Square className="size-3" /> : <Play className="size-3" />}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`${asset.name}を上へ移動`}
                  title="上へ"
                  disabled={index === 0 || ordering}
                  onClick={() => void moveAsset(index, -1)}
                >
                  <ArrowUp className="size-3" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`${asset.name}を下へ移動`}
                  title="下へ"
                  disabled={index === assets.length - 1 || ordering}
                  onClick={() => void moveAsset(index, 1)}
                >
                  <ArrowDown className="size-3" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  className="text-destructive hover:text-destructive"
                  aria-label={`${asset.name}を削除`}
                  title="削除"
                  disabled={deletingAssetId === asset.id}
                  onClick={() => void handleDelete(asset)}
                >
                  {deletingAssetId === asset.id ? <Loader2 className="size-3 animate-spin" /> : <Trash2 className="size-3" />}
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-2 rounded-md border bg-background/50 p-2.5">
        <div className="flex items-start gap-2">
          <MonitorSpeaker className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <div>
            <p className="text-xs font-medium">PCスピーカー出力を録音</p>
            <p className="text-[10px] text-muted-foreground">マイクではなく、Windowsで再生中のシステム出力を録音します。</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <AppSelect
            aria-label="録音するPCスピーカー出力"
            value={selectedDeviceId}
            onChange={(event) => setSelectedDeviceId(event.target.value)}
            disabled={!hasCharacter || devicesLoading || Boolean(captureId)}
            className="h-7 min-w-0 flex-1 text-xs"
          >
            <option value="">既定のスピーカー出力</option>
            {devices.map((device) => <option key={device.id} value={device.id}>{device.name}</option>)}
          </AppSelect>
          <Button type="button" variant="outline" size="sm" onClick={() => void loadDevices()} disabled={!hasCharacter || devicesLoading || Boolean(captureId)}>
            {devicesLoading ? <Loader2 className="size-3 animate-spin" /> : <RefreshCw className="size-3" />}
            <span className="sr-only">スピーカー一覧を更新</span>
          </Button>
          {captureId ? (
            <Button type="button" variant="destructive" size="sm" onClick={() => void stopCapture()} disabled={stoppingCapture}>
              {stoppingCapture ? <Loader2 className="size-3 animate-spin" /> : <Square className="size-3" />}
              {stoppingCapture ? "停止中..." : "録音停止"}
            </Button>
          ) : (
            <Button type="button" variant="outline" size="sm" onClick={() => void startCapture()} disabled={!hasCharacter}>
              <MonitorSpeaker className="size-3" /> 録音開始
            </Button>
          )}
        </div>
        {captureId && <p role="status" className="text-[10px] text-primary">録音状態: {captureStatus || "準備中"}</p>}
      </div>

      <div className="space-y-2 rounded-md border bg-background/50 p-2.5">
        <div>
          <p className="text-xs font-medium">試聴</p>
          <p className="text-[10px] text-muted-foreground">現在の参照音声とcaptionで音声を生成します。</p>
        </div>
        <Input
          aria-label="Irodori試聴テキスト"
          value={previewText}
          onChange={(event) => setPreviewText(event.target.value)}
          placeholder="試聴する文章を入力"
          disabled={!hasCharacter || previewLoading}
        />
        <div className="flex items-center gap-2">
          <Button type="button" size="sm" onClick={() => void generatePreview()} disabled={!hasCharacter || previewLoading}>
            {previewLoading && <Loader2 className="size-3 animate-spin" />}
            {previewLoading ? "生成中..." : "生成して試聴"}
          </Button>
          {previewUrl && <audio controls preload="none" src={previewUrl} className="h-8 min-w-0 flex-1" aria-label="Irodori試聴結果" />}
        </div>
      </div>

      {error && <p role="alert" className="flex items-start gap-1.5 text-[11px] text-destructive"><CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />{error}</p>}
      {notice && <p role="status" className="text-[11px] text-muted-foreground">{notice}</p>}
      <audio ref={playbackAudioRef} className="hidden" aria-hidden="true" />
    </div>
  );
}

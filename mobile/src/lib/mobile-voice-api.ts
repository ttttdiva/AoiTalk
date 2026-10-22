import * as FileSystem from "expo-file-system/legacy";
import { fetchApiAtServerFingerprint, isApiHttpError } from "./api-client";
import type { components } from "../types/api-types.gen";

type Schema = components["schemas"];
export type VoiceScope = Pick<Schema["MobileVoiceTranscriptionRequest"], "conversation_session_id" | "project_id">;
export type VoiceCapabilities = Schema["MobileVoiceCapabilities"];

async function voiceRequest<T>(server: string, path: string, options: RequestInit, timeout = 125_000): Promise<T> {
  try {
    return await fetchApiAtServerFingerprint<T>(server, `/api/mobile-voice/${path}`, options, timeout);
  } catch (error) {
    if (isApiHttpError(error)) {
      if (error.status === 404) throw new Error("サーバーの音声APIまたはキャラクター設定が見つかりません。サーバーの更新・設定を確認してください。");
      try {
        const body = JSON.parse(error.responseBody) as { detail?: unknown };
        if (typeof body.detail === "string") throw new Error(body.detail);
      } catch (detailError) {
        if (detailError instanceof Error && !(detailError instanceof SyntaxError)) throw detailError;
      }
    }
    throw new Error("音声機能にはAoiTalkサーバーへの接続が必要です。接続とログイン状態を確認してください。");
  }
}

export const mobileVoiceApi = {
  capabilities(server: string, signal: AbortSignal): Promise<VoiceCapabilities> {
    return voiceRequest(server, "capabilities", { signal }, 12_000);
  },
  async transcribe(server: string, scope: VoiceScope, uri: string, signal: AbortSignal): Promise<string> {
    const info = await FileSystem.getInfoAsync(uri);
    if (!info.exists || ("size" in info && info.size > 2 * 1024 * 1024)) throw new Error("録音データが空か、大きすぎます。");
    const audio = await FileSystem.readAsStringAsync(uri, { encoding: FileSystem.EncodingType.Base64 });
    if (signal.aborted) throw new Error("音声入力を中止しました。");
    const payload: Schema["MobileVoiceTranscriptionRequest"] = { ...scope, audio_base64: audio, mime_type: "audio/mp4" };
    const response = await voiceRequest<Schema["MobileVoiceTranscriptionResponse"]>(server, "transcriptions", { method: "POST", body: JSON.stringify(payload), signal });
    return response.text;
  },
  async speech(server: string, scope: VoiceScope, text: string, character: string, signal: AbortSignal): Promise<string> {
    const payload: Schema["MobileVoiceSpeechRequest"] = { ...scope, text, character_name: character };
    const response = await voiceRequest<Schema["MobileVoiceSpeechResponse"]>(server, "speech", { method: "POST", body: JSON.stringify(payload), signal });
    return response.audio_base64;
  },
};

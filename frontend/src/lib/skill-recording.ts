"use client";

// 画面録画 + 音声説明から Skill を自動生成する機能のクライアント側ユーティリティ。
// MediaRecorder（ブラウザネイティブ）で画面共有映像 + マイク音声を録画し、
// バックエンドの skill-recordings API と連携して解析ドラフトを取得・保存する。

import { useCallback, useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// 録画（MediaRecorder）
// ---------------------------------------------------------------------------

export type SkillRecordingPhase = "idle" | "recording" | "stopped";

/**
 * ブラウザが画面録画（getDisplayMedia + MediaRecorder）に対応しているかを判定する。
 * SSR や非対応ブラウザでは false を返し、導線側でボタンを無効化する。
 */
export function isScreenRecordingSupported(): boolean {
  if (typeof navigator === "undefined" || typeof window === "undefined") {
    return false;
  }
  return (
    !!navigator.mediaDevices &&
    typeof navigator.mediaDevices.getDisplayMedia === "function" &&
    typeof window.MediaRecorder !== "undefined"
  );
}

// 対応する webm コーデックを優先度順に探索する。いずれも未対応なら
// mimeType 未指定で MediaRecorder に委譲する（ブラウザ既定を使う）。
function pickSupportedMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  const candidates = [
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm;codecs=vp9",
    "video/webm;codecs=vp8",
    "video/webm",
  ];
  for (const type of candidates) {
    try {
      if (MediaRecorder.isTypeSupported(type)) return type;
    } catch {
      // isTypeSupported 自体が無い環境は無視して次候補へ
    }
  }
  return undefined;
}

export interface SkillRecorderController {
  phase: SkillRecordingPhase;
  /** 録画エラー（権限拒否・非対応など）。ユーザー向け日本語メッセージ。 */
  error: string | null;
  /** 経過秒数（録画中は 1 秒ごとに更新）。 */
  durationSec: number;
  /** 停止後に生成される録画データ。未生成なら null。 */
  blob: Blob | null;
  /** blob のオブジェクト URL（プレビュー用）。停止後に生成。 */
  previewUrl: string | null;
  /** 録画に使用した mimeType。 */
  mimeType: string | null;
  /** 録画中プレビュー用の生ストリーム（映像トラックのみ表示すればよい）。 */
  liveStream: MediaStream | null;
  start: () => Promise<void>;
  stop: () => void;
  /** 状態を初期化し、保持中のリソースを解放する。 */
  reset: () => void;
}

/**
 * 画面共有映像 + マイク音声を合成して録画する MediaRecorder 管理フック。
 * ユーザーがブラウザ UI から共有を停止した場合も録画停止として扱う。
 */
export function useSkillRecorder(): SkillRecorderController {
  const [phase, setPhase] = useState<SkillRecordingPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [durationSec, setDurationSec] = useState(0);
  const [blob, setBlob] = useState<Blob | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [mimeType, setMimeType] = useState<string | null>(null);
  const [liveStream, setLiveStream] = useState<MediaStream | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const displayStreamRef = useRef<MediaStream | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const combinedStreamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number | null>(null);
  const startedAtRef = useRef<number>(0);
  const previewUrlRef = useRef<string | null>(null);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // 取得済みの全トラック・ストリームを停止する（映像・音声の LED を消す）。
  const stopAllTracks = useCallback(() => {
    for (const stream of [
      displayStreamRef.current,
      micStreamRef.current,
    ]) {
      stream?.getTracks().forEach((track) => track.stop());
    }
    displayStreamRef.current = null;
    micStreamRef.current = null;
    combinedStreamRef.current = null;
    setLiveStream(null);
  }, []);

  const revokePreview = useCallback(() => {
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = null;
    }
  }, []);

  const start = useCallback(async () => {
    if (!isScreenRecordingSupported()) {
      setError("このブラウザは画面録画に対応していません。");
      return;
    }
    setError(null);
    setBlob(null);
    setDurationSec(0);
    revokePreview();
    setPreviewUrl(null);
    chunksRef.current = [];

    let displayStream: MediaStream;
    try {
      // 画面／ウィンドウ選択。音声（システム音）は任意で拾えたら拾う。
      displayStream = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: true,
      });
    } catch (err) {
      const name = err instanceof DOMException ? err.name : "";
      setError(
        name === "NotAllowedError"
          ? "画面共有がキャンセルされました。"
          : "画面共有を開始できませんでした。",
      );
      return;
    }

    let micStream: MediaStream | null = null;
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      // マイクが取得できなくても録画自体は継続する（映像のみ）。
      micStream = null;
    }

    displayStreamRef.current = displayStream;
    micStreamRef.current = micStream;

    // 表示ストリームの映像トラック + マイク音声トラックを合成する。
    const tracks: MediaStreamTrack[] = [...displayStream.getVideoTracks()];
    if (micStream) {
      tracks.push(...micStream.getAudioTracks());
    } else {
      // マイクが無い場合は共有側の音声トラックがあれば使う。
      tracks.push(...displayStream.getAudioTracks());
    }
    const combined = new MediaStream(tracks);
    combinedStreamRef.current = combined;
    setLiveStream(displayStream);

    const selectedMime = pickSupportedMimeType();
    let recorder: MediaRecorder;
    try {
      recorder = selectedMime
        ? new MediaRecorder(combined, { mimeType: selectedMime })
        : new MediaRecorder(combined);
    } catch {
      stopAllTracks();
      setError("録画を初期化できませんでした。");
      return;
    }
    recorderRef.current = recorder;
    setMimeType(recorder.mimeType || selectedMime || "video/webm");

    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        chunksRef.current.push(event.data);
      }
    };
    recorder.onstop = () => {
      clearTimer();
      const type = recorder.mimeType || selectedMime || "video/webm";
      const recorded = new Blob(chunksRef.current, { type });
      setBlob(recorded);
      const url = URL.createObjectURL(recorded);
      previewUrlRef.current = url;
      setPreviewUrl(url);
      stopAllTracks();
      setPhase("stopped");
    };

    // ユーザーがブラウザの「共有を停止」を押した場合も録画を止める。
    const primaryVideoTrack = displayStream.getVideoTracks()[0];
    if (primaryVideoTrack) {
      primaryVideoTrack.addEventListener("ended", () => {
        if (recorderRef.current && recorderRef.current.state !== "inactive") {
          recorderRef.current.stop();
        }
      });
    }

    startedAtRef.current = Date.now();
    clearTimer();
    timerRef.current = window.setInterval(() => {
      setDurationSec(Math.floor((Date.now() - startedAtRef.current) / 1000));
    }, 1000);

    recorder.start(1000); // 1 秒ごとに chunk 化して欠落を防ぐ
    setPhase("recording");
  }, [clearTimer, revokePreview, stopAllTracks]);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    } else {
      clearTimer();
      stopAllTracks();
      setPhase("stopped");
    }
  }, [clearTimer, stopAllTracks]);

  const reset = useCallback(() => {
    clearTimer();
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      try {
        recorderRef.current.stop();
      } catch {
        // 既に停止済みなら無視
      }
    }
    recorderRef.current = null;
    stopAllTracks();
    revokePreview();
    chunksRef.current = [];
    setPhase("idle");
    setError(null);
    setDurationSec(0);
    setBlob(null);
    setPreviewUrl(null);
    setMimeType(null);
  }, [clearTimer, revokePreview, stopAllTracks]);

  // アンマウント時にストリーム・タイマー・URL を確実に解放する。
  useEffect(() => {
    return () => {
      clearTimer();
      if (recorderRef.current && recorderRef.current.state !== "inactive") {
        try {
          recorderRef.current.stop();
        } catch {
          // 無視
        }
      }
      stopAllTracks();
      revokePreview();
    };
  }, [clearTimer, revokePreview, stopAllTracks]);

  return {
    phase,
    error,
    durationSec,
    blob,
    previewUrl,
    mimeType,
    liveStream,
    start,
    stop,
    reset,
  };
}

/** 秒数を mm:ss 形式に整形する。 */
export function formatDuration(totalSec: number): string {
  const sec = Math.max(0, Math.floor(totalSec));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// バックエンド API 連携
// ---------------------------------------------------------------------------

const API_BASE = "/api/python-proxy/skill-recordings";

// バックエンド（skill_recording_service.MAX_UPLOAD_BYTES）と揃えた上限。
const MAX_UPLOAD_BYTES = 500 * 1024 * 1024;

export type SkillRecordingStatus =
  | "uploaded"
  | "analyzing"
  | "draft_ready"
  | "failed";

export interface SkillRecordingRecord {
  id: string;
  status: SkillRecordingStatus;
  error?: string | null;
  title?: string | null;
  project_id?: string | null;
  created_at?: string | null;
}

export interface SkillRecordingFrameNote {
  time_sec: number;
  note: string;
}

export interface SkillRecordingDraft {
  name: string;
  description: string;
  markdown: string;
  trigger_mode: string;
  bound_tools: string[];
  transcript: string;
  frame_notes: SkillRecordingFrameNote[];
}

export interface SkillRecordingSavePayload {
  name: string;
  description: string;
  markdown: string;
  trigger_mode: string;
  target: "global" | "project";
  project_id?: string;
  delete_recording: boolean;
}

export interface SkillRecordingSaveResult {
  saved: boolean;
  location: string;
}

async function readError(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: string };
    if (data?.detail) return data.detail;
  } catch {
    // JSON 以外のレスポンスは無視
  }
  return `API Error: ${res.status}`;
}

/**
 * 録画データ（webm）を multipart/form-data でアップロードする。
 * 動画は大きいため FormData を使い、XHR で進捗（0〜100）を通知する。
 */
export function uploadSkillRecording(
  blob: Blob,
  options: {
    title?: string;
    projectId?: string | null;
    fileName?: string;
    onProgress?: (percent: number) => void;
    signal?: AbortSignal;
  } = {},
): Promise<SkillRecordingRecord> {
  return new Promise((resolve, reject) => {
    // サーバー側上限（500MB）を超える送信を事前に防ぐ。
    if (blob.size > MAX_UPLOAD_BYTES) {
      reject(
        new Error("録画サイズが上限（500MB）を超えています。録画を短くして再試行してください。"),
      );
      return;
    }
    const formData = new FormData();
    formData.append("file", blob, options.fileName || "skill-recording.webm");
    if (options.title) formData.append("title", options.title);
    if (options.projectId) formData.append("project_id", options.projectId);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", API_BASE);
    xhr.withCredentials = true;
    // Content-Type はブラウザに設定させる（boundary 付与のため明示指定しない）。

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && options.onProgress) {
        options.onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as SkillRecordingRecord);
        } catch {
          reject(new Error("アップロード応答の解析に失敗しました。"));
        }
      } else {
        let detail = `アップロードに失敗しました (${xhr.status})`;
        try {
          const parsed = JSON.parse(xhr.responseText) as { detail?: string };
          if (parsed?.detail) detail = parsed.detail;
        } catch {
          // 無視
        }
        reject(new Error(detail));
      }
    };
    xhr.onerror = () => reject(new Error("アップロード中に通信エラーが発生しました。"));
    xhr.onabort = () => reject(new DOMException("Aborted", "AbortError"));

    if (options.signal) {
      if (options.signal.aborted) {
        xhr.abort();
        return;
      }
      options.signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }

    xhr.send(formData);
  });
}

/** 解析を開始する。 */
export async function analyzeSkillRecording(
  id: string,
): Promise<SkillRecordingRecord> {
  const res = await fetch(`${API_BASE}/${encodeURIComponent(id)}/analyze`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** 現在の解析ステータスを取得する（ポーリング用）。 */
export async function getSkillRecording(
  id: string,
): Promise<SkillRecordingRecord> {
  const res = await fetch(`${API_BASE}/${encodeURIComponent(id)}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** 解析結果のドラフトを取得する。 */
export async function getSkillRecordingDraft(
  id: string,
): Promise<SkillRecordingDraft> {
  const res = await fetch(`${API_BASE}/${encodeURIComponent(id)}/draft`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** ドラフトを Skill として保存する。 */
export async function saveSkillRecording(
  id: string,
  payload: SkillRecordingSavePayload,
): Promise<SkillRecordingSaveResult> {
  const res = await fetch(`${API_BASE}/${encodeURIComponent(id)}/save`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** 録画を破棄（削除）する。 */
export async function deleteSkillRecording(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/${encodeURIComponent(id)}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!res.ok && res.status !== 404) throw new Error(await readError(res));
}

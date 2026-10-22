import {decodeWaveMeter,observeRigPlayback} from '../characters/rig-playback';
import { Audio, type AVPlaybackStatus } from "expo-av";
import * as FileSystem from "expo-file-system/legacy";
import type { VoiceRecording } from "./voice-session";

function aborted(): Error { return new Error("音声操作を中止しました。"); }
function check(signal: AbortSignal) { if (signal.aborted) throw aborted(); }

/** A silence gate, not an ASR result: silence is never uploaded or sent as chat. */
export class UtteranceDetector {
  private voicedMs = 0;
  private lastVoiceMs = 0;
  private previousMs = 0;
  update(durationMs: number, metering: number | undefined): "send" | "discard" | null {
    const elapsed = Math.max(0, Math.min(250, durationMs - this.previousMs));
    this.previousMs = durationMs;
    if (metering !== undefined && metering > -42) {
      this.voicedMs += elapsed;
      this.lastVoiceMs = durationMs;
    }
    const hasSpeech = this.voicedMs >= 300;
    if (durationMs >= 29_000) return hasSpeech ? "send" : "discard";
    if (hasSpeech && durationMs - this.lastVoiceMs >= 1100) return "send";
    if (!hasSpeech && durationMs >= 10_000) return "discard";
    return null;
  }
}

export async function requestMicrophone(): Promise<void> {
  const permission = await Audio.requestPermissionsAsync();
  if (!permission.granted) throw new Error("マイクの許可がありません。端末の設定から許可してください。文字入力と読み上げは利用できます。");
}

export async function recordUtterance(signal: AbortSignal): Promise<VoiceRecording | null> {
  check(signal);
  await Audio.setAudioModeAsync({ allowsRecordingIOS: true, playsInSilentModeIOS: true, staysActiveInBackground: false, shouldDuckAndroid: true, playThroughEarpieceAndroid: false });
  check(signal);
  const recording = new Audio.Recording();
  let uri: string | null = null;
  let send = false;
  const detector = new UtteranceDetector();
  let settle: ((result: boolean) => void) | null = null;
  const stop = () => settle?.(false);
  try {
    await recording.prepareToRecordAsync({ ...Audio.RecordingOptionsPresets.HIGH_QUALITY, isMeteringEnabled: true });
    check(signal);
    // Register before startAsync: even a background/abort during native startup
    // must settle, unload, and delete the temporary recording.
    const completion = new Promise<boolean>((resolve) => { settle = resolve; });
    signal.addEventListener("abort", stop, { once: true });
    recording.setProgressUpdateInterval(100);
    recording.setOnRecordingStatusUpdate((status) => {
      if (!status.isRecording) return;
      const result = detector.update(status.durationMillis, status.metering);
      if (result) settle?.(result === "send");
    });
    await recording.startAsync();
    if (signal.aborted) stop();
    const timeout = setTimeout(stop, 30_000);
    try { send = await completion; } finally { clearTimeout(timeout); }
  } finally {
    signal.removeEventListener("abort", stop);
    recording.setOnRecordingStatusUpdate(null);
    try { await recording.stopAndUnloadAsync(); } catch { /* Empty/aborted native recordings can reject E_AUDIO_NODATA. */ }
    uri = recording.getURI();
    await Audio.setAudioModeAsync({ allowsRecordingIOS: false }).catch(() => undefined);
    if ((!send || signal.aborted) && uri) await FileSystem.deleteAsync(uri, { idempotent: true }).catch(() => undefined);
  }
  if (!send || signal.aborted || !uri) return null;
  const path = uri;
  return { uri: path, dispose: () => FileSystem.deleteAsync(path, { idempotent: true }) };
}

export async function playVoiceBase64(encoded: string, signal: AbortSignal, character?:string): Promise<void> {
  check(signal);
  if (!FileSystem.cacheDirectory) throw new Error("音声の一時保存先を利用できません。");
  const path = `${FileSystem.cacheDirectory}chat-voice-${Date.now()}-${Math.random().toString(36).slice(2)}.wav`;
  let sound: Audio.Sound | null = null;
  const playback=character?observeRigPlayback(character,{},decodeWaveMeter(encoded)):null;
  try {
    await FileSystem.writeAsStringAsync(path, encoded, { encoding: FileSystem.EncodingType.Base64 });
    check(signal);
    await Audio.setAudioModeAsync({ allowsRecordingIOS: false, playsInSilentModeIOS: true, staysActiveInBackground: false, shouldDuckAndroid: true, playThroughEarpieceAndroid: false });
    check(signal);
    const created = await Audio.Sound.createAsync({ uri: path }, { shouldPlay: false, progressUpdateIntervalMillis: 100 });
    sound = created.sound;
    check(signal);
    await new Promise<void>((resolve, reject) => {
      const cleanup = () => { signal.removeEventListener("abort", cancel); clearTimeout(timer); };
      const cancel = () => { cleanup(); reject(aborted()); };
      const timer = setTimeout(() => { cleanup(); reject(new Error("音声再生がタイムアウトしました。")); }, 120_000);
      signal.addEventListener("abort", cancel, { once: true });
      sound!.setOnPlaybackStatusUpdate((status: AVPlaybackStatus) => {
        if (!status.isLoaded) {
          playback?.update(0,false);
          if (status.error) { cleanup(); reject(new Error("音声を再生できませんでした。")); }
          return;
        }
        playback?.update(status.positionMillis,status.isPlaying&&!status.didJustFinish);
        if (status.didJustFinish) { cleanup(); resolve(); }
      });
      void sound!.playAsync().catch((error: unknown) => { cleanup(); reject(error); });
      if (signal.aborted) cancel();
    });
  } finally {
    playback?.dispose();
    if (sound) {
      sound.setOnPlaybackStatusUpdate(null);
      await sound.unloadAsync().catch(() => undefined);
    }
    await FileSystem.deleteAsync(path, { idempotent: true }).catch(() => undefined);
  }
}

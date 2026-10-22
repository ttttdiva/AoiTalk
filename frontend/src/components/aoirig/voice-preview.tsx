"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import type { components } from "@/lib/api-types.gen";
import { decodeLipAudio, observeMediaPlayback } from "@/lib/aoirig/lip-sync";

/** Uses the existing bounded speech worker shared by native and Voice Sessions. */
export function CharacterVoicePreview({ characterName, onPlaybackStart }: { characterName: string; onPlaybackStart?: () => void }) {
  const media = useRef<HTMLAudioElement>(null);
  const clip = useRef<{ url: string; buffer: AudioBuffer } | null>(null);
  const abort = useRef<AbortController | null>(null);
  const observation = useRef<(() => void) | null>(null);
  const [busy, setBusy] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const audio = media.current;
    return () => {
      abort.current?.abort();
      observation.current?.();
      if (clip.current) {
        audio?.pause();
        URL.revokeObjectURL(clip.current.url);
        clip.current = null;
      }
    };
  }, [characterName]);

  async function play() {
    if (!media.current) return;
    if (playing) { media.current.pause(); return; }
    const controller = new AbortController();
    abort.current?.abort(); abort.current = controller;
    setBusy(true); setError("");
    try {
      if (!clip.current) {
        const body: components["schemas"]["MobileVoiceSpeechRequest"] = {
          character_name: characterName, text: "こんにちは。今日はどんなお話をしましょうか。",
        };
        const response = await fetch("/api/python-proxy/mobile-voice/speech", {
          method: "POST", credentials: "include", signal: controller.signal,
          headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "音声を生成できませんでした");
        const speech = payload as components["schemas"]["MobileVoiceSpeechResponse"];
        if (typeof speech.audio_base64 !== "string" || speech.audio_base64.length > 12 * 1024 * 1024) throw new Error("音声データが不正です");
        const bytes = Uint8Array.from(atob(speech.audio_base64), (c) => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: speech.mime_type });
        const buffer = await decodeLipAudio(blob);
        if (controller.signal.aborted) return;
        if (!buffer) throw new Error("この音声を口パク用に解析できませんでした");
        clip.current = { url: URL.createObjectURL(blob), buffer };
      }
      if (controller.signal.aborted || !media.current) return;
      media.current.src = clip.current.url;
      media.current.currentTime = 0;
      observation.current?.();
      observation.current = observeMediaPlayback(characterName, {
        media: media.current, buffer: clip.current.buffer, session: "character-preview",
        generation: clip.current.url, segment: clip.current.url, visemes: [],
      });
      await media.current.play();
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "音声を再生できませんでした");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }

  return <div className="space-y-1">
    <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void play()}>
      {busy ? "音声を準備中…" : playing ? "試聴を停止" : "声と口パクを確認"}
    </Button>
    <audio ref={media} preload="auto" onPlay={() => { setPlaying(true); onPlaybackStart?.(); }} onPause={() => setPlaying(false)}
      onEnded={() => { setPlaying(false); observation.current?.(); observation.current = null; }} />
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
  </div>;
}

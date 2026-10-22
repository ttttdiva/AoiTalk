"use client";

import { useState } from "react";
import Image from "next/image";
import useSWR from "swr";
import { SeeThroughCanvas } from "./seethrough-canvas";
import { rigFetch, rigUrl, type RigView } from "@/lib/seethrough-rig/api";
import { useRigPlaybackSettings } from "@/lib/seethrough-rig/playback-settings";
import type { PlaybackSettings } from "@aoitalk/seethrough-rig-runtime";
import type { ConversationState } from "@/lib/aoirig/conversation";
import type { ReactionCue } from "@/lib/aoirig/reactions";

export { rigFetch, rigUrl, type RigView };

type RigCanvasProps = {
  url: string;
  character: string;
  modelFormat?: string | null;
  state?: ConversationState;
  expression?: string;
  focus?: "full" | "face";
  paused?: boolean;
  interactive?: boolean;
  pointerTracking?: boolean;
  previewEyes?: number;
  previewJitome?: boolean;
  previewMouth?: number;
  cue?: ReactionCue;
  faceZoomScale?: number;
  fallback?: React.ReactNode;
  playbackSettings?: PlaybackSettings;
  onCapabilities?: (value: Record<string, unknown>) => void;
};

function storedFormat(value: string | null | undefined): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function viewNames(caps: Record<string, unknown>): string[] {
  return Array.isArray(caps.views) ? caps.views.filter((view): view is string => typeof view === "string") : [];
}

export function RigCanvas({ modelFormat, fallback, ...props }: RigCanvasProps) {
  const format = storedFormat(modelFormat);
  const status = format ? "このモデル形式は未対応です" : "See-through Rigモデル未設定";
  if (format === "seethrough-rig") return <SeeThroughCanvas {...props} />;
  return <div>{fallback}<p role="status">{status}</p></div>;
}

export function CharacterAnimation({ character, state = "idle", cue }: { character?: string | null; state?: ConversationState; cue?: ReactionCue }) {
  // Old Appearance saves contain full snapshots, including inherited
  // tracking=false. A chat-local toggle makes the new default unambiguous.
  const [pointerTracking, setPointerTracking] = useState(true);
  const [focus, setFocus] = useState<"face" | "full">("face");
  const [reported, setReported] = useState<{ key: string; caps: Record<string, unknown> } | null>(null);
  const { data } = useSWR(character ? rigUrl(character) : null, rigFetch, { revalidateOnFocus: true, shouldRetryOnError: false, keepPreviousData: false });
  const { data: playback } = useRigPlaybackSettings(data?.character_id ?? character ?? "", data?.active_revision);
  const revisionKey = `${character ?? ""}:${data?.active_revision ?? ""}`;
  const caps = reported?.key === revisionKey ? reported.caps : {};
  if (!character || !data || (!data.has_source && !data.has_model && !data.active_revision)) return null;
  const base = rigUrl(character);
  const activeBuild = data.builds.find((build) => build.id === data.active_revision);
  const modelFormat = storedFormat(activeBuild?.model_format ?? data.active_format);
  const activeSeethrough = Boolean(data.active_revision && modelFormat === "seethrough-rig");
  const still = activeSeethrough || !data.has_source ? null : `${base}/source.png`;
  const fallback = activeSeethrough
    ? null
    : still
      ? <Image src={still} alt={character} width={320} height={400} unoptimized className="ao-chat-character-still max-h-72 h-auto w-full object-contain" />
      : <p className="text-xs text-muted-foreground">取り込んだモデルを設定で選択してください。</p>;
  const views = activeSeethrough ? viewNames(caps) : [];
  return <section className="ao-chat-character shrink-0 border-b border-border-subtle px-4 py-3" aria-label="キャラクター">
    {data.active_revision ? <>
      <RigCanvas interactive pointerTracking={pointerTracking} playbackSettings={playback?.editable ? playback.effective as PlaybackSettings : undefined} modelFormat={modelFormat} url={`${base}/assets/${data.active_revision}`} character={character} state={state} cue={cue} focus={focus} faceZoomScale={0.72} fallback={fallback} onCapabilities={(value) => setReported({ key: revisionKey, caps: value })} />
      {(views.includes("face") || views.includes("full") || caps.gaze === true) && <div className="mt-1 flex justify-end gap-2 text-xs text-muted-foreground">
        {caps.gaze === true && <button type="button" aria-label="カーソルへの視線追従" aria-pressed={pointerTracking} title="チャット中のカーソル追従を切り替え" onClick={() => setPointerTracking(value => !value)}>視線</button>}
        {views.includes("face") && <button type="button" aria-pressed={focus === "face"} onClick={() => setFocus("face")}>顔</button>}
        {views.includes("full") && <button type="button" aria-pressed={focus === "full"} onClick={() => setFocus("full")}>全身</button>}
      </div>}
    </> : fallback}
  </section>;
}

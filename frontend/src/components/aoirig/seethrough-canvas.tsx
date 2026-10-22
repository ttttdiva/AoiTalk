"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { bindAvatarInteraction, touchFace } from "@/lib/aoirig/avatar-interaction";
import { sampleLip } from "@/lib/aoirig/lip-sync";
import { rigConversation, rigReaction, type ConversationState } from "@/lib/aoirig/conversation";
import { RigFrameLoop } from "@/lib/seethrough-rig/frame-loop";
import { fetchManifest } from "@/lib/seethrough-rig/manifest";
import type { ReactionCue } from "@/lib/aoirig/reactions";
import { filterPlaybackSettings, type RigRuntime, type PlaybackSettings } from "@aoitalk/seethrough-rig-runtime";

type SeeThroughCanvasProps = {
  url: string;
  character: string;
  state?: ConversationState;
  expression?: string;
  focus?: "full" | "face";
  paused?: boolean;
  /** Enabled only by the chat surface, never by production/settings previews. */
  interactive?: boolean;
  pointerTracking?: boolean;
  cue?: ReactionCue;
  previewEyes?: number;
  previewJitome?: boolean;
  previewMouth?: number;
  faceZoomScale?: number;
  playbackSettings?: PlaybackSettings;
  onCapabilities?: (value: Record<string, unknown>) => void;
};

/** Chat has an explicit cursor-follow default; old packages commonly export
 * tracking=false. Never persist this host override or apply it to previews. */
function applyPlayback(current: RigRuntime, settings: PlaybackSettings | undefined, chatGaze: boolean) {
  if (settings && current.capabilities.playbackSettings?.length) current.setPlaybackSettings(filterPlaybackSettings(settings, current.capabilities));
  if (chatGaze && current.capabilities.gaze && current.capabilities.playbackSettings?.includes('gaze.enabled')) {
    current.setPlaybackSettings(filterPlaybackSettings({ gaze: { enabled: true, tracking: true } }, current.capabilities));
  }
}

const CANVAS_HEIGHT = "min(24rem, 60vh)";
type LoadGeneration = { url: string; character: string };
type LoadStatus = { url: string; character: string; generation: LoadGeneration; error?: string };

export function SeeThroughCanvas({
  url,
  character,
  state = "idle",
  expression = "neutral",
  focus = "full",
  paused = false,
  interactive = false,
  pointerTracking = true,
  cue,
  previewEyes,
  previewJitome = false,
  previewMouth,
  faceZoomScale = 1,
  onCapabilities,
  playbackSettings,
}: SeeThroughCanvasProps) {
  const generation = useMemo<LoadGeneration>(() => ({ url, character }), [url, character]);
  const canvas = useRef<HTMLCanvasElement>(null);
  const runtime = useRef<RigRuntime | null>(null);
  const loop = useRef<RigFrameLoop | null>(null);
  const visible = useRef(true);
  const inputs = useRef({ state, expression, focus, paused, cue, previewEyes, previewJitome, previewMouth, faceZoomScale, pointerTracking });
  const [status, setStatus] = useState<LoadStatus | null>(null);
  const capsCallback = useRef(onCapabilities);
  const playback = useRef(playbackSettings);

  useEffect(() => {
    playback.current = playbackSettings;
    const current = runtime.current;
    if (current) applyPlayback(current, playbackSettings, interactive && pointerTracking);
  }, [playbackSettings, interactive, pointerTracking]);

  useEffect(() => {
    capsCallback.current = onCapabilities;
  }, [onCapabilities]);

  useEffect(() => {
    inputs.current = { state, expression, focus, paused, cue, previewEyes, previewJitome, previewMouth, faceZoomScale, pointerTracking };
    loop.current?.setActive(visible.current && !document.hidden && !paused);
  }, [state, expression, focus, paused, cue, previewEyes, previewJitome, previewMouth, faceZoomScale, pointerTracking]);

  useEffect(() => {
    const controller = new AbortController();
    let instance: RigRuntime | null = null;
    let scheduler: RigFrameLoop | null = null;
    let interaction: ReturnType<typeof bindAvatarInteraction> | null = null;
    const element = canvas.current;
    if (!element) return;

    let intersecting = true;
    const update = () => {
      visible.current = intersecting && !document.hidden;
      if (!visible.current) interaction?.reset();
      scheduler?.setActive(visible.current && !inputs.current.paused);
    };
    const observer = typeof IntersectionObserver === "undefined"
      ? null
      : new IntersectionObserver((entries) => {
          intersecting = entries.some((entry) => entry.isIntersecting);
          update();
        });
    observer?.observe(element);
    document.addEventListener("visibilitychange", update);

    const lost = (event: Event) => {
      event.preventDefault();
      controller.abort();
      interaction?.dispose(); interaction = null;
      scheduler?.dispose();
      instance?.dispose();
      if (runtime.current === instance) runtime.current = null;
      setStatus({ url, character, generation, error: "描画を停止しました。モデルを選び直してください" });
    };
    element.addEventListener("webglcontextlost", lost);

    void (async () => {
      const manifest = await fetchManifest(url, controller.signal);
      const { RigRuntime } = await import("@aoitalk/seethrough-rig-runtime");
      if (controller.signal.aborted) return;
      const created = await RigRuntime.create(element, `${url}/${manifest.model}`, {
        signal: controller.signal,
        fetch: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, { ...init, credentials: "include" }),
        settingsUrl: manifest.settings ? `${url}/${manifest.settings}` : undefined,
        manifest,
        profile: { ...manifest.profile, id: "conversation-v1" },
        playbackSettings: playback.current,
        seed: Number(manifest.profile.seed ?? 3),
        pixelRatio: Math.min(window.devicePixelRatio || 1, 2),
      });
      if (controller.signal.aborted) {
        created.dispose();
        return;
      }
      instance = created;
      runtime.current = created;
      applyPlayback(created, playback.current, interactive && inputs.current.pointerTracking);
      capsCallback.current?.(created.capabilities as unknown as Record<string, unknown>);
      let hadJitomePreview = false;
      let hadTouch = false;
      let hadGaze = false;
      const clearInteraction = () => {
        element.style.transform = '';
        if (hadGaze) { created.setGaze(0, 0); hadGaze = false; }
        if (hadTouch) {
          created.setParameters(touchFace(created.capabilities.parameters, 'shy', 0));
          const expression = inputs.current.expression;
          created.setExpression(created.capabilities.expressions.includes(expression) ? expression : null);
          hadTouch = false;
        }
      };
      if (interactive) interaction = bindAvatarInteraction(element,
        () => visible.current && !document.hidden && !inputs.current.paused && runtime.current === created,
        clearInteraction);

      scheduler = new RigFrameLoop(
        (dt) => {
          const v = inputs.current;
          const lip = sampleLip(character);
          const reaction = v.cue ?? rigReaction(character);
          const touch = interaction?.state.step(dt);
          if (touch) {
            element.style.transform = touch.transform;
            const gazeEnabled = v.pointerTracking && !interaction!.state.reducedMotion && created.capabilities.gaze && created.getPlaybackSettings().gaze.enabled;
            if (gazeEnabled) { created.setGaze(touch.gaze.x, touch.gaze.y); hadGaze = true; }
            else if (hadGaze) { created.setGaze(0, 0); hadGaze = false; }
            const faceStrength = reaction?.kind === 'error' ? 0 : touch.strength;
            if (faceStrength > 0 || hadTouch) {
              created.setParameters(touchFace(created.capabilities.parameters, touch.mood, faceStrength));
              hadTouch = faceStrength > 0;
            }
          }
          const view = created.capabilities.views.includes(v.focus) ? v.focus : created.capabilities.views[0];
          if (view) created.setView(view, { zoomScale: view === "face" ? v.faceZoomScale : 1 });
          const desired = reaction?.expression ?? (reaction?.kind === "reply" ? "happy" : reaction?.kind === "error" ? "worried" : reaction?.kind === "greeting" ? "shy" : reaction ? "surprised" : undefined);
          // Resolve one host request, rather than alternating neutral and a reply cue.
          const touchExpression = touch && touch.strength > .05
            ? [touch.mood, touch.mood === 'shy' ? 'happy' : 'shy'].find(id => created.capabilities.expressions.includes(id))
            : undefined;
          // An error cue remains authoritative; touch otherwise supplies a brief
          // expression. Runtime removes its mouth channels during real speech.
          const effectiveExpression = reaction?.kind !== 'error' && touchExpression
            ? touchExpression : desired && created.capabilities.expressions.includes(desired) ? desired : v.expression;
          const jitomePreview = v.previewJitome && created.capabilities.parameters.includes("EyeJitome");
          // This temporary inspection input never rewrites the model's named presets.
          created.setExpression(jitomePreview ? null : created.capabilities.expressions.includes(effectiveExpression) ? effectiveExpression : null);
          if (jitomePreview || hadJitomePreview) {
            created.setParameters({ EyeJitome: jitomePreview ? 1 : 0 });
          }
          hadJitomePreview = jitomePreview;
          created.setConversation(lip.speaking ? "speaking" : rigConversation(character) ?? v.state);
          created.setMouth({ open: v.previewMouth ?? lip.open, form: lip.shape, speaking: lip.speaking });
          if (v.previewEyes !== undefined) {
            created.setParameters(Object.fromEntries(["EyeLOpen", "EyeROpen"].filter((id) => created.capabilities.parameters.includes(id)).map((id) => [id, v.previewEyes!])));
          }
          created.resize();
          created.step(dt);
        },
        () => {
          interaction?.reset();
          created.setMouth({ open: 0, speaking: false });
          created.pause();
        },
        () => created.resume(),
        (error) => {
          interaction?.dispose(); interaction = null;
          created.dispose();
          if (runtime.current === created) runtime.current = null;
          setStatus({ url, character, generation, error: error instanceof Error ? error.message : "モデルを描画できません" });
        },
      );
      created.resize();
      created.step(0);
      loop.current = scheduler;
      setStatus({ url, character, generation });
      update();
    })().catch((error) => {
      interaction?.dispose(); interaction = null;
      scheduler?.dispose();
      instance?.dispose();
      if (runtime.current === instance) runtime.current = null;
      if (!controller.signal.aborted) setStatus({ url, character, generation, error: error instanceof Error ? error.message : "モデルを読み込めません" });
    });

    return () => {
      controller.abort();
      interaction?.dispose(); interaction = null;
      observer?.disconnect();
      document.removeEventListener("visibilitychange", update);
      element.removeEventListener("webglcontextlost", lost);
      scheduler?.dispose();
      instance?.dispose();
      if (runtime.current === instance) runtime.current = null;
      if (loop.current === scheduler) loop.current = null;
    };
  }, [url, character, generation, interactive]);

  const currentStatus = status?.generation === generation ? status : null;
  const ready = Boolean(currentStatus && !currentStatus.error);
  const error = currentStatus?.error;
  return (
    <div className="relative w-full" data-testid="aoirig-surface" data-model-format="seethrough-rig" data-animated={ready} aria-busy={!ready && !error}>
      <canvas
        ref={canvas}
        aria-label={interactive ? `${character}のアニメーション。クリックでふれる、ドラッグでなでる` : `${character}のアニメーション`}
        role={interactive ? 'button' : undefined}
        tabIndex={interactive && ready ? 0 : undefined}
        title={interactive ? 'クリックでふれる・ドラッグでなでる' : undefined}
        onPointerMove={event => {
          const current = runtime.current;
          if (interactive || !current) return;
          const gaze = current.getPlaybackSettings().gaze;
          if (!gaze.enabled || !gaze.tracking) return;
          const bounds = event.currentTarget.getBoundingClientRect();
          if (bounds.width && bounds.height) current.setGaze((event.clientX - bounds.left) / bounds.width * 2 - 1, 1 - (event.clientY - bounds.top) / bounds.height * 2);
        }}
        onPointerLeave={() => {
          const current = runtime.current;
          if (!interactive && current?.getPlaybackSettings().gaze.tracking) current.setGaze(0, 0);
        }}
        aria-hidden={!ready}
        style={{ height: `var(--ao-rig-height, ${CANVAS_HEIGHT})`, maxHeight: "var(--ao-rig-max-height, 24rem)", opacity: ready ? 1 : 0, visibility: ready ? "visible" : "hidden", transformOrigin: "50% 75%", touchAction: interactive ? "pan-y" : undefined }}
        className={`block w-full object-contain ${interactive ? "select-none cursor-grab active:cursor-grabbing focus-visible:outline focus-visible:outline-primary" : ""}`}
      />
      {error && <p role="status" className="absolute inset-x-2 top-1/2 -translate-y-1/2 text-center text-xs text-muted-foreground">{error}</p>}
    </div>
  );
}

"use client";

import { useEffect, useRef, useState, useSyncExternalStore, type PointerEvent } from "react";
import { DEFAULT_PREFERENCES, clamp, lookFrame, type PetMotion } from "@/lib/pets/codex-pet";
import { getPetActivity, subscribePetActivity } from "@/lib/pets/pet-activity";
import { PetSprite } from "./pet-sprite";
import { usePet } from "./pet-provider";

export function useReducedPetMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(query.matches);
    update(); query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return reduced;
}
export function PetOverlay() {
  const context = usePet();
  return context?.available && context.ready && context.pet?.preferences.enabled
    ? <VisiblePet /> : null;
}
function VisiblePet() {
  const context = usePet()!;
  const { pet, updatePreferences, busy } = context;
  const preferences = pet!.preferences;
  const systemReduced = useReducedPetMotion();
  const reduced = systemReduced || preferences.reduceMotion;
  const activity = useSyncExternalStore(subscribePetActivity, getPetActivity, () => "idle" as PetMotion);
  const [settledActivity, setSettledActivity] = useState<PetMotion | null>(null);
  const [override, setOverride] = useState<PetMotion | null>(null);
  const [look, setLook] = useState<number | null>(null);
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const [dragPosition, setDragPosition] = useState<{ x: number; y: number } | null>(null);
  const drag = useRef<{ id: number; startX: number; startY: number; x: number; y: number; lastX: number; moved: boolean } | null>(null);
  const clickSuppressed = useRef(false);
  const root = useRef<HTMLDivElement>(null);
  const overrideTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => {
    const update = () => setViewport({ width: window.innerWidth, height: window.innerHeight });
    update(); window.addEventListener("resize", update);
    return () => { window.removeEventListener("resize", update); clearTimeout(overrideTimer.current); };
  }, []);
  // A terminal pose is a reaction, not a permanent state. Re-arm after idle/new work.
  useEffect(() => {
    // Re-arm even when a new response completes in less than 1.5 seconds.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSettledActivity(null);
    const timer = setTimeout(() => setSettledActivity(activity), activity === "failed" ? 3500 : 1500);
    return () => clearTimeout(timer);
  }, [activity]);
  const baseMotion = (activity === "waving" || activity === "failed") && settledActivity === activity ? "idle" : activity;
  useEffect(() => {
    if (!preferences.followPointer || reduced || baseMotion !== "idle") return;
    const follow = (event: globalThis.PointerEvent) => {
      if (event.pointerType === "touch" || drag.current) return;
      const rect = root.current?.getBoundingClientRect();
      if (!rect) return;
      const dx = event.clientX - rect.left - rect.width / 2;
      const dy = event.clientY - rect.top - rect.height / 2;
      setLook(Math.hypot(dx, dy) > 32 ? lookFrame(dx, dy) : null);
    };
    const leave = () => setLook(null);
    window.addEventListener("pointermove", follow, { passive: true });
    document.documentElement.addEventListener("pointerleave", leave);
    return () => {
      window.removeEventListener("pointermove", follow);
      document.documentElement.removeEventListener("pointerleave", leave);
    };
  }, [preferences.followPointer, reduced, baseMotion]);

  const size = Math.max(32, Math.min(preferences.size, viewport.width - 16, (viewport.height - 80) * 192 / 208));
  const maxX = Math.max(0, viewport.width - size - 16);
  const maxY = Math.max(0, viewport.height - size * 208 / 192 - 76);
  const x = clamp(dragPosition?.x ?? preferences.x * maxX, 0, maxX);
  const y = clamp(dragPosition?.y ?? preferences.y * maxY, 0, maxY);
  const motion = override ?? (baseMotion === "idle" && preferences.followPointer && !reduced && look !== null ? "look" : baseMotion);
  const stopDrag = (event: PointerEvent<HTMLButtonElement>, cancel = false) => {
    const state = drag.current;
    if (!state || state.id !== event.pointerId) return;
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    clickSuppressed.current = state.moved || cancel;
    setOverride(null);
    if (!cancel && state.moved) {
      const nextX = clamp(state.x + event.clientX - state.startX, 0, maxX);
      const nextY = clamp(state.y + event.clientY - state.startY, 0, maxY);
      void updatePreferences({ x: maxX ? nextX / maxX : 0, y: maxY ? nextY / maxY : 0 }).finally(() => setDragPosition(null));
    } else setDragPosition(null);
  };
  if (!viewport.width) return null;
  return <div ref={root} data-testid="pet-overlay" className="fixed z-40 select-none" style={{ left: x + 8, top: y + 60, width: size }}>
    <button type="button" aria-label="ペットを非表示" title="ペットを非表示（設定から再表示できます）" disabled={busy}
      className="absolute -right-1 -top-3 z-10 flex size-6 items-center justify-center rounded-full border bg-background text-foreground shadow"
      onClick={() => void updatePreferences({ enabled: false })}>×</button>
    <button type="button" aria-label={`${pet!.manifest.displayName}：ドラッグで移動、クリックで手振り`}
      title="ドラッグで移動・クリックで手振り。矢印キーでも移動できます。Homeで位置を初期化。"
      className="block cursor-grab rounded-md border-0 bg-transparent p-0 focus-visible:outline-2 focus-visible:outline-ring active:cursor-grabbing"
      style={{ touchAction: "none" }}
      onPointerDown={(event) => {
        if (event.button !== 0 || !event.isPrimary || busy) return;
        clearTimeout(overrideTimer.current); clickSuppressed.current = false;
        drag.current = { id: event.pointerId, startX: event.clientX, startY: event.clientY, x, y, lastX: event.clientX, moved: false };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        const state = drag.current;
        if (!state || state.id !== event.pointerId) return;
        if (Math.hypot(event.clientX - state.startX, event.clientY - state.startY) > 4) state.moved = true;
        if (!state.moved) return;
        setOverride(event.clientX >= state.lastX ? "running-right" : "running-left");
        state.lastX = event.clientX;
        setDragPosition({ x: state.x + event.clientX - state.startX, y: state.y + event.clientY - state.startY });
      }}
      onPointerUp={(event) => stopDrag(event)} onPointerCancel={(event) => stopDrag(event, true)}
      onLostPointerCapture={(event) => { if (drag.current) stopDrag(event, true); }}
      onClick={() => {
        if (clickSuppressed.current) { clickSuppressed.current = false; return; }
        clearTimeout(overrideTimer.current); setOverride("waving");
        overrideTimer.current = setTimeout(() => setOverride(null), 1400);
      }}
      onKeyDown={(event) => {
        const offsets: Record<string, [number, number]> = { ArrowLeft: [-16, 0], ArrowRight: [16, 0], ArrowUp: [0, -16], ArrowDown: [0, 16] };
        if (event.key === "Home") { event.preventDefault(); void updatePreferences({ x: DEFAULT_PREFERENCES.x, y: DEFAULT_PREFERENCES.y }); }
        else if (offsets[event.key]) {
          event.preventDefault(); const [dx, dy] = offsets[event.key];
          void updatePreferences({ x: maxX ? clamp((x + dx) / maxX, 0, 1) : 0, y: maxY ? clamp((y + dy) / maxY, 0, 1) : 0 });
        }
      }}>
      <PetSprite image={pet!.image} label={pet!.manifest.displayName} size={size} motion={motion} look={look ?? 0} reduced={reduced} />
    </button>
    {context.error && <p role="alert" className="rounded border bg-background p-1 text-xs text-destructive">{context.error}</p>}
  </div>;
}

"use client";

// Motion geometry from adopted v25 / 4827c33db5d8222b8fd55b2e547c801b0847b606.
import { useId, useLayoutEffect, useState, type CSSProperties, type RefObject } from "react";

export function LightParticle({ path, delay = 0, duration = 900, color = "#f586b0" }: {
  path: string; delay?: number; duration?: number; color?: string;
}) {
  return <g className="memory-light-particle" opacity="0" aria-hidden="true">
    <animate attributeName="opacity" values="0;1;1;0" keyTimes="0;.1;.86;1" dur={`${duration}ms`} begin={`${delay}ms`} fill="freeze" />
    <animateMotion path={path} dur={`${duration}ms`} begin={`${delay}ms`} calcMode="spline" keyTimes="0;1" keySplines=".33 0 .3 1" fill="freeze" />
    <circle r="20" fill={color} opacity=".18" /><circle r="11" fill={color} opacity=".6" /><circle r="5.5" fill="white" />
  </g>;
}

type Flight = { id: string; path: string; color: string };
type Clip = { left: number; top: number; right: number; bottom: number };
const intersect = (a: Clip, b: Clip): Clip => ({ left: Math.max(a.left, b.left), top: Math.max(a.top, b.top),
  right: Math.min(a.right, b.right), bottom: Math.min(a.bottom, b.bottom) });
const contains = (clip: Clip, x: number, y: number) => x >= clip.left && x <= clip.right && y >= clip.top && y <= clip.bottom;

/** Visible geometry includes each independent scroll/overflow clip, not just the viewport. */
function visibleRect(element: Element): Clip | null {
  const box = element.getBoundingClientRect();
  if (!box.width || !box.height) return null;
  let clip = intersect(box, { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight });
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    const style = getComputedStyle(parent);
    if (style.visibility === "hidden" || style.display === "none") return null;
    const clipsX = /^(auto|scroll|hidden|clip)$/.test(style.overflowX || style.overflow);
    const clipsY = /^(auto|scroll|hidden|clip)$/.test(style.overflowY || style.overflow);
    if (!clipsX && !clipsY) continue;
    const bounds = parent.getBoundingClientRect();
    const left = bounds.left + parent.clientLeft, top = bounds.top + parent.clientTop;
    clip = intersect(clip, { left: clipsX ? left : -Infinity, top: clipsY ? top : -Infinity,
      right: clipsX ? left + (parent.clientWidth || bounds.width) : Infinity,
      bottom: clipsY ? top + (parent.clientHeight || bounds.height) : Infinity });
  }
  return clip.right > clip.left && clip.bottom > clip.top ? clip : null;
}

export function SidebarFlights({ ids, root, generationKey }: {
  ids: string[]; root: RefObject<HTMLElement | null>; generationKey: string;
}) {
  const [geometry, setGeometry] = useState<{ flights: Flight[]; clip: Clip | null }>({ flights: [], clip: null });
  const clipId = `memory-flight-${useId()}`;
  const idsKey = JSON.stringify(ids); // Stable for the same originals, including tool reference keys.
  useLayoutEffect(() => {
    const host = root.current;
    if (!host) return;
    const measure = () => {
      // This surface ends before the mounted host composer. Clip the entire
      // curve/glow here as well as validating both real, visible endpoints.
      const clip = visibleRect(host.querySelector(".brain-layout") ?? host);
      const paths: Flight[] = [];
      if (!clip) { setGeometry({ flights: paths, clip }); return; }
      for (const [i, id] of (JSON.parse(idsKey) as string[]).entries()) {
        const anchor = Array.from(host.querySelectorAll("[data-flight-anchor]")).find(element => element.getAttribute("data-flight-anchor") === id);
        const card = Array.from(host.querySelectorAll("[data-thought-id]")).find(element => element.getAttribute("data-thought-id") === id);
        if (!anchor || !card) continue;
        const sourceClip = visibleRect(anchor), targetClip = visibleRect(card);
        if (!sourceClip || !targetClip) continue;
        const a = anchor.getBoundingClientRect(), b = card.getBoundingClientRect();
        const x = a.x + a.width / 2, y = a.y + a.height / 2;
        const tx = b.x + Math.min(28, b.width * .1), ty = b.y + b.height / 2;
        if (!contains(sourceClip, x, y) || !contains(targetClip, tx, ty) || !contains(clip, x, y) || !contains(clip, tx, ty)) continue;
        const bend = Math.max(45, Math.abs(tx - x) * .65);
        const controlX = Math.min(clip.right, x + bend), controlY = Math.max(clip.top, y - 45 - i * 14);
        const targetControlX = Math.max(clip.left, tx - bend * .4);
        paths.push({ id, path: `M${x} ${y} C${controlX} ${controlY},${targetControlX} ${ty},${tx} ${ty}`, color: i % 2 ? "#ae93e8" : "#f28caf" });
      }
      setGeometry({ flights: paths, clip });
    };
    measure();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    observer?.observe(host);
    host.querySelectorAll(".brain-map-surface,.brain-memories").forEach(element => observer?.observe(element));
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [idsKey, root, generationKey]);
  const { flights, clip } = geometry;
  return <svg className="memory-flight-overlay" aria-hidden="true">
    <defs><clipPath id={clipId}><rect x={clip?.left ?? 0} y={clip?.top ?? 0} width={clip ? clip.right - clip.left : 0} height={clip ? clip.bottom - clip.top : 0} /></clipPath></defs>
    <g clipPath={`url(#${clipId})`}>{flights.map((flight, i) => <g key={flight.id} data-flight-id={flight.id}>
    <path className="memory-flight-trail" d={flight.path} pathLength="1" style={{ "--flight-delay": `${i * 140}ms` } as CSSProperties} />
    <LightParticle path={flight.path} delay={i * 140} duration={830} color={flight.color} />
  </g>)}</g></svg>;
}

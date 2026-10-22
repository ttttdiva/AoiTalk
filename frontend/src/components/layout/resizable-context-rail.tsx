"use client";

import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";
import { X } from "lucide-react";

export const CONTEXT_RAIL_WIDTH_KEY = "aoitalk:context-rail-width";
export const DEFAULT_CONTEXT_RAIL_WIDTH = 300;

/** Keep a usable chat column even when navigation is expanded at tablet widths. */
export function contextRailBounds(available: number) {
  const space = Math.max(0, available);
  const max = Math.floor(Math.min(800, space - Math.min(360, space / 2)));
  return { min: Math.min(240, max), max };
}

function clamp(width: number, bounds: ReturnType<typeof contextRailBounds>) {
  return Math.min(bounds.max, Math.max(bounds.min, Math.round(width)));
}

export function ResizableContextRail({
  children, mainRef, isMobile, persistent, onClose, hidden = false, temporary = false,
}: {
  children: ReactNode;
  mainRef: RefObject<HTMLElement | null>;
  isMobile: boolean;
  persistent: boolean;
  onClose: () => void;
  hidden?: boolean;
  temporary?: boolean;
}) {
  const id = useId();
  const [preferredWidth, setPreferredWidth] = useState(DEFAULT_CONTEXT_RAIL_WIDTH);
  const [temporaryWidth, setTemporaryWidth] = useState<number | null>(null);
  const [bounds, setBounds] = useState({ min: 240, max: 800 });
  const [resizing, setResizing] = useState(false);
  const preference = useRef(DEFAULT_CONTEXT_RAIL_WIDTH);
  const limits = useRef(bounds);
  const cancelDrag = useRef<((update?: boolean) => void) | null>(null);
  const width = clamp(temporary ? temporaryWidth ?? preferredWidth : preferredWidth, bounds);
  const updateWidth = temporary ? setTemporaryWidth : setPreferredWidth;
  useEffect(() => { cancelDrag.current?.(); }, [temporary]);
  useEffect(() => { if (hidden) cancelDrag.current?.(); }, [hidden]);

  useEffect(() => {
    if (isMobile) return;
    const main = mainRef.current;
    const container = main?.parentElement;
    if (!main || !container) return;
    const measure = () => {
      // Measure the space shared by main + rail; navigation may be animating.
      const available = container.getBoundingClientRect().right - main.getBoundingClientRect().left;
      if (available <= 0) return;
      const next = contextRailBounds(available);
      limits.current = next;
      setBounds(previous => previous.min === next.min && previous.max === next.max ? previous : next);
    };
    const restore = () => {
      setResizing(false);
      setPreferredWidth(preference.current);
      try {
        const stored = Number(window.localStorage.getItem(CONTEXT_RAIL_WIDTH_KEY));
        if (Number.isFinite(stored) && stored >= 240 && stored <= 800) {
          preference.current = stored;
          setPreferredWidth(stored);
        }
      } catch { /* Storage can be disabled; resizing still works. */ }
      measure();
    };
    restore();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(container);
    observer?.observe(main);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
      cancelDrag.current?.(false);
    };
  }, [isMobile, mainRef]);

  const commit = (next: number) => {
    if (temporary) { setTemporaryWidth(next); return; }
    preference.current = next;
    setPreferredWidth(next);
    // Narrow viewports can require less than the normal minimum. Keep the
    // stored preference valid so expanding the viewport can restore it.
    try {
      window.localStorage.setItem(CONTEXT_RAIL_WIDTH_KEY, String(Math.max(240, next)));
    } catch { /* Private browsing or a full quota must not break the handle. */ }
  };

  return (
    <aside
      className="ao-context-rail shrink-0 overflow-hidden border-l"
      aria-label={persistent ? "チャット情報レール" : "Context Rail"}
      data-shell-region="context-rail"
      data-context-rail-persistent={persistent ? "true" : "false"}
      data-resizing={resizing ? "true" : "false"}
      hidden={hidden}
      inert={hidden}
      style={{ ...(isMobile ? {} : { "--ao-context-rail-width": `${width}px` }), ...(hidden ? { display: "none" } : {}) } as CSSProperties}
    >
      {!isMobile && (
        <div
          role="separator"
          tabIndex={0}
          aria-label="Context Railの幅"
          aria-orientation="vertical"
          aria-controls={id}
          aria-valuemin={bounds.min}
          aria-valuemax={bounds.max}
          aria-valuenow={width}
          aria-valuetext={`${width}px`}
          title="左右にドラッグで幅を変更。← →キーでも調整、Enterまたはダブルクリックで標準幅"
          className="ao-context-rail-resizer"
          onDoubleClick={() => { cancelDrag.current?.(); commit(clamp(DEFAULT_CONTEXT_RAIL_WIDTH, limits.current)); }}
          onKeyDown={event => {
            if (event.key === "Escape" && cancelDrag.current) {
              event.preventDefault();
              event.stopPropagation();
              cancelDrag.current();
              return;
            }
            if (!["ArrowLeft", "ArrowRight", "Home", "End", "Enter"].includes(event.key)) return;
            event.preventDefault();
            cancelDrag.current?.();
            const step = event.shiftKey ? 40 : 16;
            const next = event.key === "Home" ? limits.current.min
              : event.key === "End" ? limits.current.max
              : event.key === "Enter" ? DEFAULT_CONTEXT_RAIL_WIDTH
              : width + (event.key === "ArrowLeft" ? step : -step);
            commit(clamp(next, limits.current));
          }}
          onPointerDown={event => {
            if (event.button !== 0 || event.isPrimary === false || cancelDrag.current) return;
            event.preventDefault();
            const handle = event.currentTarget;
            const pointerId = event.pointerId;
            const startX = event.clientX;
            const startWidth = width;
            const previousPreference = preference.current;
            let currentWidth = width;
            const previousCursor = document.body.style.cursor;
            const previousSelection = document.body.style.userSelect;
            handle.focus({ preventScroll: true });
            document.body.style.cursor = "col-resize";
            document.body.style.userSelect = "none";
            setResizing(true);
            const move = (next: PointerEvent) => {
              if (next.pointerId !== pointerId) return;
              currentWidth = clamp(startWidth + startX - next.clientX, limits.current);
              updateWidth(currentWidth);
            };
            const finish = (save: boolean, update = true) => {
              cancelDrag.current = null;
              window.removeEventListener("pointermove", move);
              window.removeEventListener("pointerup", up);
              window.removeEventListener("pointercancel", cancel);
              window.removeEventListener("blur", cancel);
              window.removeEventListener("keydown", escape);
              document.removeEventListener("visibilitychange", visibility);
              handle.removeEventListener("lostpointercapture", cancel);
              try {
                if (handle.hasPointerCapture?.(pointerId)) handle.releasePointerCapture(pointerId);
              } catch { /* Capture may already have been released by the browser. */ }
              document.body.style.cursor = previousCursor;
              document.body.style.userSelect = previousSelection;
              if (!update) return;
              setResizing(false);
              if (save) commit(currentWidth);
              else updateWidth(previousPreference);
            };
            const up = (next: PointerEvent) => {
              if (next.pointerId !== pointerId) return;
              move(next);
              finish(true);
            };
            const cancel = (next?: Event) => {
              if (next && "pointerId" in next && next.pointerId !== pointerId) return;
              finish(false);
            };
            const escape = (next: KeyboardEvent) => {
              if (next.key !== "Escape") return;
              next.preventDefault();
              next.stopPropagation();
              finish(false);
            };
            const visibility = () => { if (document.hidden) finish(false); };
            cancelDrag.current = (update = true) => finish(false, update);
            window.addEventListener("pointermove", move);
            window.addEventListener("pointerup", up);
            window.addEventListener("pointercancel", cancel);
            window.addEventListener("blur", cancel);
            window.addEventListener("keydown", escape);
            document.addEventListener("visibilitychange", visibility);
            handle.addEventListener("lostpointercapture", cancel);
            try { handle.setPointerCapture?.(pointerId); } catch { /* Window listeners also handle uncaptured pointers. */ }
          }}
        />
      )}
      {!persistent && (
        <button type="button" className="ao-context-rail-close" aria-label="Context Railを閉じる" title="Context Railを閉じる" onClick={onClose}>
          <X className="size-4" />
        </button>
      )}
      <div id={id} className="ao-context-rail-content h-full min-h-0 overflow-auto">{children}</div>
    </aside>
  );
}

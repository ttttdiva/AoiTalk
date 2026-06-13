"use client";

import { useLayoutEffect, useMemo, useRef, useState } from "react";

type MenuPoint = {
  x: number;
  y: number;
};

type MenuSize = {
  width: number;
  height: number;
};

export type SubmenuSide = "left" | "right";

const VIEWPORT_MARGIN = 8;
const DEFAULT_SUBMENU_WIDTH = 168;

export function clampContextMenuToViewport(
  point: MenuPoint,
  size: MenuSize,
  margin = VIEWPORT_MARGIN,
) {
  if (typeof window === "undefined") {
    return { left: point.x, top: point.y };
  }

  const maxLeft = Math.max(margin, window.innerWidth - size.width - margin);
  const maxTop = Math.max(margin, window.innerHeight - size.height - margin);

  return {
    left: Math.min(Math.max(point.x, margin), maxLeft),
    top: Math.min(Math.max(point.y, margin), maxTop),
  };
}

export function useContextMenuPosition(
  point: MenuPoint | null,
  options?: {
    fallbackWidth?: number;
    fallbackHeight?: number;
    submenuWidth?: number;
  },
) {
  const ref = useRef<HTMLDivElement>(null);
  const fallbackWidth = options?.fallbackWidth ?? 200;
  const fallbackHeight = options?.fallbackHeight ?? 260;
  const submenuWidth = options?.submenuWidth ?? DEFAULT_SUBMENU_WIDTH;
  const [position, setPosition] = useState(() =>
    point
      ? clampContextMenuToViewport(point, {
          width: fallbackWidth,
          height: fallbackHeight,
        })
      : { left: 0, top: 0 },
  );
  const [submenuSide, setSubmenuSide] = useState<SubmenuSide>("right");

  useLayoutEffect(() => {
    if (!point) return;

    const measure = () => {
      const rect = ref.current?.getBoundingClientRect();
      const size = {
        width: rect?.width || fallbackWidth,
        height: rect?.height || fallbackHeight,
      };
      const nextPosition = clampContextMenuToViewport(point, size);
      setPosition(nextPosition);
      setSubmenuSide(
        nextPosition.left + size.width + submenuWidth + VIEWPORT_MARGIN >
          window.innerWidth
          ? "left"
          : "right",
      );
    };

    const frame = window.requestAnimationFrame(measure);
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [fallbackHeight, fallbackWidth, point, submenuWidth]);

  const style = useMemo(
    () => ({ left: position.left, top: position.top }),
    [position.left, position.top],
  );

  return { ref, style, submenuSide };
}

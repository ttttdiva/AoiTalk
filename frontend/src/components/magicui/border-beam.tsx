"use client";

import {
  motion,
  useReducedMotion,
  type MotionStyle,
  type Transition,
} from "motion/react";
import type { CSSProperties } from "react";

import { cn } from "@/lib/utils";

// Upstream: Magic UI Border Beam (adapted for AoiTalk tokens).

export interface BorderBeamProps {
  /** Size of the travelling beam in pixels. */
  size?: number;
  /** Duration of one border traversal in seconds. */
  duration?: number;
  /** Delay before the traversal starts in seconds. */
  delay?: number;
  /** Start color for the beam gradient. */
  colorFrom?: string;
  /** End color for the beam gradient. */
  colorTo?: string;
  /** Optional Motion transition override. */
  transition?: Transition;
  /** Additional classes for the beam itself. */
  className?: string;
  /** Additional styles for the beam itself. */
  style?: CSSProperties;
  /** Travel in the opposite direction. */
  reverse?: boolean;
  /** Initial offset along the border, from 0 to 100. */
  initialOffset?: number;
  /** Width of the border beam in pixels. */
  borderWidth?: number;
}

export function BorderBeam({
  className,
  size = 50,
  delay = 0,
  duration = 6,
  colorFrom = "var(--primary)",
  colorTo = "var(--chart-2)",
  transition,
  style,
  reverse = false,
  initialOffset = 0,
  borderWidth = 1,
}: BorderBeamProps) {
  const reducedMotion = useReducedMotion() === true;

  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 rounded-[inherit] border-(length:--border-beam-width) border-transparent mask-[linear-gradient(transparent,transparent),linear-gradient(#000,#000)] mask-intersect [mask-clip:padding-box,border-box]"
      style={{ "--border-beam-width": `${borderWidth}px` } as CSSProperties}
    >
      <motion.div
        className={cn(
          "absolute aspect-square",
          "bg-linear-to-l from-(--color-from) via-(--color-to) to-transparent",
          className,
        )}
        style={
          {
            width: size,
            offsetPath: `rect(0 auto auto 0 round ${size}px)`,
            "--color-from": colorFrom,
            "--color-to": colorTo,
            ...style,
          } as MotionStyle
        }
        initial={{ offsetDistance: `${initialOffset}%` }}
        animate={
          reducedMotion
            ? { offsetDistance: `${initialOffset}%` }
            : {
                offsetDistance: reverse
                  ? [`${100 - initialOffset}%`, `${-initialOffset}%`]
                  : [`${initialOffset}%`, `${100 + initialOffset}%`],
              }
        }
        transition={
          reducedMotion
            ? { duration: 0 }
            : {
                repeat: Infinity,
                ease: "linear",
                duration,
                delay: -delay,
                ...transition,
              }
        }
      />
    </div>
  );
}

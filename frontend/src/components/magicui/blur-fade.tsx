"use client";

import { useRef, type ReactNode } from "react";
import {
  AnimatePresence,
  motion,
  useInView,
  useReducedMotion,
  type MotionProps,
  type UseInViewOptions,
  type Variants,
} from "motion/react";

// Upstream: Magic UI Blur Fade (adapted for AoiTalk motion/accessibility rules).

type MarginType = UseInViewOptions["margin"];

export interface BlurFadeProps extends MotionProps {
  children: ReactNode;
  className?: string;
  variant?: Variants;
  duration?: number;
  delay?: number;
  offset?: number;
  direction?: "up" | "down" | "left" | "right";
  inView?: boolean;
  inViewMargin?: MarginType;
  blur?: string;
}

const getFilter = (value: Variants[string]) =>
  typeof value === "function" ? undefined : value.filter;

export function BlurFade({
  children,
  className,
  variant,
  duration = 0.4,
  delay = 0,
  offset = 6,
  direction = "down",
  inView = false,
  inViewMargin = "-50px",
  blur = "6px",
  ...props
}: BlurFadeProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const inViewResult = useInView(ref, { once: true, margin: inViewMargin });
  const reducedMotion = useReducedMotion() === true;
  const isInView = reducedMotion || !inView || inViewResult;
  const axis = direction === "left" || direction === "right" ? "x" : "y";
  const initialOffset =
    direction === "right" || direction === "down" ? -offset : offset;
  const defaultVariants: Variants = {
    hidden: {
      [axis]: initialOffset,
      opacity: 0,
      filter: `blur(${blur})`,
    },
    visible: {
      [axis]: 0,
      opacity: 1,
      filter: "blur(0px)",
    },
  };
  const combinedVariants = variant ?? defaultVariants;
  const hiddenFilter = getFilter(combinedVariants.hidden);
  const visibleFilter = getFilter(combinedVariants.visible);
  const shouldTransitionFilter =
    hiddenFilter != null &&
    visibleFilter != null &&
    hiddenFilter !== visibleFilter;

  return (
    <AnimatePresence>
      <motion.div
        ref={ref}
        initial={reducedMotion ? "visible" : "hidden"}
        animate={isInView ? "visible" : "hidden"}
        exit="hidden"
        variants={combinedVariants}
        transition={
          reducedMotion
            ? { duration: 0 }
            : {
                delay: 0.04 + delay,
                duration,
                ease: "easeOut",
                ...(shouldTransitionFilter ? { filter: { duration } } : {}),
              }
        }
        className={className}
        {...props}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}

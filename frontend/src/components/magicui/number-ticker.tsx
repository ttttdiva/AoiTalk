"use client";

import { useEffect, useRef, type ComponentPropsWithoutRef } from "react";
import {
  useInView,
  useMotionValue,
  useReducedMotion,
  useSpring,
} from "motion/react";

import { cn } from "@/lib/utils";

// Upstream: Magic UI Number Ticker (adapted for AoiTalk accessibility).

export interface NumberTickerProps extends ComponentPropsWithoutRef<"span"> {
  value: number;
  startValue?: number;
  direction?: "up" | "down";
  delay?: number;
  decimalPlaces?: number;
}

const formatNumber = (value: number, decimalPlaces: number) =>
  Intl.NumberFormat("en-US", {
    minimumFractionDigits: decimalPlaces,
    maximumFractionDigits: decimalPlaces,
  }).format(Number(value.toFixed(decimalPlaces)));

export function NumberTicker({
  value,
  startValue = 0,
  direction = "up",
  delay = 0,
  className,
  decimalPlaces = 0,
  "aria-label": ariaLabel,
  ...props
}: NumberTickerProps) {
  const visualRef = useRef<HTMLSpanElement>(null);
  const motionValue = useMotionValue(direction === "down" ? value : startValue);
  const springValue = useSpring(motionValue, {
    damping: 60,
    stiffness: 100,
  });
  const isInView = useInView(visualRef, { once: true, margin: "0px" });
  const reducedMotion = useReducedMotion() === true;
  const decimals = Math.max(0, Math.min(20, Math.floor(decimalPlaces)));
  const targetValue = direction === "down" ? startValue : value;
  const initialValue = direction === "down" ? value : startValue;
  const formattedTarget = formatNumber(targetValue, decimals);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;

    if (reducedMotion) {
      springValue.stop();
      if (visualRef.current) {
        visualRef.current.textContent = formattedTarget;
      }
    } else if (isInView) {
      timer = setTimeout(
        () => {
          motionValue.set(targetValue);
        },
        Math.max(0, delay) * 1000,
      );
    }

    return () => {
      if (timer !== null) clearTimeout(timer);
    };
  }, [
    delay,
    formattedTarget,
    isInView,
    motionValue,
    reducedMotion,
    springValue,
    targetValue,
  ]);

  useEffect(
    () =>
      springValue.on("change", (latest) => {
        if (visualRef.current && !reducedMotion) {
          visualRef.current.textContent = formatNumber(
            Number(latest),
            decimals,
          );
        }
      }),
    [decimals, reducedMotion, springValue],
  );

  const accessibleLabel = ariaLabel ?? formattedTarget;

  return (
    <span
      className={cn(
        "inline-block tracking-wider tabular-nums text-foreground",
        className,
      )}
      aria-label={accessibleLabel}
      {...props}
    >
      <span ref={visualRef} aria-hidden="true">
        {formatNumber(initialValue, decimals)}
      </span>
      <span className="sr-only">{accessibleLabel}</span>
    </span>
  );
}

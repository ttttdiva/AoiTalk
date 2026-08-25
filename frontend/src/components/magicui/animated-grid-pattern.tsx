"use client";

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
} from "react";
import { motion, useReducedMotion } from "motion/react";

import { cn } from "@/lib/utils";

// Upstream: Magic UI Animated Grid Pattern (adapted for AoiTalk tokens).

export interface AnimatedGridPatternProps extends ComponentPropsWithoutRef<"svg"> {
  width?: number;
  height?: number;
  x?: number;
  y?: number;
  strokeDasharray?: number;
  numSquares?: number;
  maxOpacity?: number;
  duration?: number;
  repeatDelay?: number;
}

type Square = {
  id: number;
  pos: [number, number];
  iteration: number;
};

const MAX_SQUARES = 30;

export function AnimatedGridPattern({
  width = 40,
  height = 40,
  x = -1,
  y = -1,
  strokeDasharray = 0,
  numSquares = 24,
  className,
  maxOpacity = 0.1,
  duration = 4,
  repeatDelay = 1,
  ...props
}: AnimatedGridPatternProps) {
  const id = useId();
  const patternId = `animated-grid-${id.replace(/:/g, "")}`;
  const containerRef = useRef<SVGSVGElement | null>(null);
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });
  const [squares, setSquares] = useState<Array<Square>>([]);
  const reducedMotion = useReducedMotion();
  const squareCount = Math.min(
    MAX_SQUARES,
    Math.max(0, Math.floor(numSquares)),
  );

  const getPos = useCallback((): [number, number] => {
    const columns = Math.max(1, Math.floor(dimensions.width / width));
    const rows = Math.max(1, Math.floor(dimensions.height / height));
    return [
      Math.floor(Math.random() * columns),
      Math.floor(Math.random() * rows),
    ];
  }, [dimensions.height, dimensions.width, height, width]);

  const generateSquares = useCallback(
    (count: number) =>
      Array.from({ length: count }, (_, index) => ({
        id: index,
        pos: getPos(),
        iteration: 0,
      })),
    [getPos],
  );

  const updateSquarePosition = useCallback(
    (squareId: number) => {
      setSquares((currentSquares) => {
        const current = currentSquares[squareId];
        if (!current || current.id !== squareId) return currentSquares;

        const nextSquares = currentSquares.slice();
        nextSquares[squareId] = {
          ...current,
          pos: getPos(),
          iteration: current.iteration + 1,
        };
        return nextSquares;
      });
    },
    [getPos],
  );

  useEffect(() => {
    if (dimensions.width > 0 && dimensions.height > 0) {
      const task = window.setTimeout(() => {
        setSquares(generateSquares(squareCount));
      }, 0);
      return () => window.clearTimeout(task);
    }
  }, [dimensions.height, dimensions.width, generateSquares, squareCount]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;

    const updateDimensions = (nextWidth: number, nextHeight: number) => {
      setDimensions((current) =>
        current.width === nextWidth && current.height === nextHeight
          ? current
          : { width: nextWidth, height: nextHeight },
      );
    };

    if (typeof ResizeObserver === "undefined") {
      const rect = element.getBoundingClientRect();
      updateDimensions(rect.width, rect.height);
      return;
    }

    const resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) {
        updateDimensions(entry.contentRect.width, entry.contentRect.height);
      }
    });
    resizeObserver.observe(element);

    return () => resizeObserver.disconnect();
  }, []);

  return (
    <svg
      ref={containerRef}
      aria-hidden="true"
      className={cn(
        "pointer-events-none absolute inset-0 h-full w-full text-primary/30",
        className,
      )}
      {...props}
    >
      <defs>
        <pattern
          id={patternId}
          width={width}
          height={height}
          patternUnits="userSpaceOnUse"
          x={x}
          y={y}
        >
          <path
            d={`M.5 ${height}V.5H${width}`}
            fill="none"
            stroke="currentColor"
            strokeDasharray={strokeDasharray}
          />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${patternId})`} />
      <svg x={x} y={y} className="overflow-visible">
        {squares.map(
          ({ pos: [squareX, squareY], id: squareId, iteration }, index) => (
            <motion.rect
              key={`${squareId}-${iteration}`}
              initial={{ opacity: reducedMotion ? maxOpacity : 0 }}
              animate={{ opacity: maxOpacity }}
              transition={
                reducedMotion
                  ? { duration: 0 }
                  : {
                      duration,
                      repeat: 1,
                      delay: index * 0.1,
                      repeatType: "reverse",
                      repeatDelay,
                    }
              }
              onAnimationComplete={
                reducedMotion ? undefined : () => updateSquarePosition(squareId)
              }
              width={Math.max(0, width - 1)}
              height={Math.max(0, height - 1)}
              x={squareX * width + 1}
              y={squareY * height + 1}
              fill="currentColor"
              strokeWidth="0"
            />
          ),
        )}
      </svg>
    </svg>
  );
}

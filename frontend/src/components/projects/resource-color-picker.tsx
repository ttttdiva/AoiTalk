"use client";

import type { CSSProperties } from "react";

import { cn } from "@/lib/utils";

/**
 * The shared palette used by Projects and the Tasks workspace tree.
 *
 * Keep this list deliberately small: it is rendered in compact popovers in
 * Tasks, while the native color input still provides the custom-color path.
 */
export const PROJECT_COLOR_PRESETS = [
  { name: "Crystal Cyan", value: "#0E7490" },
  { name: "Lagoon Teal", value: "#0F766E" },
  { name: "Emerald", value: "#047857" },
  { name: "Cobalt Blue", value: "#2563EB" },
  { name: "Lapis Indigo", value: "#4F46E5" },
  { name: "Aurora Violet", value: "#7C3AED" },
  { name: "Fuchsia", value: "#C026D3" },
  { name: "Rose", value: "#DB2777" },
  { name: "Crimson", value: "#BE123C" },
  { name: "Coral", value: "#C2410C" },
  { name: "Amber Brown", value: "#A16207" },
  { name: "Slate", value: "#475569" },
] as const;

export type ResourceColorPickerProps = {
  value: string;
  onChange: (value: string) => void;
  inputClassName?: string;
  /** Use a shorter shell when the picker is hosted in a tree popover. */
  compact?: boolean;
  /** Let a surrounding form provide its own localized label. */
  showLabel?: boolean;
};

/** A neutral, explicit-color-only marker shared by selectors and tree rows. */
export function ResourceColorDot({
  color,
  className,
  style,
}: {
  color?: string | null;
  className?: string;
  style?: CSSProperties;
}) {
  const normalized = typeof color === "string" ? color.trim() : "";
  if (!normalized) return null;

  return (
    <span
      aria-hidden="true"
      className={cn(
        "inline-block size-2 shrink-0 rounded-full ring-1 ring-black/10 dark:ring-white/20",
        className,
      )}
      style={{ ...style, backgroundColor: normalized }}
    />
  );
}

/**
 * Shared palette/custom color input for Project and Space metadata.
 * `value` is intentionally controlled by the host so a host can keep an
 * explicit null/unset value neutral while editing uses a valid input fallback.
 */
export function ResourceColorPicker({
  value,
  onChange,
  inputClassName = "h-8",
  compact = false,
  showLabel = true,
}: ResourceColorPickerProps) {
  const currentColor = value || "#3b82f6";
  const selectedColor = currentColor.toLowerCase();

  return (
    <div
      className={cn(
        "space-y-2 rounded border border-input px-2 py-1.5",
        compact && "space-y-1 border-0 px-0 py-0",
      )}
    >
      <div className="flex items-center gap-2">
        {showLabel ? (
          <span className="text-xs text-muted-foreground">色</span>
        ) : null}
        <input
          type="color"
          value={currentColor}
          onChange={(event) => onChange(event.target.value)}
          className={`${inputClassName} w-10 cursor-pointer rounded border-0 bg-transparent p-0`}
          aria-label="カスタム色"
        />
        <span className="text-[11px] text-muted-foreground">
          {currentColor}
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {PROJECT_COLOR_PRESETS.map((preset) => {
          const isSelected = preset.value.toLowerCase() === selectedColor;

          return (
            <button
              key={preset.value}
              type="button"
              title={`${preset.name} ${preset.value}`}
              aria-label={`${preset.name} ${preset.value}`}
              aria-pressed={isSelected}
              onClick={() => onChange(preset.value)}
              className={cn(
                "size-6 rounded-full border border-white/70 shadow-sm transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                isSelected &&
                  "ring-2 ring-ring ring-offset-2 ring-offset-background",
              )}
              style={{ backgroundColor: preset.value }}
            />
          );
        })}
      </div>
    </div>
  );
}

/** Backwards-compatible name for existing Projects page imports. */
export const ProjectColorPicker = ResourceColorPicker;

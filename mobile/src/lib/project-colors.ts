import type { Project } from "../types/api";

export const DEFAULT_PROJECT_COLOR = "#3b82f6";

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

export function isValidProjectColor(value?: string | null): boolean {
  return Boolean(value && /^#[0-9a-f]{6}$/i.test(value.trim()));
}

export function normalizeProjectColor(value?: string | null): string {
  return isValidProjectColor(value) ? value!.trim() : DEFAULT_PROJECT_COLOR;
}

export function getProjectColor(project?: Project | null): string {
  const metadataColor =
    project?.metadata && typeof project.metadata.color === "string"
      ? project.metadata.color
      : null;
  return normalizeProjectColor(project?.color ?? metadataColor);
}

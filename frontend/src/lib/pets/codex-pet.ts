/** Codex v2 atlas. Timings/layout match the supplied Aoi package's preview.html. */
export const ATLAS = { width: 1536, height: 2288, columns: 8, cellWidth: 192, cellHeight: 208 } as const;
export const MOTIONS = {
  idle: { row: 0, label: "待機", durations: [1680, 660, 660, 840, 840, 1920] },
  "running-right": { row: 1, label: "右へ走る", durations: [120, 120, 120, 120, 120, 120, 120, 220] },
  "running-left": { row: 2, label: "左へ走る", durations: [120, 120, 120, 120, 120, 120, 120, 220] },
  waving: { row: 3, label: "手を振る", durations: [140, 140, 140, 280] },
  jumping: { row: 4, label: "ジャンプ", durations: [140, 140, 140, 140, 280] },
  failed: { row: 5, label: "失敗", durations: [140, 140, 140, 140, 140, 140, 140, 240] },
  waiting: { row: 6, label: "返答待ち", durations: [150, 150, 150, 150, 150, 260] },
  running: { row: 7, label: "作業中", durations: [120, 120, 120, 120, 120, 220] },
  review: { row: 8, label: "レビュー", durations: [150, 150, 150, 150, 150, 280] },
  look: { row: 9, label: "視線", durations: Array<number>(16).fill(260) },
} as const;
export type PetMotion = keyof typeof MOTIONS;
export type PetManifest = {
  displayName: string;
  description: string;
  spriteVersionNumber: 2;
  spritesheetPath: string;
};
export type PetPreferences = {
  enabled: boolean;
  size: number;
  x: number;
  y: number;
  followPointer: boolean;
  reduceMotion: boolean;
};
export type StoredPet = { version: 1; manifest: PetManifest; image: Blob; preferences: PetPreferences };
export const DEFAULT_PREFERENCES: PetPreferences = {
  enabled: true, size: 128, x: 0.94, y: 0.82, followPointer: true, reduceMotion: false,
};
export function clamp(value: number, min: number, max: number): number {
  return Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : min;
}
export function normalizePreferences(value: Partial<PetPreferences> = {}): PetPreferences {
  return {
    enabled: typeof value.enabled === "boolean" ? value.enabled : true,
    size: clamp(value.size ?? 128, 64, 192),
    x: clamp(value.x ?? 0.94, 0, 1), y: clamp(value.y ?? 0.82, 0, 1),
    followPointer: typeof value.followPointer === "boolean" ? value.followPointer : true,
    reduceMotion: typeof value.reduceMotion === "boolean" ? value.reduceMotion : false,
  };
}
export function safeRelativePath(value: string): string {
  const path = value.replaceAll("\\", "/");
  if (!path || path.startsWith("/") || /[:\x00-\x1f\x7f]/.test(path) || path.split("/").some((p) => !p || p === "." || p === "..")) {
    throw new Error("ペット内のファイル名が不正です。相対パスを使用してください。");
  }
  return path;
}
export function parseManifest(text: string): PetManifest {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("pet.json が不正です。");
  const v = value as Record<string, unknown>;
  if (v.spriteVersionNumber !== 2) throw new Error("対応するペット形式は Codex v2（spriteVersionNumber: 2）です。");
  if (typeof v.displayName !== "string" || !v.displayName.trim() || v.displayName.length > 120) throw new Error("ペットの表示名は1〜120文字で指定してください。");
  if (typeof v.spritesheetPath !== "string") throw new Error("spritesheetPath がありません。");
  return {
    displayName: v.displayName.trim(),
    description: typeof v.description === "string" ? v.description.slice(0, 1000) : "",
    spriteVersionNumber: 2,
    spritesheetPath: safeRelativePath(v.spritesheetPath),
  };
}
export function frameCell(motion: PetMotion, frame: number) {
  const spec = MOTIONS[motion];
  const index = Math.floor(clamp(frame, 0, spec.durations.length - 1));
  return { x: (index % 8) * 192, y: (spec.row + Math.floor(index / 8)) * 208 };
}
export function frameAt(motion: PetMotion, elapsed: number): number {
  const durations = MOTIONS[motion].durations;
  let remaining = Math.max(0, elapsed) % durations.reduce((sum, n) => sum + n, 0);
  for (let i = 0; i < durations.length; i++) {
    if (remaining < durations[i]) return i;
    remaining -= durations[i];
  }
  return 0;
}
/** Aoi/Codex v2: north is 0, clockwise in screen coordinates. */
export function lookFrame(dx: number, dy: number): number {
  return ((Math.round(Math.atan2(dx, -dy) / (Math.PI / 8)) % 16) + 16) % 16;
}
export function motionForPhase(phase: string): PetMotion {
  switch (phase) {
    case "dispatching": case "queued": case "stopping": case "cancellation_pending": return "waiting";
    case "streaming": case "tool": return "running";
    case "completed": return "waving";
    case "failed": case "cancellation_failed": return "failed";
    default: return "idle";
  }
}

/**
 * Huggingface API エラーハンドリング・メディア判定ユーティリティ
 * 60_huggingface-sync/src/lib/api-utils.ts を移植
 */

export type MediaType = "image" | "video" | "audio" | "text" | "other";

const IMAGE_EXTENSIONS = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".bmp",
  ".avif",
  ".svg",
  ".ico",
  ".heic",
  ".heif",
  ".tiff",
  ".tif",
]);

const VIDEO_EXTENSIONS = new Set([
  ".mp4",
  ".webm",
  ".mov",
  ".mkv",
  ".avi",
  ".flv",
  ".wmv",
  ".m4v",
  ".3gp",
  ".ts",
]);

const AUDIO_EXTENSIONS = new Set([
  ".mp3",
  ".wav",
  ".ogg",
  ".flac",
  ".m4a",
  ".aac",
  ".opus",
  ".wma",
]);

const TEXT_EXTENSIONS = new Set([
  ".txt",
  ".md",
  ".json",
  ".jsonl",
  ".yaml",
  ".yml",
  ".toml",
  ".csv",
  ".tsv",
  ".py",
  ".ts",
  ".tsx",
  ".js",
  ".jsx",
  ".html",
  ".css",
  ".sh",
  ".bat",
  ".ps1",
  ".gitattributes",
  ".gitignore",
]);

export function errorToString(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

export function isRateLimitError(error: unknown): boolean {
  const msg = errorToString(error).toLowerCase();
  return (
    msg.includes("429") ||
    msg.includes("rate limit") ||
    msg.includes("rate-limited")
  );
}

export function isNetworkError(error: unknown): boolean {
  const msg = errorToString(error).toLowerCase();
  return (
    msg.includes("unable to resolve host") ||
    msg.includes("network request failed") ||
    msg.includes("enotfound") ||
    msg.includes("etimedout") ||
    msg.includes("econnrefused") ||
    msg.includes("econnreset") ||
    msg.includes("econnaborted") ||
    msg.includes("fetch failed")
  );
}

export function parseRateLimitWait(errorStr: string): number {
  const candidates: number[] = [];
  const sec = errorStr.match(/[Rr]etry after (\d+) seconds/);
  if (sec) candidates.push(parseInt(sec[1], 10) + 10);
  const min = errorStr.match(/(?:in|in about) (\d+) minute/i);
  if (min) candidates.push(parseInt(min[1], 10) * 60 + 60);
  const hour = errorStr.match(/(?:in|in about) (\d+) hour/i);
  if (hour) candidates.push(parseInt(hour[1], 10) * 3600 + 60);
  if (candidates.length > 0) return Math.max(...candidates);
  return 3600;
}

export function getExtension(path: string): string {
  const idx = path.lastIndexOf(".");
  if (idx === -1) return "";
  return path.slice(idx).toLowerCase();
}

export function getMediaType(path: string): MediaType {
  const ext = getExtension(path);
  if (IMAGE_EXTENSIONS.has(ext)) return "image";
  if (VIDEO_EXTENSIONS.has(ext)) return "video";
  if (AUDIO_EXTENSIONS.has(ext)) return "audio";
  if (TEXT_EXTENSIONS.has(ext)) return "text";
  return "other";
}

export function formatFileSize(bytes?: number | null): string {
  if (bytes == null) return "-";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const clamped = Math.min(i, units.length - 1);
  const size = bytes / Math.pow(1024, clamped);
  return `${size.toFixed(clamped > 0 ? 1 : 0)} ${units[clamped]}`;
}

import fs from "node:fs";
import path from "node:path";
import sharp from "sharp";
import { getWorkspacesBaseDir } from "@/lib/server/project-workspace-management";

/** プロフィールアイコンとして受け付ける最大バイト数。 */
export const MAX_AVATAR_BYTES = 5 * 1024 * 1024;

export const AVATAR_MIME_EXTENSIONS = {
  "image/jpeg": ".jpg",
  "image/png": ".png",
  "image/gif": ".gif",
  "image/webp": ".webp",
} as const;

export type AvatarMimeType = keyof typeof AVATAR_MIME_EXTENSIONS;

const SHARP_FORMAT_MIME: Record<string, AvatarMimeType> = {
  jpeg: "image/jpeg",
  png: "image/png",
  gif: "image/gif",
  webp: "image/webp",
};

export type AvatarImageMetadata = {
  mime: AvatarMimeType;
  width: number;
  height: number;
};

const AVATAR_RELATIVE_PREFIX = "_users";

function isStorageLink(stat: fs.Stats): boolean {
  if (stat.isSymbolicLink()) return true;
  const attributes = (stat as fs.Stats & { st_file_attributes?: number })
    .st_file_attributes;
  return typeof attributes === "number" && (attributes & 0x400) !== 0;
}

/**
 * Verify that an existing path component is a real directory/file, not a
 * symbolic link or a Windows junction/reparse point. Missing descendants are
 * allowed so this can be used before creating an upload directory.
 */
export function assertNoAvatarStorageLinks(
  storageRoot: string,
  candidate: string,
): void {
  const root = path.resolve(storageRoot);
  const target = path.resolve(candidate);
  const relative = path.relative(root, target);
  if (
    relative.startsWith("..") ||
    path.isAbsolute(relative)
  ) {
    throw new Error("avatar path escapes the user storage root");
  }

  let current = path.parse(root).root;
  const components = target
    .slice(current.length)
    .split(path.sep)
    .filter(Boolean);
  for (const component of components) {
    current = path.join(current, component);
    try {
      const stat = fs.lstatSync(current);
      if (isStorageLink(stat)) {
        throw new Error("avatar storage path contains a link");
      }
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "ENOENT" || code === "ENOTDIR") break;
      throw error;
    }
  }
}

export function getAvatarStorageRoot(userId: string): string {
  return path.resolve(
    /* turbopackIgnore: true */ getWorkspacesBaseDir(),
    AVATAR_RELATIVE_PREFIX,
    `user_${userId}`,
    "avatar",
  );
}

export function ensureAvatarStorageRoot(userId: string): string {
  const base = path.resolve(/* turbopackIgnore: true */ getWorkspacesBaseDir());
  const root = getAvatarStorageRoot(userId);
  assertNoAvatarStorageLinks(base, root);
  fs.mkdirSync(root, { recursive: true });
  assertNoAvatarStorageLinks(base, root);
  const stat = fs.lstatSync(root);
  if (!stat.isDirectory() || isStorageLink(stat)) {
    throw new Error("avatar storage root is not a real directory");
  }
  return root;
}

export function getAvatarRelativePath(userId: string, fileName: string): string {
  return `${AVATAR_RELATIVE_PREFIX}/user_${userId}/avatar/${fileName}`;
}

/**
 * Resolve a DB reference only when it remains inside this user's avatar root.
 * The DB value is intentionally treated as untrusted because an administrator
 * or an old database may contain a stale/invalid path.
 */
export function resolveStoredAvatarPath(
  userId: string,
  avatarPath: string | null | undefined,
): string | null {
  if (!avatarPath || avatarPath.includes("\0")) return null;
  const normalized = avatarPath.replace(/\\/g, "/");
  if (normalized.startsWith("/") || /^[A-Za-z]:\//.test(normalized)) {
    return null;
  }
  const base = path.resolve(/* turbopackIgnore: true */ getWorkspacesBaseDir());
  const userRoot = getAvatarStorageRoot(userId);
  const candidate = path.resolve(base, normalized.replace(/\//g, path.sep));
  const relative = path.relative(userRoot, candidate);
  if (
    !relative ||
    relative.startsWith("..") ||
    path.isAbsolute(relative) ||
    path.basename(candidate) !== relative
  ) {
    return null;
  }
  try {
    assertNoAvatarStorageLinks(base, candidate);
  } catch {
    return null;
  }
  return candidate;
}

/** Detect the actual image format from file bytes, independent of extension. */
export function detectAvatarMime(bytes: Uint8Array): AvatarMimeType | null {
  if (
    bytes.length >= 3 &&
    bytes[0] === 0xff &&
    bytes[1] === 0xd8 &&
    bytes[2] === 0xff
  ) {
    return "image/jpeg";
  }
  if (
    bytes.length >= 8 &&
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47 &&
    bytes[4] === 0x0d &&
    bytes[5] === 0x0a &&
    bytes[6] === 0x1a &&
    bytes[7] === 0x0a
  ) {
    return "image/png";
  }
  if (
    bytes.length >= 6 &&
    bytes[0] === 0x47 &&
    bytes[1] === 0x49 &&
    bytes[2] === 0x46 &&
    bytes[3] === 0x38 &&
    (bytes[4] === 0x37 || bytes[4] === 0x39) &&
    bytes[5] === 0x61
  ) {
    return "image/gif";
  }
  if (
    bytes.length >= 12 &&
    bytes[0] === 0x52 &&
    bytes[1] === 0x49 &&
    bytes[2] === 0x46 &&
    bytes[3] === 0x46 &&
    bytes[8] === 0x57 &&
    bytes[9] === 0x45 &&
    bytes[10] === 0x42 &&
    bytes[11] === 0x50
  ) {
    return "image/webp";
  }
  return null;
}

/**
 * Validate both the container signature and the decoded image metadata.
 * Magic bytes alone are insufficient because a truncated JPEG can still start
 * with ``FF D8 FF``.  Sharp rejects malformed/truncated payloads and exposes
 * the actual format and dimensions from the decoded image header.
 */
export async function validateAvatarImage(
  bytes: Uint8Array,
  declaredMime: string,
): Promise<AvatarImageMetadata> {
  const detectedMime = detectAvatarMime(bytes);
  if (!detectedMime || detectedMime !== declaredMime) {
    throw new Error("avatar MIME does not match the file signature");
  }

  let metadata: sharp.Metadata;
  try {
    metadata = await sharp(Buffer.from(bytes), { failOn: "error" }).metadata();
  } catch (error) {
    throw new Error("avatar image could not be decoded", { cause: error });
  }
  const decodedMime = metadata.format ? SHARP_FORMAT_MIME[metadata.format] : undefined;
  if (
    !decodedMime ||
    decodedMime !== declaredMime ||
    !Number.isInteger(metadata.width) ||
    !Number.isInteger(metadata.height) ||
    metadata.width <= 0 ||
    metadata.height <= 0
  ) {
    throw new Error("avatar image metadata is invalid");
  }
  return {
    mime: decodedMime,
    width: metadata.width,
    height: metadata.height,
  };
}

export function avatarUrl(
  userId: string,
  avatarPath: string | null | undefined,
): string | null {
  if (!avatarPath) return null;
  const fileName = path.basename(avatarPath.replace(/\\/g, "/"));
  if (!fileName || fileName === "." || fileName === "..") return null;
  const version = encodeURIComponent(fileName);
  return `/api/users/${encodeURIComponent(userId)}/avatar?v=${version}`;
}

export function removeAvatarFile(
  userId: string,
  avatarPath: string | null | undefined,
): void {
  const resolved = resolveStoredAvatarPath(userId, avatarPath);
  if (!resolved) return;
  try {
    fs.unlinkSync(resolved);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== "ENOENT") throw error;
  }
}

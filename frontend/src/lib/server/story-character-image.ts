import fs from "node:fs";
import path from "node:path";
import { assertNoAvatarStorageLinks } from "@/lib/server/user-avatar";
import { getWorkspacesBaseDir } from "@/lib/server/project-workspace-management";

export {
  AVATAR_MIME_EXTENSIONS,
  MAX_AVATAR_BYTES,
  detectAvatarMime,
  validateAvatarImage,
  type AvatarMimeType,
} from "@/lib/server/user-avatar";

const STORAGE_PREFIX = "_users";

export function getStoryCharacterImageStorageRoot(userId: string, characterId: string): string {
  return path.resolve(
    /* turbopackIgnore: true */ getWorkspacesBaseDir(),
    STORAGE_PREFIX,
    `user_${userId}`,
    "story_characters",
    `char_${characterId}`,
  );
}

export function ensureStoryCharacterImageStorageRoot(userId: string, characterId: string): string {
  const base = path.resolve(/* turbopackIgnore: true */ getWorkspacesBaseDir());
  const root = getStoryCharacterImageStorageRoot(userId, characterId);
  assertNoAvatarStorageLinks(base, root);
  fs.mkdirSync(root, { recursive: true });
  assertNoAvatarStorageLinks(base, root);
  return root;
}

export function getStoryCharacterImageRelativePath(
  userId: string,
  characterId: string,
  fileName: string,
): string {
  return `${STORAGE_PREFIX}/user_${userId}/story_characters/char_${characterId}/${fileName}`;
}

export function resolveStoredStoryCharacterImagePath(
  userId: string,
  characterId: string,
  imagePath: string | null | undefined,
): string | null {
  if (!imagePath || imagePath.includes("\0")) return null;
  const normalized = imagePath.replace(/\\/g, "/");
  if (normalized.startsWith("/") || /^[A-Za-z]:\//.test(normalized)) {
    return null;
  }
  const base = path.resolve(/* turbopackIgnore: true */ getWorkspacesBaseDir());
  const characterRoot = getStoryCharacterImageStorageRoot(userId, characterId);
  const candidate = path.resolve(base, normalized.replace(/\//g, path.sep));
  const relative = path.relative(characterRoot, candidate);
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

export function storyCharacterImageUrl(
  characterId: string,
  imagePath: string | null | undefined,
): string | null {
  if (!imagePath) return null;
  const fileName = path.basename(imagePath.replace(/\\/g, "/"));
  if (!fileName || fileName === "." || fileName === "..") return null;
  const version = encodeURIComponent(fileName);
  return `/api/story/characters/${encodeURIComponent(characterId)}/image?v=${version}`;
}

export function removeStoryCharacterImageFile(
  userId: string,
  characterId: string,
  imagePath: string | null | undefined,
): void {
  const resolved = resolveStoredStoryCharacterImagePath(userId, characterId, imagePath);
  if (!resolved) return;
  try {
    fs.unlinkSync(resolved);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== "ENOENT") throw error;
  }
}

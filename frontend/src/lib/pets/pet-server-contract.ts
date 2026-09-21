import { parseManifest, type PetManifest } from "./codex-pet";

export const PET_IMAGE_LIMIT = 12 * 1024 * 1024;
export const PET_MANIFEST_LIMIT = 16 * 1024;
export const PET_UPLOAD_LIMIT = PET_IMAGE_LIMIT + 64 * 1024;
export const PET_REFRESH_MS = 15_000;
export type ServerPet = {
  revision: string;
  manifest: PetManifest;
  updatedAt: string;
};
export class PetRequestError extends Error {
  constructor(message: string, public readonly status: number) { super(message); }
}
export function parseServerPet(value: unknown): ServerPet | null {
  if (value === null) return null;
  if (!value || typeof value !== "object") throw new Error("サーバーのペット情報が不正です。");
  const pet = value as Record<string, unknown>;
  if (typeof pet.revision !== "string" || !/^[a-f0-9-]{36}$/.test(pet.revision) ||
      typeof pet.updatedAt !== "string" || !Number.isFinite(Date.parse(pet.updatedAt))) {
    throw new Error("サーバーのペット情報が不正です。");
  }
  return { revision: pet.revision, updatedAt: pet.updatedAt, manifest: parseManifest(JSON.stringify(pet.manifest)) };
}

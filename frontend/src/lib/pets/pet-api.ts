import type { PetManifest } from "./codex-pet";
import { PET_IMAGE_LIMIT, PetRequestError, parseServerPet } from "./pet-server-contract";

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(path, { ...init, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new PetRequestError(typeof body?.detail === "string" ? body.detail : "サーバーのペットを読み書きできません。", response.status);
  }
  return response;
}
export async function fetchServerPet(signal?: AbortSignal) {
  const response = await request("/api/pets", { signal });
  const body = await response.json();
  return parseServerPet(body.pet);
}
export async function fetchPetImage(revision: string, signal?: AbortSignal): Promise<Blob> {
  const response = await request(`/api/pets/image?revision=${encodeURIComponent(revision)}`, { signal });
  const image = await response.blob();
  if (image.type !== "image/png" || !image.size || image.size > PET_IMAGE_LIMIT) throw new Error("サーバーのペット画像が不正です。");
  return image;
}
export async function registerServerPet(manifest: PetManifest, image: Blob, createOnly: boolean, signal?: AbortSignal) {
  const body = new FormData();
  body.append("manifest", JSON.stringify(manifest));
  body.append("image", image, "sprite");
  const response = await request("/api/pets", {
    method: "PUT", body, signal,
    headers: { "x-aoitalk-pet": "1", ...(createOnly ? { "if-none-match": "*" } : {}) },
  });
  const pet = parseServerPet((await response.json()).pet);
  if (!pet) throw new Error("サーバーへのペット登録を確認できませんでした。");
  return pet;
}
export async function deleteServerPet(signal?: AbortSignal): Promise<void> {
  await request("/api/pets", { method: "DELETE", signal, headers: { "x-aoitalk-pet": "1" } });
}

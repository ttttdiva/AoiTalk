import { PetRequestError } from "../pets/pet-server-contract";
import { ServerPetStore } from "./pet-store";
import { readPetUpload } from "./pet-upload";

const headers = {
  "cache-control": "private, no-store, max-age=0",
  "x-content-type-options": "nosniff",
  "cross-origin-resource-policy": "same-origin",
};
function json(value: unknown, status = 200) { return Response.json(value, { status, headers }); }
function mutationAllowed(request: Request) {
  // Non-simple header forces a CORS preflight; these routes never grant CORS.
  const site = request.headers.get("sec-fetch-site");
  if (request.headers.get("x-aoitalk-pet") !== "1" || (site && site !== "same-origin" && site !== "none")) {
    throw new PetRequestError("別サイトからペットを変更することはできません。", 403);
  }
}
export function assertPetCapability(profiles: readonly (string | undefined)[], features: unknown): void {
  const application = (features as { application_features?: { entertainment?: unknown } } | null)?.application_features;
  if (profiles.some((value) => value?.trim().toLowerCase() === "enterprise") || application?.entertainment !== true) {
    throw new PetRequestError("このプロファイルではペットを利用できません。", 403);
  }
}

/** Framework-independent handlers also exercised by the server integration tests. */
export function createPetHandlers(authorize: () => Promise<void>, store = new ServerPetStore()) {
  const handle = (action: (request: Request) => Promise<Response>) => async (request: Request) => {
    try { await authorize(); return await action(request); }
    catch (error) {
      if (error instanceof PetRequestError) return json({ detail: error.message }, error.status);
      return json({ detail: "サーバーのペット処理に失敗しました。保存領域・接続状態を確認して再試行してください。" }, 503);
    }
  };
  return {
    GET: handle(async () => json({ pet: await store.metadata() })),
    PUT: handle(async (request) => {
      mutationAllowed(request);
      const { manifest, image } = await readPetUpload(request);
      const pet = await store.save(manifest, image, request.headers.get("if-none-match") === "*");
      return json({ pet });
    }),
    DELETE: handle(async (request) => {
      mutationAllowed(request);
      await store.remove();
      return json({ pet: null });
    }),
    IMAGE: handle(async (request) => {
      const revision = new URL(request.url).searchParams.get("revision") ?? "";
      if (!/^[a-f0-9-]{36}$/.test(revision)) throw new PetRequestError("画像の版が不正です。", 400);
      const image = await store.image(revision);
      return new Response(new Uint8Array(image), { headers: {
        ...headers, "content-type": "image/png", "content-length": String(image.length),
        "content-disposition": 'inline; filename="pet.png"',
      } });
    }),
  };
}

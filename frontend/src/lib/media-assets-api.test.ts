import { afterEach, expect, it, vi } from "vitest";
import { mediaAssetsApi } from "./media-assets-api";
afterEach(() => vi.restoreAllMocks());
it("keeps available asset candidates while projecting the server partial header", async () => {
  const candidate = { source_kind: "library_file", source_id: "source", mime_type: "image/png", size_bytes: 100, sha256: null };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify([candidate]), { headers: { "X-Media-Asset-Sources-Partial": "true", "X-Media-Asset-Sources-Warning": "storage_unavailable" } }));
  await expect(mediaAssetsApi.sources("persona")).resolves.toEqual({ sources: [candidate], partial: true });
});
it("keeps the existing JSON-array contract for complete and empty results", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("[]"));
  await expect(mediaAssetsApi.sources("persona")).resolves.toEqual({ sources: [], partial: false });
});
it("does not hide permission or malformed-reference errors as partial results", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("[]", { status: 403, headers: { "X-Media-Asset-Sources-Partial": "true" } }));
  await expect(mediaAssetsApi.sources("persona")).rejects.toMatchObject({ status: 403 });
  fetch.mockResolvedValue(new Response(JSON.stringify([{ source_kind: "library_file", source_id: "source", mime_type: "image/png", sha256: "changed" }]), { headers: { "X-Media-Asset-Sources-Partial": "true" } }));
  await expect(mediaAssetsApi.sources("persona")).rejects.toThrow("既存制作物の参照");
});

it("shows storage guidance from the typed 503 code without displaying server messages", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: {
    category: "storage_unavailable", code: "storage_unavailable", message: "endpoint=https://private.test/token-secret path=/private/mount",
  } }), { status: 503 }));
  await expect(mediaAssetsApi.register({ persona_id: "persona", source_kind: "library_file", source_id: "source" }, "offline"))
    .rejects.toMatchObject({ status: 503, code: "storage_unavailable", message: "素材の保存先を利用できません。接続状態と登録記録を確認してください。" });
  fetch.mockResolvedValue(new Response(JSON.stringify({ detail: { category: "storage_unavailable", code: "storage_busy" } }), { status: 503 }));
  await expect(mediaAssetsApi.sources("persona")).rejects.toMatchObject({ status: 503, code: "storage_busy", message: "素材の保存先で処理が続いています。少し待ってから登録記録を確認してください。" });
});
it("keeps permission and unknown 503 errors on the existing status boundary", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: { category: "storage_unavailable", code: "storage_unavailable", message: "/private/path" } }), { status: 403 }));
  await expect(mediaAssetsApi.sources("persona")).rejects.toMatchObject({ status: 403, message: "管理者権限、所属範囲または実行環境の許可がありません。" });
  for (const body of ["broken json", JSON.stringify({ detail: { category: "storage_unavailable", code: "unknown", message: "/private/path" } }), JSON.stringify({ detail: { category: "internal_error", code: "storage_unavailable", message: "/private/path" } })]) {
    fetch.mockResolvedValue(new Response(body, { status: 503 }));
    await expect(mediaAssetsApi.sources("persona")).rejects.toMatchObject({ status: 503, message: "接続先または実行サービスを利用できません。準備状態を確認してください。" });
  }
});

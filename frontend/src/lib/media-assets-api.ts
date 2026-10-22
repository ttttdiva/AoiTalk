import type { components } from "./api-types.gen";
import { EmployeeApiError } from "./agent-employees-api";
import { withBasePath } from "./base-path";
import type { PersonaWorkspaceScope } from "./persona-workspace-model";
import type { PlatformContentPayload, ContentVariantRevision } from "./media-operations-content-api";

/** Canonical backend-generated wire contract; see docs/media_asset_contract.md. */
export type MediaAssetReceipt = components["schemas"]["MediaAssetResponse"];
export type MediaAssetSource = components["schemas"]["MediaAssetSourceResponse"];
export type MediaAssetCapabilities = components["schemas"]["MediaAssetCapabilitiesResponse"];
export type MediaAssetRegistration = components["schemas"]["MediaAssetRegisterRequest"];
const base = "/api/python-proxy/operations/media";
const id = encodeURIComponent;
async function request<T>(path: string, init?: RequestInit, metadata?: (headers: Headers) => void): Promise<T> {
  const response = await fetch(withBasePath(`${base}${path}`), { credentials: "include", cache: "no-store", ...init }).catch(() => { throw new EmployeeApiError(0); });
  if (!response.ok) {
    const error = new EmployeeApiError(response.status);
    if (response.status === 503) {
      const body: unknown = await response.json().catch(() => null);
      const detail = body && typeof body === "object" && "detail" in body ? body.detail : null;
      if (detail && typeof detail === "object" && "category" in detail && detail.category === "storage_unavailable" && "code" in detail) {
        const message = detail.code === "storage_unavailable" ? "素材の保存先を利用できません。接続状態と登録記録を確認してください。"
          : detail.code === "storage_busy" ? "素材の保存先で処理が続いています。少し待ってから登録記録を確認してください。" : null;
        if (message) Object.assign(error, { code: detail.code, message });
      }
    }
    throw error;
  }
  metadata?.(response.headers);
  try { return await response.json() as T; } catch { throw new Error("制作物の保存応答を照合できません。登録記録を再取得してください。"); }
}
export const mediaAssetsApi = {
  capabilities: async () => { const value = await request<MediaAssetCapabilities>("/assets/capabilities"); if (!value || !Number.isSafeInteger(value.image_max_bytes) || value.image_max_bytes <= 0 || !Number.isSafeInteger(value.video_max_bytes) || value.video_max_bytes <= 0 || !Array.isArray(value.supported_mime_types) || value.supported_mime_types.some(mime => typeof mime !== "string") || value.upload_transport !== "binary_stream") throw new Error("素材登録の種類とサイズ上限を照合できません。登録情報を再取得してください。"); return value; },
  list: (personaId: string) => request<MediaAssetReceipt[]>(`/assets?${new URLSearchParams({ persona_id: personaId })}`),
  sources: async (personaId: string) => { let partial = false; const value = await request<MediaAssetSource[]>(`/asset-sources?${new URLSearchParams({ persona_id: personaId })}`, undefined, headers => { partial = headers.get("x-media-asset-sources-partial") === "true"; }); if (!Array.isArray(value) || value.some(s => !s || !["generated_media", "library_file", "generation_output"].includes(s.source_kind) || typeof s.source_id !== "string" || typeof s.mime_type !== "string" || !(s.sha256 === null || typeof s.sha256 === "string" && /^[a-f0-9]{64}$/.test(s.sha256)))) throw new Error("既存制作物の参照を照合できません。登録情報を再取得してください。"); return { sources: value, partial }; },
  get: (outputId: string) => request<MediaAssetReceipt>(`/assets/${id(outputId)}`),
  register: (body: MediaAssetRegistration, key: string) => request<MediaAssetReceipt>("/assets/register", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key }, body: JSON.stringify(body) }),
  upload: (file: File, binding: { persona_id: string; content_item_id: string; expected_content_item_hash: string }, key: string, sha256: string) => request<MediaAssetReceipt>(`/assets/upload?${new URLSearchParams(binding)}`, { method: "POST", headers: { "Content-Type": file.type, "Idempotency-Key": key, "X-Asset-SHA256": sha256 }, body: file }),
};
export function assetInScope(asset: MediaAssetReceipt, scope: PersonaWorkspaceScope, outputId?: string, sha256?: string): boolean {
  return Boolean(asset && asset.owner_user_id === scope.ownerUserId && asset.project_id === scope.projectId && asset.persona_id === scope.personaId &&
    typeof asset.generation_output_id === "string" && typeof asset.asset_revision_id === "string" && /^[a-f0-9]{64}$/.test(asset.sha256) &&
    Number.isSafeInteger(asset.size_bytes) && asset.size_bytes > 0 && /^(?:image\/(?:png|jpeg|webp|gif)|video\/(?:mp4|webm|quicktime))$/.test(asset.mime_type) &&
    (!outputId || asset.generation_output_id === outputId) && (!sha256 || asset.sha256 === sha256));
}
export function assetPreviewUrl(asset: MediaAssetReceipt): string {
  // Construct the exact authenticated route; never navigate a returned URL/path.
  return withBasePath(`${base}/assets/${id(asset.generation_output_id)}/preview?${new URLSearchParams({ expected_sha256: asset.sha256 })}`);
}
export function publicationAssetRefs(payload: PlatformContentPayload): Array<{ id: string; role: string }> {
  const refs: Array<{ id: string; role: string }> = [];
  for (const field of ["media", "media_asset", "thumbnail", "thumbnail_assets", "preview_assets", "attachments", "cover"] as const) {
    const value = field in payload ? (payload as unknown as Record<string, unknown>)[field] : undefined;
    if (Array.isArray(value)) for (const outputId of value) if (typeof outputId === "string") refs.push({ id: outputId, role: field === "thumbnail" ? "thumbnail" : field });
  }
  if (payload.type === "x_thread") payload.posts.forEach((post, i) => post.media?.forEach(outputId => refs.push({ id: outputId, role: `投稿${i + 1}の添付` })));
  return refs;
}
export function assetPin(revision: ContentVariantRevision, outputId: string): string | null {
  const pins = (Array.isArray(revision.generation_output_refs) ? revision.generation_output_refs : []).filter(r => r.generation_output_id === outputId);
  return pins.length === 1 && typeof pins[0].sha256 === "string" && /^[a-f0-9]{64}$/.test(pins[0].sha256) ? pins[0].sha256 : null;
}

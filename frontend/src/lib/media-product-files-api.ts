import type { components } from "./api-types.gen";
import { EmployeeApiError } from "./agent-employees-api";
import { withBasePath } from "./base-path";

type S = components["schemas"];
export type ProductFile = S["MediaProductFile"];
export type ProductFileArtifact = S["MediaProductFileArtifact"];
export type ProductDeliverable = S["MediaProductDeliverableResponse"];
const base = "/api/python-proxy/operations/media";

async function request<T>(path: string, signal?: AbortSignal, body?: unknown): Promise<T> {
  const response = await fetch(withBasePath(`${base}${path}`), { credentials: "include", cache: "no-store", signal,
    ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) throw new EmployeeApiError(response.status);
  return response.json() as Promise<T>;
}

export const productFilesApi = {
  list: async (personaId: string, signal?: AbortSignal, fileName = "") => {
    const result = await request<S["MediaProductFilesResponse"]>(`/product-files?${new URLSearchParams({ persona_id: personaId, file_name: fileName })}`, signal);
    if (!result || !Array.isArray(result.files) || !Number.isInteger(result.max_bytes) || result.max_bytes < 1 || result.max_bytes > 1024 ** 3 || result.files.some(file => !file || typeof file.source_id !== "string" || !file.source_id || typeof file.filename !== "string" || typeof file.owner_user_id !== "string" || (file.project_id !== null && typeof file.project_id !== "string") || typeof file.etag !== "string" || !file.etag || !Number.isInteger(file.size_bytes) || file.size_bytes < 1 || file.size_bytes > result.max_bytes)) throw new Error("Filesの配布物一覧を照合できません。");
    return result;
  },
  import: (personaId: string, file: ProductFile, signal: AbortSignal) => request<ProductFileArtifact>("/product-files/import", signal, {
    persona_id: personaId, source_id: file.source_id, expected_etag: file.etag, expected_size_bytes: file.size_bytes,
  }),
  manifest: (actionId: string, version: number, payloadHash: string, signal: AbortSignal) => request<ProductDeliverable>(`/product-actions/${encodeURIComponent(actionId)}/deliverable?${new URLSearchParams({ expected_action_version: String(version), expected_payload_hash: payloadHash })}`, signal),
};

/** A browser download navigation streams to disk instead of building a JS Blob. */
export function productDeliverableUrl(actionId: string, version: number, payloadHash: string, file: ProductFileArtifact) {
  return withBasePath(`${base}/product-actions/${encodeURIComponent(actionId)}/deliverable/content?${new URLSearchParams({
    expected_action_version: String(version), expected_payload_hash: payloadHash,
    expected_artifact_id: file.artifact_version_id, expected_sha256: file.sha256,
  })}`);
}

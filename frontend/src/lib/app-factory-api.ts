export type AppFactoryArtifact = {
  artifact_id: string;
  kind: "local_web" | "hosted_web" | "bat_macro" | string;
  title: string;
  slug: string;
  created_at: string;
  files: string[];
  warnings: string[];
  download_filename: string;
  download_url: string;
  preview_url?: string | null;
  download_available?: boolean;
  preview_available?: boolean;
  download_size_bytes?: number;
  runtime?: Record<string, unknown>;
};

async function appFactoryFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`/api/python-proxy/api/app-factory${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json() as Promise<T>;
}

export const appFactoryApi = {
  async listArtifacts(limit = 100) {
    return appFactoryFetch<{
      success: boolean;
      artifacts: AppFactoryArtifact[];
    }>(`/artifacts?limit=${encodeURIComponent(String(limit))}`);
  },

  async deleteArtifact(artifactId: string) {
    return appFactoryFetch<{
      success: boolean;
      artifact: AppFactoryArtifact;
    }>(`/artifacts/${encodeURIComponent(artifactId)}`, {
      method: "DELETE",
    });
  },

  downloadUrl(artifactId: string) {
    return `/api/python-proxy/api/app-factory/artifacts/${encodeURIComponent(
      artifactId,
    )}/download`;
  },

  previewUrl(artifactId: string) {
    return `/api/python-proxy/api/app-factory/artifacts/${encodeURIComponent(
      artifactId,
    )}/preview`;
  },
};

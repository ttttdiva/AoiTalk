"use client";

import type { components } from "@/lib/api-types.gen";

export type MediaSearchResult = components["schemas"]["MediaSearchResult"];
export type MediaSearchRequest = components["schemas"]["MediaSearchRequest"];
export type MediaImageQuery = components["schemas"]["MediaImageQuery"];
type SearchResponse = components["schemas"]["MediaSearchResponse"];
type OriginalResponse = components["schemas"]["MediaOriginalResponse"];
export type MediaIndexProgress = components["schemas"]["MediaIndexProgressResponse"];
export type MediaQueryPlan = components['schemas']['MediaQueryPlan'];
export type MediaUnifiedHit = components['schemas']['MediaUnifiedHit'];
export type MediaUnifiedSearchResponse = components['schemas']['MediaUnifiedSearchResponse'];
export type ChatSearchAttachment = components['schemas']['ChatSearchAttachment'];

export function mediaSearchLink(value: string | null | undefined): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return null;
  try {
    const decoded = decodeURIComponent(value);
    if (decoded.startsWith("//") || decoded.includes("\\") || Array.from(decoded).some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) return null;
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin) return null;
    return value.startsWith("/api/") && !value.startsWith("/api/python-proxy/")
      ? `/api/python-proxy${value.slice(4)}` : value;
  } catch { return null; }
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`/api/python-proxy/search/media${path}`, {
    credentials: "include", ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) throw new Error(response.status === 409
    ? "資料が検索時から変更されています。再検索して最新の根拠を確認してください。"
    : response.status === 413
    ? "添付検索は12件・合計100 MBまでです。対象を選び直してください。"
    : response.status === 404
    ? "資料を取得できません。権限や原本を確認してください。"
    : "検索を利用できません。しばらくしてから再試行してください。");
  return response.json() as Promise<T>;
}

export type SearchEvidence = components['schemas']['MediaEvidenceResponse'];

async function readHit(hit: MediaUnifiedHit, scope: { project_id?: string | null; space_id?: string | null }, signal?: AbortSignal): Promise<SearchEvidence> {
  // Files hits live in the same media index and are read by unit_id.
  if ((hit.target_kind === 'media' || hit.target_kind === 'files') && hit.unit_id) {
    const result = (await mediaSearchApi.read(hit.unit_id, scope, signal)).result;
    return { ...result, citation: hit.citation ?? {}, complete: false, warnings: ['evidence_snippet'] };
  }
  if (hit.target_kind === 'media' || hit.target_kind === 'files') throw new Error('資料の参照を確認できません');
  return request<SearchEvidence>('/evidence', { method: 'POST', signal,
    body: JSON.stringify({ target_kind: hit.target_kind, locator: hit.locator, citation: hit.citation ?? {}, ...scope }) });
}

export const mediaSearchApi = {
  readHit,
  search: (body: MediaSearchRequest, signal?: AbortSignal) => request<SearchResponse>("", {
    method: "POST", body: JSON.stringify(body), signal,
  }),
  unified: (body: MediaSearchRequest, signal?: AbortSignal) => request<MediaUnifiedSearchResponse>('', {
    method: 'POST', body: JSON.stringify(body), signal,
  }),
  attachments: (session: string, message: string, signal?: AbortSignal) =>
    request<ChatSearchAttachment[]>(`/attachments?${new URLSearchParams({ session_id: session, message_id: message })}`, { signal }),
  read: (unit: string, scope: { project_id?: string | null; space_id?: string | null }, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (scope.project_id) params.set("project_id", scope.project_id);
    if (scope.space_id) params.set("space_id", scope.space_id);
    return request<OriginalResponse>(`/${encodeURIComponent(unit)}${params.size ? `?${params}` : ""}`, { signal });
  },
  progress: (scope: { project_id?: string | null; space_id?: string | null }, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (scope.project_id) params.set("project_id", scope.project_id);
    if (scope.space_id) params.set("space_id", scope.space_id);
    return request<MediaIndexProgress>(`/progress${params.size ? `?${params}` : ""}`, { signal });
  },
};

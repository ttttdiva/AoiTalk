import { fetchApiAtServerFingerprint, getConfiguredApiServerFingerprint, isApiHttpError } from "./api-client";
import { readAuthActor } from "./auth";

export async function graphIdentity(): Promise<string> {
  const server = await getConfiguredApiServerFingerprint();
  const { readable, actor } = await readAuthActor();
  if (!readable || !actor || actor.serverFingerprint !== server) throw new Error("identity unavailable");
  return JSON.stringify([server, actor.user_id, actor.role]);
}

export type GraphNode = { key: string; agent_id: string; revision_id: string | null; role: "coordinator" | "worker" | "reviewer"; x: number; y: number; title: string | null };
export type GraphEdge = { key: string; source: string; target: string; kind: "supervises" | "assigns" | "reviews" | "handoff"; output_key: string | null };
export type GraphGroup = { key: string; title: string; purpose: string; node_keys: string[]; leader_key: string | null };
export type GraphDefinition = { schema_version: 1; root_key: string | null; nodes: GraphNode[]; edges: GraphEdge[]; groups: GraphGroup[] };
export type TeamGraph = { id: string; title: string; project_id: string | null; created_by: string; created_at: string; updated_at: string; current_revision_id: string; revision_id: string; version: number; content_hash: string; definition: GraphDefinition; diagnostics: string[] };
export type GraphContent = { title: string; project_id: string | null; definition: GraphDefinition };
export const emptyGraph = (): GraphDefinition => ({ schema_version: 1, root_key: null, nodes: [], edges: [], groups: [] });
const path = "/api/agent-team-graphs";
const request = <T>(server: string, url: string, signal?: AbortSignal, method = "GET", body?: unknown) => fetchApiAtServerFingerprint<T>(server, url, { method, signal, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
export const teamGraphApi = {
  list: (server: string, signal?: AbortSignal) => request<{ success: boolean; graphs: TeamGraph[]; total: number }>(server, path, signal),
  get: (server: string, id: string, signal?: AbortSignal, revisionId?: string) => request<{ success: boolean; graph: TeamGraph }>(server, `${path}/${encodeURIComponent(id)}${revisionId ? `?revision_id=${encodeURIComponent(revisionId)}` : ""}`, signal),
  revisions: (server: string, id: string, signal?: AbortSignal) => request<{ success: boolean; revisions: TeamGraph[]; total: number }>(server, `${path}/${encodeURIComponent(id)}/revisions`, signal),
  create: (server: string, body: GraphContent & { idempotency_key: string }, signal?: AbortSignal) => request<{ success: boolean; graph: TeamGraph }>(server, path, signal, "POST", body),
  update: (server: string, id: string, body: GraphContent & { expected_revision: number }, signal?: AbortSignal) => request<{ success: boolean; graph: TeamGraph }>(server, `${path}/${encodeURIComponent(id)}`, signal, "PUT", body),
};
export function graphError(error: unknown): string {
  if (isApiHttpError(error)) {
    if (error.status === 403) return "Teamの管理には管理者権限が必要です。アカウントと利用範囲を確認してください。";
    if (error.status === 409) return "別の更新と競合しました。変更を保持しています。再読み込みして内容を確認してください。";
    if (error.status === 401) return "ログイン状態を確認してください。";
    if (error.status === 404 || error.status === 503) return "Teamの機能または対象を利用できません。接続先を確認してください。";
    if (error.status === 422) return "入力内容や参照先を確認してください。";
  }
  return "通信を完了できませんでした。接続先と保存状態を確認してください。";
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
  return JSON.stringify(value);
}
export function sameGraphContent(graph: TeamGraph, content: GraphContent): boolean {
  return canonical({ title: graph.title, project_id: graph.project_id, definition: graph.definition }) === canonical(content);
}

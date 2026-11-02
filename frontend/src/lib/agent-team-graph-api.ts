import type { components } from "@/lib/api-types.gen";
import { employeeRequest } from "@/lib/agent-employees-api";
export type GraphNode = { key: string; agent_id: string; revision_id: string | null; role: "coordinator" | "worker" | "reviewer"; x: number; y: number; title: string | null };
export type GraphEdge = { key: string; source: string; target: string; kind: "supervises" | "assigns" | "reviews" | "handoff"; output_key: string | null };
export type GraphGroup = { key: string; title: string; purpose: string; node_keys: string[]; leader_key: string | null; execution_kind?: components["schemas"]["Group"]["execution_kind"] | "software_code" };
export type GraphDefinition = { schema_version: 1; root_key: string | null; nodes: GraphNode[]; edges: GraphEdge[]; groups: GraphGroup[] };
export type TeamGraph = { id: string; title: string; project_id: string | null; revision_id: string; version: number; content_hash: string; definition: GraphDefinition; diagnostics: string[] };
export const emptyGraph = (): GraphDefinition => ({ schema_version: 1, root_key: null, nodes: [], edges: [], groups: [] });
export type DefaultGraphRef = { graph_id: string; revision_id: string; content_hash: string };
export type RoutingNode = { key: string; role: string; agent_id: string; revision_id: string | null };
export type RoutingContext = DefaultGraphRef & { root: RoutingNode; groups: { key: string; leader: RoutingNode; members: RoutingNode[]; title: string; purpose: string; execution_kind: string | null }[]; title: string };
export type DefaultGraphConfiguration = { available: boolean; configuration: RoutingContext | null };
const path = "/api/agent-team-graphs";
async function readDefault(): Promise<DefaultGraphConfiguration> {
 const value = await employeeRequest<DefaultGraphConfiguration>(`${path}/default/configuration`);
 const ref = value?.configuration;
 const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
 if (typeof value?.available !== "boolean" || (value.available ? !ref || typeof ref.graph_id !== "string" || !uuid.test(ref.graph_id) || typeof ref.revision_id !== "string" || !uuid.test(ref.revision_id) || typeof ref.content_hash !== "string" || !/^[0-9a-f]{64}$/.test(ref.content_hash) : ref !== null)) throw new Error("既定の応答を確認できません。");
 return value;
}
export const teamGraphApi = {
 getDefault: readDefault,
 setDefault: (body: { graph_id: string; revision_id: string; expected: DefaultGraphRef | null }) => employeeRequest<DefaultGraphConfiguration>(`${path}/default/configuration`, "PUT", body),
 list: () => employeeRequest<{ graphs: TeamGraph[] }>(path),
 get: (id: string) => employeeRequest<{ graph: TeamGraph }>(`${path}/${encodeURIComponent(id)}`),
 revisions: (id: string) => employeeRequest<{ revisions: TeamGraph[] }>(`${path}/${encodeURIComponent(id)}/revisions`),
 create: (body: { title: string; project_id: string | null; definition: GraphDefinition; idempotency_key: string }) => employeeRequest<{ graph: TeamGraph }>(path, "POST", body),
 update: (id: string, body: { title: string; project_id: string | null; definition: GraphDefinition; expected_revision: number }) => employeeRequest<{ graph: TeamGraph }>(`${path}/${encodeURIComponent(id)}`, "PUT", body),
};

/** Future runtime integration receives a saved immutable revision; no execution is initiated here. */
export type TeamGraphExecutionAdapter = (revision: Pick<TeamGraph, "id" | "revision_id" | "version" | "content_hash">) => Promise<void>;

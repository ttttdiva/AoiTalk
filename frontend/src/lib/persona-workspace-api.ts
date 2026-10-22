import { employeeRequest } from "./agent-employees-api";
import type { components } from "./api-types.gen";

export type PersonaTeamSnapshot = {
  organization?: { display_name?: string };
  agents: Array<{
    id: string; display_name: string; state: string; job_title?: string;
    responsibility_summary?: string; primary_space_id?: string;
    persona_assignments?: Array<{ persona_id: string; assignment_role?: string; state: string }>;
    project_grants?: Array<{ project_id: string; role: string; state: string }>;
  }>;
  spaces?: Array<{ id: string; name: string }>;
  current_work: Array<{
    id: string; project_id: string; assigned_agent_id?: string; agent?: string;
    task_title?: string; source_type?: string; state: string;
    execution_summary?: { current_phase?: string; attention_reasons?: string[] };
  }>;
};

/** Read-only projection: no Task or Job is created while loading the workspace. */
export const personaWorkspaceApi = {
  async team(projectId: string): Promise<PersonaTeamSnapshot> {
    const query = new URLSearchParams({ scope: "project", project_id: projectId });
    const value = await employeeRequest<PersonaTeamSnapshot>(`/api/operations/command-center?${query}`);
    if (!value || !Array.isArray(value.agents) || !Array.isArray(value.current_work)) throw new Error("担当Agentと仕事の応答を照合できません。");
    return value;
  },
  organization: async () => {
    const value = await employeeRequest<components["schemas"]["OrganizationGraphResponse"]>("/api/agent-organization");
    if (!value || !Array.isArray(value.nodes) || !Array.isArray(value.edges) || value.nodes.some(n => !n || typeof n !== "object") || value.edges.some(e => !e || typeof e !== "object")) throw new Error("組織の応答を照合できません。");
    return value;
  },
  delegations: async () => {
    const value = await employeeRequest<components["schemas"]["DelegationListResponse"]>("/api/agent-delegations?limit=200");
    if (!value || !Array.isArray(value.delegations) || value.delegations.some(d => !d || typeof d.id !== "string" || !d.phases || typeof d.phases !== "object" || !d.artifacts || typeof d.artifacts !== "object" || !d.readiness || typeof d.readiness.ready !== "boolean")) throw new Error("委託の応答を照合できません。");
    return value;
  },
  delegationArtifact: (delegationId: string, artifactId: string) => employeeRequest<components["schemas"]["DelegationArtifactResponse"]>(`/api/agent-delegations/${encodeURIComponent(delegationId)}/artifacts/${encodeURIComponent(artifactId)}`),
  delegation: (id: string) => employeeRequest<components["schemas"]["DelegationResponse"]>(`/api/agent-delegations/${encodeURIComponent(id)}`),
};

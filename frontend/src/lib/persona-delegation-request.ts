import { EmployeeApiError } from "./agent-employees-api";
import type { components } from "./api-types.gen";
import type { PersonaWorkspaceScope } from "./persona-workspace-model";
import type { ContentItem } from "./media-operations-research-api";
import type { OrganizationGraph } from "./employee-organization-setup-api";

type S = components["schemas"];
export type PersonaDelegationRequest = S["DelegationCreateRequest"];
export type RequestEmployee = { id: string; revisionId: string; name: string };
export function delegationRequestBody(scope: PersonaWorkspaceScope, brief: ContentItem, manager: RequestEmployee, worker: RequestEmployee, reviewer: RequestEmployee, role: "researcher" | "writer" | "data_worker" | "general_worker", perCall: number, total: number, key: string): PersonaDelegationRequest {
  if (brief.owner_user_id !== scope.ownerUserId || brief.project_id !== scope.projectId || brief.persona_revision_id !== scope.personaRevisionId || brief.status === "archived" || !brief.content_hash || !brief.brief.trim() || new Set([manager.id, worker.id, reviewer.id]).size !== 3) throw new Error("同じ本人・Project・人格のbriefと独立した3Agentを確認してください。");
  if (!Number.isInteger(perCall) || perCall < 256 || perCall > 8192 || !Number.isInteger(total) || total < perCall || total > 262144) throw new Error("1呼出256〜8192、全体は1呼出以上〜262144の出力token上限を指定してください。");
  const instruction = `保存済み企画の依頼\n${JSON.stringify({ content_item_id: brief.id, content_item_version: brief.version, content_item_hash: brief.content_hash, input_kind: brief.input_kind, owner_user_id: scope.ownerUserId, project_id: scope.projectId, persona_id: scope.personaId, persona_revision_id: scope.personaRevisionId })}\n\n${brief.brief}\n\n目的: ${brief.objective ?? "未設定"}\n\n許可された担当で計画・制作・独立review・返却を行ってください。公開は本人の公開ボタンで別途確認します。`;
  if (instruction.length > 12000) throw new Error("briefと参照情報が委託の12000文字上限を超えています。");
  return { delegator_agent_id: manager.id, delegator_revision_id: manager.revisionId, worker_agent_id: worker.id, worker_revision_id: worker.revisionId, reviewer_agent_id: reviewer.id, reviewer_revision_id: reviewer.revisionId, project_id: scope.projectId, persona_id: scope.personaId, title: brief.title.trim().slice(0, 160), instruction, work_role: role, idempotency_key: key,
    planning: { roster: [{ key: "worker", agent_id: worker.id, agent_revision_id: worker.revisionId, allowed_work_roles: [role], scope_keys: ["current"], can_review: false, can_delegate: false }, { key: "reviewer", agent_id: reviewer.id, agent_revision_id: reviewer.revisionId, allowed_work_roles: [], scope_keys: ["current"], can_review: true, can_delegate: false }], brand_keys: [], input_artifacts: {}, max_children: 2, max_total_children: 2, max_depth: 1, max_output_tokens_per_call: perCall, max_total_output_tokens: total, max_attempts_per_phase: 1, max_rework_rounds: 0 } };
}
export function delegationEmployeesUnchanged(body: PersonaDelegationRequest, graph: OrganizationGraph): boolean {
  return (["delegator", "worker", "reviewer"] as const).every(role => { const n = graph.nodes.find(n => n.agent.id === body[`${role}_agent_id`]); return n?.agent.state === "active" && n.revision?.id === body[`${role}_revision_id`]; });
}
export class DelegationRequestError extends EmployeeApiError { constructor(status: number, public readonly code?: string) { super(status); } }
export async function createPersonaDelegation(body: PersonaDelegationRequest): Promise<S["DelegationResponse"]> {
  const response = await fetch("/api/python-proxy/agent-delegations", { method: "POST", credentials: "include", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).catch(() => { throw new DelegationRequestError(0); });
  if (!response.ok) { const value = await response.json().catch(() => null); const code = typeof value?.detail === "string" && /^delegation_[a-z_]{1,100}$/.test(value.detail) ? value.detail : undefined; throw new DelegationRequestError(response.status, code); }
  return response.json();
}
export function delegationReceiptMatches(body: PersonaDelegationRequest, owner: string, response: S["DelegationResponse"]): boolean {
  const d = response?.delegation;
  return response?.success === true && !!d?.id && /^[a-f0-9]{64}$/.test(d.content_hash) && d.created_by === owner && d.project_id === body.project_id && d.persona_id === body.persona_id && d.title === body.title && d.work_role === body.work_role && d.planning?.enabled === true && d.planning.max_total_output_tokens === body.planning?.max_total_output_tokens && (["delegator", "worker", "reviewer"] as const).every(role => d[`${role}_agent_id`] === body[`${role}_agent_id`] && d[`${role}_revision_id`] === body[`${role}_revision_id`]);
}
/** Canonical server digest binds saved brief text, revisions and every planning limit. */
export async function delegationContentHash(body: PersonaDelegationRequest, owner: string): Promise<string> {
  const value = { delegator_agent_id: body.delegator_agent_id, delegator_revision_id: body.delegator_revision_id, worker_agent_id: body.worker_agent_id, worker_revision_id: body.worker_revision_id, reviewer_agent_id: body.reviewer_agent_id, reviewer_revision_id: body.reviewer_revision_id, project_id: body.project_id, persona_id: body.persona_id ?? null, parent_delegation_id: body.parent_delegation_id ?? null, linked_work_item_id: body.linked_work_item_id ?? null, title: body.title, work_role: body.work_role, instruction: body.instruction, created_by: owner, linked_work_binding: {}, planning: body.planning };
  const encoded = JSON.stringify(value, (_key, v) => v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) : v);
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(encoded))), b => b.toString(16).padStart(2, "0")).join("");
}

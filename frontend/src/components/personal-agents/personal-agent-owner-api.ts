import type { components } from "@/lib/api-types.gen";

export type PersonalOwnerAgent = components["schemas"]["PersonalAgentOwnerAgentOut"];
export type PersonalAgentProfileId = PersonalOwnerAgent["profile_id"];
export type PersonalAgentOwnerAgentSaveRequest = components["schemas"]["PersonalAgentOwnerAgentSaveRequest"];
export type PersonalOwnerAgentSession = components["schemas"]["PersonalAgentOwnerSessionOut"];

const URL = "/api/python-proxy/personal-agents/owner-agent";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

async function ownerRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    credentials: "include", cache: "no-store", ...init,
    headers: { ...(init?.body ? { "Content-Type": "application/json" } : {}), ...(init?.headers ?? {}) },
  });
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(response.status === 401 ? "ログイン状態を確認してください" : `個人Agentを保存できませんでした（${response.status}）`);
  return value as T;
}

function validateAgent(value: PersonalOwnerAgent): PersonalOwnerAgent {
  if (!value || !UUID.test(value.agent_id) || !UUID.test(value.agent_revision_id) ||
    value.state !== "active" || typeof value.display_name !== "string" ||
    !["personal-firearms-admin", "personal-ammunition-bookkeeper", "personal-finance-reviewer", "personal-career-planner"].includes(value.profile_id)) {
    throw new Error("個人Agentの応答内容を確認できません");
  }
  return value;
}

function validateSession(value: unknown): PersonalOwnerAgentSession {
  if (!value || typeof value !== "object") throw new Error("本人用sessionの応答内容を確認できません");
  const candidate = value as Partial<PersonalOwnerAgentSession>;
  if (!candidate.id || !UUID.test(candidate.id) || typeof candidate.user_id !== "string" ||
    !candidate.agent_id || !UUID.test(candidate.agent_id) ||
    !(candidate.agent_binding_updated_at === undefined || candidate.agent_binding_updated_at === null ||
      typeof candidate.agent_binding_updated_at === "string") ||
    typeof candidate.title !== "string" || typeof candidate.character_name !== "string") {
    throw new Error("本人用sessionの応答内容を確認できません");
  }
  return candidate as PersonalOwnerAgentSession;
}

export const personalAgentOwnerApi = {
  async list(): Promise<PersonalOwnerAgent[]> {
    const result = await ownerRequest<{ items: PersonalOwnerAgent[] }>(URL);
    if (!result || !Array.isArray(result.items) || result.items.length > 100) throw new Error("個人Agent一覧を取得できません");
    return result.items.map(validateAgent);
  },
  async listSessions(profileId: PersonalAgentProfileId): Promise<PersonalOwnerAgentSession[]> {
    const result = await ownerRequest<{ items: unknown[] }>(
      `${URL}/sessions?profile_id=${encodeURIComponent(profileId)}`,
    );
    if (!result || !Array.isArray(result.items)) throw new Error("本人用session一覧を取得できません");
    return result.items.map(validateSession);
  },
  async save(body: PersonalAgentOwnerAgentSaveRequest, agentId?: string): Promise<PersonalOwnerAgent> {
    const result = await ownerRequest<PersonalOwnerAgent>(agentId ? `${URL}/${encodeURIComponent(agentId)}` : URL, {
      method: agentId ? "PATCH" : "POST", body: JSON.stringify(body),
    });
    return validateAgent(result);
  },
};

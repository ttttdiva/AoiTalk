import type { components } from "./api-types.gen";
import { employeeRequest, employeeScopeRequest, type Employee, type EmployeeCatalog } from "./agent-employees-api";
import type { BrandPortfolio } from "./media-brand-portfolio-api";

type S = components["schemas"];
export type OrganizationEntry = Pick<S["OrganizationSetupEmployee"], "key" | "display_name"> & Partial<Omit<S["OrganizationSetupEmployee"], "key" | "display_name">>;
export type OrganizationManifest = { employees: OrganizationEntry[] };
export type OrganizationNode = { agent: Employee; organization_profile: Record<string, unknown> | null; revision: Record<string, unknown> | null };
export type OrganizationGraph = { success: true; nodes: OrganizationNode[]; edges: Record<string, string>[] };
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const identifier = (value: unknown) => typeof value === "string" && /^[a-z0-9][a-z0-9_.-]{0,99}$/.test(value);

export const employeeOrganizationApi = {
  async owner(): Promise<string> {
    // The existing Next.js status route resolves getSession() against the user DB.
    // Read it afresh for each owner check; the FastAPI status projection has no ID.
    const status = await employeeScopeRequest<{ authenticated?: boolean; user?: { id?: string; password_reset_required?: boolean } }>("/api/auth/status");
    if (status?.authenticated !== true || typeof status.user?.id !== "string" || !status.user.id.trim() || status.user.id !== status.user.id.trim() || status.user.password_reset_required === true) throw new Error("組織設定の本人ownerを確認できません。ログイン状態を確認してください。");
    return status.user.id;
  },
  async graph(): Promise<OrganizationGraph> {
    const value = await employeeRequest<S["OrganizationGraphResponse"]>("/api/agent-organization");
    if (value?.success !== true || !Array.isArray(value.nodes) || !Array.isArray(value.edges) || value.nodes.some(n => !object(n.agent) || typeof n.agent.id !== "string" || typeof n.agent.display_name !== "string" || typeof n.agent.slug !== "string" || typeof n.agent.state !== "string" || n.organization_profile != null && !object(n.organization_profile) || n.revision != null && !object(n.revision))) throw new Error("組織の保存状態を照合できません。");
    return value as OrganizationGraph;
  },
  setup: (body: OrganizationManifest) => employeeRequest<S["OrganizationSetupResponse"]>("/api/agent-organization/setup", "POST", body),
};

/** 省略項目を補完せず、既存プロフィール・モデル設定を保全する。 */
export function parseOrganizationManifest(raw: string, graph: OrganizationGraph, catalog: EmployeeCatalog, reconcile = false): OrganizationManifest {
  let value: unknown;
  try { value = JSON.parse(raw); } catch { throw new Error("設定内容をJSON形式で入力してください。"); }
  if (!object(value) || Object.keys(value).some(k => k !== "employees") || !Array.isArray(value.employees) || value.employees.length < 1 || value.employees.length > 64) throw new Error("employeesには1〜64人の設定を指定してください。Project・grant・credentialはこの保存の対象外です。");
  const allowed = ["key", "agent_id", "slug", "display_name", "manager_key", "job_title", "responsibility_summary", "revision", "activate"];
  const revisionKeys = ["agent_team_id", "execution_profile_id", "display_name", "mission", "operational_instructions", "responsibility_summary", "allowed_subagent_ids", "capability_ceiling", "budget_policy", "concurrency_policy", "wake_policy", "version", "content_hash", "idempotency_key"];
  const keys = new Set<string>(); const refs = new Set<string>();
  for (const entry of value.employees) {
    if (!object(entry) || Object.keys(entry).some(k => !allowed.includes(k)) || !identifier(entry.key) || typeof entry.display_name !== "string" || !entry.display_name.trim() || entry.display_name.length > 160 || (!entry.agent_id && !identifier(entry.slug)) || entry.agent_id != null && typeof entry.agent_id !== "string" || entry.slug != null && !identifier(entry.slug) || entry.activate != null && typeof entry.activate !== "boolean") throw new Error("Agentのkey・名前・既存IDまたはslugと正規の設定項目を確認してください。");
    if (keys.has(entry.key as string)) throw new Error("Agentkeyが重複しています。"); keys.add(entry.key as string);
    const existing = graph.nodes.find(n => entry.agent_id ? n.agent.id === entry.agent_id : n.agent.slug === entry.slug);
    if (entry.agent_id && !existing || existing && entry.slug && existing.agent.slug !== entry.slug) throw new Error("既存AgentのIDとslugを照合できません。");
    const ref = existing?.agent.id ?? String(entry.slug); if (refs.has(ref)) throw new Error("同じAgentを複数のkeyに割り当てられません。"); refs.add(ref);
    if (existing && entry.display_name !== existing.agent.display_name) throw new Error("既存Agentの名前を保持してください。");
    if (!reconcile && existing?.revision && entry.revision) throw new Error("既存の職務・Team/Profileは保持します。変更はAgentの職務・モデル画面で明示してください。");
    if (!reconcile && entry.activate === true && (!existing || existing.agent.state === "draft") && !existing?.revision && !entry.revision) throw new Error("draftの有効化には職務revisionが必要です。Agentの職務・モデルを先に設定してください。");
    if (entry.revision != null) {
      const revision = entry.revision;
      if (!object(revision) || Object.keys(revision).some(k => !revisionKeys.includes(k)) || typeof revision.agent_team_id !== "string" || typeof revision.execution_profile_id !== "string" || !revision.execution_profile_id || !catalog.teams.some(t => t.team_id === revision.agent_team_id)) throw new Error("未設定Agentの職務には既存Teamと明示したProfileを指定してください。");
      for (const key of ["capability_ceiling", "allowed_subagent_ids"]) if (revision[key] != null && (!Array.isArray(revision[key]) || revision[key].some(v => typeof v !== "string"))) throw new Error("職務の権限上限・担当subagentは文字列の配列で指定してください。");
    }
  }
  const entries = value.employees as OrganizationEntry[];
  for (const entry of entries) {
    const seen = new Set([entry.key]); let manager = entry.manager_key;
    while (manager) { const parent = entries.find(e => e.key === manager); if (!parent || seen.has(manager)) throw new Error("直属上司のkeyが存在しないか、循環しています。");
      const supervisor = graph.nodes.find(n => parent.agent_id ? n.agent.id === parent.agent_id : n.agent.slug === parent.slug);
      if (!reconcile && supervisor?.agent.state !== "active" && !(parent.activate === true && (!supervisor || supervisor.agent.state === "draft") && (parent.revision || supervisor?.revision))) throw new Error(`${parent.display_name}の職務設定・有効化が必要です。直属上司が未準備のまま途中保存しません。`);
      seen.add(manager); manager = parent.manager_key; }
  }
  return { employees: entries };
}

export function organizationSnapshot(graph: OrganizationGraph): string { return JSON.stringify(graph.nodes.map(n => ({ id: n.agent.id, slug: n.agent.slug, display_name: n.agent.display_name, state: n.agent.state, profile: n.organization_profile, revision: n.revision })).sort((a, b) => a.id.localeCompare(b.id))); }

/** 正本scriptのpreviewだけを読み込み、SNSの参照は現在の本人portfolioに照合する。 */
export function importOrganizationPreview(raw: string, graph: OrganizationGraph, catalog: EmployeeCatalog, portfolio: BrandPortfolio | undefined, owner: string, reconcile = false): OrganizationManifest {
  let value: unknown;
  try { value = JSON.parse(raw); } catch { throw new Error("UTF-8のpreview JSONファイルを選択してください。"); }
  if (!object(value)) throw new Error("正本scriptのpreview JSONを確認してください。");
  if ("employees" in value) return parseOrganizationManifest(raw, graph, catalog, reconcile);
  if (value.applied !== false || !object(value.manifest) || Object.keys(value).some(k => !["applied", "manifest", "brand_bindings"].includes(k))) throw new Error("保存結果や未確認の項目は読み込めません。--applyなしの正本previewを選択してください。");
  const body = parseOrganizationManifest(JSON.stringify(value.manifest), graph, catalog, reconcile);
  const brands = body.employees.filter(e => e.key.startsWith("sns-brand-"));
  if (brands.length || "brand_bindings" in value) {
    if (!portfolio || portfolio.status !== "ready" || portfolio.owner_user_id !== owner || !Array.isArray(value.brand_bindings) || value.brand_bindings.length !== portfolio.brands.length || brands.length !== portfolio.brands.length) throw new Error("SNS previewの本人owner・8brand担当範囲を照合できません。");
    const keys = new Set<string>();
    for (const binding of value.brand_bindings) {
      if (!object(binding) || Object.keys(binding).some(k => !["employee_key", "brand_key", "project_id", "persona_id", "persona_revision_id", "editorial_program_id", "content_item_id"].includes(k))) throw new Error("SNS担当範囲に未確認の項目があります。");
      const brand = portfolio.brands.find(b => b.definition.brand_key === binding.brand_key);
      const expected = brand && { employee_key: `sns-brand-${brand.definition.brand_key}`, brand_key: brand.definition.brand_key, project_id: brand.project_id, persona_id: brand.persona?.id, persona_revision_id: brand.persona?.current_revision?.id, editorial_program_id: brand.editorial_program_id, content_item_id: brand.content_item_id };
      if (!expected || Object.entries(expected).some(([k, v]) => !v || binding[k] !== v) || keys.has(String(binding.employee_key)) || !brands.some(e => e.key === binding.employee_key)) throw new Error("SNS previewが現在の本人Project・Persona・制作範囲と一致しません。別の範囲へ切り替えません。");
      keys.add(String(binding.employee_key));
    }
  }
  return body;
}

const omit = (value: Record<string, unknown>, excluded: string[]) => Object.fromEntries(Object.entries(value).filter(([k]) => !excluded.includes(k)));
const canonical = (value: unknown): string => JSON.stringify(value, (_k, v) => object(v) ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => a.localeCompare(b))) : v);
async function digest(value: unknown): Promise<string> {
  const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(value))));
  return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
}
type PendingEmployee = { key: string; id?: string; slug?: string; name: string; state: string; agentHash?: string; revisionHash?: string; revisionFields?: Record<string, string>; profileFields: Record<string, string>; managerKey?: string | null };
export type OrganizationPending = { version: 2; owner: string; manifestHash: string; employees: PendingEmployee[] };
export const legacyOrganizationPendingKey = "aoitalk-employee-organization-setup-pending-v1";
export const organizationPendingKey = (owner: string) => `aoitalk-employee-organization-setup-pending-v2:${owner}`;
async function fields(value: Record<string, unknown>): Promise<Record<string, string>> { return Object.fromEntries(await Promise.all(Object.entries(value).map(async ([k, v]) => [k, await digest(v)]))); }

/** 永続記録には入力本文を残さず、owner・参照・保全条件と内容hashだけを保持する。 */
export async function createOrganizationPending(body: OrganizationManifest, before: OrganizationGraph, owner: string): Promise<OrganizationPending> {
  const employees = await Promise.all(body.employees.map(async e => {
    const existing = before.nodes.find(n => e.agent_id ? n.agent.id === e.agent_id : n.agent.slug === e.slug);
    const profile = existing?.organization_profile;
    const expected: Record<string, unknown> = { job_title: e.display_name, responsibility_summary: "", primary_space_id: null, manager_user_id: null, manager_agent_id: null, autonomy_level: "supervised", company_permission_ceiling: {}, employment_state: "active", ...(profile ? omit(profile, ["agent_id", "updated_at"]) : {}) };
    for (const k of ["job_title", "responsibility_summary"] as const) if (k in e) expected[k] = e[k];
    if ("manager_key" in e) { delete expected.manager_agent_id; expected.manager_user_id = null; }
    const revisionFields = e.revision ? await fields(omit(e.revision as Record<string, unknown>, ["idempotency_key", "content_hash"])) : undefined;
    return { key: e.key, id: existing?.agent.id, slug: e.slug ?? existing?.agent.slug, name: e.display_name,
      state: e.activate && (!existing || existing.agent.state === "draft") ? "active" : existing?.agent.state ?? "draft",
      ...(existing ? { agentHash: await digest(omit(existing.agent as unknown as Record<string, unknown>, ["updated_at", "state"])) } : {}),
      ...(!e.revision ? { revisionHash: await digest(existing?.revision ? omit(existing.revision, ["route_warnings"]) : null) } : { revisionFields }),
      profileFields: await fields(expected), ...("manager_key" in e ? { managerKey: e.manager_key } : {}) };
  }));
  return { version: 2, owner, manifestHash: await digest(body), employees };
}

export function readOrganizationPending(raw: string, owner: string): OrganizationPending {
  let value: unknown; try { value = JSON.parse(raw); } catch { throw new Error("送信記録のowner・内容hash・保全条件を確認できません。"); }
  const hash = (v: unknown) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
  const profileKeys = ["job_title", "responsibility_summary", "primary_space_id", "manager_user_id", "autonomy_level", "company_permission_ceiling", "employment_state"];
  if (!object(value) || value.version !== 2 || value.owner !== owner || !hash(value.manifestHash) || !Array.isArray(value.employees) || !value.employees.length || value.employees.length > 64 || value.employees.some(e => !object(e) || !identifier(e.key) || typeof e.name !== "string" || !e.name || !["draft", "active", "paused", "retired"].includes(String(e.state)) || !(typeof e.id === "string" && e.id || identifier(e.slug)) || e.id != null && !hash(e.agentHash) || e.slug != null && !identifier(e.slug) || (hash(e.revisionHash) === object(e.revisionFields)) || e.revisionFields != null && (!object(e.revisionFields) || !hash(e.revisionFields.agent_team_id) || !hash(e.revisionFields.execution_profile_id) || Object.values(e.revisionFields).some(h => !hash(h))) || e.managerKey != null && !identifier(e.managerKey) || !object(e.profileFields) || profileKeys.some(k => !hash((e.profileFields as Record<string, unknown>)[k])) || !("managerKey" in e) && !hash(e.profileFields.manager_agent_id) || Object.values(e.profileFields).some(h => !hash(h))) || new Set(value.employees.map(e => (e as Record<string, unknown>).key)).size !== value.employees.length) throw new Error("送信記録のowner・内容hash・保全条件を確認できません。");
  return value as OrganizationPending;
}

/** 読み取り結果が全行一致した場合だけ復旧を許す。GET snapshotはserver CASではない。 */
export async function reconcileOrganizationPending(proof: OrganizationPending, owner: string, body: OrganizationManifest, graph: OrganizationGraph): Promise<boolean> {
  if (proof.owner !== owner || proof.manifestHash !== await digest(body) || proof.employees.length !== body.employees.length) return false;
  const nodes = new Map<string, OrganizationNode>(); const ids = new Set<string>();
  for (const e of proof.employees) {
    const node = graph.nodes.find(n => e.id ? n.agent.id === e.id : n.agent.slug === e.slug);
    if (!node || ids.has(node.agent.id) || node.agent.display_name !== e.name || node.agent.state !== e.state || e.slug && node.agent.slug !== e.slug) return false;
    ids.add(node.agent.id); nodes.set(e.key, node);
    if (e.agentHash && e.agentHash !== await digest(omit(node.agent as unknown as Record<string, unknown>, ["updated_at", "state"]))) return false;
    if (e.revisionHash && e.revisionHash !== await digest(node.revision ? omit(node.revision, ["route_warnings"]) : null)) return false;
    if (e.revisionFields && (!node.revision || (await Promise.all(Object.entries(e.revisionFields).map(async ([k, h]) => h === await digest(node.revision![k])))).some(v => !v))) return false;
    if (!node.organization_profile || (await Promise.all(Object.entries(e.profileFields).map(async ([k, h]) => h === await digest(node.organization_profile![k])))).some(v => !v)) return false;
  }
  return proof.employees.every(e => !("managerKey" in e) || (nodes.get(e.key)?.organization_profile?.manager_agent_id ?? null) === (e.managerKey ? nodes.get(e.managerKey)?.agent.id : null));
}

export function organizationSetupReadback(body: OrganizationManifest, result: S["OrganizationSetupResponse"], graph: OrganizationGraph): boolean {
  if (result?.success !== true || typeof result.ready !== "boolean" || !Array.isArray(result.blockers) || !object(result.employees)) return false;
  const ids = new Map<string, string>();
  for (const entry of body.employees) {
    const saved = result.employees[entry.key]; const node = graph.nodes.find(n => n.agent.id === saved?.id);
    if (!node || entry.agent_id && node.agent.id !== entry.agent_id || entry.slug && node.agent.slug !== entry.slug || node.agent.display_name !== entry.display_name) return false;
    ids.set(entry.key, node.agent.id);
    if (entry.revision && (node.revision?.agent_team_id !== entry.revision.agent_team_id || node.revision?.execution_profile_id !== entry.revision.execution_profile_id)) return false;
  }
  return body.employees.every(entry => {
    if (!("manager_key" in entry) || result.blockers.some(b => b.key === entry.key && b.code === "manager_activation_required")) return true;
    const node = graph.nodes.find(n => n.agent.id === ids.get(entry.key));
    return (node?.organization_profile?.manager_agent_id ?? null) === (entry.manager_key ? ids.get(entry.manager_key) : null);
  });
}

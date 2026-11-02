import { afterEach, describe, expect, it, vi } from "vitest";
import { webcrypto } from "node:crypto";
import { employeeOrganizationApi, parseOrganizationManifest, organizationSetupReadback, organizationSnapshot, createOrganizationPending, reconcileOrganizationPending, importOrganizationPreview, readOrganizationPending, type OrganizationGraph } from "./employee-organization-setup-api";
import type { EmployeeCatalog } from "./agent-employees-api";
const catalog = { can_manage: true, capabilities: [] } as EmployeeCatalog;
const graph: OrganizationGraph = { success: true, edges: [], nodes: [{ agent: { id: "director", slug: "director", display_name: "既存統括", state: "active" }, organization_profile: { manager_agent_id: "old-manager" }, revision: { legacy: { agent_team_id: "team", execution_profile_id: "existing-profile" } } }] };
const existing = { key: "director", agent_id: "director", display_name: "既存統括" };
describe("canonical authenticated owner readback", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("uses the existing Next.js status DTO and rechecks the current cookie owner", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ authenticated: true, user: { id: "owner", role: "admin", password_reset_required: false } })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ authenticated: true, user: { id: "other", role: "admin", password_reset_required: false } })));
    vi.stubGlobal("fetch", fetch);
    expect(await employeeOrganizationApi.owner()).toBe("owner");
    expect(await employeeOrganizationApi.owner()).toBe("other");
    expect(fetch.mock.calls).toEqual(Array(2).fill(["/api/auth/status", { credentials: "include", cache: "no-store" }]));
  });
  it.each([
    { authenticated: false, user: { id: "owner" } },
    { user: { id: "owner" } },
    { authenticated: true },
    { authenticated: true, user: { id: " " } },
    { authenticated: true, user: { id: " owner" } },
    { authenticated: true, user: { id: "owner", password_reset_required: true } },
  ])("rejects unverified or incomplete identity without fallback: %j", async body => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(body)));
    vi.stubGlobal("fetch", fetch);
    await expect(employeeOrganizationApi.owner()).rejects.toThrow("本人owner");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("keeps authentication failure closed without requesting another endpoint", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 }));
    vi.stubGlobal("fetch", fetch);
    await expect(employeeOrganizationApi.owner()).rejects.toMatchObject({ status: 401 });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
describe("organization route response", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("accepts the ORM's nullable historical slug without losing fields", async () => {
    const response = { success: true, edges: [], nodes: [{ agent: { id: "id", display_name: "Agent", slug: null, state: "active", created_by: "owner" }, organization_profile: { manager_agent_id: null, extra: { preserved: true } }, revision: { model_overrides: null, legacy: { agent_team_id: "team" } } }] };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(response))));
    expect(await employeeOrganizationApi.graph()).toEqual(response);
  });
  it.each([401, 403, 500])("propagates HTTP %i", async status => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status })));
    await expect(employeeOrganizationApi.graph()).rejects.toMatchObject({ status });
  });
  it.each([null, ""])('rejects invalid identity %s', async id => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ success: true, edges: [], nodes: [{ agent: { id, display_name: "Agent", slug: null, state: "active" }, organization_profile: null, revision: null }] }))));
    await expect(employeeOrganizationApi.graph()).rejects.toThrow("照合");
  });
});
describe("canonical organization setup preserves bound employees", () => {
  it("preserves omitted manager/model/lifecycle and never adds a grant", () => {
    expect(parseOrganizationManifest(JSON.stringify({ employees: [existing] }), graph, catalog)).toEqual({ employees: [existing] });
    expect(() => parseOrganizationManifest(JSON.stringify({ employees: [{ ...existing, revision: { unsupported: true } }] }), graph, catalog)).toThrow("職務");
    expect(() => parseOrganizationManifest(JSON.stringify({ employees: [{ ...existing, project_grants: [] }] }), graph, catalog)).toThrow("正規");
  });
  it("shows explicit new model configuration and rejects unknown references and cycles", () => {
    const worker = { key: "worker", slug: "new-worker", display_name: "制作担当", manager_key: "director", revision: { display_name: "制作担当", mission: "根拠から制作", operational_instructions: "許可済み資料だけ", responsibility_summary: "制作", model_overrides: {}, tool_ids: [], delegate_policy: { agents: [], workers: [] } }, activate: true };
    expect(parseOrganizationManifest(JSON.stringify({ employees: [existing, worker] }), graph, catalog).employees[1]).toEqual(worker);
    expect(() => parseOrganizationManifest(JSON.stringify({ employees: [{ ...existing, agent_id: "foreign" }] }), graph, catalog)).toThrow("照合");
    expect(() => parseOrganizationManifest(JSON.stringify({ employees: [{ ...existing, manager_key: "worker" }, worker] }), graph, catalog)).toThrow("循環");
  });
  it("requires same IDs and actual manager/model readback and snapshots names", () => {
    const body = { employees: [{ ...existing, manager_key: null }] };
    const response = { success: true, ready: true, employees: { director: { id: "director" } }, blockers: [] };
    expect(organizationSetupReadback(body, response, graph)).toBe(false);
    expect(organizationSetupReadback(body, response, { ...graph, nodes: [{ ...graph.nodes[0], organization_profile: { manager_agent_id: null } }] })).toBe(true);
    expect(organizationSetupReadback(body, { ...response, employees: { director: { id: "foreign" } } }, graph)).toBe(false);
    expect(organizationSnapshot({ ...graph, nodes: [{ ...graph.nodes[0], agent: { ...graph.nodes[0].agent, display_name: "別名" } }] })).not.toBe(organizationSnapshot(graph));
  });
});
describe("owner-bound read-only organization reconciliation", () => {
  vi.stubGlobal("crypto", webcrypto);
  const before: OrganizationGraph = { ...graph, nodes: [{ ...graph.nodes[0], organization_profile: { agent_id: "director", job_title: "統括", responsibility_summary: "既存責務", manager_agent_id: "old-manager", manager_user_id: null, primary_space_id: "space", autonomy_level: "supervised", employment_state: "active", company_permission_ceiling: { capabilities: ["workspace_read"] }, created_at: "original", updated_at: "before" }, revision: { id: "rev", legacy: { agent_team_id: "team", execution_profile_id: "existing-profile" }, mission: "既存本文", capability_ceiling: ["workspace_read"], wake_policy: { mode: "manual" }, budget_policy: { currency: "JPY" } } }] };
  const body = { employees: [{ ...existing, manager_key: null, job_title: "新しい役職" }] };
  const saved = { ...before, nodes: [{ ...before.nodes[0], organization_profile: { ...before.nodes[0].organization_profile, job_title: "新しい役職", manager_agent_id: null, updated_at: "after" } }] };
  it("retains ID-based pending proof for an agent without a slug", async () => {
    const historical = { ...before, nodes: [{ ...before.nodes[0], agent: { ...before.nodes[0].agent, slug: null } }] };
    const proof = await createOrganizationPending(body, historical, "owner");
    expect(readOrganizationPending(JSON.stringify(proof), "owner").employees[0].id).toBe("director");
  });
  it("requires original manifest and owner, preserves full profile/revision, accepts only complete readback", async () => {
    const proof = await createOrganizationPending(body, before, "owner");
    expect(JSON.stringify(proof)).not.toContain("既存本文");
    expect(readOrganizationPending(JSON.stringify(proof), "owner")).toEqual(proof);
    expect(() => readOrganizationPending(JSON.stringify(proof), "other")).toThrow("owner");
    for (const patch of [{ agentHash: undefined }, { profileFields: {} }, { revisionHash: undefined }, { revisionFields: { agent_team_id: "broken" } }]) expect(() => readOrganizationPending(JSON.stringify({ ...proof, employees: [{ ...proof.employees[0], ...patch }] }), "owner")).toThrow("保全条件");
    expect(await reconcileOrganizationPending(proof, "owner", body, saved)).toBe(true);
    expect(await reconcileOrganizationPending(proof, "other", body, saved)).toBe(false);
    expect(await reconcileOrganizationPending(proof, "owner", { employees: [existing] }, saved)).toBe(false);
    expect(await reconcileOrganizationPending(proof, "owner", body, before)).toBe(false);
    for (const field of ["primary_space_id", "company_permission_ceiling", "responsibility_summary", "employment_state"]) expect(await reconcileOrganizationPending(proof, "owner", body, { ...saved, nodes: [{ ...saved.nodes[0], organization_profile: { ...saved.nodes[0].organization_profile, [field]: "changed" } }] })).toBe(false);
    expect(await reconcileOrganizationPending(proof, "owner", body, { ...saved, nodes: [{ ...saved.nodes[0], revision: { ...saved.nodes[0].revision, mission: "changed" } }] })).toBe(false);
  });
  it("accepts preview-only canonical export without adding defaults and rejects applied results/extra data", () => {
    const source = JSON.stringify({ applied: false, manifest: { employees: [existing] } });
    expect(importOrganizationPreview(source, before, catalog, undefined, "owner")).toEqual({ employees: [existing] });
    expect(() => importOrganizationPreview(JSON.stringify({ applied: true, manifest: { employees: [existing] } }), before, catalog, undefined, "owner")).toThrow("preview");
    expect(() => importOrganizationPreview(JSON.stringify({ applied: false, manifest: { employees: [existing] }, grants: [] }), before, catalog, undefined, "owner")).toThrow("未確認");
    expect(() => importOrganizationPreview(JSON.stringify({ applied: false, manifest: { employees: [{ ...existing, key: "sns-brand-offby1" }] }, brand_bindings: [] }), before, catalog, undefined, "owner")).toThrow("owner");
  });
});

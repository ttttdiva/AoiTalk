import { webcrypto } from "node:crypto";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BootstrapApiError, BootstrapRunner, canonical, newJournal, parseBootstrapPlan, sha256, stableProjection,
  type ApiCall, type BootstrapAction, type BootstrapPlan, type Json, type ObjectValue } from "./mediaops-bootstrap";

beforeEach(() => { vi.stubGlobal("crypto", webcrypto); });
const OWNER = "11111111-1111-4111-8111-111111111111";
const NODE = "187a18f4-223d-4547-b662-8bca5a9bf320";
const MEDIA = "/api/operations/media";
async function seal(action: Omit<BootstrapAction, "payload_hash" | "idempotency_key">): Promise<BootstrapAction> {
  const key = "mediaops:" + (await sha256(action.key)).slice(0, 56);
  if (action.key.startsWith("agent:") || action.key.startsWith("revision:")) action.payload.idempotency_key = key;
  return { ...action, idempotency_key: key, payload_hash: await sha256(action.payload) };
}
async function reseal(plan: BootstrapPlan): Promise<BootstrapPlan> {
  const rest: Record<string, unknown> = { ...plan }; delete rest.plan_hash; return { ...plan, plan_hash: await sha256(rest) };
}
async function fixture(): Promise<BootstrapPlan> {
  const actions = [await seal({ key: "source:example", endpoint: "/api/docs/nodes", payload: { id: NODE, title: "Source", description: "hash:source", node_type: "node", body_json: {} },
    read_path: "/api/docs/nodes/{id}", unwrap: "node", reuse: { path: `/api/docs/nodes/${NODE}`, unwrap: "node", match: { description: "hash:source" } }, rollback: null }),
    await seal({ key: "agent:director", endpoint: "/api/agents", payload: { display_name: "Media Director", slug: "mediaops-director" }, read_path: "/api/agents/{id}", unwrap: "agent",
      reuse: { path: "/api/agents?limit=200", unwrap: "agents", list_match: { slug: "mediaops-director" } }, rollback: null }),
    await seal({ key: "automation:bootstrap-research", endpoint: MEDIA + "/automations", payload: { name: "Safe research", enabled: false, execution_mode: "research_only", generation_action: {} },
      read_path: MEDIA + "/automations/{id}", unwrap: null, reuse: null, rollback: null })];
  return reseal({ schema: "mediaops-bootstrap/v1", scope_key: "test", expected_owner_user_id: OWNER, mode: "research_only", role_bindings: {}, requested_image_model: "GPT Image2.5 Sunburst",
    model_substitution_allowed: false, actions, blocked: [], plan_hash: "" });
}
function fakeApi() {
  const rows = new Map<string, ObjectValue>();
  const idempotency = new Map<string, Json>();
  const runs = new Map<string, ObjectValue>();
  let owner = OWNER;
  let failAfterCommit = false;
  const api = vi.fn<ApiCall>(async (method, path, payload, key) => {
    if (path === "/api/auth/status") return { authenticated: true, user: { id: owner } };
    if (path === "/api/agents?limit=200") return { agents: [...rows].filter(([p]) => p.startsWith("/api/agents/")).map(([, v]) => v) };
    if (method === "GET") {
      const row = rows.get(path);
      if (!row) throw new BootstrapApiError(404, path);
      return path.startsWith("/api/docs/") ? { node: structuredClone(row) } : path.startsWith("/api/agents/") ? { agent: structuredClone(row) } : structuredClone(row);
    }
    if (method === "PATCH") {
      const base = path.slice(0, path.lastIndexOf("/")); const row = rows.get(base)!;
      if (path.endsWith("/state") && row.state !== "active") throw new BootstrapApiError(409, path);
      Object.assign(row, payload); return { agent: row };
    }
    if (path.endsWith("/runs")) {
      const trigger = String((payload as ObjectValue).trigger_key);
      let row = runs.get(trigger);
      if (!row) {
        row = { id: "run-1", state: "complete" }; runs.set(trigger, row); rows.set(MEDIA + "/automations/runs/run-1", row);
        const program = rows.get(path.slice(0, -5)); if (program) program.latest_run = row;
      }
      return row;
    }
    if (key && idempotency.has(key)) return structuredClone(idempotency.get(key)!);
    const row: ObjectValue = { ...(payload as ObjectValue), id: (payload as ObjectValue).id || `id-${rows.size}`, owner_user_id: owner, created_by: owner };
    if (path === "/api/agents") row.state = "draft";
    if (path.endsWith("automations")) { row.current_revision = payload!; row.latest_run = null; }
    rows.set(path + "/" + row.id, row);
    const out = path === "/api/docs/nodes" ? { node: row } : path === "/api/agents" ? { agent: row } : row;
    if (key) idempotency.set(key, structuredClone(out));
    if (failAfterCommit) { failAfterCommit = false; throw new Error("response lost"); }
    return structuredClone(out);
  });
  return { api, rows, runs, changeOwner: () => { owner = "wrong-owner"; }, failAfterCommit: () => { failAfterCommit = true; } };
}

describe("closed MediaOps bootstrap", () => {
  it("validates canonical checksums and normal source text", async () => {
    const plan = await fixture(); expect((await parseBootstrapPlan(JSON.stringify(plan))).plan_hash).toBe(plan.plan_hash);
    expect(canonical({ b: 2, a: "原文" })).toBe('{"a":"原文","b":2}');
  });
  it.each([undefined, null, "not-an-array", [null]])("rejects malformed blocked metadata %s", async blocked => {
    const plan = await fixture();
    const raw = JSON.parse(JSON.stringify(plan));
    if (blocked === undefined) delete raw.blocked; else raw.blocked = blocked;
    delete raw.plan_hash; raw.plan_hash = await sha256(raw);
    await expect(parseBootstrapPlan(JSON.stringify(raw))).rejects.toThrow("未解決項目");
  });
  it("rejects malformed role binding metadata", async () => {
    const raw = JSON.parse(JSON.stringify(await fixture())); raw.role_bindings = ["invalid"];
    delete raw.plan_hash; raw.plan_hash = await sha256(raw);
    await expect(parseBootstrapPlan(JSON.stringify(raw))).rejects.toThrow("binding");
  });
  it("rejects altered hashes before requests", async () => {
    const plan = await fixture(); plan.scope_key = "changed";
    await expect(parseBootstrapPlan(JSON.stringify(plan))).rejects.toThrow("ハッシュ");
  });
  it.each(["/api/auth/long-lived-tokens", "/api/settings", "/api/agents/id/project-grants", "/api/operations/actions"])("rejects rehashed forbidden endpoint %s", async endpoint => {
    const plan = await fixture(); plan.actions[0].endpoint = endpoint;
    await expect(parseBootstrapPlan(JSON.stringify(await reseal(plan)))).rejects.toThrow("API");
  });
  it("rejects authority principal, source parent injection and unsupported generation", async () => {
    const plan = await fixture(); plan.actions[0].payload.owner_user_id = OWNER; plan.actions[0].payload_hash = await sha256(plan.actions[0].payload);
    await expect(parseBootstrapPlan(JSON.stringify(await reseal(plan)))).rejects.toThrow("権限");
    const another = await fixture(); another.actions[2].payload.execution_mode = "auto_generate"; another.actions[2].payload_hash = await sha256(another.actions[2].payload);
    await expect(parseBootstrapPlan(JSON.stringify(await reseal(another)))).rejects.toThrow("生成");
  });
  it("preview and owner mismatch never POST", async () => {
    const plan = await fixture(); const remote = fakeApi(); const journal = newJournal(plan);
    expect((await new BootstrapRunner(plan, journal, remote.api).run("preview"))).toHaveLength(3);
    expect(remote.api.mock.calls.every(([m]) => m === "GET")).toBe(true);
    remote.changeOwner(); await expect(new BootstrapRunner(plan, journal, remote.api).run("apply")).rejects.toThrow("owner");
    expect(remote.rows.size).toBe(0);
  });
  it("apply, fresh runner readback and replay keep identities", async () => {
    const plan = await fixture(); const remote = fakeApi(); const journal = newJournal(plan);
    await new BootstrapRunner(plan, journal, remote.api).run("apply"); const count = remote.rows.size;
    const result = await new BootstrapRunner(plan, journal, remote.api).run("apply");
    expect(result.every(x => x.status === "verified")).toBe(true); expect(remote.rows.size).toBe(count);
  });
  it("persists unresolved intent and refuses changed-plan retries", async () => {
    const plan = await fixture(); const remote = fakeApi(); const journal = newJournal(plan); remote.failAfterCommit();
    await expect(new BootstrapRunner(plan, journal, remote.api).run("apply")).rejects.toThrow("response lost");
    expect(journal.pending?.payload_hash).toBe(plan.actions[0].payload_hash);
    const changed = structuredClone(plan); changed.actions[0].payload_hash = "f".repeat(64);
    await expect(new BootstrapRunner(changed, journal, remote.api).run("apply")).rejects.toThrow("未照合");
    await new BootstrapRunner(plan, journal, remote.api).run("apply"); expect(remote.rows.size).toBe(3);
  });
  it("checks complete Docs body/parent before establishing readback hash", async () => {
    const plan = await fixture(); const remote = fakeApi(); const journal = newJournal(plan);
    await new BootstrapRunner(plan, journal, remote.api).run("apply");
    remote.rows.get(`/api/docs/nodes/${NODE}`)!.title = "Unrelated";
    await expect(new BootstrapRunner(plan, newJournal(plan), remote.api).run("apply")).rejects.toThrow("Docs");
  });
  it("does not reuse another creator or an implicit active agent", async () => {
    const plan = await fixture(); const remote = fakeApi(); const journal = newJournal(plan);
    await new BootstrapRunner(plan, journal, remote.api).run("apply");
    const agent = remote.rows.get("/api/agents/id-1")!; agent.state = "active";
    await expect(new BootstrapRunner(plan, newJournal(plan), remote.api).run("apply")).rejects.toThrow("binding");
    agent.state = "draft"; agent.created_by = "someone-else";
    await expect(new BootstrapRunner(plan, newJournal(plan), remote.api).run("apply")).rejects.toThrow("作成者");
  });
  it("pausing skips non-executable drafts and never deletes evidence", async () => {
    const plan = await fixture(); const remote = fakeApi(); const journal = newJournal(plan);
    const runner = new BootstrapRunner(plan, journal, remote.api); await runner.run("apply");
    expect(await runner.pauseCreated()).toBe(0);
    expect(remote.api.mock.calls.some(([m]) => m === "PATCH")).toBe(false);
  });
  it("initial run is separately triggered and latest_run does not invalidate its revision", async () => {
    const plan = await fixture(); const remote = fakeApi(); const journal = newJournal(plan);
    const runner = new BootstrapRunner(plan, journal, remote.api); await runner.run("apply");
    const first = await runner.initialRun(); const second = await runner.initialRun();
    expect(first.id).toBe(second.id); expect(remote.runs.size).toBe(1);
    await new BootstrapRunner(plan, journal, remote.api).run("readback");
  });
  it("finds an older finding beyond the first page", async () => {
    const plan = await fixture(); const action = await seal({ key: "evidence:youtube:E0", endpoint: [MEDIA + "/research-runs/", { $ref: "import-run:youtube", path: ["id"] }, "/findings"],
      payload: {}, read_path: [MEDIA + "/research-runs/", { $ref: "import-run:youtube", path: ["id"] }, "/findings?limit=100"], unwrap: "find_by_id", reuse: null, rollback: null });
    const api = vi.fn<ApiCall>(async (_m, path) => path.endsWith("offset=0") ? Array.from({ length: 100 }, (_, i) => ({ id: `other-${i}` })) : [{ id: "target", owner_user_id: OWNER }]);
    const runner = new BootstrapRunner(plan, newJournal(plan), api); runner.results["import-run:youtube"] = { id: "run" };
    expect((await runner.read(action, "target")).id).toBe("target"); expect(api).toHaveBeenCalledTimes(2);
  });
  it("hashes immutable content independently of graph expansions", () => {
    expect(stableProjection({ id: "1", latest_run: { id: "r" }, findings: [] })).toEqual({ id: "1" });
  });
});


it("rejects a revision without an exact corresponding model binding", async () => {
  const plan = await fixture();
  const revision = await seal({ key: "revision:director", endpoint: ["/api/agents/", { $ref: "agent:director", path: ["id"] }, "/revisions"],
    payload: { display_name: "Director", agent_team_id: "team", execution_profile_id: "profile", allowed_subagent_ids: ["worker"] },
    read_path: "/api/agent-revisions/{id}", unwrap: "revision", reuse: null, rollback: null });
  plan.actions.push(revision);
  await expect(parseBootstrapPlan(JSON.stringify(await reseal(plan)))).rejects.toThrow("binding");
  plan.role_bindings.director = { agent_team_id: "team", execution_profile_id: "different-profile", allowed_subagent_ids: ["worker"], resolved_provider: "codex-cli", resolved_model_id: "example-model" };
  await expect(parseBootstrapPlan(JSON.stringify(await reseal(plan)))).rejects.toThrow("binding");
});


it.each([undefined, null, 1, {}, []])("rejects typed Docs child with invalid label %s", async label => {
  const plan = await fixture();
  const body: ObjectValue = { format: "doc_block", block_type: "code", language: "text", content: "原文\n" };
  if (label !== undefined) body.label = label;
  const action = await seal({ key: "source-content:example", endpoint: "/api/docs/nodes", payload: {
    id: NODE, parent_id: NODE, title: "原文", node_type: "node", body_json: body,
  }, read_path: "/api/docs/nodes/{id}", unwrap: "node", reuse: null, rollback: null });
  plan.actions = [action];
  await expect(parseBootstrapPlan(JSON.stringify(await reseal(plan)))).rejects.toThrow("Docs原文");
  body.label = "原文";
  action.payload_hash = await sha256(action.payload);
  await expect(parseBootstrapPlan(JSON.stringify(await reseal(plan)))).resolves.toMatchObject({ schema: "mediaops-bootstrap/v1" });
});

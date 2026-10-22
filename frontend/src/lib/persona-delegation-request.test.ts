import { describe, expect, it, vi } from "vitest";
import { webcrypto, createHash } from "node:crypto";
import { delegationContentHash, delegationEmployeesUnchanged, delegationReceiptMatches, delegationRequestBody, createPersonaDelegation, DelegationRequestError } from "./persona-delegation-request";
import type { ContentItem } from "./media-operations-research-api";
import type { components } from "./api-types.gen";
const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "persona-revision", displayName: "OFFBY1" };
const brief = { id: "brief", version: 3, content_hash: "a".repeat(64), owner_user_id: "owner", project_id: "project", persona_revision_id: "persona-revision", title: "本人の企画", brief: "本人入力\n研究とは区別する", input_kind: "user_authored", objective: "制作", status: "draft" } as ContentItem;
const manager = { id: "manager", revisionId: "manager-rev", name: "統括" }, worker = { id: "worker", revisionId: "worker-rev", name: "制作" }, reviewer = { id: "reviewer", revisionId: "reviewer-rev", name: "review" };
const body = () => delegationRequestBody(scope, brief, manager, worker, reviewer, "writer", 2048, 16384, "explicit-key");
describe("brief-bound manager request", () => {
  vi.stubGlobal("crypto", webcrypto);
  it("pins provided input and bounded planning without broad scope or authority grants", async () => {
    const request = body(); expect(request.instruction).toContain(brief.brief); expect(request.instruction).toContain(brief.content_hash); expect(request.instruction).toContain('"input_kind":"user_authored"');
    expect(request.planning?.roster).toHaveLength(2); expect(request.planning?.brand_keys).toEqual([]); expect(request.planning?.max_depth).toBe(1); expect(request.planning?.max_total_children).toBe(2);
    expect(() => delegationRequestBody(scope, { ...brief, owner_user_id: "foreign" }, manager, worker, reviewer, "writer", 2048, 16384, "key")).toThrow("同じ本人");
    expect(() => delegationRequestBody(scope, brief, manager, worker, worker, "writer", 2048, 16384, "key")).toThrow("3Agent");
    expect(() => delegationRequestBody(scope, brief, manager, worker, reviewer, "writer", 8193, 16384, "key")).toThrow("上限");
    const hash = await delegationContentHash(request, "owner"); expect(hash).toMatch(/^[a-f0-9]{64}$/); expect(await delegationContentHash({ ...request, planning: { ...request.planning!, max_total_output_tokens: 8192 } }, "owner")).not.toBe(hash);
    const serverInput = { ...request, persona_id: "persona", parent_delegation_id: null, linked_work_item_id: null, created_by: "owner", linked_work_binding: {} } as Record<string, unknown>; delete serverInput.idempotency_key;
    const canonical = JSON.stringify(serverInput, (_key, value) => value && typeof value === "object" && !Array.isArray(value) ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) : value);
    expect(hash).toBe(createHash("sha256").update(canonical).digest("hex"));
  });
  it("rejects changed revision and mismatched receipt scope/budget/hash", async () => {
    const request = body(); const graph = { success: true as const, edges: [], nodes: [manager, worker, reviewer].map(e => ({ agent: { id: e.id, slug: e.id, display_name: e.name, state: "active" as const }, revision: { id: e.revisionId }, organization_profile: null })) };
    expect(delegationEmployeesUnchanged(request, graph)).toBe(true); graph.nodes[0].revision.id = "changed"; expect(delegationEmployeesUnchanged(request, graph)).toBe(false);
    const response = { success: true, delegation: { ...request, id: "saved", created_by: "owner", content_hash: await delegationContentHash(request, "owner"), planning: { enabled: true, max_total_output_tokens: 16384 } } } as unknown as components["schemas"]["DelegationResponse"];
    expect(delegationReceiptMatches(request, "owner", response)).toBe(true);
    for (const patch of [{ created_by: "foreign" }, { persona_id: "foreign" }, { content_hash: "unknown" }, { planning: { enabled: true, max_total_output_tokens: 1 } }]) expect(delegationReceiptMatches(request, "owner", { ...response, delegation: { ...response.delegation, ...patch } })).toBe(false);
  });
  it("retains only canonical failure codes and never retries the POST", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 403, json: async () => ({ detail: "delegation_scope_read_denied", secrets: "never-retain" }) }); vi.stubGlobal("fetch", fetch);
    await expect(createPersonaDelegation(body())).rejects.toMatchObject({ status: 403, code: "delegation_scope_read_denied" }); expect(fetch).toHaveBeenCalledTimes(1);
    fetch.mockResolvedValue({ ok: false, status: 422, json: async () => ({ detail: "echoed credential: private" }) });
    await expect(createPersonaDelegation(body())).rejects.toMatchObject({ status: 422, code: undefined });
    fetch.mockRejectedValue(new Error("untrusted network body")); await expect(createPersonaDelegation(body())).rejects.toBeInstanceOf(DelegationRequestError);
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";
import { emptyGraph, teamGraphApi } from "./agent-team-graph-api";

afterEach(() => vi.unstubAllGlobals());
describe("Agent Team graph API", () => {
  it("sends the revision CAS through the authenticated proxy", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ graph: { id: "g" } }) });
    vi.stubGlobal("fetch", fetcher);
    const body = { title: "チーム", project_id: null, definition: emptyGraph(), expected_revision: 4 };
    await teamGraphApi.update("g/1", body);
    expect(fetcher).toHaveBeenCalledWith("/api/python-proxy/agent-team-graphs/g%2F1", expect.objectContaining({ method: "PUT", credentials: "include", body: JSON.stringify(body) }));
  });
  it("uses the proxy and exact default CAS JSON", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ available: false, configuration: null }) });
    vi.stubGlobal("fetch", fetcher);
    await teamGraphApi.getDefault();
    const body = { graph_id: "g", revision_id: "r", expected: { graph_id: "old", revision_id: "rev", content_hash: "hash" } };
    await teamGraphApi.setDefault(body);
    expect(fetcher).toHaveBeenNthCalledWith(1, "/api/python-proxy/agent-team-graphs/default/configuration", expect.objectContaining({ method: "GET", credentials: "include" }));
    expect(fetcher).toHaveBeenNthCalledWith(2, "/api/python-proxy/agent-team-graphs/default/configuration", expect.objectContaining({ method: "PUT", credentials: "include", body: JSON.stringify(body) }));
  });
  it("rejects a default response whose configuration is unknown", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ available: true }) }));
    await expect(teamGraphApi.getDefault()).rejects.toThrow("既定の応答を確認できません");
  });
  it.each([{ available: true, configuration: null }, { available: false, configuration: { graph_id: "g", revision_id: "r", content_hash: "h" } }, { available: true, configuration: {} }, { configuration: null }])("rejects malformed default responses", async response => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => response }));
    await expect(teamGraphApi.getDefault()).rejects.toThrow("既定の応答を確認できません");
  });
  it("accepts an unavailable null default", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ available: false, configuration: null }) }));
    await expect(teamGraphApi.getDefault()).resolves.toEqual({ available: false, configuration: null });
  });
  it("accepts an available valid reference", async () => {
    const configuration = { graph_id: "11111111-1111-4111-8111-111111111111", revision_id: "22222222-2222-4222-8222-222222222222", content_hash: "a".repeat(64) };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ available: true, configuration }) }));
    await expect(teamGraphApi.getDefault()).resolves.toEqual({ available: true, configuration });
  });
  it("surfaces stale revision conflicts without interpreting them as success", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 409 }));
    await expect(teamGraphApi.update("g", { title: "T", project_id: null, definition: emptyGraph(), expected_revision: 1 })).rejects.toMatchObject({ status: 409, message: expect.stringContaining("競合") });
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  conversationAgentApi,
  unwrapAgentConversations,
  unwrapAgentRuns,
} from "./conversation-agent-api";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
describe("conversationAgentApi", () => {
  it("lists session-scoped Agent candidates without sending a manifest", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ agents: [{ id: "agent-1", display_name: "調査Agent" }] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await conversationAgentApi.list({
      sessionId: "session-1",
      projectId: "project-1",
    });

    expect(response.agents[0]?.id).toBe("agent-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/python-proxy/conversation-agents?session_id=session-1&project_id=project-1",
      expect.objectContaining({ credentials: "include", cache: "no-store" }),
    );
    expect(JSON.stringify(fetchMock.mock.calls[0]?.[1])).not.toContain("manifest");
  });

  it("binds with an explicit CAS token and preserves clearing as null", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ success: true, session: { id: "session-1", agent_id: null }, binding: null }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await conversationAgentApi.bindSessionAgent("session-1", null, "agent-old");

    const [path, init] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe("/api/conversations/session-1/agent");
    expect(init?.method).toBe("PATCH");
    expect(JSON.parse(String(init?.body))).toEqual({
      agent_id: null,
      expected_agent_id: "agent-old",
    });
  });

  it("surfaces busy/stale conflicts and normalizes related activity envelopes", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: { code: "agent_binding_busy" } }), {
        status: 409,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      conversationAgentApi.bindSessionAgent("session-1", "agent-2", "agent-1"),
    ).rejects.toMatchObject({
      status: 409,
      code: "agent_binding_busy",
    });
    expect(unwrapAgentConversations({ sessions: [{ id: "session-1" } as never] })).toHaveLength(1);
    expect(unwrapAgentRuns({ runs: [{ id: "run-1", run_type: "chat", status: "succeeded" }] })).toHaveLength(1);
  });
});


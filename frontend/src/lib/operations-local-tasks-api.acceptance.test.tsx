import { beforeEach, describe, expect, it, vi } from "vitest";
import { operationsLocalTasksApi as api, readingTaskBody, parsePendingLocalTask, type LocalTaskInput } from "./operations-local-tasks-api";
const input: LocalTaskInput = { goal: "Read a known marker", workspace: "/allowed/proof", completion_criteria: ["Report the marker"], max_tool_rounds: 4, timeout_sec: 120 };
beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
describe("Local reading task boundary", () => {
  it("requires an explicit Codex route for the profile and preserves that exact pending body", () => {
    const profile = { ...input, route: { provider: "codex-cli", model: "gpt-5.6-sol", effort: "high" }, codex_connection: "app_profile" as const };
    const body = readingTaskBody(profile);
    expect(body.codex_connection).toBe("app_profile");
    expect(body.allow_commands).toBe(false);
    const pending = { owner: "owner-a", key: "receipt_123", input: profile, body };
    expect(parsePendingLocalTask(JSON.stringify(pending), "owner-a")).toEqual(pending);
    expect(() => readingTaskBody({ ...input, codex_connection: "app_profile" })).toThrow();
    expect(() => readingTaskBody({ ...profile, route: { provider: "openai_compatible_local", model: "local" } })).toThrow();
    expect(() => parsePendingLocalTask(JSON.stringify({ ...pending, body: { ...body, codex_connection: "host" } }), "owner-a")).toThrow();
  });
  it("fixes mutation capabilities off and omits any route for the existing API", () => {
    const body = readingTaskBody(input);
    expect(body).toMatchObject({ mode: "read_only", allow_commands: false, allow_adb: false, allow_git_commit: false });
    expect(body).not.toHaveProperty("provider"); expect(body).not.toHaveProperty("model"); expect(body).not.toHaveProperty("effort");
  });
  it.each([{ goal: "" }, { workspace: "" }, { max_tool_rounds: 0 }, { timeout_sec: 90000 }, { completion_criteria: ["x".repeat(513)] }])("rejects invalid bounded input %j", override => {
    expect(() => readingTaskBody({ ...input, ...override })).toThrow();
  });
  it("restores only the matching owner's identical intent", () => {
    const pending = { owner: "owner-a", key: "receipt_123", input, body: readingTaskBody(input) };
    expect(parsePendingLocalTask(JSON.stringify(pending), "owner-a")).toEqual(pending);
    expect(() => parsePendingLocalTask(JSON.stringify(pending), "owner-b")).toThrow();
    expect(() => parsePendingLocalTask(JSON.stringify({ ...pending, body: { ...pending.body, mode: "mutate" } }), "owner-a")).toThrow();
  });
  it("accepts a valid maximum-size draft on reload", () => {
    const large = { ...input, goal: "g".repeat(8000), completion_criteria: Array(20).fill("x".repeat(512)) };
    const pending = { owner: "owner-a", key: "receipt_123", input: large, body: readingTaskBody(large) };
    expect(parsePendingLocalTask(JSON.stringify(pending), "owner-a")).toEqual(pending);
  });
  it("uses the normal cookie proxy and the supplied idempotency key", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ task: { task_id: "a".repeat(32) }, created: true }), { status: 202 }));
    await api.create(readingTaskBody(input), "receipt_123");
    expect(fetch).toHaveBeenCalledWith("/api/python-proxy/api/local-agent/tasks", expect.objectContaining({ credentials: "include", method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": "receipt_123" } }));
    expect(vi.mocked(fetch).mock.calls[0][1]).not.toHaveProperty("Authorization");
  });
  it("rejects a path injection before any request", () => {
    expect(() => api.get("../settings")).toThrow(); expect(fetch).not.toHaveBeenCalled();
  });
  it("preserves backend denial instead of a successful empty result", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ detail: "administrator required" }), { status: 403 }));
    await expect(api.list()).rejects.toMatchObject({ status: 403, message: "administrator required" });
  });
});

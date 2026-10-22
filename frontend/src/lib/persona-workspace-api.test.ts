import { expect, it, vi } from "vitest";
import { personaWorkspaceApi } from "./persona-workspace-api";
const mock = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("./agent-employees-api", () => ({ employeeRequest: mock.request }));
it("turns incomplete successful organization/delegation responses into scoped fetch errors", async () => {
  mock.request.mockResolvedValue({});
  await expect(personaWorkspaceApi.organization()).rejects.toThrow("組織の応答");
  await expect(personaWorkspaceApi.delegations()).rejects.toThrow("委託の応答");
  await expect(personaWorkspaceApi.team("project")).rejects.toThrow("担当Agentと仕事の応答");
});
it("accepts empty authorized organization data and rejects malformed nested delegations", async () => {
  mock.request.mockResolvedValue({ success: true, nodes: [], edges: [] });
  await expect(personaWorkspaceApi.organization()).resolves.toMatchObject({ nodes: [], edges: [] });
  mock.request.mockResolvedValue({ success: true, delegations: [{ id: "delegation", phases: {}, artifacts: {} }] });
  await expect(personaWorkspaceApi.delegations()).rejects.toThrow("委託の応答");
});

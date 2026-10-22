// @vitest-environment jsdom
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MediaAutomationPanel } from "./automation-panel";
import { mediaGenerationApi } from "@/lib/media-operations-generation-api";
import { mediaResearchApi } from "@/lib/media-operations-research-api";

vi.mock("@/lib/media-operations-generation-api", () => ({
  mediaGenerationApi: { listWorkspaces: vi.fn() },
}));
vi.mock("@/lib/media-operations-research-api", () => ({
  mediaResearchApi: { listRoutines: vi.fn() },
}));

const workspaceId = "11111111-1111-4111-8111-111111111111";
const program = {
  id: "22222222-2222-4222-8222-222222222222",
  name: "Fixture automation",
  enabled: true,
  current_revision: {
    id: "33333333-3333-4333-8333-333333333333",
    program_id: "22222222-2222-4222-8222-222222222222",
    version: 1,
    execution_mode: "review_before_generate",
    trigger: { type: "manual" },
    discovery: { type: "manual", theme: "Fixture" },
    research_binding: {},
    planning_policy: { candidate_count: 3 },
    generation_action: {
      kind: "image",
      studio_workspace_binding_id: workspaceId,
      preset_id: "pst_fixture01",
      preset_revision_policy: "latest_at_submission",
    },
    fallback: {},
    content_hash: "a".repeat(64),
  },
  revisions: [],
  latest_run: null,
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

function stubFetch(presets: () => Response) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (input: string) => {
      calls.push(input);
      if (input.includes("/generation-studio/")) return presets();
      if (input.includes("/automations/runs/list")) return json([]);
      if (input.endsWith("/operations/media/automations")) return json([program]);
      return json({ detail: "unexpected" }, 404);
    }),
  );
  return calls;
}

describe("MediaAutomationPanel Generation Studio binding", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(mediaGenerationApi.listWorkspaces).mockResolvedValue([
      {
        id: workspaceId,
        external_workspace_id: "wsp_fixture01",
        status: "verified",
        base_url: "http://127.0.0.1:8000",
      },
    ] as never);
    vi.mocked(mediaResearchApi.listRoutines).mockResolvedValue([]);
  });

  it("shows an authentication failure instead of an empty Preset list", async () => {
    const calls = stubFetch(() => json({ detail: "studio_auth_failed" }, 503));
    render(<MediaAutomationPanel />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Generation Studioの認証に失敗しました");
    expect(alert).not.toHaveTextContent("studio_auth_failed");
    expect(calls.some((url) => url.includes(`/generation-studio/${workspaceId}/presets`))).toBe(true);
  });

  it("shows an unavailable Studio as an error", async () => {
    stubFetch(() => json({ detail: "studio_unavailable" }, 503));
    render(<MediaAutomationPanel />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Generation StudioからPresetを取得できません",
    );
  });

  it("keeps a successful empty catalog silent", async () => {
    const calls = stubFetch(() => json([]));
    render(<MediaAutomationPanel />);
    await waitFor(() =>
      expect(calls.some((url) => url.includes("/generation-studio/"))).toBe(true),
    );
    await screen.findByText("Generation Studio binding");
    expect(screen.getByLabelText("Agent ID（heartbeat時）")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

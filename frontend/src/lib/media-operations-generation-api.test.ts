import { afterEach, describe, expect, it, vi } from "vitest";

import { mediaGenerationApi } from "@/lib/media-operations-generation-api";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("mediaGenerationApi", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("creates a semantic GenerationPlan through the Python proxy", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ id: "plan-id" }));
    vi.stubGlobal("fetch", fetchMock);

    await mediaGenerationApi.createPlan(
      {
        persona_revision_id: "persona-revision-id",
        creative_recipe_revision_id: "recipe-revision-id",
        workspace_id: "workspace-id",
        requested_outputs: 2,
        request_spec: {
          prompt: "a quiet room",
          negative_prompt: "",
          seed: 42,
          image_model_selection_id: "ims_default1234",
          generation_settings: { quality: "standard" },
          size_preset_id: "normal_square",
          width: null,
          height: null,
          accept_metered_generation: false,
          aspect_ratio: "1:1",
        },
      },
      "plan-key",
    );

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/python-proxy/operations/media/generation-plans");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("idempotency-key")).toBe("plan-key");
    expect(JSON.parse(String(init.body))).toEqual({
      persona_revision_id: "persona-revision-id",
      creative_recipe_revision_id: "recipe-revision-id",
      workspace_id: "workspace-id",
      requested_outputs: 2,
      request_spec: {
        prompt: "a quiet room",
        negative_prompt: "",
        seed: 42,
        image_model_selection_id: "ims_default1234",
        generation_settings: { quality: "standard" },
        size_preset_id: "normal_square",
        width: null,
        height: null,
        accept_metered_generation: false,
        aspect_ratio: "1:1",
      },
    });
  });

  it("submits the pinned request spec and never contacts a provider URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      response({
        plan: { id: "plan-id", status: "unavailable" },
        intent: { id: "intent-id", plan_id: "plan-id", status: "unavailable" },
        run: null,
        outputs: [],
        status: "unavailable",
        external_idempotency_key: "receipt-key",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const receipt = await mediaGenerationApi.submitPlan(
      "plan-id",
      { expected_plan_hash: "a".repeat(64) },
      "submit-key",
    );
    expect(receipt.run).toBeNull();
    expect(receipt.status).toBe("unavailable");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "/api/python-proxy/operations/media/generation-plans/plan-id/submit",
    );
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("idempotency-key")).toBe("submit-key");

  });

  it("selects an output using the safe run route and an opaque output id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ id: "run-id" }));
    vi.stubGlobal("fetch", fetchMock);

    await mediaGenerationApi.selectOutput("run-id", "output-row-id", "select-key");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "/api/python-proxy/operations/media/generation-runs/run-id/outputs/select",
    );
    expect(JSON.parse(String(init.body))).toEqual({ output_id: "output-row-id" });
    expect(new Headers(init.headers).get("idempotency-key")).toBe("select-key");
  });
});


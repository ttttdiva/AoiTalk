// @vitest-environment jsdom

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MediaGenerationPanel } from "@/components/operations/media/generation-panel";
import { mediaGenerationApi } from "@/lib/media-operations-generation-api";

vi.mock("@/lib/media-operations-generation-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/media-operations-generation-api")>("@/lib/media-operations-generation-api");
  return {
    ...actual,
    mediaGenerationApi: {
      listWorkspaces: vi.fn(),
      createWorkspace: vi.fn(),
      getWorkspace: vi.fn(),
      listRecipes: vi.fn(),
      createRecipe: vi.fn(),
      getRecipe: vi.fn(),
      appendRecipeRevision: vi.fn(),
      listPlans: vi.fn(),
      createPlan: vi.fn(),
      getPlan: vi.fn(),
      submitPlan: vi.fn(),
      reconcilePlan: vi.fn(),
      listRuns: vi.fn(),
      getRun: vi.fn(),
      refreshRun: vi.fn(),
      selectOutput: vi.fn(),
      listImageCatalog: vi.fn(),
      listVideoCatalog: vi.fn(),
    },
  };
});

const workspace = {
  id: "workspace-row",
  provider: "comfyui_workbench",
  external_workspace_id: "wsp_demo1234",
  external_project_id: "prj_demo1234",
  base_url: "https://studio.example.invalid",
  status: "configured",
};

const revision = {
  id: "recipe-revision-row",
  creative_recipe_id: "recipe-row",
  persona_id: "persona-row",
  persona_revision_id: "persona-revision-row",
  version: 3,
  recipe_type: "image",
  image_model_selection_id: "ims_default1234",
  prompt_template: "{{prompt}}",
  reference_asset_ids: [],
  candidate_count: 1,
  content_hash: "a".repeat(64),
};

const recipe = {
  id: "recipe-row",
  persona_id: "persona-row",
  name: "Image recipe",
  current_revision: revision,
};

const videoRevision = {
  ...revision,
  id: "video-recipe-revision-row",
  creative_recipe_id: "video-recipe-row",
  recipe_type: "video",
  model_selection_id: "wsl_video1234",
  image_model_selection_id: undefined,
  duration_seconds: 6,
  frame_count: 24,
  storyboard: ["Shot 1: establish", "Shot 2: close-up"],
};

const videoRecipe = {
  ...recipe,
  id: "video-recipe-row",
  name: "Video recipe",
  current_revision: videoRevision,
};

const plan = {
  id: "plan-row",
  persona_revision_id: "persona-revision-row",
  creative_recipe_revision_id: "recipe-revision-row",
  workspace_id: "workspace-row",
  requested_outputs: 1,
  request_spec: {
    prompt: "a quiet room",
    negative_prompt: "",
    seed: null,
    image_model_selection_id: "ims_default1234",
    generation_settings: {},
    size_preset_id: "normal_square",
    width: null,
    height: null,
    accept_metered_generation: false,
    aspect_ratio: null,
  },
  status: "draft",
};

const unavailableRun = {
  id: "run-row",
  plan_id: "plan-row",
  external_run_id: "run_unavailable1234",
  external_workspace_id: "wsp_demo1234",
  status: "unavailable",
  outputs: [],
};

const meteredPlan = {
  ...plan,
  id: "metered-plan-row",
  request_spec: { ...plan.request_spec, accept_metered_generation: true },
};

const uncertainPlan = {
  ...plan,
  id: "uncertain-plan-row",
  plan_hash: "b".repeat(64),
  status: "uncertain",
};

describe("MediaGenerationPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(mediaGenerationApi.listWorkspaces).mockResolvedValue([workspace] as never);
    vi.mocked(mediaGenerationApi.listRecipes).mockResolvedValue([recipe] as never);
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([]);
    vi.mocked(mediaGenerationApi.listRuns).mockResolvedValue([]);
    vi.mocked(mediaGenerationApi.createPlan).mockResolvedValue(plan as never);
    vi.mocked(mediaGenerationApi.submitPlan).mockResolvedValue({
      plan: { ...plan, status: "unavailable" },
      intent: { id: "intent-row", plan_id: "plan-row", status: "unavailable" },
      run: unavailableRun,
      outputs: [],
      status: "unavailable",
      external_idempotency_key: "receipt-key",
    } as never);
    vi.mocked(mediaGenerationApi.reconcilePlan).mockResolvedValue({
      plan: { ...plan, status: "uncertain" },
      intent: { id: "intent-row", plan_id: "plan-row", status: "uncertain", request_hash: "c".repeat(64) },
      run: null,
      outputs: [],
      status: "uncertain",
      external_idempotency_key: "receipt-key",
    } as never);
  });

  it("creates a Plan pinned to the selected Persona and Recipe revisions", async () => {
    render(<MediaGenerationPanel />);
    await waitFor(() => expect(screen.getByLabelText("GenerationPlan prompt")).toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "GenerationPlanを作成" })).not.toBeDisabled(),
    );

    await userEvent.type(screen.getByLabelText("GenerationPlan prompt"), "a quiet room");
    await userEvent.click(screen.getByRole("button", { name: "GenerationPlanを作成" }));

    await waitFor(() => {
      expect(mediaGenerationApi.createPlan).toHaveBeenCalledWith(
        expect.objectContaining({
          persona_revision_id: "persona-revision-row",
          creative_recipe_revision_id: "recipe-revision-row",
          workspace_id: "workspace-row",
          request_spec: expect.objectContaining({ prompt: "a quiet room" }),
        }),
        expect.any(String),
      );
    });
  });

  it("submits the pinned request once and surfaces an unavailable Run", async () => {
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([plan] as never);
    vi.mocked(mediaGenerationApi.listRuns).mockResolvedValue([unavailableRun] as never);
    render(<MediaGenerationPanel />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "このPlanをsubmit" })).not.toBeDisabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "このPlanをsubmit" }));

    await waitFor(() => {
      expect(mediaGenerationApi.submitPlan).toHaveBeenCalledWith(
        "plan-row",
        { expected_plan_hash: null },
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(screen.getAllByText("run_unavailable1234").length).toBeGreaterThan(0);
    });
  });

  it("does not fabricate a run when the submit receipt has no run", async () => {
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([plan] as never);
    vi.mocked(mediaGenerationApi.listRuns).mockResolvedValue([]);
    vi.mocked(mediaGenerationApi.submitPlan).mockResolvedValue({
      plan: { ...plan, status: "unavailable" },
      intent: { id: "intent-row", plan_id: "plan-row", status: "unavailable" },
      run: null,
      outputs: [],
      status: "unavailable",
      external_idempotency_key: "receipt-key",
    } as never);
    render(<MediaGenerationPanel />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "このPlanをsubmit" })).not.toBeDisabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "このPlanをsubmit" }));

    await waitFor(() => expect(mediaGenerationApi.submitPlan).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByText("Runは作成されませんでした。", { exact: false }))
        .toBeInTheDocument();
    });
    expect(screen.queryByText("run_unavailable1234")).not.toBeInTheDocument();
  });

  it("keeps image requests on the legacy image selection alias", async () => {
    render(<MediaGenerationPanel />);
    await waitFor(() =>
      expect(screen.getByLabelText("Image model selection ID")).toHaveValue(
        "ims_default1234",
      ),
    );
    await userEvent.type(screen.getByLabelText("GenerationPlan prompt"), " image regression");
    await userEvent.click(screen.getByRole("button", { name: "image GenerationPlanを作成" }));
    await waitFor(() => {
      expect(mediaGenerationApi.createPlan).toHaveBeenCalledWith(
        expect.objectContaining({
          request_spec: expect.objectContaining({
            image_model_selection_id: "ims_default1234",
          }),
        }),
        expect.any(String),
      );
    });
  });

  it("renders a video recipe and sends the wsl selection plus storyboard fields", async () => {
    vi.mocked(mediaGenerationApi.listRecipes).mockResolvedValue([videoRecipe] as never);
    render(<MediaGenerationPanel />);
    await waitFor(() =>
      expect(screen.getByLabelText("Video model selection ID")).toHaveValue(
        "wsl_video1234",
      ),
    );
    await userEvent.type(screen.getByLabelText("GenerationPlan prompt"), " a short clip");
    await userEvent.clear(screen.getByLabelText("Video duration seconds"));
    await userEvent.type(screen.getByLabelText("Video duration seconds"), "8");
    await userEvent.clear(screen.getByLabelText("Video storyboard"));
    await userEvent.type(screen.getByLabelText("Video storyboard"), "Shot 1: wide\nShot 2: close");
    await userEvent.click(screen.getByRole("button", { name: "video GenerationPlanを作成" }));
    await waitFor(() => {
      expect(mediaGenerationApi.createPlan).toHaveBeenCalledWith(
        expect.objectContaining({
          request_spec: expect.objectContaining({
            model_selection_id: "wsl_video1234",
            duration_seconds: 8,
            storyboard: ["Shot 1: wide", "Shot 2: close"],
          }),
        }),
        expect.any(String),
      );
    });
  });

  it("requires a human acknowledgement before submitting a metered plan", async () => {
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([meteredPlan] as never);
    render(<MediaGenerationPanel />);
    const submit = await screen.findByRole("button", { name: "このPlanをsubmit" });
    expect(submit).toBeDisabled();
    await userEvent.click(screen.getByLabelText("Generation Studio paid generationを承認"));
    expect(submit).not.toBeDisabled();
    await userEvent.click(submit);
    await waitFor(() => {
      expect(mediaGenerationApi.submitPlan).toHaveBeenCalledWith(
        "metered-plan-row",
        expect.objectContaining({ acknowledge_metered_generation: true }),
        expect.any(String),
      );
    });
  });

  it("fails closed instead of sending an uncertain reconcile request without both hashes", async () => {
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([uncertainPlan] as never);
    render(<MediaGenerationPanel />);
    const reconcile = await screen.findByRole("button", { name: "uncertainをreconcile" });
    expect(reconcile).not.toBeDisabled();
    await userEvent.click(reconcile);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Plan hashとintent request hash"));
    expect(mediaGenerationApi.reconcilePlan).not.toHaveBeenCalled();
  });

  it("reconciles an uncertain receipt only after submit supplied both hashes", async () => {
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([plan] as never);
    vi.mocked(mediaGenerationApi.submitPlan).mockResolvedValue({
      plan: { ...plan, status: "uncertain", plan_hash: "d".repeat(64) },
      intent: { id: "intent-row", plan_id: "plan-row", status: "uncertain", request_hash: "e".repeat(64) },
      run: null,
      outputs: [],
      status: "uncertain",
      external_idempotency_key: "receipt-key",
    } as never);
    render(<MediaGenerationPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "このPlanをsubmit" }));
    const reconcile = await screen.findByRole("button", { name: "uncertainをreconcile" });
    await userEvent.click(reconcile);
    await waitFor(() => {
      expect(mediaGenerationApi.reconcilePlan).toHaveBeenCalledWith(
        "plan-row",
        {
          expected_plan_hash: "d".repeat(64),
          expected_intent_request_hash: "e".repeat(64),
          acknowledge_metered_generation: false,
        },
        expect.any(String),
      );
    });
  });
});





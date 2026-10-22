// @vitest-environment jsdom

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MediaGenerationPanel } from "@/components/operations/media/generation-panel";
import * as basePath from "@/lib/base-path";
import { mediaGenerationApi } from "@/lib/media-operations-generation-api";
import { mediaResearchApi } from "@/lib/media-operations-research-api";

vi.mock("@/lib/media-operations-research-api", async (importOriginal) => ({ ...await importOriginal<typeof import("@/lib/media-operations-research-api")>(), mediaResearchApi: { listContentItems: vi.fn() } }));

vi.mock("@/lib/media-operations-generation-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/media-operations-generation-api")>("@/lib/media-operations-generation-api");
  return {
    ...actual,
    mediaGenerationApi: {
      listWorkspaces: vi.fn(),
      createWorkspace: vi.fn(),
      getWorkspace: vi.fn(),
      retireWorkspace: vi.fn(),
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
  config_hash: "a".repeat(64),
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

const managedImageOutput = {
  id: "output-row",
  external_asset_id: "ast_fixture1234",
  external_output_version: "outv_fixture1234",
  sha256: "c".repeat(64),
  mime_type: "image/png",
  generation_kind: "image",
};

const managedImageRun = {
  ...unavailableRun,
  status: "succeeded",
  outputs: [
    {
      ...managedImageOutput,
      preview_url:
        "/api/operations/media/assets/123e4567-e89b-12d3-a456-426614174000/preview?expected_sha256=" +
        "c".repeat(64),
      deep_link: "https://provider.example.invalid/signed/output.png?token=secret",
    },
  ],
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
    vi.mocked(mediaResearchApi.listContentItems).mockResolvedValue([]);
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

  it("retires only the selected binding hash and keeps saved plans visible", async () => {
    const user = userEvent.setup();
    const retired = { ...workspace, status: "unavailable", config_hash: "f".repeat(64) };
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([plan] as never);
    vi.mocked(mediaGenerationApi.retireWorkspace).mockImplementation(async () => {
      vi.mocked(mediaGenerationApi.listWorkspaces).mockResolvedValue([retired] as never);
      return retired as never;
    });
    render(<MediaGenerationPanel />);
    await user.click(await screen.findByText("制作環境の切り替え"));
    await user.click(screen.getByRole("button", { name: "この制作環境を無効にする" }));
    await waitFor(() => expect(mediaGenerationApi.retireWorkspace).toHaveBeenCalledWith(workspace.id, workspace.config_hash));
    await waitFor(() => expect(screen.getByRole("button", { name: "この制作環境を無効にする" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "GenerationPlanを作成" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "このPlanをsubmit" })).toBeDisabled();
    expect(mediaGenerationApi.submitPlan).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: new RegExp(plan.id) })).toBeInTheDocument();
  });

  it("pins a compatible ContentItem without accepting client research context", async () => {
    const user = userEvent.setup();
    const item = { id: "formal-item", title: "A01-T1", version: 1, status: "draft", persona_revision_id: revision.persona_revision_id, project_id: null, owner_user_id: "owner", brief: "Original task; rights unknown; acceptance not_run" };
    vi.mocked(mediaResearchApi.listContentItems).mockResolvedValue([item, { ...item, id: "foreign", title: "Foreign scope", project_id: "foreign-project" }] as never);
    vi.mocked(mediaGenerationApi.createPlan).mockResolvedValue({ ...plan, content_item_id: item.id, request_spec: { ...plan.request_spec, research_context: { context_hash: "c".repeat(64), brief: { requirements: item.brief } } } } as never);
    render(<MediaGenerationPanel />);
    await waitFor(() => expect(mediaResearchApi.listContentItems).toHaveBeenCalled());
    await user.click(screen.getByLabelText("Generation ContentItem"));
    await screen.findByRole("option", { name: "A01-T1 · v1" });
    expect(screen.queryByRole("option", { name: /Foreign scope/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "A01-T1 · v1" }));
    await user.type(screen.getByLabelText("GenerationPlan prompt"), "Original diagram");
    await user.click(screen.getByRole("button", { name: "GenerationPlanを作成" }));
    await waitFor(() => expect(mediaGenerationApi.createPlan).toHaveBeenCalled());
    const request = vi.mocked(mediaGenerationApi.createPlan).mock.calls[0][0];
    expect(request.content_item_id).toBe(item.id);
    expect(request.request_spec).not.toHaveProperty("research_context");
    expect(await screen.findByTestId("generation-research-context")).toHaveTextContent(item.brief);
    expect(mediaGenerationApi.submitPlan).not.toHaveBeenCalled();
  });

  for (const altered of [false, true]) {
    it(`${altered ? "rejects a changed" : "keeps the original"} user-authored brief in the server Plan snapshot`, async () => {
      const user = userEvent.setup();
      const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona-row", personaRevisionId: revision.persona_revision_id, displayName: "UN//REAL" };
      const item = { id: "user-item", title: "本人の制作企画", input_kind: "user_authored", version: 1, content_hash: "f".repeat(64), status: "draft", persona_revision_id: revision.persona_revision_id, project_id: "project", owner_user_id: "owner", brief: "  本人の原文\n\n" };
      vi.mocked(mediaGenerationApi.listWorkspaces).mockResolvedValue([{ ...workspace, owner_user_id: "owner", project_id: "project" }] as never);
      vi.mocked(mediaGenerationApi.listRecipes).mockResolvedValue([{ ...recipe, owner_user_id: "owner", project_id: "project" }] as never);
      vi.mocked(mediaResearchApi.listContentItems).mockResolvedValue([item] as never);
      vi.mocked(mediaGenerationApi.createPlan).mockResolvedValue({ ...plan, owner_user_id: "owner", project_id: "project", content_item_id: item.id, request_spec: { ...plan.request_spec, research_context: { content_item_id: item.id, content_item_hash: item.content_hash, content_item_version: item.version, owner_user_id: "owner", project_id: "project", persona_id: scope.personaId, brief: { input_kind: "user_authored", persona_revision_id: revision.persona_revision_id, requirements: altered ? item.brief.trim() : item.brief } } } } as never);
      render(<MediaGenerationPanel scope={scope} contentItemId={item.id} />);
      await user.click(await screen.findByLabelText("Generation Workspace")); await user.click(await screen.findByRole("option", { name: /wsp_demo1234/ }));
      await user.click(screen.getByLabelText("Creative Recipe")); await user.click(await screen.findByRole("option", { name: "Image recipe · image · v3" }));
      await waitFor(() => expect(screen.getByLabelText("Generation ContentItem")).toHaveTextContent(item.title));
      await user.type(screen.getByLabelText("GenerationPlan prompt"), "Original diagram"); await user.click(screen.getByRole("button", { name: "GenerationPlanを作成" }));
      if (altered) {
        expect(await screen.findByRole("alert")).toHaveTextContent("本人入力の種別・原文・保存hashが一致しません");
        expect(screen.queryByTestId("generation-research-context")).not.toBeInTheDocument();
      } else expect(await screen.findByTestId("generation-research-context")).toHaveTextContent("本人入力の創作briefを固定");
      expect(mediaGenerationApi.submitPlan).not.toHaveBeenCalled();
    });
  }

  it("stops an altered snapshot read from an existing Plan before fetching runs or submitting", async () => {
    const user = userEvent.setup();
    const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona-row", personaRevisionId: revision.persona_revision_id, displayName: "UN//REAL" };
    const item = { id: "user-item", title: "本人の制作企画", input_kind: "user_authored", version: 1, content_hash: "f".repeat(64), status: "draft", persona_revision_id: revision.persona_revision_id, project_id: "project", owner_user_id: "owner", brief: "  本人の原文\n\n" };
    vi.mocked(mediaGenerationApi.listWorkspaces).mockResolvedValue([{ ...workspace, owner_user_id: "owner", project_id: "project" }] as never);
    vi.mocked(mediaGenerationApi.listRecipes).mockResolvedValue([{ ...recipe, owner_user_id: "owner", project_id: "project" }] as never);
    vi.mocked(mediaResearchApi.listContentItems).mockResolvedValue([item] as never);
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([{ ...plan, owner_user_id: "owner", project_id: "project", content_item_id: item.id, request_spec: { ...plan.request_spec, research_context: { content_item_id: item.id, content_item_hash: "0".repeat(64), content_item_version: item.version, owner_user_id: "owner", project_id: "project", persona_id: scope.personaId, brief: { input_kind: "user_authored", persona_revision_id: revision.persona_revision_id, requirements: item.brief } } } }] as never);
    render(<MediaGenerationPanel scope={scope} contentItemId={item.id} />);
    await user.click(await screen.findByLabelText("Generation Workspace")); await user.click(await screen.findByRole("option", { name: /wsp_demo1234/ }));
    await user.click(await screen.findByRole("button", { name: /plan-row/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("制作brief・種別・保存hashを照合できません");
    expect(screen.queryByTestId("generation-research-context")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "このPlanをsubmit" })).toBeDisabled();
    expect(mediaGenerationApi.listRuns).not.toHaveBeenCalled(); expect(mediaGenerationApi.createPlan).not.toHaveBeenCalled(); expect(mediaGenerationApi.submitPlan).not.toHaveBeenCalled();
  });

  it("pages past 100 mixed-scope items and submits only the compatible later item", async () => {
    const user = userEvent.setup();
    const item = { id: "later-item", title: "A01-T3 later", version: 1, status: "draft", persona_revision_id: revision.persona_revision_id, project_id: null, owner_user_id: "owner", brief: "Original task" };
    const firstPage = Array.from({ length: 100 }, (_, index) => ({ ...item, id: `foreign-${index}`, title: `Foreign ${index}`, ...(index < 50 ? { owner_user_id: "other-owner" } : { project_id: "other-project" }) }));
    vi.mocked(mediaGenerationApi.listWorkspaces).mockResolvedValue([{ ...workspace, owner_user_id: "owner" }] as never);
    vi.mocked(mediaGenerationApi.listRecipes).mockResolvedValue([{ ...recipe, owner_user_id: "owner" }] as never);
    vi.mocked(mediaResearchApi.listContentItems).mockImplementation(async (_program, page) => (page?.offset === 100 ? [item, { ...item, id: "wrong-revision", title: "Wrong revision", persona_revision_id: "other-revision" }] : firstPage) as never);
    render(<MediaGenerationPanel />);
    const next = screen.getByRole("button", { name: "次のContentItem" });
    await waitFor(() => expect(next).not.toBeDisabled());
    expect(mediaResearchApi.listContentItems).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByLabelText("Generation ContentItem")).not.toBeDisabled());
    await user.click(screen.getByLabelText("Generation ContentItem"));
    expect(screen.queryByRole("option", { name: /Foreign/ })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("option", { name: "制作briefを指定しない" }));
    await waitFor(() => expect(screen.getByLabelText("Generation ContentItem")).toHaveAttribute("aria-expanded", "false"));
    await user.click(next);
    await screen.findByText("ContentItem 2ページ目");
    await waitFor(() => expect(screen.getByLabelText("Generation ContentItem")).not.toBeDisabled());
    expect(mediaResearchApi.listContentItems).toHaveBeenLastCalledWith(undefined, { limit: 100, offset: 100 });
    expect(mediaResearchApi.listContentItems).toHaveBeenCalledTimes(2);
    screen.getByLabelText("Generation ContentItem").focus();
    await user.keyboard("{ArrowDown}");
    await screen.findByRole("option", { name: "A01-T3 later · v1" });
    expect(screen.queryByRole("option", { name: /Wrong revision/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "A01-T3 later · v1" }));
    await user.type(screen.getByLabelText("GenerationPlan prompt"), "Original diagram");
    await user.click(screen.getByRole("button", { name: "GenerationPlanを作成" }));
    await waitFor(() => expect(mediaGenerationApi.createPlan).toHaveBeenCalledWith(expect.objectContaining({ content_item_id: item.id }), expect.any(String)));
    expect(screen.getByRole("button", { name: "次のContentItem" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "前のContentItem" }));
    await screen.findByText("ContentItem 1ページ目");
    expect(mediaResearchApi.listContentItems).toHaveBeenLastCalledWith(undefined, { limit: 100, offset: 0 });
    expect(screen.getByLabelText("Generation ContentItem")).toHaveTextContent("制作briefを指定しない");
    expect(mediaGenerationApi.submitPlan).not.toHaveBeenCalled();
  });

  it("renders fixed research context after loading saved Plans", async () => {
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([{ ...plan, content_item_id: "formal-item", request_spec: { ...plan.request_spec, research_context: { context_hash: "c".repeat(64), brief: { requirements: "Original task, acceptance not_run" } } } }] as never);
    render(<MediaGenerationPanel />);
    expect(await screen.findByTestId("generation-research-context")).toHaveTextContent("Original task, acceptance not_run");
    expect(mediaGenerationApi.createPlan).not.toHaveBeenCalled();
    expect(mediaGenerationApi.submitPlan).not.toHaveBeenCalled();
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

  it("renders a managed PNG through the canonical same-origin proxy route", async () => {
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([plan] as never);
    vi.mocked(mediaGenerationApi.listRuns).mockResolvedValue([managedImageRun] as never);
    render(<MediaGenerationPanel />);

    const preview = await screen.findByRole("img", { name: "Generated image preview" });
    expect(preview).toHaveAttribute(
      "src",
      "/api/python-proxy/operations/media/assets/123e4567-e89b-12d3-a456-426614174000/preview?expected_sha256=" +
        "c".repeat(64),
    );
  });

  it("keeps an already-proxied canonical preview route scoped", async () => {
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([plan] as never);
    vi.mocked(mediaGenerationApi.listRuns).mockResolvedValue([
      {
        ...managedImageRun,
        outputs: [
          {
            ...managedImageOutput,
            preview_url:
              "/api/python-proxy/operations/media/assets/123e4567-e89b-12d3-a456-426614174000/preview?expected_sha256=" +
              "c".repeat(64),
          },
        ],
      },
    ] as never);
    render(<MediaGenerationPanel />);

    const preview = await screen.findByRole("img", { name: "Generated image preview" });
    expect(preview).toHaveAttribute(
      "src",
      "/api/python-proxy/operations/media/assets/123e4567-e89b-12d3-a456-426614174000/preview?expected_sha256=" +
        "c".repeat(64),
    );
  });

  it("preserves the configured base path while normalizing the preview route", async () => {
    const withBasePathSpy = vi
      .spyOn(basePath, "withBasePath")
      .mockImplementation((value) => `/at${value}`);
    try {
      vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([plan] as never);
      vi.mocked(mediaGenerationApi.listRuns).mockResolvedValue([managedImageRun] as never);
      render(<MediaGenerationPanel />);

      const preview = await screen.findByRole("img", { name: "Generated image preview" });
      expect(preview).toHaveAttribute(
        "src",
        "/at/api/python-proxy/operations/media/assets/123e4567-e89b-12d3-a456-426614174000/preview?expected_sha256=" +
          "c".repeat(64),
      );
    } finally {
      withBasePathSpy.mockRestore();
    }
  });

  it.each([
    "https://provider.example.invalid/output.png",
    "/api/python-proxy/operations/media/assets/123e4567-e89b-12d3-a456-426614174000/preview?expected_sha256=" + "c".repeat(64) + "&token=secret",
    "/api/python-proxy/operations/media/assets/not-a-uuid/preview?expected_sha256=" + "c".repeat(64),
    "/api/python-proxy/operations/media/assets/123e4567-e89b-12d3-a456-426614174000/preview?expected_sha256=" + "g".repeat(64),
    "/api/python-proxy/operations/media/assets/123e4567-e89b-12d3-a456-426614174000/preview#fragment",
    "//provider.example.invalid/output.png",
  ])("rejects an unsafe managed preview URL: %s", async (previewUrl) => {
    vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([plan] as never);
    vi.mocked(mediaGenerationApi.listRuns).mockResolvedValue([
      {
        ...managedImageRun,
        outputs: [{ ...managedImageOutput, preview_url: previewUrl }],
      },
    ] as never);
    render(<MediaGenerationPanel />);

    await screen.findByText("ast_fixture1234", { exact: true });
    expect(screen.queryByRole("img", { name: "Generated image preview" })).not.toBeInTheDocument();
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
    await userEvent.click(screen.getByRole("button", { name: "GenerationPlanを作成" }));
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
    await userEvent.click(screen.getByRole("button", { name: "GenerationPlanを作成" }));
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
    await userEvent.click(await screen.findByLabelText("Generation Studio paid generationを承認"));
    await waitFor(() => expect(submit).not.toBeDisabled());
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
    await waitFor(() => expect(reconcile).not.toBeDisabled());
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

  it("keeps reloaded bindings scoped when the prior persona response arrives late", async () => {
    const first = { ownerUserId: "owner", projectId: "project", personaId: "persona-row", personaRevisionId: revision.persona_revision_id, displayName: "First" };
    const next = { ...first, projectId: "next-project", personaId: "next-persona", personaRevisionId: "next-revision", displayName: "Next" };
    let complete!: () => void;
    vi.mocked(mediaGenerationApi.listWorkspaces).mockImplementationOnce(() => new Promise(resolve => { complete = () => resolve([{ ...workspace, owner_user_id: first.ownerUserId, project_id: first.projectId }] as never); }));
    vi.mocked(mediaGenerationApi.listWorkspaces).mockResolvedValue([{ ...workspace, id: "next-workspace", external_workspace_id: "wsp_next1234", owner_user_id: next.ownerUserId, project_id: next.projectId }] as never);
    vi.mocked(mediaGenerationApi.listRecipes).mockResolvedValue([]);
    const view = render(<MediaGenerationPanel scope={first} />);
    await waitFor(() => expect(mediaGenerationApi.listWorkspaces).toHaveBeenCalledWith(first.projectId));
    view.rerender(<MediaGenerationPanel scope={next} />);
    const selector = screen.getByLabelText("Generation Workspace");
    await waitFor(() => expect(selector).not.toBeDisabled());
    await act(async () => complete());
    await userEvent.click(selector);
    expect(await screen.findByRole("option", { name: /wsp_next1234/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /wsp_demo1234/ })).not.toBeInTheDocument();
    expect(mediaGenerationApi.createWorkspace).not.toHaveBeenCalled();
  });

  for (const transition of ["unmount", "scope"] as const) {
    it(`discards a delayed output selection after ${transition}`, async () => {
      const onOutputSelected = vi.fn();
      const output = { id: "output-row", external_asset_id: "ast_fixture1234", external_output_version: "outv_fixture1234", sha256: "c".repeat(64), mime_type: "image/png", generation_kind: "image" };
      vi.mocked(mediaGenerationApi.listPlans).mockResolvedValue([plan] as never);
      vi.mocked(mediaGenerationApi.listRuns).mockResolvedValue([{ ...unavailableRun, status: "completed", outputs: [output] }] as never);
      let complete!: () => void;
      vi.mocked(mediaGenerationApi.selectOutput).mockImplementation(() => new Promise(resolve => { complete = () => resolve({ id: "selection" } as never); }));
      const view = render(<MediaGenerationPanel onOutputSelected={onOutputSelected} />);
      await userEvent.click(await screen.findByRole("button", { name: "採用候補に選択" }));
      await waitFor(() => expect(mediaGenerationApi.selectOutput).toHaveBeenCalledTimes(1));
      if (transition === "unmount") view.unmount();
      else view.rerender(<MediaGenerationPanel scope={{ ownerUserId: "other-owner", projectId: "other-project", personaId: "other-persona", personaRevisionId: "other-revision", displayName: "Other" }} onOutputSelected={onOutputSelected} />);
      await act(async () => complete());
      expect(onOutputSelected).not.toHaveBeenCalled();
    });
  }
});

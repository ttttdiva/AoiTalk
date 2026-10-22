// @vitest-environment jsdom
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GenerationRegistrationPanel } from "./generation-registration-panel";
import { mediaGenerationApi } from "@/lib/media-operations-generation-api";
import { mediaOperationsApi } from "@/lib/media-operations-api";
import {
  emptyRecipeDraft,
  registrationStorageKey,
} from "@/lib/media-generation-registration";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";

vi.mock("@/lib/media-operations-generation-api", () => ({
  mediaGenerationApi: {
    createWorkspace: vi.fn(),
    getWorkspace: vi.fn(),
    createRecipe: vi.fn(),
    getRecipe: vi.fn(),
  },
}));
vi.mock("@/lib/media-operations-api", () => ({
  mediaOperationsApi: { getPersona: vi.fn() },
}));
const scope: PersonaWorkspaceScope = {
  ownerUserId: "owner",
  projectId: "project",
  personaId: "persona",
  personaRevisionId: "revision",
  displayName: "Fixture persona",
};
const canonical = (s = scope) => ({
  id: s.personaId,
  owner_user_id: s.ownerUserId,
  project_id: s.projectId,
  state: "active",
  current_revision: {
    id: s.personaRevisionId,
    persona_id: s.personaId,
    owner_user_id: s.ownerUserId,
    project_id: s.projectId,
    display_name: s.displayName,
  },
});
const session = (owner = scope.ownerUserId) =>
  new Response(JSON.stringify({ authenticated: true, user: { id: owner } }));
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

describe("GenerationRegistrationPanel explicit registration", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    sessionStorage.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => session()),
    );
    vi.mocked(mediaOperationsApi.getPersona).mockResolvedValue(
      canonical() as never,
    );
    vi.mocked(mediaGenerationApi.createWorkspace).mockImplementation(
      async (body) =>
        ({
          ...body,
          id: "workspace-row",
          owner_user_id: scope.ownerUserId,
          status: "configured",
          config_hash: "a".repeat(64),
        }) as never,
    );
    vi.mocked(mediaGenerationApi.getWorkspace).mockImplementation(
      async () =>
        ({
          ...vi.mocked(mediaGenerationApi.createWorkspace).mock.calls[0][0],
          id: "workspace-row",
          owner_user_id: scope.ownerUserId,
          status: "configured",
          config_hash: "a".repeat(64),
        }) as never,
    );
    vi.mocked(mediaGenerationApi.createRecipe).mockImplementation(
      async (body) =>
        ({
          ...body,
          id: "recipe-row",
          owner_user_id: scope.ownerUserId,
          current_revision: {
            ...body,
            id: "recipe-revision",
            version: 1,
            creative_recipe_id: "recipe-row",
            owner_user_id: scope.ownerUserId,
            content_hash: "b".repeat(64),
          },
        }) as never,
    );
    vi.mocked(mediaGenerationApi.getRecipe).mockImplementation(
      async () =>
        ({
          ...vi.mocked(mediaGenerationApi.createRecipe).mock.calls[0][0],
          id: "recipe-row",
          owner_user_id: scope.ownerUserId,
          current_revision: {
            ...vi.mocked(mediaGenerationApi.createRecipe).mock.calls[0][0],
            id: "recipe-revision",
            version: 1,
            creative_recipe_id: "recipe-row",
            owner_user_id: scope.ownerUserId,
            content_hash: "b".repeat(64),
          },
        }) as never,
    );
  });
  async function workspacePreview(fresh = true) {
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Workspaceを登録" }));
    if (fresh)
      expect(
        screen.getByLabelText("Generation Studioの接続先origin"),
      ).toHaveValue("");
    else {
      await user.clear(
        screen.getByLabelText("Generation Studioの接続先origin"),
      );
      await user.clear(screen.getByLabelText("73 Workspace ID"));
    }
    await user.type(
      screen.getByLabelText("Generation Studioの接続先origin"),
      "https://studio.example.invalid",
    );
    await user.type(screen.getByLabelText("73 Workspace ID"), "wsp_fixture01");
    await user.click(screen.getByRole("button", { name: "登録内容を確認" }));
    await screen.findByLabelText("Generation登録内容の確認");
    return user;
  }
  async function approve(user: ReturnType<typeof userEvent.setup>) {
    await user.click(
      screen.getByRole("checkbox", {
        name: "この所属・人格版と登録内容を確認しました",
      }),
    );
  }

  it("requires canonical persona scope and never silently adopts a workspace", () => {
    render(<GenerationRegistrationPanel onSaved={vi.fn()} />);
    expect(screen.getByRole("link", { name: "人格Workspace" })).toHaveAttribute(
      "href",
      "/operations?tab=studio",
    );
    expect(
      screen.queryByRole("button", { name: "Workspaceを登録" }),
    ).not.toBeInTheDocument();
    expect(mediaGenerationApi.createWorkspace).not.toHaveBeenCalled();
  });
  it("registers, reads back and reloads after explicit approval", async () => {
    const saved = vi.fn().mockResolvedValue(undefined);
    render(<GenerationRegistrationPanel scope={scope} onSaved={saved} />);
    const user = await workspacePreview();
    expect(
      screen.getByRole("button", { name: "確認したWorkspaceを保存" }),
    ).toBeDisabled();
    expect(mediaGenerationApi.createWorkspace).not.toHaveBeenCalled();
    await approve(user);
    const button = screen.getByRole("button", {
      name: "確認したWorkspaceを保存",
    });
    fireEvent.click(button);
    fireEvent.click(button);
    await screen.findByRole("status");
    expect(mediaGenerationApi.createWorkspace).toHaveBeenCalledTimes(1);
    const [body, key] = vi.mocked(mediaGenerationApi.createWorkspace).mock
      .calls[0];
    expect(body).toEqual({
      project_id: scope.projectId,
      provider: "comfyui_workbench",
      base_url: "https://studio.example.invalid",
      external_workspace_id: "wsp_fixture01",
      external_project_id: null,
    });
    expect(key).toMatch(/^media-registration:/);
    expect(mediaGenerationApi.getWorkspace).toHaveBeenCalledWith(
      "workspace-row",
    );
    expect(saved).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(registrationStorageKey(scope))).toBeNull();
    await user.click(screen.getByRole("button", { name: "登録一覧を再読込" }));
    await waitFor(() => expect(saved).toHaveBeenCalledTimes(2));
  });
  it("registers a pinned Recipe including candidate, model and review policies", async () => {
    const saved = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<GenerationRegistrationPanel scope={scope} onSaved={saved} />);
    await user.click(screen.getByRole("button", { name: "Recipeを登録" }));
    await user.type(screen.getByLabelText("Recipe名"), "Fixture recipe");
    await user.type(
      screen.getByLabelText("Recipeのprompt template"),
      "Original illustration",
    );
    await user.type(screen.getByLabelText("モデル選択ID"), "ims_fixture01");
    await user.clear(screen.getByLabelText("候補数"));
    await user.type(screen.getByLabelText("候補数"), "2");
    await user.click(
      screen.getByRole("checkbox", {
        name: "従量課金モデルは実行時の本人確認を必須にする",
      }),
    );
    await user.click(screen.getByRole("button", { name: "登録内容を確認" }));
    await screen.findByLabelText("Generation登録内容の確認");
    await approve(user);
    await user.click(
      screen.getByRole("button", { name: "確認したRecipeを保存" }),
    );
    await screen.findByRole("status");
    expect(mediaGenerationApi.createRecipe).toHaveBeenCalledTimes(1);
    expect(
      vi.mocked(mediaGenerationApi.createRecipe).mock.calls[0][0],
    ).toMatchObject({
      persona_id: scope.personaId,
      persona_revision_id: scope.personaRevisionId,
      project_id: scope.projectId,
      candidate_count: 2,
      model_selection_id: "ims_fixture01",
      cost_policy: { metered: true, requires_metered_ack: true },
      human_review_policy: { required: true },
      provenance_retention_policy: { retain: true },
    });
    expect(saved).toHaveBeenCalledTimes(1);
  });
  it("retains the exact request/key through lost response, close and remount", async () => {
    const saved = vi.fn().mockResolvedValue(undefined);
    vi.mocked(mediaGenerationApi.createWorkspace).mockRejectedValueOnce(
      new Error("Response lost"),
    );
    const view = render(
      <GenerationRegistrationPanel scope={scope} onSaved={saved} />,
    );
    const user = await workspacePreview();
    await approve(user);
    await user.click(
      screen.getByRole("button", { name: "確認したWorkspaceを保存" }),
    );
    await screen.findByRole("alert");
    const first = vi.mocked(mediaGenerationApi.createWorkspace).mock.calls[0];
    await user.click(
      screen.getByRole("button", { name: "閉じる（再試行内容を保持）" }),
    );
    view.unmount();
    render(
      <GenerationRegistrationPanel scope={{ ...scope }} onSaved={saved} />,
    );
    await screen.findByRole("button", { name: "同じ内容で登録を再試行" });
    expect(screen.queryByLabelText("73 Workspace ID")).not.toBeInTheDocument();
    await approve(user);
    await user.click(
      screen.getByRole("button", { name: "同じ内容で登録を再試行" }),
    );
    await screen.findByRole("status");
    expect(vi.mocked(mediaGenerationApi.createWorkspace).mock.calls[1]).toEqual(
      first,
    );
    expect(saved).toHaveBeenCalledTimes(1);
  });
  it("rejects another logged-in owner before any mutation", async () => {
    vi.mocked(fetch).mockImplementation(async () => session("foreign"));
    render(<GenerationRegistrationPanel scope={scope} onSaved={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Workspaceを登録" }));
    await user.type(
      screen.getByLabelText("Generation Studioの接続先origin"),
      "https://studio.example.invalid",
    );
    await user.type(screen.getByLabelText("73 Workspace ID"), "wsp_fixture01");
    await user.click(screen.getByRole("button", { name: "登録内容を確認" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("所有者");
    expect(mediaOperationsApi.getPersona).not.toHaveBeenCalled();
    expect(mediaGenerationApi.createWorkspace).not.toHaveBeenCalled();
  });
  it("cancels pending preflight without creating a record and clears unsubmitted previews on kind change", async () => {
    const saved = vi.fn();
    const view = render(
      <GenerationRegistrationPanel scope={scope} onSaved={saved} />,
    );
    const user = await workspacePreview();
    await user.click(screen.getByRole("button", { name: "Recipeを登録" }));
    expect(screen.getByLabelText("Recipe名")).toHaveValue("");
    expect(
      screen.queryByLabelText("Generation登録内容の確認"),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "登録を取り消す" }));
    await user.click(screen.getByRole("button", { name: "Workspaceを登録" }));
    await user.type(
      screen.getByLabelText("Generation Studioの接続先origin"),
      "https://studio.example.invalid",
    );
    await user.type(screen.getByLabelText("73 Workspace ID"), "wsp_fixture01");
    const read =
      deferred<Awaited<ReturnType<typeof mediaOperationsApi.getPersona>>>();
    vi.mocked(mediaOperationsApi.getPersona).mockReturnValueOnce(read.promise);
    await user.click(screen.getByRole("button", { name: "登録内容を確認" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "登録内容を確認" }),
      ).toBeDisabled(),
    );
    await user.click(screen.getByRole("button", { name: "登録を取り消す" }));
    await act(async () => read.resolve(canonical() as never));
    expect(
      screen.queryByLabelText("Generation登録内容の確認"),
    ).not.toBeInTheDocument();
    expect(mediaGenerationApi.createWorkspace).not.toHaveBeenCalled();
    view.unmount();
  });
  it("resets every unsent input after cancel, including a confirmed preview", async () => {
    render(<GenerationRegistrationPanel scope={scope} onSaved={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Workspaceを登録" }));
    await user.type(
      screen.getByLabelText("Generation Studioの接続先origin"),
      "https://studio.example.invalid",
    );
    await user.type(screen.getByLabelText("73 Workspace ID"), "wsp_fixture01");
    await user.type(
      screen.getByLabelText("73 Project ID（任意）"),
      "prj_fixture01",
    );
    await user.click(screen.getByRole("button", { name: "登録を取り消す" }));
    await user.click(screen.getByRole("button", { name: "Workspaceを登録" }));
    expect(screen.getByLabelText("Generation Studioの接続先origin")).toHaveValue(
      "",
    );
    expect(screen.getByLabelText("73 Workspace ID")).toHaveValue("");
    expect(screen.getByLabelText("73 Project ID（任意）")).toHaveValue("");

    await workspacePreview();
    await approve(user);
    await user.click(screen.getByRole("button", { name: "登録を取り消す" }));
    await user.click(screen.getByRole("button", { name: "Workspaceを登録" }));
    expect(
      screen.queryByLabelText("Generation登録内容の確認"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Generation Studioの接続先origin")).toHaveValue(
      "",
    );
    expect(screen.getByLabelText("73 Workspace ID")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "登録を取り消す" }));

    await user.click(screen.getByRole("button", { name: "Recipeを登録" }));
    await user.type(screen.getByLabelText("Recipe名"), "Fixture recipe");
    await user.type(
      screen.getByLabelText("Recipeのprompt template"),
      "Original illustration",
    );
    await user.type(screen.getByLabelText("モデル選択ID"), "ims_fixture01");
    await user.clear(screen.getByLabelText("候補数"));
    await user.type(screen.getByLabelText("候補数"), "2");
    await user.click(
      screen.getByRole("checkbox", {
        name: "従量課金モデルは実行時の本人確認を必須にする",
      }),
    );
    await user.click(screen.getByRole("button", { name: "登録を取り消す" }));
    await user.click(screen.getByRole("button", { name: "Recipeを登録" }));
    expect(screen.getByLabelText("Recipe名")).toHaveValue("");
    expect(screen.getByLabelText("Recipeのprompt template")).toHaveValue("");
    expect(screen.getByLabelText("モデル選択ID")).toHaveValue("");
    expect(screen.getByLabelText("候補数")).toHaveValue(
      Number(emptyRecipeDraft().candidateCount),
    );
    expect(
      screen.getByRole("checkbox", {
        name: "従量課金モデルは実行時の本人確認を必須にする",
      }),
    ).not.toBeChecked();
    expect(mediaGenerationApi.createWorkspace).not.toHaveBeenCalled();
    expect(mediaGenerationApi.createRecipe).not.toHaveBeenCalled();
  });
  it("keeps same-authority object updates live, suppresses stale receipts after actual scope changes", async () => {
    const saved = vi.fn().mockResolvedValue(undefined);
    const response =
      deferred<
        Awaited<ReturnType<typeof mediaGenerationApi.createWorkspace>>
      >();
    vi.mocked(mediaGenerationApi.createWorkspace).mockReturnValueOnce(
      response.promise,
    );
    const view = render(
      <GenerationRegistrationPanel scope={scope} onSaved={saved} />,
    );
    const user = await workspacePreview();
    await approve(user);
    await user.click(
      screen.getByRole("button", { name: "確認したWorkspaceを保存" }),
    );
    await waitFor(() =>
      expect(mediaGenerationApi.createWorkspace).toHaveBeenCalledTimes(1),
    );
    view.rerender(
      <GenerationRegistrationPanel
        scope={{ ...scope, displayName: "Updated label" }}
        onSaved={saved}
      />,
    );
    await act(async () =>
      response.resolve({
        ...vi.mocked(mediaGenerationApi.createWorkspace).mock.calls[0][0],
        id: "workspace-row",
        owner_user_id: scope.ownerUserId,
        status: "configured",
        config_hash: "a".repeat(64),
      } as never),
    );
    await screen.findByRole("status");
    expect(saved).toHaveBeenCalledTimes(1);
    const nextResponse =
      deferred<
        Awaited<ReturnType<typeof mediaGenerationApi.createWorkspace>>
      >();
    vi.mocked(mediaGenerationApi.createWorkspace).mockReturnValueOnce(
      nextResponse.promise,
    );
    await workspacePreview(false);
    await approve(user);
    await user.click(
      screen.getByRole("button", { name: "確認したWorkspaceを保存" }),
    );
    await waitFor(() =>
      expect(mediaGenerationApi.createWorkspace).toHaveBeenCalledTimes(2),
    );
    view.rerender(
      <GenerationRegistrationPanel
        scope={{ ...scope, personaRevisionId: "new-revision" }}
        onSaved={saved}
      />,
    );
    await act(async () =>
      nextResponse.resolve({
        ...vi.mocked(mediaGenerationApi.createWorkspace).mock.calls[1][0],
        id: "workspace-row",
        owner_user_id: scope.ownerUserId,
        status: "configured",
        config_hash: "a".repeat(64),
      } as never),
    );
    expect(saved).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(
      sessionStorage.getItem(registrationStorageKey(scope)),
    ).not.toBeNull();
  });
  it("blocks revision changes between approval and saving", async () => {
    render(<GenerationRegistrationPanel scope={scope} onSaved={vi.fn()} />);
    const user = await workspacePreview();
    await approve(user);
    vi.mocked(mediaOperationsApi.getPersona).mockResolvedValue(
      canonical({ ...scope, personaRevisionId: "next-revision" }) as never,
    );
    await user.click(
      screen.getByRole("button", { name: "確認したWorkspaceを保存" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("現在版");
    expect(mediaGenerationApi.createWorkspace).not.toHaveBeenCalled();
  });
});

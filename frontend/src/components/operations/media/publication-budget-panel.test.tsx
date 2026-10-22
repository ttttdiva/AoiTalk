// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MediaResearchPanel } from "@/components/operations/media/research-panel";
import { mediaResearchApi } from "@/lib/media-operations-research-api";

vi.mock("@/lib/media-operations-research-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/media-operations-research-api")>("@/lib/media-operations-research-api");
  return { ...actual, mediaResearchApi: Object.fromEntries(Object.keys(actual.mediaResearchApi).map((key) => [key, vi.fn().mockResolvedValue([])])) };
});

const program = {
  id: "program-budget", owner_user_id: "owner", project_id: null, persona_id: "persona",
  state: "draft", enabled: false, last_due_at: null, next_due_at: null, create_hash: "a".repeat(64),
  created_by: "owner", created_at: "2026-10-06T00:00:00",
  current_revision: {
    id: "revision", editorial_program_id: "program-budget", owner_user_id: "owner", project_id: null,
    version: 1, name: "Budget program", objective: "Preserve purpose", content_type: "article",
    cadence: "weekly", target_platforms: ["x"], target_account_refs: ["account"], content_pillar: null,
    required_resources: [], default_creative_recipe_ref: null, default_qa_policy_ref: null,
    experiment_ref: null, draft_generation_policy: "human_review", content_hash: "b".repeat(64),
    created_by: "owner", created_at: "2026-10-06T00:00:00", max_publications: null,
  },
};

async function ready() {
  render(<MediaResearchPanel />);
  await waitFor(() => expect(screen.getByLabelText("Program累積投稿上限")).not.toBeDisabled());
  return screen.getByLabelText("Program累積投稿上限");
}

describe("EditorialProgram publication budget", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(mediaResearchApi.listPrograms).mockResolvedValue([program] as never);
    vi.mocked(mediaResearchApi.reviseProgram).mockReset().mockResolvedValue(program.current_revision as never);
  });

  it("shows unset and saves zero while preserving the current recipe", async () => {
    const input = await ready();
    const caption = screen.getByText("累積投稿上限（0〜10000、空欄は未設定）", { selector: "label" });
    expect(caption).toHaveAttribute("for", "program-publication-limit");
    expect(input).toHaveAttribute("id", "program-publication-limit");
    await userEvent.click(caption);
    expect(input).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "累積投稿上限の説明" }));
    expect(await screen.findByText(/Programの全期間・全子jobに適用します/)).toBeVisible();
    expect(screen.getByText("現在の累積投稿上限: 未設定（上限なし）")).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "0" } });
    vi.mocked(mediaResearchApi.listPrograms).mockResolvedValue([{ ...program, current_revision: { ...program.current_revision, version: 2, max_publications: 0 } }] as never);
    await userEvent.click(screen.getByRole("button", { name: "投稿上限を保存" }));
    await waitFor(() => expect(mediaResearchApi.reviseProgram).toHaveBeenCalledWith("program-budget", expect.objectContaining({ expected_version: 1, max_publications: 0, cadence: "weekly", target_account_refs: ["account"], objective: "Preserve purpose" }), expect.any(String)));
    await waitFor(() => expect(screen.getByText("現在の累積投稿上限: 0件")).toBeInTheDocument());
  });

  it("cancels unsaved edits and clears with explicit null", async () => {
    vi.mocked(mediaResearchApi.listPrograms).mockResolvedValue([{ ...program, current_revision: { ...program.current_revision, max_publications: 7 } }] as never);
    const input = await ready();
    expect(input).toHaveValue(7);
    fireEvent.change(input, { target: { value: "4" } });
    await userEvent.click(screen.getByRole("button", { name: "変更を取り消す" }));
    expect(input).toHaveValue(7);
    expect(mediaResearchApi.reviseProgram).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "" } });
    await userEvent.click(screen.getByRole("button", { name: "投稿上限を保存" }));
    await waitFor(() => expect(mediaResearchApi.reviseProgram).toHaveBeenCalledWith("program-budget", expect.objectContaining({ max_publications: null }), expect.any(String)));
  });

  it("admits the max boundary once while pending and retains saved value after failure", async () => {
    let resolve!: (value: never) => void;
    vi.mocked(mediaResearchApi.reviseProgram).mockReturnValue(new Promise((done) => { resolve = done; }));
    const input = await ready();
    fireEvent.change(input, { target: { value: "10000" } });
    const button = screen.getByRole("button", { name: "投稿上限を保存" });
    await userEvent.click(button);
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(mediaResearchApi.reviseProgram).toHaveBeenCalledTimes(1);
    expect(mediaResearchApi.reviseProgram).toHaveBeenCalledWith("program-budget", expect.objectContaining({ max_publications: 10000 }), expect.any(String));
    resolve(program.current_revision as never);
    await waitFor(() => expect(button).not.toBeDisabled());
    vi.mocked(mediaResearchApi.reviseProgram).mockRejectedValue(new Error("stale EditorialProgram version"));
    fireEvent.change(input, { target: { value: "3" } });
    await userEvent.click(button);
    await waitFor(() => expect(screen.getByText("stale EditorialProgram version")).toBeInTheDocument());
    expect(screen.getByText("現在の累積投稿上限: 未設定（上限なし）")).toBeInTheDocument();
  });

  it("switches fields with the selected program without saving old edits", async () => {
    vi.mocked(mediaResearchApi.listPrograms).mockResolvedValue([program, { ...program, id: "other", current_revision: { ...program.current_revision, name: "Other", max_publications: 5 } }] as never);
    const input = await ready();
    fireEvent.change(input, { target: { value: "9" } });
    await userEvent.click(screen.getByLabelText("EditorialProgram"));
    await userEvent.click(await screen.findByRole("option", { name: "Other" }));
    await waitFor(() => expect(input).toHaveValue(5));
    expect(mediaResearchApi.reviseProgram).not.toHaveBeenCalled();
  });
});

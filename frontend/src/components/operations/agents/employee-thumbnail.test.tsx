// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EmployeeThumbnail, EmployeeThumbnailControl } from "./employee-thumbnail";

const mocks = vi.hoisted(() => ({ upload: vi.fn(), save: vi.fn(), swr: vi.fn() }));
vi.mock("swr", () => ({ default: mocks.swr }));
vi.mock("@/lib/operations-api", () => ({ operationsApi: { uploadArtifact: mocks.upload } }));
vi.mock("@/lib/agent-employees-api", () => ({ employeeApi: { thumbnail: mocks.save } }));
const agent = { id: "agent-a", display_name: "担当", slug: "worker", state: "active" as const, thumbnail_artifact_id: "old-image" };

beforeEach(() => { vi.clearAllMocks(); mocks.swr.mockReturnValue({}); mocks.upload.mockResolvedValue({ id: "saved-image" }); mocks.save.mockResolvedValue({ agent_id: agent.id, artifact_id: "saved-image" }); });

describe("Agentのサムネイル", () => {
  it("shows the persisted, authenticated image and falls back on a broken image", () => {
    render(<EmployeeThumbnail agent={agent} />);
    const image = screen.getByRole("img", { name: "担当のサムネイル" });
    expect(image).toHaveAttribute("src", expect.stringContaining("/operations/artifacts/old-image/content"));
    fireEvent.error(image);
    expect(screen.getByText("画像を表示できません")).toBeVisible();
  });

  it("uses character preview when no artifact exists and keeps artifact priority", () => {
    mocks.swr.mockReturnValue({ data: { has_source: true, builds: [], active_revision: null } });
    const character = { ...agent, thumbnail_artifact_id: null, character_id: "character-a" };
    const { rerender } = render(<EmployeeThumbnail agent={character} />);
    expect(screen.getByRole("img", { name: "担当のサムネイル" })).toHaveAttribute("src", expect.stringContaining("/source.png"));
    rerender(<EmployeeThumbnail agent={{ ...character, thumbnail_artifact_id: "artifact-a" }} />);
    expect(screen.getByRole("img", { name: "担当のサムネイル" })).toHaveAttribute("src", expect.stringContaining("/artifacts/artifact-a/content"));
  });

  it("shows stable, distinct identity and role icons without saving configuration", () => {
    const qa = { ...agent, thumbnail_artifact_id: null, display_name: "QA Gateway", slug: "qa-gateway" };
    const research = { ...qa, id: "agent-b", display_name: "調査担当", slug: "research" };
    const { rerender } = render(<><EmployeeThumbnail agent={qa} /><EmployeeThumbnail agent={research} /></>);
    const identity = screen.getByLabelText("QA Gatewayの識別アイコン");
    const markup = identity.outerHTML;
    expect(identity).toHaveTextContent("識別アイコン・QA");
    expect(screen.getByLabelText("調査担当の識別アイコン")).toHaveTextContent("識別アイコン・調査");
    expect(identity.querySelector("svg")?.outerHTML).not.toBe(screen.getByLabelText("調査担当の識別アイコン").querySelector("svg")?.outerHTML);
    rerender(<><EmployeeThumbnail agent={qa} /><EmployeeThumbnail agent={research} /></>);
    expect(screen.getByLabelText("QA Gatewayの識別アイコン").outerHTML).toBe(markup);
    expect(mocks.save).not.toHaveBeenCalled();
    expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("uploads then saves with the previous reference and refreshes only after persistence", async () => {
    const changed = vi.fn().mockResolvedValue(undefined);
    render(<EmployeeThumbnailControl agent={agent} onChanged={changed} />);
    await userEvent.upload(screen.getByLabelText("担当の画像ファイル"), new File(["png"], "new.png", { type: "image/png" }));
    await waitFor(() => expect(changed).toHaveBeenCalledWith(agent.id, "saved-image"));
    expect(mocks.save).toHaveBeenCalledWith(agent.id, { artifact_id: "saved-image", expected_artifact_id: "old-image" });
    expect(mocks.upload.mock.invocationCallOrder[0]).toBeLessThan(mocks.save.mock.invocationCallOrder[0]);
  });

  it("does not mutate for cancellation, invalid files or a failed save", async () => {
    const changed = vi.fn();
    render(<EmployeeThumbnailControl agent={agent} onChanged={changed} />);
    const input = screen.getByLabelText("担当の画像ファイル");
    fireEvent.change(input, { target: { files: [] } });
    fireEvent.change(input, { target: { files: [new File(["svg"], "image.svg", { type: "image/svg+xml" })] } });
    expect(mocks.upload).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("PNG / JPEG");
    mocks.save.mockRejectedValue(new Error("他の更新と競合しています"));
    fireEvent.change(input, { target: { files: [new File(["png"], "new.png", { type: "image/png" })] } });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("他の更新と競合"));
    expect(changed).not.toHaveBeenCalled();
  });
});

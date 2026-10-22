// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EmployeeThumbnail, EmployeeThumbnailControl } from "./employee-thumbnail";

const mocks = vi.hoisted(() => ({ upload: vi.fn(), save: vi.fn() }));
vi.mock("@/lib/operations-api", () => ({ operationsApi: { uploadArtifact: mocks.upload } }));
vi.mock("@/lib/agent-employees-api", () => ({ employeeApi: { thumbnail: mocks.save } }));
const agent = { id: "agent-a", display_name: "担当", slug: "worker", state: "active" as const, thumbnail_artifact_id: "old-image" };

beforeEach(() => { vi.clearAllMocks(); mocks.upload.mockResolvedValue({ id: "saved-image" }); mocks.save.mockResolvedValue({ agent_id: agent.id, artifact_id: "saved-image" }); });

describe("Agentのサムネイル", () => {
  it("shows the persisted, authenticated image and falls back on a broken image", () => {
    render(<EmployeeThumbnail agent={agent} />);
    const image = screen.getByRole("img", { name: "担当のサムネイル" });
    expect(image).toHaveAttribute("src", expect.stringContaining("/operations/artifacts/old-image/content"));
    fireEvent.error(image);
    expect(screen.getByText("画像を表示できません")).toBeVisible();
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

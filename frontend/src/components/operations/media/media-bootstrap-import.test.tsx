// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MediaBootstrapImportPanel } from "./media-bootstrap-import";
const mocks = vi.hoisted(() => ({ parse: vi.fn(), run: vi.fn(), lock: vi.fn() }));
vi.mock("@/lib/mediaops-bootstrap", () => ({
  parseBootstrapPlan: mocks.parse,
  journalKey: () => "mediaops-bootstrap:test", newJournal: (p: { schema: string; scope_key: string; expected_owner_user_id: string }) => ({ schema: p.schema, scope_key: p.scope_key, owner: p.expected_owner_user_id, bindings: {}, live_runs: {} }),
  BootstrapRunner: class {
    plan; journal;
    constructor(plan: unknown, journal: unknown) { this.plan = plan; this.journal = journal; }
    run = mocks.run;
  },
}));
const plan = { schema: "mediaops-bootstrap/v1", scope_key: "test", expected_owner_user_id: "owner", plan_hash: "a".repeat(64), actions: [{ key: "source:one" }], blocked: [] };
const file = { name: "plan.json", text: () => Promise.resolve("{}") };
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); mocks.parse.mockResolvedValue(plan); mocks.run.mockResolvedValue([{ key: "source:one", status: "create" }]);
  Object.defineProperty(navigator, "locks", { configurable: true, value: { request: mocks.lock.mockImplementation(async (_key, _opts, callback) => callback({ name: "test" })) } });
});
describe("MediaOps bootstrap explicit import flow", () => {
  it("file import and preview do not apply until a separate click", async () => {
    render(<MediaBootstrapImportPanel />);
    fireEvent.change(screen.getByLabelText(/Bootstrap plan JSON/), { target: { files: [file] } });
    await screen.findByText(/ファイル検証済み/);
    expect(mocks.run).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "この内容を適用" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "差分を確認" }));
    await waitFor(() => expect(mocks.run).toHaveBeenCalledWith("preview", expect.any(Function), expect.any(Function)));
    await waitFor(() => expect(screen.getByRole("button", { name: "この内容を適用" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "この内容を適用" }));
    await waitFor(() => expect(mocks.run).toHaveBeenCalledWith("apply", expect.any(Function), expect.any(Function)));
  });
  it("invalid plan exposes no apply controls", async () => {
    mocks.parse.mockRejectedValue(new Error("許可されていないAPIです")); render(<MediaBootstrapImportPanel />);
    fireEvent.change(screen.getByLabelText(/Bootstrap plan JSON/), { target: { files: [file] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("許可されていないAPI");
    expect(screen.queryByRole("button", { name: "この内容を適用" })).not.toBeInTheDocument();
    expect(mocks.run).not.toHaveBeenCalled();
  });
  it("cross-tab conflict prevents all runner operations", async () => {
    mocks.lock.mockImplementation(async (_key, _opts, callback) => callback(null)); render(<MediaBootstrapImportPanel />);
    fireEvent.change(screen.getByLabelText(/Bootstrap plan JSON/), { target: { files: [file] } }); await screen.findByText(/ファイル検証済み/);
    fireEvent.click(screen.getByRole("button", { name: "差分を確認" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("別のタブ"); expect(mocks.run).not.toHaveBeenCalled();
  });
});

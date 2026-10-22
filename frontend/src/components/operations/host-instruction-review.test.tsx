// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { HostInstructionReview } from "./host-instruction-review";
import { OperationsLocalTaskPanel } from "./local-task-panel";
import { LocalTaskApiError, operationsLocalTasksApi as api, readingTaskBody, type InstructionReview, type LocalTask } from "@/lib/operations-local-tasks-api";
vi.mock("@/lib/operations-local-tasks-api", async original => ({ ...await original<typeof import("@/lib/operations-local-tasks-api")>(), operationsLocalTasksApi: { owner: vi.fn(), previewInstructions: vi.fn(), approveInstructions: vi.fn(), instructionReviewStatus: vi.fn(), get: vi.fn(), status: vi.fn(), list: vi.fn(), tail: vi.fn(), create: vi.fn(), cancel: vi.fn() } }));
const principal = vi.hoisted(() => ({ id: undefined as string | null | undefined }));
vi.mock("@/components/providers/swr-global-provider", () => ({ useCurrentUserId: () => principal.id }));
const owner = "11111111-1111-4111-8111-111111111111";
const task: LocalTask = { task_id: "a".repeat(32), status: "queued", goal: "fixture", workspace: "/fixture", mode: "read_only", is_terminal: false };
const review: InstructionReview = { nonce: "b".repeat(32), task_id: task.task_id, owner, cli_version: "0.159.2", task_fingerprint: "c".repeat(64), review_digest: "d".repeat(64), expires_at: Date.now() / 1000 + 300, execution_expires_at: Date.now() / 1000 + 420,
  sources: [{ path: "/fixture/AGENTS.override.md", exists: false, selected: false, size_bytes: 0, sha256: null, text: null }, { path: "/fixture/AGENTS.md", exists: true, selected: true, size_bytes: 42, sha256: "e".repeat(64), text: "<img src=x onerror=alert(1)> inert fixture" }] };
const pointerKey = `aoitalk:host-instruction-review:${owner}`;
const onAccepted = vi.fn(async () => {}); const onLock = vi.fn();
function reviewElement(id = owner) { return <HostInstructionReview owner={id} disabled={false} canPreview onAccepted={onAccepted} onLock={onLock} body={() => readingTaskBody({ goal: "Read fixture", workspace: "/fixture", completion_criteria: [], max_tool_rounds: 4, timeout_sec: 120, route: { provider: "codex-cli", model: "gpt-5.6-sol", effort: "high" } })} />; }
function mount() { return render(reviewElement()); }
async function preview() { fireEvent.click(screen.getByRole("button", { name: "ホスト指示の全文を確認" })); await screen.findByText(review.sources[1].text!); }
function checkBoth() { for (const checkbox of screen.getAllByRole("checkbox")) fireEvent.click(checkbox); }
beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); principal.id = undefined;
  vi.mocked(api.owner).mockResolvedValue({ authenticated: true, user: { id: owner, role: "admin" } });
  vi.mocked(api.previewInstructions).mockResolvedValue(review);
  vi.mocked(api.approveInstructions).mockResolvedValue({ task, created: true });
  vi.mocked(api.instructionReviewStatus).mockResolvedValue({ task_id: task.task_id, task, consumed: true, expired: false });
});
it("renders complete inert text and requires both explicit confirmations", async () => {
  mount(); await preview();
  expect(document.querySelector("img")).toBeNull();
  expect(screen.getByText(/Review digest:/u)).toHaveTextContent(review.review_digest);
  const accept = screen.getByRole("button", { name: "この内容をこのタスクに採用して実行" });
  expect(accept).toBeDisabled(); expect(screen.getAllByRole("checkbox")).toHaveLength(2);
  fireEvent.click(screen.getAllByRole("checkbox")[0]); expect(accept).toBeDisabled();
  fireEvent.click(screen.getAllByRole("checkbox")[1]); fireEvent.click(accept);
  await waitFor(() => expect(onAccepted).toHaveBeenCalledWith(task));
  expect(api.approveInstructions).toHaveBeenCalledTimes(1);
  expect(api.approveInstructions).toHaveBeenCalledWith(review.nonce, { task_fingerprint: review.task_fingerprint, review_digest: review.review_digest, accept_openai_transmission: true, accept_trusted_host_assumption: true });
});
it("resets confirmations when the user closes and reviews again", async () => {
  mount(); await preview(); checkBoth(); fireEvent.click(screen.getByRole("button", { name: "確認を閉じて入力を編集" }));
  expect(onLock).toHaveBeenLastCalledWith(false); await preview();
  expect(screen.getByRole("button", { name: "この内容をこのタスクに採用して実行" })).toBeDisabled();
  expect(api.approveInstructions).not.toHaveBeenCalled();
});
it("reconciles unknown acceptance without repeating approval and stores only a lookup pointer", async () => {
  vi.mocked(api.approveInstructions).mockRejectedValueOnce(new Error("response lost"));
  mount(); await preview(); checkBoth(); fireEvent.click(screen.getByRole("button", { name: "この内容をこのタスクに採用して実行" }));
  await screen.findByText("response lost");
  expect(JSON.parse(localStorage.getItem(pointerKey)!)).toEqual({ nonce: review.nonce, task_id: task.task_id });
  fireEvent.click(screen.getByRole("button", { name: "採用の受付結果を確認" }));
  await waitFor(() => expect(onAccepted).toHaveBeenCalledWith(task));
  expect(api.approveInstructions).toHaveBeenCalledTimes(1); expect(localStorage.getItem(pointerKey)).toBeNull();
});
it("reload looks up a saved reserved task after a restart and never auto-approves", async () => {
  localStorage.setItem(pointerKey, JSON.stringify({ nonce: review.nonce, task_id: task.task_id }));
  vi.mocked(api.instructionReviewStatus).mockRejectedValue(new LocalTaskApiError("expired challenge", 404));
  vi.mocked(api.get).mockResolvedValue({ task }); mount();
  await waitFor(() => expect(onAccepted).toHaveBeenCalledWith(task));
  expect(api.get).toHaveBeenCalledWith(task.task_id); expect(api.approveInstructions).not.toHaveBeenCalled(); expect(api.previewInstructions).not.toHaveBeenCalled();
});
it("keeps recovery locked and preserves the pointer on a transient lookup failure", async () => {
  localStorage.setItem(pointerKey, JSON.stringify({ nonce: review.nonce, task_id: task.task_id }));
  vi.mocked(api.instructionReviewStatus).mockRejectedValueOnce(new Error("temporarily unavailable")); mount();
  await screen.findByText("temporarily unavailable");
  expect(screen.getByRole("button", { name: "ホスト指示の全文を確認" })).toBeDisabled(); expect(localStorage.getItem(pointerKey)).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "採用の受付結果を確認" })); await waitFor(() => expect(onAccepted).toHaveBeenCalledWith(task));
  expect(api.approveInstructions).not.toHaveBeenCalled();
});
it("blocks owner changes and expired challenges before approval", async () => {
  vi.mocked(api.previewInstructions).mockResolvedValue({ ...review, expires_at: Date.now() / 1000 - 1 }); mount(); await preview(); checkBoth();
  expect(screen.getByRole("button", { name: "この内容をこのタスクに採用して実行" })).toBeDisabled(); expect(api.approveInstructions).not.toHaveBeenCalled();
});
it("revalidates owner immediately before the explicit adoption", async () => {
  mount(); await preview(); checkBoth();
  vi.mocked(api.owner).mockResolvedValue({ authenticated: true, user: { id: "other", role: "admin" } });
  fireEvent.click(screen.getByRole("button", { name: "この内容をこのタスクに採用して実行" })); await screen.findByRole("alert");
  expect(api.approveInstructions).not.toHaveBeenCalled(); expect(localStorage.getItem(pointerKey)).toBeNull();
});
it("panel reload reconciles an earlier Codex adoption when the default provider is local", async () => {
  const saved = { ...task, status: "completed" as const, is_terminal: true, summary_text: "Recovered reviewed task" };
  localStorage.setItem(pointerKey, JSON.stringify({ nonce: review.nonce, task_id: task.task_id }));
  vi.mocked(api.status).mockResolvedValue({ enabled: true, running: true, model: "local", allowed_roots: ["/fixture"], max_concurrency: 1, queued: 0, limits: {}, supports_task_routes: true, supported_providers: ["openai_compatible_local", "codex-cli"], default_route: { provider: "openai_compatible_local", model: "local" } });
  vi.mocked(api.list).mockResolvedValue({ tasks: [saved] }); vi.mocked(api.get).mockResolvedValue({ task: saved });
  vi.mocked(api.tail).mockResolvedValue({ events: [], truncated: false });
  vi.mocked(api.instructionReviewStatus).mockResolvedValue({ task_id: saved.task_id, task: saved, consumed: true, expired: false });
  render(<OperationsLocalTaskPanel />); await screen.findByText("Recovered reviewed task");
  expect(api.instructionReviewStatus).toHaveBeenCalledWith(review.nonce);
  expect(api.approveInstructions).not.toHaveBeenCalled(); expect(api.create).not.toHaveBeenCalled(); expect(localStorage.getItem(pointerKey)).toBeNull();
});

function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason: Error) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
const otherOwner = "22222222-2222-4222-8222-222222222222";
const previewButton = "ホスト指示の全文を確認";
const acceptButton = "この内容をこのタスクに採用して実行";
const reconcileButton = "採用の受付結果を確認";
function noPrivate() { expect(screen.queryByText(review.sources[1].text!)).toBeNull(); expect(screen.queryAllByRole("checkbox")).toHaveLength(0); expect(onAccepted).not.toHaveBeenCalled(); }
function changeSession(id: string | null) { vi.mocked(api.owner).mockResolvedValue(id ? { authenticated: true, user: { id, role: "admin" } } : { authenticated: false }); }
for (const outcome of ["resolve", "reject"] as const) {
  it(`drops a delayed preview ${outcome} after a live session switch`, async () => {
    const held = deferred<InstructionReview>(); vi.mocked(api.previewInstructions).mockReturnValue(held.promise);
    mount(); fireEvent.click(screen.getByRole("button", { name: previewButton })); await waitFor(() => expect(api.previewInstructions).toHaveBeenCalledOnce());
    changeSession(otherOwner);
    await act(async () => { if (outcome === "resolve") held.resolve(review); else held.reject(new Error("old preview error")); });
    await screen.findByRole("alert"); noPrivate(); expect(screen.queryByText("old preview error")).toBeNull(); expect(onLock).toHaveBeenLastCalledWith(false);
  });
  it(`drops a delayed approval ${outcome} after logout`, async () => {
    const held = deferred<{ task: LocalTask; created: boolean }>(); vi.mocked(api.approveInstructions).mockReturnValue(held.promise);
    mount(); await preview(); checkBoth(); fireEvent.click(screen.getByRole("button", { name: acceptButton })); await waitFor(() => expect(api.approveInstructions).toHaveBeenCalledOnce());
    changeSession(null);
    await act(async () => { if (outcome === "resolve") held.resolve({ task, created: true }); else held.reject(new Error("old approval error")); });
    await screen.findByRole("alert"); noPrivate(); expect(localStorage.getItem(pointerKey)).toBeNull();
  });
  for (const automatic of [false, true]) {
    it(`drops delayed ${automatic ? "automatic" : "manual"} recovery ${outcome} after logout`, async () => {
      const held = deferred<Awaited<ReturnType<typeof api.instructionReviewStatus>>>(); vi.mocked(api.instructionReviewStatus).mockReturnValue(held.promise);
      if (automatic) localStorage.setItem(pointerKey, JSON.stringify({ nonce: review.nonce, task_id: task.task_id }));
      else vi.mocked(api.approveInstructions).mockRejectedValueOnce(new Error("response lost"));
      mount();
      if (!automatic) { await preview(); checkBoth(); fireEvent.click(screen.getByRole("button", { name: acceptButton })); await screen.findByText("response lost"); fireEvent.click(screen.getByRole("button", { name: reconcileButton })); }
      await waitFor(() => expect(api.instructionReviewStatus).toHaveBeenCalledOnce()); changeSession(null);
      await act(async () => { if (outcome === "resolve") held.resolve({ task_id: task.task_id, task, consumed: true, expired: false }); else held.reject(new Error("old recovery error")); });
      await screen.findByRole("alert"); noPrivate(); expect(localStorage.getItem(pointerKey)).toBeNull();
    });
  }
}
it("owner prop changes immediately discard text and confirmations", async () => {
  const view = mount(); await preview(); checkBoth(); changeSession(otherOwner); view.rerender(reviewElement(otherOwner)); noPrivate();
  vi.mocked(api.previewInstructions).mockResolvedValue({ ...review, owner: otherOwner });
  await preview(); expect(screen.getAllByRole("checkbox").every(box => box.getAttribute("aria-checked") === "false")).toBe(true);
});
it("logout and same-owner login cannot revive an old preview or unlock a new request", async () => {
  const old = deferred<InstructionReview>(); const next = deferred<InstructionReview>();
  vi.mocked(api.previewInstructions).mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
  principal.id = owner; const view = mount(); fireEvent.click(screen.getByRole("button", { name: previewButton })); await waitFor(() => expect(api.previewInstructions).toHaveBeenCalledOnce());
  principal.id = null; view.rerender(reviewElement()); noPrivate();
  principal.id = owner; view.rerender(reviewElement()); fireEvent.click(screen.getByRole("button", { name: previewButton })); await waitFor(() => expect(api.previewInstructions).toHaveBeenCalledTimes(2));
  await act(async () => old.resolve(review)); noPrivate(); expect(screen.getByRole("button", { name: previewButton })).toBeDisabled();
  await act(async () => next.resolve(review)); await screen.findByText(review.sources[1].text!); expect(screen.getAllByRole("checkbox").every(box => box.getAttribute("aria-checked") === "false")).toBe(true);
});
it("focus revalidation removes already displayed private text and pointer on a session error", async () => {
  mount(); await preview(); checkBoth(); localStorage.setItem(pointerKey, JSON.stringify({ nonce: review.nonce, task_id: task.task_id }));
  vi.mocked(api.owner).mockRejectedValue(new Error("session unavailable")); fireEvent(window, new Event("focus")); await screen.findByRole("alert"); noPrivate(); expect(localStorage.getItem(pointerKey)).toBeNull();
});
it("automatic recovery validates the current session before requesting a private receipt", async () => {
  localStorage.setItem(pointerKey, JSON.stringify({ nonce: review.nonce, task_id: task.task_id })); changeSession(otherOwner); mount();
  await screen.findByRole("alert"); noPrivate(); expect(api.instructionReviewStatus).not.toHaveBeenCalled(); expect(api.get).not.toHaveBeenCalled(); expect(localStorage.getItem(pointerKey)).toBeNull();
});
function panelDefaults() {
  vi.mocked(api.status).mockResolvedValue({ enabled: true, running: true, model: "local", allowed_roots: ["/fixture"], max_concurrency: 1, queued: 0, limits: {}, supports_task_routes: true, supported_providers: ["openai_compatible_local", "codex-cli"], default_route: { provider: "codex-cli", model: "gpt-5.6-sol" } });
  vi.mocked(api.list).mockResolvedValue({ tasks: [task] }); vi.mocked(api.get).mockResolvedValue({ task }); vi.mocked(api.tail).mockResolvedValue({ events: [], truncated: false });
}
it("panel context switch clears an unknown approval pointer and ignores its late task", async () => {
  panelDefaults(); principal.id = owner;
  const held = deferred<{ task: LocalTask; created: boolean }>(); vi.mocked(api.approveInstructions).mockReturnValue(held.promise);
  const view = render(<OperationsLocalTaskPanel />); await screen.findByRole("button", { name: previewButton });
  fireEvent.change(screen.getByLabelText("作業フォルダー"), { target: { value: "/fixture" } }); fireEvent.change(screen.getByLabelText("目的"), { target: { value: "Read fixture" } });
  await preview(); checkBoth(); fireEvent.click(screen.getByRole("button", { name: acceptButton })); await waitFor(() => expect(api.approveInstructions).toHaveBeenCalledOnce());
  principal.id = null; view.rerender(<OperationsLocalTaskPanel />); noPrivate(); await waitFor(() => expect(localStorage.getItem(pointerKey)).toBeNull());
  principal.id = otherOwner; changeSession(otherOwner); view.rerender(<OperationsLocalTaskPanel />);
  await act(async () => held.resolve({ task: { ...task, summary_text: "A private report" }, created: true })); expect(screen.queryByText("A private report")).toBeNull();
});
for (const outcome of ["resolve", "reject"] as const) {
  it(`panel discards private result/tail on delayed ${outcome} after live logout`, async () => {
    panelDefaults(); const saved = { ...task, status: "completed" as const, is_terminal: true, summary_text: "A private report" };
    vi.mocked(api.list).mockResolvedValue({ tasks: [saved] }); vi.mocked(api.get).mockResolvedValue({ task: saved }); vi.mocked(api.tail).mockResolvedValue({ events: ["A private log"], truncated: false });
    render(<OperationsLocalTaskPanel />); await screen.findByRole("button", { name: /fixture/u });
    fireEvent.click(screen.getByRole("button", { name: /fixture/u })); await screen.findByText("A private report");
    const held = deferred<{ task: LocalTask }>(); vi.mocked(api.get).mockReturnValue(held.promise);
    fireEvent.click(screen.getByRole("button", { name: "結果を再読込" })); await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2)); changeSession(null);
    await act(async () => { if (outcome === "resolve") held.resolve({ task: saved }); else held.reject(new LocalTaskApiError("private missing", 404)); });
    await screen.findByRole("alert"); expect(screen.queryByText("A private report")).toBeNull(); expect(screen.queryByText("A private log")).toBeNull();
  });
}

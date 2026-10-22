// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OperationsLocalTaskPanel } from "./local-task-panel";
import { operationsLocalTasksApi as api, codexProfileApi, codexTaskConnectionKey, LocalTaskApiError, pendingLocalTaskKey, readingTaskBody, type LocalTask } from "@/lib/operations-local-tasks-api";
const sessionScope = vi.hoisted(() => ({ owner: undefined as string | undefined }));
vi.mock("@/components/providers/swr-global-provider", () => ({ useCurrentUserId: () => sessionScope.owner }));
vi.mock("@/lib/operations-local-tasks-api", async original => ({ ...await original<typeof import("@/lib/operations-local-tasks-api")>(), operationsLocalTasksApi: { owner: vi.fn(), status: vi.fn(), list: vi.fn(), get: vi.fn(), tail: vi.fn(), create: vi.fn(), cancel: vi.fn() }, codexProfileApi: { status: vi.fn(), disconnect: vi.fn() } }));
const owner = { authenticated: true, user: { id: "owner-a", role: "admin" } };
const task: LocalTask = { task_id: "a".repeat(32), status: "completed", goal: "Known saved task", workspace: "/allowed/proof", mode: "read_only", is_terminal: true, summary_text: "Verified marker report", llm: { provider: "openai_compatible_local", model: "configured-local", effort: "thinking" }, counters: { llm_calls: 2, tool_calls: 1 }, changed_files: [] };
const status = { enabled: true, running: true, model: "configured-local", allowed_roots: ["/allowed"], max_concurrency: 1, queued: 0, limits: { max_max_tool_rounds: 12, max_timeout_sec: 3600 } };
const input = { goal: "Read a known marker", workspace: "/allowed/proof", completion_criteria: ["Report the marker"], max_tool_rounds: 4, timeout_sec: 120 };
const codexRoute = { provider: "codex-cli", model: "gpt-5.6-sol", effort: "high" };
const connectedProfile = { enabled: true, state: "connected", connected: true, model_ready: false,
  connector_available: true, generation: 1, profile_id: "fixture", login_started: false };
function enableProfile() {
  vi.mocked(api.status).mockResolvedValue({ ...status, supports_task_routes: true, supported_providers: ["codex-cli"],
    default_route: codexRoute, codex_app_profile_connection_enabled: true, codex_app_profile_connector_available: true });
  vi.mocked(codexProfileApi.status).mockResolvedValue(connectedProfile);
  vi.mocked(codexProfileApi.disconnect).mockResolvedValue({ ...connectedProfile, state: "disconnected", connected: false, generation: 2 });
}
async function chooseConnection(name: string) {
  const user = userEvent.setup(); await user.click(screen.getByLabelText("Codex接続先"));
  await user.click(await screen.findByRole("option", { name }));
}
async function chooseProfile() {
  fireEvent.click(screen.getByRole("button", { name: "接続条件を確認" }));
  await screen.findByText(/公式接続を確認しました/u);
  await chooseConnection("本人が接続した専用profile");
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
async function ready() { const result = render(<OperationsLocalTaskPanel />); await screen.findByRole("form", { name: "ローカル読み取りタスクを作成" }); return result; }
function fill() { fireEvent.change(screen.getByLabelText("作業フォルダー"), { target: { value: input.workspace } }); fireEvent.change(screen.getByLabelText("目的"), { target: { value: input.goal } }); fireEvent.change(screen.getByLabelText("完了条件（1行1件）"), { target: { value: input.completion_criteria.join("\n") } }); }
beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); sessionScope.owner = undefined;
  vi.mocked(api.owner).mockResolvedValue(owner); vi.mocked(api.status).mockResolvedValue(status);
  vi.mocked(api.list).mockResolvedValue({ tasks: [task] }); vi.mocked(api.get).mockResolvedValue({ task });
  vi.mocked(api.tail).mockResolvedValue({ events: [{ type: "finished", text: "saved log" }], truncated: false });
  vi.mocked(api.create).mockResolvedValue({ task, created: true }); vi.mocked(api.cancel).mockResolvedValue({ task: { ...task, status: "cancelled" } });
});
describe("Operations local reading task UI", () => {
  it("selects the owned profile only after explicit connection confirmation and task selection", async () => {
    enableProfile();
    await ready(); fill();
    expect(screen.queryByLabelText("Codex接続先")).toBeNull();
    expect(codexProfileApi.status).not.toHaveBeenCalled();
    await chooseProfile();
    const connection = screen.getByLabelText("Codex接続先");
    expect(connection).toHaveTextContent("本人が接続した専用profile");
    fireEvent.submit(screen.getByRole("form"));
    await screen.findByText("Verified marker report");
    expect(vi.mocked(api.create).mock.calls[0][0]).toMatchObject({ codex_connection: "app_profile", provider: "codex-cli", allow_commands: false });
    fireEvent.click(screen.getByRole("button", { name: "切断・承認待ちを破棄" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "タスクを保存して実行" })).toBeDisabled());
    expect(connection).toHaveTextContent("本人が接続した専用profile");
    fireEvent.submit(screen.getByRole("form")); expect(api.create).toHaveBeenCalledTimes(1);
    await chooseConnection("既存のホスト接続"); fireEvent.submit(screen.getByRole("form"));
    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.create).mock.calls[1][0]).not.toHaveProperty("codex_connection");
    expect(localStorage.getItem(codexTaskConnectionKey("owner-a"))).toBe("host");
  });
  it("keeps the selected profile and blocks POST while a fresh review is unresolved", async () => {
    enableProfile(); await ready(); fill(); await chooseProfile();
    const held = deferred<typeof connectedProfile>(); vi.mocked(codexProfileApi.status).mockReturnValueOnce(held.promise);
    fireEvent.click(screen.getByRole("button", { name: "接続条件を確認" }));
    await waitFor(() => expect(codexProfileApi.status).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("Codex接続先")).toHaveTextContent("本人が接続した専用profile");
    expect(screen.getByRole("button", { name: "タスクを保存して実行" })).toBeDisabled();
    fireEvent.submit(screen.getByRole("form")); expect(api.create).not.toHaveBeenCalled();
    await act(async () => held.resolve(connectedProfile));
    await waitFor(() => expect(screen.getByRole("button", { name: "タスクを保存して実行" })).toBeEnabled());
    fireEvent.submit(screen.getByRole("form")); await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.create).mock.calls[0][0]).toHaveProperty("codex_connection", "app_profile");
  });
  it("holds the profile selection after review status503 until an explicit successful review", async () => {
    enableProfile(); await ready(); fill(); await chooseProfile();
    vi.mocked(codexProfileApi.status).mockRejectedValueOnce(new LocalTaskApiError("profile status503", 503));
    fireEvent.click(screen.getByRole("button", { name: "接続条件を確認" })); await screen.findByText(/profile status503/u);
    expect(screen.getByLabelText("Codex接続先")).toHaveTextContent("本人が接続した専用profile");
    expect(screen.getByRole("button", { name: "タスクを保存して実行" })).toBeDisabled();
    fireEvent.submit(screen.getByRole("form")); expect(api.create).not.toHaveBeenCalled();
    expect(localStorage.getItem(codexTaskConnectionKey("owner-a"))).toBe("app_profile");
    fireEvent.click(screen.getByRole("button", { name: "接続条件を確認" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "タスクを保存して実行" })).toBeEnabled());
    fireEvent.submit(screen.getByRole("form")); await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.create).mock.calls[0][0]).toHaveProperty("codex_connection", "app_profile");
  });
  for (const outcome of ["expired", "status503"] as const) it(`rechecks the selected profile before task POST and blocks ${outcome}`, async () => {
    enableProfile(); await ready(); fill(); await chooseProfile();
    if (outcome === "expired") vi.mocked(codexProfileApi.status).mockResolvedValueOnce({ ...connectedProfile, state: "disconnected", connected: false });
    else vi.mocked(codexProfileApi.status).mockRejectedValueOnce(new LocalTaskApiError("profile status503", 503));
    fireEvent.submit(screen.getByRole("form"));
    await screen.findByText(outcome === "expired" ? "専用Codex接続を確認してから実行してください" : "profile status503");
    expect(screen.getByLabelText("Codex接続先")).toHaveTextContent("本人が接続した専用profile");
    expect(screen.getByRole("button", { name: "タスクを保存して実行" })).toBeDisabled();
    expect(api.create).not.toHaveBeenCalled(); expect(localStorage.getItem(pendingLocalTaskKey("owner-a"))).toBeNull();
    fireEvent.submit(screen.getByRole("form")); expect(api.create).not.toHaveBeenCalled();
  });
  it("invalidates confirmation after cancelling an active task without changing the route choice", async () => {
    enableProfile(); const active = { ...task, status: "running" as const, is_terminal: false };
    vi.mocked(api.get).mockResolvedValue({ task: active }); await ready(); fill(); await chooseProfile();
    fireEvent.click(screen.getByRole("button", { name: /Known saved task/u }));
    const cancel = await screen.findByRole("button", { name: "このタスクを中止" });
    vi.mocked(api.get).mockResolvedValue({ task: { ...task, status: "cancelled" } }); fireEvent.click(cancel);
    await waitFor(() => expect(api.cancel).toHaveBeenCalledWith(task.task_id));
    await waitFor(() => expect(screen.getByRole("button", { name: "タスクを保存して実行" })).toBeDisabled());
    expect(screen.getByLabelText("Codex接続先")).toHaveTextContent("本人が接続した専用profile");
    fireEvent.submit(screen.getByRole("form")); expect(api.create).not.toHaveBeenCalled();
  });
  it("restores only the owner-scoped route preference after reload, requiring fresh confirmation", async () => {
    enableProfile(); const first = await ready(); await chooseProfile(); first.unmount(); vi.mocked(codexProfileApi.status).mockClear();
    await ready(); fill();
    expect(screen.getByLabelText("Codex接続先")).toHaveTextContent("本人が接続した専用profile");
    expect(screen.getByRole("button", { name: "タスクを保存して実行" })).toBeDisabled();
    expect(codexProfileApi.status).not.toHaveBeenCalled(); fireEvent.submit(screen.getByRole("form")); expect(api.create).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "接続条件を確認" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "タスクを保存して実行" })).toBeEnabled());
    fireEvent.submit(screen.getByRole("form")); await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.create).mock.calls[0][0]).toHaveProperty("codex_connection", "app_profile");
  });
  it("preserves the exact app-profile pending receipt over a conflicting host preference", async () => {
    enableProfile(); const savedInput = { ...input, route: codexRoute, codex_connection: "app_profile" as const };
    const pending = { owner: "owner-a", key: "receipt_app_123", input: savedInput, body: readingTaskBody(savedInput) };
    localStorage.setItem(pendingLocalTaskKey("owner-a"), JSON.stringify(pending));
    localStorage.setItem(codexTaskConnectionKey("owner-a"), "host"); await ready();
    expect(screen.getByLabelText("Codex接続先")).toHaveTextContent("本人が接続した専用profile");
    expect(screen.getByRole("button", { name: "同じ受付キーで再照合" })).toBeDisabled();
    fireEvent.submit(screen.getByRole("form")); expect(api.create).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "接続条件を確認" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "同じ受付キーで再照合" })).toBeEnabled());
    fireEvent.submit(screen.getByRole("form")); await waitFor(() => expect(api.create).toHaveBeenCalledWith(pending.body, pending.key));
  });
  it("does not carry an owner choice or delayed confirmation into the next live owner", async () => {
    enableProfile(); sessionScope.owner = "owner-a"; const view = await ready(); fill(); await chooseProfile();
    const held = deferred<typeof connectedProfile>(); vi.mocked(codexProfileApi.status).mockReturnValueOnce(held.promise);
    fireEvent.click(screen.getByRole("button", { name: "接続条件を確認" }));
    await waitFor(() => expect(codexProfileApi.status).toHaveBeenCalledTimes(2));
    sessionScope.owner = "owner-b"; vi.mocked(api.owner).mockResolvedValue({ authenticated: true, user: { id: "owner-b", role: "admin" } });
    view.rerender(<OperationsLocalTaskPanel />); await screen.findByRole("form");
    await act(async () => held.resolve(connectedProfile));
    expect(screen.queryByLabelText("Codex接続先")).toBeNull();
    expect(localStorage.getItem(codexTaskConnectionKey("owner-a"))).toBe("app_profile");
    expect(localStorage.getItem(codexTaskConnectionKey("owner-b"))).toBeNull(); expect(api.create).not.toHaveBeenCalled();
  });
  it("does not expose or call task APIs without an authenticated administrator", async () => {
    vi.mocked(api.owner).mockResolvedValue({ authenticated: true, user: { id: "reader", role: "user" } });
    render(<OperationsLocalTaskPanel />); await screen.findByRole("alert");
    expect(api.status).not.toHaveBeenCalled(); expect(api.list).not.toHaveBeenCalled(); expect(api.create).not.toHaveBeenCalled(); expect(screen.queryByRole("form")).toBeNull();
  });
  it("persists a read-only intent before POST and reads its actual saved result", async () => {
    vi.mocked(api.create).mockImplementation(async (body, key) => { expect(JSON.parse(localStorage.getItem(pendingLocalTaskKey("owner-a"))!)).toMatchObject({ key, body }); return { task, created: true }; });
    await ready(); fill(); fireEvent.click(screen.getByRole("button", { name: "タスクを保存して実行" }));
    const result = await screen.findByRole("region", { name: "ローカルタスクの結果" }); await within(result).findByText("Verified marker report");
    expect(api.create).toHaveBeenCalledTimes(1); expect(vi.mocked(api.create).mock.calls[0][0]).toEqual(readingTaskBody(input));
    expect(result).toHaveTextContent("configured-local"); expect(result).toHaveTextContent("thinking"); expect(result).toHaveTextContent("LLM呼出: 2"); expect(result).toHaveTextContent("saved log");
    expect(localStorage.getItem(pendingLocalTaskKey("owner-a"))).toBeNull(); expect(screen.queryByLabelText("実行provider")).toBeNull();
  });
  it("reuses exactly the same intent after response loss and page reload", async () => {
    vi.mocked(api.create).mockRejectedValueOnce(new Error("response lost"));
    const first = await ready(); fill(); fireEvent.click(screen.getByRole("button", { name: "タスクを保存して実行" })); await screen.findByText("response lost");
    const [body, key] = vi.mocked(api.create).mock.calls[0]; expect(screen.getByLabelText("目的")).toBeDisabled(); first.unmount();
    await ready(); expect(api.create).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "同じ受付キーで再照合" })); await screen.findByText("Verified marker report");
    expect(api.create).toHaveBeenNthCalledWith(2, body, key);
  });
  it("blocks changed owner before POST and preserves an existing intent", async () => {
    const pending = { owner: "owner-a", key: "receipt_123", input, body: readingTaskBody(input) }; localStorage.setItem(pendingLocalTaskKey("owner-a"), JSON.stringify(pending));
    await ready(); vi.mocked(api.owner).mockResolvedValue({ authenticated: true, user: { id: "owner-b", role: "admin" } });
    fireEvent.click(screen.getByRole("button", { name: "同じ受付キーで再照合" })); await screen.findByText(/ログイン中の所有者を確認できません/u);
    expect(api.create).not.toHaveBeenCalled(); expect(localStorage.getItem(pendingLocalTaskKey("owner-a"))).not.toBeNull();
  });
  it("blocks tampered pending mutation while keeping saved task readback available", async () => {
    localStorage.setItem(pendingLocalTaskKey("owner-a"), JSON.stringify({ owner: "owner-a", key: "receipt_123", input, body: { ...readingTaskBody(input), allow_commands: true } }));
    await ready(); expect(screen.getByRole("button", { name: "タスクを保存して実行" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /Known saved task/u })); await screen.findByText("Verified marker report"); expect(api.create).not.toHaveBeenCalled();
  });
  it("prevents duplicate submission while the first acceptance is unresolved", async () => {
    const waiting = deferred<{ task: LocalTask; created: boolean }>(); vi.mocked(api.create).mockReturnValue(waiting.promise);
    await ready(); fill(); const form = screen.getByRole("form"); fireEvent.submit(form); fireEvent.submit(form);
    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1)); await act(async () => waiting.resolve({ task, created: true }));
  });
  it("unlocks a rejected invalid request but never silently resubmits", async () => {
    vi.mocked(api.create).mockRejectedValue(new LocalTaskApiError("workspace rejected", 422)); await ready(); fill(); fireEvent.submit(screen.getByRole("form"));
    await screen.findByText("workspace rejected"); expect(localStorage.getItem(pendingLocalTaskKey("owner-a"))).toBeNull(); expect(screen.getByLabelText("目的")).not.toBeDisabled(); expect(api.create).toHaveBeenCalledTimes(1);
  });
  it("keeps an uncertain server error locked to its receipt key", async () => {
    vi.mocked(api.create).mockRejectedValue(new LocalTaskApiError("server failed", 500)); await ready(); fill(); fireEvent.submit(screen.getByRole("form"));
    await screen.findByText("server failed"); expect(localStorage.getItem(pendingLocalTaskKey("owner-a"))).not.toBeNull(); expect(screen.getByLabelText("目的")).toBeDisabled();
  });
  it("keeps an earlier unknown acceptance when revalidation now rejects the same key", async () => {
    const pending = { owner: "owner-a", key: "receipt_123", input, body: readingTaskBody(input) }; localStorage.setItem(pendingLocalTaskKey("owner-a"), JSON.stringify(pending));
    vi.mocked(api.create).mockRejectedValue(new LocalTaskApiError("configuration changed", 422)); await ready(); fireEvent.submit(screen.getByRole("form"));
    await screen.findByText("configuration changed"); expect(localStorage.getItem(pendingLocalTaskKey("owner-a"))).toEqual(JSON.stringify(pending)); expect(screen.getByLabelText("目的")).toBeDisabled();
  });
  it("does not let a slower old task response replace a newly selected task", async () => {
    const second = { ...task, task_id: "b".repeat(32), goal: "Second task", summary_text: "Second report" }; const slow = deferred<{ task: LocalTask }>();
    vi.mocked(api.list).mockResolvedValue({ tasks: [task, second] }); vi.mocked(api.get).mockImplementation(id => id === task.task_id ? slow.promise : Promise.resolve({ task: second }));
    await ready(); fireEvent.click(screen.getByRole("button", { name: /Known saved task/u })); fireEvent.click(screen.getByRole("button", { name: /Second task/u })); await screen.findByText("Second report");
    await act(async () => slow.resolve({ task })); expect(screen.getByRole("region", { name: "ローカルタスクの結果" })).toHaveTextContent("Second report"); expect(screen.queryByText("Verified marker report")).toBeNull();
  });
  it("displays failed saved tasks without reporting successful model execution", async () => {
    vi.mocked(api.get).mockResolvedValue({ task: { ...task, status: "failed", llm: null, summary_text: null, failure_reason: "runtime_unavailable" } });
    await ready(); fireEvent.click(screen.getByRole("button", { name: /Known saved task/u })); const result = await screen.findByRole("region", { name: "ローカルタスクの結果" });
    expect(result).toHaveTextContent("実行状態: 失敗"); expect(result).toHaveTextContent("runtime_unavailable"); expect(result).toHaveTextContent("実行経路: 未記録");
  });
  it("does not submit while the worker is stopped", async () => {
    vi.mocked(api.status).mockResolvedValue({ ...status, running: false }); await ready(); fill(); fireEvent.submit(screen.getByRole("form")); expect(api.create).not.toHaveBeenCalled();
  });
  it("uses the real manager limit keys", async () => {
    await ready(); expect(screen.getByLabelText("Tool回数上限")).toHaveAttribute("max", "12"); expect(screen.getByLabelText("時間上限（秒）")).toHaveAttribute("max", "3600");
  });
});

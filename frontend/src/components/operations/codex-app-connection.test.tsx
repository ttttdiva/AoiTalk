// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CodexAppProfileNotice } from "./codex-app-profile-notice";
import { codexProfileApi as api, operationsLocalTasksApi } from "@/lib/operations-local-tasks-api";
vi.mock("@/lib/operations-local-tasks-api", async original => ({ ...await original<typeof import("@/lib/operations-local-tasks-api")>(),
  operationsLocalTasksApi: { owner: vi.fn() }, codexProfileApi: { status: vi.fn(), handoff: vi.fn(), approve: vi.fn(), login: vi.fn(), poll: vi.fn(), disconnect: vi.fn() } }));
const owner = { authenticated: true, user: { id: "owner-a", role: "admin" } };
const status = { enabled: true, state: "disconnected", profile_id: null, generation: 0, connected: false, model_ready: false, login_started: false, connector_available: true };
const scopes = ["openid", "profile", "email", "offline_access", "api.connectors.read", "api.connectors.invoke"];
const handoff = { ...status, state: "awaiting_user_approval", nonce: "a".repeat(32), spec_digest: "b".repeat(64), expires_in_seconds: 300,
  intended_use: "operations_read_only" as const, credential_store: "keyring_only" as const, oauth_scopes: scopes, login_method: "official_browser_or_device" as const,
  required_approvals: [], token_import_supported: false as const, disclosure: { version: 1, cli_version: "0.160.0", storage_destination: "C:/fixture-private/owner-a", storage_provisioned: false as const, credential_saved: false as const,
    credential_store: "keyring_only" as const, windows_storage: "os_keyring_key_and_encrypted_codex_home_secrets" as const, credential_lifetime: "no_fixed_expiry_refresh_possible" as const,
    connection_lifetime: "memory_only_until_disconnect_or_restart" as const, oauth_scopes: scopes, intended_use: "operations_read_only" as const, usage: "owner_attributed_observed_usage" as const,
    disconnect_effects: [], disconnect_removes_credentials: false as const, disconnect_revokes_provider_grant: false as const, official_logout_required_separately: true as const,
    official_connector_available: true, connector_spec: { expected_account: ["fixture@example.invalid", "fixture-workspace", "https://chatgpt.com"] }, approval_receipt_seconds: 60, login_wait_seconds: 300 } };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
const invalid = vi.fn();
function mount() { return render(<CodexAppProfileNotice owner="owner-a" enabled onSessionInvalid={invalid} />); }
async function review() { fireEvent.click(screen.getByRole("button", { name: "接続条件を確認" })); await screen.findByRole("checkbox", { name: /本人として/u }); }
function checkAll() { screen.getAllByRole("checkbox").forEach(box => fireEvent.click(box)); }
beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear();
  vi.mocked(operationsLocalTasksApi.owner).mockResolvedValue(owner);
  vi.mocked(api.status).mockResolvedValue(status); vi.mocked(api.handoff).mockResolvedValue(handoff);
  vi.mocked(api.approve).mockResolvedValue({ approval_receipt: "c".repeat(32), expires_in_seconds: 60 });
  vi.mocked(api.login).mockResolvedValue({ login_id: "fixture-login", auth_url: "https://auth.openai.com/oauth/authorize?fixture", expires_in_seconds: 300 });
  vi.mocked(api.poll).mockResolvedValue({ ...status, state: "connected", connected: true });
  vi.mocked(api.disconnect).mockResolvedValue(status);
});
describe("Explicit app profile approval", () => {
  it("keeps default disclosure inert, including automatic requests", () => {
    render(<CodexAppProfileNotice />);
    expect(screen.queryByRole("button")).toBeNull(); expect(screen.getByText(scopes.join(" "))).toBeVisible();
    expect(api.status).not.toHaveBeenCalled(); expect(operationsLocalTasksApi.owner).not.toHaveBeenCalled();
  });
  it("requires all four fresh confirmations and never automatically opens or polls login", async () => {
    mount(); expect(api.status).not.toHaveBeenCalled(); await review();
    const boxes = screen.getAllByRole("checkbox"), start = screen.getByRole("button", { name: "承認して公式ログインを準備" });
    boxes.forEach(box => expect(box).not.toBeChecked()); expect(start).toBeDisabled();
    boxes.slice(0, 3).forEach(box => fireEvent.click(box)); expect(start).toBeDisabled();
    fireEvent.click(boxes[3]); fireEvent.click(start);
    await screen.findByRole("link", { name: "公式ログインページを開く" });
    expect(api.approve).toHaveBeenCalledTimes(1); expect(api.login).toHaveBeenCalledWith("c".repeat(32)); expect(api.poll).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "ログイン完了を確認" }));
    await screen.findByText(/公式接続を確認しました/u); expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("モデル利用とタスク権限は別の確認が必要");
  });
  it("invalidates the parent if API owner changes before context changes", async () => {
    mount(); await review();
    vi.mocked(operationsLocalTasksApi.owner).mockResolvedValue({ authenticated: true, user: { id: "owner-b", role: "admin" } });
    checkAll(); fireEvent.click(screen.getByRole("button", { name: "承認して公式ログインを準備" }));
    await waitFor(() => expect(invalid).toHaveBeenCalledTimes(1));
    expect(api.approve).not.toHaveBeenCalled(); expect(screen.queryByText("C:/fixture-private/owner-a", { exact: false })).toBeNull();
  });
  it("drops a delayed handoff after logout and invalidates parent results", async () => {
    const slow = deferred<typeof handoff>(); vi.mocked(api.handoff).mockReturnValue(slow.promise);
    mount(); fireEvent.click(screen.getByRole("button", { name: "接続条件を確認" })); await waitFor(() => expect(api.handoff).toHaveBeenCalled());
    vi.mocked(operationsLocalTasksApi.owner).mockResolvedValue({ authenticated: false });
    await act(async () => slow.resolve(handoff)); await waitFor(() => expect(invalid).toHaveBeenCalled());
    expect(screen.queryByRole("checkbox")).toBeNull(); expect(api.approve).not.toHaveBeenCalled();
  });
  it("does not begin login when ownership changes after approval", async () => {
    const slow = deferred<{ approval_receipt: string; expires_in_seconds: number }>(); vi.mocked(api.approve).mockReturnValue(slow.promise);
    mount(); await review(); checkAll(); fireEvent.click(screen.getByRole("button", { name: "承認して公式ログインを準備" })); await waitFor(() => expect(api.approve).toHaveBeenCalled());
    vi.mocked(operationsLocalTasksApi.owner).mockResolvedValue({ authenticated: false });
    await act(async () => slow.resolve({ approval_receipt: "c".repeat(32), expires_in_seconds: 60 })); await waitFor(() => expect(invalid).toHaveBeenCalled());
    expect(api.login).not.toHaveBeenCalled(); expect(screen.queryByRole("link")).toBeNull();
  });
  it("cannot restore the old owner's response into a remounted owner view", async () => {
    const slow = deferred<typeof handoff>(); vi.mocked(api.handoff).mockReturnValue(slow.promise);
    const view = mount(); fireEvent.click(screen.getByRole("button", { name: "接続条件を確認" })); await waitFor(() => expect(api.handoff).toHaveBeenCalled());
    view.rerender(<CodexAppProfileNotice owner="owner-b" enabled onSessionInvalid={invalid} />);
    await act(async () => slow.resolve(handoff));
    expect(screen.queryByRole("checkbox")).toBeNull(); expect(screen.queryByText(/fixture@example.invalid/u)).toBeNull();
  });
  it("resets confirmations after lost approval and requires explicit discard", async () => {
    vi.mocked(api.approve).mockRejectedValue(new Error("response lost")); mount(); await review(); checkAll();
    fireEvent.click(screen.getByRole("button", { name: "承認して公式ログインを準備" })); await screen.findByRole("alert");
    screen.getAllByRole("checkbox").forEach(box => expect(box).not.toBeChecked()); expect(api.login).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "切断・承認待ちを破棄" })); await waitFor(() => expect(api.disconnect).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("checkbox")).toBeNull());
    await review(); screen.getAllByRole("checkbox").forEach(box => expect(box).not.toBeChecked());
  });
});

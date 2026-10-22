// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PersonaBridgeConnectionPanel } from "./persona-bridge-connection";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import type { BridgeConnectionPreview, BridgeConnectionPreviewRequest, BridgeConnectionSave } from "@/lib/media-bridge-connection-api";

const api = vi.hoisted(() => ({ devices: vi.fn(), preview: vi.fn(), save: vi.fn(), refresh: vi.fn() }));
vi.mock("@/lib/media-browser-preparation-api", async original => ({ ...await original<typeof import("@/lib/media-browser-preparation-api")>(), browserPreparationApi: { devices: api.devices } }));
vi.mock("@/lib/media-bridge-connection-api", async original => ({ ...await original<typeof import("@/lib/media-bridge-connection-api")>(), bridgeConnectionApi: { preview: api.preview, save: api.save } }));
vi.mock("next/link", () => ({ default: ({ children, ...props }: React.ComponentProps<"a">) => <a {...props}>{children}</a> }));
// eslint-disable-next-line no-restricted-syntax -- controlled test shim; production uses AppSelect.
vi.mock("@/components/ui/app-select", () => ({ AppSelect: ({ onValueChange, children, value, disabled, "aria-label": label }: { onValueChange: (value: string) => void; children: React.ReactNode; value: string; disabled?: boolean; "aria-label"?: string }) => <select aria-label={label} value={value} disabled={disabled} onChange={event => onValueChange(event.target.value)}>{children}</select> }));

const scope = { ownerUserId: "owner", projectId: "brand-project", personaId: "persona", personaRevisionId: "persona-rev", displayName: "UN//REAL" };
const account = { id: "existing-slot", owner_user_id: "owner", project_id: "brand-project", persona_id: "persona", platform: "x", account_ref: "sns-v6:brand:x", status: "paused", connection_id: null, current_revision: { id: "account-rev", platform_account_id: "existing-slot", owner_user_id: "owner", project_id: "brand-project", display_name: "Brand X", content_hash: "a".repeat(64) } } as PlatformAccount;
const context = { context_id: "11111111-1111-4111-8111-111111111111", context_epoch: "22222222-2222-4222-8222-222222222222" };
const preview = (input: BridgeConnectionPreviewRequest): BridgeConnectionPreview => ({ mode: "bridge_manual", owner_user_id: input.owner_user_id, project_id: input.project_id, persona_id: input.persona_id, persona_display_name: scope.displayName, platform_account_id: "existing-slot", platform_account_revision_id: input.expected_account_revision_id, platform_account_revision_hash: input.expected_account_revision_hash, platform: "x", account_display_name: "Brand X", account_ref: input.account_ref, device_id: input.device_id, tab_id: input.tab_id, browser_context_label: input.browser_context_label, google_account_label: input.google_account_label ?? null, browser_context: { ...context }, checked_at: new Date(Date.now() - 1000).toISOString(), expires_at: new Date(Date.now() + 300000).toISOString(), preview_token: "fixture-preview-only", publication_confirmed: false, official_api_verified: false });
const result = (): BridgeConnectionSave => ({ mode: "bridge_manual", publication_confirmed: false, official_api_verified: false, readback_binding_matches: true,
  binding_receipt: { binding_revision_id: "new-account-rev", account_revision_hash: "a".repeat(64), connection_id: "bridge-manual-connection", connection_version: 1,
    owner_user_id: "owner", project_id: "brand-project", persona_id: "persona", platform_account_id: "existing-slot", platform: "x", account_ref: "@own_brand", device_id: "own-pc", tab_id: 7, browser_context_label: "Brand A profile", google_account_label: null, browser_context: { ...context }, saved_at: new Date().toISOString() },
  account: { ...account, account_ref: "@own_brand", status: "active", connection_id: "bridge-manual-connection", current_revision: { ...account.current_revision, id: "new-account-rev" }, revisions: [], revision_history_truncated: false } as BridgeConnectionSave["account"] });
const view = (accounts: PlatformAccount[] = [account], nextScope = scope) => <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0, shouldRetryOnError: false }}><PersonaBridgeConnectionPanel scope={nextScope} accounts={accounts} onSaved={api.refresh} /></SWRConfig>;
const mount = () => render(view());
const selectTarget = async () => {
  fireEvent.change(screen.getByLabelText("接続する既存媒体枠"), { target: { value: "existing-slot" } });
  await waitFor(() => expect(api.devices).toHaveBeenCalled());
  fireEvent.change(screen.getByLabelText("本人が接続するXの@handle"), { target: { value: "@Own_Brand" } });
  await waitFor(() => expect(within(screen.getByLabelText("接続確認に使う本人のPC")).getByRole("option", { name: /本人PC/ })).toBeInTheDocument());
  fireEvent.change(screen.getByLabelText("接続確認に使う本人のPC"), { target: { value: "own-pc" } });
  fireEvent.change(screen.getByLabelText("接続確認する既存タブID"), { target: { value: "7" } });
  fireEvent.change(screen.getByLabelText("本人が使うEdge profileの識別ラベル"), { target: { value: "Brand A profile" } });
};
const reviewPreview = async () => { await selectTarget(); fireEvent.click(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" })); await screen.findByLabelText("Bridge本人接続preview"); };
beforeEach(() => {
  vi.clearAllMocks();
  api.devices.mockResolvedValue([{ id: "own-pc", name: "本人PC", online: true, edge_connected: true, browser_backend: "extension", capabilities: ["browser"] }, { id: "offline", name: "未接続PC", online: false, edge_connected: false, browser_backend: "extension", capabilities: ["browser"] }]);
  api.preview.mockImplementation(async (_account, input) => preview(input)); api.save.mockResolvedValue(result()); api.refresh.mockResolvedValue([account]);
});

describe("existing persona slot Bridge manual connection", () => {
  it("requires explicit slot, identity, owner PC and existing tab before read-only preview", async () => {
    mount(); expect(screen.queryByLabelText("本人が接続するXの@handle")).not.toBeInTheDocument(); expect(api.devices).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("接続する既存媒体枠"), { target: { value: "existing-slot" } });
    expect(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" })).toBeDisabled();
    expect(screen.getByLabelText("本人が接続するXの@handle")).toHaveValue(""); expect(api.preview).not.toHaveBeenCalled(); expect(api.save).not.toHaveBeenCalled();
    expect(screen.getByLabelText("本人が使うEdge profileの識別ラベル")).toHaveValue(""); expect(screen.getByLabelText("接続確認に使う本人のPC")).toHaveValue("");
  });

  it("previews exact brand/project/slot and saves once only after human confirmation without promoting API authority", async () => {
    mount(); await reviewPreview(); const panel = screen.getByLabelText("Bridge本人接続preview");
    expect(panel).toHaveTextContent("UN//REAL · x · @own_brand"); expect(panel).toHaveTextContent("brand-project"); expect(panel).toHaveTextContent("existing-slot"); expect(panel).toHaveTextContent("own-pc · タブ 7"); expect(panel).toHaveTextContent("公式APIは未確認");
    expect(panel).toHaveTextContent("Brand A profile"); expect(panel).toHaveTextContent(context.context_id); expect(panel).toHaveTextContent(context.context_epoch);
    expect(api.preview).toHaveBeenCalledWith(account, { owner_user_id: "owner", project_id: "brand-project", persona_id: "persona", expected_account_revision_id: "account-rev", expected_account_revision_hash: "a".repeat(64), account_ref: "@own_brand", device_id: "own-pc", tab_id: 7, browser_context_label: "Brand A profile", google_account_label: null });
    const button = screen.getByRole("button", { name: "本人が確認してBridge手動接続を保存" }); expect(button).toBeDisabled();
    fireEvent.click(within(panel).getByRole("checkbox")); fireEvent.click(button); fireEvent.click(button);
    await waitFor(() => expect(api.refresh).toHaveBeenCalledTimes(1)); expect(api.save).toHaveBeenCalledTimes(1);
    expect(api.save.mock.calls[0][0].id).toBe("existing-slot"); expect(api.save.mock.calls[0][2]).toMatch(/^bridge-connection:/u);
    expect(screen.getByRole("status")).toHaveTextContent("新しい版を保存してください"); expect(screen.getByRole("status")).toHaveTextContent("公開結果は未確認");
  });

  it.each(["本人が接続するXの@handle", "接続確認する既存タブID", "接続確認に使う本人のPC", "本人が使うEdge profileの識別ラベル"])("invalidates preview and confirmation when %s changes", async label => {
    mount(); await reviewPreview(); fireEvent.click(within(screen.getByLabelText("Bridge本人接続preview")).getByRole("checkbox"));
    fireEvent.change(screen.getByLabelText(label), { target: { value: label.includes("handle") ? "@Other" : label.includes("タブ") ? "8" : "offline" } });
    expect(screen.queryByLabelText("Bridge本人接続preview")).not.toBeInTheDocument(); expect(api.save).not.toHaveBeenCalled();
  });

  it("discards an old preview when the expected account revision changes", async () => {
    const rendered = mount(); await reviewPreview(); rendered.rerender(view([{ ...account, current_revision: { ...account.current_revision, id: "changed-revision" } }]));
    expect(screen.queryByLabelText("Bridge本人接続preview")).not.toBeInTheDocument(); expect(screen.getByLabelText("接続確認する既存タブID")).toHaveValue(null); expect(api.save).not.toHaveBeenCalled();
  });

  it("rejects mismatched owner evidence and never offers a save", async () => {
    api.preview.mockImplementation(async (_account, input) => ({ ...preview(input), owner_user_id: "foreign" })); mount(); await selectTarget();
    fireEvent.click(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" })); await screen.findByRole("alert");
    expect(screen.queryByLabelText("Bridge本人接続preview")).not.toBeInTheDocument(); expect(api.save).not.toHaveBeenCalled();
  });

  it("stops unknown save replay until an explicit state read, then requires a new preview", async () => {
    api.save.mockRejectedValue(new Error("timeout")); mount(); await reviewPreview(); fireEvent.click(within(screen.getByLabelText("Bridge本人接続preview")).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "本人が確認してBridge手動接続を保存" })); await screen.findByRole("alert");
    expect(screen.getByRole("alert")).toHaveTextContent("同じpreviewは再送しません"); expect(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "接続の保存状態を更新" })); await waitFor(() => expect(api.refresh).toHaveBeenCalled());
    expect(api.save).toHaveBeenCalledTimes(1); expect(screen.queryByLabelText("Bridge本人接続preview")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" })).toBeEnabled());
    expect(api.preview).toHaveBeenCalledTimes(1);
  });

  it("ignores an in-flight preview after changing the persona-owned slot", async () => {
    let complete!: (value: BridgeConnectionPreview) => void;
    api.preview.mockImplementation(() => new Promise(resolve => { complete = resolve; })); mount(); await selectTarget();
    fireEvent.click(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" }));
    const input = api.preview.mock.calls[0][1]; fireEvent.change(screen.getByLabelText("接続する既存媒体枠"), { target: { value: "" } });
    await act(async () => complete(preview(input))); expect(screen.queryByLabelText("Bridge本人接続preview")).not.toBeInTheDocument(); expect(api.save).not.toHaveBeenCalled();
  });

  it("filters cross-owner/project/persona slots and does not call unsupported media connected", async () => {
    render(view([{ ...account, owner_user_id: "other" }, { ...account, project_id: "other" }, { ...account, persona_id: "other" }, { ...account, platform: "instagram" }]));
    expect(screen.queryByLabelText("接続する既存媒体枠")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Bridgeで本人の接続先を設定の説明" }));
    expect(await screen.findByText(/X・YouTube以外の媒体はBridge手動接続に未対応/)).toBeVisible();
    expect(api.preview).not.toHaveBeenCalled();
  });

  it("requires an exact UC channel for YouTube and keeps it case-sensitive through preview/save", async () => {
    const channel = "UC" + "Ab".repeat(11);
    const videoAccount = { ...account, platform: "youtube" as const };
    api.preview.mockImplementation(async (_account, input) => ({ ...preview(input), platform: "youtube" }));
    api.save.mockResolvedValue({ ...result(), binding_receipt: { ...result().binding_receipt, platform: "youtube", account_ref: channel, google_account_label: "Brand A Google" }, account: { ...result().account, platform: "youtube", account_ref: channel } });
    render(view([videoAccount])); fireEvent.change(screen.getByLabelText("接続する既存媒体枠"), { target: { value: "existing-slot" } });
    await waitFor(() => expect(within(screen.getByLabelText("接続確認に使う本人のPC")).getByRole("option", { name: /本人PC/ })).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("接続確認に使う本人のPC"), { target: { value: "own-pc" } });
    fireEvent.change(screen.getByLabelText("接続確認する既存タブID"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("本人が使うEdge profileの識別ラベル"), { target: { value: "Brand A profile" } });
    fireEvent.change(screen.getByLabelText("本人が使うGoogleアカウントの識別ラベル"), { target: { value: "Brand A Google" } });
    fireEvent.change(screen.getByLabelText("本人が接続するYouTubeのUC channel ID"), { target: { value: channel.toLowerCase() } });
    expect(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" })).toBeDisabled(); expect(api.preview).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("本人が接続するYouTubeのUC channel ID"), { target: { value: channel } });
    fireEvent.click(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" })); await screen.findByLabelText("Bridge本人接続preview");
    expect(api.preview.mock.calls[0][1].account_ref).toBe(channel);
    fireEvent.click(within(screen.getByLabelText("Bridge本人接続preview")).getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "本人が確認してBridge手動接続を保存" }));
    await waitFor(() => expect(api.refresh).toHaveBeenCalledTimes(1)); expect(api.save.mock.calls[0][1].account_ref).toBe(channel);
  });

  it.each(["slot", "scope", "unmount", "switch-back", "revision"])("suppresses a pending old save callback after %s changes", async change => {
    let complete!: (value: BridgeConnectionSave) => void;
    api.save.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
    const second = { ...account, id: "slot-b", current_revision: { ...account.current_revision, id: "revision-b", platform_account_id: "slot-b", display_name: "Brand B" } };
    const rendered = render(view([account, second])); await reviewPreview();
    fireEvent.click(within(screen.getByLabelText("Bridge本人接続preview")).getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "本人が確認してBridge手動接続を保存" }));
    await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1));
    if (change === "slot" || change === "switch-back") {
      fireEvent.change(screen.getByLabelText("接続する既存媒体枠"), { target: { value: "slot-b" } });
      if (change === "switch-back") fireEvent.change(screen.getByLabelText("接続する既存媒体枠"), { target: { value: "existing-slot" } });
    } else if (change === "scope") {
      const next = { ...scope, personaId: "persona-b", personaRevisionId: "persona-rev-b", projectId: "brand-project-b", displayName: "Brand B" };
      rendered.rerender(view([{ ...account, persona_id: next.personaId, project_id: next.projectId, current_revision: { ...account.current_revision, project_id: next.projectId } }], next));
    } else if (change === "revision") rendered.rerender(view([{ ...account, current_revision: { ...account.current_revision, id: "changed-revision" } }]));
    else rendered.unmount();
    await act(async () => complete(result()));
    expect(api.refresh).not.toHaveBeenCalled(); expect(screen.queryByRole("status")).not.toBeInTheDocument(); expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Bridge本人接続preview")).not.toBeInTheDocument();
  });

  it.each(["device", "tab", "revision", "connection", "readback"])("never displays success for a save receipt with a mismatched %s", async field => {
    const saved = result();
    const receipt = { ...saved.binding_receipt, ...(field === "device" ? { device_id: "other-pc" } : field === "tab" ? { tab_id: 8 } : field === "revision" ? { binding_revision_id: "other-revision" } : field === "connection" ? { connection_id: "other-connection" } : {}) };
    api.save.mockResolvedValue({ ...saved, binding_receipt: receipt, readback_binding_matches: field !== "readback" });
    mount(); await reviewPreview(); fireEvent.click(within(screen.getByLabelText("Bridge本人接続preview")).getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "本人が確認してBridge手動接続を保存" }));
    await screen.findByRole("alert"); expect(api.refresh).not.toHaveBeenCalled(); expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("does not display a late save failure in another selected slot", async () => {
    let reject!: (error: Error) => void;
    api.save.mockImplementation(() => new Promise((_resolve, decline) => { reject = decline; }));
    const second = { ...account, id: "slot-b", current_revision: { ...account.current_revision, platform_account_id: "slot-b" } };
    render(view([account, second])); await reviewPreview();
    fireEvent.click(within(screen.getByLabelText("Bridge本人接続preview")).getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "本人が確認してBridge手動接続を保存" }));
    fireEvent.change(screen.getByLabelText("接続する既存媒体枠"), { target: { value: "slot-b" } });
    await act(async () => reject(new Error("late timeout"))); expect(screen.queryByRole("alert")).not.toBeInTheDocument(); expect(api.refresh).not.toHaveBeenCalled();
  });

  it("keeps the current save usable when the same slot is selected again", async () => {
    let complete!: (value: BridgeConnectionSave) => void;
    api.save.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
    mount(); await reviewPreview(); fireEvent.click(within(screen.getByLabelText("Bridge本人接続preview")).getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "本人が確認してBridge手動接続を保存" }));
    fireEvent.change(screen.getByLabelText("接続する既存媒体枠"), { target: { value: "existing-slot" } });
    await act(async () => complete(result())); expect(api.refresh).toHaveBeenCalledTimes(1); expect(screen.getByRole("status")).toHaveTextContent("同じ媒体枠に保存しました");
  });

  it("does not carry a previously successful persona notification into another scope", async () => {
    const rendered = mount(); await reviewPreview(); fireEvent.click(within(screen.getByLabelText("Bridge本人接続preview")).getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "本人が確認してBridge手動接続を保存" }));
    await screen.findByRole("status");
    const next = { ...scope, personaId: "persona-b", personaRevisionId: "persona-rev-b", projectId: "brand-project-b", displayName: "Brand B" };
    rendered.rerender(view([], next)); expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("requires a YouTube Google label declared by the human and discards preview when it changes", async () => {
    api.preview.mockImplementation(async (_account, input) => ({ ...preview(input), platform: "youtube" }));
    render(view([{ ...account, platform: "youtube" }])); fireEvent.change(screen.getByLabelText("接続する既存媒体枠"), { target: { value: "existing-slot" } });
    await waitFor(() => expect(within(screen.getByLabelText("接続確認に使う本人のPC")).getByRole("option", { name: /本人PC/ })).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("本人が接続するYouTubeのUC channel ID"), { target: { value: "UC" + "A".repeat(22) } });
    fireEvent.change(screen.getByLabelText("接続確認に使う本人のPC"), { target: { value: "own-pc" } }); fireEvent.change(screen.getByLabelText("接続確認する既存タブID"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("本人が使うEdge profileの識別ラベル"), { target: { value: "Brand A profile" } });
    expect(screen.getByLabelText("本人が使うGoogleアカウントの識別ラベル")).toHaveValue(""); expect(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("本人が使うGoogleアカウントの識別ラベル"), { target: { value: "Brand A Google" } });
    fireEvent.click(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" })); const panel = await screen.findByLabelText("Bridge本人接続preview");
    expect(panel).toHaveTextContent("Googleアカウントの識別ラベル（本人の申告）"); expect(panel).toHaveTextContent("Brand A Google");
    await userEvent.click(screen.getByRole("button", { name: "Bridge接続手順の説明" }));
    expect(await screen.findByText(/Google本人確認済みの情報ではありません/)).toBeVisible();
    fireEvent.click(within(panel).getByRole("checkbox")); fireEvent.change(screen.getByLabelText("本人が使うGoogleアカウントの識別ラベル"), { target: { value: "Brand B Google" } });
    expect(screen.queryByLabelText("Bridge本人接続preview")).not.toBeInTheDocument(); expect(api.save).not.toHaveBeenCalled();
  });

  it.each(["context_id", "context_epoch", "browser_context_label", "google_account_label"])("refuses a saved profile receipt with mismatched %s", async key => {
    const saved = result(); api.save.mockResolvedValue({ ...saved, binding_receipt: { ...saved.binding_receipt,
      ...(key.startsWith("context_") ? { browser_context: { ...context, [key]: "33333333-3333-4333-8333-333333333333" } } : { [key]: "another-profile-label" }) } });
    mount(); await reviewPreview(); fireEvent.click(within(screen.getByLabelText("Bridge本人接続preview")).getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "本人が確認してBridge手動接続を保存" }));
    await screen.findByRole("alert"); expect(screen.queryByRole("status")).not.toBeInTheDocument(); expect(api.refresh).not.toHaveBeenCalled();
  });

  it.each([undefined, { context_id: "unknown", context_epoch: context.context_epoch }, { ...context, profile: "other" }])("does not offer a save for an unknown or open source context: %o", async browserContext => {
    api.preview.mockImplementation(async (_account, input) => ({ ...preview(input), browser_context: browserContext }));
    mount(); await selectTarget(); fireEvent.click(screen.getByRole("button", { name: "既存タブの本人接続を読み取りで確認" })); await screen.findByRole("alert");
    expect(screen.queryByLabelText("Bridge本人接続preview")).not.toBeInTheDocument(); expect(api.save).not.toHaveBeenCalled();
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { bridgeConnectionApi, bridgeConnectionLabelsValid, bridgeConnectionPreviewMatches, bridgeConnectionRefValid, bridgeConnectionSaveMatches, type BridgeConnectionPreview, type BridgeConnectionPreviewRequest, type BridgeConnectionSave } from "./media-bridge-connection-api";
import type { PlatformAccount } from "./media-operations-setup-api";

const request = vi.hoisted(() => vi.fn());
vi.mock("./agent-employees-api", () => ({ employeeRequest: request }));
const account = { id: "existing-slot", owner_user_id: "owner", project_id: "brand-project", persona_id: "persona", platform: "x", status: "paused", account_ref: "sns-v6:brand:x", connection_id: null, current_revision: { id: "revision", platform_account_id: "existing-slot", owner_user_id: "owner", project_id: "brand-project", content_hash: "a".repeat(64) } } as PlatformAccount;
const context = { context_id: "11111111-1111-4111-8111-111111111111", context_epoch: "22222222-2222-4222-8222-222222222222" };
const input: BridgeConnectionPreviewRequest = { owner_user_id: "owner", project_id: "brand-project", persona_id: "persona", expected_account_revision_id: "revision", expected_account_revision_hash: "a".repeat(64), account_ref: "@Own_Brand", device_id: "own-pc", tab_id: 7, browser_context_label: "Brand A profile", google_account_label: null };
const preview = (): BridgeConnectionPreview => ({ mode: "bridge_manual", owner_user_id: "owner", project_id: "brand-project", persona_id: "persona", persona_display_name: "UN//REAL", platform_account_id: "existing-slot", platform_account_revision_id: "revision", platform_account_revision_hash: "a".repeat(64), platform: "x", account_display_name: "brand", account_ref: "@own_brand", device_id: "own-pc", tab_id: 7, browser_context_label: input.browser_context_label, google_account_label: null, browser_context: { ...context }, checked_at: new Date(Date.now() - 1000).toISOString(), expires_at: new Date(Date.now() + 300000).toISOString(), preview_token: "opaque-fixture-preview", official_api_verified: false, publication_confirmed: false });
const saved = (): BridgeConnectionSave => ({ mode: "bridge_manual", official_api_verified: false, publication_confirmed: false, readback_binding_matches: true,
  binding_receipt: { binding_revision_id: "new-revision", account_revision_hash: "a".repeat(64), connection_id: "manual-connection", connection_version: 1,
    owner_user_id: "owner", project_id: "brand-project", persona_id: "persona", platform_account_id: "existing-slot", platform: "x", account_ref: "@own_brand", device_id: "own-pc", tab_id: 7, browser_context_label: input.browser_context_label, google_account_label: null, browser_context: { ...context }, saved_at: new Date().toISOString() },
  account: { ...account, status: "active", account_ref: "@own_brand", connection_id: "manual-connection", current_revision: { ...account.current_revision, id: "new-revision" }, revisions: [], revision_history_truncated: false } as BridgeConnectionSave["account"] });
beforeEach(() => vi.clearAllMocks());

describe("secret-free Bridge connection contract", () => {
  it("uses only the existing slot preview/save routes and canonical X identity", async () => {
    const value = preview(); request.mockResolvedValueOnce(value).mockResolvedValueOnce(saved());
    expect(await bridgeConnectionApi.preview(account, input)).toEqual(value);
    expect(request).toHaveBeenNthCalledWith(1, "/api/operations/media/platform-accounts/existing-slot/bridge-connection/preview", "POST", { ...input, account_ref: "@own_brand" });
    await bridgeConnectionApi.save(account, value, "save-key");
    expect(request).toHaveBeenNthCalledWith(2, "/api/operations/media/platform-accounts/existing-slot/bridge-connection", "POST", { preview_token: value.preview_token, confirmed: true, idempotency_key: "save-key" });
    expect(request.mock.calls.some(call => /oauth|credential|platform-connections/u.test(call[0]))).toBe(false);
  });

  it.each(["owner_user_id", "project_id", "persona_id", "platform_account_id", "platform_account_revision_id", "platform_account_revision_hash", "platform", "account_ref", "device_id", "tab_id", "browser_context_label", "google_account_label", "browser_context", "mode", "checked_at", "expires_at", "preview_token", "publication_confirmed", "official_api_verified"])("rejects malformed or foreign preview %s", async key => {
    const value = { ...preview(), [key]: key === "tab_id" ? 8 : key.endsWith("confirmed") || key === "official_api_verified" ? true : key === "preview_token" ? "" : "foreign" } as BridgeConnectionPreview;
    request.mockResolvedValue(value); expect(bridgeConnectionPreviewMatches(value, account, input)).toBe(false);
    await expect(bridgeConnectionApi.preview(account, input)).rejects.toThrow("照合できません");
  });

  it.each([{ account_ref: "12345" }, { device_id: "" }, { tab_id: 0 }, { browser_context_label: "" }, { browser_context_label: "   " }, { browser_context_label: "a".repeat(81) }, { google_account_label: "not-allowed-for-x" }, { expected_account_revision_id: "old-revision" }, { expected_account_revision_hash: "bad" }, { owner_user_id: "foreign" }, { project_id: "foreign" }, { persona_id: "foreign" }])("refuses incomplete authority before requesting a preview: %o", async patch => {
    await expect(bridgeConnectionApi.preview(account, { ...input, ...patch })).rejects.toThrow("明示選択"); expect(request).not.toHaveBeenCalled();
  });

  it("accepts a new revision ID with an unchanged content hash, and refuses identity or API-authority promotion", () => {
    const value = preview(); expect(bridgeConnectionSaveMatches(saved(), account, value)).toBe(true);
    for (const patch of [{ id: "new-slot" }, { owner_user_id: "foreign" }, { project_id: "foreign" }, { persona_id: "foreign" }, { platform: "youtube" }, { account_ref: "@other" }, { status: "paused" }, { connection_id: null }, { current_revision: account.current_revision }]) expect(bridgeConnectionSaveMatches({ ...saved(), account: { ...saved().account, ...patch } } as BridgeConnectionSave, account, value)).toBe(false);
    expect(bridgeConnectionSaveMatches({ ...saved(), official_api_verified: true } as unknown as BridgeConnectionSave, account, value)).toBe(false);
    expect(bridgeConnectionSaveMatches({ ...saved(), publication_confirmed: true } as unknown as BridgeConnectionSave, account, value)).toBe(false);
  });

  it("rejects expired preview without resending and keeps YouTube channel case exact", async () => {
    await expect(bridgeConnectionApi.save(account, { ...preview(), expires_at: new Date(Date.now() - 1).toISOString() }, "key")).rejects.toThrow("失効");
    expect(request).not.toHaveBeenCalled();
    expect(bridgeConnectionRefValid("youtube", "UC" + "A".repeat(22))).toBe(true);
    expect(bridgeConnectionRefValid("youtube", "uc" + "A".repeat(22))).toBe(false);
    expect(bridgeConnectionRefValid("instagram", "@own_brand")).toBe(false);
  });

  it.each(["binding_revision_id", "account_revision_hash", "connection_id", "connection_version", "owner_user_id", "project_id", "persona_id", "platform_account_id", "platform", "account_ref", "device_id", "tab_id", "browser_context_label", "google_account_label", "browser_context", "saved_at"])("rejects a save receipt with another or malformed %s", async key => {
    const value = { ...saved(), binding_receipt: { ...saved().binding_receipt, [key]: key === "connection_version" ? 0 : key === "tab_id" ? 8 : "foreign" } } as BridgeConnectionSave;
    expect(bridgeConnectionSaveMatches(value, account, preview())).toBe(false);
    request.mockResolvedValue(value); await expect(bridgeConnectionApi.save(account, preview(), "save-key")).rejects.toThrow("保存結果を照合できません");
  });

  it("refuses a merely newer account revision, a missing receipt, or a mismatched readback", () => {
    const value = preview();
    expect(bridgeConnectionSaveMatches({ ...saved(), binding_receipt: undefined } as unknown as BridgeConnectionSave, account, value)).toBe(false);
    expect(bridgeConnectionSaveMatches({ ...saved(), readback_binding_matches: false }, account, value)).toBe(false);
    expect(bridgeConnectionSaveMatches({ ...saved(), readback_binding_matches: undefined } as unknown as BridgeConnectionSave, account, value)).toBe(false);
    expect(bridgeConnectionSaveMatches({ ...saved(), account: { ...saved().account, current_revision: { ...saved().account.current_revision, id: "unrelated-newer-revision" } } }, account, value)).toBe(false);
    expect(bridgeConnectionSaveMatches({ ...saved(), readback_binding_matches: false, binding_receipt: { ...saved().binding_receipt, connection_version: 2 } }, account, value)).toBe(false);
  });

  it.each(["context_id", "context_epoch"])("refuses receipt %s drift after profile switching or reconnecting", key => {
    const value = { ...saved(), binding_receipt: { ...saved().binding_receipt, browser_context: { ...context, [key]: "33333333-3333-4333-8333-333333333333" } } };
    expect(bridgeConnectionSaveMatches(value, account, preview())).toBe(false);
  });

  it.each([undefined, null, {}, { context_id: "unknown", context_epoch: context.context_epoch }, { context_id: context.context_id, context_epoch: "00000000-0000-0000-0000-000000000000" }, { ...context, profile: "other" }, { ...context, context_id: "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA" }])("rejects an unknown or malformed source context: %o", browserContext => {
    expect(bridgeConnectionPreviewMatches({ ...preview(), browser_context: browserContext } as unknown as BridgeConnectionPreview, account, input)).toBe(false);
  });

  it("rejects extra-field or noncanonical receipt contexts", () => {
    for (const browserContext of [{ ...context, profile: "other" }, { ...context, context_epoch: "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA" }]) expect(bridgeConnectionSaveMatches({ ...saved(), binding_receipt: { ...saved().binding_receipt, browser_context: browserContext } }, account, preview())).toBe(false);
  });

  it("normalizes only caller labels and never sends or infers the source browser context", async () => {
    request.mockResolvedValue(preview()); await bridgeConnectionApi.preview(account, { ...input, browser_context_label: "  Brand A profile  " });
    expect(request.mock.calls[0][2]).toEqual({ ...input, account_ref: "@own_brand" }); expect(request.mock.calls[0][2]).not.toHaveProperty("browser_context");
    expect(bridgeConnectionLabelsValid("youtube", "Brand A profile", null)).toBe(false);
    expect(bridgeConnectionLabelsValid("youtube", "Brand A profile", "  ")).toBe(false);
    expect(bridgeConnectionLabelsValid("youtube", "Brand A profile", "a".repeat(81))).toBe(false);
    expect(bridgeConnectionLabelsValid("youtube", "Brand A profile", "Brand A Google")).toBe(true);
  });
});

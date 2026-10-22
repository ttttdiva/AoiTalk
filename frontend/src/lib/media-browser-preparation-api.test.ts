import { createHash, webcrypto } from "node:crypto";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { browserPreparationApi, browserPreparationBlocker, browserPreparationContentHash, browserPreparationMatches, browserPreparationDeviceReady, type BrowserPreparation } from "./media-browser-preparation-api";
import { publicationActionMatches, type PublicationAction } from "./media-persona-publication-api";
import type { ContentVariantRevision } from "./media-operations-content-api";
import type { PlatformAccount } from "./media-operations-setup-api";

const requests = vi.hoisted(() => vi.fn());
vi.mock("./agent-employees-api", async original => ({ ...await original<typeof import("./agent-employees-api")>(), employeeRequest: requests }));
const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "persona-rev", displayName: "UN//REAL" };
const account = { id: "account", owner_user_id: "owner", project_id: "project", persona_id: "persona", platform: "x", account_ref: "@Own_Aoi", status: "active", connection_id: "connection", current_revision: { id: "account-rev", owner_user_id: "owner", project_id: "project", platform_account_id: "account", content_hash: "d".repeat(64) } } as PlatformAccount;
const revision = { id: "variant-rev", content_variant_id: "variant", content_item_id: "item", content_item_hash: "a".repeat(64), persona_revision_id: "persona-rev", persona_revision_hash: "b".repeat(64), platform_account_id: "account", platform_account_revision_id: "account-rev", platform_account_revision_hash: "d".repeat(64), platform: "x", version: 1, content_hash: "c".repeat(64), generation_output_refs: [], source_evidence: [], payload: { type: "x_post", text: "  本人の本文  ", media: [], hashtags: ["#準備"], links: ["https://example.invalid/reference"] } } as ContentVariantRevision;
const target = { owner_user_id: "owner", project_id: "project", persona_id: "persona", platform_account_id: "account", platform_account_revision_id: "account-rev", platform: "x", device_id: "own-device", tab_id: 7 };
const availability = { available: false, source_available: true, execution_mode: "manual", publication_confirmation_allowed: false, required_terminal_stage: "awaiting_final_confirmation", prepare_operation_id: "media_operations_prepare_browser_action", get_operation_id: "media_operations_get_browser_preparation", inspect_operation_id: "media_operations_inspect_browser_preparation", blockers: ["browser_explicit_owner_device_tab_required"], readiness_target: null };
const reply = (preparation: unknown) => ({ platforms: { x: { browser_publication: { preparation } } } });
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
beforeEach(() => { vi.clearAllMocks(); vi.stubGlobal("crypto", webcrypto); });

describe("product Bridge is an explicit prepare-only contract", () => {
  it("accepts an owned profile transport and still requires online browser authority", () => {
    const device = { id: "own", name: "own", online: true, edge_connected: false, browser_backend: "agent_browser", media_preparation_backend: "managed_profile", media_preparation_connected: true, capabilities: ["media_prepare"] };
    expect(browserPreparationDeviceReady(device)).toBe(true);
    expect(browserPreparationDeviceReady({ ...device, edge_connected: true })).toBe(true);
    for (const patch of [{ online: false }, { media_preparation_connected: false }, { capabilities: ["browser"] }, { media_preparation_backend: null }]) expect(browserPreparationDeviceReady({ ...device, ...patch })).toBe(false);
    expect(browserPreparationDeviceReady({ ...device, media_preparation_backend: null, browser_backend: "extension", edge_connected: true, capabilities: ["browser"] })).toBe(true);
    expect(browserPreparationDeviceReady({ ...device, media_preparation_backend: null, browser_backend: "managed_profile", edge_connected: true, capabilities: ["browser"] })).toBe(false);
    expect(browserPreparationDeviceReady({ ...device, media_preparation_backend: "unknown", browser_backend: "extension", edge_connected: true, capabilities: ["browser"] })).toBe(false);
  });
  it("checks account scope and rejects unsupported media settings without provider fallback", () => {
    expect(browserPreparationBlocker(scope, revision, account)).toBe("");
    expect(browserPreparationBlocker(scope, revision, { ...account, account_ref: "123456789" })).toContain("@handle");
    expect(browserPreparationBlocker(scope, revision, { ...account, persona_id: "other-persona" })).toContain("一致しません");
    for (const payload of [{ type: "x_thread", posts: [] }, { ...revision.payload, reply_to: "123" }, { ...revision.payload, sensitive_content: true }, { ...revision.payload, alt_text: "説明" }, { ...revision.payload, media: ["one", "two"] }]) expect(browserPreparationBlocker(scope, { ...revision, payload } as ContentVariantRevision, account)).toContain("未対応");
    const ytAccount = { ...account, platform: "youtube" as const, account_ref: `UC${"a".repeat(22)}` };
    const video = { ...revision, platform: "youtube", payload: { type: "youtube_video", title: "原文", description: "説明", media_asset: ["asset"], tags: [], visibility: "private", audience: "not_made_for_kids" } } as ContentVariantRevision;
    expect(browserPreparationBlocker(scope, video, ytAccount)).toBe("");
    for (const patch of [{ visibility: "public" }, { audience: null }, { tags: ["tag"] }, { disclosure: "ai_assisted" }, { type: "youtube_short" }]) expect(browserPreparationBlocker(scope, { ...video, payload: { ...video.payload, ...patch } } as ContentVariantRevision, ytAccount)).toContain("未対応");
  });

  it("preserves actual availability=false and refuses malformed preparation metadata", async () => {
    const preparation = { available: false, source_available: true, execution_mode: "manual", publication_confirmation_allowed: false, required_terminal_stage: "awaiting_final_confirmation", prepare_operation_id: "media_operations_prepare_browser_action", get_operation_id: "media_operations_get_browser_preparation", inspect_operation_id: "media_operations_inspect_browser_preparation", blockers: ["browser_extension_distribution_acceptance_pending"] };
    requests.mockResolvedValue({ platforms: { x: { browser_publication: { preparation } } } });
    expect(await browserPreparationApi.availability("x")).toMatchObject({ available: false, source_available: true });
    requests.mockResolvedValue({ platforms: { x: { browser_publication: { preparation: { ...preparation, publication_confirmation_allowed: true } } } } });
    await expect(browserPreparationApi.availability("x")).rejects.toThrow("契約と利用可否");
    expect(requests.mock.calls.every(call => call[1] === undefined)).toBe(true);
  });

  it("queries only the selected account, device and tab and verifies every returned authority pin", async () => {
    requests.mockResolvedValue(reply({ ...availability, available: true, blockers: [], readiness_target: { ...target, checked_at: "2026-10-07T09:00:00Z" } }));
    expect(await browserPreparationApi.availability("x", target)).toMatchObject({ available: true, readiness_target: target });
    expect(requests).toHaveBeenCalledWith("/api/operations/media/adapter-status?platform_account_id=account&device_id=own-device&tab_id=7");
  });

  it.each(["owner_user_id", "project_id", "persona_id", "platform_account_id", "platform_account_revision_id", "platform", "device_id", "tab_id", "checked_at"])("rejects a mismatched or malformed readiness %s", async key => {
    requests.mockResolvedValue(reply({ ...availability, available: true, blockers: [], readiness_target: { ...target, checked_at: "2026-10-07T09:00:00Z", [key]: key === "tab_id" ? 8 : "other" } }));
    await expect(browserPreparationApi.availability("x", target)).rejects.toThrow("一致しません");
  });

  it("does not promote static source availability or accept positive replies without a target", async () => {
    requests.mockResolvedValue(reply(availability));
    expect(await browserPreparationApi.availability("x")).toMatchObject({ available: false, source_available: true });
    requests.mockResolvedValue(reply({ ...availability, available: true, blockers: [] }));
    await expect(browserPreparationApi.availability("x")).rejects.toThrow("一致しません");
    await expect(browserPreparationApi.availability("x", target)).rejects.toThrow("一致しません");
    requests.mockClear();
    await expect(browserPreparationApi.availability("x", { ...target, tab_id: 0 })).rejects.toThrow("明示選択");
    expect(requests).not.toHaveBeenCalled();
  });

  it("computes backend UTF-8 X text and canonical YouTube metadata digests", async () => {
    expect(await browserPreparationContentHash(revision)).toBe(sha("本人の本文 #準備 https://example.invalid/reference"));
    const video = { ...revision, payload: { type: "youtube_video", title: "原文🙂", description: "  説明\n", media_asset: ["asset"], tags: [], visibility: "private", audience: "not_made_for_kids" } } as ContentVariantRevision;
    expect(await browserPreparationContentHash(video)).toBe(sha('{"audience":"not_made_for_kids","description":"  説明\\n","tags":[],"title":"原文🙂","visibility":"private"}'));
  });

  it("keeps manual actions distinct and rejects foreign or falsely confirmed evidence", async () => {
    const digest = await browserPreparationContentHash(revision);
    const action = { id: "action", execution_mode: "manual", action_type: "media.publish_content", owner_user_id: "owner", project_id: "project", platform: "x", content_item_id: "item", content_variant_id: "variant", content_variant_revision_id: revision.id, persona_revision_id: "persona-rev", platform_account_id: "account", platform_account_revision_id: "account-rev", connection_id: "connection", execution_key: "bridge-key", version: 3, action_version: 1, status: "approved", payload_hash: "e".repeat(64), payload: { content_variant_revision_id: revision.id, content_variant_revision_hash: revision.content_hash, content_item_id: "item", content_item_hash: revision.content_item_hash, persona_revision_id: "persona-rev", persona_revision_hash: revision.persona_revision_hash, platform_account_id: "account", platform_account_revision_id: "account-rev", platform_account_revision_hash: revision.platform_account_revision_hash, payload: revision.payload } } as PublicationAction;
    expect(publicationActionMatches(action, scope, revision, account)).toBe(false);
    expect(publicationActionMatches(action, scope, revision, account, "manual")).toBe(true);
    const evidence = { id: "preparation", action_id: action.id, action_version: 1, action_state_version: 3, payload_hash: action.payload_hash, owner_user_id: "owner", project_id: "project", platform: "x", platform_account_id: "account", platform_account_revision_id: "account-rev", device_id: "own-device", tab_id: 7, content_sha256: digest, text_sha256: digest, metadata_sha256: null, version: 1, status: "awaiting_final_confirmation", current_action_matches: true, current_approval_valid: true, publication_confirmed: false, automatic_retry_allowed: false } as BrowserPreparation;
    expect(browserPreparationMatches(evidence, action, scope, revision, account, digest)).toBe(true);
    for (const patch of [{ owner_user_id: "other-owner" }, { project_id: "other-project" }, { platform_account_id: "foreign-account" }, { payload_hash: "0".repeat(64) }, { content_sha256: "0".repeat(64) }, { publication_confirmed: true }, { automatic_retry_allowed: true }, { action_state_version: 2 }, { tab_id: 0 }]) expect(browserPreparationMatches({ ...evidence, ...patch } as BrowserPreparation, action, scope, revision, account, digest)).toBe(false);
    expect(browserPreparationMatches({ ...evidence, current_action_matches: false, current_approval_valid: false }, { ...action, version: 4, status: "rejected" }, scope, revision, account, digest)).toBe(true);
  });
});

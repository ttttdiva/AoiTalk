// @vitest-environment jsdom
import { createHash, webcrypto } from "node:crypto";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PersonaBrowserPreparationControls } from "./persona-browser-preparation-controls";
import type { ContentVariantRevision } from "@/lib/media-operations-content-api";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import type { PublicationAction } from "@/lib/media-persona-publication-api";
import type { BrowserPreparation } from "@/lib/media-browser-preparation-api";

const api = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), create: vi.fn(), package: vi.fn(), approve: vi.fn(), account: vi.fn(), qa: vi.fn(), availability: vi.fn(), devices: vi.fn(), preparation: vi.fn(), prepare: vi.fn(), inspect: vi.fn(), attempt: vi.fn() }));
vi.mock("@/lib/media-operations-setup-api", () => ({ mediaOperationsSetupApi: { getPlatformAccount: api.account } }));
vi.mock("@/lib/media-operations-content-api", () => ({ mediaContentApi: { getReadiness: api.qa } }));
vi.mock("@/lib/media-persona-publication-api", async original => ({ ...await original<typeof import("@/lib/media-persona-publication-api")>(), personaPublicationApi: api }));
vi.mock("@/lib/media-browser-preparation-api", async original => ({ ...await original<typeof import("@/lib/media-browser-preparation-api")>(), browserPreparationApi: { availability: api.availability, devices: api.devices, get: api.preparation, prepare: api.prepare, inspect: api.inspect } }));
vi.mock("next/link", () => ({ default: ({ children, ...props }: React.ComponentProps<"a">) => <a {...props}>{children}</a> }));
vi.mock("@/components/ui/app-select", () => ({ AppSelect: ({ onValueChange, triggerContent, disabled, "aria-label": label }: { onValueChange: (value: string) => void; triggerContent?: React.ReactNode; disabled?: boolean; "aria-label"?: string }) => <button type="button" aria-label={label} disabled={disabled} onClick={() => onValueChange("own-device")}>{triggerContent}</button> }));

const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "persona-rev", displayName: "UN//REAL" };
const account = { id: "account", owner_user_id: "owner", project_id: "project", persona_id: "persona", platform: "x", account_ref: "@Own_Aoi", status: "active", connection_id: "connection", current_revision: { id: "account-rev", platform_account_id: "account", owner_user_id: "owner", project_id: "project", content_hash: "d".repeat(64) } } as PlatformAccount;
const revision = { id: "variant-rev", content_variant_id: "variant", content_item_id: "item", content_item_hash: "a".repeat(64), persona_revision_id: "persona-rev", persona_revision_hash: "b".repeat(64), platform_account_id: "account", platform_account_revision_id: "account-rev", platform_account_revision_hash: "d".repeat(64), platform: "x", version: 1, content_hash: "c".repeat(64), payload: { type: "x_post", text: "本人の下書き", media: [] }, generation_output_refs: [], source_evidence: [] } as ContentVariantRevision;
const actionKey = `persona-bridge-action-v1:${revision.id}:${revision.content_hash}:${account.id}`;
let saved: PublicationAction;
let evidence: BrowserPreparation | null;
function proposal(): PublicationAction {
  return { id: "manual-action", owner_user_id: "owner", project_id: "project", platform: "x", action_type: "media.publish_content", execution_mode: "manual", content_item_id: "item", content_variant_id: "variant", content_variant_revision_id: revision.id, persona_revision_id: "persona-rev", platform_account_id: "account", platform_account_revision_id: "account-rev", connection_id: "connection", execution_key: actionKey, version: 1, action_version: 1, payload_hash: "e".repeat(64), status: "proposed", artifact_hashes: [], attempts: [], receipt: null, payload: { content_item_id: revision.content_item_id, content_item_hash: revision.content_item_hash, content_variant_revision_id: revision.id, content_variant_revision_hash: revision.content_hash, persona_revision_id: revision.persona_revision_id, persona_revision_hash: revision.persona_revision_hash, platform_account_id: account.id, platform_account_revision_id: revision.platform_account_revision_id, platform_account_revision_hash: revision.platform_account_revision_hash, connection: { id: "connection", provider_key: "x", remote_account_ref: "@Own_Aoi" }, payload: revision.payload } };
}
function mount() { return render(<SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false, dedupingInterval: 0 }}><PersonaBrowserPreparationControls scope={scope} revision={revision} account={account} /></SWRConfig>); }
async function reviewedPreview() {
  fireEvent.click(await screen.findByLabelText("本人の接続済みPC"));
  fireEvent.change(screen.getByLabelText("本人が開いた既存タブID"), { target: { value: "7" } });
  const create = screen.getByRole("button", { name: "この保存版の準備Actionを作成" }); await waitFor(() => expect(create).toBeEnabled()); fireEvent.click(create);
  await screen.findByLabelText("Bridgeの凍結preview");
  fireEvent.click(screen.getByRole("checkbox")); await waitFor(() => expect(screen.getByRole("button", { name: "本人が承認して下書きだけ準備" })).toBeEnabled());
}
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); vi.stubGlobal("crypto", webcrypto); saved = proposal(); evidence = null;
  Object.defineProperty(navigator, "locks", { configurable: true, value: { request: vi.fn(async (_key, task) => task()) } });
  api.account.mockResolvedValue(account); api.list.mockResolvedValue([]);
  api.availability.mockImplementation(async (platform, target) => {
    const matching = platform === "x" && target?.owner_user_id === scope.ownerUserId && target?.project_id === scope.projectId && target?.persona_id === scope.personaId && target?.platform_account_id === account.id && target?.platform_account_revision_id === revision.platform_account_revision_id && target?.device_id === "own-device" && target?.tab_id === 7;
    return { available: matching && !evidence, source_available: true, execution_mode: "manual", publication_confirmation_allowed: false, blockers: !matching ? ["browser_explicit_owner_device_tab_required"] : evidence ? ["browser_preparation_new_document_required"] : [], readiness_target: target ? { ...target, checked_at: "2026-10-07T09:00:00Z" } : null };
  });
  api.devices.mockResolvedValue([{ id: "own-device", name: "本人のPC", online: true, edge_connected: true, browser_backend: "extension", capabilities: ["browser", "media_asset"] }]);
  api.qa.mockResolvedValue({ ready: true, content_variant_id: "variant", revision_id: revision.id, revision_hash: revision.content_hash });
  api.create.mockImplementation(async () => saved); api.get.mockImplementation(async () => saved);
  api.package.mockImplementation(async value => ({ manifest: { action: { id: value.id, action_version: value.action_version, payload_hash: value.payload_hash }, source: { content_variant_revision_id: revision.id, content_variant_revision_hash: revision.content_hash }, payload: revision.payload, status: "verified", assets: [], reasons: [] } }));
  api.preparation.mockImplementation(async () => ({ preparation: evidence, publication_confirmed: false }));
  api.approve.mockImplementation(async () => { saved = { ...saved, status: "approved", version: 2 }; return saved; });
  api.prepare.mockImplementation(async () => {
    saved = { ...saved, version: 3 };
    const digest = createHash("sha256").update("本人の下書き").digest("hex");
    evidence = { id: "prepared", action_id: saved.id, action_state_version: 3, action_version: 1, payload_hash: saved.payload_hash, owner_user_id: "owner", project_id: "project", platform: "x", platform_account_id: "account", platform_account_revision_id: "account-rev", device_id: "own-device", tab_id: 7, content_sha256: digest, text_sha256: digest, metadata_sha256: null, status: "awaiting_final_confirmation", phase: "final_checkpoint", version: 1, current_action_matches: true, current_approval_valid: true, publication_confirmed: false, automatic_retry_allowed: false } as BrowserPreparation;
    return evidence;
  });
  api.inspect.mockImplementation(async () => evidence);
});

describe("human approved Bridge preparation", () => {
  it("keeps the Bridge tab instructions behind the preparation help", async () => {
    mount(); fireEvent.click(screen.getByRole("button", { name: "Bridgeで下書きを準備の説明" }));
    expect(await screen.findByText(/BridgeでこのProject・人格・accountの専用ブラウザ/)).toBeVisible();
  });

  it("accepts backend claim +1, serializes clicks and saves evidence without an attempt or receipt", async () => {
    mount(); await reviewedPreview(); const button = screen.getByRole("button", { name: "本人が承認して下書きだけ準備" }); fireEvent.click(button); fireEvent.click(button);
    const panel = await screen.findByLabelText("Bridge準備の保存証跡"); expect(within(panel).getByText("本人の最終操作待ち")).toBeVisible(); expect(panel).toHaveTextContent("公開結果は未確認");
    expect(api.create).toHaveBeenCalledTimes(1); expect(api.approve).toHaveBeenCalledTimes(1); expect(api.prepare).toHaveBeenCalledTimes(1); expect(api.attempt).not.toHaveBeenCalled();
    expect(api.create.mock.calls[0][0].execution_mode).toBe("manual");
    expect(api.prepare).toHaveBeenCalledWith("manual-action", { expected_version: 2, action_version: 1, payload_hash: "e".repeat(64), device_id: "own-device", tab_id: 7, idempotency_key: `persona-bridge-prepare-v1:manual-action:1:${"e".repeat(64)}` });
    expect(screen.queryByTestId("operations-action-receipt")).not.toBeInTheDocument(); expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Bridgeの利用状態を更新" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("新しい制作タブを手動で開いて"));
    const inspectName = "同じ下書きを読み取り専用で再照合";
    await waitFor(() => expect(screen.getByRole("button", { name: inspectName })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: inspectName }));
    await waitFor(() => expect(screen.getByRole("button", { name: inspectName })).toBeDisabled());
    await waitFor(() => expect(api.inspect).toHaveBeenCalledWith("manual-action", { preparation_id: "prepared", expected_version: 1 }));
    await waitFor(() => expect(screen.getByRole("button", { name: inspectName })).toBeEnabled());
    expect(api.prepare).toHaveBeenCalledTimes(1);
  });

  it("never replays unknown input after reload or read-only reconciliation", async () => {
    api.prepare.mockRejectedValue(new Error("準備結果が不明")); const view = mount(); await reviewedPreview(); fireEvent.click(screen.getByRole("button", { name: "本人が承認して下書きだけ準備" })); await screen.findByText("準備結果が不明");
    view.unmount(); mount(); fireEvent.click(await screen.findByRole("button", { name: "準備記録を読み取って照合" })); await screen.findByLabelText("Bridgeの凍結preview");
    expect(screen.getByRole("button", { name: "本人が承認して下書きだけ準備" })).toBeDisabled(); expect(api.prepare).toHaveBeenCalledTimes(1); expect(api.create).toHaveBeenCalledTimes(1); expect(api.approve).toHaveBeenCalledTimes(1); expect(api.attempt).not.toHaveBeenCalled();
  });

  it("retains verified saved evidence when the original preview package is unavailable", async () => {
    mount(); await reviewedPreview(); fireEvent.click(screen.getByRole("button", { name: "本人が承認して下書きだけ準備" })); await screen.findByLabelText("Bridge準備の保存証跡");
    api.package.mockRejectedValue(new Error("元のpreview素材を取得できません")); fireEvent.click(screen.getByRole("button", { name: "準備記録を読み取って照合" }));
    await screen.findByText("元のpreview素材を取得できません"); expect(screen.getByLabelText("Bridge準備の保存証跡")).toHaveTextContent("公開結果は未確認"); expect(api.prepare).toHaveBeenCalledTimes(1); expect(api.attempt).not.toHaveBeenCalled();
  });

  it("keeps actual availability false and displays a single concrete blocker", async () => {
    api.availability.mockResolvedValue({ available: false, source_available: true, blockers: ["browser_preparation_new_document_required"] }); mount();
    fireEvent.click(await screen.findByLabelText("本人の接続済みPC")); fireEvent.change(screen.getByLabelText("本人が開いた既存タブID"), { target: { value: "7" } });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("新しい制作タブを手動で開いて")); expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "この保存版の準備Actionを作成" })).toBeDisabled(); expect(api.create).not.toHaveBeenCalled(); expect(api.prepare).not.toHaveBeenCalled();
  });

  it("requires selectors before any readiness query or Action creation and rechecks a changed tab", async () => {
    mount(); await screen.findByRole("alert");
    expect(screen.getByRole("alert")).toHaveTextContent("Action作成前に明示選択");
    expect(api.availability).not.toHaveBeenCalled(); expect(api.create).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("本人の接続済みPC"));
    expect(api.availability).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("本人が開いた既存タブID"), { target: { value: "7" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "この保存版の準備Actionを作成" })).toBeEnabled());
    expect(api.availability).toHaveBeenLastCalledWith("x", expect.objectContaining({ platform_account_id: account.id, platform_account_revision_id: revision.platform_account_revision_id, device_id: "own-device", tab_id: 7 }));
    fireEvent.change(screen.getByLabelText("本人が開いた既存タブID"), { target: { value: "8" } });
    await waitFor(() => expect(api.availability).toHaveBeenLastCalledWith("x", expect.objectContaining({ tab_id: 8 })));
    expect(screen.getByRole("button", { name: "この保存版の準備Actionを作成" })).toBeDisabled(); expect(api.create).not.toHaveBeenCalled();
  });

  it("refreshes the selected target before creating an Action and refuses a newly blocked tab", async () => {
    mount(); fireEvent.click(await screen.findByLabelText("本人の接続済みPC")); fireEvent.change(screen.getByLabelText("本人が開いた既存タブID"), { target: { value: "7" } });
    const create = screen.getByRole("button", { name: "この保存版の準備Actionを作成" }); await waitFor(() => expect(create).toBeEnabled());
    api.availability.mockResolvedValue({ available: false, source_available: true, blockers: ["browser_preparation_legacy_revision_unverified"] });
    fireEvent.click(create); await screen.findByText(/旧拡張機能による準備証跡/);
    expect(create).toBeDisabled();
    expect(api.create).not.toHaveBeenCalled(); expect(api.approve).not.toHaveBeenCalled(); expect(api.prepare).not.toHaveBeenCalled();
  });

  it("stops a foreign connection snapshot before approval and input", async () => {
    mount(); await reviewedPreview(); saved = { ...saved, payload: { ...saved.payload, connection: { id: "connection", provider_key: "x", remote_account_ref: "@Foreign" } } };
    fireEvent.click(screen.getByRole("button", { name: "本人が承認して下書きだけ準備" })); await screen.findByText(/接続snapshot/); expect(api.approve).not.toHaveBeenCalled(); expect(api.prepare).not.toHaveBeenCalled();
  });

  it("discards delayed device refresh after switching persona before approval", async () => {
    const view = mount(); await reviewedPreview(); let complete!: () => void;
    api.devices.mockImplementation(() => new Promise(resolve => { complete = () => resolve([{ id: "own-device", name: "本人のPC", online: true, edge_connected: true, browser_backend: "extension", capabilities: ["browser", "media_asset"] }]); }));
    fireEvent.click(screen.getByRole("button", { name: "本人が承認して下書きだけ準備" })); await waitFor(() => expect(complete).toBeDefined()); view.unmount(); await act(async () => complete());
    expect(api.approve).not.toHaveBeenCalled(); expect(api.prepare).not.toHaveBeenCalled();
  });
});

// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PersonaPublicationControls } from "./persona-publication-controls";
import { submissionStorageKey, writeSubmissionMarker } from "@/lib/media-publication-submission";
import type { ContentVariantRevision } from "@/lib/media-operations-content-api";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import type { PublicationAction } from "@/lib/media-persona-publication-api";

const api = vi.hoisted(() => ({ readiness: vi.fn(), refreshReadiness: vi.fn(), list: vi.fn(), get: vi.fn(), create: vi.fn(), package: vi.fn(), approve: vi.fn(), attempt: vi.fn(), recover: vi.fn(), account: vi.fn(), qa: vi.fn() }));
vi.mock("@/lib/media-operations-setup-api", () => ({ mediaOperationsSetupApi: { getPlatformAccount: api.account } }));
vi.mock("@/lib/media-operations-content-api", () => ({ mediaContentApi: { getReadiness: api.qa } }));
vi.mock("@/lib/media-persona-publication-api", async importOriginal => ({ ...await importOriginal<typeof import("@/lib/media-persona-publication-api")>(), personaPublicationApi: api }));
vi.mock("next/link", () => ({ default: ({ children, ...props }: React.ComponentProps<"a">) => <a {...props}>{children}</a> }));

const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "persona-rev", displayName: "UN//REAL" };
const hash = "c".repeat(64);
const account = { id: "account", owner_user_id: "owner", project_id: "project", persona_id: "persona", platform: "x", account_ref: "fixture-account", status: "active", connection_id: "connection", current_revision: { id: "account-rev", platform_account_id: "account", owner_user_id: "owner", project_id: "project", content_hash: "d".repeat(64) } } as PlatformAccount;
const revision = { id: "variant-rev", content_variant_id: "variant", content_item_id: "item", content_item_hash: "a".repeat(64), persona_revision_id: "persona-rev", persona_revision_hash: "b".repeat(64), platform_account_id: "account", platform_account_revision_id: "account-rev", platform_account_revision_hash: "d".repeat(64), platform: "x", version: 1, content_hash: hash, payload: { type: "x_post", text: "本人が確認する保存本文", media: [] }, generation_output_refs: [], source_evidence: [] } as ContentVariantRevision;
const key = `persona-publish-v1:${revision.id}:${hash}:${account.id}`;
let saved: PublicationAction;
function proposal(): PublicationAction { return { id: "publication", owner_user_id: "owner", project_id: "project", platform: "x", action_type: "media.publish_content", execution_mode: "provider", content_item_id: "item", content_variant_id: "variant", content_variant_revision_id: "variant-rev", persona_revision_id: "persona-rev", platform_account_id: "account", platform_account_revision_id: "account-rev", connection_id: "connection", execution_key: key, version: 1, action_version: 1, payload_hash: "e".repeat(64), status: "proposed", artifact_hashes: [], attempts: [], receipt: null, payload: { content_item_id: revision.content_item_id, content_item_hash: revision.content_item_hash, content_variant_revision_id: revision.id, content_variant_revision_hash: hash, persona_revision_id: revision.persona_revision_id, persona_revision_hash: revision.persona_revision_hash, platform_account_id: account.id, platform_account_revision_id: revision.platform_account_revision_id, platform_account_revision_hash: revision.platform_account_revision_hash, payload: revision.payload } }; }
function mount() { return render(<SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false, dedupingInterval: 0 }}><PersonaPublicationControls scope={scope} revision={revision} account={account} /></SWRConfig>); }
async function prepare() { const button = await screen.findByRole("button", { name: "この保存版の公開準備" }); await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button); await screen.findByLabelText("凍結した公開preview"); await waitFor(() => expect(screen.getByRole("button", { name: "本人が X に投稿" })).toBeDisabled()); fireEvent.click(screen.getByRole("checkbox")); await waitFor(() => expect(screen.getByRole("button", { name: "本人が X に投稿" })).toBeEnabled()); }
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); saved = proposal();
  Object.defineProperty(navigator, "locks", { configurable: true, value: { request: vi.fn(async (_key, task) => task()) } });
  api.account.mockResolvedValue(account);
  api.readiness.mockResolvedValue({ platform_account_id: "account", platform: "x", account_status: "active", account_revision_id: "account-rev", credential_status: "verified", credential_revision: 1, verified: true, write_capable: true, read_only: false, operations: [{ id: "capability", owner_user_id: "owner", project_id: "project", platform_account_id: "account", account_revision_id: "account-rev", operation: "text", status: "automatable", account_eligibility: "eligible" }] });
  api.list.mockResolvedValue([]); api.qa.mockResolvedValue({ ready: true, content_variant_id: "variant", revision_id: "variant-rev", revision_hash: hash });
  api.create.mockImplementation(async () => saved); api.get.mockImplementation(async () => saved);
  api.package.mockImplementation(async value => ({ filename: "fixture.zip", manifest_sha256: "f".repeat(64), manifest: { action: { id: value.id, action_version: value.action_version, payload_hash: value.payload_hash }, source: { content_variant_revision_id: revision.id, content_variant_revision_hash: hash }, payload: revision.payload, status: "verified", assets: [], reasons: [] } }));
  api.approve.mockImplementation(async () => { saved = { ...saved, status: "approved", version: 2 }; return saved; });
  api.attempt.mockImplementation(async () => { saved = { ...saved, status: "succeeded", version: 3, attempts: [{ id: "attempt", action_id: "publication", action_version: 1, execution_key: key, provider_recovery_available: false }], receipt: { id: "receipt", action_id: "publication", action_version: 1, attempt_id: "attempt", confirmation_level: "provider", remote_status: "published", remote_resource_id: "post", remote_url: "https://x.com/fixture/status/123" } }; return { id: "attempt", action_id: "publication", execution_key: key }; });
});
describe("explicit persona publication", () => {
  it("accepts minimal immutable payload and its HTTP DTO defaults without changing pins or auto-publishing", async () => {
    saved = { ...saved, payload: { ...saved.payload, payload: { type: "x_post", text: revision.payload.type === "x_post" ? revision.payload.text : "" } } };
    api.package.mockImplementation(async value => ({ manifest: { action: { id: value.id, action_version: value.action_version, payload_hash: value.payload_hash }, source: { content_variant_revision_id: revision.id, content_variant_revision_hash: hash }, payload: { ...revision.payload, hashtags: [], links: [], alt_text: null, reply_to: null, scheduled_at: null, sensitive_content: false }, status: "verified", assets: [], reasons: [] } }));
    mount(); await prepare(); expect(api.approve).not.toHaveBeenCalled(); expect(api.attempt).not.toHaveBeenCalled();
    expect(api.create.mock.calls[0][0].content_variant_revision_hash).toBe(hash);
  });
  it("sends only the saved pins after review and serializes double clicks, then displays a persisted receipt", async () => {
    mount(); await prepare();
    expect(api.approve).not.toHaveBeenCalled(); expect(api.attempt).not.toHaveBeenCalled();
    const button = screen.getByRole("button", { name: "本人が X に投稿" }); fireEvent.click(button); fireEvent.click(button);
    await screen.findByText("published");
    expect(api.create).toHaveBeenCalledTimes(1); expect(api.approve).toHaveBeenCalledTimes(1); expect(api.attempt).toHaveBeenCalledTimes(1);
    expect(api.create.mock.calls[0][0]).toMatchObject({ idempotency_key: key, execution_key: key, payload: revision.payload, content_variant_revision_id: revision.id, platform_account_id: account.id });
    expect(api.approve).toHaveBeenCalledWith("publication", { expected_version: 1, action_version: 1, payload_hash: "e".repeat(64), artifact_hashes: [] });
    expect(api.attempt).toHaveBeenCalledWith("publication", { expected_version: 2, executor_type: "provider", execution_mode: "provider", execution_key: key });
    expect(screen.getByRole("button", { name: "本人が X に投稿" })).toBeDisabled();
  });
  it("never retries an unknown provider submission after reload; explicit reconciliation reads the same action", async () => {
    api.attempt.mockRejectedValue(new Error("通信結果が不明"));
    const view = mount(); await prepare(); fireEvent.click(screen.getByRole("button", { name: "本人が X に投稿" }));
    await screen.findByText("通信結果が不明"); view.unmount();
    mount(); await screen.findByRole("button", { name: "保存記録・receiptを照合" });
    expect(api.attempt).toHaveBeenCalledTimes(1); expect(api.create).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "保存記録・receiptを照合" })); await screen.findByLabelText("凍結した公開preview");
    expect(screen.getByRole("button", { name: "本人が X に投稿" })).toBeDisabled(); expect(api.attempt).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "同じ実行の外部状態を再照合" })).toBeEnabled();
  });
  for (const boundary of ["create-key", "get-id", "get-key", "approve-id", "approve-key"] as const) it(`rejects a same-content response with a foreign action identity at ${boundary}`, async () => {
    if (boundary === "create-key") api.create.mockResolvedValue({ ...saved, execution_key: "foreign" });
    mount();
    if (boundary === "create-key") { const button = await screen.findByRole("button", { name: "この保存版の公開準備" }); await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button); }
    else { await prepare(); if (boundary.startsWith("get")) api.get.mockResolvedValue({ ...saved, ...(boundary.endsWith("id") ? { id: "foreign" } : { execution_key: "foreign" }) }); else api.approve.mockResolvedValue({ ...saved, status: "approved", version: 2, ...(boundary.endsWith("id") ? { id: "foreign" } : { execution_key: "foreign" }) }); fireEvent.click(screen.getByRole("button", { name: "本人が X に投稿" })); }
    await screen.findByText(/保存された公開記録のID・操作key/); expect(api.attempt).not.toHaveBeenCalled();
    if (boundary.startsWith("get")) expect(api.approve).not.toHaveBeenCalled();
  });
  it("retains an unknown marker without a phase as an executed operation and does not replay", async () => {
    writeSubmissionMarker(submissionStorageKey(scope, account.id, hash), { idempotencyKey: key, revisionId: revision.id, contentHash: hash, accountId: account.id, publicationId: "publication", state: "unknown" });
    mount(); fireEvent.click(await screen.findByRole("button", { name: "保存記録・receiptを照合" })); await screen.findByLabelText("凍結した公開preview");
    expect(screen.getByRole("button", { name: "本人が X に投稿" })).toBeDisabled(); expect(api.create).not.toHaveBeenCalled(); expect(api.approve).not.toHaveBeenCalled(); expect(api.attempt).not.toHaveBeenCalled();
  });
  it("discards a delayed prepare result when the persona workspace unmounts", async () => {
    let complete!: () => void; api.create.mockImplementation(() => new Promise(resolve => { complete = () => resolve(saved); }));
    const view = mount(); const button = await screen.findByRole("button", { name: "この保存版の公開準備" }); await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button);
    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1)); view.unmount(); await act(async () => complete());
    expect(api.package).not.toHaveBeenCalled(); expect(api.approve).not.toHaveBeenCalled(); expect(api.attempt).not.toHaveBeenCalled();
  });
  it("displays a persisted receipt even when its original asset package can no longer be read", async () => {
    saved = { ...saved, status: "succeeded", attempts: [{ id: "attempt", action_id: "publication", action_version: 1, execution_key: key }], receipt: { id: "receipt", action_id: "publication", action_version: 1, attempt_id: "attempt", remote_status: "private", confirmation_level: "provider" } };
    writeSubmissionMarker(submissionStorageKey(scope, account.id, hash), { idempotencyKey: key, revisionId: revision.id, contentHash: hash, accountId: account.id, publicationId: "publication", state: "recorded", phase: "execute" });
    api.package.mockRejectedValue(new Error("素材取得失敗")); mount(); fireEvent.click(await screen.findByRole("button", { name: "保存記録・receiptを照合" }));
    const receipt = await screen.findByTestId("operations-action-receipt"); expect(within(receipt).getByText("private")).toBeInTheDocument(); expect(api.attempt).not.toHaveBeenCalled();
  });
  it("keeps provider recovery available when a saved action's package preview cannot be read", async () => {
    saved = { ...saved, status: "receipt_unknown", version: 3, attempts: [{ id: "attempt", action_id: "publication", action_version: 1, execution_key: key }] };
    writeSubmissionMarker(submissionStorageKey(scope, account.id, hash), { idempotencyKey: key, revisionId: revision.id, contentHash: hash, accountId: account.id, publicationId: "publication", state: "unknown", phase: "execute" });
    api.package.mockRejectedValue(new Error("素材取得失敗")); api.recover.mockResolvedValue(saved);
    mount(); fireEvent.click(await screen.findByRole("button", { name: "保存記録・receiptを照合" })); await screen.findByText("素材取得失敗");
    expect(screen.queryByLabelText("凍結した公開preview")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "同じ実行の外部状態を再照合" }));
    await waitFor(() => expect(api.recover).toHaveBeenCalledWith("publication", { expected_version: 3, resume: false }));
    expect(api.create).not.toHaveBeenCalled(); expect(api.approve).not.toHaveBeenCalled(); expect(api.attempt).not.toHaveBeenCalled();
  });
  for (const phase of ["create", "approve"] as const) it(`stops before publication when artifact pins change at ${phase}`, async () => {
    if (phase === "create") api.create.mockResolvedValue({ ...saved, artifact_hashes: ["f".repeat(64)] });
    mount();
    if (phase === "create") { const button = await screen.findByRole("button", { name: "この保存版の公開準備" }); await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button); }
    else { await prepare(); api.approve.mockResolvedValue({ ...saved, status: "approved", version: 2, artifact_hashes: ["f".repeat(64)] }); fireEvent.click(screen.getByRole("button", { name: "本人が X に投稿" })); }
    await screen.findByText(/保存された公開記録のID・操作key/); expect(api.attempt).not.toHaveBeenCalled();
    if (phase === "approve") { expect(screen.getByRole("button", { name: "本人が X に投稿" })).toBeDisabled(); expect(screen.getByRole("checkbox")).toHaveAttribute("aria-disabled", "true"); expect(screen.getByText(/保存記録を照合するまで公開操作を停止/)).toBeInTheDocument(); }
  });
  for (const changed of [{ action_version: 2 }, { attempt_id: "foreign-attempt" }, { action_id: "foreign-action" }]) it(`does not present a receipt with a mismatched binding ${JSON.stringify(changed)}`, async () => {
    saved = { ...saved, status: "succeeded", attempts: [{ id: "attempt", action_id: "publication", action_version: 1, execution_key: key }], receipt: { id: "receipt", action_id: "publication", action_version: 1, attempt_id: "attempt", remote_status: "published", ...changed } };
    writeSubmissionMarker(submissionStorageKey(scope, account.id, hash), { idempotencyKey: key, revisionId: revision.id, contentHash: hash, accountId: account.id, publicationId: "publication", state: "recorded", phase: "execute" });
    mount(); fireEvent.click(await screen.findByRole("button", { name: "保存記録・receiptを照合" }));
    await screen.findByText(/外部結果のAction版・実行記録/); expect(screen.queryByText("published")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "同じ実行の外部状態を再照合" })).toBeEnabled(); expect(api.attempt).not.toHaveBeenCalled();
  });
  it("shows exact-account operation states and never treats a foreign capability as ready", async () => {
    const data = await api.readiness();
    api.readiness.mockResolvedValue({ ...data, operations: [...data.operations, { ...data.operations[0], id: "foreign", operation: "image", project_id: "other" }, { ...data.operations[0], id: "video", operation: "video", status: "unverified" }] });
    mount(); const status = await screen.findByLabelText("公式APIの対応範囲");
    await waitFor(() => expect(status).toHaveTextContent("本文利用可能")); expect(status).toHaveTextContent("画像添付権限・媒体条件が未確認"); expect(status).toHaveTextContent("予約未対応");
    expect(api.create).not.toHaveBeenCalled();
  });
});

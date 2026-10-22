import { expect, it } from "vitest";
import type { ContentVariantRevision, PlatformContentPayload } from "./media-operations-content-api";
import { publicationPayloadValue, providerBlocker, type ProviderReadiness } from "./media-persona-publication-api";
it("compares declared nullable/empty defaults while preserving required values and media order", () => {
  const saved = { type: "youtube_video", title: "title", description: "", tags: [], media_asset: ["video"], visibility: "private" };
  expect(publicationPayloadValue(saved)).toBe(publicationPayloadValue({ ...saved, thumbnail: [], captions: [], scheduled_at: null, audience: null, disclosure: null }));
  for (const changed of [{ visibility: "public" }, { scheduled_at: "2026-10-08T09:00:00Z" }, { thumbnail: ["different"] }, { media_asset: ["other"] }, { audience: "made_for_kids" }]) expect(publicationPayloadValue(saved)).not.toBe(publicationPayloadValue({ ...saved, ...changed }));
  expect(publicationPayloadValue({ ...saved, thumbnail: ["a", "b"] })).not.toBe(publicationPayloadValue({ ...saved, thumbnail: ["b", "a"] }));
});
it("accepts X thread DTO defaults and rejects text, media and sensitive-content changes", () => {
  const thread = { type: "x_thread", posts: [{ text: "first" }, { text: "second", media: ["output"] }] };
  expect(publicationPayloadValue(thread)).toBe(publicationPayloadValue({ ...thread, reply_to: null, scheduled_at: null, posts: [{ text: "first", media: [], alt_text: null }, { text: "second", media: ["output"], alt_text: null }] }));
  expect(publicationPayloadValue({ type: "x_post", text: "saved" })).not.toBe(publicationPayloadValue({ type: "x_post", text: "saved", sensitive_content: true }));
  expect(publicationPayloadValue(thread)).not.toBe(publicationPayloadValue({ ...thread, posts: [...thread.posts].reverse() }));
});


const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "persona-revision", displayName: "UN//REAL" };
const account = (platform: "x" | "youtube") => ({ id: "account", owner_user_id: "owner", project_id: "project", persona_id: "persona", status: "active", platform, current_revision: { id: "account-revision", platform_account_id: "account", owner_user_id: "owner", project_id: "project" } }) as import("./media-operations-setup-api").PlatformAccount;
const revision = (payload: PlatformContentPayload, platform: "x" | "youtube"): ContentVariantRevision => ({ id: "variant-revision", content_variant_id: "variant", version: 1, content_item_id: "brief", content_item_hash: "a".repeat(64), persona_revision_id: "persona-revision", persona_revision_hash: "b".repeat(64), platform_account_id: "account", platform_account_revision_id: "account-revision", platform, payload, generation_output_refs: [], source_evidence: [], content_hash: "c".repeat(64) });
function readiness(platform: "x" | "youtube", operations: string[]): ProviderReadiness { return { platform_account_id: "account", platform, account_status: "active", account_revision_id: "account-revision", credential_revision: 1, credential_status: "verified", verified: true, write_capable: true, read_only: false, operations: operations.map(operation => ({ id: operation, owner_user_id: "owner", project_id: "project", platform_account_id: "account", account_revision_id: "account-revision", operation, status: "automatable", account_eligibility: "eligible" })) }; }
it("checks the backend video operation for Shorts and schedule operation for scheduled YouTube uploads", () => {
  const payload: Extract<PlatformContentPayload, { type: "youtube_video" | "youtube_short" }> = { type: "youtube_short", title: "short", description: "", tags: [], media_asset: ["video"], visibility: "private" };
  const saved = revision(payload, "youtube");
  expect(providerBlocker(readiness("youtube", ["video"]), scope, saved, account("youtube"))).toBe("");
  expect(providerBlocker(readiness("youtube", ["text"]), scope, saved, account("youtube"))).toContain("動画upload");
  const scheduled = revision({ ...payload, scheduled_at: "2026-10-08T09:00:00Z" }, "youtube");
  expect(providerBlocker(readiness("youtube", ["schedule"]), scope, scheduled, account("youtube"))).toBe("");
  expect(providerBlocker(readiness("youtube", ["video"]), scope, scheduled, account("youtube"))).toContain("予約公開");
});
it("requires exact X media authority for an attached post or thread and never uses a foreign authority row", () => {
  const cases: PlatformContentPayload[] = [{ type: "x_post", text: "post", media: ["image"] }, { type: "x_thread", posts: [{ text: "post", media: ["video"] }] }];
  for (const payload of cases) {
    const saved = revision(payload, "x");
    expect(providerBlocker(readiness("x", ["text"]), scope, saved, account("x"))).toContain("添付素材");
    expect(providerBlocker(readiness("x", ["image"]), scope, saved, account("x"))).toBe("");
    const foreign = readiness("x", ["image"]); foreign.operations[0].project_id = "other";
    expect(providerBlocker(foreign, scope, saved, account("x"))).toContain("未確認");
  }
});
it("reports unsupported X scheduling and YouTube caption upload before preparing a publication", () => {
  const x = revision({ type: "x_post", text: "post", scheduled_at: "2026-10-08T09:00:00Z" }, "x");
  expect(providerBlocker(readiness("x", ["schedule"]), scope, x, account("x"))).toContain("X APIの予約投稿は未対応");
  const youtube = revision({ type: "youtube_video", title: "video", description: "", tags: [], media_asset: ["video"], visibility: "private", captions: ["caption"] }, "youtube");
  expect(providerBlocker(readiness("youtube", ["video"]), scope, youtube, account("youtube"))).toContain("字幕uploadに未対応");
});

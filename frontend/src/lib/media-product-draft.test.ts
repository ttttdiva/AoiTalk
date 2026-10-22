import { describe, expect, it } from "vitest";
import { emptyProductDraft, productDraftError, productDraftFromPayload, productPayloadFromDraft } from "./media-product-draft";
import { productActionMatches, productRevenueRows } from "./media-product-api";
import type { FanzaContentPayload, ContentVariantRevision } from "./media-operations-content-api";
import type { PlatformAccount } from "./media-operations-setup-api";

const payload: FanzaContentPayload = { type: "fanza_release", title: "風景素材", description: "合成fixture", category: "illustration", age_rating: "all_ages", price: 0,
  sales: { currency: "USD", tax_included: false, distribution: "download" }, preview_assets: ["preview"], thumbnail_assets: ["thumbnail"], deliverable_package_ref: "artifact:fixture",
  rights_checklist: [{ code: "original", status: "passed", note: "自作のfixture" }, { code: "license", status: "review_required", note: null }],
  storefront: "commercial", seller_ref: "seller-fixture", ai_usage: "partial", ai_generated: false, submission_state: "ready_for_review" };
describe("saved product draft", () => {
  it("retains price zero, currency, distribution, rights status/note and FANZA preparation on edit", () => {
    expect(productPayloadFromDraft("fanza", productDraftFromPayload(payload))).toEqual(payload);
    const dlsite = { ...payload, rights_checklist: payload.rights_checklist.map(row => ({ ...row, note: row.note ?? null })), type: "dlsite_release" as const };
    const { storefront, seller_ref, ai_usage, ai_generated, submission_state, ...expected } = dlsite;
    expect(productPayloadFromDraft("dlsite", productDraftFromPayload(expected))).toEqual(expected);
    expect([storefront, seller_ref, ai_usage, ai_generated, submission_state]).toBeDefined();
  });
  it("names FANZA on required input errors and rejects invalid/negative prices without writing zero", () => {
    expect(productDraftError("fanza", emptyProductDraft())).toContain("FANZA");
    for (const price of ["", "NaN", "-1", "10000001"]) expect(() => productPayloadFromDraft("fanza", { ...productDraftFromPayload(payload), price })).toThrow("FANZA価格");
  });
});
describe("existing revenue scope", () => {
  const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "persona-rev", displayName: "Fixture" };
  const revision = { content_item_id: "item", content_variant_id: "variant", platform: "fanza" } as ContentVariantRevision;
  const account = { id: "account", account_ref: "seller-fixture" } as PlatformAccount;
  const event = { id: "event", owner_user_id: "owner", project_id: "project", persona_ref: "persona", platform: "fanza", platform_account_ref: "account", content_ref: "variant", gross_amount: null, net_amount: 0, currency: "JPY" };
  it("includes verified UUID and seller references while excluding other owner/project/persona/platform/account/content", () => {
    const external = { ...event, id: "external", platform_account_ref: account.account_ref };
    const outsiders = ["owner_user_id", "project_id", "persona_ref", "platform", "platform_account_ref", "content_ref"].map(key => ({ ...event, [key]: "foreign" }));
    expect(productRevenueRows([event, external, ...outsiders], scope, revision, account)).toEqual([event, external]);
    expect(productRevenueRows([event], scope, revision, account)[0]).toMatchObject({ gross_amount: null, net_amount: 0 });
  });
});

describe("saved product Action identity", () => {
  it("requires exact scoped revision, connection, immutable source pins and reviewed payload", () => {
    const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "persona-rev", displayName: "Fixture" };
    const revision = { id: "revision", platform: "fanza", content_item_id: "item", content_variant_id: "variant", content_item_hash: "a".repeat(64), content_hash: "b".repeat(64), persona_revision_id: "persona-rev", persona_revision_hash: "c".repeat(64), platform_account_revision_id: "account-rev", platform_account_revision_hash: "d".repeat(64), payload } as ContentVariantRevision;
    const account = { id: "account", connection_id: "connection" } as PlatformAccount;
    const action = { id: "action", owner_user_id: "owner", project_id: "project", action_type: "media.release_product", execution_mode: "manual", platform: "fanza", action_version: 1, version: 2, payload_hash: "e".repeat(64), content_item_id: "item", content_variant_id: "variant", content_variant_revision_id: "revision", persona_revision_id: "persona-rev", platform_account_id: "account", platform_account_revision_id: "account-rev", connection_id: "connection", payload: { content_variant_revision_hash: revision.content_hash, content_item_id: "item", content_item_hash: revision.content_item_hash, persona_revision_id: "persona-rev", persona_revision_hash: revision.persona_revision_hash, platform_account_id: "account", platform_account_revision_id: "account-rev", platform_account_revision_hash: revision.platform_account_revision_hash, payload } };
    expect(productActionMatches(action as never, scope, revision, account)).toBe(true);
    for (const key of ["owner_user_id", "project_id", "content_variant_revision_id", "platform_account_id", "connection_id", "execution_mode"]) expect(productActionMatches({ ...action, [key]: "foreign" } as never, scope, revision, account)).toBe(false);
    for (const key of ["content_item_hash", "persona_revision_hash", "platform_account_revision_hash", "content_variant_revision_hash"]) expect(productActionMatches({ ...action, payload: { ...action.payload, [key]: "f".repeat(64) } } as never, scope, revision, account)).toBe(false);
    expect(productActionMatches({ ...action, payload: { ...action.payload, payload: { ...payload, price: 99 } } } as never, scope, revision, account)).toBe(false);
  });
});

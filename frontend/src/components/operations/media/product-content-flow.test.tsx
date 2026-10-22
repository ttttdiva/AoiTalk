// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MediaContentVariantPanel } from "./content-variant-panel";
import type { ContentVariant, ContentVariantRevision, FanzaContentPayload } from "@/lib/media-operations-content-api";

const api = vi.hoisted(() => ({ listVariants: vi.fn(), getVariant: vi.fn(), listRevisions: vi.fn(), getReadiness: vi.fn(), appendRevision: vi.fn(), createVariant: vi.fn() }));
vi.mock("@/lib/media-operations-content-api", () => ({ mediaContentApi: api }));
const payload: FanzaContentPayload = { type: "fanza_release", title: "保存済み商品", description: "合成fixture", category: "illustration", age_rating: "all_ages", price: 550,
  sales: { currency: "USD", tax_included: false, distribution: "download" }, preview_assets: ["preview"], thumbnail_assets: ["thumbnail"], deliverable_package_ref: "artifact:fixture",
  rights_checklist: [{ code: "original", status: "passed", note: "fixture note" }], storefront: "commercial", seller_ref: "seller", ai_usage: "partial", ai_generated: false, submission_state: "ready_for_review" };
const revision = { id: "rev", content_variant_id: "variant", content_item_id: "item", persona_revision_id: "persona", platform_account_id: "account", platform_account_revision_id: "account-rev", content_item_hash: "a".repeat(64), persona_revision_hash: "b".repeat(64), version: 1, content_hash: "c".repeat(64), platform: "fanza", payload, generation_output_refs: [{ generation_output_id: "preview", sha256: "a".repeat(64) }], source_evidence: [] } as ContentVariantRevision;
const variant = { id: "variant", platform: "fanza", content_item_id: "item", current_revision: revision } as ContentVariant;
beforeEach(() => {
  vi.clearAllMocks(); api.listVariants.mockResolvedValue([variant]); api.getVariant.mockResolvedValue(variant); api.listRevisions.mockResolvedValue([revision]);
  api.getReadiness.mockResolvedValue({ ready: true, qa: { result: "passed" }, rights: { result: "cleared" } }); api.appendRevision.mockResolvedValue({ ...revision, version: 2 });
});
describe("product revision form", () => {
  it("loads the saved product explicitly and appends it without losing sale/rights/preparation values", async () => {
    render(<MediaContentVariantPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "保存済み商品を編集フォームに読込" }));
    expect(screen.getByLabelText("価格")).toHaveValue("550"); expect(screen.getByLabelText("通貨")).toHaveValue("USD");
    expect(screen.getByLabelText("権利項目 1")).toHaveValue("original"); expect(screen.getByLabelText("権利注記 1")).toHaveValue("fixture note");
    fireEvent.click(screen.getByRole("button", { name: "Revisionを追加" }));
    await waitFor(() => expect(api.appendRevision).toHaveBeenCalledTimes(1));
    expect(api.appendRevision.mock.calls[0][0]).toBe("variant");
    expect(api.appendRevision.mock.calls[0][1]).toMatchObject({ expected_version: 1, platform_account_id: "account", payload });
    expect(api.createVariant).not.toHaveBeenCalled();
  });
});

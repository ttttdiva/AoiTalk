// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ContentVariantRevision } from "@/lib/media-operations-content-api";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import { PersonaPublicationPreview } from "./persona-publication-preview";

describe("immutable publication preview", () => {
  it("shows persona, saved YouTube privacy/schedule, exact account and content hash without fetching untrusted URLs", () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const revision: ContentVariantRevision = { content_variant_id: "variant", content_item_id: "brief", content_item_hash: "b".repeat(64), persona_revision_id: "persona-revision", persona_revision_hash: "p".repeat(64), platform_account_id: "account", source_evidence: [], id: "revision", version: 2, platform: "youtube", platform_account_revision_id: "account-revision", content_hash: "c".repeat(64), generation_output_refs: [], payload: { type: "youtube_video", title: "Saved title", description: '<img src="https://untrusted.test/asset">', tags: [], media_asset: ["opaque-asset"], visibility: "private", scheduled_at: "2026-10-08T09:00:00Z" } };
    const account = { platform: "youtube", account_ref: "fixture-channel", current_revision: { display_name: "選択したチャンネル" } } as PlatformAccount;
    render(<PersonaPublicationPreview scope={{ personaId: "p", personaRevisionId: "pr", ownerUserId: "owner", projectId: "project", displayName: "UN//REAL" }} revision={revision} account={account} />);
    expect(screen.getByRole("region", { name: "公開する内容と接続先" })).toHaveTextContent("UN//REAL");
    expect(screen.getByRole("region", { name: "公開する内容と接続先" })).toHaveTextContent("fixture-channel");
    expect(screen.getByRole("region", { name: "公開する内容と接続先" })).toHaveTextContent("2026-10-08T09:00:00Z");
    expect(screen.getByText("c".repeat(64))).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "公開する内容と接続先" }).querySelector("img,video,iframe,script")).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

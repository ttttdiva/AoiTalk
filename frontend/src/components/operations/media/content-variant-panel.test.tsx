// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MediaContentVariantPanel } from "@/components/operations/media/content-variant-panel";
import { mediaContentApi } from "@/lib/media-operations-content-api";

vi.mock("@/lib/media-operations-content-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/media-operations-content-api")>("@/lib/media-operations-content-api");
  return {
    ...actual,
    mediaContentApi: {
      listVariants: vi.fn(),
      createVariant: vi.fn(),
      getVariant: vi.fn(),
      listRevisions: vi.fn(),
      appendRevision: vi.fn(),
      recordQa: vi.fn(),
      recordRights: vi.fn(),
      getReadiness: vi.fn(),
    },
  };
});

const variant = {
  id: "variant-1",
  content_item_id: "item-1",
  content_item_hash: "a".repeat(64),
  platform: "x" as const,
  status: "draft",
  current_revision: {
    id: "revision-1",
    content_variant_id: "variant-1",
    version: 1,
    content_item_id: "item-1",
    content_item_hash: "a".repeat(64),
    persona_revision_id: "persona-1",
    persona_revision_hash: "b".repeat(64),
    platform_account_id: "account-1",
    platform: "x" as const,
    payload: { type: "x_post" as const, text: "hello" },
    generation_output_refs: [],
    source_evidence: [],
    content_hash: "c".repeat(64),
  },
};

describe("MediaContentVariantPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(mediaContentApi.listVariants).mockResolvedValue([variant]);
    vi.mocked(mediaContentApi.getVariant).mockResolvedValue(variant);
    vi.mocked(mediaContentApi.listRevisions).mockResolvedValue([variant.current_revision]);
    vi.mocked(mediaContentApi.getReadiness).mockResolvedValue({
      content_variant_id: "variant-1",
      revision_id: "revision-1",
      revision_hash: "c".repeat(64),
      status: "blocked",
      ready: false,
      blocking_reasons: ["qa_missing", "rights_missing"],
      qa: null,
      rights: null,
    });
    vi.mocked(mediaContentApi.createVariant).mockResolvedValue(variant);
    vi.mocked(mediaContentApi.recordQa).mockResolvedValue({ id: "qa-1" } as never);
    vi.mocked(mediaContentApi.recordRights).mockResolvedValue({ id: "rights-1" } as never);
  });

  it("renders fail-closed readiness and submits an X typed payload", async () => {
    render(<MediaContentVariantPanel />);
    await waitFor(() => expect(mediaContentApi.listVariants).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId("content-variant-readiness")).toHaveTextContent("fail-closed"));

    await userEvent.type(screen.getByLabelText("ContentItem ID"), "item-2");
    await userEvent.type(screen.getByLabelText("Persona Revision ID"), "persona-2");
    await userEvent.type(screen.getByLabelText("PlatformAccount ID"), "account-2");
    await userEvent.type(screen.getByLabelText("本文"), "typed post");
    await userEvent.click(screen.getByRole("button", { name: "Variantを作成" }));

    await waitFor(() => expect(mediaContentApi.createVariant).toHaveBeenCalledTimes(1));
    const [input] = vi.mocked(mediaContentApi.createVariant).mock.calls[0];
    expect(input.payload).toEqual(expect.objectContaining({ type: "x_post", text: "typed post" }));
    expect(JSON.stringify(input.payload)).not.toContain("provider");
  });

  it("records structured QA checks rather than arbitrary JSON", async () => {
    render(<MediaContentVariantPanel />);
    await waitFor(() => expect(mediaContentApi.listVariants).toHaveBeenCalled());
    await waitFor(() => expect(screen.getAllByLabelText("Checks（codeを1行1件）")[0]).toBeInTheDocument());
    await userEvent.type(screen.getAllByLabelText("Checks（codeを1行1件）")[0], "alt_text");
    await userEvent.click(screen.getByRole("button", { name: "QAを記録" }));
    await waitFor(() => expect(mediaContentApi.recordQa).toHaveBeenCalledTimes(1));
    const [input] = vi.mocked(mediaContentApi.recordQa).mock.calls[0];
    expect(input.checks[0]).toEqual(expect.objectContaining({ code: "alt_text", mandatory: true }));
  });
  it("requires explicit TikTok privacy and sends the video rights declaration", async () => {
    render(<MediaContentVariantPanel />);
    await waitFor(() => expect(mediaContentApi.listVariants).toHaveBeenCalled());
    await userEvent.click(screen.getByLabelText("Platform"));
    await userEvent.click(await screen.findByRole("option", { name: "TikTok" }));
    await userEvent.type(screen.getByLabelText("ContentItem ID"), "item-2");
    await userEvent.type(screen.getByLabelText("Persona Revision ID"), "persona-2");
    await userEvent.type(screen.getByLabelText("PlatformAccount ID"), "account-2");
    await userEvent.type(screen.getByLabelText("Video Asset ID"), "video-1");
    await userEvent.click(screen.getByRole("button", { name: "Variantを作成" }));
    expect(mediaContentApi.createVariant).not.toHaveBeenCalled();
    await userEvent.click(screen.getByLabelText("公開範囲（選択必須）"));
    await userEvent.click(await screen.findByRole("option", { name: "自分のみ" }));
    await userEvent.click(screen.getByLabelText("音源の権利"));
    await userEvent.click(await screen.findByRole("option", { name: "音楽なし" }));
    await userEvent.click(screen.getByRole("button", { name: "Variantを作成" }));
    await waitFor(() => expect(mediaContentApi.createVariant).toHaveBeenCalledTimes(1));
    expect(vi.mocked(mediaContentApi.createVariant).mock.calls[0][0].payload).toEqual(expect.objectContaining({
      type: "tiktok_video", privacy: "SELF_ONLY", music_rights: "no_music", ai_disclosure: false,
    }));
  });

  it("exposes distinct FANZA storefront and AI usage without claiming publication", async () => {
    render(<MediaContentVariantPanel />);
    await waitFor(() => expect(mediaContentApi.listVariants).toHaveBeenCalled());
    await userEvent.click(screen.getByLabelText("Platform"));
    await userEvent.click(await screen.findByRole("option", { name: "FANZA" }));
    expect(screen.getByLabelText("FANZA売場")).toBeInTheDocument();
    expect(screen.getByLabelText("販売者参照")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("AI利用区分"));
    await userEvent.click(await screen.findByRole("option", { name: "一部AI利用" }));
    expect(screen.getByText(/商品下書きです/)).toBeInTheDocument();
    expect(mediaContentApi.createVariant).not.toHaveBeenCalled();
  });

});

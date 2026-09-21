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
});

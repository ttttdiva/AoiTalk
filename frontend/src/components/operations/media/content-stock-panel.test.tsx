// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MediaStockItem } from "@/lib/media-operations-api";
import { ContentStockPanel } from "./content-stock-panel";

const mocks = vi.hoisted(() => ({ list: vi.fn(), change: vi.fn() }));
vi.mock("@/lib/media-operations-api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/media-operations-api")>(),
  mediaOperationsApi: {
    listStockItems: mocks.list,
    changeStockAdoption: mocks.change,
  },
}));

const waitingItem: MediaStockItem = {
  id: "item-1",
  owner_user_id: "owner-1",
  project_id: "project-1",
  brand_name: "UN//REAL",
  title: "初期制作設計",
  brief: "制作brief",
  status: "draft",
  version: 1,
  content_hash: "a".repeat(64),
  stock_status: "awaiting_adoption",
  blockers: ["analysis_evidence_missing"],
  findings: [{
    id: "finding-1",
    kind: "hypothesis",
    statement: "設計原文に基づく仮説",
    evidence: [{ type: "url", url: "https://example.invalid/source" }],
  }],
  variants: [],
  latest_decision: null,
};

const adoptableItem: MediaStockItem = {
  ...waitingItem,
  stock_status: "awaiting_adoption",
  blockers: [],
  findings: [{
    id: "finding-fact",
    kind: "fact",
    statement: "確認した計測結果",
    evidence: [{ type: "url", url: "https://example.invalid/metrics" }],
  }],
  variants: [{
    id: "variant-1",
    platform: "x",
    revision_id: "revision-1",
    revision_hash: "b".repeat(64),
    selected_outputs: [],
    qa_result: "passed",
    rights_result: "cleared",
    blockers: [],
  }],
};

function mount() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false }}>
      <ContentStockPanel />
    </SWRConfig>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.list.mockResolvedValue([waitingItem]);
  mocks.change.mockResolvedValue({ id: "decision-1" });
});

describe("ContentStockPanel", () => {
  it("shows Japanese finding and blocker labels and disables blocked adoption", async () => {
    mount();
    expect(await screen.findByTestId("stock-finding-finding-1")).toHaveTextContent("設計原文に基づく仮説");
    expect(screen.getByText("fact / signal の URL または計測値参照がありません")).toBeVisible();
    expect(screen.queryByRole("button", { name: "制作ストックに採用" })).not.toBeInTheDocument();
  });

  it("adopts an eligible item with its optimistic tokens and refreshes the list", async () => {
    mocks.list.mockResolvedValue([adoptableItem]);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "制作ストックに採用" }));
    await waitFor(() => expect(mocks.change).toHaveBeenCalledWith(
      "item-1",
      "adopt",
      "a".repeat(64),
      1,
      expect.any(String),
    ));
    await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));
  });

  it("keeps mutation failures visible", async () => {
    mocks.list.mockResolvedValue([adoptableItem]);
    mocks.change.mockRejectedValue(new Error("conflict"));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "制作ストックに採用" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("ストック状態を更新できませんでした");
  });
});

// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { MediaResultsPanel } from "./results-panel";
import { mediaOverviewApi, type MediaResultsResponse } from "@/lib/media-operations-overview-api";
vi.mock("@/lib/media-operations-overview-api", () => ({ mediaOverviewApi: { getResults: vi.fn() } }));

it("keeps same-currency evidence classes and negative refunds distinct without false zero or profit", async () => {
  const result: MediaResultsResponse = {
    start: "2026-10-01", end: "2026-10-06",
    metric_snapshots: { count: 1, by_platform: [{ platform: "youtube", snapshot_count: 1, metrics: { views: 5, gross_revenue: 555, net_revenue: 444, refunds: 333, cost: 222 } }], by_persona: [], by_account: [], by_content: [] },
    revenue: { event_count: 5, by_currency: [
      { currency: "USD", event_count: 1, gross: 100, net: 90, revenue_authority: "manual_estimate", event_type: "sale" },
      { currency: "USD", event_count: 1, gross: 100, net: 90, revenue_authority: "provider_settled", event_type: "sale" },
      { currency: "USD", event_count: 1, gross: -20, net: -18, revenue_authority: "provider_settled", event_type: "refund" },
      { currency: "JPY", event_count: 1, gross: null, net: 0 },
      { currency: null, event_count: 1, gross: 100, net: 90 },
    ] },
    experiments: { count: 0, by_status: {}, result_count: 0 }, learning: { count: 0, by_status: {}, pending_review_count: 0 }, evidence_count: 0,
  };
  vi.mocked(mediaOverviewApi.getResults).mockResolvedValue(result);
  const errors = vi.spyOn(console, "error");
  try {
    render(<MediaResultsPanel />);
    expect(await screen.findAllByTestId("revenue-bucket")).toHaveLength(5);
    expect(screen.getByText(/USD · 売上 · 推計（自己申告）/)).toBeInTheDocument();
    expect(screen.getByText(/USD · 売上 · API記録・入金確定/)).toBeInTheDocument();
    expect(screen.getByText(/USD · 返金 · API記録・入金確定/)).toBeInTheDocument();
    expect(screen.getByText("総額 -20 · 手取り -18")).toBeInTheDocument();
    expect(screen.getByText("総額 不明 · 手取り 0")).toBeInTheDocument();
    expect(screen.getByText("総額 不明 · 手取り 不明")).toBeInTheDocument();
    expect(screen.getByText(/利益は算出できません/)).toBeInTheDocument();
    expect(screen.queryByText(/gross_revenue:|net_revenue:|refunds:|cost:/)).not.toBeInTheDocument();
    expect(errors.mock.calls.flat().join(" ")).not.toMatch(/same key|unique.*key/i);
  } finally { errors.mockRestore(); }
});

it("separates cost authority and only shows a finite, server-calculated profit in a known currency", async () => {
  const base = { currency: "USD", event_count: 1, gross: 100, net: 90, revenue_authority: "provider_settled", event_type: "sale", fee_amount: 10, production_cost_amount: 20, human_cost_amount: 5, human_minutes: 30, profit_amount: 65, profit_status: "calculated_input_scope" };
  vi.mocked(mediaOverviewApi.getResults).mockResolvedValue({
    start: "2026-10-01", end: "2026-10-06",
    metric_snapshots: { count: 0, by_platform: [], by_persona: [], by_account: [], by_content: [] },
    revenue: { event_count: 5, by_currency: [
      { ...base, cost_authority: "manual_settled" },
      { ...base, cost_authority: "imported_settled" },
      { ...base, cost_authority: "manual_estimate", profit_status: "unavailable_basis_mismatch" },
      { ...base, cost_authority: "unknown", profit_amount: Number.POSITIVE_INFINITY },
      { ...base, currency: null, cost_authority: "manual_settled" },
    ] },
    experiments: { count: 0, by_status: {}, result_count: 0 },
    learning: { count: 0, by_status: {}, pending_review_count: 0 }, evidence_count: 0,
  });
  const errors = vi.spyOn(console, "error");
  try {
    render(<MediaResultsPanel />);
    const buckets = await screen.findAllByTestId("revenue-bucket");
    expect(buckets).toHaveLength(5);
    expect(within(buckets[0]).getByText("入力範囲の利益（自己申告）: 65 USD")).toBeInTheDocument();
    expect(within(buckets[1]).getByText("入力範囲の利益: 65 USD")).toBeInTheDocument();
    for (const bucket of buckets.slice(2)) expect(bucket).toHaveTextContent(/入力範囲の利益.*: 不明/);
    expect(buckets[0]).toHaveTextContent("手数料 10 · 制作原価 20 · 人的費用 5");
    expect(buckets[2]).toHaveTextContent("売上と費用の推計・確定区分が不一致");
    expect(screen.getByText(/手数料は手取りから控除済みのため再控除しません/)).toBeInTheDocument();
    expect(errors.mock.calls.flat().join(" ")).not.toMatch(/same key|unique.*key/i);
  } finally { errors.mockRestore(); }
});

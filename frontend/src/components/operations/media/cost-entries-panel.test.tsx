// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CostEntriesPanel } from "./cost-entries-panel";
import { mediaOperationsApi, MediaOperationsApiError, type MediaCharacterDashboard, type MediaCostEntry, type RevenueCostRecord, type CharacterDashboardRevenueItem } from "@/lib/media-operations-api";

vi.mock("@/lib/media-operations-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/media-operations-api")>("@/lib/media-operations-api");
  return { ...actual, mediaOperationsApi: { ...actual.mediaOperationsApi, listCostEntries: vi.fn(), createCostEntry: vi.fn(), getCostEntryHistory: vi.fn(), getRevenueCostRecords: vi.fn(), allocateCostEntry: vi.fn() } };
});
const dashboard = { character: { id: "persona-1", project_id: "project-1" }, variants: { items: [{ id: "content-1", name: "動画A" }] } } as MediaCharacterDashboard;
const event: CharacterDashboardRevenueItem = { id: "event-1", persona_ref: "persona-1", event_at: "2026-10-05T12:00:00Z", currency: "USD", gross_amount: 100, net_amount: 90, settlement_status: "estimated", event_type: "sale", revenue_authority: "manual_estimate" };
function entry(overrides: Partial<MediaCostEntry> = {}): MediaCostEntry {
  return {
    id: "revision-1", entry_id: "entry-1", revision: 1, owner_user_id: "owner", project_id: "project-1", persona_ref: "persona-1", content_ref: null,
    currency: "USD", source: "manual", basis: "estimated", window_start: "2026-10-01T00:00:00Z", window_end: "2026-10-31T23:59:00Z",
    fee_amount: 10, production_cost_amount: 20, human_cost_amount: null, human_minutes: null,
    missing_reasons: { human_cost_amount: "見積未確定", human_minutes: "未計測" }, coverage_complete: false, coverage_note: "動画制作の費用",
    evidence: [{ kind: "record", ref: "persona-1", note: "制作メモ", evidence_hash: "e".repeat(64) }], correction_of_id: null,
    entry_hash: "a".repeat(64), idempotency_key: "entry-key", created_by: "owner", created_at: "2026-10-06T10:00:00Z",
    allocation_status: "unallocated", allocated_revenue_event_id: null,
    unallocated_fee_amount: 10, unallocated_production_cost_amount: 20, unallocated_human_cost_amount: null, unallocated_human_minutes: null,
    ...overrides,
  };
}
function statement(): RevenueCostRecord {
  return {
    id: "statement-1", cost_entry_id: "revision-1", revenue_event_id: "event-1", revision: 1,
    owner_user_id: "owner", project_id: "project-1", revenue_event_hash: "b".repeat(64), cost_hash: "c".repeat(64),
    currency: "USD", source: "manual", basis: "estimated", fee_amount: 10, production_cost_amount: 20, human_cost_amount: null, human_minutes: null,
    missing_reasons: entry().missing_reasons, coverage_complete: false, coverage_note: entry().coverage_note, evidence: entry().evidence,
    correction_of_id: null, idempotency_key: "allocation-key", created_by: "owner", created_at: "2026-10-06T11:00:00Z",
    attribution_method: "direct_event", window_start: event.event_at!, window_end: event.event_at!,
    cost_authority: "manual_estimate", profit_amount: null, profit_status: "unavailable_cost_coverage",
  };
}
const emptyRevenue = { items: [], effective: null, has_more: false };
function history(...items: MediaCostEntry[]) { return { items, effective: items[0] ?? null, has_more: false }; }
async function openPanel(events: CharacterDashboardRevenueItem[] = []) {
  const onAllocated = vi.fn();
  const view = render(<CostEntriesPanel dashboard={dashboard} revenueItems={events} onAllocated={onAllocated} />);
  await userEvent.click(screen.getByRole("button", { name: "未配賦費用を開く" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "新しい未配賦費用" })).toBeEnabled());
  return { ...view, onAllocated };
}
function fillEntry() {
  for (const [label, value] of [["未配賦費用の通貨", "USD"], ["費用対象開始UTC", "2026-10-01T00:00"], ["費用対象終了UTC", "2026-10-31T23:59"], ["手数料", "10"], ["制作原価", "20"], ["人的費用の未入力理由", "見積未確定"], ["作業時間（分）の未入力理由", "未計測"], ["費用の入力範囲・判断根拠", "動画制作の費用"], ["追加の証拠メモ", "制作メモ"]]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
}
function submit() { fireEvent.submit(screen.getByRole("form", { name: "未配賦費用フォーム" })); }

describe("CostEntriesPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(mediaOperationsApi.listCostEntries).mockResolvedValue([]);
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValue(emptyRevenue);
  });

  it("saves costs with no sale, reads them back, then allocates the whole nullable entry to a selected sale", async () => {
    const saved = entry();
    const allocated = entry({ allocation_status: "allocated", allocated_revenue_event_id: "event-1", unallocated_fee_amount: 0, unallocated_production_cost_amount: 0, unallocated_human_cost_amount: 0, unallocated_human_minutes: 0 });
    const cost = statement();
    vi.mocked(mediaOperationsApi.listCostEntries).mockResolvedValueOnce([]).mockResolvedValueOnce([saved]).mockResolvedValueOnce([allocated]);
    vi.mocked(mediaOperationsApi.createCostEntry).mockResolvedValue(saved);
    vi.mocked(mediaOperationsApi.getCostEntryHistory).mockResolvedValueOnce(history(saved)).mockResolvedValueOnce(history(allocated));
    vi.mocked(mediaOperationsApi.allocateCostEntry).mockResolvedValue(cost);
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValueOnce(emptyRevenue).mockResolvedValueOnce({ items: [cost], effective: cost, has_more: false });
    const { rerender, onAllocated } = await openPanel();
    fillEntry(); submit();
    await screen.findByText("未配賦費用の保存後の再読込を確認しました");
    expect(mediaOperationsApi.createCostEntry).toHaveBeenCalledWith(expect.objectContaining({ persona_ref: "persona-1", project_id: "project-1", content_ref: null, currency: "USD", basis: "estimated", human_cost_amount: null, human_minutes: null, window_start: "2026-10-01T00:00:00.000Z", missing_reasons: { human_cost_amount: "見積未確定", human_minutes: "未計測" } }), expect.any(String));
    await userEvent.click(screen.getByRole("button", { name: "売上前・未配賦費用の説明" }));
    expect(await screen.findByText(/未配賦費用は売上の入力範囲の利益に含まれません/)).toBeInTheDocument();
    rerender(<CostEntriesPanel dashboard={dashboard} revenueItems={[event]} onAllocated={onAllocated} />);
    await userEvent.click(screen.getByRole("button", { name: "費用 entry-1 の配賦先を選ぶ" }));
    await userEvent.click(screen.getByRole("button", { name: "売上 event-1 を配賦先に選択" }));
    const allocate = await screen.findByRole("button", { name: "この売上に全額配賦する" });
    await userEvent.click(allocate);
    await screen.findByText("全額配賦後の費用・売上を再読込して確認しました");
    expect(mediaOperationsApi.allocateCostEntry).toHaveBeenCalledWith("event-1", "revision-1", { correction_of_id: null }, expect.any(String));
    expect(onAllocated).toHaveBeenCalledWith("event-1", cost);
    expect(screen.getByRole("button", { name: "費用 entry-1 の配賦先を選ぶ" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "未配賦費用の訂正版を保存" })).toBeDisabled();
    expect(screen.getByText("配賦済み費用は訂正・移動できません。保存履歴を確認できます。")).toBeInTheDocument();
    const readback = screen.getAllByTestId("cost-entry-readback-revision-1")[0];
    expect(readback).toHaveTextContent("人的費用 不明 · 作業時間 不明 分");
    expect(readback).toHaveTextContent("未配賦残: 手数料 0 · 制作原価 0 · 人的費用 0 · 作業時間 0 分");
  });

  it("loads the latest revision before correction and preserves normalized URL evidence", async () => {
    const original = entry({ evidence: [{ kind: "url", ref: "https://example.com/invoice", evidence_hash: "d".repeat(64) }] });
    const corrected = entry({ id: "revision-2", revision: 2, correction_of_id: "revision-1", production_cost_amount: 25, evidence: original.evidence });
    vi.mocked(mediaOperationsApi.listCostEntries).mockResolvedValue([original]);
    vi.mocked(mediaOperationsApi.getCostEntryHistory).mockResolvedValueOnce(history(original)).mockResolvedValueOnce(history(corrected, original));
    vi.mocked(mediaOperationsApi.createCostEntry).mockResolvedValue(corrected);
    await openPanel();
    await userEvent.click(screen.getByRole("button", { name: "費用 entry-1 の詳細・訂正" }));
    await waitFor(() => expect(screen.getByLabelText("制作原価")).toHaveValue(20));
    fireEvent.change(screen.getByLabelText("制作原価"), { target: { value: "25" } });
    submit();
    await screen.findByText("未配賦費用の保存後の再読込を確認しました");
    expect(mediaOperationsApi.createCostEntry).toHaveBeenCalledWith(expect.objectContaining({ correction_of_id: "revision-1", currency: "USD", basis: "estimated", production_cost_amount: 25, evidence: [expect.objectContaining({ kind: "url", url: "https://example.com/invoice" })] }), expect.any(String));
    expect(vi.mocked(mediaOperationsApi.createCostEntry).mock.calls[0][0].evidence[0]).not.toHaveProperty("ref");
    expect(vi.mocked(mediaOperationsApi.createCostEntry).mock.calls[0][0].evidence[0]).not.toHaveProperty("evidence_hash");
    expect(screen.getByLabelText("未配賦費用の通貨")).toBeDisabled();
    expect(screen.getByText(/独立費用の保存履歴（2件）/)).toBeInTheDocument();
  });

  it("blocks double submit and closing during a save, and recovers accepted saves using GET only", async () => {
    let resolve!: (entry: MediaCostEntry) => void;
    vi.mocked(mediaOperationsApi.createCostEntry).mockReturnValue(new Promise((done) => { resolve = done; }));
    vi.mocked(mediaOperationsApi.getCostEntryHistory).mockRejectedValueOnce(new Error("readback offline")).mockResolvedValueOnce(history(entry()));
    await openPanel(); fillEntry(); submit(); submit();
    expect(mediaOperationsApi.createCostEntry).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "未配賦費用を閉じる" })).toBeDisabled();
    await act(async () => resolve(entry()));
    await screen.findByText(/保存は受け付けられましたが、再読込確認に失敗/);
    submit();
    expect(mediaOperationsApi.createCostEntry).toHaveBeenCalledTimes(1);
    vi.mocked(mediaOperationsApi.listCostEntries).mockResolvedValue([entry()]);
    await userEvent.click(screen.getByRole("button", { name: "費用一覧を再読込" }));
    await screen.findByText("費用一覧をサーバーから再読込しました");
    expect(screen.getByRole("button", { name: "未配賦費用の訂正版を保存" })).toBeEnabled();
  });

  it("previews replacement of current sale costs and requires a reload after an allocation conflict", async () => {
    vi.mocked(mediaOperationsApi.listCostEntries).mockResolvedValue([entry()]);
    const existing = { ...statement(), id: "manual-cost", cost_entry_id: null };
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValue({ items: [existing], effective: existing, has_more: false });
    vi.mocked(mediaOperationsApi.allocateCostEntry).mockRejectedValue(new MediaOperationsApiError("conflict", { status: 409 }));
    await openPanel([event]);
    await userEvent.click(screen.getByRole("button", { name: "費用 entry-1 の配賦先を選ぶ" }));
    await userEvent.click(screen.getByRole("button", { name: "売上 event-1 を配賦先に選択" }));
    expect(await screen.findByText(/既存額への加算はしません/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "この売上に全額配賦する" }));
    await screen.findByText(/費用または売上の最新版が変わりました/);
    expect(mediaOperationsApi.allocateCostEntry).toHaveBeenCalledWith("event-1", "revision-1", { correction_of_id: "manual-cost" }, expect.any(String));
    expect(screen.getByRole("button", { name: "この売上に全額配賦する" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "費用一覧を再読込" }));
    await screen.findByText("費用一覧をサーバーから再読込しました");
    expect(screen.queryByRole("region", { name: "費用の配賦先" })).not.toBeInTheDocument();
    expect(mediaOperationsApi.allocateCostEntry).toHaveBeenCalledTimes(1);
  });

  it("filters incompatible allocation targets and supports cancelling before any POST", async () => {
    vi.mocked(mediaOperationsApi.listCostEntries).mockResolvedValue([entry({ content_ref: "content-1" })]);
    await openPanel([
      { ...event, id: "match", content_ref: "content-1" },
      { ...event, id: "wrong-currency", content_ref: "content-1", currency: "JPY" },
      { ...event, id: "wrong-time", content_ref: "content-1", event_at: "2026-09-01T00:00:00Z" },
      { ...event, id: "wrong-content", content_ref: "content-2" },
      { ...event, id: "wrong-basis", content_ref: "content-1", settlement_status: "settled" },
      { ...event, id: "wrong-persona", content_ref: "content-1", persona_ref: "persona-2" },
      { ...event, id: "wrong-null-persona", content_ref: "content-1", persona_ref: null },
    ]);
    await userEvent.click(screen.getByRole("button", { name: "費用 entry-1 の配賦先を選ぶ" }));
    const region = screen.getByRole("region", { name: "費用の配賦先" });
    expect(within(region).getByRole("button", { name: "売上 match を配賦先に選択" })).toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: /wrong-/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "配賦をキャンセル" }));
    expect(screen.queryByRole("region", { name: "費用の配賦先" })).not.toBeInTheDocument();
    expect(mediaOperationsApi.allocateCostEntry).not.toHaveBeenCalled();
  });

  it("preserves UTC-naive windows in the UTC fields and matches explicit UTC event boundaries", async () => {
    const naive = entry({ window_start: "2026-10-01T00:00:00", window_end: "2026-10-01T00:10:00" });
    vi.mocked(mediaOperationsApi.listCostEntries).mockResolvedValue([naive]);
    vi.mocked(mediaOperationsApi.getCostEntryHistory).mockResolvedValue(history(naive));
    await openPanel([
      { ...event, id: "utc-start", event_at: "2026-10-01T00:00:00Z" },
      { ...event, id: "naive-end", event_at: "2026-10-01T00:10:00" },
      { ...event, id: "before-start", event_at: "2026-09-30T23:59:59Z" },
    ]);
    await userEvent.click(screen.getByRole("button", { name: "費用 entry-1 の詳細・訂正" }));
    await waitFor(() => expect(screen.getByLabelText("費用対象開始UTC")).toHaveValue("2026-10-01T00:00"));
    expect(screen.getByLabelText("費用対象終了UTC")).toHaveValue("2026-10-01T00:10");
    await userEvent.click(screen.getByRole("button", { name: "費用 entry-1 の配賦先を選ぶ" }));
    expect(screen.getByRole("button", { name: "売上 utc-start を配賦先に選択" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "売上 naive-end を配賦先に選択" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "売上 before-start を配賦先に選択" })).not.toBeInTheDocument();
  });
});

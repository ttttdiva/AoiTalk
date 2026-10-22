// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  mediaOperationsApi,
  MediaOperationsApiError,
  type CharacterDashboardRevenueItem,
  type RevenueCostRecord,
  type RevenueCostRecordHistory,
} from "@/lib/media-operations-api";
import { RevenueCostEditor } from "./revenue-cost-editor";

vi.mock("@/lib/media-operations-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/media-operations-api")>("@/lib/media-operations-api");
  return { ...actual, mediaOperationsApi: { ...actual.mediaOperationsApi, getRevenueCostRecords: vi.fn(), createRevenueCostRecord: vi.fn() } };
});

const event: CharacterDashboardRevenueItem = {
  id: "event-1", currency: "USD", event_at: "2026-10-05T12:00:00Z",
  gross_amount: 100, net_amount: 90, settlement_status: "settled", revenue_authority: "provider_settled", event_type: "sale",
};
function record(overrides: Partial<RevenueCostRecord> = {}): RevenueCostRecord {
  return {
    id: "cost-1", cost_entry_id: null, revenue_event_id: "event-1", revision: 1,
    owner_user_id: "owner", project_id: null, revenue_event_hash: "a".repeat(64), cost_hash: "b".repeat(64),
    currency: "USD", source: "manual", basis: "settled",
    fee_amount: 10, production_cost_amount: 20, human_cost_amount: 5, human_minutes: 30,
    missing_reasons: {}, coverage_complete: true, coverage_note: "この売上の費用3区分を確認",
    evidence: [{ kind: "revenue_event", ref: "event-1", note: "作業記録を確認", evidence_hash: "e".repeat(64) }],
    correction_of_id: null, idempotency_key: "key-1", attribution_method: "direct_event",
    window_start: event.event_at!, window_end: event.event_at!, created_by: "owner", created_at: "2026-10-06T10:00:00Z",
    profit_amount: 65, profit_status: "calculated_input_scope", cost_authority: "manual_settled",
    ...overrides,
  };
}
const empty: RevenueCostRecordHistory = { items: [], effective: null, has_more: false };
function history(...items: RevenueCostRecord[]): RevenueCostRecordHistory {
  return { items, effective: items[0] ?? null, has_more: false };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
async function renderEditor() {
  const onClose = vi.fn();
  const onReadback = vi.fn();
  const view = render(<RevenueCostEditor event={event} onClose={onClose} onReadback={onReadback} />);
  await screen.findByRole("form", { name: "費用記録フォーム" });
  return { ...view, onClose, onReadback };
}
function fillKnown() {
  for (const [label, value] of [["手数料", "10"], ["制作原価", "20"], ["人的費用", "5"], ["作業時間（分）", "30"]]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
  fireEvent.change(screen.getByLabelText("費用の入力範囲・判断根拠"), { target: { value: "この売上の費用3区分を確認" } });
  fireEvent.change(screen.getByLabelText("追加の証拠メモ"), { target: { value: "作業記録を確認" } });
}
function submit() { fireEvent.submit(screen.getByRole("form", { name: "費用記録フォーム" })); }

describe("RevenueCostEditor", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValue(empty);
  });

  it("saves nullable inputs with reasons and confirms the actual GET before appending a correction", async () => {
    const incomplete = record({ human_cost_amount: null, human_minutes: null, missing_reasons: { human_cost_amount: "金額未確定", human_minutes: "未計測" }, coverage_complete: false, profit_amount: null, profit_status: "unavailable_cost_coverage" });
    const corrected = record({ id: "cost-2", revision: 2, correction_of_id: "cost-1" });
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValueOnce(empty).mockResolvedValueOnce(history(incomplete)).mockResolvedValueOnce(history(corrected, incomplete));
    vi.mocked(mediaOperationsApi.createRevenueCostRecord).mockResolvedValueOnce(incomplete).mockResolvedValueOnce(corrected);
    const { onReadback } = await renderEditor();
    expect(screen.getByText("対象日時: 2026-10-05T12:00:00Z")).toBeVisible();
    fillKnown();
    fireEvent.change(screen.getByLabelText("人的費用"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("作業時間（分）"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("人的費用の未入力理由"), { target: { value: "金額未確定" } });
    fireEvent.change(screen.getByLabelText("作業時間（分）の未入力理由"), { target: { value: "未計測" } });
    submit();
    await screen.findByText("保存後の再読込を確認しました（保存版 1）");
    expect(mediaOperationsApi.createRevenueCostRecord).toHaveBeenCalledWith("event-1", expect.objectContaining({
      human_cost_amount: null, human_minutes: null, coverage_complete: false, basis: "settled", source: "manual",
      missing_reasons: { human_cost_amount: "金額未確定", human_minutes: "未計測" }, correction_of_id: null,
    }), expect.any(String));
    expect(within(screen.getByTestId("cost-readback")).getByText(/入力範囲の利益（自己申告）: 不明/)).toBeInTheDocument();
    expect(onReadback).toHaveBeenLastCalledWith(incomplete);
    fillKnown();
    await userEvent.click(screen.getByRole("checkbox"));
    submit();
    await screen.findByText("保存後の再読込を確認しました（保存版 2）");
    expect(mediaOperationsApi.createRevenueCostRecord).toHaveBeenLastCalledWith("event-1", expect.objectContaining({ correction_of_id: "cost-1", coverage_complete: true, fee_amount: 10, production_cost_amount: 20, human_cost_amount: 5 }), expect.any(String));
    expect(screen.getByTestId("cost-readback")).toHaveTextContent("入力範囲の利益（自己申告）: 65 USD");
    expect(screen.getByText(/保存履歴（2件/)).toBeInTheDocument();
    await userEvent.click(screen.getByText(/保存履歴（2件/));
    expect(screen.getByText(/第1版 · .*過去の版/)).toBeVisible();
    expect(screen.getByText(/人的費用 未入力: 金額未確定/)).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "費用記録の説明" }));
    expect(screen.getByText(/税・その他の費用まで網羅した純利益ではありません/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "取込資料に基づく" })).toBeDisabled();
  });

  it("blocks repeated submit and cancel while POST and readback are pending", async () => {
    const pending = deferred<RevenueCostRecord>();
    vi.mocked(mediaOperationsApi.createRevenueCostRecord).mockReturnValue(pending.promise);
    const { onClose } = await renderEditor();
    fillKnown();
    submit(); submit();
    expect(mediaOperationsApi.createRevenueCostRecord).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "閉じる・キャンセル" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "閉じる・キャンセル" }));
    expect(onClose).not.toHaveBeenCalled();
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValue(history(record()));
    await act(async () => pending.resolve(record()));
    await screen.findByText(/保存後の再読込を確認しました/);
    expect(mediaOperationsApi.getRevenueCostRecords).toHaveBeenCalledTimes(2);
  });

  it("ignores a late read after cancellation or changing the selected event", async () => {
    const pending = deferred<RevenueCostRecordHistory>();
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(empty);
    const onReadback = vi.fn();
    const onClose = vi.fn();
    const view = render(<RevenueCostEditor key="first" event={event} onClose={onClose} onReadback={onReadback} />);
    fireEvent.click(screen.getByRole("button", { name: "閉じる・キャンセル" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    view.rerender(<RevenueCostEditor key="second" event={{ ...event, id: "event-2" }} onClose={onClose} onReadback={onReadback} />);
    await screen.findByRole("form");
    await act(async () => pending.resolve(history(record())));
    expect(onReadback).toHaveBeenCalledTimes(1);
    expect(onReadback).toHaveBeenCalledWith(null);
    expect(screen.getByTestId("cost-readback")).toHaveTextContent("記録なし");
    expect(mediaOperationsApi.createRevenueCostRecord).not.toHaveBeenCalled();
  });

  it("reuses the idempotency key when retrying an uncertain POST with unchanged input", async () => {
    vi.mocked(mediaOperationsApi.createRevenueCostRecord).mockRejectedValueOnce(new TypeError("network")).mockResolvedValueOnce(record());
    await renderEditor(); fillKnown(); submit();
    await screen.findByText(/保存を確認できませんでした/);
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValue(history(record()));
    submit();
    await screen.findByText(/保存後の再読込を確認しました/);
    const calls = vi.mocked(mediaOperationsApi.createRevenueCostRecord).mock.calls;
    expect(calls[0][2]).toBe(calls[1][2]);
    expect(calls[0][1]).toEqual(calls[1][1]);
  });

  it("requires GET recovery after an accepted save whose readback failed", async () => {
    vi.mocked(mediaOperationsApi.createRevenueCostRecord).mockResolvedValue(record());
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValueOnce(empty).mockRejectedValueOnce(new Error("read failed")).mockResolvedValueOnce(history(record()));
    await renderEditor(); fillKnown(); submit();
    await screen.findByText(/保存は受け付けられましたが/);
    expect(screen.getByRole("button", { name: "費用記録を保存" })).toBeDisabled();
    submit();
    expect(mediaOperationsApi.createRevenueCostRecord).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "最新の履歴を再読込" }));
    await screen.findByText("サーバーから費用記録を再読込しました");
    expect(screen.getByTestId("cost-readback")).toHaveTextContent("65 USD");
    expect(screen.getByRole("button", { name: "訂正版を追加保存" })).toBeEnabled();
  });

  it("retains the draft on 409 and requires loading the latest revision before correcting it", async () => {
    const latest = record({ id: "other-cost", revision: 2, production_cost_amount: 28 });
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValueOnce(history(record())).mockResolvedValueOnce(history(latest, record()));
    vi.mocked(mediaOperationsApi.createRevenueCostRecord).mockRejectedValue(new MediaOperationsApiError("conflict", { status: 409 }));
    await renderEditor();
    fireEvent.change(screen.getByLabelText("制作原価"), { target: { value: "29" } });
    submit();
    await screen.findByText(/別の費用記録が先に保存されました/);
    expect(screen.getByLabelText("制作原価")).toHaveValue(29);
    expect(screen.getByRole("button", { name: "訂正版を追加保存" })).toBeDisabled();
    submit();
    expect(mediaOperationsApi.createRevenueCostRecord).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "最新の履歴を再読込" }));
    await screen.findByText("サーバーから費用記録を再読込しました");
    expect(screen.getByLabelText("制作原価")).toHaveValue(28);
    submit();
    await waitFor(() => expect(mediaOperationsApi.createRevenueCostRecord).toHaveBeenCalledTimes(2));
    expect(vi.mocked(mediaOperationsApi.createRevenueCostRecord).mock.calls[1][1].correction_of_id).toBe("other-cost");
  });

  it("keeps empty inputs unknown, requires missing reasons and blocks writes after a permission failure", async () => {
    await renderEditor();
    submit();
    await screen.findByText("手数料が未入力の理由を入力してください");
    expect(mediaOperationsApi.createRevenueCostRecord).not.toHaveBeenCalled();
    fillKnown();
    vi.mocked(mediaOperationsApi.createRevenueCostRecord).mockRejectedValue(new MediaOperationsApiError("forbidden", { status: 403 }));
    submit();
    await screen.findByText(/費用を保存する権限がありません/);
    expect(screen.getByRole("button", { name: "費用記録を保存" })).toBeDisabled();
    submit();
    expect(mediaOperationsApi.createRevenueCostRecord).toHaveBeenCalledTimes(1);
  });

  it("allows signed refund costs while keeping human minutes nonnegative", async () => {
    const refunded = record({ fee_amount: -10, production_cost_amount: -20, human_cost_amount: -5, profit_amount: -65 });
    vi.mocked(mediaOperationsApi.createRevenueCostRecord).mockResolvedValue(refunded);
    render(<RevenueCostEditor event={{ ...event, event_type: "refund", gross_amount: -100, net_amount: -90 }} onClose={vi.fn()} onReadback={vi.fn()} />);
    await screen.findByRole("form");
    fillKnown();
    for (const [label, value] of [["手数料", "-10"], ["制作原価", "-20"], ["人的費用", "-5"], ["作業時間（分）", "-1"]]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
    }
    submit();
    await screen.findByText("作業時間（分）は0以上の有限の数値で入力してください");
    expect(mediaOperationsApi.createRevenueCostRecord).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("作業時間（分）"), { target: { value: "30" } });
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValue(history(refunded));
    submit();
    await screen.findByText(/保存後の再読込を確認しました/);
    expect(mediaOperationsApi.createRevenueCostRecord).toHaveBeenCalledWith("event-1", expect.objectContaining({ fee_amount: -10, production_cost_amount: -20, human_cost_amount: -5, human_minutes: 30 }), expect.any(String));
  });

  it("does not expose a writable form until an initial history read succeeds", async () => {
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockRejectedValueOnce(new Error("history unavailable")).mockResolvedValueOnce(empty);
    render(<RevenueCostEditor event={event} onClose={vi.fn()} onReadback={vi.fn()} />);
    await screen.findByText("history unavailable");
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "最新の履歴を再読込" }));
    await screen.findByRole("form");
    expect(screen.getByRole("button", { name: "費用記録を保存" })).toBeEnabled();
  });

  it("keeps a statement allocated from an independent cost entry read-only", async () => {
    vi.mocked(mediaOperationsApi.getRevenueCostRecords).mockResolvedValue(history(record({ cost_entry_id: "entry-revision-1" })));
    await renderEditor();
    expect(screen.getByText(/独立費用から配賦済みです/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "訂正版を追加保存" })).toBeDisabled();
    submit();
    expect(mediaOperationsApi.createRevenueCostRecord).not.toHaveBeenCalled();
  });
});

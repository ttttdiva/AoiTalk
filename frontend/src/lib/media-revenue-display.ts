const authorityLabels: Record<string, string> = {
  provider_settled: "API記録・入金確定", provider_estimate: "API記録・推計",
  imported_settled: "取込記録・入金済み", imported_estimate: "取込記録・推計",
  provider_unknown: "API記録・入金状態不明", imported_unknown: "取込記録・入金状態不明", manual_unknown: "自己申告・入金状態不明",
  unknown_settled: "出所不明・入金済み記録", unknown_estimate: "出所不明・推計", unknown_unknown: "出所・入金状態不明",
  manual_settled: "入金済み（自己申告）", manual_estimate: "推計（自己申告）",
};
const eventLabels: Record<string, string> = { sale: "売上", refund: "返金", chargeback: "チャージバック", adjustment: "調整", reversal: "取消" };

export function revenueEvidenceClass(rawAuthority: unknown, rawType: unknown) {
  const authority = typeof rawAuthority === "string" && Object.prototype.hasOwnProperty.call(authorityLabels, rawAuthority) ? rawAuthority : "unknown";
  const eventType = typeof rawType === "string" && Object.prototype.hasOwnProperty.call(eventLabels, rawType) ? rawType : "unknown";
  return { authority, eventType, authorityLabel: authorityLabels[authority] ?? "出所・入金状態不明", eventLabel: eventLabels[eventType] ?? "種別不明" };
}

export function revenueCurrency(value: unknown): string | null {
  const currency = typeof value === "string" ? value.trim().toUpperCase() : "";
  return /^[A-Z]{3}$/u.test(currency) ? currency : null;
}

export function revenueAmountText(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= 1_000_000_000_000
    ? new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 2 }).format(value) : "不明";
}

export const UNTYPED_MONETARY_METRICS = new Set(["gross_revenue", "net_revenue", "refunds", "cost"]);

const costAuthorityLabels: Record<string, string> = {
  manual_estimate: "費用推計（自己申告）",
  manual_settled: "費用確定（自己申告）",
  imported_estimate: "費用取込・推計",
  imported_settled: "費用取込・確定",
};

export function revenueCostClass(value: unknown) {
  const authority = typeof value === "string" && Object.prototype.hasOwnProperty.call(costAuthorityLabels, value)
    ? value : "unknown";
  return { authority, label: costAuthorityLabels[authority] ?? "費用不明" };
}

export function revenueProfitAmount(value: unknown, status: unknown, currency: unknown): number | null {
  return status === "calculated_input_scope" && revenueCurrency(currency) &&
    typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= 1_000_000_000_000
    ? value : null;
}

export function revenueProfitStatus(status: unknown): string {
  const labels: Record<string, string> = {
    calculated_input_scope: "費用3区分の入力範囲で算出",
    unavailable_cost_record: "費用記録なし",
    unavailable_cost_scope: "売上への費用帰属を確認できません",
    unavailable_cost_authority: "費用の出所・確定区分を確認できません",
    unavailable_cost_coverage: "費用3区分の入力範囲が未確認",
    unavailable_cost_inputs: "費用に未入力あり",
    unavailable_basis_mismatch: "売上と費用の推計・確定区分が不一致",
    unavailable_fee_reconciliation: "総額・手取り・手数料が不一致",
    unavailable_invalid_amount: "金額を検証できません",
  };
  return typeof status === "string" && Object.prototype.hasOwnProperty.call(labels, status)
    ? labels[status] : "算出条件を確認できません";
}

export const REVENUE_COST_SCOPE_NOTE = "利益は手取り − 制作原価 − 人的費用で計算します。手数料は手取りから控除済みのため再控除しません。税・その他の費用まで網羅した純利益ではありません。未入力・未確認の費用がある場合、利益は算出できません。作業時間を無料や0円に換算しません。";

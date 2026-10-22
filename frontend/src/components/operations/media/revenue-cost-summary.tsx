import type { RevenueCostProjection } from "@/lib/media-operations-api";
import {
  revenueAmountText,
  revenueCostClass,
  revenueCurrency,
  revenueProfitAmount,
  revenueProfitStatus,
} from "@/lib/media-revenue-display";

export function RevenueCostSummary({
  item,
  currency,
  revenueAuthority,
}: {
  item: RevenueCostProjection;
  currency: unknown;
  revenueAuthority?: unknown;
}) {
  const cost = revenueCostClass(item.cost_authority);
  const knownCurrency = revenueCurrency(currency);
  const profit = revenueProfitAmount(item.profit_amount, item.profit_status, currency);
  const displayStatus = item.profit_status === "calculated_input_scope" && profit === null
    ? "unavailable_invalid_amount" : item.profit_status ?? "unavailable_cost_record";
  const selfReported = cost.authority.startsWith("manual_") ||
    (typeof revenueAuthority === "string" && revenueAuthority.startsWith("manual_"));
  return (
    <div className="space-y-1 text-xs tabular-nums" data-testid="revenue-cost-summary">
      <p>{cost.label}</p>
      <p>手数料 {revenueAmountText(knownCurrency ? item.fee_amount : null)} · 制作原価 {revenueAmountText(knownCurrency ? item.production_cost_amount : null)} · 人的費用 {revenueAmountText(knownCurrency ? item.human_cost_amount : null)}</p>
      <p>作業時間 {revenueAmountText(item.human_minutes)} 分</p>
      <p>入力範囲の利益{selfReported ? "（自己申告）" : ""}: {revenueAmountText(profit)}{profit !== null ? ` ${knownCurrency}` : ""}</p>
      <p className="text-muted-foreground">{revenueProfitStatus(displayStatus)}</p>
    </div>
  );
}

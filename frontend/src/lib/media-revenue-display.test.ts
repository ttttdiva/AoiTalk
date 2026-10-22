import { describe, expect, it } from "vitest";
import { revenueAmountText, revenueCostClass, revenueProfitAmount, revenueProfitStatus } from "./media-revenue-display";

describe("revenue cost display", () => {
  it("keeps explicit zero distinct from unknown and refuses unverified profit", () => {
    expect(revenueAmountText(0)).toBe("0");
    for (const value of [null, undefined, "0", NaN, Infinity]) expect(revenueAmountText(value)).toBe("不明");
    expect(revenueProfitAmount(0, "calculated_input_scope", "JPY")).toBe(0);
    expect(revenueProfitAmount(-5, "calculated_input_scope", "USD")).toBe(-5);
    expect(revenueProfitAmount(123, "unavailable_cost_coverage", "USD")).toBeNull();
    expect(revenueProfitAmount(123, "calculated_input_scope", null)).toBeNull();
    expect(revenueProfitAmount(NaN, "calculated_input_scope", "USD")).toBeNull();
    expect(revenueProfitAmount(Infinity, "calculated_input_scope", "USD")).toBeNull();
  });

  it("keeps manual/imported cost authority distinct and does not trust arbitrary class names", () => {
    expect(revenueCostClass("manual_settled").label).toContain("自己申告");
    expect(revenueCostClass("imported_settled").label).toBe("費用取込・確定");
    expect(revenueCostClass("provider_settled").authority).toBe("unknown");
    expect(revenueCostClass("__proto__").authority).toBe("unknown");
    expect(revenueProfitStatus("unavailable_fee_reconciliation")).toContain("不一致");
    expect(revenueProfitStatus("__proto__")).toBe("算出条件を確認できません");
  });
});

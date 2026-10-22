import { expect, it } from "vitest";
import { costFieldsInput, formFromCostRecord } from "./media-cost-input";

it("replays only command evidence fields, normalizes URLs and deduplicates an unchanged correction note", () => {
  const form = formFromCostRecord({ fee_amount: 0, production_cost_amount: 20, human_cost_amount: 10, human_minutes: 30, coverage_note: "制作費用を確認" });
  form.evidenceNote = "作業記録";
  const payload = costFieldsInput(form, [
    { kind: "url", ref: "https://example.com/invoice", evidence_hash: "a".repeat(64) },
    { kind: "record", ref: "persona-1", note: "作業記録", evidence_hash: "b".repeat(64) },
  ], { kind: "record", ref: "persona-1" }, true);
  expect(payload.evidence).toHaveLength(2);
  expect(payload.evidence[0]).toEqual(expect.objectContaining({ kind: "url", url: "https://example.com/invoice" }));
  expect(payload.evidence[0]).not.toHaveProperty("ref");
  for (const item of payload.evidence) expect(item).not.toHaveProperty("evidence_hash");
});

import type { RevenueCostRecordInput, RevenueCostRecord } from "./media-operations-api";

export const COST_FIELDS = [
  { key: "fee_amount", label: "手数料" },
  { key: "production_cost_amount", label: "制作原価" },
  { key: "human_cost_amount", label: "人的費用" },
  { key: "human_minutes", label: "作業時間（分）" },
] as const;
type AmountKey = (typeof COST_FIELDS)[number]["key"];
export type CostForm = {
  amounts: Record<AmountKey, string>;
  reasons: Record<AmountKey, string>;
  source: RevenueCostRecordInput["source"];
  basis: RevenueCostRecordInput["basis"];
  coverageComplete: boolean;
  coverageNote: string;
  evidenceUrl: string;
  evidenceNote: string;
};

export function formFromCostRecord(record: Partial<RevenueCostRecordInput> | null, basis: CostForm["basis"] = "estimated"): CostForm {
  return {
    amounts: Object.fromEntries(COST_FIELDS.map(({ key }) => [key, record?.[key] == null ? "" : String(record[key])])) as CostForm["amounts"],
    reasons: Object.fromEntries(COST_FIELDS.map(({ key }) => [key, record?.missing_reasons?.[key] ?? ""])) as CostForm["reasons"],
    source: record?.source ?? "manual",
    basis: record?.basis ?? basis,
    // Each appended record requires a deliberate confirmation of its scope.
    coverageComplete: false,
    coverageNote: record?.coverage_note ?? "",
    evidenceUrl: "",
    evidenceNote: "",
  };
}

export function newCostKey(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID() : `revenue-cost-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function costErrorText(error: unknown): string {
  return error instanceof Error ? error.message : "費用記録の操作に失敗しました";
}

export function costFieldsInput(form: CostForm, previousEvidence: RevenueCostRecord["evidence"], noteRef: { kind: "revenue_event" | "record"; ref: string }, allowNegative: boolean): Omit<RevenueCostRecordInput, "currency" | "correction_of_id"> {
  const amounts = {} as Record<AmountKey, number | null>;
  const missingReasons: Record<string, string> = {};
  for (const { key, label } of COST_FIELDS) {
    const raw = form.amounts[key].trim();
    if (!raw) {
      amounts[key] = null;
      const reason = form.reasons[key].trim();
      if (!reason) throw new Error(`${label}が未入力の理由を入力してください`);
      missingReasons[key] = reason;
    } else {
      const value = Number(raw);
      if (!Number.isFinite(value) || Math.abs(value) > 1_000_000_000_000 || ((key === "human_minutes" || !allowNegative) && value < 0)) {
        throw new Error(`${label}は${key === "human_minutes" || !allowNegative ? "0以上の" : ""}有限の数値で入力してください`);
      }
      amounts[key] = value;
    }
  }
  if (!form.coverageNote.trim()) throw new Error("費用の入力範囲・判断根拠を入力してください");
  const evidence: RevenueCostRecordInput["evidence"] = previousEvidence.map((item) => {
    // A response includes evidence_hash and may grow more audit fields. Only
    // command fields can be replayed into the extra-forbid request schema.
    const { kind, ref, sha256, mime_type, role, label, note, format_version, row_index } = item;
    return kind === "url"
      ? { kind, url: item.url ?? ref, label, note, sha256 }
      : { kind, ref, sha256, mime_type, role, label, note, format_version, row_index };
  });
  if (form.evidenceUrl.trim()) {
    let url: URL;
    try { url = new URL(form.evidenceUrl.trim()); } catch { throw new Error("証拠URLを確認してください"); }
    if (!["https:", "http:"].includes(url.protocol)) throw new Error("証拠URLはhttpまたはhttpsで入力してください");
    evidence.push({ kind: "url", url: url.href, note: form.evidenceNote.trim() || null });
  } else if (form.evidenceNote.trim()) {
    evidence.push({ ...noteRef, note: form.evidenceNote.trim() });
  }
  const seen = new Set<string>();
  const uniqueEvidence = evidence.filter((item) => {
    const key = JSON.stringify(Object.fromEntries(Object.entries(item).filter(([, value]) => value != null).sort(([a], [b]) => a.localeCompare(b))));
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  if (uniqueEvidence.length > 32) throw new Error("証拠は既存分と合わせて32件以内で記録してください");
  if (!uniqueEvidence.length) throw new Error("証拠URLまたは費用対象に直接帰属する証拠メモを入力してください");
  return {
    source: form.source, basis: form.basis, ...amounts,
    missing_reasons: missingReasons,
    coverage_complete: form.coverageComplete,
    coverage_note: form.coverageNote.trim(), evidence: uniqueEvidence,
  };
}

import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { COST_FIELDS, type CostForm } from "@/lib/media-cost-input";
import { HelpPopover } from "@/components/ui/help-popover";

export function RevenueCostFields({ form, setForm, allowNegative, lockSource, fixedBasis = true, scopeLabel = "この売上", basisNote = "対象売上と一致する区分で固定" }: {
  form: CostForm; setForm: (form: CostForm) => void; allowNegative: boolean; lockSource: boolean; fixedBasis?: boolean; scopeLabel?: string; basisNote?: string;
}) {
  return <>
            <div className="grid gap-3 sm:grid-cols-2">
              {COST_FIELDS.map(({ key, label }) => <div key={key} className="space-y-1">
                <label className="text-xs">{label}<Input aria-label={label} type="number" min={key === "human_minutes" || !allowNegative ? 0 : undefined} step="any" value={form.amounts[key]} onChange={(e) => setForm({ ...form, amounts: { ...form.amounts, [key]: e.target.value } })} /></label>
                {!form.amounts[key].trim() ? <label className="text-xs">{label}の未入力理由<Input aria-label={`${label}の未入力理由`} value={form.reasons[key]} maxLength={500} onChange={(e) => setForm({ ...form, reasons: { ...form.reasons, [key]: e.target.value } })} /></label> : null}
              </div>)}
            </div>
            <fieldset disabled={lockSource} className="flex flex-wrap gap-3 text-xs"><legend>費用の出所（訂正時は変更不可）</legend>
              <label><input type="radio" name="cost-source" checked={form.source === "manual"} onChange={() => setForm({ ...form, source: "manual" })} /> 手動（自己申告）</label>
              <label><input type="radio" name="cost-source" checked={form.source === "imported"} onChange={() => setForm({ ...form, source: "imported" })} /> 取込資料に基づく</label>
            </fieldset>
            {fixedBasis ? <p className="text-xs">費用の確定状態: {form.basis === "settled" ? "確定" : "推計"}（{basisNote}）</p> : <fieldset className="flex gap-3 text-xs"><legend>費用の確定状態</legend>
              <label><input type="radio" name="entry-basis" checked={form.basis === "estimated"} onChange={() => setForm({ ...form, basis: "estimated" })} /> 推計</label>
              <label><input type="radio" name="entry-basis" checked={form.basis === "settled"} onChange={() => setForm({ ...form, basis: "settled" })} /> 確定</label>
            </fieldset>}
            <label className="block text-xs">費用の入力範囲・判断根拠<Textarea aria-label="費用の入力範囲・判断根拠" value={form.coverageNote} maxLength={2000} onChange={(e) => setForm({ ...form, coverageNote: e.target.value })} /></label>
            <div className="flex items-start gap-2 text-xs">
              <label className="flex items-start gap-2"><Checkbox checked={form.coverageComplete} onCheckedChange={(checked) => setForm({ ...form, coverageComplete: checked === true })} /> {scopeLabel}に直接帰属する手数料・制作原価・人的費用の3区分を確認しました</label>
              <HelpPopover label="費用入力の確認">未確認のまま保存できます。金額が不明なら空欄と理由を残してください。0は明示的に費用なしと確認できる場合だけ入力します。返金・調整などのイベントで返還された費用は負数で記録できます。</HelpPopover>
            </div>
            <label className="block text-xs">追加の証拠URL<Input aria-label="追加の証拠URL" type="url" value={form.evidenceUrl} onChange={(e) => setForm({ ...form, evidenceUrl: e.target.value })} /></label>
            <div className="space-y-1">
              <span className="inline-flex items-center gap-1 text-xs">
                追加の証拠メモ
                <HelpPopover label="追加の証拠メモ">証拠URLまたは費用対象への帰属根拠を残します。訂正時は既存の証拠を保持します。</HelpPopover>
              </span>
              <Textarea aria-label="追加の証拠メモ" value={form.evidenceNote} maxLength={2000} onChange={(e) => setForm({ ...form, evidenceNote: e.target.value })} />
            </div>
  </>;
}

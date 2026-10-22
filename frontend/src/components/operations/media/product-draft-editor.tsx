"use client";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { ProductDraft, ProductPlatform } from "@/lib/media-product-draft";

export function ProductDraftEditor({ platform, value, onChange, idPrefix }: {
  platform: ProductPlatform; value: ProductDraft; onChange: (value: ProductDraft) => void; idPrefix: string;
}) {
  const field = (key: keyof ProductDraft, label: string, multiline = false) => {
    const props = { id: `${idPrefix}-${key}`, value: String(value[key]), onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onChange({ ...value, [key]: e.target.value }) };
    return <div className="space-y-1.5"><label className="text-xs font-medium" htmlFor={props.id}>{label}</label>{multiline ? <Textarea {...props} /> : <Input {...props} />}</div>;
  };
  const select = <K extends keyof ProductDraft,>(key: K, label: string, options: [string, string][]) => <div className="space-y-1.5"><label className="text-xs font-medium" htmlFor={`${idPrefix}-${key}`}>{label}</label><AppSelect id={`${idPrefix}-${key}`} value={String(value[key])} onValueChange={v => onChange({ ...value, [key]: v })}>{options.map(([v, text]) => <option key={v} value={v}>{text}</option>)}</AppSelect></div>;
  return <div className="space-y-3" aria-label={`${platform === "fanza" ? "FANZA" : "DLsite"}商品下書き`}>
    <p className="inline-flex items-center gap-1 text-xs text-muted-foreground">商品下書き<HelpPopover label="商品下書き">商品下書きです。保存する商品準備状態と、外部の審査・販売状態は別に記録します。</HelpPopover></p>
    {platform === "fanza" && <div className="grid gap-3 sm:grid-cols-2">
      {select("storefront", "FANZA売場", [["doujin", "同人"], ["commercial", "商業"]])}
      {select("ai_usage", "AI利用区分", [["unknown", "未確認"], ["none", "AI利用なし"], ["partial", "一部AI利用"], ["generated", "AI生成"]])}
      {field("seller_ref", "販売者参照")}
      {select("submission_state", "商品準備状態", [["draft", "下書き"], ["ready_for_review", "審査準備"]])}
    </div>}
    {field("title", "商品タイトル")}{field("description", "商品説明", true)}
    <div className="grid gap-3 sm:grid-cols-2">{field("category", "カテゴリ")}{field("age_rating", "年齢区分")}</div>
    <div className="grid gap-3 sm:grid-cols-3">{field("price", "価格")}{field("currency", "通貨")}{field("distribution", "配布方式")}</div>
    <label className="flex items-center gap-2 text-xs"><Checkbox checked={value.tax_included} onCheckedChange={v => onChange({ ...value, tax_included: v === true })} />税込価格</label>
    {field("preview_assets", "Preview Asset ID（1行1件）", true)}{field("thumbnail_assets", "Thumbnail Asset ID（1行1件）", true)}
    {field("deliverable_package_ref", "配布パッケージ参照（artifact:ID）")}
    <fieldset className="space-y-2 rounded-md border p-3"><legend className="text-xs font-medium">Rights checklist</legend>
      {value.rights_checklist.map((item, index) => <div key={index} className="grid gap-2 sm:grid-cols-[1fr_10rem_1fr_auto]">
        <Input aria-label={`権利項目 ${index + 1}`} value={item.code} onChange={e => onChange({ ...value, rights_checklist: value.rights_checklist.map((v, i) => i === index ? { ...v, code: e.target.value } : v) })} />
        <AppSelect aria-label={`権利状態 ${index + 1}`} value={item.status} onValueChange={v => onChange({ ...value, rights_checklist: value.rights_checklist.map((c, i) => i === index ? { ...c, status: v as typeof item.status } : c) })}><option value="not_run">未確認</option><option value="review_required">レビュー要</option><option value="failed">不可</option><option value="passed">確認済み</option></AppSelect>
        <Input aria-label={`権利注記 ${index + 1}`} value={item.note ?? ""} onChange={e => onChange({ ...value, rights_checklist: value.rights_checklist.map((v, i) => i === index ? { ...v, note: e.target.value || null } : v) })} />
        <Button type="button" size="sm" variant="outline" aria-label={`権利項目 ${index + 1}を削除`} onClick={() => onChange({ ...value, rights_checklist: value.rights_checklist.filter((_, i) => i !== index) })}>削除</Button>
      </div>)}
      <Button type="button" size="sm" variant="outline" onClick={() => onChange({ ...value, rights_checklist: [...value.rights_checklist, { code: "", status: "not_run", note: null }] })}>権利項目を追加</Button>
    </fieldset>
  </div>;
}

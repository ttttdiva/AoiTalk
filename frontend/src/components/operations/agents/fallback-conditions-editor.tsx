"use client";

import { Checkbox } from "@/components/ui/checkbox";
import { HelpPopover } from "@/components/ui/help-popover";

export const FALLBACK_CONDITION_CODES = ["model_unavailable", "provider_unavailable", "rate_limited", "connect_timeout"] as const;
export type FallbackConditionCode = typeof FALLBACK_CONDITION_CODES[number];
const LABELS: Record<FallbackConditionCode, string> = {
  model_unavailable: "モデルが利用できない", provider_unavailable: "プロバイダーが利用できない",
  rate_limited: "レート制限に達した", connect_timeout: "接続がタイムアウトした",
};

// Adapted from the user-provided Muse FallbackConditionsEditor; fully controlled.
export function FallbackConditionsEditor({ value, onChange, disabled = false, idPrefix }: {
  value: readonly FallbackConditionCode[] | null | undefined;
  onChange: (next: FallbackConditionCode[] | null) => void;
  disabled?: boolean; idPrefix: string;
}) {
  const isDefault = value == null;
  const selected = new Set(isDefault ? FALLBACK_CONDITION_CODES : value);
  return <fieldset disabled={disabled} className="space-y-2 rounded-md border p-3">
    <legend className="px-1 font-medium">フォールバック条件 <HelpPopover label="フォールバック条件"><p>既定は4条件、すべて外すと無効です。この設定は保存されますが、実行時への接続はまだ完了していません。</p></HelpPopover></legend>
    <div className="flex items-center gap-2"><Checkbox id={`${idPrefix}-default`} disabled={disabled} checked={isDefault} onCheckedChange={checked => onChange(checked ? null : [...FALLBACK_CONDITION_CODES])} /><label htmlFor={`${idPrefix}-default`}>既定の技術エラー条件を使う</label></div>
    <div className="space-y-1 pl-6">{FALLBACK_CONDITION_CODES.map(code => <div key={code} className="flex items-center gap-2"><Checkbox id={`${idPrefix}-${code}`} disabled={disabled || isDefault} checked={selected.has(code)} onCheckedChange={checked => { const next = new Set(isDefault ? [] : value); if (checked) next.add(code); else next.delete(code); onChange(FALLBACK_CONDITION_CODES.filter(item => next.has(item))); }} /><label htmlFor={`${idPrefix}-${code}`}>{LABELS[code]}</label></div>)}</div>
  </fieldset>;
}

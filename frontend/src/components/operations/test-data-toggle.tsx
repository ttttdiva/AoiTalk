"use client";

import { Checkbox } from "@/components/ui/checkbox";

/** Shown only while QA / acceptance fixtures exist in the list it controls. */
export function TestDataToggle({ hiddenCount, checked, onChange }: { hiddenCount: number; checked: boolean; onChange: (checked: boolean) => void }) {
  if (!checked && hiddenCount === 0) return null;
  return (
    <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <Checkbox checked={checked} onCheckedChange={(value) => onChange(value === true)} />
      テスト用を表示{hiddenCount > 0 ? `（${hiddenCount}件）` : ""}
    </label>
  );
}

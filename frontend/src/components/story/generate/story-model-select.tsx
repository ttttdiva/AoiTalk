"use client";

import { AppSelect } from "@/components/ui/app-select";
import { useResponseModelOptions } from "@/hooks/use-response-model-options";

/** §8.8 層②/層③ の執筆モデル指定。空文字は「設定に従う」＝上位層へ委譲する。 */
export function StoryModelSelect({
  value,
  onChange,
  className = "mt-1 w-full",
}: {
  value: string;
  onChange: (value: string) => void;
  className?: string;
}) {
  const { responseModelOptions, responseModelOptionsLoading } = useResponseModelOptions();
  const hasValue = value === "" || responseModelOptions.some((option) => `${option.provider}::${option.model}` === value);
  return (
    <AppSelect
      className={className}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      aria-label="執筆モデル"
      placeholder="設定に従う"
    >
      <option value="">設定に従う</option>
      {!hasValue && value ? <option value={value}>{value.replace("::", " / ")}</option> : null}
      {responseModelOptions.map((option) => (
        <option key={`${option.provider}::${option.model}`} value={`${option.provider}::${option.model}`}>
          {option.label}
        </option>
      ))}
      {responseModelOptionsLoading ? <option disabled>モデル一覧を読み込み中…</option> : null}
    </AppSelect>
  );
}

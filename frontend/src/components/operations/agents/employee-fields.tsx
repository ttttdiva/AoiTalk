"use client";

import { Children, cloneElement, useId, useState, type ReactElement, type ReactNode, type FormEvent } from "react";
import { EmployeeApiError } from "@/lib/agent-employees-api";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Checkbox } from "@/components/ui/checkbox";

export function EmployeeNotice({ error, reload }: { error: unknown; reload?: () => void }) {
  if (!error) return null;
  return <div role="alert" className="rounded-lg border border-destructive/30 p-3 text-sm text-destructive">
    {error instanceof EmployeeApiError ? error.message : "処理を完了できませんでした。再読み込みして確認してください。"}
    {reload && <Button type="button" variant="outline" size="sm" onClick={reload}>再読み込み</Button>}
  </div>;
}
export function EmployeeField({ label, children }: { label: string; children: ReactNode }) {
  const generatedId = useId();
  const control = Children.only(children) as ReactElement<{ id?: string }>;
  const controlId = control.props.id ?? generatedId;
  return <div className="flex min-w-0 flex-col gap-1.5 text-sm"><label className="font-medium" htmlFor={controlId}>{label}</label>{cloneElement(control, { id: controlId })}</div>;
}
export const employeeInputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm disabled:opacity-50";
export function EmployeeSelect({ label, value, onChange, options, required = false, disabled = false }: {
  label: string; value: string; onChange: (value: string) => void; options: { value: string; label: string; disabled?: boolean }[]; required?: boolean; disabled?: boolean;
}) {
  return <EmployeeField label={label}><AppSelect aria-label={label} className={employeeInputClass} value={value} onChange={e => onChange(e.target.value)} required={required} disabled={disabled}>
    <option value="">選択してください</option>{options.map(o => <option key={o.value} value={o.value} disabled={o.disabled}>{o.label}</option>)}
  </AppSelect></EmployeeField>;
}
export function EmployeeCheck({ label, checked, onChange, disabled, required }: { label: string; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean; required?: boolean }) {
  return <label className="flex items-center gap-2 text-sm"><Checkbox checked={checked} disabled={disabled} required={required} onCheckedChange={value => onChange(value === true)} />{label}</label>;
}
export function EmployeeForm({ children, onSave, label = "保存", disabled = false, reload, onSuccess }: {
  children: ReactNode; onSave: () => Promise<unknown>; label?: string; disabled?: boolean; reload?: () => void; onSuccess?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault(); if (busy || disabled) return;
    setBusy(true); setError(null); setSaved(false);
    try { await onSave(); setSaved(true); onSuccess?.(); } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <form onSubmit={submit} className="space-y-4"><fieldset disabled={disabled || busy} className="space-y-4">{children}
    <Button type="submit">{busy ? "保存中…" : label}</Button></fieldset><EmployeeNotice error={error} reload={reload} />{saved && <p role="status" className="text-sm">保存しました。</p>}</form>;
}
export function EmployeeStatus({ state }: { state: string }) {
  const labels: Record<string, string> = { draft: "下書き", active: "有効", paused: "一時停止", retired: "退職", revoked: "解除済み", expired: "期限切れ", uncertain: "要確認・再実行禁止", unavailable: "利用不可", unverified: "未検証", human_approval: "毎回人間承認", bounded_auto: "範囲内自動実行" };
  return <span className="inline-flex rounded-full border px-2 py-0.5 text-xs" data-employee-status={state}>{labels[state] ?? state}</span>;
}

"use client";

import { useState } from "react";
import type { components } from "@/lib/api-types.gen";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { pyFetch } from "./llm-model-section-types";

type Context = components["schemas"]["OpenAIConfirmationResponse"];
type SaveRequest = components["schemas"]["OpenAIConfirmationRequest"];

export function OpenAIConfirmationPanel({ onChanged }: { onChanged: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [context, setContext] = useState<Context | null>(null);
  const [reference, setReference] = useState<SaveRequest["credential_reference"]>("openai_api_key");
  const [scope, setScope] = useState("");
  const [tier, setTier] = useState<SaveRequest["usage_tier"]>("tier_3_plus");
  const [source, setSource] = useState("本人が現在の資格情報と組織の無料枠・参加状態を確認済み");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const credential = context?.credentials.find((item) => item.reference === reference);

  async function load() {
    setBusy(true); setMessage("");
    try {
      const data = await pyFetch<Context>("/free-team/openai-confirmation");
      setContext(data); setScope(data.billing_scope_id);
      const savedReference = data.record.credential_reference;
      const selected = data.credentials.find((item) => item.reference === savedReference)
        ?? data.credentials.find((item) => item.configured);
      if (selected) setReference(selected.reference as SaveRequest["credential_reference"]);
      setTier(data.usage_tier === "tier_1_2" ? "tier_1_2" : "tier_3_plus");
      if (data.record.source_reference) setSource(data.record.source_reference);
    } catch (error) {
      setContext(null); setMessage(error instanceof Error ? error.message : "確認記録を取得できませんでした");
    } finally { setBusy(false); }
  }

  async function write(revoke = false) {
    if (!credential || !context) return;
    setBusy(true); setMessage("");
    const body: SaveRequest = { credential_reference: reference, fingerprint: credential.fingerprint,
      expected_record_id: context.record.record_id ?? null, billing_scope_id: scope.trim(), usage_tier: tier,
      eligible: true, enrolled: true, source_reference: source.trim() };
    try {
      const data = await pyFetch<Context>("/free-team/openai-confirmation", {
        method: revoke ? "DELETE" : "PUT",
        body: JSON.stringify(revoke ? { expected_record_id: context.record.record_id } : body),
      });
      setContext(data); setMessage(revoke ? "確認記録を失効させました" : "本人申告の確認記録を保存しました");
      await onChanged();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "確認記録を保存できませんでした");
    } finally { setBusy(false); }
  }

  return <section className="space-y-2 rounded border p-2 text-xs" data-testid="openai-confirmation-panel">
    <Button size="sm" variant="outline" aria-expanded={open} onClick={() => {
      setOpen(!open); if (!open) void load();
    }}>確認済み無料枠の記録</Button>
    {open && <div className="space-y-3">
      <p>現在のキーで確認済みの対象資格・参加状態を、本人申告として記録します。OpenAI側の登録・共有設定や資格情報は変更しません。</p>
      <p className="text-muted-foreground">Providerの自動attestationではありません。組織外部の同時消費はこの記録では把握できません。</p>
      {context && <>
        <div className="grid gap-3 sm:grid-cols-2">
          <label>既存資格情報<AppSelect value={reference} onChange={(event) => setReference(event.target.value as SaveRequest["credential_reference"])}>
            {context.credentials.map((item) => <option key={item.reference} value={item.reference} disabled={!item.configured}>
              {item.reference === "openai_api_key" ? "現在のOpenAIキー" : "既存無料Teamキー"}{!item.configured ? "（未設定）" : ""}
            </option>)}
          </AppSelect></label>
          <label>確認済み請求組織scope<Input value={scope} maxLength={200} onChange={(event) => setScope(event.target.value)} placeholder="org-…" /></label>
          <label>確認済み無料枠<AppSelect value={tier} onChange={(event) => setTier(event.target.value as SaveRequest["usage_tier"])}>
            <option value="tier_3_plus">Sol 1M / Luna 10M token/日</option>
            <option value="tier_1_2">Sol 250K / Luna 2.5M token/日</option>
          </AppSelect></label>
          <label>本人申告の出典メモ<Input value={source} maxLength={500} onChange={(event) => setSource(event.target.value)} /></label>
        </div>
        <p className="break-all text-muted-foreground">Fingerprint (SHA-256): {credential?.fingerprint || "資格情報未設定"}<br />Project: {credential?.project_id || "SDK指定なし"} / 接続先: {credential?.base_url}</p>
        <div role="status" className="space-y-1">
          <p>記録状態: {context.record.revoked_at ? "失効済み" : context.stop_reason ? `停止（${context.stop_reason}）` : "確認済み（本人申告）"}</p>
          {context.record.record_id && <p>操作actor: {context.record.actor_user_id}<br />記録時刻: {context.record.recorded_at}<br />出典: {context.record.source_kind} / {context.record.source_reference}</p>}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={busy || !credential?.configured || !scope.trim() || !source.trim()} onClick={() => void write()}>確認済みの無料枠を記録</Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void load()}>記録を再読込</Button>
          {context.record.record_id && !context.record.revoked_at && <Button size="sm" variant="outline" disabled={busy} onClick={() => void write(true)}>記録を失効</Button>}
        </div>
      </>}
      {busy && <p role="status">確認記録を処理中...</p>}
      {message && <p role="status">{message}</p>}
    </div>}
  </section>;
}

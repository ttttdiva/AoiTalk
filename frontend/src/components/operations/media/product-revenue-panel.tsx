"use client";
import { useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import type { ContentVariantRevision } from "@/lib/media-operations-content-api";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { productApi, productRevenueRows } from "@/lib/media-product-api";
import { revenueAmountText, revenueCurrency, revenueEvidenceClass } from "@/lib/media-revenue-display";

export function ProductRevenuePanel({ scope, revision, account }: { scope: PersonaWorkspaceScope; revision: ContentVariantRevision; account: PlatformAccount }) {
  const [offset, setOffset] = useState(0);
  const ledger = useSWR(["product-revenue", scope.ownerUserId, scope.projectId, scope.personaId, revision.id, account.id, offset], () => productApi.revenue(scope.projectId, offset), { shouldRetryOnError: false, revalidateOnFocus: false });
  const rows = productRevenueRows(ledger.data ?? [], scope, revision, account);
  return <section className="space-y-2 rounded-lg border p-3" aria-label="商品の売上記録">
    <h3 className="text-sm font-semibold">この制作brief・販売接続先の売上記録</h3>
    <p className="text-xs text-muted-foreground">{revision.platform} · {account.account_ref}。既存台帳の出典と通貨を表示します。記録がない場合は未取得で、0円とは扱いません。訂正・返金は関連イベントとして表示します。</p>
    <Button type="button" size="sm" variant="outline" onClick={() => void ledger.mutate()}>売上記録を更新</Button>
    {ledger.isLoading && <p role="status" className="text-xs">売上記録を確認中…</p>}
    {ledger.error && <p role="alert" className="text-xs">売上記録を取得できません。更新して再確認してください。</p>}
    {!ledger.isLoading && !ledger.error && !rows.length && <p className="text-xs">このページに該当する売上記録は未取得です。</p>}
    {rows.map(row => {
      const evidence = revenueEvidenceClass(row.revenue_authority, row.event_type);
      const currency = revenueCurrency(row.currency);
      return <div key={String(row.id)} className="space-y-1 rounded-md border p-2 text-xs" data-testid="product-revenue-event">
        <p>{currency ?? "通貨不明"} · {evidence.eventLabel} · {evidence.authorityLabel}</p>
        <p>総額 {revenueAmountText(currency ? row.gross_amount : null)} · 手取り {revenueAmountText(currency ? row.net_amount : null)}</p>
        <p className="break-all">商品参照 {String(row.product_ref ?? "未設定")} · 出典 {String(row.source ?? "未確認")} · {String(row.event_at ?? "日時不明")}</p>
        {!!row.correction_of_id && <p className="break-all">訂正・取消対象 {String(row.correction_of_id)}</p>}
        <details><summary>出典・証拠</summary><pre className="whitespace-pre-wrap break-all">{JSON.stringify(row.evidence ?? [], null, 2)}</pre></details>
      </div>;
    })}
    <div className="flex gap-2"><Button type="button" size="sm" variant="outline" disabled={offset === 0 || ledger.isLoading} onClick={() => setOffset(v => Math.max(0, v - 100))}>前の台帳ページ</Button><Button type="button" size="sm" variant="outline" disabled={ledger.isLoading || (ledger.data?.length ?? 0) < 100} onClick={() => setOffset(v => v + 100)}>次の台帳ページ</Button></div>
  </section>;
}

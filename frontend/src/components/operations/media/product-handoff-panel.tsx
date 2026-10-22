"use client";
import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { withBasePath } from "@/lib/base-path";
import { mediaContentApi, type ContentVariantRevision } from "@/lib/media-operations-content-api";
import { mediaOperationsSetupApi, type PlatformAccount } from "@/lib/media-operations-setup-api";
import { accountInScope, type PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { personaPublicationApi, type PublicationAction } from "@/lib/media-persona-publication-api";
import { productActionMatches, productApi } from "@/lib/media-product-api";
import { ActionPackagePanel } from "../action-package-panel";
import { ActionReceiptPanel } from "../action-receipt-panel";
import { ProductRevenuePanel } from "./product-revenue-panel";
import { ProductDeliverablePanel } from "./product-deliverable-panel";

export function ProductHandoffPanel({ scope, revision, account }: { scope: PersonaWorkspaceScope; revision: ContentVariantRevision; account: PlatformAccount }) {
  const [action, setAction] = useState<PublicationAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const history = useSWR(["product-actions", scope.ownerUserId, scope.projectId, revision.id, account.id], () => productApi.listActions(scope.projectId, revision.platform), { shouldRetryOnError: false, revalidateOnFocus: false });
  const records = (history.data ?? []).filter(a => a.owner_user_id === scope.ownerUserId && a.project_id === scope.projectId && a.action_type === "media.release_product" && a.platform === revision.platform && a.content_variant_revision_id === revision.id && a.platform_account_id === account.id && a.execution_mode === "manual");
  const run = async (work: () => Promise<void>) => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError("");
    try { await work(); } catch (e) { if (active.current) setError(e instanceof Error ? e.message : "商品準備の保存状態を確認できません。"); }
    finally { pending.current = false; if (active.current) setBusy(false); }
  };
  const load = async (id: string) => {
    const saved = await personaPublicationApi.get(id);
    if (saved.id !== id || !productActionMatches(saved, scope, revision, account)) throw new Error("商品Actionの人格・Project・接続先・内容版が一致しません。");
    if (active.current) setAction(saved);
  };
  const prepare = async () => {
    const freshAccount = await mediaOperationsSetupApi.getPlatformAccount(account.id);
    if (!accountInScope(freshAccount, scope) || freshAccount.current_revision.id !== revision.platform_account_revision_id || freshAccount.current_revision.content_hash !== revision.platform_account_revision_hash || freshAccount.connection_id !== account.connection_id || !freshAccount.connection_id) throw new Error("販売接続先の保存版・Connectionを確認してください。");
    const ready = await mediaContentApi.getReadiness(revision.content_variant_id);
    if (!ready.ready || ready.revision_id !== revision.id || ready.revision_hash !== revision.content_hash) throw new Error("商品版・QA・権利確認が変わりました。保存版を再読込してください。");
    if (!active.current) return;
    const key = `product-release-v1:${revision.id}:${revision.content_hash}`;
    const saved = await productApi.prepare({ action_type: "media.release_product", execution_mode: "manual", platform: revision.platform,
      content_item_id: revision.content_item_id, content_variant_id: revision.content_variant_id, content_variant_revision_id: revision.id,
      persona_revision_id: revision.persona_revision_id, platform_account_id: account.id, platform_account_revision_id: revision.platform_account_revision_id,
      connection_id: account.connection_id!, project_id: scope.projectId, payload: revision.payload,
      content_item_hash: revision.content_item_hash, content_variant_revision_hash: revision.content_hash, content_variant_revision_version: revision.version,
      persona_revision_hash: revision.persona_revision_hash, platform_account_revision_hash: revision.platform_account_revision_hash,
      idempotency_key: key, execution_key: key });
    if (saved.execution_key !== key || !productActionMatches(saved, scope, revision, account)) throw new Error("保存された商品Actionを照合できません。保存記録を更新してください。");
    if (active.current) { setAction(saved); await history.mutate(); }
  };
  return <section className="space-y-3 rounded-xl border p-3" aria-label="商品素材・手動結果への引継ぎ">
    <h3 className="inline-flex items-center gap-1 text-sm font-semibold">
      商品素材と手動Action
      <HelpPopover label="商品素材と手動Action">下書き・審査準備を保存済みです。素材取得とAction作成は外部の審査申請・販売開始を実行しません。</HelpPopover>
    </h3>
    {!account.connection_id && <p role="alert" className="text-xs">この販売アカウントのManual Connectionを設定してください。</p>}
    {history.error && <p role="alert" className="text-xs">保存済み商品Actionを取得できません。更新して照合してください。</p>}
    {error && <p role="alert" className="text-xs">{error}</p>}
    <div className="flex flex-wrap gap-2"><Button type="button" disabled={busy || history.isLoading || !account.connection_id} onClick={() => void run(prepare)}>{busy ? "素材・保存状態を確認中…" : "商品素材を固定してManual Actionを準備"}</Button><Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void history.mutate()}>保存記録を更新</Button></div>
    {records.map(a => <Button type="button" key={a.id} className="h-auto min-w-0 max-w-full whitespace-normal break-all text-left" size="sm" variant="outline" disabled={busy} onClick={() => void run(() => load(a.id))}>{a.status} · 商品Action {a.id}</Button>)}
    {action && <>
      <p className="break-all text-xs">商品Action {action.id} · {action.status}</p>
      <ProductDeliverablePanel key={`${action.id}:${action.action_version}:${action.payload_hash}`} scope={scope} actionId={action.id} version={action.action_version ?? 1} payloadHash={action.payload_hash ?? ""} />
      <ActionPackagePanel action={{ id: action.id, action_version: action.action_version ?? undefined, payload: action.payload, payload_hash: action.payload_hash }} />
      <a className="inline-block text-sm text-primary underline" href={withBasePath(`/operations?tab=actions&action=${encodeURIComponent(action.id)}`)}>このActionの承認・Manual attempt・外部結果を開く</a>
      <p className="inline-flex items-center gap-1 text-xs">
        Manual attemptの記録
        <HelpPopover label="Manual attemptの記録">外部で操作した後に実際の受付状態と証拠をManual attemptへ記録します。succeededだけでは販売中と確定しません。商品改訂はQA・媒体別内容で新しいRevisionを保存します。</HelpPopover>
      </p>
      <ActionReceiptPanel action={{ id: action.id, receipt: action.receipt }} />
    </>}
    <ProductRevenuePanel scope={scope} revision={revision} account={account} />
  </section>;
}

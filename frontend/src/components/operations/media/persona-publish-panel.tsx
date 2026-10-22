"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { mediaContentApi } from "@/lib/media-operations-content-api";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import { publicationBlockerMessage, variantInScope, type PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { PersonaPublicationPreview } from "./persona-publication-preview";
import { PersonaPublicationControls } from "./persona-publication-controls";
import { PersonaAccountConnection } from "./persona-account-connection";
import { ProductHandoffPanel } from "./product-handoff-panel";
import { ProductPreview } from "./product-preview";
import { PersonaBrowserPreparationControls } from "./persona-browser-preparation-controls";

/** Saved content is selected explicitly; editor state never becomes a publication. */
export function PersonaPublishPanel({ scope, accounts, selectedVariantId, onSelect }: {
  scope: PersonaWorkspaceScope; accounts: PlatformAccount[];
  selectedVariantId: string; onSelect: (id: string) => void;
}) {
  const options = { shouldRetryOnError: false, revalidateOnFocus: false };
  const [route, setRoute] = useState<"provider" | "bridge">("provider");
  const list = useSWR(["persona-publish-variants", scope.personaId, scope.personaRevisionId, scope.ownerUserId, scope.projectId],
    () => mediaContentApi.listVariants({ projectId: scope.projectId }), options);
  const variants = useMemo(() => (list.data ?? []).filter(v => variantInScope(v, scope, accounts)), [list.data, scope, accounts]);
  const selected = variants.find(v => v.id === selectedVariantId);
  const detail = useSWR(selected ? ["persona-publish-detail", scope.personaId, scope.personaRevisionId, scope.ownerUserId, selected.id] : null,
    () => mediaContentApi.getVariant(selected!.id), options);
  const variant = !list.error && !detail.error && detail.data && selected && detail.data.id === selected.id && variantInScope(detail.data, scope, accounts) ? detail.data : null;
  const revision = variant?.current_revision;
  const account = accounts.find(a => a.id === revision?.platform_account_id);
  const pinned = account && revision && account.current_revision.id === revision.platform_account_revision_id &&
    account.current_revision.content_hash === revision.platform_account_revision_hash;
  const readiness = useSWR(pinned ? ["persona-publish-readiness", scope.personaId, scope.ownerUserId, revision.id, revision.content_hash] : null,
    () => mediaContentApi.getReadiness(variant!.id), options);
  const validReadiness = !readiness.error && readiness.data && variant && revision && readiness.data.content_variant_id === variant.id &&
    readiness.data.revision_id === revision.id && readiness.data.revision_hash === revision.content_hash ? readiness.data : null;
  const error = list.error ? "この人格の保存済み内容を取得できませんでした。"
    : detail.error ? "選択した内容の保存済み版を取得できませんでした。"
    : selectedVariantId && !list.isLoading && !selected ? "選択した内容の人格・Project・接続先を確認できません。保存済み内容から選択してください。"
    : detail.data && !variant ? "取得した内容の所属が選択した人格と一致しません。"
    : revision && !pinned ? "保存内容と接続先の版が一致しません。QA・媒体別内容で現在の接続先を選び、新しい版を保存してください。"
    : readiness.error ? "選択した版のQA・権利確認状態を取得できませんでした。"
    : readiness.data && !validReadiness ? "QA確認結果の版・内容hashが選択した内容と一致しません。"
    : validReadiness && !validReadiness.ready ? validReadiness.blocking_reasons.map(publicationBlockerMessage).join(" / ") || "QA・権利・素材の確認が必要です。" : "";

  return <section aria-label="preview・公開" className="min-w-0 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><div><h2 className="inline-flex items-center gap-1 text-lg font-semibold">公開前のpreview<HelpPopover label="公開前のpreview">保存済みの媒体別内容を選び、公開人格・接続先・内容・公開範囲を確認します。</HelpPopover></h2></div><Button variant="outline" size="sm" onClick={() => { void list.mutate(); void detail.mutate(); void readiness.mutate(); }}>保存内容を更新</Button></div>
    {error && <p role="alert" className="rounded-xl border border-destructive/30 p-3 text-sm text-destructive">{error}</p>}
    {list.isLoading ? <p role="status" className="text-sm">保存済み内容を確認中…</p> : !list.error && !variants.length ? <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">この人格の接続先に紐づく保存済み内容がありません。QA・媒体別内容で内容を保存してください。</p> : null}
    <div className="grid items-start gap-4 lg:grid-cols-[15rem_minmax(0,1fr)]"><div className="min-w-0 space-y-2" aria-label="公開する保存済み内容">{variants.map(v => <button key={v.id} type="button" aria-pressed={v.id === selectedVariantId} onClick={() => onSelect(v.id)} className={`block w-full min-w-0 rounded-xl border p-3 text-left ${v.id === selectedVariantId ? "border-primary bg-primary/5" : "bg-card"}`}><span className="block text-sm font-medium">{v.platform} · {v.status}</span><span className="mt-1 block break-all text-xs text-muted-foreground">内容hash {v.current_revision?.content_hash.slice(0, 12)}</span><span className="mt-1 block break-words text-xs">{accounts.find(a => a.id === v.current_revision?.platform_account_id)?.account_ref}</span></button>)}</div>
      <div className="min-w-0 space-y-3">{detail.isLoading || readiness.isLoading ? <p role="status" className="text-sm">選択した版とQAを確認中…</p> : null}
        {!selectedVariantId && variants.length > 0 && <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">previewする保存済み内容を選択してください。</p>}
        {revision && account && pinned && (["dlsite", "fanza"].includes(revision.platform) ? <ProductPreview scope={scope} revision={revision} account={account} /> : <PersonaPublicationPreview scope={scope} revision={revision} account={account} />)}
        {revision && account && pinned && ["x", "youtube"].includes(revision.platform) && <nav aria-label="本人が選ぶ公開経路" className="flex flex-wrap gap-2"><Button size="sm" variant={route === "provider" ? "default" : "outline"} aria-pressed={route === "provider"} onClick={() => setRoute("provider")}>公式APIで公開</Button><Button size="sm" variant={route === "bridge" ? "default" : "outline"} aria-pressed={route === "bridge"} onClick={() => setRoute("bridge")}>Bridgeで準備のみ</Button></nav>}
        {route === "provider" && revision && account && pinned && ["x", "youtube"].includes(revision.platform) && <PersonaAccountConnection key={`${scope.ownerUserId}:${scope.personaId}:${account.id}:${account.current_revision.id}`} scope={scope} account={account} variantId={revision.content_variant_id} />}
        {validReadiness?.ready && !error && revision && account && ["x", "youtube"].includes(revision.platform) && (route === "provider" ? <PersonaPublicationControls key={`${scope.ownerUserId}:${scope.personaId}:${revision.id}:${revision.content_hash}:${account.id}:${account.current_revision.id}`} scope={scope} revision={revision} account={account} /> : <PersonaBrowserPreparationControls key={`${scope.ownerUserId}:${scope.personaId}:${revision.id}:${revision.content_hash}:${account.id}:${account.current_revision.id}`} scope={scope} revision={revision} account={account} />)}
        {validReadiness?.ready && !error && revision && account && ["dlsite", "fanza"].includes(revision.platform) && <ProductHandoffPanel key={`${scope.ownerUserId}:${scope.personaId}:${revision.id}:${revision.content_hash}:${account.id}`} scope={scope} revision={revision} account={account} />}
        {validReadiness?.ready && !error && revision && !["x", "youtube", "dlsite", "fanza"].includes(revision.platform) && <p className="rounded-xl border p-3 text-sm text-muted-foreground">この媒体の本人公開経路は未対応です。保存済みpreviewを確認してください。</p>}
      </div>
    </div>
  </section>;
}

"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo } from "react";
import useSWR from "swr";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { mediaOperationsApi, type MediaPersona } from "@/lib/media-operations-api";
import { mediaBrandPortfolioApi, portfolioReadOptions } from "@/lib/media-brand-portfolio-api";
import { mediaOperationsSetupApi as mediaSetupApi, type PlatformAccount } from "@/lib/media-operations-setup-api";
import { accountInScope, personaScope, PERSONA_WORKSPACE_STAGES, workspaceHref, type PersonaWorkspaceStage } from "@/lib/persona-workspace-model";
import { cn } from "@/lib/utils";
import { oauthResultTitle, takeOAuthResultNotice } from "@/lib/media-oauth-result";
import { PersonaTeamPanel } from "./persona-team-panel";
import { PersonaBriefPanel } from "./persona-brief-panel";
import { MediaGenerationPanel } from "./generation-panel";
import { MediaContentVariantPanel } from "./content-variant-panel";
import { PersonaPublishPanel } from "./persona-publish-panel";
import { PersonaPortfolioPanel } from "./persona-portfolio-panel";
import { PersonaAssetPanel } from "./persona-asset-panel";
import { ContentStockPanel, useStockItems, visibleStockItems } from "./content-stock-panel";
import { AccountConnectionChip, PersonaConnectionsCard, PersonaNextActions, connectableAccounts } from "./persona-connections";

const readOptions = { shouldRetryOnError: false, revalidateOnFocus: false };

/** Persona cards before a selection: connection state and product count per persona. */
function PersonaOverviewGrid({ personas, ownerUserId }: { personas: MediaPersona[]; ownerUserId: string }) {
  const accounts = useSWR(["persona-workspace-all-accounts", ownerUserId], () => mediaSetupApi.listPlatformAccounts(), readOptions);
  const stock = useStockItems();
  return <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" aria-label="公開人格">
    {personas.map(persona => {
      const scope = personaScope(persona);
      const owned = scope ? connectableAccounts((accounts.data ?? []).filter(account => accountInScope(account, scope))) : [];
      const items = visibleStockItems(stock.data, persona.project_id ?? undefined);
      const waiting = items.filter(item => item.stock_status !== "adopted").length;
      return <li key={persona.id} className="min-w-0">
        <Link href={workspaceHref(persona.id)} className="flex h-full min-w-0 flex-col gap-2 rounded-xl border bg-card p-3 transition-colors hover:border-primary/50 hover:bg-muted/40">
          <span className="break-words text-sm font-semibold">{persona.current_revision.display_name}</span>
          <span className="flex flex-wrap gap-1">{owned.map(account => <AccountConnectionChip key={account.id} ownerUserId={ownerUserId} account={account} />)}
            {!accounts.isLoading && !accounts.error && owned.length === 0 && <span className="text-[11px] text-muted-foreground">X・YouTube 枠なし</span>}</span>
          <span className="mt-auto text-xs text-muted-foreground">{stock.data ? `制作物 ${items.length}件${waiting ? `（未採用 ${waiting}）` : ""}` : "制作物を確認中…"}</span>
        </Link>
      </li>;
    })}
  </ul>;
}

export function PersonaWorkspace() {
  const router = useRouter();
  const query = useSearchParams();
  const requestedPersona = query?.get("persona") ?? "";
  const requestedStage = query?.get("stage") ?? "brief";
  const stage: PersonaWorkspaceStage = PERSONA_WORKSPACE_STAGES.find(s => s.id === requestedStage)?.id ?? "brief";
  // `content_item_id` / `output_ref` are the removed pipeline tab's deep-link names.
  const contentItemId = query?.get("content") ?? query?.get("content_item_id") ?? "";
  const outputRef = query?.get("output") ?? query?.get("output_ref") ?? "";
  const variantId = query?.get("variant") ?? "";
  const creation = query?.get("creation") === "existing" ? "existing" : "generation";
  const options = portfolioReadOptions;
  // The portfolio endpoint resolves the authenticated owner; listPersonas can also
  // include other authorized owners, so its result must never select an owner.
  const portfolio = useSWR("sns-v6-portfolio", mediaBrandPortfolioApi.get, options);
  const owner = !portfolio.error ? portfolio.data?.owner_user_id : undefined;
  const list = useSWR(owner ? ["persona-workspace-list", owner] : null, () => mediaOperationsApi.listPersonas(), options);
  // Only the portfolio's brand personas are listed, so QA / acceptance personas never appear here.
  const personas = !portfolio.error && !list.error ? (list.data ?? []).filter(p => p.owner_user_id === owner && p.project_id && p.state !== "archived"
    && p.project_id !== portfolio.data?.headquarters_project_id
    && portfolio.data?.brands.some(brand => brand.project_id === p.project_id && brand.persona?.id === p.id)
    && !/^(?:00[_\s]*)?SNS事業本部$/u.test(p.current_revision.display_name)) : [];
  const otherOwnerPersonas = !portfolio.error && !list.error && owner ? (list.data ?? []).filter(p =>
    p.owner_user_id !== owner && p.state !== "archived" && personaScope(p)
    && p.parent_brand_ref?.startsWith("sns-v6:")
    && p.current_revision.research_policy?.source_sha256 === portfolio.data?.source.sha256) : [];
  const setupBlockedReason = otherOwnerPersonas.length && portfolio.data?.status !== "ready"
    ? "保存済みのSNS v6人格は別の所有者に登録されています。所有者のログインを確認してから構成を用意してください。"
    : portfolio.error || list.error ? "保存済み人格の所有者を確認できません。workspaceを更新してください。"
    : !list.data || list.isLoading ? "保存済み人格の所有者を確認中です。" : undefined;
  const selected = personas.find(p => p.id === requestedPersona);
  const detail = useSWR(selected ? ["persona-workspace-detail", selected.id, selected.owner_user_id, selected.project_id] : null,
    () => mediaOperationsApi.getPersona(selected!.id), readOptions);
  const canonical = detail.data?.id === selected?.id && detail.data?.owner_user_id === selected?.owner_user_id && detail.data?.project_id === selected?.project_id ? detail.data : null;
  const scope = useMemo(() => canonical ? personaScope(canonical) : null, [canonical]);
  const accounts = useSWR(scope ? ["persona-workspace-accounts", scope.personaId, scope.projectId, scope.ownerUserId] : null,
    () => mediaSetupApi.listPlatformAccounts(scope!.projectId, scope!.personaId), readOptions);
  const scopedAccounts = useMemo<PlatformAccount[]>(() => scope ? (accounts.data ?? []).filter(a => accountInScope(a, scope)) : [], [accounts.data, scope]);
  const stock = useStockItems();
  const personaStock = scope ? visibleStockItems(stock.data, scope.projectId) : [];
  const navigate = (next: PersonaWorkspaceStage, content = contentItemId) => {
    const params = new URLSearchParams({ tab: "studio", persona: requestedPersona, stage: next });
    if (content) params.set("content", content);
    if (outputRef) params.set("output", outputRef);
    if (variantId) params.set("variant", variantId);
    if (next === "generation") params.set("creation", creation);
    router.replace(`/operations?${params}`, { scroll: false });
  };
  // A content link without a persona opens the persona whose Project owns that content.
  const contentPersonaId = !requestedPersona && contentItemId
    ? personas.find(p => p.project_id && p.project_id === stock.data?.find(item => item.id === contentItemId)?.project_id)?.id
    : undefined;
  useEffect(() => {
    if (!contentPersonaId) return;
    const params = new URLSearchParams({ tab: "studio", persona: contentPersonaId, stage: query?.get("stage") === "brief" ? "brief" : "qa", content: contentItemId });
    if (outputRef) params.set("output", outputRef);
    router.replace(`/operations?${params}`, { scroll: false });
  }, [contentPersonaId, contentItemId, outputRef, query, router]);
  // The OAuth callback relay returns here and leaves its result for one toast.
  useEffect(() => {
    const notice = takeOAuthResultNotice(window.sessionStorage);
    if (!notice) return;
    const show = notice.tone === "success" ? toast.success : notice.tone === "warning" ? toast.warning : toast.error;
    show(oauthResultTitle(notice));
  }, []);
  const listReady = !portfolio.isLoading && !portfolio.error && !list.isLoading && !list.error;

  return <div className="mx-auto w-full max-w-6xl min-w-0 space-y-4" data-testid="persona-workspace">
    <header className="flex flex-wrap items-center justify-between gap-3">
      <h1 className="inline-flex items-center gap-1 text-xl font-semibold">人格<HelpPopover label="人格">公開人格を選ぶと、本人接続・次にやること・制作物をこの画面で確認できます。</HelpPopover></h1>
      <div className="flex flex-wrap items-center gap-2">
        <Link href="/operations?tab=personas" className="text-xs text-primary hover:underline">人格を管理</Link>
        <Button variant="outline" size="sm" onClick={() => { void portfolio.mutate(); void list.mutate(); void detail.mutate(); void accounts.mutate(); void stock.mutate(); }}><RefreshCw className="size-3.5" /> 更新</Button>
      </div>
    </header>
    {(portfolio.error || (portfolio.data && portfolio.data.status !== "ready")) && <PersonaPortfolioPanel onSaved={() => list.mutate()} setupBlockedReason={setupBlockedReason} />}
    {portfolio.error && <div role="alert" className="flex flex-wrap items-center gap-3 rounded-xl border p-3 text-sm"><span>制作構成を取得できません。</span><Button size="sm" variant="outline" onClick={() => void portfolio.mutate()}>制作構成を再取得</Button></div>}
    {(portfolio.isLoading || list.isLoading) && <p role="status" className="text-sm">公開人格を確認中…</p>}
    {!portfolio.error && list.error && <p role="alert" className="text-sm text-destructive">公開人格を取得できませんでした。更新してください。</p>}
    {listReady && !personas.length && (otherOwnerPersonas.length
      ? <p role="alert" className="rounded-xl border p-4 text-sm">保存済みのSNS v6人格が別の所有者に{otherOwnerPersonas.length}件あります。このログインではその制作workspaceを開けません。保存済み人格の所有者のログインで開いてください。</p>
      : <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">このログインの所有者には公開人格がまだ登録されていません。「人格を管理」から設定してください。</p>)}

    {!requestedPersona ? personas.length > 0 && owner ? <>
      <PersonaOverviewGrid personas={personas} ownerUserId={owner} />
      <p className="text-xs text-muted-foreground">公開人格を選択すると、その人格の本人接続と制作物を開きます。</p>
    </> : null : <>
      {personas.length > 0 && <nav aria-label="公開人格の選択" className="flex flex-wrap gap-1.5">
        {personas.map(persona => <Link key={persona.id} href={workspaceHref(persona.id)} aria-current={persona.id === requestedPersona ? "page" : undefined}
          className={cn("rounded-full border px-3 py-1 text-xs transition-colors", persona.id === requestedPersona ? "border-primary bg-primary/10 font-semibold text-primary" : "bg-card text-muted-foreground hover:bg-muted/50 hover:text-foreground")}>{persona.current_revision.display_name}</Link>)}
      </nav>}
      {!selected && listReady ? <div role="alert" className="rounded-xl border p-4 text-sm">指定された人格を確認できません。公開人格一覧から選択してください。</div>
        : detail.error ? <div role="alert" className="rounded-xl border border-destructive/30 p-4 text-sm text-destructive">選択した人格のProjectと設定を取得できませんでした。更新してください。</div>
        : detail.isLoading ? <p role="status" className="p-4 text-sm">選択した人格を確認中…</p>
        : detail.data && !canonical ? <div role="alert" className="rounded-xl border p-4 text-sm">取得した人格の所属が選択したProjectと一致しません。更新して確認してください。</div>
        : canonical && !scope ? <div role="alert" className="rounded-xl border p-4 text-sm">この人格のProject所属を確認できません。人格設定で公開人格ごとのProjectを確認してください。</div>
        : scope && canonical ? <div key={`${scope.personaId}:${scope.personaRevisionId}:${scope.ownerUserId}`} className="min-w-0 space-y-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2"><h2 className="min-w-0 break-words text-lg font-semibold">{scope.displayName}</h2><Link className="text-xs text-primary hover:underline" href={`/operations?tab=personas&persona=${encodeURIComponent(scope.personaId)}`}>人格を設定</Link></div>
          <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
            <PersonaConnectionsCard scope={scope} accounts={scopedAccounts} loading={accounts.isLoading} error={accounts.error} onUpdated={() => accounts.mutate()} />
            <PersonaNextActions scope={scope} accounts={accounts.error ? [] : scopedAccounts} stockItems={personaStock} />
          </div>
          <ContentStockPanel projectId={scope.projectId} personaId={scope.personaId} />
          <section aria-label="制作" className="min-w-0 space-y-3 rounded-xl border bg-card p-4">
            <nav aria-label="人格の制作工程" className="grid grid-cols-2 gap-2 lg:grid-cols-4">{PERSONA_WORKSPACE_STAGES.map((item, index) => <Button key={item.id} variant={stage === item.id ? "default" : "outline"} className="h-auto min-w-0 justify-start whitespace-normal px-3 py-2 text-left" aria-current={stage === item.id ? "step" : undefined} onClick={() => navigate(item.id)}><span><span className="block text-sm">{index + 1}. {item.label}</span><span className="mt-0.5 block text-[11px] opacity-80">{item.detail}</span></span></Button>)}</nav>
            {stage === "brief" && <PersonaBriefPanel scope={scope} selectedContentItemId={contentItemId} onSelect={id => navigate("brief", id)} onContinue={() => navigate("generation")} />}
            {stage === "generation" && <div className="min-w-0 space-y-4"><nav aria-label="制作物を用意する方法" className="flex flex-wrap gap-2">{([['generation', '生成を計画する'], ['existing', '既存画像・動画を登録する']] as const).map(([value, label]) => <Button key={value} size="sm" variant={creation === value ? "default" : "outline"} aria-pressed={creation === value} onClick={() => { const params = new URLSearchParams({ tab: "studio", persona: scope.personaId, stage: "generation", creation: value }); if (contentItemId) params.set("content", contentItemId); router.replace(`/operations?${params}`, { scroll: false }); }}>{label}</Button>)}</nav>
              {creation === "existing" ? <PersonaAssetPanel scope={scope} contentItemId={contentItemId} onBack={() => navigate("brief")} onSelected={asset => { const params = new URLSearchParams({ tab: "studio", persona: scope.personaId, stage: "qa", output: `${asset.generation_output_id} | ${asset.sha256}` }); if (contentItemId) params.set("content", contentItemId); router.replace(`/operations?${params}`, { scroll: false }); }} /> : <MediaGenerationPanel scope={scope} contentItemId={contentItemId} onOutputSelected={output => { const params = new URLSearchParams({ tab: "studio", persona: scope.personaId, stage: "qa", output: `${output.id} | ${output.sha256}` }); if (contentItemId) params.set("content", contentItemId); router.replace(`/operations?${params}`, { scroll: false }); }} />}
            </div>}
            {stage === "qa" && !accounts.error && <MediaContentVariantPanel scope={scope} accounts={scopedAccounts} initialContentItemId={contentItemId} initialOutputRef={outputRef} />}
            {stage === "publish" && !accounts.error && <PersonaPublishPanel scope={scope} accounts={scopedAccounts} selectedVariantId={variantId} onSelect={id => { const params = new URLSearchParams({ tab: "studio", persona: scope.personaId, stage: "publish", variant: id }); if (contentItemId) params.set("content", contentItemId); router.replace(`/operations?${params}`, { scroll: false }); }} />}
          </section>
          <details className="min-w-0 rounded-xl border bg-card p-4 text-sm" aria-label="人格の戦略と担当">
            <summary className="cursor-pointer font-semibold">戦略と担当 Agent</summary>
            <p className="mt-2 whitespace-pre-wrap break-words">{canonical.current_revision.summary || canonical.current_revision.positioning || "人格の戦略はまだ設定されていません。"}</p>
            <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-2"><div><dt className="text-muted-foreground">対象読者</dt><dd className="mt-1 break-words">{canonical.current_revision.audience || "未設定"}</dd></div><div><dt className="text-muted-foreground">測る成果</dt><dd className="mt-1 break-words">{canonical.current_revision.kpi_objectives.join(" / ") || "未設定"}</dd></div></dl>
            <div className="mt-3"><PersonaTeamPanel scope={scope} /></div>
          </details>
        </div> : null}
    </>}
  </div>;
}

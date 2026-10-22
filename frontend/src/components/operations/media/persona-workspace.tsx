"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo } from "react";
import useSWR from "swr";
import { RefreshCw, UserRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { mediaOperationsApi } from "@/lib/media-operations-api";
import { mediaBrandPortfolioApi, portfolioReadOptions } from "@/lib/media-brand-portfolio-api";
import { mediaOperationsSetupApi as mediaSetupApi } from "@/lib/media-operations-setup-api";
import { accountInScope, personaScope, PERSONA_WORKSPACE_STAGES, workspaceHref, type PersonaWorkspaceStage } from "@/lib/persona-workspace-model";
import { PersonaTeamPanel } from "./persona-team-panel";
import { PersonaBriefPanel } from "./persona-brief-panel";
import { MediaGenerationPanel } from "./generation-panel";
import { MediaContentVariantPanel } from "./content-variant-panel";
import { PersonaPublishPanel } from "./persona-publish-panel";
import { PersonaPortfolioPanel } from "./persona-portfolio-panel";
import { PersonaAssetPanel } from "./persona-asset-panel";
import { PersonaBridgeConnectionPanel } from "./persona-bridge-connection";
import { MediaPortfolioSummary } from "./media-portfolio-summary";

export function PersonaWorkspace() {
  const router = useRouter();
  const query = useSearchParams();
  const requestedPersona = query?.get("persona") ?? "";
  const requestedStage = query?.get("stage") ?? "brief";
  const stage: PersonaWorkspaceStage = PERSONA_WORKSPACE_STAGES.find(s => s.id === requestedStage)?.id ?? "brief";
  const contentItemId = query?.get("content") ?? "";
  const outputRef = query?.get("output") ?? "";
  const variantId = query?.get("variant") ?? "";
  const creation = query?.get("creation") === "existing" ? "existing" : "generation";
  const options = portfolioReadOptions;
  // The portfolio endpoint resolves the authenticated owner; listPersonas can also
  // include other authorized owners, so its result must never select an owner.
  const portfolio = useSWR("sns-v6-portfolio", mediaBrandPortfolioApi.get, options);
  const owner = !portfolio.error ? portfolio.data?.owner_user_id : undefined;
  const list = useSWR(owner ? ["persona-workspace-list", owner] : null, () => mediaOperationsApi.listPersonas(), options);
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
    () => mediaOperationsApi.getPersona(selected!.id), { shouldRetryOnError: false, revalidateOnFocus: false });
  const canonical = detail.data?.id === selected?.id && detail.data?.owner_user_id === selected?.owner_user_id && detail.data?.project_id === selected?.project_id ? detail.data : null;
  const scope = useMemo(() => canonical ? personaScope(canonical) : null, [canonical]);
  const accounts = useSWR(scope ? ["persona-workspace-accounts", scope.personaId, scope.projectId, scope.ownerUserId] : null,
    () => mediaSetupApi.listPlatformAccounts(scope!.projectId, scope!.personaId), { shouldRetryOnError: false, revalidateOnFocus: false });
  const scopedAccounts = useMemo(() => scope ? (accounts.data ?? []).filter(a => accountInScope(a, scope)) : [], [accounts.data, scope]);
  const navigate = (next: PersonaWorkspaceStage, content = contentItemId) => {
    const params = new URLSearchParams({ tab: "studio", persona: requestedPersona, stage: next });
    if (content) params.set("content", content);
    if (outputRef) params.set("output", outputRef);
    if (variantId) params.set("variant", variantId);
    if (next === "generation") params.set("creation", creation);
    router.replace(`/operations?${params}`, { scroll: false });
  };

  return <div className="min-w-0 space-y-5" data-testid="persona-workspace">
    <header className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><p className="text-xs font-semibold text-primary">Operations / 発信・制作</p><h1 className="mt-1 inline-flex items-center gap-1 text-2xl font-semibold">人格から制作する<HelpPopover label="人格から制作する">公開人格を選び、担当Agent・制作brief・媒体別内容を確認して公開します。</HelpPopover></h1></div><Button variant="outline" size="sm" onClick={() => { void portfolio.mutate(); void list.mutate(); void detail.mutate(); void accounts.mutate(); }}><RefreshCw className="size-3.5" /> workspaceを更新</Button></header>
    {requestedPersona && <MediaPortfolioSummary compact />}
    <PersonaPortfolioPanel onSaved={() => list.mutate()} setupBlockedReason={setupBlockedReason} />
    <div className="grid items-start gap-5 xl:grid-cols-[17rem_minmax(0,1fr)]">
      <aside className="min-w-0 space-y-3" aria-label="公開人格の選択">
        <div className="flex items-center justify-between"><h2 className="inline-flex items-center gap-1 text-sm font-semibold">公開人格<HelpPopover label="公開人格">1つの公開人格 = 1つのProject。SNS事業本部は非公開の統括Projectです。</HelpPopover></h2><Link href="/operations?tab=personas" className="text-xs text-primary hover:underline">人格を管理</Link></div>
        {(portfolio.isLoading || list.isLoading) && <p role="status" className="text-sm">公開人格を確認中…</p>}
        {!portfolio.error && list.error && <p role="alert" className="text-sm text-destructive">公開人格を取得できませんでした。workspaceを更新してください。</p>}
        {!portfolio.isLoading && !portfolio.error && !list.isLoading && !list.error && !personas.length && (otherOwnerPersonas.length
          ? <p role="alert" className="rounded-xl border p-4 text-sm">保存済みのSNS v6人格が別の所有者に{otherOwnerPersonas.length}件あります。このログインではその制作workspaceを開けません。保存済み人格の所有者のログインで開いてください。</p>
          : <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">このログインの所有者には公開人格がまだ登録されていません。「人格を管理」から設定してください。</p>)}
        {personas.map(persona => <Link key={persona.id} href={workspaceHref(persona.id)} aria-current={persona.id === requestedPersona ? "page" : undefined} className={`block min-w-0 rounded-xl border p-3 transition-colors ${persona.id === requestedPersona ? "border-primary bg-primary/5" : "bg-card hover:bg-muted/50"}`}>
          <span className="flex items-start gap-2"><UserRound className="mt-0.5 size-4 shrink-0 text-primary" /><span className="min-w-0 break-words text-sm font-semibold">{persona.current_revision.display_name}</span></span>
          <span className="mt-2 block break-words text-xs text-muted-foreground">{persona.current_revision.platforms.join(" / ") || "媒体未設定"} · {persona.state}</span>
        </Link>)}
      </aside>
      <div className="min-w-0 space-y-4">
        {!requestedPersona ? <><MediaPortfolioSummary /><p className="text-sm text-muted-foreground">公開人格を選択すると、この人格の制作workspaceを開きます。</p></>
          : !selected && !portfolio.isLoading && !portfolio.error && !list.isLoading && !list.error ? <div role="alert" className="rounded-xl border p-4 text-sm">指定された人格を確認できません。公開人格一覧から選択してください。</div>
          : detail.error ? <div role="alert" className="rounded-xl border border-destructive/30 p-4 text-sm text-destructive">選択した人格のProjectと設定を取得できませんでした。workspaceを更新してください。</div>
          : detail.isLoading ? <p role="status" className="p-4 text-sm">選択した人格を確認中…</p>
          : detail.data && !canonical ? <div role="alert" className="rounded-xl border p-4 text-sm">取得した人格の所属が選択したProjectと一致しません。workspaceを更新して確認してください。</div>
          : canonical && !scope ? <div role="alert" className="rounded-xl border p-4 text-sm">この人格のProject所属を確認できません。人格設定で公開人格ごとのProjectを確認してください。</div>
          : scope && canonical ? <div key={`${scope.personaId}:${scope.personaRevisionId}:${scope.ownerUserId}`} className="min-w-0 space-y-4">
            <section className="rounded-xl border bg-card p-4" aria-label="選択した人格の戦略"><div className="flex flex-wrap items-center justify-between gap-2"><h2 className="min-w-0 break-words text-xl font-semibold">{scope.displayName}</h2><Link className="text-xs text-primary hover:underline" href={`/operations?tab=personas&persona=${encodeURIComponent(scope.personaId)}`}>人格・接続先を設定</Link></div>
              <details className="mt-2 text-sm"><summary className="cursor-pointer text-primary">人格の戦略・制作方針を読む</summary><p className="mt-2 whitespace-pre-wrap break-words">{canonical.current_revision.summary || canonical.current_revision.positioning || "人格の戦略はまだ設定されていません。"}</p></details>
              <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-2"><div><dt className="text-muted-foreground">対象読者</dt><dd className="mt-1 break-words">{canonical.current_revision.audience || "未設定"}</dd></div><div><dt className="text-muted-foreground">測る成果</dt><dd className="mt-1 break-words">{canonical.current_revision.kpi_objectives.join(" / ") || "未設定"}</dd></div></dl>
              <details className="mt-3 text-xs text-muted-foreground"><summary className="cursor-pointer">人格・Projectの参照</summary><p className="mt-1 break-all">Project: {scope.projectId} · 人格revision: {scope.personaRevisionId}</p></details>
            </section>
            <PersonaTeamPanel scope={scope} />
            <section aria-label="人格の接続先" className="rounded-xl border bg-card p-4"><h2 className="text-sm font-semibold">接続先アカウント</h2>
              {accounts.error ? <p role="alert" className="mt-2 text-sm text-destructive">この人格の接続先を取得できませんでした。workspaceを更新してください。</p> : accounts.isLoading ? <p role="status" className="mt-2 text-sm">接続先を確認中…</p>
                : scopedAccounts.length ? <ul className="mt-2 flex flex-wrap gap-2">{scopedAccounts.map(a => <li key={a.id} className="min-w-0 rounded-lg border px-3 py-2 text-xs"><span className="break-words font-medium">{a.platform} · {a.account_ref}</span><span className="mt-1 block text-muted-foreground">{a.connection_id ? "接続設定あり・公開時に準備状態を確認" : "接続が必要"}</span></li>)}</ul>
                : <p className="mt-2 text-sm text-muted-foreground">この人格の媒体アカウントは未接続です。本人による接続後に公開できます。制作briefは先に準備できます。</p>}
            </section>
            <nav aria-label="人格の制作工程" className="grid grid-cols-2 gap-2 lg:grid-cols-4">{PERSONA_WORKSPACE_STAGES.map((item, index) => <Button key={item.id} variant={stage === item.id ? "default" : "outline"} className="h-auto min-w-0 justify-start whitespace-normal px-3 py-3 text-left" aria-current={stage === item.id ? "step" : undefined} onClick={() => navigate(item.id)}><span><span className="block text-sm">{index + 1}. {item.label}</span><span className="mt-1 block text-[11px] opacity-80">{item.detail}</span></span></Button>)}</nav>
            {!accounts.error && !accounts.isLoading && <PersonaBridgeConnectionPanel scope={scope} accounts={scopedAccounts} onSaved={() => accounts.mutate()} />}
            {stage === "brief" && <PersonaBriefPanel scope={scope} selectedContentItemId={contentItemId} onSelect={id => navigate("brief", id)} onContinue={() => navigate("generation")} />}
            {stage === "generation" && <div className="min-w-0 space-y-4"><nav aria-label="制作物を用意する方法" className="flex flex-wrap gap-2">{([['generation', '生成を計画する'], ['existing', '既存画像・動画を登録する']] as const).map(([value, label]) => <Button key={value} variant={creation === value ? "default" : "outline"} aria-pressed={creation === value} onClick={() => { const params = new URLSearchParams({ tab: "studio", persona: scope.personaId, stage: "generation", creation: value }); if (contentItemId) params.set("content", contentItemId); router.replace(`/operations?${params}`, { scroll: false }); }}>{label}</Button>)}</nav>
              {creation === "existing" ? <PersonaAssetPanel scope={scope} contentItemId={contentItemId} onBack={() => navigate("brief")} onSelected={asset => { const params = new URLSearchParams({ tab: "studio", persona: scope.personaId, stage: "qa", output: `${asset.generation_output_id} | ${asset.sha256}` }); if (contentItemId) params.set("content", contentItemId); router.replace(`/operations?${params}`, { scroll: false }); }} /> : <MediaGenerationPanel scope={scope} contentItemId={contentItemId} onOutputSelected={output => { const params = new URLSearchParams({ tab: "studio", persona: scope.personaId, stage: "qa", output: `${output.id} | ${output.sha256}` }); if (contentItemId) params.set("content", contentItemId); router.replace(`/operations?${params}`, { scroll: false }); }} />}
            </div>}
            {stage === "qa" && !accounts.error && <MediaContentVariantPanel scope={scope} accounts={scopedAccounts} initialContentItemId={contentItemId} initialOutputRef={outputRef} />}
            {stage === "publish" && !accounts.error && <PersonaPublishPanel scope={scope} accounts={scopedAccounts} selectedVariantId={variantId} onSelect={id => { const params = new URLSearchParams({ tab: "studio", persona: scope.personaId, stage: "publish", variant: id }); if (contentItemId) params.set("content", contentItemId); router.replace(`/operations?${params}`, { scroll: false }); }} />}
          </div> : null}
      </div>
    </div>
  </div>;
}

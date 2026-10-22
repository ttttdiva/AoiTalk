"use client";

import Link from "next/link";
import useSWR from "swr";
import { mediaBrandPortfolioApi, portfolioReadOptions } from "@/lib/media-brand-portfolio-api";
import { workspaceHref } from "@/lib/persona-workspace-model";
import { HelpPopover } from "@/components/ui/help-popover";

/** This is the authenticated owner's portfolio, independently of Operations' work filters. */
export function MediaPortfolioSummary({ compact = false }: { compact?: boolean }) {
  const portfolio = useSWR("sns-v6-portfolio", mediaBrandPortfolioApi.get, portfolioReadOptions);
  // Reuse the app header's canonical auth response. Never infer an owner from a persona list.
  const auth = useSWR<{ user?: { id: string; username: string; display_name?: string } }>("auth/status");
  const current = !portfolio.error ? portfolio.data : undefined;
  const authUser = auth.data?.user;
  const user = current && authUser && authUser.id === current.owner_user_id ? authUser : undefined;
  const brands = current?.brands.filter(brand => brand.persona && brand.project_id) ?? [];
  const accounts = brands.flatMap(brand => brand.channels.flatMap(channel => channel.account ? [channel.account] : []));
  const pending = accounts.filter(account => !account.connection_id).length;

  if (compact) return <section aria-label="ログイン所有者の制作状況" className="shrink-0 rounded-lg border px-3 py-1.5 text-xs">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1"><h2 className="font-semibold">Media Studio</h2>
      {current ? <span>{brands.length}人格 · {accounts.length}媒体枠 · 接続未設定 {pending}件</span> : <span>{portfolio.error ? "制作構成を再確認してください" : "制作構成を確認中…"}</span>}
      <Link className="underline" href="/operations?tab=studio">制作を開く</Link>
      <details className="relative"><summary className="cursor-pointer">ログイン・保存状態</summary><div className="absolute right-0 top-full z-30 mt-2 w-72 rounded-lg border bg-background p-3 text-foreground shadow-lg">
        <p>{user ? `ログイン: ${user.display_name || user.username}（${user.username}）` : current ? `所有者: ${current.owner_user_id}` : "所有者を確認中"}</p>
        <p className="mt-2">{current?.status === "ready" ? "媒体が未接続でも企画・制作物・下書きを保存できます。公開前に本人の接続先を確認します。" : "このログインのSNS v6構成は未登録、または未確認です。"}</p>
        {portfolio.error && <button type="button" className="mt-2 underline" onClick={() => void portfolio.mutate()}>制作構成を再取得</button>}
      </div></details>
    </div>
  </section>;

  return <section aria-label="ログイン所有者の制作状況" className={`min-w-0 rounded-xl border p-3 ${compact ? "shrink-0" : "bg-card"}`}>
    <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
      <h2 className="inline-flex items-center gap-1 font-semibold">
        このログインの制作workspace
        <HelpPopover label="このログインの制作workspace">
          媒体が未接続でも企画・制作物・下書きを保存できます。公開前に本人の接続先を確認します。接続設定の保存件数は一覧に表示されます。公開時には本人接続・内容・権利を確認します。
        </HelpPopover>
      </h2>
      {user && <p>ログイン: {user.display_name || user.username}（{user.username}）</p>}
      {!user && current && <p className="break-all text-xs text-muted-foreground">所有者: {current.owner_user_id}</p>}
      {current && <p>{brands.length}人格 · {accounts.length}媒体枠 · 接続未設定 {pending}件</p>}
    </div>
    {portfolio.error ? <div role="alert" className="mt-2 flex flex-wrap items-center gap-3 text-sm"><span>制作構成を取得できません。通信復帰後に再確認します。</span><button type="button" className="underline" onClick={() => void portfolio.mutate()}>制作構成を再取得</button></div>
      : !current ? <p role="status" className="mt-2 text-sm">ログイン所有者の制作構成を確認中…</p>
      : current.status !== "ready" ? <p className="mt-2 text-sm">このログインにはSNS v6の構成が登録されていません。保存済み人格の所有者を確認してください。</p>
      : <>
        {<div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">{brands.map(brand => {
          const channels = brand.channels.filter(channel => channel.account);
          const unconnected = channels.filter(channel => !channel.account?.connection_id).length;
          return <article key={brand.definition.brand_key} className="min-w-0 rounded-lg border p-3">
            <h3 className="break-words text-sm font-semibold">{brand.definition.display_name}</h3>
            <p className="mt-2 break-words text-xs text-muted-foreground">{channels.map(channel => channel.platform).join(" / ")} · 接続未設定 {unconnected}件</p>
            <p className="mt-1 text-xs">{brand.editorial_program_id ? "制作計画あり" : "制作計画未設定"} · {brand.content_item_id ? "保存済み企画あり" : "企画未保存"}</p>
            <Link className="mt-3 inline-block text-sm text-primary underline" href={workspaceHref(brand.persona!.id)}>制作briefを開く<span className="sr-only"> · {brand.definition.display_name}</span></Link>
          </article>;
        })}</div>}
      </>}
  </section>;
}

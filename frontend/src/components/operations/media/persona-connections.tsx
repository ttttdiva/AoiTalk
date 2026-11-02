"use client";

import Link from "next/link";
import type { MediaStockItem } from "@/lib/media-operations-api";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import {
  CONNECTABLE_PLATFORMS,
  PLATFORM_LABELS,
  isConnectablePlatform,
  personaConnectionState,
  type PersonaConnectionState,
} from "@/lib/persona-connection-state";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { cn } from "@/lib/utils";
import { PersonaAccountConnection, usePlatformCredential } from "./persona-account-connection";
import { YouTubeChannelSlot } from "./youtube-channel-slot-panel";
import { PersonaBridgeConnectionPanel } from "./persona-bridge-connection";
import { blockerLabel, stockItemHref } from "./content-stock-panel";

const TONE_CLASSES: Record<PersonaConnectionState["tone"], string> = {
  ok: "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  action: "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200",
  unknown: "border-border bg-muted/40 text-muted-foreground",
};

export function ConnectionStateChip({ platform, state }: { platform: string; state: PersonaConnectionState }) {
  return <span className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px]", TONE_CLASSES[state.tone])} data-connection-tone={state.tone}>
    <span className="font-semibold">{PLATFORM_LABELS[platform] ?? platform}</span>{state.label}
  </span>;
}

export function connectableAccounts(accounts: readonly PlatformAccount[]): PlatformAccount[] {
  return CONNECTABLE_PLATFORMS.flatMap(platform => accounts.filter(account => account.platform === platform));
}

function useConnectionState(ownerUserId: string, account: PlatformAccount): PersonaConnectionState {
  const credential = usePlatformCredential(ownerUserId, account.id);
  return personaConnectionState(account, credential.error || credential.isLoading ? undefined : credential.data);
}

/** Read-only chip for persona cards; shares the credential read with the connection row. */
export function AccountConnectionChip({ ownerUserId, account }: { ownerUserId: string; account: PlatformAccount }) {
  return <ConnectionStateChip platform={account.platform} state={useConnectionState(ownerUserId, account)} />;
}

function ConnectionRow({ scope, account, onUpdated }: { scope: PersonaWorkspaceScope; account: PlatformAccount; onUpdated: () => Promise<unknown> }) {
  const state = useConnectionState(scope.ownerUserId, account);
  return <li className="grid min-w-0 gap-2 p-3 sm:grid-cols-[9rem_minmax(0,1fr)]" data-connection-platform={account.platform}>
    <div className="flex flex-wrap items-start gap-2 sm:flex-col">
      <span className="text-sm font-semibold">{PLATFORM_LABELS[account.platform]}</span>
      <ConnectionStateChip platform={account.platform} state={state} />
    </div>
    {account.platform === "youtube"
      ? <YouTubeChannelSlot scope={scope} account={account} onUpdated={onUpdated} />
      : <PersonaAccountConnection scope={scope} account={account} />}
  </li>;
}

/** The first thing a persona shows: X / YouTube 本人接続 with its button. */
export function PersonaConnectionsCard({ scope, accounts, loading, error, onUpdated }: {
  scope: PersonaWorkspaceScope;
  accounts: PlatformAccount[];
  loading: boolean;
  error: unknown;
  onUpdated: () => Promise<unknown>;
}) {
  const connectable = connectableAccounts(accounts);
  const others = accounts.filter(account => !isConnectablePlatform(account.platform));
  return <section aria-label="人格の接続先" className="min-w-0 rounded-xl border bg-card" data-testid="persona-connections">
    <h2 className="border-b px-4 py-2.5 text-sm font-semibold">本人接続</h2>
    {error ? <p role="alert" className="p-4 text-sm text-destructive">この人格の接続先を取得できませんでした。workspaceを更新してください。</p>
      : loading ? <p role="status" className="p-4 text-sm">接続先を確認中…</p>
      : <ul className="divide-y">
        {CONNECTABLE_PLATFORMS.map(platform => {
          const slots = connectable.filter(account => account.platform === platform);
          return slots.length ? slots.map(account => <ConnectionRow key={account.id} scope={scope} account={account} onUpdated={onUpdated} />)
            : <li key={platform} className="grid gap-2 p-3 text-xs text-muted-foreground sm:grid-cols-[9rem_minmax(0,1fr)]" data-connection-platform={platform}>
              <span className="text-sm font-semibold text-foreground">{PLATFORM_LABELS[platform]}</span>
              <span>この人格に {PLATFORM_LABELS[platform]} 枠はありません。<Link className="text-primary hover:underline" href={`/operations?tab=personas&persona=${encodeURIComponent(scope.personaId)}`}>人格設定で作成</Link></span>
            </li>;
        })}
      </ul>}
    {!error && !loading && (others.length > 0 || connectable.length > 0) ? <div className="space-y-2 border-t px-4 py-2.5 text-xs text-muted-foreground">
      {others.length > 0 && <p>{others.map(account => PLATFORM_LABELS[account.platform] ?? account.platform).join("・")} は公式の本人接続に未対応です（{others.every(account => account.status !== "active") ? "停止中" : "手動運用"}）。</p>}
      {connectable.length > 0 && <details><summary className="cursor-pointer">別の接続方法: Bridge（開いている Edge のタブに結ぶ）</summary>
        <div className="mt-2"><PersonaBridgeConnectionPanel scope={scope} accounts={accounts} onSaved={onUpdated} /></div>
      </details>}
    </div> : null}
  </section>;
}

function AccountNextAction({ scope, account }: { scope: PersonaWorkspaceScope; account: PlatformAccount }) {
  const state = useConnectionState(scope.ownerUserId, account);
  if (!state.nextAction) return null;
  return <li className="flex gap-2"><span aria-hidden="true" className="mt-1.5 size-1.5 shrink-0 rounded-full bg-amber-500" /><span>{state.nextAction}<span className="text-muted-foreground">（{PLATFORM_LABELS[account.platform]} · {state.label}）</span></span></li>;
}

const NEXT_PRODUCT_LIMIT = 3;

/** Connection steps first, then the first open blocker of up to three products. */
export function PersonaNextActions({ scope, accounts, stockItems }: { scope: PersonaWorkspaceScope; accounts: PlatformAccount[]; stockItems: MediaStockItem[] }) {
  const products = stockItems.filter(item => item.stock_status !== "adopted" && (item.blockers ?? []).length > 0);
  return <section aria-label="次にやること" className="min-w-0 rounded-xl border bg-card">
    <h2 className="border-b px-4 py-2.5 text-sm font-semibold">次にやること</h2>
    <ul className="peer space-y-2 p-4 text-sm empty:hidden">
      {connectableAccounts(accounts).map(account => <AccountNextAction key={account.id} scope={scope} account={account} />)}
      {products.slice(0, NEXT_PRODUCT_LIMIT).map(item => <li key={item.id} className="flex gap-2"><span aria-hidden="true" className="mt-1.5 size-1.5 shrink-0 rounded-full bg-primary" />
        <span className="min-w-0 break-words"><Link className="text-primary hover:underline" href={stockItemHref(scope.personaId, item)}>{item.title}</Link>: {blockerLabel(item.blockers![0])}</span></li>)}
    </ul>
    <p className="hidden p-4 text-sm text-muted-foreground peer-empty:block">今やることはありません。</p>
    {products.length > NEXT_PRODUCT_LIMIT && <p className="border-t px-4 py-2 text-xs text-muted-foreground">ほか {products.length - NEXT_PRODUCT_LIMIT} 件は下の制作物にあります。</p>}
  </section>;
}

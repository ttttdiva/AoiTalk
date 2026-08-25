"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { Boxes, Plus, Search } from "lucide-react";
import { buttonVariants } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Input } from "@/components/ui/input";
import { appsApi, type AppSummary, type ProjectAppBinding } from "@/lib/apps-api";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { AppIdentityIcon } from "@/components/apps/app-identity-icon";
import { getAppVisualIdentity } from "@/lib/app-visual-identity";

function appHref(appId: string, projectId: string): string {
  return `/apps/${encodeURIComponent(appId)}${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`;
}

function AppListIdentity({ app }: { app: AppSummary }) {
  const identity = getAppVisualIdentity(app);
  return (
    <span
      className={`flex size-8 shrink-0 items-center justify-center rounded-lg ${identity.compactClass}`}
      data-app-id={app.id}
      data-app-identity-kind={identity.kind}
      data-app-identity-palette={identity.paletteKey}
    >
      <AppIdentityIcon app={app} className="size-4" />
    </span>
  );
}

export function AppsWorkspaceShell({
  children,
  projectIdOverride,
  embedded = false,
  selectedAppId,
  onSelectApp,
  onCreateApp,
  onAddExistingApp,
}: {
  children: React.ReactNode;
  projectIdOverride?: string;
  embedded?: boolean;
  selectedAppId?: string;
  onSelectApp?: (appId: string) => void;
  onCreateApp?: () => void;
  onAddExistingApp?: () => void;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const projectId = projectIdOverride ?? searchParams.get("project_id") ?? "";
  const activeAppId = selectedAppId || pathname?.split("/")[2] || "";
  const showCreate = !embedded && searchParams.get("create") === "1";
  const [query, setQuery] = useState("");
  const { data, error, isLoading } = useSWR<{ apps: AppSummary[] }>(
    `/apps/workspace${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
    () => appsApi.list(projectId || undefined),
  );
  const { data: projectAppsData, error: projectAppsError, isLoading: projectAppsLoading } = useSWR<{ project_id: string; apps: ProjectAppBinding[] }>(
    projectId ? `/projects/${projectId}/apps` : null,
    () => appsApi.getProjectApps(projectId),
  );
  const projectApps = useMemo(
    () => (projectAppsData?.apps || []).filter((binding) => binding.enabled && binding.app),
    [projectAppsData?.apps],
  );
  const boundAppIds = useMemo(() => new Set(projectApps.map((binding) => binding.app_id)), [projectApps]);

  const apps = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    const source = embedded ? projectApps.map((binding) => binding.app) : data?.apps || [];
    return source.filter((app) => {
      if (!normalized) return true;
      return [app.name, app.slug, app.description || ""]
        .join(" ")
        .toLocaleLowerCase()
        .includes(normalized);
    });
  }, [data?.apps, embedded, projectApps, query]);
  const hasAvailableApps = embedded ? projectApps.length > 0 : Boolean(data?.apps?.length);

  useEffect(() => {
    if (embedded || pathname !== "/apps" || showCreate || !data?.apps?.length) return;
    const firstProjectApp = projectId ? data.apps.find((app) => boundAppIds.has(app.id)) : data.apps[0];
    if (firstProjectApp) router.replace(appHref(firstProjectApp.id, projectId));
  }, [boundAppIds, data?.apps, embedded, pathname, projectId, router, showCreate]);

  const newAppHref = `/apps?create=1${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`;

  // Apps used to own a dedicated global rail.  The global rail now belongs to
  // SharedAppShell; only the Apps list is registered as the route's
  // workspace-navigation slot.  Embedded Project Apps deliberately leaves the
  // slot untouched so it cannot replace the Project tree or mount a second
  // shell around the App detail surface.
  const appsNavigation = useMemo(() => (
    <aside
      className="ao-workspace-nav-panel flex h-full min-h-0 min-w-0 flex-col border-r border-border/80 bg-card/60"
      data-shell-slot="workspace-navigation"
      data-workspace="apps"
    >
      <div className="border-b border-border/80 px-4 pb-4 pt-5">
        <h1 className="text-base font-semibold tracking-tight">Apps</h1>
        <p className="mt-1 text-xs text-muted-foreground">Local Navigation</p>
        <div className="relative mt-3">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input aria-label="Appを検索" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Appを検索" className="h-8 bg-background/60 pl-8 text-sm" />
        </div>
        <Link href={newAppHref} className={buttonVariants({ variant: "outline", size: "sm", className: "mt-3 h-8 w-full justify-center border-border/80 text-primary" })}>
          <Plus className="size-3.5" /> 新しいApp
        </Link>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {isLoading && !data ? (
          <div className="px-2 py-4 text-sm text-muted-foreground">Appsを読み込み中…</div>
        ) : error ? (
          <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-2.5 text-xs text-destructive">
            Appsを読み込めませんでした: {error.message}
          </div>
        ) : apps.length ? (
          <div className="space-y-0.5">
            {apps.map((app) => {
              const active = pathname === `/apps/${app.id}` || pathname?.startsWith(`/apps/${app.id}/`);
              const card = (
                <>
                  <AppListIdentity app={app} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium leading-5">{app.name}</span>
                    <span className="block truncate text-[11px] leading-4 text-muted-foreground">{app.description || app.slug}</span>
                  </span>
                </>
              );
              return (
                <Link
                  key={app.id}
                  href={appHref(app.id, projectId)}
                  aria-current={active ? "page" : undefined}
                  className={`group relative flex items-center gap-2.5 rounded px-2.5 py-2 text-left text-sm transition-colors ${active ? "bg-muted/70 font-semibold text-primary before:absolute before:left-0 before:top-1/2 before:h-4 before:w-0.5 before:-translate-y-1/2 before:bg-primary" : "text-muted-foreground hover:bg-muted/40 hover:text-foreground"}`}
                >
                  {card}
                </Link>
              );
            })}
          </div>
        ) : hasAvailableApps ? (
          <div className="rounded-lg border border-dashed border-border px-3 py-4 text-center">
            <Search className="mx-auto size-5 text-muted-foreground/50" />
            <p className="mt-1.5 text-xs font-medium">一致するAppがありません</p>
            <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">検索語を変更してください。</p>
          </div>
        ) : (
          <div className="rounded-lg border border-dashed border-border px-3 py-4 text-center">
            <Boxes className="mx-auto size-5 text-muted-foreground/50" />
            <p className="mt-1.5 text-xs font-medium">まだAppがありません</p>
            <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">上の「新しいApp」から作成できます。</p>
          </div>
        )}
      </div>
    </aside>
  ), [apps, data, error, hasAvailableApps, isLoading, newAppHref, pathname, projectId, query]);

  useWorkspaceShellRegistration({
    id: "apps-workspace",
    workspaceNavigation: embedded ? undefined : appsNavigation,
    priority: 20,
  });

  return (
    <div className="flex h-full min-h-0 min-w-0 w-full flex-col overflow-hidden bg-background lg:flex-row">
      {embedded && <aside className="hidden w-[clamp(15rem,21%,17rem)] shrink-0 flex-col border-r border-border/80 bg-card/60 lg:flex">
        <div className="border-b border-border/80 px-4 pb-4 pt-5">
          <h1 className="text-base font-semibold tracking-tight">Apps</h1>
          <p className="mt-1 text-xs text-muted-foreground">App Workspace</p>
          <div className="relative mt-3">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input aria-label="Appを検索" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Appを検索" className="h-8 bg-background/60 pl-8 text-sm" />
          </div>
          {embedded ? (onCreateApp || onAddExistingApp) ? <div className="mt-3 grid grid-cols-1 gap-1.5">{onCreateApp && <button type="button" onClick={onCreateApp} className={buttonVariants({ variant: "outline", size: "sm", className: "h-8 w-full justify-center border-border/80 px-1.5 text-primary" })}><Plus className="size-3.5" /> 新しいApp</button>}{onAddExistingApp && <button type="button" onClick={onAddExistingApp} className={buttonVariants({ size: "sm", variant: "outline", className: "h-8 w-full justify-center px-1.5" })}>既存App</button>}</div> : null : <Link href={newAppHref} className={buttonVariants({ variant: "outline", size: "sm", className: "mt-3 h-8 w-full justify-center border-border/80 text-primary" })}><Plus className="size-3.5" /> 新しいApp</Link>}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {(embedded && projectAppsLoading) || (!embedded && isLoading && !data) ? <div className="px-2 py-4 text-sm text-muted-foreground">Appsを読み込み中…</div> : embedded && projectAppsError ? <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-2.5 text-xs text-destructive">このProjectのAppsを読み込めませんでした: {projectAppsError.message}</div> : !embedded && error ? <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-2.5 text-xs text-destructive">Appsを読み込めませんでした: {error.message}</div> : apps.length ? <div className="space-y-0.5">{apps.map((app) => {
                const active = embedded
                  ? selectedAppId === app.id
                  : pathname === `/apps/${app.id}` || pathname?.startsWith(`/apps/${app.id}/`);
            const card = <><AppListIdentity app={app} /><span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium leading-5">{app.name}</span><span className="block truncate text-[11px] leading-4 text-muted-foreground">{app.description || app.slug}</span></span></>;
            const className = `group relative flex items-center gap-2.5 rounded px-2.5 py-2 text-left text-sm transition-colors ${active ? "bg-muted/70 font-semibold text-primary before:absolute before:left-0 before:top-1/2 before:h-4 before:w-0.5 before:-translate-y-1/2 before:bg-primary" : "text-muted-foreground hover:bg-muted/40 hover:text-foreground"}`;
            return embedded ? <button key={app.id} type="button" aria-current={active ? "page" : undefined} className={className} onClick={() => onSelectApp?.(app.id)}>{card}</button> : <Link key={app.id} href={appHref(app.id, projectId)} aria-current={active ? "page" : undefined} className={className}>{card}</Link>;
          })}</div> : hasAvailableApps ? <div className="rounded-lg border border-dashed border-border px-3 py-4 text-center"><Search className="mx-auto size-5 text-muted-foreground/50" /><p className="mt-1.5 text-xs font-medium">一致するAppがありません</p><p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">検索語を変更してください。</p></div> : <div className="rounded-lg border border-dashed border-border px-3 py-4 text-center"><Boxes className="mx-auto size-5 text-muted-foreground/50" /><p className="mt-1.5 text-xs font-medium">まだAppがありません</p><p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">上の「新しいApp」から作成できます。</p></div>}
        </div>
      </aside>}
      {embedded && (
        <div className="flex shrink-0 items-center gap-2 border-b border-border bg-card/25 p-3 lg:hidden">
          <label className="sr-only" htmlFor="apps-mobile-picker">Appを選択</label>
          <AppSelect
            id="apps-mobile-picker"
            aria-label="Appを選択"
            size="sm"
            value={activeAppId}
            onChange={(event) => {
              const selected = apps.find((app) => app.id === event.target.value);
              if (selected) onSelectApp?.(selected.id);
            }}
            className="min-w-0 flex-1"
          >
            <option value="">Appを選択</option>
            {apps.map((app) => (
              <option key={app.id} value={app.id}>{app.name}</option>
            ))}
          </AppSelect>
          {(onCreateApp || onAddExistingApp) && (
            <div className="flex shrink-0 gap-1.5">
              {onAddExistingApp && (
                <button
                  type="button"
                  onClick={onAddExistingApp}
                  aria-label="既存Appを追加"
                  title="既存Appを追加"
                  className={buttonVariants({ size: "sm", variant: "outline", className: "h-8 px-2 text-xs" })}
                >
                  既存App
                </button>
              )}
              {onCreateApp && (
                <button
                  type="button"
                  onClick={onCreateApp}
                  aria-label="新しいApp"
                  className={buttonVariants({ size: "icon-sm" })}
                >
                  <Plus className="size-4" />
                </button>
              )}
            </div>
          )}
        </div>
      )}
      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto" data-shell-region="apps-workspace-content">{children}</div>
    </div>
  );
}

"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { Archive, ArchiveRestore, Boxes, Download, Plus, Search } from "lucide-react";
import { buttonVariants } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Input } from "@/components/ui/input";
import { appsApi, permissionAtLeast, type AppSummary, type ProjectAppBinding } from "@/lib/apps-api";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { AppIdentityIcon } from "@/components/apps/app-identity-icon";
import { AppListContextMenu, type AppListContextMenuState } from "@/components/apps/app-list-context-menu";
import { getAppVisualIdentity } from "@/lib/app-visual-identity";
import { toast } from "sonner";

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

type AppListResponse = { apps: AppSummary[] };

function updateAppListCache(
  current: AppListResponse | undefined,
  app: AppSummary,
  includeArchived: boolean,
  archived: boolean,
): AppListResponse | undefined {
  if (!current) return current;
  const remaining = current.apps.filter((candidate) => candidate.id !== app.id);
  if (includeArchived) return { apps: [app, ...remaining] };
  return archived ? { apps: remaining } : { apps: [app, ...remaining] };
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function appsWorkspaceKey(projectId: string, includeArchived = false): string {
  const projectQuery = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  if (!includeArchived) return `/apps/workspace${projectQuery}`;
  return `/apps/workspace${projectQuery}${projectQuery ? "&" : "?"}include_archived=true`;
}

function isAppsWorkspaceKey(key: unknown): key is string {
  return typeof key === "string" && key.startsWith("/apps/workspace");
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
  const [archiveView, setArchiveView] = useState(false);
  const [contextMenu, setContextMenu] = useState<AppListContextMenuState | null>(null);
  const { mutate: mutateAllWorkspaceApps } = useSWRConfig();
  const { data, error, isLoading, mutate: mutateApps } = useSWR<{ apps: AppSummary[] }>(
    appsWorkspaceKey(projectId),
    () => appsApi.list(projectId || undefined),
  );
  const {
    data: archivedData,
    error: archivedError,
    isLoading: archivedLoading,
    mutate: mutateArchivedApps,
  } = useSWR<{ apps: AppSummary[] }>(
    !embedded && archiveView ? appsWorkspaceKey(projectId, true) : null,
    () => appsApi.list(projectId || undefined, { includeArchived: true }),
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
  const activeProjectBinding = useMemo(
    () => projectApps.find((binding) => binding.app_id === activeAppId),
    [activeAppId, projectApps],
  );
  const activeProjectApp = activeProjectBinding?.app;
  const canDownloadActiveApp = Boolean(
    embedded
      && activeProjectApp
      && permissionAtLeast(activeProjectBinding?.permission, "runner"),
  );

  const activeApps = useMemo(
    () => (data?.apps || []).filter((app) => !app.archived_at),
    [data?.apps],
  );
  const archivedApps = useMemo(
    () => (archivedData?.apps || []).filter((app) => Boolean(app.archived_at)),
    [archivedData?.apps],
  );
  const apps = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    const source = embedded
      ? projectApps.map((binding) => binding.app)
      : archiveView
        ? archivedApps
        : activeApps;
    return source.filter((app) => {
      if (!normalized) return true;
      return [app.name, app.slug, app.description || ""]
        .join(" ")
        .toLocaleLowerCase()
        .includes(normalized);
    });
  }, [activeApps, archiveView, archivedApps, embedded, projectApps, query]);
  const hasAvailableApps = embedded
    ? projectApps.length > 0
    : archiveView
      ? archivedApps.length > 0
      : activeApps.length > 0;

  useEffect(() => {
    if (embedded || archiveView || pathname !== "/apps" || showCreate || !activeApps.length) return;
    const firstProjectApp = projectId ? activeApps.find((app) => boundAppIds.has(app.id)) : activeApps[0];
    if (firstProjectApp) router.replace(appHref(firstProjectApp.id, projectId));
  }, [activeApps, archiveView, boundAppIds, embedded, pathname, projectId, router, showCreate]);

  const closeContextMenu = useCallback(() => {
    setContextMenu(null);
  }, []);

  const handleContextMenu = useCallback(
    (event: React.MouseEvent<HTMLElement>, app: AppSummary, archived: boolean) => {
      if (!permissionAtLeast(app.permission, "admin")) {
        setContextMenu(null);
        return;
      }
      event.preventDefault();
      setContextMenu({ x: event.clientX, y: event.clientY, app, archived });
    },
    [],
  );

  const refreshAppLists = useCallback(async (optimisticApp: AppSummary, archived: boolean) => {
    const refreshes = [
      mutateApps(
        () => appsApi.list(projectId || undefined),
        { revalidate: false, populateCache: false, rollbackOnError: false },
      ),
      ...(!embedded && archiveView
        ? [mutateArchivedApps(
            () => appsApi.list(projectId || undefined, { includeArchived: true }),
            { revalidate: false, populateCache: false, rollbackOnError: false },
          )]
        : []),
      mutateAllWorkspaceApps(isAppsWorkspaceKey),
    ];
    const results = await Promise.allSettled(refreshes);
    const refreshFailure = results.find((result) => result.status === "rejected");
    if (refreshFailure?.status === "rejected") {
      await Promise.allSettled([
        mutateApps(
          (current) => updateAppListCache(current, optimisticApp, false, archived),
          { revalidate: false },
        ),
        mutateArchivedApps(
          (current) => updateAppListCache(current, optimisticApp, true, archived),
          { revalidate: false },
        ),
      ]);
    }
    return refreshFailure;
  }, [archiveView, embedded, mutateAllWorkspaceApps, mutateApps, mutateArchivedApps, projectId]);

  const handleArchive = useCallback(async (app: AppSummary) => {
    try {
      const result = await appsApi.update(app.id, { archived: true });
      const updatedApp = { ...app, ...result.app };
      const optimisticResults = await Promise.allSettled([
        mutateApps(
          (current) => updateAppListCache(current, updatedApp, false, true),
          { revalidate: false },
        ),
        mutateArchivedApps(
          (current) => updateAppListCache(current, updatedApp, true, true),
          { revalidate: false },
        ),
      ]);
      toast.success("Appをアーカイブしました");
      if (!embedded && activeAppId === app.id) {
        const nextApp = activeApps
          .filter((candidate) => candidate.id !== app.id && !candidate.archived_at)
          .find((candidate) => !projectId || boundAppIds.has(candidate.id));
        router.replace(nextApp ? appHref(nextApp.id, projectId) : `/apps${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`);
      }
      const optimisticFailure = optimisticResults.find((result) => result.status === "rejected");
      const refreshFailure = await refreshAppLists(updatedApp, true);
      const refreshError = optimisticFailure || refreshFailure;
      if (refreshError?.status === "rejected") {
        toast.error(`アーカイブは完了しましたが、App一覧の再取得に失敗しました: ${errorMessage(refreshError.reason, "再取得に失敗しました")}`);
      }
    } catch (error) {
      toast.error(errorMessage(error, "Appのアーカイブに失敗しました"));
    }
  }, [activeAppId, activeApps, boundAppIds, embedded, mutateApps, mutateArchivedApps, projectId, refreshAppLists, router]);

  const handleRestore = useCallback(async (app: AppSummary) => {
    try {
      const result = await appsApi.update(app.id, { archived: false });
      const updatedApp = { ...app, ...result.app };
      const optimisticResults = await Promise.allSettled([
        mutateApps(
          (current) => updateAppListCache(current, updatedApp, false, false),
          { revalidate: false },
        ),
        mutateArchivedApps(
          (current) => updateAppListCache(current, updatedApp, true, false),
          { revalidate: false },
        ),
      ]);
      toast.success("Appのアーカイブを解除しました");
      const optimisticFailure = optimisticResults.find((result) => result.status === "rejected");
      const refreshFailure = await refreshAppLists(updatedApp, false);
      const refreshError = optimisticFailure || refreshFailure;
      if (refreshError?.status === "rejected") {
        toast.error(`アーカイブ解除は完了しましたが、App一覧の再取得に失敗しました: ${errorMessage(refreshError.reason, "再取得に失敗しました")}`);
      }
    } catch (error) {
      toast.error(errorMessage(error, "Appのアーカイブ解除に失敗しました"));
    }
  }, [mutateApps, mutateArchivedApps, refreshAppLists]);

  const newAppHref = `/apps?create=1${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`;

  // Apps used to own a dedicated global rail.  The global rail now belongs to
  // SharedAppShell; only the Apps list is registered as the route's
  // workspace-navigation slot.  Embedded Project Apps deliberately leaves the
  // slot untouched so it cannot replace the Project tree or mount a second
  // shell around the App detail surface.
  const appsNavigation = useMemo(() => (
    <>
      <aside
        className="ao-workspace-nav-panel flex h-full min-h-0 min-w-0 flex-col border-r border-border/80 bg-card/60"
        data-shell-slot="workspace-navigation"
        data-workspace="apps"
      >
        <div className="border-b border-border/80 px-4 pb-4 pt-5">
          <h1 className="text-base font-semibold tracking-tight">{archiveView ? "Apps / アーカイブ" : "Apps"}</h1>
          <p className="mt-1 text-xs text-muted-foreground">Local Navigation</p>
          <div className="relative mt-3">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input aria-label="Appを検索" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Appを検索" className="h-8 bg-background/60 pl-8 text-sm" />
          </div>
          {archiveView ? (
            <button
              type="button"
              onClick={() => {
                setArchiveView(false);
                closeContextMenu();
              }}
              className="mt-3 flex h-8 w-full items-center justify-start rounded-md border border-border/80 px-2.5 text-sm text-muted-foreground hover:bg-muted/40 hover:text-foreground"
            >
              ← Appsに戻る
            </button>
          ) : (
            <Link href={newAppHref} className={buttonVariants({ variant: "outline", size: "sm", className: "mt-3 h-8 w-full justify-center border-border/80 text-primary" })}>
              <Plus className="size-3.5" /> 新しいApp
            </Link>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {archiveView && archivedLoading && !archivedData ? (
            <div className="px-2 py-4 text-sm text-muted-foreground">アーカイブ済みAppを読み込み中…</div>
          ) : archiveView && archivedError ? (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-2.5 text-xs text-destructive">
              アーカイブ済みAppを読み込めませんでした: {archivedError.message}
            </div>
          ) : !archiveView && isLoading && !data ? (
            <div className="px-2 py-4 text-sm text-muted-foreground">Appsを読み込み中…</div>
          ) : !archiveView && error ? (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-2.5 text-xs text-destructive">
              Appsを読み込めませんでした: {error.message}
            </div>
          ) : apps.length ? (
            <div className="space-y-0.5">
              {apps.map((app) => {
                const active = !archiveView && (pathname === `/apps/${app.id}` || pathname?.startsWith(`/apps/${app.id}/`));
                const card = (
                  <>
                    <AppListIdentity app={app} />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium leading-5">{app.name}</span>
                      <span className="block truncate text-[11px] leading-4 text-muted-foreground">{app.description || app.slug}</span>
                    </span>
                  </>
                );
                const className = `group relative flex items-center gap-2.5 rounded px-2.5 py-2 text-left text-sm transition-colors ${active ? "bg-muted/70 font-semibold text-primary before:absolute before:left-0 before:top-1/2 before:h-4 before:w-0.5 before:-translate-y-1/2 before:bg-primary" : "text-muted-foreground hover:bg-muted/40 hover:text-foreground"}`;
                if (archiveView) {
                  return (
                    <div
                      key={app.id}
                      data-app-row={app.id}
                      data-app-archived="true"
                      className={className}
                      onContextMenu={(event) => handleContextMenu(event, app, true)}
                    >
                      {card}
                      {permissionAtLeast(app.permission, "admin") && (
                        <button
                          type="button"
                          aria-label={`${app.name}のアーカイブを解除`}
                          title="アーカイブ解除"
                          className="shrink-0 rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-accent hover:text-accent-foreground focus-visible:opacity-100 group-hover:opacity-100"
                          onClick={(event) => {
                            event.stopPropagation();
                            void handleRestore(app);
                          }}
                        >
                          <ArchiveRestore className="size-3.5" />
                        </button>
                      )}
                    </div>
                  );
                }
                return (
                  <Link
                    key={app.id}
                    href={appHref(app.id, projectId)}
                    data-app-row={app.id}
                    aria-current={active ? "page" : undefined}
                    className={className}
                    onContextMenu={(event) => handleContextMenu(event, app, false)}
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
              <p className="mt-1.5 text-xs font-medium">{archiveView ? "アーカイブ済みAppはありません" : "まだAppがありません"}</p>
              <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{archiveView ? "アーカイブしたAppがここに表示されます。" : "上の「新しいApp」から作成できます。"}</p>
            </div>
          )}
        </div>

        <div className="border-t border-border/80 px-4 py-3">
          {!archiveView && (
            <button
              type="button"
              onClick={() => {
                setArchiveView(true);
                closeContextMenu();
              }}
              className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm text-muted-foreground hover:bg-muted/40 hover:text-foreground"
            >
              <Archive className="size-3.5" /> アーカイブ
            </button>
          )}
          <p className="mt-3 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">外部ツール</p>
          <div className="mt-2 space-y-1 text-sm">
            <a href="/story/" target="_blank" rel="noopener noreferrer" className="block truncate text-muted-foreground hover:text-foreground hover:underline">Story / Novel Studio</a>
            <a href="/hf/" target="_blank" rel="noopener noreferrer" className="block truncate text-muted-foreground hover:text-foreground hover:underline">Hugging Face</a>
            <a href="/hydrus/" target="_blank" rel="noopener noreferrer" className="block truncate text-muted-foreground hover:text-foreground hover:underline">Hydrus</a>
          </div>
        </div>
      </aside>
      <AppListContextMenu
        menu={contextMenu}
        onClose={closeContextMenu}
        onArchive={handleArchive}
        onRestore={handleRestore}
      />
    </>
  ), [apps, archiveView, archivedData, archivedError, archivedLoading, closeContextMenu, contextMenu, data, error, handleArchive, handleContextMenu, handleRestore, hasAvailableApps, isLoading, newAppHref, pathname, projectId, query]);

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
      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto" data-shell-region="apps-workspace-content">
        {canDownloadActiveApp && activeProjectApp && (
          <div className="sticky top-0 z-20 flex justify-end border-b border-border/80 bg-background/95 px-4 py-2 backdrop-blur">
            <a
              href={appsApi.downloadArchive(activeProjectApp.id, projectId || undefined, { git: true, dependencies: true, runtime: true, credentials: true })}
              download
              className={buttonVariants({ variant: "outline", size: "sm", className: "h-8 px-3" })}
              title={`${activeProjectApp.name} を軽量・安全設定でダウンロード`}
            >
              <Download className="size-3.5" /> ダウンロード
            </a>
          </div>
        )}
        {children}
      </div>
    </div>
  );
}

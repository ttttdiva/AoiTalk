"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR, { useSWRConfig } from "swr";
import { toast } from "sonner";
import { Boxes, Loader2, Plus, Search, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { AppsWorkspaceShell } from "@/components/apps/apps-workspace-shell";
import { AppDetailPage } from "@/components/apps/app-detail-page";
import { AppIdentityIcon } from "@/components/apps/app-identity-icon";
import { appsApi, permissionAtLeast, type AppSourceImportFile, type AppSummary, type ProjectAppBinding } from "@/lib/apps-api";
import { getAppVisualIdentity } from "@/lib/app-visual-identity";
import { getDroppedExplorerFiles } from "@/lib/file-drop";

export function ProjectAppsPanel({ projectId, canWrite = true }: { projectId: string; projectName?: string; canWrite?: boolean }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedAppId = searchParams.get("app_id") || "";
  const [selectedAppId, setSelectedAppId] = useState(requestedAppId);
  const [showCreate, setShowCreate] = useState(false);
  const [showLink, setShowLink] = useState(false);
  const [linkQuery, setLinkQuery] = useState("");
  const [addingAppId, setAddingAppId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [sourceFiles, setSourceFiles] = useState<AppSourceImportFile[]>([]);
  const [sourceDragging, setSourceDragging] = useState(false);
  const [creating, setCreating] = useState(false);
  const { mutate } = useSWRConfig();
  const { data: bindingsData, mutate: mutateProjectApps } = useSWR<{ project_id: string; apps: ProjectAppBinding[] }>(
    `/projects/${projectId}/apps`,
    () => appsApi.getProjectApps(projectId),
  );
  const {
    data: availableAppsData,
    error: availableAppsError,
    isLoading: availableAppsLoading,
    mutate: mutateAvailableApps,
  } = useSWR<{ apps: AppSummary[] }>(
    showLink ? `/apps/available?project_id=${encodeURIComponent(projectId)}` : null,
    () => appsApi.list(projectId),
  );

  useEffect(() => {
    setSelectedAppId(requestedAppId);
    setShowCreate(false);
    setShowLink(false);
    setLinkQuery("");
    setAddingAppId(null);
    setName("");
    setSlug("");
    setDescription("");
    setSourceFiles([]);
    setSourceDragging(false);
  }, [projectId, requestedAppId]);

  const enabledBindings = (bindingsData?.apps || []).filter((binding) => binding.enabled);
  const boundAppIds = new Set(enabledBindings.map((binding) => binding.app_id));
  const normalizedLinkQuery = linkQuery.trim().toLocaleLowerCase();
  const availableApps = (availableAppsData?.apps || [])
    .filter((app) => !boundAppIds.has(app.id))
    .filter((app) => !normalizedLinkQuery || [app.name, app.slug, app.description || ""].join(" ").toLocaleLowerCase().includes(normalizedLinkQuery));
  const activeAppId = selectedAppId && enabledBindings.some((binding) => binding.app_id === selectedAppId)
    ? selectedAppId
    : enabledBindings[0]?.app_id || "";

  useEffect(() => {
    if (!bindingsData || !requestedAppId || enabledBindings.some((binding) => binding.app_id === requestedAppId)) return;
    // Never leave a stale app_id in the URL while showing another App.  This
    // is especially important when a binding was removed in another tab.
    const params = new URLSearchParams(searchParams.toString());
    if (activeAppId) params.set("app_id", activeAppId);
    else params.delete("app_id");
    params.set("project_id", projectId);
    router.replace(`/projects?${params.toString()}`, { scroll: false });
  }, [activeAppId, bindingsData, enabledBindings, projectId, requestedAppId, router, searchParams]);

  const selectApp = (appId: string) => {
    setSelectedAppId(appId);
    const params = new URLSearchParams(searchParams.toString());
    params.set("project_id", projectId);
    params.set("app_id", appId);
    router.replace(`/projects?${params.toString()}`, { scroll: false });
  };

  const openExistingAppPicker = () => {
    setShowCreate(false);
    setShowLink(true);
    setLinkQuery("");
  };

  const linkExistingApp = async (app: AppSummary) => {
    if (addingAppId || !canWrite || !permissionAtLeast(app.permission, "runner")) return;
    setAddingAppId(app.id);
    try {
      await appsApi.linkProjectApp(projectId, { app_id: app.id, binding_mode: "development" });
      toast.success(`${app.name} をこのProjectに追加しました`);
      const refreshResults = await Promise.allSettled([
        mutateProjectApps(),
        mutateAvailableApps(),
        mutate(`/apps/workspace?project_id=${encodeURIComponent(projectId)}`),
        mutate("/apps/workspace"),
      ]);
      if (refreshResults.some((result) => result.status === "rejected")) {
        toast.warning("Appは追加されましたが、一覧の更新に失敗しました。ページを再読み込みしてください。");
      }
      setShowLink(false);
      if (refreshResults[0]?.status === "rejected") {
        // Keep the current selection while the project binding cache is stale;
        // otherwise the URL correction effect can immediately replace the new
        // app_id with the previous binding before a reload.
        return;
      }
      selectApp(app.id);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "既存AppをProjectに追加できませんでした");
    } finally {
      setAddingAppId(null);
    }
  };

  const createApp = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canWrite || !name.trim()) return;
    setCreating(true);
    try {
      const result = await appsApi.create({
        name: name.trim(),
        slug: slug.trim() || undefined,
        description: description.trim(),
        origin_project_id: projectId,
      });
      let sourceImportWarning: string | null = null;
      if (sourceFiles.length) {
        try {
          const status = await appsApi.getGitStatus(result.app.id, projectId);
          const preview = await appsApi.previewSourceImport(result.app.id, {
            files: sourceFiles,
            expected_revision: status.revision,
            root_mode: "strip_common",
          }, projectId);
          if (Array.isArray(preview.rejected) && preview.rejected.length) {
            sourceImportWarning = "取り込めないファイルが含まれているため、ソースはまだ反映していません。App詳細の「ソースを更新」から対象を選び直してください。";
          } else {
            await appsApi.applySourceImport(result.app.id, preview.import_id, {
              expected_revision: preview.base_revision || status.revision || "",
            }, projectId);
          }
        } catch (error) {
          sourceImportWarning = error instanceof Error
            ? `ソースの取込に失敗しました。Appは作成済みですので、App詳細の「ソースを更新」から再試行してください: ${error.message}`
            : "ソースの取込に失敗しました。Appは作成済みですので、App詳細の「ソースを更新」から再試行してください。";
        }
      }
      try {
        await appsApi.analyze(result.app.id, {}, projectId);
      } catch (error) {
        // Chatへ進むことを妨げず、分析失敗はApp画面で再実行できるようにする。
        toast.warning(error instanceof Error ? `Appは作成しました。業務分析は再実行してください: ${error.message}` : "Appは作成しました。業務分析は再実行してください");
      }
      const cacheRefreshes = await Promise.allSettled([
        mutate(`/apps/workspace?project_id=${encodeURIComponent(projectId)}`),
        mutate(`/projects/${projectId}/apps`),
      ]);
      if (cacheRefreshes.some((result) => result.status === "rejected")) {
        toast.warning("Appは作成済みですが、一覧の更新に失敗しました。画面を再読み込みすると反映されます。");
      }
      setShowCreate(false);
      setName("");
      setSlug("");
      setDescription("");
      setSourceFiles([]);
      if (sourceImportWarning) {
        toast.warning(`Appを作成しました。${sourceImportWarning}`);
      } else {
        toast.success("Appを作成しました。Chatで開発を始めます");
      }
      const target = result.app.targets?.find((item) => item.target_key === result.app.default_target_key) || result.app.targets?.[0];
      const chatParams = new URLSearchParams({ app_id: result.app.id, project_id: projectId });
      if (target) chatParams.set("app_target_id", target.id);
      router.push(`/chat?${chatParams.toString()}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Appの作成に失敗しました");
    } finally {
      setCreating(false);
    }
  };

  const acceptSourceFiles = (items: AppSourceImportFile[]) => {
    const unique = new Map(items.map((item) => [`${item.relativePath}:${item.file.size}:${item.file.lastModified}`, item]));
    setSourceFiles(Array.from(unique.values()));
  };

  const handleSourceDrop = async (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setSourceDragging(false);
    try {
      acceptSourceFiles((await getDroppedExplorerFiles(event.dataTransfer)).map(({ file, relativePath }) => ({ file, relativePath: relativePath || file.name })));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "ドロップしたソースを読み込めませんでした");
    }
  };

  return (
    <div className="min-h-0 flex-1 overflow-hidden">
      <AppsWorkspaceShell
        projectIdOverride={projectId}
        embedded
        selectedAppId={activeAppId}
        onSelectApp={selectApp}
        onCreateApp={canWrite ? () => { setShowLink(false); setShowCreate(true); } : undefined}
        onAddExistingApp={canWrite ? openExistingAppPicker : undefined}
      >
        {showLink ? (
          <div className="flex h-full min-h-0 items-start justify-center overflow-auto p-5 md:p-8">
            <Card className="w-full max-w-2xl">
              <CardHeader className="flex-row items-start justify-between gap-4 space-y-0">
                <div>
                  <CardTitle className="flex items-center gap-2 text-base"><Boxes className="size-4 text-primary" /> 既存AppをProjectに追加</CardTitle>
                  <p className="mt-1 text-xs text-muted-foreground">このProjectで利用するAppを選びます。App本体や他Projectとの関連は変更しません。</p>
                </div>
                <Button type="button" variant="ghost" size="sm" onClick={() => setShowLink(false)}>閉じる</Button>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="relative"><Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" /><Input aria-label="追加するAppを検索" value={linkQuery} onChange={(event) => setLinkQuery(event.target.value)} placeholder="App名、識別子、用途で検索" className="pl-8" autoFocus /></div>
                {availableAppsLoading ? <p className="py-6 text-center text-xs text-muted-foreground">追加可能なAppを読み込み中…</p> : availableAppsError ? <p className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">App一覧を読み込めませんでした: {availableAppsError.message}</p> : availableApps.length ? <div className="grid gap-2">{availableApps.map((app) => { const canLink = permissionAtLeast(app.permission, "runner"); const busy = addingAppId === app.id; const identity = getAppVisualIdentity(app); return <button key={app.id} type="button" disabled={!canLink || addingAppId !== null} onClick={() => void linkExistingApp(app)} className="flex items-center gap-3 rounded-lg border p-3 text-left transition-colors hover:border-primary/60 hover:bg-accent/50 disabled:cursor-not-allowed disabled:opacity-50"><span className={`flex size-9 shrink-0 items-center justify-center rounded-lg ${identity.compactClass}`} data-app-id={app.id} data-app-identity-kind={identity.kind} data-app-identity-palette={identity.paletteKey}><AppIdentityIcon app={app} className="size-4" /></span><span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{app.name}</span><span className="block truncate text-[11px] text-muted-foreground">{app.description || app.slug}</span></span><span className="shrink-0 text-xs text-muted-foreground">{busy ? <Loader2 className="size-4 animate-spin" /> : canLink ? "追加" : "権限不足"}</span></button>; })}</div> : <div className="rounded-lg border border-dashed p-6 text-center"><p className="text-sm font-medium">追加できるAppがありません</p><p className="mt-1 text-xs text-muted-foreground">既に追加済みか、runner以上の権限があるAppが見つかりません。</p></div>}
              </CardContent>
            </Card>
          </div>
        ) : showCreate ? (
          <div className="flex h-full min-h-0 items-start justify-center overflow-auto p-5 md:p-8">
            <Card className="w-full max-w-2xl">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base"><Plus className="size-4 text-primary" /> 新しいApp</CardTitle>
                <p className="text-xs text-muted-foreground">App workspaceを作成し、業務内容を分析したあと、App context付きのChatで開発を始めます。</p>
              </CardHeader>
              <CardContent>
                <form className="grid gap-4 md:grid-cols-2" onSubmit={createApp}>
                  <div className="space-y-1.5 md:col-span-2"><Label htmlFor="project-app-name">App名</Label><Input id="project-app-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="例：申請ファイル変換" required autoFocus /></div>
                  <div className="space-y-1.5"><Label htmlFor="project-app-slug">識別子（任意）</Label><Input id="project-app-slug" value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="request-file-converter" /></div>
                  <div className="space-y-1.5 md:col-span-2"><Label htmlFor="project-app-description">用途</Label><Textarea id="project-app-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="何を自動化するAppか" rows={4} /></div>
                  <div className="space-y-1.5 md:col-span-2"><Label>既存ソースを取り込む（任意）</Label><div className={`rounded-xl border-2 border-dashed p-5 text-center transition-colors ${sourceDragging ? "border-primary bg-primary/10" : "border-border bg-muted/15"}`} onDragEnter={(event) => { event.preventDefault(); setSourceDragging(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={(event) => { event.preventDefault(); setSourceDragging(false); }} onDrop={(event) => void handleSourceDrop(event)}><Upload className="mx-auto size-6 text-muted-foreground" /><p className="mt-2 text-sm font-medium">フォルダ、ファイル、ZIPをここへドロップ</p><p className="mt-1 text-xs text-muted-foreground">元の場所は保存せず、App workspaceへコピーします。XLSMなどのバイナリもそのまま取り込めます。</p><Input id="project-app-source-files" type="file" multiple className="mx-auto mt-3 max-w-sm" onChange={(event) => acceptSourceFiles(Array.from(event.target.files || []).map((file) => ({ file, relativePath: file.webkitRelativePath || file.name })))} /></div>{sourceFiles.length > 0 && <p className="text-xs text-primary">{sourceFiles.length}ファイルを取り込みます</p>}</div>
                  <div className="flex justify-end gap-2 md:col-span-2"><Button type="button" variant="ghost" onClick={() => setShowCreate(false)}>キャンセル</Button><Button type="submit" disabled={creating || !name.trim()}>{creating && <Loader2 className="size-3.5 animate-spin" />} 作成してChatで開発</Button></div>
                </form>
              </CardContent>
            </Card>
          </div>
        ) : activeAppId ? <AppDetailPage key={`${projectId}:${activeAppId}`} appIdOverride={activeAppId} projectIdOverride={projectId} projectCanWrite={canWrite} embedded /> : (
          <div className="flex h-full min-h-0 items-center justify-center p-6 text-center">
            <div><Boxes className="mx-auto size-8 text-muted-foreground/50" /><h2 className="mt-2 text-base font-semibold">Appを選択してください</h2><p className="mt-1 text-xs text-muted-foreground">左の一覧から、このProjectで使うAppを選択できます。</p></div>
          </div>
        )}
      </AppsWorkspaceShell>
    </div>
  );
}

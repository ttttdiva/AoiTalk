"use client";

import Link from "next/link";
import { usePathname, useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { toast } from "sonner";
import {
  ArrowRight,
  Box,
  BookOpen,
  Check,
  CheckCircle2,
  CircleAlert,
  CirclePlay,
  Code2,
  Download,
  FileCode2,
  FileText,
  FileSpreadsheet,
  FolderOpen,
  GitBranch,
  Hammer,
  History,
  Loader2,
  Globe2,
  MessageSquare,
  Monitor,
  Package,
  Play,
  RefreshCw,
  Save,
  Settings2,
  Terminal,
  TestTube2,
  Wrench,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AppSelect } from "@/components/ui/app-select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { AppSourceUpdateDialog } from "@/components/apps/app-source-update-dialog";
import { AppArchiveDownloadDialog } from "@/components/apps/app-archive-download-dialog";
import { AppIdentityIcon } from "@/components/apps/app-identity-icon";
import { chatApi, type ConversationSession } from "@/lib/chat-api";
import { getAppVisualIdentity } from "@/lib/app-visual-identity";
import {
  appsApi,
  permissionAtLeast,
  type AppContext,
  type AppFile,
  type AppGitHistoryEntry,
  type AppGitStatus,
  type AppJob,
  type AppOverviewAnalysis,
  type AppRelease,
  type AppSummary,
  type AppTarget,
  type ProjectAppInput,
  type ProjectAppBinding,
  type TaskAppLink,
} from "@/lib/apps-api";

type DetailTab = "overview" | "use" | "development" | "history" | "settings";
type DevelopmentSection = "chats" | "tasks" | "files" | "docs" | "operations";
type HistorySection = "jobs" | "releases" | "changes";

const TABS: Array<{ key: DetailTab; label: string }> = [
  { key: "overview", label: "概要" },
  { key: "use", label: "利用" },
  { key: "development", label: "開発" },
  { key: "history", label: "履歴" },
  { key: "settings", label: "設定" },
];

const ALL_TAB_KEYS = new Set<DetailTab>(TABS.map((item) => item.key));

function isDevelopmentSection(value: string | null | undefined): value is DevelopmentSection {
  return value === "chats" || value === "tasks" || value === "files" || value === "docs" || value === "operations";
}

function isHistorySection(value: string | null | undefined): value is HistorySection {
  return value === "jobs" || value === "releases" || value === "changes";
}

function resolveTab(value: string | null): { tab: DetailTab; section?: DevelopmentSection | HistorySection } {
  if (value === "tasks" || value === "files" || value === "docs" || value === "development") {
    return { tab: "development", section: value === "development" ? "chats" : value };
  }
  if (value === "jobs" || value === "releases") {
    return { tab: "history", section: value };
  }
  if (value === "history") return { tab: "history", section: "jobs" };
  if (value && ALL_TAB_KEYS.has(value as DetailTab)) return { tab: value as DetailTab };
  return { tab: "overview" };
}

function formatDate(value?: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("ja-JP");
}

function jobTypeLabel(value: string): string {
  return ({ build: "Build", test: "Test", run: "実行", package: "Package" } as Record<string, string>)[value] || value;
}

function relationTypeLabel(value?: string | null): string {
  return ({ develops: "開発", fixes: "修正", tests: "テスト", releases: "リリース", uses: "利用", related: "関連" } as Record<string, string>)[value || ""] || value || "関連";
}

function surfaceLabel(value?: string | null): string {
  return ({ embedded_web: "AoiTalk内", standalone_web: "Standalone Web", desktop_gui: "デスクトップ", headless: "画面なし", office: "Office" } as Record<string, string>)[value || ""] || value || "—";
}

function runtimeLabel(value?: string | null): string {
  return ({ static_web: "静的Web", node: "Node.js", python: "Python", powershell: "PowerShell", batch: "バッチ", vba: "VBA", executable: "実行ファイル" } as Record<string, string>)[value || ""] || value || "—";
}

function executionHostLabel(value?: string | null): string {
  return ({ aoitalk: "AoiTalk", server: "サーバー", client: "利用者PC", browser: "ブラウザ", office: "Office", download_only: "ダウンロード" } as Record<string, string>)[value || ""] || value || "—";
}

function readableStatus(value: string): string {
  return { queued: "待機中", running: "実行中", succeeded: "成功", failed: "失敗", cancelled: "中止" }[value] || value;
}

function releaseStatusLabel(value: string): string {
  return { published: "保存済み", deprecated: "旧版" }[value] || value;
}

function TargetIcon({ target, className = "size-6" }: { target: AppTarget; className?: string }) {
  if (target.surface === "embedded_web") return <MessageSquare className={className} />;
  if (target.surface === "standalone_web") return <Globe2 className={className} />;
  if (target.surface === "headless") return <Terminal className={className} />;
  if (target.surface === "desktop_gui") return <Monitor className={className} />;
  if (target.surface === "office" || target.runtime === "vba") return <FileSpreadsheet className={className} />;
  return <Box className={className} />;
}

type OverviewDiagramRole = "input" | "process" | "output";

function OverviewDiagramIcon({ target, role, label, detail, className = "size-6" }: { target?: AppTarget; role: OverviewDiagramRole; label: string; detail: string; className?: string }) {
  const text = `${label} ${detail}`;
  if (role === "input") {
    if (target?.surface === "office" || target?.runtime === "vba" || /csv|tsv|excel|xlsx|xlsm|xls|office|spreadsheet|表計算/i.test(text)) return <FileSpreadsheet className={className} />;
    if (target?.surface === "headless" || /powershell|python|batch|script|command|コマンド|スクリプト/i.test(text)) return <Terminal className={className} />;
    if (/url|http|api|web|ブラウザ|ウェブ/i.test(text)) return <Globe2 className={className} />;
    return <FileText className={className} />;
  }
  if (role === "process") {
    if (/通知|slack|message|メッセージ|チャット/i.test(text)) return <MessageSquare className={className} />;
    if (["powershell", "python", "batch"].includes(target?.runtime || "") || /スクリプト/i.test(text)) return <Terminal className={className} />;
    return <Settings2 className={className} />;
  }
  if (/通知|slack|message|メッセージ|チャット/i.test(text)) return <MessageSquare className={className} />;
  if (target?.surface === "office" || target?.runtime === "vba" || /csv|tsv|excel|xlsx|xlsm|xls|office|spreadsheet|表計算/i.test(text)) return <FileSpreadsheet className={className} />;
  if (/log|ログ|document|docs|文書/i.test(text)) return <FileText className={className} />;
  return <Package className={className} />;
}

function readmeSummary(readme: string): string[] {
  const lines = readme
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => !line.startsWith("#"))
    .map((line) => line.replace(/^[-*]\s*/, "").trim())
    .filter(Boolean);
  return lines.slice(0, 3);
}

export function AppDetailPage({ appIdOverride, projectIdOverride, projectCanWrite = true, embedded = false }: { appIdOverride?: string; projectIdOverride?: string; projectCanWrite?: boolean; embedded?: boolean } = {}) {
  const params = useParams<{ app_id: string }>();
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const previousRouteQuery = useRef(searchParams.toString());
  const applyingRouteFromUrl = useRef(false);
  const appId = appIdOverride || params.app_id;
  const initialRoute = resolveTab(searchParams.get("tab"));
  const initialSection = searchParams.get("section");
  const initialDevelopmentSection: DevelopmentSection = isDevelopmentSection(initialSection)
    ? initialSection
    : initialRoute.tab === "development" && isDevelopmentSection(initialRoute.section) ? initialRoute.section : "chats";
  const initialHistorySection: HistorySection = isHistorySection(initialSection)
    ? initialSection
    : initialRoute.tab === "history" && isHistorySection(initialRoute.section) ? initialRoute.section : "jobs";
  const [tab, setTab] = useState<DetailTab>(initialRoute.tab);
  const [developmentSection, setDevelopmentSection] = useState<DevelopmentSection>(initialDevelopmentSection);
  const [historySection, setHistorySection] = useState<HistorySection>(initialHistorySection);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState("");
  const [fileSha, setFileSha] = useState<string | undefined>();
  const [readmeContent, setReadmeContent] = useState<string | null>(null);
  const [selectedDocPath, setSelectedDocPath] = useState("README.md");
  const [docContent, setDocContent] = useState("");
  const [docSha, setDocSha] = useState<string | undefined>();
  const [saving, setSaving] = useState(false);
  const [busyJob, setBusyJob] = useState<string | null>(null);
  const [selectedTarget, setSelectedTarget] = useState("");
  const projectId = projectIdOverride ?? searchParams.get("project_id") ?? "";
  const [releaseVersion, setReleaseVersion] = useState("");
  const [releaseNotes, setReleaseNotes] = useState("");
  const [bindingBusy, setBindingBusy] = useState(false);
  const [analysisBusy, setAnalysisBusy] = useState(false);

  const projectQuery = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  const appKey = appId ? `/apps/${appId}${projectQuery}` : null;
  const { data: appData, error: appError, mutate: mutateApp } = useSWR<{ app: AppSummary }>(appKey, () => appsApi.get(appId, projectId || undefined));
  const { data: context, error: contextError, mutate: mutateContext } = useSWR<AppContext>(appId ? `/apps/${appId}/context${projectQuery}` : null, () => appsApi.getContext(appId, projectId || undefined));
  const { data: filesData, error: filesError, isLoading: filesLoading, mutate: mutateFiles } = useSWR<{ files: AppFile[] }>(appId ? `/apps/${appId}/files${projectQuery}` : null, () => appsApi.getFiles(appId, projectId || undefined));
  const { data: status, error: statusError, mutate: mutateStatus } = useSWR<AppGitStatus>(appId ? `/apps/${appId}/git/status${projectQuery}` : null, () => appsApi.getGitStatus(appId, projectId || undefined));
  const { data: historyData, error: historyError, isLoading: historyLoading, mutate: mutateHistory } = useSWR<{ history: AppGitHistoryEntry[] }>(appId ? `/apps/${appId}/git/history${projectQuery}` : null, () => appsApi.getGitHistory(appId, projectId || undefined));
  const { data: jobsData, error: jobsError, isLoading: jobsLoading, mutate: mutateJobs } = useSWR<{ jobs: AppJob[] }>(appId ? `/apps/${appId}/jobs${projectQuery}` : null, () => appsApi.getJobs(appId, projectId || undefined), { refreshInterval: 5000 });
  const { data: developmentChatsData, error: developmentChatsError, isLoading: developmentChatsLoading, mutate: mutateDevelopmentChats } = useSWR<{ conversations: ConversationSession[] }>(
    appId ? `/conversations?app_id=${encodeURIComponent(appId)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}` : null,
    () => chatApi.listSessions(projectId || undefined, appId),
    { refreshInterval: 5000 },
  );
  const { data: releasesData, error: releasesError, isLoading: releasesLoading, mutate: mutateReleases } = useSWR<{ releases: AppRelease[] }>(appId ? `/apps/${appId}/releases${projectQuery}` : null, () => appsApi.getReleases(appId, projectId || undefined));
  const { data: projectAppsData, mutate: mutateProjectApps } = useSWR<{ project_id: string; apps: ProjectAppBinding[] }>(projectId ? `/projects/${projectId}/apps` : null, () => appsApi.getProjectApps(projectId));
  const { data: projectDirectory } = useSWR<{ projects: Array<{ id: string; name: string }> }>("/projects/directory", async () => {
    const response = await fetch("/api/projects", { credentials: "include" });
    if (!response.ok) return { projects: [] };
    return response.json() as Promise<{ projects: Array<{ id: string; name: string }> }>;
  });
  const { mutate: mutateCache } = useSWRConfig();

  const app = appData?.app;
  const targets = useMemo(() => context?.targets || context?.app?.targets || app?.targets || [], [context?.targets, context?.app?.targets, app?.targets]);
  const permission = context?.permission || app?.permission;
  const projectBinding = projectAppsData?.apps?.find((binding) => binding.app_id === appId);
  const isInstalledBinding = Boolean(projectId && projectAppsData && projectBinding?.binding_mode === "installed");
  const canEdit = !isInstalledBinding && permissionAtLeast(permission, "developer");
  const canRun = permissionAtLeast(permission, "runner");
  const canRelease = !isInstalledBinding && permissionAtLeast(permission, "maintainer");
  const canAdmin = permissionAtLeast(permission, "admin");
  const canManageProjectBinding = projectCanWrite && canRun;
  const projectNames = useMemo(() => new Map((projectDirectory?.projects || []).map((project) => [project.id, project.name])), [projectDirectory?.projects]);
  const publishedReleases = useMemo(
    () => (releasesData?.releases || app?.releases || []).filter((release) => release.status === "published"),
    [app?.releases, releasesData?.releases],
  );
  const selectedFile = useMemo(() => filesData?.files?.find((file) => file.path === selectedPath), [filesData?.files, selectedPath]);

  useEffect(() => {
    const query = searchParams.toString();
    if (query === previousRouteQuery.current) return;
    previousRouteQuery.current = query;
    applyingRouteFromUrl.current = true;
    const route = resolveTab(searchParams.get("tab"));
    const requestedSection = searchParams.get("section");
    setTab(route.tab);
    if (route.tab === "development") {
      const nextSection = isDevelopmentSection(requestedSection) ? requestedSection : route.section;
      if (isDevelopmentSection(nextSection)) setDevelopmentSection(nextSection);
    }
    if (route.tab === "history") {
      const nextSection = isHistorySection(requestedSection) ? requestedSection : route.section;
      if (isHistorySection(nextSection)) setHistorySection(nextSection);
    }
  }, [searchParams]);

  useEffect(() => {
    const urlRoute = resolveTab(searchParams.get("tab"));
    const requestedSection = searchParams.get("section");
    const urlDevelopmentSection = isDevelopmentSection(requestedSection)
      ? requestedSection
      : urlRoute.tab === "development" && isDevelopmentSection(urlRoute.section)
        ? urlRoute.section
        : "chats";
    const urlHistorySection = isHistorySection(requestedSection)
      ? requestedSection
      : urlRoute.tab === "history" && isHistorySection(urlRoute.section)
        ? urlRoute.section
        : "jobs";
    const stateMatchesUrl = tab === urlRoute.tab
      && (tab !== "development" || developmentSection === urlDevelopmentSection)
      && (tab !== "history" || historySection === urlHistorySection);
    if (!stateMatchesUrl && applyingRouteFromUrl.current) return;
    if (stateMatchesUrl) applyingRouteFromUrl.current = false;
    const params = new URLSearchParams(searchParams.toString());
    params.set("tab", tab);
    if (tab === "development") params.set("section", developmentSection);
    else if (tab === "history") params.set("section", historySection);
    else params.delete("section");
    const nextQuery = params.toString();
    if (nextQuery !== searchParams.toString() && pathname) {
      router.replace(`${pathname}?${nextQuery}`, { scroll: false });
    }
  }, [developmentSection, historySection, pathname, router, searchParams, tab]);

  useEffect(() => {
    setSelectedPath(null);
    setFileContent("");
    setFileSha(undefined);
    setSelectedDocPath("README.md");
    setDocContent("");
    setDocSha(undefined);
    setReadmeContent(null);
    setSelectedTarget("");
  }, [appId]);

  useEffect(() => {
    if (!targets.length) {
      if (selectedTarget) setSelectedTarget("");
      return;
    }
    // Manifest再同期でTargetが削除・改名されても、古いkeyを操作対象に
    // 残さない。現在値が有効ならそのまま維持し、無効になった場合だけ
    // context/default/先頭の優先順で安全に選び直す。
    if (selectedTarget && targets.some((target) => target.target_key === selectedTarget)) return;
    const preferredKey = context?.target_key || app?.default_target_key;
    const preferred = preferredKey ? targets.find((target) => target.target_key === preferredKey) : undefined;
    const nextTarget = preferred?.target_key || targets[0].target_key;
    if (nextTarget !== selectedTarget) setSelectedTarget(nextTarget);
  }, [app?.default_target_key, context?.target_key, selectedTarget, targets]);

  useEffect(() => {
    if (!selectedPath || !appId) return;
    let cancelled = false;
    void appsApi.getFile(appId, selectedPath, projectId || undefined).then((result) => {
      if (!cancelled) {
        setFileContent(result.content);
        setFileSha(result.sha256);
      }
    }).catch((error) => toast.error(error instanceof Error ? error.message : "ファイルを読み込めませんでした"));
    return () => { cancelled = true; };
  }, [appId, projectId, selectedPath]);

  useEffect(() => {
    if (context?.app?.id === appId && context.readme !== undefined && readmeContent === null) setReadmeContent(context.readme);
  }, [appId, context?.app?.id, context?.readme, readmeContent]);

  useEffect(() => {
    if (!appId || !selectedDocPath) return;
    if (selectedDocPath.toLowerCase() === "readme.md") {
      let cancelled = false;
      void appsApi.getFile(appId, "README.md", projectId || undefined).then((result) => {
        if (!cancelled) {
          setDocContent(result.content);
          setDocSha(result.sha256);
          setReadmeContent(result.content);
        }
      }).catch(() => {
        if (!cancelled) {
          const value = context?.app?.id === appId ? context.readme ?? readmeContent ?? "" : readmeContent ?? "";
          setDocContent(value);
          setDocSha(undefined);
        }
      });
      return () => { cancelled = true; };
    }
    let cancelled = false;
    void appsApi.getFile(appId, selectedDocPath, projectId || undefined).then((result) => {
      if (!cancelled) {
        setDocContent(result.content);
        setDocSha(result.sha256);
      }
    }).catch((error) => {
      if (!cancelled) {
        setDocContent("");
        setDocSha(undefined);
        toast.error(error instanceof Error ? error.message : "Docsを読み込めませんでした");
      }
    });
    return () => { cancelled = true; };
  }, [appId, context?.app?.id, context?.readme, projectId, readmeContent, selectedDocPath]);

  useEffect(() => {
    if (filesError || !filesData) return;
    const docs = (filesData.files || []).filter((file) => isMarkdownAppFile(file));
    const paths = new Set(docs.map((file) => String(file.path || file.filename || "")));
    if (paths.has(selectedDocPath)) return;
    const nextPath = docs.find((file) => String(file.path || file.filename || "").toLowerCase() === "readme.md")
      ? "README.md"
      : String(docs[0]?.path || docs[0]?.filename || "README.md");
    setSelectedDocPath(nextPath);
    setDocContent("");
    setDocSha(undefined);
  }, [filesData, filesError, selectedDocPath]);

  if (appError || contextError) {
    return <div className="m-6 rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">Appを読み込めませんでした: {(appError || contextError)?.message}</div>;
  }
  if (!app) return <div className="p-6 text-sm text-muted-foreground">Appを読み込み中…</div>;

  const runJob = async (jobType: "build" | "test" | "run" | "package") => {
    if (busyJob) return;
    if (!selectedTarget) {
      toast.error("Targetを選択してください");
      return;
    }
    if (jobType === "run" && !canExecuteTarget(targets.find((target) => target.target_key === selectedTarget))) {
      toast.error("このTargetはAoiTalkから実行できません。利用タブから成果物を取得してください");
      return;
    }
    setBusyJob(jobType);
    try {
      await appsApi.createJob(app.id, { target_key: selectedTarget, job_type: jobType, project_id: projectId || null });
      toast.success(`${jobType} を開始しました`);
      await mutateJobs();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Jobを開始できませんでした");
    } finally {
      setBusyJob(null);
    }
  };

  const saveFile = async (path: string, content: string, expectedSha?: string): Promise<string | undefined> => {
    setSaving(true);
    try {
      const result = await appsApi.writeFile(app.id, { path, content, expected_sha256: expectedSha }, projectId || undefined);
      setFileSha(result.sha256);
      if (path.toLowerCase() === "readme.md") setReadmeContent(content);
      toast.success(`${path} を保存しました`);
      await Promise.all([mutateFiles(), mutateApp(), path.toLowerCase() === "readme.md" ? mutateContext() : Promise.resolve()]);
      return result.sha256;
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "ファイルを保存できませんでした");
      return undefined;
    } finally {
      setSaving(false);
    }
  };

  const refreshAfterSourceUpdate = async () => {
    setSelectedPath(null);
    setFileContent("");
    setFileSha(undefined);
    setReadmeContent(null);
    setDocContent("");
    setDocSha(undefined);
    await Promise.all([mutateFiles(), mutateApp(), mutateContext(), mutateStatus(), mutateHistory()]);
  };

  const createRelease = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!releaseVersion.trim()) return;
    try {
      await appsApi.createRelease(app.id, { version: releaseVersion.trim(), changelog: releaseNotes.trim() }, projectId || undefined);
      toast.success("リリースを作成しました");
      setReleaseVersion("");
      setReleaseNotes("");
      await mutateReleases();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "リリースを作成できませんでした");
    }
  };

  const saveReadme = async (content: string = readmeContent ?? "") => {
    setSaving(true);
    try {
      const result = await appsApi.updateReadme(app.id, content, docSha, projectId || undefined);
      setReadmeContent(content);
      setDocContent(content);
      setDocSha(result.sha256);
      toast.success("READMEとDocsを保存しました");
      await Promise.all([mutateApp(), mutateFiles(), mutateContext()]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "READMEを保存できませんでした");
    } finally {
      setSaving(false);
    }
  };

  const saveDoc = async () => {
    if (selectedDocPath.toLowerCase() === "readme.md") {
      await saveReadme(docContent);
      return;
    }
    const nextSha = await saveFile(selectedDocPath, docContent, docSha);
    if (nextSha) setDocSha(nextSha);
  };

  const reanalyzeApp = async () => {
    setAnalysisBusy(true);
    try {
      const result = await appsApi.analyze(
        app.id,
        context?.manifest_hash ? { expected_manifest_sha256: context.manifest_hash } : {},
        projectId || undefined,
      );
      setReadmeContent(result.readme);
      toast.success("業務内容を分析して概要UIを更新しました");
      await Promise.all([mutateApp(), mutateContext(), mutateFiles()]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "業務内容を分析できませんでした");
    } finally {
      setAnalysisBusy(false);
    }
  };

  const toggleProjectBinding = async () => {
    if (!projectId || !canManageProjectBinding) return;
    setBindingBusy(true);
    try {
      if (projectBinding) {
        await appsApi.unlinkProjectApp(projectId, appId);
        toast.success("ProjectからAppを外しました");
      } else {
        await appsApi.linkProjectApp(projectId, { app_id: appId, binding_mode: "development" });
        toast.success("ProjectでAppを使えるようにしました");
      }
      await mutateProjectApps();
      await mutateCache(`/apps/workspace?project_id=${encodeURIComponent(projectId)}`);
      await mutateApp();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Projectとの関連を更新できませんでした");
    } finally {
      setBindingBusy(false);
    }
  };

  const updateProjectBinding = async (input: Partial<ProjectAppInput>) => {
    if (!projectId || !projectBinding || !canManageProjectBinding) return;
    setBindingBusy(true);
    try {
      await appsApi.updateProjectApp(projectId, appId, input);
      toast.success("ProjectでのAppの使い方を更新しました");
      await Promise.all([
        mutateProjectApps(),
        mutateApp(),
        mutateCache(`/apps/workspace?project_id=${encodeURIComponent(projectId)}`),
      ]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "ProjectでのApp設定を更新できませんでした");
    } finally {
      setBindingBusy(false);
    }
  };

  const changeBindingMode = async (value: string) => {
    if (value === "installed") {
      const releaseId = projectBinding?.installed_release_id || publishedReleases[0]?.id;
      if (!releaseId) {
        toast.error("選択できるリリースがありません");
        return;
      }
      await updateProjectBinding({ binding_mode: "installed", installed_release_id: releaseId });
      return;
    }
    await updateProjectBinding({ binding_mode: "development", installed_release_id: null });
  };

  const developmentTarget = targets.find((target) => target.target_key === app.default_target_key) || targets[0];
  const appIdentity = getAppVisualIdentity(app);
  const chatHref = developmentTarget
    ? `/chat?app_id=${encodeURIComponent(app.id)}&app_target_id=${encodeURIComponent(developmentTarget.id)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`
    : `/chat?app_id=${encodeURIComponent(app.id)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`;

  const latestJob = [...(jobsData?.jobs || [])].sort((left, right) => new Date(right.started_at || 0).getTime() - new Date(left.started_at || 0).getTime())[0];
  const latestRelease = [...(releasesData?.releases || app.releases || [])].filter((release) => release.status === "published").sort((left, right) => new Date(right.created_at || 0).getTime() - new Date(left.created_at || 0).getTime())[0];

  return (
    <div className={`apps-detail-page grid min-h-full min-w-0 w-full grid-cols-1 overflow-x-hidden bg-background ${embedded ? "" : "lg:grid-cols-[minmax(0,1fr)_minmax(240px,300px)]"}`}>
      <div className="min-w-0 space-y-4 p-4 md:p-6">
        <header className="flex min-w-0 max-w-full flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 flex-1 items-start gap-3">
            <div
              className={`flex size-11 shrink-0 items-center justify-center rounded-md border ${appIdentity.detailClass}`}
              data-app-id={app.id}
              data-app-identity-kind={appIdentity.kind}
              data-app-identity-palette={appIdentity.paletteKey}
            >
              <AppIdentityIcon app={app} variant="detail" className="size-5.5" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="truncate text-xl font-semibold leading-7 tracking-tight text-foreground">{app.name}</h1>
                <Badge variant="outline" className="rounded-full border-primary/35 px-1.5 py-0 text-[10px] uppercase tracking-wide text-primary">{app.visibility}</Badge>
              </div>
              <p className="mt-1 max-w-3xl overflow-hidden text-sm leading-5 text-muted-foreground" style={{ display: "-webkit-box", WebkitBoxOrient: "vertical", WebkitLineClamp: 2 }}>{app.description || "このAppの説明はまだありません。"}</p>
            </div>
          </div>
          <div className="flex max-w-full shrink-0 flex-wrap items-center gap-2">
            <Button size="sm" className="h-8 px-3" onClick={() => setTab("use")}>
              <CirclePlay className="size-3.5" /> 利用する
            </Button>
            {canRun && <AppArchiveDownloadDialog appId={app.id} appName={app.name} projectId={projectId || undefined} files={filesData?.files || []} filesLoading={filesLoading} />}
            <Button size="sm" variant="outline" className="h-8 px-3" render={<Link href={chatHref} />} nativeButton={false}>
              <MessageSquare className="size-3.5" /> Chatで開発
            </Button>
          </div>
        </header>

        <div className="flex gap-5 overflow-x-auto border-b border-border/80" role="tablist" aria-label="App詳細">
          {TABS.map((item) => <button key={item.key} id={`app-tab-${item.key}`} type="button" role="tab" aria-controls={`app-tabpanel-${item.key}`} aria-selected={tab === item.key} onClick={() => setTab(item.key)} className={`shrink-0 border-b-2 px-0 pb-2.5 pt-1 text-xs font-medium transition-colors ${tab === item.key ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground"}`}>{item.label}</button>)}
        </div>

        <div role="tabpanel" id={`app-tabpanel-${tab}`} aria-labelledby={`app-tab-${tab}`} className="min-w-0">
          {tab === "overview" && <OverviewTab app={app} targets={targets} context={context} status={status} selectedRelease={context?.selected_release} jobs={jobsData?.jobs || []} jobsLoading={jobsLoading} jobsError={jobsError} releases={releasesData?.releases || app.releases || []} releasesLoading={releasesLoading} releasesError={releasesError} projectNames={projectNames} openDocs={() => { setTab("development"); setDevelopmentSection("docs"); }} openJobs={() => { setTab("history"); setHistorySection("jobs"); }} openReleases={() => { setTab("history"); setHistorySection("releases"); }} openDevelopment={() => { setTab("development"); setDevelopmentSection("operations"); }} canEdit={canEdit} analysisBusy={analysisBusy} onReanalyze={() => void reanalyzeApp()} />}
          {tab === "use" && <UseTab app={app} targets={targets} projectId={projectId} canRun={canRun} files={filesData?.files || []} filesLoading={filesLoading} />}
          {tab === "development" && <DevelopmentWorkspace section={developmentSection} onSectionChange={setDevelopmentSection} app={app} appId={app.id} projectId={projectId} targets={targets} context={context} status={status} statusError={statusError} history={historyData?.history || []} historyError={historyError} historyLoading={historyLoading} sessions={developmentChatsData?.conversations || []} sessionsError={developmentChatsError} sessionsLoading={developmentChatsLoading} selectedTarget={selectedTarget} setSelectedTarget={setSelectedTarget} canEdit={canEdit} canRun={canRun} canRelease={canRelease} busyJob={busyJob} runJob={runJob} mutateSessions={mutateDevelopmentChats} mutateStatus={mutateStatus} mutateHistory={mutateHistory} files={filesData?.files || []} filesError={filesError} filesLoading={filesLoading} selectedPath={selectedPath} setSelectedPath={setSelectedPath} selectedFile={selectedFile} fileContent={fileContent} setFileContent={setFileContent} fileSha={fileSha} saving={saving} saveFile={saveFile} onSourceUpdated={refreshAfterSourceUpdate} docsFiles={(filesData?.files || []).filter((file) => isMarkdownAppFile(file))} selectedDocPath={selectedDocPath} setSelectedDocPath={setSelectedDocPath} docContent={docContent} setDocContent={setDocContent} saveDoc={saveDoc} />}
          {tab === "history" && <HistoryWorkspace section={historySection} onSectionChange={setHistorySection} appId={app.id} projectId={projectId} targets={targets} jobs={jobsData?.jobs || []} jobsLoading={jobsLoading} jobsError={jobsError} canRun={canRun} selectedTarget={selectedTarget} runJob={runJob} busyJob={busyJob} mutateJobs={mutateJobs} releases={releasesData?.releases || app.releases || []} releasesLoading={releasesLoading} releasesError={releasesError} canRelease={canRelease} version={releaseVersion} setVersion={setReleaseVersion} notes={releaseNotes} setNotes={setReleaseNotes} createRelease={createRelease} canEdit={canEdit} status={status} statusError={statusError} history={historyData?.history || []} historyError={historyError} historyLoading={historyLoading} mutateStatus={mutateStatus} mutateHistory={mutateHistory} />}
          {tab === "settings" && <SettingsTab app={app} canEdit={canAdmin} mutate={mutateApp} projectId={projectId} projectBinding={projectBinding} publishedReleases={publishedReleases} canRun={canRun} projectCanWrite={projectCanWrite} bindingBusy={bindingBusy} onToggleProjectBinding={toggleProjectBinding} onChangeBindingMode={changeBindingMode} onUpdateProjectBinding={updateProjectBinding} />}
        </div>
      </div>
      {!embedded && <AppContextRail app={app} targets={targets} permission={permission} projectBinding={projectBinding} projectNames={projectNames} latestJob={latestJob} latestRelease={latestRelease} tab={tab} selectedTarget={targets.find((target) => target.target_key === selectedTarget)} />}
    </div>
  );
}

function OverviewTab({
  app,
  targets,
  context,
  status,
  selectedRelease,
  jobs,
  jobsLoading,
  jobsError,
  releases,
  releasesLoading,
  releasesError,
  projectNames,
  openDocs,
  openJobs,
  openReleases,
  openDevelopment,
  canEdit,
  analysisBusy,
  onReanalyze,
}: {
  app: AppSummary;
  targets: AppTarget[];
  context?: AppContext;
  status?: AppGitStatus;
  selectedRelease?: AppRelease | null;
  jobs: AppJob[];
  jobsLoading: boolean;
  jobsError?: Error;
  releases: AppRelease[];
  releasesLoading: boolean;
  releasesError?: Error;
  projectNames: Map<string, string>;
  openDocs: () => void;
  openJobs: () => void;
  openReleases: () => void;
  openDevelopment: () => void;
  canEdit: boolean;
  analysisBusy: boolean;
  onReanalyze: () => void;
}) {
  const latestJob = [...jobs].sort((left, right) => new Date(right.started_at || 0).getTime() - new Date(left.started_at || 0).getTime())[0];
   const latestRelease = releases
     .filter((release) => release.status === "published")
     .sort((left, right) => new Date(right.created_at || 0).getTime() - new Date(left.created_at || 0).getTime())[0];
  const displayRelease = context?.binding_mode === "installed" ? selectedRelease : latestRelease;
  const readme = context?.readme || "";
  const readmeLines = readmeSummary(readme);
  const primaryTarget = targets.find((target) => target.target_key === app.default_target_key) || targets[0];
  const targetSnapshot = primaryTarget?.manifest_snapshot || {};
  const manifestOverview = context?.manifest?.overview;
  const overview = manifestOverview && typeof manifestOverview === "object" && !Array.isArray(manifestOverview)
    ? manifestOverview as AppOverviewAnalysis
    : undefined;
  const targetGuides = targets
    .map((target) => ({ target, guide: overview?.targets?.[target.target_key] }))
    .filter(({ guide }) => Boolean(guide));
  const snapshotText = (key: string, fallback: string) => {
    const value = targetSnapshot[key];
    return typeof value === "string" && value.trim() ? value : fallback;
  };
  const pairText = (pair: keyof Pick<AppOverviewAnalysis, "input" | "process" | "output">, key: "label" | "detail", fallback: string) => {
    const value = overview?.[pair];
    return value && typeof value === "object" && typeof value[key] === "string" && value[key]?.trim()
      ? value[key]
      : fallback;
  };
  const inputLabel = pairText("input", "label", snapshotText("input_label", "入力データ"));
  const inputDetail = snapshotText(
    "input_detail",
    primaryTarget?.entrypoint ? `${primaryTarget.entrypoint} を入力として利用` : "入力データを利用",
  );
  const inputOverviewDetail = pairText("input", "detail", inputDetail);
  const processLabel = pairText("process", "label", snapshotText("process_label", primaryTarget?.display_name || "Appの処理"));
  const processDetail = pairText("process", "detail", snapshotText("process_detail", "Targetの処理を実行"));
  const outputLabel = pairText("output", "label", snapshotText("output_label", "成果物"));
  const outputDetail = pairText("output", "detail", snapshotText("output_detail", "処理結果を確認"));
  const usageStepSource = Array.isArray(overview?.steps) && overview.steps.length
    ? overview.steps.filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
    : readmeLines;
   const overviewUsageSteps = usageStepSource.slice(0, 4);
   const hiddenUsageStepCount = Math.max(0, usageStepSource.length - overviewUsageSteps.length);
   const inputIcon = <OverviewDiagramIcon target={primaryTarget} role="input" label={inputLabel} detail={inputOverviewDetail} />;
   const processIcon = <OverviewDiagramIcon target={primaryTarget} role="process" label={processLabel} detail={processDetail} />;
   const outputIcon = <OverviewDiagramIcon target={primaryTarget} role="output" label={outputLabel} detail={outputDetail} />;
  const analysisMethod = overview?.method === "starter" || !overview?.method
    ? "未分析"
    : overview.method === "llm" ? "業務内容を分析済み" : "ソースから分析済み";
  const confidence = typeof overview?.confidence === "number" ? `${Math.round(overview.confidence * 100)}%` : null;
   const evidenceFiles = Array.isArray(overview?.evidence_files) ? overview.evidence_files.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
   const limitations = Array.isArray(overview?.limitations) ? overview.limitations.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
   return (
     <div className="space-y-4">
       <div className="grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(260px,.75fr)]">
         <Card className="overflow-hidden border-border/80 bg-card/80">
           <CardHeader className="border-b border-border/70">
             <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
               <div>
                 <CardTitle className="text-base">Target Environments</CardTitle>
                 <p className="mt-1 text-xs text-muted-foreground">このAppを実行・利用できる実在のTarget</p>
               </div>
               <div className="flex items-center gap-2">
                 <span className="rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-[10px] text-primary">{targets.length} targets</span>
                 {canEdit && <Button type="button" size="icon-sm" variant="ghost" aria-label="Targetを編集" title="Targetを編集" onClick={openDevelopment}><Settings2 className="size-3.5" /></Button>}
               </div>
             </div>
           </CardHeader>
           <CardContent className="p-4">
             {targets.length ? <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{targets.map((target) => (
               <div key={target.id} className="group flex min-w-0 flex-col rounded-md border border-border/80 bg-background/40 p-3 transition-colors hover:border-primary/45">
                 <div className="flex items-start gap-2.5">
                   <span className="flex size-9 shrink-0 items-center justify-center rounded border border-border/80 bg-muted/40 text-primary"><TargetIcon target={target} className="size-4.5" /></span>
                   <div className="min-w-0 flex-1">
                     <p className="truncate text-sm font-semibold" title={target.display_name}>{target.display_name}</p>
                     <p className="mt-0.5 truncate text-[11px] text-muted-foreground" title={target.target_key}>{target.target_key}</p>
                   </div>
                 </div>
                 <div className="mt-3 flex flex-wrap gap-1.5 text-[10px] text-muted-foreground">
                   <span className="rounded border border-border/70 px-1.5 py-0.5">{surfaceLabel(target.surface)}</span>
                   <span className="rounded border border-border/70 px-1.5 py-0.5">{runtimeLabel(target.runtime)}</span>
                 </div>
                 <p className="mt-2 truncate text-[11px] text-muted-foreground" title={target.entrypoint}>Host: {executionHostLabel(target.execution_host)} · {target.entrypoint || "entrypoint未設定"}</p>
               </div>
             ))}</div> : <EmptyHint icon={<Box className="size-3.5" />} title="Targetがまだ設定されていません" detail="Manifestを設定すると、利用できる場所と実行方法が表示されます。" action={canEdit ? { label: "開発タブで設定する", onClick: openDevelopment } : undefined} />}
           </CardContent>
         </Card>

         <Card className="border-border/80 bg-card/80">
           <CardHeader className="border-b border-border/70"><CardTitle className="text-base">Runtime status</CardTitle><p className="mt-1 text-xs text-muted-foreground">実際のJob・保存版から確認できる状態</p></CardHeader>
           <CardContent className="space-y-3 p-4">
             <ActualStatusRow icon={jobsError ? <CircleAlert className="size-4" /> : latestJob?.status === "failed" ? <CircleAlert className="size-4" /> : latestJob?.status === "succeeded" ? <CheckCircle2 className="size-4" /> : <History className="size-4" />} label="Latest job" value={jobsLoading ? "読み込み中…" : jobsError ? "取得できません" : latestJob ? `${jobTypeLabel(latestJob.job_type)} · ${readableStatus(latestJob.status)}` : "Jobなし"} detail={jobsError?.message || (latestJob ? formatDate(latestJob.started_at) : "実行履歴から確認できます")} tone={jobsError || latestJob?.status === "failed" ? "error" : latestJob?.status === "succeeded" ? "success" : "neutral"} />
             <ActualStatusRow icon={releasesError ? <CircleAlert className="size-4" /> : <Package className="size-4" />} label={context?.binding_mode === "installed" ? "Installed release" : "Latest release"} value={releasesLoading ? "読み込み中…" : releasesError ? "取得できません" : displayRelease?.version || "保存版なし"} detail={releasesError?.message || (displayRelease ? `${releaseStatusLabel(displayRelease.status)} · ${formatDate(displayRelease.created_at)}` : "履歴から保存版を作成できます")} tone={releasesError ? "error" : displayRelease ? "success" : "neutral"} />
             <ActualStatusRow icon={<GitBranch className="size-4" />} label="Git" value={status?.branch || "branch未取得"} detail={status?.revision ? `${status.clean === false ? "変更あり" : status.clean === true ? "変更なし" : "状態未取得"} · ${status.revision.slice(0, 8)}` : "Operationsから確認できます"} tone={status?.clean === false ? "error" : status?.clean === true ? "success" : "neutral"} />
             <div className="flex flex-wrap gap-3"><button type="button" onClick={openJobs} className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">Jobsを開く <ArrowRight className="size-3" /></button><button type="button" onClick={openReleases} className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">Releasesを開く <ArrowRight className="size-3" /></button></div>
           </CardContent>
         </Card>
       </div>

       <div className="grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(260px,.9fr)]">
         <Card className="border-border/80 bg-card/80">
           <CardHeader className="border-b border-border/70"><div className="flex flex-wrap items-center justify-between gap-2"><div><CardTitle className="text-base">このAppでできること</CardTitle><p className="mt-1 text-xs text-muted-foreground">README・Manifestを分析した実在の概要</p></div><div className="flex flex-wrap items-center gap-1.5"><span className={`rounded-full border px-2 py-0.5 text-[10px] ${overview?.method && overview.method !== "starter" ? "border-success/35 bg-success/10 text-success" : "border-warning/35 bg-warning/10 text-warning"}`}>分析: {analysisMethod}</span>{confidence && <span className="rounded-full border border-border/80 px-2 py-0.5 text-[10px] text-muted-foreground">確度 {confidence}</span>}{canEdit && <Button type="button" size="sm" variant="ghost" className="h-6 px-1.5 text-[11px]" onClick={onReanalyze} disabled={analysisBusy}><RefreshCw className={`size-3 ${analysisBusy ? "animate-spin" : ""}`} /> 再分析</Button>}</div></div></CardHeader>
           <CardContent className="space-y-3 p-4">
             <p className="max-w-3xl text-sm leading-5 text-muted-foreground">{overview?.purpose || "入力から成果物の作成までを、ここから確認・実行できます。"}</p>
             {targetGuides.length > 0 ? <div className="grid min-w-0 gap-2.5 sm:grid-cols-2">{targetGuides.map(({ target, guide }) => <TargetGuideCard key={target.id} target={target} guide={guide} />)}</div> : <div className="flex min-w-0 items-stretch gap-1.5 rounded-md border border-border/70 bg-background/30 p-3"><FlowStep stage="入力" tone="input" icon={inputIcon} label={inputLabel} detail={inputOverviewDetail} /><ArrowRight className="apps-flow-arrow mx-auto size-4 shrink-0 self-center text-muted-foreground" /><FlowStep stage="処理" tone="process" icon={processIcon} label={processLabel} detail={processDetail} /><ArrowRight className="apps-flow-arrow mx-auto size-4 shrink-0 self-center text-muted-foreground" /><FlowStep stage="出力" tone="output" icon={outputIcon} label={outputLabel} detail={outputDetail} /></div>}
             {(evidenceFiles.length > 0 || limitations.length > 0) && <div className="grid gap-2 md:grid-cols-2">{evidenceFiles.length > 0 && <div className="min-w-0 rounded-md border border-border/70 bg-background/30 px-2.5 py-2"><p className="text-[11px] font-semibold">分析の根拠</p><div className="mt-1 flex flex-wrap gap-1">{evidenceFiles.slice(0, 6).map((file) => <span key={file} title={file} className="max-w-full truncate rounded border border-border/70 px-1.5 py-0.5 text-[10px] leading-4 text-muted-foreground">{file}</span>)}</div></div>}{limitations.length > 0 && <div className="min-w-0 rounded-md border border-warning/25 bg-warning/5 px-2.5 py-2"><p className="text-[11px] font-semibold text-warning">確認が必要な点</p><ul className="mt-1 space-y-0.5 text-[11px] leading-4 text-muted-foreground">{limitations.slice(0, 3).map((item) => <li key={item} className="line-clamp-2">{item}</li>)}</ul>{limitations.length > 3 && <p className="mt-0.5 text-[10px] text-muted-foreground">ほか{limitations.length - 3}件</p>}</div>}</div>}
           </CardContent>
         </Card>

         <Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><FolderOpen className="size-4 text-primary" /> 関連Project</CardTitle><p className="mt-1 text-xs text-muted-foreground">このAppを利用するProject</p></CardHeader><CardContent className="p-4">{app.related_project_ids?.length ? <div className="space-y-1.5">{app.related_project_ids.map((relatedProjectId) => <Link key={relatedProjectId} href={`/projects?project_id=${encodeURIComponent(relatedProjectId)}&app_id=${encodeURIComponent(app.id)}`} className="flex items-center gap-2 rounded-md border border-border/70 bg-background/30 px-2.5 py-2 text-xs font-medium transition-colors hover:border-primary/50 hover:bg-muted/40"><FolderOpen className="size-3.5 text-primary" /><span className="truncate">{projectNames.get(relatedProjectId) || relatedProjectId.slice(0, 8)}</span><ArrowRight className="ml-auto size-3 text-muted-foreground" /></Link>)}</div> : <EmptyHint icon={<FolderOpen className="size-3.5" />} title="関連Projectはありません" detail="ProjectからこのAppを追加すると表示されます。" />}</CardContent></Card>
       </div>

       <Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><div className="flex items-center justify-between gap-2"><div><CardTitle className="flex items-center gap-1.5 text-base"><BookOpen className="size-4 text-primary" /> 基本手順</CardTitle><p className="mt-1 text-xs text-muted-foreground">README / Docsに登録された実際の手順</p></div><button type="button" onClick={openDocs} className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">Docsを開く <ArrowRight className="size-3" /></button></div></CardHeader><CardContent className="apps-usage-grid grid gap-4 p-4"><div>{overviewUsageSteps.length ? <><ol aria-label="概要手順" className="space-y-1.5">{overviewUsageSteps.map((line, index) => <li key={`${line}-${index}`} className="flex items-start gap-2 rounded-md border border-border/70 bg-background/30 px-2.5 py-2"><span className="flex size-4 shrink-0 items-center justify-center rounded-full bg-primary/10 text-[10px] font-semibold text-primary">{index + 1}</span><span className="text-xs leading-4 text-muted-foreground">{line}</span></li>)}</ol>{hiddenUsageStepCount > 0 && <p className="mt-1.5 text-[11px] text-muted-foreground">詳細な手順があと{hiddenUsageStepCount}件あります。</p>}</> : <EmptyHint icon={<BookOpen className="size-3.5" />} title="使い方がまだ登録されていません" detail="READMEに手順を書くとここに表示されます。" action={{ label: "Docsで追加する", onClick: openDocs }} />}</div><div className="apps-usage-visual items-center justify-end gap-2"><UsageDiagramStep icon={inputIcon} label={inputLabel} detail={inputOverviewDetail} tone="input" /><span className="h-px w-6 shrink-0 border-t-2 border-dotted border-muted-foreground/50" /><UsageDiagramStep icon={processIcon} label={processLabel} detail={processDetail} tone="process" /><span className="h-px w-6 shrink-0 border-t-2 border-dotted border-muted-foreground/50" /><UsageDiagramStep icon={outputIcon} label={outputLabel} detail={outputDetail} tone="output" /></div></CardContent></Card>
     </div>
   );
 }

function ActualStatusRow({ icon, label, value, detail, tone }: { icon: React.ReactNode; label: string; value: string; detail: string; tone: "success" | "error" | "neutral" }) {
  const toneClass = tone === "success" ? "border-success/25 bg-success/5 text-success" : tone === "error" ? "border-destructive/30 bg-destructive/5 text-destructive" : "border-border/70 bg-background/30 text-muted-foreground";
  return <div className={`flex items-start gap-2.5 rounded-md border px-2.5 py-2 ${toneClass}`}><span className="mt-0.5 shrink-0">{icon}</span><div className="min-w-0"><p className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</p><p className="truncate text-sm font-medium text-foreground" title={value}>{value}</p><p className="truncate text-[11px] text-muted-foreground" title={detail}>{detail}</p></div></div>;
}

function AppContextRail({ app, targets, permission, projectBinding, projectNames, latestJob, latestRelease, tab, selectedTarget }: { app: AppSummary; targets: AppTarget[]; permission?: string; projectBinding?: ProjectAppBinding; projectNames: Map<string, string>; latestJob?: AppJob; latestRelease?: AppRelease; tab: DetailTab; selectedTarget?: AppTarget }) {
  const permissionLabel = permission || app.permission || "viewer";
  const runtimeLabelValue = latestJob ? `${jobTypeLabel(latestJob.job_type)} · ${readableStatus(latestJob.status)}` : latestRelease ? `保存版 ${latestRelease.version}` : targets.length ? `${targets.length} targets` : "未設定";
  const isTargetContext = tab === "use" || tab === "development";
  return <aside className="hidden min-w-0 border-border/80 bg-card/45 2xl:sticky 2xl:top-0 2xl:block 2xl:h-full 2xl:border-l"><div className="flex items-center gap-2 border-b border-border/80 px-4 py-3"><span className="flex size-6 items-center justify-center rounded border border-primary/30 bg-primary/10 text-primary">{isTargetContext && selectedTarget ? <TargetIcon target={selectedTarget} className="size-3.5" /> : <Settings2 className="size-3.5" />}</span><h2 className="text-base font-semibold">{isTargetContext ? "Target Context" : "App Properties"}</h2></div><div className="space-y-5 overflow-y-auto p-4">
    <div><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{isTargetContext ? "Active target" : "Status"}</p><div className="mt-2 rounded-md border border-border/70 bg-background/35 px-2.5 py-2">{isTargetContext && selectedTarget ? <><p className="truncate text-sm font-medium" title={selectedTarget.display_name}>{selectedTarget.display_name}</p><p className="mt-1 truncate text-[11px] text-muted-foreground">{runtimeLabel(selectedTarget.runtime)} · {executionHostLabel(selectedTarget.execution_host)}</p></> : <div className="flex items-center gap-2"><span className={`size-2 rounded-full ${latestJob?.status === "failed" ? "bg-destructive" : latestJob?.status === "running" || latestJob?.status === "queued" ? "bg-warning" : latestJob || latestRelease ? "bg-primary" : "bg-muted-foreground"}`} /><span className="text-sm">{runtimeLabelValue}</span></div>}</div></div>
    <div className="space-y-4"><div className="border-b border-border/70 pb-3"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Identifier</p><p className="mt-1 break-all font-mono text-xs text-foreground">{app.slug || app.id}</p></div><div className="border-b border-border/70 pb-3"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Permission</p><div className="mt-1 flex items-center gap-2 text-sm"><span className="flex size-5 items-center justify-center rounded-full bg-primary/10 text-primary"><Check className="size-3" /></span>{permissionLabel}</div></div><div className="border-b border-border/70 pb-3"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Binding</p><p className="mt-1 text-sm">{projectBinding ? projectBinding.binding_mode === "installed" ? `固定版 ${projectBinding.installed_release_id ? "選択中" : "未選択"}` : "main（最新）" : "Project未選択"}</p></div><div className="border-b border-border/70 pb-3"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Owner</p><p className="mt-1 break-all text-xs text-muted-foreground">{app.owner_user_id || "未設定"}</p></div></div>
    <div><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Project links</p>{app.related_project_ids?.length ? <div className="mt-2 space-y-1.5">{app.related_project_ids.map((id) => <Link key={id} href={`/projects?project_id=${encodeURIComponent(id)}&app_id=${encodeURIComponent(app.id)}`} className="flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs text-primary hover:bg-muted/50"><FolderOpen className="size-3" /><span className="truncate">{projectNames.get(id) || id.slice(0, 8)}</span></Link>)}</div> : <p className="mt-2 text-xs text-muted-foreground">関連Projectはありません。</p>}</div>
  </div></aside>;
}

/** 空状態を1〜2行に収めつつ、次の行動が分かるようにするApps共通の空表示。 */
function EmptyHint({ icon, title, detail, action }: { icon: React.ReactNode; title: string; detail: string; action?: { label: string; onClick: () => void } }) {
  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-dashed bg-muted/10 px-2.5 py-2">
      <span className="mt-px flex size-6 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground">{icon}</span>
      <div className="min-w-0">
        <p className="text-xs font-medium leading-4">{title}</p>
        <p className="text-[11px] leading-4 text-muted-foreground">{detail}</p>
        {action && <button type="button" onClick={action.onClick} className="mt-1 inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline">{action.label} <ArrowRight className="size-3" /></button>}
      </div>
    </div>
  );
}

function UsageDiagramStep({ icon, label, detail, tone }: { icon: React.ReactNode; label: string; detail: string; tone: "input" | "process" | "output" }) {
  const toneClass = tone === "input" ? "border bg-background text-info" : tone === "process" ? "rounded-full border bg-primary/10 text-primary" : "border-success/40 bg-success/10 text-success";
  return <div className="flex min-w-0 flex-1 flex-col items-center text-center"><span className={`flex size-13 shrink-0 items-center justify-center rounded-xl ${toneClass}`}>{icon}</span><span className="mt-1.5 max-w-40 overflow-hidden text-[11px] font-semibold leading-4" style={{ display: "-webkit-box", WebkitBoxOrient: "vertical", WebkitLineClamp: 2 }}>{label}</span><span className="max-w-44 overflow-hidden text-[11px] leading-4 text-muted-foreground" style={{ display: "-webkit-box", WebkitBoxOrient: "vertical", WebkitLineClamp: 2 }}>{detail}</span></div>;
}

function FlowStep({ stage, icon, label, detail, tone }: { stage: string; icon: React.ReactNode; label: string; detail: string; tone: "input" | "process" | "output" }) {
  const toneClass = tone === "input" ? "border border-info/40 bg-info/10 text-info" : tone === "process" ? "border border-primary/40 bg-primary/10 text-primary" : "border border-success/40 bg-success/10 text-success";
  return <div aria-label={`${stage}: ${label}. ${detail}`} className="flex min-w-0 flex-1 flex-col items-center justify-start p-1.5 text-center"><span className={`flex size-14 shrink-0 items-center justify-center rounded-xl ${toneClass}`}>{icon}</span><span className="mt-1.5 min-w-0 max-w-full"><span className="block max-w-[14rem] truncate text-xs font-semibold leading-4" title={label}>{label}</span><span className="block max-w-[16rem] overflow-hidden text-[11px] leading-4 text-muted-foreground" style={{ display: "-webkit-box", WebkitBoxOrient: "vertical", WebkitLineClamp: 2 }} title={detail}>{detail}</span></span></div>;
}

function TargetGuideCard({ target, guide }: { target: AppTarget; guide?: NonNullable<AppOverviewAnalysis["targets"]>[string] }) {
  const input = guide?.input;
  const output = guide?.output;
  const steps = (guide?.steps || []).filter((step): step is string => typeof step === "string" && Boolean(step.trim())).slice(0, 4);
  return <div className="min-w-0 rounded-lg border bg-muted/10 p-2.5">
    <div className="flex items-start gap-2.5"><span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary"><TargetIcon target={target} className="size-5" /></span><div className="min-w-0"><p className="truncate text-xs font-semibold leading-4" title={target.display_name}>{target.display_name}</p><p className="truncate text-[11px] leading-4 text-muted-foreground" title={target.target_key}>{target.target_key}</p></div></div>
    <p className="mt-2 line-clamp-2 text-[11px] leading-4 text-muted-foreground">{guide?.purpose || `${target.display_name} を実行します。`}</p>
    <div className="mt-2 grid gap-1.5 text-[11px] sm:grid-cols-2"><div className="rounded-md border bg-background/30 px-2 py-1.5"><p className="font-medium">入力</p><p className="line-clamp-2 leading-4 text-muted-foreground">{input?.label || target.entrypoint || "Manifestで定義された入力"}{input?.detail ? ` · ${input.detail}` : ""}</p></div><div className="rounded-md border bg-background/30 px-2 py-1.5"><p className="font-medium">出力</p><p className="line-clamp-2 leading-4 text-muted-foreground">{output?.label || "実行結果"}{output?.detail ? ` · ${output.detail}` : ""}</p></div></div>
    {steps.length ? <ol className="mt-2 space-y-1" aria-label={`${target.display_name} の手順`}>{steps.map((step, index) => <li key={`${step}-${index}`} className="flex items-start gap-1.5 text-[11px] leading-4 text-muted-foreground"><span className="flex size-4 shrink-0 items-center justify-center rounded-full bg-primary/10 text-[10px] font-semibold text-primary">{index + 1}</span><span className="line-clamp-2">{step}</span></li>)}</ol> : null}
  </div>;
}

type InputSchemaProperty = {
  type?: string;
  title?: string;
  description?: string;
  format?: string;
  enum?: unknown[];
};

type InputSchema = {
  type?: string;
  properties?: Record<string, InputSchemaProperty>;
  required?: string[];
};

function targetInputSchema(target: AppTarget): InputSchema | null {
  const value = target.manifest_snapshot?.input_schema;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as InputSchema
    : null;
}

function isMarkdownAppFile(file?: AppFile): boolean {
  const path = String(file?.path || file?.filename || "").replaceAll("\\", "/").toLowerCase();
  return Boolean(path && !path.startsWith(".agents/") && !path.includes("/.agents/") && path.endsWith(".md"));
}

function isTextAppFile(file?: AppFile): boolean {
  if (!file) return false;
  if (file.is_dir) return false;
  const path = String(file.filename || file.path).replaceAll("\\", "/");
  const name = path.split("/").pop()?.toLowerCase() || "";
  if ([".gitignore", "dockerfile", "makefile"].includes(name)) return true;
  const extension = String(file.extension || name.split(".").pop() || "").toLowerCase();
  return ["bas", "bat", "cfg", "cjs", "cmd", "cls", "conf", "css", "csv", "frm", "htm", "html", "ini", "js", "json", "jsx", "log", "mjs", "md", "py", "ps1", "sh", "sql", "toml", "ts", "tsv", "tsx", "txt", "vba", "xml", "yaml", "yml"].includes(extension);
}

function UseTab({ app, targets, projectId, canRun, files, filesLoading }: { app: AppSummary; targets: AppTarget[]; projectId: string; canRun: boolean; files: AppFile[]; filesLoading: boolean }) {
  const embeddedTargets = targets.filter((target) => target.surface === "embedded_web" && target.runtime === "static_web");
  const headlessTargets = targets.filter((target) => target.surface === "headless" || target.execution_host === "download_only");
  const runnableHeadlessTargets = headlessTargets.filter((target) => canExecuteTarget(target));
  const downloadableTargets = headlessTargets.filter((target) => !canExecuteTarget(target));
  const embeddedTargetIds = new Set(embeddedTargets.map((target) => target.id));
  const headlessTargetIds = new Set(headlessTargets.map((target) => target.id));
  const externalTargets = targets.filter((target) => !embeddedTargetIds.has(target.id) && !headlessTargetIds.has(target.id));
  const embedQuery = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return <div className="space-y-4">
    <div className="flex flex-wrap items-end justify-between gap-3"><div><h2 className="text-2xl font-semibold tracking-tight">Available Targets</h2><p className="mt-1 text-sm text-muted-foreground">{targets.length ? `Select a runtime environment or deployment target for ${app.name}.` : "このAppには利用可能なTargetがまだありません。"}</p></div>{canRun && <AppArchiveCard app={app} projectId={projectId} files={files} filesLoading={filesLoading} compact />}</div>
    <div className="rounded-md border border-warning/25 bg-warning/5 px-3 py-2.5"><div className="flex gap-2 text-xs leading-5 text-muted-foreground"><CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" /><p>利用可否はTargetの実際の実行環境、Manifest、現在の権限に基づきます。接続・到達性や利用者環境への反映はここでは保証しません。</p></div></div>
    {!targets.length && <EmptyHint icon={<Box className="size-3.5" />} title="利用できるTargetがありません" detail="開発タブでManifestとTargetを確認してください。" />}
    {embeddedTargets.map((embedded) => <Card key={embedded.id} className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><Code2 className="size-4 text-primary" /> AoiTalk内で開く · {embedded.display_name}</CardTitle><p className="mt-1 text-xs text-muted-foreground">{surfaceLabel(embedded.surface)} · {runtimeLabel(embedded.runtime)} · {executionHostLabel(embedded.execution_host)}</p></CardHeader><CardContent className="p-4"><iframe title={`${app.name} / ${embedded.target_key}`} src={`/api/python-proxy/apps/${encodeURIComponent(app.id)}/targets/${encodeURIComponent(embedded.target_key)}/embed${embedQuery}`} sandbox="allow-scripts allow-forms" referrerPolicy="no-referrer" className="h-[32rem] w-full rounded-md border border-border/70 bg-white" /></CardContent></Card>)}
    {(runnableHeadlessTargets.length + downloadableTargets.length + externalTargets.length) > 0 && <div className="grid gap-4 lg:grid-cols-2">{runnableHeadlessTargets.map((target) => <HeadlessRunCard key={target.id} app={app} target={target} projectId={projectId} canRun={canRun} />)}{downloadableTargets.map((target) => <DownloadTargetCard key={target.id} app={app} target={target} projectId={projectId} />)}{externalTargets.map((target) => <DownloadTargetCard key={target.id} app={app} target={target} projectId={projectId} />)}</div>}
  </div>;
}

function AppArchiveCard({ app, projectId, files, filesLoading, compact = false }: { app: AppSummary; projectId: string; files: AppFile[]; filesLoading: boolean; compact?: boolean }) {
  if (compact) return <AppArchiveDownloadDialog appId={app.id} appName={app.name} projectId={projectId || undefined} label="Download bundle" compact files={files} filesLoading={filesLoading} />;
  return <Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><Download className="size-4 text-primary" /> Appをダウンロード</CardTitle></CardHeader><CardContent className="p-4"><div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border/70 bg-background/30 px-3 py-2.5"><div className="min-w-0"><p className="text-sm font-medium leading-4">フォルダ構成のままzipで取得します</p><p className="mt-1 text-xs text-muted-foreground">保存済みの階層を取得します。範囲や除外はダウンロード設定から変更できます。</p></div><AppArchiveDownloadDialog appId={app.id} appName={app.name} projectId={projectId || undefined} label="ダウンロード" compact files={files} filesLoading={filesLoading} /></div></CardContent></Card>;
}

function DownloadTargetCard({ app, target, projectId }: { app: AppSummary; target: AppTarget; projectId: string }) {
  return <Card className="flex min-w-0 flex-col border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-2 text-base"><span className="flex size-9 items-center justify-center rounded border border-border/70 bg-muted/40 text-primary"><TargetIcon target={target} className="size-4" /></span><span className="min-w-0 truncate">{target.display_name}</span></CardTitle><div className="mt-2 flex flex-wrap gap-1.5"><span className="rounded border border-border/70 px-1.5 py-0.5 text-[10px] text-muted-foreground">{surfaceLabel(target.surface)}</span><span className="rounded border border-border/70 px-1.5 py-0.5 text-[10px] text-muted-foreground">{runtimeLabel(target.runtime)}</span><span className="rounded border border-border/70 px-1.5 py-0.5 text-[10px] text-muted-foreground">{executionHostLabel(target.execution_host)}</span></div></CardHeader><CardContent className="flex flex-1 flex-col justify-end gap-3 p-4"><p className="min-h-12 text-sm leading-5 text-muted-foreground">このTargetはAoiTalkから直接実行しません。利用者の環境で開きます。</p><div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border/70 bg-background/30 px-2.5 py-2"><p className="min-w-0 truncate text-[11px] text-muted-foreground" title={target.entrypoint}>Entrypoint: {target.entrypoint || "未設定"}</p>{target.entrypoint ? <a href={appsApi.downloadFile(app.id, target.entrypoint, projectId || undefined)} download className="inline-flex shrink-0 items-center gap-1 rounded border border-border/70 px-2 py-1 text-xs font-medium hover:border-primary/50 hover:bg-muted/40"><Download className="size-3.5" /> Download</a> : <span className="text-xs text-muted-foreground">ダウンロード対象なし</span>}</div></CardContent></Card>;
}

function HeadlessRunCard({ app, target, projectId, canRun }: { app: AppSummary; target: AppTarget; projectId: string; canRun: boolean }) {
  const schema = targetInputSchema(target);
  const [input, setInput] = useState<Record<string, string>>({});
  const [jsonInput, setJsonInput] = useState("{}");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const properties = Object.entries(schema?.properties || {});
  const run = async () => {
    setRunning(true);
    setError(null);
    try {
      const payload = properties.length ? input : JSON.parse(jsonInput || "{}");
      await appsApi.createJob(app.id, { target_key: target.target_key, job_type: "run", project_id: projectId || null, input_json: payload });
      toast.success(`${target.display_name} の実行を開始しました`);
    } catch (runError) {
      setError(runError instanceof Error ? runError.message : "入力値または実行内容が不正です");
    } finally {
      setRunning(false);
    }
  };
  return <Card className="flex min-w-0 flex-col border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-2 text-base"><span className="flex size-9 items-center justify-center rounded border border-primary/35 bg-primary/10 text-primary"><Play className="size-4" /></span><span className="min-w-0 truncate">{target.display_name} を実行</span></CardTitle><p className="mt-2 text-xs text-muted-foreground">{surfaceLabel(target.surface)} · {runtimeLabel(target.runtime)} · input_schemaに沿って入力</p></CardHeader><CardContent className="flex flex-1 flex-col gap-3 p-4">{properties.length ? <div className="grid gap-3 md:grid-cols-2">{properties.map(([name, property]) => <div key={name} className="space-y-1.5"><Label htmlFor={`app-input-${target.id}-${name}`}>{property.title || name}{schema?.required?.includes(name) ? " *" : ""}</Label>{property.enum?.length ? <AppSelect id={`app-input-${target.id}-${name}`} value={input[name] || ""} onChange={(event) => setInput((current) => ({ ...current, [name]: event.target.value }))} className="w-full"><option value="">選択してください</option>{property.enum.map((value) => <option key={String(value)} value={String(value)}>{String(value)}</option>)}</AppSelect> : <Input id={`app-input-${target.id}-${name}`} type={property.type === "number" || property.type === "integer" ? "number" : "text"} value={input[name] || ""} onChange={(event) => setInput((current) => ({ ...current, [name]: event.target.value }))} placeholder={property.format || property.type || "string"} />}{property.description && <p className="text-[11px] text-muted-foreground">{property.description}</p>}</div>)}</div> : <Textarea value={jsonInput} onChange={(event) => setJsonInput(event.target.value)} className="min-h-32 font-mono text-xs" aria-label={`${target.display_name} の入力JSON`} placeholder='{"input_file":"..."}' />}{error && <p className="text-xs text-destructive">{error}</p>}<Button className="mt-auto w-full" onClick={() => void run()} disabled={!canRun || running}>{running ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />} 実行</Button></CardContent></Card>;
}

function developmentStatusLabel(status?: ConversationSession["development_status"]): string {
  return {
    working: "作業中",
    waiting_for_user: "ユーザー確認待ち",
    completed: "完了",
  }[status || "working"] || "作業中";
}

function DevelopmentChatsCard({ app, projectId, targets, sessions, sessionsError, sessionsLoading, mutateSessions }: { app: AppSummary; projectId: string; targets: AppTarget[]; sessions: ConversationSession[]; sessionsError?: Error; sessionsLoading: boolean; mutateSessions: () => Promise<unknown> }) {
  const [busySessionId, setBusySessionId] = useState<string | null>(null);
  const setDevelopmentStatus = async (session: ConversationSession) => {
    const current = session.development_status || (session.is_active ? "working" : "completed");
    const next = current === "completed" ? "working" : "completed";
    setBusySessionId(session.id);
    try {
      await chatApi.updateSessionDevelopmentStatus(session.id, next);
      await mutateSessions();
      toast.success(next === "completed" ? "開発チャットを完了にしました" : "開発チャットを作業中に戻しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "開発チャットの状態を更新できませんでした");
    } finally {
      setBusySessionId(null);
    }
  };
  const defaultTarget = targets.find((target) => target.target_key === app.default_target_key) || targets[0];
  const defaultTargetId = defaultTarget && /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(defaultTarget.id) ? defaultTarget.id : "";
  const newChatHref = `/chat?app_id=${encodeURIComponent(app.id)}${defaultTargetId ? `&app_target_id=${encodeURIComponent(defaultTargetId)}` : ""}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`;
  return <Card className="border-border/80 bg-card/80"><CardHeader className="flex-row items-center justify-between gap-3 border-b border-border/70"><div><CardTitle className="flex items-center gap-1.5 text-base"><MessageSquare className="size-4 text-primary" /> Recent Development Chats</CardTitle><p className="mt-1 text-xs text-muted-foreground">このAppの開発指示をチャット単位で管理します。</p></div><Button size="sm" render={<Link href={newChatHref} />} nativeButton={false}><MessageSquare className="size-3.5" /> 新しい開発チャット</Button></CardHeader><CardContent className="p-4">{sessionsLoading ? <p className="py-4 text-center text-xs text-muted-foreground">開発チャットを読み込み中…</p> : sessionsError ? <p className="text-sm text-destructive">開発チャットを読み込めませんでした: {sessionsError.message}</p> : sessions.length ? <div className="grid gap-3 md:grid-cols-2">{sessions.map((session) => { const status = session.development_status || (session.is_active ? "working" : "completed"); const target = targets.find((item) => item.id === session.app_target_id); const targetId = target && /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(target.id) ? target.id : ""; const sessionTargetId = session.app_target_id && /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(session.app_target_id) ? session.app_target_id : targetId; const chatHref = `/chat?s=${encodeURIComponent(session.id)}&app_id=${encodeURIComponent(app.id)}${sessionTargetId ? `&app_target_id=${encodeURIComponent(sessionTargetId)}` : ""}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`; return <div key={session.id} className="flex min-w-0 flex-col gap-3 rounded-md border border-border/70 bg-background/30 p-3"><Link href={chatHref} className="min-w-0 flex-1 hover:underline"><div className="truncate text-sm font-medium">{session.title || "無題の開発チャット"}</div><div className="mt-1 line-clamp-2 text-xs leading-5 text-muted-foreground">{target?.display_name || "App context"} · {formatDate(session.last_activity)} · {session.message_count}件</div></Link><div className="flex items-center justify-between gap-2"><span className={`inline-flex items-center gap-1.5 text-xs ${status === "completed" ? "text-muted-foreground" : "text-primary"}`}><span className={`size-2 rounded-full ${status === "completed" ? "bg-muted-foreground" : "bg-primary"}`} />{developmentStatusLabel(status)}</span><Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={() => void setDevelopmentStatus(session)} disabled={busySessionId === session.id}>{busySessionId === session.id ? <Loader2 className="size-3.5 animate-spin" /> : status === "completed" ? "再開" : "完了"}</Button></div></div>; })}</div> : <div className="rounded-md border border-dashed border-border/70 p-5 text-center"><MessageSquare className="mx-auto size-6 text-muted-foreground/50" /><p className="mt-2 text-sm font-medium">まだ開発チャットがありません</p><p className="mt-1 text-xs text-muted-foreground">「新しい開発チャット」から、このApp専用の指示スレッドを作成できます。</p></div>}</CardContent></Card>;
}

type DevelopmentWorkspaceProps = {
  section: DevelopmentSection;
  onSectionChange: (section: DevelopmentSection) => void;
  app: AppSummary;
  appId: string;
  projectId: string;
  targets: AppTarget[];
  context?: AppContext;
  status?: AppGitStatus;
  statusError?: Error;
  history: AppGitHistoryEntry[];
  historyError?: Error;
  historyLoading: boolean;
  sessions: ConversationSession[];
  sessionsError?: Error;
  sessionsLoading: boolean;
  selectedTarget: string;
  setSelectedTarget: (value: string) => void;
  canEdit: boolean;
  canRun: boolean;
  canRelease: boolean;
  busyJob: string | null;
  runJob: (jobType: "build" | "test" | "run" | "package") => Promise<void>;
  mutateSessions: () => Promise<unknown>;
  mutateStatus: () => Promise<unknown>;
  mutateHistory: () => Promise<unknown>;
  files: AppFile[];
  filesError?: Error;
  filesLoading: boolean;
  selectedPath: string | null;
  setSelectedPath: (path: string) => void;
  selectedFile?: AppFile;
  fileContent: string;
  setFileContent: (value: string) => void;
  fileSha?: string;
  saving: boolean;
  saveFile: (path: string, content: string, sha?: string) => Promise<string | undefined>;
  onSourceUpdated: () => Promise<unknown>;
  docsFiles: AppFile[];
  selectedDocPath: string;
  setSelectedDocPath: (path: string) => void;
  docContent: string;
  setDocContent: (value: string) => void;
  saveDoc: () => Promise<void>;
};

const DEVELOPMENT_SECTIONS: Array<{ key: DevelopmentSection; label: string }> = [
  { key: "chats", label: "開発チャット" },
  { key: "tasks", label: "タスク" },
  { key: "files", label: "ソース" },
  { key: "docs", label: "Docs" },
  { key: "operations", label: "Build / Test" },
];

function DevelopmentWorkspace(props: DevelopmentWorkspaceProps) {
  const { section, onSectionChange } = props;
  const sectionMeta: Record<DevelopmentSection, { title: string; description: string }> = {
    chats: { title: "Development", description: "このAppに紐づく開発チャットと作業状態" },
    tasks: { title: "Related Tasks", description: "このAppに関係するTask-Appリンク" },
    files: { title: "App Files", description: "Appのソースコードとアセットを管理" },
    docs: { title: "App Docs", description: "READMEとApp内ドキュメントを編集" },
    operations: { title: "Operations", description: "Manifest・Target・Build/Test/Run操作" },
  };
  return <div className="apps-subworkspace grid min-w-0 gap-4">
    <header><h2 className="text-2xl font-semibold tracking-tight">{sectionMeta[section].title}</h2><p className="mt-1 text-sm text-muted-foreground">{sectionMeta[section].description}</p></header>
    <nav className="apps-subnav flex min-w-0 gap-5 overflow-x-auto border-b border-border/80" aria-label="開発メニュー">
      {DEVELOPMENT_SECTIONS.map((item) => <button key={item.key} type="button" aria-current={section === item.key ? "page" : undefined} onClick={() => onSectionChange(item.key)} className={`shrink-0 border-b-2 px-0 pb-2 pt-1 text-xs font-medium transition-colors ${section === item.key ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground"}`}>{item.label}</button>)}
    </nav>
    <div className="min-w-0">
      {section === "chats" && <DevelopmentChatsCard app={props.app} projectId={props.projectId} targets={props.targets} sessions={props.sessions} sessionsError={props.sessionsError} sessionsLoading={props.sessionsLoading} mutateSessions={props.mutateSessions} />}
      {section === "tasks" && <TasksTab appId={props.appId} projectId={props.projectId} />}
      {section === "files" && <FilesTab appId={props.appId} projectId={props.projectId} status={props.status} files={props.files} filesLoading={props.filesLoading} error={props.filesError} selectedPath={props.selectedPath} setSelectedPath={props.setSelectedPath} selectedFile={props.selectedFile} content={props.fileContent} setContent={props.setFileContent} sha={props.fileSha} canEdit={props.canEdit} canRun={props.canRun} saving={props.saving} saveFile={props.saveFile} onSourceUpdated={props.onSourceUpdated} />}
      {section === "docs" && <DocsTab files={props.docsFiles} filesError={props.filesError} filesLoading={props.filesLoading} selectedPath={props.selectedDocPath} setSelectedPath={props.setSelectedDocPath} content={props.docContent} setContent={props.setDocContent} canEdit={props.canEdit} saving={props.saving} save={props.saveDoc} />}
      {section === "operations" && <DevelopmentOperationsTab targets={props.targets} selectedTarget={props.selectedTarget} setSelectedTarget={props.setSelectedTarget} canEdit={props.canEdit} canRun={props.canRun} canRelease={props.canRelease} busyJob={props.busyJob} runJob={props.runJob} status={props.status} statusError={props.statusError} />}
    </div>
  </div>;
}

function DevelopmentOperationsTab({ targets, selectedTarget, setSelectedTarget, canEdit, canRun, canRelease, busyJob, runJob, status, statusError }: { targets: AppTarget[]; selectedTarget: string; setSelectedTarget: (value: string) => void; canEdit: boolean; canRun: boolean; canRelease: boolean; busyJob: string | null; runJob: (jobType: "build" | "test" | "run" | "package") => Promise<void>; status?: AppGitStatus; statusError?: Error }) {
  const manifestTargets = targets.map((target) => ({ key: target.target_key, name: target.display_name, surface: target.surface, runtime: target.runtime, host: target.execution_host, entrypoint: target.entrypoint }));
  const commands = targets.find((target) => target.target_key === selectedTarget)?.manifest_snapshot || {};
  return <div className="apps-operations-grid grid min-w-0 gap-4">
    <Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><Wrench className="size-4 text-primary" /> 開発操作</CardTitle></CardHeader><CardContent className="space-y-4 p-4"><div className="space-y-1.5"><Label>Target</Label><AppSelect value={selectedTarget} onChange={(event) => setSelectedTarget(event.target.value)} className="w-full"><option value="">選択してください</option>{targets.map((target) => <option key={target.id} value={target.target_key}>{target.display_name} ({target.target_key})</option>)}</AppSelect></div><div className="flex flex-wrap gap-2"><JobButton label="Build" icon={<Hammer />} disabled={busyJob !== null || !canEdit || !hasTargetCommand(commands, "build")} busy={busyJob === "build"} onClick={() => void runJob("build")} /><JobButton label="Test" icon={<TestTube2 />} disabled={busyJob !== null || !canEdit || !hasTargetCommand(commands, "test")} busy={busyJob === "test"} onClick={() => void runJob("test")} /><JobButton label="Run" icon={<Play />} disabled={busyJob !== null || !canRun || !canExecuteTarget(targets.find((target) => target.target_key === selectedTarget))} busy={busyJob === "run"} onClick={() => void runJob("run")} /><JobButton label="Package" icon={<Package />} disabled={busyJob !== null || !canRelease || !hasTargetCommand(commands, "package")} busy={busyJob === "package"} onClick={() => void runJob("package")} /></div><p className="text-xs text-muted-foreground">表示される操作はManifestのcommand、Targetの実行環境、現在の権限に基づきます。</p></CardContent></Card>
    <Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><FileCode2 className="size-4 text-primary" /> Target一覧</CardTitle></CardHeader><CardContent className="space-y-2 p-4">{manifestTargets.length ? manifestTargets.map((target) => <div key={target.key} className="rounded-md border border-border/70 bg-background/30 p-3"><div className="flex items-center justify-between gap-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">{target.name}</p><p className="mt-1 truncate text-xs text-muted-foreground">{surfaceLabel(target.surface)} · {runtimeLabel(target.runtime)} · {executionHostLabel(target.host)}</p></div><span className="rounded border border-border/70 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">{target.key}</span></div><p className="mt-2 truncate text-xs text-muted-foreground">{target.entrypoint || "entrypoint未設定"}</p></div>) : <p className="text-sm text-muted-foreground">Targetがありません。</p>}</CardContent></Card>
    <Card className="apps-operations-wide border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><GitBranch className="size-4 text-primary" /> Git status</CardTitle></CardHeader><CardContent className="p-4">{statusError ? <p className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">Git statusを読み込めませんでした: {statusError.message}</p> : <><div className="grid gap-3 sm:grid-cols-3"><InfoMetric label="branch" value={status?.branch || "main"} /><InfoMetric label="revision" value={status?.revision ? status.revision.slice(0, 8) : "—"} /><InfoMetric label="状態" value={status?.clean === false ? "変更あり" : status?.clean === true ? "変更なし" : "確認できません"} /></div>{status?.files?.length ? <div className="mt-3 space-y-1 rounded-md border border-border/70 bg-background/30 p-3 text-xs">{status.files.slice(0, 12).map((file, index) => <div key={`${file.path || "file"}-${index}`} className="flex gap-3"><span className="w-6 shrink-0 font-semibold text-muted-foreground">{String(file.status || "?")}</span><span className="truncate">{file.path || "—"}</span></div>)}</div> : null}</>}</CardContent></Card>
  </div>;
}

function hasTargetCommand(snapshot: Record<string, unknown>, jobType: string): boolean {
  const value = snapshot[jobType];
  return typeof value === "string" ? value.trim().length > 0 : Boolean(value && typeof value === "object");
}

function canExecuteTarget(target?: AppTarget): boolean {
  return Boolean(target && (target.execution_host === "aoitalk" || target.execution_host === "server") && hasTargetCommand(target.manifest_snapshot || {}, "run"));
}

function InfoMetric({ label, value }: { label: string; value: string }) {
  return <div className="min-w-0 rounded-lg border bg-muted/10 px-2.5 py-1.5"><p className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</p><p className="truncate text-xs font-semibold leading-4" title={value}>{value}</p></div>;
}

type HistoryWorkspaceProps = {
  section: HistorySection;
  onSectionChange: (section: HistorySection) => void;
  appId: string;
  projectId: string;
  targets: AppTarget[];
  jobs: AppJob[];
  jobsLoading: boolean;
  jobsError?: Error;
  canRun: boolean;
  canEdit: boolean;
  selectedTarget: string;
  runJob: (jobType: "build" | "test" | "run" | "package") => Promise<void>;
  busyJob: string | null;
  mutateJobs: () => Promise<unknown>;
  releases: AppRelease[];
  releasesLoading: boolean;
  releasesError?: Error;
  canRelease: boolean;
  version: string;
  setVersion: (value: string) => void;
  notes: string;
  setNotes: (value: string) => void;
  createRelease: (event: React.FormEvent<HTMLFormElement>) => Promise<void>;
  status?: AppGitStatus;
  statusError?: Error;
  history: AppGitHistoryEntry[];
  historyError?: Error;
  historyLoading: boolean;
  mutateStatus: () => Promise<unknown>;
  mutateHistory: () => Promise<unknown>;
};

const HISTORY_SECTIONS: Array<{ key: HistorySection; label: string }> = [
  { key: "jobs", label: "実行" },
  { key: "releases", label: "保存版" },
  { key: "changes", label: "変更履歴" },
];

function HistoryWorkspace(props: HistoryWorkspaceProps) {
  return <div className="apps-subworkspace grid min-w-0 gap-4"><nav className="apps-subnav flex min-w-0 gap-5 overflow-x-auto border-b border-border/80" aria-label="履歴メニュー">{HISTORY_SECTIONS.map((item) => <button key={item.key} type="button" aria-current={props.section === item.key ? "page" : undefined} onClick={() => props.onSectionChange(item.key)} className={`shrink-0 border-b-2 px-0 pb-2 pt-1 text-xs font-medium transition-colors ${props.section === item.key ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground"}`}>{item.label}</button>)}</nav><div className="min-w-0">{props.section === "jobs" && <JobsTab appId={props.appId} projectId={props.projectId} targets={props.targets} jobs={props.jobs} jobsLoading={props.jobsLoading} jobsError={props.jobsError} canRun={props.canRun} selectedTarget={props.selectedTarget} runJob={props.runJob} busyJob={props.busyJob} mutateJobs={props.mutateJobs} />}{props.section === "releases" && <ReleasesTab appId={props.appId} projectId={props.projectId} releases={props.releases} releasesLoading={props.releasesLoading} releasesError={props.releasesError} canRelease={props.canRelease} canDownloadArtifact={props.canRun} version={props.version} setVersion={props.setVersion} notes={props.notes} setNotes={props.setNotes} createRelease={props.createRelease} />}{props.section === "changes" && <GitHistoryTab appId={props.appId} projectId={props.projectId} canEdit={props.canEdit} status={props.status} statusError={props.statusError} history={props.history} historyError={props.historyError} historyLoading={props.historyLoading} mutateStatus={props.mutateStatus} mutateHistory={props.mutateHistory} />}</div></div>;
}

function GitHistoryTab({ appId, projectId, canEdit, status, statusError, history, historyError, historyLoading, mutateStatus, mutateHistory }: { appId: string; projectId: string; canEdit: boolean; status?: AppGitStatus; statusError?: Error; history: AppGitHistoryEntry[]; historyError?: Error; historyLoading: boolean; mutateStatus: () => Promise<unknown>; mutateHistory: () => Promise<unknown> }) {
  const [restoringRevision, setRestoringRevision] = useState<string | null>(null);
  const restoreRevision = async (revision: string) => {
    if (!window.confirm("この時点のApp全体へ戻します。現在の変更はcheckpoint済みですか？")) return;
    setRestoringRevision(revision);
    try {
      await appsApi.restoreGitRevision(appId, revision, projectId || undefined);
      toast.success("Appを指定したrevisionへ戻しました");
      await Promise.all([mutateStatus(), mutateHistory()]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Appを復元できませんでした");
    } finally {
      setRestoringRevision(null);
    }
  };
  return <div className="space-y-4"><Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><GitBranch className="size-4 text-primary" /> App Git</CardTitle></CardHeader><CardContent className="p-4">{statusError ? <p className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">Git statusを読み込めませんでした: {statusError.message}</p> : <><div className="grid gap-3 sm:grid-cols-3"><InfoMetric label="branch" value={status?.branch || "main"} /><InfoMetric label="revision" value={status?.revision ? status.revision.slice(0, 8) : "—"} /><InfoMetric label="状態" value={status?.clean === false ? "変更あり" : status?.clean === true ? "変更なし" : "確認できません"} /></div><div className="mt-3 flex gap-2"><Button size="sm" variant="outline" onClick={() => void Promise.all([mutateStatus(), mutateHistory()])}><RefreshCw className="size-3.5" /> 再読込</Button></div></>}</CardContent></Card><Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><History className="size-4 text-primary" /> 変更履歴</CardTitle></CardHeader><CardContent className="space-y-2 p-4">{historyLoading ? <p className="py-4 text-center text-xs text-muted-foreground">変更履歴を読み込み中…</p> : historyError ? <p className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">変更履歴を読み込めませんでした: {historyError.message}</p> : history.length ? history.map((entry, index) => <div key={`${entry.revision || "revision"}-${index}`} className="border-b border-border/70 pb-2 text-xs last:border-0"><div className="flex items-center justify-between gap-3"><div className="min-w-0"><div className="truncate font-medium">{entry.message || entry.revision?.slice(0, 8) || "変更"}</div><div className="text-muted-foreground">{entry.author || ""} {entry.date || ""}</div></div>{entry.revision && canEdit && <Button type="button" size="sm" variant="outline" className="shrink-0 px-2 text-xs" onClick={() => void restoreRevision(entry.revision!)} disabled={restoringRevision !== null}>{restoringRevision === entry.revision ? <Loader2 className="size-3.5 animate-spin" /> : "この時点に戻す"}</Button>}</div></div>) : <p className="text-sm text-muted-foreground">変更履歴がありません。</p>}{!canEdit && history.length > 0 && <p className="text-xs text-muted-foreground">復元はdeveloper以上で利用できます。</p>}</CardContent></Card></div>;
}

function TasksTab({ appId, projectId }: { appId: string; projectId: string }) {
  const { data, error, isLoading } = useSWR<{ tasks: Array<TaskAppLink & { task?: Record<string, unknown> }> }>(projectId ? `/apps/${appId}/tasks?project_id=${encodeURIComponent(projectId)}` : null, () => appsApi.listAppTasks(appId, projectId));
  const tasks = data?.tasks || [];
  return <Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><Check className="size-4 text-primary" /> Related Tasks</CardTitle><p className="mt-1 text-xs text-muted-foreground">このAppに関係するタスクを開発画面から確認できます。</p></CardHeader><CardContent className="space-y-2 p-4">{!projectId ? <p className="text-sm text-muted-foreground">Projectを選択すると関連タスクを表示できます。</p> : isLoading ? <p className="py-4 text-center text-xs text-muted-foreground">関連タスクを読み込み中…</p> : error ? <p className="text-sm text-destructive">関連タスクを読み込めませんでした。</p> : tasks.length ? tasks.map((link) => { const task = link.task || {}; const title = String(task.title || task.name || task.subject || link.task_id); return <Link key={link.id} href={`/tasks/${encodeURIComponent(link.task_id)}`} className="flex items-center justify-between gap-3 rounded-md border border-border/70 bg-background/30 p-3 hover:border-primary/50 hover:bg-muted/40"><span className="min-w-0 truncate text-sm font-medium">{title}</span><span className="shrink-0 text-xs text-muted-foreground">{relationTypeLabel(link.relation_type)}{link.target?.display_name ? ` · ${link.target.display_name}` : ""}</span></Link>; }) : <div className="rounded-md border border-dashed border-border/70 p-4 text-center"><p className="text-sm font-medium">関連タスクはありません</p><p className="mt-1 text-xs text-muted-foreground">App contextの開発チャットから作成したタスクがここに表示されます。</p></div>}{projectId && <Link href={`/tasks?app_id=${encodeURIComponent(appId)}&project_id=${encodeURIComponent(projectId)}`} className="inline-flex text-xs text-primary hover:underline">Tasks画面を開く <ArrowRight className="size-3.5" /></Link>}</CardContent></Card>;
}

function FilesTab({ appId, projectId, status, files, filesLoading, error, selectedPath, setSelectedPath, selectedFile, content, setContent, sha, canEdit, canRun, saving, saveFile, onSourceUpdated }: { appId: string; projectId: string; status?: AppGitStatus; files: AppFile[]; filesLoading: boolean; error?: Error; selectedPath: string | null; setSelectedPath: (path: string) => void; selectedFile?: AppFile; content: string; setContent: (value: string) => void; sha?: string; canEdit: boolean; canRun: boolean; saving: boolean; saveFile: (path: string, content: string, sha?: string) => Promise<string | undefined>; onSourceUpdated: () => Promise<unknown> }) {
  const basenameCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const file of files) {
      const path = String(file.path || file.filename || "");
      const basename = path.split("/").pop() || path;
      counts.set(basename, (counts.get(basename) || 0) + 1);
    }
    return counts;
  }, [files]);
  return <div className="apps-files-grid grid min-w-0 items-start gap-4"><Card className="min-h-0 border-border/80 bg-card/80"><CardHeader className="flex-row items-center justify-between gap-2 border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><FileText className="size-4 text-primary" /> App Files / ファイル一覧</CardTitle><div className="flex shrink-0 items-center gap-2">{canRun && <AppArchiveDownloadDialog appId={appId} projectId={projectId || undefined} label="Download" compact files={files} filesLoading={filesLoading} />}<AppSourceUpdateDialog appId={appId} projectId={projectId || undefined} status={status} canEdit={canEdit} onApplied={onSourceUpdated} /></div></CardHeader><CardContent className="max-h-[60vh] min-h-0 space-y-0.5 overflow-y-auto p-2">{filesLoading ? <p className="py-4 text-center text-xs text-muted-foreground">ファイルを読み込み中…</p> : error ? <p className="p-2 text-sm text-destructive">{error.message}</p> : files.map((file) => { const path = String(file.path || file.filename || ""); const basename = path.split("/").pop() || path; const label = (basenameCounts.get(basename) || 0) > 1 ? path : basename; const textFile = isTextAppFile(file); const meta = textFile ? (file.size_bytes !== undefined ? `${file.size_bytes} B` : "") : "バイナリ"; return textFile ? <button key={file.path} type="button" title={path} onClick={() => setSelectedPath(file.path)} className={`flex w-full items-center justify-between gap-2 rounded px-2.5 py-2 text-left text-xs ${selectedPath === file.path ? "border-l-2 border-l-primary bg-primary/5 text-foreground" : "border-l-2 border-l-transparent hover:bg-muted/40"}`}><span className="truncate">{label}</span><span className="shrink-0 text-[11px] text-muted-foreground">{meta}</span></button> : <a key={file.path} href={appsApi.downloadFile(appId, file.path, projectId || undefined)} download title={path} className="flex w-full items-center justify-between gap-2 rounded px-2.5 py-2 text-left text-xs opacity-75 hover:bg-muted/40 hover:opacity-100"><span className="truncate">{label}</span><span className="shrink-0 text-[11px] text-muted-foreground">{meta} · 取得</span></a>; })}{!files.length && !error && !filesLoading && <p className="p-2 text-sm text-muted-foreground">ファイルがありません。</p>}</CardContent></Card><Card className="min-w-0 border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center justify-between gap-2 text-base"><span className="flex min-w-0 items-center gap-2"><Terminal className="size-4 text-primary" /> <span className="truncate">{selectedPath || "ファイルを選択"}</span></span>{selectedPath && selectedFile && isTextAppFile(selectedFile) && canEdit && <Button size="sm" onClick={() => void saveFile(selectedPath, content, sha)} disabled={saving}>{saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />} 保存</Button>}</CardTitle></CardHeader><CardContent className="p-4">{selectedPath && selectedFile && !isTextAppFile(selectedFile) ? <div className="rounded-md border border-dashed border-border/70 p-4 text-center"><FileSpreadsheet className="mx-auto size-6 text-muted-foreground/60" /><p className="mt-2 text-sm font-medium">バイナリファイルです</p><p className="mt-1 text-xs text-muted-foreground">Officeファイルなどはテキストエディタで開かず、一覧から取得してください。</p></div> : selectedPath ? <Textarea value={content} onChange={(event) => setContent(event.target.value)} readOnly={!canEdit} className="max-h-[60vh] min-h-[22rem] resize-y overflow-y-auto font-mono text-xs" aria-label={`${selectedPath} の内容`} /> : <p className="text-sm text-muted-foreground">編集または閲覧するファイルを選択してください。</p>}{selectedFile && isTextAppFile(selectedFile) && !canEdit && <p className="mt-2 text-xs text-muted-foreground">現在の権限ではファイル編集できません。</p>}</CardContent></Card></div>;
}

function DocsTab({ files, filesError, filesLoading, selectedPath, setSelectedPath, content, setContent, canEdit, saving, save }: { files: AppFile[]; filesError?: Error; filesLoading: boolean; selectedPath: string; setSelectedPath: (path: string) => void; content: string; setContent: (value: string) => void; canEdit: boolean; saving: boolean; save: () => Promise<void> }) {
  const docs = files.filter((file) => isMarkdownAppFile(file)).sort((left, right) => {
    const leftPath = String(left.path || left.filename || "");
    const rightPath = String(right.path || right.filename || "");
    if (leftPath.toLowerCase() === "readme.md") return -1;
    if (rightPath.toLowerCase() === "readme.md") return 1;
    return leftPath.localeCompare(rightPath);
  });
  return <div className="apps-docs-grid grid min-w-0 items-start gap-4">
    <Card className="min-h-0 min-w-0 border-border/80 bg-card/80">
      <CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-base"><BookOpen className="size-4 text-primary" /> Docs</CardTitle><p className="mt-1 text-xs text-muted-foreground">READMEとApp内ドキュメント</p></CardHeader>
      <CardContent className="max-h-[60vh] min-h-0 space-y-0.5 overflow-y-auto p-2">
        {filesLoading ? <p className="py-4 text-center text-xs text-muted-foreground">Docs一覧を読み込み中…</p> : filesError ? <p className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">Docs一覧を読み込めませんでした: {filesError.message}</p> : docs.map((file) => { const path = String(file.path || file.filename || ""); const isReadme = path.toLowerCase() === "readme.md"; return <button key={path} type="button" onClick={() => setSelectedPath(path)} className={`flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm ${selectedPath === path ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}><FileText className="size-3.5 shrink-0" /><span className="min-w-0 truncate">{isReadme ? "README.md" : path}</span></button>; })}
        {!filesLoading && !filesError && !docs.length && <p className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">READMEやDocsがまだありません。</p>}
      </CardContent>
    </Card>
     <Card className="min-w-0 border-border/80 bg-card/80">
       <CardHeader className="border-b border-border/70"><CardTitle className="flex items-center justify-between gap-2 text-base"><span className="flex min-w-0 items-center gap-2"><BookOpen className="size-4 text-primary" /><span className="truncate">{selectedPath || "README.md"}</span></span>{canEdit && <Button size="sm" onClick={() => void save()} disabled={saving}>{saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />} 保存</Button>}</CardTitle></CardHeader>
      {/* field-sizing-content のままだと長いREADMEでページ全体が数千pxに伸びるため、高さを閉じ込めて内側スクロールにする。 */}
       <CardContent className="p-4"><Textarea value={content} onChange={(event) => setContent(event.target.value)} readOnly={!canEdit} className="max-h-[60vh] min-h-[24rem] resize-y overflow-y-auto font-mono text-xs" aria-label={`${selectedPath || "README.md"} の内容`} />{!canEdit && <p className="mt-1.5 text-[11px] text-muted-foreground">developer以上でDocsを編集できます。</p>}</CardContent>
    </Card>
  </div>;
}

function JobsTab({ appId, projectId, targets, jobs, jobsLoading, jobsError, canRun, selectedTarget, runJob, busyJob, mutateJobs }: { appId: string; projectId: string; targets: AppTarget[]; jobs: AppJob[]; jobsLoading: boolean; jobsError?: Error; canRun: boolean; selectedTarget: string; runJob: (jobType: "build" | "test" | "run" | "package") => Promise<void>; busyJob: string | null; mutateJobs: () => Promise<unknown> }) {
  const [logs, setLogs] = useState<{ id: string; value: string } | null>(null);
  const [stoppingJob, setStoppingJob] = useState<string | null>(null);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const loadLogs = async (jobId: string) => {
    try { const result = await appsApi.getJobLogs(appId, jobId, projectId || undefined); setLogs({ id: jobId, value: result.logs }); } catch (error) { toast.error(error instanceof Error ? error.message : "ログを取得できませんでした"); }
  };
  const selectedTargetData = targets.find((target) => target.target_key === selectedTarget);
  const selectedJob = jobs.find((job) => job.id === selectedJobId) || jobs[0];
  const stopJob = async (jobId: string) => {
    setStoppingJob(jobId);
    try {
      await appsApi.stopJob(appId, jobId, projectId || undefined);
      toast.success("Jobの停止を要求しました");
      await mutateJobs();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Jobを停止できませんでした");
    } finally {
      setStoppingJob(null);
    }
  };
  return <Card className="border-border/80 bg-card/80"><CardHeader className="flex-row items-center justify-between gap-3 border-b border-border/70"><div><CardTitle className="flex items-center gap-2 text-base"><History className="size-4 text-primary" /> Jobs / 実行履歴</CardTitle><p className="mt-1 text-xs text-muted-foreground">Build・Test・Run・Packageの実行履歴</p></div><div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => void mutateJobs()}><RefreshCw className="size-3.5" /> 再読込</Button><Button size="sm" onClick={() => void runJob("run")} disabled={!canRun || !canExecuteTarget(selectedTargetData) || !!busyJob}><Play className="size-3.5" /> Run</Button></div></CardHeader><CardContent className="p-0">{jobsLoading ? <p className="py-8 text-center text-xs text-muted-foreground">実行履歴を読み込み中…</p> : jobsError ? <p className="m-4 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">実行履歴を読み込めませんでした: {jobsError.message}</p> : jobs.length ? <div className="grid min-w-0 lg:grid-cols-[minmax(0,1fr)_minmax(260px,.65fr)]"><div className="min-w-0 overflow-x-auto"><div className="grid min-w-[560px] grid-cols-[1.25fr_1fr_1fr_1fr] border-b border-border/70 bg-muted/20 px-4 py-2 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground"><span>Job</span><span>Type</span><span>Status</span><span>Started</span></div>{jobs.map((job) => { const active = selectedJob?.id === job.id; return <button key={job.id} type="button" onClick={() => setSelectedJobId(job.id)} className={`grid w-full min-w-[560px] grid-cols-[1.25fr_1fr_1fr_1fr] items-center border-b border-border/60 px-4 py-3 text-left text-xs transition-colors ${active ? "border-l-2 border-l-primary bg-primary/5" : "border-l-2 border-l-transparent hover:bg-muted/30"}`}><span className="truncate font-mono text-muted-foreground">{job.id.slice(0, 12)}</span><span>{jobTypeLabel(job.job_type)}</span><span className={`flex items-center gap-1.5 ${job.status === "failed" ? "text-destructive" : job.status === "succeeded" ? "text-primary" : "text-muted-foreground"}`}><span className="size-2 rounded-full bg-current" />{readableStatus(job.status)}</span><span className="truncate text-muted-foreground">{formatDate(job.started_at)}</span></button>; })}</div><div className="min-w-0 border-t border-border/70 p-4 lg:border-l lg:border-t-0"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Job details</p>{selectedJob ? <div className="mt-3 space-y-3"><div className="flex flex-wrap items-center gap-2"><h3 className="text-lg font-semibold">{jobTypeLabel(selectedJob.job_type)}</h3><Badge variant={selectedJob.status === "failed" ? "destructive" : "outline"}>{readableStatus(selectedJob.status)}</Badge></div><dl className="space-y-2 text-xs"><div className="flex justify-between gap-3"><dt className="text-muted-foreground">Target</dt><dd className="truncate">{targets.find((target) => target.id === selectedJob.target_id)?.display_name || "—"}</dd></div><div className="flex justify-between gap-3"><dt className="text-muted-foreground">開始</dt><dd>{formatDate(selectedJob.started_at)}</dd></div><div className="flex justify-between gap-3"><dt className="text-muted-foreground">終了</dt><dd>{formatDate(selectedJob.ended_at)}</dd></div><div className="flex justify-between gap-3"><dt className="text-muted-foreground">終了コード</dt><dd>{selectedJob.exit_code ?? "—"}</dd></div></dl><div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" onClick={() => void loadLogs(selectedJob.id)}>ログ表示</Button>{canRun && (selectedJob.status === "queued" || selectedJob.status === "running") && <Button size="sm" variant="outline" className="text-destructive hover:text-destructive" onClick={() => void stopJob(selectedJob.id)} disabled={stoppingJob === selectedJob.id}>{stoppingJob === selectedJob.id ? <Loader2 className="size-3.5 animate-spin" /> : "停止"}</Button>}</div>{logs?.id === selectedJob.id && <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md border border-border/70 bg-background/60 p-2 font-mono text-[11px] leading-4 text-muted-foreground">{logs.value || "ログはありません。"}</pre>}</div> : <p className="mt-3 text-xs text-muted-foreground">Jobを選択してください。</p>}</div></div> : <p className="px-4 py-8 text-center text-xs text-muted-foreground">実行履歴がありません。</p>}</CardContent></Card>;
}

/**
 * 成果物ダウンロードは per-path の秘匿フィルタが効かない一括アーカイブで、
 * 作成時点のフィルタ規則のまま凍結される。バックエンドも runner 以上を要求するため、
 * 閲覧のみ（viewer）にはリンクを出さず理由を明示する。
 */
export function ReleasesTab({ appId, projectId, releases, releasesLoading, releasesError, canRelease, canDownloadArtifact, version, setVersion, notes, setNotes, createRelease }: { appId: string; projectId: string; releases: AppRelease[]; releasesLoading: boolean; releasesError?: Error; canRelease: boolean; canDownloadArtifact: boolean; version: string; setVersion: (value: string) => void; notes: string; setNotes: (value: string) => void; createRelease: (event: React.FormEvent<HTMLFormElement>) => Promise<void> }) {
  return <div className="space-y-4"><header><h2 className="text-2xl font-semibold tracking-tight">Releases</h2><p className="mt-1 text-sm text-muted-foreground">実在するGit revisionとManifestから作成された保存版</p></header><Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="text-base">保存版を作成</CardTitle></CardHeader><CardContent className="p-4"><form className="grid gap-3 md:grid-cols-[180px_1fr_auto]" onSubmit={(event) => void createRelease(event)}><Input value={version} onChange={(event) => setVersion(event.target.value)} placeholder="v1.0.0" disabled={!canRelease} required /><Input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="変更概要" disabled={!canRelease} /><Button type="submit" disabled={!canRelease || !version.trim()}><Package className="size-3.5" /> 作成</Button></form>{!canRelease && <p className="mt-2 text-xs text-muted-foreground">保存版の作成にはmaintainer以上の権限が必要です。</p>}</CardContent></Card><Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="text-base">保存版一覧</CardTitle></CardHeader><CardContent className="space-y-3 p-4">{releasesLoading ? <p className="py-4 text-center text-xs text-muted-foreground">保存版を読み込み中…</p> : releasesError ? <p className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">保存版を読み込めませんでした: {releasesError.message}</p> : releases.map((release) => <div key={release.id} className="rounded-md border border-border/70 bg-background/30 p-3"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="flex items-center gap-2 font-medium"><span>{release.version}</span><Badge variant="secondary" className="px-1.5 py-0 text-[10px]">{releaseStatusLabel(release.status)}</Badge></div><p className="mt-1 text-xs text-muted-foreground">{release.changelog || "変更概要なし"}</p></div><span className="text-xs text-muted-foreground">{formatDate(release.created_at)}</span></div>{release.artifacts?.length ? (canDownloadArtifact ? <div className="mt-3 flex flex-wrap gap-2">{release.artifacts.map((artifact) => <a key={artifact.id} href={appsApi.downloadArtifact(appId, artifact.id, projectId || undefined)} className="inline-flex items-center gap-1 rounded border border-border/70 px-2 py-1 text-xs hover:border-primary/50 hover:bg-muted/40" download>{artifact.filename} · {artifact.artifact_type}</a>)}</div> : <div className="mt-3 space-y-1.5"><div className="flex flex-wrap gap-2">{release.artifacts.map((artifact) => <span key={artifact.id} className="inline-flex items-center gap-1 rounded border border-dashed border-border/70 px-2 py-1 text-xs text-muted-foreground">{artifact.filename} · {artifact.artifact_type}</span>)}</div><p className="text-[11px] leading-4 text-muted-foreground">成果物のダウンロードには実行（runner）以上の権限が必要です。成果物はソース一式を含む一括アーカイブのため、閲覧のみの権限では取得できません。</p></div>) : null}</div>)}{!releasesLoading && !releasesError && !releases.length && <EmptyHint icon={<Package className="size-3.5" />} title="保存版はありません" detail="maintainer以上の権限があれば保存版を作成できます。" />}</CardContent></Card></div>;
}

function SettingsTab({ app, canEdit, mutate, projectId, projectBinding, publishedReleases, canRun, projectCanWrite, bindingBusy, onToggleProjectBinding, onChangeBindingMode, onUpdateProjectBinding }: { app: AppSummary; canEdit: boolean; mutate: () => Promise<unknown>; projectId: string; projectBinding?: ProjectAppBinding; publishedReleases: AppRelease[]; canRun: boolean; projectCanWrite: boolean; bindingBusy: boolean; onToggleProjectBinding: () => Promise<void>; onChangeBindingMode: (value: string) => Promise<void>; onUpdateProjectBinding: (input: Partial<ProjectAppInput>) => Promise<void> }) {
  const [description, setDescription] = useState(app.description || "");
  const [visibility, setVisibility] = useState(app.visibility);
  const [saving, setSaving] = useState(false);
  const canManageProjectBinding = canRun && projectCanWrite;
  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setSaving(true);
      try { await appsApi.update(app.id, { description, visibility: visibility as "private" | "shared" | "public" }, projectId || undefined); toast.success("App設定を保存しました"); await mutate(); } catch (error) { toast.error(error instanceof Error ? error.message : "設定を保存できませんでした"); } finally { setSaving(false); }
  };
  return <div className="space-y-4"><Card><CardHeader><CardTitle className="flex items-center gap-1.5 text-sm"><Settings2 className="size-4" /> App設定</CardTitle></CardHeader><CardContent><form className="max-w-2xl space-y-4" onSubmit={save}><div className="space-y-1.5"><Label htmlFor="settings-description">説明</Label><Textarea id="settings-description" value={description} onChange={(event) => setDescription(event.target.value)} readOnly={!canEdit} rows={4} /></div><div className="space-y-1.5"><Label htmlFor="settings-visibility">共有範囲</Label><AppSelect id="settings-visibility" value={visibility} onChange={(event) => setVisibility(event.target.value)} disabled={!canEdit} className="w-full"><option value="private">非公開</option><option value="shared">共有</option><option value="public">公開</option></AppSelect></div>{canEdit ? <Button type="submit" disabled={saving}>{saving && <Loader2 className="size-3.5 animate-spin" />} 保存</Button> : <p className="text-xs text-muted-foreground">App管理権限が必要です。</p>}</form></CardContent></Card>{projectId && <Card><CardHeader><CardTitle className="flex items-center gap-1.5 text-sm"><FolderOpen className="size-4" /> このProjectでの利用</CardTitle><p className="text-xs text-muted-foreground">App本体を削除せず、このProjectとの関連だけを管理します。</p></CardHeader><CardContent className="space-y-3">{projectBinding ? <><div className="flex flex-wrap items-center gap-2"><AppSelect aria-label="このProjectで使う版" size="sm" value={projectBinding.binding_mode} onChange={(event) => void onChangeBindingMode(event.target.value)} disabled={!canManageProjectBinding || bindingBusy}><option value="development">main（最新）</option><option value="installed" disabled={!publishedReleases.length}>保存版</option></AppSelect>{projectBinding.binding_mode === "installed" && <AppSelect aria-label="保存版の選択" size="sm" value={projectBinding.installed_release_id || ""} onChange={(event) => void onUpdateProjectBinding({ binding_mode: "installed", installed_release_id: event.target.value || null })} disabled={!canManageProjectBinding || bindingBusy}>{publishedReleases.length ? publishedReleases.map((release) => <option key={release.id} value={release.id}>{release.version}</option>) : <option value="">保存版なし</option>}</AppSelect>}<span className="text-xs text-muted-foreground">{projectBinding.binding_mode === "installed" ? "このProjectは選択した保存版を使います。" : "このProjectはAppのmain（最新）を使います。"}</span></div><Button type="button" size="sm" variant="outline" onClick={() => void onToggleProjectBinding()} disabled={!canManageProjectBinding || bindingBusy}>{bindingBusy ? <Loader2 className="size-3.5 animate-spin" /> : null}Projectから外す</Button></> : <div className="flex flex-wrap items-center gap-3"><p className="text-sm text-muted-foreground">このProjectではまだ利用していません。</p><Button type="button" size="sm" onClick={() => void onToggleProjectBinding()} disabled={!canManageProjectBinding || bindingBusy}>{bindingBusy ? <Loader2 className="size-3.5 animate-spin" /> : null}Projectで利用する</Button></div>}{!canRun ? <p className="text-xs text-muted-foreground">Projectで利用するにはrunner以上の権限が必要です。</p> : !projectCanWrite && <p className="text-xs text-muted-foreground">Projectの編集権限が必要です。</p>}</CardContent></Card>}</div>;
}

function JobButton({ label, icon, disabled, busy, onClick }: { label: string; icon: React.ReactNode; disabled: boolean; busy: boolean; onClick: () => void }) { return <Button size="sm" variant="outline" onClick={onClick} disabled={disabled || busy}>{busy ? <Loader2 className="size-3.5 animate-spin" /> : icon}<span>{label}</span></Button>; }

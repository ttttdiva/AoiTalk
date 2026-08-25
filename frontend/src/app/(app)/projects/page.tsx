"use client";

import { AppSelect } from "@/components/ui/app-select";

import Link from "next/link";
import {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  FolderOpen,
  Plus,
  Users,
  Loader2,
  Pencil,
  Trash2,
  Check,
  X,
  LayoutDashboard,
  Layers,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  Circle,
  BookOpen,
  Settings2,
  Tags,
  FileText,
  PanelLeftClose,
  PanelLeftOpen,
  Boxes,
  Brain,
  Link2,
} from "lucide-react";
import { useProject } from "@/contexts/project-context";
import { ProjectDashboard } from "@/components/project-dashboard";
import { ProjectInformationPanel } from "@/components/projects/project-information-panel";
import { ProjectDocsCandidatesPanel } from "@/components/projects/ProjectDocsCandidatesPanel";
import { ProjectContextPackStatus } from "@/components/projects/ProjectContextPackStatus";
import { ProjectManagementPanel } from "@/components/projects/project-management-panel";
import { ProjectMembersPanel } from "@/components/projects/project-members-panel";
import { ProjectMemoryPanel } from "@/components/projects/project-memory-panel";
import { ProjectKnowledgePanel } from "@/components/projects/ProjectKnowledgePanel";
import { useProjectManagementFiles } from "@/components/projects/hooks/use-project-management-files";
import { useProjectMembers } from "@/components/projects/hooks/use-project-members";
import { SpaceTagManagementPanel } from "@/components/projects/space-tag-management-panel";
import { SpaceOverviewPanel } from "@/components/projects/space-overview-panel";
import {
  ProjectColorPicker,
  ResourceColorPicker,
} from "@/components/projects/resource-color-picker";
import { ProjectAppsPanel } from "@/components/apps/project-apps-panel";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useConfirm } from "@/hooks/use-confirm";
import { useProjectsData } from "@/hooks/use-projects-data";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";

async function apiFetch<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

interface SpaceInfo {
  id: string;
  name: string;
  slug: string;
  description?: string | null;
  color?: string | null;
  sort_order?: number;
  /** Remote resources are view-only in the local Projects workspace. */
  source?: string;
  /** Some resource providers expose an explicit write permission. */
  can_write?: boolean;
}

interface ProjectInfo {
  id: string;
  name: string;
  description?: string | null;
  slug: string;
  aliases?: string[];
  color?: string | null;
  metadata?: Record<string, unknown> & {
    workspace_tools_enabled?: boolean;
    isInboxDefault?: boolean;
  };
  owner_id?: string;
  can_manage_settings?: boolean;
  estimated_hours?: number | null;
  space_id?: string | null;
  is_completed?: boolean;
  can_write?: boolean;
  knowledge_node_id?: string | null;
  /** API may explicitly mark a reverse pointer stale while it is repaired. */
  knowledge_node_id_valid?: boolean;
  knowledge_node_id_validated?: boolean;
  created_at?: string | null;
}

// SWR データ未取得時に返す安定参照（再レンダー抑制用）。
const EMPTY_SPACES: SpaceInfo[] = [];
const EMPTY_PROJECTS: ProjectInfo[] = [];

const PROJECTS_PAGE_NAV_COLLAPSED_KEY = "projectsPageNavigationCollapsed";

type RightTab =
  | "dashboard"
  | "information"
  | "knowledge"
  | "memory"
  | "members"
  | "management"
  | "tags"
  | "apps";
type SelectedScope =
  | { type: "project"; id: string }
  | { type: "space"; id: string };

export default function ProjectsPage() {
  const confirm = useConfirm();
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedProjectId = searchParams.get("project_id");
  const requestedAppId = searchParams.get("app_id");
  const { refreshProjects, refreshSpaces } = useProject();

  // スペース一覧・プロジェクト一覧（取得は SWR に委譲）
  const {
    spaces: spacesData,
    projects: projectsData,
    refresh: refreshProjectsData,
    spacesError: spacesDataError,
    projectsError: projectsDataError,
    spacesLoaded: spacesDataLoaded,
    projectsLoaded: projectsDataLoaded,
  } = useProjectsData<SpaceInfo, ProjectInfo>();
  const spaces = spacesData ?? EMPTY_SPACES;
  const [expandedSpaces, setExpandedSpaces] = useState<Set<string>>(new Set());
  const [expandedClosedSpaces, setExpandedClosedSpaces] = useState<Set<string>>(
    new Set(),
  );
  const [isNavigationCollapsed, setIsNavigationCollapsed] = useState(false);

  // プロジェクト一覧
  const projects = projectsData ?? EMPTY_PROJECTS;
  // API ごとの取得結果を独立して扱う。古いテスト用モックなどが状態を返さない
  // 場合は、データの有無から後方互換に推定する。
  const spacesError = spacesDataError ?? null;
  const projectsError = projectsDataError ?? null;
  const spacesLoaded = spacesDataLoaded ?? spacesData !== undefined;
  const projectsLoaded = projectsDataLoaded ?? projectsData !== undefined;
  const resourcesReady = spacesLoaded && projectsLoaded;
  const hasResourceError = Boolean(spacesError || projectsError);
  const canShowEmptyState =
    resourcesReady && !hasResourceError && spaces.length === 0 && projects.length === 0;
  const [loading, setLoading] = useState(true);
  const fetchGenerationRef = useRef(0);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(
    null,
  );
  const [selectedScope, setSelectedScope] = useState<SelectedScope | null>(
    null,
  );
  const projectMembers = useProjectMembers({
    projectId: selectedScope?.type === "project" ? selectedScope.id : null,
    confirm,
  });

  // 右パネルタブ
  const [rightTab, setRightTab] = useState<RightTab>("dashboard");

  // 新規スペース作成
  const [showCreateSpace, setShowCreateSpace] = useState(false);
  const [newSpaceName, setNewSpaceName] = useState("");
  const [creatingSpace, setCreatingSpace] = useState(false);

  // スペース編集
  const [editingSpaceId, setEditingSpaceId] = useState<string | null>(null);
  const [editSpaceName, setEditSpaceName] = useState("");
  const [editSpaceColor, setEditSpaceColor] = useState("#3b82f6");

  // 新規プロジェクト作成
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [createSpaceId, setCreateSpaceId] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [newAliases, setNewAliases] = useState("");
  const [newEstHours, setNewEstHours] = useState("");
  const [newColor, setNewColor] = useState("#3b82f6");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  // プロジェクト編集
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editEstHours, setEditEstHours] = useState("");
  const [editAliases, setEditAliases] = useState("");
  const [editColor, setEditColor] = useState("#3b82f6");
  const [editSpaceIdField, setEditSpaceIdField] = useState<string>("");
  const [editWorkspaceToolsEnabled, setEditWorkspaceToolsEnabled] =
    useState(false);
  const [saving, setSaving] = useState(false);

  // データ取得（取得自体は SWR に委譲し、選択復元・展開などの副作用はここで実行）
  const fetchAll = useCallback(async () => {
    const fetchGeneration = ++fetchGenerationRef.current;
    const isCurrentFetch = () => fetchGenerationRef.current === fetchGeneration;
    setLoading(true);
    try {
      const result = await refreshProjectsData();
      // A retry can settle before an earlier request. Only the latest result
      // may restore selection/expanded state or end the loading indicator.
      if (!isCurrentFetch()) return;
      const spacesList = result?.spaces ?? EMPTY_SPACES;
      const projectsList = result?.projects ?? EMPTY_PROJECTS;
      const activeProjects = projectsList.filter(
        (project) => !project.is_completed,
      );

      // 全スペースを展開
      setExpandedSpaces(new Set(spacesList.map((s) => s.id)));

      const savedScope = localStorage.getItem("projectsPageSelectedScope");
      const savedProject = localStorage.getItem("projectsPageSelected");
      let parsedScope: SelectedScope | null = null;
      try {
        parsedScope = savedScope
          ? (JSON.parse(savedScope) as SelectedScope)
          : null;
      } catch {
        parsedScope = null;
      }
      if (
        requestedProjectId &&
        projectsList.some((project) => project.id === requestedProjectId)
      ) {
        setSelectedProjectId(requestedProjectId);
        setSelectedScope({ type: "project", id: requestedProjectId });
        if (requestedAppId) setRightTab("apps");
      } else if (
        parsedScope?.type === "space" &&
        spacesList.some((s) => s.id === parsedScope.id)
      ) {
        setSelectedScope(parsedScope);
      } else if (
        parsedScope?.type === "project" &&
        projectsList.some((p) => p.id === parsedScope.id)
      ) {
        setSelectedProjectId(parsedScope.id);
        setSelectedScope(parsedScope);
      } else if (
        savedProject &&
        projectsList.some((p) => p.id === savedProject)
      ) {
        setSelectedProjectId(savedProject);
        setSelectedScope({ type: "project", id: savedProject });
      } else if (activeProjects.length > 0) {
        setSelectedProjectId(activeProjects[0].id);
        setSelectedScope({ type: "project", id: activeProjects[0].id });
      } else if (spacesList.length > 0) {
        setSelectedScope({ type: "space", id: spacesList[0].id });
      }
    } catch (err) {
      if (isCurrentFetch()) console.error("データ取得失敗:", err);
    } finally {
      if (isCurrentFetch()) setLoading(false);
    }
  }, [refreshProjectsData, requestedAppId, requestedProjectId]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const retryDataFetch = useCallback(() => {
    void fetchAll();
  }, [fetchAll]);

  useEffect(() => {
    if (requestedAppId && selectedProjectId) setRightTab("apps");
  }, [requestedAppId, selectedProjectId]);

  useEffect(() => {
    setIsNavigationCollapsed(
      localStorage.getItem(PROJECTS_PAGE_NAV_COLLAPSED_KEY) === "true",
    );
  }, []);

  const toggleNavigationCollapsed = useCallback(() => {
    setIsNavigationCollapsed((prev) => {
      const next = !prev;
      localStorage.setItem(PROJECTS_PAGE_NAV_COLLAPSED_KEY, String(next));
      return next;
    });
  }, []);

  useEffect(() => {
    if (selectedScope) {
      localStorage.setItem(
        "projectsPageSelectedScope",
        JSON.stringify(selectedScope),
      );
    }
    if (selectedScope?.type === "project") {
      setSelectedProjectId(selectedScope.id);
      localStorage.setItem("projectsPageSelected", selectedScope.id);
    }
  }, [selectedScope]);

  // === スペース操作 ===
  const handleCreateSpace = useCallback(async () => {
    if (!newSpaceName.trim()) return;
    setCreatingSpace(true);
    try {
      await apiFetch("/api/spaces", {
        method: "POST",
        body: JSON.stringify({ name: newSpaceName.trim() }),
      });
      setNewSpaceName("");
      setShowCreateSpace(false);
      await fetchAll();
      refreshSpaces();
    } catch (err) {
      console.error("スペース作成失敗:", err);
    } finally {
      setCreatingSpace(false);
    }
  }, [newSpaceName, fetchAll, refreshSpaces]);

  const handleUpdateSpace = useCallback(async () => {
    if (!editingSpaceId || !editSpaceName.trim()) return;
    try {
      await apiFetch(`/api/spaces/${editingSpaceId}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: editSpaceName.trim(),
          color: editSpaceColor || null,
        }),
      });
      setEditingSpaceId(null);
      await fetchAll();
      refreshSpaces();
    } catch (err) {
      console.error("スペース更新失敗:", err);
    }
  }, [editingSpaceId, editSpaceName, editSpaceColor, fetchAll, refreshSpaces]);

  const handleDeleteSpace = useCallback(
    async (id: string) => {
      if (
        !(await confirm({
          description: "このスペースと配下のプロジェクトを削除しますか？",
          destructive: true,
        }))
      )
        return;
      try {
        await apiFetch(`/api/spaces/${id}`, { method: "DELETE" });
        await fetchAll();
        refreshSpaces();
      } catch (err) {
        console.error("スペース削除失敗:", err);
      }
    },
    [fetchAll, refreshSpaces, confirm],
  );

  // === プロジェクト操作 ===
  const handleCreate = useCallback(async () => {
    if (!newName.trim()) return;
    setCreating(true);
    setCreateError("");
    try {
      const parsedAliases = newAliases
        .split(",")
        .map((s) => s.trim().toLowerCase())
        .filter(Boolean);
      await apiFetch("/api/projects", {
        method: "POST",
        body: JSON.stringify({
          name: newName.trim(),
          description: newDesc.trim() || null,
          aliases: parsedAliases,
          color: newColor || null,
          estimated_hours: newEstHours ? parseFloat(newEstHours) : null,
          space_id: createSpaceId || null,
        }),
      });
      setNewName("");
      setNewDesc("");
      setNewAliases("");
      setNewEstHours("");
      setNewColor("#3b82f6");
      setShowCreateForm(false);
      setCreateSpaceId(null);
      await fetchAll();
      refreshProjects();
    } catch (err) {
      console.error("プロジェクト作成失敗:", err);
      setCreateError(
        err instanceof Error ? err.message : "プロジェクト作成に失敗しました",
      );
    } finally {
      setCreating(false);
    }
  }, [
    newName,
    newDesc,
    newAliases,
    newColor,
    newEstHours,
    createSpaceId,
    fetchAll,
    refreshProjects,
  ]);

  const handleUpdate = useCallback(async () => {
    if (!editingId || !editName.trim()) return;
    setSaving(true);
    try {
      const parsedAliases = editAliases
        .split(",")
        .map((s) => s.trim().toLowerCase())
        .filter(Boolean);
      await apiFetch(`/api/projects/${editingId}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: editName.trim(),
          description: editDesc.trim() || null,
          aliases: parsedAliases,
          color: editColor || null,
          estimated_hours: editEstHours ? parseFloat(editEstHours) : null,
          space_id: editSpaceIdField || null,
          metadata: {
            workspace_tools_enabled: editWorkspaceToolsEnabled,
          },
        }),
      });
      setEditingId(null);
      await fetchAll();
      refreshProjects();
    } catch (err) {
      console.error("プロジェクト更新失敗:", err);
    } finally {
      setSaving(false);
    }
  }, [
    editingId,
    editName,
    editDesc,
    editAliases,
    editColor,
    editEstHours,
    editSpaceIdField,
    editWorkspaceToolsEnabled,
    fetchAll,
    refreshProjects,
  ]);

  const handleDelete = useCallback(
    async (id: string) => {
      if (
        !(await confirm({
          description: "このプロジェクトを削除しますか？",
          destructive: true,
        }))
      )
        return;
      try {
        await apiFetch(`/api/projects/${id}`, { method: "DELETE" });
        if (selectedProjectId === id) {
          setSelectedProjectId(null);
          setSelectedScope(null);
        }
        await fetchAll();
        refreshProjects();
      } catch (err) {
        console.error("プロジェクト削除失敗:", err);
      }
    },
    [selectedProjectId, fetchAll, refreshProjects, confirm],
  );

  const toggleSpace = useCallback((spaceId: string) => {
    setExpandedSpaces((prev) => {
      const next = new Set(prev);
      if (next.has(spaceId)) {
        next.delete(spaceId);
      } else {
        next.add(spaceId);
      }
      return next;
    });
  }, []);

  const toggleClosedSpace = useCallback((spaceId: string) => {
    setExpandedClosedSpaces((prev) => {
      const next = new Set(prev);
      if (next.has(spaceId)) {
        next.delete(spaceId);
      } else {
        next.add(spaceId);
      }
      return next;
    });
  }, []);

  const selectProject = useCallback((projectId: string) => {
    setSelectedProjectId(projectId);
    setSelectedScope({ type: "project", id: projectId });
    setRightTab("dashboard");
    const params = new URLSearchParams(searchParams.toString());
    params.set("project_id", projectId);
    params.delete("app_id");
    router.replace(`/projects?${params.toString()}`, { scroll: false });
  }, [router, searchParams]);

  const selectSpace = useCallback((spaceId: string) => {
    setSelectedScope({ type: "space", id: spaceId });
    setRightTab("dashboard");
    const params = new URLSearchParams(searchParams.toString());
    params.delete("project_id");
    params.delete("app_id");
    const query = params.toString();
    router.replace(query ? `/projects?${query}` : "/projects", { scroll: false });
  }, [router, searchParams]);

  const selectedProject =
    selectedScope?.type === "project"
      ? projects.find((p) => p.id === selectedScope.id)
      : undefined;
  const selectedSpace =
    selectedScope?.type === "space"
      ? spaces.find((s) => s.id === selectedScope.id)
      : undefined;
  const selectedProjectIsInbox = selectedProject?.metadata?.isInboxDefault === true
    || selectedProject?.slug === `inbox-project-${selectedProject?.owner_id ?? ""}`;
  // The Projects page is backed by the Next.js `/api/projects` route, which
  // serializes the effective project write permission for the current user.
  // Keep management controls/actions behind that canonical value; do not
  // infer write access from an absent field (Python Project.to_dict() does not
  // carry this field).
  const selectedProjectCanWrite = selectedProject?.can_write === true;
  const projectManagementFiles = useProjectManagementFiles({
    projectId: selectedScope?.type === "project" ? selectedScope.id : null,
    enabled:
      rightTab === "management" &&
      selectedProjectCanWrite &&
      !selectedProjectIsInbox,
  });
  const canonicalProjectDocsNodeId = typeof selectedProject?.knowledge_node_id === "string"
    ? selectedProject.knowledge_node_id.trim() || null
    : null;
  const canonicalProjectDocsPointerValid = Boolean(
    canonicalProjectDocsNodeId
      && selectedProject?.knowledge_node_id_valid !== false
      && selectedProject?.knowledge_node_id_validated !== false,
  );

  // スペースごとにプロジェクトをグループ化する。スペース取得に失敗した
  // 場合でも、成功したプロジェクト一覧を「未分類」として表示する。
  const {
    activeProjectsBySpace,
    closedProjectsBySpace,
    activeProjectsWithoutKnownSpace,
    closedProjectsWithoutKnownSpace,
  } = useMemo(() => {
    const active = new Map<string, ProjectInfo[]>();
    const closed = new Map<string, ProjectInfo[]>();
    const activeWithoutKnownSpace: ProjectInfo[] = [];
    const closedWithoutKnownSpace: ProjectInfo[] = [];
    const knownSpaceIds = new Set(spaces.map((space) => space.id));
    for (const p of projects) {
      if (!p.space_id || !knownSpaceIds.has(p.space_id)) {
        (p.is_completed ? closedWithoutKnownSpace : activeWithoutKnownSpace).push(p);
        continue;
      }
      const target = p.is_completed ? closed : active;
      const list = target.get(p.space_id) || [];
      list.push(p);
      target.set(p.space_id, list);
    }
    return {
      activeProjectsBySpace: active,
      closedProjectsBySpace: closed,
      activeProjectsWithoutKnownSpace: activeWithoutKnownSpace,
      closedProjectsWithoutKnownSpace: closedWithoutKnownSpace,
    };
  }, [projects, spaces]);

  // プロジェクトカードのレンダリング
  const renderProjectCard = useCallback((project: ProjectInfo) => {
    const isSelected =
      selectedScope?.type === "project" && selectedScope.id === project.id;
    return (
    <li
      key={project.id}
      className={`group/project relative cursor-pointer rounded-md border-l-2 px-3 py-2.5 transition-colors ${
        isSelected
          ? "border-primary bg-surface-container-high text-on-surface shadow-sm"
          : "border-transparent text-on-surface-variant hover:bg-surface-container"
      }`}
    >
      {editingId === project.id ? (
        <div className="space-y-2" onClick={(e) => e.stopPropagation()}>
          <Input
            value={editName}
            onChange={(e) => setEditName(e.target.value)}
            className="h-7 text-sm"
            autoFocus
          />
          <Textarea
            value={editDesc}
            onChange={(e) => setEditDesc(e.target.value)}
            className="min-h-12 text-xs"
          />
          <Input
            placeholder="エイリアス（カンマ区切り 例: tokyo, fy25）"
            value={editAliases}
            onChange={(e) => setEditAliases(e.target.value)}
            className="h-7 text-xs"
          />
          <ProjectColorPicker
            value={editColor}
            onChange={setEditColor}
            inputClassName="h-7"
          />
          <Input
            type="number"
            placeholder="見積工数（時間）"
            value={editEstHours}
            onChange={(e) => setEditEstHours(e.target.value)}
            className="h-7 text-xs"
            min="0"
            step="0.5"
          />
          <AppSelect
            value={editSpaceIdField}
            onChange={(e) => setEditSpaceIdField(e.target.value)}
            className="h-7 w-full rounded border border-input bg-transparent px-2 text-xs outline-none"
          >
            <option value="">スペースなし</option>
            {spaces.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </AppSelect>
          <label className="flex cursor-pointer items-start gap-2 rounded border border-amber-500/30 bg-amber-500/5 p-2">
            <Checkbox
              checked={editWorkspaceToolsEnabled}
              onCheckedChange={(checked) =>
                setEditWorkspaceToolsEnabled(checked === true)
              }
              aria-label="プロジェクトツールを有効にする"
              className="mt-0.5"
            />
            <span className="min-w-0">
              <span className="block text-xs font-medium">
                プロジェクトツールを有効にする
              </span>
              <span className="mt-0.5 block text-[11px] leading-relaxed text-muted-foreground">
                tools/ 配下のプログラムをエージェントが実行できるようにします。信頼できるツールだけを配置してください。
              </span>
            </span>
          </label>
          <div className="flex gap-1">
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              onClick={handleUpdate}
              disabled={saving}
              aria-label={`${project.name}を保存`}
              title={`${project.name}を保存`}
            >
              <Check className="size-3" />
            </Button>
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              onClick={() => setEditingId(null)}
              aria-label={`${project.name}の編集をキャンセル`}
              title={`${project.name}の編集をキャンセル`}
            >
              <X className="size-3" />
            </Button>
          </div>
        </div>
      ) : (
        <>
          <button
            type="button"
            aria-current={isSelected ? "page" : undefined}
            aria-label={`${project.name}を選択`}
            className="absolute inset-0 z-0 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-charcoal"
            onClick={() => selectProject(project.id)}
          />
          <div className="pointer-events-none relative z-10">
          <div className="flex items-start justify-between gap-2">
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <FolderOpen
                className={`size-3.5 shrink-0 ${project.is_completed ? "text-green-500" : "text-on-surface-variant"}`}
                style={
                  project.color
                    ? { color: project.color }
                    : undefined
                }
              />
              <span
                className={`min-w-0 flex-1 truncate text-[13px] font-medium ${
                  isSelected ? "font-semibold text-on-surface" : ""
                } ${project.is_completed ? "line-through text-muted-foreground" : ""}`}
              >
                {project.name}
              </span>
              {project.is_completed && (
                <span className="shrink-0 text-[10px] text-green-600 dark:text-green-400 font-medium">
                  完了
                </span>
              )}
            </div>
            <div className="pointer-events-auto flex shrink-0 gap-0.5 text-on-surface-variant">
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                className={project.is_completed ? "text-green-500" : "text-on-surface-variant"}
                title={
                  project.is_completed
                    ? `${project.name}の完了を解除`
                    : `${project.name}を完了としてマーク`
                }
                aria-label={
                  project.is_completed
                    ? `${project.name}の完了を解除`
                    : `${project.name}を完了としてマーク`
                }
                onClick={async (e) => {
                  e.stopPropagation();
                  try {
                    await apiFetch(`/api/projects/${project.id}`, {
                      method: "PATCH",
                      body: JSON.stringify({
                        is_completed: !project.is_completed,
                      }),
                    });
                    await fetchAll();
                    refreshProjects();
                  } catch (err) {
                    console.error("プロジェクト完了状態の更新失敗:", err);
                  }
                }}
              >
                {project.is_completed ? (
                  <CheckCircle2 className="size-3" />
                ) : (
                  <Circle className="size-3" />
                )}
              </Button>
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                aria-label={`${project.name}を編集`}
                title={`${project.name}を編集`}
                onClick={(e) => {
                  e.stopPropagation();
                  setEditingId(project.id);
                  setEditName(project.name);
                  setEditDesc(project.description || "");
                  setEditAliases((project.aliases || []).join(", "));
                  setEditColor(project.color || "#3b82f6");
                  setEditEstHours(
                    project.estimated_hours != null
                      ? String(project.estimated_hours)
                      : "",
                  );
                  setEditSpaceIdField(project.space_id || "");
                  setEditWorkspaceToolsEnabled(
                    project.metadata?.workspace_tools_enabled === true,
                  );
                }}
              >
                <Pencil className="size-3" />
              </Button>
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                className="text-destructive"
                aria-label={`${project.name}を削除`}
                title={`${project.name}を削除`}
                onClick={(e) => {
                  e.stopPropagation();
                  handleDelete(project.id);
                }}
              >
                <Trash2 className="size-3" />
              </Button>
            </div>
          </div>
          {project.aliases && project.aliases.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1 ml-6">
              {project.aliases.map((alias) => (
                <span
                  key={alias}
                  className="inline-block rounded bg-surface-container-high px-1.5 py-0.5 text-[10px] text-on-surface-variant"
                >
                  {alias}
                </span>
              ))}
            </div>
          )}
          {project.estimated_hours != null && (
            <p className="text-[10px] text-muted-foreground mt-0.5 ml-6">
              見積: {project.estimated_hours}h
            </p>
          )}
          {project.description && (
            <p className="text-xs text-muted-foreground mt-1 line-clamp-2 ml-6">
              {project.description}
            </p>
          )}
          </div>
        </>
      )}
    </li>
    );
  }, [
    editAliases,
    editColor,
    editDesc,
    editEstHours,
    editName,
    editSpaceIdField,
    editWorkspaceToolsEnabled,
    editingId,
    fetchAll,
    handleDelete,
    handleUpdate,
    refreshProjects,
    saving,
    selectProject,
    selectedScope,
    spaces,
  ]);


  const projectNavigation = useMemo(() => (
    <aside
      className="ao-workspace-nav-panel"
      data-shell-slot="workspace-navigation"
      data-workspace="projects"
    >
      <div className="flex h-full min-h-0 flex-col overflow-auto bg-surface-charcoal text-on-surface">
      <div className="border-b border-border-subtle bg-surface-charcoal px-4 py-4">
        <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="grid size-7 shrink-0 place-items-center rounded border border-border-subtle bg-surface-container-high text-primary">
              <Layers className="size-3.5" aria-hidden="true" />
            </span>
            <h1 className="truncate text-base font-semibold tracking-tight">Projects</h1>
          </div>
          <p className="mt-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-on-surface-variant">Space navigation</p>
        </div>
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={
                  isNavigationCollapsed
                    ? "スペースとプロジェクトのリストを表示"
                    : "スペースとプロジェクトのリストを畳む"
                }
                aria-expanded={!isNavigationCollapsed}
                aria-controls="projects-navigation-list"
                onClick={toggleNavigationCollapsed}
              >
                {isNavigationCollapsed ? (
                  <PanelLeftOpen className="size-4" />
                ) : (
                  <PanelLeftClose className="size-4" />
                )}
              </Button>
            }
          />
          <TooltipContent>
            {isNavigationCollapsed
              ? "スペースとプロジェクトのリストを表示"
              : "スペースとプロジェクトのリストを畳む"}
          </TooltipContent>
        </Tooltip>
      </div>
      </div>

        <div
          className={
            isNavigationCollapsed
              ? "hidden"
              : "w-full min-h-0 flex-1 space-y-3 overflow-auto px-3 py-4"
          }
          id="projects-navigation-list"
        >
          {/* スペース作成ボタン */}
          <div className="grid grid-cols-2 gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-8 border-border-subtle bg-surface-container-low px-2 text-[11px] text-on-surface hover:bg-surface-container-high"
              onClick={() => setShowCreateSpace(!showCreateSpace)}
            >
              <Layers className="size-3.5 mr-1" />
              新規スペース
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-8 border-border-subtle bg-surface-container-low px-2 text-[11px] text-on-surface hover:bg-surface-container-high"
              onClick={() => {
                setShowCreateForm(!showCreateForm);
                if (spaces.length > 0 && !createSpaceId) {
                  setCreateSpaceId(spaces[0].id);
                }
              }}
            >
              <Plus className="size-3.5 mr-1" />
              新規プロジェクト
            </Button>
          </div>

          {/* スペース作成フォーム */}
          {showCreateSpace && (
            <Card size="sm" className="border-border-subtle bg-surface-container-low">
              <CardContent className="space-y-2 pt-4">
                <Input
                  placeholder="スペース名"
                  value={newSpaceName}
                  onChange={(e) => setNewSpaceName(e.target.value)}
                  className="h-8"
                  autoFocus
                  onKeyDown={(e) => e.key === "Enter" && handleCreateSpace()}
                />
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    onClick={handleCreateSpace}
                    disabled={creatingSpace || !newSpaceName.trim()}
                  >
                    {creatingSpace && (
                      <Loader2 className="size-3 animate-spin mr-1" />
                    )}
                    作成
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setShowCreateSpace(false);
                      setNewSpaceName("");
                    }}
                  >
                    キャンセル
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}

          {/* プロジェクト作成フォーム */}
          {showCreateForm && (
            <Card size="sm" className="border-border-subtle bg-surface-container-low">
              <CardContent className="space-y-2 pt-4">
                {createError ? (
                  <p className="text-sm text-destructive">{createError}</p>
                ) : null}
                <AppSelect
                  value={createSpaceId || ""}
                  onChange={(e) => setCreateSpaceId(e.target.value || null)}
                  className="h-8 w-full rounded border border-input bg-transparent px-2 text-sm outline-none"
                >
                  <option value="">スペースなし</option>
                  {spaces.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </AppSelect>
                <Input
                  placeholder="プロジェクト名"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  className="h-8"
                  autoFocus
                />
                <Textarea
                  placeholder="説明（任意）"
                  value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                  className="min-h-16 text-sm"
                />
                <Input
                  placeholder="エイリアス（カンマ区切り）"
                  value={newAliases}
                  onChange={(e) => setNewAliases(e.target.value)}
                  className="h-8"
                />
                <ProjectColorPicker value={newColor} onChange={setNewColor} />
                <Input
                  type="number"
                  placeholder="見積工数（時間）"
                  value={newEstHours}
                  onChange={(e) => setNewEstHours(e.target.value)}
                  className="h-8"
                  min="0"
                  step="0.5"
                />
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    onClick={handleCreate}
                    disabled={creating || !newName.trim()}
                  >
                    {creating && (
                      <Loader2 className="size-3 animate-spin mr-1" />
                    )}
                    作成
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setShowCreateForm(false);
                      setCreateError("");
                      setNewName("");
                      setNewDesc("");
                      setNewAliases("");
                      setNewEstHours("");
                      setNewColor("#3b82f6");
                      setCreateSpaceId(null);
                    }}
                  >
                    キャンセル
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}

          {/* スペースごとのプロジェクト一覧 */}
          <nav aria-label="スペースとプロジェクト" className="space-y-3">
          {(hasResourceError || !resourcesReady) && (
            <div
              role="alert"
              className="mb-3 space-y-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs"
            >
              <p className="font-medium">
                {hasResourceError ? "一覧を取得できませんでした" : "一覧を読み込み中です…"}
              </p>
              {spacesError && (
                <p>スペース: {spacesError.message || "取得に失敗しました"}</p>
              )}
              {projectsError && (
                <p>プロジェクト: {projectsError.message || "取得に失敗しました"}</p>
              )}
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-7 px-2 text-xs"
                onClick={retryDataFetch}
              >
                再取得
              </Button>
            </div>
          )}
          {spaces.map((space) => {
            const activeProjects = activeProjectsBySpace.get(space.id) || [];
            const closedProjects = closedProjectsBySpace.get(space.id) || [];
            const isExpanded = expandedSpaces.has(space.id);
            const isClosedExpanded = expandedClosedSpaces.has(space.id);

            return (
              <div
                key={space.id}
                className={`group/space overflow-hidden rounded-lg border bg-surface-container-low transition-colors ${
                  selectedScope?.type === "space" &&
                  selectedScope.id === space.id
                    ? "border-primary/70 shadow-sm"
                    : "border-border-subtle"
                }`}
              >
                {/* スペースヘッダー */}
                <div
                  className={`flex items-center justify-between border-b border-border-subtle/70 px-3 py-2.5 ${
                    selectedScope?.type === "space" &&
                    selectedScope.id === space.id
                      ? "bg-surface-container-high"
                      : "bg-surface-container"
                  }`}
                >
                  {editingSpaceId === space.id ? (
                    <div className="min-w-0 flex-1 space-y-1.5 text-sm font-medium">
                      <div className="flex min-w-0 items-center gap-1.5">
                        <Layers
                          className="size-3.5 shrink-0 text-muted-foreground"
                          style={
                            editSpaceColor
                              ? { color: editSpaceColor }
                              : undefined
                          }
                        />
                        <Input
                          value={editSpaceName}
                          onChange={(e) => setEditSpaceName(e.target.value)}
                          className="h-6 min-w-0 flex-1 text-sm"
                          autoFocus
                          onClick={(e) => e.stopPropagation()}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") handleUpdateSpace();
                            if (e.key === "Escape") setEditingSpaceId(null);
                          }}
                        />
                        <Badge
                          variant="secondary"
                          className="shrink-0 px-1.5 py-0 text-[10px]"
                        >
                          {activeProjects.length}
                        </Badge>
                      </div>
                      <div className="flex min-w-0 items-start gap-1.5 pl-5">
                        <span className="pt-1 text-[10px] text-muted-foreground">
                          色
                        </span>
                        <ResourceColorPicker
                          value={editSpaceColor}
                          onChange={setEditSpaceColor}
                          inputClassName="h-6"
                          compact
                          showLabel={false}
                        />
                      </div>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-1 py-1 text-left text-sm font-semibold text-on-surface outline-none transition-colors hover:text-primary focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-charcoal"
                      onClick={() => selectSpace(space.id)}
                      aria-current={
                        selectedScope?.type === "space" &&
                        selectedScope.id === space.id
                          ? "page"
                          : undefined
                      }
                    >
                      <span
                        aria-hidden="true"
                        className="size-1.5 shrink-0 rounded-full bg-primary/60"
                        style={space.color ? { backgroundColor: space.color } : undefined}
                      />
                      <Layers
                        className="size-4 shrink-0 text-on-surface-variant"
                        style={space.color ? { color: space.color } : undefined}
                      />
                      <span className="min-w-0 truncate">{space.name}</span>
                      <Badge
                        variant="secondary"
                        className="text-[10px] px-1.5 py-0"
                      >
                        {activeProjects.length}
                      </Badge>
                    </button>
                  )}
                  <div className="flex gap-0.5 text-on-surface-variant">
                    <Button
                      type="button"
                      size="icon-sm"
                      variant="ghost"
                      aria-label={
                        isExpanded
                          ? `${space.name}のプロジェクトを折り畳む`
                          : `${space.name}のプロジェクトを展開`
                      }
                      title={
                        isExpanded
                          ? `${space.name}のプロジェクトを折り畳む`
                          : `${space.name}のプロジェクトを展開`
                      }
                      aria-expanded={isExpanded}
                      aria-controls={`space-projects-${space.id}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleSpace(space.id);
                      }}
                    >
                      {isExpanded ? (
                        <ChevronDown className="size-3" />
                      ) : (
                        <ChevronRight className="size-3" />
                      )}
                    </Button>
                    {editingSpaceId === space.id ? (
                      <>
                        <Button
                          type="button"
                          size="icon-sm"
                          variant="ghost"
                          onClick={handleUpdateSpace}
                          aria-label={`${space.name}を保存`}
                          title={`${space.name}を保存`}
                        >
                          <Check className="size-3" />
                        </Button>
                        <Button
                          type="button"
                          size="icon-sm"
                          variant="ghost"
                          onClick={() => setEditingSpaceId(null)}
                          aria-label={`${space.name}の編集をキャンセル`}
                          title={`${space.name}の編集をキャンセル`}
                        >
                          <X className="size-3" />
                        </Button>
                      </>
                    ) : (
                      <>
                        <Button
                          type="button"
                          size="icon-sm"
                          variant="ghost"
                          aria-label={`${space.name}を編集`}
                          title={`${space.name}を編集`}
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingSpaceId(space.id);
                            setEditSpaceName(space.name);
                            setEditSpaceColor(space.color || "#3b82f6");
                          }}
                        >
                          <Pencil className="size-3" />
                        </Button>
                        <Button
                          type="button"
                          size="icon-sm"
                          variant="ghost"
                          className="text-destructive"
                          aria-label={`${space.name}を削除`}
                          title={`${space.name}を削除`}
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDeleteSpace(space.id);
                          }}
                        >
                          <Trash2 className="size-3" />
                        </Button>
                      </>
                    )}
                  </div>
                </div>

                {/* プロジェクトリスト */}
                {isExpanded && (
                  <div
                    id={`space-projects-${space.id}`}
                    className="space-y-3 bg-surface-container-lowest px-3 py-3"
                  >
                    <div className="space-y-3 border-l border-border-subtle/70 pl-3">
                      <ul
                        className="space-y-1"
                        aria-label={`${space.name}のプロジェクト`}
                      >
                        {activeProjects.length === 0 &&
                        closedProjects.length === 0 ? (
                          <li className="px-2 py-1 text-xs text-muted-foreground">
                            プロジェクトなし
                          </li>
                        ) : (
                          activeProjects.map(renderProjectCard)
                        )}
                      </ul>

                      {closedProjects.length > 0 && (
                        <div className="space-y-2">
                          <button
                            type="button"
                            className="flex w-full items-center justify-between rounded-md border border-border-subtle bg-surface-container px-2.5 py-2 text-left text-xs text-on-surface-variant transition-colors hover:bg-surface-container-high focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            aria-expanded={isClosedExpanded}
                            aria-controls={`space-closed-projects-${space.id}`}
                            aria-label={`${space.name}の完了プロジェクトを${isClosedExpanded ? "折り畳む" : "展開"}`}
                            title={`${space.name}の完了プロジェクトを${isClosedExpanded ? "折り畳む" : "展開"}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              toggleClosedSpace(space.id);
                            }}
                          >
                            <span className="flex min-w-0 items-center gap-1.5 font-medium">
                              {isClosedExpanded ? (
                                <ChevronDown className="size-3 shrink-0" />
                              ) : (
                                <ChevronRight className="size-3 shrink-0" />
                              )}
                              <CheckCircle2 className="size-3.5 shrink-0 text-green-500" />
                              <span className="truncate">Closed</span>
                            </span>
                            <Badge
                              variant="secondary"
                              className="bg-surface-container-high px-1.5 py-0 text-[10px] text-on-surface-variant"
                            >
                              {closedProjects.length}
                            </Badge>
                          </button>

                          {isClosedExpanded && (
                            <ul
                              id={`space-closed-projects-${space.id}`}
                              className="space-y-1 border-l border-border-subtle/70 pl-3"
                              aria-label={`${space.name}の完了プロジェクト`}
                            >
                              {closedProjects.map(renderProjectCard)}
                            </ul>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}

          {(activeProjectsWithoutKnownSpace.length > 0 ||
            closedProjectsWithoutKnownSpace.length > 0) && (
            <div className="rounded-lg border border-border-subtle bg-surface-container-low p-3">
              <div className="flex items-center gap-2 px-1 py-1 text-sm font-semibold text-on-surface">
                <FolderOpen className="size-3.5 text-on-surface-variant" />
                <span className="truncate">スペース未分類</span>
                <Badge
                  variant="secondary"
                  className="bg-surface-container-high px-1.5 py-0 text-[10px] text-on-surface-variant"
                >
                  {activeProjectsWithoutKnownSpace.length +
                    closedProjectsWithoutKnownSpace.length}
                </Badge>
              </div>
              <div className="mt-2 border-l border-border-subtle/70 pl-3">
                <ul className="space-y-1" aria-label="スペース未分類のプロジェクト">
                  {activeProjectsWithoutKnownSpace.map(renderProjectCard)}
                  {closedProjectsWithoutKnownSpace.map(renderProjectCard)}
                </ul>
              </div>
            </div>
          )}

          {canShowEmptyState && (
            <p className="px-2 text-sm text-muted-foreground">
              スペースまたはプロジェクトを作成してください
            </p>
          )}
          </nav>
        </div>

      </div>
    </aside>
  ), [
    activeProjectsBySpace,
    activeProjectsWithoutKnownSpace,
    canShowEmptyState,
    closedProjectsBySpace,
    closedProjectsWithoutKnownSpace,
    createError,
    createSpaceId,
    creating,
    creatingSpace,
    editingSpaceId,
    editSpaceColor,
    editSpaceName,
    expandedClosedSpaces,
    expandedSpaces,
    hasResourceError,
    isNavigationCollapsed,
    newAliases,
    newColor,
    newDesc,
    newEstHours,
    newName,
    newSpaceName,
    projectsError,
    resourcesReady,
    renderProjectCard,
    retryDataFetch,
    selectSpace,
    selectedScope,
    showCreateForm,
    showCreateSpace,
    spaces,
    spacesError,
    toggleClosedSpace,
    toggleNavigationCollapsed,
    toggleSpace,
    handleCreate,
    handleCreateSpace,
    handleDeleteSpace,
    handleUpdateSpace,
  ]);

  const projectContextRail = useMemo(() => {
    if (selectedProject) {
      const toolsEnabled = selectedProject.metadata?.workspace_tools_enabled === true;
      return (
        <div className="flex h-full min-h-0 flex-col bg-card text-card-foreground">
          <div className="border-b border-border px-4 py-5">
            <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
              Project details
            </p>
            <h2 className="mt-2 truncate text-lg font-semibold">{selectedProject.name}</h2>
            {selectedProject.description ? (
              <p className="mt-2 text-sm leading-5 text-muted-foreground">{selectedProject.description}</p>
            ) : null}
          </div>
          <div className="space-y-4 overflow-auto px-4 py-5">
            <dl className="space-y-3 text-sm">
              {selectedProject.owner_id ? (
                <div>
                  <dt className="text-xs uppercase tracking-[0.1em] text-muted-foreground">Owner</dt>
                  <dd className="mt-1 truncate">{selectedProject.owner_id}</dd>
                </div>
              ) : null}
              {selectedProject.created_at ? (
                <div>
                  <dt className="text-xs uppercase tracking-[0.1em] text-muted-foreground">Created</dt>
                  <dd className="mt-1">{new Date(selectedProject.created_at).toLocaleDateString("ja-JP")}</dd>
                </div>
              ) : null}
              {selectedProject.estimated_hours != null ? (
                <div>
                  <dt className="text-xs uppercase tracking-[0.1em] text-muted-foreground">Estimate</dt>
                  <dd className="mt-1">{selectedProject.estimated_hours}h</dd>
                </div>
              ) : null}
              <div>
                <dt className="text-xs uppercase tracking-[0.1em] text-muted-foreground">Workspace tools</dt>
                <dd className="mt-1">{toolsEnabled ? "Enabled" : "Disabled"}</dd>
              </div>
            </dl>
            <div className="border-t border-border pt-4">
              <p className="text-xs uppercase tracking-[0.1em] text-muted-foreground">Actions</p>
              <div className="mt-3 grid gap-2">
                <button
                  type="button"
                  className="rounded border border-border px-3 py-2 text-left text-sm transition-colors hover:border-primary hover:text-primary"
                  onClick={() => setRightTab("information")}
                >
                  Open project information
                </button>
                {selectedProjectCanWrite && !selectedProjectIsInbox && (
                  <button
                    type="button"
                    className="rounded border border-border px-3 py-2 text-left text-sm transition-colors hover:border-primary hover:text-primary"
                    onClick={() => setRightTab("management")}
                  >
                    Open management
                  </button>
                )}
                {canonicalProjectDocsPointerValid ? (
                  <Link
                    href={`/docs/${encodeURIComponent(canonicalProjectDocsNodeId ?? "")}`}
                    className="rounded border border-border px-3 py-2 text-left text-sm transition-colors hover:border-primary hover:text-primary"
                  >
                    Open canonical Docs
                  </Link>
                ) : null}
              </div>
            </div>
          </div>
        </div>
      );
    }
    if (selectedSpace) {
      const spaceProjects = projects.filter((project) => project.space_id === selectedSpace.id);
      return (
        <div className="flex h-full min-h-0 flex-col bg-card text-card-foreground">
          <div className="border-b border-border px-4 py-5">
            <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">Space details</p>
            <h2 className="mt-2 truncate text-lg font-semibold">{selectedSpace.name}</h2>
            {selectedSpace.description ? (
              <p className="mt-2 text-sm leading-5 text-muted-foreground">{selectedSpace.description}</p>
            ) : null}
          </div>
          <div className="space-y-4 overflow-auto px-4 py-5">
            <dl className="space-y-3 text-sm">
              <div className="flex items-center justify-between gap-3 border-b border-border pb-2">
                <dt className="text-muted-foreground">Projects</dt>
                <dd className="tabular-nums">{spaceProjects.length}</dd>
              </div>
              <div className="flex items-center justify-between gap-3">
                <dt className="text-muted-foreground">Active</dt>
                <dd className="tabular-nums">{spaceProjects.filter((project) => !project.is_completed).length}</dd>
              </div>
            </dl>
            <button
              type="button"
              className="w-full rounded border border-border px-3 py-2 text-left text-sm transition-colors hover:border-primary hover:text-primary"
              onClick={() => setRightTab("tags")}
            >
              Manage Space tags
            </button>
          </div>
        </div>
      );
    }
    return null;
  }, [
    canonicalProjectDocsNodeId,
    canonicalProjectDocsPointerValid,
    projects,
    selectedProject,
    selectedProjectCanWrite,
    selectedProjectIsInbox,
    selectedSpace,
  ]);

  useWorkspaceShellRegistration({
    id: "projects-workspace",
    workspaceNavigation: projectNavigation,
    contextRail: projectContextRail,
    priority: 20,
  });

  if (loading) {
    return (
      <div className="p-4 space-y-4">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-24 w-full rounded-xl" />
        ))}
      </div>
    );
  }

  return (
    <div className="flex h-full min-w-0 flex-col overflow-auto bg-background px-4 py-5 lg:px-6">
        {/* 右: ダッシュボード / 詳細管理 */}
        <div className="min-w-0 flex-1 overflow-visible lg:overflow-auto">
          {selectedScope && (selectedProject || selectedSpace) ? (
            <div className="flex h-full flex-col">
              <div className="mb-4 flex flex-wrap items-end justify-between gap-3 border-b border-border pb-4">
                <div className="min-w-0">
                  <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
                    Projects / {selectedProject ? "Project" : "Space"}
                  </p>
                  <h1 className="mt-1 truncate text-2xl font-semibold tracking-tight">
                    {selectedProject?.name || selectedSpace?.name}
                  </h1>
                  {(selectedProject?.description || selectedSpace?.description) ? (
                    <p className="mt-1 max-w-3xl truncate text-sm text-muted-foreground">
                      {selectedProject?.description || selectedSpace?.description}
                    </p>
                  ) : null}
                </div>
                {selectedProject?.can_write === false ? (
                  <span className="rounded border border-border px-2 py-1 text-xs text-muted-foreground">
                    Read-only
                  </span>
                ) : null}
              </div>
              {/* タブヘッダー */}
              <div className="mb-5 flex flex-wrap items-center gap-1 border-b border-border">
                <button
                  className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                    rightTab === "dashboard"
                      ? "border-primary text-primary"
                      : "border-transparent text-muted-foreground hover:text-foreground"
                  }`}
                  onClick={() => setRightTab("dashboard")}
                >
                  <LayoutDashboard className="size-3.5" />
                  ダッシュボード
                </button>
                {selectedProject && !selectedProjectIsInbox && (
                  <button
                    className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                      rightTab === "memory"
                        ? "border-primary text-primary"
                        : "border-transparent text-muted-foreground hover:text-foreground"
                    }`}
                    onClick={() => setRightTab("memory")}
                  >
                    <Brain className="size-3.5" />
                    メモリ
                  </button>
                )}
                {selectedProject && (
                  <button
                    className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                      rightTab === "information"
                        ? "border-primary text-primary"
                        : "border-transparent text-muted-foreground hover:text-foreground"
                    }`}
                    onClick={() => setRightTab("information")}
                  >
                    <BookOpen className="size-3.5" />
                    案件情報
                  </button>
                )}
                {selectedProject && !selectedProjectIsInbox && (
                  <button
                    className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                      rightTab === "knowledge"
                        ? "border-primary text-primary"
                        : "border-transparent text-muted-foreground hover:text-foreground"
                    }`}
                    onClick={() => setRightTab("knowledge")}
                  >
                    <Link2 className="size-3.5" />
                    Knowledge
                  </button>
                )}
                {selectedProject && (
                  <button
                    className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                      rightTab === "members"
                        ? "border-primary text-primary"
                        : "border-transparent text-muted-foreground hover:text-foreground"
                    }`}
                    onClick={() => setRightTab("members")}
                  >
                    <Users className="size-3.5" />
                    メンバー
                    </button>
                )}
                {selectedProject && selectedProjectCanWrite && !selectedProjectIsInbox && (
                  <button
                    className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                      rightTab === "management"
                        ? "border-primary text-primary"
                        : "border-transparent text-muted-foreground hover:text-foreground"
                    }`}
                    onClick={() => setRightTab("management")}
                  >
                    <Settings2 className="size-3.5" />
                    管理
                  </button>
                )}
                {selectedProject && !selectedProjectIsInbox && (
                  <button
                    className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                      rightTab === "apps"
                        ? "border-primary text-primary"
                        : "border-transparent text-muted-foreground hover:text-foreground"
                    }`}
                    onClick={() => setRightTab("apps")}
                  >
                    <Boxes className="size-3.5" />
                    アプリ
                  </button>
                )}
                {selectedSpace && (
                  <button
                    className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                      rightTab === "tags"
                        ? "border-primary text-primary"
                        : "border-transparent text-muted-foreground hover:text-foreground"
                    }`}
                    onClick={() => setRightTab("tags")}
                  >
                    <Tags className="size-3.5" />
                    タグ
                  </button>
                )}
                {selectedProject && !selectedProjectIsInbox && (
                  canonicalProjectDocsPointerValid ? (
                    <Link
                      href={`/docs/${encodeURIComponent(canonicalProjectDocsNodeId ?? "")}`}
                      className="ml-auto flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                    >
                      <FileText className="size-3.5" />
                      Docs
                    </Link>
                  ) : (
                    <button
                      type="button"
                      className="ml-auto flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                      onClick={() => setRightTab("information")}
                      title="案件情報タブを開く"
                    >
                      <FileText className="size-3.5" />
                      Docs
                    </button>
                  )
                )}
              </div>

              {/* タブコンテンツ */}
              {rightTab === "dashboard" ? (
                <div className="flex-1 overflow-auto">
                  {selectedSpace ? (
                    <SpaceOverviewPanel
                      space={selectedSpace}
                      projects={projects.filter(
                        (project) => project.space_id === selectedSpace.id,
                      )}
                      onSelectProject={selectProject}
                      onOpenTags={() => setRightTab("tags")}
                    />
                  ) : (
                    <ProjectDashboard scope={selectedScope} />
                  )}
                </div>
              ) : rightTab === "information" && selectedProject && !selectedProjectIsInbox ? (
                <div className="flex-1 overflow-auto">
                  <div className="flex min-h-0 flex-col gap-4 pb-8">
                    <ProjectInformationPanel project={selectedProject} />
                    <ProjectContextPackStatus
                      key={`context-pack-${selectedProject.id}`}
                      projectId={selectedProject.id}
                      canManageSettings={selectedProject.can_manage_settings === true}
                    />
                    <ProjectDocsCandidatesPanel
                      key={selectedProject.id}
                      projectId={selectedProject.id}
                      canManageSettings={selectedProject.can_manage_settings === true}
                    />
                  </div>
                </div>
              ) : rightTab === "knowledge" && selectedProject && !selectedProjectIsInbox ? (
                <div className="flex-1 overflow-auto">
                  <ProjectKnowledgePanel
                    projectId={selectedProject.id}
                    projectName={selectedProject.name}
                    canManageSettings={selectedProject.can_manage_settings === true}
                  />
                </div>
              ) : rightTab === "memory" && selectedProject && !selectedProjectIsInbox ? (
                <div className="flex-1 overflow-auto">
                  <ProjectMemoryPanel
                    projectId={selectedProject.id}
                    projectName={selectedProject.name}
                    canWrite={selectedProject.can_write !== false}
                  />
                </div>
              ) : rightTab === "tags" && selectedSpace ? (
                <div className="flex-1 overflow-auto">
                  <SpaceTagManagementPanel
                    space={selectedSpace}
                    spaces={spaces}
                    readOnly={
                      selectedSpace.source === "remote" ||
                      selectedSpace.can_write === false
                    }
                  />
                </div>
              ) : rightTab === "apps" && selectedProject && !selectedProjectIsInbox ? (
                <ProjectAppsPanel projectId={selectedProject.id} projectName={selectedProject.name} canWrite={selectedProject.can_write !== false} />
              ) : rightTab === "management" && selectedProject && selectedProjectCanWrite && !selectedProjectIsInbox ? (
                <ProjectManagementPanel
                  projectName={selectedProject.name}
                  controller={projectManagementFiles}
                />
              ) : rightTab === "members" && selectedProject ? (
                <ProjectMembersPanel
                  projectName={selectedProject.name}
                  controller={projectMembers}
                />
              ) : (
                <div className="flex-1 overflow-auto">
                  <ProjectDashboard scope={selectedScope} />
                </div>
              )}
            </div>
          ) : (
            <div className="flex h-full items-center justify-center text-muted-foreground">
              {hasResourceError ? (
                <div
                  role="alert"
                  className="max-w-md space-y-3 rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm"
                >
                  <p className="font-medium">一覧を取得できませんでした</p>
                  {spacesError && <p>スペース: {spacesError.message}</p>}
                  {projectsError && <p>プロジェクト: {projectsError.message}</p>}
                  <Button type="button" size="sm" onClick={retryDataFetch}>
                    再取得
                  </Button>
                </div>
              ) : !resourcesReady ? (
                <p className="text-sm">一覧を読み込み中です…</p>
              ) : canShowEmptyState ? (
                <p className="text-sm">スペースまたはプロジェクトを作成してください</p>
              ) : (
                <p className="text-sm">プロジェクトを選択してください</p>
              )}
            </div>
          )}
        </div>
    </div>
  );
}

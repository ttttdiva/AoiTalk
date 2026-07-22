"use client";

import Link from "next/link";
import {
  useState,
  useEffect,
  useCallback,
  useRef,
  type ChangeEvent,
  type DragEvent,
} from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  FolderOpen,
  Plus,
  Users,
  UserPlus,
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
  RefreshCw,
  Settings2,
  AlertTriangle,
  Tags,
  Upload,
  UploadCloud,
  FileText,
  Folder,
  Link2,
  Unlink,
  ChevronLeft,
  PanelLeftClose,
  PanelLeftOpen,
} from "lucide-react";
import { useProject } from "@/contexts/project-context";
import { ProjectDashboard } from "@/components/project-dashboard";
import { ProjectInformationPanel } from "@/components/projects/project-information-panel";
import { SpaceTagManagementPanel } from "@/components/projects/space-tag-management-panel";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatBytes } from "@/lib/utils";
import { useConfirm } from "@/hooks/use-confirm";
import { useProjectsData } from "@/hooks/use-projects-data";

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

type ManagementFileKind = "wbs" | "issue" | "risk" | "request" | "attachment";
type ManagementDocumentKind = Exclude<ManagementFileKind, "attachment">;

type ManagementFileUploadResponse = {
  success: boolean;
  kind: ManagementFileKind;
  name: string;
  path: string;
  size: number;
  registered: boolean;
  config: ManagementConfig;
};

type ProjectFilerDirectory = {
  name: string;
  path: string;
  modifiedAt?: string;
};

type ProjectFilerFile = {
  name: string;
  path: string;
  size: number;
  modifiedAt: string;
  extension: string;
};

type ProjectFilerListResponse = {
  currentPath: string;
  parentPath: string | null;
  directories: ProjectFilerDirectory[];
  files: ProjectFilerFile[];
};

type ManagementDocumentCardProps = {
  title: string;
  description: string;
  value?: string | null;
  values?: string[];
  accept?: string;
  multiple?: boolean;
  uploading?: boolean;
  onFiles: (files: File[]) => void;
  onPickFromFiler: () => void;
  onClear?: () => void;
  onRemove?: (path: string) => void;
};

const PROJECT_COLOR_PRESETS = [
  { name: "Crystal Cyan", value: "#0E7490" },
  { name: "Lagoon Teal", value: "#0F766E" },
  { name: "Emerald", value: "#047857" },
  { name: "Cobalt Blue", value: "#2563EB" },
  { name: "Lapis Indigo", value: "#4F46E5" },
  { name: "Aurora Violet", value: "#7C3AED" },
  { name: "Fuchsia", value: "#C026D3" },
  { name: "Rose", value: "#DB2777" },
  { name: "Crimson", value: "#BE123C" },
  { name: "Coral", value: "#C2410C" },
  { name: "Amber Brown", value: "#A16207" },
  { name: "Slate", value: "#475569" },
] as const;

type ProjectColorPickerProps = {
  value: string;
  onChange: (value: string) => void;
  inputClassName?: string;
};

function ProjectColorPicker({
  value,
  onChange,
  inputClassName = "h-8",
}: ProjectColorPickerProps) {
  const currentColor = value || "#3b82f6";
  const selectedColor = currentColor.toLowerCase();

  return (
    <div className="space-y-2 rounded border border-input px-2 py-1.5">
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">色</span>
        <input
          type="color"
          value={currentColor}
          onChange={(e) => onChange(e.target.value)}
          className={`${inputClassName} w-10 cursor-pointer rounded border-0 bg-transparent p-0`}
        />
        <span className="text-[11px] text-muted-foreground">
          {currentColor}
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {PROJECT_COLOR_PRESETS.map((preset) => {
          const isSelected = preset.value.toLowerCase() === selectedColor;

          return (
            <button
              key={preset.value}
              type="button"
              title={`${preset.name} ${preset.value}`}
              aria-label={`${preset.name} ${preset.value}`}
              aria-pressed={isSelected}
              onClick={() => onChange(preset.value)}
              className={`size-6 rounded-full border border-white/70 shadow-sm transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                isSelected
                  ? "ring-2 ring-ring ring-offset-2 ring-offset-background"
                  : ""
              }`}
              style={{ backgroundColor: preset.value }}
            />
          );
        })}
      </div>
    </div>
  );
}

function fileNameFromPath(filePath: string): string {
  const normalized = filePath.replace(/\\/g, "/");
  return normalized.split("/").filter(Boolean).at(-1) || normalized;
}

function folderFromPath(filePath: string): string {
  const normalized = filePath.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/") || "プロジェクトファイラー直下";
}

function acceptMatchesPath(filePath: string, accept?: string): boolean {
  if (!accept) return true;
  const extension =
    `.${fileNameFromPath(filePath).split(".").pop() || ""}`.toLowerCase();
  return accept
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
    .some((item) => item === extension || item === "*/*");
}

function ManagementDocumentCard({
  title,
  description,
  value,
  values,
  accept,
  multiple = false,
  uploading = false,
  onFiles,
  onPickFromFiler,
  onClear,
  onRemove,
}: ManagementDocumentCardProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const registeredFiles = multiple ? values || [] : value ? [value] : [];

  const submitFiles = useCallback(
    (files: FileList | File[]) => {
      const list = Array.from(files);
      if (list.length > 0) onFiles(list);
    },
    [onFiles],
  );

  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      event.stopPropagation();
      setDragging(false);
      submitFiles(event.dataTransfer.files);
    },
    [submitFiles],
  );

  const handleSelect = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      if (event.target.files) submitFiles(event.target.files);
      event.target.value = "";
    },
    [submitFiles],
  );

  return (
    <div
      className={`rounded-md border p-4 transition-colors ${
        dragging ? "border-primary bg-primary/5" : "border-border bg-card"
      }`}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <FileText className="size-4 text-muted-foreground" />
            <h3 className="text-sm font-medium">{title}</h3>
            {registeredFiles.length > 0 && (
              <Badge variant="secondary" className="text-[11px]">
                {multiple ? `${registeredFiles.length}件` : "登録済み"}
              </Badge>
            )}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{description}</p>
        </div>
      </div>

      {registeredFiles.length > 0 ? (
        <div className="mt-3 space-y-2">
          {registeredFiles.map((filePath) => (
            <div
              key={filePath}
              className="flex items-center justify-between gap-3 rounded-md border bg-muted/30 px-3 py-2"
            >
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">
                  {fileNameFromPath(filePath)}
                </div>
                <div className="truncate text-[11px] text-muted-foreground">
                  {folderFromPath(filePath)}
                </div>
              </div>
              {multiple && onRemove ? (
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  onClick={() => onRemove(filePath)}
                  aria-label={`${fileNameFromPath(filePath)} の登録を解除`}
                >
                  <X className="size-3.5" />
                </Button>
              ) : null}
            </div>
          ))}
        </div>
      ) : (
        <button
          type="button"
          className="mt-3 flex min-h-24 w-full flex-col items-center justify-center rounded-md border border-dashed bg-muted/20 px-3 py-4 text-center transition-colors hover:border-primary hover:bg-primary/5"
          onClick={() => inputRef.current?.click()}
          disabled={uploading}
        >
          {uploading ? (
            <Loader2 className="mb-2 size-5 animate-spin text-muted-foreground" />
          ) : (
            <UploadCloud className="mb-2 size-5 text-muted-foreground" />
          )}
          <span className="text-sm font-medium">
            ファイルをドロップ、またはアップロード
          </span>
          <span className="mt-1 text-xs text-muted-foreground">
            ローカルパスは保存しません
          </span>
        </button>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => inputRef.current?.click()}
          disabled={uploading}
        >
          {uploading ? (
            <Loader2 className="mr-1 size-3 animate-spin" />
          ) : (
            <Upload className="mr-1 size-3" />
          )}
          アップロード
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onPickFromFiler}
          disabled={uploading}
        >
          <Link2 className="mr-1 size-3" />
          ファイラーから選択
        </Button>
        {registeredFiles.length > 0 && !multiple && onClear ? (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={onClear}
            disabled={uploading}
          >
            <Unlink className="mr-1 size-3" />
            解除
          </Button>
        ) : null}
      </div>
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        accept={accept}
        multiple={multiple}
        onChange={handleSelect}
      />
    </div>
  );
}

interface SpaceInfo {
  id: string;
  name: string;
  slug: string;
  description?: string | null;
  color?: string | null;
  sort_order?: number;
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
  estimated_hours?: number | null;
  space_id?: string | null;
  is_completed?: boolean;
  created_at?: string | null;
}

// SWR データ未取得時に返す安定参照（再レンダー抑制用）。
const EMPTY_SPACES: SpaceInfo[] = [];
const EMPTY_PROJECTS: ProjectInfo[] = [];

interface ProjectMember {
  id: string;
  project_id: string;
  user_id: string;
  role: string | null;
  joined_at: string | null;
  username: string;
  display_name: string | null;
}

interface SimpleUser {
  id: string;
  username: string;
  display_name: string | null;
}

const ROLE_OPTIONS = [
  { value: "owner", label: "オーナー" },
  { value: "admin", label: "管理者" },
  { value: "member", label: "メンバー" },
  { value: "viewer", label: "閲覧者" },
];

const PROJECTS_PAGE_NAV_COLLAPSED_KEY = "projectsPageNavigationCollapsed";

type RightTab = "dashboard" | "information" | "members" | "management" | "tags";
type SelectedScope =
  | { type: "project"; id: string }
  | { type: "space"; id: string };

type ManagementConfig = {
  wbsFile: string | null;
  issueFile: string | null;
  riskFile: string | null;
  requestFiles: string[];
};

type WbsRowInfo = {
  title: string;
  wbsId: string | null;
  status: string;
  priority: string;
  plannedEnd: string | null;
  assignee: string | null;
  requestText: string | null;
  sheetName: string;
  rowNumber: number;
};

type RequestItem = {
  title: string;
  target: string;
  reason: string;
  sourceType: string;
  sourcePath: string;
  sourceRef: string;
  dueAt: string | null;
  status: string;
};

type WbsScanResponse = {
  config: ManagementConfig;
  file_path: string | null;
  upcoming: WbsRowInfo[];
  requests: RequestItem[];
  errors: string[];
  summary: {
    total: number;
    open: number;
    review: number;
    overdue: number;
    request_count: number;
  };
};

export default function ProjectsPage() {
  const confirm = useConfirm();
  const { refreshProjects, refreshSpaces } = useProject();

  // スペース一覧・プロジェクト一覧（取得は SWR に委譲）
  const {
    spaces: spacesData,
    projects: projectsData,
    refresh: refreshProjectsData,
  } = useProjectsData<SpaceInfo, ProjectInfo>();
  const spaces = spacesData ?? EMPTY_SPACES;
  const [expandedSpaces, setExpandedSpaces] = useState<Set<string>>(new Set());
  const [expandedClosedSpaces, setExpandedClosedSpaces] = useState<Set<string>>(
    new Set(),
  );
  const [isNavigationCollapsed, setIsNavigationCollapsed] = useState(false);

  // プロジェクト一覧
  const projects = projectsData ?? EMPTY_PROJECTS;
  const [loading, setLoading] = useState(true);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(
    null,
  );
  const [selectedScope, setSelectedScope] = useState<SelectedScope | null>(
    null,
  );

  // 右パネルタブ
  const [rightTab, setRightTab] = useState<RightTab>("dashboard");

  // 新規スペース作成
  const [showCreateSpace, setShowCreateSpace] = useState(false);
  const [newSpaceName, setNewSpaceName] = useState("");
  const [creatingSpace, setCreatingSpace] = useState(false);

  // スペース編集
  const [editingSpaceId, setEditingSpaceId] = useState<string | null>(null);
  const [editSpaceName, setEditSpaceName] = useState("");

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

  // 管理資料設定
  const [wbsFile, setWbsFile] = useState("");
  const [issueFile, setIssueFile] = useState("");
  const [riskFile, setRiskFile] = useState("");
  const [requestFiles, setRequestFiles] = useState<string[]>([]);
  const [wbsScan, setWbsScan] = useState<WbsScanResponse | null>(null);
  const [managementLoading, setManagementLoading] = useState(false);
  const [managementSaving, setManagementSaving] = useState(false);
  const [managementUploading, setManagementUploading] =
    useState<ManagementFileKind | null>(null);
  const [managementError, setManagementError] = useState("");
  const [syncResult, setSyncResult] = useState<string | null>(null);
  const [filePicker, setFilePicker] = useState<{
    kind: ManagementDocumentKind;
    title: string;
    accept: string;
  } | null>(null);
  const [filePickerPath, setFilePickerPath] = useState("");
  const [filePickerData, setFilePickerData] =
    useState<ProjectFilerListResponse | null>(null);
  const [filePickerLoading, setFilePickerLoading] = useState(false);
  const [filePickerError, setFilePickerError] = useState("");

  // メンバー管理
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [membersLoading, setMembersLoading] = useState(false);

  // ユーザーリスト（メンバー追加用）
  const [allUsers, setAllUsers] = useState<SimpleUser[]>([]);
  const [selectedUserIds, setSelectedUserIds] = useState<Set<string>>(
    new Set(),
  );
  const [addRole, setAddRole] = useState("member");
  const [addingMembers, setAddingMembers] = useState(false);
  const [addError, setAddError] = useState("");

  // データ取得（取得自体は SWR に委譲し、選択復元・展開などの副作用はここで実行）
  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const result = await refreshProjectsData();
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
      console.error("データ取得失敗:", err);
    } finally {
      setLoading(false);
    }
  }, [refreshProjectsData]);

  const fetchUsers = useCallback(async () => {
    try {
      const data = await apiFetch<SimpleUser[]>("/api/users/list");
      setAllUsers(data);
    } catch (err) {
      console.error("ユーザー取得失敗:", err);
    }
  }, []);

  useEffect(() => {
    fetchAll();
    fetchUsers();
  }, [fetchAll, fetchUsers]);

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

  // メンバー一覧取得
  const fetchMembers = useCallback(async (projectId: string) => {
    setMembersLoading(true);
    try {
      const data = await apiFetch<ProjectMember[]>(
        `/api/projects/${projectId}/members`,
      );
      setMembers(data);
    } catch (err) {
      console.error("メンバー取得失敗:", err);
      setMembers([]);
    } finally {
      setMembersLoading(false);
    }
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
      fetchMembers(selectedScope.id);
      localStorage.setItem("projectsPageSelected", selectedScope.id);
    } else {
      setMembers([]);
    }
  }, [selectedScope, fetchMembers]);

  const applyManagementConfig = useCallback((config: ManagementConfig) => {
    setWbsFile(config.wbsFile || "");
    setIssueFile(config.issueFile || "");
    setRiskFile(config.riskFile || "");
    setRequestFiles(config.requestFiles || []);
  }, []);

  const fetchManagement = useCallback(
    async (projectId: string) => {
      setManagementLoading(true);
      setManagementError("");
      try {
        const [configData, scanData] = await Promise.all([
          apiFetch<{ config: ManagementConfig }>(
            `/api/projects/${projectId}/management`,
          ),
          apiFetch<WbsScanResponse>(`/api/projects/${projectId}/wbs`),
        ]);
        applyManagementConfig(configData.config);
        setWbsScan(scanData);
      } catch (err) {
        setManagementError(
          err instanceof Error
            ? err.message
            : "管理資料設定の取得に失敗しました",
        );
      } finally {
        setManagementLoading(false);
      }
    },
    [applyManagementConfig],
  );

  useEffect(() => {
    if (rightTab === "management" && selectedProjectId) {
      fetchManagement(selectedProjectId);
    }
  }, [rightTab, selectedProjectId, fetchManagement]);

  const saveManagementConfigPatch = useCallback(
    async (
      patch: Partial<{
        wbs_file: string | null;
        issue_file: string | null;
        risk_file: string | null;
        request_files: string[];
      }>,
    ) => {
      if (!selectedProjectId) return null;
      setManagementSaving(true);
      setManagementError("");
      setSyncResult(null);
      try {
        const data = await apiFetch<{ config: ManagementConfig }>(
          `/api/projects/${selectedProjectId}/management`,
          {
            method: "PATCH",
            body: JSON.stringify(patch),
          },
        );
        applyManagementConfig(data.config);
        await fetchManagement(selectedProjectId);
        return data.config;
      } catch (err) {
        setManagementError(
          err instanceof Error ? err.message : "案件資料の登録に失敗しました",
        );
        return null;
      } finally {
        setManagementSaving(false);
      }
    },
    [selectedProjectId, applyManagementConfig, fetchManagement],
  );

  const handleRegisterExistingManagementFile = useCallback(
    async (kind: ManagementDocumentKind, filePath: string) => {
      const normalizedPath = filePath.trim();
      if (!normalizedPath) return;
      const nextRequestFiles =
        kind === "request"
          ? [...new Set([...requestFiles, normalizedPath])]
          : requestFiles;
      const config = await saveManagementConfigPatch({
        wbs_file: kind === "wbs" ? normalizedPath : wbsFile || null,
        issue_file: kind === "issue" ? normalizedPath : issueFile || null,
        risk_file: kind === "risk" ? normalizedPath : riskFile || null,
        request_files: nextRequestFiles,
      });
      if (config) {
        setSyncResult(
          kind === "wbs"
            ? "WBSをプロジェクトファイラーから登録しました。"
            : "資料をプロジェクトファイラーから登録しました。",
        );
      }
    },
    [issueFile, requestFiles, riskFile, saveManagementConfigPatch, wbsFile],
  );

  const handleClearManagementFile = useCallback(
    async (kind: ManagementDocumentKind, filePath?: string) => {
      const nextRequestFiles =
        kind === "request" && filePath
          ? requestFiles.filter((item) => item !== filePath)
          : requestFiles;
      await saveManagementConfigPatch({
        wbs_file: kind === "wbs" ? null : wbsFile || null,
        issue_file: kind === "issue" ? null : issueFile || null,
        risk_file: kind === "risk" ? null : riskFile || null,
        request_files: kind === "request" ? nextRequestFiles : requestFiles,
      });
    },
    [issueFile, requestFiles, riskFile, saveManagementConfigPatch, wbsFile],
  );

  const openFilePicker = useCallback(
    (kind: ManagementDocumentKind, title: string, accept: string) => {
      setFilePicker({ kind, title, accept });
      setFilePickerPath("");
      setFilePickerData(null);
      setFilePickerError("");
    },
    [],
  );

  const fetchProjectFilerFiles = useCallback(
    async (path: string) => {
      if (!selectedProjectId) return;
      setFilePickerLoading(true);
      setFilePickerError("");
      try {
        const data = await apiFetch<ProjectFilerListResponse>(
          `/api/projects/${selectedProjectId}/management/files?path=${encodeURIComponent(path)}`,
        );
        setFilePickerData(data);
      } catch (err) {
        setFilePickerError(
          err instanceof Error
            ? err.message
            : "ファイラーの読み込みに失敗しました",
        );
      } finally {
        setFilePickerLoading(false);
      }
    },
    [selectedProjectId],
  );

  useEffect(() => {
    if (filePicker && selectedProjectId) {
      fetchProjectFilerFiles(filePickerPath);
    }
  }, [filePicker, filePickerPath, fetchProjectFilerFiles, selectedProjectId]);

  const handleUploadManagementFiles = useCallback(
    async (kind: ManagementFileKind, files: File[]) => {
      if (!selectedProjectId || files.length === 0) return;
      setManagementUploading(kind);
      setManagementError("");
      setSyncResult(null);
      try {
        let latestConfig: ManagementConfig | null = null;
        for (const file of files) {
          const formData = new FormData();
          formData.append("file", file);
          formData.append("kind", kind);
          formData.append("directory", "management");
          const res = await fetch(
            `/api/projects/${selectedProjectId}/management/files`,
            {
              method: "POST",
              credentials: "include",
              body: formData,
            },
          );
          if (!res.ok) {
            const detail = await res
              .json()
              .catch(() => ({ detail: "アップロードに失敗しました" }));
            throw new Error(detail.detail || "アップロードに失敗しました");
          }
          const data = (await res.json()) as ManagementFileUploadResponse;
          latestConfig = data.config;
        }
        if (latestConfig) applyManagementConfig(latestConfig);
        setSyncResult(
          kind === "wbs"
            ? "WBSをプロジェクトファイラーへ登録しました。"
            : "資料をプロジェクトファイラーへ登録しました。",
        );
        await fetchManagement(selectedProjectId);
      } catch (err) {
        setManagementError(
          err instanceof Error ? err.message : "アップロードに失敗しました",
        );
      } finally {
        setManagementUploading(null);
      }
    },
    [selectedProjectId, applyManagementConfig, fetchManagement],
  );

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
        body: JSON.stringify({ name: editSpaceName.trim() }),
      });
      setEditingSpaceId(null);
      await fetchAll();
      refreshSpaces();
    } catch (err) {
      console.error("スペース更新失敗:", err);
    }
  }, [editingSpaceId, editSpaceName, fetchAll, refreshSpaces]);

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
          setMembers([]);
        }
        await fetchAll();
        refreshProjects();
      } catch (err) {
        console.error("プロジェクト削除失敗:", err);
      }
    },
    [selectedProjectId, fetchAll, refreshProjects, confirm],
  );

  // === メンバー操作 ===
  const handleAddMembers = useCallback(async () => {
    if (!selectedProjectId || selectedUserIds.size === 0) return;
    setAddingMembers(true);
    setAddError("");
    try {
      const promises = Array.from(selectedUserIds).map((userId) =>
        apiFetch(`/api/projects/${selectedProjectId}/members`, {
          method: "POST",
          body: JSON.stringify({ user_id: userId, role: addRole }),
        }),
      );
      await Promise.all(promises);
      setSelectedUserIds(new Set());
      setAddRole("member");
      await fetchMembers(selectedProjectId);
    } catch (err) {
      setAddError(err instanceof Error ? err.message : "追加に失敗しました");
    } finally {
      setAddingMembers(false);
    }
  }, [selectedProjectId, selectedUserIds, addRole, fetchMembers]);

  const handleRemoveMember = useCallback(
    async (memberId: string, displayName: string) => {
      if (!selectedProjectId) return;
      if (
        !(await confirm({
          description: `${displayName} をプロジェクトから除外しますか？`,
          destructive: true,
        }))
      )
        return;
      try {
        await apiFetch(`/api/projects/${selectedProjectId}/members`, {
          method: "DELETE",
          body: JSON.stringify({ member_id: memberId }),
        });
        await fetchMembers(selectedProjectId);
      } catch (err) {
        console.error("メンバー除外失敗:", err);
      }
    },
    [selectedProjectId, fetchMembers, confirm],
  );

  const handleChangeRole = useCallback(
    async (memberId: string, newRole: string) => {
      if (!selectedProjectId) return;
      try {
        await apiFetch(`/api/projects/${selectedProjectId}/members`, {
          method: "PATCH",
          body: JSON.stringify({ member_id: memberId, role: newRole }),
        });
        await fetchMembers(selectedProjectId);
      } catch (err) {
        console.error("ロール変更失敗:", err);
      }
    },
    [selectedProjectId, fetchMembers],
  );

  const toggleUser = (userId: string) => {
    setSelectedUserIds((prev) => {
      const next = new Set(prev);
      if (next.has(userId)) {
        next.delete(userId);
      } else {
        next.add(userId);
      }
      return next;
    });
  };

  const toggleSpace = (spaceId: string) => {
    setExpandedSpaces((prev) => {
      const next = new Set(prev);
      if (next.has(spaceId)) {
        next.delete(spaceId);
      } else {
        next.add(spaceId);
      }
      return next;
    });
  };

  const toggleClosedSpace = (spaceId: string) => {
    setExpandedClosedSpaces((prev) => {
      const next = new Set(prev);
      if (next.has(spaceId)) {
        next.delete(spaceId);
      } else {
        next.add(spaceId);
      }
      return next;
    });
  };

  const memberUserIds = new Set(members.map((m) => m.user_id));
  const availableUsers = allUsers.filter((u) => !memberUserIds.has(u.id));
  const selectProject = (projectId: string) => {
    setSelectedProjectId(projectId);
    setSelectedScope({ type: "project", id: projectId });
    setRightTab("dashboard");
  };

  const selectSpace = (spaceId: string) => {
    setSelectedScope({ type: "space", id: spaceId });
    setRightTab("dashboard");
  };

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

  // スペースごとにプロジェクトをグループ化（space_idの無いレガシーレコードは表示しない）
  const activeProjectsBySpace = new Map<string, ProjectInfo[]>();
  const closedProjectsBySpace = new Map<string, ProjectInfo[]>();
  for (const p of projects) {
    if (!p.space_id) continue;
    const target = p.is_completed
      ? closedProjectsBySpace
      : activeProjectsBySpace;
    const list = target.get(p.space_id) || [];
    list.push(p);
    target.set(p.space_id, list);
  }

  if (loading) {
    return (
      <div className="p-4 space-y-4">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-24 w-full rounded-xl" />
        ))}
      </div>
    );
  }

  // プロジェクトカードのレンダリング
  const renderProjectCard = (project: ProjectInfo) => (
    <div
      key={project.id}
      className={`rounded-lg border p-3 cursor-pointer transition-colors ${
        selectedScope?.type === "project" && selectedScope.id === project.id
          ? "border-primary bg-accent"
          : "hover:bg-accent/50"
      }`}
      onClick={() => selectProject(project.id)}
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
          <select
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
          </select>
          <label className="flex cursor-pointer items-start gap-2 rounded border border-amber-500/30 bg-amber-500/5 p-2">
            <Checkbox
              checked={editWorkspaceToolsEnabled}
              onCheckedChange={(checked) =>
                setEditWorkspaceToolsEnabled(checked === true)
              }
              aria-label="Workspaceツールを有効にする"
              className="mt-0.5"
            />
            <span className="min-w-0">
              <span className="block text-xs font-medium">
                Workspaceツールを有効にする
              </span>
              <span className="mt-0.5 block text-[11px] leading-relaxed text-muted-foreground">
                tools/ 配下のプログラムをエージェントが実行できるようにします。信頼できるツールだけを配置してください。
              </span>
            </span>
          </label>
          <div className="flex gap-1">
            <Button
              size="sm"
              variant="ghost"
              className="h-6 px-2"
              onClick={handleUpdate}
              disabled={saving}
            >
              <Check className="size-3" />
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-6 px-2"
              onClick={() => setEditingId(null)}
            >
              <X className="size-3" />
            </Button>
          </div>
        </div>
      ) : (
        <>
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-2 min-w-0">
              <span
                className="size-2.5 shrink-0 rounded-full border border-black/10"
                style={{ backgroundColor: project.color || "#94a3b8" }}
              />
              <FolderOpen
                className={`size-4 shrink-0 ${project.is_completed ? "text-green-500" : "text-muted-foreground"}`}
              />
              <span
                className={`text-sm font-medium truncate ${project.is_completed ? "line-through text-muted-foreground" : ""}`}
              >
                {project.name}
              </span>
              {project.is_completed && (
                <span className="shrink-0 text-[10px] text-green-600 dark:text-green-400 font-medium">
                  完了
                </span>
              )}
            </div>
            <div className="flex gap-0.5 shrink-0">
              <Button
                size="sm"
                variant="ghost"
                className={`h-6 w-6 p-0 ${project.is_completed ? "text-green-500" : "text-muted-foreground"}`}
                title={
                  project.is_completed
                    ? "完了済み（クリックで解除）"
                    : "完了としてマーク"
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
                size="sm"
                variant="ghost"
                className="h-6 w-6 p-0"
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
                size="sm"
                variant="ghost"
                className="h-6 w-6 p-0 text-destructive"
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
                  className="inline-block rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
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
        </>
      )}
    </div>
  );

  return (
    <div className="flex h-full flex-col overflow-auto p-4">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h1 className="text-lg font-bold">プロジェクト</h1>
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

      <div className="flex flex-1 flex-col gap-4 lg:min-h-0 lg:flex-row">
        {/* 左: スペース + プロジェクト一覧 */}
        {/* space-y-2でblock積み重ね＋overflow-autoでスクロール。flex-colを避けてflex-shrinkによるCard縮小クリップを防止 */}
        <div
          className={
            isNavigationCollapsed
              ? "hidden"
              : "w-full space-y-2 overflow-visible lg:w-72 lg:shrink-0 lg:overflow-auto"
          }
        >
          {/* スペース作成ボタン */}
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              className="flex-1"
              onClick={() => setShowCreateSpace(!showCreateSpace)}
            >
              <Layers className="size-3.5 mr-1" />
              新規スペース
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="flex-1"
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
            <Card size="sm">
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
            <Card size="sm">
              <CardContent className="space-y-2 pt-4">
                {createError ? (
                  <p className="text-sm text-destructive">{createError}</p>
                ) : null}
                <select
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
                </select>
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
          {spaces.map((space) => {
            const activeProjects = activeProjectsBySpace.get(space.id) || [];
            const closedProjects = closedProjectsBySpace.get(space.id) || [];
            const isExpanded = expandedSpaces.has(space.id);
            const isClosedExpanded = expandedClosedSpaces.has(space.id);

            return (
              <div
                key={space.id}
                className={`border rounded-lg ${
                  selectedScope?.type === "space" &&
                  selectedScope.id === space.id
                    ? "border-primary bg-accent/40"
                    : ""
                }`}
              >
                {/* スペースヘッダー */}
                <div className="flex items-center justify-between px-3 py-2 bg-muted/30">
                  <button
                    className="flex items-center gap-1.5 text-sm font-medium flex-1 text-left"
                    onClick={() => selectSpace(space.id)}
                  >
                    <Layers className="size-3.5 text-muted-foreground" />
                    {editingSpaceId === space.id ? (
                      <Input
                        value={editSpaceName}
                        onChange={(e) => setEditSpaceName(e.target.value)}
                        className="h-6 text-sm flex-1"
                        autoFocus
                        onClick={(e) => e.stopPropagation()}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleUpdateSpace();
                          if (e.key === "Escape") setEditingSpaceId(null);
                        }}
                      />
                    ) : (
                      <span>{space.name}</span>
                    )}
                    <Badge
                      variant="secondary"
                      className="text-[10px] px-1.5 py-0"
                    >
                      {activeProjects.length}
                    </Badge>
                  </button>
                  <div className="flex gap-0.5">
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-5 w-5 p-0"
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
                          size="sm"
                          variant="ghost"
                          className="h-5 w-5 p-0"
                          onClick={handleUpdateSpace}
                        >
                          <Check className="size-3" />
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-5 w-5 p-0"
                          onClick={() => setEditingSpaceId(null)}
                        >
                          <X className="size-3" />
                        </Button>
                      </>
                    ) : (
                      <>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-5 w-5 p-0"
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingSpaceId(space.id);
                            setEditSpaceName(space.name);
                          }}
                        >
                          <Pencil className="size-3" />
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-5 w-5 p-0 text-destructive"
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
                  <div className="p-2 space-y-2">
                    {activeProjects.length === 0 &&
                    closedProjects.length === 0 ? (
                      <p className="text-xs text-muted-foreground px-2 py-1">
                        プロジェクトなし
                      </p>
                    ) : (
                      activeProjects.map(renderProjectCard)
                    )}

                    {closedProjects.length > 0 && (
                      <div className="space-y-2">
                        <button
                          className="flex w-full items-center justify-between rounded-md border border-dashed px-2.5 py-2 text-left text-xs text-muted-foreground transition-colors hover:bg-accent/50"
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
                            className="text-[10px] px-1.5 py-0"
                          >
                            {closedProjects.length}
                          </Badge>
                        </button>

                        {isClosedExpanded && (
                          <div className="space-y-2 pl-3">
                            {closedProjects.map(renderProjectCard)}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}

          {spaces.length === 0 && projects.length === 0 && (
            <p className="text-sm text-muted-foreground px-2">
              スペースまたはプロジェクトを作成してください
            </p>
          )}
        </div>

        {/* 右: ダッシュボード / 詳細管理 */}
        <div className="min-w-0 flex-1 overflow-visible lg:overflow-auto">
          {selectedScope && (selectedProject || selectedSpace) ? (
            <div className="flex flex-col h-full">
              {/* タブヘッダー */}
              <div className="flex items-center gap-1 mb-4 border-b">
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
                  <Link
                    href={`/docs?project_id=${encodeURIComponent(selectedProject.id)}`}
                    className="ml-auto flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                  >
                    <FileText className="size-3.5" />
                    Docs
                  </Link>
                )}
              </div>

              {/* タブコンテンツ */}
              {rightTab === "dashboard" ? (
                <div className="flex-1 overflow-auto">
                  <ProjectDashboard scope={selectedScope} />
                </div>
              ) : rightTab === "information" && selectedProject && !selectedProjectIsInbox ? (
                <div className="flex-1 overflow-auto">
                  <ProjectInformationPanel project={selectedProject} />
                </div>
              ) : rightTab === "tags" && selectedSpace ? (
                <div className="flex-1 overflow-auto">
                  <SpaceTagManagementPanel
                    space={selectedSpace}
                    spaces={spaces}
                  />
                </div>
              ) : rightTab === "management" && selectedProject ? (
                <div className="flex-1 overflow-auto space-y-4">
                  <section className="space-y-4">
                    <div className="flex items-center gap-2 text-sm font-medium">
                      <Settings2 className="size-4" />
                      案件資料: {selectedProject.name}
                    </div>
                    <div className="space-y-4">
                      <div className="grid gap-3 md:grid-cols-2">
                        <div className="md:col-span-2 rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
                          案件資料はプロジェクトファイラーで管理します。新規アップロードするか、既にファイラーにあるファイルを選択してください。ローカルPCの絶対パスは保存しません。
                        </div>
                        <ManagementDocumentCard
                          title="WBS"
                          description="ExcelのWBSを登録します。アップロードするとプロジェクトファイラーの management/ に保存されます。"
                          value={wbsFile}
                          accept=".xlsx,.xlsm,.xls"
                          uploading={
                            managementUploading === "wbs" || managementSaving
                          }
                          onFiles={(files) =>
                            handleUploadManagementFiles("wbs", files)
                          }
                          onPickFromFiler={() =>
                            openFilePicker(
                              "wbs",
                              "WBSを選択",
                              ".xlsx,.xlsm,.xls",
                            )
                          }
                          onClear={() => handleClearManagementFile("wbs")}
                        />
                        <ManagementDocumentCard
                          title="課題管理表"
                          description="課題一覧として扱うExcel/CSVを登録します。"
                          value={issueFile}
                          accept=".xlsx,.xlsm,.xls,.csv,.tsv"
                          uploading={
                            managementUploading === "issue" || managementSaving
                          }
                          onFiles={(files) =>
                            handleUploadManagementFiles("issue", files)
                          }
                          onPickFromFiler={() =>
                            openFilePicker(
                              "issue",
                              "課題管理表を選択",
                              ".xlsx,.xlsm,.xls,.csv,.tsv",
                            )
                          }
                          onClear={() => handleClearManagementFile("issue")}
                        />
                        <ManagementDocumentCard
                          title="リスク管理表"
                          description="リスク一覧として扱うExcel/CSVを登録します。"
                          value={riskFile}
                          accept=".xlsx,.xlsm,.xls,.csv,.tsv"
                          uploading={
                            managementUploading === "risk" || managementSaving
                          }
                          onFiles={(files) =>
                            handleUploadManagementFiles("risk", files)
                          }
                          onPickFromFiler={() =>
                            openFilePicker(
                              "risk",
                              "リスク管理表を選択",
                              ".xlsx,.xlsm,.xls,.csv,.tsv",
                            )
                          }
                          onClear={() => handleClearManagementFile("risk")}
                        />
                        <ManagementDocumentCard
                          title="補助資料・議事録"
                          description="確認事項、議事録、補足資料を複数登録できます。"
                          values={requestFiles}
                          accept=".md,.txt,.csv,.tsv,.xlsx,.xlsm,.xls,.docx,.pdf"
                          multiple
                          uploading={
                            managementUploading === "request" ||
                            managementSaving
                          }
                          onFiles={(files) =>
                            handleUploadManagementFiles("request", files)
                          }
                          onPickFromFiler={() =>
                            openFilePicker(
                              "request",
                              "補助資料・議事録を選択",
                              ".md,.txt,.csv,.tsv,.xlsx,.xlsm,.xls,.docx,.pdf",
                            )
                          }
                          onRemove={(path) =>
                            handleClearManagementFile("request", path)
                          }
                        />
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            selectedProjectId &&
                            fetchManagement(selectedProjectId)
                          }
                          disabled={managementLoading}
                        >
                          <RefreshCw
                            className={`size-3 mr-1 ${managementLoading ? "animate-spin" : ""}`}
                          />
                          再読込
                        </Button>
                      </div>
                      {managementError && (
                        <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
                          <AlertTriangle className="size-3.5 shrink-0 mt-0.5" />
                          <span>{managementError}</span>
                        </div>
                      )}
                      {syncResult && (
                        <p className="text-xs text-muted-foreground">
                          {syncResult}
                        </p>
                      )}
                    </div>
                  </section>

                  <Dialog
                    open={!!filePicker}
                    onOpenChange={(open) => {
                      if (!open) setFilePicker(null);
                    }}
                  >
                    <DialogContent className="max-w-2xl">
                      <DialogHeader>
                        <DialogTitle>
                          {filePicker?.title || "ファイラーから選択"}
                        </DialogTitle>
                        <DialogDescription>
                          プロジェクトファイラー内のファイルを案件資料として登録します。
                        </DialogDescription>
                      </DialogHeader>
                      <div className="space-y-3">
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          <FolderOpen className="size-3.5" />
                          <span className="truncate">
                            {filePickerData?.currentPath ||
                              "プロジェクトファイラー直下"}
                          </span>
                        </div>
                        <div className="max-h-[420px] overflow-auto rounded-md border">
                          {filePickerLoading ? (
                            <div className="flex items-center justify-center gap-2 p-8 text-sm text-muted-foreground">
                              <Loader2 className="size-4 animate-spin" />
                              読み込み中
                            </div>
                          ) : filePickerError ? (
                            <div className="p-4 text-sm text-destructive">
                              {filePickerError}
                            </div>
                          ) : (
                            <div className="divide-y">
                              {filePickerData &&
                                filePickerData.parentPath !== null && (
                                  <button
                                    type="button"
                                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-muted"
                                    onClick={() =>
                                      setFilePickerPath(
                                        filePickerData?.parentPath || "",
                                      )
                                    }
                                  >
                                    <ChevronLeft className="size-4 text-muted-foreground" />
                                    上のフォルダ
                                  </button>
                                )}
                              {filePickerData?.directories.map((directory) => (
                                <button
                                  key={directory.path}
                                  type="button"
                                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-muted"
                                  onClick={() =>
                                    setFilePickerPath(directory.path)
                                  }
                                >
                                  <Folder className="size-4 text-muted-foreground" />
                                  <span className="min-w-0 truncate">
                                    {directory.name}
                                  </span>
                                </button>
                              ))}
                              {filePickerData?.files
                                .filter((file) =>
                                  acceptMatchesPath(
                                    file.path,
                                    filePicker?.accept,
                                  ),
                                )
                                .map((file) => (
                                  <button
                                    key={file.path}
                                    type="button"
                                    className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-muted"
                                    onClick={async () => {
                                      if (!filePicker) return;
                                      await handleRegisterExistingManagementFile(
                                        filePicker.kind,
                                        file.path,
                                      );
                                      setFilePicker(null);
                                    }}
                                  >
                                    <span className="flex min-w-0 items-center gap-2">
                                      <FileText className="size-4 shrink-0 text-muted-foreground" />
                                      <span className="min-w-0">
                                        <span className="block truncate text-sm">
                                          {file.name}
                                        </span>
                                        <span className="block truncate text-[11px] text-muted-foreground">
                                          {folderFromPath(file.path)}
                                        </span>
                                      </span>
                                    </span>
                                    <span className="shrink-0 text-[11px] text-muted-foreground">
                                      {formatBytes(file.size)}
                                    </span>
                                  </button>
                                ))}
                              {filePickerData &&
                                filePickerData.directories.length === 0 &&
                                filePickerData.files.filter((file) =>
                                  acceptMatchesPath(
                                    file.path,
                                    filePicker?.accept,
                                  ),
                                ).length === 0 && (
                                  <div className="p-4 text-sm text-muted-foreground">
                                    選択できるファイルがありません
                                  </div>
                                )}
                            </div>
                          )}
                        </div>
                      </div>
                    </DialogContent>
                  </Dialog>

                  <div className="grid gap-4 lg:grid-cols-2">
                    <Card>
                      <CardHeader>
                        <CardTitle className="text-sm">WBS状況</CardTitle>
                      </CardHeader>
                      <CardContent className="space-y-3">
                        {wbsScan ? (
                          <>
                            <div className="grid grid-cols-4 gap-2 text-center">
                              <div className="rounded-md border p-2">
                                <div className="text-lg font-semibold">
                                  {wbsScan.summary.total}
                                </div>
                                <div className="text-[11px] text-muted-foreground">
                                  総数
                                </div>
                              </div>
                              <div className="rounded-md border p-2">
                                <div className="text-lg font-semibold">
                                  {wbsScan.summary.open}
                                </div>
                                <div className="text-[11px] text-muted-foreground">
                                  未完了
                                </div>
                              </div>
                              <div className="rounded-md border p-2">
                                <div className="text-lg font-semibold">
                                  {wbsScan.summary.review}
                                </div>
                                <div className="text-[11px] text-muted-foreground">
                                  確認待ち
                                </div>
                              </div>
                              <div className="rounded-md border p-2">
                                <div className="text-lg font-semibold">
                                  {wbsScan.summary.overdue}
                                </div>
                                <div className="text-[11px] text-muted-foreground">
                                  超過
                                </div>
                              </div>
                            </div>
                            {wbsScan.errors.length > 0 && (
                              <div className="rounded-md border p-2 text-xs text-muted-foreground">
                                {wbsScan.errors.join(" / ")}
                              </div>
                            )}
                            <div className="space-y-2">
                              {wbsScan.upcoming.length > 0 ? (
                                wbsScan.upcoming.slice(0, 8).map((row) => (
                                  <div
                                    key={`${row.sheetName}-${row.rowNumber}`}
                                    className="rounded-md border p-2"
                                  >
                                    <div className="flex items-start justify-between gap-2">
                                      <div className="min-w-0">
                                        <p className="truncate text-sm font-medium">
                                          {row.title}
                                        </p>
                                        <p className="text-xs text-muted-foreground">
                                          {row.wbsId ||
                                            `${row.sheetName}:${row.rowNumber}`}
                                          {row.assignee
                                            ? ` / ${row.assignee}`
                                            : ""}
                                        </p>
                                      </div>
                                      <Badge
                                        variant={
                                          row.priority === "urgent"
                                            ? "destructive"
                                            : "secondary"
                                        }
                                      >
                                        {row.plannedEnd || "期限なし"}
                                      </Badge>
                                    </div>
                                  </div>
                                ))
                              ) : (
                                <p className="text-sm text-muted-foreground">
                                  直近のWBSタスクはありません
                                </p>
                              )}
                            </div>
                          </>
                        ) : (
                          <p className="text-sm text-muted-foreground">
                            管理資料設定を読み込んでください
                          </p>
                        )}
                      </CardContent>
                    </Card>

                    <Card>
                      <CardHeader>
                        <CardTitle className="text-sm">
                          要依頼・要確認事項
                        </CardTitle>
                      </CardHeader>
                      <CardContent className="space-y-2">
                        {wbsScan?.requests && wbsScan.requests.length > 0 ? (
                          wbsScan.requests.slice(0, 10).map((item) => (
                            <div
                              key={`${item.sourcePath}-${item.sourceRef}-${item.title}`}
                              className="rounded-md border p-2"
                            >
                              <div className="flex items-start justify-between gap-2">
                                <p className="text-sm font-medium">
                                  {item.title}
                                </p>
                                <Badge variant="outline">{item.target}</Badge>
                              </div>
                              <p className="mt-1 text-xs text-muted-foreground">
                                {item.reason}
                              </p>
                              <p className="mt-1 truncate text-[11px] text-muted-foreground">
                                {item.sourceType}: {item.sourceRef}
                                {item.dueAt ? ` / ${item.dueAt}` : ""}
                              </p>
                            </div>
                          ))
                        ) : (
                          <p className="text-sm text-muted-foreground">
                            要依頼事項は検出されていません
                          </p>
                        )}
                      </CardContent>
                    </Card>
                  </div>
                </div>
              ) : rightTab === "members" && selectedProject ? (
                <Card className="flex-1">
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2 text-sm">
                      <Users className="size-4" />
                      メンバー管理: {selectedProject.name}
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    {/* 現在のメンバー */}
                    {membersLoading ? (
                      <div className="space-y-2">
                        {Array.from({ length: 3 }).map((_, i) => (
                          <Skeleton
                            key={i}
                            className="h-10 w-full rounded-lg"
                          />
                        ))}
                      </div>
                    ) : members.length > 0 ? (
                      <div className="space-y-2">
                        {members.map((member) => (
                          <div
                            key={member.id}
                            className="flex items-center justify-between rounded-lg border p-2.5"
                          >
                            <div className="flex items-center gap-3">
                              <div className="flex size-8 items-center justify-center rounded-full bg-muted text-xs font-medium">
                                {(member.display_name || member.username)
                                  .charAt(0)
                                  .toUpperCase()}
                              </div>
                              <div>
                                <p className="text-sm font-medium">
                                  {member.display_name || member.username}
                                </p>
                                <p className="text-xs text-muted-foreground">
                                  @{member.username}
                                </p>
                              </div>
                            </div>
                            <div className="flex items-center gap-2">
                              <select
                                value={member.role || "member"}
                                onChange={(e) =>
                                  handleChangeRole(member.id, e.target.value)
                                }
                                className="h-7 rounded-md border border-input bg-transparent px-2 text-xs outline-none focus-visible:border-ring focus-visible:ring-1 focus-visible:ring-ring/50 dark:bg-input/30"
                              >
                                {ROLE_OPTIONS.map((opt) => (
                                  <option key={opt.value} value={opt.value}>
                                    {opt.label}
                                  </option>
                                ))}
                              </select>
                              {member.joined_at && (
                                <span className="text-xs text-muted-foreground">
                                  {new Date(
                                    member.joined_at,
                                  ).toLocaleDateString("ja-JP")}
                                </span>
                              )}
                              <Button
                                size="sm"
                                variant="ghost"
                                className="h-6 w-6 p-0 text-destructive hover:text-destructive"
                                onClick={() =>
                                  handleRemoveMember(
                                    member.id,
                                    member.display_name || member.username,
                                  )
                                }
                              >
                                <X className="size-3" />
                              </Button>
                            </div>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="text-sm text-muted-foreground">
                        メンバーがいません
                      </p>
                    )}

                    {/* メンバー追加 */}
                    <Separator />
                    <div className="space-y-3">
                      <Label className="flex items-center gap-1.5">
                        <UserPlus className="size-3.5" />
                        メンバー追加
                      </Label>

                      {availableUsers.length > 0 ? (
                        <>
                          <div className="max-h-48 overflow-auto rounded-lg border p-2 space-y-1">
                            {availableUsers.map((user) => (
                              <label
                                key={user.id}
                                className="flex items-center gap-3 rounded-md px-2 py-1.5 hover:bg-accent cursor-pointer"
                              >
                                <Checkbox
                                  checked={selectedUserIds.has(user.id)}
                                  onCheckedChange={() => toggleUser(user.id)}
                                />
                                <div className="flex items-center gap-2 min-w-0">
                                  <div className="flex size-6 items-center justify-center rounded-full bg-muted text-xs font-medium shrink-0">
                                    {(user.display_name || user.username)
                                      .charAt(0)
                                      .toUpperCase()}
                                  </div>
                                  <span className="text-sm truncate">
                                    {user.display_name || user.username}
                                  </span>
                                  <span className="text-xs text-muted-foreground truncate">
                                    @{user.username}
                                  </span>
                                </div>
                              </label>
                            ))}
                          </div>

                          <div className="flex items-center gap-2">
                            <div className="space-y-1">
                              <Label className="text-xs text-muted-foreground">
                                ロール
                              </Label>
                              <select
                                value={addRole}
                                onChange={(e) => setAddRole(e.target.value)}
                                className="h-8 w-32 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                              >
                                {ROLE_OPTIONS.map((opt) => (
                                  <option key={opt.value} value={opt.value}>
                                    {opt.label}
                                  </option>
                                ))}
                              </select>
                            </div>
                            <div className="pt-5">
                              <Button
                                size="sm"
                                onClick={handleAddMembers}
                                disabled={
                                  addingMembers || selectedUserIds.size === 0
                                }
                              >
                                {addingMembers ? (
                                  <Loader2 className="size-3 animate-spin mr-1" />
                                ) : (
                                  <Plus className="size-3 mr-1" />
                                )}
                                {selectedUserIds.size > 0
                                  ? `${selectedUserIds.size}人を追加`
                                  : "追加"}
                              </Button>
                            </div>
                          </div>
                        </>
                      ) : (
                        <p className="text-xs text-muted-foreground">
                          追加可能なユーザーがいません
                        </p>
                      )}

                      {addError && (
                        <p className="text-xs text-destructive">{addError}</p>
                      )}
                    </div>
                  </CardContent>
                </Card>
              ) : (
                <div className="flex-1 overflow-auto">
                  <ProjectDashboard scope={selectedScope} />
                </div>
              )}
            </div>
          ) : (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              <p className="text-sm">プロジェクトを選択してください</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

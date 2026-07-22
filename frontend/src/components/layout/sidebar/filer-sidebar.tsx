"use client";

/* eslint-disable @next/next/no-img-element */

import { useRouter } from "next/navigation";
import {
  useState,
  useEffect,
  useCallback,
  useRef,
  useMemo,
  type MouseEvent,
} from "react";
import {
  ChevronRight,
  X,
  Folder,
  FileIcon,
  Music,
  Film,
  ArrowUp,
  Home,
  Upload,
  Table2,
} from "lucide-react";
import {
  explorerCopy,
  explorerDelete,
  explorerList,
  ExplorerUploadError,
  explorerMove,
  explorerUpload,
  filerBrowse,
  type ExplorerDirectory,
  type ExplorerListResponse,
  type ExplorerFile,
} from "@/lib/explorer-api";
import { getDroppedExplorerFiles } from "@/lib/file-drop";
import {
  deleteProjectRecordTable,
  isRecordTableFile,
} from "@/lib/record-tables-api";
import { useProject } from "@/contexts/project-context";
import { useAudioPlayer } from "@/contexts/audio-player-context";
import { cn, getFileExt } from "@/lib/utils";
import { toast } from "sonner";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";

// ─── ファイラー用サイドバー ───

// ファイルタイプ判定
const AUDIO_EXTS = ["mp3", "wav", "ogg", "flac", "aac", "m4a", "opus", "wma"];
const IMAGE_EXTS = ["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"];
const VIDEO_EXTS = ["mp4", "webm", "mov", "avi", "mkv"];

// ファイル配信URL（絶対パス→ファイラーAPI、相対パス→エクスプローラーAPI）
function getFilerFileUrl(filePath: string) {
  if (isAbsolutePath(filePath)) {
    return `/api/python-proxy/filer/file?path=${encodeURIComponent(filePath)}`;
  }
  return `/api/python-proxy/explorer/serve?path=${encodeURIComponent(filePath)}`;
}

// 絶対パス判定
function isAbsolutePath(p: string): boolean {
  if (!p) return false;
  if (/^[A-Za-z]:[\\/]/.test(p)) return true;
  if (p.startsWith("/")) return true;
  return false;
}

type FilerTab = "workspace" | "user";

export function FilerSidebar() {
  const { selectedProjectId } = useProject();
  const audioPlayer = useAudioPlayer();
  const router = useRouter();

  const [filerTab, setFilerTab] = useState<FilerTab>("workspace");
  const [currentPath, setCurrentPath] = useState("");
  const [browseData, setBrowseData] = useState<ExplorerListResponse | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [isAbsoluteFilerPath, setIsAbsoluteFilerPath] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [viewerFile, setViewerFile] = useState<ExplorerFile | null>(null);
  const [selectedItems, setSelectedItems] = useState<Set<string>>(new Set());
  const [clipboard, setClipboard] = useState<{
    paths: string[];
    operation: "copy" | "cut";
  } | null>(null);

  const initDoneRef = useRef(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // コンテキストルートパス
  const contextRootPath = useMemo(() => {
    if (isAbsoluteFilerPath) return "";
    if (filerTab === "workspace" && selectedProjectId) {
      return `_projects/project_${selectedProjectId}`;
    }
    if (filerTab === "user" && userId) {
      return `_users/user_${userId}`;
    }
    return "";
  }, [filerTab, selectedProjectId, userId, isAbsoluteFilerPath]);
  const itemByPath = useMemo(() => {
    const entries = [
      ...(browseData?.directories ?? []),
      ...(browseData?.files ?? []),
    ] as Array<ExplorerDirectory | ExplorerFile>;
    return new Map(entries.map((entry) => [entry.path, entry]));
  }, [browseData]);
  const selectedPaths = useMemo(
    () => Array.from(selectedItems).filter((path) => itemByPath.has(path)),
    [itemByPath, selectedItems],
  );
  const selectedRegularPaths = useMemo(
    () =>
      selectedPaths.filter((path) => {
        const item = itemByPath.get(path);
        return !item || !("type" in item) || !isRecordTableFile(item);
      }),
    [itemByPath, selectedPaths],
  );
  const canUseFileShortcuts = !isAbsoluteFilerPath;

  // ディレクトリ読み込み
  const fetchDirectory = useCallback(
    async (path: string) => {
      setLoading(true);
      setError(null);
      const useAbsoluteFilerPath = isAbsolutePath(path);
      try {
        if (useAbsoluteFilerPath && isAdmin) {
          const data = await explorerList(path);
          setIsAbsoluteFilerPath(true);
          setBrowseData(data);
          setCurrentPath(data.current_path);
        } else if (useAbsoluteFilerPath) {
          await filerBrowse(path);
          throw new Error("absolute path access denied");
        } else {
          const data = await explorerList(path);
          setIsAbsoluteFilerPath(false);
          setBrowseData(data);
          setCurrentPath(data.current_path);
        }
        setSelectedItems(new Set());
      } catch {
        setError("読み込みに失敗しました");
      } finally {
        setLoading(false);
      }
    },
    [isAdmin],
  );

  const navigate = useCallback(
    (path: string) => {
      fetchDirectory(path);
    },
    [fetchDirectory],
  );

  const goUp = useCallback(() => {
    if (
      !isAdmin &&
      !isAbsoluteFilerPath &&
      contextRootPath &&
      currentPath === contextRootPath
    )
      return;
    if (browseData?.parent_path != null) navigate(browseData.parent_path);
  }, [
    browseData,
    navigate,
    currentPath,
    contextRootPath,
    isAbsoluteFilerPath,
    isAdmin,
  ]);

  const goHome = useCallback(() => {
    navigate(contextRootPath || "");
  }, [navigate, contextRootPath]);

  // タブ切り替え
  const handleSetFilerTab = useCallback(
    (tab: FilerTab) => {
      setFilerTab(tab);
      setIsAbsoluteFilerPath(false);
      if (tab === "workspace" && selectedProjectId) {
        fetchDirectory(`_projects/project_${selectedProjectId}`);
      } else if (tab === "user" && userId) {
        fetchDirectory(`_users/user_${userId}`);
      }
    },
    [fetchDirectory, selectedProjectId, userId],
  );

  const toggleSelect = useCallback((path: string) => {
    setSelectedItems((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const selectAll = useCallback(() => {
    if (!browseData) return;
    setSelectedItems(
      new Set([
        ...browseData.directories.map((dir) => dir.path),
        ...browseData.files.map((file) => file.path),
      ]),
    );
  }, [browseData]);

  const handleDirectoryClick = useCallback(
    (e: MouseEvent<HTMLButtonElement>, dir: ExplorerDirectory) => {
      if (e.ctrlKey || e.metaKey) {
        toggleSelect(dir.path);
        return;
      }
      navigate(dir.path);
    },
    [navigate, toggleSelect],
  );

  // ファイルクリック
  const handleFileClick = useCallback(
    (file: ExplorerFile) => {
      if (isRecordTableFile(file) && file.project_id && file.record_table_id) {
        const params = new URLSearchParams({
          recordProject: file.project_id,
          recordTable: file.record_table_id,
          recordName: file.name,
        });
        router.push(`/filer?${params.toString()}`);
        return;
      }
      const ext = getFileExt(file.name);
      if (AUDIO_EXTS.includes(ext)) {
        const audioFiles = (browseData?.files ?? [])
          .filter((f) => AUDIO_EXTS.includes(getFileExt(f.name)))
          .map((f) => ({
            name: f.name,
            path: f.path,
            type: f.type || "audio",
            rootPath: isAbsoluteFilerPath ? currentPath : contextRootPath || "",
            sourceKind: isAbsoluteFilerPath ? "filer" as const : "explorer" as const,
          }));
        audioPlayer.play(
          {
            name: file.name,
            path: file.path,
            type: file.type || "audio",
            rootPath: isAbsoluteFilerPath ? currentPath : contextRootPath || "",
            sourceKind: isAbsoluteFilerPath ? "filer" : "explorer",
          },
          audioFiles,
        );
      } else if (IMAGE_EXTS.includes(ext) || VIDEO_EXTS.includes(ext)) {
        setViewerFile(file);
      }
    },
    [audioPlayer, browseData, contextRootPath, currentPath, isAbsoluteFilerPath, router],
  );

  // D&D アップロード
  const handleFileButtonClick = useCallback(
    (e: MouseEvent<HTMLButtonElement>, file: ExplorerFile) => {
      if (e.ctrlKey || e.metaKey) {
        toggleSelect(file.path);
        return;
      }
      handleFileClick(file);
    },
    [handleFileClick, toggleSelect],
  );

  const copySelectedItems = useCallback(
    (operation: "copy" | "cut") => {
      if (selectedRegularPaths.length === 0) return;
      setClipboard({ paths: selectedRegularPaths, operation });
    },
    [selectedRegularPaths],
  );

  const pasteClipboardItems = useCallback(async () => {
    if (!clipboard || !canUseFileShortcuts) return;
    for (const src of clipboard.paths) {
      if (clipboard.operation === "cut") {
        await explorerMove(src, currentPath);
      } else {
        await explorerCopy(src, currentPath);
      }
    }
    if (clipboard.operation === "cut") setClipboard(null);
    setSelectedItems(new Set());
    await fetchDirectory(currentPath);
  }, [canUseFileShortcuts, clipboard, currentPath, fetchDirectory]);

  const deleteSelectedItems = useCallback(async () => {
    if (!canUseFileShortcuts || selectedPaths.length === 0) return;
    for (const path of selectedPaths) {
      const item = itemByPath.get(path);
      if (item && "type" in item && isRecordTableFile(item)) {
        if (item.project_id && item.record_table_id) {
          await deleteProjectRecordTable(item.project_id, item.record_table_id);
        }
      } else {
        await explorerDelete(path);
      }
    }
    setSelectedItems(new Set());
    await fetchDirectory(currentPath);
  }, [
    canUseFileShortcuts,
    currentPath,
    fetchDirectory,
    itemByPath,
    selectedPaths,
  ]);

  useEffect(() => {
    const isTextInput = (target: EventTarget | null) => {
      if (!(target instanceof HTMLElement)) return false;
      return (
        target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable
      );
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!containerRef.current?.contains(document.activeElement)) return;
      if (viewerFile || isTextInput(e.target)) return;
      const key = e.key.toLowerCase();
      const primaryModifier = e.ctrlKey || e.metaKey;

      if (primaryModifier && key === "a") {
        e.preventDefault();
        selectAll();
        return;
      }
      if (primaryModifier && key === "c") {
        e.preventDefault();
        if (canUseFileShortcuts) copySelectedItems("copy");
        return;
      }
      if (primaryModifier && key === "x") {
        e.preventDefault();
        if (canUseFileShortcuts) copySelectedItems("cut");
        return;
      }
      if (primaryModifier && key === "v") {
        e.preventDefault();
        void pasteClipboardItems();
        return;
      }
      // 削除は Delete のみ。Backspace は削除対象から外す。
      if (
        canUseFileShortcuts &&
        selectedPaths.length > 0 &&
        e.key === "Delete"
      ) {
        e.preventDefault();
        void deleteSelectedItems();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    canUseFileShortcuts,
    copySelectedItems,
    deleteSelectedItems,
    pasteClipboardItems,
    selectAll,
    selectedPaths.length,
    viewerFile,
  ]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    if (e.dataTransfer.types.includes("application/x-explorer-paths")) return;
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  }, []);
  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  }, []);
  const handleDrop = useCallback(
    async (e: React.DragEvent) => {
      if (e.dataTransfer.types.includes("application/x-explorer-paths")) return;
      e.preventDefault();
      e.stopPropagation();
      setIsDragging(false);
      const files = await getDroppedExplorerFiles(e.dataTransfer);
      if (!files || files.length === 0) return;
      setUploading(true);
      try {
        const result = await explorerUpload(currentPath, files);
        toast.success(`${result.successCount}件アップロードしました`);
        await fetchDirectory(currentPath);
      } catch (error) {
        if (error instanceof ExplorerUploadError) {
          const { successCount, failureCount } = error.batchResult;
          if (successCount > 0) await fetchDirectory(currentPath);
          toast.error(
            successCount > 0
              ? `${successCount}件アップロード、${failureCount}件失敗しました`
              : error.message,
          );
        } else {
          toast.error("アップロードに失敗しました");
        }
      } finally {
        setUploading(false);
      }
    },
    [currentPath, fetchDirectory],
  );

  // 初期化
  useEffect(() => {
    (async () => {
      try {
        const res = await fetch("/api/auth/status", { credentials: "include" });
        if (res.ok) {
          const data = await res.json();
          if (data.authenticated && data.user) {
            setUserId(data.user.id);
            setIsAdmin(data.user.role === "admin");
          }
        }
      } catch {
        /* ignore */
      }
    })();
  }, []);

  // ユーザーIDまたはプロジェクトIDが揃ったら初期表示
  useEffect(() => {
    if (initDoneRef.current) return;
    if (filerTab === "workspace" && selectedProjectId) {
      fetchDirectory(`_projects/project_${selectedProjectId}`);
      initDoneRef.current = true;
    } else if (filerTab === "user" && userId) {
      fetchDirectory(`_users/user_${userId}`);
      initDoneRef.current = true;
    }
  }, [selectedProjectId, userId, filerTab, fetchDirectory]);

  // プロジェクト切り替え時
  useEffect(() => {
    if (!initDoneRef.current || !selectedProjectId) return;
    if (filerTab === "workspace" && !isAbsoluteFilerPath) {
      fetchDirectory(`_projects/project_${selectedProjectId}`);
    }
  }, [selectedProjectId]); // eslint-disable-line react-hooks/exhaustive-deps

  // パンくず
  const breadcrumbs = useMemo(() => {
    if (!currentPath) return [];
    if (isAbsoluteFilerPath) return currentPath.split(/[/\\]/).filter(Boolean);
    if (contextRootPath && currentPath.startsWith(contextRootPath)) {
      const rel = currentPath
        .slice(contextRootPath.length)
        .replace(/^[/\\]/, "");
      return rel ? rel.split(/[/\\]/).filter(Boolean) : [];
    }
    return currentPath.split(/[/\\]/).filter(Boolean);
  }, [currentPath, contextRootPath, isAbsoluteFilerPath]);

  // ファイルアイコン
  const fileIcon = (file: ExplorerFile) => {
    if (isRecordTableFile(file)) {
      return <Table2 className="size-4 shrink-0 text-emerald-500" />;
    }
    const ext = getFileExt(file.name);
    if (VIDEO_EXTS.includes(ext))
      return <Film className="size-4 shrink-0 text-purple-500" />;
    if (AUDIO_EXTS.includes(ext))
      return <Music className="size-4 shrink-0 text-orange-500" />;
    if (IMAGE_EXTS.includes(ext))
      return <FileIcon className="size-4 shrink-0 text-blue-500" />;
    return <FileIcon className="size-4 shrink-0 text-muted-foreground" />;
  };

  return (
    <>
      <SidebarGroup ref={containerRef}>
        {/* タブ切り替え */}
        <div className="flex items-center gap-1 px-2 pb-1">
          <button
            onClick={() => handleSetFilerTab("workspace")}
            className={cn(
              "flex-1 rounded px-2 py-1 text-xs font-medium transition-colors",
              filerTab === "workspace" && !isAbsoluteFilerPath
                ? "bg-primary text-primary-foreground"
                : "hover:bg-accent",
            )}
          >
            ワークスペース
          </button>
          <button
            onClick={() => handleSetFilerTab("user")}
            className={cn(
              "flex-1 rounded px-2 py-1 text-xs font-medium transition-colors",
              filerTab === "user" && !isAbsoluteFilerPath
                ? "bg-primary text-primary-foreground"
                : "hover:bg-accent",
            )}
          >
            ユーザー
          </button>
        </div>

        {/* パンくず + ナビゲーション */}
        <div className="flex items-center gap-0.5 px-2 pb-1 text-xs text-muted-foreground flex-wrap">
          <button
            onClick={goHome}
            className="p-0.5 rounded hover:bg-accent"
            title="ホーム"
          >
            <Home className="size-3" />
          </button>
          {browseData?.can_go_up && browseData.parent_path !== null && (
            <button
              onClick={goUp}
              className="p-0.5 rounded hover:bg-accent"
              title="上のフォルダへ"
            >
              <ArrowUp className="size-3" />
            </button>
          )}
          {breadcrumbs.slice(-3).map((segment, i) => (
            <span key={i} className="flex items-center gap-0.5">
              <ChevronRight className="size-2.5" />
              <span className="truncate max-w-[80px]">{segment}</span>
            </span>
          ))}
        </div>

        <SidebarGroupContent>
          {/* D&D ゾーン */}
          <div
            className="relative"
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
            {/* ローディング */}
            {loading && (
              <div className="px-4 py-6 text-center text-xs text-muted-foreground">
                読み込み中...
              </div>
            )}

            {/* エラー */}
            {error && !loading && (
              <div className="px-4 py-3 text-center text-xs text-destructive">
                {error}
              </div>
            )}

            {/* フォルダ・ファイル一覧 */}
            {!loading && browseData && (
              <SidebarMenu>
                {/* フォルダ */}
                {browseData.directories.map((dir) => (
                  <SidebarMenuItem key={dir.path}>
                    <SidebarMenuButton
                      className={cn(
                        selectedItems.has(dir.path) &&
                          "bg-accent text-accent-foreground",
                      )}
                      onClick={(e) => handleDirectoryClick(e, dir)}
                    >
                      <Folder className="size-4 shrink-0 text-yellow-500" />
                      <span className="truncate text-sm">{dir.name}</span>
                      {dir.item_count !== undefined && (
                        <span className="ml-auto text-[10px] text-muted-foreground tabular-nums">
                          {dir.item_count}
                        </span>
                      )}
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
                {/* ファイル */}
                {browseData.files.map((file) => (
                  <SidebarMenuItem key={file.path}>
                    <SidebarMenuButton
                      className={cn(
                        selectedItems.has(file.path) &&
                          "bg-accent text-accent-foreground",
                      )}
                      onClick={(e) => handleFileButtonClick(e, file)}
                    >
                      {fileIcon(file)}
                      <span className="truncate text-sm">{file.name}</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
                {/* 空フォルダ */}
                {browseData.directories.length === 0 &&
                  browseData.files.length === 0 && (
                    <li className="px-4 py-6 text-center text-xs text-muted-foreground">
                      空のフォルダです。ファイルをドラッグ&ドロップでアップロードできます。
                    </li>
                  )}
              </SidebarMenu>
            )}

            {/* D&Dオーバーレイ */}
            {isDragging && (
              <div className="absolute inset-0 z-40 flex items-center justify-center rounded border-2 border-dashed border-blue-400 bg-blue-500/10">
                <div className="flex flex-col items-center gap-1 text-blue-500">
                  <Upload className="size-5" />
                  <span className="text-xs font-medium">
                    ドロップしてアップロード
                  </span>
                </div>
              </div>
            )}
            {uploading && (
              <div className="absolute inset-0 z-40 flex items-center justify-center bg-background/60">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <div className="size-3 animate-spin rounded-full border-2 border-primary border-t-transparent" />
                  アップロード中...
                </div>
              </div>
            )}
          </div>
        </SidebarGroupContent>
      </SidebarGroup>

      {/* 画像/動画ビューア */}
      {viewerFile && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/90"
          onClick={() => setViewerFile(null)}
        >
          <button
            className="absolute top-4 right-4 z-50 text-white p-2 rounded hover:bg-white/20"
            onClick={() => setViewerFile(null)}
          >
            <X className="size-6" />
          </button>
          <div className="absolute top-4 left-4 z-50 text-white text-sm bg-black/50 px-3 py-1.5 rounded">
            {viewerFile.name}
          </div>
          <div
            className="max-w-[98vw] max-h-[96vh] flex items-center justify-center"
            onClick={(e) => e.stopPropagation()}
          >
            {IMAGE_EXTS.includes(getFileExt(viewerFile.name)) && (
              <img
                src={getFilerFileUrl(viewerFile.path)}
                alt={viewerFile.name}
                className="max-w-[98vw] max-h-[96vh] object-contain"
              />
            )}
            {VIDEO_EXTS.includes(getFileExt(viewerFile.name)) && (
              <video
                src={getFilerFileUrl(viewerFile.path)}
                controls
                autoPlay
                className="max-w-[98vw] max-h-[96vh]"
              >
                <track kind="captions" />
              </video>
            )}
          </div>
        </div>
      )}
    </>
  );
}

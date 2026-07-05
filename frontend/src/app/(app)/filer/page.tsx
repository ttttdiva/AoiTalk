"use client";

/* eslint-disable @next/next/no-img-element */

import {
  useState,
  useEffect,
  useCallback,
  useRef,
  Suspense,
  useMemo,
} from "react";
import { useSearchParams } from "next/navigation";
import { useAudioPlayer } from "@/contexts/audio-player-context";
import { ExplorerProvider, useExplorer } from "@/contexts/explorer-context";
import { useProject } from "@/contexts/project-context";
import { useSnippets } from "@/contexts/snippets-context";
import { ExplorerToolbar } from "@/components/explorer/explorer-toolbar";
import { FileGrid } from "@/components/explorer/file-grid";
import { FileList } from "@/components/explorer/file-list";
import { FileContextMenu } from "@/components/explorer/file-context-menu";
import { NewFolderDialog } from "@/components/explorer/new-folder-dialog";
import { RenameDialog } from "@/components/explorer/rename-dialog";
import { UploadZone } from "@/components/explorer/upload-zone";
import { FilePreviewPanel } from "@/components/explorer/file-preview-panel";
import { GitPanel } from "@/components/explorer/git-panel";
import { HydrusSearchBar } from "@/components/hf-browser/hydrus-search-bar";
import { RecordTableEditor } from "@/components/records/record-table-editor";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  ChevronRight,
  Home,
  ArrowUp,
  Image as ImageIcon,
  X,
  Star,
  GitBranch,
  Search,
} from "lucide-react";
import type {
  ExplorerDirectory,
  ExplorerFile,
  SearchResult,
} from "@/lib/explorer-api";
import {
  explorerArchive,
  explorerCopy,
  explorerDelete,
  explorerErrorMessage,
  explorerExtract,
  explorerFullContent,
  explorerMove,
  explorerRemoveBookmark,
  explorerSave,
  explorerSearch,
} from "@/lib/explorer-api";
import {
  buildFallbackMigemoTerms,
  findIncrementalSearchMatch,
  type FilerSearchItem,
} from "@/lib/migemo-lite";
import {
  createProjectRecordTable,
  deleteProjectRecordTable,
  isRecordTableFile,
} from "@/lib/record-tables-api";
import { getFileServeUrl } from "@/lib/explorer-serve-url";
import { HF_PREFIX } from "@/lib/hf/virtual-path";
import dynamic from "next/dynamic";
import { toast } from "sonner";

const DocumentEditor = dynamic(
  () =>
    import("@/components/editor/document-editor").then((m) => ({
      default: m.DocumentEditor,
    })),
  {
    ssr: false,
    loading: () => (
      <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">
        エディタ読み込み中...
      </div>
    ),
  },
);

const InlineChatPanel = dynamic(
  () =>
    import("@/components/editor/inline-chat-panel").then((m) => ({
      default: m.InlineChatPanel,
    })),
  { ssr: false },
);

// ─── ファイル配信URL ───
const getFilerFileUrl = getFileServeUrl;
function isImage(type: string) {
  return type.startsWith("image");
}
function isVideo(type: string) {
  return type.startsWith("video");
}
function isAudio(type: string) {
  return type.startsWith("audio");
}
type ExplorerItem = ExplorerDirectory | ExplorerFile;
function isExplorerDirectory(item: ExplorerItem): item is ExplorerDirectory {
  return !("type" in item);
}

function searchResultToDirectory(item: SearchResult): ExplorerDirectory {
  return {
    name: item.name,
    path: item.path,
    item_count: item.item_count ?? undefined,
    modified_at: item.modified_at,
  };
}

function searchResultToFile(item: SearchResult): ExplorerFile {
  return {
    name: item.name,
    path: item.path,
    type: item.type || "binary",
    extension: item.extension || "",
    size: item.size_bytes,
    size_display: item.size_display,
    modified_at: item.modified_at,
  };
}

// 画像表示（onError フォールバック付き）
function ImageDisplay({ path, alt }: { path: string; alt: string }) {
  const [error, setError] = useState<string | null>(null);
  if (error) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-md bg-black/60 p-8 text-center text-white/80">
        <ImageIcon className="size-12 opacity-60" />
        <div className="text-sm">画像を表示できませんでした</div>
        <div className="max-w-[60vw] truncate text-xs opacity-70">{path}</div>
        <div className="text-[11px] opacity-50">{error}</div>
      </div>
    );
  }
  return (
    <img
      src={getFilerFileUrl(path)}
      alt={alt}
      className="max-w-[98vw] max-h-[96vh] object-contain select-none touch-pan-y"
      draggable={false}
      onError={() =>
        setError(
          "サーバーから画像の取得に失敗しました（404 / 認証 / パス解決失敗の可能性）",
        )
      }
    />
  );
}

// ─── ファイルビューア（画像/動画全画面モーダル） ───
function FileViewer({
  file,
  files,
  onClose,
  onNavigate,
}: {
  file: ExplorerFile;
  files: ExplorerFile[];
  onClose: () => void;
  onNavigate: (file: ExplorerFile) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const touchStartRef = useRef<{ x: number; y: number; t: number } | null>(
    null,
  );
  const isViewableFile = (f: ExplorerFile) =>
    isImage(f.type || "") || isVideo(f.type || "");
  const viewableFiles = files.filter(isViewableFile);
  const currentIndex = viewableFiles.findIndex((f) => f.path === file.path);
  const isImageFile = isImage(file.type || "");

  const goPrev = useCallback(() => {
    if (currentIndex > 0) onNavigate(viewableFiles[currentIndex - 1]);
  }, [currentIndex, viewableFiles, onNavigate]);

  const goNext = useCallback(() => {
    if (currentIndex < viewableFiles.length - 1)
      onNavigate(viewableFiles[currentIndex + 1]);
  }, [currentIndex, viewableFiles, onNavigate]);

  // 前後 PRELOAD_RADIUS 枚の画像をブラウザキャッシュに載せておく（動画は容量的に除外）
  useEffect(() => {
    if (currentIndex < 0) return;
    const PRELOAD_RADIUS = 2;
    const targets: ExplorerFile[] = [];
    for (let d = 1; d <= PRELOAD_RADIUS; d++) {
      const next = viewableFiles[currentIndex + d];
      const prev = viewableFiles[currentIndex - d];
      if (next && isImage(next.type || "")) targets.push(next);
      if (prev && isImage(prev.type || "")) targets.push(prev);
    }
    const loaders = targets.map((f) => {
      const img = new window.Image();
      img.decoding = "async";
      img.src = getFilerFileUrl(f.path);
      return img;
    });
    return () => {
      // 遷移時に未完了のリクエストを破棄してネットワークを空ける
      for (const img of loaders) img.src = "";
    };
  }, [currentIndex, viewableFiles]);

  // スワイプでページ送り（画像表示時のみ。動画はネイティブコントロール優先）
  const handleTouchStart = useCallback(
    (e: React.TouchEvent) => {
      if (!isImageFile) return;
      if (e.touches.length !== 1) {
        touchStartRef.current = null;
        return;
      }
      const t = e.touches[0];
      touchStartRef.current = { x: t.clientX, y: t.clientY, t: Date.now() };
    },
    [isImageFile],
  );

  const handleTouchEnd = useCallback(
    (e: React.TouchEvent) => {
      if (!isImageFile) return;
      const start = touchStartRef.current;
      touchStartRef.current = null;
      if (!start) return;
      const end = e.changedTouches[0];
      if (!end) return;
      const dx = end.clientX - start.x;
      const dy = end.clientY - start.y;
      const dt = Date.now() - start.t;
      // 水平方向に 50px 以上、縦より明確に水平、1秒以内
      if (dt > 1000) return;
      if (Math.abs(dx) < 50) return;
      if (Math.abs(dx) < Math.abs(dy) * 1.3) return;
      if (dx > 0) goPrev();
      else goNext();
    },
    [isImageFile, goPrev, goNext],
  );

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      // video要素自体がフォーカスされている場合はネイティブ処理に任せる（二重発火防止）
      if (e.target === videoRef.current) return;
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key === "ArrowLeft") {
        goPrev();
        return;
      }
      if (e.key === "ArrowRight") {
        goNext();
        return;
      }
      const video = videoRef.current;
      if (video) {
        if (e.key === " " || e.key === "k" || e.key === "K") {
          e.preventDefault();
          if (video.paused) {
            void video.play();
          } else {
            video.pause();
          }
        }
        if (e.key === "j" || e.key === "J") video.currentTime -= 10;
        if (e.key === "l" || e.key === "L") video.currentTime += 10;
        if (e.key === "f" || e.key === "F") {
          if (document.fullscreenElement) {
            void document.exitFullscreen();
          } else {
            void video.requestFullscreen();
          }
        }
        if (e.key === "m" || e.key === "M") video.muted = !video.muted;
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose, goPrev, goNext]);

  const handleOverlayClick = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/90"
      onClick={handleOverlayClick}
      onTouchStart={handleTouchStart}
      onTouchEnd={handleTouchEnd}
    >
      <Button
        variant="ghost"
        size="icon"
        className="absolute top-4 right-4 z-50 text-white hover:bg-white/20"
        onClick={onClose}
      >
        <X className="size-6" />
      </Button>
      <div className="absolute top-4 left-4 z-50 text-white text-sm bg-black/50 px-3 py-1.5 rounded">
        {file.name}
        {viewableFiles.length > 1 && (
          <span className="ml-2 text-white/60">
            {currentIndex + 1} / {viewableFiles.length}
          </span>
        )}
      </div>
      <div className="max-w-[98vw] max-h-[96vh] flex items-center justify-center">
        {isImage(file.type || "") && (
          <ImageDisplay path={file.path} alt={file.name} />
        )}
        {isVideo(file.type || "") && (
          <video
            ref={videoRef}
            src={getFilerFileUrl(file.path)}
            controls
            autoPlay
            className="max-w-[98vw] max-h-[96vh]"
          >
            <track kind="captions" />
          </video>
        )}
      </div>
      <div className="absolute bottom-4 left-1/2 -translate-x-1/2 text-white/40 text-xs flex gap-4">
        <span>← → ファイル切替</span>
        <span>ESC 閉じる</span>
        {isVideo(file.type || "") && (
          <>
            <span>Space/K 再生/停止</span>
            <span>J/L 10秒戻る/進む</span>
            <span>F 全画面</span>
          </>
        )}
      </div>
    </div>
  );
}

// 絶対パス判定（ブックマーク権限フィルタ用）
function isAbsolutePath(p: string): boolean {
  if (!p) return false;
  if (/^[A-Za-z]:[\\/]/.test(p)) return true;
  if (p.startsWith("/")) return true;
  return false;
}

// ─── ファイルエクスプローラメインコンテンツ ───
// テキストファイル拡張子
const TEXT_EXTS = new Set([
  ".txt",
  ".md",
  ".json",
  ".yaml",
  ".yml",
  ".xml",
  ".csv",
  ".log",
  ".py",
  ".js",
  ".ts",
  ".jsx",
  ".tsx",
  ".html",
  ".css",
  ".sql",
  ".ini",
  ".cfg",
]);

// ─── エディタスプリットビュー ───
function EditorPane({
  editingFile,
  closeEditor,
  handleFileClick,
  onContextMenu,
  onBackgroundContextMenu,
}: {
  editingFile: { path: string; name: string; extension: string };
  closeEditor: () => void;
  handleFileClick: (file: ExplorerFile) => void;
  onContextMenu: (
    e: React.MouseEvent,
    item: ExplorerDirectory | ExplorerFile,
  ) => void;
  onBackgroundContextMenu: (e: React.MouseEvent) => void;
}) {
  const { browseData, navigate, currentPath, goUp } = useExplorer();
  const { snippets } = useSnippets();
  const [editorState, setEditorState] = useState<{
    path: string | null;
    content: string | null;
    error: string | null;
  }>({ path: null, content: null, error: null });
  const [showChat, setShowChat] = useState(false);
  const [selectedText, setSelectedText] = useState("");
  const defaultEditorLoadError = "Failed to load file";
  /*

  // ファイル内容をロード
  useEffect(() => {
    let cancelled = false;
    explorerFullContent(editingFile.path).then((res) => {
      if (cancelled) return;
      setEditorState({
        path: editingFile.path,
        content: res.success ? res.content : null,
        error: res.success
          ? null
          : (res.error || "繝輔ぃ繧､繝ｫ縺ｮ隱ｭ縺ｿ霎ｼ縺ｿ縺ｫ螟ｱ謨励＠縺ｾ縺励◆"),
      });
      return;
      if (res.success) {
        if (cancelled) return;
        setEditorState({
          path: editingFile.path,
          content: res.content,
          error: null,
        });
      } else {
        setLoadError(res.error || "ファイルの読み込みに失敗しました");
      }
    });
  }, [editingFile.path]);
  */

  useEffect(() => {
    let cancelled = false;
    explorerFullContent(editingFile.path).then((res) => {
      if (cancelled) return;
      setEditorState({
        path: editingFile.path,
        content: res.success ? res.content : null,
        error: res.success ? null : res.error || defaultEditorLoadError,
      });
    });
    return () => {
      cancelled = true;
    };
  }, [defaultEditorLoadError, editingFile.path]);

  const isLoading = editorState.path !== editingFile.path;
  const editorContent = isLoading ? null : editorState.content;
  const loadError = isLoading ? null : editorState.error;

  return (
    <div className="flex h-full min-h-0">
      {/* 左: ファイルリスト（コンパクト） */}
      <div
        className="w-56 flex-shrink-0 border-r overflow-auto bg-muted/30"
        onContextMenu={onBackgroundContextMenu}
      >
        <div className="p-2 border-b">
          <div
            className="text-xs font-medium text-muted-foreground truncate"
            title={currentPath}
          >
            {currentPath.split("/").pop() || "ルート"}
          </div>
        </div>
        <div className="p-1">
          {browseData?.can_go_up && (
            <button
              className="flex items-center gap-1.5 w-full px-2 py-1 text-xs rounded hover:bg-accent/50 text-muted-foreground"
              onClick={goUp}
            >
              <ArrowUp className="size-3" />
              上へ
            </button>
          )}
          {browseData?.directories.map((dir) => (
            <button
              key={dir.path}
              className="flex items-center gap-1.5 w-full px-2 py-1 text-xs rounded hover:bg-accent/50 truncate"
              onClick={() => navigate(dir.path)}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onContextMenu(e, dir);
              }}
              title={dir.name}
            >
              📁 {dir.name}
            </button>
          ))}
          {browseData?.files.map((file) => (
            <button
              key={file.path}
              className={`flex items-center gap-1.5 w-full px-2 py-1 text-xs rounded hover:bg-accent/50 truncate ${
                file.path === editingFile.path
                  ? "bg-primary/10 font-medium"
                  : ""
              }`}
              onClick={() => handleFileClick(file)}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onContextMenu(e, file);
              }}
              title={file.name}
            >
              📄 {file.name}
            </button>
          ))}
        </div>
      </div>

      {/* 右: エディタ + チャットパネル */}
      <div className="flex min-h-0 flex-1 min-w-0">
        <div className="min-h-0 flex-1 min-w-0">
          {loadError ? (
            <div className="flex h-full items-center justify-center text-sm text-destructive">
              {loadError}
            </div>
          ) : editorContent === null ? (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              読み込み中...
            </div>
          ) : (
            <DocumentEditor
              filePath={editingFile.path}
              initialContent={editorContent}
              extension={editingFile.extension}
              snippets={snippets}
              onClose={closeEditor}
              onAskAI={(text) => {
                setSelectedText(text);
                setShowChat(true);
              }}
            />
          )}
        </div>

        {/* インラインチャットパネル */}
        {showChat && (
          <InlineChatPanel
            filePath={editingFile.path}
            selectedText={selectedText}
            onClose={() => setShowChat(false)}
          />
        )}
      </div>
    </div>
  );
}

// ─── 新規テキストファイルダイアログ ───
function NewTextFileDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (path: string, name: string) => void;
}) {
  const { currentPath, refresh } = useExplorer();
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);

  const handleCreate = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    const fileName = trimmed.includes(".") ? trimmed : trimmed + ".md";
    const filePath = currentPath ? `${currentPath}/${fileName}` : fileName;
    setLoading(true);
    try {
      const result = await explorerSave(filePath, "");
      if (result.success) {
        refresh();
        setName("");
        onOpenChange(false);
        onCreated(filePath, fileName);
      }
    } catch {
      // create error
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>新規テキストファイル</DialogTitle>
        </DialogHeader>
        <Input
          placeholder="ファイル名（例: notes.txt）"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleCreate();
          }}
          autoFocus
        />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button onClick={handleCreate} disabled={!name.trim() || loading}>
            {loading ? "作成中..." : "作成"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function NewRecordTableDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (file: ExplorerFile) => void;
}) {
  const { selectedProjectId } = useProject();
  const { refresh } = useExplorer();
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);

  const handleCreate = async () => {
    const tableName = name.trim();
    if (!selectedProjectId || !tableName) return;
    setLoading(true);
    try {
      const result = await createProjectRecordTable(
        selectedProjectId,
        tableName,
      );
      refresh();
      setName("");
      onOpenChange(false);
      onCreated({
        name: `${result.table.name}.dbtable`,
        path: `aoitalk-record-table:${selectedProjectId}:${result.table.id}`,
        type: "application/x-aoitalk-record-table",
        extension: ".dbtable",
        virtual_kind: "record_table",
        project_id: selectedProjectId,
        record_table_id: result.table.id,
        row_count: result.table.row_count ?? 0,
        description: result.table.description,
      });
    } catch {
      // create error
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>新規DBテーブル</DialogTitle>
        </DialogHeader>
        <Input
          placeholder="例: 申請台帳"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void handleCreate();
          }}
          autoFocus
        />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button
            onClick={handleCreate}
            disabled={!selectedProjectId || !name.trim() || loading}
          >
            {loading ? "作成中..." : "作成"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ExplorerContent() {
  const explorerRootRef = useRef<HTMLDivElement>(null);
  const explorerScrollRef = useRef<HTMLDivElement>(null);
  const incrementalSearchRef = useRef<{
    query: string;
    lastInputAt: number;
    timeoutId: number | null;
    requestId: number;
  }>({
    query: "",
    lastInputAt: 0,
    timeoutId: null,
    requestId: 0,
  });
  const activePathRef = useRef<string | null>(null);
  const searchParams = useSearchParams();
  const {
    currentPath,
    navigate,
    goBack,
    goForward,
    goUp,
    refresh,
    browseData,
    loading,
    error,
    viewMode,
    bookmarks,
    refreshBookmarks,
    filerTab,
    setFilerTab,
    contextRootPath,
    isAdmin,
    isAbsoluteFilerPath,
    isHfMode,
    isHydrusMode,
    setBrowseData,
    selectedItems,
    focusedItemPath,
    selectItem,
    selectAll,
    clearSelection,
    clipboard,
    setClipboard,
    editingFile,
    openEditor,
    closeEditor,
    hfCreatorMapping,
    hfSearchQuery,
    setHfSearchQuery,
  } = useExplorer();
  void isHydrusMode;

  // Hydrus 検索エラー（検索結果そのものは context の browseData に流し込む）
  const [hydrusError, setHydrusError] = useState<string | null>(null);
  const [fileSearchQuery, setFileSearchQuery] = useState("");
  const [fileSearchActive, setFileSearchActive] = useState(false);
  const [fileSearchLoading, setFileSearchLoading] = useState(false);
  const [fileSearchError, setFileSearchError] = useState<string | null>(null);
  const [fileSearchCount, setFileSearchCount] = useState(0);
  const audioPlayer = useAudioPlayer();

  // ダイアログ状態
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newTextFileOpen, setNewTextFileOpen] = useState(false);
  const [newRecordTableOpen, setNewRecordTableOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<
    ExplorerDirectory | ExplorerFile | null
  >(null);
  const [viewerFile, setViewerFile] = useState<ExplorerFile | null>(null);
  const [previewFile, setPreviewFile] = useState<ExplorerFile | null>(null);
  const [recordTableFile, setRecordTableFile] = useState<ExplorerFile | null>(
    null,
  );
  const [gitOpen, setGitOpen] = useState(false);

  // コンテキストメニュー
  const [ctxItem, setCtxItem] = useState<
    ExplorerDirectory | ExplorerFile | null
  >(null);
  const [ctxPos, setCtxPos] = useState<{ x: number; y: number } | null>(null);

  // 音楽ファイルプレイリスト
  const audioFiles = useMemo(
    () =>
      (browseData?.files ?? [])
        .filter((f) => isAudio(f.type || ""))
        .map((f) => ({
          name: f.name,
          path: f.path,
          type: f.type || "audio",
          rootPath: isAbsoluteFilerPath ? currentPath : contextRootPath || "",
          sourceKind: isAbsoluteFilerPath
            ? ("filer" as const)
            : ("explorer" as const),
        })),
    [browseData, contextRootPath, currentPath, isAbsoluteFilerPath],
  );
  const visibleItems = useMemo<ExplorerItem[]>(
    () =>
      browseData
        ? ([...browseData.directories, ...browseData.files] as ExplorerItem[])
        : [],
    [browseData],
  );
  const itemByPath = useMemo(() => {
    return new Map(visibleItems.map((entry) => [entry.path, entry]));
  }, [visibleItems]);
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
  const selectedZipPaths = useMemo(
    () =>
      selectedRegularPaths.filter((path) => {
        const item = itemByPath.get(path);
        if (!item || !("type" in item)) return false;
        const extension = (item.extension || "").toLowerCase();
        return extension === ".zip" || item.name.toLowerCase().endsWith(".zip");
      }),
    [itemByPath, selectedRegularPaths],
  );
  const activePath =
    focusedItemPath && itemByPath.has(focusedItemPath)
      ? focusedItemPath
      : (selectedPaths[0] ?? null);
  const activeItem = activePath ? (itemByPath.get(activePath) ?? null) : null;
  const canUseFileShortcuts =
    !isAbsoluteFilerPath && !isHfMode && filerTab !== "hydrus";
  const canUseExplorerSearch = !isHfMode && filerTab !== "hydrus";
  const isExplorerInteractionBlocked =
    !!editingFile ||
    !!recordTableFile ||
    !!viewerFile ||
    !!previewFile ||
    !!renameTarget ||
    newFolderOpen ||
    newTextFileOpen ||
    newRecordTableOpen ||
    gitOpen ||
    !!ctxPos;

  // URL ?open=path パラメータでファイルを自動オープン
  useEffect(() => {
    const openPath = searchParams.get("open");
    if (openPath) {
      const name = openPath.split("/").pop() || openPath;
      openEditor({ path: openPath, name });
    }
    const recordProject = searchParams.get("recordProject");
    const recordTable = searchParams.get("recordTable");
    if (recordProject && recordTable) {
      const name = searchParams.get("recordName") || "DBテーブル.dbtable";
      const file: ExplorerFile = {
        name,
        path: `aoitalk-record-table:${recordProject}:${recordTable}`,
        type: "application/x-aoitalk-record-table",
        extension: ".dbtable",
        virtual_kind: "record_table",
        project_id: recordProject,
        record_table_id: recordTable,
      };
      window.setTimeout(() => setRecordTableFile(file), 0);
    }
  }, [searchParams, openEditor]);

  const resetFileSearchState = useCallback((clearQuery = false) => {
    if (clearQuery) setFileSearchQuery("");
    setFileSearchActive(false);
    setFileSearchError(null);
    setFileSearchCount(0);
  }, []);

  const clearFileSearch = useCallback(() => {
    resetFileSearchState(true);
    clearSelection();
    void refresh();
  }, [clearSelection, refresh, resetFileSearchState]);

  const runFileSearch = useCallback(async () => {
    const query = fileSearchQuery.trim();
    if (!query) {
      clearFileSearch();
      return;
    }

    setFileSearchLoading(true);
    setFileSearchError(null);
    try {
      const data = await explorerSearch(query, currentPath, 50);
      const directories: ExplorerDirectory[] = [];
      const files: ExplorerFile[] = [];

      for (const item of data.results) {
        if (item.kind === "directory" || !item.type) {
          directories.push(searchResultToDirectory(item));
        } else {
          files.push(searchResultToFile(item));
        }
      }

      setBrowseData({
        success: true,
        current_path: currentPath,
        parent_path: browseData?.parent_path ?? null,
        can_go_up: browseData?.can_go_up ?? false,
        directories,
        files,
        total_items: directories.length + files.length,
        is_admin_mode: browseData?.is_admin_mode,
      });
      setFileSearchActive(true);
      setFileSearchCount(directories.length + files.length);
      clearSelection();
    } catch (error) {
      setFileSearchError(explorerErrorMessage(error));
    } finally {
      setFileSearchLoading(false);
    }
  }, [
    browseData?.can_go_up,
    browseData?.is_admin_mode,
    browseData?.parent_path,
    clearFileSearch,
    clearSelection,
    currentPath,
    fileSearchQuery,
    setBrowseData,
  ]);

  useEffect(() => {
    resetFileSearchState();
  }, [currentPath, resetFileSearchState]);

  // ファイルを開く
  const handleFileClick = useCallback(
    (file: ExplorerFile) => {
      if (isRecordTableFile(file)) {
        setRecordTableFile(file);
        closeEditor();
        setPreviewFile(null);
      } else if (isAudio(file.type || "")) {
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
      } else if (isImage(file.type || "") || isVideo(file.type || "")) {
        setViewerFile(file);
      } else if (TEXT_EXTS.has(file.extension || "")) {
        // テキストファイルはエディタで開く
        openEditor(file);
      } else {
        setPreviewFile(file);
      }
    },
    [
      audioPlayer,
      audioFiles,
      closeEditor,
      contextRootPath,
      currentPath,
      isAbsoluteFilerPath,
      openEditor,
    ],
  );

  // F7 / Shift+F7 ショートカット（ファイラー・エディタ画面共通、書き込み可能時のみ）
  useEffect(() => {
    const canWrite = !isAbsoluteFilerPath;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!canWrite) return;
      // 入力フィールドがフォーカスされている場合は無効
      const target = e.target as HTMLElement;
      if (
        target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable
      )
        return;
      if (e.code === "F7" && e.shiftKey) {
        e.preventDefault();
        setNewTextFileOpen(true);
      } else if (e.code === "F7" && !e.shiftKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        setNewFolderOpen(true);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isAbsoluteFilerPath]);

  const copySelectedItems = useCallback(
    (operation: "copy" | "cut") => {
      if (selectedRegularPaths.length === 0) return;
      setClipboard({ paths: selectedRegularPaths, operation });
    },
    [selectedRegularPaths, setClipboard],
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
    clearSelection();
    await refresh();
  }, [
    canUseFileShortcuts,
    clearSelection,
    clipboard,
    currentPath,
    refresh,
    setClipboard,
  ]);

  const archiveSelectedItems = useCallback(async () => {
    let targetPaths = selectedRegularPaths;
    if (
      targetPaths.length === 0 &&
      activeItem &&
      (!("type" in activeItem) || !isRecordTableFile(activeItem))
    ) {
      targetPaths = [activeItem.path];
    }
    if (!canUseFileShortcuts || targetPaths.length === 0) return;
    try {
      const result = await explorerArchive(targetPaths, currentPath);
      clearSelection();
      await refresh();
      toast.success(`${result.archive_name}を作成しました`);
    } catch {
      toast.error("zip圧縮に失敗しました");
    }
  }, [
    canUseFileShortcuts,
    clearSelection,
    currentPath,
    refresh,
    activeItem,
    selectedRegularPaths,
  ]);

  const extractSelectedItems = useCallback(async () => {
    const activeItemExtension =
      activeItem && "type" in activeItem
        ? (activeItem.extension || "").toLowerCase()
        : "";
    let targetPaths = selectedZipPaths;
    if (
      targetPaths.length === 0 &&
      activeItem &&
      "type" in activeItem &&
      (activeItemExtension === ".zip" ||
        activeItem.name.toLowerCase().endsWith(".zip"))
    ) {
      targetPaths = [activeItem.path];
    }
    if (!canUseFileShortcuts || (!activeItem && selectedPaths.length === 0)) {
      return;
    }
    if (targetPaths.length === 0) {
      toast.error("展開できるZIPファイルを選択してください");
      return;
    }
    try {
      const result = await explorerExtract(targetPaths, currentPath);
      clearSelection();
      await refresh();
      toast.success(`${result.extracted.length}件のZIPを展開しました`);
    } catch {
      toast.error("zip展開に失敗しました");
    }
  }, [
    canUseFileShortcuts,
    clearSelection,
    currentPath,
    refresh,
    activeItem,
    selectedPaths.length,
    selectedZipPaths,
  ]);

  const deleteSelectedItems = useCallback(async () => {
    if (!canUseFileShortcuts || selectedPaths.length === 0) return;
    try {
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
      clearSelection();
      await refresh();
      toast.success("削除しました");
    } catch (error) {
      toast.error(`削除に失敗しました: ${explorerErrorMessage(error)}`);
    }
  }, [canUseFileShortcuts, clearSelection, itemByPath, refresh, selectedPaths]);

  const getRenderedItemPaths = useCallback(() => {
    const renderedPaths = Array.from(
      explorerRootRef.current?.querySelectorAll<HTMLElement>(
        "[data-explorer-item-path]",
      ) ?? [],
    )
      .map((item) => item.dataset.explorerItemPath)
      .filter((path): path is string => !!path && itemByPath.has(path));
    return renderedPaths.length > 0
      ? renderedPaths
      : visibleItems.map((item) => item.path);
  }, [itemByPath, visibleItems]);

  const focusRenderedItemPath = useCallback(
    (path: string) => {
      activePathRef.current = path;
      selectItem(path);
      window.requestAnimationFrame(() => {
        const element = Array.from(
          explorerRootRef.current?.querySelectorAll<HTMLElement>(
            "[data-explorer-item-path]",
          ) ?? [],
        ).find((item) => item.dataset.explorerItemPath === path);
        element?.scrollIntoView({ block: "nearest", inline: "nearest" });
      });
    },
    [selectItem],
  );

  const focusItemByIndex = useCallback(
    (index: number) => {
      const itemPaths = getRenderedItemPaths();
      if (itemPaths.length === 0) return;
      const nextIndex = Math.max(0, Math.min(itemPaths.length - 1, index));
      const nextPath = itemPaths[nextIndex];
      if (nextPath) focusRenderedItemPath(nextPath);
    },
    [focusRenderedItemPath, getRenderedItemPaths],
  );

  const focusItemByOffset = useCallback(
    (offset: number) => {
      const itemPaths = getRenderedItemPaths();
      if (itemPaths.length === 0) return;
      const currentIndex = activePath
        ? itemPaths.findIndex((path) => path === activePath)
        : -1;
      const nextIndex =
        currentIndex < 0
          ? offset < 0
            ? itemPaths.length - 1
            : 0
          : Math.max(0, Math.min(itemPaths.length - 1, currentIndex + offset));
      const nextPath = itemPaths[nextIndex];
      if (nextPath) focusRenderedItemPath(nextPath);
    },
    [activePath, focusRenderedItemPath, getRenderedItemPaths],
  );

  const getVisibleItemPageSize = useCallback(() => {
    const scrollRoot = explorerScrollRef.current ?? explorerRootRef.current;
    if (!scrollRoot) return 10;
    const scrollRect = scrollRoot.getBoundingClientRect();
    const visibleCount = Array.from(
      explorerRootRef.current?.querySelectorAll<HTMLElement>(
        "[data-explorer-item-path]",
      ) ?? [],
    ).filter((item) => {
      const rect = item.getBoundingClientRect();
      return rect.bottom > scrollRect.top && rect.top < scrollRect.bottom;
    }).length;
    return Math.max(1, visibleCount || 10);
  }, []);

  const openExplorerItem = useCallback(
    (item: ExplorerItem) => {
      if (isExplorerDirectory(item)) {
        resetFileSearchState();
        navigate(item.path);
      } else {
        handleFileClick(item);
      }
    },
    [handleFileClick, navigate, resetFileSearchState],
  );

  const getRenderedSearchItems = useCallback((): FilerSearchItem[] => {
    return getRenderedItemPaths()
      .map((path) => itemByPath.get(path))
      .filter((item): item is ExplorerItem => !!item)
      .map((item) => ({ path: item.path, name: item.name }));
  }, [getRenderedItemPaths, itemByPath]);

  const focusSearchMatch = useCallback(
    (
      terms: string[],
      baseActivePath: string | null = activePathRef.current,
    ) => {
      const match = findIncrementalSearchMatch(
        getRenderedSearchItems(),
        baseActivePath,
        terms,
      );
      if (!match) return null;
      focusRenderedItemPath(match.path);
      return match.path;
    },
    [focusRenderedItemPath, getRenderedSearchItems],
  );

  const handleIncrementalSearchCharacter = useCallback(
    (input: string) => {
      const state = incrementalSearchRef.current;
      const now = Date.now();
      if (state.timeoutId !== null) {
        window.clearTimeout(state.timeoutId);
      }

      state.query =
        now - state.lastInputAt <= 1000 ? state.query + input : input;
      state.lastInputAt = now;
      state.timeoutId = window.setTimeout(() => {
        state.query = "";
        state.timeoutId = null;
      }, 1000);

      const query = state.query;
      const requestId = state.requestId + 1;
      state.requestId = requestId;
      const baseActivePath = activePathRef.current;
      const fallbackTerms = buildFallbackMigemoTerms(query);
      const fallbackFocusedPath =
        focusSearchMatch(fallbackTerms, baseActivePath) ?? baseActivePath;

      void (async () => {
        try {
          const params = new URLSearchParams({ q: query, limit: "240" });
          const response = await fetch(`/api/migemo?${params.toString()}`, {
            credentials: "include",
            cache: "no-store",
          });
          if (!response.ok) return;
          const data = (await response.json()) as {
            terms?: string[];
          };
          if (
            incrementalSearchRef.current.requestId !== requestId ||
            incrementalSearchRef.current.query !== query
          ) {
            return;
          }
          const terms =
            data.terms && data.terms.length > 0 ? data.terms : fallbackTerms;
          focusSearchMatch(terms, fallbackFocusedPath);
        } catch {
          // Fallback terms already ran synchronously.
        }
      })();
    },
    [focusSearchMatch],
  );

  useEffect(() => {
    activePathRef.current = activePath;
  }, [activePath]);

  useEffect(() => {
    if (loading || isExplorerInteractionBlocked || activePath) return;
    if (!browseData || visibleItems.length === 0) return;
    const frame = window.requestAnimationFrame(() => {
      const firstPath = getRenderedItemPaths()[0] ?? visibleItems[0]?.path;
      if (firstPath) focusRenderedItemPath(firstPath);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    activePath,
    browseData,
    focusRenderedItemPath,
    getRenderedItemPaths,
    isExplorerInteractionBlocked,
    loading,
    visibleItems,
  ]);

  useEffect(() => {
    const searchState = incrementalSearchRef.current;
    return () => {
      const timeoutId = searchState.timeoutId;
      if (timeoutId !== null) window.clearTimeout(timeoutId);
    };
  }, []);

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
      const activeElement = document.activeElement;
      if (
        activeElement &&
        activeElement !== document.body &&
        !explorerRootRef.current?.contains(activeElement)
      ) {
        return;
      }
      if (isExplorerInteractionBlocked || isTextInput(e.target)) return;
      const key = e.key.toLowerCase();
      const primaryModifier = e.ctrlKey || e.metaKey;
      const activePathForEvent =
        activePathRef.current ??
        activePath ??
        getRenderedItemPaths()[0] ??
        null;
      const activeItemForEvent = activePathForEvent
        ? (itemByPath.get(activePathForEvent) ?? null)
        : null;

      if (!primaryModifier && e.altKey && !e.shiftKey) {
        if (e.key === "ArrowLeft" || e.key === "Backspace") {
          e.preventDefault();
          goBack();
          return;
        }
        if (e.key === "ArrowRight") {
          e.preventDefault();
          goForward();
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          goUp();
          return;
        }
      }

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
      if (primaryModifier && !e.altKey && !e.shiftKey && key === "i") {
        e.preventDefault();
        void archiveSelectedItems();
        return;
      }
      if (primaryModifier && !e.altKey && !e.shiftKey && key === "u") {
        e.preventDefault();
        void extractSelectedItems();
        return;
      }
      if (!primaryModifier && !e.altKey && !e.shiftKey) {
        let offset = 0;
        if (e.key === "ArrowLeft") offset = -1;
        if (e.key === "ArrowRight") offset = 1;
        if (e.key === "ArrowUp") offset = -1;
        if (e.key === "ArrowDown") offset = 1;
        if (e.key === "PageUp") offset = -getVisibleItemPageSize();
        if (e.key === "PageDown") offset = getVisibleItemPageSize();
        if (offset !== 0) {
          e.preventDefault();
          focusItemByOffset(offset);
          return;
        }
        if (e.key === "Home") {
          e.preventDefault();
          focusItemByIndex(0);
          return;
        }
        if (e.key === "End") {
          e.preventDefault();
          focusItemByIndex(Number.MAX_SAFE_INTEGER);
          return;
        }
      }
      if (!primaryModifier && !e.altKey && !e.shiftKey && e.key === "Enter") {
        if (activeItemForEvent) {
          e.preventDefault();
          openExplorerItem(activeItemForEvent);
        }
        return;
      }
      if (!primaryModifier && !e.altKey && !e.shiftKey && e.key === "F2") {
        if (canUseFileShortcuts && activeItemForEvent) {
          e.preventDefault();
          setRenameTarget(activeItemForEvent);
        }
        return;
      }
      if (
        canUseFileShortcuts &&
        selectedPaths.length > 0 &&
        !primaryModifier &&
        !e.altKey &&
        !e.shiftKey &&
        (e.key === "Delete" || e.key === "Backspace")
      ) {
        e.preventDefault();
        void deleteSelectedItems();
        return;
      }
      if (
        !primaryModifier &&
        !e.altKey &&
        e.key.length === 1 &&
        !e.isComposing
      ) {
        e.preventDefault();
        handleIncrementalSearchCharacter(e.key);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    canUseFileShortcuts,
    archiveSelectedItems,
    copySelectedItems,
    deleteSelectedItems,
    extractSelectedItems,
    activePath,
    focusItemByIndex,
    focusItemByOffset,
    getVisibleItemPageSize,
    getRenderedItemPaths,
    goBack,
    goForward,
    goUp,
    handleIncrementalSearchCharacter,
    isExplorerInteractionBlocked,
    itemByPath,
    openExplorerItem,
    pasteClipboardItems,
    selectAll,
    selectedPaths.length,
  ]);

  // コンテキストメニュー（アイテム）
  const handleContextMenu = useCallback(
    (e: React.MouseEvent, item: ExplorerDirectory | ExplorerFile) => {
      e.preventDefault();
      setCtxItem(item);
      setCtxPos({ x: e.clientX, y: e.clientY });
    },
    [],
  );

  // 背景右クリック（アイテムなし）
  const handleBackgroundContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setCtxItem(null);
    setCtxPos({ x: e.clientX, y: e.clientY });
  }, []);

  // パンくず: コンテキストルート以降の相対パスのみ表示
  const breadcrumbs = useMemo(() => {
    if (!currentPath) return [];
    // HF 仮想パスは "HF|<account>|<type>|<repoId>|<subPath>" なので
    // "HF" を先頭、リポ名（表示用に簡略化）、サブパスセグメントを並べる
    if (isHfMode) {
      if (currentPath === HF_PREFIX) return ["HF"];
      const parts = currentPath.split("|");
      // parts[0]="HF", parts[1]=accountId, parts[2]=repoType, parts[3]=repoId, parts[4..]=subPath
      if (parts.length < 4) return [currentPath];
      const repoLabel = parts[3]; // "owner/name"
      const subPath = parts.slice(4).join("|");
      const crumbs = ["HF", repoLabel];
      if (subPath) crumbs.push(...subPath.split("/").filter(Boolean));
      return crumbs;
    }
    // 絶対パス閲覧時はフルパス表示
    if (isAbsoluteFilerPath) {
      return currentPath.split(/[/\\]/).filter(Boolean);
    }
    // コンテキストルートを省略して相対パスのみ
    if (contextRootPath && currentPath.startsWith(contextRootPath)) {
      const relative = currentPath
        .slice(contextRootPath.length)
        .replace(/^[/\\]/, "");
      return relative ? relative.split(/[/\\]/).filter(Boolean) : [];
    }
    return currentPath.split(/[/\\]/).filter(Boolean);
  }, [currentPath, contextRootPath, isAbsoluteFilerPath, isHfMode]);

  // ブックマーク: 非管理者は絶対パス（絶対パス閲覧）を非表示
  const filteredBookmarks = useMemo(() => {
    if (isAdmin) return bookmarks;
    return bookmarks.filter((bm) => !isAbsolutePath(bm.path));
  }, [bookmarks, isAdmin]);

  const removeBookmark = useCallback(
    async (path: string) => {
      await explorerRemoveBookmark(path);
      refreshBookmarks();
    },
    [refreshBookmarks],
  );

  // Homeボタンのナビゲート先
  const homeNavigate = useCallback(() => {
    if (isHfMode) {
      navigate(HF_PREFIX);
    } else {
      navigate(contextRootPath || "");
    }
  }, [navigate, contextRootPath, isHfMode]);

  return (
    <div ref={explorerRootRef} className="flex flex-col h-full">
      {/* エディタが開いている場合はスプリットビュー、それ以外はメインファイラー */}
      {recordTableFile?.project_id && recordTableFile.record_table_id ? (
        <RecordTableEditor
          projectId={recordTableFile.project_id}
          tableId={recordTableFile.record_table_id}
          initialName={recordTableFile.name.replace(/\.dbtable$/i, "")}
          onClose={() => setRecordTableFile(null)}
          onChanged={() => {
            refresh();
          }}
        />
      ) : editingFile ? (
        <EditorPane
          editingFile={editingFile}
          closeEditor={closeEditor}
          handleFileClick={handleFileClick}
          onContextMenu={handleContextMenu}
          onBackgroundContextMenu={handleBackgroundContextMenu}
        />
      ) : (
        <UploadZone onContextMenu={handleBackgroundContextMenu}>
          <div
            ref={explorerScrollRef}
            className="flex h-full flex-col overflow-auto p-4 space-y-3"
          >
            {/* ヘッダー: タイトル + Git */}
            <div className="flex items-center justify-between">
              <h1 className="text-lg font-bold">ファイラー</h1>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setGitOpen(true)}
                title="Git"
              >
                <GitBranch className="size-4 mr-1" />
                Git
              </Button>
            </div>

            {/* ファイラータブ: ワークスペース / ユーザー / HF / Hydrus */}
            <div className="flex gap-1 border-b border-border pb-2">
              <Button
                variant={
                  filerTab === "workspace" && !isAbsoluteFilerPath
                    ? "default"
                    : "outline"
                }
                size="sm"
                onClick={() => setFilerTab("workspace")}
              >
                ワークスペース
              </Button>
              <Button
                variant={
                  filerTab === "user" && !isAbsoluteFilerPath
                    ? "default"
                    : "outline"
                }
                size="sm"
                onClick={() => setFilerTab("user")}
              >
                ユーザー
              </Button>
              <Button
                variant={filerTab === "hf" ? "default" : "outline"}
                size="sm"
                onClick={() => setFilerTab("hf")}
              >
                HF
              </Button>
              <Button
                variant={filerTab === "hydrus" ? "default" : "outline"}
                size="sm"
                onClick={() => setFilerTab("hydrus")}
              >
                Hydrus
              </Button>
            </div>

            {/* Hydrus 検索バー（カスタムUIはこれだけ。結果は context の browseData に流す） */}
            {filerTab === "hydrus" && (
              <HydrusSearchBar
                onResults={(data) => {
                  setBrowseData(data);
                  setHydrusError(null);
                }}
                onError={(msg) => setHydrusError(msg)}
              />
            )}
            {filerTab === "hydrus" && hydrusError && (
              <div className="py-2 text-center text-xs text-destructive">
                {hydrusError}
              </div>
            )}

            {/* HF タグ/作者フィルタ（creator_mapping.json があるリポのみ表示） */}
            {filerTab === "hf" && isHfMode && hfCreatorMapping && (
              <div className="flex items-center gap-2 rounded-md border bg-muted/20 p-2">
                <Input
                  value={hfSearchQuery}
                  onChange={(e) => setHfSearchQuery(e.target.value)}
                  placeholder="作者名・フォルダ名・タグで検索..."
                  className="h-8"
                />
                {hfSearchQuery && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setHfSearchQuery("")}
                  >
                    クリア
                  </Button>
                )}
              </div>
            )}

            {/* ブックマーク（ワークスペース/ユーザーのみ） */}
            {filerTab !== "hf" &&
              filerTab !== "hydrus" &&
              filteredBookmarks.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {filteredBookmarks.map((bm) => (
                    <div
                      key={bm.path}
                      className="inline-flex items-center overflow-hidden rounded-md border border-border"
                    >
                      <Button
                        variant={currentPath === bm.path ? "default" : "ghost"}
                        size="xs"
                        className="rounded-none border-0"
                        onClick={() => navigate(bm.path)}
                      >
                        <Star className="size-3 mr-1" />
                        {bm.name}
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        className="size-6 rounded-none border-l border-border"
                        title={`${bm.name} をブックマークから削除`}
                        onClick={() => void removeBookmark(bm.path)}
                      >
                        <X className="size-3" />
                      </Button>
                    </div>
                  ))}
                </div>
              )}

            {/* ツールバー（HF/Hydrus でも表示。書き込みボタンは ExplorerToolbar 側で自動的に無効化） */}
            {filerTab !== "hydrus" && (
              <ExplorerToolbar
                onNewFolder={() => setNewFolderOpen(true)}
                onNewRecordTable={() => setNewRecordTableOpen(true)}
              />
            )}

            {canUseExplorerSearch && (
              <form
                className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/20 p-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  void runFileSearch();
                }}
              >
                <div className="relative min-w-56 flex-1">
                  <Search className="pointer-events-none absolute left-2 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={fileSearchQuery}
                    onChange={(e) => setFileSearchQuery(e.target.value)}
                    placeholder="現在のフォルダ内を検索..."
                    className="h-9 pl-8"
                  />
                </div>
                <Button
                  type="submit"
                  size="sm"
                  disabled={fileSearchLoading || !fileSearchQuery.trim()}
                >
                  {fileSearchLoading ? "検索中..." : "検索"}
                </Button>
                {(fileSearchActive || fileSearchQuery) && (
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    onClick={clearFileSearch}
                  >
                    クリア
                  </Button>
                )}
                {fileSearchActive && (
                  <span className="text-xs text-muted-foreground">
                    {fileSearchCount}件
                  </span>
                )}
                {fileSearchError && (
                  <span className="text-xs text-destructive">
                    {fileSearchError}
                  </span>
                )}
              </form>
            )}

            {/* パンくず */}
            {filerTab !== "hydrus" && (
              <div className="flex items-center gap-1 text-sm flex-wrap">
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={homeNavigate}
                  title="ホーム"
                >
                  <Home className="size-3" />
                </Button>
                {breadcrumbs.slice(-4).map((segment, i, arr) => {
                  const fullIndex = breadcrumbs.length - arr.length + i;
                  let partialPath: string;
                  if (isHfMode) {
                    // HF: breadcrumbs[0]="HF", [1]="owner/name", [2..]=subPath
                    if (fullIndex === 0) {
                      partialPath = HF_PREFIX;
                    } else {
                      const parts = currentPath.split("|");
                      // リポジトリルートまで
                      const repoBase = parts.slice(0, 4).join("|");
                      if (fullIndex === 1) {
                        partialPath = repoBase;
                      } else {
                        const subCrumbs = breadcrumbs.slice(2, fullIndex + 1);
                        partialPath = repoBase + "|" + subCrumbs.join("/");
                      }
                    }
                  } else if (isAbsoluteFilerPath) {
                    const allSegments = currentPath
                      .split(/[/\\]/)
                      .filter(Boolean);
                    partialPath = allSegments.slice(0, fullIndex + 1).join("/");
                  } else if (contextRootPath) {
                    partialPath =
                      contextRootPath +
                      "/" +
                      breadcrumbs.slice(0, fullIndex + 1).join("/");
                  } else {
                    partialPath = breadcrumbs.slice(0, fullIndex + 1).join("/");
                  }
                  const isLast = fullIndex === breadcrumbs.length - 1;
                  return (
                    <span key={fullIndex} className="flex items-center gap-1">
                      <ChevronRight className="size-3 text-muted-foreground" />
                      {isLast ? (
                        <span className="font-medium text-xs">{segment}</span>
                      ) : (
                        <Button
                          variant="ghost"
                          size="xs"
                          onClick={() => navigate(partialPath)}
                        >
                          {segment}
                        </Button>
                      )}
                    </span>
                  );
                })}
              </div>
            )}

            {/* ローディング */}
            {filerTab !== "hydrus" && loading && (
              <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-2">
                {Array.from({ length: 12 }).map((_, i) => (
                  <Skeleton key={i} className="aspect-square rounded-lg" />
                ))}
              </div>
            )}

            {/* エラー */}
            {error && !loading && (
              <div className="py-4 text-center text-sm text-destructive">
                {error}
              </div>
            )}

            {/* 上のフォルダへ（HF/ワークスペース/ユーザー 共通） */}
            {filerTab !== "hydrus" &&
              !loading &&
              browseData?.can_go_up &&
              browseData.parent_path !== null && (
                <button
                  className="flex items-center gap-2 px-3 py-1.5 text-sm rounded-md hover:bg-accent/50 transition-colors text-muted-foreground"
                  onClick={goUp}
                >
                  <ArrowUp className="size-4" />
                  <span>上のフォルダへ</span>
                </button>
              )}

            {/* ファイル一覧: HF/Workspace/User/Hydrus すべて同じ FileGrid/FileList で描画 */}
            {!loading &&
              browseData &&
              (viewMode === "grid" ? (
                <FileGrid
                  onFileClick={handleFileClick}
                  onContextMenu={handleContextMenu}
                />
              ) : (
                <FileList
                  onFileClick={handleFileClick}
                  onContextMenu={handleContextMenu}
                />
              ))}

            {/* 空フォルダ */}
            {filerTab !== "hydrus" &&
              !loading &&
              browseData &&
              browseData.directories.length === 0 &&
              browseData.files.length === 0 && (
                <div className="mx-auto my-8 flex max-w-lg flex-col items-center overflow-hidden rounded-2xl border border-white/65 bg-white/56 text-center text-sm text-muted-foreground shadow-[inset_0_1px_rgba(255,255,255,0.76),0_24px_64px_-46px_rgba(6,81,110,0.75)] backdrop-blur-xl dark:border-white/12 dark:bg-card/75 dark:shadow-[inset_0_1px_rgba(255,255,255,0.12),0_24px_64px_-46px_rgba(0,0,0,0.9)]">
                  <img
                    src="/images/ui/empty-files.png"
                    alt=""
                    className="aspect-[16/7] w-full object-cover"
                  />
                  <div className="px-6 py-5">
                    {fileSearchActive
                      ? "検索結果はありません。"
                      : isHfMode
                        ? "HFリポジトリは空です。"
                        : "このフォルダは空です。ファイルをドラッグ&ドロップでアップロードできます。"}
                  </div>
                </div>
              )}
          </div>
        </UploadZone>
      )}

      {/* コンテキストメニュー・ダイアログ: ファイラー・エディタ画面共通 */}
      <FileContextMenu
        item={ctxItem}
        position={ctxPos}
        onClose={() => {
          setCtxItem(null);
          setCtxPos(null);
        }}
        onRename={(item) => {
          setRenameTarget(item);
          setCtxItem(null);
          setCtxPos(null);
        }}
        onProperties={(item) => {
          if ("type" in item) setPreviewFile(item as ExplorerFile);
          setCtxItem(null);
          setCtxPos(null);
        }}
        onOpen={(item) => {
          handleFileClick(item as ExplorerFile);
          setCtxItem(null);
          setCtxPos(null);
        }}
        onNewFolder={() => setNewFolderOpen(true)}
        onNewTextFile={() => setNewTextFileOpen(true)}
      />
      <NewFolderDialog open={newFolderOpen} onOpenChange={setNewFolderOpen} />
      <NewTextFileDialog
        open={newTextFileOpen}
        onOpenChange={setNewTextFileOpen}
        onCreated={(path, name) => {
          openEditor({ path, name });
        }}
      />
      <NewRecordTableDialog
        open={newRecordTableOpen}
        onOpenChange={setNewRecordTableOpen}
        onCreated={(file) => {
          setRecordTableFile(file);
        }}
      />
      <RenameDialog
        item={renameTarget}
        open={renameTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRenameTarget(null);
        }}
      />
      <GitPanel open={gitOpen} onOpenChange={setGitOpen} />
      {viewerFile && browseData && (
        <FileViewer
          file={viewerFile}
          files={browseData.files}
          onClose={() => setViewerFile(null)}
          onNavigate={(f) => setViewerFile(f)}
        />
      )}
      {previewFile && (
        <FilePreviewPanel
          file={previewFile}
          onClose={() => setPreviewFile(null)}
        />
      )}
    </div>
  );
}

// ─── ページエントリ ───
function FilerPageInner() {
  return (
    <ExplorerProvider>
      <ExplorerContent />
    </ExplorerProvider>
  );
}

export default function FilerPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-full items-center justify-center">
          <div className="text-sm text-muted-foreground">読み込み中...</div>
        </div>
      }
    >
      <FilerPageInner />
    </Suspense>
  );
}

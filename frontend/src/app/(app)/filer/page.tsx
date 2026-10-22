"use client";

import { useConfirm } from "@/hooks/use-confirm";
import { FileViewer, isImage, isVideo } from "./file-viewer";
import { StorageControls, StorageWorkspace, StorageWorkspacePanel, useStorageWorkspace } from "@/components/explorer/storage-workspace";
import { scrollExplorerItemIntoView } from "./scroll-explorer-item";

/* eslint-disable @next/next/no-img-element */

import {
  useState,
  useEffect,
  useLayoutEffect,
  useCallback,
  useRef,
  Suspense,
  useMemo,
} from "react";
import { useSearchParams } from "next/navigation";
import { useAudioPlayer } from "@/contexts/audio-player-context";
import {
  ExplorerProvider,
  FILER_TAB_LABELS,
  type FilerTab,
  useExplorer,
} from "@/contexts/explorer-context";
import { useProject } from "@/contexts/project-context";
import { useSnippets } from "@/contexts/snippets-context";
import { ExplorerToolbar } from "@/components/explorer/explorer-toolbar";
import { FileGrid } from "@/components/explorer/file-grid";
import { FileList } from "@/components/explorer/file-list";
import { FileContextMenu } from "@/components/explorer/file-context-menu";
import { NewFolderDialog } from "@/components/explorer/new-folder-dialog";
import { NewItemNameInput } from "@/components/explorer/new-item-name-input";
import { RenameDialog } from "@/components/explorer/rename-dialog";
import { UploadZone } from "@/components/explorer/upload-zone";
import { FilePreviewPanel } from "@/components/explorer/file-preview-panel";
import { RecordTableEditor } from "@/components/records/record-table-editor";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
  X,
  Search,
  FileIcon,
  Folder,
} from "lucide-react";
import { withBasePath } from "@/lib/base-path";
import type {
  ExplorerDirectory,
  ExplorerFile,
  ExplorerSearchResponse,
  SearchResult,
} from "@/lib/explorer-api";
import {
  ExplorerUploadError,
  explorerArchive,
  explorerDownloadPaths,
  explorerErrorMessage,
  explorerExtract,
  explorerFullContent,
  explorerUpload,
  explorerSearch,
} from "@/lib/explorer-api";
import { uploadFailureToastOptions } from "@/lib/upload-failure";
import {
  canRedoFiler,
  canUndoFiler,
  useFilerOperations,
} from "@/hooks/use-filer-operations";
import { toFilerDeleteTarget } from "@/lib/explorer/filer-operations";
import { type FilerSearchItem } from "@/lib/migemo-lite";
import { useIncrementalSearch } from "@/hooks/use-incremental-search";
import {
  isRecordTableFile,
} from "@/lib/record-tables-api";
import { cn } from "@/lib/utils";
import { replaceFilerName } from "@/lib/explorer/filer-search";
import { resolveFilerCreationShortcut } from "@/lib/explorer/filer-creation-shortcuts";
import {
  buildExplorerProjectScopeIdentity,
  useExplorerOpenPath,
} from "@/lib/explorer-open-path";
import { FilesBookmarkLauncherSidebar } from "@/components/explorer/files-bookmark-launcher-sidebar";
import {
  FilesIndexDialog,
  FilesIndexStatusBar,
  FilesSemanticResults,
  filesScopeLabel,
} from "@/components/explorer/files-semantic-search";
import { useFilesSemanticSearch } from "@/hooks/use-files-semantic-search";
import {
  resolveFilesSemanticScope,
  type FilesSemanticResult,
} from "@/lib/explorer/files-semantic-search";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import {
  FILES_DOWNLOAD_PATH_EVENT,
  requestFilesBookmark,
} from "@/lib/files-sidebar-events";
import dynamic from "next/dynamic";
import { toast } from "sonner";
import {
  DOUBLE_ESCAPE_RESET_EVENT,
  EMPTY_DOUBLE_ESCAPE_STATE,
  resetDoubleEscapeState,
  transitionDoubleEscapeKey,
  type DoubleEscapeState,
} from "@/lib/double-escape";
import {
  CLEAN_EDITOR_EDIT_STATE,
  DIRTY_ESCAPE_NOTICE,
  DOCUMENT_EDITOR_SELECTOR,
  focusDocumentEditorSurface,
  isEditorTransientEscapeSurface,
  isOrphanedFocusTarget,
  nextOrphanedFocusOwner,
  shouldHoldDirtyEditorClose,
  type EditorEditState,
} from "@/lib/double-escape-focus";

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
function isAudio(type: string) {
  return type.startsWith("audio");
}
type ExplorerItem = ExplorerDirectory | ExplorerFile;
const FILER_TAB_ORDER: readonly FilerTab[] = [
  "workspace",
  "user",
];
const SUPPORTED_ARCHIVE_SUFFIXES = [
  ".tar.bz2",
  ".tar.gz",
  ".tar.xz",
  ".tbz2",
  ".tgz",
  ".txz",
  ".7z",
  ".tar",
  ".zip",
  ".bz2",
  ".gz",
  ".xz",
] as const;

function isSupportedArchiveName(name: string) {
  const lowerName = name.toLowerCase();
  return SUPPORTED_ARCHIVE_SUFFIXES.some((suffix) =>
    lowerName.endsWith(suffix),
  );
}

function isExplorerDirectory(item: ExplorerItem): item is ExplorerDirectory {
  return !("type" in item);
}

/**
 * The Files editor and its persistent bookmark/launcher sidebar are siblings
 * in the shared shell, so the sidebar is intentionally outside the canvas
 * root.  Treat only those Files-marked surfaces as one keyboard workspace;
 * unrelated shell/header focus still resets the double-Escape sequence.  The
 * canvas keeps the singular `data-shell-workspace` marker; the shell slot uses
 * the existing `data-workspace` marker instead.
 */
function isFilesWorkspaceTarget(
  target: EventTarget | null,
  canvasRoot: HTMLElement | null,
): boolean {
  if (!(target instanceof Node)) return false;
  if (canvasRoot?.contains(target)) return true;
  return target instanceof Element && Boolean(
    target.closest(
      '[data-shell-workspace="files"], [data-workspace="files"], [data-shell-region="files-bookmark-launcher-sidebar"], [data-files-bookmark-quick-launcher]',
    ),
  );
}

/**
 * Keyboard delivery from a browser/CUA surface can report a broader target
 * than the element that still owns DOM focus.  Keep the document boundary
 * scoped to Files while accepting that focused Files control as the fallback.
 */
function isFilesWorkspaceKeyboardTarget(
  target: EventTarget | null,
  canvasRoot: HTMLElement | null,
  filesOwnsOrphanedFocus: boolean,
): boolean {
  if (
    isFilesWorkspaceTarget(target, canvasRoot) ||
    isFilesWorkspaceTarget(document.activeElement, canvasRoot)
  ) {
    return true;
  }
  // Disabling the focused Save button, hiding the editor for the Markdown
  // preview, unmounting a menu, or clicking a non-focusable spot drops focus
  // to <body> without moving it anywhere else.  Keys then target <body>, so
  // without this fallback Esc×2 silently stopped working depending on what
  // the user last clicked.  Accept that orphaned focus only while Files was
  // the last keyboard/pointer owner (see filesOwnsOrphanedFocusRef).
  return filesOwnsOrphanedFocus && isOrphanedFocusTarget(target);
}

/**
 * Alt+A のクイックランチャーが開いている間は、そちらのニーモニック入力と
 * ショートカットを優先し、Files 側のインクリメンタルサーチは抑止する。
 */
function isBookmarkQuickLauncherOpen(): boolean {
  return Boolean(
    document.querySelector(
      '[data-files-bookmark-quick-launcher][data-open], [data-files-bookmark-quick-launcher][data-slot="dropdown-menu-content"][data-open]',
    ),
  );
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
    modified_at: item.modified_at,
  };
}

/** 検索パネルのモード: 名前・本文の文字列検索 / 内容（意味）検索。 */
type FileSearchMode = "string" | "semantic";
/** 文字列検索の対象: ファイル名・フォルダ名 / テキストファイル本文。 */
type FileSearchTarget = "name" | "content";

interface ContentSearchHit {
  path: string;
  name: string;
  matchCount: number;
  matches: { line: number; text: string }[];
}

interface ContentSearchSummary {
  hits: ContentSearchHit[];
  limitReason: ExplorerSearchResponse["limit_reason"];
  skippedLargeFiles: number;
}

const CONTENT_LIMIT_LABELS: Record<string, string> = {
  results: "件数上限に達したため、残りは表示していません",
  scan: "走査上限（10,000項目）に達したため、一部のフォルダは未検索です",
  bytes: "読み取り量の上限に達したため、一部のファイルは未検索です",
};

function normalizeFilerPath(path: string): string {
  return path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
}

function filerParentPath(path: string): string {
  const normalized = normalizeFilerPath(path);
  const index = normalized.lastIndexOf("/");
  return index > 0 ? normalized.slice(0, index) : "";
}

// ─── ファイルビューア（画像/動画全画面モーダル） ───
// 絶対パス判定（ブックマーク権限フィルタ用）
// ─── ファイルエクスプローラメインコンテンツ ───
// テキストファイル拡張子
const TEXT_EXTS = new Set([
  ".txt",
  ".md",
  ".markdown",
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
  ".bat",
  ".cmd",
  ".sh",
  ".ps1",
  ".vbs",
]);

// ─── エディタスプリットビュー ───
function EditorPane({
  editingFile,
  closeEditor,
  handleFileClick,
  onContextMenu,
  onBackgroundContextMenu,
  onEditStateChange,
}: {
  editingFile: { path: string; name: string; extension: string };
  closeEditor: () => void;
  onEditStateChange?: (state: EditorEditState) => void;
  handleFileClick: (file: ExplorerFile) => void;
  onContextMenu: (
    e: React.MouseEvent,
    item: ExplorerDirectory | ExplorerFile,
  ) => void;
  onBackgroundContextMenu: (e: React.MouseEvent) => void;
}) {
  const { browseData, navigate, currentPath, goUp, refresh, filerTab } = useExplorer();
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
    <div className="flex h-full min-h-0" data-shell-region="files-editor">
      {/* 左: ファイルリスト（コンパクト） */}
      <div
        className="hidden w-[240px] shrink-0 flex-col overflow-auto border-r border-border bg-card md:flex"
        onContextMenu={onBackgroundContextMenu}
      >
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-border px-4">
          <span className="text-[16px] font-semibold">Files</span>
          <div className="flex items-center gap-1 text-muted-foreground">
            <Button variant="ghost" size="icon-xs" className="text-muted-foreground hover:bg-muted" onClick={() => void refresh()} title="更新"><Search className="size-3.5" /></Button>
          </div>
        </div>
        <div className="border-b border-border px-4 py-3">
          <div
            className="truncate text-[11px] font-semibold uppercase tracking-wider text-muted-foreground"
            title={currentPath}
          >
            {FILER_TAB_LABELS[filerTab]} / {currentPath.split("/").pop() || "ルート"}
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-2">
          {browseData?.can_go_up && (
            <button
              className="mb-1 flex h-7 w-full items-center gap-1.5 rounded px-2 text-xs text-muted-foreground hover:bg-muted"
              onClick={goUp}
            >
              <ArrowUp className="size-3" />
              上へ
            </button>
          )}
          {browseData?.directories.map((dir) => (
            <button
              key={dir.path}
              className="flex h-7 w-full items-center gap-1.5 truncate rounded px-2 text-[13px] text-muted-foreground hover:bg-muted hover:text-foreground"
              onClick={() => navigate(dir.path)}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onContextMenu(e, dir);
              }}
              title={dir.name}
            >
              <><span className="text-muted-foreground">▸</span><Folder className="size-3.5 shrink-0 text-tertiary" />{dir.name}</>
            </button>
          ))}
          {browseData?.files.map((file) => (
            <button
              key={file.path}
              className={`flex h-7 w-full items-center gap-1.5 truncate rounded px-2 text-[13px] hover:bg-muted ${
                file.path === editingFile.path
                  ? "border-l-2 border-primary bg-muted font-medium text-foreground"
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
              <><FileIcon className="size-3.5 shrink-0 text-muted-foreground" />{file.name}</>
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
              autoFocus
              onClose={closeEditor}
              onEditStateChange={onEditStateChange}
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
  const confirmOverwrite = useConfirm();
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);

  const handleCreate = async () => {
    const trimmed = name.trim();
    if (!trimmed || loading) return;
    const fileName = trimmed.includes(".") ? trimmed : trimmed + ".md";
    const filePath = currentPath ? `${currentPath}/${fileName}` : fileName;
    setLoading(true);
    try {
      const file = new File([""], fileName, { type: "text/plain" });
      const result = await explorerUpload(currentPath, [file], { confirmOverwrite });
      if (result.successCount === 0) return;
      refresh();
      setName("");
      onOpenChange(false);
      onCreated(filePath, fileName);
    } catch (error) {
      if (error instanceof ExplorerUploadError) {
        toast.error(error.message, uploadFailureToastOptions(error.batchResult.failures));
      } else {
        toast.error(
          error instanceof Error ? error.message : "ファイルの作成に失敗しました",
        );
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新規テキストファイル</DialogTitle>
        </DialogHeader>
        <NewItemNameInput
          placeholder="ファイル名（例: notes.txt）"
          value={name}
          onValueChange={setName}
          onSubmit={handleCreate}
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

function ExplorerContent() {
  const explorerRootRef = useRef<HTMLDivElement>(null);
  const explorerScrollRef = useRef<HTMLDivElement>(null);
  const pendingEditorEscapeFocusExitFrameRef = useRef<number | null>(null);
  // True while Files was the last surface to receive focus or a pointer
  // press.  Lets the Esc×2 boundary accept keys whose focus fell back to
  // <body> from a Files control, while a click/focus elsewhere in the shell
  // (header, nav rail, other panels) still excludes them.
  const filesOwnsOrphanedFocusRef = useRef(false);
  // Unsaved / failed-save state reported by the Files DocumentEditor; the
  // Esc×2 boundary must never discard such edits from a newly enabled path.
  const editorEditStateRef = useRef<EditorEditState>(CLEAN_EDITOR_EDIT_STATE);
  const handleEditorEditStateChange = useCallback((state: EditorEditState) => {
    editorEditStateRef.current = state;
  }, []);
  const contextMenuFocusFrameRef = useRef<number | null>(null);
  const pendingIncrementalSearchRef = useRef("");
  const quickFilterInputRef = useRef<HTMLInputElement>(null);
  const fileSearchInputRef = useRef<HTMLInputElement>(null);
  const fileSearchReplaceInputRef = useRef<HTMLInputElement>(null);
  const isTextInput = useCallback((target: EventTarget | null) => {
    if (!(target instanceof HTMLElement)) return false;
    return (
      target.tagName === "INPUT" ||
      target.tagName === "TEXTAREA" ||
      target.isContentEditable
    );
  }, []);
  const activePathRef = useRef<string | null>(null);
  // A restore signal is emitted only for a successful Back/Forward view-state
  // restore.  Remember the navigation identity we have consumed so ordinary
  // Arrow/click focus changes (or unrelated rerenders) never scroll again.
  const consumedHistoryScrollRestoreRef = useRef<string | null>(null);
  const searchParams = useSearchParams();
  const {isDefaultStorage, selectDefaultStorage} = useStorageWorkspace();
  const { selectedSpaceId, selectedProjectId, selectedProject } = useProject();
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
    setViewMode,
    setSort,
    filerTab,
    setFilerTab,
    homeRootPath,
    contextRootPath,
    isAbsoluteFilerPath,
    isRemoteWorkspace,
    capabilities,
    setBrowseData,
    selectedItems,
    focusedItemPath,
    historyScrollRestore,
    selectItem,
    toggleSelect,
    selectRange,
    selectAll,
    clearSelection,
    clipboard,
    setClipboard,
    editingFile,
    openEditor,
    closeEditor,
    userId,
  } = useExplorer();

  // Homeボタンと Ctrl+H のナビゲート先。
  const homeNavigate = useCallback(() => {
    navigate(homeRootPath || "");
  }, [navigate, homeRootPath]);

  const cycleFilerTab = useCallback(
    (direction: -1 | 1) => {
      const currentIndex = FILER_TAB_ORDER.indexOf(filerTab);
      const nextIndex =
        (Math.max(0, currentIndex) + direction + FILER_TAB_ORDER.length) %
        FILER_TAB_ORDER.length;
      const nextTab = FILER_TAB_ORDER[nextIndex];

      if (nextTab) {
        setFilerTab(nextTab);
      }
    },
    [filerTab, setFilerTab],
  );

  // 削除・移動・Undo/Redo の実行は filer-operations 側へ集約。
  const { deleteTargets, transfer, undo, redo, renameBatch } = useFilerOperations({
    capabilities,
    refresh,
  });

  const [fileSearchQuery, setFileSearchQuery] = useState("");
  const [fileSearchReplaceQuery, setFileSearchReplaceQuery] = useState("");
  const [fileSearchRegex, setFileSearchRegex] = useState(false);
  const [fileSearchOpen, setFileSearchOpen] = useState(false);
  const [fileSearchActive, setFileSearchActive] = useState(false);
  const [fileSearchLoading, setFileSearchLoading] = useState(false);
  const [fileSearchError, setFileSearchError] = useState<string | null>(null);
  const [fileSearchCount, setFileSearchCount] = useState(0);
  const [fileSearchTruncated, setFileSearchTruncated] = useState(false);
  const [fileSearchMode, setFileSearchMode] = useState<FileSearchMode>("string");
  const [fileSearchTarget, setFileSearchTarget] = useState<FileSearchTarget>("name");
  const [contentSearchSummary, setContentSearchSummary] =
    useState<ContentSearchSummary | null>(null);
  const [semanticUnderFolder, setSemanticUnderFolder] = useState(true);
  const [filesIndexDialogOpen, setFilesIndexDialogOpen] = useState(false);
  // 「Filesで場所を開く」で移動した後、読み込み完了時に選択するファイル。
  const pendingRevealPathRef = useRef<string | null>(null);
  const fileSearchModeRef = useRef<FileSearchMode>("string");
  useEffect(() => {
    fileSearchModeRef.current = fileSearchMode;
  }, [fileSearchMode]);
  const fileSearchGenerationRef = useRef(0);
  const [quickFilterOpen, setQuickFilterOpen] = useState(false);
  const [quickFilterQuery, setQuickFilterQuery] = useState("");
  // `openEditorFromFiler` is also used by the URL-open effect.  Keep the
  // transient search state in refs so that the callback does not change
  // identity whenever a filter is typed (and accidentally re-run that
  // effect).
  const fileSearchStateRef = useRef({ open: false, active: false });
  const refreshRef = useRef(refresh);
  useEffect(() => {
    fileSearchStateRef.current = {
      open: fileSearchOpen,
      active: fileSearchActive,
    };
  }, [fileSearchActive, fileSearchOpen]);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);
  const audioPlayer = useAudioPlayer();

  // ダイアログ状態
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newTextFileOpen, setNewTextFileOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<
    ExplorerDirectory | ExplorerFile | null
  >(null);
  const [viewerFile, setViewerFile] = useState<ExplorerFile | null>(null);
  const viewerReturnFocusRef = useRef<HTMLElement | null>(null);
  const [previewFile, setPreviewFile] = useState<ExplorerFile | null>(null);
  const [recordTableFile, setRecordTableFile] = useState<ExplorerFile | null>(
    null,
  );

  // Do not retain an A user's viewer/preview selection while the authenticated
  // principal changes (including logout).  The panel itself also gates late
  // metadata responses, but clearing these roots prevents one-frame leakage of
  // the previous file entirely.
  useEffect(() => {
    setViewerFile(null);
    setPreviewFile(null);
    setCtxItem(null);
    setCtxPos(null);
    setRenameTarget(null);
    setRecordTableFile(null);
    closeEditor();
  }, [closeEditor, userId]);

  // コンテキストメニュー
  const [ctxItem, setCtxItem] = useState<
    ExplorerDirectory | ExplorerFile | null
  >(null);
  const [ctxPos, setCtxPos] = useState<{ x: number; y: number } | null>(null);
  const editorEscapeStateRef = useRef<DoubleEscapeState>(
    EMPTY_DOUBLE_ESCAPE_STATE,
  );
  // The shared shell boundary and the canvas React capture handler both see
  // key events for the main Files surface.  Mark each native event once so
  // the state machine is never advanced twice for the same Escape.
  const handledEditorEscapeEventsRef = useRef<WeakSet<Event>>(new WeakSet());

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
  const normalizedQuickFilter = quickFilterQuery.trim().toLowerCase();
  const visibleDirectories = useMemo(
    () =>
      (browseData?.directories ?? []).filter((directory) =>
        directory.name.toLowerCase().includes(normalizedQuickFilter),
      ),
    [browseData?.directories, normalizedQuickFilter],
  );
  const visibleFiles = useMemo(
    () =>
      (browseData?.files ?? []).filter((file) =>
        file.name.toLowerCase().includes(normalizedQuickFilter),
      ),
    [browseData?.files, normalizedQuickFilter],
  );
  const visibleItems = useMemo<ExplorerItem[]>(
    () => [...visibleDirectories, ...visibleFiles],
    [visibleDirectories, visibleFiles],
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
  const selectedArchivePaths = useMemo(
    () =>
      selectedRegularPaths.filter((path) => {
        const item = itemByPath.get(path);
        if (!item || !("type" in item)) return false;
        return isSupportedArchiveName(item.name);
      }),
    [itemByPath, selectedRegularPaths],
  );
  const activePath =
    focusedItemPath && itemByPath.has(focusedItemPath)
      ? focusedItemPath
      : (selectedPaths[0] ?? null);
  const activeItem = activePath ? (itemByPath.get(activePath) ?? null) : null;
  const canUseFileShortcuts = !isRemoteWorkspace;
  const canUseDownloadShortcut = true;
  const canUseExplorerSearch = true;
  const isExplorerInteractionBlocked =
    loading ||
    !!editingFile ||
    !!recordTableFile ||
    !!viewerFile ||
    !!previewFile ||
    !!renameTarget ||
    newFolderOpen ||
    newTextFileOpen ||
    !!ctxPos;
  const isEditorEscapeBlocked =
    !!recordTableFile ||
    !!viewerFile ||
    !!previewFile ||
    !!renameTarget ||
    newFolderOpen ||
    newTextFileOpen ||
    quickFilterOpen ||
    !!ctxPos;

  const resetEditorEscape = useCallback(() => {
    editorEscapeStateRef.current = resetDoubleEscapeState();
  }, []);

  const scheduleEditorEscapeFocusExitCheck = useCallback(() => {
    if (pendingEditorEscapeFocusExitFrameRef.current !== null) {
      window.cancelAnimationFrame(
        pendingEditorEscapeFocusExitFrameRef.current,
      );
    }
    pendingEditorEscapeFocusExitFrameRef.current = window.requestAnimationFrame(
      () => {
        pendingEditorEscapeFocusExitFrameRef.current = null;
        if (
          isFilesWorkspaceTarget(
            document.activeElement,
            explorerRootRef.current,
          )
        ) {
          return;
        }
        resetEditorEscape();
      },
    );
  }, [resetEditorEscape]);

  useEffect(() => {
    return () => {
      if (pendingEditorEscapeFocusExitFrameRef.current !== null) {
        window.cancelAnimationFrame(
          pendingEditorEscapeFocusExitFrameRef.current,
        );
        pendingEditorEscapeFocusExitFrameRef.current = null;
      }
      if (contextMenuFocusFrameRef.current !== null) {
        window.cancelAnimationFrame(contextMenuFocusFrameRef.current);
        contextMenuFocusFrameRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    resetEditorEscape();
  }, [
    currentPath,
    editingFile?.path,
    filerTab,
    isEditorEscapeBlocked,
    resetEditorEscape,
  ]);

  useEffect(() => {
    window.addEventListener("blur", resetEditorEscape);
    return () => window.removeEventListener("blur", resetEditorEscape);
  }, [resetEditorEscape]);

  // Shell-owned transient panels consume their Escape before it reaches the
  // workspace.  Reset the armed state through a narrow event bridge so that
  // closing such a panel can never become the editor's second Escape.
  useEffect(() => {
    window.addEventListener(DOUBLE_ESCAPE_RESET_EVENT, resetEditorEscape);
    return () => window.removeEventListener(DOUBLE_ESCAPE_RESET_EVENT, resetEditorEscape);
  }, [resetEditorEscape]);

  const processEditorEscape = useCallback(
    (
      event: {
        key: string;
        repeat: boolean;
        defaultPrevented?: boolean;
        isComposing?: boolean;
        keyCode?: number;
        preventDefault: () => void;
        stopPropagation: () => void;
        stopImmediatePropagation?: () => void;
      },
      nativeEvent: Event,
    ) => {
      if (handledEditorEscapeEventsRef.current.has(nativeEvent)) return;
      handledEditorEscapeEventsRef.current.add(nativeEvent);
      if (!editingFile) {
        resetEditorEscape();
        return;
      }
      // React's SyntheticKeyboardEvent has no isComposing, and the first
      // handler to run (React capture) marks the native event as handled, so
      // read the IME state from the native event.
      const composing = nativeEvent as KeyboardEvent;
      const next = transitionDoubleEscapeKey(
        editorEscapeStateRef.current,
        {
          key: event.key,
          repeat: event.repeat,
          defaultPrevented: event.defaultPrevented,
          isComposing: composing.isComposing,
          keyCode: composing.keyCode,
        },
        {
          // An open completion popup / search panel consumes its own
          // Escape; that press must neither arm nor complete the sequence.
          blocked:
            isEditorEscapeBlocked ||
            isBookmarkQuickLauncherOpen() ||
            isEditorTransientEscapeSurface(
              nativeEvent.target,
              explorerRootRef.current?.querySelector(DOCUMENT_EDITOR_SELECTOR) ??
                null,
            ),
          now: performance.now(),
        },
      );
      editorEscapeStateRef.current = next.state;
      if (!next.shouldClose) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation?.();
      if (
        shouldHoldDirtyEditorClose(
          editorEditStateRef.current,
          nativeEvent.target,
        )
      ) {
        toast.warning(DIRTY_ESCAPE_NOTICE, { id: "files-editor-dirty-escape" });
        focusDocumentEditorSurface(
          explorerRootRef.current?.querySelector(DOCUMENT_EDITOR_SELECTOR) ??
            null,
        );
        return;
      }
      closeEditor();
    },
    [closeEditor, editingFile, isEditorEscapeBlocked, resetEditorEscape],
  );

  const handleEditorKeyDownCapture = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      processEditorEscape(event, event.nativeEvent);
    },
    [processEditorEscape],
  );

  // The sidebar is rendered beside the canvas, and the mobile Sheet renders a
  // Portal outside the shell subtree.  A document capture boundary covers
  // both while remaining scoped to Files-marked surfaces (never all Escapes).
  useEffect(() => {
    const handleFilesKeyDown = (event: KeyboardEvent) => {
      if (
        !isFilesWorkspaceKeyboardTarget(
          event.target,
          explorerRootRef.current,
          filesOwnsOrphanedFocusRef.current,
        )
      ) {
        return;
      }
      processEditorEscape(event, event);
    };
    document.addEventListener("keydown", handleFilesKeyDown, true);
    return () => document.removeEventListener("keydown", handleFilesKeyDown, true);
  }, [processEditorEscape]);

  useEffect(() => {
    const trackOwner = (event: Event) => {
      filesOwnsOrphanedFocusRef.current = nextOrphanedFocusOwner(
        filesOwnsOrphanedFocusRef.current,
        event.target,
        (target) => isFilesWorkspaceTarget(target, explorerRootRef.current),
      );
    };
    document.addEventListener("focusin", trackOwner, true);
    document.addEventListener("pointerdown", trackOwner, true);
    filesOwnsOrphanedFocusRef.current = isFilesWorkspaceTarget(
      document.activeElement,
      explorerRootRef.current,
    );
    return () => {
      document.removeEventListener("focusin", trackOwner, true);
      document.removeEventListener("pointerdown", trackOwner, true);
    };
  }, []);

  // The canvas owns a React blur handler, but the persistent sidebar (and its
  // mobile Sheet/Portal copy) is rendered outside that subtree.  Observe
  // focusout at the document boundary without consuming it: moving between
  // any Files-marked surface keeps the armed first Escape, while leaving the
  // workspace resets it even when the canvas never receives a blur event.
  useEffect(() => {
    const handleDocumentFocusOut = (event: FocusEvent) => {
      if (!isFilesWorkspaceTarget(event.target, explorerRootRef.current)) return;
      if (event.relatedTarget) {
        if (isFilesWorkspaceTarget(event.relatedTarget, explorerRootRef.current)) {
          return;
        }
        resetEditorEscape();
        return;
      }
      scheduleEditorEscapeFocusExitCheck();
    };
    document.addEventListener("focusout", handleDocumentFocusOut, true);
    return () => document.removeEventListener("focusout", handleDocumentFocusOut, true);
  }, [resetEditorEscape, scheduleEditorEscapeFocusExitCheck]);

  const handleExplorerBlur = useCallback(
    (event: React.FocusEvent<HTMLDivElement>) => {
      const relatedTarget = event.relatedTarget;
      if (relatedTarget) {
        if (isFilesWorkspaceTarget(relatedTarget, event.currentTarget)) {
          return;
        }
        resetEditorEscape();
        return;
      }
      scheduleEditorEscapeFocusExitCheck();
    },
    [resetEditorEscape, scheduleEditorEscapeFocusExitCheck],
  );

  // 内容（意味）検索は User Files / Project Files の中だけで使える。
  const semanticScope = useMemo(
    () =>
      isRemoteWorkspace || isAbsoluteFilerPath
        ? null
        : resolveFilesSemanticScope(currentPath),
    [currentPath, isAbsoluteFilerPath, isRemoteWorkspace],
  );
  const semanticSearch = useFilesSemanticSearch(
    semanticScope,
    fileSearchOpen && fileSearchMode === "semantic",
  );
  const resetSemanticSearch = semanticSearch.reset;

  const resetFileSearchState = useCallback((clearQuery = false) => {
    fileSearchGenerationRef.current += 1;
    if (clearQuery) {
      setFileSearchQuery("");
    }
    setFileSearchActive(false);
    setFileSearchError(null);
    setFileSearchCount(0);
    setFileSearchTruncated(false);
    setFileSearchLoading(false);
    setContentSearchSummary(null);
  }, []);

  const clearFileSearch = useCallback(() => {
    resetFileSearchState(true);
    clearSelection();
    void refresh();
  }, [clearSelection, refresh, resetFileSearchState]);

  const openFileSearch = useCallback(() => {
    setQuickFilterOpen(false);
    setQuickFilterQuery("");
    setFileSearchOpen(true);
    window.requestAnimationFrame(() => {
      fileSearchInputRef.current?.focus();
      fileSearchInputRef.current?.select();
    });
  }, []);

  const closeFileSearch = useCallback(() => {
    setFileSearchOpen(false);
    setFileSearchReplaceQuery("");
    setFileSearchRegex(false);
    resetSemanticSearch();
    clearFileSearch();
    window.requestAnimationFrame(() => {
      explorerRootRef.current?.focus({ preventScroll: true });
    });
  }, [clearFileSearch, resetSemanticSearch]);

  const closeQuickFilter = useCallback(() => {
    setQuickFilterOpen(false);
    setQuickFilterQuery("");
    window.requestAnimationFrame(() => {
      explorerRootRef.current?.focus({ preventScroll: true });
    });
  }, []);

  /**
   * Move from an explorer interaction into the editor as one state
   * transition.  Quick Filter, file search, preview and the context menu are
   * all transient surfaces owned by this workspace; leaving any of them
   * mounted while `editingFile` is set makes the shell quite correctly treat
   * Escape as belonging to that (now hidden) surface forever.  Clear those
   * surfaces before opening the editor, while leaving real dialogs/viewers
   * untouched so their higher Escape priority is not changed.
   */
  const openEditorFromFiler = useCallback(
    (file: { path: string; name: string; type?: string }) => {
      const hadFileSearch =
        fileSearchStateRef.current.open || fileSearchStateRef.current.active;

      setQuickFilterOpen(false);
      setQuickFilterQuery("");
      setFileSearchOpen(false);
      setFileSearchReplaceQuery("");
      setFileSearchRegex(false);
      resetFileSearchState(true);
      resetSemanticSearch();
      if (hadFileSearch) {
        // Search replaces browseData with a result set.  Restore the current
        // directory without the focus side effect of closeFileSearch(), since
        // the next focus owner is the CodeMirror editor.
        clearSelection();
        void refreshRef.current();
      }
      setPreviewFile(null);
      setCtxItem(null);
      setCtxPos(null);

      resetEditorEscape();
      openEditor(file);
    },
    [
      clearSelection,
      openEditor,
      resetEditorEscape,
      resetFileSearchState,
      resetSemanticSearch,
    ],
  );

  // URL ?open=path パラメータでファイルを自動オープン
  useEffect(() => {
    const openPath = searchParams.get("open");
    if (openPath) {
      const name = openPath.split("/").pop() || openPath;
      openEditorFromFiler({ path: openPath, name });
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
  }, [openEditorFromFiler, searchParams]);

  const runFileSearch = useCallback(async (
    override?: { target?: FileSearchTarget },
  ) => {
    const query = fileSearchQuery.trim();
    if (!query) {
      clearFileSearch();
      return;
    }
    const target: FileSearchTarget = isRemoteWorkspace
      ? "name"
      : (override?.target ?? fileSearchTarget);

    const requestGeneration = ++fileSearchGenerationRef.current;
    const requestPath = currentPath;
    const requestTab = filerTab;
    const requestUserId = userId;

    setFileSearchLoading(true);
    setFileSearchError(null);
    setFileSearchActive(false);
    setFileSearchTruncated(false);
    setContentSearchSummary(null);
    try {
      const data = await explorerSearch(query, currentPath, 200, {
        regex: fileSearchRegex,
        mode: target,
      });
      if (
        requestGeneration !== fileSearchGenerationRef.current ||
        requestPath !== currentPath ||
        requestTab !== filerTab ||
        requestUserId !== userId
      ) {
        return;
      }
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
      setFileSearchTruncated(Boolean(data.truncated));
      setContentSearchSummary(
        target === "content"
          ? {
              hits: data.results.map((item) => ({
                path: item.path,
                name: item.name,
                matchCount: item.match_count ?? item.matches?.length ?? 0,
                matches: item.matches ?? [],
              })),
              limitReason: data.limit_reason ?? null,
              skippedLargeFiles: data.skipped_large_files ?? 0,
            }
          : null,
      );
      clearSelection();
    } catch (error) {
      if (requestGeneration === fileSearchGenerationRef.current) {
        setFileSearchError(explorerErrorMessage(error));
      }
    } finally {
      if (requestGeneration === fileSearchGenerationRef.current) {
        setFileSearchLoading(false);
      }
    }
  }, [
    browseData?.can_go_up,
    browseData?.is_admin_mode,
    browseData?.parent_path,
    clearFileSearch,
    clearSelection,
    currentPath,
    fileSearchQuery,
    fileSearchRegex,
    fileSearchTarget,
    filerTab,
    isRemoteWorkspace,
    setBrowseData,
    userId,
  ]);

  const replaceFileSearch = useCallback(async () => {
    const query = fileSearchQuery.trim();
    if (
      !query ||
      !fileSearchActive ||
      fileSearchMode !== "string" ||
      fileSearchTarget !== "name" ||
      !browseData ||
      !capabilities.canRename
    ) {
      return;
    }

    try {
      const items = [...browseData.directories, ...browseData.files]
        .map((item) => ({
          path: item.path,
          currentName: item.name,
          newName: replaceFilerName(
            item.name,
            query,
            fileSearchReplaceQuery,
            fileSearchRegex,
          ),
        }))
        .filter((item) => item.newName !== item.currentName);
      if (items.length === 0) {
        setFileSearchError("置換対象がありません");
        return;
      }

      setFileSearchError(null);
      const result = await renameBatch(items);
      if (result.renamed > 0) {
        await runFileSearch();
        if (fileSearchTruncated) {
          toast.warning("検索結果の上限200件までを置換しました");
        }
      }
    } catch (error) {
      setFileSearchError(
        error instanceof Error ? error.message : "置換に失敗しました",
      );
    }
  }, [
    browseData,
    capabilities.canRename,
    fileSearchActive,
    fileSearchMode,
    fileSearchQuery,
    fileSearchRegex,
    fileSearchReplaceQuery,
    fileSearchTarget,
    fileSearchTruncated,
    renameBatch,
    runFileSearch,
  ]);

  useEffect(() => {
    resetFileSearchState();
    setFileSearchReplaceQuery("");
    setFileSearchRegex(false);
    // 内容検索の結果は「Filesで場所を開く」でフォルダを移動しても残す。
    // タブ・プロジェクト・ユーザーの切替では下の effect と hook 側で破棄する。
    if (fileSearchModeRef.current === "semantic") return;
    setFileSearchOpen(false);
    setFileSearchQuery("");
  }, [currentPath, filerTab, resetFileSearchState]);

  useEffect(() => {
    // タブ・ユーザー切替では内容検索のパネルも閉じる（別タブの結果を残さない）。
    setFileSearchOpen(false);
    setFileSearchQuery("");
    pendingRevealPathRef.current = null;
  }, [filerTab, userId]);

  const switchFileSearchMode = useCallback(
    (mode: FileSearchMode) => {
      if (mode === fileSearchModeRef.current) return;
      if (fileSearchStateRef.current.active) {
        // 文字列検索の結果一覧（browseData の置換）を通常一覧へ戻す。
        resetFileSearchState();
        clearSelection();
        void refresh();
      } else {
        resetFileSearchState();
      }
      resetSemanticSearch();
      setFileSearchMode(mode);
      window.requestAnimationFrame(() => fileSearchInputRef.current?.focus());
    },
    [clearSelection, refresh, resetFileSearchState, resetSemanticSearch],
  );

  const switchFileSearchTarget = useCallback(
    (target: FileSearchTarget) => {
      if (target === fileSearchTarget) return;
      if (fileSearchStateRef.current.active) {
        resetFileSearchState();
        clearSelection();
        void refresh();
      }
      setFileSearchTarget(target);
      window.requestAnimationFrame(() => fileSearchInputRef.current?.focus());
    },
    [clearSelection, fileSearchTarget, refresh, resetFileSearchState],
  );

  const runSemantic = semanticSearch.run;
  const runSemanticSearch = useCallback(() => {
    if (!semanticScope) return;
    void runSemantic(fileSearchQuery, semanticUnderFolder);
  }, [fileSearchQuery, runSemantic, semanticScope, semanticUnderFolder]);

  /** 0件・縮退時の導線: 同じ語で本文の文字列検索を実行する。 */
  const switchToStringSearch = useCallback(() => {
    resetSemanticSearch();
    setFileSearchMode("string");
    setFileSearchTarget("content");
    if (fileSearchQuery.trim()) {
      void runFileSearch({ target: "content" });
    }
    window.requestAnimationFrame(() => fileSearchInputRef.current?.focus());
  }, [fileSearchQuery, resetSemanticSearch, runFileSearch]);

  useEffect(() => {
    resetFileSearchState();
  }, [resetFileSearchState, userId]);

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
        const sourceItem = Array.from(
          document.querySelectorAll<HTMLElement>("[data-explorer-item-path]"),
        ).find((item) => item.dataset.explorerItemPath === file.path);
        if (sourceItem) {
          if (!sourceItem.hasAttribute("tabindex")) sourceItem.tabIndex = -1;
          viewerReturnFocusRef.current = sourceItem;
        } else if (document.activeElement instanceof HTMLElement) {
          viewerReturnFocusRef.current = document.activeElement;
        }
        setViewerFile(file);
      } else if (TEXT_EXTS.has(file.extension || "")) {
        // テキストファイルはエディタで開く
        openEditorFromFiler(file);
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
      openEditorFromFiler,
    ],
  );

  // Shell launchers dispatch into the canonical Files open/download handlers
  // so editor, media viewer, audio player, and download behavior stay aligned
  // with normal Files interactions.  Keep the open listener stable across
  // ordinary browseData or Project/User Files transitions; the hook applies
  // the authenticated and target project boundaries and later open requests.
  const openScopeIdentity = buildExplorerProjectScopeIdentity({
    selectedSpaceId,
    selectedProjectId,
    projectSource: selectedProject?.source,
    currentPath,
  });
  useExplorerOpenPath({
    browseData,
    currentPath,
    authIdentity: userId ?? "",
    scopeIdentity: openScopeIdentity,
    handleFileClick,
    navigate,
  });

  useEffect(() => {
    const onDownload = (event: Event) => {
      const path = (event as CustomEvent<{ path?: unknown }>).detail?.path;
      if (typeof path !== "string" || !path) return;
      void explorerDownloadPaths([path]).catch((error) => {
        toast.error(`ダウンロードに失敗しました: ${explorerErrorMessage(error)}`);
      });
    };
    window.addEventListener(FILES_DOWNLOAD_PATH_EVENT, onDownload);
    return () => {
      window.removeEventListener(FILES_DOWNLOAD_PATH_EVENT, onDownload);
    };
  }, []);

  // F7 / Shift+F7 と Ctrl/Cmd+N / Ctrl/Cmd+Shift+N（作成可能時のみ）
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const creationShortcut = resolveFilerCreationShortcut(e, {
        canCreate: capabilities.canCreate,
        creationDialogOpen: newFolderOpen || newTextFileOpen,
        inputFocused: isTextInput(e.target),
      });
      if (creationShortcut.matched) {
        // Ctrl/Cmd+N はブラウザの新規ウィンドウと競合するため、Files画面
        // 上では権限・フォーカス状態にかかわらず既定動作を抑止する。
        if (creationShortcut.preventDefault) e.preventDefault();
        if (creationShortcut.action === "text-file") {
          setNewTextFileOpen(true);
        } else if (creationShortcut.action === "folder") {
          setNewFolderOpen(true);
        }
        // 権限なし・入力中・作成モーダル表示中はpreventDefaultだけで終了。
        return;
      }
      if (!capabilities.canCreate || newFolderOpen || newTextFileOpen) return;
      // 入力フィールドがフォーカスされている場合は既存のF7操作を無効化。
      if (isTextInput(e.target)) return;
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
  }, [capabilities.canCreate, isTextInput, newFolderOpen, newTextFileOpen]);

  const copySelectedItems = useCallback(
    (operation: "copy" | "cut") => {
      if (selectedRegularPaths.length === 0) return;
      setClipboard({ paths: selectedRegularPaths, operation });
    },
    [selectedRegularPaths, setClipboard],
  );

  const pasteClipboardItems = useCallback(async () => {
    if (!clipboard) return;
    await transfer({
      paths: clipboard.paths,
      destDir: currentPath,
      operation: clipboard.operation === "cut" ? "move" : "copy",
      onTransferred: (transferredPaths) => {
        if (clipboard.operation === "cut") {
          const transferred = new Set(transferredPaths);
          const remainingPaths = clipboard.paths.filter(
            (path) => !transferred.has(path),
          );
          setClipboard(
            remainingPaths.length > 0
              ? { ...clipboard, paths: remainingPaths }
              : null,
          );
        }
        clearSelection();
      },
    });
  }, [clearSelection, clipboard, currentPath, setClipboard, transfer]);

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
    let targetPaths = selectedArchivePaths;
    if (
      targetPaths.length === 0 &&
      activeItem &&
      "type" in activeItem &&
      isSupportedArchiveName(activeItem.name)
    ) {
      targetPaths = [activeItem.path];
    }
    if (!canUseFileShortcuts || (!activeItem && selectedPaths.length === 0)) {
      return;
    }
    if (targetPaths.length === 0) {
      toast.error("対応している圧縮ファイルを選択してください");
      return;
    }
    try {
      const result = await explorerExtract(targetPaths, currentPath);
      clearSelection();
      await refresh();
      toast.success(`${result.extracted.length}件の圧縮ファイルを展開しました`);
    } catch {
      toast.error("圧縮ファイルの展開に失敗しました");
    }
  }, [
    canUseFileShortcuts,
    clearSelection,
    currentPath,
    refresh,
    activeItem,
    selectedPaths.length,
    selectedArchivePaths,
  ]);

  const downloadSelectedItems = useCallback(async () => {
    let targetPaths = selectedRegularPaths;
    if (
      targetPaths.length === 0 &&
      activeItem &&
      (!("type" in activeItem) || !isRecordTableFile(activeItem))
    ) {
      targetPaths = [activeItem.path];
    }
    if (!canUseDownloadShortcut || targetPaths.length === 0) return;
    try {
      await explorerDownloadPaths(targetPaths);
      toast.success(
        targetPaths.length === 1
          ? "ダウンロードを開始しました"
          : `${targetPaths.length}件をダウンロードします`,
      );
    } catch (error) {
      toast.error(`ダウンロードに失敗しました: ${explorerErrorMessage(error)}`);
    }
  }, [activeItem, canUseDownloadShortcut, selectedRegularPaths]);

  const deleteSelectedItems = useCallback(async () => {
    if (selectedPaths.length === 0) return;
    const targets = selectedPaths
      .map((path) => itemByPath.get(path))
      .filter((item): item is ExplorerItem => !!item)
      .map(toFilerDeleteTarget);
    await deleteTargets(targets, { onDeleted: clearSelection });
  }, [clearSelection, deleteTargets, itemByPath, selectedPaths]);

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

  const scrollRenderedItemIntoView = useCallback((path: string) => {
    window.requestAnimationFrame(() => {
      const element = Array.from(
        explorerRootRef.current?.querySelectorAll<HTMLElement>(
          "[data-explorer-item-path]",
        ) ?? [],
      ).find((item) => item.dataset.explorerItemPath === path);
      element?.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  }, []);

  const extendSelectionToPath = useCallback(
    (nextPath: string, itemPaths: string[]) => {
      activePathRef.current = nextPath;
      selectRange(nextPath, itemPaths);
      scrollRenderedItemIntoView(nextPath);
    },
    [scrollRenderedItemIntoView, selectRange],
  );

  const extendSelectionByOffset = useCallback(
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
      if (nextPath) extendSelectionToPath(nextPath, itemPaths);
    },
    [activePath, extendSelectionToPath, getRenderedItemPaths],
  );

  const extendSelectionToIndex = useCallback(
    (index: number) => {
      const itemPaths = getRenderedItemPaths();
      if (itemPaths.length === 0) return;
      const nextIndex = Math.max(0, Math.min(itemPaths.length - 1, index));
      const nextPath = itemPaths[nextIndex];
      if (nextPath) extendSelectionToPath(nextPath, itemPaths);
    },
    [extendSelectionToPath, getRenderedItemPaths],
  );

  const orderedSelectionForClipboard = useCallback(() => {
    const source =
      selectedPaths.length > 0
        ? selectedPaths
        : activePath
          ? [activePath]
          : [];
    if (source.length === 0) return [];
    const set = new Set(source);
    const ordered = getRenderedItemPaths().filter((path) => set.has(path));
    return ordered.length > 0 ? ordered : source;
  }, [activePath, getRenderedItemPaths, selectedPaths]);

  const copySelectedPathsText = useCallback(async () => {
    const paths = orderedSelectionForClipboard();
    if (paths.length === 0) return;
    try {
      await navigator.clipboard.writeText(paths.join("\n"));
      toast.success(
        paths.length > 1
          ? `${paths.length}件のパスをコピーしました`
          : "パスをコピーしました",
      );
    } catch {
      toast.error("クリップボードへのコピーに失敗しました");
    }
  }, [orderedSelectionForClipboard]);

  const copySelectedNamesText = useCallback(async () => {
    const paths = orderedSelectionForClipboard();
    if (paths.length === 0) return;
    const names = paths
      .map((path) => itemByPath.get(path)?.name)
      .filter((name): name is string => !!name);
    if (names.length === 0) return;
    try {
      await navigator.clipboard.writeText(names.join("\n"));
      toast.success(
        names.length > 1
          ? `${names.length}件のファイル名をコピーしました`
          : "ファイル名をコピーしました",
      );
    } catch {
      toast.error("クリップボードへのコピーに失敗しました");
    }
  }, [itemByPath, orderedSelectionForClipboard]);

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

  const incrementalSearch = useIncrementalSearch({
    // ディレクトリ移動・タブ切替・再帰検索結果表示の切替で以前の検索条件を失効させる。
    getContextKey: () =>
      `${filerTab}|${currentPath}|${fileSearchActive ? "search" : "list"}`,
    getItems: getRenderedSearchItems,
    getActiveKey: () => activePathRef.current,
    focusMatch: (item) => focusRenderedItemPath(item.path),
  });

  useEffect(() => {
    activePathRef.current = activePath;
  }, [activePath]);

  useEffect(() => {
    const restore = historyScrollRestore;
    if (!restore) {
      consumedHistoryScrollRestoreRef.current = null;
      return;
    }
    // The provider only emits this signal for a successful history restore.
    // Keep the page-side DOM work tied to that request's directory and virtual
    // focus; a late signal from another navigation must be ignored.
    if (
      loading ||
      currentPath !== restore.directoryPath ||
      focusedItemPath !== restore.focusedItemPath
    ) {
      return;
    }
    const identity = `${restore.navigationEpoch}\u0000${restore.directoryPath}\u0000${restore.focusedItemPath}`;
    if (consumedHistoryScrollRestoreRef.current === identity) return;
    consumedHistoryScrollRestoreRef.current = identity;

    const frame = window.requestAnimationFrame(() => {
      scrollExplorerItemIntoView(
        explorerRootRef.current,
        restore.focusedItemPath,
      );
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    currentPath,
    focusedItemPath,
    historyScrollRestore,
    loading,
  ]);

  useEffect(() => {
    if (
      loading ||
      isExplorerInteractionBlocked ||
      activePath ||
      historyScrollRestore
    ) {
      return;
    }
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
    historyScrollRestore,
    isExplorerInteractionBlocked,
    loading,
    visibleItems,
  ]);

  // 「Filesで場所を開く」: 移動先フォルダの読み込み完了後に対象ファイルを選択する。
  // 先頭項目の自動フォーカス（上の effect）より後に登録し、同じフレームで上書きする。
  useEffect(() => {
    const pending = pendingRevealPathRef.current;
    if (!pending || loading || !browseData) return;
    if (normalizeFilerPath(currentPath) !== filerParentPath(pending)) return;
    pendingRevealPathRef.current = null;
    const target = Array.from(itemByPath.keys()).find(
      (path) => normalizeFilerPath(path) === pending,
    );
    if (!target) {
      toast.error("ファイルが見つかりません。移動・削除された可能性があります");
      return;
    }
    const frame = window.requestAnimationFrame(() => focusRenderedItemPath(target));
    return () => window.cancelAnimationFrame(frame);
  }, [browseData, currentPath, focusRenderedItemPath, itemByPath, loading]);

  const openFilesLocation = useCallback(
    (filePath: string) => {
      const normalized = normalizeFilerPath(filePath);
      const folder = filerParentPath(normalized);
      setQuickFilterOpen(false);
      setQuickFilterQuery("");
      pendingRevealPathRef.current = normalized;
      if (folder === normalizeFilerPath(currentPath)) {
        if (!fileSearchActive) {
          const target = Array.from(itemByPath.keys()).find(
            (path) => normalizeFilerPath(path) === normalized,
          );
          if (target) {
            pendingRevealPathRef.current = null;
            focusRenderedItemPath(target);
            return;
          }
        } else {
          // 文字列検索の結果一覧を表示中なら、通常一覧へ戻してから選択する。
          resetFileSearchState();
        }
        void refresh();
        return;
      }
      navigate(folder);
    },
    [
      currentPath,
      fileSearchActive,
      focusRenderedItemPath,
      itemByPath,
      navigate,
      refresh,
      resetFileSearchState,
    ],
  );

  const openSemanticResultLocation = useCallback(
    (result: FilesSemanticResult) => openFilesLocation(result.filePath),
    [openFilesLocation],
  );

  useLayoutEffect(() => {
    incrementalSearch.reset();
    if (loading) return;

    const root = explorerRootRef.current;
    const activeElement = document.activeElement;
    const ownsKeyboardFocus =
      !activeElement ||
      activeElement === document.body ||
      !!root?.contains(activeElement);
    if (!ownsKeyboardFocus || isTextInput(activeElement)) return;
    root?.focus({ preventScroll: true });
  }, [currentPath, filerTab, incrementalSearch, isTextInput, loading]);

  useEffect(() => {
    setQuickFilterOpen(false);
    setQuickFilterQuery("");
  }, [currentPath, filerTab]);

  useEffect(() => {
    if (!quickFilterOpen) return;
    const frame = window.requestAnimationFrame(() => {
      quickFilterInputRef.current?.focus();
      quickFilterInputRef.current?.select();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [quickFilterOpen, viewMode]);

  useEffect(() => {
    if (!fileSearchOpen) return;
    const frame = window.requestAnimationFrame(() => {
      fileSearchInputRef.current?.focus();
      fileSearchInputRef.current?.select();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [fileSearchOpen, viewMode]);

  useLayoutEffect(() => {
    if (loading) return;
    const pendingInput = pendingIncrementalSearchRef.current;
    pendingIncrementalSearchRef.current = "";
    if (!pendingInput) return;

    const root = explorerRootRef.current;
    const activeElement = document.activeElement;
    if (
      (activeElement &&
        activeElement !== document.body &&
        !root?.contains(activeElement)) ||
      isTextInput(activeElement)
    ) {
      return;
    }
    incrementalSearch.handleCharacter(pendingInput);
  }, [
    currentPath,
    filerTab,
    incrementalSearch,
    isTextInput,
    loading,
  ]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const activeElement = document.activeElement;
      if (
        activeElement &&
        activeElement !== document.body &&
        !explorerRootRef.current?.contains(activeElement)
      ) {
        return;
      }
      const key = e.key.toLowerCase();
      const primaryModifier = e.ctrlKey || e.metaKey;
      const isQuickFilterShortcut =
        primaryModifier && !e.altKey && !e.shiftKey && key === "s";
      const isFileSearchShortcut =
        (!primaryModifier &&
          !e.altKey &&
          !e.shiftKey &&
          e.key === "F3") ||
        (primaryModifier && !e.altKey && !e.shiftKey && key === "f");
      // エディタ表示中の Ctrl+S は DocumentEditor の保存へ譲る。
      if (editingFile && isQuickFilterShortcut) return;
      if (isFileSearchShortcut) {
        if (!canUseExplorerSearch || isExplorerInteractionBlocked) return;
        e.preventDefault();
        openFileSearch();
        return;
      }
      // 検索パネル内のボタン・チェックボックス・結果リストのキー操作は
      // そのコントロールに任せる（一覧の Enter/Space/単発キー操作へ流さない）。
      if (
        e.target instanceof Element &&
        e.target.closest("[data-filer-search-panel]")
      ) {
        return;
      }
      if (isQuickFilterShortcut && quickFilterOpen) {
        e.preventDefault();
        closeQuickFilter();
        return;
      }
      if (
        loading &&
        (activeElement === document.body ||
          activeElement === explorerRootRef.current) &&
        !isTextInput(e.target) &&
        !primaryModifier &&
        !e.altKey &&
        e.key.length === 1 &&
        e.key !== ":" &&
        e.key !== ";" &&
        !e.isComposing
      ) {
        e.preventDefault();
        pendingIncrementalSearchRef.current += e.key;
        return;
      }

      // History navigation is intentionally allowed while a directory GET is
      // in flight.  ExplorerProvider queues the newer request and binds it to
      // its navigation epoch; dropping Backspace/Alt+Arrow here would lose a
      // user's rapid A→B→Back intent.  Other transient surfaces (editor,
      // viewer, dialogs, context menu, and text inputs) still own the key.
      const isHistoryShortcut =
        (!primaryModifier &&
          !e.altKey &&
          !e.shiftKey &&
          e.key === "Backspace") ||
        (!primaryModifier &&
          e.altKey &&
          !e.shiftKey &&
          (e.key === "ArrowLeft" ||
            e.key === "ArrowRight" ||
            e.key === "Backspace"));
      if (isHistoryShortcut) {
        const historyNavigationBlocked =
          !!editingFile ||
          !!recordTableFile ||
          !!viewerFile ||
          !!previewFile ||
          !!renameTarget ||
          newFolderOpen ||
          newTextFileOpen ||
          !!ctxPos;
        if (historyNavigationBlocked || isTextInput(e.target)) return;
        e.preventDefault();
        if (e.altKey && e.key === "ArrowRight") goForward();
        else goBack();
        return;
      }

      if (isExplorerInteractionBlocked || isTextInput(e.target)) return;
      // Files source tabs: Ctrl+← / Ctrl+→.
      // Alt+←/→ は履歴移動、修飾なし←/→はファイルフォーカス移動のまま維持する。
      if (
        e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        !e.shiftKey &&
        (e.key === "ArrowLeft" || e.key === "ArrowRight")
      ) {
        e.preventDefault();
        cycleFilerTab(e.key === "ArrowRight" ? 1 : -1);
        return;
      }
      // Ctrl+J: 直近に成立した検索条件で次の一致へ進む。1秒の入力継続タイムアウトとは
      // 独立しており、検索条件が無い場合は preventDefault せずグローバルの Ctrl+J
      // （チャット入力欄フォーカス）へ譲る。
      if (
        primaryModifier &&
        !e.altKey &&
        !e.shiftKey &&
        key === "j" &&
        !isBookmarkQuickLauncherOpen() &&
        incrementalSearch.hasActiveSearch()
      ) {
        e.preventDefault();
        incrementalSearch.focusNextMatch();
        return;
      }
      if (isQuickFilterShortcut) {
        e.preventDefault();
        if (fileSearchOpen) closeFileSearch();
        setQuickFilterOpen(true);
        window.requestAnimationFrame(() => {
          quickFilterInputRef.current?.focus();
          quickFilterInputRef.current?.select();
        });
        return;
      }
      const activePathForEvent =
        activePathRef.current ??
        activePath ??
        getRenderedItemPaths()[0] ??
        null;
      const activeItemForEvent = activePathForEvent
        ? (itemByPath.get(activePathForEvent) ?? null)
        : null;

      // 表示切替（`:` サムネイル / `;` リスト）。
      // インクリメンタル検索が単一文字キーを消費する前に処理する。
      // `:` は US 配列では Shift+`;` のため shiftKey は条件に含めない。
      if (!primaryModifier && !e.altKey && !e.isComposing) {
        if (e.key === ":") {
          e.preventDefault();
          setViewMode("grid");
          return;
        }
        if (e.key === ";") {
          e.preventDefault();
          setViewMode("list");
          return;
        }
      }

      // ソート切替（F8 名前昇順 / F9 更新日時降順）。
      if (!primaryModifier && !e.altKey && !e.shiftKey) {
        if (e.key === "F8") {
          e.preventDefault();
          setSort("name", "asc");
          return;
        }
        if (e.key === "F9") {
          e.preventDefault();
          setSort("date", "desc");
          return;
        }
      }

      // 現在のファイラータブのホームへ戻る。
      if (
        e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        !e.shiftKey &&
        key === "h"
      ) {
        e.preventDefault();
        homeNavigate();
        return;
      }

      if (!primaryModifier && e.altKey && !e.shiftKey) {
        if (e.key === "ArrowUp") {
          e.preventDefault();
          goUp();
          return;
        }
      }

      // Undo / Redo。スタックが空の時は preventDefault せず、
      // レイアウト側の TaskCompletionUndoProvider（Ctrl+Z）へ譲る。
      if (primaryModifier && !e.altKey && !e.shiftKey && key === "z") {
        if (e.defaultPrevented || !canUndoFiler()) return;
        e.preventDefault();
        void undo();
        return;
      }
      if (
        primaryModifier &&
        !e.altKey &&
        ((!e.shiftKey && key === "y") || (e.shiftKey && key === "z"))
      ) {
        if (e.defaultPrevented || !canRedoFiler()) return;
        e.preventDefault();
        void redo();
        return;
      }
      if (primaryModifier && key === "a") {
        e.preventDefault();
        selectAll(visibleItems.map((item) => item.path));
        return;
      }
      if (primaryModifier && key === "c") {
        e.preventDefault();
        if (capabilities.canCopy) copySelectedItems("copy");
        return;
      }
      if (primaryModifier && key === "x") {
        e.preventDefault();
        if (capabilities.canMove) copySelectedItems("cut");
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
      if (primaryModifier && !e.altKey && e.shiftKey && key === "l") {
        if (canUseDownloadShortcut) {
          e.preventDefault();
          void downloadSelectedItems();
        }
        return;
      }
      if (primaryModifier && !e.altKey && !e.shiftKey && key === "p") {
        e.preventDefault();
        void copySelectedPathsText();
        return;
      }
      if (primaryModifier && !e.altKey && e.shiftKey && key === "p") {
        e.preventDefault();
        void copySelectedNamesText();
        return;
      }
      if (
        primaryModifier &&
        !e.altKey &&
        !e.shiftKey &&
        !e.isComposing &&
        (e.code === "Space" || e.key === " ")
      ) {
        e.preventDefault();
        if (activePathForEvent) toggleSelect(activePathForEvent);
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
      if (
        !e.altKey &&
        e.shiftKey &&
        (!primaryModifier || e.key === "Home" || e.key === "End")
      ) {
        let offset = 0;
        if (e.key === "ArrowUp") offset = -1;
        if (e.key === "ArrowDown") offset = 1;
        if (e.key === "PageUp") offset = -getVisibleItemPageSize();
        if (e.key === "PageDown") offset = getVisibleItemPageSize();
        if (offset !== 0) {
          e.preventDefault();
          extendSelectionByOffset(offset);
          return;
        }
        if (e.key === "Home") {
          e.preventDefault();
          extendSelectionToIndex(0);
          return;
        }
        if (e.key === "End") {
          e.preventDefault();
          extendSelectionToIndex(Number.MAX_SAFE_INTEGER);
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
        if (capabilities.canRename && activeItemForEvent) {
          e.preventDefault();
          setRenameTarget(activeItemForEvent);
        }
        return;
      }
      if (
        capabilities.canDelete &&
        selectedPaths.length > 0 &&
        !primaryModifier &&
        !e.altKey &&
        !e.shiftKey &&
        e.key === "Delete"
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
        if (isBookmarkQuickLauncherOpen()) return;
        e.preventDefault();
        incrementalSearch.handleCharacter(e.key);
      }
    };
    // capture フェーズで登録し、レイアウト側の TaskCompletionUndoProvider（Ctrl+Z）
    // より先に評価されるようにする。スタックが空なら preventDefault せず譲る。
    window.addEventListener("keydown", handleKeyDown, true);
    return () => window.removeEventListener("keydown", handleKeyDown, true);
  }, [
    canUseExplorerSearch,
    canUseDownloadShortcut,
    capabilities,
    archiveSelectedItems,
    copySelectedItems,
    deleteSelectedItems,
    downloadSelectedItems,
    editingFile,
    extractSelectedItems,
    filerTab,
    redo,
    undo,
    activePath,
    focusItemByIndex,
    focusItemByOffset,
    getVisibleItemPageSize,
    getRenderedItemPaths,
    goBack,
    goForward,
    goUp,
    homeNavigate,
    incrementalSearch,
    isTextInput,
    isExplorerInteractionBlocked,
    itemByPath,
    loading,
    openExplorerItem,
    pasteClipboardItems,
    selectAll,
    selectedPaths.length,
    setSort,
    setViewMode,
    toggleSelect,
    visibleItems,
    copySelectedPathsText,
    copySelectedNamesText,
    extendSelectionByOffset,
    extendSelectionToIndex,
    closeFileSearch,
    closeQuickFilter,
    cycleFilerTab,
    ctxPos,
    fileSearchOpen,
    newFolderOpen,
    newTextFileOpen,
    openFileSearch,
    previewFile,
    quickFilterOpen,
    recordTableFile,
    renameTarget,
    viewerFile,
  ]);

  const isSemanticMode = fileSearchMode === "semantic";
  const effectiveSearchTarget: FileSearchTarget = isRemoteWorkspace
    ? "name"
    : fileSearchTarget;
  const fileSearchInputLabel = isSemanticMode
    ? "内容（意味）で検索"
    : effectiveSearchTarget === "content"
      ? "本文の文字列検索"
      : "ファイル名・フォルダ名の検索";
  const fileSearchPlaceholder = isSemanticMode
    ? "探したい内容を言葉で（例: 昨年の見積もり条件の説明）"
    : effectiveSearchTarget === "content"
      ? "本文に含まれる文字列を検索..."
      : "ファイル名・フォルダ名を検索...";
  const semanticFolderLabel = semanticScope
    ? `${filesScopeLabel(semanticScope)}${
        semanticScope.relativeFolder ? ` / ${semanticScope.relativeFolder}` : ""
      }`
    : "";
  const segmentClass = (active: boolean) =>
    cn(
      "h-6 rounded px-2 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
      active
        ? "bg-background font-medium text-foreground shadow-sm"
        : "text-muted-foreground hover:text-foreground",
    );

  const fileSearchControl = fileSearchOpen ? (
    <div
      className="flex w-full max-w-2xl flex-col gap-1 rounded-md bg-muted/50 px-2 py-1.5"
      data-testid="filer-search-panel"
      data-filer-search-panel=""
      data-search-mode={fileSearchMode}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || event.defaultPrevented) return;
        event.preventDefault();
        event.stopPropagation();
        closeFileSearch();
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <div
          role="radiogroup"
          aria-label="検索モード"
          className="inline-flex shrink-0 items-center gap-0.5 rounded-md bg-muted p-0.5"
        >
          <button
            type="button"
            role="radio"
            aria-checked={!isSemanticMode}
            className={segmentClass(!isSemanticMode)}
            onClick={() => switchFileSearchMode("string")}
            title="ファイル名・フォルダ名、またはテキストの本文に含まれる文字列で探します"
          >
            名前・本文の文字列
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={isSemanticMode}
            className={segmentClass(isSemanticMode)}
            onClick={() => switchFileSearchMode("semantic")}
            disabled={!semanticScope}
            title={
              semanticScope
                ? "言葉の意味が近い箇所を、索引済みの文書・PDF・音声・動画から探します"
                : "User Files / Project Files の中で利用できます"
            }
          >
            内容（意味）
          </button>
        </div>
        {!isSemanticMode && (
          <div
            role="radiogroup"
            aria-label="文字列検索の対象"
            className="inline-flex shrink-0 items-center gap-0.5 rounded-md bg-muted p-0.5"
          >
            <button
              type="button"
              role="radio"
              aria-checked={effectiveSearchTarget === "name"}
              className={segmentClass(effectiveSearchTarget === "name")}
              onClick={() => switchFileSearchTarget("name")}
            >
              名前
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={effectiveSearchTarget === "content"}
              className={segmentClass(effectiveSearchTarget === "content")}
              onClick={() => switchFileSearchTarget("content")}
              disabled={isRemoteWorkspace}
              title={
                isRemoteWorkspace
                  ? "リモートのファイルでは本文検索を利用できません"
                  : "テキストファイルの本文を検索します（Office/PDFは内容検索を使用）"
              }
            >
              本文
            </button>
          </div>
        )}
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          className="ml-auto"
          onClick={closeFileSearch}
          aria-label="ファイル検索を閉じる"
          title="検索を閉じる (Esc)"
        >
          <X className="size-3.5" />
        </Button>
      </div>
      <div className="flex items-center gap-2">
        <Search className="size-4 shrink-0 text-muted-foreground" />
        <Input
          ref={fileSearchInputRef}
          value={fileSearchQuery}
          onChange={(event) => setFileSearchQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              closeFileSearch();
              return;
            }
            if (event.key === "Enter" && !event.nativeEvent.isComposing) {
              event.preventDefault();
              if (isSemanticMode) runSemanticSearch();
              else void runFileSearch();
            }
          }}
          placeholder={fileSearchPlaceholder}
          aria-label={fileSearchInputLabel}
          disabled={fileSearchLoading}
          className="h-7 min-w-0 flex-1 sm:min-w-40"
        />
        {!isSemanticMode && (
          <label
            htmlFor="filer-search-regex"
            className="flex shrink-0 cursor-pointer items-center gap-1 text-[11px] text-muted-foreground"
          >
            <Checkbox
              id="filer-search-regex"
              checked={fileSearchRegex}
              onCheckedChange={(checked) => setFileSearchRegex(checked === true)}
              disabled={isRemoteWorkspace || fileSearchLoading}
            />
            正規表現
          </label>
        )}
        {isSemanticMode ? (
          <Button
            type="button"
            size="xs"
            variant="outline"
            onClick={runSemanticSearch}
            disabled={!fileSearchQuery.trim() || semanticSearch.search.phase === "loading"}
          >
            検索
          </Button>
        ) : fileSearchLoading ? (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            検索中...
          </span>
        ) : fileSearchActive ? (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            {fileSearchCount}
            {fileSearchTruncated ? "+" : ""}件
          </span>
        ) : null}
      </div>
      {!isSemanticMode && effectiveSearchTarget === "name" && (
        <div className="flex items-center gap-2 pl-6">
          <Input
            ref={fileSearchReplaceInputRef}
            value={fileSearchReplaceQuery}
            onChange={(event) => setFileSearchReplaceQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                closeFileSearch();
                return;
              }
              if (event.key === "Enter") {
                event.preventDefault();
                void replaceFileSearch();
              }
            }}
            placeholder="置換後の文字列..."
            aria-label="置換後の文字列"
            className="h-7 min-w-0 flex-1 sm:min-w-40"
          />
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void replaceFileSearch()}
            disabled={
              fileSearchLoading ||
              !fileSearchActive ||
              !fileSearchQuery.trim() ||
              !capabilities.canRename
            }
          >
            すべて置換
          </Button>
        </div>
      )}
      {!isSemanticMode && contentSearchSummary && (
        <div className="flex flex-col gap-1 pl-6" data-testid="filer-content-search-matches">
          {contentSearchSummary.limitReason && (
            <span className="text-[11px] text-amber-600">
              {CONTENT_LIMIT_LABELS[contentSearchSummary.limitReason] ??
                "上限に達したため一部のみ表示しています"}
            </span>
          )}
          {contentSearchSummary.skippedLargeFiles > 0 && (
            <span className="text-[11px] text-muted-foreground">
              2MBを超える {contentSearchSummary.skippedLargeFiles}件のテキストは本文検索の対象外です
            </span>
          )}
          {contentSearchSummary.hits.length > 0 && (
            <ul className="max-h-40 overflow-y-auto rounded border border-border bg-background text-left text-xs">
              {contentSearchSummary.hits.map((hit) => (
                <li key={hit.path} className="border-b border-border/60 last:border-b-0">
                  <button
                    type="button"
                    className="flex w-full flex-col gap-0.5 px-2 py-1 text-left hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
                    onClick={() => focusRenderedItemPath(hit.path)}
                    title={hit.path}
                  >
                    <span className="flex items-center gap-2">
                      <span className="truncate font-medium">{hit.name}</span>
                      <span className="shrink-0 text-[10px] text-muted-foreground">
                        {hit.matchCount}箇所
                      </span>
                    </span>
                    {hit.matches.map((match) => (
                      <span
                        key={match.line}
                        className="truncate font-mono text-[11px] text-muted-foreground"
                      >
                        {match.line}: {match.text}
                      </span>
                    ))}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {isSemanticMode && semanticScope && (
        <div className="flex flex-col gap-1.5 pl-6">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            <span className="truncate" title={semanticFolderLabel}>
              対象: <span className="text-foreground">{semanticFolderLabel}</span>
            </span>
            <label
              htmlFor="filer-semantic-under-folder"
              className="flex shrink-0 cursor-pointer items-center gap-1"
            >
              <Checkbox
                id="filer-semantic-under-folder"
                checked={semanticUnderFolder && Boolean(semanticScope.relativeFolder)}
                disabled={!semanticScope.relativeFolder}
                onCheckedChange={(checked) => setSemanticUnderFolder(checked === true)}
              />
              このフォルダ配下のみ
            </label>
          </div>
          <FilesIndexStatusBar
            status={semanticSearch.status}
            error={semanticSearch.statusError}
            loading={semanticSearch.statusLoading}
            onRefresh={() => void semanticSearch.refreshStatus()}
            onOpenDetails={() => setFilesIndexDialogOpen(true)}
          />
          <FilesSemanticResults
            controller={semanticSearch}
            scope={semanticScope}
            onOpenLocation={openSemanticResultLocation}
            onSwitchToString={switchToStringSearch}
          />
        </div>
      )}
      {fileSearchError && (
        <span className="pl-6 text-xs text-destructive">{fileSearchError}</span>
      )}
    </div>
  ) : null;

  const quickFilterControl = quickFilterOpen ? (
    <div className="flex items-center gap-2 rounded-md bg-muted/50 px-2 py-1">
      <Search className="size-4 shrink-0 text-muted-foreground" />
      <Input
        ref={quickFilterInputRef}
        value={quickFilterQuery}
        onChange={(event) => setQuickFilterQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== "Escape") return;
          event.preventDefault();
          event.stopPropagation();
          closeQuickFilter();
        }}
        placeholder="ファイル名で絞り込み..."
        aria-label="ファイル名の即席フィルター"
        className="h-7"
      />
      <span className="shrink-0 text-[11px] text-muted-foreground">
        {visibleItems.length}/{browseData
          ? browseData.directories.length + browseData.files.length
          : 0}
        件
      </span>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        onClick={closeQuickFilter}
        aria-label="即席フィルターを閉じる"
        title="フィルターを閉じる (Esc)"
      >
        <X className="size-3.5" />
      </Button>
    </div>
  ) : null;

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
  }, [currentPath, contextRootPath, isAbsoluteFilerPath]);

  return (
    <div
      ref={explorerRootRef}
      className="flex min-h-0 min-w-0 flex-1 flex-row overflow-hidden"
      data-shell-workspace="files"
      data-shell-region="files-canvas"
      tabIndex={-1}
      onKeyDownCapture={handleEditorKeyDownCapture}
      onBlur={handleExplorerBlur}
    >
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
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
          onEditStateChange={handleEditorEditStateChange}
        />
      ) : (
        <UploadZone disabled={!isDefaultStorage} onContextMenu={isDefaultStorage ? handleBackgroundContextMenu : undefined}>
          <div
            ref={explorerScrollRef}
            data-files-scroll-region="browser"
            className="flex h-full min-h-0 flex-col overflow-auto bg-background"
          >
            {/* ヘッダー */}
            <div className="flex min-h-14 shrink-0 items-center justify-between gap-4 border-b border-border bg-card/40 px-6">
              <div className="flex min-w-0 items-center gap-3">
                <h1 className="text-[16px] font-semibold">Files</h1>
                {isRemoteWorkspace && <span className="rounded border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 text-[10px] font-medium text-amber-300">Read-only</span>}
              </div>
              {isDefaultStorage ? (
                <ExplorerToolbar
                  onNewFolder={() => setNewFolderOpen(true)}
                  onAddBookmark={isRemoteWorkspace
                    ? undefined
                    : () => requestFilesBookmark(activePath ?? currentPath, activeItem?.name)}
                  onDownloadSelection={() => void downloadSelectedItems()}
                  hasDownloadSelection={
                    canUseDownloadShortcut && selectedRegularPaths.length > 0
                  }
                />
              ) : null}
            </div>

            {/* Files tabs: project files / user files */}
            <div className="flex shrink-0 items-center gap-3 border-b border-border px-3 pt-1 sm:px-6" data-files-toolbar="source-storage">
              <div className="flex min-w-0 flex-1 items-center gap-5 overflow-x-auto">
                <Button
                  variant={isDefaultStorage && filerTab === "workspace" && !isAbsoluteFilerPath ? "default" : "outline"}
                  size="sm"
                  className={cn("h-9 rounded-none border-0 border-b-2 border-transparent bg-transparent px-0 text-xs text-muted-foreground hover:bg-transparent hover:text-foreground", isDefaultStorage && filerTab === "workspace" && !isAbsoluteFilerPath && "border-primary text-primary")}
                  onClick={() => {selectDefaultStorage(); setFilerTab("workspace");}}
                >
                  {FILER_TAB_LABELS.workspace}
                </Button>
                <Button
                  variant={isDefaultStorage && filerTab === "user" && !isAbsoluteFilerPath ? "default" : "outline"}
                  size="sm"
                  className={cn("h-9 rounded-none border-0 border-b-2 border-transparent bg-transparent px-0 text-xs text-muted-foreground hover:bg-transparent hover:text-foreground", isDefaultStorage && filerTab === "user" && !isAbsoluteFilerPath && "border-primary text-primary")}
                  onClick={() => {selectDefaultStorage(); setFilerTab("user");}}
                >
                  {FILER_TAB_LABELS.user}
                </Button>
              </div>
              <div className="ml-auto flex min-w-0 shrink-0 items-center gap-3 pb-1">
                {isDefaultStorage && selectedPaths.length > 0 && (
                  <span className="hidden shrink-0 text-xs font-medium text-primary xl:inline" aria-live="polite">
                    {selectedPaths.length} item{selectedPaths.length === 1 ? "" : "s"} selected
                  </span>
                )}
                <StorageControls />
              </div>
            </div>

            <StorageWorkspacePanel />
            {isDefaultStorage && <>
            {/* ナビゲーション（ホーム・上位移動・パンくず） */}
            <div className="flex shrink-0 items-center gap-2 border-b border-border px-6 py-1 text-sm">
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={homeNavigate}
                  title="ホーム"
                  aria-label="ホーム"
                >
                  <Home className="size-3" />
                </Button>
                {!loading &&
                  browseData?.can_go_up &&
                  browseData.parent_path !== null && (
                    <button
                      className="flex h-6 shrink-0 items-center gap-1 rounded px-2 text-xs text-muted-foreground transition-colors hover:bg-muted"
                      onClick={goUp}
                    >
                      <ArrowUp className="size-3.5" />
                      <span>上のフォルダへ</span>
                    </button>
                  )}
                <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto whitespace-nowrap">
                  {breadcrumbs.slice(-4).map((segment, i, arr) => {
                    const fullIndex = breadcrumbs.length - arr.length + i;
                    let partialPath: string;
                    if (isAbsoluteFilerPath) {
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
                      <span key={fullIndex} className="flex shrink-0 items-center gap-1">
                        <ChevronRight className="size-3 text-muted-foreground" />
                        {isLast ? (
                          <span className="text-xs font-medium text-foreground">{segment}</span>
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
              </div>

            {/* ローディング */}
            {loading && (
              <div className="grid grid-cols-2 gap-3 p-6 sm:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
                {Array.from({ length: 12 }).map((_, i) => (
                  <Skeleton key={i} className="h-32 rounded-md bg-muted/40" />
                ))}
              </div>
            )}

            {/* エラー */}
            {error && !loading && (
                <div className="mx-6 my-4 rounded border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {error}
              </div>
            )}

            {/* ファイル一覧: Workspace/User すべて同じ FileGrid/FileList で描画 */}
            {!loading &&
              browseData &&
              (viewMode === "grid" ? (
                <FileGrid
                  onFileClick={handleFileClick}
                  onContextMenu={handleContextMenu}
                  directories={visibleDirectories}
                  files={visibleFiles}
                  headerAddon={fileSearchControl ?? quickFilterControl}
                />
              ) : (
                <FileList
                  onFileClick={handleFileClick}
                  onContextMenu={handleContextMenu}
                  directories={visibleDirectories}
                  files={visibleFiles}
                  headerAddon={fileSearchControl ?? quickFilterControl}
                />
              ))}

            {/* 空フォルダ */}
            {!loading &&
              browseData &&
              visibleDirectories.length === 0 &&
              visibleFiles.length === 0 && (
                <div className="mx-6 my-8 flex max-w-lg flex-col items-center overflow-hidden rounded-md border border-border bg-card/40 text-center text-sm text-muted-foreground">
                  <img
                    src={withBasePath("/images/ui/empty-files.png")}
                    alt=""
                    className="aspect-[16/7] w-full object-cover"
                  />
                  <div className="px-6 py-5">
                    {quickFilterOpen && normalizedQuickFilter
                      ? "一致する項目はありません。"
                      : fileSearchActive
                      ? "検索結果はありません。"
                      : "このフォルダは空です。ファイルをドラッグ&ドロップでアップロードできます。"}
                  </div>
                </div>
              )}
            </>}
          </div>
        </UploadZone>
      )}
      </div>

      {/* コンテキストメニュー・ダイアログ: ファイラー・エディタ画面共通 */}
      <FileContextMenu
        item={ctxItem}
        position={ctxPos}
        onClose={() => {
          setCtxItem(null);
          setCtxPos(null);
          // The menu unmounts with focus on <body>.  Return focus to Files
          // only when Files still owns it (Esc / menu item); dismissing the
          // menu by pressing outside Files must not pull focus back in.
          if (contextMenuFocusFrameRef.current !== null) {
            window.cancelAnimationFrame(contextMenuFocusFrameRef.current);
          }
          contextMenuFocusFrameRef.current = window.requestAnimationFrame(() => {
            contextMenuFocusFrameRef.current = null;
            if (!filesOwnsOrphanedFocusRef.current) return;
            const active = document.activeElement;
            if (!active || active === document.body) {
              explorerRootRef.current?.focus({ preventScroll: true });
            }
          });
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
        activeSelectionPaths={selectedPaths}
      />
      {semanticScope && (
        <FilesIndexDialog
          open={filesIndexDialogOpen}
          onOpenChange={setFilesIndexDialogOpen}
          scope={semanticScope}
          status={semanticSearch.status}
          statusError={semanticSearch.statusError}
          onRefreshStatus={() => void semanticSearch.refreshStatus()}
          onOpenPath={openFilesLocation}
        />
      )}
      <NewFolderDialog open={newFolderOpen} onOpenChange={setNewFolderOpen} />
      <NewTextFileDialog
        open={newTextFileOpen}
        onOpenChange={setNewTextFileOpen}
        onCreated={(path, name) => {
          openEditorFromFiler({ path, name });
        }}
      />
      <RenameDialog
        item={renameTarget}
        open={renameTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRenameTarget(null);
        }}
      />
      {viewerFile && browseData && (
        <FileViewer
          file={viewerFile}
          files={browseData.files}
          onClose={() => setViewerFile(null)}
          onNavigate={(f) => setViewerFile(f)}
          returnFocusRef={viewerReturnFocusRef}
        />
      )}
      {previewFile && (
        <FilePreviewPanel
          file={previewFile}
          userId={userId}
          onClose={() => setPreviewFile(null)}
          onOpenWorkspace={(file) => {
            openEditorFromFiler(file);
          }}
        />
      )}
    </div>
  );
}

// ─── ページエントリ ───
function FilerPageInner() {
  return (
    <ExplorerProvider>
      <StorageWorkspace>
        <FilesWorkspaceShellRegistration />
        <ExplorerContent />
      </StorageWorkspace>
    </ExplorerProvider>
  );
}

function FilesWorkspaceShellRegistration() {
  useWorkspaceShellRegistration({
    id: "files-workspace",
    workspaceNavigation: (
      <div
        className="h-full min-h-0 w-full"
        data-shell-slot="workspace-navigation"
        data-workspace="files"
      >
        <FilesBookmarkLauncherSidebar />
      </div>
    ),
    priority: 10,
  });
  return null;
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

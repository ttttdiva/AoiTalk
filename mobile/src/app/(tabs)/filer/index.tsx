import { ScopeSwitcher } from "../../../components/scope-switcher";
import { NativeStorageWorkspace } from "../../../components/files/storage-workspace";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  BackHandler,
  FlatList,
  Image,
  Modal,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import * as DocumentPicker from "expo-document-picker";
import { Audio, ResizeMode, Video, VideoFullscreenUpdate } from "expo-av";
import type {
  AVPlaybackStatus,
  Video as VideoRef,
  VideoFullscreenUpdateEvent,
} from "expo-av";
import * as ScreenOrientation from "expo-screen-orientation";
import {
  ActivityIndicator,
  Button,
  Dialog,
  Divider,
  IconButton,
  Menu,
  Portal,
  ProgressBar,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { useFocusEffect, useRouter } from "expo-router";
import { SafeAreaView } from "react-native-safe-area-context";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import {
  filesApi,
  formatDisplayPath,
  getFilesMediaKind,
  type FilesBookmarkScope,
  type FilesBookmark,
  type FilesEntry,
  type FilesScope,
  type FilesSource,
  getParentPath,
  isProjectFilesNamespacePath,
  parseProjectFilesPath,
} from "../../../lib/files-api";
import { getProjectCapabilities } from "../../../lib/project-api";
import {
  filesLocationCache,
  filesLocationKey,
  isFilesLoadCurrent,
  type FilesLocation,
} from "../../../lib/files-location-cache";
import {
  DEFAULT_AUDIO_PLAYER_SETTINGS,
  loadAudioPlayerSettings,
  type AudioPlayerSettings,
} from "../../../lib/audio-player-settings";
import {
  isServerKnownUnreachable,
  useNetworkStore,
} from "../../../stores/network";
import { ThemedTextInput } from "../../../components/themed-text-input";
import { ScreenHeader } from "../../../components/screen-header";
import { FullScreenModalShell } from "../../../components/full-screen-modal-shell";
import { FilesToolbar } from "../../../features/files/files-toolbar";
import { loadLocalFileBookmarks, setLocalFileBookmark } from "../../../features/files/local-file-bookmarks";
import { fileBookmarkRelativePath, normalizeFileBookmarkPath, visibleFileBookmarks } from "../../../features/files/file-bookmarks";
import {
  SOURCE_LABELS,
  SCOPE_LABELS,
  canPasteClipboardToFilesLocation,
  canRouteClipboardEntries,
  clipboardMatchesFilesLocation,
  dedupeFileEntries,
  fileEntrySelectionKey,
  fileSelectionCount,
  getClipboardAffectedPaths,
  formatScopedServerPath,
  formatTime,
  initialHistories,
  initialLocationMetas,
  initialPaths,
  isAudioEntry,
  isViewableMedia,
  filesLocationIdentityKey,
  canMutateFilesLocation,
  locationKey,
  resolveFilesOpenKind,
  resolveFilesHomePath,
  isFilesPathWithinRoot,
  resolveFilesServerRequestPath,
  resolveFilesParentPath,
  reconcileFileSelection,
  reconcileClipboardForSecurityContext,
  resolveFilesPressAction,
  sortAudioEntries,
  startFileSelection,
  toggleFileSelection,
  type AudioState,
  type ClipboardOperation,
  type ClipboardState,
  type HistoryState,
  type LocationKey,
  type LocationMetaState,
  type LocationState,
  type ViewMode,
} from "../../../features/files/file-browser-model";
import {
  FileMetadata,
  FileThumbnail,
} from "../../../features/files/file-thumbnail";
import { ZoomableImage } from "../../../features/files/zoomable-image";
import { FileNameDialog } from "../../../features/files/file-name-dialog";
import { filesTextEditorParams } from "../../../features/files/files-text-editor-route";
import {
  FILES_DOCUMENT_PICKER_OPTIONS,
  createFilesBatchUploadAlert,
  uploadLocalClipboardFiles,
  uploadPickedFiles,
  type FilesUploadConflict,
} from "../../../features/files/file-upload";

type MediaSource = Awaited<ReturnType<typeof filesApi.getMediaSource>>;

type FilesUploadLocation = {
  source: FilesSource;
  scope: FilesScope;
  path: string;
  destinationPath: string;
  authScope: string;
  projectId: string | null | undefined;
};

type PendingFilesUploadConflict = {
  conflicts: readonly FilesUploadConflict[];
  location: FilesUploadLocation;
  resolve: (decision: "overwrite" | "skip") => void;
};

type FilesBatchUploadOwner = "picker" | "clipboard";

function filesLoadIdentityKey(
  authScope: string,
  source: FilesSource,
  scope: FilesScope,
  projectId: string | null | undefined,
): string {
  return JSON.stringify([authScope, source, scope, projectId ?? ""]);
}

function FilesHeaderChip({
  children,
  selected,
  disabled,
  onPress,
  testID,
  variant = "segment",
}: {
  children: React.ReactNode;
  selected?: boolean;
  disabled?: boolean;
  onPress: () => void;
  testID?: string;
  variant?: "segment" | "scope";
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled, selected }}
      disabled={disabled}
      onPress={onPress}
      hitSlop={6}
      style={[
        styles.segmentChip,
        selected && (variant === "scope" ? styles.scopeChipActive : styles.segmentChipActive),
        disabled && styles.segmentChipDisabled,
      ]}
      testID={testID}
    >
      <Text style={styles.segmentChipText}>{children}</Text>
    </Pressable>
  );
}

export default function FilesScreen() {
  return <ManagedFilesScreen />;
}

function ManagedFilesScreen() {
  const router = useRouter();
  const { isAuthenticated, user } = useAuth();
  const authScope = isAuthenticated
    ? `auth:${user?.user_id ?? "unknown"}`
    : "anonymous";
  const {
    projects,
    selectedProjectId,
    selectedProject,
    selectedSpaceId,
    setSelectedProjectId,
  } = useProject();
  const isAdmin = user?.role === "admin";

  // Project selection is the canonical source of Space identity on mobile.
  // Selecting a project clears selectedSpaceId in the legacy store, so retain
  // the selected project's relation as a compatibility fallback while the
  // store transition settles.
  const effectiveSpaceId = selectedSpaceId ?? selectedProject?.space_id ?? null;

  const [createMenuVisible, setCreateMenuVisible] = useState(false);
  const createFolderRequestRef = useRef(0);

  const [source, setSource] = useState<FilesSource>("local");
  // ローカルは workspace 区分を廃止し user 固定。初期表示も user とする。
  const [scope, setScope] = useState<FilesScope>("user");
  const [paths, setPaths] = useState<LocationState>(initialPaths);
  const [histories, setHistories] = useState<HistoryState>(initialHistories);
  const [locationMetas, setLocationMetas] =
    useState<LocationMetaState>(initialLocationMetas);
  // An empty path is both the initial state and an actual administrator root.
  // Remember which auth/project location has been loaded so the latter is not
  // reinterpreted as the former on focus/refresh.
  const loadedLocationKeysRef = useRef(new Set<string>());
  // A navigation intent may target an unvisited directory (for example a
  // bookmark in a newly selected Project). Keep that explicit path separate
  // from the empty initial state used to request a scope root.
  const explicitLocationPathsRef = useRef(new Map<string, string>());
  const handledProjectSelectionRef = useRef<{ projectId: string | null } | null>(null);
  const previousProjectIdRef = useRef<string | null>(selectedProjectId);
  const [items, setItems] = useState<FilesEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const uploadingRef = useRef(false);
  const filesBatchUploadLockRef = useRef<FilesBatchUploadOwner | null>(null);
  const [uploadConflictDialog, setUploadConflictDialog] =
    useState<PendingFilesUploadConflict | null>(null);
  const uploadConflictWaiterRef = useRef<PendingFilesUploadConflict | null>(null);
  const filesScreenMountedRef = useRef(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [searchVisible, setSearchVisible] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [bookmarks, setBookmarks] = useState<FilesBookmark[]>([]);
  const [bookmarksVisible, setBookmarksVisible] = useState(false);

  const [createFileVisible, setCreateFileVisible] = useState(false);
  const [createFolderVisible, setCreateFolderVisible] = useState(false);
  const [renameVisible, setRenameVisible] = useState(false);
  const [actionTarget, setActionTarget] = useState<FilesEntry | null>(null);
  const [viewerVisible, setViewerVisible] = useState(false);
  const [viewerFile, setViewerFile] = useState<FilesEntry | null>(null);
  const [viewerSource, setViewerSource] = useState<MediaSource | null>(null);
  const [viewerLoading, setViewerLoading] = useState(false);
  const [viewerError, setViewerError] = useState<string | null>(null);
  const [viewerChromeVisible, setViewerChromeVisible] = useState(true);
  const [viewerPreloadSources, setViewerPreloadSources] = useState<
    Record<string, MediaSource>
  >({});
  const viewerImageSourceCacheRef = useRef(new Map<string, MediaSource>());
  const viewerImageSourceFlightRef = useRef(
    new Map<string, Promise<MediaSource>>(),
  );
  const viewerImageReadyRef = useRef(new Set<string>());
  const [videoUri, setVideoUri] = useState<string | null>(null);
  const [videoLoading, setVideoLoading] = useState(false);
  const videoRef = useRef<VideoRef>(null);
  const audioRef = useRef<Audio.Sound | null>(null);
  const audioRequestGenerationRef = useRef(0);
  const audioAdvancingRef = useRef(false);
  const audioEndedHandlerRef = useRef<() => void>(() => {});

  const [renameTarget, setRenameTarget] = useState<FilesEntry | null>(null);
  const [clipboard, setClipboard] = useState<ClipboardState | null>(null);
  const [transferring, setTransferring] = useState(false);
  const clipboardTransferRef = useRef(false);
  const [selectedEntries, setSelectedEntries] = useState<FilesEntry[]>([]);
  // React Native dispatches onPress after onLongPress for Pressable.  Keep the
  // long-pressed key so the synthetic press cannot immediately toggle the
  // newly-selected entry (or open it in normal mode).
  const longPressedEntryKeyRef = useRef<string | null>(null);
  const clipboardRef = useRef<ClipboardState | null>(null);
  clipboardRef.current = clipboard;
  const [downloadingPath, setDownloadingPath] = useState<string | null>(null);
  const [audioState, setAudioState] = useState<AudioState>({
    track: null,
    playlist: [],
    index: -1,
    scope: "workspace",
    rootPath: "",
    loading: false,
    playing: false,
    positionMillis: 0,
    durationMillis: 0,
  });
  const [audioPlayerSettings, setAudioPlayerSettings] =
    useState<AudioPlayerSettings>(DEFAULT_AUDIO_PLAYER_SETTINGS);
  const activeRequestKeyRef = useRef<string | null>(null);
  const displayedRequestKeyRef = useRef<string | null>(null);
  const bookmarkMutationRef = useRef(false);
  const bookmarkRequestGenerationRef = useRef(0);
  const bookmarkScopeKeyRef = useRef<string | null>(null);

  // サーバー一覧をオフラインキャッシュから表示している間の最終同期時刻。
  // null の間はオンライン（もしくはローカル）で、書き込み操作を許可する。
  const [staleCachedAt, setStaleCachedAt] = useState<string | null>(null);
  const staleActive = staleCachedAt !== null;

  const networkConnected = useNetworkStore((s) => s.connected !== false);
  const networkServerReachable = useNetworkStore((s) => s.serverReachable);
  const networkCheckedAt = useNetworkStore((s) => s.serverCheckedAt);
  const isOffline = useMemo(
    () => isServerKnownUnreachable() || !networkConnected,
    // Internet reachability (`online`) is intentionally not a Files server
    // gate: a server on the same LAN remains usable when Android has no
    // internet route.  Re-evaluate after server probe/physical-network state
    // changes so a temporary failure can recover after its short TTL.
    [networkConnected, networkServerReachable, networkCheckedAt],
  );

  const activeKey = locationKey(source, scope);
  const activePath = paths[activeKey];
  const activeHistory = histories[activeKey];
  const activeMeta = locationMetas[activeKey];
  const activeLocationIdentity = useMemo(
    () =>
      filesLocationIdentityKey({
        source,
        scope,
        path: activePath,
        authScope,
        projectId: selectedProjectId,
      }),
    [activePath, authScope, scope, selectedProjectId, source],
  );
  const activeLocationRef = useRef({
    source,
    scope,
    path: activePath,
    authScope,
    projectId: selectedProjectId,
  });
  activeLocationRef.current = {
    source,
    scope,
    path: activePath,
    authScope,
    projectId: selectedProjectId,
  };

  const isFilesUploadLocationCurrent = useCallback(
    (location: FilesUploadLocation) => {
      const current = activeLocationRef.current;
      return (
        filesScreenMountedRef.current &&
        current.source === location.source &&
        current.scope === location.scope &&
        current.path === location.path &&
        current.authScope === location.authScope &&
        current.projectId === location.projectId
      );
    },
    [],
  );

  const settleUploadConflict = useCallback(
    (decision: "overwrite" | "skip") => {
      const pending = uploadConflictWaiterRef.current;
      if (!pending) return;
      uploadConflictWaiterRef.current = null;
      setUploadConflictDialog(null);
      pending.resolve(decision);
    },
    [],
  );

  const confirmFilesUploadConflicts = useCallback(
    (
      conflicts: readonly FilesUploadConflict[],
      location: FilesUploadLocation,
    ): Promise<"overwrite" | "skip"> => {
      if (!isFilesUploadLocationCurrent(location)) return Promise.resolve("skip");
      // A batch owns the only confirmation waiter.  Keep this fail-closed
      // guard even though both entry points also use the shared batch lock.
      if (uploadConflictWaiterRef.current) return Promise.resolve("skip");
      return new Promise((resolve) => {
        const pending: PendingFilesUploadConflict = {
          conflicts,
          location,
          resolve,
        };
        uploadConflictWaiterRef.current = pending;
        setUploadConflictDialog(pending);
      });
    },
    [isFilesUploadLocationCurrent],
  );

  useEffect(() => {
    filesScreenMountedRef.current = true;
    return () => {
      filesScreenMountedRef.current = false;
      settleUploadConflict("skip");
    };
  }, [settleUploadConflict]);

  useEffect(() => {
    const pending = uploadConflictWaiterRef.current;
    if (pending && !isFilesUploadLocationCurrent(pending.location)) {
      // Navigation, project selection, scope changes, and auth changes all
      // invalidate the old destination. Settle the coordinator as skip so an
      // awaiting upload cannot hang or overwrite a newly selected location.
      settleUploadConflict("skip");
    }
  }, [activeLocationIdentity, isFilesUploadLocationCurrent, settleUploadConflict]);

  useEffect(() => {
    // A pending dialog belongs to the directory that opened it.  Close it and
    // invalidate its completion callback when navigation changes that
    // directory, preventing a stale mkdir from hiding a newly opened dialog
    // or refreshing the wrong listing.
    createFolderRequestRef.current += 1;
    setCreateFolderVisible(false);
    setCreateMenuVisible(false);
  }, [activeKey, activePath, authScope, selectedProjectId]);

  // Selection is scoped to the complete Files location identity.  Never carry
  // entries into another source/scope/path/project or authenticated user.
  useEffect(() => {
    longPressedEntryKeyRef.current = null;
    setSelectedEntries([]);
    setActionTarget(null);
    setBookmarksVisible(false);
  }, [activeLocationIdentity]);

  // Keep device-local copies while switching Local -> Server or selecting a
  // destination Project. Account changes still clear every clipboard, while
  // server workspace entries remain bound to their captured Project.
  useEffect(() => {
    setClipboard((current) =>
      reconcileClipboardForSecurityContext(current, {
        authScope,
        projectId: selectedProjectId,
        source,
        scope,
      }),
    );
  }, [authScope, selectedProjectId, scope, source]);

  // A revalidation can remove an entry that was selected earlier.  Reconcile
  // against the full listing (not the search-filtered view) so changing the
  // search query does not unexpectedly drop valid selections.
  useEffect(() => {
    setSelectedEntries((previous) => {
      const next = reconcileFileSelection(previous, items);
      if (
        next.length === previous.length &&
        next.every((entry, index) => entry === previous[index])
      ) {
        return previous;
      }
      return next;
    });
  }, [items]);

  const bookmarkCollection = useMemo<FilesBookmarkScope | null>(() => {
    if (!isAuthenticated) return null;
    if (source === "server" && scope === "workspace") {
      return effectiveSpaceId
        ? { scope: "shared", spaceId: effectiveSpaceId }
        : null;
    }
    return { scope: "personal" };
  }, [effectiveSpaceId, isAuthenticated, scope, source]);
  const bookmarkScopeKey = useMemo(() => {
    if (source === "local") return `${authScope}:local`;
    if (!bookmarkCollection) return `${authScope}:none`;
    return bookmarkCollection.scope === "shared"
      ? `${authScope}:shared:${bookmarkCollection.spaceId}`
      : `${authScope}:personal`;
  }, [authScope, bookmarkCollection, source]);

  const getServerRootPath = useCallback(
    (nextScope: FilesScope, projectIdOverride?: string | null) => {
      if (nextScope === "workspace") {
        const projectId =
          projectIdOverride !== undefined ? projectIdOverride : selectedProjectId;
        if (isAdmin && !projectId) return "";
        return projectId ? `_projects/project_${projectId}` : "";
      }
      return user?.user_id ? `_users/user_${user.user_id}` : "";
    },
    [isAdmin, selectedProjectId, user?.user_id],
  );

  const getFilesLoadIdentityKey = useCallback(
    (
      nextSource: FilesSource,
      nextScope: FilesScope,
      projectId: string | null | undefined = selectedProjectId,
    ) => filesLoadIdentityKey(authScope, nextSource, nextScope, projectId),
    [authScope, selectedProjectId],
  );

  // ProjectContext can change from another tab/screen.  The Files-local
  // selector already clears this state in applyProjectSelection; this effect
  // covers external changes while preserving the admin namespace navigation
  // where the canonical Project intentionally remains selected.
  useEffect(() => {
    const previousProjectId = previousProjectIdRef.current;
    previousProjectIdRef.current = selectedProjectId;
    if (previousProjectId === selectedProjectId) return;

    const handled = handledProjectSelectionRef.current;
    handledProjectSelectionRef.current = null;
    if (handled?.projectId === selectedProjectId) return;

    const workspaceKey = locationKey("server", "workspace");
    loadedLocationKeysRef.current.delete(
      getFilesLoadIdentityKey("server", "workspace", previousProjectId),
    );
    loadedLocationKeysRef.current.delete(
      getFilesLoadIdentityKey("server", "workspace", selectedProjectId),
    );
    explicitLocationPathsRef.current.delete(
      getFilesLoadIdentityKey("server", "workspace", previousProjectId),
    );
    explicitLocationPathsRef.current.delete(
      getFilesLoadIdentityKey("server", "workspace", selectedProjectId),
    );
    setPaths((previous) => ({ ...previous, [workspaceKey]: "" }));
    setHistories((previous) => ({ ...previous, [workspaceKey]: [] }));
    setLocationMetas((previous) => ({
      ...previous,
      [workspaceKey]: { parentPath: null, canGoUp: false, isAdminMode: false },
    }));
    setQuery("");
    setViewerVisible(false);
    setViewerFile(null);
  }, [getFilesLoadIdentityKey, selectedProjectId]);

  const locationWasLoaded = loadedLocationKeysRef.current.has(
    getFilesLoadIdentityKey(source, scope),
  );
  const currentLoadIdentityKey = getFilesLoadIdentityKey(source, scope);
  const hasExplicitLocationPath = explicitLocationPathsRef.current.has(
    currentLoadIdentityKey,
  );
  const pathForCurrentLocationLoad = hasExplicitLocationPath
    ? explicitLocationPathsRef.current.get(currentLoadIdentityKey)
    : locationWasLoaded
      ? activePath
      : source === "server"
        ? undefined
        : activePath || undefined;

  // サーバー・ワークスペースの一覧を実際に取得できるか。
  // 管理者はプロジェクト未選択でも管理者ルートを開ける。一般ユーザーは
  // プロジェクトを選択している場合のみ。
  const isServerWorkspaceListable = useCallback(
    (projectIdOverride?: string | null) => {
      const projectId =
        projectIdOverride !== undefined ? projectIdOverride : selectedProjectId;
      return isAdmin || Boolean(projectId);
    },
    [isAdmin, selectedProjectId],
  );

  // ソース／スコープの表示自体を開けるか（切替の可否）。
  // プロジェクト未選択でもサーバー・ワークスペース表示は開けるようにし、
  // 一覧取得や操作だけを別途無効化する。
  const canOpenLocation = useCallback(
    (nextSource: FilesSource) => {
      if (nextSource === "local") return true;
      return isAuthenticated;
    },
    [isAuthenticated],
  );

  // 指定パスが選択中プロジェクト（管理者ルート含む）の範囲内かどうか。
  const isWithinWorkspaceRoot = useCallback(
    (path: string) => {
      if (isAdmin) return true;
      return isFilesPathWithinRoot(path, getServerRootPath("workspace"));
    },
    [getServerRootPath, isAdmin],
  );

  const canUseServerProjectMutation = useCallback(
    (
      path: string,
      permission: "write" | "delete",
      allowProjectRoot = false,
    ) => {
      if (!isProjectFilesNamespacePath(path)) return true;
      const projectPath = parseProjectFilesPath(path);
      if (!projectPath || (!allowProjectRoot && !projectPath.relativePath)) {
        return false;
      }
      const project =
        projects.find(
          (candidate) =>
            candidate.id.toLowerCase() === projectPath.projectId.toLowerCase(),
        ) ?? null;
      if (!project) return false;
      const capabilities = getProjectCapabilities(project, user);
      return permission === "delete"
        ? capabilities.canDelete
        : capabilities.canWrite;
    },
    [projects, user],
  );

  const canMutateBase = canMutateFilesLocation({
    source,
    staleActive,
    offline: isOffline,
    authenticated: isAuthenticated,
    activePath,
    isAdminMode: activeMeta.isAdminMode,
  });

  const canMutateCurrentPath =
    canMutateBase &&
    (source !== "server" ||
      canUseServerProjectMutation(activePath, "write", true));

  const canMutateEntry = useCallback(
    (
      entry: FilesEntry | null,
      permission: "write" | "delete",
      allowProjectRoot = false,
    ) => {
      if (!entry || !canMutateBase) return false;
      return (
        entry.source !== "server" ||
        canUseServerProjectMutation(entry.path, permission, allowProjectRoot)
      );
    },
    [canMutateBase, canUseServerProjectMutation],
  );

  const setPathForLocation = useCallback(
    (
      nextSource: FilesSource,
      nextScope: FilesScope,
      path: string,
      options: { explicit?: boolean } = {},
    ) => {
      const key = locationKey(nextSource, nextScope);
      const identityKey = filesLoadIdentityKey(
        authScope,
        nextSource,
        nextScope,
        selectedProjectId,
      );
      if (options.explicit === false) {
        explicitLocationPathsRef.current.delete(identityKey);
      } else {
        explicitLocationPathsRef.current.set(identityKey, path);
      }
      setPaths((prev) => ({ ...prev, [key]: path }));
    },
    [authScope, selectedProjectId],
  );

  const locationUnavailableMessage = useCallback(
    (nextScope: FilesScope) => {
      if (!isAuthenticated) {
        return "サーバーファイルはログイン中のみ利用できます。";
      }
      return nextScope === "workspace"
        ? "プロジェクトを選択してください。"
        : "ユーザー領域を開けませんでした。";
    },
    [isAuthenticated],
  );

  const loadEntries = useCallback(
    async (
      nextSource: FilesSource,
      nextScope: FilesScope,
      nextPath?: string,
      projectIdOverride?: string | null,
      options: { revalidate?: boolean } = {},
    ) => {
      const requestKeys: string[] = [];
      const clearLocation = (message: string | null) => {
        activeRequestKeyRef.current = null;
        displayedRequestKeyRef.current = null;
        setStaleCachedAt(null);
        setItems([]);
        setPathForLocation(nextSource, nextScope, "", { explicit: false });
        setLocationMetas((prev) => ({
          ...prev,
          [locationKey(nextSource, nextScope)]: {
            parentPath: null,
            canGoUp: false,
            isAdminMode: false,
          },
        }));
        setError(message);
        setLoading(false);
      };
      try {
        if (nextSource === "server" && !isAuthenticated) {
          clearLocation("サーバーファイルはログイン中のみ利用できます。");
          return;
        }

        // 一般ユーザーがプロジェクト未選択でワークスペースを開いた場合は、
        // 拒否せずプロジェクト選択を促す空状態を表示する。
        if (
          nextSource === "server" &&
          nextScope === "workspace" &&
          !isServerWorkspaceListable(projectIdOverride)
        ) {
          clearLocation("プロジェクトを選択してください。");
          return;
        }

        const serverRootPath = getServerRootPath(nextScope, projectIdOverride);
        let requestPath =
          nextSource === "server"
            ? (nextPath ?? serverRootPath)
            : nextPath;

        const projectId =
          projectIdOverride !== undefined
            ? projectIdOverride
            : selectedProjectId;
        if (nextSource === "server") {
          requestPath = resolveFilesServerRequestPath({
            scope: nextScope,
            requestedPath: requestPath ?? "",
            serverRootPath,
            isAdmin,
          });
        }

        const location: FilesLocation = {
          source: nextSource,
          scope: nextScope,
          authScope,
          // Preserve an explicit empty server path.  It is the administrator
          // workspace root after navigating above a Project/User namespace;
          // `undefined` means the location has not been initialized yet.
          path: requestPath === undefined ? undefined : requestPath,
          projectId,
        };
        const requestKey = filesLocationKey(location);
        requestKeys.push(requestKey);
        activeRequestKeyRef.current = requestKey;
        const cached = filesLocationCache.peek(location);
        if (cached) {
          setItems(cached.items);
          setPathForLocation(nextSource, nextScope, cached.currentPath, {
            explicit: false,
          });
          loadedLocationKeysRef.current.add(
            getFilesLoadIdentityKey(nextSource, nextScope, projectId),
          );
          setLocationMetas((prev) => ({
            ...prev,
            [locationKey(nextSource, nextScope)]: {
              parentPath: cached.parentPath,
              canGoUp: cached.canGoUp,
              isAdminMode: cached.isAdminMode,
            },
          }));
          displayedRequestKeyRef.current = filesLocationKey({
            ...location,
            path: cached.currentPath,
          });
          setLoading(false);
          setError(null);
        } else if (displayedRequestKeyRef.current !== requestKey) {
          setItems([]);
          setLoading(true);
          setError(null);
        }

        const loaded = await filesLocationCache.load(location, options);
        requestKeys.push(loaded.resolvedKey);
        if (!isFilesLoadCurrent(activeRequestKeyRef.current, loaded)) {
          return;
        }
        activeRequestKeyRef.current = loaded.resolvedKey;
        displayedRequestKeyRef.current = loaded.resolvedKey;
        const result = loaded.result;
        setItems(result.items);
        setPathForLocation(nextSource, nextScope, result.currentPath, {
          explicit: false,
        });
        loadedLocationKeysRef.current.add(
          getFilesLoadIdentityKey(nextSource, nextScope, projectId),
        );
        setLocationMetas((prev) => ({
          ...prev,
          [locationKey(nextSource, nextScope)]: {
            parentPath: result.parentPath,
            canGoUp: result.canGoUp,
            isAdminMode: result.isAdminMode,
          },
        }));
        // オフラインキャッシュ表示中は最終同期時刻を保持し、書き込みを無効化する。
        setStaleCachedAt(loaded.stale ? loaded.cachedAt ?? "" : null);
        setError(null);
      } catch (loadError) {
        if (
          requestKeys.length > 0 &&
          !requestKeys.includes(activeRequestKeyRef.current ?? "")
        ) {
          return;
        }
        if (displayedRequestKeyRef.current === null) setItems([]);
        setStaleCachedAt(null);
        const offlineNow =
          nextSource === "server" &&
          (isServerKnownUnreachable() ||
            useNetworkStore.getState().connected === false);
        setError(
          offlineNow
            ? "オフラインのため一覧を取得できません"
            : loadError instanceof Error
              ? loadError.message
              : "ファイル一覧の取得に失敗しました",
        );
      } finally {
        if (
          requestKeys.length === 0 ||
          requestKeys.includes(activeRequestKeyRef.current ?? "")
        ) {
          setLoading(false);
        }
      }
    },
    [
      getServerRootPath,
      authScope,
      isAuthenticated,
      isServerWorkspaceListable,
      isAdmin,
      getFilesLoadIdentityKey,
      selectedProjectId,
      setPathForLocation,
    ],
  );

  const loadBookmarks = useCallback(async () => {
    const collection = bookmarkCollection;
    const scopeKey = bookmarkScopeKey;
    const requestGeneration = bookmarkRequestGenerationRef.current + 1;
    bookmarkRequestGenerationRef.current = requestGeneration;
    bookmarkScopeKeyRef.current = scopeKey;

    // A Space transition must not leave the previous collection visible while
    // the new request is in flight.  It also intentionally clears the state
    // for an unauthenticated/admin-without-space screen instead of falling
    // back to a user-wide collection.
    if (!isAuthenticated || !collection) {
      setBookmarks([]);
      return;
    }

    // Bookmarks are server-owned too.  Avoid starting a request when there is
    // no physical network path; unlike `online`, this is a hard transport
    // prerequisite.  A connected LAN with online=false continues below.
    if (source === "server" && !networkConnected) {
      setBookmarks([]);
      return;
    }

    try {
      const result = source === "local"
        ? { bookmarks: await loadLocalFileBookmarks(authScope) }
        : await filesApi.listBookmarks(collection);
      if (
        bookmarkRequestGenerationRef.current !== requestGeneration ||
        bookmarkScopeKeyRef.current !== scopeKey
      ) {
        return;
      }
      setBookmarks(result.bookmarks || []);
    } catch {
      if (
        bookmarkRequestGenerationRef.current !== requestGeneration ||
        bookmarkScopeKeyRef.current !== scopeKey
      ) {
        return;
      }
      setBookmarks([]);
    }
  }, [authScope, bookmarkCollection, bookmarkScopeKey, isAuthenticated, networkConnected, source]);

  // Keep collection identity separate from Files location identity.  A stale
  // Space A response must never populate Space B, even if the request began
  // before ProjectContext finished switching its selected project.
  useEffect(() => {
    bookmarkRequestGenerationRef.current += 1;
    bookmarkScopeKeyRef.current = bookmarkScopeKey;
    setBookmarks([]);
    void loadBookmarks();
  }, [bookmarkScopeKey, loadBookmarks]);

  useEffect(() => {
    void loadEntries(source, scope, pathForCurrentLocationLoad);
  }, [activePath, loadEntries, pathForCurrentLocationLoad, scope, source]);

  const visibleLocationRef = useRef<() => Promise<void>>(async () => {});
  visibleLocationRef.current = () =>
    loadEntries(source, scope, pathForCurrentLocationLoad, undefined, {
      revalidate: true,
    });

  useFocusEffect(
    useCallback(() => {
      void visibleLocationRef.current();
      void loadBookmarks();
      void loadAudioPlayerSettings()
        .then(setAudioPlayerSettings)
        .catch(() => {});
    }, [loadBookmarks]),
  );

  useEffect(() => {
    if (!isAuthenticated && source === "server") {
      setSource("local");
      setScope("user");
    }
  }, [isAuthenticated, source]);

  const onRefresh = async () => {
    setRefreshing(true);
    await Promise.all([
      loadEntries(source, scope, pathForCurrentLocationLoad, undefined, {
        revalidate: true,
      }),
      loadBookmarks(),
    ]);
    setRefreshing(false);
  };

  const resetLocationTransientUi = () => {
    longPressedEntryKeyRef.current = null;
    setSelectedEntries([]);
    setActionTarget(null);
    setCreateMenuVisible(false);
  };

  const changeSource = (nextSource: FilesSource) => {
    resetLocationTransientUi();
    if (!canOpenLocation(nextSource)) {
      Alert.alert("Files", locationUnavailableMessage(scope));
      return;
    }
    // ローカルは常に user 区分を使う（workspace 区分は廃止）。
    if (nextSource === "local") {
      setScope("user");
      // staleCachedAt belongs to a server cache entry and must not disable
      // local create/upload operations while the local listing refreshes.
      setStaleCachedAt(null);
    }
    setSource(nextSource);
  };

  const changeScope = (nextScope: FilesScope) => {
    resetLocationTransientUi();
    if (!canOpenLocation(source)) {
      Alert.alert("Files", locationUnavailableMessage(nextScope));
      return;
    }
    setScope(nextScope);
  };

  const openCreateFileDialog = () => {
    setCreateMenuVisible(false);
    setCreateFileVisible(true);
  };

  const openCreateFolderDialog = () => {
    setCreateMenuVisible(false);
    createFolderRequestRef.current += 1;
    setCreateFolderVisible(true);
  };

  const dismissCreateFolderDialog = () => {
    createFolderRequestRef.current += 1;
    setCreateFolderVisible(false);
  };

  const openUploadPicker = () => {
    setCreateMenuVisible(false);
    void uploadFile();
  };

  // ファイラー画面内でプロジェクトを切り替える。旧プロジェクトの現在地・履歴・
  // server workspace のクリップボード・検索・プレビュー状態は引き継がない。
  // local COPY はアップロード先を選ぶための切替なので保持する。nextProjectId が
  // null の場合、管理者は管理者ルート、一般ユーザーは未選択状態になる。
  const applyProjectSelection = useCallback(
    async (nextProjectId: string | null) => {
      if (
        nextProjectId === selectedProjectId &&
        !selectedSpaceId &&
        source === "server" &&
        scope === "workspace"
      ) {
        return;
      }

      const workspaceKey = locationKey("server", "workspace");
      loadedLocationKeysRef.current.delete(
        getFilesLoadIdentityKey("server", "workspace", selectedProjectId),
      );
      loadedLocationKeysRef.current.delete(
        getFilesLoadIdentityKey("server", "workspace", nextProjectId),
      );
      explicitLocationPathsRef.current.delete(
        getFilesLoadIdentityKey("server", "workspace", selectedProjectId),
      );
      explicitLocationPathsRef.current.delete(
        getFilesLoadIdentityKey("server", "workspace", nextProjectId),
      );
      // 旧プロジェクトの位置・履歴・親情報をクリアする。
      setPaths((prev) => ({ ...prev, [workspaceKey]: "" }));
      setHistories((prev) => ({ ...prev, [workspaceKey]: [] }));
      setLocationMetas((prev) => ({
        ...prev,
        [workspaceKey]: { parentPath: null, canGoUp: false, isAdminMode: false },
      }));
      // Server workspace の項目はProject境界で破棄し、local COPY は保持する。
      setClipboard((current) =>
        reconcileClipboardForSecurityContext(current, {
          authScope,
          projectId: nextProjectId,
          source: "server",
          scope: "workspace",
        }),
      );
      longPressedEntryKeyRef.current = null;
      setSelectedEntries([]);
      setActionTarget(null);
      setQuery("");
      setViewerVisible(false);
      setViewerFile(null);

      // The store updates synchronously, while persistence is asynchronous.
      // Awaiting the canonical setter lets bookmark navigation apply its path
      // only after the target Project selection has been committed.
      handledProjectSelectionRef.current = { projectId: nextProjectId };
      await setSelectedProjectId(nextProjectId);
      setSource("server");
      setScope("workspace");
    },
    [
      authScope,
      getFilesLoadIdentityKey,
      scope,
      selectedProjectId,
      selectedSpaceId,
      setSelectedProjectId,
      source,
    ],
  );

  const pushHistoryForKey = useCallback((key: LocationKey, path: string) => {
    setHistories((prev) => ({
      ...prev,
      [key]: [...prev[key], path],
    }));
  }, []);

  const pushHistory = useCallback((path: string) => {
    pushHistoryForKey(activeKey, path);
  }, [activeKey, pushHistoryForKey]);

  const popHistory = useCallback((): string | null => {
    const previousPath =
      activeHistory.length > 0 ? activeHistory[activeHistory.length - 1] : null;
    setHistories((prev) => ({
      ...prev,
      [activeKey]: prev[activeKey].slice(0, -1),
    }));
    return previousPath;
  }, [activeHistory, activeKey]);

  // 一般ユーザーの境界は維持する。管理者はProjectContextの選択を
  // canonicalなまま保ち、namespace親を参照してもHomeは元Project rootへ戻す。
  const isWorkspaceNavigationAllowed = useCallback(
    (targetPath: string) => {
      if (source !== "server" || scope !== "workspace") return true;
      return isWithinWorkspaceRoot(targetPath);
    },
    [isWithinWorkspaceRoot, scope, source],
  );

  const setNavigationPath = (targetPath: string) => {
    setPathForLocation(source, scope, targetPath);
  };

  const parentPath = resolveFilesParentPath({
    source, scope, currentPath: activePath, isAdmin,
    projectRoot: getServerRootPath("workspace"),
    scopeRoot: source === "server" ? getServerRootPath(scope) : undefined,
    parentPath: activeMeta.parentPath, canGoUp: activeMeta.canGoUp,
  });

  const goBack = () => {
    const previousPath = popHistory();
    if (previousPath == null) return;
    if (!isWorkspaceNavigationAllowed(previousPath)) {
      // 境界外の履歴はプロジェクトルートへ丸める。
      const rootPath = getServerRootPath("workspace");
      setPathForLocation(source, scope, rootPath);
      return;
    }
    setNavigationPath(previousPath);
  };

  const goUp = () => {
    if (parentPath == null || !isWorkspaceNavigationAllowed(parentPath)) return;
    if (activePath !== parentPath) pushHistory(activePath);
    setNavigationPath(parentPath);
  };

  const homePath = useMemo(
    () =>
      resolveFilesHomePath({
        source,
        scope,
        currentPath: activePath,
        serverRootPath: getServerRootPath(scope),
      }),
    [activePath, getServerRootPath, scope, source],
  );

  const goHome = () => {
    if (activePath === homePath) return;
    if (!isWorkspaceNavigationAllowed(homePath)) return;
    pushHistory(activePath);
    setNavigationPath(homePath);
  };

  const navigateTo = (nextPath: string) => {
    if (!isWorkspaceNavigationAllowed(nextPath)) {
      Alert.alert("Files", "選択中プロジェクトの範囲外へは移動できません。");
      return;
    }
    if (activePath !== nextPath) {
      pushHistory(activePath);
    }
    setNavigationPath(nextPath);
  };

  const openMediaViewer = (entry: FilesEntry) => {
    setViewerChromeVisible(true);
    setViewerFile(entry);
    setViewerVisible(true);
  };

  const getAudioRootPath = useCallback(
    (entry: FilesEntry, trackScope: FilesScope) => {
      if (entry.source === "server") return getServerRootPath(trackScope);
      let cursor = getParentPath(entry.source, entry.path, trackScope);
      let root = cursor ?? "";
      while (cursor) {
        const parent = getParentPath(entry.source, cursor, trackScope);
        if (parent == null) break;
        root = parent;
        cursor = parent;
      }
      return root;
    },
    [getServerRootPath],
  );

  const collectAudioPlaylist = useCallback(
    async (track: FilesEntry, trackScope: FilesScope, rootPath: string) => {
      const pending = [rootPath];
      const tracks: FilesEntry[] = [];
      let scanned = 0;
      while (pending.length > 0 && tracks.length < 1000 && scanned < 240) {
        const path = pending.shift() ?? "";
        scanned += 1;
        const result = await filesApi.list(track.source, path || undefined, trackScope);
        pending.push(
          ...result.items
            .filter((entry) => entry.type === "directory")
            .map((entry) => entry.path),
        );
        tracks.push(...result.items.filter(isAudioEntry));
      }
      return sortAudioEntries(tracks);
    },
    [],
  );

  const updateAudioStatus = useCallback(
    (status: AVPlaybackStatus, requestGeneration: number) => {
      if (audioRequestGenerationRef.current !== requestGeneration) return;
      if (!status.isLoaded) {
        setAudioState((prev) => ({
          ...prev,
          loading: false,
          playing: false,
        }));
        return;
      }
      setAudioState((prev) => ({
        ...prev,
        loading: false,
        playing: status.isPlaying,
        positionMillis: status.positionMillis,
        durationMillis: status.durationMillis ?? 0,
      }));
      if (status.didJustFinish) {
        audioEndedHandlerRef.current();
      }
    },
    [],
  );

  const playAudioAt = useCallback(
    async (
      playlist: FilesEntry[],
      index: number,
      trackScope = scope,
      rootPath?: string,
    ) => {
      const track = playlist[index];
      if (!track) return;
      const requestGeneration = audioRequestGenerationRef.current + 1;
      audioRequestGenerationRef.current = requestGeneration;
      const nextRootPath = rootPath ?? getAudioRootPath(track, trackScope);
      setAudioState((prev) => ({
        ...prev,
        track,
        playlist,
        index,
        scope: trackScope,
        rootPath: nextRootPath,
        loading: true,
        playing: false,
        positionMillis: 0,
        durationMillis: 0,
      }));
      try {
        const previousSound = audioRef.current;
        audioRef.current = null;
        if (previousSound) {
          await previousSound.unloadAsync();
        }
        const uri = await filesApi.getPlayableUri(track);
        if (audioRequestGenerationRef.current !== requestGeneration) return;
        const { sound, status } = await Audio.Sound.createAsync(
          { uri },
          { shouldPlay: true },
          (nextStatus) => updateAudioStatus(nextStatus, requestGeneration),
        );
        if (audioRequestGenerationRef.current !== requestGeneration) {
          await sound.unloadAsync();
          return;
        }
        audioRef.current = sound;
        updateAudioStatus(status, requestGeneration);
      } catch (audioError) {
        if (audioRequestGenerationRef.current !== requestGeneration) return;
        setAudioState((prev) => ({ ...prev, loading: false, playing: false }));
        Alert.alert(
          "Audio",
          audioError instanceof Error
            ? audioError.message
            : "音声を再生できませんでした。",
        );
      }
    },
    [getAudioRootPath, scope, updateAudioStatus],
  );

  const playAudioEntry = useCallback(
    async (entry: FilesEntry) => {
      const playlist = items.filter(isAudioEntry);
      const index = Math.max(
        0,
        playlist.findIndex((item) => item.path === entry.path),
      );
      await playAudioAt(playlist.length > 0 ? playlist : [entry], index, scope);
    },
    [items, playAudioAt, scope],
  );

  const toggleAudio = async () => {
    const sound = audioRef.current;
    if (!sound || audioState.loading) return;
    const status = await sound.getStatusAsync();
    if (!status.isLoaded) return;
    if (status.isPlaying) {
      await sound.pauseAsync();
    } else {
      await sound.playAsync();
    }
  };

  const stopAudio = async () => {
    audioRequestGenerationRef.current += 1;
    const sound = audioRef.current;
    audioRef.current = null;
    setAudioState({
      track: null,
      playlist: [],
      index: -1,
      scope: "workspace",
      rootPath: "",
      loading: false,
      playing: false,
      positionMillis: 0,
      durationMillis: 0,
    });
    if (sound) {
      await sound.unloadAsync();
    }
  };

  const nextAudio = async () => {
    await advanceAudio(1, true);
  };

  const previousAudio = async () => {
    await advanceAudio(-1, false);
  };

  const advanceAudio = async (direction: 1 | -1, wrap: boolean) => {
    const track = audioState.track;
    if (!track || audioAdvancingRef.current) return;
    const requestGeneration = audioRequestGenerationRef.current;
    audioAdvancingRef.current = true;
    try {
      const settings = audioPlayerSettings;
      if (direction === 1 && settings.repeatOne) {
        await playAudioAt([track], 0, audioState.scope, audioState.rootPath);
        return;
      }

      let playlist = audioState.playlist;
      if (direction === 1 && settings.playbackScope === "global_next") {
        playlist = await collectAudioPlaylist(
          track,
          audioState.scope,
          audioState.rootPath || getAudioRootPath(track, audioState.scope),
        );
        if (audioRequestGenerationRef.current !== requestGeneration) return;
      }
      if (playlist.length === 0) return;

      if (direction === 1 && settings.shuffle && playlist.length > 1) {
        const pool = playlist.filter((entry) => entry.path !== track.path);
        const next = pool[Math.floor(Math.random() * pool.length)];
        const nextIndex = playlist.findIndex((entry) => entry.path === next?.path);
        if (next && nextIndex >= 0) {
          await playAudioAt(playlist, nextIndex, audioState.scope, audioState.rootPath);
        }
        return;
      }

      const index = playlist.findIndex((entry) => entry.path === track.path);
      const nextIndex = index + direction;
      if (nextIndex >= 0 && nextIndex < playlist.length) {
        await playAudioAt(playlist, nextIndex, audioState.scope, audioState.rootPath);
      } else if (wrap && direction === 1) {
        await playAudioAt(playlist, 0, audioState.scope, audioState.rootPath);
      }
    } finally {
      audioAdvancingRef.current = false;
    }
  };

  const focusAudioTrackLocation = () => {
    const track = audioState.track;
    if (!track) return;

    // ローカルは user 区分固定。サーバーのみ再生時の scope を引き継ぐ。
    const targetScope: FilesScope =
      track.source === "local" ? "user" : audioState.scope;
    if (!canOpenLocation(track.source)) {
      Alert.alert("Files", locationUnavailableMessage(targetScope));
      return;
    }

    const parentPath = getParentPath(track.source, track.path, targetScope);
    if (parentPath == null) return;

    const targetKey = locationKey(track.source, targetScope);
    const currentTargetPath = paths[targetKey];
    if (currentTargetPath !== parentPath) {
      pushHistoryForKey(targetKey, currentTargetPath);
    }

    setSource(track.source);
    setScope(targetScope);
    setPathForLocation(track.source, targetScope, parentPath);
  };

  useEffect(() => {
    audioEndedHandlerRef.current = () => {
      void advanceAudio(1, true);
    };
  });

  useEffect(() => {
    return () => {
      audioRequestGenerationRef.current += 1;
      const sound = audioRef.current;
      audioRef.current = null;
      if (sound) {
        void sound.unloadAsync();
      }
    };
  }, []);

  const handleOpenEntry = async (entry: FilesEntry) => {
    const kind = resolveFilesOpenKind(entry);
    if (kind === "directory") {
      await navigateTo(entry.path);
      return;
    }
    if (kind === "audio") {
      await playAudioEntry(entry);
      return;
    }
    if (kind === "media") {
      openMediaViewer(entry);
      return;
    }
    if (kind === "text") {
      router.push(filesTextEditorParams(entry));
      return;
    }

    Alert.alert("Files", "この形式はアプリ内プレビュー未対応です。");
  };

  const createTextFile = async (name: string) => {
    if (
      !name ||
      (!activePath && !(source === "server" && activeMeta.isAdminMode))
    ) {
      return;
    }
    if (!canMutateCurrentPath) {
      Alert.alert("Files", "この場所にはファイルを作成できません。");
      return;
    }
    try {
      await filesApi.createTextFile(source, activePath, name);
      setCreateFileVisible(false);
      await loadEntries(source, scope, activePath, undefined, {
        revalidate: true,
      });
    } catch (createError) {
      Alert.alert(
        "Files",
        createError instanceof Error
          ? createError.message
          : "ファイル作成に失敗しました。",
      );
    }
  };

  const createFolder = async (name: string) => {
    if (
      !name ||
      (!activePath && !(source === "server" && activeMeta.isAdminMode))
    ) {
      return;
    }
    if (!canMutateCurrentPath) {
      Alert.alert("Files", "この場所にはフォルダーを作成できません。");
      return;
    }
    const requestLocation = {
      source,
      scope,
      path: activePath,
      authScope,
      projectId: selectedProjectId,
    };
    const requestId = createFolderRequestRef.current + 1;
    createFolderRequestRef.current = requestId;
    try {
      await filesApi.createFolder(
        requestLocation.source,
        requestLocation.path,
        name,
      );

      // A user can navigate or switch project/source while the mutation is
      // in flight.  The folder was created in requestLocation, so only
      // refresh that location when it is still the visible one; otherwise
      // leave the current listing and dialog state untouched (the location
      // change effect already invalidated the stale dialog).
      const currentLocation = activeLocationRef.current;
      if (
        createFolderRequestRef.current !== requestId ||
        currentLocation.source !== requestLocation.source ||
        currentLocation.scope !== requestLocation.scope ||
        currentLocation.path !== requestLocation.path ||
        currentLocation.authScope !== requestLocation.authScope ||
        currentLocation.projectId !== requestLocation.projectId
      ) {
        return;
      }
      setCreateFolderVisible(false);
      // Do not let a pre-create list request win the refresh.  invalidate()
      // supersedes that flight before starting the post-create revalidation.
      filesLocationCache.invalidate({
        source: requestLocation.source,
        scope: requestLocation.scope,
        authScope: requestLocation.authScope,
        path: requestLocation.path || undefined,
        projectId: requestLocation.projectId,
      });
      await loadEntries(
        requestLocation.source,
        requestLocation.scope,
        requestLocation.path,
        undefined,
        {
          revalidate: true,
        },
      );
    } catch (createError) {
      if (createFolderRequestRef.current !== requestId) return;
      Alert.alert(
        "Files",
        createError instanceof Error
          ? createError.message
          : "フォルダー作成に失敗しました。",
      );
    }
  };

  const submitRename = async (name: string) => {
    if (!renameTarget || !name) return;
    if (!canMutateEntry(renameTarget, "write")) {
      Alert.alert("Files", "この項目を名前変更する権限がありません。");
      return;
    }
    try {
      const oldPath = renameTarget.path;
      const nextPath = await filesApi.rename(
        renameTarget.source,
        oldPath,
        name,
      );
      if (renameTarget.type === "directory") {
        const parentOfCurrent = getParentPath(source, activePath, scope);
        if (activePath === oldPath) {
          setPathForLocation(source, scope, nextPath);
        } else if (parentOfCurrent === oldPath) {
          setPathForLocation(source, scope, parentOfCurrent || activePath);
        }
      }
      setRenameVisible(false);
      setRenameTarget(null);
      await loadEntries(
        source,
        scope,
        activePath === oldPath && renameTarget.type === "directory"
          ? nextPath
          : activePath,
        undefined,
        { revalidate: true },
      );
    } catch (renameError) {
      Alert.alert(
        "Files",
        renameError instanceof Error
          ? renameError.message
          : "名前変更に失敗しました。",
      );
    }
  };

  const deleteEntry = (entry: FilesEntry) => {
    if (!canMutateEntry(entry, "delete")) {
      Alert.alert("Files", "この項目を削除する権限がありません。");
      return;
    }
    Alert.alert(
      "Files",
      `${entry.name} を削除します。`,
      [
        { text: "キャンセル", style: "cancel" },
        {
          text: "削除",
          style: "destructive",
          onPress: () => {
            void (async () => {
              try {
                await filesApi.remove(entry.source, entry.path);
                await loadEntries(source, scope, activePath, undefined, {
                  revalidate: true,
                });
              } catch (deleteError) {
                Alert.alert(
                  "Files",
                  deleteError instanceof Error
                    ? deleteError.message
                    : "削除に失敗しました。",
                );
              }
            })();
          },
        },
      ],
      { cancelable: true },
    );
  };

  const deleteSelectedEntries = () => {
    const entries = dedupeFileEntries(selectedEntries);
    if (entries.length === 0) return;
    const denied = entries.find((entry) => !canMutateEntry(entry, "delete"));
    if (denied) {
      Alert.alert("Files", `${denied.name} を削除する権限がありません。`);
      return;
    }

    Alert.alert(
      "Files",
      `${entries.length}件の項目を削除します。`,
      [
        { text: "キャンセル", style: "cancel" },
        {
          text: "削除",
          style: "destructive",
          onPress: () => {
            const requestLocation = {
              source,
              scope,
              path: activePath,
              authScope,
              projectId: selectedProjectId,
            };
            void (async () => {
              const deniedAtStart = entries.find(
                (entry) => !canMutateEntry(entry, "delete"),
              );
              if (deniedAtStart) {
                Alert.alert(
                  "Files",
                  `${deniedAtStart.name} を削除する権限がありません。`,
                );
                return;
              }
              setTransferring(true);
              const succeeded: FilesEntry[] = [];
              const failures: Array<{ entry: FilesEntry; error: unknown }> = [];
              try {
                // Validate all entries before this loop (above), then execute
                // each request independently so partial failures are explicit.
                for (const entry of entries) {
                  try {
                    await filesApi.remove(entry.source, entry.path);
                    succeeded.push(entry);
                  } catch (error) {
                    failures.push({ entry, error });
                  }
                }

                const currentLocation = activeLocationRef.current;
                const stillCurrent =
                  currentLocation.source === requestLocation.source &&
                  currentLocation.scope === requestLocation.scope &&
                  currentLocation.path === requestLocation.path &&
                  currentLocation.authScope === requestLocation.authScope &&
                  currentLocation.projectId === requestLocation.projectId;
                filesLocationCache.invalidate({
                  source: requestLocation.source,
                  scope: requestLocation.scope,
                  authScope: requestLocation.authScope,
                  path: requestLocation.path || undefined,
                  projectId: requestLocation.projectId,
                });
                if (stillCurrent) {
                  setSelectedEntries(failures.map(({ entry }) => entry));
                  await loadEntries(
                    requestLocation.source,
                    requestLocation.scope,
                    requestLocation.path,
                    requestLocation.projectId,
                    { revalidate: true },
                  );
                }
                if (failures.length > 0) {
                  const firstFailure = failures[0].error;
                  const detail =
                    firstFailure instanceof Error ? ` ${firstFailure.message}` : "";
                  Alert.alert(
                    "削除結果",
                    `${succeeded.length}件を削除しました。${failures.length}件は削除できませんでした。${detail}`,
                  );
                }
              } catch (error) {
                Alert.alert(
                  "Files",
                  error instanceof Error ? error.message : "削除に失敗しました。",
                );
              } finally {
                setTransferring(false);
              }
            })();
          },
        },
      ],
      { cancelable: true },
    );
  };

  const downloadEntry = async (entry: FilesEntry) => {
    if (entry.type !== "file" || downloadingPath) return;
    setDownloadingPath(entry.path);
    try {
      const result = await filesApi.download(entry);
      if (result.status === "saved") {
        Alert.alert(
          "ダウンロード完了",
          `${entry.name} を選択したフォルダーに保存しました。`,
        );
      }
    } catch (downloadError) {
      Alert.alert(
        "ダウンロードに失敗",
        downloadError instanceof Error
          ? downloadError.message
          : "ファイルを保存できませんでした。",
      );
    } finally {
      setDownloadingPath(null);
    }
  };

  const setClipboardEntry = (
    entry: FilesEntry,
    operation: ClipboardOperation,
  ) => {
    setClipboardEntries([entry], operation);
  };

  const setClipboardEntries = (
    entries: readonly FilesEntry[],
    operation: ClipboardOperation,
  ): boolean => {
    const uniqueEntries = dedupeFileEntries(entries);
    if (uniqueEntries.length === 0) return false;
    const clipboardSource = uniqueEntries[0].source;
    if (uniqueEntries.some((entry) => entry.source !== clipboardSource)) {
      Alert.alert("Files", "異なる場所の項目を同時に処理できません。");
      return false;
    }
    const denied = uniqueEntries.find((entry) => !canMutateEntry(entry, "write"));
    if (denied) {
      Alert.alert("Files", `${denied.name} はコピー・移動できません。`);
      return false;
    }
    setClipboard({
      entries: uniqueEntries,
      operation,
      source: clipboardSource,
      scope,
      authScope,
      sourcePath: activePath,
      projectId: selectedProjectId,
      projectRoot:
        clipboardSource === "server" && scope === "workspace"
          ? getServerRootPath("workspace")
          : null,
    });
    Alert.alert(
      "Files",
      `${uniqueEntries.length}件を${operation === "copy" ? "コピー" : "移動"}対象にしました。移動先で貼り付けてください。`,
    );
    return true;
  };

  // 同一sourceの既存transferに加え、local COPY -> server uploadだけを許可する。
  const clipboardMatchesCurrent = useCallback(
    () =>
      clipboardMatchesFilesLocation(clipboard, {
        source,
        scope,
        path: activePath,
        authScope,
        projectRoot:
          source === "server" && scope === "workspace"
            ? getServerRootPath("workspace")
            : null,
      }),
    [activePath, authScope, clipboard, getServerRootPath, scope, source],
  );

  const pasteClipboard = async (
    destinationPath = activePath,
    destinationEntry: FilesEntry | null = null,
  ) => {
    if (
      !clipboard ||
      transferring ||
      clipboardTransferRef.current ||
      filesBatchUploadLockRef.current ||
      uploadConflictWaiterRef.current
    ) return;
    const canWriteDestination = destinationEntry
      ? canMutateEntry(destinationEntry, "write", true)
      : canMutateCurrentPath;
    if (!canWriteDestination || !destinationPath) {
      Alert.alert("Files", "この場所には貼り付けできません。");
      return;
    }
    if (!clipboardMatchesCurrent()) {
      let mismatchMessage = "別プロジェクトの項目はここに貼り付けできません。";
      if (clipboard.authScope !== authScope) {
        mismatchMessage = "別のアカウントの項目はここに貼り付けできません。";
      } else if (clipboard.source !== source) {
        mismatchMessage =
          clipboard.source === "local" &&
          source === "server" &&
          clipboard.operation === "move"
            ? "ローカルからサーバーへの移動には対応していません。コピーを選択してください。"
            : "この向きのローカル・サーバー間貼り付けには対応していません。";
      } else if (clipboard.scope !== scope) {
        mismatchMessage = "別の領域の項目はここに貼り付けできません。";
      }
      Alert.alert(
        "Files",
        mismatchMessage,
      );
      return;
    }
    if (!canRouteClipboardEntries(clipboard, destinationPath, source)) {
      Alert.alert(
        "Files",
        clipboard.source === "local" && source === "server"
          ? "サーバーへ貼り付けできるのはローカルのファイルだけです。"
          : "プロジェクトファイルのコピー・移動は同一プロジェクト内のみ利用できます。",
      );
      return;
    }

    // Re-check source permissions for every entry before starting any request.
    // A capability can change after the clipboard was populated.
    const denied = clipboard.entries.find(
      (entry) => !canMutateEntry(entry, "write"),
    );
    if (denied) {
      Alert.alert("Files", `${denied.name} はコピー・移動できません。`);
      return;
    }

    const clipboardAtStart = clipboard;
    filesBatchUploadLockRef.current = "clipboard";
    clipboardTransferRef.current = true;
    setTransferring(true);
    const requestLocation = {
      source,
      scope,
      path: activePath,
      destinationPath,
      authScope,
      projectId: selectedProjectId,
    };
    const succeeded: FilesEntry[] = [];
    const failures: Array<{ entry: FilesEntry; error: unknown }> = [];
    try {
      if (
        clipboard.source === "local" &&
        source === "server" &&
        clipboard.operation === "copy"
      ) {
        const uploadResult = await uploadLocalClipboardFiles(
          clipboard.entries,
          destinationPath,
          (uploadSource, parentPath, asset, uploadOptions) =>
            uploadOptions?.overwrite === true
              ? filesApi.upload(uploadSource, parentPath, asset, {
                  overwrite: true,
                })
              : filesApi.upload(uploadSource, parentPath, asset),
          {
            confirmConflicts: (conflicts) =>
              confirmFilesUploadConflicts(conflicts, requestLocation),
            isCurrent: () => isFilesUploadLocationCurrent(requestLocation),
          },
        );
        const currentLocation = activeLocationRef.current;
        const sameContext =
          currentLocation.source === requestLocation.source &&
          currentLocation.scope === requestLocation.scope &&
          currentLocation.authScope === requestLocation.authScope &&
          currentLocation.projectId === requestLocation.projectId;

        // The destination cache belongs to Server, not to the local clipboard
        // source. Invalidate it even if the user navigates during the upload.
        filesLocationCache.invalidate({
          source: requestLocation.source,
          scope: requestLocation.scope,
          authScope: requestLocation.authScope,
          projectId: requestLocation.projectId,
          path: destinationPath || undefined,
        });
        if (
          sameContext &&
          (currentLocation.path === requestLocation.path ||
            currentLocation.path === destinationPath)
        ) {
          await loadEntries(
            requestLocation.source,
            requestLocation.scope,
            currentLocation.path,
            requestLocation.projectId,
            { revalidate: true },
          );
        }
        const uploadAlert = createFilesBatchUploadAlert(uploadResult);
        if (uploadAlert) {
          Alert.alert(uploadAlert.title, uploadAlert.message);
        }
        return;
      }

      // Existing Files APIs are single-entry operations. Execute them in a
      // deterministic sequence and retain explicit partial-failure state.
      for (const entry of clipboard.entries) {
        try {
          if (clipboard.operation === "copy") {
            await filesApi.copy(entry.source, entry.path, destinationPath);
          } else {
            await filesApi.move(entry.source, entry.path, destinationPath);
          }
          succeeded.push(entry);
        } catch (error) {
          failures.push({ entry, error });
        }
      }
      const currentLocation = activeLocationRef.current;
      const sameContext =
        currentLocation.source === requestLocation.source &&
        currentLocation.scope === requestLocation.scope &&
        currentLocation.authScope === requestLocation.authScope &&
        currentLocation.projectId === requestLocation.projectId;
      const operationLocation = {
        source: clipboard.source,
        scope: clipboard.scope,
        authScope: clipboard.authScope ?? requestLocation.authScope,
        projectId: clipboard.projectId ?? requestLocation.projectId,
      };
      if (
        clipboard.operation === "move" &&
        clipboardRef.current === clipboardAtStart
      ) {
        setClipboard(
          failures.length > 0
            ? { ...clipboard, entries: failures.map(({ entry }) => entry) }
            : null,
        );
      }
      if (sameContext) {
        // A move affects the captured source parent as well as the
        // destination.  Invalidate every affected directory before any
        // subsequent navigation can reuse an old memory/SQLite listing.
        for (const affectedPath of getClipboardAffectedPaths(
          clipboard,
          destinationPath,
        )) {
          filesLocationCache.invalidate({
            ...operationLocation,
            path: affectedPath || undefined,
          });
        }
        if (currentLocation.path === requestLocation.path) {
          await loadEntries(
            requestLocation.source,
            requestLocation.scope,
            requestLocation.path,
            requestLocation.projectId,
            { revalidate: true },
          );
        } else if (currentLocation.path === destinationPath) {
          await loadEntries(
            requestLocation.source,
            requestLocation.scope,
            destinationPath,
            requestLocation.projectId,
            { revalidate: true },
          );
        }
      }
      if (failures.length > 0) {
        const firstFailure = failures[0].error;
        const detail =
          firstFailure instanceof Error ? ` ${firstFailure.message}` : "";
        Alert.alert(
          "貼り付け結果",
          `${succeeded.length}件を処理しました。${failures.length}件は処理できませんでした。${detail}`,
        );
      }
    } catch (transferError) {
      Alert.alert(
        "Files",
        transferError instanceof Error
          ? transferError.message
          : "貼り付けに失敗しました。",
      );
    } finally {
      if (filesBatchUploadLockRef.current === "clipboard") {
        filesBatchUploadLockRef.current = null;
      }
      clipboardTransferRef.current = false;
      setTransferring(false);
    }
  };

  const selectionMode = fileSelectionCount(selectedEntries) > 0;
  const selectedEntryKeys = useMemo(
    () => new Set(selectedEntries.map(fileEntrySelectionKey)),
    [selectedEntries],
  );
  const allEntriesSelected =
    items.length > 0 &&
    items.every((entry) =>
      selectedEntryKeys.has(fileEntrySelectionKey(entry)),
    );

  const clearSelection = useCallback(() => {
    longPressedEntryKeyRef.current = null;
    setSelectedEntries([]);
  }, []);

  const handleItemPress = useCallback(
    (entry: FilesEntry) => {
      const entryKey = fileEntrySelectionKey(entry);
      if (longPressedEntryKeyRef.current === entryKey) {
        // Suppress the synthetic onPress emitted after onLongPress.
        longPressedEntryKeyRef.current = null;
        return;
      }
      const action = resolveFilesPressAction({
        wasLongPress: false,
        selectionMode: fileSelectionCount(selectedEntries) > 0,
      });
      if (action === "toggle-selection") {
        setSelectedEntries((previous) => toggleFileSelection(previous, entry));
        return;
      }
      if (action === "open") {
        void handleOpenEntry(entry);
      }
    },
    [handleOpenEntry, selectedEntries.length],
  );

  const handleItemLongPress = useCallback(
    (entry: FilesEntry) => {
      const entryKey = fileEntrySelectionKey(entry);
      longPressedEntryKeyRef.current = entryKey;
      const action = resolveFilesPressAction({
        wasLongPress: true,
        selectionMode: fileSelectionCount(selectedEntries) > 0,
      });
      if (action !== "start-selection") return;
      setActionTarget(null);
      setSelectedEntries((previous) => {
        if (
          previous.some(
            (candidate) => fileEntrySelectionKey(candidate) === entryKey,
          )
        ) {
          return previous;
        }
        return previous.length === 0
          ? startFileSelection(entry)
          : [...previous, entry];
      });
    },
    [selectedEntries.length],
  );

  const selectAllEntries = useCallback(() => {
    if (items.length === 0) return;
    setSelectedEntries((previous) => {
      const allSelected = items.every((entry) =>
        previous.some(
          (candidate) =>
            fileEntrySelectionKey(candidate) === fileEntrySelectionKey(entry),
        ),
      );
      return allSelected ? previous : dedupeFileEntries(items);
    });
  }, [items]);

  const showEntryActions = (entry: FilesEntry) => {
    if (selectionMode) return;
    setActionTarget(entry);
  };

  const dismissEntryActions = () => setActionTarget(null);

  const runEntryAction = (action: () => void) => {
    dismissEntryActions();
    action();
  };

  const uploadFile = async () => {
    if (
      !canMutateCurrentPath ||
      uploadingRef.current ||
      filesBatchUploadLockRef.current ||
      uploadConflictWaiterRef.current
    ) return;

    const requestLocation = {
      source,
      scope,
      path: activePath,
      destinationPath: activePath,
      authScope,
      projectId: selectedProjectId,
    };
    filesBatchUploadLockRef.current = "picker";
    uploadingRef.current = true;
    setUploading(true);
    try {
      const picked = await DocumentPicker.getDocumentAsync(
        FILES_DOCUMENT_PICKER_OPTIONS,
      );
      if (picked.canceled || !picked.assets?.length) return;

      // The picker can remain open while the user changes account, project,
      // scope, or folder. Never send a selected asset to a new destination.
      if (!isFilesUploadLocationCurrent(requestLocation)) return;

      const result = await uploadPickedFiles(
        picked.assets,
        (asset, uploadOptions) =>
          uploadOptions?.overwrite === true
            ? filesApi.upload(
                requestLocation.source,
                requestLocation.path,
                asset,
                { overwrite: true },
              )
            : filesApi.upload(
                requestLocation.source,
                requestLocation.path,
                asset,
              ),
        {
          confirmConflicts: (conflicts) =>
            confirmFilesUploadConflicts(conflicts, requestLocation),
          isCurrent: () => isFilesUploadLocationCurrent(requestLocation),
        },
      );

      filesLocationCache.invalidate({
        source: requestLocation.source,
        scope: requestLocation.scope,
        authScope: requestLocation.authScope,
        projectId: requestLocation.projectId,
        path: requestLocation.path || undefined,
      });
      if (isFilesUploadLocationCurrent(requestLocation)) {
        await loadEntries(
          requestLocation.source,
          requestLocation.scope,
          requestLocation.path,
          requestLocation.projectId,
          { revalidate: true },
        );
      }

      const uploadAlert = createFilesBatchUploadAlert(result);
      if (uploadAlert) {
        Alert.alert(uploadAlert.title, uploadAlert.message);
      }
    } catch (uploadError) {
      Alert.alert(
        "Upload failed",
        uploadError instanceof Error
          ? uploadError.message
          : "アップロードに失敗しました。",
      );
    } finally {
      if (filesBatchUploadLockRef.current === "picker") {
        filesBatchUploadLockRef.current = null;
      }
      uploadingRef.current = false;
      setUploading(false);
    }
  };

  const normalizeServerPath = useCallback((path: string) => {
    return path.replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/+$/, "");
  }, []);

  const projectForWorkspacePath = useCallback(
    (path: string) => {
      const normalized = normalizeServerPath(path);
      if (!normalized || !effectiveSpaceId) return null;
      return (
        projects.find((project) => {
          if (project.space_id !== effectiveSpaceId) return false;
          const root = `_projects/project_${project.id}`;
          return normalized === root || normalized.startsWith(`${root}/`);
        }) ?? null
      );
    },
    [effectiveSpaceId, normalizeServerPath, projects],
  );

  const canBookmarkActivePath = useMemo(() => {
    if (!isAuthenticated || !activePath || !bookmarkCollection) return false;
    if (source !== "server" || scope !== "workspace") return true;
    return Boolean(projectForWorkspacePath(activePath));
  }, [activePath, bookmarkCollection, isAuthenticated, projectForWorkspacePath, scope, source]);

  const isBookmarked = useMemo(
    () =>
      canBookmarkActivePath &&
      bookmarks.some((bookmark) => normalizeFileBookmarkPath(bookmark.path) === normalizeFileBookmarkPath(activePath)),
    [activePath, bookmarks, canBookmarkActivePath],
  );

  const toggleBookmark = async () => {
    if (!isAuthenticated || !activePath || !bookmarkCollection) {
      Alert.alert("Files", "ブックマークはログイン中のみ利用できます。");
      return;
    }
    if (!canBookmarkActivePath) {
      Alert.alert(
        "Files",
        "選択中Spaceに属するProject Filesのみブックマークできます。",
      );
      return;
    }
    if (bookmarkMutationRef.current) return;
    bookmarkMutationRef.current = true;
    try {
      if (source === "local") {
        const name = activePath.split(/[\\/]/).filter(Boolean).pop() || "Local";
        await setLocalFileBookmark(authScope, { name, path: activePath }, !isBookmarked);
      } else if (isBookmarked) {
        await filesApi.removeBookmark(activePath, bookmarkCollection);
      } else {
        const name =
          activePath.split(/[\\/]/).filter(Boolean).pop() ||
          currentDisplayPath ||
          "Bookmark";
        await filesApi.addBookmark(
          name,
          activePath,
          "📁",
          bookmarkCollection,
        );
      }
      await loadBookmarks();
    } catch (bookmarkError) {
      Alert.alert(
        "Files",
        bookmarkError instanceof Error
          ? bookmarkError.message
          : "ブックマーク更新に失敗しました。",
      );
    } finally {
      bookmarkMutationRef.current = false;
    }
  };

  const navigateBookmark = useCallback(
    async (bookmark: FilesBookmark) => {
      if (!bookmark.path) return;
      if (source === "server" && scope === "workspace") {
        const targetProject = projectForWorkspacePath(bookmark.path);
        if (!targetProject) {
          Alert.alert(
            "Files",
            "このブックマークのProjectは選択中Spaceから利用できません。",
          );
          return;
        }
        if (targetProject.id !== selectedProjectId) {
          // applyProjectSelection is the canonical ProjectContext bridge.  It
          // clears the old root/history and updates the store before this
          // bookmark path is applied, avoiding a header/path mismatch.
          await applyProjectSelection(targetProject.id);
          setPathForLocation("server", "workspace", bookmark.path);
          return;
        }
      }
      navigateTo(bookmark.path);
    },
    [
      applyProjectSelection,
      navigateTo,
      projectForWorkspacePath,
      scope,
      selectedProjectId,
      setPathForLocation,
      source,
    ],
  );

  const currentDisplayPath = useMemo(() => {
    const prefix = `${SOURCE_LABELS[source]} / ${SCOPE_LABELS[scope]}`;
    const relative =
      source === "server"
        ? formatScopedServerPath(activePath, getServerRootPath(scope))
        : formatDisplayPath(source, activePath, scope);
    if (!relative || relative === "/") return prefix;
    return `${prefix}${relative}`;
  }, [activePath, getServerRootPath, scope, source]);

  // 候補は参加権限で絞り込み済みの全Project。現在のSpaceで再度絞ると
  // 一度選択した後に別Spaceへ切り替えられなくなる。
  const showProjectSelector = isAuthenticated && source === "server" && scope === "workspace";

  const filteredItems = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return items;
    return items.filter((item) => item.name.toLowerCase().includes(keyword));
  }, [items, query]);

  const viewableFiles = useMemo(
    () => filteredItems.filter(isViewableMedia),
    [filteredItems],
  );
  const viewerIndex = useMemo(
    () =>
      viewerFile
        ? viewableFiles.findIndex((entry) => entry.path === viewerFile.path)
        : -1,
    [viewableFiles, viewerFile],
  );

  const navigateViewer = (delta: number) => {
    if (viewerIndex < 0) return;
    const next = viewableFiles[viewerIndex + delta];
    if (next) setViewerFile(next);
  };

  const viewerMediaKey = useCallback(
    (entry: FilesEntry) => `${entry.source}:${entry.path}`,
    [],
  );

  const getViewerImageSource = useCallback(
    async (entry: FilesEntry): Promise<MediaSource> => {
      const key = viewerMediaKey(entry);
      const cached = viewerImageSourceCacheRef.current.get(key);
      if (cached) return cached;

      const running = viewerImageSourceFlightRef.current.get(key);
      if (running) return running;

      const flight = filesApi
        .getMediaSource(entry)
        .then((sourceValue) => {
          viewerImageSourceCacheRef.current.set(key, sourceValue);
          return sourceValue;
        })
        .finally(() => {
          if (viewerImageSourceFlightRef.current.get(key) === flight) {
            viewerImageSourceFlightRef.current.delete(key);
          }
        });
      viewerImageSourceFlightRef.current.set(key, flight);
      return flight;
    },
    [viewerMediaKey],
  );

  const preloadViewerImage = useCallback(
    async (entry: FilesEntry): Promise<MediaSource> => {
      const key = viewerMediaKey(entry);
      const uri = await filesApi.getPlayableUri(entry);
      const sourceValue: MediaSource = { uri };
      viewerImageSourceCacheRef.current.set(key, sourceValue);
      return sourceValue;
    },
    [viewerMediaKey],
  );

  useEffect(() => {
    let cancelled = false;
    setViewerError(null);
    setVideoUri(null);

    if (!viewerVisible || !viewerFile) {
      setViewerLoading(false);
      setVideoLoading(false);
      setViewerPreloadSources({});
      return;
    }

    const currentKey = viewerMediaKey(viewerFile);
    const neighbors = [viewableFiles[viewerIndex - 1], viewableFiles[viewerIndex + 1]]
      .filter((entry): entry is FilesEntry => Boolean(entry))
      .filter((entry) => getFilesMediaKind(entry) === "image");

    const wantedPreloadKeys = new Set(
      neighbors.map((entry) => viewerMediaKey(entry)),
    );
    setViewerPreloadSources((previous) =>
      Object.fromEntries(
        Object.entries(previous).filter(([key]) => wantedPreloadKeys.has(key)),
      ),
    );

    for (const entry of neighbors) {
      const key = viewerMediaKey(entry);
      void preloadViewerImage(entry)
        .then((sourceValue) => {
          if (cancelled) return;
          setViewerPreloadSources((previous) => ({
            ...previous,
            [key]: sourceValue,
          }));
        })
        .catch(() => {
          // Preload failures must not block the current image. If the user
          // navigates there, the normal viewer load surfaces the real error.
        });
    }

    const kind = getFilesMediaKind(viewerFile);
    if (kind === "image") {
      setVideoLoading(false);
      const cached = viewerImageSourceCacheRef.current.get(currentKey);
      if (cached) {
        setViewerSource(cached);
        setViewerLoading(!viewerImageReadyRef.current.has(currentKey));
      } else {
        setViewerSource(null);
        setViewerLoading(true);
      }

      void getViewerImageSource(viewerFile)
        .then((sourceValue) => {
          if (cancelled) return;
          setViewerSource(sourceValue);
          setViewerLoading(!viewerImageReadyRef.current.has(currentKey));
        })
        .catch((error) => {
          if (cancelled) return;
          setViewerLoading(false);
          setViewerError(
            error instanceof Error ? error.message : "画像を表示できませんでした。",
          );
        });
    } else if (kind === "video") {
      setViewerSource(null);
      setViewerLoading(false);
      setVideoLoading(true);
      void filesApi
        .getPlayableUri(viewerFile)
        .then((uri) => {
          if (!cancelled) setVideoUri(uri);
        })
        .catch((error) => {
          if (!cancelled) {
            setViewerError(
              error instanceof Error ? error.message : "動画を表示できませんでした。",
            );
          }
        })
        .finally(() => {
          if (!cancelled) setVideoLoading(false);
        });
    }

    return () => {
      cancelled = true;
    };
  }, [
    getViewerImageSource,
    preloadViewerImage,
    viewerFile,
    viewerIndex,
    viewerMediaKey,
    viewerVisible,
    viewableFiles,
  ]);

  useEffect(() => {
    if (viewerVisible) return;
    setViewerSource(null);
    setViewerPreloadSources({});
    setViewerLoading(false);
    setVideoLoading(false);
    setVideoUri(null);
    viewerImageReadyRef.current.clear();
  }, [viewerVisible]);

  const handleVideoFullscreenUpdate = async (
    event: VideoFullscreenUpdateEvent,
  ) => {
    try {
      if (
        event.fullscreenUpdate === VideoFullscreenUpdate.PLAYER_WILL_PRESENT
      ) {
        await ScreenOrientation.lockAsync(
          ScreenOrientation.OrientationLock.LANDSCAPE,
        );
      } else if (
        event.fullscreenUpdate === VideoFullscreenUpdate.PLAYER_DID_DISMISS
      ) {
        await ScreenOrientation.lockAsync(
          ScreenOrientation.OrientationLock.PORTRAIT_UP,
        );
      }
    } catch {
      // Orientation APIs can be unavailable on some devices.
    }
  };

  useEffect(() => {
    if (viewerVisible) return;
    void ScreenOrientation.lockAsync(
      ScreenOrientation.OrientationLock.PORTRAIT_UP,
    ).catch(() => {});
  }, [viewerVisible]);

  useFocusEffect(
    useCallback(() => {
      const subscription = BackHandler.addEventListener(
        "hardwareBackPress",
        () => {
          if (selectionMode) {
            clearSelection();
            return true;
          }
          if (viewerVisible) {
            setViewerVisible(false);
            return true;
          }
          if (activeHistory.length > 0) {
            void goBack();
            return true;
          }
          if (parentPath !== null) {
            void goUp();
            return true;
          }
          return true;
        },
      );
      return () => subscription.remove();
    }, [
      activeHistory.length,
      parentPath,
      clearSelection,
      goBack,
      goUp,
      selectionMode,
      viewerVisible,
    ]),
  );

  const bookmarkRootPath = source === "local" ? homePath : getServerRootPath(scope);
  const bookmarkItems = useMemo(() => visibleFileBookmarks(
    source === "server" && scope === "workspace"
      ? bookmarks.filter((bookmark) => Boolean(projectForWorkspacePath(bookmark.path)))
      : bookmarks,
    { source, scope, rootPath: bookmarkRootPath, projectId: selectedProjectId },
  ), [bookmarkRootPath, bookmarks, projectForWorkspacePath, scope, selectedProjectId, source]);

  const staleSyncedAtLabel = useMemo(() => {
    if (!staleCachedAt) return "";
    const parsed = new Date(staleCachedAt);
    if (Number.isNaN(parsed.getTime())) return "";
    return parsed.toLocaleString();
  }, [staleCachedAt]);

  const actionPasteDestination =
    actionTarget?.type === "directory" ? actionTarget.path : activePath;
  const actionPasteDestinationEntry =
    actionTarget?.type === "directory" ? actionTarget : null;
  const canWriteActionTarget = canMutateEntry(actionTarget, "write");
  const canDeleteActionTarget = canMutateEntry(actionTarget, "delete");
  const canWritePasteDestination =
    actionTarget?.type === "directory"
      ? canMutateEntry(actionTarget, "write", true)
      : canMutateCurrentPath;
  const pasteProjectRoot =
    source === "server" && scope === "workspace"
      ? getServerRootPath("workspace")
      : null;
  const canPasteToActionTarget =
    Boolean(actionTarget) &&
    canPasteClipboardToFilesLocation(
      clipboard,
      {
        source,
        scope,
        path: actionPasteDestination || "",
        authScope,
        projectRoot: pasteProjectRoot,
      },
      canWritePasteDestination,
    );
  const canPasteClipboard = canPasteClipboardToFilesLocation(
    clipboard,
    {
      source,
      scope,
      path: activePath,
      authScope,
      projectRoot: pasteProjectRoot,
    },
    canMutateCurrentPath,
  );

  const renderListItem = ({ item }: { item: FilesEntry }) => {
    const selected = selectedEntryKeys.has(fileEntrySelectionKey(item));
    return (
      <View style={styles.fileItemRow}>
        <Pressable
          style={styles.fileItemPressable}
          onPress={() => handleItemPress(item)}
          onLongPress={() => handleItemLongPress(item)}
          testID={`files-entry-${fileEntrySelectionKey(item)}`}
          accessibilityRole="button"
          accessibilityState={{ selected }}
          accessibilityLabel={selected ? `選択中: ${item.name}` : item.name}
        >
          {({ pressed }) => (
            <Surface
              style={[
                styles.fileItem,
                selected ? styles.fileItemSelected : null,
                pressed
                  ? selected
                    ? styles.fileItemSelectedPressed
                    : styles.fileItemPressed
                  : null,
              ]}
              elevation={0}
            >
              <View style={styles.fileThumbnailWrap}>
                <FileThumbnail entry={item} size={32} />
                {selected ? (
                  <View style={styles.selectionIndicator}>
                    <Text style={styles.selectionIndicatorText}>✓</Text>
                  </View>
                ) : null}
              </View>
              <View
                style={[
                  styles.fileInfo,
                  !selectionMode ? styles.fileInfoWithOverflow : null,
                ]}
              >
                <Text style={styles.fileName} numberOfLines={1}>
                  {item.name}
                </Text>
              </View>
            </Surface>
          )}
        </Pressable>
        {!selectionMode ? (
          <IconButton
            icon="dots-vertical"
            iconColor="#a6adc8"
            size={20}
            style={styles.listOverflowButton}
            testID={`files-entry-actions-${fileEntrySelectionKey(item)}`}
            accessibilityLabel={`${item.name} の操作`}
            onPress={() => showEntryActions(item)}
          />
        ) : null}
      </View>
    );
  };

  const renderGridItem = ({ item }: { item: FilesEntry }) => {
    const selected = selectedEntryKeys.has(fileEntrySelectionKey(item));
    return (
      <View style={styles.gridItemWrap}>
        <Pressable
          style={styles.gridItemPressable}
          onPress={() => handleItemPress(item)}
          onLongPress={() => handleItemLongPress(item)}
          testID={`files-entry-${fileEntrySelectionKey(item)}`}
          accessibilityRole="button"
          accessibilityState={{ selected }}
          accessibilityLabel={selected ? `選択中: ${item.name}` : item.name}
        >
          {({ pressed }) => (
            <Surface
              style={[
                styles.gridItem,
                selected ? styles.gridItemSelected : null,
                pressed
                  ? selected
                    ? styles.fileItemSelectedPressed
                    : styles.fileItemPressed
                  : null,
              ]}
              elevation={0}
            >
              <View style={styles.gridThumbnailWrap}>
                <FileThumbnail entry={item} size={104} />
                {selected ? (
                  <View style={styles.selectionIndicator}>
                    <Text style={styles.selectionIndicatorText}>✓</Text>
                  </View>
                ) : null}
              </View>
              <Text style={styles.gridFileName} numberOfLines={2}>
                {item.name}
              </Text>
              <FileMetadata entry={item} grid />
            </Surface>
          )}
        </Pressable>
        {!selectionMode ? (
          <IconButton
            icon="dots-vertical"
            iconColor="#a6adc8"
            size={18}
            style={styles.gridOverflowButton}
            testID={`files-entry-actions-${fileEntrySelectionKey(item)}`}
            accessibilityLabel={`${item.name} の操作`}
            onPress={() => showEntryActions(item)}
          />
        ) : null}
      </View>
    );
  };

  return (
    <View style={styles.container}>
      <ScreenHeader title="Files" compact />
      <NativeStorageWorkspace
        controls={(storageGear) => (
          <>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.segmentRow} testID="files-location-switcher">
          {(["local", "server"] as FilesSource[]).map((value) => (
            <FilesHeaderChip
              key={value}
              selected={source === value}
              testID={`files-source-${value}`}
              onPress={() => void changeSource(value)}
            >
              {SOURCE_LABELS[value]}
            </FilesHeaderChip>
          ))}
          {source === "server" ? (
            <View style={styles.scopeSegments}>
              {(["workspace", "user"] as FilesScope[]).map((value) => (
                <FilesHeaderChip
                  key={value}
                  selected={scope === value}
                  variant="scope"
                  disabled={!isAuthenticated}
                  testID={`files-scope-${value}`}
                  onPress={() => void changeScope(value)}
                >
                  {SCOPE_LABELS[value]}
                </FilesHeaderChip>
              ))}
            </View>
          ) : null}
          {showProjectSelector ? (
            <View style={styles.projectScopeSlot} testID="files-project-scope-slot">
              <ScopeSwitcher variant="inline" projects={projects} projectId={selectedProjectId}
                onSelectProject={applyProjectSelection} allowAll={isAdmin}
                allLabel={isAdmin ? "管理者ルート" : "プロジェクトを選択"} />
            </View>
          ) : null}
          {storageGear ? <View style={styles.storageGearSlot}>{storageGear}</View> : null}
        </View>
      </Surface>

      {selectionMode ? (
        <View style={[styles.toolbarRow, styles.selectionToolbar]}>
          <IconButton
            style={styles.selectionControl}
            icon="close"
            iconColor="#cdd6f4"
            accessibilityLabel="選択モードを終了"
            testID="files-selection-clear"
            onPress={clearSelection}
            disabled={transferring}
          />
          <Text
            style={styles.selectionCount}
            testID="files-selection-count"
          >
            {fileSelectionCount(selectedEntries)}件選択
          </Text>
          <IconButton
            style={styles.selectionControl}
            icon="content-copy"
            iconColor="#cdd6f4"
            accessibilityLabel="選択項目をコピー"
            testID="files-selection-copy"
            onPress={() => {
              if (setClipboardEntries(selectedEntries, "copy")) {
                clearSelection();
              }
            }}
            disabled={transferring}
          />
          <IconButton
            style={styles.selectionControl}
            icon="file-move-outline"
            iconColor="#cdd6f4"
            accessibilityLabel="選択項目を移動"
            testID="files-selection-move"
            onPress={() => {
              if (setClipboardEntries(selectedEntries, "move")) {
                clearSelection();
              }
            }}
            disabled={transferring}
          />
          <IconButton
            style={styles.selectionControl}
            icon="delete-outline"
            iconColor="#f38ba8"
            accessibilityLabel="選択項目を削除"
            testID="files-selection-delete"
            onPress={deleteSelectedEntries}
            disabled={transferring}
          />
          <IconButton
            style={styles.selectionControl}
            icon="select-all"
            iconColor="#a6adc8"
            accessibilityLabel="すべて選択"
            testID="files-selection-all"
            onPress={selectAllEntries}
            disabled={transferring || items.length === 0 || allEntriesSelected}
          />
        </View>
      ) : (
        <FilesToolbar
          path={currentDisplayPath}
          canGoBack={activeHistory.length > 0}
          canGoUp={parentPath !== null}
          canGoHome={activePath !== homePath}
          onBack={goBack}
          onUp={goUp}
          onHome={goHome}
          createAction={
          <Menu
            visible={createMenuVisible}
            onDismiss={() => setCreateMenuVisible(false)}
            anchor={
              <IconButton
                icon="plus-box-outline"
                size={22}
                style={{width:40,height:40,margin:0}}
                hitSlop={4}
                iconColor="#89b4fa"
                disabled={!canMutateCurrentPath}
                onPress={() => setCreateMenuVisible(true)}
                testID="files-create-menu"
                accessibilityLabel="新規作成"
                accessibilityHint="新しいファイル、フォルダーの作成またはアップロード"
              />
            }
          >
            <Menu.Item
              leadingIcon="file-plus-outline"
              title="新しいテキストファイル"
              onPress={openCreateFileDialog}
              accessibilityLabel="新しいテキストファイル"
            />
            <Menu.Item
              leadingIcon="folder-plus-outline"
              title="新しいフォルダー"
              onPress={openCreateFolderDialog}
              testID="files-create-folder"
              accessibilityLabel="新しいフォルダー"
            />
            <Menu.Item
              leadingIcon="upload"
              title="アップロード"
              onPress={openUploadPicker}
              disabled={uploading}
              accessibilityLabel="アップロード"
            />
          </Menu>
          }
          actions={[
            { id: "files-search-toggle", icon: "magnify", title: searchVisible ? "検索を閉じる" : "検索",
              onPress: () => { setSearchVisible(!searchVisible); if (searchVisible) setQuery(""); } },
            { id: "files-bookmarks", icon: "bookmark-multiple-outline", title: "ブックマーク",
              onPress: () => setBookmarksVisible(true) },
            { id: "files-bookmark-toggle", icon: isBookmarked ? "star" : "star-outline",
              title: isBookmarked ? "現在地のブックマークを解除" : "現在地をブックマーク",
              disabled: !canBookmarkActivePath, onPress: () => void toggleBookmark() },
            { id: "files-view-toggle", icon: viewMode === "grid" ? "format-list-bulleted" : "view-grid-outline",
              title: viewMode === "grid" ? "リスト表示" : "グリッド表示",
              onPress: () => setViewMode(viewMode === "grid" ? "list" : "grid") },
            { id: "files-paste", icon: "clipboard-arrow-down-outline", title: "貼り付け",
              disabled: !canPasteClipboard || transferring, onPress: () => void pasteClipboard() },
          ]}
        />
      )}

      {source === "server" && (isOffline || staleActive) ? (
        <View style={styles.offlineBanner}>
          <IconButton
            icon="cloud-off-outline"
            iconColor="#f9e2af"
            size={16}
            style={styles.offlineBannerIcon}
          />
          <Text style={styles.offlineBannerText} numberOfLines={1}>
            {staleSyncedAtLabel
              ? `オフライン（最終同期: ${staleSyncedAtLabel}）`
              : "オフライン"}
          </Text>
        </View>
      ) : null}

      {error ? (
        <Text accessibilityRole="alert" style={styles.errorBanner}>
          {error}
        </Text>
      ) : null}

      {searchVisible ? (
        <View style={styles.searchRow}>
          <ThemedTextInput
            mode="outlined"
            dense
            placeholder="Search"
            value={query}
            cursorColor="#ffffff"
            onChangeText={setQuery}
            style={styles.searchInput}
            right={
              query ? (
                <TextInput.Icon icon="close" onPress={() => setQuery("")} />
              ) : undefined
            }
          />
        </View>
      ) : null}

      {audioState.track ? (
        <Surface style={styles.audioBar} elevation={3}>
          <Pressable
            style={styles.audioInfo}
            accessibilityRole="button"
            onPress={() => void focusAudioTrackLocation()}
          >
            <Text style={styles.audioTitle} numberOfLines={1}>
              {audioState.track.name}
            </Text>
            <Text style={styles.audioTime}>
              {formatTime(audioState.positionMillis)} /{" "}
              {formatTime(audioState.durationMillis)}
            </Text>
            <ProgressBar
              progress={
                audioState.durationMillis
                  ? audioState.positionMillis / audioState.durationMillis
                  : 0
              }
              color="#a78bfa"
              style={styles.audioProgress}
            />
          </Pressable>
          <IconButton
            icon="skip-previous"
            iconColor="#cdd6f4"
            disabled={audioState.index <= 0 || audioState.loading}
            onPress={() => void previousAudio()}
          />
          <IconButton
            icon={audioState.playing ? "pause" : "play"}
            iconColor="#ffffff"
            containerColor="#7c3aed"
            disabled={audioState.loading}
            onPress={() => void toggleAudio()}
          />
          <IconButton
            icon="skip-next"
            iconColor="#cdd6f4"
            disabled={
              audioState.index < 0 ||
              audioState.index >= audioState.playlist.length - 1 ||
              audioState.loading
            }
            onPress={() => void nextAudio()}
          />
          <IconButton
            icon="close"
            iconColor="#a6adc8"
            onPress={() => void stopAudio()}
          />
        </Surface>
      ) : null}
          </>
        )}
      >

      <FullScreenModalShell
        visible={bookmarksVisible}
        title="ブックマーク"
        onClose={() => setBookmarksVisible(false)}
        testID="files-bookmarks-modal"
      >
        <Text style={styles.bookmarkPath}>
          {source === "local" ? "Local" : scope === "workspace" ? selectedProject?.name || "プロジェクト未選択" : "User"}
        </Text>
        {bookmarkItems.length === 0 ? (
          <Text>この領域にはブックマークがありません。</Text>
        ) : bookmarkItems.map((bookmark) => (
          <Pressable
            key={normalizeFileBookmarkPath(bookmark.path)}
            accessibilityRole="button"
            accessibilityLabel={`${bookmark.name}: ${fileBookmarkRelativePath(bookmark.path, bookmarkRootPath)}`}
            style={styles.bookmarkItem}
            onPress={() => { setBookmarksVisible(false); void navigateBookmark(bookmark); }}
          >
            <Text style={styles.bookmarkName}>{bookmark.name}</Text>
            <Text style={styles.bookmarkPath}>{fileBookmarkRelativePath(bookmark.path, bookmarkRootPath)}</Text>
          </Pressable>
        ))}
      </FullScreenModalShell>

      {loading ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color="#7c3aed" />
        </View>
      ) : (
        <FlatList
          testID="files-list"
          style={styles.list}
          key={viewMode}
          data={filteredItems}
          keyExtractor={(item) => `${item.source}:${item.path}`}
          renderItem={viewMode === "grid" ? renderGridItem : renderListItem}
          numColumns={viewMode === "grid" ? 3 : 1}
          ItemSeparatorComponent={
            viewMode === "list" ? () => <Divider style={styles.divider} /> : undefined
          }
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={onRefresh}
              tintColor="#7c3aed"
            />
          }
          ListEmptyComponent={
            <View style={styles.center}>
              <Text style={styles.emptyText}>
                この場所にはまだファイルがありません。
              </Text>
            </View>
          }
          contentContainerStyle={
            filteredItems.length === 0
              ? { flexGrow: 1 }
              : viewMode === "grid"
                ? styles.gridContent
                : undefined
          }
        />
      )}

      <Portal>
        <Dialog
          visible={Boolean(actionTarget)}
          onDismiss={dismissEntryActions}
          style={styles.actionDialog}
        >
          <Dialog.Title style={styles.dialogTitle} numberOfLines={1}>
            {actionTarget?.name || "ファイル操作"}
          </Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogLabel} numberOfLines={1}>
              {staleActive
                ? "オフライン表示中のため、開くのみ利用できます。"
                : actionTarget?.type === "directory"
                  ? "フォルダー"
                  : actionTarget?.mimeType || "ファイル"}
            </Text>
          </Dialog.Content>
          {actionTarget ? (
            <Dialog.ScrollArea style={styles.actionScrollArea}>
              <ScrollView contentContainerStyle={styles.actionList}>
                <Button
                  icon="folder-open-outline"
                  mode="text"
                  textColor="#cdd6f4"
                  contentStyle={styles.actionButtonContent}
                  labelStyle={styles.actionButtonLabel}
                  onPress={() =>
                    runEntryAction(() => void handleOpenEntry(actionTarget))
                  }
                >
                  開く
                </Button>
                {!staleActive && actionTarget.type === "file" ? (
                  <Button
                    icon="download"
                    mode="text"
                    textColor="#cdd6f4"
                    contentStyle={styles.actionButtonContent}
                    labelStyle={styles.actionButtonLabel}
                    loading={downloadingPath === actionTarget.path}
                    disabled={Boolean(downloadingPath)}
                    onPress={() =>
                      runEntryAction(() => void downloadEntry(actionTarget))
                    }
                  >
                    ダウンロード
                  </Button>
                ) : null}
                {!staleActive ? (
                  <>
                    <Button
                      icon="rename-box-outline"
                      mode="text"
                      textColor="#cdd6f4"
                      contentStyle={styles.actionButtonContent}
                      labelStyle={styles.actionButtonLabel}
                      disabled={!canWriteActionTarget || transferring}
                      onPress={() =>
                        runEntryAction(() => {
                          setRenameTarget(actionTarget);
                          setRenameVisible(true);
                        })
                      }
                    >
                      名前を変更
                    </Button>
                    <Button
                      icon="content-copy"
                      mode="text"
                      textColor="#cdd6f4"
                      contentStyle={styles.actionButtonContent}
                      labelStyle={styles.actionButtonLabel}
                      disabled={!canWriteActionTarget || transferring}
                      onPress={() =>
                        runEntryAction(() =>
                          setClipboardEntry(actionTarget, "copy"),
                        )
                      }
                    >
                      コピー
                    </Button>
                    <Button
                      icon="file-move-outline"
                      mode="text"
                      textColor="#cdd6f4"
                      contentStyle={styles.actionButtonContent}
                      labelStyle={styles.actionButtonLabel}
                      disabled={!canWriteActionTarget || transferring}
                      onPress={() =>
                        runEntryAction(() =>
                          setClipboardEntry(actionTarget, "move"),
                        )
                      }
                    >
                      移動
                    </Button>
                    {canPasteToActionTarget ? (
                      <Button
                        icon="content-paste"
                        mode="text"
                        textColor="#cdd6f4"
                        contentStyle={styles.actionButtonContent}
                        labelStyle={styles.actionButtonLabel}
                        disabled={transferring}
                        onPress={() =>
                          runEntryAction(
                            () =>
                              void pasteClipboard(
                                actionPasteDestination,
                                actionPasteDestinationEntry,
                              ),
                          )
                        }
                      >
                        {actionTarget.type === "directory"
                          ? "このフォルダーへ貼り付け"
                          : "ここに貼り付け"}
                      </Button>
                    ) : null}
                    <Button
                      icon="delete-outline"
                      mode="text"
                      textColor="#f38ba8"
                      contentStyle={styles.actionButtonContent}
                      labelStyle={styles.actionButtonLabel}
                      disabled={!canDeleteActionTarget || transferring}
                      onPress={() =>
                        runEntryAction(() => deleteEntry(actionTarget))
                      }
                    >
                      削除
                    </Button>
                  </>
                ) : null}
              </ScrollView>
            </Dialog.ScrollArea>
          ) : null}
          <Dialog.Actions>
            <Button onPress={dismissEntryActions} textColor="#a6adc8">
              キャンセル
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={Boolean(uploadConflictDialog)}
          onDismiss={() => settleUploadConflict("skip")}
          style={styles.uploadConflictDialog}
          testID="files-upload-conflict-dialog"
        >
          <Dialog.Title style={styles.dialogTitle}>
            同名ファイルを一括確認
          </Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogLabel}>
              {uploadConflictDialog?.conflicts.length ?? 0}件の同名ファイルが見つかりました。すべて上書きするか、すべてスキップしてください。
            </Text>
          </Dialog.Content>
          <Dialog.ScrollArea style={styles.uploadConflictScrollArea}>
            <ScrollView contentContainerStyle={styles.uploadConflictList}>
              {uploadConflictDialog?.conflicts.map((conflict) => (
                <Text
                  key={`${conflict.index}:${conflict.name}`}
                  style={styles.uploadConflictName}
                  numberOfLines={2}
                >
                  {conflict.name}
                </Text>
              ))}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button
              textColor="#a6adc8"
              onPress={() => settleUploadConflict("skip")}
              disabled={!uploadConflictWaiterRef.current}
            >
              すべてスキップ
            </Button>
            <Button
              textColor="#f38ba8"
              onPress={() => settleUploadConflict("overwrite")}
              disabled={!uploadConflictWaiterRef.current}
            >
              すべて上書き
            </Button>
          </Dialog.Actions>
        </Dialog>

        <FileNameDialog
          visible={createFileVisible}
          title="テキストファイルを作成"
          label="ファイル名"
          helperText={currentDisplayPath}
          submitLabel="作成"
          onDismiss={() => setCreateFileVisible(false)}
          onSubmit={createTextFile}
        />

        <FileNameDialog
          visible={createFolderVisible}
          title="フォルダーを作成"
          label="フォルダー名"
          helperText={currentDisplayPath}
          submitLabel="作成"
          onDismiss={dismissCreateFolderDialog}
          onSubmit={createFolder}
        />

        <FileNameDialog
          visible={renameVisible}
          title="名前を変更"
          label="新しい名前"
          helperText={renameTarget?.name}
          initialValue={renameTarget?.name || ""}
          submitLabel="保存"
          onDismiss={() => setRenameVisible(false)}
          onSubmit={submitRename}
        />

      </Portal>

      <Modal
        visible={viewerVisible}
        animationType="fade"
        onRequestClose={() => setViewerVisible(false)}
      >
        <View style={styles.viewerContainer}>
          <View style={styles.viewerBody}>
            {viewerError ? (
              <Text style={styles.viewerError}>{viewerError}</Text>
            ) : viewerFile && getFilesMediaKind(viewerFile) === "image" ? (
              <>
                {viewerSource ? (
                  <ZoomableImage
                    source={viewerSource}
                    onLoad={() => {
                      const key = viewerMediaKey(viewerFile);
                      viewerImageReadyRef.current.add(key);
                      setViewerLoading(false);
                    }}
                    onError={() => {
                      setViewerLoading(false);
                      setViewerError("画像を表示できませんでした。");
                    }}
                    onSingleTap={() => setViewerChromeVisible((visible) => !visible)}
                    onSwipeLeft={() => navigateViewer(1)}
                    onSwipeRight={() => navigateViewer(-1)}
                    onSwipeDown={() => setViewerVisible(false)}
                  />
                ) : null}
                {viewerLoading ? (
                  <View pointerEvents="none" style={styles.viewerLoadingOverlay}>
                    <ActivityIndicator size="large" color="#a78bfa" />
                  </View>
                ) : null}
              </>
            ) : viewerFile && getFilesMediaKind(viewerFile) === "video" && videoUri ? (
              <Video
                ref={videoRef}
                source={{ uri: videoUri }}
                style={styles.viewerVideo}
                resizeMode={ResizeMode.CONTAIN}
                useNativeControls
                shouldPlay
                onFullscreenUpdate={(event) => {
                  void handleVideoFullscreenUpdate(event);
                }}
              />
            ) : videoLoading ? (
              <ActivityIndicator size="large" color="#a78bfa" />
            ) : (
              <Text style={styles.viewerError}>プレビューを表示できません。</Text>
            )}

            <View
              pointerEvents="none"
              accessibilityElementsHidden
              importantForAccessibility="no-hide-descendants"
              style={styles.viewerPreloadLayer}
            >
              {Object.entries(viewerPreloadSources).map(([key, sourceValue]) => (
                <Image
                  key={key}
                  source={sourceValue}
                  resizeMode="contain"
                  style={styles.viewerPreloadImage}
                  onLoad={() => {
                    viewerImageReadyRef.current.add(key);
                  }}
                />
              ))}
            </View>
          </View>

          {viewerChromeVisible ? (
            <>
              <SafeAreaView
                edges={["top", "left", "right"]}
                style={styles.viewerHeader}
              >
                <Text style={styles.viewerTitle}>
                  {viewerFile?.name || ""}
                </Text>
                <IconButton
                  icon="close"
                  iconColor="#ffffff"
                  style={styles.viewerClose}
                  onPress={() => setViewerVisible(false)}
                />
              </SafeAreaView>

              <SafeAreaView
                edges={["bottom", "left", "right"]}
                style={styles.viewerFooter}
              >
                <IconButton
                  icon="chevron-left"
                  iconColor="#ffffff"
                  disabled={viewerIndex <= 0}
                  onPress={() => navigateViewer(-1)}
                />
                <Text style={styles.viewerCount}>
                  {viewerIndex >= 0 ? `${viewerIndex + 1} / ${viewableFiles.length}` : ""}
                </Text>
                <IconButton
                  icon="chevron-right"
                  iconColor="#ffffff"
                  disabled={viewerIndex < 0 || viewerIndex >= viewableFiles.length - 1}
                  onPress={() => navigateViewer(1)}
                />
              </SafeAreaView>
            </>
          ) : null}
        </View>
      </Modal>
      </NativeStorageWorkspace>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  header: {
    paddingHorizontal: 8,
    paddingVertical: 2,
    backgroundColor: "#1e1e2e",
  },
  segmentRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    alignItems: "center",
    gap: 4,
    minHeight: 36,
  },
  storageGearSlot: { marginLeft: "auto" },
  // 折り返し行の残り幅へ押し込まれると1文字ずつ縦に折り返すため、
  // 最小幅を確保して入らない場合は行ごと次段へ送り、残り幅を占有させる。
  projectScopeSlot: {
    flexGrow: 1,
    flexShrink: 1,
    flexBasis: 140,
    minWidth: 120,
    height: 36,
    justifyContent: "center",
    backgroundColor: "#313244",
    borderRadius: 14,
  },
  scopeSegments: { flexDirection: "row", flexWrap: "wrap", gap: 4, marginLeft: 2 },
  list: { flex: 1, minHeight: 0 },
  segmentChip: {
    maxWidth: "100%",
    minHeight: 32,
    flexShrink: 1,
    justifyContent: "center",
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 16,
    backgroundColor: "#313244",
  },
  segmentChipActive: { backgroundColor: "#4c1d95" },
  scopeChipActive: { backgroundColor: "#3b2f5f" },
  segmentChipDisabled: { opacity: 0.5 },
  segmentChipText: {
    color: "#cdd6f4",
    fontSize: 12,
    flexShrink: 1,
    flexWrap: "wrap",
    textAlign: "center",
  },
  toolbarRow: {
    flexDirection: "row",
    alignItems: "center",
    minHeight: 40,
    paddingHorizontal: 4,
    backgroundColor: "#181825",
  },
  selectionToolbar: { paddingHorizontal: 2, flexWrap: "wrap", gap: 2 },
  selectionControl: { width: 40, height: 40, margin: 0 },
  selectionCount: {
    color: "#cdd6f4",
    fontSize: 14,
    fontWeight: "600",
    flex: 1,
    minWidth: 0,
  },
  searchRow: {
    paddingHorizontal: 12,
    paddingVertical: 8,
    backgroundColor: "#181825",
  },
  searchInput: { backgroundColor: "#1e1e2e" },
  bookmarkItem: { paddingVertical: 12, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: "#313244" },
  bookmarkName: { color: "#cdd6f4", fontSize: 16 },
  bookmarkPath: { color: "#a6adc8", fontSize: 12, marginTop: 4 },
  offlineBanner: {
    flexDirection: "row",
    alignItems: "center",
    paddingLeft: 8,
    paddingRight: 12,
    paddingVertical: 4,
    backgroundColor: "#2a2418",
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#4d3f1a",
  },
  offlineBannerIcon: { margin: 0 },
  offlineBannerText: { color: "#f9e2af", fontSize: 12, flex: 1 },
  errorBanner: {
    paddingHorizontal: 12,
    paddingVertical: 8,
    color: "#f38ba8",
    backgroundColor: "#2a1c28",
  },
  fileItem: {
    flexDirection: "row",
    alignItems: "center",
    minHeight: 48,
    paddingVertical: 4,
    paddingHorizontal: 8,
    backgroundColor: "#11111b",
  },
  fileItemSelected: {
    backgroundColor: "#2b2050",
    borderWidth: 1,
    borderColor: "#8b5cf6",
  },
  fileItemPressed: { backgroundColor: "#181825" },
  fileItemSelectedPressed: { backgroundColor: "#3c2c68" },
  fileItemRow: { position: "relative" },
  fileItemPressable: { width: "100%" },
  fileInfoWithOverflow: { marginRight: 40 },
  listOverflowButton: {
    position: "absolute",
    top: 0,
    width: 48,
    height: 48,
    right: 0,
    margin: 0,
    zIndex: 2,
  },
  fileThumbnailWrap: { position: "relative" },
  selectionIndicator: {
    position: "absolute",
    top: -2,
    right: -2,
    width: 22,
    height: 22,
    borderRadius: 11,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "#7c3aed",
    borderWidth: 2,
    borderColor: "#cdd6f4",
  },
  selectionIndicatorText: {
    color: "#ffffff",
    fontSize: 13,
    lineHeight: 16,
    fontWeight: "700",
  },
  fileIcon: { margin: 0, marginRight: 10 },
  fileInfo: { flex: 1, minWidth: 0, marginLeft: 10 },
  fileName: { color: "#cdd6f4", fontSize: 15 },
  gridContent: { padding: 8 },
  gridItemWrap: { width: "33.333%", padding: 4, position: "relative" },
  gridItemPressable: { width: "100%" },
  gridItem: {
    minHeight: 168,
    alignItems: "center",
    padding: 8,
    borderRadius: 8,
    backgroundColor: "#11111b",
  },
  gridItemSelected: {
    backgroundColor: "#2b2050",
    borderWidth: 1,
    borderColor: "#8b5cf6",
  },
  gridThumbnailWrap: {
    width: "100%",
    alignItems: "center",
    position: "relative",
  },
  gridOverflowButton: {
    position: "absolute",
    top: 0,
    right: 0,
    margin: 0,
    zIndex: 2,
  },
  gridFileName: {
    color: "#cdd6f4",
    fontSize: 12,
    lineHeight: 16,
    marginTop: 8,
    textAlign: "center",
  },
  divider: { backgroundColor: "#313244", marginHorizontal: 12 },
  center: {
    flex: 1,
    justifyContent: "center",
    alignItems: "center",
    padding: 24,
  },
  emptyText: { color: "#585b70", fontSize: 14, textAlign: "center" },
  actionDialog: { backgroundColor: "#1e1e2e", maxHeight: "92%" },
  actionScrollArea: { maxHeight: 390, paddingHorizontal: 0 },
  actionList: { gap: 2, paddingVertical: 4 },
  uploadConflictDialog: { backgroundColor: "#1e1e2e", maxHeight: "92%" },
  uploadConflictScrollArea: { maxHeight: 360, paddingHorizontal: 0 },
  uploadConflictList: { gap: 8, paddingHorizontal: 24, paddingVertical: 8 },
  uploadConflictName: { color: "#cdd6f4", fontSize: 13, lineHeight: 18 },
  actionButtonContent: { minHeight: 42, justifyContent: "flex-start" },
  actionButtonLabel: { flexGrow: 1, textAlign: "left" },
  dialogTitle: { color: "#cdd6f4" },
  dialogLabel: { color: "#a6adc8", fontSize: 12, marginBottom: 12 },
  audioBar: {
    flexDirection: "row",
    alignItems: "center",
    gap: 2,
    paddingLeft: 12,
    paddingRight: 4,
    paddingVertical: 6,
    backgroundColor: "#1e1e2e",
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
  },
  audioInfo: { flex: 1 },
  audioTitle: { color: "#cdd6f4", fontSize: 13, fontWeight: "600" },
  audioTime: { color: "#a6adc8", fontSize: 10, marginTop: 2 },
  audioProgress: { height: 3, borderRadius: 2, marginTop: 5 },
  viewerContainer: { flex: 1, backgroundColor: "#050509" },
  viewerHeader: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    zIndex: 4,
    minHeight: 56,
    paddingTop: 10,
    paddingBottom: 10,
    paddingHorizontal: 8,
    flexDirection: "row",
    alignItems: "flex-start",
    backgroundColor: "rgba(5, 5, 9, 0.72)",
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#313244",
  },
  viewerTitle: {
    flex: 1,
    minWidth: 0,
    flexShrink: 1,
    color: "#ffffff",
    fontSize: 14,
    lineHeight: 20,
    fontWeight: "600",
    paddingVertical: 4,
  },
  viewerClose: { width: 48, height: 48, margin: 0, flexShrink: 0 },
  viewerBody: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  viewerVideo: { width: "100%", height: "100%" },
  viewerError: { color: "#fca5a5", fontSize: 14, textAlign: "center" },
  viewerLoadingOverlay: {
    ...StyleSheet.absoluteFillObject,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "rgba(5, 5, 9, 0.32)",
  },
  viewerPreloadLayer: {
    position: "absolute",
    width: 1,
    height: 1,
    left: 0,
    top: 0,
    opacity: 0.001,
    overflow: "hidden",
  },
  viewerPreloadImage: { width: 1, height: 1 },
  viewerFooter: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    zIndex: 4,
    minHeight: 64,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "rgba(5, 5, 9, 0.72)",
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
  },
  viewerCount: { minWidth: 88, color: "#cdd6f4", textAlign: "center" },
});

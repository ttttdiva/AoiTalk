import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  BackHandler,
  FlatList,
  Image,
  type LayoutChangeEvent,
  Modal,
  PanResponder,
  Pressable,
  RefreshControl,
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
  Chip,
  Dialog,
  Divider,
  IconButton,
  Portal,
  ProgressBar,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { useFocusEffect } from "expo-router";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import {
  filesApi,
  formatDisplayPath,
  getFilesMediaKind,
  type FilesBookmark,
  type FilesEntry,
  type FilesScope,
  type FilesSource,
  getParentPath,
  isTextEntry,
} from "../../../lib/files-api";
import {
  DEFAULT_AUDIO_PLAYER_SETTINGS,
  loadAudioPlayerSettings,
  type AudioPlayerSettings,
} from "../../../lib/audio-player-settings";

const SOURCE_LABELS: Record<FilesSource, string> = {
  local: "ローカル",
  server: "サーバー",
};

const SCOPE_LABELS: Record<FilesScope, string> = {
  workspace: "ワークスペース",
  user: "ユーザー",
};

const FILE_ICONS: Record<string, string> = {
  directory: "folder",
  image: "file-image-outline",
  video: "file-video-outline",
  audio: "file-music-outline",
  pdf: "file-pdf-box",
  text: "file-document-edit-outline",
  default: "file-outline",
};

type LocationKey = `${FilesSource}:${FilesScope}`;
type LocationState = Record<LocationKey, string>;
type HistoryState = Record<LocationKey, string[]>;
type LocationMeta = {
  parentPath: string | null;
  canGoUp: boolean;
  isAdminMode: boolean;
};
type LocationMetaState = Record<LocationKey, LocationMeta>;
type ClipboardOperation = "copy" | "move";
type ClipboardState = {
  operation: ClipboardOperation;
  entry: FilesEntry;
};
type ViewMode = "grid" | "list";
type MediaSource = Awaited<ReturnType<typeof filesApi.getMediaSource>>;
type AudioState = {
  track: FilesEntry | null;
  playlist: FilesEntry[];
  index: number;
  scope: FilesScope;
  rootPath: string;
  loading: boolean;
  playing: boolean;
  positionMillis: number;
  durationMillis: number;
};

const initialPaths: LocationState = {
  "local:workspace": "",
  "local:user": "",
  "server:workspace": "",
  "server:user": "",
};

const initialHistories: HistoryState = {
  "local:workspace": [],
  "local:user": [],
  "server:workspace": [],
  "server:user": [],
};

const initialLocationMetas: LocationMetaState = {
  "local:workspace": { parentPath: null, canGoUp: false, isAdminMode: false },
  "local:user": { parentPath: null, canGoUp: false, isAdminMode: false },
  "server:workspace": { parentPath: null, canGoUp: false, isAdminMode: false },
  "server:user": { parentPath: null, canGoUp: false, isAdminMode: false },
};

function locationKey(source: FilesSource, scope: FilesScope): LocationKey {
  return `${source}:${scope}`;
}

function getFileIcon(entry: FilesEntry): string {
  if (entry.type === "directory") return FILE_ICONS.directory;
  const kind = getFilesMediaKind(entry);
  if (kind === "image") return FILE_ICONS.image;
  if (kind === "video") return FILE_ICONS.video;
  if (kind === "audio") return FILE_ICONS.audio;
  if (kind === "pdf") return FILE_ICONS.pdf;
  if (isTextEntry(entry)) return FILE_ICONS.text;
  return FILE_ICONS.default;
}

function formatSize(bytes?: number): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatScopedServerPath(path: string, rootPath: string): string {
  if (!path || path === rootPath) return "/";
  if (path === "__drives__") return "/drives";
  if (/^[A-Za-z]:[\\/]/.test(path)) return path.replace(/\\/g, "/");
  const normalizedRoot = rootPath.replace(/\/+$/, "");
  const relative = normalizedRoot && path.startsWith(normalizedRoot)
    ? path.slice(normalizedRoot.length).replace(/^\/+/, "")
    : path.replace(/^\/+/, "");
  return relative ? `/${relative}` : "/";
}

function formatTime(ms?: number): string {
  if (!ms || ms < 0) return "0:00";
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

function isViewableMedia(entry: FilesEntry): boolean {
  const kind = getFilesMediaKind(entry);
  return kind === "image" || kind === "video";
}

function isAudioEntry(entry: FilesEntry): boolean {
  return getFilesMediaKind(entry) === "audio";
}

function sortAudioEntries(entries: FilesEntry[]): FilesEntry[] {
  const seen = new Set<string>();
  return entries
    .filter((entry) => {
      if (seen.has(entry.path)) return false;
      seen.add(entry.path);
      return true;
    })
    .sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true }));
}

function FileThumbnail({
  entry,
  size,
}: {
  entry: FilesEntry;
  size: number;
}) {
  const [source, setSource] = useState<MediaSource | null>(null);
  const [failed, setFailed] = useState(false);
  const kind = getFilesMediaKind(entry);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    setSource(null);
    if (entry.type === "directory" || (kind !== "image" && kind !== "video")) {
      return () => {
        cancelled = true;
      };
    }
    void filesApi
      .getMediaSource(entry, { thumbnail: true, size: Math.max(160, size * 2) })
      .then((nextSource) => {
        if (!cancelled) setSource(nextSource);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [entry, kind, size]);

  if (entry.type === "directory" || !source || failed) {
    return (
      <View
        style={[
          styles.thumbnailFallback,
          { width: size, height: size },
          entry.type === "directory" ? styles.thumbnailFolder : null,
        ]}
      >
        <IconButton
          icon={getFileIcon(entry)}
          iconColor={entry.type === "directory" ? "#f9e2af" : "#89b4fa"}
          size={Math.min(44, size * 0.38)}
          style={styles.thumbnailIcon}
        />
      </View>
    );
  }

  return (
    <View style={[styles.thumbnailFrame, { width: size, height: size }]}>
      <Image
        source={source}
        style={styles.thumbnailImage}
        resizeMode="cover"
        onError={() => setFailed(true)}
      />
      {kind === "video" ? (
        <View style={styles.videoBadge}>
          <IconButton
            icon="play"
            iconColor="#ffffff"
            size={16}
            style={styles.videoBadgeIcon}
          />
        </View>
      ) : null}
    </View>
  );
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function touchDistance(touches: Array<{ pageX: number; pageY: number }>): number {
  if (touches.length < 2) return 0;
  const [first, second] = touches;
  return Math.hypot(second.pageX - first.pageX, second.pageY - first.pageY);
}

function ZoomableImage({
  source,
  onError,
  onSwipeLeft,
  onSwipeRight,
}: {
  source: MediaSource;
  onError: () => void;
  onSwipeLeft: () => void;
  onSwipeRight: () => void;
}) {
  const [scale, setScale] = useState(1);
  const [translate, setTranslate] = useState({ x: 0, y: 0 });
  const [viewport, setViewport] = useState({ width: 1, height: 1 });
  const scaleRef = useRef(1);
  const translateRef = useRef({ x: 0, y: 0 });
  const gestureRef = useRef({
    startDistance: 0,
    startScale: 1,
    startX: 0,
    startY: 0,
    startTranslateX: 0,
    startTranslateY: 0,
  });

  const clampTranslate = useCallback(
    (nextScale: number, x: number, y: number) => {
      if (nextScale <= 1) return { x: 0, y: 0 };
      const maxX = (viewport.width * (nextScale - 1)) / 2;
      const maxY = (viewport.height * (nextScale - 1)) / 2;
      return {
        x: clamp(x, -maxX, maxX),
        y: clamp(y, -maxY, maxY),
      };
    },
    [viewport.height, viewport.width],
  );

  const applyTransform = useCallback(
    (nextScale: number, x: number, y: number) => {
      const boundedScale = clamp(nextScale, 1, 5);
      const boundedTranslate = clampTranslate(boundedScale, x, y);
      scaleRef.current = boundedScale;
      translateRef.current = boundedTranslate;
      setScale(boundedScale);
      setTranslate(boundedTranslate);
    },
    [clampTranslate],
  );

  const resetZoom = useCallback(() => {
    applyTransform(1, 0, 0);
  }, [applyTransform]);

  useEffect(() => {
    resetZoom();
  }, [resetZoom, source.uri]);

  const panResponder = useMemo(
    () =>
      PanResponder.create({
        onStartShouldSetPanResponder: (event) =>
          event.nativeEvent.touches.length >= 2 || scaleRef.current > 1,
        onMoveShouldSetPanResponder: (event, gesture) =>
          event.nativeEvent.touches.length >= 2 ||
          (scaleRef.current > 1 &&
            (Math.abs(gesture.dx) > 2 || Math.abs(gesture.dy) > 2)) ||
          (scaleRef.current <= 1 &&
            Math.abs(gesture.dx) > 12 &&
            Math.abs(gesture.dx) > Math.abs(gesture.dy) * 1.4),
        onPanResponderGrant: (event) => {
          const touches = event.nativeEvent.touches;
          gestureRef.current = {
            startDistance: touchDistance(touches),
            startScale: scaleRef.current,
            startX: touches[0]?.pageX ?? 0,
            startY: touches[0]?.pageY ?? 0,
            startTranslateX: translateRef.current.x,
            startTranslateY: translateRef.current.y,
          };
        },
        onPanResponderMove: (event, gesture) => {
          const touches = event.nativeEvent.touches;
          const gestureStart = gestureRef.current;

          if (touches.length >= 2 && gestureStart.startDistance > 0) {
            const ratio = touchDistance(touches) / gestureStart.startDistance;
            applyTransform(
              gestureStart.startScale * ratio,
              gestureStart.startTranslateX,
              gestureStart.startTranslateY,
            );
            return;
          }

          if (scaleRef.current > 1) {
            applyTransform(
              scaleRef.current,
              gestureStart.startTranslateX + gesture.dx,
              gestureStart.startTranslateY + gesture.dy,
            );
          }
        },
        onPanResponderRelease: (_, gesture) => {
          if (scaleRef.current <= 1.03) {
            if (
              Math.abs(gesture.dx) > 50 &&
              Math.abs(gesture.dx) > Math.abs(gesture.dy) * 1.4
            ) {
              if (gesture.dx < 0) onSwipeLeft();
              else onSwipeRight();
              return;
            }
            resetZoom();
            return;
          }
          if (Math.abs(gesture.dx) < 6 && Math.abs(gesture.dy) < 6) {
            return;
          }
          const currentScale = scaleRef.current;
          const currentTranslate = translateRef.current;
          applyTransform(currentScale, currentTranslate.x, currentTranslate.y);
        },
        onPanResponderTerminationRequest: () => false,
      }),
    [applyTransform, onSwipeLeft, onSwipeRight, resetZoom],
  );

  const handleLayout = (event: LayoutChangeEvent) => {
    const { width, height } = event.nativeEvent.layout;
    setViewport({ width: Math.max(1, width), height: Math.max(1, height) });
  };

  return (
    <Pressable
      style={styles.zoomSurface}
      onLayout={handleLayout}
      onLongPress={resetZoom}
      {...panResponder.panHandlers}
    >
      <Image
        source={source}
        style={[
          styles.viewerImage,
          {
            transform: [
              { translateX: translate.x },
              { translateY: translate.y },
              { scale },
            ],
          },
        ]}
        resizeMode="contain"
        onError={onError}
      />
    </Pressable>
  );
}

type FileNameDialogProps = {
  visible: boolean;
  title: string;
  label: string;
  helperText?: string;
  initialValue?: string;
  submitLabel: string;
  onDismiss: () => void;
  onSubmit: (name: string) => void | Promise<void>;
};

function FileNameDialog({
  visible,
  title,
  label,
  helperText,
  initialValue = "",
  submitLabel,
  onDismiss,
  onSubmit,
}: FileNameDialogProps) {
  const [draft, setDraft] = useState(initialValue);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (visible) {
      setDraft(initialValue);
      setSubmitting(false);
    }
  }, [initialValue, visible]);

  const trimmedDraft = draft.trim();

  const submit = async () => {
    if (!trimmedDraft || submitting) return;
    setSubmitting(true);
    try {
      await onSubmit(trimmedDraft);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog visible={visible} onDismiss={onDismiss} style={styles.dialog}>
      <Dialog.Title style={styles.dialogTitle}>{title}</Dialog.Title>
      <Dialog.Content>
        {helperText ? (
          <Text style={styles.dialogLabel} numberOfLines={2}>
            {helperText}
          </Text>
        ) : null}
        <TextInput
          mode="outlined"
          label={label}
          value={draft}
          onChangeText={setDraft}
          style={styles.dialogInput}
          autoCorrect={false}
          autoCapitalize="none"
        />
      </Dialog.Content>
      <Dialog.Actions>
        <Button onPress={onDismiss} textColor="#a6adc8" disabled={submitting}>
          キャンセル
        </Button>
        <Button
          onPress={() => void submit()}
          textColor="#7c3aed"
          disabled={!trimmedDraft || submitting}
        >
          {submitLabel}
        </Button>
      </Dialog.Actions>
    </Dialog>
  );
}

export default function FilesScreen() {
  const { isAuthenticated, user } = useAuth();
  const { selectedProjectId } = useProject();
  const isAdmin = user?.role === "admin";

  const [source, setSource] = useState<FilesSource>("local");
  const [scope, setScope] = useState<FilesScope>("workspace");
  const [paths, setPaths] = useState<LocationState>(initialPaths);
  const [histories, setHistories] = useState<HistoryState>(initialHistories);
  const [locationMetas, setLocationMetas] =
    useState<LocationMetaState>(initialLocationMetas);
  const [items, setItems] = useState<FilesEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [searchVisible, setSearchVisible] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [bookmarks, setBookmarks] = useState<FilesBookmark[]>([]);

  const [createFileVisible, setCreateFileVisible] = useState(false);
  const [createFolderVisible, setCreateFolderVisible] = useState(false);
  const [renameVisible, setRenameVisible] = useState(false);
  const [editorVisible, setEditorVisible] = useState(false);
  const [viewerVisible, setViewerVisible] = useState(false);
  const [viewerFile, setViewerFile] = useState<FilesEntry | null>(null);
  const [viewerSource, setViewerSource] = useState<MediaSource | null>(null);
  const [viewerLoading, setViewerLoading] = useState(false);
  const [viewerError, setViewerError] = useState<string | null>(null);
  const [videoUri, setVideoUri] = useState<string | null>(null);
  const [videoLoading, setVideoLoading] = useState(false);
  const videoRef = useRef<VideoRef>(null);
  const audioRef = useRef<Audio.Sound | null>(null);
  const audioAdvancingRef = useRef(false);
  const audioEndedHandlerRef = useRef<() => void>(() => {});

  const [renameTarget, setRenameTarget] = useState<FilesEntry | null>(null);
  const [editorTarget, setEditorTarget] = useState<FilesEntry | null>(null);
  const [editorSessionKey, setEditorSessionKey] = useState(0);
  const [editorInitialContent, setEditorInitialContent] = useState("");
  const [editorContent, setEditorContent] = useState("");
  const [editorLoading, setEditorLoading] = useState(false);
  const [editorSaving, setEditorSaving] = useState(false);
  const [clipboard, setClipboard] = useState<ClipboardState | null>(null);
  const [transferring, setTransferring] = useState(false);
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

  const activeKey = locationKey(source, scope);
  const activePath = paths[activeKey];
  const activeHistory = histories[activeKey];
  const activeMeta = locationMetas[activeKey];

  const getServerRootPath = useCallback(
    (nextScope: FilesScope) => {
      if (nextScope === "workspace") {
        if (isAdmin && !selectedProjectId) return "";
        return selectedProjectId ? `_projects/project_${selectedProjectId}` : "";
      }
      return user?.user_id ? `_users/user_${user.user_id}` : "";
    },
    [isAdmin, selectedProjectId, user?.user_id],
  );

  const setPathForLocation = useCallback(
    (nextSource: FilesSource, nextScope: FilesScope, path: string) => {
      const key = locationKey(nextSource, nextScope);
      setPaths((prev) => ({ ...prev, [key]: path }));
    },
    [],
  );

  const canUseLocation = useCallback(
    (nextSource: FilesSource, nextScope: FilesScope) => {
      if (nextSource === "local") return true;
      if (!isAuthenticated) return false;
      if (isAdmin) return true;
      return Boolean(getServerRootPath(nextScope));
    },
    [getServerRootPath, isAdmin, isAuthenticated],
  );

  const locationUnavailableMessage = useCallback(
    (nextScope: FilesScope) => {
      if (!isAuthenticated) {
        return "サーバーファイルはログイン中のみ利用できます。";
      }
      return nextScope === "workspace"
        ? "ワークスペースを開くにはプロジェクトを選択してください。"
        : "ユーザー領域を開けませんでした。";
    },
    [isAuthenticated],
  );

  const loadEntries = useCallback(
    async (
      nextSource: FilesSource,
      nextScope: FilesScope,
      nextPath?: string,
    ) => {
      setLoading(true);
      setError(null);
      try {
        if (nextSource === "server" && !canUseLocation(nextSource, nextScope)) {
          setItems([]);
          setPathForLocation(nextSource, nextScope, "");
          setLocationMetas((prev) => ({
            ...prev,
            [locationKey(nextSource, nextScope)]: {
              parentPath: null,
              canGoUp: false,
              isAdminMode: false,
            },
          }));
          setError(locationUnavailableMessage(nextScope));
          return;
        }

        const serverRootPath = getServerRootPath(nextScope);
        const requestPath =
          nextSource === "server"
            ? (nextPath ?? serverRootPath)
            : nextPath;

        const result = await filesApi.list(
          nextSource,
          requestPath || undefined,
          nextScope,
        );
        setItems(result.items);
        setPathForLocation(nextSource, nextScope, result.currentPath);
        setLocationMetas((prev) => ({
          ...prev,
          [locationKey(nextSource, nextScope)]: {
            parentPath: result.parentPath,
            canGoUp: result.canGoUp,
            isAdminMode: result.isAdminMode,
          },
        }));
      } catch (loadError) {
        setItems([]);
        setError(
          loadError instanceof Error
            ? loadError.message
            : "ファイル一覧の取得に失敗しました",
        );
      } finally {
        setLoading(false);
      }
    },
    [
      canUseLocation,
      getServerRootPath,
      locationUnavailableMessage,
      setPathForLocation,
    ],
  );

  const loadBookmarks = useCallback(async () => {
    if (!isAuthenticated) {
      setBookmarks([]);
      return;
    }
    try {
      const result = await filesApi.listBookmarks();
      setBookmarks(result.bookmarks || []);
    } catch {
      setBookmarks([]);
    }
  }, [isAuthenticated]);

  useEffect(() => {
    void loadEntries("local", "workspace");
  }, [loadEntries]);

  useFocusEffect(
    useCallback(() => {
      void loadEntries(source, scope, activePath || undefined);
      void loadBookmarks();
      void loadAudioPlayerSettings()
        .then(setAudioPlayerSettings)
        .catch(() => {});
    }, [activePath, loadBookmarks, loadEntries, scope, source]),
  );

  useEffect(() => {
    if (!isAuthenticated && source === "server") {
      setSource("local");
      void loadEntries("local", scope, paths[locationKey("local", scope)] || undefined);
    }
  }, [isAuthenticated, loadEntries, paths, scope, source]);

  useEffect(() => {
    if (source !== "server" || scope !== "workspace") return;
    void loadEntries("server", "workspace", getServerRootPath("workspace"));
  }, [getServerRootPath, loadEntries, scope, source]);

  const onRefresh = async () => {
    setRefreshing(true);
    await Promise.all([
      loadEntries(source, scope, activePath || undefined),
      loadBookmarks(),
    ]);
    setRefreshing(false);
  };

  const changeSource = async (nextSource: FilesSource) => {
    if (!canUseLocation(nextSource, scope)) {
      Alert.alert("Files", locationUnavailableMessage(scope));
      return;
    }
    setSource(nextSource);
    const key = locationKey(nextSource, scope);
    await loadEntries(nextSource, scope, paths[key] || undefined);
  };

  const changeScope = async (nextScope: FilesScope) => {
    if (!canUseLocation(source, nextScope)) {
      Alert.alert("Files", locationUnavailableMessage(nextScope));
      return;
    }
    setScope(nextScope);
    const key = locationKey(source, nextScope);
    await loadEntries(source, nextScope, paths[key] || undefined);
  };

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

  const goBack = async () => {
    const previousPath = popHistory();
    if (previousPath == null) return;
    setPathForLocation(source, scope, previousPath);
    await loadEntries(source, scope, previousPath);
  };

  const goUp = async () => {
    if (!activeMeta.canGoUp || activeMeta.parentPath == null) return;
    if (activePath !== activeMeta.parentPath) {
      pushHistory(activePath);
    }
    setPathForLocation(source, scope, activeMeta.parentPath);
    await loadEntries(source, scope, activeMeta.parentPath);
  };

  const navigateTo = async (nextPath: string) => {
    if (activePath !== nextPath) {
      pushHistory(activePath);
    }
    setPathForLocation(source, scope, nextPath);
    await loadEntries(source, scope, nextPath);
  };

  const openMediaViewer = (entry: FilesEntry) => {
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

  const updateAudioStatus = (status: AVPlaybackStatus) => {
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
  };

  const playAudioAt = useCallback(
    async (
      playlist: FilesEntry[],
      index: number,
      trackScope = scope,
      rootPath?: string,
    ) => {
      const track = playlist[index];
      if (!track) return;
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
        if (audioRef.current) {
          await audioRef.current.unloadAsync();
          audioRef.current = null;
        }
        const uri = await filesApi.getPlayableUri(track);
        const { sound, status } = await Audio.Sound.createAsync(
          { uri },
          { shouldPlay: true },
          updateAudioStatus,
        );
        audioRef.current = sound;
        updateAudioStatus(status);
      } catch (audioError) {
        setAudioState((prev) => ({ ...prev, loading: false, playing: false }));
        Alert.alert(
          "Audio",
          audioError instanceof Error
            ? audioError.message
            : "音声を再生できませんでした。",
        );
      }
    },
    [getAudioRootPath, scope],
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
    if (audioRef.current) {
      await audioRef.current.unloadAsync();
      audioRef.current = null;
    }
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

  const focusAudioTrackLocation = async () => {
    const track = audioState.track;
    if (!track) return;

    const targetScope = audioState.scope;
    if (!canUseLocation(track.source, targetScope)) {
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
    await loadEntries(track.source, targetScope, parentPath);
  };

  useEffect(() => {
    audioEndedHandlerRef.current = () => {
      void advanceAudio(1, true);
    };
  });

  useEffect(() => {
    return () => {
      if (audioRef.current) {
        void audioRef.current.unloadAsync();
      }
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setViewerError(null);
    setViewerSource(null);
    setVideoUri(null);
    if (!viewerFile || !viewerVisible) return;

    const kind = getFilesMediaKind(viewerFile);
    if (kind === "image") {
      setViewerLoading(true);
      void filesApi
        .getMediaSource(viewerFile, { size: 1280 })
        .then((sourceValue) => {
          if (!cancelled) setViewerSource(sourceValue);
        })
        .catch((error) => {
          if (!cancelled) {
            setViewerError(
              error instanceof Error ? error.message : "画像を表示できませんでした。",
            );
          }
        })
        .finally(() => {
          if (!cancelled) setViewerLoading(false);
        });
    } else if (kind === "video") {
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
  }, [viewerFile, viewerVisible]);

  const openTextEditor = async (entry: FilesEntry) => {
    setEditorSessionKey((key) => key + 1);
    setEditorVisible(true);
    setEditorTarget(entry);
    setEditorInitialContent("");
    setEditorContent("");
    setEditorLoading(true);
    try {
      const content = await filesApi.readText(entry.source, entry.path);
      setEditorInitialContent(content);
      setEditorContent(content);
    } catch (readError) {
      Alert.alert(
        "Files",
        readError instanceof Error
          ? readError.message
          : "テキスト読み込みに失敗しました。",
      );
      setEditorVisible(false);
      setEditorTarget(null);
    } finally {
      setEditorLoading(false);
    }
  };

  const handleOpenEntry = async (entry: FilesEntry) => {
    if (entry.type === "directory") {
      await navigateTo(entry.path);
      return;
    }
    if (isAudioEntry(entry)) {
      await playAudioEntry(entry);
      return;
    }
    if (isViewableMedia(entry)) {
      openMediaViewer(entry);
      return;
    }
    if (isTextEntry(entry)) {
      await openTextEditor(entry);
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
    try {
      await filesApi.createTextFile(source, activePath, name);
      setCreateFileVisible(false);
      await loadEntries(source, scope, activePath);
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
    try {
      await filesApi.createFolder(source, activePath, name);
      setCreateFolderVisible(false);
      await loadEntries(source, scope, activePath);
    } catch (createError) {
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
                await loadEntries(source, scope, activePath);
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

  const setClipboardEntry = (
    entry: FilesEntry,
    operation: ClipboardOperation,
  ) => {
    setClipboard({ entry, operation });
    Alert.alert(
      "Files",
      `${entry.name} を${operation === "copy" ? "コピー" : "移動"}対象にしました。移動先で貼り付けてください。`,
    );
  };

  const pasteClipboard = async (destinationPath = activePath) => {
    if (!clipboard || transferring) return;
    if (!canMutateCurrentPath || !destinationPath) {
      Alert.alert("Files", "この場所には貼り付けできません。");
      return;
    }
    if (clipboard.entry.source !== source) {
      Alert.alert("Files", "ローカルとサーバーをまたいだ貼り付けはできません。");
      return;
    }

    setTransferring(true);
    try {
      if (clipboard.operation === "copy") {
        await filesApi.copy(source, clipboard.entry.path, destinationPath);
      } else {
        await filesApi.move(source, clipboard.entry.path, destinationPath);
        setClipboard(null);
      }
      await loadEntries(source, scope, activePath);
    } catch (transferError) {
      Alert.alert(
        "Files",
        transferError instanceof Error
          ? transferError.message
          : "貼り付けに失敗しました。",
      );
    } finally {
      setTransferring(false);
    }
  };

  const showEntryActions = (entry: FilesEntry) => {
    const pasteDestination = entry.type === "directory" ? entry.path : activePath;
    const canPasteHere =
      Boolean(clipboard) &&
      clipboard?.entry.source === source &&
      Boolean(pasteDestination) &&
      canMutateCurrentPath;

    Alert.alert(
      entry.name,
      entry.type === "directory" ? "フォルダー" : entry.mimeType || "ファイル",
      [
        { text: "開く", onPress: () => void handleOpenEntry(entry) },
        {
          text: "名前を変更",
          onPress: () => {
            setRenameTarget(entry);
            setRenameVisible(true);
          },
        },
        {
          text: "コピー",
          onPress: () => setClipboardEntry(entry, "copy"),
        },
        {
          text: "移動",
          onPress: () => setClipboardEntry(entry, "move"),
        },
        ...(canPasteHere
          ? [
              {
                text:
                  entry.type === "directory"
                    ? "このフォルダーへ貼り付け"
                    : "ここに貼り付け",
                onPress: () => void pasteClipboard(pasteDestination),
              },
            ]
          : []),
        { text: "削除", style: "destructive", onPress: () => deleteEntry(entry) },
        { text: "キャンセル", style: "cancel" },
      ],
      { cancelable: true },
    );
  };

  const saveEditor = async () => {
    if (!editorTarget) return;
    setEditorSaving(true);
    try {
      await filesApi.saveText(
        editorTarget.source,
        editorTarget.path,
        editorContent,
      );
      setEditorVisible(false);
      await loadEntries(source, scope, activePath);
    } catch (saveError) {
      Alert.alert(
        "Files",
        saveError instanceof Error ? saveError.message : "保存に失敗しました。",
      );
    } finally {
      setEditorSaving(false);
    }
  };

  const uploadFile = async () => {
    if (!canMutateCurrentPath || uploading) return;
    if (source === "server" && scope !== "workspace") {
      Alert.alert("Files", "サーバーアップロードはワークスペースで利用できます。");
      return;
    }
    if (source === "server" && !selectedProjectId) {
      Alert.alert("Files", "アップロード先のプロジェクトを選択してください。");
      return;
    }

    try {
      const picked = await DocumentPicker.getDocumentAsync({
        copyToCacheDirectory: true,
        multiple: false,
      });
      if (picked.canceled || !picked.assets?.[0]) return;
      const asset = picked.assets[0];
      setUploading(true);
      await filesApi.upload(
        source,
        activePath,
        {
          uri: asset.uri,
          name: asset.name || "upload",
          mimeType: asset.mimeType,
        },
        { projectId: selectedProjectId },
      );
      await loadEntries(source, scope, activePath);
    } catch (uploadError) {
      Alert.alert(
        "Upload failed",
        uploadError instanceof Error
          ? uploadError.message
          : "アップロードに失敗しました。",
      );
    } finally {
      setUploading(false);
    }
  };

  const isBookmarked = useMemo(
    () => bookmarks.some((bookmark) => bookmark.path === activePath),
    [activePath, bookmarks],
  );

  const toggleBookmark = async () => {
    if (!isAuthenticated || !activePath) {
      Alert.alert("Files", "ブックマークはログイン中のみ利用できます。");
      return;
    }
    try {
      if (isBookmarked) {
        await filesApi.removeBookmark(activePath);
      } else {
        const name =
          activePath.split(/[\\/]/).filter(Boolean).pop() ||
          currentDisplayPath ||
          "Bookmark";
        await filesApi.addBookmark(name, activePath);
      }
      await loadBookmarks();
    } catch (bookmarkError) {
      Alert.alert(
        "Files",
        bookmarkError instanceof Error
          ? bookmarkError.message
          : "ブックマーク更新に失敗しました。",
      );
    }
  };

  const currentDisplayPath = useMemo(() => {
    const prefix = `${SOURCE_LABELS[source]} / ${SCOPE_LABELS[scope]}`;
    const relative =
      source === "server"
        ? formatScopedServerPath(activePath, getServerRootPath(scope))
        : formatDisplayPath(source, activePath, scope);
    if (!relative || relative === "/") return prefix;
    return `${prefix}${relative}`;
  }, [activePath, getServerRootPath, scope, source]);

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
          if (viewerVisible) {
            setViewerVisible(false);
            return true;
          }
          if (editorVisible) {
            setEditorVisible(false);
            return true;
          }
          if (activeHistory.length > 0) {
            void goBack();
            return true;
          }
          if (activeMeta.canGoUp) {
            void goUp();
            return true;
          }
          return true;
        },
      );
      return () => subscription.remove();
    }, [activeHistory.length, activeMeta.canGoUp, editorVisible, goBack, goUp, viewerVisible]),
  );

  const bookmarkItems = useMemo(() => {
    if (source === "local") return [];
    return bookmarks.filter((bookmark) => bookmark.path);
  }, [bookmarks, source]);

  const canMutateCurrentPath =
    canUseLocation(source, scope) &&
    (Boolean(activePath) || (source === "server" && activeMeta.isAdminMode));

  const renderListItem = ({ item }: { item: FilesEntry }) => (
    <Pressable
      onPress={() => void handleOpenEntry(item)}
      onLongPress={() => showEntryActions(item)}
    >
      {({ pressed }) => (
        <Surface
          style={[styles.fileItem, pressed ? styles.fileItemPressed : null]}
          elevation={0}
        >
          <FileThumbnail entry={item} size={48} />
          <View style={styles.fileInfo}>
            <Text style={styles.fileName} numberOfLines={1}>
              {item.name}
            </Text>
            <Text style={styles.fileMeta} numberOfLines={1}>
              {item.type === "directory" ? "フォルダー" : item.mimeType || "ファイル"}
              {item.size ? ` ・ ${formatSize(item.size)}` : ""}
            </Text>
          </View>
          <IconButton
            icon="dots-vertical"
            iconColor="#a6adc8"
            size={20}
            onPress={() => showEntryActions(item)}
          />
        </Surface>
      )}
    </Pressable>
  );

  const renderGridItem = ({ item }: { item: FilesEntry }) => (
    <Pressable
      style={styles.gridItemWrap}
      onPress={() => void handleOpenEntry(item)}
      onLongPress={() => showEntryActions(item)}
    >
      {({ pressed }) => (
        <Surface
          style={[styles.gridItem, pressed ? styles.fileItemPressed : null]}
          elevation={0}
        >
          <FileThumbnail entry={item} size={104} />
          <Text style={styles.gridFileName} numberOfLines={2}>
            {item.name}
          </Text>
          <Text style={styles.gridFileMeta} numberOfLines={1}>
            {item.type === "directory" ? "フォルダー" : formatSize(item.size)}
          </Text>
        </Surface>
      )}
    </Pressable>
  );

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <Text variant="titleLarge" style={styles.headerTitle}>
          Files
        </Text>
        <View style={styles.segmentRow}>
          {(["local", "server"] as FilesSource[]).map((value) => (
            <Chip
              key={value}
              compact
              selected={source === value}
              style={source === value ? styles.segmentChipActive : styles.segmentChip}
              textStyle={styles.segmentChipText}
              disabled={value === "server" && !isAuthenticated}
              onPress={() => void changeSource(value)}
            >
              {SOURCE_LABELS[value]}
            </Chip>
          ))}
        </View>
        <View style={styles.segmentRow}>
          {(["workspace", "user"] as FilesScope[]).map((value) => {
            const disabled =
              source === "server" &&
              (!isAuthenticated || (!isAdmin && !getServerRootPath(value)));
            return (
              <Chip
                key={value}
                compact
                selected={scope === value}
                style={scope === value ? styles.scopeChipActive : styles.segmentChip}
                textStyle={styles.segmentChipText}
                disabled={disabled}
                onPress={() => void changeScope(value)}
              >
                {SCOPE_LABELS[value]}
              </Chip>
            );
          })}
        </View>
      </Surface>

      <View style={styles.toolbarRow}>
        <IconButton
          icon="arrow-left"
          iconColor={activeHistory.length > 0 ? "#cdd6f4" : "#585b70"}
          onPress={() => void goBack()}
          disabled={activeHistory.length === 0}
        />
        <IconButton
          icon="arrow-up"
          iconColor={activeMeta.canGoUp ? "#cdd6f4" : "#585b70"}
          onPress={() => void goUp()}
          disabled={!activeMeta.canGoUp}
        />
        <Text style={styles.pathText} numberOfLines={1}>
          {currentDisplayPath}
        </Text>
        <IconButton
          icon="magnify"
          iconColor={searchVisible ? "#c084fc" : "#a6adc8"}
          onPress={() => setSearchVisible((prev) => !prev)}
        />
        <IconButton
          icon={isBookmarked ? "star" : "star-outline"}
          iconColor={isBookmarked ? "#f9e2af" : "#a6adc8"}
          disabled={!isAuthenticated || !activePath}
          onPress={() => void toggleBookmark()}
        />
        <IconButton
          icon={viewMode === "grid" ? "format-list-bulleted" : "view-grid-outline"}
          iconColor="#a6adc8"
          onPress={() => setViewMode((prev) => (prev === "grid" ? "list" : "grid"))}
        />
        <IconButton
          icon="clipboard-arrow-down-outline"
          iconColor={clipboard ? "#a6e3a1" : "#585b70"}
          disabled={!clipboard || !canMutateCurrentPath || transferring}
          onPress={() => void pasteClipboard()}
        />
        <IconButton
          icon="file-plus-outline"
          iconColor="#89b4fa"
          disabled={!canMutateCurrentPath}
          onPress={() => setCreateFileVisible(true)}
        />
        <IconButton
          icon="upload"
          iconColor="#89b4fa"
          disabled={!canMutateCurrentPath || uploading}
          onPress={() => void uploadFile()}
        />
        <IconButton
          icon="folder-plus-outline"
          iconColor="#89b4fa"
          disabled={!canMutateCurrentPath}
          onPress={() => setCreateFolderVisible(true)}
        />
      </View>

      {bookmarkItems.length > 0 ? (
        <View style={styles.bookmarkRow}>
          {bookmarkItems.map((bookmark) => (
            <Chip
              key={bookmark.path}
              compact
              icon="star"
              style={
                activePath === bookmark.path
                  ? styles.bookmarkChipActive
                  : styles.bookmarkChip
              }
              textStyle={styles.bookmarkText}
              onPress={() => void navigateTo(bookmark.path)}
            >
              {bookmark.name}
            </Chip>
          ))}
        </View>
      ) : null}

      {searchVisible ? (
        <View style={styles.searchRow}>
          <TextInput
            mode="outlined"
            dense
            placeholder="Search"
            value={query}
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

      {loading ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color="#7c3aed" />
        </View>
      ) : (
        <FlatList
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
                {error || "この場所にはまだファイルがありません。"}
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

      <Portal>
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
          onDismiss={() => setCreateFolderVisible(false)}
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

        <Dialog
          visible={editorVisible}
          onDismiss={() => setEditorVisible(false)}
          style={styles.editorDialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            {editorTarget?.name || "Editor"}
          </Dialog.Title>
          <Dialog.Content>
            {editorLoading ? (
              <View style={styles.editorLoading}>
                <ActivityIndicator size="small" color="#7c3aed" />
              </View>
            ) : (
              <TextInput
                key={`editor-${editorSessionKey}`}
                mode="outlined"
                multiline
                defaultValue={editorInitialContent}
                onChangeText={setEditorContent}
                style={styles.editorInput}
                autoCorrect={false}
                autoCapitalize="none"
              />
            )}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setEditorVisible(false)} textColor="#a6adc8">
              閉じる
            </Button>
            <Button
              onPress={() => void saveEditor()}
              textColor="#7c3aed"
              disabled={editorLoading || editorSaving || !editorTarget}
            >
              保存
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>

      <Modal
        visible={viewerVisible}
        animationType="fade"
        onRequestClose={() => setViewerVisible(false)}
      >
        <View style={styles.viewerContainer}>
          <View style={styles.viewerHeader}>
            <Text style={styles.viewerTitle} numberOfLines={1}>
              {viewerFile?.name || ""}
            </Text>
            <IconButton
              icon="close"
              iconColor="#ffffff"
              onPress={() => setViewerVisible(false)}
            />
          </View>

          <View style={styles.viewerBody}>
            {viewerLoading || videoLoading ? (
              <ActivityIndicator size="large" color="#a78bfa" />
            ) : viewerError ? (
              <Text style={styles.viewerError}>{viewerError}</Text>
            ) : viewerFile && getFilesMediaKind(viewerFile) === "image" && viewerSource ? (
              <ZoomableImage
                source={viewerSource}
                onError={() => setViewerError("画像を表示できませんでした。")}
                onSwipeLeft={() => navigateViewer(1)}
                onSwipeRight={() => navigateViewer(-1)}
              />
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
            ) : (
              <Text style={styles.viewerError}>プレビューを表示できません。</Text>
            )}
          </View>

          <View style={styles.viewerFooter}>
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
          </View>
        </View>
      </Modal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  header: { padding: 16, paddingTop: 56, backgroundColor: "#1e1e2e" },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold", marginBottom: 12 },
  segmentRow: { flexDirection: "row", gap: 8, marginBottom: 8 },
  segmentChip: { backgroundColor: "#313244" },
  segmentChipActive: { backgroundColor: "#4c1d95" },
  scopeChipActive: { backgroundColor: "#3b2f5f" },
  segmentChipText: { color: "#cdd6f4" },
  toolbarRow: {
    flexDirection: "row",
    alignItems: "center",
    minHeight: 54,
    paddingHorizontal: 4,
    backgroundColor: "#181825",
  },
  pathText: { color: "#a6adc8", fontSize: 12, flex: 1 },
  searchRow: {
    paddingHorizontal: 12,
    paddingVertical: 8,
    backgroundColor: "#181825",
  },
  searchInput: { backgroundColor: "#1e1e2e" },
  bookmarkRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
    paddingHorizontal: 10,
    paddingVertical: 8,
    backgroundColor: "#181825",
  },
  bookmarkChip: { backgroundColor: "#313244" },
  bookmarkChipActive: { backgroundColor: "#4c1d95" },
  bookmarkText: { color: "#cdd6f4", fontSize: 11 },
  fileItem: {
    flexDirection: "row",
    alignItems: "center",
    minHeight: 72,
    paddingVertical: 6,
    paddingHorizontal: 8,
    backgroundColor: "#11111b",
  },
  fileItemPressed: { backgroundColor: "#181825" },
  fileIcon: { margin: 0, marginRight: 10 },
  fileInfo: { flex: 1 },
  fileName: { color: "#cdd6f4", fontSize: 15 },
  fileMeta: { color: "#a6adc8", fontSize: 11, marginTop: 3 },
  gridContent: { padding: 8 },
  gridItemWrap: { width: "33.333%", padding: 4 },
  gridItem: {
    minHeight: 168,
    alignItems: "center",
    padding: 8,
    borderRadius: 8,
    backgroundColor: "#11111b",
  },
  gridFileName: {
    color: "#cdd6f4",
    fontSize: 12,
    lineHeight: 16,
    marginTop: 8,
    textAlign: "center",
  },
  gridFileMeta: {
    color: "#a6adc8",
    fontSize: 10,
    marginTop: 4,
    textAlign: "center",
  },
  thumbnailFrame: {
    overflow: "hidden",
    borderRadius: 8,
    backgroundColor: "#181825",
  },
  thumbnailImage: { width: "100%", height: "100%" },
  thumbnailFallback: {
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 8,
    backgroundColor: "#181825",
  },
  thumbnailFolder: { backgroundColor: "#241f2f" },
  thumbnailIcon: { margin: 0 },
  videoBadge: {
    position: "absolute",
    left: 0,
    right: 0,
    top: 0,
    bottom: 0,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "rgba(0, 0, 0, 0.18)",
  },
  videoBadgeIcon: { margin: 0, backgroundColor: "rgba(0,0,0,0.45)" },
  divider: { backgroundColor: "#313244", marginHorizontal: 12 },
  center: {
    flex: 1,
    justifyContent: "center",
    alignItems: "center",
    padding: 24,
  },
  emptyText: { color: "#585b70", fontSize: 14, textAlign: "center" },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogLabel: { color: "#a6adc8", fontSize: 12, marginBottom: 12 },
  dialogInput: { backgroundColor: "#1e1e2e" },
  editorDialog: { backgroundColor: "#1e1e2e", maxHeight: "92%" },
  editorInput: {
    minHeight: 320,
    maxHeight: 460,
    backgroundColor: "#1e1e2e",
  },
  editorLoading: {
    height: 120,
    alignItems: "center",
    justifyContent: "center",
  },
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
    minHeight: 56,
    paddingTop: 10,
    paddingHorizontal: 8,
    flexDirection: "row",
    alignItems: "center",
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#313244",
  },
  viewerTitle: { flex: 1, color: "#ffffff", fontSize: 14, fontWeight: "600" },
  viewerBody: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    padding: 8,
  },
  zoomSurface: {
    flex: 1,
    alignSelf: "stretch",
    overflow: "hidden",
    alignItems: "center",
    justifyContent: "center",
  },
  viewerImage: { width: "100%", height: "100%" },
  viewerVideo: { width: "100%", height: "100%" },
  viewerError: { color: "#fca5a5", fontSize: 14, textAlign: "center" },
  viewerFooter: {
    minHeight: 64,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
  },
  viewerCount: { minWidth: 88, color: "#cdd6f4", textAlign: "center" },
});

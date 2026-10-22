import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as DocumentPicker from "expo-document-picker";
import * as FileSystem from "expo-file-system/legacy";
import {
  Alert,
  RefreshControl,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import {
  ActivityIndicator,
  Button,
  Chip,
  Dialog,
  Divider,
  Icon,
  IconButton,
  Menu,
  Portal,
  SegmentedButtons,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import {
  useFocusEffect,
  useLocalSearchParams,
  useRouter,
} from "expo-router";
import { ScreenHeader } from "../../../components/screen-header";
import { useNetworkStore } from "../../../stores/network";
import { useProject } from "../../../contexts/ProjectContext";
import { useAuth } from "../../../contexts/AuthContext";
import {
  appsRepo,
} from "../../../repositories/apps";
import type {
  AppsDetailReadStatuses,
  AppsReadStatus,
} from "../../../repositories/apps";
import {
  permissionAtLeast,
  type AppContext,
  type AppFile,
  type AppFileContent,
  type AppJob,
  type AppRelease,
  type AppRuntimeDeclaration,
  type AppRuntimeFileRef,
  type AppRuntimeFormField,
  type AppRuntimeRun,
  type AppSummary,
  type AppTarget,
  type ProjectAppBinding,
  type TaskAppLink,
} from "../../../lib/apps-api";
import { createCurrentCharacterSession } from "../../../features/characters/current-character";
import { downloadFileToDevice } from "../../../lib/files-download";
import type { FilesEntry } from "../../../lib/files-types";
import {
  isRuntimeActionActiveStatus,
  isRuntimeActionPermanentTerminalStatus,
  isDefinitiveRuntimeActionRejectStatus,
  buildRuntimeScopeKey,
  mergeRuntimeActionActivity,
  reconcileRuntimePendingActions,
  runtimeActionHttpStatus,
  runtimePollBackoffMs,
  shouldBlockRuntimeAction,
} from "../../../lib/apps-runtime-action-state";
import {
  captureRuntimeSelection,
  isCurrentRuntimeSelection,
  selectRuntimeRun,
  type RuntimeSelectionToken,
} from "../../../lib/apps-runtime-selection";
import {
  DeclarativeAppRenderer,
  explainRuntimeRun,
  formatRuntimeRunState,
  isRuntimeRunTerminal,
  runtimeRunNeedsStop,
  type RuntimeFileSelection,
} from "../../../components/apps/declarative-app-renderer";

const BACKGROUND = "#11111b";
const SURFACE = "#1e1e2e";
const TEXT = "#cdd6f4";
const MUTED = "#a6adc8";

type AppDetailData = {
  app: AppSummary | null;
  context: AppContext | null;
  targets: AppTarget[];
  files: AppFile[];
  releases: AppRelease[];
  jobs: AppJob[];
  projectBindings: ProjectAppBinding[];
  taskLinks: Array<TaskAppLink & { task?: Record<string, unknown> }>;
  runtime: AppRuntimeDeclaration | null;
  runtimeRuns: AppRuntimeRun[];
  readStatuses: AppsDetailReadStatuses;
};

function createEmptyAppDetailData(): AppDetailData {
  return {
    app: null,
    context: null,
    targets: [],
    files: [],
    releases: [],
    jobs: [],
    projectBindings: [],
    taskLinks: [],
    runtime: null,
    runtimeRuns: [],
    readStatuses: {
      context: { state: "unavailable", reason: "not_loaded" },
      targets: { state: "unavailable", reason: "not_loaded" },
      files: { state: "unavailable", reason: "not_loaded" },
      releases: { state: "unavailable", reason: "not_loaded" },
      jobs: { state: "unavailable", reason: "not_loaded" },
      runtime: { state: "unavailable", reason: "not_loaded" },
      runtimeRuns: { state: "unavailable", reason: "not_loaded" },
    },
  };
}

type PendingRuntimeRetry = {
  actionId: string;
  idempotencyKey: string;
};

function getParam(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function formatJobStatus(job: AppJob): string {
  if (job.exit_code !== null && job.exit_code !== undefined) {
    return `${job.status} (exit ${job.exit_code})`;
  }
  return job.status;
}

function newRuntimeIdempotencyKey(): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  return randomUuid || `app-action-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function bytesToBase64(value: ArrayBuffer): string {
  const bytes = new Uint8Array(value);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  if (typeof globalThis.btoa === "function") return globalThis.btoa(binary);
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let result = "";
  for (let index = 0; index < bytes.length; index += 3) {
    const first = bytes[index];
    const second = bytes[index + 1];
    const third = bytes[index + 2];
    result += alphabet[first >> 2];
    result += alphabet[((first & 3) << 4) | ((second ?? 0) >> 4)];
    result += second === undefined ? "=" : alphabet[((second & 15) << 2) | ((third ?? 0) >> 6)];
    result += third === undefined ? "=" : alphabet[third & 63];
  }
  return result;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Surface style={styles.section} elevation={1}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {children}
    </Surface>
  );
}

function readReasonText(reason: AppsReadStatus["reason"]): string {
  switch (reason) {
    case "offline": return "オフラインです";
    case "auth": return "認証状態を確認できません";
    case "forbidden": return "アクセス権を確認できません";
    case "not_found": return "情報が見つかりません";
    case "network": return "サーバーに接続できません";
    case "legacy": return "従来のTarget/Job実行を使用します";
    case "not_loaded": return "まだ情報を取得できていません";
    default: return "取得状態を確認できません";
  }
}

function ReadStateNotice({
  status,
  onReload,
}: {
  status: AppsReadStatus;
  onReload: () => void;
}) {
  if (status.state === "fresh" || (status.legacy && status.reason === "legacy")) return null;
  const reason = readReasonText(status.reason);
  return (
    <View>
      <Text style={styles.offlineText}>
        {status.state === "cached"
          ? `保存済み情報を表示しています。最新状態を確認できませんでした（${reason}）。`
          : `情報を確認できませんでした（${reason}）。`}
      </Text>
      <Button mode="text" compact onPress={onReload} accessibilityLabel="App情報を再読み込み">
        再読み込み
      </Button>
    </View>
  );
}

function detailSectionTitle(label: string, count: number, status: AppsReadStatus): string {
  if (status.state === "fresh") return `${label} (${count})`;
  return `${label} (${status.state === "cached" ? "保存済み" : "未確認"})`;
}

function UseRuntimePanel({
  appName,
  runtime,
  runtimeStatus,
  runtimeRunsStatus,
  runs,
  canRun,
  online,
  busyActionId,
  activeActionIds,
  runtimeRunId,
  onRun,
  onStop,
  canStop,
  onRefreshRun,
  onPickFile,
  runtimeActionError,
  pendingRuntimeRetry,
  onReload,
  onClearPending,
  onOpenLogs,
  onOpenFile,
  runtimeLogs,
  logsLoading,
}: {
  appName: string;
  runtime: AppRuntimeDeclaration | null;
  runtimeStatus: AppsReadStatus;
  runtimeRunsStatus: AppsReadStatus;
  runs: AppRuntimeRun[];
  canRun: boolean;
  online: boolean;
  busyActionId: string | null;
  activeActionIds: readonly string[];
  runtimeRunId: string | null;
  onRun: (actionId: string, input: Record<string, unknown>) => Promise<void>;
  onStop: (run: AppRuntimeRun) => Promise<void>;
  canStop: boolean;
  onRefreshRun: (run: AppRuntimeRun) => Promise<void>;
  onPickFile: (field: AppRuntimeFormField, actionId?: string) => Promise<RuntimeFileSelection | null>;
  runtimeActionError: string | null;
  pendingRuntimeRetry: PendingRuntimeRetry | null;
  onReload: () => void;
  onClearPending: () => Promise<void>;
  onOpenLogs: (run: AppRuntimeRun) => Promise<void>;
  onOpenFile: (file: AppRuntimeFileRef) => void;
  runtimeLogs: string | null;
  logsLoading: boolean;
}) {
  const available = runtime?.availability?.available ?? Boolean(runtime?.ui);
  const legacyRuntime = runtime === null && runtimeStatus.legacy === true;
  return (
    <View style={styles.usePanel}>
      <Surface style={styles.identityCard} elevation={1}>
        <View style={styles.identityIcon}><Icon source="play-circle-outline" size={32} color="#89b4fa" /></View>
        <View style={styles.identityCopy}>
          <Text style={styles.identityTitle}>{appName}</Text>
          <Text style={styles.identityMeta}>利用画面 · Release固定の宣言UI</Text>
          {runtime?.release?.version ? <Text style={styles.mutedText}>保存版 {runtime.release.version}</Text> : null}
        </View>
      </Surface>
      <ReadStateNotice status={runtimeStatus} onReload={onReload} />
      {runtime === null && !legacyRuntime ? (
        <Section title="利用情報を取得できません">
          <Text style={styles.bodyText}>取得状態が確認できるまで、実行方式を判定できません。</Text>
        </Section>
      ) : null}
      {!online ? <Text style={styles.offlineText}>オフラインではActionを実行できません。再接続後に手動で実行してください。</Text> : null}
      {runtimeActionError ? <View style={styles.runtimeErrorBlock}><Text style={styles.error}>{runtimeActionError}</Text>{pendingRuntimeRetry ? <Button mode="outlined" onPress={() => void onClearPending()}>pendingを解除して再入力</Button> : null}</View> : null}
      {runtime && !available ? <Text style={styles.mutedText}>{runtime.availability.reason || "このAppは現在利用できません"}</Text> : null}
      {legacyRuntime ? (
        <Section title="利用できる宣言UIがありません">
          <Text style={styles.bodyText}>このAppは従来のTarget/Job実行を使用します。管理タブからTargetを選択してください。</Text>
        </Section>
      ) : runtime?.ui ? (
        <DeclarativeAppRenderer
          ui={runtime.ui}
          releaseKey={runtime.release?.id}
          actions={runtime.actions ?? []}
          runs={runs}
          canRun={canRun}
          online={online && available}
          busyActionId={busyActionId}
          activeActionIds={activeActionIds}
          onRun={onRun}
          onPickFile={onPickFile}
          onOpenFile={onOpenFile}
        />
      ) : runtime ? (
        <Section title="利用できる宣言UIがありません">
          <Text style={styles.bodyText}>このAppの公開Releaseには利用画面がありません。</Text>
        </Section>
      ) : null}
      <ReadStateNotice status={runtimeRunsStatus} onReload={onReload} />
      {runs.length > 0 ? (
          <Section title={detailSectionTitle("実行結果", runs.length, runtimeRunsStatus)}>
          {runs.slice(0, 10).map((run) => (
            <View key={run.id} style={styles.jobRow}>
              <View style={styles.jobCopy}>
                <Text style={styles.bodyText}>{run.action_id || "Action"}</Text>
                <Text style={styles.mutedText}>{formatRuntimeRunState(run)}</Text>
                <Text style={styles.mutedText}>{run.finished_at || run.started_at || run.created_at || "時刻情報なし"}</Text>
                {explainRuntimeRun(run) ? <Text style={styles.offlineText}>{explainRuntimeRun(run)}</Text> : null}
              </View>
              <View style={styles.jobActions}>
                <IconButton icon="text-box-outline" iconColor="#89b4fa" onPress={() => void onOpenLogs(run)} accessibilityLabel="Actionログを表示" />
                <IconButton icon="refresh" iconColor="#89b4fa" onPress={() => void onRefreshRun(run)} disabled={busyActionId !== null} accessibilityLabel="Action状態を更新" />
                {canStop && runtimeRunNeedsStop(run) ? <IconButton icon="stop-circle-outline" iconColor="#f38ba8" onPress={() => void onStop(run)} disabled={busyActionId !== null} accessibilityLabel="Actionを停止" /> : null}
              </View>
            </View>
          ))}
          {runtimeRunId && logsLoading ? <ActivityIndicator color="#89b4fa" /> : null}
          {runtimeRunId && runtimeLogs ? <Surface style={styles.runtimeLogSurface} elevation={0}><Text selectable style={styles.logText}>{runtimeLogs}</Text></Surface> : null}
        </Section>
      ) : null}
      {runs.length === 0 && runtimeRunsStatus.state === "fresh" ? (
        <Section title="実行結果 (0)"><Text style={styles.mutedText}>実行結果はありません</Text></Section>
      ) : null}
    </View>
  );
}

export default function AppDetailScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ appId: string; projectId?: string | string[] }>();
  const appId = getParam(params.appId);
  const projectId = getParam(params.projectId) || undefined;
  const { selectedProjectId } = useProject();
  const { user } = useAuth();
  const online = useNetworkStore((state) => state.connected ?? state.online);
  const effectiveProjectId = projectId || selectedProjectId || undefined;
  const [data, setData] = useState<AppDetailData>(createEmptyAppDetailData);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [screenMode, setScreenMode] = useState<"use" | "manage">("use");
  const [runtimeBusyAction, setRuntimeBusyAction] = useState<string | null>(null);
  const [activeRuntimeActionIds, setActiveRuntimeActionIds] = useState<string[]>([]);
  const [runtimeMonitorNonce, setRuntimeMonitorNonce] = useState(0);
  const [runtimeLogs, setRuntimeLogs] = useState<string | null>(null);
  const [runtimeLogsLoading, setRuntimeLogsLoading] = useState(false);
  const [runtimeRunId, setRuntimeRunId] = useState<string | null>(null);
  const runtimeActionKeysRef = useRef(new Map<string, string>());
  const runtimeActionInputsRef = useRef(new Map<string, Record<string, unknown>>());
  const runtimeActionFenceRef = useRef(new Set<string>());
  const runtimeRunStatusRef = useRef(new Map<string, string>());
  const runtimeActiveActionIdsRef = useRef(new Map<string, Set<string>>());
  const runtimeTerminalRunIdsRef = useRef(new Set<string>());
  const runtimeScopeRef = useRef({ key: "", generation: 0 });
  const runtimeLoadRequestRef = useRef(0);
  const runtimeRefreshRequestRef = useRef(0);
  const runtimeActionRequestRef = useRef(new Map<string, number>());
  const runtimeBusyOwnerRef = useRef<{ key: string; generation: number; requestId: number } | null>(null);
  const runtimePendingClearRequestRef = useRef(0);
  const runtimeStopRequestRef = useRef(new Map<string, number>());
  const runtimeRequestSequenceRef = useRef(0);
  const runtimeSelectionRef = useRef({ generation: 0, activeRunId: null as string | null });
  const runtimePendingReconciledRef = useRef<string | null>(null);
  const [runtimeActionError, setRuntimeActionError] = useState<string | null>(null);
  const [pendingRuntimeRetry, setPendingRuntimeRetry] = useState<PendingRuntimeRetry | null>(null);
  const [releaseActivatingId, setReleaseActivatingId] = useState<string | null>(null);
  const [selectedTarget, setSelectedTarget] = useState<string | null>(null);
  const [busyJob, setBusyJob] = useState(false);
  const [logs, setLogs] = useState<string | null>(null);
  const [linkBusy, setLinkBusy] = useState(false);
  const [metadataDialogVisible, setMetadataDialogVisible] = useState(false);
  const [archiveDialogVisible, setArchiveDialogVisible] = useState(false);
  const [metadataSaving, setMetadataSaving] = useState(false);
  const [metadataName, setMetadataName] = useState("");
  const [metadataDescription, setMetadataDescription] = useState("");
  const [metadataVisibility, setMetadataVisibility] = useState<
    "private" | "shared" | "public"
  >("private");
  const [metadataTargetKey, setMetadataTargetKey] = useState<string | null>(null);
  const [visibilityMenuVisible, setVisibilityMenuVisible] = useState(false);
  const [targetMenuVisible, setTargetMenuVisible] = useState(false);
  const [bindingDialogVisible, setBindingDialogVisible] = useState(false);
  const [bindingSaving, setBindingSaving] = useState(false);
  const [bindingMode, setBindingMode] = useState<"development" | "installed">(
    "development",
  );
  const [bindingEnabled, setBindingEnabled] = useState(true);
  const [bindingPinned, setBindingPinned] = useState(false);
  const [bindingReleaseId, setBindingReleaseId] = useState<string | null>(null);
  const [releaseMenuVisible, setReleaseMenuVisible] = useState(false);
  const [filePreview, setFilePreview] = useState<{
    path: string;
    content: AppFileContent | null;
    loading: boolean;
    error?: string;
  } | null>(null);
  const selectedRuntimeRun = useMemo(
    () => data.runtimeRuns.find((run) => run.id === runtimeRunId) ?? null,
    [data.runtimeRuns, runtimeRunId],
  );
  const effectiveActiveActionIds = useMemo(() => Array.from(new Set([
    ...activeRuntimeActionIds,
    ...(pendingRuntimeRetry ? [pendingRuntimeRetry.actionId] : []),
  ])), [activeRuntimeActionIds, pendingRuntimeRetry]);
  const selectActiveRuntimeRun = useCallback((runId: string | null): RuntimeSelectionToken => {
    const token = selectRuntimeRun(runtimeSelectionRef.current, runId);
    setRuntimeRunId(runId);
    setRuntimeLogs(null);
    setRuntimeLogsLoading(false);
    return token;
  }, []);
  const captureRuntimeScope = useCallback(() => ({ ...runtimeScopeRef.current }), []);
  const isCurrentRuntimeScope = useCallback((token: { key: string; generation: number }) => (
    runtimeScopeRef.current.key === token.key && runtimeScopeRef.current.generation === token.generation
  ), []);
  const nextRuntimeRequestId = useCallback(() => {
    runtimeRequestSequenceRef.current += 1;
    return runtimeRequestSequenceRef.current;
  }, []);
  const syncRuntimeActionActivity = useCallback((runs: readonly AppRuntimeRun[]) => {
    const merged = mergeRuntimeActionActivity({
      activeRunIds: runtimeActiveActionIdsRef.current,
      terminalRunIds: runtimeTerminalRunIdsRef.current,
    }, runs);
    runtimeActiveActionIdsRef.current = merged.activeRunIds;
    runtimeTerminalRunIdsRef.current = merged.terminalRunIds;
    const next = Array.from(runtimeActiveActionIdsRef.current.keys()).sort();
    setActiveRuntimeActionIds((previous) => previous.length === next.length && previous.every((value, index) => value === next[index]) ? previous : next);
  }, []);
  const runtimeScopeKey = buildRuntimeScopeKey(appId, effectiveProjectId, user?.user_id);
  const canReadProjectDetails = !effectiveProjectId || Boolean(
    data.app?.related_project_ids?.includes(effectiveProjectId),
  );
  const projectDetailsUnavailable = Boolean(effectiveProjectId && data.app && !canReadProjectDetails);
  useEffect(() => {
    if (runtimeScopeRef.current.key === runtimeScopeKey) return;
    runtimeScopeRef.current = { key: runtimeScopeKey, generation: runtimeScopeRef.current.generation + 1 };
    runtimeLoadRequestRef.current += 1;
    runtimeRefreshRequestRef.current += 1;
    runtimeRequestSequenceRef.current += 1;
    runtimeActionKeysRef.current.clear();
    runtimeActionInputsRef.current.clear();
    runtimeActionFenceRef.current.clear();
    runtimeActionRequestRef.current.clear();
    runtimeBusyOwnerRef.current = null;
    runtimeStopRequestRef.current.clear();
    runtimePendingClearRequestRef.current += 1;
    runtimePendingReconciledRef.current = null;
    runtimeActiveActionIdsRef.current.clear();
    runtimeTerminalRunIdsRef.current.clear();
    runtimeRunStatusRef.current.clear();
    setData(createEmptyAppDetailData());
    setLoading(true);
    setRefreshing(false);
    setError(null);
    setScreenMode("use");
    setSelectedTarget(null);
    setBusyJob(false);
    setLinkBusy(false);
    setLogs(null);
    setFilePreview(null);
    setReleaseActivatingId(null);
    setMetadataSaving(false);
    setMetadataDialogVisible(false);
    setArchiveDialogVisible(false);
    setBindingSaving(false);
    setBindingDialogVisible(false);
    setVisibilityMenuVisible(false);
    setTargetMenuVisible(false);
    setReleaseMenuVisible(false);
    setRuntimeBusyAction(null);
    setRuntimeActionError(null);
    setPendingRuntimeRetry(null);
    setActiveRuntimeActionIds([]);
    setRuntimeMonitorNonce((value) => value + 1);
    selectActiveRuntimeRun(null);
  }, [runtimeScopeKey, selectActiveRuntimeRun]);
  const noteRuntimeRunStatus = useCallback((run: AppRuntimeRun) => {
    if (runtimeTerminalRunIdsRef.current.has(run.id) && isRuntimeActionActiveStatus(run.status)) return;
    runtimeRunStatusRef.current.set(run.id, run.status);
    if (isRuntimeActionPermanentTerminalStatus(run.status)) runtimeTerminalRunIdsRef.current.add(run.id);
  }, []);
  const isCurrentRuntimeActionRequest = useCallback((scopeToken: { key: string; generation: number }, actionId: string, requestId: number) => (
    isCurrentRuntimeScope(scopeToken) && runtimeActionRequestRef.current.get(actionId) === requestId
  ), [isCurrentRuntimeScope]);

  const load = useCallback(async () => {
    if (!appId) return;
    const loadScopeToken = captureRuntimeScope();
    const loadRequestId = ++runtimeLoadRequestRef.current;
    const loadSelectionToken = captureRuntimeSelection(runtimeSelectionRef.current);
    setError(null);
    try {
      const app = await appsRepo.get(appId, { projectId: effectiveProjectId });
      if (!isCurrentRuntimeScope(loadScopeToken) || runtimeLoadRequestRef.current !== loadRequestId) return;
      if (effectiveProjectId && !app) {
        setData(createEmptyAppDetailData());
        setError("App情報を確認できませんでした。再読み込みしてください。");
        return;
      }
      if (effectiveProjectId && !app?.related_project_ids?.includes(effectiveProjectId)) {
        setRuntimeMonitorNonce((value) => value + 1);
        setData({ ...createEmptyAppDetailData(), app });
        setSelectedTarget(null);
        selectActiveRuntimeRun(null);
        return;
      }
      const [context, targets, files, releases, jobs, projectBindings, taskLinks, runtime, runtimeRuns] =
        await Promise.all([
          appsRepo.getContext(appId, { projectId: effectiveProjectId }),
          appsRepo.listTargets(appId, { projectId: effectiveProjectId }),
          appsRepo.listFiles(appId, { projectId: effectiveProjectId }),
          appsRepo.listReleases(appId, { projectId: effectiveProjectId }),
          appsRepo.listJobs(appId, { projectId: effectiveProjectId }),
          effectiveProjectId ? appsRepo.listProjectApps(effectiveProjectId) : Promise.resolve([]),
          effectiveProjectId
            ? appsRepo.listAppTasks(appId, effectiveProjectId)
            : Promise.resolve([] as TaskAppLink[]),
          appsRepo.getRuntime(appId, { projectId: effectiveProjectId }),
          appsRepo.listRuntimeRuns(appId, { projectId: effectiveProjectId, limit: 20 }),
        ]);
      if (!isCurrentRuntimeScope(loadScopeToken) || runtimeLoadRequestRef.current !== loadRequestId) return;
      const readStatuses = await appsRepo.getDetailReadStatuses(appId, { projectId: effectiveProjectId });
      if (!isCurrentRuntimeScope(loadScopeToken) || runtimeLoadRequestRef.current !== loadRequestId) return;
      setRuntimeMonitorNonce((value) => value + 1);
      setData({
        app,
        context,
        targets,
        files,
        releases,
        jobs,
        projectBindings,
        taskLinks,
        runtime,
        runtimeRuns,
        readStatuses,
      });
      for (const run of runtimeRuns) noteRuntimeRunStatus(run);
      syncRuntimeActionActivity(runtimeRuns);
      const currentRuntimeRunId = runtimeSelectionRef.current.activeRunId;
      if (isCurrentRuntimeSelection(runtimeSelectionRef.current, loadSelectionToken, currentRuntimeRunId)) {
        const nextRuntimeRunId = currentRuntimeRunId && runtimeRuns.some((run) => run.id === currentRuntimeRunId)
          ? currentRuntimeRunId
          : runtimeRuns[0]?.id ?? null;
        selectActiveRuntimeRun(nextRuntimeRunId);
      }
      setSelectedTarget((current) =>
        current && targets.some((target) => target.target_key === current)
          ? current
          : context?.target_key || targets[0]?.target_key || null,
      );
    } catch {
      if (isCurrentRuntimeScope(loadScopeToken) && runtimeLoadRequestRef.current === loadRequestId) {
        setError("App詳細を読み込めませんでした。再読み込みしてください。");
      }
    } finally {
      if (isCurrentRuntimeScope(loadScopeToken) && runtimeLoadRequestRef.current === loadRequestId) setLoading(false);
    }
  }, [appId, captureRuntimeScope, effectiveProjectId, isCurrentRuntimeScope, noteRuntimeRunStatus, runtimeScopeKey, selectActiveRuntimeRun, syncRuntimeActionActivity]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  useEffect(() => {
    if (!appId || !data.app || !canReadProjectDetails) return;
    const reconciliationKey = `${appId}:${effectiveProjectId || "personal"}:${online ? "online" : "offline"}`;
    if (runtimePendingReconciledRef.current === reconciliationKey) return;
    runtimePendingReconciledRef.current = reconciliationKey;
    let cancelled = false;
    const pendingSelectionToken = captureRuntimeSelection(runtimeSelectionRef.current);
    const pendingScopeToken = captureRuntimeScope();
    void (async () => {
      try {
        const pending = await appsRepo.listRuntimePendingActions(appId, { projectId: effectiveProjectId });
        if (cancelled || !isCurrentRuntimeScope(pendingScopeToken)) return;
        const reconciled = await reconcileRuntimePendingActions(
          pending.map((item) => ({ action_id: item.action_id, idempotency_key: item.idempotency_key })),
          (item) => appsRepo.listRuntimeRuns(appId, {
            projectId: effectiveProjectId,
            limit: 1,
            idempotencyKey: item.idempotency_key,
          }),
          (item) => appsRepo.forgetRuntimeAction(appId, item.action_id, {
            projectId: effectiveProjectId,
            idempotencyKey: item.idempotency_key,
          }),
          () => !cancelled && isCurrentRuntimeScope(pendingScopeToken),
        );
        if (!reconciled) return;
        const { matchedRuns, unresolved } = reconciled;
        for (const matched of matchedRuns) {
          noteRuntimeRunStatus(matched);
          syncRuntimeActionActivity([matched]);
        }
        if (matchedRuns.length) {
          setData((previous) => ({
            ...previous,
            runtimeRuns: [
              ...matchedRuns,
              ...previous.runtimeRuns.filter((run) => !matchedRuns.some((item) => item.id === run.id)),
            ],
            runtime: previous.runtime
              ? {
                ...previous.runtime,
                latest_runs: [
                  ...matchedRuns,
                  ...(previous.runtime.latest_runs ?? []).filter((run) => !matchedRuns.some((item) => item.id === run.id)),
                ],
              }
              : previous.runtime,
          }));
        }
        const unresolvedRetry = unresolved[0]
          ? { actionId: unresolved[0].action_id, idempotencyKey: unresolved[0].idempotency_key }
          : null;
        setPendingRuntimeRetry(unresolvedRetry);
        setRuntimeActionError(unresolvedRetry ? "前回のActionは通信状態が不明です。GET照合を継続し、再送はしていません。" : null);
        if (matchedRuns.length && isCurrentRuntimeSelection(runtimeSelectionRef.current, pendingSelectionToken, pendingSelectionToken.activeRunId)) {
          selectActiveRuntimeRun(matchedRuns[0].id);
        }
      } catch {
        // Keep the owner/App/Project-scoped pending key. A reconnect/read
        // failure is still ambiguous and must never turn into a POST retry.
      }
    })();
    return () => { cancelled = true; };
  }, [appId, canReadProjectDetails, captureRuntimeScope, data.app, effectiveProjectId, isCurrentRuntimeScope, noteRuntimeRunStatus, online, runtimeScopeKey, selectActiveRuntimeRun, syncRuntimeActionActivity]);

  useEffect(() => {
    if (!canReadProjectDetails || !data.app || !selectedRuntimeRun?.id || isRuntimeActionPermanentTerminalStatus(selectedRuntimeRun.status) || !online) return;
    const runId = selectedRuntimeRun.id;
    const selectionToken = captureRuntimeSelection(runtimeSelectionRef.current);
    const scopeToken = captureRuntimeScope();
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let consecutiveFailures = 0;
    const scheduleRetry = (delay: number) => {
      if (!cancelled) timer = setTimeout(() => void poll(), delay);
    };
    const poll = async () => {
      if (cancelled || !isCurrentRuntimeScope(scopeToken) || !isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) return;
      try {
        const next = await appsRepo.getRuntimeRun(data.app!.id, runId, { projectId: effectiveProjectId });
        if (cancelled || !isCurrentRuntimeScope(scopeToken) || !isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) return;
        if (!next || next.id !== runId) {
          consecutiveFailures += 1;
          if (consecutiveFailures >= 5) setRuntimeActionError("Action状態の監視を停止しました。再読込または状態更新で再確認してください。");
          else scheduleRetry(runtimePollBackoffMs(consecutiveFailures));
          return;
        }
        consecutiveFailures = 0;
        noteRuntimeRunStatus(next);
        syncRuntimeActionActivity([next]);
        if (isRuntimeActionPermanentTerminalStatus(next.status)) {
          void appsRepo.forgetRuntimeAction(data.app!.id, next.action_id, {
            projectId: effectiveProjectId,
            idempotencyKey: next.idempotency_key,
          });
          setPendingRuntimeRetry((previous) => (
            previous?.idempotencyKey === next.idempotency_key ? null : previous
          ));
          for (const [key, idKey] of runtimeActionKeysRef.current.entries()) {
            if (idKey === next.idempotency_key) runtimeActionKeysRef.current.delete(key);
          }
          runtimeActionInputsRef.current.delete(next.idempotency_key);
          setRuntimeActionError((previous) => (
            previous && (previous.includes("unknown") || previous.includes("通信状態が不明") || previous.includes("前回のActionは")) ? null : previous
          ));
        }
        setData((previous) => ({
          ...previous,
          runtimeRuns: [next, ...previous.runtimeRuns.filter((run) => run.id !== next.id)],
          runtime: previous.runtime
            ? { ...previous.runtime, latest_runs: [next, ...(previous.runtime.latest_runs ?? []).filter((run) => run.id !== next.id)] }
            : previous.runtime,
        }));
        void appsRepo.getRuntimeRunLogs(data.app!.id, runId, { projectId: effectiveProjectId }).then((logs) => {
          if (!cancelled && logs && isCurrentRuntimeScope(scopeToken) && isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) setRuntimeLogs(logs.logs || "ログはありません");
        }).catch(() => {
          // A log read is best effort; a late or transient failure must not
          // replace the selected run or surface an error for a newer scope.
        });
        if (!isRuntimeActionPermanentTerminalStatus(next.status)) scheduleRetry(1200);
      } catch {
        if (cancelled || !isCurrentRuntimeScope(scopeToken) || !isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) return;
        consecutiveFailures += 1;
        if (consecutiveFailures >= 5) setRuntimeActionError("Action状態の監視を停止しました。再読込または状態更新で再確認してください。");
        else scheduleRetry(runtimePollBackoffMs(consecutiveFailures));
      }
    };
    timer = setTimeout(() => void poll(), 1200);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [canReadProjectDetails, captureRuntimeScope, data.app, effectiveProjectId, isCurrentRuntimeScope, noteRuntimeRunStatus, online, runtimeMonitorNonce, runtimeRunId, selectedRuntimeRun?.id, selectedRuntimeRun?.status, syncRuntimeActionActivity]);

  useEffect(() => {
    if (!canReadProjectDetails || !data.app || !runtimeRunId || !online) return;
    const runId = runtimeRunId;
    const selectionToken = captureRuntimeSelection(runtimeSelectionRef.current);
    const scopeToken = captureRuntimeScope();
    let attempts = 0;
    let consecutiveFailures = 0;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const refreshFiles = async () => {
      if (cancelled || !isCurrentRuntimeScope(scopeToken) || !isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) return;
      attempts += 1;
      try {
        const files = await appsRepo.listRuntimeRunFiles(data.app!.id, runId, { projectId: effectiveProjectId });
        if (cancelled || !isCurrentRuntimeScope(scopeToken) || !isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) return;
        consecutiveFailures = 0;
        if (files.length) {
          setData((previous) => ({
            ...previous,
            runtimeRuns: previous.runtimeRuns.map((run) => run.id === runId ? { ...run, files } : run),
            runtime: previous.runtime
              ? { ...previous.runtime, latest_runs: previous.runtime.latest_runs.map((run) => run.id === runId ? { ...run, files } : run) }
              : previous.runtime,
          }));
        }
        const status = runtimeRunStatusRef.current.get(runId);
        const terminal = status ? isRuntimeRunTerminal(status) : false;
        const maxAttempts = terminal ? 5 : 12;
        if (attempts < maxAttempts) timer = setTimeout(() => void refreshFiles(), 1200);
      } catch {
        if (cancelled || !isCurrentRuntimeScope(scopeToken) || !isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) return;
        consecutiveFailures += 1;
        const status = runtimeRunStatusRef.current.get(runId);
        const terminal = status ? isRuntimeRunTerminal(status) : false;
        const maxAttempts = terminal ? 5 : 12;
        if (attempts < maxAttempts) timer = setTimeout(() => void refreshFiles(), runtimePollBackoffMs(consecutiveFailures));
      }
    };
    void refreshFiles();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [canReadProjectDetails, captureRuntimeScope, data.app, effectiveProjectId, isCurrentRuntimeScope, online, runtimeMonitorNonce, runtimeRunId]);

  useEffect(() => {
    if (!canReadProjectDetails || !data.app || !online || runtimeActiveActionIdsRef.current.size === 0) return;
    const scopeToken = captureRuntimeScope();
    let cancelled = false;
    let attempts = 0;
    let consecutiveFailures = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const pollKnownActiveRuns = async () => {
      if (cancelled || !isCurrentRuntimeScope(scopeToken)) return;
      const runIds = Array.from(runtimeActiveActionIdsRef.current.values()).flatMap((ids) => Array.from(ids)).filter((runId) => runId !== runtimeRunId);
      if (!runIds.length) return;
      let failed = false;
      for (const runId of runIds) {
        try {
          const next = await appsRepo.getRuntimeRun(data.app!.id, runId, { projectId: effectiveProjectId });
          if (cancelled || !isCurrentRuntimeScope(scopeToken)) return;
          if (!next || next.id !== runId) {
            failed = true;
            continue;
          }
          noteRuntimeRunStatus(next);
          syncRuntimeActionActivity([next]);
          if (isRuntimeActionPermanentTerminalStatus(next.status)) {
            void appsRepo.forgetRuntimeAction(data.app!.id, next.action_id, {
              projectId: effectiveProjectId,
              idempotencyKey: next.idempotency_key,
            });
            setPendingRuntimeRetry((previous) => (
              previous?.idempotencyKey === next.idempotency_key ? null : previous
            ));
            for (const [key, idKey] of runtimeActionKeysRef.current.entries()) {
              if (idKey === next.idempotency_key) runtimeActionKeysRef.current.delete(key);
            }
            runtimeActionInputsRef.current.delete(next.idempotency_key);
            setRuntimeActionError((previous) => (
              previous && (previous.includes("unknown") || previous.includes("通信状態が不明") || previous.includes("前回のActionは")) ? null : previous
            ));
          }
          setData((previous) => ({
            ...previous,
            runtimeRuns: [next, ...previous.runtimeRuns.filter((run) => run.id !== runId)],
            runtime: previous.runtime
              ? { ...previous.runtime, latest_runs: [next, ...(previous.runtime.latest_runs ?? []).filter((run) => run.id !== runId)] }
              : previous.runtime,
          }));
        } catch {
          if (cancelled || !isCurrentRuntimeScope(scopeToken)) return;
          failed = true;
        }
      }
      if (cancelled || !isCurrentRuntimeScope(scopeToken)) return;
      attempts += 1;
      consecutiveFailures = failed ? consecutiveFailures + 1 : 0;
      if (consecutiveFailures >= 5) {
        setRuntimeActionError("一部Action状態の監視を停止しました。再読込または状態更新で再確認してください。");
        return;
      }
      if (!cancelled && isCurrentRuntimeScope(scopeToken) && attempts < 12 && runtimeActiveActionIdsRef.current.size > 0) {
        timer = setTimeout(() => void pollKnownActiveRuns(), runtimePollBackoffMs(consecutiveFailures));
      }
    };
    timer = setTimeout(() => void pollKnownActiveRuns(), 1200);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeRuntimeActionIds, canReadProjectDetails, captureRuntimeScope, data.app, effectiveProjectId, isCurrentRuntimeScope, noteRuntimeRunStatus, online, runtimeMonitorNonce, runtimeRunId, syncRuntimeActionActivity]);

  const permission = data.context?.permission ?? data.app?.permission;
  const canRun = permissionAtLeast(permission, "runner");
  const canAdmin = permissionAtLeast(permission, "admin");
  const canActivateRelease = permissionAtLeast(permission, "maintainer");
  const binding = useMemo(
    () => data.projectBindings.find((item) => item.app_id === appId),
    [appId, data.projectBindings],
  );
  const selectedTargetObject = data.targets.find(
    (target) => target.target_key === selectedTarget,
  );

  const handleRefresh = async () => {
    const scopeToken = captureRuntimeScope();
    const requestId = ++runtimeRefreshRequestRef.current;
    setRefreshing(true);
    try {
      await load();
    } finally {
      if (isCurrentRuntimeScope(scopeToken) && runtimeRefreshRequestRef.current === requestId) setRefreshing(false);
    }
  };

  const handleActivateRelease = async (release: AppRelease) => {
    if (!data.app || !online || !canActivateRelease) return;
    const scopeToken = captureRuntimeScope();
    setReleaseActivatingId(release.id);
    try {
      const runtime = await appsRepo.activateRelease(data.app.id, release.id, {
        projectId: effectiveProjectId,
        permission,
      });
      if (!isCurrentRuntimeScope(scopeToken)) return;
      setData((previous) => ({ ...previous, runtime }));
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken)) Alert.alert("Releaseを有効化", cause instanceof Error ? cause.message : "Releaseを有効化できませんでした");
    } finally {
      if (isCurrentRuntimeScope(scopeToken)) setReleaseActivatingId(null);
    }
  };

  const openMetadataDialog = () => {
    if (!data.app) return;
    setMetadataName(data.app.name);
    setMetadataDescription(data.app.description ?? "");
    setMetadataVisibility(
      data.app.visibility === "shared" || data.app.visibility === "public"
        ? data.app.visibility
        : "private",
    );
    setMetadataTargetKey(data.app.default_target_key ?? null);
    setMetadataDialogVisible(true);
  };

  const handleMetadataSave = async () => {
    if (!data.app || !canAdmin || !online) return;
    const name = metadataName.trim();
    if (!name) {
      Alert.alert("App設定", "App名を入力してください");
      return;
    }
    const scopeToken = captureRuntimeScope();
    setMetadataSaving(true);
    try {
      const updated = await appsRepo.update(
        data.app.id,
        {
          name,
          description: metadataDescription.trim(),
          visibility: metadataVisibility,
          default_target_key: metadataTargetKey,
        },
        { projectId: effectiveProjectId, permission },
      );
      if (!isCurrentRuntimeScope(scopeToken)) return;
      setData((previous) => ({
        ...previous,
        app: updated,
        context: previous.context
          ? { ...previous.context, app: updated }
          : previous.context,
      }));
      setMetadataDialogVisible(false);
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken)) {
        Alert.alert(
          "App設定",
          cause instanceof Error ? cause.message : "App設定を保存できませんでした",
        );
      }
    } finally {
      if (isCurrentRuntimeScope(scopeToken)) setMetadataSaving(false);
    }
  };

  const handleArchive = async () => {
    if (!data.app || !canAdmin || !online) return;
    const scopeToken = captureRuntimeScope();
    setMetadataSaving(true);
    try {
      await appsRepo.archive(data.app.id, {
        projectId: effectiveProjectId,
        permission,
      });
      if (!isCurrentRuntimeScope(scopeToken)) return;
      setArchiveDialogVisible(false);
      router.replace("/(tabs)/apps");
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken)) {
        Alert.alert(
          "Appをアーカイブ",
          cause instanceof Error ? cause.message : "Appをアーカイブできませんでした",
        );
      }
    } finally {
      if (isCurrentRuntimeScope(scopeToken)) setMetadataSaving(false);
    }
  };

  const openBindingDialog = () => {
    if (!binding) return;
    setBindingMode(binding.binding_mode);
    setBindingEnabled(binding.enabled);
    setBindingPinned(binding.pinned);
    setBindingReleaseId(binding.installed_release_id ?? data.releases[0]?.id ?? null);
    setBindingDialogVisible(true);
  };

  const handleBindingSave = async () => {
    if (!binding || !effectiveProjectId || !canRun || !online) return;
    if (bindingMode === "installed" && !bindingReleaseId) {
      Alert.alert("Project App", "installed モードでは Release を選択してください");
      return;
    }
    const scopeToken = captureRuntimeScope();
    setBindingSaving(true);
    try {
      const updated = await appsRepo.updateProjectApp(
        effectiveProjectId,
        binding.app_id,
        {
          binding_mode: bindingMode,
          enabled: bindingEnabled,
          pinned: bindingPinned,
          installed_release_id:
            bindingMode === "installed" ? bindingReleaseId : null,
        },
        permission,
      );
      if (!isCurrentRuntimeScope(scopeToken)) return;
      setData((previous) => ({
        ...previous,
        projectBindings: [
          ...previous.projectBindings.filter((item) => item.app_id !== updated.app_id),
          updated,
        ],
      }));
      setBindingDialogVisible(false);
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken)) {
        Alert.alert(
          "Project App",
          cause instanceof Error ? cause.message : "Project App設定を保存できませんでした",
        );
      }
    } finally {
      if (isCurrentRuntimeScope(scopeToken)) setBindingSaving(false);
    }
  };

  const handleFilePreview = async (file: AppFile) => {
    if (file.is_dir) return;
    const targetAppId = data.app?.id ?? appId;
    if (!targetAppId) return;
    const scopeToken = captureRuntimeScope();
    setFilePreview({ path: file.path, content: null, loading: true });
    try {
      const content = await appsRepo.getFile(targetAppId, file.path, {
        projectId: effectiveProjectId,
      });
      if (!isCurrentRuntimeScope(scopeToken)) return;
      setFilePreview({
        path: file.path,
        content,
        loading: false,
        ...(content ? {} : { error: "ファイル内容を取得できませんでした" }),
      });
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken)) {
        setFilePreview({
          path: file.path,
          content: null,
          loading: false,
          error: cause instanceof Error ? cause.message : "ファイル内容を取得できませんでした",
        });
      }
    }
  };

  const handleOpenChat = async () => {
    if (!data.app) return;
    const scopeToken = captureRuntimeScope();
    try {
      const session = await createCurrentCharacterSession(effectiveProjectId, {
        localFirst: true,
        userId: user?.user_id,
      });
      if (!isCurrentRuntimeScope(scopeToken)) return;
      router.push({
        pathname: "/(tabs)/chat/[sessionId]",
        params: {
          sessionId: session.id,
          appId: data.app.id,
          appTargetId: selectedTargetObject?.id || "",
          projectId: effectiveProjectId || "",
        },
      });
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken)) Alert.alert("Chat", cause instanceof Error ? cause.message : "Chatを開けませんでした");
    }
  };

  const handleStartJob = async () => {
    if (!data.app || !selectedTarget || !canRun || !online) return;
    const scopeToken = captureRuntimeScope();
    setBusyJob(true);
    try {
      const job = await appsRepo.startJob(
        data.app.id,
        {
          target_key: selectedTarget,
          job_type: "run",
          project_id: effectiveProjectId,
        },
        permission,
      );
      if (!isCurrentRuntimeScope(scopeToken)) return;
      setData((previous) => ({ ...previous, jobs: [job, ...previous.jobs.filter((item) => item.id !== job.id)] }));
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken)) Alert.alert("App実行", cause instanceof Error ? cause.message : "実行を開始できませんでした");
    } finally {
      if (isCurrentRuntimeScope(scopeToken)) setBusyJob(false);
    }
  };

  const handleStopJob = async (job: AppJob) => {
    if (!data.app || !canRun || !online) return;
    const scopeToken = captureRuntimeScope();
    setBusyJob(true);
    try {
      const next = await appsRepo.stopJob(data.app.id, job.id, {
        projectId: effectiveProjectId,
        permission,
      });
      if (!isCurrentRuntimeScope(scopeToken)) return;
      setData((previous) => ({ ...previous, jobs: [next, ...previous.jobs.filter((item) => item.id !== next.id)] }));
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken)) Alert.alert("App実行", cause instanceof Error ? cause.message : "停止できませんでした");
    } finally {
      if (isCurrentRuntimeScope(scopeToken)) setBusyJob(false);
    }
  };

  const handleLogs = async (job: AppJob) => {
    if (!data.app) return;
    const scopeToken = captureRuntimeScope();
    try {
      const result = await appsRepo.getJobLogs(data.app.id, job.id, {
        projectId: effectiveProjectId,
      });
      if (isCurrentRuntimeScope(scopeToken)) setLogs(result?.logs || "ログはありません");
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken)) setLogs(cause instanceof Error ? cause.message : "ログを取得できませんでした");
    }
  };

  const handleProjectLink = async () => {
    if (!data.app || !effectiveProjectId || !canRun || !online) return;
    const scopeToken = captureRuntimeScope();
    setLinkBusy(true);
    try {
      if (binding) {
        await appsRepo.unlinkProjectApp(effectiveProjectId, data.app.id, permission);
        if (!isCurrentRuntimeScope(scopeToken)) return;
        setData((previous) => ({
          ...previous,
          projectBindings: previous.projectBindings.filter((item) => item.app_id !== data.app?.id),
        }));
      } else {
        const linked = await appsRepo.linkProjectApp(
          effectiveProjectId,
          { app_id: data.app.id, binding_mode: "development", enabled: true, pinned: false },
          permission,
        );
        if (!isCurrentRuntimeScope(scopeToken)) return;
        setData((previous) => ({
          ...previous,
          projectBindings: [...previous.projectBindings.filter((item) => item.app_id !== linked.app_id), linked],
        }));
        await load();
      }
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken)) Alert.alert("Project連携", cause instanceof Error ? cause.message : "Project連携を変更できませんでした");
    } finally {
      if (isCurrentRuntimeScope(scopeToken)) setLinkBusy(false);
    }
  };

  const handleRuntimeAction = useCallback(async (actionId: string, input: Record<string, unknown>) => {
    if (!data.app || !online) {
      Alert.alert("App実行", "オフラインではActionを実行できません。再接続後に手動で実行してください。");
      return;
    }
    const app = data.app;
    const actionSelectionToken = captureRuntimeSelection(runtimeSelectionRef.current);
    const actionScopeToken = captureRuntimeScope();
    const requestId = nextRuntimeRequestId();
    let inputKey = "";
    let idempotencyKey = "";
    if (pendingRuntimeRetry && pendingRuntimeRetry.actionId === actionId) {
      setRuntimeActionError("このActionは前回の通信状態を確認中です。再送せず、同じApp・Project範囲で再読込してください。");
      return;
    }
    const activeIds = new Set([
      ...runtimeActiveActionIdsRef.current.keys(),
      ...(pendingRuntimeRetry ? [pendingRuntimeRetry.actionId] : []),
    ]);
    if (shouldBlockRuntimeAction(actionId, activeIds)) {
      setRuntimeActionError("このActionは実行中です。完了または停止後に再実行してください。");
      return;
    }
    if (runtimeActionFenceRef.current.has(actionId)) {
      return;
    }
    runtimeActionRequestRef.current.set(actionId, requestId);
    runtimeActionFenceRef.current.add(actionId);
    const ownsRequest = () => isCurrentRuntimeActionRequest(actionScopeToken, actionId, requestId);
    try {
      const pending = await appsRepo.listRuntimePendingActions(app.id, { projectId: effectiveProjectId });
      if (!ownsRequest()) return;
      const existingPending = pending.find((item) => item.action_id === actionId);
      if (existingPending) {
        setPendingRuntimeRetry({ actionId, idempotencyKey: existingPending.idempotency_key });
        setRuntimeActionError("このActionは前回の通信状態を確認中です。再送せず、同じApp・Project範囲で再読込してください。");
        return;
      }
      inputKey = `${actionId}:${JSON.stringify(input)}`;
      idempotencyKey = runtimeActionKeysRef.current.get(inputKey) || newRuntimeIdempotencyKey();
      runtimeActionKeysRef.current.set(inputKey, idempotencyKey);
      runtimeActionInputsRef.current.set(idempotencyKey, input);
      await appsRepo.rememberRuntimeAction(app.id, {
        action_id: actionId,
        idempotency_key: idempotencyKey,
        created_at: Date.now(),
      }, { projectId: effectiveProjectId });
      if (!ownsRequest()) return;
      setRuntimeActionError(null);
      runtimeBusyOwnerRef.current = { ...actionScopeToken, requestId };
      setRuntimeBusyAction(actionId);
      try {
        const run = await appsRepo.runRuntimeAction(app.id, actionId, input, {
          idempotencyKey,
          projectId: effectiveProjectId,
        });
        if (!ownsRequest()) return;
        noteRuntimeRunStatus(run);
        syncRuntimeActionActivity([run]);
        if (isRuntimeActionPermanentTerminalStatus(run.status)) {
          await appsRepo.forgetRuntimeAction(app.id, actionId, {
            projectId: effectiveProjectId,
            idempotencyKey,
          });
          if (!ownsRequest()) return;
          runtimeActionKeysRef.current.delete(inputKey);
          runtimeActionInputsRef.current.delete(idempotencyKey);
          setPendingRuntimeRetry(null);
        } else {
          setPendingRuntimeRetry({ actionId, idempotencyKey });
          if (run.status === "unknown") {
            setRuntimeActionError("Actionの実行状態が未確定(unknown)です。同じRunの状態照会を継続しています。");
          }
        }
        setData((previous) => ({
          ...previous,
          runtimeRuns: [run, ...previous.runtimeRuns.filter((item) => item.id !== run.id)],
          runtime: previous.runtime
            ? { ...previous.runtime, latest_runs: [run, ...(previous.runtime.latest_runs ?? []).filter((item) => item.id !== run.id)] }
            : previous.runtime,
        }));
        if (isCurrentRuntimeSelection(runtimeSelectionRef.current, actionSelectionToken, actionSelectionToken.activeRunId)) {
          selectActiveRuntimeRun(run.id);
        }
      } catch (cause) {
        if (!ownsRequest()) return;
        // A transport failure does not prove that the POST was rejected. GET
        // the same owner/App/Project projection with the same key, never POST
        // again automatically, and keep the pending key if GET is uncertain.
        const status = runtimeActionHttpStatus(cause);
        if (isDefinitiveRuntimeActionRejectStatus(status)) {
          try {
            await appsRepo.forgetRuntimeAction(app.id, actionId, { projectId: effectiveProjectId, idempotencyKey });
          } catch {
            if (ownsRequest()) {
              setPendingRuntimeRetry({ actionId, idempotencyKey });
              setRuntimeActionError("Actionの拒否を記録できませんでした。再送せず、同じキーの状態確認を続けます。");
            }
            return;
          }
          if (!ownsRequest()) return;
          runtimeActionKeysRef.current.delete(inputKey);
          runtimeActionInputsRef.current.delete(idempotencyKey);
          setPendingRuntimeRetry(null);
          setRuntimeActionError(cause instanceof Error ? cause.message : "Actionを実行できませんでした");
          return;
        }
        try {
          const observed = await appsRepo.listRuntimeRuns(app.id, {
            projectId: effectiveProjectId,
            limit: 50,
            idempotencyKey,
          });
          if (!ownsRequest()) return;
          const matched = observed.find((run) => run.idempotency_key === idempotencyKey);
          if (matched) {
            noteRuntimeRunStatus(matched);
            syncRuntimeActionActivity([matched]);
            if (isRuntimeActionPermanentTerminalStatus(matched.status)) {
              await appsRepo.forgetRuntimeAction(app.id, actionId, {
                projectId: effectiveProjectId,
                idempotencyKey,
              });
              if (!ownsRequest()) return;
              runtimeActionKeysRef.current.delete(inputKey);
              runtimeActionInputsRef.current.delete(idempotencyKey);
              setPendingRuntimeRetry(null);
            } else {
              setPendingRuntimeRetry({ actionId, idempotencyKey });
              if (matched.status === "unknown") {
                setRuntimeActionError("Actionの実行状態が未確定(unknown)です。同じRunの状態照会を継続しています。");
              }
            }
            setData((previous) => ({
              ...previous,
              runtimeRuns: observed,
              runtime: previous.runtime ? { ...previous.runtime, latest_runs: observed } : previous.runtime,
            }));
            if (isCurrentRuntimeSelection(runtimeSelectionRef.current, actionSelectionToken, actionSelectionToken.activeRunId)) {
              selectActiveRuntimeRun(matched.id);
            }
          } else {
            setPendingRuntimeRetry({ actionId, idempotencyKey });
            setRuntimeActionError(`${cause instanceof Error ? cause.message : "通信状態が不明です"}\nActionは再送していません。再読込で状態を確認してください。`);
          }
        } catch (reconcileError) {
          if (!ownsRequest()) return;
          setPendingRuntimeRetry({ actionId, idempotencyKey });
          setRuntimeActionError(`${reconcileError instanceof Error ? reconcileError.message : "実行状態を確認できません"}\nActionは再送していません。再読込で状態を確認してください。`);
        }
      } finally {
        if (ownsRequest() && runtimeBusyOwnerRef.current?.requestId === requestId) {
          runtimeBusyOwnerRef.current = null;
          setRuntimeBusyAction(null);
        }
      }
    } catch (cause) {
      if (!ownsRequest()) return;
      const status = runtimeActionHttpStatus(cause);
      if (idempotencyKey && isDefinitiveRuntimeActionRejectStatus(status)) {
        try {
          await appsRepo.forgetRuntimeAction(app.id, actionId, { projectId: effectiveProjectId, idempotencyKey });
        } catch {
          if (ownsRequest()) {
            setPendingRuntimeRetry({ actionId, idempotencyKey });
            setRuntimeActionError("Actionの拒否を記録できませんでした。再送せず、同じキーの状態確認を続けます。");
          }
          return;
        }
        if (!ownsRequest()) return;
        if (inputKey) runtimeActionKeysRef.current.delete(inputKey);
        runtimeActionInputsRef.current.delete(idempotencyKey);
        setPendingRuntimeRetry(null);
      } else if (idempotencyKey) {
        setPendingRuntimeRetry({ actionId, idempotencyKey });
      }
      setRuntimeActionError(cause instanceof Error ? cause.message : "Actionを実行できませんでした");
    } finally {
      if (ownsRequest()) {
        runtimeActionFenceRef.current.delete(actionId);
        runtimeActionRequestRef.current.delete(actionId);
      }
    }
  }, [data.app, effectiveProjectId, isCurrentRuntimeActionRequest, nextRuntimeRequestId, noteRuntimeRunStatus, online, pendingRuntimeRetry, selectActiveRuntimeRun, syncRuntimeActionActivity]);

  const clearPendingRuntimeAction = useCallback(async () => {
    if (!data.app || !pendingRuntimeRetry) return;
    const scopeToken = captureRuntimeScope();
    const requestId = ++runtimePendingClearRequestRef.current;
    const pending = pendingRuntimeRetry;
    try {
      await appsRepo.forgetRuntimeAction(data.app.id, pending.actionId, {
        projectId: effectiveProjectId,
        idempotencyKey: pending.idempotencyKey,
      });
      if (!isCurrentRuntimeScope(scopeToken) || runtimePendingClearRequestRef.current !== requestId) return;
      for (const [inputKey, idempotencyKey] of runtimeActionKeysRef.current.entries()) {
        if (idempotencyKey === pending.idempotencyKey) runtimeActionKeysRef.current.delete(inputKey);
      }
      runtimeActionInputsRef.current.delete(pending.idempotencyKey);
      setPendingRuntimeRetry(null);
      setRuntimeActionError("pendingを解除しました。重複実行の可能性を確認したうえで、入力を確認して明示的に再実行してください。");
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken) && runtimePendingClearRequestRef.current === requestId) {
        setRuntimeActionError(cause instanceof Error ? cause.message : "pendingを解除できませんでした。再送せず状態確認を続けます。");
      }
    }
  }, [captureRuntimeScope, data.app, effectiveProjectId, isCurrentRuntimeScope, pendingRuntimeRetry]);

  const pickRuntimeFile = useCallback(async (field: AppRuntimeFormField, actionId?: string): Promise<RuntimeFileSelection | null> => {
    if (!data.app || !online || !actionId) return null;
    const app = data.app;
    const scopeToken = captureRuntimeScope();
    try {
      const result = await DocumentPicker.getDocumentAsync({
        copyToCacheDirectory: true,
        multiple: false,
        type: "*/*",
      });
      if (!isCurrentRuntimeScope(scopeToken) || result.canceled || !result.assets[0]) return null;
      const asset = result.assets[0];
      const metadata = await appsRepo.uploadRuntimeInputFile(
        app.id,
        actionId,
        {
          uri: asset.uri,
          name: asset.name || field.label || field.name,
          mimeType: asset.mimeType,
          size: asset.size,
        },
        { projectId: effectiveProjectId },
      );
      if (!isCurrentRuntimeScope(scopeToken)) return null;
      return {
        file_id: metadata.id,
        name: metadata.filename,
        size_bytes: metadata.size_bytes,
        content_type: metadata.content_type,
      };
    } catch (cause) {
      if (!isCurrentRuntimeScope(scopeToken)) return null;
      throw cause;
    }
  }, [captureRuntimeScope, data.app, effectiveProjectId, isCurrentRuntimeScope, online]);

  const handleStopRuntimeRun = useCallback(async (run: AppRuntimeRun) => {
    if (!data.app || !online || !canRun) return;
    const selectionToken = selectActiveRuntimeRun(run.id);
    const scopeToken = captureRuntimeScope();
    const requestId = nextRuntimeRequestId();
    runtimeStopRequestRef.current.set(run.id, requestId);
    const ownsRequest = () => isCurrentRuntimeScope(scopeToken) && runtimeStopRequestRef.current.get(run.id) === requestId;
    runtimeBusyOwnerRef.current = { ...scopeToken, requestId };
    setRuntimeBusyAction(run.action_id || `stop:${run.id}`);
    setRuntimeActionError(null);
    try {
      const stopped = await appsRepo.stopRuntimeRun(data.app.id, run.id, {
        projectId: effectiveProjectId,
        permission,
      });
      if (!ownsRequest()) return;
      noteRuntimeRunStatus(stopped);
      syncRuntimeActionActivity([stopped]);
      setData((previous) => ({
        ...previous,
        runtimeRuns: [stopped, ...previous.runtimeRuns.filter((item) => item.id !== stopped.id)],
        runtime: previous.runtime
          ? { ...previous.runtime, latest_runs: [stopped, ...(previous.runtime.latest_runs ?? []).filter((item) => item.id !== stopped.id)] }
          : previous.runtime,
      }));
      if (!isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, run.id)) return;
    } catch (cause) {
      if (ownsRequest()) setRuntimeActionError(cause instanceof Error ? cause.message : "Actionを停止できませんでした");
    } finally {
      if (ownsRequest() && runtimeBusyOwnerRef.current?.requestId === requestId) {
        runtimeBusyOwnerRef.current = null;
        setRuntimeBusyAction(null);
        runtimeStopRequestRef.current.delete(run.id);
      }
    }
  }, [canRun, captureRuntimeScope, data.app, effectiveProjectId, isCurrentRuntimeScope, nextRuntimeRequestId, noteRuntimeRunStatus, online, permission, selectActiveRuntimeRun, syncRuntimeActionActivity]);

  const refreshRuntimeRun = useCallback(async (run: AppRuntimeRun) => {
    if (!data.app || !online) return;
    const runId = run.id;
    const selectionToken = selectActiveRuntimeRun(runId);
    const scopeToken = captureRuntimeScope();
    setRuntimeMonitorNonce((value) => value + 1);
    try {
      const [next, logs, files] = await Promise.all([
        appsRepo.getRuntimeRun(data.app.id, runId, { projectId: effectiveProjectId }),
        appsRepo.getRuntimeRunLogs(data.app.id, runId, { projectId: effectiveProjectId }),
        appsRepo.listRuntimeRunFiles(data.app.id, runId, { projectId: effectiveProjectId }),
      ]);
      if (!next || next.id !== runId || !isCurrentRuntimeScope(scopeToken) || !isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) return;
      noteRuntimeRunStatus(next);
      syncRuntimeActionActivity([next]);
      setData((previous) => ({
        ...previous,
        runtimeRuns: [files.length ? { ...next, files } : next, ...previous.runtimeRuns.filter((item) => item.id !== runId)],
        runtime: previous.runtime
          ? { ...previous.runtime, latest_runs: [next, ...(previous.runtime.latest_runs ?? []).filter((item) => item.id !== runId)] }
          : previous.runtime,
      }));
      if (logs && isCurrentRuntimeScope(scopeToken) && isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) setRuntimeLogs(logs.logs || "ログはありません");
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken) && isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) {
        setRuntimeActionError(cause instanceof Error ? cause.message : "Action状態を更新できませんでした");
      }
    }
  }, [captureRuntimeScope, data.app, effectiveProjectId, isCurrentRuntimeScope, noteRuntimeRunStatus, online, selectActiveRuntimeRun, syncRuntimeActionActivity]);

  const openRuntimeLogs = useCallback(async (run: AppRuntimeRun) => {
    if (!data.app || !online) return;
    const selectionToken = selectActiveRuntimeRun(run.id);
    const scopeToken = captureRuntimeScope();
    setRuntimeLogsLoading(true);
    try {
      const result = await appsRepo.getRuntimeRunLogs(data.app.id, run.id, { projectId: effectiveProjectId });
      if (isCurrentRuntimeScope(scopeToken) && isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, run.id)) {
        setRuntimeLogs(result?.logs || "ログはありません");
      }
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken) && isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, run.id)) {
        setRuntimeLogs(cause instanceof Error ? cause.message : "ログを取得できませんでした");
      }
    } finally {
      if (isCurrentRuntimeScope(scopeToken) && isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, run.id)) setRuntimeLogsLoading(false);
    }
  }, [captureRuntimeScope, data.app, effectiveProjectId, isCurrentRuntimeScope, online, selectActiveRuntimeRun]);

  const openRuntimeFile = useCallback(async (file: AppRuntimeFileRef) => {
    const runId = file.run_id || runtimeRunId || null;
    const fileId = file.file_id || file.id || file.artifact_id || "";
    if (!data.app || !runId || !fileId || !online) return;
    const selectionToken = selectActiveRuntimeRun(runId);
    const scopeToken = captureRuntimeScope();
    try {
      const metadata = await appsRepo.getRuntimeRunFile(data.app.id, runId, fileId, { projectId: effectiveProjectId });
      if (!isCurrentRuntimeScope(scopeToken) || !isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) return;
      if (!metadata) {
        Alert.alert("ファイル", "ファイルを取得できませんでした");
        return;
      }
      const bytes = await appsRepo.downloadRuntimeRunFileBytes(data.app.id, runId, fileId, { projectId: effectiveProjectId });
      if (!isCurrentRuntimeScope(scopeToken) || !isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) return;
      const cacheRoot = FileSystem.cacheDirectory;
      if (!cacheRoot) throw new Error("ファイルの一時保存先を利用できません");
      const safeName = (metadata.filename || file.filename || fileId).replace(/[\\/:*?"<>|]/g, "_");
      const temporaryUri = `${cacheRoot}app-run-${Date.now().toString(36)}-${safeName}`;
      try {
        await FileSystem.writeAsStringAsync(temporaryUri, bytesToBase64(bytes), {
          encoding: FileSystem.EncodingType.Base64,
        });
        const entry: FilesEntry = {
          name: safeName,
          path: temporaryUri,
          type: "file",
          size: metadata.size_bytes ?? undefined,
          mimeType: metadata.content_type ?? undefined,
          source: "server",
        };
        if (!isCurrentRuntimeScope(scopeToken) || !isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) return;
        await downloadFileToDevice(entry, async () => temporaryUri);
      } finally {
        await FileSystem.deleteAsync(temporaryUri, { idempotent: true }).catch(() => undefined);
      }
    } catch (cause) {
      if (isCurrentRuntimeScope(scopeToken) && isCurrentRuntimeSelection(runtimeSelectionRef.current, selectionToken, runId)) {
        Alert.alert("ファイル", cause instanceof Error ? cause.message : "ファイルを保存できませんでした");
      }
    }
  }, [captureRuntimeScope, data.app, effectiveProjectId, isCurrentRuntimeScope, online, runtimeRunId, selectActiveRuntimeRun]);

  if (!appId) {
    return <View style={styles.container}><ScreenHeader title="App" onBack={() => router.back()} /><Text style={styles.error}>App IDがありません</Text></View>;
  }

  return (
    <View style={styles.container}>
      <ScreenHeader
        title={data.app?.name || "App"}
        subtitle={data.app?.slug}
        onBack={() => router.back()}
        right={
          <View style={styles.headerActions}>
            {screenMode === "manage" && !projectDetailsUnavailable ? <>
              <IconButton
                icon="pencil-outline"
                iconColor="#89b4fa"
                style={styles.headerAction}
                onPress={openMetadataDialog}
                disabled={!data.app || !canAdmin || !online}
                accessibilityLabel="App情報を編集"
              />
              <IconButton
                icon="archive-outline"
                iconColor="#f38ba8"
                style={styles.headerAction}
                onPress={() => setArchiveDialogVisible(true)}
                disabled={!data.app || !canAdmin || !online}
                accessibilityLabel="Appをアーカイブ"
              />
            </> : null}
            {!projectDetailsUnavailable ? <IconButton
              icon="chat-outline"
              iconColor="#89b4fa"
              style={styles.headerAction}
              onPress={() => void handleOpenChat()}
              disabled={!data.app}
              accessibilityLabel="AppコンテキストでChatを開く"
            /> : null}
          </View>
        }
      />
      {error ? (
        <View>
          <Text style={styles.error}>{error}</Text>
          <Button mode="text" onPress={() => void handleRefresh()}>再読み込み</Button>
        </View>
      ) : null}
      {loading && !data.app ? (
        <View style={styles.centered}><ActivityIndicator size="large" color="#89b4fa" /></View>
      ) : (
        <ScrollView
          contentContainerStyle={styles.content}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={handleRefresh} tintColor="#89b4fa" />}
        >
          {projectDetailsUnavailable ? (
            <>
              <Surface style={styles.identityCard} elevation={1}>
                <View style={styles.identityIcon}><Icon source="application-brackets-outline" size={32} color="#89b4fa" /></View>
                <View style={styles.identityCopy}>
                  <Text style={styles.identityTitle}>{data.app?.name || "App"}</Text>
                  <Text style={styles.identityMeta}>{data.app?.slug || ""} · {permission || "権限情報なし"}</Text>
                  {data.app?.description ? <Text style={styles.identityDescription}>{data.app.description}</Text> : null}
                </View>
              </Surface>
              <Section title="Projectとの関係">
                <Text style={styles.bodyText}>このProjectには未連携です</Text>
                <Button
                  mode="outlined"
                  onPress={() => void handleProjectLink()}
                  disabled={!canRun || !online || linkBusy}
                  loading={linkBusy}
                  style={styles.actionButton}
                >
                  Projectに追加
                </Button>
                {!canRun ? <Text style={styles.mutedText}>viewer は Project連携を変更できません</Text> : null}
              </Section>
              <Section title="Project固有の情報">
                <Text style={styles.bodyText}>ProjectにAppを追加して連携を有効にすると、Project固有の詳細を表示できます。</Text>
              </Section>
            </>
          ) : <>
          <SegmentedButtons
            value={screenMode}
            onValueChange={(value) => setScreenMode(value as "use" | "manage")}
            buttons={[
              { value: "use", label: "利用", icon: "play-circle-outline" },
              { value: "manage", label: "管理", icon: "cog-outline" },
            ]}
            style={styles.modeSwitch}
          />
          {screenMode === "use" ? (
            <UseRuntimePanel
              appName={data.app?.name || "App"}
              runtime={data.runtime}
              runtimeStatus={data.readStatuses.runtime}
              runtimeRunsStatus={data.readStatuses.runtimeRuns}
              runs={data.runtimeRuns}
              canRun={canRun}
              online={online}
              busyActionId={runtimeBusyAction}
              activeActionIds={effectiveActiveActionIds}
              runtimeRunId={runtimeRunId}
              onRun={handleRuntimeAction}
              onStop={handleStopRuntimeRun}
              canStop={canRun && online}
              onRefreshRun={refreshRuntimeRun}
              onPickFile={pickRuntimeFile}
              runtimeActionError={runtimeActionError}
              pendingRuntimeRetry={pendingRuntimeRetry}
              onReload={() => void handleRefresh()}
              onClearPending={clearPendingRuntimeAction}
              onOpenLogs={openRuntimeLogs}
              onOpenFile={openRuntimeFile}
              runtimeLogs={runtimeLogs}
              logsLoading={runtimeLogsLoading}
            />
          ) : <>
          <Surface style={styles.identityCard} elevation={1}>
            <View style={styles.identityIcon}><Icon source="application-brackets-outline" size={32} color="#89b4fa" /></View>
            <View style={styles.identityCopy}>
              <Text style={styles.identityTitle}>{data.app?.name || "App"}</Text>
              <Text style={styles.identityMeta}>{data.app?.slug || ""} · {permission || "権限情報なし"}</Text>
              {data.app?.description ? <Text style={styles.identityDescription}>{data.app.description}</Text> : null}
            </View>
          </Surface>
          {!canAdmin ? <Text style={styles.mutedText}>viewer/runner は App metadata編集・アーカイブを実行できません</Text> : null}
          {canAdmin && !online ? <Text style={styles.offlineText}>オフラインでは App metadata編集・アーカイブを実行できません</Text> : null}

          {effectiveProjectId ? (
            <Section title="Projectとの関係">
              <Text style={styles.bodyText}>{binding ? `${binding.binding_mode} / ${binding.enabled ? "有効" : "無効"} / ${binding.pinned ? "固定" : "未固定"}` : "このProjectには未連携です"}</Text>
              {binding?.binding_mode === "installed" ? (
                <Text style={styles.mutedText}>
                  Release: {binding.installed_release_id || "未選択"}
                </Text>
              ) : null}
              {binding ? (
                <Button
                  mode="outlined"
                  icon="tune-variant"
                  onPress={openBindingDialog}
                  disabled={!canRun || !online || linkBusy || bindingSaving}
                  style={styles.actionButton}
                >
                  Project App設定
                </Button>
              ) : null}
              <Button
                mode="outlined"
                onPress={() => void handleProjectLink()}
                disabled={!canRun || !online || linkBusy}
                loading={linkBusy}
                style={styles.actionButton}
              >
                {binding ? "Project連携を解除" : "Projectに追加"}
              </Button>
              {!canRun ? <Text style={styles.mutedText}>viewer は Project連携を変更できません</Text> : null}
            </Section>
          ) : null}

          <Section title={`関連 Tasks (${data.taskLinks.length})`}>
            {data.taskLinks.length === 0 ? (
              <Text style={styles.mutedText}>関連付けられた Task はありません</Text>
            ) : (
              data.taskLinks.slice(0, 20).map((link) => (
                <Button
                  key={link.id}
                  mode="text"
                  icon="checkbox-marked-outline"
                  contentStyle={styles.taskLinkButtonContent}
                  onPress={() => router.push({ pathname: "/(tabs)/tasks/[taskId]", params: { taskId: link.task_id } })}
                >
                  {(link as TaskAppLink & { task?: Record<string, unknown> }).task?.title as string || link.task_id} · {link.relation_type}
                </Button>
              ))
            )}
          </Section>

          <Section title="Chat App Context">
            <ReadStateNotice status={data.readStatuses.context} onReload={() => void handleRefresh()} />
            {data.context?.readme?.trim() ? (
              <Text style={styles.bodyText}>{data.context.readme.trim()}</Text>
            ) : data.readStatuses.context.state === "fresh" ? (
              <Text style={styles.bodyText}>App Context の README はありません</Text>
            ) : null}
            <Button mode="contained-tonal" icon="chat-outline" onPress={() => void handleOpenChat()} disabled={!data.app} style={styles.actionButton}>
              このAppでChatを開く
            </Button>
          </Section>

          <Section title={detailSectionTitle("Targets", data.targets.length, data.readStatuses.targets)}>
            <ReadStateNotice status={data.readStatuses.targets} onReload={() => void handleRefresh()} />
            {data.targets.length === 0 && data.readStatuses.targets.state === "fresh" ? <Text style={styles.mutedText}>Targetはありません</Text> : null}
            <View style={styles.chipRow}>
              {data.targets.map((target) => (
                <Chip key={target.id} selected={selectedTarget === target.target_key} onPress={() => setSelectedTarget(target.target_key)} style={styles.chip}>
                  {target.display_name || target.target_key}
                </Chip>
              ))}
            </View>
            <Button mode="contained" icon="play" onPress={() => void handleStartJob()} disabled={!canRun || !online || !selectedTarget || busyJob} loading={busyJob} style={styles.actionButton}>
              実行
            </Button>
            {!canRun ? <Text style={styles.mutedText}>viewer には実行ボタンを表示しません（runner権限が必要）</Text> : null}
            {!online ? <Text style={styles.offlineText}>オフラインでは実行できません</Text> : null}
          </Section>

          <Section title={detailSectionTitle("実行履歴", data.jobs.length, data.readStatuses.jobs)}>
            <ReadStateNotice status={data.readStatuses.jobs} onReload={() => void handleRefresh()} />
            {data.jobs.length === 0 && data.readStatuses.jobs.state === "fresh" ? <Text style={styles.mutedText}>実行履歴はありません</Text> : null}
            {data.jobs.slice(0, 20).map((job) => (
              <View key={job.id} style={styles.jobRow}>
                <View style={styles.jobCopy}>
                  <Text style={styles.bodyText}>{job.job_type} · {formatJobStatus(job)}</Text>
                  <Text style={styles.mutedText}>{job.started_at || job.ended_at || "時刻情報なし"}</Text>
                </View>
                <View style={styles.jobActions}>
                  <IconButton icon="text-box-outline" iconColor="#89b4fa" onPress={() => void handleLogs(job)} accessibilityLabel="ジョブログを表示" />
                  {canRun && online && ["queued", "running", "started"].includes(job.status) ? <IconButton icon="stop-circle-outline" iconColor="#f38ba8" onPress={() => void handleStopJob(job)} disabled={busyJob} accessibilityLabel="ジョブを停止" /> : null}
                </View>
              </View>
            ))}
          </Section>

          <Section title={detailSectionTitle("Files", data.files.length, data.readStatuses.files)}>
            <ReadStateNotice status={data.readStatuses.files} onReload={() => void handleRefresh()} />
            <Text style={styles.mutedText}>読み取り専用。モバイルから source import / ファイル編集はできません。</Text>
            {data.files.length === 0 && data.readStatuses.files.state === "fresh" ? <Text style={styles.mutedText}>Filesはありません</Text> : null}
            {data.files.slice(0, 30).map((file) => (
              <Button
                key={file.path}
                mode="text"
                icon={file.is_dir ? "folder-outline" : "file-document-outline"}
                contentStyle={styles.fileButtonContent}
                onPress={() => void handleFilePreview(file)}
                disabled={Boolean(file.is_dir)}
                accessibilityLabel={file.is_dir ? `${file.path}フォルダー` : `${file.path}をプレビュー`}
              >
                {file.path}
              </Button>
            ))}
          </Section>

          <Section title={detailSectionTitle("Releases", data.releases.length, data.readStatuses.releases)}>
            <ReadStateNotice status={data.readStatuses.releases} onReload={() => void handleRefresh()} />
            <Text style={styles.mutedText}>Releaseの作成・公開はWeb/desktopで行います。maintainer以上は有効版を切り替えられます。</Text>
            {data.releases.length === 0 && data.readStatuses.releases.state === "fresh" ? <Text style={styles.mutedText}>Releaseはありません</Text> : null}
            {data.runtime?.activation?.release_id ? <Text style={styles.bodyText}>現在の有効Release: {data.runtime.activation.release_id}</Text> : null}
            {data.releases.slice(0, 20).map((release) => {
              const active = data.runtime?.activation?.release_id === release.id;
              return (
                <View key={release.id} style={styles.releaseRow}>
                  <View style={styles.jobCopy}>
                    <Text style={styles.fileRow}>{release.version} · {release.status}</Text>
                    {active ? <Text style={styles.activeReleaseText}>利用画面で使用中</Text> : null}
                  </View>
                  {canActivateRelease ? (
                    <Button
                      mode={active ? "contained-tonal" : "outlined"}
                      compact
                      disabled={!online || active || releaseActivatingId !== null}
                      loading={releaseActivatingId === release.id}
                      onPress={() => void handleActivateRelease(release)}
                    >
                      {active ? "有効" : "有効化"}
                    </Button>
                  ) : null}
                </View>
              );
            })}
          </Section>

          <Divider style={styles.bottomDivider} />
          <Text style={styles.mutedText}>Git復元、README/manifest編集、Grant管理、embedded runtime はモバイル対象外です。</Text>
          </>}
          </>}
        </ScrollView>
      )}
      <Portal>
        <Dialog visible={logs !== null} onDismiss={() => setLogs(null)}>
          <Dialog.Title>ジョブログ</Dialog.Title>
          <Dialog.Content><Text selectable style={styles.logText}>{logs || ""}</Text></Dialog.Content>
          <Dialog.Actions><Button onPress={() => setLogs(null)}>閉じる</Button></Dialog.Actions>
        </Dialog>
        <Dialog
          visible={metadataDialogVisible}
          onDismiss={() => !metadataSaving && setMetadataDialogVisible(false)}
        >
          <Dialog.Title>App情報を編集</Dialog.Title>
          <Dialog.Content>
            <TextInput
              label="App名"
              mode="outlined"
              value={metadataName}
              onChangeText={setMetadataName}
              style={styles.dialogInput}
            />
            <TextInput
              label="説明"
              mode="outlined"
              value={metadataDescription}
              onChangeText={setMetadataDescription}
              multiline
              style={styles.dialogInput}
            />
            <Text style={styles.dialogLabel}>公開範囲</Text>
            <Menu
              visible={visibilityMenuVisible}
              onDismiss={() => setVisibilityMenuVisible(false)}
              anchor={
                <Button mode="outlined" onPress={() => setVisibilityMenuVisible(true)}>
                  {metadataVisibility}
                </Button>
              }
            >
              {(["private", "shared", "public"] as const).map((value) => (
                <Menu.Item
                  key={value}
                  title={value}
                  leadingIcon={metadataVisibility === value ? "check" : undefined}
                  onPress={() => {
                    setMetadataVisibility(value);
                    setVisibilityMenuVisible(false);
                  }}
                />
              ))}
            </Menu>
            <Text style={styles.dialogLabel}>既定Target</Text>
            <Menu
              visible={targetMenuVisible}
              onDismiss={() => setTargetMenuVisible(false)}
              anchor={
                <Button mode="outlined" onPress={() => setTargetMenuVisible(true)}>
                  {metadataTargetKey || "未選択"}
                </Button>
              }
            >
              <Menu.Item
                title="未選択"
                onPress={() => {
                  setMetadataTargetKey(null);
                  setTargetMenuVisible(false);
                }}
              />
              {data.targets.map((target) => (
                <Menu.Item
                  key={target.id}
                  title={target.target_key}
                  leadingIcon={metadataTargetKey === target.target_key ? "check" : undefined}
                  onPress={() => {
                    setMetadataTargetKey(target.target_key);
                    setTargetMenuVisible(false);
                  }}
                />
              ))}
            </Menu>
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setMetadataDialogVisible(false)} disabled={metadataSaving}>キャンセル</Button>
            <Button onPress={() => void handleMetadataSave()} loading={metadataSaving} disabled={!canAdmin || !online || metadataSaving}>保存</Button>
          </Dialog.Actions>
        </Dialog>
        <Dialog
          visible={archiveDialogVisible}
          onDismiss={() => !metadataSaving && setArchiveDialogVisible(false)}
        >
          <Dialog.Title>Appをアーカイブ</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.bodyText}>「{data.app?.name || "App"}」をアーカイブしますか？</Text>
            {!online ? <Text style={styles.offlineText}>オフラインでは実行できません</Text> : null}
            {!canAdmin ? <Text style={styles.mutedText}>admin権限が必要です</Text> : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setArchiveDialogVisible(false)} disabled={metadataSaving}>キャンセル</Button>
            <Button
              onPress={() => void handleArchive()}
              loading={metadataSaving}
              disabled={!canAdmin || !online || metadataSaving}
              textColor="#f38ba8"
            >
              アーカイブ
            </Button>
          </Dialog.Actions>
        </Dialog>
        <Dialog
          visible={bindingDialogVisible}
          onDismiss={() => !bindingSaving && setBindingDialogVisible(false)}
        >
          <Dialog.Title>Project App設定</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogLabel}>Binding mode</Text>
            <View style={styles.chipRow}>
              {(["development", "installed"] as const).map((value) => (
                <Chip
                  key={value}
                  selected={bindingMode === value}
                  onPress={() => setBindingMode(value)}
                  style={styles.chip}
                >
                  {value}
                </Chip>
              ))}
            </View>
            <View style={styles.switchRow}>
              <Text style={styles.bodyText}>有効</Text>
              <Switch value={bindingEnabled} onValueChange={setBindingEnabled} />
            </View>
            <View style={styles.switchRow}>
              <Text style={styles.bodyText}>Pinned</Text>
              <Switch value={bindingPinned} onValueChange={setBindingPinned} />
            </View>
            {bindingMode === "installed" ? (
              <>
                <Text style={styles.dialogLabel}>Installed Release</Text>
                <Menu
                  visible={releaseMenuVisible}
                  onDismiss={() => setReleaseMenuVisible(false)}
                  anchor={
                    <Button mode="outlined" onPress={() => setReleaseMenuVisible(true)}>
                      {data.releases.find((release) => release.id === bindingReleaseId)?.version || "選択してください"}
                    </Button>
                  }
                >
                  {data.releases.map((release) => (
                    <Menu.Item
                      key={release.id}
                      title={`${release.version} (${release.status})`}
                      disabled={release.status !== "published"}
                      leadingIcon={bindingReleaseId === release.id ? "check" : undefined}
                      onPress={() => {
                        setBindingReleaseId(release.id);
                        setReleaseMenuVisible(false);
                      }}
                    />
                  ))}
                </Menu>
              </>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setBindingDialogVisible(false)} disabled={bindingSaving}>キャンセル</Button>
            <Button
              onPress={() => void handleBindingSave()}
              loading={bindingSaving}
              disabled={!canRun || !online || bindingSaving || (bindingMode === "installed" && !bindingReleaseId)}
            >
              保存
            </Button>
          </Dialog.Actions>
        </Dialog>
        <Dialog
          visible={filePreview !== null}
          onDismiss={() => setFilePreview(null)}
        >
          <Dialog.Title>{filePreview?.path || "ファイル"}</Dialog.Title>
          <Dialog.Content>
            {filePreview?.loading ? <ActivityIndicator color="#89b4fa" /> : null}
            {filePreview?.error ? <Text style={styles.error}>{filePreview.error}</Text> : null}
            {filePreview?.content ? (
              <ScrollView style={styles.previewScroll}>
                <Text selectable style={styles.previewText}>{filePreview.content.content}</Text>
              </ScrollView>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions><Button onPress={() => setFilePreview(null)}>閉じる</Button></Dialog.Actions>
        </Dialog>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: BACKGROUND },
  centered: { flex: 1, justifyContent: "center", alignItems: "center" },
  content: { padding: 12, paddingBottom: 48 },
  modeSwitch: { marginBottom: 10 },
  usePanel: { gap: 10 },
  headerActions: {
    flexDirection: "row",
    alignItems: "center",
    flexWrap: "wrap",
    maxWidth: "100%",
    minWidth: 0,
  },
  headerAction: { width: 44, height: 44, flexShrink: 0, margin: 0 },
  identityCard: { flexDirection: "row", alignItems: "center", backgroundColor: SURFACE, padding: 16, borderRadius: 14, marginBottom: 10 },
  identityIcon: { width: 52, height: 52, borderRadius: 26, backgroundColor: "#313244", justifyContent: "center", alignItems: "center", marginRight: 12 },
  identityCopy: { flex: 1 },
  identityTitle: { color: TEXT, fontSize: 20, fontWeight: "700" },
  identityMeta: { color: MUTED, marginTop: 3 },
  identityDescription: { color: "#bac2de", marginTop: 8, lineHeight: 19 },
  section: { backgroundColor: SURFACE, padding: 14, borderRadius: 14, marginBottom: 10 },
  sectionTitle: { color: TEXT, fontWeight: "700", fontSize: 16, marginBottom: 10 },
  bodyText: { color: TEXT, lineHeight: 20 },
  mutedText: { color: MUTED, fontSize: 12, lineHeight: 18 },
  offlineText: { color: "#f9e2af", marginTop: 8, fontSize: 12 },
  runtimeErrorBlock: { gap: 8, borderWidth: StyleSheet.hairlineWidth, borderColor: "#f38ba8", borderRadius: 8, padding: 8 },
  actionButton: { alignSelf: "flex-start", marginTop: 12 },
  taskLinkButtonContent: { justifyContent: "flex-start" },
  fileButtonContent: { justifyContent: "flex-start" },
  dialogInput: { marginBottom: 10 },
  dialogLabel: { color: MUTED, marginTop: 8, marginBottom: 6 },
  switchRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginTop: 8 },
  previewScroll: { maxHeight: 360 },
  previewText: { color: TEXT, fontFamily: "monospace", fontSize: 12 },
  chipRow: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  chip: { marginBottom: 4 },
  jobRow: { flexDirection: "row", alignItems: "center", borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: "#313244", paddingVertical: 8 },
  jobCopy: { flex: 1 },
  jobActions: { flexDirection: "row" },
  releaseRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: "#313244", paddingVertical: 6, gap: 8 },
  activeReleaseText: { color: "#a6e3a1", fontSize: 11 },
  fileRow: { color: "#bac2de", fontSize: 13, paddingVertical: 3 },
  bottomDivider: { marginVertical: 12, backgroundColor: "#313244" },
  error: { color: "#f38ba8", padding: 16 },
  logText: { color: TEXT, fontFamily: "monospace", maxHeight: 320 },
  runtimeLogSurface: { backgroundColor: "#0b0b12", padding: 10, borderRadius: 8, marginTop: 8 },
});

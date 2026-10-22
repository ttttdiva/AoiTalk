/**
 * Docs 一覧画面
 *
 * トップレベルページ（parent_id null / archived_at null）をフラットリスト表示する。
 * 各行タップで詳細（アウトライン編集）へ。FAB で新規ページ作成、ヘッダから検索とデイリーページ。
 */

import React, { useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  AppState,
  FlatList,
  Platform,
  type LayoutChangeEvent,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
  Pressable,
  RefreshControl,
  StyleSheet,
  type StyleProp,
  type ViewToken,
  View,
  type ViewStyle,
} from "react-native";
import {
  ActivityIndicator,
  Divider,
  FAB,
  IconButton,
  List,
  Text,
  TextInput,
} from "react-native-paper";
import { useFocusEffect, usePathname, useRouter } from "expo-router";
import { ScreenHeader, type ScreenHeaderInteractionEvent } from "../../../components/screen-header";
import { RichTitle } from "../../../components/docs/rich-title";
import { ClipIngestDialog } from "../../../components/docs/clip-ingest-dialog";
import { createDocsListLayoutTrace, type DocsListLayoutSnapshot } from "../../../components/docs/list-layout-trace";
import { runDocsSyncDiagnostic, type DocsSyncDiagnosticSnapshot } from "../../../sync/api";
import { DocsListLayoutInspector } from "../../../components/docs/list-layout-inspector";
import {
  createDocsListPositionController,
  createDocsListPositionGuard,
  type DocsListCellLayout,
  type DocsListMeasuredSnapshot,
  type DocsListMeasurementToken,
  type DocsListSeedRestoreGeometry,
  type DocsListRestoreLifecycle,
  type DocsListViewportLayout,
} from "../../../components/docs/list-position-guard";
import { docsRepo, type DocsPagesReadSummary } from "../../../repositories/docs";
import { docsApi } from "../../../lib/docs-api";
import { useNetworkStore } from "../../../stores/network";
import { useSyncStatusStore } from "../../../stores/sync-status";
import {
  getSyncDiagnosticSnapshot,
  getSyncDiagnosticRequestHandle,
  runSync,
  subscribeSyncDiagnostics,
  type SyncDiagnosticRequestHandle,
  type SyncDiagnosticReceipt,
  type SyncDiagnosticSnapshot,
  type SyncPullDiagnostic,
} from "../../../sync/engine";
import type { DocsNode } from "../../../types/api";
import { useAuth } from "../../../contexts/AuthContext";

type SearchResult = { id: string; title: string; subtitle?: string };

type DocsRouteState = "settings" | "docs-root" | "docs-detail" | "other";
type DocsSettingsInteraction = {
  event: ScreenHeaderInteractionEvent;
  routeState: DocsRouteState;
};
type DocsRouteObservation = {
  state: DocsRouteState;
  at: number;
};

function classifyDocsRoute(pathname: string | null | undefined): DocsRouteState {
  if (!pathname) return "other";
  if (pathname === "/(tabs)/settings" || pathname.endsWith("/settings")) return "settings";
  if (pathname === "/(tabs)/docs" || pathname === "/docs" || pathname.endsWith("/docs")) return "docs-root";
  if (pathname.startsWith("/(tabs)/docs/") || pathname.startsWith("/docs/")) return "docs-detail";
  return "other";
}

type DocsRootCellProps = {
  cellKey: string;
  index: number;
  item: DocsNode;
  children: React.ReactNode;
  // VirtualizedList's generated public type resolves this callback to the
  // platform FocusEvent; the runtime is still forwarded unchanged below.
  onFocusCapture?: (event: FocusEvent) => void;
  onLayout?: (event: LayoutChangeEvent) => void;
  style: StyleProp<ViewStyle>;
};

type DocsRootCellMeasurement = {
  onLayout: (layout: DocsListCellLayout) => void;
  onCellRef: (key: string, view: View | null, releasedView: View | null) => void;
};

const DocsRootCellMeasurementContext = React.createContext<DocsRootCellMeasurement | null>(null);

type DocsRootNativeMeasureRef = {
  measure: (callback: (x: number, y: number, width: number, height: number) => void) => void;
  measureLayout: (
    relativeTo: DocsRootNativeMeasureRef,
    onSuccess: (x: number, y: number, width: number, height: number) => void,
    onFail?: () => void,
  ) => void;
};

type DocsRootScrollResponder = {
  getInnerViewRef?: () => DocsRootNativeMeasureRef | null;
  getNativeScrollRef?: () => DocsRootNativeMeasureRef | null;
};

/**
 * VirtualizedList's CellRendererComponent is the direct native child of the
 * ScrollView content view.  Forward its internal onLayout event so the JS
 * position controller receives content-relative y coordinates.
 */
const DocsRootCell = React.forwardRef<View, DocsRootCellProps>(function DocsRootCell(
  { cellKey: _cellKey, item, index, children, onFocusCapture, onLayout, style },
  ref,
) {
  const measurement = useContext(DocsRootCellMeasurementContext);
  const handleLayout = useCallback((event: LayoutChangeEvent) => {
    onLayout?.(event);
    measurement?.onLayout({
      key: item.id,
      index,
      y: event.nativeEvent.layout.y,
      height: event.nativeEvent.layout.height,
    });
  }, [index, item.id, measurement, onLayout]);
  const handleRef = useMemo(() => {
    // Keep the registered instance per ref callback. An old callback may be
    // released after a newer callback has already registered another host.
    let registeredView: View | null = null;
    return (view: View | null) => {
      const releasedView = view === null ? registeredView : null;
      registeredView = view;
      if (typeof ref === "function") ref(view);
      else if (ref) ref.current = view;
      measurement?.onCellRef(item.id, view, releasedView);
    };
  }, [item.id, measurement, ref]);
  // RN's View declaration omits the internal focus-capture prop even though
  // VirtualizedListCellRenderer forwards it.  Keep the runtime prop intact;
  // the cast only describes that upstream RN compatibility surface.
  return React.createElement(
    View,
    {
      ref: handleRef,
      style,
      collapsable: false,
      onFocusCapture,
      onLayout: handleLayout,
    } as unknown as React.ComponentPropsWithRef<typeof View>,
    children,
  );
});

type DocsReadMeta = Omit<DocsPagesReadSummary, "pages"> & {
  requestId: number;
  outcome: "success" | "error";
};

type DocsSyncMeta = {
  runId: string;
  canonicalRequestId: string | null;
  authGeneration: number;
  requestedAt: number;
  outcome: SyncDiagnosticReceipt["outcome"];
  skipReason: SyncDiagnosticReceipt["skipReason"];
  failureReason: SyncDiagnosticReceipt["failureReason"];
  docsSubstage: SyncDiagnosticReceipt["docsSubstage"];
  docsWatchdog: SyncDiagnosticReceipt["docsWatchdog"];
  pulls: SyncPullDiagnostic[];
};

type DocsRefreshAction = {
  actionId: number;
  kind: "automatic" | "manual";
  reason: "local_only" | "auth_wait" | "coalesced" | "sync_delegated";
  networkState: "online" | "offline";
  requestedAt: number;
  requestedRunId: string | null;
  canonicalRunId: string | null;
  syncOutcome: SyncDiagnosticReceipt["outcome"] | null;
  syncSkipReason: SyncDiagnosticReceipt["skipReason"];
  syncFailureReason: SyncDiagnosticReceipt["failureReason"];
};

type DocsSyncBinding = Pick<
  DocsRefreshAction,
  "requestedRunId" | "canonicalRunId" | "syncOutcome" | "syncSkipReason" | "syncFailureReason"
>;

type DocsRemoteValidation = "validated-scoped-empty" | "has-rows" | "unknown";
type DocsPullValidation = "success" | "failed" | "unknown";

function currentDocsPulls(sync: DocsSyncMeta | null): SyncPullDiagnostic[] {
  if (!sync) return [];
  const active = sync.pulls.filter((pull) => pull.promotion !== "discarded");
  const latestGenerationByScope = new Map<string, number>();
  for (const pull of active) {
    if (pull.promotionGeneration == null) continue;
    latestGenerationByScope.set(
      pull.scope,
      Math.max(latestGenerationByScope.get(pull.scope) ?? Number.NEGATIVE_INFINITY, pull.promotionGeneration),
    );
  }
  return active.filter((pull) => {
    const latestGeneration = latestGenerationByScope.get(pull.scope);
    return latestGeneration == null || pull.promotionGeneration === latestGeneration;
  });
}

function getDocsPullValidation(sync: DocsSyncMeta | null): DocsPullValidation {
  const pulls = currentDocsPulls(sync);
  if (!sync || pulls.length === 0) {
    return sync?.outcome === "failed" ? "failed" : "unknown";
  }
  // A successful Docs HTTP response can arrive while the canonical Core
  // flight is still in an earlier stage.  That is useful live evidence, but
  // it cannot validate the final product state until Core is terminal.
  if (sync.outcome === "running") return "unknown";
  if (pulls.some((pull) => pull.finishedAt == null || pull.outcome === "not_sent")) {
    return "unknown";
  }
  if (pulls.some((pull) =>
    pull.outcome !== "success"
    || pull.status == null
    || pull.status < 200
    || pull.status >= 300
    || pull.promotion === "failed",
  )) return "failed";
  if (pulls.every((pull) => pull.promotion === "applied" || pull.promotion === "promoted")) {
    return "success";
  }
  return "unknown";
}

function getRemoteDocsValidation(sync: DocsSyncMeta | null): DocsRemoteValidation {
  if (!sync || getDocsPullValidation(sync) !== "success") return "unknown";
  // The core receipt is bounded. At the pull history limit, an omitted scope
  // cannot be mistaken for a zero-count scope.
  if (sync.pulls.length >= 32) return "unknown";
  const pulls = currentDocsPulls(sync);
  const nodeTables = pulls.flatMap((pull) =>
    pull.tables.filter((table) => table.table === "knowledge_nodes"),
  );
  if (!nodeTables.length) return "unknown";
  if (nodeTables.some((table) =>
    !table.received
    || table.authoritativeCount == null
    || table.hasMore !== false,
  )) return "unknown";
  if (nodeTables.some((table) => table.authoritativeCount != null && table.authoritativeCount > 0)) {
    return "has-rows";
  }
  return "validated-scoped-empty";
}

function latestDocsSyncMeta(): DocsSyncMeta | null {
  const snapshot = getSyncDiagnosticSnapshot();
  const receipt = snapshot.latest;
  const latest = docsSyncMetaFromReceipt(receipt);
  return latest ? resolveCanonicalDocsSyncMeta(snapshot, latest) : null;
}

function docsSyncMetaFromReceipt(receipt: SyncDiagnosticReceipt | null): DocsSyncMeta | null {
  if (!receipt) return null;
  return {
    runId: receipt.requestId,
    canonicalRequestId: receipt.canonicalRequestId,
    authGeneration: receipt.authGeneration,
    requestedAt: receipt.requestedAt,
    outcome: receipt.outcome,
    skipReason: receipt.skipReason,
    failureReason: receipt.failureReason,
    docsSubstage: receipt.docsSubstage,
    docsWatchdog: receipt.docsWatchdog,
    pulls: receipt.pulls.filter((pull) => pull.kind === "docs"),
  };
}

function docsSyncMetaFromSnapshot(
  snapshot: SyncDiagnosticSnapshot,
  requestId: string,
): DocsSyncMeta | null {
  return docsSyncMetaFromReceipt(
    [...snapshot.recent, ...(snapshot.retained ?? [])]
      .find((receipt) => receipt.requestId === requestId) ?? null,
  );
}

function resolveCanonicalDocsSyncMeta(
  snapshot: SyncDiagnosticSnapshot,
  receipt: DocsSyncMeta,
): DocsSyncMeta | null {
  const seen = new Set<string>();
  let current = receipt;
  while (current.canonicalRequestId) {
    if (seen.has(current.runId)) return null;
    seen.add(current.runId);
    const canonical = docsSyncMetaFromSnapshot(snapshot, current.canonicalRequestId);
    if (!canonical) return null;
    current = canonical;
  }
  return current;
}

function docsSyncMetaFromRequestHandle(
  snapshot: SyncDiagnosticSnapshot,
  handle: SyncDiagnosticRequestHandle | null,
  refreshAction?: DocsRefreshAction | null,
): DocsSyncMeta | null {
  const receipt = handle?.getReceipt();
  if (!receipt || receipt.authGeneration !== snapshot.authGeneration) return null;
  if (refreshAction) {
    const ownedCanonicalRunId = receipt.canonicalRequestId ?? receipt.requestId;
    const belongsToAction = refreshAction.requestedRunId === handle?.requestId
      || refreshAction.canonicalRunId === handle?.requestId
      || refreshAction.canonicalRunId === ownedCanonicalRunId;
    if (!belongsToAction) return null;
  }
  const owned = docsSyncMetaFromReceipt(receipt);
  return owned ? resolveCanonicalDocsSyncMeta(snapshot, owned) : null;
}

function liveDocsSyncMeta(
  snapshot: SyncDiagnosticSnapshot,
  refreshAction: DocsRefreshAction | null,
  fallback: DocsSyncMeta | null,
  requestHandle: SyncDiagnosticRequestHandle | null,
): DocsSyncMeta | null {
  if (!refreshAction) {
    const latest = docsSyncMetaFromReceipt(snapshot.latest);
    return latest ?? (fallback?.authGeneration === snapshot.authGeneration ? fallback : null);
  }
  const canonical = refreshAction.canonicalRunId
    ? docsSyncMetaFromSnapshot(snapshot, refreshAction.canonicalRunId)
    : null;
  if (refreshAction.canonicalRunId) {
    // The first binding may point at the requested waiter itself. Resolve a
    // later alias update to the actual canonical Core receipt before exposing
    // it to diagnostics or the action row; a missing target stays missing.
    const resolved = canonical ? resolveCanonicalDocsSyncMeta(snapshot, canonical) : null;
    return resolved ?? docsSyncMetaFromRequestHandle(snapshot, requestHandle, refreshAction);
  }
  const owned = docsSyncMetaFromRequestHandle(snapshot, requestHandle, refreshAction);
  // A request-owned handle is authoritative. If its canonical target was
  // evicted, do not substitute an unrelated latest receipt or an old fallback.
  if (requestHandle) return owned;
  const requested = refreshAction.requestedRunId
    ? docsSyncMetaFromSnapshot(snapshot, refreshAction.requestedRunId)
    : null;
  // A waiter receipt is only an alias.  If its canonical receipt is absent,
  // keep the state explicitly missing instead of showing the waiter's
  // terminal `skipped` outcome as if it were the actual Core result.
  const matched = requested ? resolveCanonicalDocsSyncMeta(snapshot, requested) : null;
  if (matched) return matched;
  // Keep the receipt already bound to this action visible if a later bounded
  // snapshot trims it. A remounted screen has no fallback and will correctly
  // classify that condition as missing.
  return fallback
    && fallback.authGeneration === snapshot.authGeneration
    && !refreshAction.canonicalRunId
    && !fallback.canonicalRequestId
    && fallback.runId === refreshAction.requestedRunId
    ? fallback
    : null;
}

function useLiveSyncDiagnosticSnapshot(): SyncDiagnosticSnapshot {
  const [snapshot, setSnapshot] = useState<SyncDiagnosticSnapshot>(() => getSyncDiagnosticSnapshot());
  useEffect(() => {
    const unsubscribe = subscribeSyncDiagnostics(setSnapshot);
    // Close the render-to-subscribe gap: a receipt may finish between the
    // initial getSnapshot call and effect installation.
    setSnapshot(getSyncDiagnosticSnapshot());
    return unsubscribe;
  }, []);
  return snapshot;
}

function latestRequestedDocsSyncMeta(requestId?: string): DocsSyncMeta | null {
  const snapshot = getSyncDiagnosticSnapshot();
  const receipt = requestId
    ? [...snapshot.recent, ...(snapshot.retained ?? [])]
      .reverse().find((candidate) => candidate.requestId === requestId)
    : snapshot.recent[snapshot.recent.length - 1];
  return docsSyncMetaFromReceipt(receipt ?? null);
}

function safeDiagnosticMessage(error: unknown, fallback: string): string {
  const status = error && typeof error === "object" && "status" in error
    && typeof error.status === "number"
    ? error.status
    : null;
  // ApiHttpError.message may include a response body.  Keep only its safe
  // status/name here; local DB errors retain their short diagnostic message.
  const raw = status != null
    ? `${error instanceof Error ? error.name : "HTTP error"} (${status})`
    : error instanceof Error ? error.message : "";
  const sanitized = raw
    .replace(/Bearer\s+[^\s]+/gi, "Bearer …")
    .replace(/https?:\/\/[^\s]+/gi, "server")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 140);
  return sanitized || fallback;
}

const DOCS_LOCAL_READ_WATCHDOG_MS = 12_000;

class DocsLocalReadTimeoutError extends Error {
  constructor() {
    super(`Docsのローカル読込が${DOCS_LOCAL_READ_WATCHDOG_MS / 1000}秒以内に完了しませんでした`);
    this.name = "DocsLocalReadTimeoutError";
  }
}

class DocsLocalReadCancelledError extends Error {
  constructor() {
    super("Docsのローカル読込が取消されました");
    this.name = "AbortError";
  }
}

function isDocsLocalReadCancelled(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted || error instanceof DocsLocalReadCancelledError;
}

/**
 * A stuck SecureStore/SQLite read must release the first-paint state.  The
 * underlying operation is still allowed to settle, but its result is ignored
 * by the request generation and AbortSignal guards.
 */
async function withDocsLocalReadWatchdog<T>(
  operation: Promise<T>,
  signal: AbortSignal,
): Promise<T> {
  let timeoutId: ReturnType<typeof setTimeout> | null = null;
  let abort: (() => void) | null = null;
  const timeout = new Promise<never>((_, reject) => {
    timeoutId = setTimeout(() => reject(new DocsLocalReadTimeoutError()), DOCS_LOCAL_READ_WATCHDOG_MS);
  });
  const cancelled = new Promise<never>((_, reject) => {
    abort = () => reject(new DocsLocalReadCancelledError());
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  });
  try {
    return await Promise.race([operation, timeout, cancelled]);
  } finally {
    if (timeoutId !== null) clearTimeout(timeoutId);
    if (abort) signal.removeEventListener("abort", abort);
  }
}

function DocsStatusCard({
  title,
  detail,
  diagnosticSummary,
  local,
  sync,
  refreshAction,
  settingsInteractions,
  routeState,
  routeHistory,
  settingsRouteTransition,
  diagnosticsExpanded,
  onToggleDiagnostics,
  onInspectLayout,
  serverDiagnostic,
  serverDiagnosticEnabled,
  onRunServerDiagnostic,
  retry,
  reauthenticate,
  compact = false,
}: {
  title: string;
  detail?: string;
  diagnosticSummary?: string;
  local: DocsReadMeta | null;
  sync: DocsSyncMeta | null;
  refreshAction: DocsRefreshAction | null;
  settingsInteractions: DocsSettingsInteraction[];
  routeState: DocsRouteState;
  routeHistory: DocsRouteObservation[];
  settingsRouteTransition: "changed" | "unchanged";
  diagnosticsExpanded: boolean;
  onToggleDiagnostics: () => void;
  onInspectLayout?: () => void;
  serverDiagnostic: DocsSyncDiagnosticSnapshot | null;
  serverDiagnosticEnabled: boolean;
  onRunServerDiagnostic: () => void;
  retry?: () => void;
  reauthenticate?: () => void;
  compact?: boolean;
}) {
  return (
    <View style={compact ? styles.notice : styles.statusCard}>
      <Text style={styles.emptyText}>{title}</Text>
      {detail ? <Text style={styles.emptySubtext}>{detail}</Text> : null}
      {diagnosticSummary ? <Text style={styles.diagnosticText}>{diagnosticSummary}</Text> : null}
      <View style={styles.statusActions}>
        {retry ? (
          <Pressable
            accessibilityRole="button"
            testID="docs-retry"
            onPress={retry}
            style={styles.statusAction}
          >
            <Text style={styles.statusActionText}>再試行</Text>
          </Pressable>
        ) : null}
        {reauthenticate ? (
          <Pressable
            accessibilityRole="button"
            testID="docs-reauthenticate"
            onPress={reauthenticate}
            style={styles.statusAction}
          >
            <Text style={styles.statusActionText}>再ログイン</Text>
          </Pressable>
        ) : null}
      </View>
      <DocsDiagnostics
        local={local}
        sync={sync}
        refreshAction={refreshAction}
        settingsInteractions={settingsInteractions}
        routeState={routeState}
        routeHistory={routeHistory}
        settingsRouteTransition={settingsRouteTransition}
        expanded={diagnosticsExpanded}
        onToggle={onToggleDiagnostics}
        onInspectLayout={onInspectLayout}
        serverDiagnostic={serverDiagnostic}
        serverDiagnosticEnabled={serverDiagnosticEnabled}
        onRunServerDiagnostic={onRunServerDiagnostic}
      />
    </View>
  );
}

function DocsDiagnostics({
  local,
  sync,
  refreshAction,
  settingsInteractions,
  routeState,
  routeHistory,
  settingsRouteTransition,
  expanded,
  onToggle,
  onInspectLayout,
  serverDiagnostic,
  serverDiagnosticEnabled,
  onRunServerDiagnostic,
}: {
  local: DocsReadMeta | null;
  sync: DocsSyncMeta | null;
  refreshAction: DocsRefreshAction | null;
  settingsInteractions: DocsSettingsInteraction[];
  routeState: DocsRouteState;
  routeHistory: DocsRouteObservation[];
  settingsRouteTransition: "changed" | "unchanged";
  expanded: boolean;
  onToggle: () => void;
  onInspectLayout?: () => void;
  serverDiagnostic: DocsSyncDiagnosticSnapshot | null;
  serverDiagnosticEnabled: boolean;
  onRunServerDiagnostic: () => void;
}) {
  // `sync` is already resolved against the action's requested/canonical IDs
  // by liveDocsSyncMeta.  Do not discard a canonical receipt merely because
  // the action binding was captured before token lookup/coalescing completed.
  const actionSync = sync;
  const remoteValidation = getRemoteDocsValidation(actionSync);
  const receiptState = refreshAction
    ? actionSync
      ? actionSync.outcome === "running" ? "running" : "terminal"
      : !refreshAction.requestedRunId && !refreshAction.canonicalRunId
        ? "no_core_request"
        : "missing"
    : null;
  return (
    <View style={styles.diagnostics}>
      <Pressable
        accessibilityRole="button"
        testID="docs-diagnostics-toggle"
        onPress={onToggle}
        style={styles.diagnosticsToggle}
      >
        <Text style={styles.diagnosticsToggleText}>
          表示・同期の確認 {expanded ? "▲" : "▼"}
        </Text>
      </Pressable>
      {expanded ? (
        <View testID="docs-diagnostics-details" style={styles.diagnosticsDetails}>
          <Pressable
            accessibilityRole="button"
            accessibilityState={{
              disabled: !serverDiagnosticEnabled || serverDiagnostic?.outcome === "running",
            }}
            testID="docs-server-sync-diagnostic"
            disabled={!serverDiagnosticEnabled || serverDiagnostic?.outcome === "running"}
            onPress={onRunServerDiagnostic}
            style={[styles.diagnosticsToggle, styles.diagnosticsAction]}
          >
            <Text style={styles.diagnosticsToggleText}>
              {serverDiagnostic?.outcome === "running" ? "サーバーDocs診断を確認中…" : "サーバーDocs診断を実行 (GET)"}
            </Text>
          </Pressable>
          {!serverDiagnosticEnabled ? (
            <Text style={styles.diagnosticsLine}>ログイン状態を確認してから実行できます</Text>
          ) : null}
          <Text style={styles.diagnosticsLine}>
            この診断は端末内の反映・昇格とoutboxを実行・参照しません
          </Text>
          {serverDiagnostic ? (
            <>
              <Text style={styles.diagnosticsLine}>
                サーバーDocs診断 request {serverDiagnostic.requestId}・試行 {serverDiagnostic.attempts}・HTTP {serverDiagnostic.httpStatus ?? "未取得"}・stage {serverDiagnostic.response?.serverPhase ?? serverDiagnostic.stage}・elapsed {serverDiagnostic.elapsedMs}ms
              </Text>
              <Text style={styles.diagnosticsLine}>
                結果 {serverDiagnosticOutcomeLabel(serverDiagnostic.outcome)}
              </Text>
              {serverDiagnostic.serverError ? (
                <Text style={styles.diagnosticsLine}>
                  server error {serverDiagnostic.serverError.errorCode}・exception {serverDiagnostic.serverError.exceptionType}・phase {serverDiagnostic.serverError.serverPhase}
                </Text>
              ) : null}
              {serverDiagnostic.response ? (
                <Text style={styles.diagnosticsLine}>
                  personal library {serverDiagnostic.response.personalLibraryExists ? "あり" : "なし"}・needs normalization {serverDiagnostic.response.needsNormalization ? "あり" : "なし"}・accessible projects {serverDiagnostic.response.accessibleProjectCount}・scopes {serverDiagnostic.response.accessibleScopeCount}・read only
                </Text>
              ) : null}
            </>
          ) : null}
          {onInspectLayout ? (
            <Pressable accessibilityRole="button" accessibilityLabel="一覧のレイアウト計測を開く"
              testID="docs-layout-inspect" onPress={onInspectLayout}
              style={[styles.diagnosticsToggle, styles.diagnosticsAction]}>
              <Text style={styles.diagnosticsToggleText}>一覧の計測</Text>
            </Pressable>
          ) : null}
          {receiptState ? (
            <Text style={styles.diagnosticsLine}>receipt state {receiptState}</Text>
          ) : null}
          {settingsInteractions.length ? (
            <>
              <Text style={styles.diagnosticsLine}>settings route {routeState}</Text>
              <Text style={styles.diagnosticsLine}>settings route transition {settingsRouteTransition}</Text>
              <Text style={styles.diagnosticsLine}>
                settings route history {routeHistory.map(({ state, at }) => `${state}@${at}`).join(" | ")}
              </Text>
              <Text style={styles.diagnosticsLine}>
                settings events {settingsInteractions.map(({ event, routeState: eventRoute }) =>
                  `${event.phase}:${event.target}:${eventRoute}${"errorName" in event ? `:${event.errorName}` : ""}`
                ).join(" | ")}
              </Text>
            </>
          ) : null}
          {local ? (
            <>
              <Text style={styles.diagnosticsLine}>
                ローカル読取: {local.outcome === "success" ? "成功" : "失敗"}・source local-cache・可視root {local.visibleCount}
              </Text>
              <Text style={styles.diagnosticsLine}>
                active nodes {local.activeNodeCount ?? "未取得"}・active root {local.activeRootCount ?? "未取得"}・dirty nodes {local.localDirtyNodeCount ?? "未取得"}・dirty root {local.localDirtyRootCount ?? "未取得"}
              </Text>
              <Text style={styles.diagnosticsLine}>
                actor scope {local.actorScopeStatus}・workspace投影 {local.workspaceProjectionCount ?? "未取得"}・outbox pending {local.outboxPendingCount ?? "未取得"}・failed {local.outboxFailedCount ?? "未取得"}
              </Text>
              <Text style={styles.diagnosticsLine}>
                ローカル読込 request {local.requestId}・read {local.readStatus}・generation {local.generationStatus}・集計 {local.aggregateStatus === "success" ? "成功" : "一部失敗"}
              </Text>
              {local.readDegradedReasons.length ? (
                <Text style={styles.diagnosticsLine}>
                  未検証理由: {local.readDegradedReasons.join(", ")}
                </Text>
              ) : null}
            </>
          ) : null}
          {actionSync ? (
            <>
              <Text style={styles.diagnosticsLine}>
                同期run {actionSync.runId}・outcome {actionSync.outcome}{actionSync.skipReason
                  ? actionSync.outcome === "skipped" ? `・skip ${actionSync.skipReason}` : `・reason ${actionSync.skipReason}`
                  : ""}
              </Text>
              <Text style={styles.diagnosticsLine}>
                server Docs count: {remoteValidation === "validated-scoped-empty" ? "確認したscopeは0件・サーバー全体は未確認" : remoteValidation === "has-rows" ? "件数あり" : "未確認"}
              </Text>
              {actionSync.docsSubstage ? (
                <Text style={styles.diagnosticsLine}>
                  Docs substage {actionSync.docsSubstage.stage}・elapsed {actionSync.docsSubstage.elapsedMs}ms・runState {actionSync.docsSubstage.runState ?? "未取得"}・pendingTables {actionSync.docsSubstage.pendingTables ?? "未取得"}
                </Text>
              ) : null}
              {actionSync.docsWatchdog ? (
                <Text style={styles.diagnosticsLine}>
                  Docs watchdog固定時点 {actionSync.docsWatchdog.stage}・elapsed {actionSync.docsWatchdog.elapsedMs}ms・runState {actionSync.docsWatchdog.runState ?? "未取得"}・pendingTables {actionSync.docsWatchdog.pendingTables ?? "未取得"}
                </Text>
              ) : null}
              {actionSync && actionSync.pulls.length === 0 ? (
                <Text style={styles.diagnosticsLine}>Docs pull: 未実行</Text>
              ) : actionSync ? (
                actionSync.pulls.map((pull) => (
                  <View key={`${actionSync.runId}:${pull.requestId}`} style={styles.diagnosticsPull}>
                    <Text style={styles.diagnosticsLine}>
                      pull state {pull.finishedAt == null ? "running" : "terminal"}
                    </Text>
                    <Text style={styles.diagnosticsLine}>
                      Docs pull {pull.requestId}・scope {pull.scope}・HTTP {pull.status ?? "未取得"}・outcome {pull.outcome}・試行 {pull.attemptCount}
                    </Text>
                    <Text style={styles.diagnosticsLine}>
                      promotion {pull.promotion}・hasMore {pull.hasMore == null ? "未取得" : pull.hasMore ? "あり" : "なし"}
                    </Text>
                    {pull.tables.map((table) => {
                      const authoritative = table.received && table.authoritativeCount != null
                        ? String(table.authoritativeCount)
                        : "未取得";
                      const countLabel = table.table === "knowledge_nodes"
                        ? "authoritative nodes (全ノード)"
                        : "authoritative rows";
                      return (
                        <Text key={`${pull.requestId}:${table.table}`} style={styles.diagnosticsLine}>
                          table {table.table}・受信 {table.received ? "あり" : "なし"}・changes {table.changes}・{countLabel} {authoritative}・hasMore {table.hasMore == null ? "未取得" : table.hasMore ? "あり" : "なし"}
                        </Text>
                      );
                    })}
                  </View>
                ))
              ) : null}
            </>
          ) : null}
          {refreshAction ? (
            <Text style={styles.diagnosticsLine}>
              この操作: {refreshAction.kind === "manual" ? "手動再読込" : "自動再読込"}・reason {refreshAction.reason}・network {refreshAction.networkState}・at {new Date(refreshAction.requestedAt).toISOString()}・requested {refreshAction.requestedRunId ?? "未取得"}・canonical {refreshAction.canonicalRunId ?? "未取得"}{refreshAction.reason === "coalesced" ? "・画面内の既存再読込へ合流（新HTTP不要）" : refreshAction.requestedRunId && refreshAction.canonicalRunId && refreshAction.requestedRunId !== refreshAction.canonicalRunId ? "・既存Core同期へ合流" : ""}・outcome {refreshAction.syncOutcome ?? "未取得"}{refreshAction.syncSkipReason ? refreshAction.syncOutcome === "skipped" ? `・skip ${refreshAction.syncSkipReason}` : `・reason ${refreshAction.syncSkipReason}` : ""}{refreshAction.syncFailureReason ? `・failure ${refreshAction.syncFailureReason}` : ""}
            </Text>
          ) : null}
        </View>
      ) : null}
    </View>
  );
}

function serverDiagnosticOutcomeLabel(outcome: DocsSyncDiagnosticSnapshot["outcome"]): string {
  switch (outcome) {
    case "running": return "実行中";
    case "success": return "成功";
    case "http_error": return "HTTP応答エラー";
    case "timeout": return "タイムアウト";
    case "connection_error": return "接続エラー";
    case "invalid_response": return "応答形式エラー";
    case "cancelled": return "キャンセル";
    case "request_error": return "リクエストエラー";
  }
}

function rootScrollGeometry(event: NativeSyntheticEvent<NativeScrollEvent>) {
  const native = event.nativeEvent as NativeScrollEvent & { timestamp?: number };
  return { offsetY: native.contentOffset.y, contentHeight: native.contentSize.height,
    timestamp: native.timestamp ?? event.timeStamp };
}

export default function DocsListScreen() {
  const router = useRouter();
  const isAndroid = Platform.OS === "android";
  const auth = useAuth();
  const authChecking = auth.isLoading;
  const authNeedsReauth = auth.reauthRequired
    || auth.authState === "reauth_required"
    || auth.authState === "reauth_required_unknown";
  const authIdentityUnknown = auth.reauthIdentityUnknown === true;
  const authIsAnonymous = auth.isAnonymous === true;
  const serverDiagnosticEnabled = !authChecking
    && !authNeedsReauth
    && !authIsAnonymous
    && auth.isAuthenticated === true;
  const authIdentityKey = auth.user?.user_id
    ? `user:${auth.user.user_id}`
    : `${auth.authState}:${authIdentityUnknown ? "unknown" : "none"}`;
  const creatingRef = useRef(false);
  const [creating, setCreating] = useState(false);
  // AoiTalk may be reachable on the current LAN even when Android reports no
  // Internet access.  This gate is for the server path, not the Internet.
  const online = useNetworkStore((state) => state.connected ?? state.online);
  const [pages, setPages] = useState<DocsNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [searchVisible, setSearchVisible] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [busyToday, setBusyToday] = useState(false);
  const [clipIngestVisible, setClipIngestVisible] = useState(false);
  const [hasRead, setHasRead] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [syncWarning, setSyncWarning] = useState<string | null>(null);
  const [lastRead, setLastRead] = useState<DocsReadMeta | null>(null);
  const [lastSync, setLastSync] = useState<DocsSyncMeta | null>(null);
  const [lastRefreshAction, setLastRefreshAction] = useState<DocsRefreshAction | null>(null);
  const [requestReceiptHandle, setRequestReceiptHandle] = useState<SyncDiagnosticRequestHandle | null>(null);
  const [diagnosticsExpanded, setDiagnosticsExpanded] = useState(false);
  const [serverDiagnostic, setServerDiagnostic] = useState<DocsSyncDiagnosticSnapshot | null>(null);
  const diagnosticSnapshot = useLiveSyncDiagnosticSnapshot();
  const pathname = usePathname();
  const routeState = useMemo(() => classifyDocsRoute(pathname), [pathname]);
  const [settingsInteractions, setSettingsInteractions] = useState<DocsSettingsInteraction[]>([]);
  const [routeHistory, setRouteHistory] = useState<DocsRouteObservation[]>([]);
  const previousRouteStateRef = useRef<DocsRouteState>(routeState);
  const [settingsRouteTransition, setSettingsRouteTransition] = useState<"changed" | "unchanged">("unchanged");
  useEffect(() => {
    setSettingsRouteTransition(previousRouteStateRef.current === routeState ? "unchanged" : "changed");
    previousRouteStateRef.current = routeState;
    if (settingsInteractions.length > 0) {
      setRouteHistory((current) => current.at(-1)?.state === routeState
        ? current
        : [...current, { state: routeState, at: Date.now() }].slice(-8));
    }
  }, [routeState, settingsInteractions.length]);
  const recordSettingsInteraction = useCallback((event: ScreenHeaderInteractionEvent) => {
    setSettingsInteractions((current) => [
      ...current,
      { event, routeState },
    ].slice(-8));
    setRouteHistory((current) => current.length
      ? current
      : [{ state: routeState, at: event.at }]);
  }, [routeState]);
  const displaySync = useMemo(
    () => liveDocsSyncMeta(diagnosticSnapshot, lastRefreshAction, lastSync, requestReceiptHandle),
    [diagnosticSnapshot, lastRefreshAction, lastSync, requestReceiptHandle],
  );
  const [preservePagePosition, setPreservePagePosition] = useState(false);
  const rootHeaderHeightRef = useRef<number | null>(null);
  const rootHeaderRef = useRef<View | null>(null);
  const rootListRef = useRef<FlatList<DocsNode>>(null);
  const rootCellRefsRef = useRef(new Map<string, View>());
  const restoreAnchorKeyRef = useRef<string | null>(null);
  const restoreMeasurementSequenceRef = useRef(0);
  const restoreMeasurementInFlightRef = useRef<{
    token: DocsListMeasurementToken; sequence: number; anchorKey: string; rowRef: View | null;
  } | null>(null);
  const restoreRetryScheduledRef = useRef(false);
  const restoreRetryGenerationRef = useRef(0);
  const restoreMountedRef = useRef(false);
  const tryRestorePagePositionRef = useRef<((token: DocsListMeasurementToken) => boolean) | null>(null);
  useLayoutEffect(() => {
    // Ref callbacks may be replayed by StrictMode. Keep the mounted marker
    // true for every setup and cancel only the cleanup belonging to unmount.
    restoreMountedRef.current = true;
    return () => {
      restoreMountedRef.current = false;
      restoreRetryGenerationRef.current += 1;
      restoreRetryScheduledRef.current = false;
      restoreMeasurementSequenceRef.current += 1;
      restoreMeasurementInFlightRef.current = null;
    };
  }, []);
  const positionGuardRef = useRef<ReturnType<typeof createDocsListPositionGuard> | null>(null);
  if (!positionGuardRef.current) positionGuardRef.current = createDocsListPositionGuard({
    measureHeader: (receive) => rootHeaderRef.current?.measure((_x, _y, _width, height) => receive(height)),
    release: () => {
      if (!isAndroid) setPreservePagePosition(false);
    },
  });
  const positionGuard = positionGuardRef.current;
  const layoutTraceRef = useRef<ReturnType<typeof createDocsListLayoutTrace> | null>(null);
  if (!layoutTraceRef.current) layoutTraceRef.current = createDocsListLayoutTrace();
  const layoutTrace = layoutTraceRef.current;
  const positionRestoreRef = useRef<ReturnType<typeof createDocsListPositionController> | null>(null);
  if (!positionRestoreRef.current) {
    positionRestoreRef.current = createDocsListPositionController({
      onLifecycle: (entry: DocsListRestoreLifecycle) => {
        layoutTrace.recordRestore(entry.event, entry);
        if (entry.event === "drop") restoreAnchorKeyRef.current = null;
      },
    });
  }
  const positionRestore = positionRestoreRef.current;
  const [measurementToken, setMeasurementToken] = useState<DocsListMeasurementToken>(0);
  const measurementTokenRef = useRef<DocsListMeasurementToken>(0);
  const publishMeasurementToken = useCallback((token: DocsListMeasurementToken) => {
    if (measurementTokenRef.current !== token) {
      // Invalidate native callbacks from the retired list before allowing a
      // new measurement sequence for the current token.
      restoreMeasurementSequenceRef.current += 1;
      restoreMeasurementInFlightRef.current = null;
    }
    measurementTokenRef.current = token;
    setMeasurementToken((previous) => previous === token ? previous : token);
  }, []);
  const cancelRestoreRetry = useCallback(() => {
    restoreRetryGenerationRef.current += 1;
    restoreRetryScheduledRef.current = false;
  }, []);
  const scheduleRestoreRetry = useCallback(() => {
    if (!restoreMountedRef.current || restoreRetryScheduledRef.current) return;
    restoreRetryScheduledRef.current = true;
    const generation = restoreRetryGenerationRef.current;
    queueMicrotask(() => {
      if (generation !== restoreRetryGenerationRef.current) return;
      restoreRetryScheduledRef.current = false;
      if (!restoreMountedRef.current || !positionRestore.active() || !positionRestore.pending()) return;
      // Read the token only when the job flushes. A token change during the
      // commit must retry the current list rather than replay a retired one.
      tryRestorePagePositionRef.current?.(measurementTokenRef.current);
    });
  }, [positionRestore]);
  const [layoutSnapshot, setLayoutSnapshot] = useState<DocsListLayoutSnapshot | null>(null);
  const focusedRef = useRef(false);
  const pagesRef = useRef<DocsNode[]>([]);
  const authIdentityRef = useRef(authIdentityKey);
  const authSessionGenerationRef = useRef(0);
  const serverDiagnosticGenerationRef = useRef(0);
  const serverDiagnosticControllerRef = useRef<AbortController | null>(null);
  const serverDiagnosticEnabledRef = useRef(serverDiagnosticEnabled);
  serverDiagnosticEnabledRef.current = serverDiagnosticEnabled;
  const loadRequestRef = useRef(0);
  const loadControllersRef = useRef(new Set<AbortController>());
  const refreshActionSequenceRef = useRef(0);
  const revalidateFlightMetaRef = useRef<DocsSyncBinding | null>(null);
  const requestReceiptHandleRef = useRef<SyncDiagnosticRequestHandle | null>(null);
  const coalescedActionIdsRef = useRef(new Set<number>());
  const revalidateFlightRef = useRef<Promise<void> | null>(null);

  const onRootCellRef = useCallback((
    key: string, view: View | null, releasedView: View | null, token: DocsListMeasurementToken,
  ) => {
    if (!isAndroid) return;
    if (view) {
      if (token !== measurementTokenRef.current) return;
      const previousView = rootCellRefsRef.current.get(key);
      const flight = restoreMeasurementInFlightRef.current;
      if (previousView && previousView !== view
        && flight?.anchorKey === key && flight.rowRef === previousView) {
        restoreMeasurementSequenceRef.current += 1;
        restoreMeasurementInFlightRef.current = null;
      }
      rootCellRefsRef.current.set(key, view);
      if (restoreAnchorKeyRef.current === key) {
        cancelRestoreRetry();
        tryRestorePagePositionRef.current?.(token);
      }
      return;
    }
    // Ref retirement belongs to a host instance, not to its old measurement
    // token. Never let an old cleanup erase a newly registered host.
    if (!releasedView || rootCellRefsRef.current.get(key) !== releasedView) return;
    rootCellRefsRef.current.delete(key);
    const flight = restoreMeasurementInFlightRef.current;
    if (flight?.anchorKey === key && flight.rowRef === releasedView) {
      restoreMeasurementSequenceRef.current += 1;
      restoreMeasurementInFlightRef.current = null;
    }
    if (restoreAnchorKeyRef.current === key) {
      // React/Fabric may retire the old callback before invoking the new
      // callback in the same commit. Defer the retry until all ref callbacks
      // in this commit have had a chance to register the replacement host.
      scheduleRestoreRetry();
    }
  }, [cancelRestoreRetry, isAndroid, scheduleRestoreRetry]);

  const clearRootCellRefs = useCallback((keys?: readonly string[]) => {
    if (!keys) {
      rootCellRefsRef.current.clear();
      return;
    }
    const currentKeys = new Set(keys);
    for (const key of rootCellRefsRef.current.keys()) {
      if (!currentKeys.has(key)) rootCellRefsRef.current.delete(key);
    }
  }, []);

  const tryRestorePagePosition = useCallback((token = measurementTokenRef.current) => {
    if (!isAndroid || !restoreMountedRef.current || token !== measurementTokenRef.current
      || !positionRestore.active() || !positionRestore.pending()) return false;
    // A detach may precede the data effect in the same commit. Reject the
    // old anchor order before starting a seed measurement from that callback.
    const dataToken = positionRestore.observeDataKeys(pagesRef.current.map((page) => page.id), token);
    if (dataToken !== token) {
      publishMeasurementToken(dataToken);
      return false;
    }
    const anchorKey = restoreAnchorKeyRef.current;
    const list = rootListRef.current;
    const headerRef = rootHeaderRef.current;
    if (!anchorKey || !list || !headerRef) return false;
    const index = pagesRef.current.findIndex((page) => page.id === anchorKey);
    if (index < 0) return false;
    const responder = list.getScrollResponder?.() as unknown as DocsRootScrollResponder | null;
    const innerRef = responder?.getInnerViewRef?.() ?? null;
    const scrollRef = responder?.getNativeScrollRef?.() ?? null;
    if (!innerRef || !scrollRef) return false;
    const rowRef = rootCellRefsRef.current.get(anchorKey) ?? null;
    const hasRowRef = rowRef !== null;
    if (restoreMeasurementInFlightRef.current?.token === token) return false;
    const sequence = ++restoreMeasurementSequenceRef.current;
    restoreMeasurementInFlightRef.current = { token, sequence, anchorKey, rowRef };
    // A mounted anchor takes the exact four-way native snapshot.  When JS
    // virtualization has evicted it, measure the current header/viewport/
    // content first and issue one bounded seed so the stable key can re-enter
    // the render window; the exact row snapshot still completes the restore.
    let remaining = hasRowRef ? 4 : 3;
    let failed = false;
    let row: DocsListCellLayout | null = null;
    let headerHeight: number | null = null;
    let viewportHeight: number | null = null;
    let contentHeight: number | null = null;
    const finish = () => {
      remaining -= 1;
      if (remaining > 0) return;
      if (restoreMeasurementInFlightRef.current?.sequence === sequence) {
        restoreMeasurementInFlightRef.current = null;
      }
      if (failed || sequence !== restoreMeasurementSequenceRef.current) return;
      if (token !== measurementTokenRef.current || !positionRestore.pending()) return;
      const currentDataToken = positionRestore.observeDataKeys(pagesRef.current.map((page) => page.id), token);
      if (currentDataToken !== token) {
        publishMeasurementToken(currentDataToken);
        return;
      }
      if (headerHeight === null || viewportHeight === null || contentHeight === null) return;
      const scrollToOffset = (offset: number) => {
        try {
          rootListRef.current?.scrollToOffset({ offset, animated: false });
          return rootListRef.current !== null;
        } catch {
          return false;
        }
      };
      if (!row) {
        // If the row reattached during this measurement, prefer its actual
        // geometry even when a seed would reject the old root extent.
        if (rootCellRefsRef.current.has(anchorKey)) {
          tryRestorePagePositionRef.current?.(token);
          return;
        }
        const geometry: DocsListSeedRestoreGeometry = {
          headerHeight,
          viewportHeight,
          contentHeight,
        };
        const seed = positionRestore.trySeedRestore(token, geometry, scrollToOffset);
        if (seed === "invalid") {
          restoreMeasurementSequenceRef.current += 1;
          positionRestore.dropUnresolved(token);
        }
        return;
      }
      const snapshot: DocsListMeasuredSnapshot = { row, headerHeight, viewportHeight, contentHeight };
      if (!positionRestore.observeSnapshot(snapshot, token)) return;
      positionRestore.tryRestore(token, scrollToOffset);
    };
    try {
      if (rowRef) {
        (rowRef as unknown as DocsRootNativeMeasureRef).measureLayout(
          innerRef,
          (_x, y, _width, height) => {
            row = { key: anchorKey, index, y, height };
            finish();
          },
          () => {
            failed = true;
            finish();
          },
        );
      }
      headerRef.measure((_x, _y, _width, height) => {
        headerHeight = height;
        finish();
      });
      scrollRef.measure((_x, _y, _width, height) => {
        viewportHeight = height;
        finish();
      });
      innerRef.measure((_x, _y, _width, height) => {
        contentHeight = height;
        finish();
      });
    } catch {
      failed = true;
      restoreMeasurementSequenceRef.current += 1;
      restoreMeasurementInFlightRef.current = null;
    }
    return true;
  }, [isAndroid, positionRestore, publishMeasurementToken]);
  tryRestorePagePositionRef.current = tryRestorePagePosition;

  const onRootCellLayout = useCallback((layout: DocsListCellLayout) => {
    if (!isAndroid) return;
    positionRestore.observeRow(layout, measurementToken);
    tryRestorePagePosition(measurementToken);
  }, [isAndroid, measurementToken, positionRestore, tryRestorePagePosition]);

  const onRootScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    if (isAndroid) {
      const native = event.nativeEvent;
      const viewportLayout: DocsListViewportLayout = {
        offsetY: native.contentOffset.y,
        viewportHeight: native.layoutMeasurement.height,
        contentHeight: native.contentSize.height,
      };
      positionRestore.observeScroll(viewportLayout, measurementToken);
      tryRestorePagePosition(measurementToken);
    }
    layoutTrace.record("scroll", {
      offsetY: event.nativeEvent.contentOffset.y,
      contentHeight: event.nativeEvent.contentSize.height,
      viewportHeight: event.nativeEvent.layoutMeasurement.height,
    });
    const headerHeight = rootHeaderHeightRef.current;
    // Native layout/MVP corrections also emit scroll events. They may enable an
    // anchor, but only an explicit current gesture may release it.
    if (!isAndroid && positionGuard.active() && pagesRef.current.length > 0 && headerHeight !== null
      && event.nativeEvent.contentOffset.y >= headerHeight) setPreservePagePosition(true);
  }, [isAndroid, layoutTrace, measurementToken, positionGuard, positionRestore, tryRestorePagePosition]);

  const onRootViewableItemsChanged = useRef(({ viewableItems }: { viewableItems: ViewToken<DocsNode>[] }) => {
    const indices = viewableItems.filter((item) => item.isViewable && item.index !== null)
      .map((item) => item.index!);
    layoutTrace.record("visible-items", {
      firstVisibleIndex: indices.length ? Math.min(...indices) : -1,
      lastVisibleIndex: indices.length ? Math.max(...indices) : -1,
      visibleCount: indices.length,
    });
  }).current;

  useEffect(() => {
    if (searchVisible || loading || pages.length === 0) {
      if (!isAndroid) {
        positionGuard.reset();
        setPreservePagePosition(false);
      }
      rootHeaderHeightRef.current = null;
      if (isAndroid && (searchVisible || pages.length === 0)) {
        clearRootCellRefs();
        restoreAnchorKeyRef.current = null;
      }
      if (isAndroid && searchVisible) {
        publishMeasurementToken(positionRestore.invalidate(11));
      }
    }
  }, [clearRootCellRefs, isAndroid, loading, pages.length, positionGuard, positionRestore, publishMeasurementToken, searchVisible]);

  useEffect(() => {
    if (!isAndroid) return;
    const keys = pages.map((page) => page.id);
    clearRootCellRefs(keys);
    if (restoreAnchorKeyRef.current && !keys.includes(restoreAnchorKeyRef.current)) {
      restoreAnchorKeyRef.current = null;
    }
    const token = positionRestore.observeDataKeys(keys, measurementToken);
    publishMeasurementToken(token);
    tryRestorePagePosition(token);
  }, [clearRootCellRefs, isAndroid, measurementToken, pages, positionRestore, publishMeasurementToken, tryRestorePagePosition]);

  const readLocalPages = useCallback(async (
    requestId: number,
    controller: AbortController,
  ): Promise<"applied" | "failed" | "cancelled"> => {
    const signal = controller.signal;
    let timedOut = false;
    try {
      const readSummary = await withDocsLocalReadWatchdog(
        Promise.resolve().then(() => docsRepo.listPagesWithDiagnostics({ signal })),
        signal,
      );
      const nextPages = readSummary.readStatus === "degraded"
        && readSummary.pages.length === 0
        && pagesRef.current.length > 0
        ? pagesRef.current
        : readSummary.pages;
      if (signal.aborted || requestId !== loadRequestRef.current) return "cancelled";
      pagesRef.current = nextPages;
      setPages(nextPages);
      setHasRead(true);
      setReadError(null);
      setLastRead({
        requestId,
        source: readSummary.source,
        visibleCount: nextPages.length,
        activeNodeCount: readSummary.activeNodeCount,
        activeRootCount: readSummary.activeRootCount,
        localDirtyNodeCount: readSummary.localDirtyNodeCount,
        localDirtyRootCount: readSummary.localDirtyRootCount,
        workspaceProjectionCount: readSummary.workspaceProjectionCount,
        outboxPendingCount: readSummary.outboxPendingCount,
        outboxFailedCount: readSummary.outboxFailedCount,
        actorScopeStatus: readSummary.actorScopeStatus,
        aggregateStatus: readSummary.aggregateStatus,
        readStatus: readSummary.readStatus,
        readDegradedReasons: readSummary.readDegradedReasons,
        generationStatus: readSummary.generationStatus,
        outcome: "success",
      });
      setLoading(false);
      return "applied";
    } catch (error) {
      timedOut = error instanceof DocsLocalReadTimeoutError;
      if (timedOut) controller.abort();
      if ((!timedOut && isDocsLocalReadCancelled(error, signal)) || requestId !== loadRequestRef.current) return "cancelled";
      // Preserve the last successful array: a failed read must never look
      // like a validated empty result or discard unsent local edits.
      setReadError(safeDiagnosticMessage(error, "ローカルキャッシュを読み込めませんでした"));
      setLastRead({
        requestId,
        source: "local-cache",
        visibleCount: pagesRef.current.length,
        activeNodeCount: null,
        activeRootCount: null,
        localDirtyNodeCount: null,
        localDirtyRootCount: null,
        workspaceProjectionCount: null,
        outboxPendingCount: null,
        outboxFailedCount: null,
        actorScopeStatus: "unknown",
        aggregateStatus: "error",
        readStatus: "degraded",
        readDegradedReasons: ["read_failed"],
        generationStatus: "unknown",
        outcome: "error",
      });
      return "failed";
    } finally {
      if (requestId === loadRequestRef.current && (!signal.aborted || timedOut)) setLoading(false);
    }
  }, []);

  const loadPages = useCallback(async () => {
    const requestId = ++loadRequestRef.current;
    for (const previous of loadControllersRef.current) previous.abort();
    const controller = new AbortController();
    loadControllersRef.current.add(controller);
    try {
      const initialRead = await readLocalPages(requestId, controller);
      if (controller.signal.aborted || requestId !== loadRequestRef.current || initialRead !== "applied") return;
      // Root display is read-only. Durable Task/Docs outbox rows stay
      // untouched; repair requires an explicit, separately guarded action.
    } finally {
      loadControllersRef.current.delete(controller);
      if (requestId === loadRequestRef.current && !controller.signal.aborted) setLoading(false);
    }
  }, [readLocalPages]);

  useEffect(() => {
    if (authIdentityRef.current === authIdentityKey) return;
    authIdentityRef.current = authIdentityKey;
    authSessionGenerationRef.current += 1;
    // A different actor must never see the previous actor's in-memory array.
    // The repository cache remains on disk for its owning scope.
    pagesRef.current = [];
    loadRequestRef.current += 1;
    for (const controller of loadControllersRef.current) controller.abort();
    loadControllersRef.current.clear();
    setPages([]);
    setHasRead(false);
    setReadError(null);
    setLastRead(null);
    setLastSync(null);
    setLastRefreshAction(null);
    serverDiagnosticGenerationRef.current += 1;
    serverDiagnosticControllerRef.current?.abort();
    serverDiagnosticControllerRef.current = null;
    setServerDiagnostic(null);
    setSettingsInteractions([]);
    setRouteHistory([]);
    setSettingsRouteTransition("unchanged");
    previousRouteStateRef.current = routeState;
    if (!isAndroid) setPreservePagePosition(false);
    rootHeaderHeightRef.current = null;
    if (!isAndroid) positionGuard.reset();
    layoutTrace.reset();
    layoutTrace.record("reset");
    setLayoutSnapshot(null);
    // A pending flight belongs to the previous actor. Keep it running for
    // engine-level safety, but prevent this screen from coalescing into it.
    revalidateFlightRef.current = null;
    revalidateFlightMetaRef.current = null;
    requestReceiptHandleRef.current = null;
    setRequestReceiptHandle(null);
    coalescedActionIdsRef.current.clear();
    setSyncError(null);
    setSyncWarning(null);
    if (isAndroid) {
      clearRootCellRefs();
      restoreAnchorKeyRef.current = null;
      publishMeasurementToken(positionRestore.invalidate(12));
    }
  }, [authIdentityKey, clearRootCellRefs, isAndroid, layoutTrace, positionGuard, positionRestore, publishMeasurementToken, routeState]);

  useEffect(() => {
    if (serverDiagnosticEnabled) return;
    serverDiagnosticGenerationRef.current += 1;
    serverDiagnosticControllerRef.current?.abort();
    serverDiagnosticControllerRef.current = null;
    setServerDiagnostic(null);
  }, [serverDiagnosticEnabled]);

  const runServerDocsDiagnostic = useCallback(async () => {
    if (!serverDiagnosticEnabled) return;
    if (serverDiagnosticControllerRef.current && !serverDiagnosticControllerRef.current.signal.aborted) return;
    const generation = ++serverDiagnosticGenerationRef.current;
    const actionAuthIdentity = authIdentityKey;
    const actionAuthGeneration = authSessionGenerationRef.current;
    const controller = new AbortController();
    serverDiagnosticControllerRef.current = controller;
    const actionBelongsToCurrentAuth = () =>
      serverDiagnosticGenerationRef.current === generation
      && !controller.signal.aborted
      && serverDiagnosticEnabledRef.current
      && authIdentityRef.current === actionAuthIdentity
      && authSessionGenerationRef.current === actionAuthGeneration;
    try {
      const result = await runDocsSyncDiagnostic({
        signal: controller.signal,
        onProgress: (snapshot) => {
          if (actionBelongsToCurrentAuth()) setServerDiagnostic(snapshot);
        },
      });
      if (actionBelongsToCurrentAuth()) setServerDiagnostic(result);
    } finally {
      if (serverDiagnosticControllerRef.current === controller) {
        serverDiagnosticControllerRef.current = null;
      }
    }
  }, [authIdentityKey, serverDiagnosticEnabled]);

  const revalidatePages = useCallback((options: { explicit?: boolean } = {}): Promise<void> => {
    const actionAuthIdentity = authIdentityRef.current;
    const actionAuthGeneration = authSessionGenerationRef.current;
    const actionBelongsToCurrentAuth = () =>
      authIdentityRef.current === actionAuthIdentity
      && authSessionGenerationRef.current === actionAuthGeneration;
    const explicit = options.explicit === true;
    const actionId = ++refreshActionSequenceRef.current;
    const requestedAt = Date.now();
    const networkState = online ? "online" : "offline";
    const actionKind = explicit ? "manual" : "automatic";
    const authBlocked = authChecking
      || authNeedsReauth
      || authIsAnonymous
      || auth.isAuthenticated === false;
    if (authBlocked) {
      setLastRefreshAction({
        actionId,
        kind: actionKind,
        reason: "auth_wait",
        networkState,
        requestedAt,
        requestedRunId: null,
        canonicalRunId: null,
        syncOutcome: null,
        syncSkipReason: null,
        syncFailureReason: null,
      });
      return loadPages();
    }
    if (!online && !explicit) {
      setLastRefreshAction({
        actionId,
        kind: actionKind,
        reason: "local_only",
        networkState,
        requestedAt,
        requestedRunId: null,
        canonicalRunId: null,
        syncOutcome: null,
        syncSkipReason: null,
        syncFailureReason: null,
      });
      return loadPages();
    }
    if (revalidateFlightRef.current) {
      const runningMeta = revalidateFlightMetaRef.current;
      const runningOwned = requestReceiptHandleRef.current
        ? docsSyncMetaFromRequestHandle(getSyncDiagnosticSnapshot(), requestReceiptHandleRef.current)
        : null;
      const effectiveRunningMeta: DocsSyncBinding | null = runningOwned
        ? {
          requestedRunId: runningMeta?.requestedRunId ?? requestReceiptHandleRef.current?.requestId ?? null,
          canonicalRunId: runningOwned.runId,
          syncOutcome: runningOwned.outcome,
          syncSkipReason: runningOwned.skipReason,
          syncFailureReason: runningOwned.failureReason,
        }
        : runningMeta;
      if (runningOwned && runningMeta) {
        revalidateFlightMetaRef.current = effectiveRunningMeta;
      }
      coalescedActionIdsRef.current.add(actionId);
      setLastRefreshAction({
        actionId,
        kind: actionKind,
        reason: "coalesced",
        networkState,
        requestedAt,
        requestedRunId: null,
        canonicalRunId: effectiveRunningMeta?.canonicalRunId ?? effectiveRunningMeta?.requestedRunId ?? null,
        syncOutcome: effectiveRunningMeta?.syncOutcome ?? "running",
        syncSkipReason: effectiveRunningMeta?.syncSkipReason ?? null,
        syncFailureReason: effectiveRunningMeta?.syncFailureReason ?? null,
      });
      return revalidateFlightRef.current;
    }

    setLastRefreshAction({
      actionId,
      kind: actionKind,
      reason: "sync_delegated",
      networkState,
      requestedAt,
      requestedRunId: null,
      canonicalRunId: null,
      syncOutcome: null,
      syncSkipReason: null,
      syncFailureReason: null,
    });
    const syncRequestId = loadRequestRef.current;
    const syncPromise = runSync();
    const requestedSync = latestRequestedDocsSyncMeta();
    const requestHandle = requestedSync
      ? getSyncDiagnosticRequestHandle(requestedSync.runId, requestedSync.authGeneration)
      : null;
    requestReceiptHandleRef.current = requestHandle;
    setRequestReceiptHandle(requestHandle);
    const bindingFromReceipt = (receipt: DocsSyncMeta | null): DocsSyncBinding => ({
      requestedRunId: receipt?.runId ?? null,
      canonicalRunId: receipt ? receipt.canonicalRequestId ?? receipt.runId : null,
      syncOutcome: receipt?.outcome ?? null,
      syncSkipReason: receipt?.skipReason ?? null,
      syncFailureReason: receipt?.failureReason ?? null,
    });
    const bindRequestedReceipt = (receipt: DocsSyncMeta | null) => {
      if (!actionBelongsToCurrentAuth()) return;
      let boundReceipt = receipt;
      let binding = bindingFromReceipt(receipt);
      const requestHandle = requestReceiptHandleRef.current;
      const ownedCanonical = requestHandle
        && binding.requestedRunId === requestHandle.requestId
        ? docsSyncMetaFromRequestHandle(getSyncDiagnosticSnapshot(), requestHandle)
        : null;
      if (ownedCanonical) {
        // Keep the logical waiter id as evidence, while the request-owned
        // receipt supplies the only trusted canonical target. This remains
        // valid after the waiter leaves bounded history.
        boundReceipt = ownedCanonical;
        binding = {
          ...binding,
          canonicalRunId: ownedCanonical.runId,
          syncOutcome: ownedCanonical.outcome,
          syncSkipReason: ownedCanonical.skipReason,
          syncFailureReason: ownedCanonical.failureReason,
        };
      }
      // Promise settlement may only expose the original waiter after its
      // bounded history entry has been evicted. Keep an already-resolved
      // canonical receipt monotonic when it is still retained; otherwise a
      // late waiter fallback would demote C back to W.
      const previousBinding = revalidateFlightMetaRef.current;
      if (
        previousBinding?.requestedRunId
        && binding.requestedRunId === previousBinding.requestedRunId
        && previousBinding.canonicalRunId
        && previousBinding.canonicalRunId !== binding.canonicalRunId
      ) {
        const snapshot = getSyncDiagnosticSnapshot();
        const retainedCanonical = docsSyncMetaFromSnapshot(snapshot, previousBinding.canonicalRunId);
        const canonical = retainedCanonical
          ? resolveCanonicalDocsSyncMeta(snapshot, retainedCanonical)
          : null;
        if (canonical) {
          boundReceipt = canonical;
          binding = {
            ...binding,
            canonicalRunId: canonical.runId,
            syncOutcome: canonical.outcome,
            syncSkipReason: canonical.skipReason,
            syncFailureReason: canonical.failureReason,
          };
        }
      }
      if (boundReceipt) setLastSync(boundReceipt);
      revalidateFlightMetaRef.current = binding;
      setLastRefreshAction((current) => {
        if (!current) return current;
        if (current.actionId === actionId) return { ...current, ...binding };
        if (coalescedActionIdsRef.current.has(current.actionId)) {
          return {
            ...current,
            canonicalRunId: binding.canonicalRunId,
            syncOutcome: binding.syncOutcome,
            syncSkipReason: binding.syncSkipReason,
            syncFailureReason: binding.syncFailureReason,
          };
        }
        return current;
      });
    };
    bindRequestedReceipt(requestedSync);
    const flight = syncPromise
      .then(() => {
        if (!actionBelongsToCurrentAuth()) return;
        const requestedFinal = requestedSync
          ? latestRequestedDocsSyncMeta(requestedSync.runId) ?? requestedSync
          : null;
        bindRequestedReceipt(requestedFinal);
        const latestSync = latestDocsSyncMeta();
        if (syncRequestId !== loadRequestRef.current) return;
        setLastSync(latestSync);
        const outcome = useSyncStatusStore.getState().lastOutcome;
        const docsPullValidation = getDocsPullValidation(latestSync);
        const globalFailure = latestSync?.outcome === "failed"
          || latestSync?.outcome === "partial"
          || outcome === "partial"
          || outcome === "error";
        if (docsPullValidation === "failed") {
          setSyncError("サーバーからDocsを取得できませんでした。保存済みキャッシュを表示しています。");
          setSyncWarning(null);
        } else if (docsPullValidation === "unknown") {
          setSyncError(globalFailure
            ? "Docsの同期結果を確認できません。保存済みキャッシュを表示しています。"
            : null);
          setSyncWarning(null);
        } else if (globalFailure) {
          setSyncError(null);
          setSyncWarning("同期の一部に失敗しました。Docsは取得済みですが、送信待ちの変更があります。");
        } else {
          setSyncError(null);
          setSyncWarning(null);
        }
      })
      .catch((error) => {
        if (!actionBelongsToCurrentAuth()) return;
        const requestedFinal = requestedSync
          ? latestRequestedDocsSyncMeta(requestedSync.runId) ?? requestedSync
          : null;
        bindRequestedReceipt(requestedFinal);
        if (syncRequestId !== loadRequestRef.current) return;
        const latestSync = latestDocsSyncMeta();
        setLastSync(latestSync);
        // Local data remains available while the next focus/foreground/manual
        // refresh retries the server path.
        setSyncError(safeDiagnosticMessage(error, "サーバーからDocsを取得できませんでした"));
        setSyncWarning(null);
      })
      .then(() => actionBelongsToCurrentAuth() ? loadPages() : undefined)
      .finally(() => {
        if (revalidateFlightRef.current === flight) {
          revalidateFlightRef.current = null;
          revalidateFlightMetaRef.current = null;
          requestReceiptHandleRef.current = null;
          setRequestReceiptHandle(null);
          coalescedActionIdsRef.current.clear();
        }
      });
    revalidateFlightRef.current = flight;
    return flight;
  }, [authChecking, authIsAnonymous, authNeedsReauth, auth.isAuthenticated, loadPages, online]);

  useFocusEffect(
    useCallback(() => {
      focusedRef.current = true;
      if (!isAndroid) positionGuard.focus();
      if (isAndroid) {
        const token = positionRestore.focus();
        publishMeasurementToken(token);
        tryRestorePagePosition(token);
      }
      layoutTrace.record("focus", { focused: true });
      void loadPages();
      void revalidatePages();
      return () => {
        focusedRef.current = false;
        if (!isAndroid) positionGuard.blur();
        if (isAndroid) publishMeasurementToken(positionRestore.blur());
        layoutTrace.record("blur", { focused: false });
        loadRequestRef.current += 1;
        for (const controller of loadControllersRef.current) controller.abort();
        loadControllersRef.current.clear();
        // Preserve same-auth flight identity across a tab back/focus cycle.
        if (!revalidateFlightRef.current) {
          revalidateFlightMetaRef.current = null;
          requestReceiptHandleRef.current = null;
          setRequestReceiptHandle(null);
        }
        coalescedActionIdsRef.current.clear();
      };
    }, [isAndroid, loadPages, revalidatePages, layoutTrace, positionGuard, positionRestore, publishMeasurementToken, tryRestorePagePosition]),
  );

  useEffect(() => () => {
    loadRequestRef.current += 1;
    for (const controller of loadControllersRef.current) controller.abort();
    loadControllersRef.current.clear();
    serverDiagnosticGenerationRef.current += 1;
    serverDiagnosticControllerRef.current?.abort();
    serverDiagnosticControllerRef.current = null;
  }, []);

  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active" && focusedRef.current) {
        void revalidatePages();
      }
    });
    return () => subscription.remove();
  }, [revalidatePages]);

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      await revalidatePages({ explicit: true });
    } finally {
      setRefreshing(false);
    }
  };

  const openNode = useCallback(
    (nodeId: string) => {
      router.push(`/(tabs)/docs/${nodeId}`);
    },
    [router],
  );

  const openLogin = useCallback(() => {
    router.push("/(auth)/login");
  }, [router]);

  const diagnosticText = lastRead
    ? `表示元: ローカルキャッシュ / 可視root: ${lastRead.visibleCount}`
    : "表示元: 未読込";
  const currentSync = displaySync;
  const visibleRefreshAction = useMemo(() => {
    if (!lastRefreshAction || !currentSync) return lastRefreshAction;
    return {
      ...lastRefreshAction,
      canonicalRunId: currentSync.runId,
      syncOutcome: currentSync.outcome,
      syncSkipReason: currentSync.skipReason,
      syncFailureReason: currentSync.failureReason,
    };
  }, [currentSync, lastRefreshAction]);
  // Product validation must use the same action-resolved live receipt shown in
  // diagnostics. A missing canonical receipt must stay unknown instead of
  // falling back to an older successful promise snapshot.
  const validationSync = currentSync?.outcome === "running" ? null : currentSync;
  useLayoutEffect(() => {
    if (!isAndroid) positionGuard.setRootActive(!searchVisible && !loading && pages.length > 0);
    if (isAndroid) {
      const token = positionRestore.setRootActive(!searchVisible && !loading && pages.length > 0);
      publishMeasurementToken(token);
      tryRestorePagePosition(token);
    }
    layoutTrace.record("commit", {
      focused: focusedRef.current, rootCount: pages.length,
      pullCount: currentSync?.pulls.length ?? 0,
      canonicalPending: visibleRefreshAction !== null
        && (visibleRefreshAction.requestedRunId !== null || visibleRefreshAction.canonicalRunId !== null)
        && currentSync === null,
      anchorEnabled: isAndroid ? false : preservePagePosition, clippingDisabled: true,
    });
  });
  const remoteDocsValidation = getRemoteDocsValidation(validationSync);
  const localReadDegraded = lastRead?.readStatus === "degraded";
  const emptySyncTitle = pages.length === 0 && hasRead && !readError && !syncError
    && !localReadDegraded
    ? remoteDocsValidation === "has-rows"
      ? "同期済みですが表示できるページがありません"
      : remoteDocsValidation === "unknown"
        ? "Docsの同期結果を確認できません"
        : null
    : null;
  const emptySyncDetail = remoteDocsValidation === "has-rows"
    ? "サーバーにはDocsがありますが、現在のローカル可視rootにはありません。再試行で最新の投影を確認してください。"
    : remoteDocsValidation === "unknown"
      ? "ローカル読取は成功しましたが、サーバーのDocs件数を確認できていません。再試行してください。"
      : undefined;
  const retryRead = () => void onRefresh();
  const authStatusTitle = authNeedsReauth
    ? "再ログインが必要です"
    : authChecking
      ? "認証状態を確認しています"
      : authIsAnonymous && (hasRead || !loading)
        ? "ゲストモードです"
        : null;
  const authStatusDetail = authNeedsReauth
    ? authIdentityUnknown
      ? "認証が切れ、未送信データの所有者を確認中です。確認できるまでキャッシュを保全します。"
      : "認証が切れたため、保存済みキャッシュを保全しています。再ログインするとDocsを再取得できます。"
    : authChecking
      ? "認証確認が終わるまでサーバー同期を待っています。"
      : authIsAnonymous
        ? "サーバーDocsを表示するにはログインしてください。端末内の未送信データは保持されます。"
        : undefined;
  const failureTitle = readError
    ? "Docsのローカル読込に失敗しました"
    : syncError
      ? "Docsの取得に失敗しました"
      : localReadDegraded
        ? "Docsの可視性確認が未完了です"
        : syncWarning
          ? "同期の一部に失敗しました"
          : null;
  const failureDetail = readError
    ? `${readError} 保存済みデータがあれば、そのまま表示しています。`
    : syncError
      ? `${syncError} 次回の再試行まで保存済みデータを表示しています。`
      : localReadDegraded
        ? "保存済みデータを保持しています。ローカル可視性の確認に失敗したため、正常な空状態とは判定していません。"
        : syncWarning
          ? syncWarning
          : undefined;
  const statusTitle = authStatusTitle ?? failureTitle ?? emptySyncTitle;
  const statusDetail = authStatusTitle ? authStatusDetail : failureTitle ? failureDetail : emptySyncDetail;
  const statusNeedsLogin = authNeedsReauth || authIsAnonymous;
  const statusNeedsRetry = Boolean(
    !authNeedsReauth
    && !authChecking
    && (readError || syncError || localReadDegraded || syncWarning || emptySyncTitle),
  );
  const statusCanRetry = statusNeedsRetry || authChecking || authNeedsReauth || authIsAnonymous;

  const runSearch = useCallback(
    async (value: string) => {
      const q = value.trim();
      if (!q) {
        setResults([]);
        return;
      }
      try {
        if (online) {
          const hits = await docsApi.search(q);
          setResults(
            hits.map((hit) => ({
              id: hit.id,
              title: hit.title,
              subtitle: hit.parent_title ?? undefined,
            })),
          );
        } else {
          const rows = await docsRepo.searchLocal(q);
          setResults(
            rows.map((row) => ({
              id: row.id,
              title: row.title,
              subtitle: row.description ?? undefined,
            })),
          );
        }
      } catch {
        // オフライン等の失敗時はローカル検索へフォールバック
        try {
          const rows = await docsRepo.searchLocal(q);
          setResults(
            rows.map((row) => ({ id: row.id, title: row.title })),
          );
        } catch {
          setResults([]);
        }
      }
    },
    [online],
  );

  const handleQueryChange = (value: string) => {
    setQuery(value);
    void runSearch(value);
  };

  const createPage = async () => {
    if (creatingRef.current) return;
    creatingRef.current = true;
    setCreating(true);
    try {
      // This screen lists Library roots, independent of the Tasks Project selection.
      // Project children inherit their destination from the opened parent.
      const node = await docsRepo.createNode({ title: "", projectId: null });
      if (!focusedRef.current) return;
      router.push({
        pathname: "/(tabs)/docs/[nodeId]",
        params: { nodeId: node.id, created: "1" },
      });
    } catch (error) {
      if (focusedRef.current) {
        Alert.alert("Docsを作成できません", error instanceof Error ? error.message : "保存先を同期してから再度お試しください");
      }
    } finally {
      creatingRef.current = false;
      setCreating(false);
    }
  };

  const openToday = async () => {
    if (busyToday) return;
    setBusyToday(true);
    try {
      const result = await docsApi.today();
      openNode(result.node.id);
    } catch {
      // オフライン時はローカルキャッシュ済みの Day ノードへフォールバックする。
      // 新規作成はサーバ ensure が重複排除を担うためオフラインでは行わない。
      const now = new Date();
      const today = [
        now.getFullYear(),
        String(now.getMonth() + 1).padStart(2, "0"),
        String(now.getDate()).padStart(2, "0"),
      ].join("-");
      const cached = await docsRepo.findDayNode(today).catch(() => null);
      if (cached) {
        openNode(cached.id);
      } else {
        Alert.alert(
          "デイリーページ",
          "オフラインのため今日のデイリーページを作成できません。接続後に再度お試しください。",
        );
      }
    } finally {
      setBusyToday(false);
    }
  };

  const searching = searchVisible && query.trim().length > 0;

  return (
    <View style={styles.container}>
      <ScreenHeader
        title="Docs"
        onSettingsInteraction={diagnosticsExpanded ? recordSettingsInteraction : undefined}
        right={
          <>
            <IconButton
              icon="tray-arrow-down"
              size={22}
              iconColor="#a6adc8"
              style={styles.headerIcon}
              accessibilityLabel="クリップ取り込み"
              onPress={() => setClipIngestVisible(true)}
            />
            <IconButton
              icon={searchVisible ? "magnify-close" : "magnify"}
              size={22}
              iconColor="#a6adc8"
              style={styles.headerIcon}
              accessibilityLabel={searchVisible ? "Docs検索を閉じる" : "Docsを検索"}
              onPress={() => {
                setSearchVisible((prev) => !prev);
                if (searchVisible) {
                  setQuery("");
                  setResults([]);
                }
              }}
            />
            <IconButton
              icon="calendar-today"
              size={22}
              iconColor="#a6adc8"
              style={styles.headerIcon}
              accessibilityLabel="今日のページを開く"
              disabled={busyToday}
              onPress={() => void openToday()}
            />
          </>
        }
      />

      {searchVisible ? (
        <View style={styles.searchRow}>
          <TextInput
            value={query}
            onChangeText={handleQueryChange}
            mode="outlined"
            dense
            placeholder="Docs を検索"
            autoCorrect={false}
            left={<TextInput.Icon icon="magnify" />}
            right={
              query ? (
                <TextInput.Icon icon="close" onPress={() => handleQueryChange("")} />
              ) : undefined
            }
          />
        </View>
      ) : null}

      {loading && !statusTitle ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color="#7c3aed" />
        </View>
      ) : searching ? (
        <FlatList
          data={results}
          keyExtractor={(item) => item.id}
          ItemSeparatorComponent={() => <Divider style={styles.divider} />}
          renderItem={({ item }) => (
            <List.Item
              title={() => (
                <RichTitle text={item.title || "無題"} numberOfLines={1} />
              )}
              description={item.subtitle}
              descriptionStyle={styles.itemDesc}
              left={(props) => (
                <List.Icon {...props} icon="text-search" color="#7c3aed" />
              )}
              onPress={() => openNode(item.id)}
              style={styles.listItem}
            />
          )}
          ListEmptyComponent={
            <View style={styles.center}>
              <Text style={styles.emptyText}>該当するページがありません</Text>
            </View>
          }
        />
      ) : (
        <DocsRootCellMeasurementContext.Provider
          value={isAndroid ? {
            onLayout: onRootCellLayout,
            onCellRef: (key, view, releasedView) => onRootCellRef(key, view, releasedView, measurementToken),
          } : null}
        >
          <FlatList
            testID="docs-root-list"
            ref={rootListRef}
            onScroll={onRootScroll}
            onLayout={(event) => {
              const height = event.nativeEvent.layout.height;
              if (isAndroid) {
                positionRestore.observeViewportLayout(height, measurementToken);
                tryRestorePagePosition(measurementToken);
              }
              layoutTrace.record("viewport-layout", { viewportHeight: height });
            }}
            onContentSizeChange={(_width, height) => {
              if (!isAndroid) positionGuard.contentSize(height);
              if (isAndroid) {
                positionRestore.observeContentSize(height, measurementToken);
                tryRestorePagePosition(measurementToken);
              }
              layoutTrace.record("content-size", { contentHeight: height });
            }}
            onScrollBeginDrag={(event) => {
              if (!isAndroid) positionGuard.beginDrag(rootScrollGeometry(event));
              if (isAndroid) positionRestore.beginUserGesture(measurementToken);
              layoutTrace.record("drag-begin");
            }}
            onScrollEndDrag={(event) => {
              if (!isAndroid) {
                positionGuard.endDrag(rootScrollGeometry(event),
                (event.nativeEvent.velocity?.y ?? 0) !== 0);
              }
              layoutTrace.record("drag-end");
            }}
            onMomentumScrollBegin={(event) => {
              if (!isAndroid) positionGuard.beginMomentum(rootScrollGeometry(event));
              layoutTrace.record("momentum-begin");
            }}
            onMomentumScrollEnd={(event) => {
              if (!isAndroid) positionGuard.endMomentum(rootScrollGeometry(event));
              layoutTrace.record("momentum-end");
            }}
            onViewableItemsChanged={onRootViewableItemsChanged}
            scrollEventThrottle={16}
            maintainVisibleContentPosition={
              isAndroid ? undefined : preservePagePosition ? { minIndexForVisible: 0 } : undefined
            }
            CellRendererComponent={isAndroid ? DocsRootCell : undefined}
            // Keep native clipping disabled from mount, including navigation
            // and MVP transitions. FlatList's JS virtualization remains enabled.
            removeClippedSubviews={false}
            data={pages}
            keyExtractor={(item) => item.id}
            ItemSeparatorComponent={() => <Divider style={styles.divider} />}
            refreshControl={
              <RefreshControl
                refreshing={refreshing}
                onRefresh={onRefresh}
                testID="docs-refresh"
                tintColor="#7c3aed"
              />
            }
            ListHeaderComponent={
              pages.length > 0 ? (
                <View
                  testID="docs-root-header"
                  ref={rootHeaderRef}
                  collapsable={false}
                  onLayout={(event) => {
                    const height = event.nativeEvent.layout.height;
                    rootHeaderHeightRef.current = height;
                    if (!isAndroid) positionGuard.headerLayout();
                    if (isAndroid) {
                      positionRestore.observeHeader(height, measurementToken);
                      tryRestorePagePosition(measurementToken);
                    }
                    layoutTrace.record("header-layout", { headerHeight: height });
                  }}
                >
                  {statusTitle ? (
                    <DocsStatusCard
                      title={statusTitle}
                      detail={statusDetail}
                      diagnosticSummary={diagnosticText}
                      local={lastRead}
                      sync={currentSync}
                      refreshAction={visibleRefreshAction}
                      settingsInteractions={settingsInteractions}
                      routeState={routeState}
                      routeHistory={routeHistory}
                      settingsRouteTransition={settingsRouteTransition}
                      diagnosticsExpanded={diagnosticsExpanded}
                      onToggleDiagnostics={() => setDiagnosticsExpanded((value) => !value)}
                      onInspectLayout={() => setLayoutSnapshot(layoutTrace.snapshot())}
                      serverDiagnostic={serverDiagnostic}
                      serverDiagnosticEnabled={serverDiagnosticEnabled}
                      onRunServerDiagnostic={() => void runServerDocsDiagnostic()}
                      retry={statusCanRetry ? retryRead : undefined}
                      reauthenticate={statusNeedsLogin ? openLogin : undefined}
                      compact
                    />
                  ) : (
                    <DocsDiagnostics
                      local={lastRead}
                      sync={currentSync}
                      refreshAction={visibleRefreshAction}
                      settingsInteractions={settingsInteractions}
                      routeState={routeState}
                      routeHistory={routeHistory}
                      settingsRouteTransition={settingsRouteTransition}
                      expanded={diagnosticsExpanded}
                      onToggle={() => setDiagnosticsExpanded((value) => !value)}
                      onInspectLayout={() => setLayoutSnapshot(layoutTrace.snapshot())}
                      serverDiagnostic={serverDiagnostic}
                      serverDiagnosticEnabled={serverDiagnosticEnabled}
                      onRunServerDiagnostic={() => void runServerDocsDiagnostic()}
                    />
                  )}
                </View>
              ) : null
            }
            renderItem={({ item }) => (
              <List.Item
                title={() => (
                  <RichTitle text={item.title || "無題のページ"} numberOfLines={1} />
                )}
                description={item.description ?? undefined}
                descriptionStyle={styles.itemDesc}
                descriptionNumberOfLines={1}
                left={(props) => (
                  <List.Icon {...props} icon="file-document-outline" color="#7c3aed" />
                )}
                onPress={() => {
                  // Preserve the tapped row's viewport position across the
                  // detail transition, including a partially visible header.
                  if (isAndroid) {
                    positionRestore.cancelUserGesture(measurementToken);
                    if (positionRestore.capture(item.id, measurementToken)) {
                      restoreAnchorKeyRef.current = item.id;
                    }
                  }
                  if (!isAndroid) {
                    positionGuard.cancelGesture();
                    setPreservePagePosition(true);
                  }
                  openNode(item.id);
                }}
                style={styles.listItem}
              />
            )}
            ListEmptyComponent={
              statusTitle ? (
                <DocsStatusCard
                  title={statusTitle}
                  detail={statusDetail}
                  diagnosticSummary={diagnosticText}
                  local={lastRead}
                  sync={currentSync}
                  refreshAction={visibleRefreshAction}
                  settingsInteractions={settingsInteractions}
                  routeState={routeState}
                  routeHistory={routeHistory}
                  settingsRouteTransition={settingsRouteTransition}
                  diagnosticsExpanded={diagnosticsExpanded}
                  onToggleDiagnostics={() => setDiagnosticsExpanded((value) => !value)}
                  serverDiagnostic={serverDiagnostic}
                  serverDiagnosticEnabled={serverDiagnosticEnabled}
                  onRunServerDiagnostic={() => void runServerDocsDiagnostic()}
                  retry={statusCanRetry ? retryRead : undefined}
                  reauthenticate={statusNeedsLogin ? openLogin : undefined}
                />
              ) : (
                <View style={styles.center}>
                  <Text style={styles.emptyText}>表示できるページはありません</Text>
                  <Text style={styles.emptySubtext}>
                    読込に成功しました。右下の + から新規ページを作成できます
                  </Text>
                  <Text style={styles.diagnosticText}>{diagnosticText}</Text>
                  <DocsDiagnostics
                    local={lastRead}
                    sync={currentSync}
                    refreshAction={visibleRefreshAction}
                    settingsInteractions={settingsInteractions}
                    routeState={routeState}
                    routeHistory={routeHistory}
                    settingsRouteTransition={settingsRouteTransition}
                    expanded={diagnosticsExpanded}
                    onToggle={() => setDiagnosticsExpanded((value) => !value)}
                    serverDiagnostic={serverDiagnostic}
                    serverDiagnosticEnabled={serverDiagnosticEnabled}
                    onRunServerDiagnostic={() => void runServerDocsDiagnostic()}
                  />
                </View>
              )
            }
            contentContainerStyle={
              pages.length === 0 ? styles.emptyContainer : undefined
            }
          />
        </DocsRootCellMeasurementContext.Provider>
      )}

      <FAB
        disabled={creating}
        loading={creating}
        icon="plus"
        style={styles.fab}
        onPress={() => void createPage()}
        color="#cdd6f4"
      />
      <ClipIngestDialog
        visible={clipIngestVisible}
        onDismiss={() => setClipIngestVisible(false)}
        onOpenNode={(nodeId) => {
          setClipIngestVisible(false);
          openNode(nodeId);
        }}
      />
      <DocsListLayoutInspector snapshot={layoutSnapshot} onDismiss={() => setLayoutSnapshot(null)} />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  headerIcon: { margin: 0, width: 44, height: 44, flexShrink: 0 },
  searchRow: {
    paddingHorizontal: 12,
    paddingVertical: 8,
    backgroundColor: "#181825",
  },
  center: { flex: 1, alignItems: "center", justifyContent: "center", paddingTop: 60 },
  statusCard: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 24,
    paddingTop: 60,
  },
  notice: {
    paddingHorizontal: 16,
    paddingVertical: 10,
    backgroundColor: "#181825",
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#313244",
  },
  emptyContainer: { flexGrow: 1 },
  emptyText: { color: "#a6adc8", fontSize: 16 },
  emptySubtext: { color: "#585b70", fontSize: 13, marginTop: 4 },
  diagnosticText: { color: "#6c7086", fontSize: 11, marginTop: 8, textAlign: "center" },
  diagnostics: { width: "100%", marginTop: 10, backgroundColor: "#181825", borderRadius: 6 },
  diagnosticsToggle: { paddingHorizontal: 10, paddingVertical: 8 },
  diagnosticsAction: { minHeight: 44, justifyContent: "center" },
  diagnosticsToggleText: { color: "#89b4fa", fontSize: 12 },
  diagnosticsDetails: { paddingHorizontal: 10, paddingBottom: 8 },
  diagnosticsLine: { color: "#a6adc8", fontSize: 11, lineHeight: 17 },
  diagnosticsPull: { marginTop: 6 },
  statusActions: { flexDirection: "row", gap: 8, marginTop: 12 },
  statusAction: {
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 6,
    backgroundColor: "#313244",
  },
  statusActionText: { color: "#cdd6f4", fontSize: 13 },
  listItem: { backgroundColor: "#11111b", paddingVertical: 4 },
  itemDesc: { color: "#a6adc8", fontSize: 12 },
  divider: { backgroundColor: "#313244" },
  fab: {
    position: "absolute",
    right: 16,
    bottom: 16,
    backgroundColor: "#7c3aed",
  },
});

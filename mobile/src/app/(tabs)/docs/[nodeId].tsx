import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  AppState,
  Pressable,
  ScrollView,
  StyleSheet,
  TextInput as NativeTextInput,
  View,
} from "react-native";
import {
  ActivityIndicator,
  Button,
  Dialog,
  IconButton,
  List,
  Portal,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { useFocusEffect, useLocalSearchParams, useNavigation, useRouter } from "expo-router";
import { usePreventRemove } from "@react-navigation/native";
import { useAuth } from "../../../contexts/AuthContext";
import { goBackOrReplace } from "../../../lib/navigation";
import { getTokenSnapshot } from "../../../lib/auth";
import { registerOutboxResolutionEditor } from "../../../lib/outbox-resolution-editor-guard";
import { isApiHttpError } from "../../../lib/api-client";
import { ScreenHeader } from "../../../components/screen-header";
import {
  OutlineEditor,
  type OutlineEditorHandle,
} from "../../../components/docs/outline-editor";
import { TagPicker } from "../../../components/docs/tag-picker";
import { FieldEditor } from "../../../components/docs/field-editor";
import { MovePicker } from "../../../components/docs/move-picker";
import { ClipIngestDialog } from "../../../components/docs/clip-ingest-dialog";
import { DocsTaskBinding } from "../../../components/docs/task-binding";
import { DocBlockEditor } from "../../../components/docs/verbatim-blocks";
import {
  DocBlockDraftStore,
  serializeDocsNodeEdit,
  waitForDocsEditorIdle,
} from "../../../components/docs/doc-block-drafts";
import {
  adoptDocsNodeServerConflict,
  docsRepo,
} from "../../../repositories/docs";
import { docsApi } from "../../../lib/docs-api";
import { useNetworkStore } from "../../../stores/network";
import { runSync } from "../../../sync/engine";
import {
  getOutboxConflict,
  rebaseOutboxConflict,
  type OutboxConflictReference,
} from "../../../repositories/outbox";
import type { DocsNode, DocsSupertag } from "../../../types/api";

type ConflictField = "title" | "description" | "body_text" | "body_json";

const DOCS_LOAD_ERROR_MESSAGE = "文書を読み込めませんでした。再試行してください。";
type DocsCanonicalProjectionMode = "blocking" | "deferred";

type DocsScreenMetricDetails = {
  result?: "success" | "error" | "stale" | "hit" | "miss" | "cleared" | "blocked";
  errorClass?: string;
  readOnly?: boolean;
  aclKnown?: boolean;
};

let docsScreenMetricSequence = 0;

const DOCS_SCREEN_SAFE_ERROR_CLASSES = new Set([
  "AbortError",
  "ApiConnectionError",
  "ApiHttpError",
  "ApiTimeoutError",
  "DocsCanonicalResponseMalformedError",
  "DocsCanonicalResponseStaleError",
  "Error",
  "RangeError",
  "TypeError",
]);

function nextDocsScreenTraceId(): string {
  docsScreenMetricSequence = (docsScreenMetricSequence + 1) % 1_000_000;
  return `screen-${docsScreenMetricSequence.toString(36)}`;
}

function docsScreenDebugMetricsEnabled(): boolean {
  return process.env.EXPO_PUBLIC_DOCS_LOAD_DEBUG_METRICS === "1";
}

function docsScreenMetricNow(): number {
  return typeof globalThis.performance?.now === "function"
    ? globalThis.performance.now()
    : Date.now();
}

function safeDocsScreenErrorClass(error: unknown): string {
  const name = error instanceof Error ? error.name : "";
  return DOCS_SCREEN_SAFE_ERROR_CLASSES.has(name) ? name : "UnknownError";
}

function emitDocsScreenMetric(
  traceId: string,
  startedAt: number,
  phase: string,
  details: DocsScreenMetricDetails = {},
): void {
  if (!docsScreenDebugMetricsEnabled()) return;
  console.info("[DocsLoadMetric]", {
    trace: traceId,
    phase,
    elapsedMs: Math.max(0, Math.round(docsScreenMetricNow() - startedAt)),
    ...(details.result ? { result: details.result } : {}),
    ...(details.errorClass ? { errorClass: details.errorClass } : {}),
    ...(details.readOnly !== undefined ? { readOnly: details.readOnly } : {}),
    ...(details.aclKnown !== undefined ? { aclKnown: details.aclKnown } : {}),
  });
}

function docsScreenNodeMetricDetails(node: DocsNode | null): DocsScreenMetricDetails {
  if (!node) return { result: "miss", readOnly: true, aclKnown: false };
  const aclKnown = typeof node.access === "string" && typeof node.read_only === "boolean";
  return {
    result: "hit",
    readOnly: Boolean(node.read_only || node.access === "read"),
    aclKnown,
  };
}

function isCanonicalDocsNotFoundError(error: unknown): boolean {
  return isApiHttpError(error)
    && error.status === 404
    && /Docs node not found/i.test(error.responseBody ?? "");
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableRecord(value: unknown): value is Record<string, unknown> | null {
  return value === null
    || (typeof value === "object" && value !== null && !Array.isArray(value));
}

function isDocsNodePayload(value: unknown, expectedId: string): value is DocsNode {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const node = value as Record<string, unknown>;
  return node.id === expectedId
    && isNullableString(node.workspace_id)
    && isNullableString(node.parent_id)
    && isNullableString(node.root_page_id)
    && isNullableString(node.project_id)
    && isNullableString(node.system_key)
    && typeof node.title === "string"
    && Array.isArray(node.aliases)
    && node.aliases.every((alias) => typeof alias === "string")
    && isNullableString(node.description)
    && isNullableRecord(node.body_json)
    && isNullableString(node.body_text)
    && (node.node_type === "node"
      || node.node_type === "search"
      || node.node_type === "day"
      || node.node_type === "system")
    && isNullableRecord(node.display_props)
    && isNullableRecord(node.query_json)
    && isNullableRecord(node.view_json)
    && isNullableString(node.day_date)
    && (node.sort_order === null
      || (typeof node.sort_order === "number" && Number.isFinite(node.sort_order)))
    && isNullableString(node.created_by)
    && isNullableString(node.updated_by)
    && isNullableString(node.created_at)
    && isNullableString(node.updated_at)
    && isNullableString(node.archived_at)
    && (node.source === undefined || isNullableString(node.source))
    && (node.access === undefined || isNullableString(node.access))
    && (node.read_only === undefined || typeof node.read_only === "boolean");
}

function parseConflictPayload(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== "string" || !value.trim()) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function formatConflictValue(value: unknown): string {
  if (value == null || value === "") return "（空）";
  if (typeof value === "string") return value;
  try {
    const text = JSON.stringify(value);
    return text.length > 600 ? `${text.slice(0, 600)}…` : text;
  } catch {
    return String(value);
  }
}

function docsScopeKey(node: DocsNode | null): string | null {
  if (!node?.workspace_id) return null;
  return `${node.workspace_id}|project:${node.project_id ?? ""}`;
}

function conflictReasonMessage(reason?: string): string {
  switch (reason) {
    case "auth_scope_changed":
      return "アカウントが切り替わったため、競合の解決を中止しました。再読み込みしてください。";
    case "docs_scope_missing":
    case "docs_scope_mismatch":
    case "docs_scope_not_writable":
    case "node_not_writable":
      return "Docsの権限が変わったため、競合を解決できません。最新の権限を確認してください。";
    case "server_scope_missing":
    case "server_scope_mismatch":
    case "server_id_mismatch":
      return "サーバー版のDocsスコープを確認できないため、競合を解決できません。再同期してください。";
    case "missing_server_snapshot":
    case "server_version_missing":
      return "サーバー版が取得できないため、競合を解決できません。再同期してください。";
    case "outbox_replaced":
      return "端末で新しい編集が発生したため、古い競合操作は適用しませんでした。画面を更新してください。";
    default:
      return "競合を解決できませんでした。最新状態を取得して再試行してください。";
  }
}

function conflictFieldValue(
  field: ConflictField,
  localPayload: Record<string, unknown>,
  localNode: DocsNode | null,
  serverPayload: Record<string, unknown>,
): { local: unknown; server: unknown } {
  const localFallback: Record<ConflictField, unknown> = {
    title: localNode?.title ?? "",
    description: localNode?.description ?? "",
    body_text: localNode?.body_text ?? "",
    body_json: localNode?.body_json ?? null,
  };
  const serverKey = field;
  const localKey = field;
  return {
    local: localPayload[localKey] ?? localFallback[field],
    server: serverPayload[serverKey],
  };
}

export default function DocsNodeScreen() {
  const router = useRouter();
  const navigation = useNavigation();
  const params = useLocalSearchParams<{ nodeId: string; created?: string }>();
  const nodeId = params.nodeId;
  const { user, isAnonymous, reauthRequired } = useAuth();
  const authIdentity = user
    ? `user:${user.user_id ?? "unknown"}:${reauthRequired ? "reauth" : "active"}`
    : isAnonymous
      ? "anonymous"
      : "signed-out";
  const outboxResolutionAuthScope = isAnonymous
    ? null
    : user?.user_id?.trim()
      ? `auth:${user.user_id.trim()}`
      : "metadata-only";
  // Docs APIs use the AoiTalk server path; Internet reachability is unrelated
  // when the server is hosted on the same LAN.
  const online = useNetworkStore((state) => state.connected ?? state.online);
  const [nodeState, setNodeState] = useState<DocsNode | null>(null);
  const [localSyncWarning, setLocalSyncWarning] = useState<string | null>(null);
  const [tags, setTags] = useState<DocsSupertag[]>([]);
  const [backlinks, setBacklinks] = useState<DocsNode[]>([]);
  const [outgoingReferences, setOutgoingReferences] = useState<DocsNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [titleDraft, setTitleDraft] = useState("");
  const [titleEditing, setTitleEditing] = useState(false);
  const [descDraft, setDescDraft] = useState("");
  const [propertiesVisible, setPropertiesVisible] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [loadError, setLoadError] = useState(false);
  const [moveTargetId, setMoveTargetId] = useState<string | null>(null);
  const [clipIngestVisible, setClipIngestVisible] = useState(false);
  const [conflict, setConflict] = useState<OutboxConflictReference | null>(null);
  const [conflictBusy, setConflictBusy] = useState(false);
  const [conflictMessage, setConflictMessage] = useState<string | null>(null);
  const draftsInitialized = useRef(false);
  const initialFocusApplied = useRef(false);
  const titleInputRef = useRef<{ focus: () => void } | null>(null);
  const outlineRef = useRef<OutlineEditorHandle>(null);
  const lastOutlineHandleRef = useRef<OutlineEditorHandle | null>(null);
  const setOutlineHandle = useCallback((handle: OutlineEditorHandle | null) => {
    outlineRef.current = handle;
    if (handle) lastOutlineHandleRef.current = handle;
  }, []);
  const pendingDocsOperationsRef = useRef(new Set<Promise<unknown>>());
  const activeNodeIdRef = useRef(nodeId);
  activeNodeIdRef.current = nodeId;
  const nodeOwnerNodeIdRef = useRef<string | null>(null);
  // AuthProvider intentionally keeps the app mounted during account changes;
  // this ref is the screen-local ownership boundary for every async result.
  const authIdentityRef = useRef(authIdentity);
  authIdentityRef.current = authIdentity;
  const nodeAuthIdentityRef = useRef<string | null>(null);
  const loadGeneration = useRef(0);
  const nodeLoadRequestRef = useRef(0);
  const focusedRef = useRef(false);
  const titleEditingRef = useRef(false);
  const propertiesVisibleRef = useRef(false);
  const titleDraftRef = useRef("");
  const descDraftRef = useRef("");
  const titleDirtyRef = useRef(false);
  const descDirtyRef = useRef(false);
  const titleSaveCountRef = useRef(0);
  const descSaveCountRef = useRef(0);
  const titleDraftRevisionRef = useRef(0);
  const descDraftRevisionRef = useRef(0);
  const revalidateFlightRef = useRef<Promise<void> | null>(null);

  const commitNode = useCallback(
    (
      next: DocsNode | null,
      identity = authIdentityRef.current,
      ownerNodeId = activeNodeIdRef.current,
    ) => {
      if (activeNodeIdRef.current !== ownerNodeId) return false;
      nodeOwnerNodeIdRef.current = ownerNodeId;
      nodeAuthIdentityRef.current = identity;
      setNodeState(next);
      return true;
    },
    [],
  );

  const node = nodeOwnerNodeIdRef.current === nodeId
    && nodeAuthIdentityRef.current === authIdentity
    ? nodeState
    : null;
  const visibleLocalSyncWarning = node ? localSyncWarning : null;
  const visibleTags = node ? tags : [];
  const visibleBacklinks = node ? backlinks : [];
  const visibleOutgoingReferences = node ? outgoingReferences : [];
  const visibleConflict = node ? conflict : null;
  const screenLoading = loading
    || nodeOwnerNodeIdRef.current !== nodeId
    || nodeAuthIdentityRef.current !== authIdentity;

  const protectedProjectNode = Boolean(
    node
    && (() => {
      const systemKey = typeof node.system_key === "string" ? node.system_key.trim() : "";
      return systemKey === "project_information_root"
        || systemKey.startsWith("project_information:");
    })(),
  );
  const nodeReadOnly = Boolean(
    !node || visibleLocalSyncWarning || node?.read_only || node?.access === "read",
  );
  const bodyWritableRef = useRef(!nodeReadOnly && !protectedProjectNode);
  bodyWritableRef.current = !nodeReadOnly && !protectedProjectNode;
  const blockDrafts = useMemo(() => new DocBlockDraftStore(() => bodyWritableRef.current), [authIdentity, nodeId]);
  const [bodyPending, setBodyPending] = useState(false);
  const [outlineBodyPending, setOutlineBodyPending] = useState(false);
  useEffect(() => {
    setBodyPending(blockDrafts.pending);
    return blockDrafts.subscribe(() => setBodyPending(blockDrafts.pending));
  }, [blockDrafts]);
  useEffect(() => blockDrafts.retain(), [blockDrafts]);
  const trackDocsOperation = useCallback(<T,>(operation: () => Promise<T>): Promise<T> => {
    const flight = operation();
    pendingDocsOperationsRef.current.add(flight);
    void flight.then(
      () => {
        pendingDocsOperationsRef.current.delete(flight);
      },
      () => {
        pendingDocsOperationsRef.current.delete(flight);
      },
    );
    return flight;
  }, []);
  const performFlushBodies = useCallback(async () => {
    await blockDrafts.flush();
    await outlineRef.current?.flushPending?.();
  }, [blockDrafts]);
  const flushBodies = useCallback(
    () => trackDocsOperation(() => performFlushBodies()),
    [performFlushBodies, trackDocsOperation],
  );

  const conflictLocalPayload = visibleConflict
    ? parseConflictPayload(visibleConflict.payload)
    : {};
  const conflictServerPayload = visibleConflict
    ? parseConflictPayload(visibleConflict.conflictPayload)
    : {};
  const conflictFields: ConflictField[] = [
    "title",
    "description",
    "body_text",
    "body_json",
  ];
  const conflictWritable = Boolean(
    visibleConflict
    && node
    && !nodeReadOnly
    && !protectedProjectNode
    && !node.read_only
    && node.access !== "read"
    && visibleConflict.docsScopeKey,
  );

  const bump = useCallback(() => {
    if (nodeAuthIdentityRef.current !== authIdentityRef.current) return;
    setReloadToken((value) => value + 1);
  }, []);
  const loadNode = useCallback(async (
    generation: number,
    refreshDrafts = false,
    projection: DocsCanonicalProjectionMode = "deferred",
  ): Promise<DocsNode | null> => {
    const traceId = nextDocsScreenTraceId();
    const metricStartedAt = docsScreenMetricNow();
    emitDocsScreenMetric(traceId, metricStartedAt, "load_start");
    const requestNodeId = nodeId;
    const requestAuthIdentity = authIdentityRef.current;
    // Actor identity alone is insufficient during refresh/logout races, so
    // retain the token generation and reject any result from an older one.
    emitDocsScreenMetric(traceId, metricStartedAt, "auth_snapshot_start");
    let requestAuthSnapshot: Awaited<ReturnType<typeof getTokenSnapshot>> | null;
    try {
      requestAuthSnapshot = await getTokenSnapshot();
      emitDocsScreenMetric(traceId, metricStartedAt, "auth_snapshot_end", { result: "success" });
    } catch (error) {
      requestAuthSnapshot = null;
      emitDocsScreenMetric(traceId, metricStartedAt, "auth_snapshot_error", {
        result: "error",
        errorClass: safeDocsScreenErrorClass(error),
      });
    }
    if (!requestAuthSnapshot) {
      if (
        authIdentityRef.current === requestAuthIdentity
        && activeNodeIdRef.current === requestNodeId
        && loadGeneration.current === generation
      ) {
        setLoadError(true);
        setLoading(false);
      }
      return null;
    }
    const requestId = ++nodeLoadRequestRef.current;
    const isCurrent = () =>
      loadGeneration.current === generation &&
      nodeLoadRequestRef.current === requestId &&
      authIdentityRef.current === requestAuthIdentity &&
      activeNodeIdRef.current === requestNodeId;
    const isAuthCurrent = async () => {
      if (!isCurrent()) return false;
      const current = await getTokenSnapshot().catch(() => null);
      return Boolean(
        current
        && isCurrent()
        && current.token === requestAuthSnapshot.token
        && current.revision === requestAuthSnapshot.revision,
      );
    };
    const loadRelations = () => {
      // Start relation reads only after the canonical read-through has
      // completed.  A pre-projection tags read can otherwise resolve with an
      // empty local result after the node itself has already been promoted.
      void Promise.resolve()
        .then(() => docsRepo.getNodeTags(requestNodeId))
        .then(async (loadedTags) => {
          if (await isAuthCurrent()) setTags(loadedTags);
        })
        .catch(async () => {
          if (await isAuthCurrent()) setTags([]);
        });
      void Promise.resolve()
        .then(() => docsRepo.getBacklinks(requestNodeId))
        .then(async (links) => {
          if (await isAuthCurrent()) setBacklinks(links);
        })
        .catch(async () => {
          if (await isAuthCurrent()) setBacklinks([]);
        });
      void Promise.resolve()
        .then(() => docsRepo.getOutgoingReferences(requestNodeId))
        .then(async (links) => {
          if (await isAuthCurrent()) setOutgoingReferences(links);
        })
        .catch(async () => {
          if (await isAuthCurrent()) setOutgoingReferences([]);
        });
    };
    const titleDraftRevision = titleDraftRevisionRef.current;
    const descDraftRevision = descDraftRevisionRef.current;

    try {
      // A remote Clip/search/today result may be navigated to before the
      // staged Docs pull has promoted its row.  Use the ACL-checked canonical
      // node endpoint as a read-through fallback instead of showing a false
      // "not found" screen; the API adapter applies the snapshot itself.
      emitDocsScreenMetric(traceId, metricStartedAt, "local_getNode_start");
      let loaded: DocsNode | null;
      try {
        loaded = await docsRepo.getNode(requestNodeId);
        emitDocsScreenMetric(traceId, metricStartedAt, "local_getNode_end", docsScreenNodeMetricDetails(loaded));
      } catch (error) {
        emitDocsScreenMetric(traceId, metricStartedAt, "local_getNode_error", {
          result: "error",
          errorClass: safeDocsScreenErrorClass(error),
        });
        throw error;
      }
      if (loaded && !isDocsNodePayload(loaded, requestNodeId)) {
        if (await isAuthCurrent()) setLoadError(true);
        return null;
      }
      let remoteLocalSyncWarning: string | null = null;
      // Recheck only the opened node when a legacy cache is readonly.
      // Canonical ACL projection preserves pending content; children remain
      // subject to their own normal navigation and sync permission checks.
      if ((!loaded || loaded.read_only === true || loaded.access === "read") && online) {
        try {
          const remote = await docsApi.getNode(requestNodeId, { projection, traceId });
          if (!isDocsNodePayload(remote?.node, requestNodeId)) {
            if (await isAuthCurrent()) setLoadError(true);
            return null;
          }
          remoteLocalSyncWarning = remote.local_sync_warning ?? null;
          // A deferred canonical read is the fresh, ACL-checked in-memory
          // value.  Re-reading the old readonly row here would keep the
          // header stale while the projection waits behind full sync.
          if (remoteLocalSyncWarning) {
            loaded = remote.node;
          } else {
            emitDocsScreenMetric(traceId, metricStartedAt, "local_getNode_start");
            try {
              loaded = await docsRepo.getNode(requestNodeId) ?? remote.node;
              emitDocsScreenMetric(traceId, metricStartedAt, "local_getNode_end", docsScreenNodeMetricDetails(loaded));
            } catch (error) {
              emitDocsScreenMetric(traceId, metricStartedAt, "local_getNode_error", {
                result: "error",
                errorClass: safeDocsScreenErrorClass(error),
              });
              throw error;
            }
          }
          emitDocsScreenMetric(traceId, metricStartedAt, "canonical_node_selected", {
            ...docsScreenNodeMetricDetails(loaded),
          });
        } catch (error) {
          if (isCanonicalDocsNotFoundError(error)) {
            // A canonical 404 is the only remote read failure treated as a
            // genuine absence.  Auth/transport/route errors retain the last
            // same-node state and surface a retryable load error below.
          } else {
            if (await isAuthCurrent()) setLoadError(true);
            return null;
          }
        }
      }
      const finalAuthCurrent = await isAuthCurrent();
      emitDocsScreenMetric(traceId, metricStartedAt, "final_auth_guard", {
        result: finalAuthCurrent ? "success" : "stale",
      });
      if (!finalAuthCurrent) {
        return null;
      }
      setLoadError(false);
      commitNode(loaded, requestAuthIdentity, requestNodeId);
      emitDocsScreenMetric(traceId, metricStartedAt, "commit_node", docsScreenNodeMetricDetails(loaded));
      setLocalSyncWarning(remoteLocalSyncWarning);
      if (!loaded) {
        setConflict(null);
        setConflictMessage(null);
      } else if (typeof getOutboxConflict === "function") {
        // Conflicts are read through the account/scope-bound outbox helper;
        // never infer one from a sibling project that happens to share UUIDs.
        emitDocsScreenMetric(traceId, metricStartedAt, "outbox_conflict_start");
        try {
          const loadedConflict = await getOutboxConflict(
            "knowledge_nodes",
            requestNodeId,
            { docsScopeKey: docsScopeKey(loaded) },
          );
          emitDocsScreenMetric(traceId, metricStartedAt, "outbox_conflict_end", {
            result: loadedConflict ? "hit" : "miss",
          });
          if (await isAuthCurrent()) {
            setConflict(loadedConflict);
            if (loadedConflict) setConflictMessage(null);
          }
        } catch (error) {
          emitDocsScreenMetric(traceId, metricStartedAt, "outbox_conflict_error", {
            result: "error",
            errorClass: safeDocsScreenErrorClass(error),
          });
          if (await isAuthCurrent()) setConflict(null);
        }
      }
      if (!(await isAuthCurrent())) return null;
      if (loaded && !remoteLocalSyncWarning) loadRelations();
      if (loaded) {
        if (
          !draftsInitialized.current ||
          (
            refreshDrafts &&
            !titleEditingRef.current &&
            !titleDirtyRef.current &&
            titleSaveCountRef.current === 0 &&
            titleDraftRevisionRef.current === titleDraftRevision
          )
        ) {
          const nextTitle = loaded.title ?? "";
          titleDraftRef.current = nextTitle;
          titleDirtyRef.current = false;
          setTitleDraft(nextTitle);
        }
        if (
          !draftsInitialized.current ||
          (
            refreshDrafts &&
            !propertiesVisibleRef.current &&
            !descDirtyRef.current &&
            descSaveCountRef.current === 0 &&
            descDraftRevisionRef.current === descDraftRevision
          )
        ) {
          const nextDescription = loaded.description ?? "";
          descDraftRef.current = nextDescription;
          descDirtyRef.current = false;
          setDescDraft(nextDescription);
        }
        draftsInitialized.current = true;
      }
      return loaded;
    } catch (error) {
      emitDocsScreenMetric(traceId, metricStartedAt, "load_error", {
        result: "error",
        errorClass: safeDocsScreenErrorClass(error),
      });
      if (await isAuthCurrent()) setLoadError(true);
      return null;
    } finally {
      const authCurrentForLoading = await isAuthCurrent();
      if (authCurrentForLoading) {
        emitDocsScreenMetric(traceId, metricStartedAt, "loading_clear", { result: "cleared" });
        setLoading(false);
      } else if (isCurrent()) {
        emitDocsScreenMetric(traceId, metricStartedAt, "loading_clear", { result: "blocked" });
        // Token refresh can keep the same actor and screen mounted. Retry
        // with the new snapshot instead of leaving the discarded load busy.
        setReloadToken((value) => value + 1);
      }
    }
  }, [authIdentity, commitNode, nodeId, online]);

  const revalidateNode = useCallback(
    (generation: number): Promise<void> => {
      if (!online) return Promise.resolve();
      let syncFlight = revalidateFlightRef.current;
      if (!syncFlight) {
        const flight = runSync()
          .catch(() => {
            // ローカル表示は維持し、次のfocus/foregroundで再試行する。
          })
          .finally(() => {
            if (revalidateFlightRef.current === flight) {
              revalidateFlightRef.current = null;
            }
          });
        revalidateFlightRef.current = flight;
        syncFlight = flight;
      }

      return syncFlight.then(async () => {
        if (
          !focusedRef.current ||
          loadGeneration.current !== generation
        ) {
          return;
        }
        const refreshedNode = await loadNode(generation, true, "blocking");
        if (!refreshedNode) return;
        try { await flushBodies(); } catch { return; }
        if (
          focusedRef.current &&
          loadGeneration.current === generation
        ) {
          try {
            await outlineRef.current?.reloadFromRepository();
          } catch {
            // 保存できなかったdraftはOutlineEditor内に残し、次回の同期で再試行する。
          }
        }
      });
    },
    [flushBodies, loadNode, online],
  );

  useEffect(() => {
    loadGeneration.current += 1;
    nodeLoadRequestRef.current += 1;
    commitNode(null);
    setLocalSyncWarning(null);
    setTags([]);
    setBacklinks([]);
    setOutgoingReferences([]);
    setLoading(true);
    setLoadError(false);
    draftsInitialized.current = false;
    initialFocusApplied.current = false;
    titleDraftRef.current = "";
    descDraftRef.current = "";
    titleDirtyRef.current = false;
    descDirtyRef.current = false;
    titleDraftRevisionRef.current += 1;
    descDraftRevisionRef.current += 1;
    setTitleDraft("");
    setDescDraft("");
    setConflict(null);
    setConflictMessage(null);
    setConflictBusy(false);
    setTitleEditing(false);
    setPropertiesVisible(false);
    setMoveTargetId(null);
    setClipIngestVisible(false);
  }, [authIdentity, commitNode, nodeId]);
  useEffect(() => {
    titleEditingRef.current = titleEditing;
  }, [titleEditing]);
  useEffect(() => {
    propertiesVisibleRef.current = propertiesVisible;
  }, [propertiesVisible]);
  useFocusEffect(
    useCallback(() => {
      focusedRef.current = true;
      const generation = ++loadGeneration.current;
      void loadNode(generation);
      void revalidateNode(generation);
      return () => {
        focusedRef.current = false;
        if (loadGeneration.current === generation) loadGeneration.current += 1;
      };
    }, [loadNode, reloadToken, revalidateNode]),
  );

  const retryLoad = useCallback(() => {
    const generation = ++loadGeneration.current;
    nodeLoadRequestRef.current += 1;
    setLoadError(false);
    setLoading(true);
    void loadNode(generation);
    void revalidateNode(generation);
  }, [loadNode, revalidateNode]);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state !== "active") void flushBodies().catch(() => undefined);
      if (state === "active" && focusedRef.current) {
        void revalidateNode(loadGeneration.current);
      }
    });
    return () => subscription.remove();
  }, [flushBodies, revalidateNode]);
  useEffect(() => {
    if (!node || params.created !== "1" || initialFocusApplied.current) return;
    initialFocusApplied.current = true;
    setTitleEditing(true);
    setTimeout(() => titleInputRef.current?.focus(), 120);
  }, [node, params.created]);

  useEffect(() => {
    const entityId = typeof nodeId === "string" ? nodeId.trim() : "";
    if (!outboxResolutionAuthScope || !entityId) return;
    const cleanupState = { started: false };
    let outlineHandle: OutlineEditorHandle | null = null;
    const waitForScreenAndStoreIdle = async () => {
      while (pendingDocsOperationsRef.current.size > 0) {
        await Promise.allSettled([...pendingDocsOperationsRef.current]);
      }
      const storeIdle = await blockDrafts.waitForIdle();
      if (!storeIdle) return false;
      if (titleDirtyRef.current || descDirtyRef.current) return false;
      return true;
    };
    const hasOutlinePending = () => {
      try {
        return Boolean(outlineHandle?.hasPendingEdits());
      } catch {
        return true;
      }
    };
    const idleProbe = () => {
      if (!cleanupState.started) return Promise.resolve(false);
      return waitForDocsEditorIdle(
        waitForScreenAndStoreIdle,
        hasOutlinePending,
      );
    };
    const release = registerOutboxResolutionEditor(
      outboxResolutionAuthScope,
      "knowledge_nodes",
      entityId,
      { idleProbe },
    );
    return () => {
      cleanupState.started = true;
      outlineHandle = lastOutlineHandleRef.current;
      queueMicrotask(() => {
        void idleProbe().then((idle) => {
          if (idle) release();
        }).catch(() => {
          // Keep the guard registered when an idle probe cannot complete.
        });
      });
    };
  }, [blockDrafts, nodeId, outboxResolutionAuthScope]);

  const performSaveTitle = useCallback(async () => {
    if (!node) return;
    const requestAuthIdentity = authIdentityRef.current;
    if (nodeAuthIdentityRef.current !== requestAuthIdentity) return;
    if (protectedProjectNode || nodeReadOnly) {
      // Project metadata is the title authority.  The mobile page does not
      // own a project-name cache, so restore the last server-provided title
      // and never enqueue a generic canonical-root rename/clear.
      titleDraftRef.current = node.title ?? "";
      titleDirtyRef.current = false;
      setTitleDraft(titleDraftRef.current);
      return;
    }
    const next = titleDraftRef.current.replace(/[\r\n]+/g, " ").slice(0, 500);
    if (next !== titleDraftRef.current) {
      titleDraftRef.current = next;
      setTitleDraft(next);
    }
    if (next === (node.title ?? "")) {
      titleDirtyRef.current = false;
      titleDraftRevisionRef.current += 1;
      return;
    }
    titleSaveCountRef.current += 1;
    try {
      const authAtSave = getTokenSnapshot();
      const updated = await serializeDocsNodeEdit(nodeId, async () => docsRepo.updateNode(nodeId, { title: next }, {
        auth: await authAtSave, workspaceId: node.workspace_id, projectId: node.project_id,
      }));
      if (
        activeNodeIdRef.current === nodeId
        && authIdentityRef.current === requestAuthIdentity
        && nodeAuthIdentityRef.current === requestAuthIdentity
      ) {
        commitNode(updated, requestAuthIdentity);
        if (titleDraftRef.current === next) {
          titleDirtyRef.current = false;
          titleDraftRevisionRef.current += 1;
        }
      }
    } finally {
      titleSaveCountRef.current -= 1;
    }
  }, [commitNode, node, nodeId, nodeReadOnly, protectedProjectNode]);

  const saveTitle = useCallback(
    () => trackDocsOperation(() => performSaveTitle()),
    [performSaveTitle, trackDocsOperation],
  );

  const submitTitle = useCallback(async () => {
    const requestAuthIdentity = authIdentityRef.current;
    await saveTitle();
    if (authIdentityRef.current !== requestAuthIdentity) return;
    setTitleEditing(false);
    await outlineRef.current?.focusFirstOrCreate();
  }, [saveTitle]);

  const performSaveDescription = useCallback(async () => {
    if (!node || nodeReadOnly) return;
    const requestAuthIdentity = authIdentityRef.current;
    if (nodeAuthIdentityRef.current !== requestAuthIdentity) return;
    const next = descDraftRef.current;
    if (next === (node.description ?? "")) {
      descDirtyRef.current = false;
      descDraftRevisionRef.current += 1;
      return;
    }
    descSaveCountRef.current += 1;
    try {
      const authAtSave = getTokenSnapshot();
      const updated = await serializeDocsNodeEdit(nodeId, async () => docsRepo.updateNode(nodeId, { description: next }, {
        auth: await authAtSave, workspaceId: node.workspace_id, projectId: node.project_id,
      }));
      if (
        activeNodeIdRef.current === nodeId
        && authIdentityRef.current === requestAuthIdentity
        && nodeAuthIdentityRef.current === requestAuthIdentity
      ) {
        commitNode(updated, requestAuthIdentity);
        if (descDraftRef.current === next) {
          descDirtyRef.current = false;
          descDraftRevisionRef.current += 1;
        }
      }
    } finally {
      descSaveCountRef.current -= 1;
    }
  }, [commitNode, node, nodeId, nodeReadOnly]);

  const saveDescription = useCallback(
    () => trackDocsOperation(() => performSaveDescription()),
    [performSaveDescription, trackDocsOperation],
  );

  const resolveServerConflict = useCallback(async () => {
    const reference = conflict;
    const requestAuthIdentity = authIdentityRef.current;
    if (!reference || !node || protectedProjectNode) return;
    if (nodeAuthIdentityRef.current !== requestAuthIdentity) return;
    if (node.read_only || node.access === "read" || !reference.docsScopeKey) {
      setConflictMessage(conflictReasonMessage("node_not_writable"));
      return;
    }
    setConflictBusy(true);
    setConflictMessage(null);
    try {
      if (typeof adoptDocsNodeServerConflict !== "function") {
        throw new Error("競合解決機能を利用できません");
      }
      const result = await trackDocsOperation(() => adoptDocsNodeServerConflict(reference));
      if (
        authIdentityRef.current !== requestAuthIdentity
        || nodeAuthIdentityRef.current !== requestAuthIdentity
      ) return;
      if (!result.ok) {
        setConflictMessage(conflictReasonMessage(result.reason));
        return;
      }
      setConflict(null);
      setConflictMessage(null);
      const generation = loadGeneration.current;
      await loadNode(generation, true);
      await outlineRef.current?.reloadFromRepository();
    } catch (error) {
      setConflictMessage(
        error instanceof Error && error.message
          ? error.message
          : conflictReasonMessage(),
      );
    } finally {
      setConflictBusy(false);
    }
  }, [conflict, loadNode, node, nodeId, protectedProjectNode, trackDocsOperation]);

  const reapplyDeviceConflict = useCallback(async () => {
    const reference = conflict;
    const requestAuthIdentity = authIdentityRef.current;
    if (!reference || !node || protectedProjectNode) return;
    if (nodeAuthIdentityRef.current !== requestAuthIdentity) return;
    if (node.read_only || node.access === "read" || !reference.docsScopeKey) {
      setConflictMessage(conflictReasonMessage("node_not_writable"));
      return;
    }
    setConflictBusy(true);
    setConflictMessage(null);
    try {
      if (typeof rebaseOutboxConflict !== "function") {
        throw new Error("競合解決機能を利用できません");
      }
      const result = await trackDocsOperation(() => rebaseOutboxConflict(reference));
      if (
        authIdentityRef.current !== requestAuthIdentity
        || nodeAuthIdentityRef.current !== requestAuthIdentity
      ) return;
      if (!result.ok) {
        setConflictMessage(conflictReasonMessage(result.reason));
        return;
      }
      // The local node/payload remains untouched.  Only the outbox base and
      // conflict marker changed, so the next sync can safely push the device
      // edit against the accepted server version.
      setConflict(null);
      setConflictMessage(null);
      const generation = loadGeneration.current;
      await loadNode(generation, true);
    } catch (error) {
      setConflictMessage(
        error instanceof Error && error.message
          ? error.message
          : conflictReasonMessage(),
      );
    } finally {
      setConflictBusy(false);
    }
  }, [conflict, loadNode, node, protectedProjectNode, trackDocsOperation]);

  const archiveSelf = useCallback(async () => {
    const requestAuthIdentity = authIdentityRef.current;
    if (nodeAuthIdentityRef.current !== requestAuthIdentity) return;
    if (nodeReadOnly) return;
    await flushBodies();
    if (authIdentityRef.current !== requestAuthIdentity) return;
    await trackDocsOperation(() => docsRepo.archiveNode(nodeId));
    if (
      authIdentityRef.current !== requestAuthIdentity
      || nodeAuthIdentityRef.current !== requestAuthIdentity
    ) return;
    goBackOrReplace(router, "/(tabs)/docs");
  }, [flushBodies, nodeId, nodeReadOnly, router, trackDocsOperation]);

  const handleMoveConfirm = useCallback(
    async (targetId: string, leaveReference: boolean) => {
      const source = moveTargetId;
      const requestAuthIdentity = authIdentityRef.current;
      setMoveTargetId(null);
      if (
        !source
        || nodeReadOnly
        || nodeAuthIdentityRef.current !== requestAuthIdentity
      ) return;
      await trackDocsOperation(() => docsRepo.moveNode(source, targetId, undefined, leaveReference));
      if (
        authIdentityRef.current !== requestAuthIdentity
        || nodeAuthIdentityRef.current !== requestAuthIdentity
      ) return;
      bump();
    },
    [bump, moveTargetId, nodeReadOnly, trackDocsOperation],
  );

  const openNode = useCallback((childId: string) => {
    if (nodeAuthIdentityRef.current !== authIdentityRef.current) return;
    router.push(`/(tabs)/docs/${childId}`);
  }, [router]);

  const requestMove = useCallback((childId: string) => {
    if (nodeAuthIdentityRef.current !== authIdentityRef.current) return;
    setMoveTargetId(childId);
  }, []);

  const flushBeforeNavigation = useCallback(async () => {
    await flushBodies();
    if (titleDirtyRef.current) await saveTitle();
    if (descDirtyRef.current) await saveDescription();
  }, [flushBodies, saveDescription, saveTitle]);
  const navigationSaveRef = useRef(false);
  // Native-stack requires usePreventRemove; a raw beforeRemove listener does
  // not reliably stop a native back gesture. Dispatch the original action after saving.
  // Arm before the first keystroke. Input and Back can share a React batch,
  // before either child's pending notification has caused a parent render.
  usePreventRemove(Boolean(node) || bodyPending || outlineBodyPending, ({ data }) => {
    if (navigationSaveRef.current) return;
    navigationSaveRef.current = true;
    const identity = authIdentityRef.current;
    void flushBeforeNavigation().then(() => {
      if (authIdentityRef.current !== identity) return;
      navigation.dispatch(data.action);
    }).catch((error) => {
      Alert.alert("Docs", error instanceof Error ? error.message : "本文の保存に失敗しました。入力内容を保持しています。");
    }).finally(() => { navigationSaveRef.current = false; });
  });

  if (!screenLoading && loadError && !node) {
    return (
      <View style={styles.container}>
        <ScreenHeader title="Docs" compact onBack={() => goBackOrReplace(router, "/(tabs)/docs")} />
        <View testID="docs-load-error" style={[styles.center, styles.loadErrorCenter]}>
          <Text style={styles.errorText}>{DOCS_LOAD_ERROR_MESSAGE}</Text>
          <Button mode="contained" onPress={retryLoad}>再試行</Button>
        </View>
      </View>
    );
  }

  if (!screenLoading && !node) {
    return (
      <View style={styles.container}>
        <ScreenHeader title="Docs" compact onBack={() => goBackOrReplace(router, "/(tabs)/docs")} />
        <View style={styles.center}><Text style={styles.emptyText}>ノードが見つかりません</Text></View>
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <ScreenHeader
        compact
        title="Docs"
        subtitle={node?.archived_at ? "アーカイブ済み" : undefined}
        onBack={() => goBackOrReplace(router, "/(tabs)/docs")}
        right={
          <>
            <IconButton
              icon="tray-arrow-down"
              size={22}
              iconColor="#a6adc8"
              accessibilityLabel="クリップ取り込み"
              style={styles.headerAction}
              onPress={() => setClipIngestVisible(true)}
            />
            <IconButton
              icon="tune-variant"
              size={22}
              iconColor="#cdd6f4"
              accessibilityLabel="プロパティ"
              style={styles.headerAction}
              disabled={!node || nodeReadOnly}
              onPress={() => setPropertiesVisible(true)}
            />
          </>
        }
      />
      <OutlineEditor
        key={`${nodeId}:${authIdentity}`}
        ref={setOutlineHandle}
        rootNodeId={nodeId}
        showArchived={showArchived}
        reloadToken={reloadToken}
        onOpen={openNode}
        onMoveRequest={requestMove}
        onChanged={bump}
        onBodyPendingChange={setOutlineBodyPending}
        readOnly={nodeReadOnly}
        listHeader={
          <Surface style={styles.editorHeader} elevation={0}>
            {loadError ? (
              <View testID="docs-load-error" style={styles.loadErrorBanner}>
                <Text style={styles.errorText}>{DOCS_LOAD_ERROR_MESSAGE}</Text>
                <Button compact mode="outlined" onPress={retryLoad}>再試行</Button>
              </View>
            ) : null}
            {screenLoading ? (
              <View style={styles.titleLoading}>
                <ActivityIndicator size="small" color="#7c3aed" />
              </View>
            ) : (
              titleEditing && !protectedProjectNode && !nodeReadOnly ? (
                <NativeTextInput
                  ref={(input: { focus: () => void } | null) => {
                    titleInputRef.current = input;
                  }}
                  value={titleDraft}
                  onChangeText={(value) => {
                    const next = value.replace(/[\r\n]+/g, " ").slice(0, 500);
                    titleDraftRef.current = next;
                    titleDirtyRef.current = true;
                    titleDraftRevisionRef.current += 1;
                    setTitleDraft(next);
                  }}
                  onBlur={() => {
                    void saveTitle();
                    setTitleEditing(false);
                  }}
                  onSubmitEditing={() => void submitTitle()}
                  submitBehavior="submit"
                  multiline
                  scrollEnabled={false}
                  rejectResponderTermination={false}
                  placeholder="タイトル"
                  placeholderTextColor="#6c7086"
                  selectionColor="#89b4fa"
                  maxLength={500}
                  style={styles.titleInput}
                  underlineColorAndroid="transparent"
                />
              ) : (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="ページタイトルを編集"
                  style={styles.titleDisplay}
                  onPress={() => {
                    if (protectedProjectNode || nodeReadOnly) return;
                    setTitleEditing(true);
                    requestAnimationFrame(() => titleInputRef.current?.focus());
                  }}
                >
                  <Text
                    style={
                      titleDraft ? styles.titleDisplayText : styles.titlePlaceholder
                    }
                  >
                    {titleDraft || "タイトル"}
                  </Text>
                </Pressable>
              )
            )}
            {visibleLocalSyncWarning ? (
              <Text testID="docs-local-sync-warning" style={styles.conflictWarning}>
                {visibleLocalSyncWarning}
              </Text>
            ) : null}
            {visibleConflict ? (
              <Surface
                testID="docs-sync-conflict"
                style={styles.conflictCard}
                elevation={0}
              >
                <Text style={styles.conflictTitle}>同期競合</Text>
                <Text style={styles.conflictDescription}>
                  端末編集とサーバー編集が同じ項目で競合しています。どちらを残すか選択してください。
                </Text>
                {conflictFields.map((field) => {
                  const values = conflictFieldValue(
                    field,
                    conflictLocalPayload,
                    node,
                    conflictServerPayload,
                  );
                  if (
                    values.server === undefined
                    && values.local === undefined
                  ) return null;
                  const labels: Record<ConflictField, string> = {
                    title: "タイトル",
                    description: "概要",
                    body_text: "本文",
                    body_json: "本文データ",
                  };
                  return (
                    <View key={field} style={styles.conflictValueRow}>
                      <Text style={styles.conflictFieldLabel}>{labels[field]}</Text>
                      <Text style={styles.conflictValue}>
                        端末: {formatConflictValue(values.local)}
                      </Text>
                      <Text style={styles.conflictValue}>
                        サーバー: {formatConflictValue(values.server)}
                      </Text>
                    </View>
                  );
                })}
                <View style={styles.conflictActions}>
                  <Button
                    compact
                    mode="outlined"
                    textColor="#89b4fa"
                    disabled={!conflictWritable || conflictBusy}
                    loading={conflictBusy}
                    onPress={() => void resolveServerConflict()}
                  >
                    サーバー版を採用
                  </Button>
                  <Button
                    compact
                    mode="contained"
                    buttonColor="#7c3aed"
                    disabled={!conflictWritable || conflictBusy}
                    loading={conflictBusy}
                    onPress={() => void reapplyDeviceConflict()}
                  >
                    端末編集を再適用
                  </Button>
                </View>
                {!conflictWritable ? (
                  <Text style={styles.conflictWarning}>
                    権限またはDocsスコープが変わったため、解決操作を無効にしています。
                  </Text>
                ) : null}
                {conflictMessage ? (
                  <Text style={styles.conflictWarning}>{conflictMessage}</Text>
                ) : null}
              </Surface>
            ) : null}
            {visibleTags.length > 0 ? (
              <Text style={styles.tagSummary} numberOfLines={1}>
                {visibleTags.map((tag) => `#${tag.name}`).join("  ")}
              </Text>
            ) : null}
            {node ? (
              <DocBlockEditor
                node={node}
                testIdPrefix="docs-page-block"
                draftSession={blockDrafts.get(node)}
                onSaved={(updated) => {
                  if (nodeAuthIdentityRef.current === authIdentityRef.current && activeNodeIdRef.current === updated.id) {
                    commitNode(updated);
                  }
                }}
                readOnly={nodeReadOnly || protectedProjectNode}
              />
            ) : null}
          </Surface>
        }
      />

      <Portal>
        <Dialog visible={propertiesVisible} onDismiss={() => setPropertiesVisible(false)} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>プロパティ</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.propertiesContent} keyboardShouldPersistTaps="handled">
              <Text style={styles.sectionLabel}>タグ</Text>
              <TagPicker
                nodeId={nodeId}
                tags={visibleTags}
                onChanged={bump}
                readOnly={protectedProjectNode || nodeReadOnly}
              />
              <Text style={styles.sectionLabel}>フィールド</Text>
              {protectedProjectNode || nodeReadOnly ? (
                <Text style={styles.readOnlyNote}>
                  {nodeReadOnly && !protectedProjectNode
                    ? "端末のDocsスコープ投影が未確定のため編集を無効にしています。同期後に再確認してください。"
                    : "Project正本のフィールドは専用画面で管理されます。"}
                </Text>
              ) : (
                <FieldEditor nodeId={nodeId} reloadToken={reloadToken} />
              )}
              <Text style={styles.sectionLabel}>概要</Text>
              <TextInput
                value={descDraft}
                onChangeText={(value) => {
                  if (nodeReadOnly) return;
                  descDraftRef.current = value;
                  descDirtyRef.current = true;
                  descDraftRevisionRef.current += 1;
                  setDescDraft(value);
                }}
                onBlur={() => void saveDescription()}
                editable={!nodeReadOnly}
                mode="outlined"
                multiline
                numberOfLines={4}
                placeholder="概要（任意）"
                style={styles.descInput}
              />
              <DocsTaskBinding
                nodeId={nodeId}
                projectId={node?.project_id}
                readOnly={Boolean(
                  nodeReadOnly,
                )}
              />
              {visibleBacklinks.length > 0 ? (
                <>
                  <Text style={styles.sectionLabel}>バックリンク</Text>
                  {visibleBacklinks.map((link) => (
                    <List.Item
                      key={link.id}
                      title={link.title || "無題"}
                      titleStyle={styles.backlinkTitle}
                      left={(props) => <List.Icon {...props} icon="link-variant" color="#89b4fa" />}
                      onPress={() => {
                        setPropertiesVisible(false);
                        router.push(`/(tabs)/docs/${link.id}`);
                      }}
                    />
                  ))}
                </>
              ) : null}
              {visibleOutgoingReferences.length > 0 ? (
                <>
                  <Text style={styles.sectionLabel}>参照先</Text>
                  {visibleOutgoingReferences.map((link) => (
                    <List.Item
                      key={link.id}
                      title={link.title || "無題"}
                      titleStyle={styles.backlinkTitle}
                      left={(props) => <List.Icon {...props} icon="link-variant-plus" color="#a6e3a1" />}
                      onPress={() => {
                        setPropertiesVisible(false);
                        router.push(`/(tabs)/docs/${link.id}`);
                      }}
                    />
                  ))}
                </>
              ) : null}
              <Button
                mode="outlined"
                icon={showArchived ? "eye-off-outline" : "eye-outline"}
                onPress={() => setShowArchived((value) => !value)}
              >
                {showArchived ? "アーカイブ済みを隠す" : "アーカイブ済みを表示"}
              </Button>
              {!protectedProjectNode && !nodeReadOnly ? (
                <Button
                  mode="outlined"
                  icon="folder-move-outline"
                  onPress={() => {
                    setPropertiesVisible(false);
                    setMoveTargetId(nodeId);
                  }}
                >
                  このページを移動
                </Button>
              ) : null}
              {!protectedProjectNode && !nodeReadOnly ? <Button mode="outlined" icon="archive-outline" textColor="#f38ba8" onPress={() => void archiveSelf()}>
                このページをアーカイブ
              </Button> : null}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setPropertiesVisible(false)}>閉じる</Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>

      <MovePicker
        visible={moveTargetId !== null}
        currentNodeId={moveTargetId ?? nodeId}
        onDismiss={() => setMoveTargetId(null)}
        onConfirm={(targetId, leaveReference) => void handleMoveConfirm(targetId, leaveReference)}
      />
      <ClipIngestDialog
        visible={clipIngestVisible}
        onDismiss={() => setClipIngestVisible(false)}
        targetNodeId={nodeId}
        projectId={node?.project_id ?? null}
        readOnly={nodeReadOnly}
        onOpenNode={(openNodeId) => {
          if (nodeAuthIdentityRef.current !== authIdentityRef.current) return;
          setClipIngestVisible(false);
          router.push(`/(tabs)/docs/${openNodeId}`);
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  loadErrorCenter: { gap: 12, paddingHorizontal: 24 },
  loadErrorBanner: {
    backgroundColor: "#2a1f35",
    borderColor: "#c084fc",
    borderWidth: 1,
    borderRadius: 8,
    marginHorizontal: 10,
    marginBottom: 8,
    padding: 10,
    gap: 8,
  },
  errorText: { color: "#f9e2af", fontSize: 14, lineHeight: 20 },
  emptyText: { color: "#a6adc8", fontSize: 16 },
  editorHeader: { backgroundColor: "#11111b", paddingHorizontal: 2, paddingTop: 4 },
  headerAction: { margin: 0, width: 44, height: 44, flexShrink: 0 },
  titleLoading: { height: 42, alignItems: "flex-start", justifyContent: "center", paddingLeft: 8 },
  titleInput: {
    minHeight: 42,
    backgroundColor: "transparent",
    color: "#cdd6f4",
    fontSize: 21,
    fontWeight: "700",
    lineHeight: 28,
    paddingHorizontal: 6,
    paddingVertical: 7,
    includeFontPadding: false,
    textAlignVertical: "top",
  },
  titleDisplay: {
    minHeight: 42,
    justifyContent: "center",
    paddingHorizontal: 6,
    paddingVertical: 7,
  },
  titleDisplayText: { color: "#cdd6f4", fontSize: 21, fontWeight: "700", lineHeight: 28, includeFontPadding: false },
  titlePlaceholder: { color: "#6c7086", fontSize: 21, fontWeight: "700", lineHeight: 28, includeFontPadding: false },
  tagSummary: { color: "#a6adc8", fontSize: 12, paddingHorizontal: 6, paddingBottom: 5 },
  conflictCard: {
    backgroundColor: "#2a1f35",
    borderColor: "#c084fc",
    borderWidth: 1,
    borderRadius: 8,
    marginHorizontal: 10,
    marginBottom: 10,
    padding: 10,
    gap: 6,
  },
  conflictTitle: { color: "#f9e2af", fontSize: 16, fontWeight: "700" },
  conflictDescription: { color: "#cdd6f4", fontSize: 12, lineHeight: 18 },
  conflictValueRow: {
    borderTopColor: "#45405a",
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingTop: 5,
    gap: 2,
  },
  conflictFieldLabel: { color: "#c084fc", fontSize: 12, fontWeight: "700" },
  conflictValue: { color: "#a6adc8", fontSize: 12, lineHeight: 17 },
  conflictActions: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: 4 },
  conflictWarning: { color: "#f38ba8", fontSize: 12, lineHeight: 17 },
  dialog: { backgroundColor: "#1e1e2e", maxHeight: "88%" },
  dialogTitle: { color: "#cdd6f4" },
  dialogScrollArea: { maxHeight: 620, borderColor: "#313244" },
  propertiesContent: { paddingVertical: 8, gap: 12 },
  sectionLabel: { color: "#c084fc", fontSize: 12, fontWeight: "700", marginTop: 4 },
  readOnlyNote: { color: "#a6adc8", fontSize: 12, lineHeight: 18 },
  descInput: { backgroundColor: "#181825" },
  backlinkTitle: { color: "#cdd6f4", fontSize: 14 },
});

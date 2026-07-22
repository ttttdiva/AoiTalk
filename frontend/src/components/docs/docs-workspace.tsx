"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import {
  createPortal,
  flushSync,
} from "react-dom";
import {
  Archive,
  CalendarDays,
  Columns2,
  ListFilter,
  Plus,
  Settings2,
  Tags,
} from "lucide-react";
import {
  toast,
} from "sonner";
import {
  Button,
} from "@/components/ui/button";
import {
  Input,
} from "@/components/ui/input";
import {
  Skeleton,
} from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  useProject,
} from "@/contexts/project-context";
import {
  invertHistoryEntry,
  midpointSortOrder,
  sortNodesByPosition,
  type BlockHistoryEntry,
  type DocsBlockSnapshot,
} from "@/lib/docs-block-model";
import {
  DocsSaveQueue,
} from "@/lib/docs-save-queue";
import {
  cn,
} from "@/lib/utils";
import {
  TaskDetailModal,
} from "@/components/tasks/task-detail-modal";
import {
  listRemoteServers,
  getRemoteDocsTree,
  type RemoteServerProfile,
} from "@/lib/remote-servers";
import {
  useUserSettings,
} from "@/contexts/user-settings-context";
import {
  getRemoteServerConnectionEnabled,
} from "@/lib/user-settings";
import {
  DocsEditorProvider,
  OutlineBlockEditor,
  type BlockCreateInput,
  type BlockMoveInput,
  type DocsEditorContextValue,
  type OutlineEditorRow,
} from "./outline/outline-editor";
import type {
  DocsField,
  DocsFieldValue,
  DocsAttachment,
  DocsNode,
  ReferencesState,
  DocsState,
  DocsSupertag,
  DocsSavedView,
  DocsAiSuggestion,
} from "./types";
import {
  EMPTY_REFERENCES,
  EMPTY_STATE,
} from "./types";
import {
  apiFetch as rawApiFetch,
  buildBreadcrumb,
  docsFieldType,
  fieldDraftToPayload,
  fieldValueToDraft,
  projectsFromContext,
} from "./docs-utils";
import {
  SIDEBAR_COLLAPSED_KEY,
  SUPERTAGS_OVERVIEW_ID,
  buildOutlineChildren,
  fieldsForNode,
  getDocsSidebarSlotSnapshot,
  mergeById,
  nodeDateDelta,
  nodeText,
  outlineRows,
  patchFromSnapshot,
  readConfigRecord,
  safeNodeDisplayProps,
  snapshotDocsNode,
  subscribeDocsSidebarSlot,
  tagIdsFromRelatedConfig,
  tagSetByNodeId,
  titleTagNames,
  titleWithoutTagTokens,
  valueByNodeField,
  writeCollapsed,
  type DocsAiCommand,
  type DocsAiCommandResult,
  type DocsAiPreview,
  type DocsSupertagTool,
  type DocsTaskBinding,
  type LoadOptions,
  type NodePatch,
  type SearchSort,
  type SearchView,
  type SidebarContextMenuState,
  type TodayResponse,
} from "./docs-workspace-shared";
import {
  AliasEditorDialog,
  DocsAiPreviewDialog,
  DocsCommandPalette,
} from "./docs-dialogs";
import {
  PageTitleEditor,
  TaskBindingButton,
  ZoomReferences,
} from "./docs-page-editor";
import {
  SearchNodeResults,
} from "./docs-search-panels";
import {
  DocsSidebarContextMenu,
  DocsSidebarNode,
  SearchNodesMainView,
  SidebarButton,
  TrashMainView,
} from "./docs-sidebar-views";
import {
  SupertagPage,
} from "./docs-supertag-page";
import {
  RightPanel,
} from "./docs-right-panel";
import {
  useDocsCommandPalette,
} from "./hooks/use-docs-command-palette";
import {
  useDocsCollapse,
} from "./hooks/use-docs-collapse";
import {
  useDocsSelection,
} from "./hooks/use-docs-selection";
import {
  useDocsMentionUsers,
} from "./hooks/use-docs-mention-users";

type DocsNeighborhoodResponse = Partial<DocsState> & {
  focus_node_id?: string;
  root_page_id?: string;
  has_children_ids?: string[];
  loaded_children_parent_ids?: string[];
  details_loaded_ids?: string[];
  has_details_ids?: string[];
  children_next_cursor_by_parent?: Record<string, string | null>;
  child_count_by_parent?: Record<string, number>;
  next_cursor?: string | null;
};

function mergeByKey<T>(current: T[], next: T[], keyFor: (item: T) => string) {
  const merged = new Map(current.map((item) => [keyFor(item), item]));
  for (const item of next) merged.set(keyFor(item), item);
  return Array.from(merged.values());
}

export function mergeLoadedDocsState(current: DocsState, incoming: DocsNeighborhoodResponse): DocsState {
  const currentDetails = new Set(current.details_loaded_ids ?? []);
  const incomingDetails = new Set(incoming.details_loaded_ids ?? []);
  const nodes = (incoming.nodes ?? []).reduce((items, next) => {
    const existing = items.find((item) => item.id === next.id);
    const mergedNode = existing && currentDetails.has(next.id) && !incomingDetails.has(next.id)
      ? {
          ...existing,
          ...next,
          body_json: { ...existing.body_json, ...next.body_json },
          body_text: existing.body_text,
        }
      : next;
    return mergeById(items, mergedNode);
  }, current.nodes);
  return {
    ...current,
    nodes,
    supertags: (incoming.supertags ?? []).reduce((items, next) => mergeById(items, next), current.supertags),
    fields: (incoming.fields ?? []).reduce((items, next) => mergeById(items, next), current.fields),
    views: (incoming.views ?? []).reduce((items, next) => mergeById(items, next), current.views),
    projects: (incoming.projects ?? []).reduce((items, next) => mergeById(items, next), current.projects),
    ai_suggestions: (incoming.ai_suggestions ?? []).reduce((items, next) => mergeById(items, next), current.ai_suggestions),
    node_supertags: mergeByKey(
      current.node_supertags,
      incoming.node_supertags ?? [],
      (item) => `${item.node_id}:${item.supertag_id}`,
    ),
    supertag_fields: mergeByKey(
      current.supertag_fields,
      incoming.supertag_fields ?? [],
      (item) => `${item.supertag_id}:${item.field_id}`,
    ),
    placements: mergeByKey(
      current.placements,
      incoming.placements ?? [],
      (item) => item.id,
    ),
    field_values: mergeByKey(
      current.field_values,
      incoming.field_values ?? [],
      (item) => `${item.node_id}:${item.field_id}`,
    ),
    attachments: mergeByKey(
      current.attachments,
      incoming.attachments ?? [],
      (item) => item.id,
    ),
    has_children_ids: Array.from(new Set([
      ...(current.has_children_ids ?? []),
      ...(incoming.has_children_ids ?? []),
    ])),
    loaded_children_parent_ids: Array.from(new Set([
      ...(current.loaded_children_parent_ids ?? []),
      ...(incoming.loaded_children_parent_ids ?? []),
    ])),
    details_loaded_ids: Array.from(new Set([
      ...(current.details_loaded_ids ?? []),
      ...(incoming.details_loaded_ids ?? []),
    ])),
    has_details_ids: Array.from(new Set([
      ...(current.has_details_ids ?? []),
      ...(incoming.has_details_ids ?? []),
    ])),
    children_next_cursor_by_parent: {
      ...(current.children_next_cursor_by_parent ?? {}),
      ...(incoming.children_next_cursor_by_parent ?? {}),
    },
    child_count_by_parent: {
      ...(current.child_count_by_parent ?? {}),
      ...(incoming.child_count_by_parent ?? {}),
    },
  };
}

function evictDocsSubtrees(state: DocsState, parentIds: string[]) {
  if (parentIds.length === 0) return state;
  const evictRoots = new Set(parentIds);
  const removeIds = new Set<string>();
  let changed = true;
  while (changed) {
    changed = false;
    for (const node of state.nodes) {
      if (!node.parent_id || removeIds.has(node.id)) continue;
      if (evictRoots.has(node.parent_id) || removeIds.has(node.parent_id)) {
        removeIds.add(node.id);
        changed = true;
      }
    }
    for (const placement of state.placements) {
      if (removeIds.has(placement.node_id)) continue;
      if (evictRoots.has(placement.parent_node_id) || removeIds.has(placement.parent_node_id)) {
        removeIds.add(placement.node_id);
        changed = true;
      }
    }
  }
  const externallyPlacedIds = new Set(
    state.placements
      .filter((placement) => !removeIds.has(placement.parent_node_id) && !evictRoots.has(placement.parent_node_id))
      .map((placement) => placement.node_id),
  );
  const externallyReachableIds = new Set(externallyPlacedIds);
  changed = true;
  while (changed) {
    changed = false;
    for (const node of state.nodes) {
      if (node.parent_id && externallyReachableIds.has(node.parent_id) && !externallyReachableIds.has(node.id)) {
        externallyReachableIds.add(node.id);
        changed = true;
      }
    }
    for (const placement of state.placements) {
      if (externallyReachableIds.has(placement.parent_node_id) && !externallyReachableIds.has(placement.node_id)) {
        externallyReachableIds.add(placement.node_id);
        changed = true;
      }
    }
  }
  for (const nodeId of externallyReachableIds) removeIds.delete(nodeId);
  const retained = <T extends string>(ids: T[] | undefined) => (ids ?? []).filter((id) => !removeIds.has(id) && !evictRoots.has(id));
  return {
    ...state,
    nodes: state.nodes.filter((node) => !removeIds.has(node.id)),
    node_supertags: state.node_supertags.filter((item) => !removeIds.has(item.node_id)),
    field_values: state.field_values.filter((item) => !removeIds.has(item.node_id)),
    attachments: state.attachments.filter((item) => !removeIds.has(item.node_id)),
    placements: state.placements.filter((item) => !removeIds.has(item.node_id) && !removeIds.has(item.parent_node_id) && !evictRoots.has(item.parent_node_id)),
    loaded_children_parent_ids: retained(state.loaded_children_parent_ids),
    details_loaded_ids: retained(state.details_loaded_ids),
    has_children_ids: (state.has_children_ids ?? []).filter((id) => !removeIds.has(id)),
    has_details_ids: (state.has_details_ids ?? []).filter((id) => !removeIds.has(id)),
    children_next_cursor_by_parent: Object.fromEntries(
      Object.entries(state.children_next_cursor_by_parent ?? {}).filter(([parentId]) => !removeIds.has(parentId) && !evictRoots.has(parentId)),
    ),
    child_count_by_parent: Object.fromEntries(
      Object.entries(state.child_count_by_parent ?? {}).filter(([parentId]) => !removeIds.has(parentId) && !evictRoots.has(parentId)),
    ),
  };
}

export function DocsWorkspace({ initialNodeId }: { initialNodeId?: string | null }) {
  const { allProjects } = useProject();
  const { settings } = useUserSettings();
  const remoteConnectionEnabled = getRemoteServerConnectionEnabled(settings);
  const [docsSource, setDocsSource] = useState<"local" | string>("local");
  const [remoteProfiles, setRemoteProfiles] = useState<RemoteServerProfile[]>([]);
  const docsReadOnly = docsSource !== "local";
  const apiFetch = useCallback(<T,>(path: string, init?: RequestInit) => {
    if (docsReadOnly) {
      return Promise.reject(new Error("Enterprise Docsは読み取り専用です"));
    }
    return rawApiFetch<T>(path, init);
  }, [docsReadOnly]);
  const [state, setState] = useState<DocsState>(EMPTY_STATE);
  const [focusNodeId, setFocusNodeId] = useState<string | null>(initialNodeId ?? null);
  const [loading, setLoading] = useState(true);
  const { collapsed, setCollapsed, sidebarCollapsed, setSidebarCollapsed, collapsedRef } = useDocsCollapse();
  const { commandOpen, setCommandOpen, commandMode, setCommandMode, commandQuery, setCommandQuery, openCommand } = useDocsCommandPalette();
  const [rightPanel, setRightPanel] = useState<"related" | "tags">("related");
  const [propertiesOpen, setPropertiesOpen] = useState(false);
  const [propertiesTagId, setPropertiesTagId] = useState<string | null>(null);
  // ゴミ箱 / Search nodes はメイン領域にフルビューとして開く。tagPageId と排他。
  const [mainView, setMainView] = useState<"trash" | "search" | null>(null);
  const [splitNodeId, setSplitNodeId] = useState<string | null>(null);
  const [newTagName, setNewTagName] = useState("");
  const [tagPageId, setTagPageId] = useState<string | null>(null);
  const [focusRequestNodeId, setFocusRequestNodeId] = useState<string | null>(null);
  const {
    selectedNodeId,
    setSelectedNodeId,
    selectedNodeIds,
    setSelectedNodeIds,
    selectionAnchorNodeId,
    setSelectionAnchorNodeId,
    selectedNodeIdsRef,
    selectionAnchorNodeIdRef,
    preserveSelectionOnNextFocusRef,
    selectSingleNode,
    extendNodeSelection,
    selectRangeToNode,
    selectDomRangeById,
  } = useDocsSelection(setFocusRequestNodeId);
  const [pageReferences, setPageReferences] = useState<ReferencesState>(EMPTY_REFERENCES);
  const [pageReferencesLoading, setPageReferencesLoading] = useState(false);
  const [sidebarContextMenu, setSidebarContextMenu] = useState<SidebarContextMenuState | null>(null);
  const [dragSidebarNodeId, setDragSidebarNodeId] = useState<string | null>(null);
  const [aiPreview, setAiPreview] = useState<DocsAiPreview | null>(null);
  const [taskModalId, setTaskModalId] = useState<string | null>(null);
  const [aliasEditorNode, setAliasEditorNode] = useState<DocsNode | null>(null);
  // メンション候補ユーザー一覧は SWR で取得（マウント時に一度取得・失敗時は空・自動再取得なし）。
  const mentionUsers = useDocsMentionUsers(apiFetch);
  const [loadingNodeIds, setLoadingNodeIds] = useState<Set<string>>(() => new Set());
  const [taskBindingsByNodeId, setTaskBindingsByNodeId] = useState<Map<string, DocsTaskBinding | null>>(() => new Map());
  const undoStackRef = useRef<BlockHistoryEntry[]>([]);
  const redoStackRef = useRef<BlockHistoryEntry[]>([]);
    const applyingHistoryRef = useRef(false);
    const editorCommitInFlightRef = useRef<Promise<boolean> | null>(null);
    const latestEditorDraftRef = useRef(new Map<string, string>());
    const pendingCreateHistoryIdsRef = useRef(new Set<string>());
  const taskBindingInFlightRef = useRef<Set<string>>(new Set());
  const sidebarScrollNodeRef = useRef<string | null>(null);
  const previousInitialNodeIdRef = useRef(initialNodeId);
  const remoteProfilesRequestRef = useRef(0);
  const docsLoadGenerationRef = useRef(0);
  const nodeNeighborhoodInFlightRef = useRef(new Map<string, Promise<DocsNeighborhoodResponse>>());
  const nodeChildrenInFlightRef = useRef(new Map<string, { controller: AbortController; promise: Promise<DocsNeighborhoodResponse> }>());
    const nodeDetailsInFlightRef = useRef(new Map<string, { controller: AbortController; promise: Promise<DocsNeighborhoodResponse> }>());
    const nodeCreateInFlightRef = useRef(new Map<string, Promise<DocsNode>>());
  const expandNodeRef = useRef<(nodeId: string) => void>(() => {});
  const undoDocsOperationRef = useRef<() => Promise<void>>(async () => {});
  const redoDocsOperationRef = useRef<() => Promise<void>>(async () => {});
  const loadedParentAccessRef = useRef(new Map<string, number>());
  const focusNodeIdRef = useRef(focusNodeId);
  const nodeLoadCountRef = useRef(new Map<string, number>());
  const nodeTagIdsRef = useRef(new Map<string, string[]>());
  const tagMutationQueueRef = useRef(new Map<string, Promise<void>>());
  const docsSaveQueue = useMemo(() => new DocsSaveQueue<DocsNode>(), []);

  useEffect(() => {
    focusNodeIdRef.current = focusNodeId;
  }, [focusNodeId]);

  useEffect(() => () => {
    for (const entry of nodeChildrenInFlightRef.current.values()) entry.controller.abort();
    for (const entry of nodeDetailsInFlightRef.current.values()) entry.controller.abort();
  }, []);

  const markNodeLoading = useCallback((nodeId: string, loading: boolean) => {
    const count = nodeLoadCountRef.current.get(nodeId) ?? 0;
    const nextCount = Math.max(0, count + (loading ? 1 : -1));
    if (nextCount === 0) nodeLoadCountRef.current.delete(nodeId);
    else nodeLoadCountRef.current.set(nodeId, nextCount);
    setLoadingNodeIds((current) => {
      const next = new Set(current);
      if (nextCount > 0) next.add(nodeId);
      else next.delete(nodeId);
      return next;
    });
  }, []);

  const abortNodeLoads = useCallback((nodeId: string) => {
    for (const [requestKey, entry] of nodeChildrenInFlightRef.current.entries()) {
      if (requestKey.startsWith(`${nodeId}:`)) entry.controller.abort();
    }
    nodeDetailsInFlightRef.current.get(nodeId)?.controller.abort();
  }, []);

  useEffect(() => {
    const flushPendingSaves = () => {
      void docsSaveQueue.flush();
    };
    window.addEventListener("pagehide", flushPendingSaves);
    window.addEventListener("beforeunload", flushPendingSaves);
    return () => {
      window.removeEventListener("pagehide", flushPendingSaves);
      window.removeEventListener("beforeunload", flushPendingSaves);
    };
  }, [docsSaveQueue]);

  useEffect(() => {
    const requestId = ++remoteProfilesRequestRef.current;
    if (!remoteConnectionEnabled) {
      setRemoteProfiles([]);
      setDocsSource("local");
    } else {
      void listRemoteServers()
        .then((profiles) => {
          if (requestId !== remoteProfilesRequestRef.current || !remoteConnectionEnabled) return;
          setRemoteProfiles(profiles.filter((profile) => profile.enabled));
        })
        .catch(() => {
          if (requestId === remoteProfilesRequestRef.current && remoteConnectionEnabled) {
            setRemoteProfiles([]);
          }
        });
    }
    return () => {
      remoteProfilesRequestRef.current += 1;
    };
  }, [remoteConnectionEnabled]);

  const projects = projectsFromContext(state.projects, allProjects);
  const nodesById = useMemo(() => new Map(state.nodes.map((node) => [node.id, node])), [state.nodes]);
  const nodesByIdRef = useRef(nodesById);
  nodesByIdRef.current = nodesById;
  const childrenByParent = useMemo(() => buildOutlineChildren(state.nodes, state.placements), [state.nodes, state.placements]);
  // 折りたたみ中で子行が rows に無いノードでもシェブロンを表示するための子有無判定。
  const nodeHasChildren = useCallback(
    (nodeId: string) =>
      (childrenByParent.get(nodeId) ?? []).some((child) => !child.archived_at)
      || (state.has_children_ids ?? []).includes(nodeId),
    [childrenByParent, state.has_children_ids],
  );
  const tagById = useMemo(() => new Map(state.supertags.map((tag) => [tag.id, tag])), [state.supertags]);
  const nodeTags = useMemo(() => {
    const map = new Map<string, DocsSupertag[]>();
    for (const relation of state.node_supertags) {
      const tag = tagById.get(relation.supertag_id);
      if (!tag) continue;
      const next = map.get(relation.node_id) ?? [];
      next.push(tag);
      map.set(relation.node_id, next);
    }
    return map;
  }, [state.node_supertags, tagById]);
  useEffect(() => {
    // レンダー中に optimistic な希望状態を上書きすると、remove→add の間に
    // 古いrelationが復活する。通信中のnodeはmutation queueを唯一の根拠にする。
    for (const node of state.nodes) {
      if (tagMutationQueueRef.current.has(node.id)) continue;
      nodeTagIdsRef.current.set(node.id, (nodeTags.get(node.id) ?? []).map((tag) => tag.id));
    }
  }, [nodeTags, state.nodes]);
  const fieldsByTag = useMemo(() => {
    const fieldsById = new Map(state.fields.map((field) => [field.id, field]));
    const map = new Map<string, DocsField[]>();
    for (const relation of state.supertag_fields) {
      const field = fieldsById.get(relation.field_id);
      if (!field) continue;
      const next = map.get(relation.supertag_id) ?? [];
      // required は Supertag と Field の関連ごとの設定。共有 Field 本体の既定値を
      // 混ぜると、別 Supertag の必須設定が漏れるため relation を唯一の根拠にする。
      next.push({ ...field, sort_order: relation.sort_order, required: relation.required });
      map.set(relation.supertag_id, next);
    }
    for (const field of state.fields) {
      if (!field.supertag_id) continue;
      const next = map.get(field.supertag_id) ?? [];
      if (!next.some((item) => item.id === field.id)) next.push(field);
      map.set(field.supertag_id, next);
    }
    for (const [tagId, fields] of map.entries()) map.set(tagId, [...fields].sort((a, b) => a.sort_order - b.sort_order));
    return map;
  }, [state.fields, state.supertag_fields]);
  const loadedChildrenParentIds = useMemo(() => new Set(state.loaded_children_parent_ids ?? []), [state.loaded_children_parent_ids]);
  const detailsLoadedIds = useMemo(() => new Set(state.details_loaded_ids ?? []), [state.details_loaded_ids]);
  const hasDetailsIds = useMemo(() => new Set(state.has_details_ids ?? []), [state.has_details_ids]);
  const nodeHasDetails = useCallback((nodeId: string) => {
    // Field定義を持つだけでは行を太らせない。値や本文など実データがある時だけ
    // details API が has_details_ids を返し、シェブロンから遅延読込する。
    return nodesById.has(nodeId) && hasDetailsIds.has(nodeId);
  }, [hasDetailsIds, nodesById]);
  const nodeHasExpandableContent = useCallback(
    (nodeId: string) => nodeHasChildren(nodeId) || nodeHasDetails(nodeId),
    [nodeHasChildren, nodeHasDetails],
  );
  const nodeIsCollapsed = useCallback((nodeId: string) => {
    if (collapsed.has(nodeId)) return true;
    if (nodeHasChildren(nodeId) && !loadedChildrenParentIds.has(nodeId)) return true;
    if (nodeHasDetails(nodeId) && !detailsLoadedIds.has(nodeId)) return true;
    return false;
  }, [collapsed, detailsLoadedIds, loadedChildrenParentIds, nodeHasChildren, nodeHasDetails]);
  const fieldValuesByKey = useMemo(() => valueByNodeField(state.field_values), [state.field_values]);
  const fieldValuesByNodeId = useMemo(() => {
    const map = new Map<string, DocsFieldValue[]>();
    for (const value of state.field_values) {
      const values = map.get(value.node_id) ?? [];
      values.push(value);
      map.set(value.node_id, values);
    }
    return map;
  }, [state.field_values]);
  const attachmentsByNodeId = useMemo(() => {
    const map = new Map<string, DocsAttachment[]>();
    for (const attachment of state.attachments) {
      const items = map.get(attachment.node_id) ?? [];
      items.push(attachment);
      map.set(attachment.node_id, items);
    }
    return map;
  }, [state.attachments]);
  const tagSetByNode = useMemo(() => tagSetByNodeId(state.node_supertags), [state.node_supertags]);
  const selectedNodeIdSet = useMemo(() => new Set(selectedNodeIds), [selectedNodeIds]);

  const focusNode = focusNodeId ? nodesById.get(focusNodeId) ?? null : null;
  const focusNodeReferenceId = !tagPageId ? focusNode?.id ?? null : null;
  const selectedNode = selectedNodeId ? nodesById.get(selectedNodeId) ?? null : focusNode;
  const activeTagPage = tagPageId && tagPageId !== SUPERTAGS_OVERVIEW_ID ? tagById.get(tagPageId) ?? null : null;
  const roots = useMemo(() => sortNodesByPosition(state.nodes.filter((node) => !node.parent_id && !node.archived_at)), [state.nodes]);
  const archivedNodes = useMemo(() => state.nodes.filter((node) => !!node.archived_at).sort((a, b) => (b.archived_at ?? "").localeCompare(a.archived_at ?? "")), [state.nodes]);
  const currentRows = useMemo(() => (focusNode ? outlineRows(focusNode.id, childrenByParent, collapsed) : []), [childrenByParent, collapsed, focusNode]);
  const splitNode = splitNodeId ? nodesById.get(splitNodeId) ?? null : null;
  const splitRows = useMemo(() => (splitNode ? outlineRows(splitNode.id, childrenByParent, collapsed) : []), [childrenByParent, collapsed, splitNode]);
  const selectedNodes = useMemo(() => selectedNodeIds.map((nodeId) => nodesById.get(nodeId)).filter((node): node is DocsNode => Boolean(node)), [nodesById, selectedNodeIds]);
  const actionNodes = selectedNodes.length > 1 ? selectedNodes : selectedNode ? [selectedNode] : [];
  const sidebarContextNode = sidebarContextMenu ? nodesById.get(sidebarContextMenu.nodeId) ?? null : null;

  useEffect(() => {
    const visibleIds = Array.from(new Set([
      focusNode?.id ?? null,
      splitNode?.id ?? null,
      ...currentRows.map((row) => row.node.id),
      ...splitRows.map((row) => row.node.id),
    ].filter((id): id is string => Boolean(id))));
    const missing = visibleIds.filter((nodeId) => !taskBindingsByNodeId.has(nodeId) && !taskBindingInFlightRef.current.has(nodeId));
    if (missing.length === 0) return;
    for (const nodeId of missing) taskBindingInFlightRef.current.add(nodeId);
    apiFetch<{ bindings: Array<{ node_id: string; task: DocsTaskBinding | null }> }>("/api/docs/task-bindings", {
      method: "POST",
      body: JSON.stringify({ node_ids: missing }),
    })
      .then((data) => {
        const returned = new Set<string>();
        setTaskBindingsByNodeId((current) => {
          const next = new Map(current);
          for (const entry of data.bindings ?? []) {
            returned.add(entry.node_id);
            next.set(entry.node_id, entry.task);
          }
          for (const nodeId of missing) {
            if (!returned.has(nodeId)) next.set(nodeId, null);
          }
          return next;
        });
      })
      .catch(() => {
        setTaskBindingsByNodeId((current) => {
          const next = new Map(current);
          for (const nodeId of missing) next.set(nodeId, null);
          return next;
        });
      })
      .finally(() => {
        for (const nodeId of missing) taskBindingInFlightRef.current.delete(nodeId);
      });
  }, [currentRows, focusNode?.id, splitNode?.id, splitRows, taskBindingsByNodeId]);

  const relatedNodes = useMemo(() => {
    if (!selectedNode) return [];
    const selectedTagIds = tagSetByNode.get(selectedNode.id) ?? new Set<string>();
    if (selectedTagIds.size === 0) return [];
    const configuredTagIds = new Set<string>();
    for (const tagId of selectedTagIds) {
      const tag = tagById.get(tagId);
      if (!tag) continue;
      for (const relatedTagId of tagIdsFromRelatedConfig(tag)) configuredTagIds.add(relatedTagId);
    }
    if (configuredTagIds.size === 0) return [];
    const acceptedTags = configuredTagIds;
    return state.nodes
      .filter((node) => node.id !== selectedNode.id && !node.archived_at)
      .filter((node) => {
        const tags = tagSetByNode.get(node.id) ?? new Set<string>();
        return Array.from(acceptedTags).some((tagId) => tags.has(tagId));
      })
      .slice(0, 20);
  }, [selectedNode, state.nodes, tagById, tagSetByNode]);
  const commandMoveTargets = useMemo(() => {
    if (!selectedNode) return [];
    const query = commandQuery.trim().toLowerCase();
    return state.nodes
      .filter((node) => !selectedNodeIdSet.has(node.id) && !selectedNodeIdSet.has(node.parent_id ?? "") && !node.archived_at)
      .filter((node) => {
        let parentId = node.parent_id;
        while (parentId) {
          if (selectedNodeIdSet.has(parentId)) return false;
          parentId = nodesById.get(parentId)?.parent_id ?? null;
        }
        return true;
      })
      .filter((node) => !query || nodeText(node).toLowerCase().includes(query))
      .slice(0, 500);
  }, [commandQuery, nodesById, selectedNode, selectedNodeIdSet, state.nodes]);
  const commandFields = useMemo(() => selectedNode ? fieldsForNode(selectedNode, nodeTags, fieldsByTag) : [], [fieldsByTag, nodeTags, selectedNode]);
  // 選択ノードに付与された全Supertagの config_json.tools を集約し、command で重複排除する。
  const commandNodeTools = useMemo<DocsSupertagTool[]>(() => {
    if (!selectedNode) return [];
    const seen = new Set<string>();
    const tools: DocsSupertagTool[] = [];
    for (const tag of nodeTags.get(selectedNode.id) ?? []) {
      const rawTools = readConfigRecord(tag.config_json).tools;
      if (!Array.isArray(rawTools)) continue;
      for (const entry of rawTools) {
        const record = readConfigRecord(entry);
        const command = typeof record.command === "string" ? record.command : "";
        // 廃止済みのFW専用Supertagが既存DBに残っていてもコマンドを再公開しない。
        if (!command || command.startsWith("fw_") || seen.has(command)) continue;
        const label = typeof record.label === "string" && record.label ? record.label : command;
        seen.add(command);
        tools.push({ command, label });
      }
    }
    return tools;
  }, [nodeTags, selectedNode]);

  const selectedOutlineText = useMemo(() => {
    if (selectedNodeIds.length <= 1) return "";
    const rowById = new Map<string, { node: DocsNode; depth: number }>();
    for (const row of currentRows) rowById.set(row.node.id, row);
    for (const row of splitRows) rowById.set(row.node.id, row);
    return selectedNodeIds
      .map((nodeId) => {
        const row = rowById.get(nodeId);
        const node = row?.node ?? nodesById.get(nodeId);
        if (!node) return "";
        return `${"  ".repeat(row?.depth ?? 0)}- ${nodeText(node)}`;
      })
      .filter(Boolean)
      .join("\n");
  }, [currentRows, nodesById, selectedNodeIds, splitRows]);

  const load = useCallback(async (options: LoadOptions = {}) => {
    const loadGeneration = ++docsLoadGenerationRef.current;
    setLoading(true);
    try {
      if (docsReadOnly) {
        const tree = await getRemoteDocsTree(docsSource);
        if (loadGeneration !== docsLoadGenerationRef.current || !remoteConnectionEnabled) return;
        // Enterprise remote Docs currently returns a complete tree rather than the
        // local cursor API. Mark that snapshot as loaded so lazy-state semantics do
        // not hide descendants or try to call local write/read endpoints.
        const remoteNodes = (tree.nodes ?? []) as unknown as DocsNode[];
        const remoteFieldValues = (tree.field_values ?? []) as unknown as DocsFieldValue[];
        const nextState = {
          ...EMPTY_STATE,
          ...tree,
          loaded_children_parent_ids: remoteNodes.map((node) => node.id),
          details_loaded_ids: remoteNodes.map((node) => node.id),
          has_details_ids: Array.from(new Set([
            ...remoteFieldValues.map((value) => value.node_id),
            ...remoteNodes
              .filter((node) => typeof node.body_json?.verbatim_content === "string" || Boolean(node.body_json?.bookmark))
              .map((node) => node.id),
          ])),
        } as DocsState;
        const defaultNodeId =
          (initialNodeId && nextState.nodes?.some((node) => node.id === initialNodeId)
            ? initialNodeId
            : nextState.nodes?.[0]?.id) ?? null;
        setState(nextState);
        setFocusNodeId(defaultNodeId);
        selectedNodeIdsRef.current = defaultNodeId ? [defaultNodeId] : [];
        selectionAnchorNodeIdRef.current = defaultNodeId;
        setSelectedNodeId(defaultNodeId);
        setSelectedNodeIds(defaultNodeId ? [defaultNodeId] : []);
        setSelectionAnchorNodeId(defaultNodeId);
        return;
      }
      const today = options.focusToday || options.date
        ? await apiFetch<TodayResponse>(`/api/docs/today${options.date ? `?date=${encodeURIComponent(options.date)}` : ""}`)
        : null;
      const data = await apiFetch<DocsState>("/api/docs/bootstrap");
      const firstNodeId = options.nodeId
        ?? initialNodeId
        ?? data.nodes.find((node) => node.system_key === "home")?.id
        ?? data.nodes[0]?.id
        ?? null;
      const [focusedTree, focusedChildren, focusedDetails] = firstNodeId && !options.focusToday && !options.date
        ? await Promise.all([
            apiFetch<DocsNeighborhoodResponse>(`/api/docs/nodes/${firstNodeId}/tree`),
            apiFetch<DocsNeighborhoodResponse>(`/api/docs/nodes/${firstNodeId}/children`),
            apiFetch<DocsNeighborhoodResponse>(`/api/docs/nodes/${firstNodeId}/details`),
          ])
        : [null, null, null];
      if (loadGeneration !== docsLoadGenerationRef.current) return;
      const focusedTreeState = focusedTree
        ? {
            ...focusedTree,
            loaded_children_parent_ids: focusedTree.loaded_children_parent_ids ?? (firstNodeId ? [firstNodeId] : []),
          }
        : null;
      let nextState = mergeLoadedDocsState(EMPTY_STATE, data);
      if (focusedTreeState) nextState = mergeLoadedDocsState(nextState, focusedTreeState);
      if (focusedChildren) nextState = mergeLoadedDocsState(nextState, focusedChildren);
      if (focusedDetails) nextState = mergeLoadedDocsState(nextState, focusedDetails);
      if (today) {
        nextState = mergeLoadedDocsState(nextState, {
          nodes: [today.node],
          supertags: today.supertag ? [today.supertag] : [],
          node_supertags: today.node_supertags ?? [],
        });
      }
      const requestedNodeId = focusedTreeState?.focus_node_id ?? options.nodeId ?? initialNodeId;
      const initialNodeChanged = previousInitialNodeIdRef.current !== initialNodeId;
      previousInitialNodeIdRef.current = initialNodeId;
      const defaultNodeId = today?.node.id
        ?? (requestedNodeId && nextState.nodes.some((node) => node.id === requestedNodeId) ? requestedNodeId : null)
        ?? firstNodeId
        ?? null;
      setState(nextState);
      setFocusNodeId((current) => (today ? today.node.id : initialNodeChanged ? defaultNodeId : current ?? defaultNodeId));
      setSelectedNodeId((current) => {
        const nextId = today ? today.node.id : initialNodeChanged ? defaultNodeId : current ?? defaultNodeId;
        selectedNodeIdsRef.current = nextId ? [nextId] : [];
        selectionAnchorNodeIdRef.current = nextId;
        setSelectedNodeIds(nextId ? [nextId] : []);
        setSelectionAnchorNodeId(nextId);
        return nextId;
      });
    } catch (error) {
      if (loadGeneration === docsLoadGenerationRef.current && (!docsReadOnly || remoteConnectionEnabled)) {
        toast.error(error instanceof Error ? error.message : "Docsの読み込みに失敗しました");
      }
    } finally {
      if (loadGeneration === docsLoadGenerationRef.current) {
        setLoading(false);
      }
    }
  }, [apiFetch, docsReadOnly, docsSource, initialNodeId, remoteConnectionEnabled]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadNodeNeighborhood = useCallback((nodeId: string) => {
    const inFlight = nodeNeighborhoodInFlightRef.current.get(nodeId);
    if (inFlight) return inFlight;
    const request = apiFetch<DocsNeighborhoodResponse>(`/api/docs/nodes/${nodeId}/tree`)
      .then((data) => {
        const normalized: DocsNeighborhoodResponse = {
          ...data,
          // 旧クライアントやリモートDocsがメタデータを返さなくても、要求した親の取得完了を記録する。
          loaded_children_parent_ids: data.loaded_children_parent_ids ?? [nodeId],
        };
        setState((current) => mergeLoadedDocsState(current, normalized));
        return normalized;
      })
      .catch((error) => {
        toast.error(error instanceof Error ? error.message : "Docsノードの読み込みに失敗しました");
        return {};
      })
      .finally(() => {
        nodeNeighborhoodInFlightRef.current.delete(nodeId);
      });
    nodeNeighborhoodInFlightRef.current.set(nodeId, request);
    return request;
  }, [apiFetch]);

  const loadNodeChildren = useCallback((nodeId: string, cursor?: string | null) => {
    const requestKey = `${nodeId}:${cursor ?? "first"}`;
    const inFlight = nodeChildrenInFlightRef.current.get(requestKey);
    if (inFlight) return inFlight.promise;
    const controller = new AbortController();
    markNodeLoading(nodeId, true);
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    const promise = apiFetch<DocsNeighborhoodResponse>(`/api/docs/nodes/${nodeId}/children${query}`, { signal: controller.signal })
      .then((data) => {
        loadedParentAccessRef.current.set(nodeId, Date.now());
        const protectedIds = new Set<string>();
        let protectedId = focusNodeIdRef.current;
        while (protectedId && !protectedIds.has(protectedId)) {
          protectedIds.add(protectedId);
          protectedId = nodesById.get(protectedId)?.parent_id ?? null;
        }
        const overflow = loadedParentAccessRef.current.size - 64;
        const evictParents = overflow > 0
          ? Array.from(loadedParentAccessRef.current.entries())
              .filter(([parentId]) => !protectedIds.has(parentId) && collapsedRef.current.has(parentId))
              .sort((a, b) => a[1] - b[1])
              .slice(0, overflow)
              .map(([parentId]) => parentId)
          : [];
        for (const parentId of evictParents) loadedParentAccessRef.current.delete(parentId);
        setState((current) => evictDocsSubtrees(mergeLoadedDocsState(current, data), evictParents));
        return data;
      })
      .catch((error) => {
        if (controller.signal.aborted || (error instanceof DOMException && error.name === "AbortError")) return {};
        toast.error(error instanceof Error ? error.message : "Docsの子ノード読み込みに失敗しました");
        return {};
      })
      .finally(() => {
        nodeChildrenInFlightRef.current.delete(requestKey);
        markNodeLoading(nodeId, false);
      });
    nodeChildrenInFlightRef.current.set(requestKey, { controller, promise });
    return promise;
  }, [apiFetch, markNodeLoading, nodesById]);

  const ensureNodeChildrenLoaded = useCallback((nodeId: string) => {
    if ((state.loaded_children_parent_ids ?? []).includes(nodeId)) return Promise.resolve(null);
    return loadNodeChildren(nodeId);
  }, [loadNodeChildren, state.loaded_children_parent_ids]);

  const loadNodeDetails = useCallback((nodeId: string) => {
    const inFlight = nodeDetailsInFlightRef.current.get(nodeId);
    if (inFlight) return inFlight.promise;
    const controller = new AbortController();
    markNodeLoading(nodeId, true);
    const promise = apiFetch<DocsNeighborhoodResponse>(`/api/docs/nodes/${nodeId}/details`, { signal: controller.signal })
      .then((data) => {
        setState((current) => mergeLoadedDocsState(current, data));
        return data;
      })
      .catch((error) => {
        if (controller.signal.aborted || (error instanceof DOMException && error.name === "AbortError")) return {};
        toast.error(error instanceof Error ? error.message : "Docsの詳細読み込みに失敗しました");
        return {};
      })
      .finally(() => {
        nodeDetailsInFlightRef.current.delete(nodeId);
        markNodeLoading(nodeId, false);
      });
    nodeDetailsInFlightRef.current.set(nodeId, { controller, promise });
    return promise;
  }, [apiFetch, markNodeLoading]);

  const ensureNodeDetailsLoaded = useCallback((nodeId: string) => {
    if ((state.details_loaded_ids ?? []).includes(nodeId)) return Promise.resolve(null);
    return loadNodeDetails(nodeId);
  }, [loadNodeDetails, state.details_loaded_ids]);

  const expandNode = useCallback((nodeId: string) => {
    const wasCollapsed = nodeIsCollapsed(nodeId);
    setCollapsed((current) => {
      const next = new Set(current);
      next.delete(nodeId);
      writeCollapsed(next);
      return next;
    });
    void Promise.all([
      ensureNodeChildrenLoaded(nodeId),
      ensureNodeDetailsLoaded(nodeId),
    ]).finally(() => {
      // 遅延読込で行DOMが差し替わっても、キーボード操作の現在地を失わない。
      setFocusRequestNodeId(nodeId);
    });
    const nextCursor = state.children_next_cursor_by_parent?.[nodeId];
    if (!wasCollapsed && nextCursor) void loadNodeChildren(nodeId, nextCursor);
  }, [ensureNodeChildrenLoaded, ensureNodeDetailsLoaded, loadNodeChildren, nodeIsCollapsed, state.children_next_cursor_by_parent]);

  useEffect(() => {
    expandNodeRef.current = expandNode;
  }, [expandNode]);

  const openDocsNode = useCallback((nodeId: string) => {
    setMainView(null);
    setTagPageId(null);
    setFocusNodeId(nodeId);
    selectSingleNode(nodeId);
    if (!docsReadOnly) void loadNodeNeighborhood(nodeId);
    void ensureNodeChildrenLoaded(nodeId);
    void ensureNodeDetailsLoaded(nodeId);
  }, [docsReadOnly, ensureNodeChildrenLoaded, ensureNodeDetailsLoaded, loadNodeNeighborhood, selectSingleNode]);

  useEffect(() => {
    sidebarScrollNodeRef.current = focusNodeId;
  }, [focusNodeId]);

  useEffect(() => {
    if (!focusNodeId || mainView || tagPageId) return;
    const ancestorIds = new Set<string>();
    let current = nodesById.get(focusNodeId);
    const visited = new Set<string>();
    while (current?.parent_id && !visited.has(current.parent_id)) {
      visited.add(current.parent_id);
      ancestorIds.add(current.parent_id);
      current = nodesById.get(current.parent_id);
    }
    if (ancestorIds.size === 0) return;
    setSidebarCollapsed((current) => {
      const next = new Set(current);
      let changed = false;
      for (const ancestorId of ancestorIds) {
        if (!next.has(ancestorId)) {
          next.add(ancestorId);
          changed = true;
        }
      }
      if (!changed) return current;
      writeCollapsed(next, SIDEBAR_COLLAPSED_KEY);
      return next;
    });
  }, [focusNodeId, mainView, nodesById, tagPageId]);

  useEffect(() => {
    const requestedNodeId = sidebarScrollNodeRef.current;
    if (!requestedNodeId || mainView || tagPageId || typeof window === "undefined") return;
    const frame = window.requestAnimationFrame(() => {
      const element = Array.from(document.querySelectorAll<HTMLElement>("[data-docs-sidebar-node-id]"))
        .find((candidate) => candidate.getAttribute("data-docs-sidebar-node-id") === requestedNodeId);
      if (element) {
        element.scrollIntoView({ block: "nearest" });
        if (sidebarScrollNodeRef.current === requestedNodeId) sidebarScrollNodeRef.current = null;
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [focusNodeId, mainView, nodesById, sidebarCollapsed, tagPageId]);

  const openToday = useCallback((date?: string) => {
    setMainView(null);
    setTagPageId(null);
    void load({ focusToday: true, date });
  }, [load]);

  useEffect(() => {
    if (!focusNodeReferenceId) {
      setPageReferences(EMPTY_REFERENCES);
      setPageReferencesLoading(false);
      return;
    }
    let cancelled = false;
    setPageReferences(EMPTY_REFERENCES);
    setPageReferencesLoading(true);
    apiFetch<ReferencesState>(`/api/docs/nodes/${focusNodeReferenceId}/references`)
      .then((data) => {
        if (!cancelled) setPageReferences({ ...EMPTY_REFERENCES, ...data });
      })
      .catch((error) => {
        if (!cancelled) toast.error(error instanceof Error ? error.message : "参照の読み込みに失敗しました");
      })
      .finally(() => {
        if (!cancelled) setPageReferencesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [focusNodeReferenceId]);

  useEffect(() => {
    const visibleDomNodeIds = () => Array.from(document.querySelectorAll<HTMLElement>("[data-docs-node-id]"))
      .map((element) => element.getAttribute("data-docs-node-id"))
      .filter((nodeId): nodeId is string => Boolean(nodeId))
      .filter((nodeId, index, all) => all.indexOf(nodeId) === index);
    const selectDomRange = (activeNodeId: string, direction?: -1 | 1) => {
      const visibleIds = visibleDomNodeIds();
      const currentIndex = visibleIds.indexOf(activeNodeId);
      if (currentIndex < 0) return false;
      const anchorId = selectionAnchorNodeIdRef.current ?? selectionAnchorNodeId ?? activeNodeId;
      const anchorIndex = Math.max(0, visibleIds.indexOf(anchorId));
      const targetIndex = typeof direction === "number"
        ? Math.max(0, Math.min(visibleIds.length - 1, currentIndex + direction))
        : currentIndex;
      const start = Math.min(anchorIndex, targetIndex);
      const end = Math.max(anchorIndex, targetIndex);
      const nextIds = visibleIds.slice(start, end + 1);
      const targetId = visibleIds[targetIndex] ?? activeNodeId;
      selectedNodeIdsRef.current = nextIds;
      selectionAnchorNodeIdRef.current = visibleIds[anchorIndex] ?? activeNodeId;
      setSelectedNodeIds(nextIds);
      setSelectedNodeId(targetId);
      setSelectionAnchorNodeId(visibleIds[anchorIndex] ?? activeNodeId);
      if (direction && targetId) {
        preserveSelectionOnNextFocusRef.current = true;
        setFocusRequestNodeId(targetId);
      }
      return true;
    };
    const activeDocsNode = () => {
      const activeNodeId = (document.activeElement as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
      if (!activeNodeId) return null;
      const node = nodesById.get(activeNodeId);
      const rows = currentRows.some((row) => row.node.id === activeNodeId) ? currentRows : splitRows;
      return node && rows.length > 0 ? { node, rows } : null;
    };
    const currentTargetNodeId = () => {
      const activeNodeId = (document.activeElement as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
      return activeNodeId ?? selectedNodeIdsRef.current[0] ?? selectionAnchorNodeIdRef.current ?? null;
    };
    const handleGlobalKey = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      const key = event.key.toLowerCase();
      const target = event.target as HTMLElement | null;
      const editableTarget = target?.closest(".cm-editor, input, textarea, select, [contenteditable='true']");
      const docsEditorTarget = target?.closest("[data-testid='docs-block-editor']");
      if ((event.ctrlKey || event.metaKey) && !editableTarget && !docsEditorTarget && (key === "z" || key === "y")) {
        event.preventDefault();
        event.stopImmediatePropagation();
        const operation = key === "y" || (key === "z" && event.shiftKey)
          ? redoDocsOperationRef.current()
          : undoDocsOperationRef.current();
        void operation.finally(() => {
          const focusedId = focusNodeIdRef.current;
          Array.from(document.querySelectorAll<HTMLElement>("[data-docs-editor-node-id]"))
            .find((item) => item.dataset.docsEditorNodeId === focusedId)
            ?.focus();
        });
        return;
      }
      if (event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
        const active = activeDocsNode();
        const activeNodeId = (document.activeElement as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
        if (activeNodeId && selectDomRange(activeNodeId, event.key === "ArrowUp" ? -1 : 1)) {
          event.preventDefault();
          event.stopImmediatePropagation();
        } else if (active) {
          event.preventDefault();
          event.stopImmediatePropagation();
          extendNodeSelection(active.node, active.rows, event.key === "ArrowUp" ? -1 : 1);
        }
        return;
      }
      if ((event.ctrlKey || event.metaKey) && key === "k") {
        event.preventDefault();
        event.stopImmediatePropagation();
        openCommand();
      }
      if (event.ctrlKey && event.shiftKey && key === "d") {
        event.preventDefault();
        openToday();
      }
      if (event.altKey && (event.key === "ArrowLeft" || event.key === "ArrowRight") && focusNode?.day_date) {
        const targetDate = nodeDateDelta(focusNode, event.key === "ArrowLeft" ? -1 : 1);
        if (targetDate) {
          event.preventDefault();
          openToday(targetDate);
        }
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
        const targetNodeId = currentTargetNodeId();
        if (targetNodeId) {
          event.preventDefault();
          event.stopImmediatePropagation();
          if (event.key === "ArrowRight") {
            expandNodeRef.current(targetNodeId);
          } else {
            setCollapsed((current) => {
              const next = new Set(current);
              next.add(targetNodeId);
              writeCollapsed(next);
              return next;
            });
            abortNodeLoads(targetNodeId);
          }
        }
        return;
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && key === "\\") {
        event.preventDefault();
        event.stopImmediatePropagation();
        const targetNodeId = currentTargetNodeId();
        if (targetNodeId) {
          setSidebarCollapsed((current) => {
            const next = new Set(current);
            if (next.has(targetNodeId)) next.delete(targetNodeId);
            else next.add(targetNodeId);
            writeCollapsed(next, SIDEBAR_COLLAPSED_KEY);
            return next;
          });
        }
        return;
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && (event.key === "." || key === ".")) {
        const targetNodeId = currentTargetNodeId();
        if (targetNodeId) {
          event.preventDefault();
          event.stopImmediatePropagation();
          openDocsNode(targetNodeId);
        }
        return;
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && (event.key === "," || key === ",")) {
        if (focusNode?.parent_id) {
          event.preventDefault();
          event.stopImmediatePropagation();
          const parentId = focusNode.parent_id;
          openDocsNode(parentId);
        }
        return;
      }
    };
    const handleGlobalKeyUp = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      if (event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
        const active = activeDocsNode();
        const activeNodeId = (document.activeElement as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
        if (activeNodeId && selectDomRange(activeNodeId)) return;
        if (active) selectRangeToNode(active.node, active.rows);
      }
    };
    document.addEventListener("keydown", handleGlobalKey, true);
    document.addEventListener("keyup", handleGlobalKeyUp, true);
    window.addEventListener("keydown", handleGlobalKey, true);
    window.addEventListener("keyup", handleGlobalKeyUp, true);
    return () => {
      document.removeEventListener("keydown", handleGlobalKey, true);
      document.removeEventListener("keyup", handleGlobalKeyUp, true);
      window.removeEventListener("keydown", handleGlobalKey, true);
      window.removeEventListener("keyup", handleGlobalKeyUp, true);
    };
  }, [abortNodeLoads, currentRows, extendNodeSelection, focusNode, nodesById, openCommand, openDocsNode, openToday, selectRangeToNode, selectSingleNode, selectionAnchorNodeId, splitRows]);

  useEffect(() => {
    const handleCopy = (event: ClipboardEvent) => {
      if (selectedNodeIds.length <= 1 || !selectedOutlineText) return;
      event.preventDefault();
      event.clipboardData?.setData("text/plain", selectedOutlineText);
    };
    document.addEventListener("copy", handleCopy);
    return () => document.removeEventListener("copy", handleCopy);
  }, [selectedNodeIds.length, selectedOutlineText]);

  const patchNode = useCallback(async (nodeId: string, patch: NodePatch) => {
    try {
      return await docsSaveQueue.enqueue(nodeId, {
        execute: async () => {
          await nodeCreateInFlightRef.current.get(nodeId);
          const data = await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${nodeId}`, {
            method: "PATCH",
            body: JSON.stringify(patch),
            keepalive: true,
          });
          return data.node;
        },
        apply: (node) => {
          setState((current) => ({ ...current, nodes: current.nodes.map((item) => (item.id === node.id ? node : item)) }));
        },
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Docsの保存に失敗しました。もう一度確定してください");
      throw error;
    }
  }, [apiFetch, docsSaveQueue]);

  const archiveNode = useCallback((nodeId: string) => (
    docsSaveQueue.enqueue(nodeId, {
      execute: async () => {
        await nodeCreateInFlightRef.current.get(nodeId);
        const data = await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${nodeId}`, { method: "DELETE", keepalive: true });
        return data.node;
      },
      apply: (node) => {
        setState((current) => ({ ...current, nodes: current.nodes.map((item) => (item.id === node.id ? node : item)) }));
      },
    })
  ), [apiFetch, docsSaveQueue]);

  const restoreNode = useCallback((nodeId: string) => (
    docsSaveQueue.enqueue(nodeId, {
      execute: async () => {
        await nodeCreateInFlightRef.current.get(nodeId);
        const data = await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${nodeId}`, {
          method: "PATCH",
          body: JSON.stringify({ archived: false }),
          keepalive: true,
        });
        return data.node;
      },
      apply: (node) => {
        setState((current) => ({ ...current, nodes: current.nodes.map((item) => (item.id === node.id ? node : item)) }));
      },
    })
  ), [apiFetch, docsSaveQueue]);

  const permanentlyDeleteNode = useCallback(async (nodeId: string) => {
    await apiFetch<{ ok: boolean }>(`/api/docs/nodes/${nodeId}?permanent=1`, { method: "DELETE" });
    setState((current) => ({ ...current, nodes: current.nodes.filter((node) => node.id !== nodeId) }));
  }, [apiFetch]);

  const createNode = useCallback((
    parentId: string | null,
    afterNode?: DocsNode | null,
    title = "",
    options: { bodyJson?: Record<string, unknown>; displayProps?: Record<string, unknown>; nodeType?: DocsNode["node_type"]; optimistic?: boolean } = {},
  ) => {
    const id = globalThis.crypto?.randomUUID?.() ?? `docs-node-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const parent = parentId ? nodesById.get(parentId) ?? null : null;
    const siblings = parentId ? childrenByParent.get(parentId) ?? [] : roots;
    const afterIndex = afterNode ? siblings.findIndex((node) => node.id === afterNode.id) : siblings.length - 1;
    const previous = afterIndex >= 0 ? siblings[afterIndex] : null;
    const next = afterIndex >= 0 ? siblings[afterIndex + 1] : siblings[0];
    const sortOrder = midpointSortOrder(previous?.sort_order, next?.sort_order);
    const now = new Date().toISOString();
    const optimisticNode: DocsNode = {
      id,
      workspace_id: parent?.workspace_id ?? afterNode?.workspace_id ?? roots[0]?.workspace_id ?? "",
      parent_id: parentId,
      root_page_id: parent ? parent.root_page_id ?? parent.id : id,
      project_id: parent?.project_id ?? afterNode?.project_id ?? null,
      system_key: null,
      title,
      aliases: [],
      description: "",
      body_json: options.bodyJson ?? { format: "doc_block", block_type: "paragraph" },
      body_text: title,
      node_type: options.nodeType ?? "node",
      display_props: options.displayProps ?? {},
      query_json: null,
      view_json: {},
      day_date: null,
      sort_order: sortOrder,
      created_at: now,
      updated_at: now,
      archived_at: null,
    };
    flushSync(() => {
      setState((current) => ({ ...current, nodes: [...current.nodes, optimisticNode] }));
    });
    const persistNode = apiFetch<{ node: DocsNode }>("/api/docs", {
      method: "POST",
      body: JSON.stringify({
        id,
        parent_id: parentId,
        title,
        body_text: title,
        body_json: optimisticNode.body_json,
        display_props: optimisticNode.display_props,
        node_type: optimisticNode.node_type,
        sort_order: sortOrder,
      }),
    });
    const persistenceReady = persistNode.then((data) => data.node);
    nodeCreateInFlightRef.current.set(id, persistenceReady);
    const clearPersistence = () => {
      if (nodeCreateInFlightRef.current.get(id) === persistenceReady) {
        nodeCreateInFlightRef.current.delete(id);
      }
    };
    void persistenceReady.then(clearPersistence, clearPersistence);
    const applyPersistedNode = (data: { node: DocsNode }) => {
      // The POST response contains the initial empty optimistic payload. Preserve
      // keystrokes and structural edits made while that request was in flight.
      setState((current) => ({
        ...current,
        nodes: current.nodes.map((node) => (
          node.id === id
            ? {
                ...data.node,
                ...node,
                workspace_id: data.node.workspace_id || node.workspace_id,
                created_at: data.node.created_at ?? node.created_at,
              }
            : node
        )),
      }));
      if (!options.optimistic) {
        selectSingleNode(data.node.id);
        setFocusRequestNodeId(data.node.id);
      }
      return data.node;
    };
    const rollbackNode = (error: unknown) => {
      setState((current) => ({ ...current, nodes: current.nodes.filter((node) => node.id !== id) }));
      throw error;
    };
    if (options.optimistic) {
      void persistNode.then(applyPersistedNode).catch((error) => {
        setState((current) => ({ ...current, nodes: current.nodes.filter((node) => node.id !== id) }));
        toast.error(error instanceof Error ? error.message : "Docsノードの作成に失敗しました");
      });
      selectSingleNode(optimisticNode.id);
      setFocusRequestNodeId(optimisticNode.id);
      return optimisticNode;
    }
    return persistNode.then(applyPersistedNode).catch(rollbackNode);
  }, [childrenByParent, nodesById, roots, selectSingleNode]);

  const createRootNode = useCallback(async () => {
    const node = await createNode(null, null, "");
    openDocsNode(node.id);
    setFocusRequestNodeId(node.id);
  }, [createNode, openDocsNode]);

  const pushHistory = useCallback((entry: BlockHistoryEntry) => {
    if (applyingHistoryRef.current || entry.patches.length === 0) return;
    undoStackRef.current = [...undoStackRef.current, entry].slice(-100);
    redoStackRef.current = [];
  }, []);

  const foldIntoPendingCreateHistory = useCallback((node: DocsNode) => {
    const previousEntry = undoStackRef.current.at(-1);
    const previousPatch = previousEntry?.patches.length === 1 ? previousEntry.patches[0] : null;
    if (
      !pendingCreateHistoryIdsRef.current.has(node.id)
      || !previousEntry
      || previousPatch?.type !== "create"
      || previousPatch.node.id !== node.id
    ) return false;
    undoStackRef.current = [
      ...undoStackRef.current.slice(0, -1),
      {
        ...previousEntry,
        patches: [{ type: "create", node: snapshotDocsNode(node) }],
      },
    ];
    redoStackRef.current = [];
    return true;
  }, []);

  const createNodeFromSnapshot = useCallback(async (snapshot: DocsBlockSnapshot) => {
    // Undo of a create archives the row; redo must restore that same row even when
    // lazy loading has already evicted it from the client state. The archived DB row
    // is canonical and still contains edits committed around the same time as the
    // create, so restoring it is safer than overwriting it with an early snapshot.
    await restoreNode(snapshot.id);
    const node = await patchNode(snapshot.id, patchFromSnapshot(snapshot));
    setState((current) => ({
      ...current,
      nodes: current.nodes.some((item) => item.id === node.id)
        ? current.nodes.map((item) => (item.id === node.id ? node : item))
        : [...current.nodes, node],
    }));
    return node;
  }, [patchNode, restoreNode]);

  const applyHistoryEntry = useCallback(async (entry: BlockHistoryEntry) => {
    applyingHistoryRef.current = true;
    try {
      for (const patch of entry.patches) {
        if (patch.type === "update") {
          await patchNode(patch.id, patchFromSnapshot(patch.after));
        } else if (patch.type === "create") {
          await createNodeFromSnapshot(patch.node);
        } else if (patch.type === "archive") {
          await archiveNode(patch.node.id);
        }
      }
      await load({ nodeId: focusNodeIdRef.current ?? undefined });
    } finally {
      applyingHistoryRef.current = false;
    }
  }, [archiveNode, createNodeFromSnapshot, load, patchNode]);

  const undoDocsOperation = useCallback(async () => {
    await editorCommitInFlightRef.current?.catch(() => false);
    const entry = undoStackRef.current.at(-1);
    if (!entry) return;
    undoStackRef.current = undoStackRef.current.slice(0, -1);
    // A create may be persisted while its CodeMirror commit is still settling.
    // Capture the canonical line at Undo time so Redo never resurrects the
    // original empty optimistic snapshot.
    const currentEntry: BlockHistoryEntry = {
      ...entry,
      patches: entry.patches.map((patch) => {
        if (patch.type !== "create") return patch;
        const canonical = nodesByIdRef.current.get(patch.node.id);
        const draft = latestEditorDraftRef.current.get(patch.node.id);
        const snapshot = canonical ? snapshotDocsNode(canonical) : patch.node;
        return {
          type: "create" as const,
          node: typeof draft === "string"
            ? { ...snapshot, title: draft, body_text: draft }
            : snapshot,
        };
      }),
    };
    const inverted = invertHistoryEntry(currentEntry);
    await applyHistoryEntry(inverted);
    redoStackRef.current = [...redoStackRef.current, currentEntry];
  }, [applyHistoryEntry]);

  const redoDocsOperation = useCallback(async () => {
    await editorCommitInFlightRef.current?.catch(() => false);
    const entry = redoStackRef.current.at(-1);
    if (!entry) return;
    redoStackRef.current = redoStackRef.current.slice(0, -1);
    await applyHistoryEntry(entry);
    undoStackRef.current = [...undoStackRef.current, entry];
  }, [applyHistoryEntry]);

  useEffect(() => {
    undoDocsOperationRef.current = undoDocsOperation;
    redoDocsOperationRef.current = redoDocsOperation;
  }, [redoDocsOperation, undoDocsOperation]);

  const toggleCollapsed = (nodeId: string) => {
    const expanding = nodeIsCollapsed(nodeId);
    if (expanding) {
      expandNode(nodeId);
      return;
    }
    setCollapsed((current) => {
      const next = new Set(current);
      next.add(nodeId);
      writeCollapsed(next);
      return next;
    });
    abortNodeLoads(nodeId);
  };

  const toggleSidebarCollapsed = (nodeId: string) => {
    const expanding = !sidebarCollapsed.has(nodeId);
    if (expanding) void ensureNodeChildrenLoaded(nodeId);
    setSidebarCollapsed((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      writeCollapsed(next, SIDEBAR_COLLAPSED_KEY);
      return next;
    });
  };

  const openSidebarNode = useCallback((node: DocsNode, event?: ReactMouseEvent<HTMLElement>) => {
    if (event?.shiftKey || event?.ctrlKey || event?.metaKey) {
      const visibleIds = Array.from(document.querySelectorAll<HTMLElement>("[data-docs-sidebar-node-id]"))
        .map((element) => element.getAttribute("data-docs-sidebar-node-id"))
        .filter((nodeId): nodeId is string => Boolean(nodeId));
      const anchorId = selectionAnchorNodeIdRef.current ?? selectedNodeIdsRef.current[0] ?? node.id;
      if (event.shiftKey && visibleIds.includes(anchorId) && visibleIds.includes(node.id)) {
        const anchorIndex = visibleIds.indexOf(anchorId);
        const targetIndex = visibleIds.indexOf(node.id);
        const start = Math.min(anchorIndex, targetIndex);
        const end = Math.max(anchorIndex, targetIndex);
        const nextIds = visibleIds.slice(start, end + 1);
        selectedNodeIdsRef.current = nextIds;
        setSelectedNodeIds(nextIds);
        setSelectedNodeId(node.id);
        setSelectionAnchorNodeId(anchorId);
        return;
      }
      if (event.ctrlKey || event.metaKey) {
        const current = new Set(selectedNodeIdsRef.current);
        if (current.has(node.id)) {
          current.delete(node.id);
        } else {
          current.add(node.id);
        }
        const nextIds = Array.from(current);
        selectedNodeIdsRef.current = nextIds;
        selectionAnchorNodeIdRef.current = node.id;
        setSelectedNodeIds(nextIds);
        setSelectedNodeId(node.id);
        setSelectionAnchorNodeId(node.id);
        return;
      }
    }
    openDocsNode(node.id);
  }, [openDocsNode, selectSingleNode]);

  const openSidebarNodeContextMenu = useCallback((event: ReactMouseEvent<HTMLElement>, node: DocsNode) => {
    event.preventDefault();
    event.stopPropagation();
    selectSingleNode(node.id);
    setSidebarContextMenu({ x: event.clientX, y: event.clientY, nodeId: node.id });
  }, [selectSingleNode]);

  const dropSidebarNode = useCallback(async (targetNode: DocsNode) => {
    if (!dragSidebarNodeId || dragSidebarNodeId === targetNode.id) return;
    const draggedNode = nodesById.get(dragSidebarNodeId);
    if (!draggedNode) return;
    setDragSidebarNodeId(null);
    const targets = selectedNodeIdsRef.current.includes(draggedNode.id)
      ? selectedNodeIdsRef.current.map((nodeId) => nodesById.get(nodeId)).filter((node): node is DocsNode => Boolean(node))
      : [draggedNode];
    for (const node of targets) {
      if (node.id === targetNode.id) continue;
      await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${node.id}/move`, {
        method: "POST",
        body: JSON.stringify({ new_parent_id: targetNode.id, leave_reference: false }),
      });
    }
    await load();
  }, [dragSidebarNodeId, load, nodesById]);

  const archiveSidebarNode = useCallback(async (node: DocsNode) => {
    setSidebarContextMenu(null);
    try {
      await archiveNode(node.id);
      const fallbackId = node.parent_id && nodesById.get(node.parent_id) && !nodesById.get(node.parent_id)?.archived_at
        ? node.parent_id
        : roots.find((root) => root.id !== node.id)?.id ?? null;
      if (focusNodeId === node.id) {
        if (fallbackId) openDocsNode(fallbackId);
        else selectSingleNode(null);
      } else if (selectedNodeId === node.id) {
        selectSingleNode(null);
      }
      if (splitNodeId === node.id) setSplitNodeId(null);
      toast.success("ノードをアーカイブしました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "ノードのアーカイブに失敗しました");
    }
  }, [archiveNode, focusNodeId, nodesById, openDocsNode, roots, selectSingleNode, selectedNodeId, splitNodeId]);

  async function renameSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    const nextTitle = window.prompt("ノード名を変更", nodeText(node));
    if (!nextTitle || nextTitle.trim() === node.title) return;
    await patchNode(node.id, { title: nextTitle.trim() });
  }

  async function duplicateSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    const duplicated = await createNode(node.parent_id, node, `${nodeText(node)} copy`);
    await patchNode(duplicated.id, {
      description: node.description,
      display_props: safeNodeDisplayProps(node),
      query_json: node.query_json,
      view_json: node.view_json,
      node_type: node.node_type,
      project_id: node.project_id,
    });
    for (const relation of state.node_supertags.filter((item) => item.node_id === node.id)) {
      await applyTag(duplicated, relation.supertag_id);
    }
    toast.success("ノードを複製しました");
  }

  async function exportSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    const rows = outlineRows(node.id, childrenByParent, new Set<string>());
    const text = [nodeText(node), ...rows.map((row) => `${"  ".repeat(row.depth + 1)}- ${nodeText(row.node)}`)].join("\n");
    await navigator.clipboard.writeText(text);
    toast.success("アウトラインをクリップボードへコピーしました");
  }

  async function pinSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    await updateDisplayProps(node, { pinned_sidebar: node.display_props?.pinned_sidebar !== true });
  }

  function moveSidebarNodeWithReference(node: DocsNode) {
    setSidebarContextMenu(null);
    selectSingleNode(node.id);
    openCommand({ kind: "move", leaveReference: true });
  }

  const mutateTag = async (node: DocsNode, tagId: string, mode: "add" | "remove") => {
    const currentIds = nodeTagIdsRef.current.get(node.id) ?? (nodeTags.get(node.id) ?? []).map((tag) => tag.id);
    if ((mode === "add" && currentIds.includes(tagId)) || (mode === "remove" && !currentIds.includes(tagId))) return;
    const desiredIds = mode === "add"
      ? [...currentIds, tagId]
      : currentIds.filter((id) => id !== tagId);
    nodeTagIdsRef.current.set(node.id, desiredIds);
    setState((currentState) => ({
      ...currentState,
      node_supertags: [
        ...currentState.node_supertags.filter((item) => item.node_id !== node.id),
        ...desiredIds.map((supertagId) => ({ node_id: node.id, supertag_id: supertagId })),
      ],
    }));

    const previous = tagMutationQueueRef.current.get(node.id) ?? Promise.resolve();
    const operation = previous.catch(() => {}).then(async () => {
      try {
        const data = await apiFetch<{ node_supertags: DocsState["node_supertags"]; nodes?: DocsNode[]; task_binding_error?: string | null }>(`/api/docs/nodes/${node.id}/supertags`, {
          method: "PUT",
          body: JSON.stringify(mode === "add"
            ? { add_supertag_ids: [tagId] }
            : { remove_supertag_ids: [tagId] }),
        });
        const latestDesiredIds = nodeTagIdsRef.current.get(node.id) ?? [];
        const responseIds = data.node_supertags.map((item) => item.supertag_id);
        const isLatestOperation = JSON.stringify([...latestDesiredIds].sort()) === JSON.stringify([...desiredIds].sort());
        const responseMatchesDesired = JSON.stringify([...desiredIds].sort()) === JSON.stringify([...responseIds].sort());
        if (isLatestOperation) nodeTagIdsRef.current.set(node.id, responseIds);
        setState((currentState) => ({
          ...currentState,
          nodes: data.nodes?.length
            ? data.nodes.reduce((nodes, item) => mergeById(nodes, item), currentState.nodes)
            : currentState.nodes,
          node_supertags: isLatestOperation
            ? [
                ...currentState.node_supertags.filter((item) => item.node_id !== node.id),
                ...data.node_supertags,
              ]
            : currentState.node_supertags,
        }));
        if (isLatestOperation && !responseMatchesDesired) toast.error("Supertagの確定状態をサーバーに合わせました");
        if (data.task_binding_error) toast.error("Supertagは更新されましたが、タスク連携に失敗しました");
      } catch (error) {
        const latestDesiredIds = nodeTagIdsRef.current.get(node.id) ?? [];
        if (JSON.stringify(latestDesiredIds) === JSON.stringify(desiredIds)) {
          nodeTagIdsRef.current.set(node.id, currentIds);
          setState((currentState) => ({
            ...currentState,
            node_supertags: [
              ...currentState.node_supertags.filter((item) => item.node_id !== node.id),
              ...currentIds.map((supertagId) => ({ node_id: node.id, supertag_id: supertagId })),
            ],
          }));
        }
        toast.error(error instanceof Error ? error.message : `Supertagを${mode === "add" ? "設定" : "解除"}できませんでした`);
      }
    });
    tagMutationQueueRef.current.set(node.id, operation);
    await operation.finally(() => {
      if (tagMutationQueueRef.current.get(node.id) === operation) tagMutationQueueRef.current.delete(node.id);
    });
  };

  const applyTag = (node: DocsNode, tagId: string) => mutateTag(node, tagId, "add");
  const removeTag = (node: DocsNode, tagId: string) => mutateTag(node, tagId, "remove");

  const saveField = async (node: DocsNode, field: DocsField, raw: string) => {
    const data = await apiFetch<{ field_values: DocsFieldValue[] }>(`/api/docs/nodes/${node.id}/fields`, {
      method: "PUT",
      body: JSON.stringify({ field_values: [{ field_id: field.id, value: fieldDraftToPayload(field, raw) }] }),
    });
    setState((current) => ({
      ...current,
      field_values: [
        ...current.field_values.filter((value) => !(value.node_id === node.id && value.field_id === field.id)),
        ...data.field_values,
      ],
    }));
  };

  const deleteAttachment = async (attachment: DocsAttachment) => {
    await apiFetch<{ ok: true }>(`/api/docs/attachments/${attachment.id}`, { method: "DELETE" });
    setState((current) => ({
      ...current,
      attachments: current.attachments.filter((item) => item.id !== attachment.id),
    }));
  };

  const updateSuggestionStatus = async (suggestionId: string, status: "accepted" | "rejected" | "stale") => {
    const data = await apiFetch<{ suggestion: DocsAiSuggestion }>(`/api/docs/suggestions/${suggestionId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    setState((current) => ({
      ...current,
      ai_suggestions: current.ai_suggestions.map((item) => item.id === data.suggestion.id ? data.suggestion : item),
    }));
  };

  const taskStatusField = state.fields.find((field) => field.system_key === "task_status") ?? null;
  const isTaskNode = (node: DocsNode) => (nodeTags.get(node.id) ?? []).some((tag) => tag.system_key === "task");
  const taskTag = state.supertags.find((tag) => tag.system_key === "task") ?? null;
  const taskDoneMapping = readConfigRecord(taskTag?.config_json?.done_state_mapping ?? taskTag?.config_json?.doneStateMapping);
  const taskDoneValue = typeof taskDoneMapping.done_value === "string"
    ? taskDoneMapping.done_value
    : typeof taskDoneMapping.checked_value === "string"
      ? taskDoneMapping.checked_value
    : typeof taskDoneMapping.doneValue === "string"
      ? taskDoneMapping.doneValue
      : "closed";
  const taskOpenValue = typeof taskDoneMapping.open_value === "string"
    ? taskDoneMapping.open_value
    : typeof taskDoneMapping.unchecked_value === "string"
      ? taskDoneMapping.unchecked_value
    : typeof taskDoneMapping.openValue === "string"
      ? taskDoneMapping.openValue
      : "todo";
  const taskStatusForNode = (node: DocsNode) =>
    taskStatusField
      ? fieldValueToDraft(fieldValuesByKey.get(`${node.id}:${taskStatusField.id}`)).trim().toLowerCase()
      : "";
  const checkedForNode = (node: DocsNode) =>
    isTaskNode(node)
      ? [taskDoneValue, "done", "closed", "complete", "completed"].map((value) => value.toLowerCase()).includes(taskStatusForNode(node))
      : node.display_props?.checked === true;
  const toggleNodeCheckbox = async (node: DocsNode) => {
    if (isTaskNode(node) && taskStatusField) {
      const nextStatus = checkedForNode(node) ? taskOpenValue : taskDoneValue;
      await saveField(node, taskStatusField, nextStatus);
      if (node.display_props?.show_checkbox !== true) {
        await updateDisplayProps(node, { show_checkbox: true });
      }
      return;
    }
    await updateDisplayProps(node, { show_checkbox: true, checked: node.display_props?.checked !== true });
  };

  const updateDisplayProps = async (node: DocsNode, patch: Record<string, unknown>) => {
    const canonical = nodesById.get(node.id) ?? node;
    await patchNode(node.id, { display_props: { ...safeNodeDisplayProps(canonical), ...patch } });
  };

  const commitTitle = async (node: DocsNode, title: string) => {
    const matchedTags = titleTagNames(title)
      .map((name) => state.supertags.find((tag) => tag.name.toLowerCase() === name.toLowerCase()))
      .filter((tag): tag is DocsSupertag => Boolean(tag));
    const nextTitle = matchedTags.length > 0 ? titleWithoutTagTokens(title) : title;
    // 入力中は同じ node.title を楽観更新しているため、確定時の比較では
    // 永続化済みかを判定できない。blur / Enter では必ず保存キューへ送る。
    await patchNode(node.id, { title: nextTitle });
    if (/\[\[user:[0-9a-f-]{36}\|[^\]\n]+\]\]/i.test(nextTitle)) {
      void apiFetch(`/api/docs/nodes/${node.id}/mentions`, {
        method: "POST",
        body: JSON.stringify({ title: nextTitle }),
      }).catch(() => undefined);
    }
    for (const tag of matchedTags) {
      await applyTag(node, tag.id);
    }
  };

  const replaceNodeTitles = async (updates: Array<{ node: DocsNode; title: string }>) => {
    const patches = updates
      .filter(({ node, title }) => node.title !== title)
      .map(({ node, title }) => ({
        type: "update" as const,
        id: node.id,
        before: snapshotDocsNode(nodesById.get(node.id) ?? node),
        after: snapshotDocsNode({ ...(nodesById.get(node.id) ?? node), title }),
      }));
    if (patches.length === 0) return;
    pushHistory({ label: "検索置換", patches });
    setState((current) => {
      const titleById = new Map(updates.map((update) => [update.node.id, update.title]));
      return {
        ...current,
        nodes: current.nodes.map((node) => titleById.has(node.id) ? { ...node, title: titleById.get(node.id) ?? node.title } : node),
      };
    });
    for (const { node, title } of updates) {
      if (node.title !== title) await patchNode(node.id, { title });
    }
  };

  const createTag = async () => {
    const name = newTagName.replace(/^#/, "").trim();
    if (!name) return;
    const data = await apiFetch<{ supertag: DocsSupertag }>("/api/docs/supertags", {
      method: "POST",
      body: JSON.stringify({
        name,
        base_type: "note",
        color: "#2563eb",
        icon: "hash",
        config_json: {},
      }),
    });
    setState((current) => ({ ...current, supertags: mergeById(current.supertags, data.supertag) }));
    setNewTagName("");
    if (selectedNode) await applyTagToActionNodes(selectedNode, data.supertag.id);
  };

  const createSupertagTable = async (name: string) => {
    const data = await apiFetch<{ supertag: DocsSupertag }>("/api/docs/supertags", {
      method: "POST",
      body: JSON.stringify({
        name,
        base_type: "record",
        color: "#2563eb",
        icon: "table-2",
        config_json: { default_layout: "table" },
        ai_instructions: `${name}の各nodeを1行として扱い、fieldに構造化して保存する。`,
      }),
    });
    setState((current) => ({
      ...current,
      supertags: mergeById(current.supertags, data.supertag),
    }));
    setMainView(null);
    setTagPageId(data.supertag.id);
    setRightPanel("tags");
    return data.supertag;
  };

  const createField = async (tagId: string, name: string, fieldType: string) => {
    const trimmed = name.trim();
    if (!trimmed) return undefined;
    const data = await apiFetch<{ field: DocsField; supertag_field: DocsState["supertag_fields"][number] }>("/api/docs/fields", {
      method: "POST",
      body: JSON.stringify({
        supertag_id: tagId,
        name: trimmed,
        field_type: fieldType,
        options_json: fieldType === "options" ? { values: ["todo", "doing", "done"] } : {},
      }),
    });
    setState((current) => ({
      ...current,
      fields: mergeById(current.fields, data.field),
      supertag_fields: current.supertag_fields.some((item) => item.supertag_id === tagId && item.field_id === data.field.id)
        ? current.supertag_fields
        : [
            ...current.supertag_fields,
            data.supertag_field,
          ],
    }));
    return data.field;
  };

  const applyTagToActionNodes = async (fallbackNode: DocsNode, tagId: string) => {
    const targets = actionNodes.length > 1 && selectedNodeIdSet.has(fallbackNode.id) ? actionNodes : [fallbackNode];
    for (const node of targets) {
      await applyTag(node, tagId);
    }
  };

  const updateField = async (fieldId: string, patch: Partial<Pick<DocsField, "name" | "field_type" | "required" | "options_json" | "sort_order">> & { default_value_json?: unknown }) => {
    const data = await apiFetch<{ field: DocsField }>(`/api/docs/fields/${fieldId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setState((current) => ({ ...current, fields: current.fields.map((field) => (field.id === data.field.id ? data.field : field)) }));
  };

  const updateSupertag = async (tagId: string, patch: Partial<Pick<DocsSupertag, "name" | "description" | "color" | "icon" | "template_json" | "config_json" | "title_template" | "ai_instructions" | "parent_supertag_id">>) => {
    const data = await apiFetch<{ supertag: DocsSupertag }>(`/api/docs/supertags/${tagId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setState((current) => ({ ...current, supertags: current.supertags.map((tag) => (tag.id === data.supertag.id ? data.supertag : tag)) }));
  };

  const createSavedView = async (
    tag: DocsSupertag,
    draft: Pick<DocsSavedView, "name" | "layout" | "config_json">,
  ) => {
    const data = await apiFetch<{ view: DocsSavedView }>("/api/docs/views", {
      method: "POST",
      body: JSON.stringify({
        supertag_id: tag.id,
        name: draft.name,
        layout: draft.layout,
        config_json: draft.config_json,
      }),
    });
    setState((current) => ({ ...current, views: mergeById(current.views, data.view) }));
    return data.view;
  };

  const updateSavedView = async (
    viewId: string,
    patch: Partial<Pick<DocsSavedView, "name" | "layout" | "config_json" | "sort_order">>,
  ) => {
    const data = await apiFetch<{ view: DocsSavedView }>(`/api/docs/views/${viewId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setState((current) => ({ ...current, views: current.views.map((view) => (view.id === data.view.id ? data.view : view)) }));
    return data.view;
  };

  const createTaggedNode = async (tag: DocsSupertag) => {
    const node = await createNode(focusNode?.id ?? null, null, "");
    await applyTag(node, tag.id);
    openDocsNode(node.id);
    setFocusRequestNodeId(node.id);
    return node;
  };

  const createTableRow = async (tag: DocsSupertag) => {
    const node = await createNode(null, null, "Untitled", { nodeType: "object" });
    await applyTag(node, tag.id);
    return node;
  };

  const mergeChangedFieldValues = (nodeId: string, fieldId: string, fieldValues: DocsFieldValue[]) => {
    setState((current) => ({
      ...current,
      field_values: [
        ...current.field_values.filter(
          (currentValue) => currentValue.node_id !== nodeId || currentValue.field_id !== fieldId,
        ),
        ...fieldValues,
      ],
    }));
  };

  const moveActionNodes = async (fallbackNode: DocsNode, targetParentId: string, leaveReference: boolean) => {
    const targets = actionNodes.length > 1 && selectedNodeIdSet.has(fallbackNode.id) ? actionNodes : [fallbackNode];
    for (const node of targets) {
      await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${node.id}/move`, {
        method: "POST",
        body: JSON.stringify({ new_parent_id: targetParentId, leave_reference: leaveReference }),
      });
    }
    setCommandOpen(false);
    await load();
  };

  const setSearchNodeView = async (node: DocsNode, view: SearchView) => {
    await patchNode(node.id, { view_json: { ...node.view_json, view } });
  };

  const setSearchNodeSort = async (node: DocsNode, sort: SearchSort) => {
    const nextQuery = { ...readConfigRecord(node.query_json) };
    if (sort) {
      nextQuery.sort = sort;
    } else {
      delete nextQuery.sort;
    }
    await patchNode(node.id, { query_json: nextQuery });
  };

  const setSearchNodeQuery = async (node: DocsNode, query: Record<string, unknown>) => {
    await patchNode(node.id, { query_json: query });
  };

  const createBlockNode = (input: BlockCreateInput) => {
    const afterNode = input.afterNodeId ? nodesById.get(input.afterNodeId) ?? null : null;
    const displayPatch = input.kind === "checkbox"
      ? { show_checkbox: true, checked: input.checked === true }
      : {};
    const bodyJson = {
      format: "doc_block",
      block_type: input.kind === "heading_1" || input.kind === "heading_2" || input.kind === "heading_3" || input.kind === "checkbox" || input.kind === "quote"
        ? input.kind
        : "paragraph",
      ...(input.kind === "checkbox" ? { checked: input.checked === true } : {}),
    };
    const created = createNode(input.parentId, afterNode, input.title, {
      bodyJson,
      displayProps: displayPatch,
      nodeType: input.kind === "search" ? "search" : "node",
      optimistic: true,
    }) as DocsNode;
    pushHistory({
      label: "ノード作成",
      patches: [{ type: "create", node: snapshotDocsNode({ ...created, body_json: bodyJson, display_props: { ...created.display_props, ...displayPatch } }) }],
    });
    pendingCreateHistoryIdsRef.current.add(created.id);
    return { ...created, body_json: bodyJson, display_props: { ...created.display_props, ...displayPatch } };
  };

  const moveBlockNode = async (input: BlockMoveInput) => {
    const target = nodesById.get(input.nodeId);
    if (!target) return;
    const parentId = input.parentId;
    const siblings = parentId ? childrenByParent.get(parentId) ?? [] : roots;
    const previous = input.afterNodeId ? nodesById.get(input.afterNodeId) ?? null : null;
    const previousIndex = previous ? siblings.findIndex((node) => node.id === previous.id) : -1;
    const next = previousIndex >= 0 ? siblings[previousIndex + 1] : siblings[0];
    const afterSnapshot: DocsBlockSnapshot = {
      ...snapshotDocsNode(target),
      parent_id: parentId,
      sort_order: midpointSortOrder(previous?.sort_order, next?.sort_order),
    };
    pushHistory({
      label: "ノード移動",
      patches: [{ type: "update", id: target.id, before: snapshotDocsNode(target), after: afterSnapshot }],
    });
    await patchNode(input.nodeId, {
      parent_id: parentId,
      sort_order: afterSnapshot.sort_order,
    });
    await load();
  };

  const createSearchNode = async (tag: DocsSupertag) => {
    if (!focusNode) return;
    const node = await createNode(focusNode.id, currentRows.at(-1)?.node, `List of ${tag.name}`);
    await patchNode(node.id, {
      node_type: "search",
      query_json: { and: [{ tag: tag.id, include_descendants: true }], limit: 100 },
      view_json: { view: "table" },
    });
  };

  // 行の子ノードとして空の検索ノードを作成する（右パネル前提の createSearchNode の親ID指定版）。
  const createSearchNodeForRow = async (row: OutlineEditorRow) => {
    const parent = row.node;
    const node = await createNode(parent.id, (childrenByParent.get(parent.id) ?? []).at(-1) ?? null, "New search");
    await patchNode(node.id, {
      node_type: "search",
      query_json: { and: [], limit: 100 },
      view_json: { view: "table" },
    });
    setCollapsed((current) => {
      const next = new Set(current);
      next.delete(parent.id);
      return next;
    });
    selectSingleNode(node.id);
    setFocusRequestNodeId(node.id);
  };

  // フィールド省略記法: 行の直近の親ノードの付与タグから fieldName 完全一致のフィールドを解決し値を保存、成功後に行を削除する。
  const applyFieldShorthand = async (row: OutlineEditorRow, fieldName: string, rawValue: string): Promise<boolean> => {
    const parentId = row.node.parent_id;
    if (!parentId) return false;
    const parent = nodesById.get(parentId);
    if (!parent) return false;
    const shorthand = `${fieldName}${rawValue ? ` ${rawValue}` : ""}`.trim();
    if (!shorthand) return false;
    const normalized = shorthand.toLowerCase();
    const field = [...fieldsForNode(parent, nodeTags, fieldsByTag)]
      .sort((a, b) => b.name.trim().length - a.name.trim().length)
      .find((item) => {
        const name = item.name.trim().toLowerCase();
        return normalized === name || normalized.startsWith(`${name} `);
      });
    if (!field) return false;
    const resolvedRawValue = shorthand.slice(field.name.trim().length).trimStart();
    try {
      const type = docsFieldType(field);
      let draft = resolvedRawValue;
      if (type === "reference") {
        const match = resolvedRawValue.match(/\[\[node:([0-9a-f-]{36})\|[^\]\n]*\]\]/i);
        draft = match?.[1] ?? resolvedRawValue.trim();
      } else if (type === "date") {
        draft = resolvedRawValue.trim();
      }
      await saveField(parent, field, draft);
      await archiveNode(row.node.id);
      return true;
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "フィールドの保存に失敗しました");
      return false;
    }
  };

  const fieldCandidatesForRow = (row: OutlineEditorRow) => {
    const parent = row.node.parent_id ? nodesById.get(row.node.parent_id) : null;
    return parent ? fieldsForNode(parent, nodeTags, fieldsByTag) : [];
  };

  const createFieldCandidateForRow = async (row: OutlineEditorRow, name: string) => {
    const parent = row.node.parent_id ? nodesById.get(row.node.parent_id) : null;
    if (!parent) return false;
    const tags = nodeTags.get(parent.id) ?? [];
    if (tags.length !== 1) {
      toast.error("Fieldの所属先を一意に決められません。対象ノードにSupertagを1つ設定してください");
      return false;
    }
    const field = await createField(tags[0].id, name, "text");
    return Boolean(field);
  };

  const runDocsAiCommand = async (node: DocsNode, command: DocsAiCommand = "continue", prompt?: string) => {
    const text = command === "generate_minutes"
      ? "/議事録生成"
      : prompt ?? window.prompt("Docs AI prompt", command === "continue" ? nodeText(node) : "") ?? "";
    const data = await apiFetch<DocsAiCommandResult>("/api/ai/docs/command", {
      method: "POST",
      body: JSON.stringify({
        node_id: node.id,
        command,
        prompt: text,
      }),
    });
    const suggestion = data.suggestion;
    if (suggestion) {
      setState((current) => ({
        ...current,
        ai_suggestions: mergeById<DocsAiSuggestion>(current.ai_suggestions, suggestion),
      }));
    }
    const result = data.result;
    if (!result) return;
    if (command === "fill_fields") {
      toast.success(result.summary ?? "AIフィールド候補を保存しました");
      return;
    }
    if (
      (result.mode === "replace_title" && typeof result.replacement === "string") ||
      (result.mode === "insert_children" && Array.isArray(result.lines))
    ) {
      setAiPreview({
        node,
        command,
        suggestionId: suggestion?.id,
        result,
      });
      return;
    }
    toast.success(result.summary ?? "AI候補を保存しました");
  };

  const applyDocsAiPreview = async () => {
    if (!aiPreview) return;
    const { node, command, suggestionId, result } = aiPreview;
    if (result.mode === "replace_title" && typeof result.replacement === "string") {
      await patchNode(node.id, { title: result.replacement });
    } else if (result.mode === "insert_children" && Array.isArray(result.lines)) {
      const minutesTag = state.supertags.find((tag) => tag.system_key === "meeting_minutes");
      // generate_minutes は結果を専用の親ノード配下へ集約し、対象ノード直下を汚さない。
      const targetParent = command === "generate_minutes"
        ? await createNode(
            node.id,
            (childrenByParent.get(node.id) ?? []).at(-1) ?? null,
            `${nodeText(node)} 議事録`,
          )
        : node;
      if (command === "generate_minutes" && minutesTag) await applyTag(targetParent, minutesTag.id);
      let afterNode: DocsNode | null = (childrenByParent.get(targetParent.id) ?? []).at(-1) ?? null;
      for (const line of result.lines) {
        const rawLine = String(line);
        const taskTag = state.supertags.find((tag) => tag.system_key === "task");
        const decisionTag = state.supertags.find((tag) => tag.system_key === "decision");
        const shouldBindTask = command === "extract_tasks" || /#(?:Task|タスク)(?=\s|$|[.,;:!?、。])/i.test(rawLine);
        const shouldTagDecision = /#(?:Decision|決定)(?=\s|$|[.,;:!?、。])/i.test(rawLine);
        const created = await createNode(targetParent.id, afterNode, shouldBindTask || shouldTagDecision ? titleWithoutTagTokens(rawLine) : rawLine);
        if (shouldBindTask && taskTag) await applyTag(created, taskTag.id);
        if (shouldTagDecision && decisionTag) await applyTag(created, decisionTag.id);
        afterNode = created;
      }
      setCollapsed((current) => {
        const next = new Set(current);
        next.delete(node.id);
        next.delete(targetParent.id);
        return next;
      });
    }
    if (suggestionId) await updateSuggestionStatus(suggestionId, "accepted");
    setAiPreview(null);
    toast.success(result.summary ?? "AI候補を反映しました");
  };

  const rejectDocsAiPreview = async () => {
    if (aiPreview?.suggestionId) await updateSuggestionStatus(aiPreview.suggestionId, "rejected");
    setAiPreview(null);
  };

  const handleWorkspaceKeyDownCapture = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
    // CodeMirror owns character-level history while a line is being edited.
    // Workspace history resumes after the line editor closes.
    if ((event.target as HTMLElement | null)?.closest(".cm-editor, input, textarea, select, [contenteditable='true']")) return;
    const key = event.key.toLowerCase();
    const targetEditor = (event.target as HTMLElement | null)?.closest<HTMLElement>("[data-testid='docs-block-editor']") ?? null;
    const targetEditorNodeId = targetEditor?.dataset.docsEditorNodeId ?? null;
    const preserveEditorFocus = (operation: Promise<void>) => {
      void operation.finally(() => {
        const nextTarget = targetEditor?.isConnected
          ? targetEditor
          : Array.from(document.querySelectorAll<HTMLElement>("[data-docs-editor-node-id]"))
              .find((item) => item.dataset.docsEditorNodeId === targetEditorNodeId) ?? null;
        nextTarget?.focus();
      });
    };
    if ((event.ctrlKey || event.metaKey) && key === "z") {
      event.preventDefault();
      event.stopPropagation();
      if (event.shiftKey) {
        preserveEditorFocus(redoDocsOperation());
      } else {
        preserveEditorFocus(undoDocsOperation());
      }
      return;
    }
    if ((event.ctrlKey || event.metaKey) && key === "y") {
      event.preventDefault();
      event.stopPropagation();
      preserveEditorFocus(redoDocsOperation());
      return;
    }
    if (!event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
    const nodeId = (event.target as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
    if (!nodeId) return;
    event.preventDefault();
    event.stopPropagation();
    selectDomRangeById(nodeId, event.key === "ArrowUp" ? -1 : 1);
  };

  const handleWorkspaceKeyUpCapture = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (!event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
    const nodeId = (event.target as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
    if (!nodeId) return;
    event.stopPropagation();
    selectDomRangeById(nodeId);
  };

  // OutlineBlockEditor へ渡す安定コールバック群を 1 箇所に束ねて Context 供給する。
  // ここに含めるのは現在ページ（renderPanel の node 引数）に依存しないハンドラのみで、
  // node 依存クロージャ（emptyParentId / onLoadMoreRows / onNavigateToDocumentTitle /
  // renderBelowRow）とデータ・query predicate は従来どおり props で渡す（挙動不変）。
  const docsEditorContextValue = useMemo<DocsEditorContextValue>(() => ({
    onSelectNode: (nodeId) => {
      if (!preserveSelectionOnNextFocusRef.current) selectSingleNode(nodeId);
    },
    onOpenNode: (nodeId) => {
      openDocsNode(nodeId);
    },
    onOpenTask: setTaskModalId,
    onFocused: (nodeId) => {
      if (nodeId && preserveSelectionOnNextFocusRef.current) {
        preserveSelectionOnNextFocusRef.current = false;
        setSelectedNodeIds(selectedNodeIdsRef.current);
        setSelectionAnchorNodeId(selectionAnchorNodeIdRef.current);
        setSelectedNodeId(nodeId);
      }
      setFocusRequestNodeId(null);
    },
    onCommitPending: (operation) => {
      editorCommitInFlightRef.current = operation;
    },
    onCommitTitle: async (target, title, patch) => {
      const beforeSnapshot = snapshotDocsNode(target);
      if (patch) {
        const afterNode = { ...target, ...patch, title };
        if (!foldIntoPendingCreateHistory(afterNode)) {
          pushHistory({
            label: "ノード更新",
            patches: [{ type: "update", id: target.id, before: beforeSnapshot, after: snapshotDocsNode(afterNode) }],
          });
        }
        setState((current) => ({
          ...current,
          nodes: current.nodes.map((item) => (
            item.id === target.id
              ? { ...item, ...patch, title }
              : item
          )),
        }));
        await patchNode(target.id, { ...patch, title });
        return;
      }
      const afterNode = { ...target, title, body_text: title };
      const foldedCreate = foldIntoPendingCreateHistory(afterNode);
      if (target.title !== title) {
        if (!foldedCreate) {
          pushHistory({
            label: "タイトル更新",
            patches: [{
              type: "update",
              id: target.id,
              before: beforeSnapshot,
              after: snapshotDocsNode(afterNode),
              }],
          });
        }
        // 保存失敗や画面遷移があっても、入力済みのタイトルを画面から失わない。
        setState((current) => ({
          ...current,
          nodes: current.nodes.map((item) => (
            item.id === target.id
              ? { ...item, title, body_text: title }
              : item
          )),
        }));
      }
      await commitTitle(target, title);
    },
    onDraftChange: (target, title) => {
      latestEditorDraftRef.current.set(target.id, title);
      foldIntoPendingCreateHistory({ ...target, title, body_text: title });
    },
    onCommitSuccess: (nodeId, committedDraft) => {
      if (latestEditorDraftRef.current.get(nodeId) === committedDraft) {
        latestEditorDraftRef.current.delete(nodeId);
      }
      pendingCreateHistoryIdsRef.current.delete(nodeId);
    },
    onCreateNode: createBlockNode,
    onArchiveNode: (target) => {
      pushHistory({
        label: "ノード削除",
        patches: [{ type: "archive", node: snapshotDocsNode(target) }],
      });
      void archiveNode(target.id);
    },
    onMoveNode: moveBlockNode,
    onToggleCheckbox: (target) => void toggleNodeCheckbox(target),
    onToggleCollapsed: toggleCollapsed,
    onDuplicateNode: (target) => void duplicateSidebarNode(target),
    onApplyTag: (target, tag) => void applyTag(target, tag.id),
    onRemoveTag: (target, tag) => void removeTag(target, tag.id),
    onOpenTag: (tag) => {
      setPropertiesTagId(tag.id);
      setRightPanel("tags");
      setPropertiesOpen(true);
    },
    onSaveField: (target, field, value) => void saveField(target, field, value),
    onDeleteAttachment: deleteAttachment,
    onMoveToPage: async (target, page) => {
      await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${target.id}/move`, {
        method: "POST",
        body: JSON.stringify({ new_parent_id: page.id, leave_reference: false }),
      });
      await load({ nodeId: focusNodeIdRef.current ?? undefined });
      toast.success(`「${page.title}」へ移動しました`);
    },
    onReplaceTitles: (updates) => void replaceNodeTitles(updates),
    onFieldShorthand: (row, fieldName, rawValue) => applyFieldShorthand(row, fieldName, rawValue),
    onOpenAliasEditor: (row) => setAliasEditorNode(row.node),
    onCreateSearchNode: (row) => void createSearchNodeForRow(row),
    onSuggestFields: (row) => void runDocsAiCommand(row.node, "fill_fields"),
    onCreateFieldCandidate: createFieldCandidateForRow,
    onSuggestionStatus: updateSuggestionStatus,
  }), [
    apiFetch,
    applyFieldShorthand,
    applyTag,
    archiveNode,
    commitTitle,
    createBlockNode,
    createFieldCandidateForRow,
    createSearchNodeForRow,
    deleteAttachment,
    duplicateSidebarNode,
    foldIntoPendingCreateHistory,
    load,
    moveBlockNode,
    openDocsNode,
    patchNode,
    pushHistory,
    removeTag,
    replaceNodeTitles,
    runDocsAiCommand,
    saveField,
    selectSingleNode,
    toggleCollapsed,
    toggleNodeCheckbox,
    updateSuggestionStatus,
    // 以下はフックから受け取る安定参照（恒等が保たれるため再計算契機にはならない）
    preserveSelectionOnNextFocusRef,
    selectedNodeIdsRef,
    selectionAnchorNodeIdRef,
    setSelectedNodeId,
    setSelectedNodeIds,
    setSelectionAnchorNodeId,
  ]);

  const renderPanel = (node: DocsNode | null, rows: Array<{ node: DocsNode; depth: number }>, compact = false) => {
    const panelBreadcrumb = buildBreadcrumb(node, nodesById);
    const outlineEditorRows: OutlineEditorRow[] = rows.map((row) => ({
      ...row,
      checked: checkedForNode(row.node),
      tags: nodeTags.get(row.node.id) ?? [],
      fields: fieldsForNode(row.node, nodeTags, fieldsByTag),
      fieldValues: fieldValuesByNodeId.get(row.node.id) ?? [],
      attachments: attachmentsByNodeId.get(row.node.id) ?? [],
      taskBinding: taskBindingsByNodeId.get(row.node.id) ?? null,
    }));
    const documentEditorRow: OutlineEditorRow | null = node ? {
      node,
      depth: 0,
      checked: checkedForNode(node),
      tags: nodeTags.get(node.id) ?? [],
      fields: fieldsForNode(node, nodeTags, fieldsByTag),
      fieldValues: fieldValuesByNodeId.get(node.id) ?? [],
      attachments: attachmentsByNodeId.get(node.id) ?? [],
      taskBinding: taskBindingsByNodeId.get(node.id) ?? null,
    } : null;
    return (
    <section className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="border-b px-5 py-3">
        <div className="mx-auto w-[94%] max-w-[160rem] lg:w-[55%]">
        <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
          {panelBreadcrumb.map((item) => (
            <button key={item.id} type="button" className="truncate hover:text-foreground" onClick={() => openDocsNode(item.id)}>
              {nodeText(item)}
            </button>
          ))}
        </div>
        {node ? (
          <div className="mt-2 flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <PageTitleEditor
                node={node}
                tags={nodeTags.get(node.id) ?? []}
                requestFocus={focusRequestNodeId === node.id}
                onFocused={() => setFocusRequestNodeId(null)}
                onChangeTitle={(title) => setState((current) => ({ ...current, nodes: current.nodes.map((item) => (item.id === node.id ? { ...item, title } : item)) }))}
                onCommitTitle={(title) => void commitTitle(node, title)}
                onRemoveTag={(tag) => void removeTag(node, tag.id)}
                onOpenTag={(tag) => {
                  setPropertiesTagId(tag.id);
                  setRightPanel("tags");
                  setPropertiesOpen(true);
                }}
                onNavigateDown={() => {
                  if (outlineEditorRows[0]) setFocusRequestNodeId(outlineEditorRows[0].node.id);
                }}
              />
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                {node.node_type === "day" && <span className="rounded border px-2 py-0.5 text-xs text-sky-300">Daily</span>}
                <TaskBindingButton task={taskBindingsByNodeId.get(node.id) ?? null} onOpenTask={setTaskModalId} />
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-xs"
                  onClick={() => setAliasEditorNode(node)}
                >
                  エイリアス
                  {(node.aliases?.length ?? 0) > 0 ? ` ${node.aliases.length}` : ""}
                </Button>
              </div>
            </div>
            {!compact && (
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="icon-sm"
                  title="Supertag・Field設定"
                  aria-label="Supertag・Field設定"
                  onClick={() => {
                    setPropertiesTagId(null);
                    setRightPanel("tags");
                    setPropertiesOpen(true);
                  }}
                >
                  <Settings2 className="size-4" />
                </Button>
                <Button variant="ghost" size="icon-sm" title="分割表示" onClick={() => setSplitNodeId(node.id)}>
                  <Columns2 className="size-4" />
                </Button>
              </div>
            )}
          </div>
        ) : null}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-4 py-3" data-docs-scroll-container>
        {node ? (
          <>
            <div data-docs-editor-surface className="mx-auto mt-2 w-[94%] max-w-[160rem] space-y-0 overflow-x-auto lg:w-[55%]">
              <DocsEditorProvider value={docsEditorContextValue}>
              <OutlineBlockEditor
                rows={outlineEditorRows}
                documentRow={documentEditorRow}
                emptyParentId={node.id}
                hasMoreRows={Boolean(state.children_next_cursor_by_parent?.[node.id])}
                onLoadMoreRows={() => {
                  const cursor = state.children_next_cursor_by_parent?.[node.id];
                  return cursor ? loadNodeChildren(node.id, cursor) : Promise.resolve(null);
                }}
                selectedNodeIds={selectedNodeIdSet}
                requestFocusNodeId={focusRequestNodeId}
                nodes={state.nodes}
                projects={projects}
                supertags={state.supertags}
                users={mentionUsers}
                suggestions={state.ai_suggestions}
                onNavigateToDocumentTitle={() => setFocusRequestNodeId(node.id)}
                isCollapsed={nodeIsCollapsed}
                nodeHasChildren={nodeHasExpandableContent}
                isNodeLoading={(nodeId) => loadingNodeIds.has(nodeId)}
                fieldCandidatesForRow={fieldCandidatesForRow}
                renderBelowRow={(row) =>
                  row.node.node_type === "search" ? (
                    <SearchNodeResults
                      apiFetch={apiFetch}
                      node={row.node}
                      depth={0}
                      // Document search results always arrive from /api/docs/query; avoid passing the broader
                      // loaded workspace snapshot to each collapsed search block.
                      nodes={[]}
                      nodeSupertags={state.node_supertags}
                      tags={state.supertags}
                      fields={state.fields}
                      fieldValues={state.field_values}
                      projects={projects}
                      fieldsByTag={fieldsByTag}
                      allSupertagFields={state.supertag_fields}
                      context="document"
                      onSetView={(view) => void setSearchNodeView(row.node, view)}
                      onSetSort={(sort) => void setSearchNodeSort(row.node, sort)}
                      onSetQuery={(query) => void setSearchNodeQuery(row.node, query)}
                      onOpenNode={openDocsNode}
                      onFieldValuesChanged={mergeChangedFieldValues}
                    />
                  ) : null
                }
              />
              </DocsEditorProvider>
              {!compact ? (
                <ZoomReferences
                  references={pageReferences}
                  loading={pageReferencesLoading}
                  onOpenNode={openDocsNode}
                />
              ) : null}
            </div>
          </>
        ) : (
          <div className="mx-auto w-[94%] max-w-[160rem] p-8 text-sm text-muted-foreground lg:w-[55%]">ノードを選択してください。</div>
        )}
      </div>
    </section>
    );
  };

  const docsSidebarSlot = useSyncExternalStore(
    subscribeDocsSidebarSlot,
    getDocsSidebarSlotSnapshot,
    () => null,
  );
  const renderDocsSidebar = (portal: boolean) => (
    <aside
      className={cn(
        "flex shrink-0 flex-col",
        portal ? "h-full min-h-0 w-full min-w-0" : "w-72 border-r",
      )}
    >
      <div className="border-b px-3 py-2">
        <select
          value={docsSource}
          onChange={(event) => {
            setDocsSource(event.target.value);
            setMainView(null);
            setTagPageId(null);
          }}
          className="h-8 w-full rounded border bg-background px-2 text-xs"
          aria-label="Docsデータソース"
        >
          <option value="local">Docs</option>
          {remoteProfiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              [EP] {profile.name}
            </option>
          ))}
        </select>
        {docsReadOnly && <div className="mt-1 text-[10px] text-amber-600">Enterprise Docs（読み取り専用）</div>}
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-2 py-2">
        <SidebarButton icon={CalendarDays} label="Today" active={!mainView && !tagPageId && focusNode?.node_type === "day"} onClick={() => openToday()} />
        <SidebarButton
          icon={Tags}
          label="Supertags"
          active={tagPageId === SUPERTAGS_OVERVIEW_ID}
          onClick={() => {
            setMainView(null);
            setTagPageId(SUPERTAGS_OVERVIEW_ID);
            setRightPanel("tags");
          }}
        />
        <SidebarButton
          icon={ListFilter}
          label="Search nodes"
          active={mainView === "search"}
          onClick={() => {
            setTagPageId(null);
            setMainView("search");
          }}
        />
        <SidebarButton
          icon={Archive}
          label="ゴミ箱"
          active={mainView === "trash"}
          onClick={() => {
            setTagPageId(null);
            setMainView("trash");
          }}
        />
        <div className="mt-4 flex items-center justify-between px-2 text-xs font-medium text-muted-foreground">
          <span>Pages</span>
          <button
            type="button"
            className="grid size-6 place-items-center rounded hover:bg-accent hover:text-foreground"
            title="ルートノートを作成"
            aria-label="ルートノートを作成"
            onClick={() => void createRootNode()}
          >
            <Plus className="size-4" />
          </button>
        </div>
        <div className="mt-1 space-y-0.5">
          {roots.map((node) => (
            <DocsSidebarNode
              key={`${node.id}:${node.parent_id ?? "root"}:${node.sort_order}`}
              node={node}
              depth={0}
              focusNodeId={mainView || tagPageId ? null : focusNodeId}
              selectedNodeId={selectedNodeId}
              selectedNodeIds={selectedNodeIds}
              dragNodeId={dragSidebarNodeId}
              childrenByParent={childrenByParent}
              nodeHasChildren={nodeHasChildren}
              collapsed={sidebarCollapsed}
              onToggle={toggleSidebarCollapsed}
              onOpen={openSidebarNode}
              onContextMenu={openSidebarNodeContextMenu}
              onDragStart={(nodeId) => setDragSidebarNodeId(nodeId)}
              onDropOnNode={(node) => void dropSidebarNode(node)}
            />
          ))}
        </div>
      </div>
    </aside>
  );
  const docsSidebar = renderDocsSidebar(Boolean(docsSidebarSlot));

  if (loading) {
    return (
      <div className="space-y-3 p-6">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-[70vh] w-full" />
      </div>
    );
  }

  return (
    <div
      className="flex h-[calc(100vh-96px)] min-h-[640px] overflow-hidden border-t bg-background"
      onKeyDownCapture={handleWorkspaceKeyDownCapture}
      onKeyUpCapture={handleWorkspaceKeyUpCapture}
    >
      {docsSidebarSlot ? createPortal(docsSidebar, docsSidebarSlot) : docsSidebar}
      <main className="flex min-w-0 flex-1 overflow-hidden">
        {tagPageId ? (
          <SupertagPage
            apiFetch={apiFetch}
            tag={activeTagPage}
            tags={state.supertags}
            views={state.views}
            nodes={state.nodes}
            nodeSupertags={state.node_supertags}
            fields={state.fields}
            fieldValues={state.field_values}
            projects={state.projects}
            fieldsByTag={fieldsByTag}
            allSupertagFields={state.supertag_fields}
            onDocument={() => setTagPageId(null)}
            onOpenTag={(tagId) => {
              setTagPageId(tagId);
              setRightPanel("tags");
            }}
            onOpenNode={openDocsNode}
            onCreateTaggedNode={createTaggedNode}
            onCreateTableRow={createTableRow}
            onCreateTable={createSupertagTable}
            onFieldValuesChanged={mergeChangedFieldValues}
            onCreateView={createSavedView}
            onUpdateView={updateSavedView}
          />
        ) : mainView === "trash" ? (
          <TrashMainView
            archivedNodes={archivedNodes}
            onRestoreNode={(nodeId) => void restoreNode(nodeId)}
            onPermanentDeleteNode={(nodeId) => void permanentlyDeleteNode(nodeId)}
          />
        ) : mainView === "search" ? (
          <SearchNodesMainView
            tags={state.supertags}
            searchNodes={state.nodes.filter((node) => node.node_type === "search")}
            onCreateSearchNode={(tag) => void createSearchNode(tag)}
            onOpenNode={(nodeId) => {
              openDocsNode(nodeId);
            }}
          />
        ) : (
          renderPanel(focusNode, currentRows)
        )}
        {!tagPageId && !mainView && splitNode ? <div className="hidden min-w-0 flex-1 border-l xl:flex">{renderPanel(splitNode, splitRows, true)}</div> : null}
      </main>
      <DocsCommandPalette
        open={commandOpen}
        onOpenChange={(open) => {
          setCommandOpen(open);
          if (!open) setCommandMode({ kind: "root" });
        }}
        mode={commandMode}
        setMode={setCommandMode}
        selectedNode={selectedNode}
        selectionCount={actionNodes.length}
        tags={state.supertags}
        fields={commandFields}
        nodeTools={commandNodeTools}
        moveTargets={commandMoveTargets}
        onAddChild={(node) => void createNode(node.id, null)}
        onOpenSplit={(node) => setSplitNodeId(node.id)}
        onToggleCheckbox={(node) => void toggleNodeCheckbox(node)}
        onApplyTag={(node, tag) => void applyTagToActionNodes(node, tag.id)}
        onMove={(node, target, leaveReference) => void moveActionNodes(node, target.id, leaveReference)}
        onSetView={(node, view) => void setSearchNodeView(node, view)}
        onSetField={(node, field, value) => void saveField(node, field, value)}
        onRunAi={(node, command) => void runDocsAiCommand(node, command)}
        onGoBack={(node) => {
          if (!node.parent_id) return;
          openDocsNode(node.parent_id);
        }}
      />
      <Sheet open={propertiesOpen} onOpenChange={setPropertiesOpen}>
        <SheetContent side="right" className="w-[min(92vw,34rem)] overflow-auto sm:max-w-[34rem]">
          <SheetHeader>
            <SheetTitle>Docs設定</SheetTitle>
          </SheetHeader>
          <RightPanel
            mode={rightPanel}
            selectedNode={selectedNode}
            selectedTag={propertiesTagId ? tagById.get(propertiesTagId) ?? null : null}
            tags={state.supertags}
            nodeTags={selectedNode ? nodeTags.get(selectedNode.id) ?? [] : []}
            fields={state.fields}
            fieldsByTag={fieldsByTag}
            newTagName={newTagName}
            setNewTagName={setNewTagName}
            onApplyTag={(tagId) => selectedNode && void applyTagToActionNodes(selectedNode, tagId)}
            onOpenTag={(tagId) => {
              setPropertiesTagId(tagId);
              setRightPanel("tags");
            }}
            onCreateTag={() => void createTag()}
            onCreateField={(tagId, name, fieldType) => void createField(tagId, name, fieldType)}
            onUpdateSupertag={(tagId, patch) => void updateSupertag(tagId, patch)}
            onUpdateField={(fieldId, patch) => void updateField(fieldId, patch)}
            onCreateSearchNode={(tag) => void createSearchNode(tag)}
            relatedNodes={relatedNodes}
            onOpenNode={(nodeId) => {
              setPropertiesOpen(false);
              openDocsNode(nodeId);
            }}
          />
        </SheetContent>
      </Sheet>
      <DocsSidebarContextMenu
        menu={sidebarContextMenu}
        node={sidebarContextNode}
        onClose={() => setSidebarContextMenu(null)}
        onOpen={(node) => {
          openSidebarNode(node);
          setSidebarContextMenu(null);
        }}
        onOpenSplit={(node) => {
          setSplitNodeId(node.id);
          selectSingleNode(node.id);
          setSidebarContextMenu(null);
        }}
        onRename={(node) => void renameSidebarNode(node)}
        onDuplicate={(node) => void duplicateSidebarNode(node)}
        onMoveWithReference={moveSidebarNodeWithReference}
        onExport={(node) => void exportSidebarNode(node)}
        onPin={(node) => void pinSidebarNode(node)}
        onArchive={(node) => void archiveSidebarNode(node)}
      />
      <DocsAiPreviewDialog
        preview={aiPreview}
        onApply={() => void applyDocsAiPreview()}
        onReject={() => void rejectDocsAiPreview()}
        onOpenChange={(open) => {
          if (!open) void rejectDocsAiPreview();
        }}
      />
      <AliasEditorDialog
        node={aliasEditorNode}
        onOpenChange={(open) => {
          if (!open) setAliasEditorNode(null);
        }}
        onSave={async (node, aliases) => {
          await patchNode(node.id, { aliases });
          setAliasEditorNode(null);
        }}
      />
      <TaskDetailModal
        taskId={taskModalId}
        open={Boolean(taskModalId)}
        onOpenChange={(open) => {
          if (!open) setTaskModalId(null);
        }}
        onTaskUpdated={() => {
          setTaskBindingsByNodeId(new Map());
          void load({ focusToday: false });
        }}
      />
    </div>
  );
}

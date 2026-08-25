"use client";

import { AppSelect } from "@/components/ui/app-select";
import {
  ReadOnlyBadge,
  StatusBadge,
  StatusNote,
} from "@/components/ui/semantic-status";

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
import { usePathname } from "next/navigation";
import {
  createPortal,
  flushSync,
} from "react-dom";
import {
  Archive,
  CalendarDays,
  Columns2,
  FileDown,
  History,
  ListTree,
  ListFilter,
  Plus,
  Share2,
  Settings2,
  Table2,
  Tags,
  X,
} from "lucide-react";
import {
  toast,
} from "sonner";
import {
  EditorView,
} from "@codemirror/view";
import {
  Button,
} from "@/components/ui/button";
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
  blankParagraphBodyJson,
  blockJsonForKind,
  clearBlankParagraphMarker,
  invertHistoryEntry,
  hasMeaningfulBlockTitle,
  isExplicitBlankParagraph,
  midpointSortOrder,
  sortNodesByPosition,
  type BlockHistoryEntry,
  type DocsBlockSnapshot,
} from "@/lib/docs-block-model";
import {
  expandArchivedNodeIds,
  resolveActionNodeIds,
  resolveArchiveTargets,
  resolveFocusAfterArchive,
  shouldArchiveSelectionFromKeyboard,
} from "@/lib/docs-archive-targets";
import {
  DocsSaveQueue,
} from "@/lib/docs-save-queue";
import {
  cn,
} from "@/lib/utils";
import {
  createDocsNodeWikilink,
} from "@/lib/docs-references";
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
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
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
  DOCS_BOOTSTRAP_CACHE_PREFIX,
  readCachedSnapshot,
} from "@/lib/persistent-cache";
import {
  apiFetch as rawApiFetch,
  buildBreadcrumb,
  docsFieldType,
  fieldDraftToPayload,
  fieldValueToDraft,
  projectsFromContext,
} from "./docs-utils";
import { getImageFiles } from "@/lib/editor-image-files";
import {
  EXPANDED_KEY,
  EXPANDED_RESTORE_LIMIT,
  DOCS_SIDEBAR_SLOT_ID,
  DOCS_WORKSPACE_UNMOUNTED_MESSAGE,
  SUPERTAGS_OVERVIEW_ID,
  buildOutlineChildren,
  fieldsForNode,
  hoistedVisibleChildren,
  isDocsNodeTitleVisible,
  isLegacyEmailEmptyLineNode,
  isLegacyEmailOutlineCandidate,
  suppressLegacyEmailOutlineRows,
  getDocsSidebarSlotSnapshot,
  mergeById,
  normalizeSearchQuery,
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
} from "./docs-dialogs";
import {
  PageTitleEditor,
  TaskBindingButton,
  ZoomReferences,
} from "./docs-page-editor";
import {
  DocsNodeContextMenu,
  type DocsNodeContextMenuPosition,
} from "./docs-node-context-menu";
import {
  SearchNodeResults,
  SearchNodeMetadata,
} from "./docs-search-panels";
import {
  DocsChildrenTable,
} from "./docs-children-table";
import {
  DocsSidebarContextMenu,
  DocsSidebarNode,
  isDocsSidebarNodeVisible,
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
import { DocsShareDialog } from "./docs-share-dialog";
import { useDocsClipIngest } from "./docs-clip-ingest-provider";
import { DocsClipIngestProvenanceField } from "./docs-clip-ingest-provenance-field";
import {
  useRegisterDocsCommand,
  requestDocsCommand,
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
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import {
  clearCanonicalDocsState,
  configureDocsNavigationScope,
  createDocsNavigationOwner,
  fetchDocsBootstrap,
  abandonDocsBootstrapInFlight,
  publishCanonicalDocsState,
} from "./docs-navigation-store";

type DocsBootstrapCacheEntry = {
  data: DocsState;
  etag: string | null;
};

function isDocsBootstrapCacheEntry(value: unknown): value is DocsBootstrapCacheEntry {
  if (!value || typeof value !== "object") return false;
  const entry = value as Partial<DocsBootstrapCacheEntry>;
  return Boolean(entry.data && Array.isArray(entry.data.nodes));
}

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

// Optimistic rollback and an in-flight lazy response can briefly overlap. Be
// defensive at the state boundary so one malformed/late array member cannot
// crash the entire Docs editor while the pending row is being restored.
function validDocsNodes(nodes: DocsNode[]): DocsNode[] {
  return nodes.filter((node): node is DocsNode => Boolean(node && typeof node.id === "string"));
}

type DocsLoadStatus = "success" | "superseded" | "failed";

export const DOCS_OPEN_NODE_TIMEOUT_MS = 10_000;
export const DOCS_OPEN_NODE_FAILED_MESSAGE = "対象のDocsノードを開けませんでした";

let docsOpenNodeTimeoutMs = DOCS_OPEN_NODE_TIMEOUT_MS;

export function setDocsOpenNodeTimeoutMsForTests(timeoutMs: number | null) {
  docsOpenNodeTimeoutMs = timeoutMs ?? DOCS_OPEN_NODE_TIMEOUT_MS;
}

async function fetchDocsPathWithTimeout<T>(
  apiFetch: <TValue>(path: string, init?: RequestInit) => Promise<TValue>,
  path: string,
  timeoutMs: number,
): Promise<T> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await Promise.race([
      apiFetch<T>(path, { signal: controller.signal }),
      new Promise<never>((_, reject) => {
        const fail = () => {
          reject(
            controller.signal.reason
              ?? new DOMException("The operation was aborted.", "AbortError"),
          );
        };
        if (controller.signal.aborted) {
          fail();
          return;
        }
        controller.signal.addEventListener("abort", fail, { once: true });
      }),
    ]);
  } finally {
    clearTimeout(timeoutId);
  }
}

async function settleWithTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
): Promise<PromiseSettledResult<T>> {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  try {
    const value = await Promise.race([
      promise,
      new Promise<never>((_, reject) => {
        timeoutId = setTimeout(() => reject(new Error("timeout")), timeoutMs);
      }),
    ]);
    return { status: "fulfilled", value };
  } catch (reason) {
    return { status: "rejected", reason };
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  }
}

function settledValue<T>(result: PromiseSettledResult<T> | null | undefined): T | null {
  return result && result.status === "fulfilled" ? result.value : null;
}

function docsLibraryIdForState(value: Partial<DocsState> | null | undefined): string | null {
  const fromLibrary = value?.library?.docs_library_id ?? value?.library?.id;
  if (typeof fromLibrary === "string" && fromLibrary.trim()) return fromLibrary.trim();
  const fromPayload = value?.docs_library_id;
  if (typeof fromPayload === "string" && fromPayload.trim()) return fromPayload.trim();
  const nodeIds = new Set(
    (value?.nodes ?? [])
      .map((node) => node.docs_library_id)
      .filter((id): id is string => typeof id === "string" && id.trim().length > 0),
  );
  return nodeIds.size === 1 ? Array.from(nodeIds)[0] : null;
}

/**
 * Keep a Docs snapshot single-library.  Bootstrap is intentionally actor
 * scoped, while a direct `/docs/<node>` request may resolve to a readable
 * Project owner's Personal Library.  Filtering every relation together with
 * the nodes prevents a personal Home snapshot from leaking into that view.
 */
export function rebaseDocsStateToLibrary(
  state: DocsState,
  docsLibraryId: string,
  library?: DocsState["library"],
): DocsState {
  const targetLibraryId = docsLibraryId.trim();
  if (!targetLibraryId) return state;
  const nodes = state.nodes.filter((node) => node.docs_library_id === targetLibraryId);
  const nodeIds = new Set(nodes.map((node) => node.id));
  const supertags = state.supertags.filter((tag) => tag.docs_library_id === targetLibraryId);
  const supertagIds = new Set(supertags.map((tag) => tag.id));
  const fields = state.fields.filter((field) => field.docs_library_id === targetLibraryId);
  const fieldIds = new Set(fields.map((field) => field.id));
  const projectIds = new Set(
    nodes
      .map((node) => node.project_id)
      .filter((projectId): projectId is string => Boolean(projectId)),
  );
  const suppliedLibraryMatchesScope = library
    && docsLibraryIdForState({ library }) === targetLibraryId;
  const retainedLibrary = suppliedLibraryMatchesScope
    ? library
    : state.library && docsLibraryIdForState(state) === targetLibraryId
      ? state.library
      : {
          id: targetLibraryId,
          docs_library_id: targetLibraryId,
          library_type: "project",
        };
  const retainedNodeIds = (ids: string[] | undefined) => (ids ?? []).filter((id) => nodeIds.has(id));
  return {
    ...state,
    library: retainedLibrary,
    docs_library_id: targetLibraryId,
    nodes,
    supertags,
    fields,
    node_supertags: state.node_supertags.filter((relation) => nodeIds.has(relation.node_id) && supertagIds.has(relation.supertag_id)),
    supertag_fields: state.supertag_fields.filter((relation) => supertagIds.has(relation.supertag_id) && fieldIds.has(relation.field_id)),
    placements: state.placements.filter((placement) => nodeIds.has(placement.node_id) && nodeIds.has(placement.parent_node_id)),
    field_values: state.field_values.filter((value) => nodeIds.has(value.node_id) && fieldIds.has(value.field_id)),
    attachments: state.attachments.filter((attachment) => nodeIds.has(attachment.node_id)),
    views: state.views.filter((view) => view.docs_library_id === targetLibraryId),
    ai_suggestions: state.ai_suggestions.filter((suggestion) => suggestion.docs_library_id === targetLibraryId && (!suggestion.node_id || nodeIds.has(suggestion.node_id))),
    projects: state.projects.filter((project) => projectIds.has(project.id)),
    loaded_children_parent_ids: retainedNodeIds(state.loaded_children_parent_ids),
    details_loaded_ids: retainedNodeIds(state.details_loaded_ids),
    has_children_ids: retainedNodeIds(state.has_children_ids),
    has_details_ids: retainedNodeIds(state.has_details_ids),
    children_next_cursor_by_parent: Object.fromEntries(
      Object.entries(state.children_next_cursor_by_parent ?? {}).filter(([parentId]) => nodeIds.has(parentId)),
    ),
    child_count_by_parent: Object.fromEntries(
      Object.entries(state.child_count_by_parent ?? {}).filter(([parentId]) => nodeIds.has(parentId)),
    ),
  };
}

/**
 * Supertag/Field/View definitions are owned by the actor's personal Docs
 * Library.  A readable Project node may carry definitions from another
 * library; those definitions can be rendered, but must never be edited from
 * the personal UI.
 */
export function canEditDocsDefinitions(
  selectedNode: Pick<DocsNode, "docs_library_id"> | null | undefined,
  selectedNodeCanWrite: boolean,
  actorOwnedDocsLibraryId: string | null,
): boolean {
  return Boolean(
    selectedNodeCanWrite
      && selectedNode
      && actorOwnedDocsLibraryId
      && selectedNode.docs_library_id === actorOwnedDocsLibraryId,
  );
}

function mergeByKey<T>(current: T[], next: T[], keyFor: (item: T) => string) {
  const merged = new Map(current.map((item) => [keyFor(item), item]));
  for (const item of next) merged.set(keyFor(item), item);
  return Array.from(merged.values());
}

export function mergeLoadedDocsState(current: DocsState, incoming: DocsNeighborhoodResponse): DocsState {
  const incomingLibraryId = docsLibraryIdForState(incoming);
  const currentLibraryId = docsLibraryIdForState(current);
  // A lazy tree/children response can be the first evidence that the
  // requested node belongs to another library.  Rebase before merging so a
  // foreign Project tree can never append to the actor's Personal snapshot.
  const scopedCurrent = incomingLibraryId && incomingLibraryId !== currentLibraryId
    ? rebaseDocsStateToLibrary(current, incomingLibraryId, incoming.library)
    : current;
  const incomingState = {
    ...EMPTY_STATE,
    ...incoming,
    library: incoming.library
      ?? (incomingLibraryId && docsLibraryIdForState(scopedCurrent) !== incomingLibraryId
        ? {
            id: incomingLibraryId,
            docs_library_id: incomingLibraryId,
            library_type: "project",
          }
        : scopedCurrent.library),
    docs_library_id: incomingLibraryId ?? scopedCurrent.docs_library_id,
  } as DocsState;
  const currentDetails = new Set(scopedCurrent.details_loaded_ids ?? []);
  const incomingDetails = new Set(incomingState.details_loaded_ids ?? []);
  const nodes = (incomingState.nodes ?? []).reduce((items, next) => {
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
  }, scopedCurrent.nodes);
  return {
    ...scopedCurrent,
    library: incomingState.library ?? current.library,
    docs_library_id: incomingState.docs_library_id ?? scopedCurrent.docs_library_id,
    nodes,
    supertags: (incomingState.supertags ?? []).reduce((items, next) => mergeById(items, next), scopedCurrent.supertags),
    fields: (incomingState.fields ?? []).reduce((items, next) => mergeById(items, next), scopedCurrent.fields),
    views: (incomingState.views ?? []).reduce((items, next) => mergeById(items, next), scopedCurrent.views),
    projects: (incomingState.projects ?? []).reduce((items, next) => mergeById(items, next), scopedCurrent.projects),
    ai_suggestions: (incomingState.ai_suggestions ?? []).reduce((items, next) => mergeById(items, next), scopedCurrent.ai_suggestions),
    node_supertags: mergeByKey(
      scopedCurrent.node_supertags,
      incomingState.node_supertags ?? [],
      (item) => `${item.node_id}:${item.supertag_id}`,
    ),
    supertag_fields: mergeByKey(
      scopedCurrent.supertag_fields,
      incomingState.supertag_fields ?? [],
      (item) => `${item.supertag_id}:${item.field_id}`,
    ),
    placements: mergeByKey(
      scopedCurrent.placements,
      incomingState.placements ?? [],
      (item) => item.id,
    ),
    field_values: mergeByKey(
      scopedCurrent.field_values,
      incomingState.field_values ?? [],
      (item) => `${item.node_id}:${item.field_id}`,
    ),
    attachments: mergeByKey(
      scopedCurrent.attachments,
      incomingState.attachments ?? [],
      (item) => item.id,
    ),
    has_children_ids: Array.from(new Set([
      ...(scopedCurrent.has_children_ids ?? []),
      ...(incomingState.has_children_ids ?? []),
    ])),
    loaded_children_parent_ids: Array.from(new Set([
      ...(scopedCurrent.loaded_children_parent_ids ?? []),
      ...(incomingState.loaded_children_parent_ids ?? []),
    ])),
    details_loaded_ids: Array.from(new Set([
      ...(scopedCurrent.details_loaded_ids ?? []),
      ...(incomingState.details_loaded_ids ?? []),
    ])),
    has_details_ids: Array.from(new Set([
      ...(scopedCurrent.has_details_ids ?? []),
      ...(incomingState.has_details_ids ?? []),
    ])),
    children_next_cursor_by_parent: {
      ...(scopedCurrent.children_next_cursor_by_parent ?? {}),
      ...(incomingState.children_next_cursor_by_parent ?? {}),
    },
    child_count_by_parent: {
      ...(scopedCurrent.child_count_by_parent ?? {}),
      ...(incomingState.child_count_by_parent ?? {}),
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

function hasCodeMirrorRangeSelection(target: Element | null): boolean {
  const element = target ?? (document.activeElement instanceof Element ? document.activeElement : null);
  const root = element?.closest(".cm-editor") as HTMLElement | null;
  if (!root) return false;
  const view = EditorView.findFromDOM(root);
  if (view) return view.state.selection.ranges.some((range) => !range.empty);
  const selection = window.getSelection();
  return Boolean(selection && !selection.isCollapsed && root.contains(selection.anchorNode));
}

/**
 * Empty titles are only valid for an explicitly persisted paragraph block.
 * Keep this check at the workspace boundary as well as in the writer: an
 * editor can blur while its row is being replaced, so the workspace must not
 * accidentally turn an arbitrary empty node into a persisted blank block.
 */
function isExplicitBlankParagraphPatch(
  node: Pick<DocsNode, "node_type">,
  title: string,
  patch?: Partial<Pick<DocsNode, "body_json" | "body_text" | "node_type" | "display_props" | "description">>,
): boolean {
  if (title.trim() !== "" || node.node_type !== "node") return false;
  const body = patch?.body_json;
  return isExplicitBlankParagraph(title === "" ? "" : title.trim(), body, "node");
}

function normalizeExplicitBlankParagraphPatch(
  patch: Partial<Pick<DocsNode, "body_json" | "body_text" | "node_type" | "display_props" | "description">>,
): Partial<Pick<DocsNode, "body_json" | "body_text" | "node_type" | "display_props" | "description">> {
  return {
    ...patch,
    body_json: blankParagraphBodyJson(patch.body_json),
    body_text: "",
    node_type: "node",
  };
}

export function DocsWorkspace({
  initialNodeId,
}: {
  initialNodeId?: string | null;
}) {
  const pathname = usePathname();
  const isDocsRoute = pathname === "/docs" || pathname?.startsWith("/docs/") === true;
  const currentUserId = useCurrentUserId();
  const docsNavigationOwnerRef = useRef(createDocsNavigationOwner());
  useEffect(() => {
    configureDocsNavigationScope(currentUserId);
  }, [currentUserId]);
  const { allProjects } = useProject();
  const { settings } = useUserSettings();
  const remoteConnectionEnabled = getRemoteServerConnectionEnabled(settings);
  const [docsSource, setDocsSource] = useState<"local" | string>("local");
  const [remoteProfiles, setRemoteProfiles] = useState<RemoteServerProfile[]>([]);
  const docsReadOnly = docsSource !== "local";
  const [sharedRoots, setSharedRoots] = useState<Array<DocsNode & { permission?: string; share_id?: string }>>([]);
  const apiFetch = useCallback(<T,>(path: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    // Enterprise/remote Docs is read-only, but GET/HEAD lazy routes remain
    // available so hidden legacy bridge parents can still hydrate descendants.
    if (docsReadOnly && method !== "GET" && method !== "HEAD") {
      return Promise.reject(new Error("Enterprise Docsは読み取り専用です"));
    }
    return rawApiFetch<T>(path, init);
  }, [docsReadOnly]);
  useEffect(() => {
    if (docsSource !== "local") {
      setSharedRoots([]);
      return;
    }
    let cancelled = false;
    void apiFetch<{ nodes?: Array<DocsNode & { permission?: string; share_id?: string }> }>("/api/docs/shared")
      .then((data) => {
        if (!cancelled) setSharedRoots(data.nodes ?? []);
      })
      .catch(() => {
        if (!cancelled) setSharedRoots([]);
      });
    return () => {
      cancelled = true;
    };
  }, [apiFetch, docsSource]);
  const [state, setState] = useState<DocsState>(EMPTY_STATE);
  useEffect(() => {
    if (isDocsRoute) publishCanonicalDocsState(docsNavigationOwnerRef.current, state);
  }, [isDocsRoute, state]);
  useEffect(
    () => () => clearCanonicalDocsState(docsNavigationOwnerRef.current),
    [isDocsRoute],
  );
  const [focusNodeId, setFocusNodeId] = useState<string | null>(initialNodeId ?? null);
  const [loading, setLoading] = useState(true);
  const { collapsed, setCollapsed, sidebarCollapsed, setSidebarCollapsed, collapsedRef, expandedRef } = useDocsCollapse();
  const [rightPanel, setRightPanel] = useState<"related" | "tags">("related");
  const [propertiesOpen, setPropertiesOpen] = useState(false);
  const [wideDocsViewport, setWideDocsViewport] = useState(false);
  const [propertiesTagId, setPropertiesTagId] = useState<string | null>(null);
  // ゴミ箱 / Search nodes はメイン領域にフルビューとして開く。tagPageId と排他。
  const [mainView, setMainView] = useState<"trash" | "search" | null>(null);
  const [splitNodeId, setSplitNodeId] = useState<string | null>(null);
  const [newTagName, setNewTagName] = useState("");
  const [tagPageId, setTagPageId] = useState<string | null>(null);
  const [focusRequestNodeId, setFocusRequestNodeId] = useState<string | null>(null);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(min-width: 1536px)");
    const update = () => setWideDocsViewport(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);
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
  const [documentContextMenu, setDocumentContextMenu] = useState<({ nodeId: string } & DocsNodeContextMenuPosition) | null>(null);
  const [dragSidebarNodeId, setDragSidebarNodeId] = useState<string | null>(null);
  const [aiPreview, setAiPreview] = useState<DocsAiPreview | null>(null);
  const [taskModalId, setTaskModalId] = useState<string | null>(null);
  const [aliasEditorNode, setAliasEditorNode] = useState<DocsNode | null>(null);
  const [shareNode, setShareNode] = useState<DocsNode | null>(null);
  const {
    dialogOpen: clipIngestOpen,
    historyOpen: clipIngestPanelOpen,
    openDialog: openClipIngest,
    setHistoryOpen: setClipIngestPanelOpen,
    setOpenNodeHandler,
    setIngestEnabled,
  } = useDocsClipIngest();

  useEffect(() => {
    setIngestEnabled(!docsReadOnly);
    return () => setIngestEnabled(true);
  }, [docsReadOnly, setIngestEnabled]);

  // メンション候補ユーザー一覧は SWR で取得（マウント時に一度取得・失敗時は空・自動再取得なし）。
  const mentionUsers = useDocsMentionUsers(apiFetch);
  const [loadingNodeIds, setLoadingNodeIds] = useState<Set<string>>(() => new Set());
  const [taskBindingsByNodeId, setTaskBindingsByNodeId] = useState<Map<string, DocsTaskBinding | null>>(() => new Map());
  const undoStackRef = useRef<BlockHistoryEntry[]>([]);
  const redoStackRef = useRef<BlockHistoryEntry[]>([]);
  // Workspace history mutates the canonical node projection without
  // remounting the outline.  Publish the affected ids so any still-mounted
  // CodeMirror view can reconcile before its blur cleanup writes stale text.
  const [historySync, setHistorySync] = useState<{
    revision: number;
    nodeIds: string[];
    titles?: Record<string, string>;
  }>({
    revision: 0,
    nodeIds: [],
  });
  const applyingHistoryRef = useRef(false);
    const editorCommitInFlightRef = useRef<Promise<boolean> | null>(null);
    const latestEditorDraftRef = useRef(new Map<string, string>());
    // PageTitleEditor updates the in-memory node on every keystroke. Keep a
    // per-page last meaningful title so an empty blur or a failed PATCH can
    // restore the canonical value instead of leaving a blank row behind.
    const pageTitleCanonicalRef = useRef(new Map<string, string>());
    const pendingCreateHistoryIdsRef = useRef(new Set<string>());
  const taskBindingInFlightRef = useRef<Set<string>>(new Set());
  const sidebarScrollNodeRef = useRef<string | null>(null);
  const previousInitialNodeIdRef = useRef(initialNodeId);
  const remoteProfilesRequestRef = useRef(0);
  const docsLoadGenerationRef = useRef(0);
  const openDocsNodeGenerationRef = useRef(0);
  // Keep the actor's own Personal Library id separate from the currently
  // focused library.  A Project node may be writable while its definitions
  // remain owned by another user's library.
  const actorDocsLibraryIdRef = useRef<string | null>(null);
  // SWR cache hydration is rendered immediately, but lazy child/detail
  // prefetches must wait until that generation's network bootstrap has
  // committed.  Otherwise a prefetch merged into the cached snapshot can be
  // overwritten by the final bootstrap replacement.
  const networkSettledGenerationRef = useRef<number | null>(null);
  const [networkSettledGeneration, setNetworkSettledGeneration] = useState<number | null>(null);
  const workspaceMountedRef = useRef(false);
  const nodeNeighborhoodInFlightRef = useRef(new Map<string, {
    controller: AbortController;
    promise: Promise<DocsNeighborhoodResponse>;
    autoMerge: boolean;
  }>());
  const nodeChildrenInFlightRef = useRef(new Map<string, { controller: AbortController; promise: Promise<DocsNeighborhoodResponse> }>());
    const nodeDetailsInFlightRef = useRef(new Map<string, { controller: AbortController; promise: Promise<DocsNeighborhoodResponse> }>());
    const nodeCreateInFlightRef = useRef(new Map<string, Promise<DocsNode>>());
  const expandNodeRef = useRef<(nodeId: string) => void>(() => {});
  const undoDocsOperationRef = useRef<() => Promise<void>>(async () => {});
  const redoDocsOperationRef = useRef<() => Promise<void>>(async () => {});
  const archiveSelectedNodesRef = useRef<() => Promise<void>>(async () => {});
  const loadedParentAccessRef = useRef(new Map<string, number>());
  const focusNodeIdRef = useRef(focusNodeId);
  const nodeLoadCountRef = useRef(new Map<string, number>());
  const nodeTagIdsRef = useRef(new Map<string, string[]>());
  const tagMutationQueueRef = useRef(new Map<string, Promise<void>>());
  const docsSaveQueue = useMemo(() => new DocsSaveQueue<DocsNode>(), []);

  // A navigation/state replacement must not race the last CodeMirror blur.
  // DocsSaveQueue.flush() deliberately swallows individual chain failures so
  // callers can continue processing other nodes; the active editor promise is
  // therefore awaited first and its rejection is intentionally propagated.
  const flushPendingDocsEditorWritesBeforeNavigation = useCallback(async () => {
    const editorCommit = editorCommitInFlightRef.current;
    let editorCommitFailed = false;
    let editorCommitError: unknown;
    if (editorCommit) {
      try {
        await editorCommit;
      } catch (error) {
        // Drain the queue even when the active editor operation rejects, but
        // rethrow that exact failure after the drain so navigation remains
        // blocked and callers can preserve the draft/toast.
        editorCommitFailed = true;
        editorCommitError = error;
      }
    }
    await docsSaveQueue.flush();
    if (editorCommitFailed) throw editorCommitError;
  }, [docsSaveQueue]);

  // 展開復元で既に処理したノード。同じノードへ何度もリクエストを出さないための記録。
  const expandRestoreDoneRef = useRef<Set<string>>(new Set());
  // 展開復元で先読みした親ノード数。大きなツリーで子取得が無制限に連鎖するのを防ぐ。
  const expandRestoreLoadCountRef = useRef(0);
  // Ctrl+→ / Ctrl+← の直前の入力。2回連続押しの判定に使う。
  // 本文とサイドバーを跨いだ入力は同じ操作の続きとして扱わない。
  const bulkArrowRef = useRef<{ direction: "expand" | "collapse"; surface: "body" | "sidebar"; at: number } | null>(null);

  useEffect(() => {
    focusNodeIdRef.current = focusNodeId;
  }, [focusNodeId]);

  useEffect(() => () => {
    workspaceMountedRef.current = false;
    for (const entry of nodeChildrenInFlightRef.current.values()) entry.controller.abort();
    for (const entry of nodeDetailsInFlightRef.current.values()) entry.controller.abort();
    for (const entry of nodeNeighborhoodInFlightRef.current.values()) entry.controller.abort();
  }, []);

  useEffect(() => {
    workspaceMountedRef.current = true;
    return () => {
      workspaceMountedRef.current = false;
    };
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

  // 本文側の格納操作で共有リクエストを中断すると、同じノードを展開中の
  // サイドバーまで巻き込む。サイドバーが展開中なら、その読み込みを維持する。
  const abortBodyNodeLoads = useCallback((nodeId: string) => {
    if (sidebarCollapsed.has(nodeId)) return;
    abortNodeLoads(nodeId);
  }, [abortNodeLoads, sidebarCollapsed]);

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
  const activeDocsLibraryId = state.library?.docs_library_id ?? state.docs_library_id ?? null;
  const nodesById = useMemo(() => new Map(state.nodes.map((node) => [node.id, node])), [state.nodes]);
  const nodesByIdRef = useRef(nodesById);
  nodesByIdRef.current = nodesById;
  const childrenByParent = useMemo(() => buildOutlineChildren(state.nodes, state.placements), [state.nodes, state.placements]);
  const networkLoadSettled = networkSettledGeneration !== null
    && networkSettledGeneration === docsLoadGenerationRef.current
    && networkSettledGenerationRef.current === docsLoadGenerationRef.current;
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
  // Keep the canonical snapshot intact (legacy blank parents are needed for
  // hoisting), but apply one visible/nonblank predicate to every UI list.
  const isNodeProjectionVisible = useCallback((node: DocsNode) => (
    !node.archived_at
    && isDocsNodeTitleVisible(node)
    && !isLegacyEmailEmptyLineNode(node, nodesById)
  ), [nodesById]);
  const isSidebarProjectionVisible = useCallback((node: DocsNode) => (
    isDocsSidebarNodeVisible(node)
    && !isLegacyEmailEmptyLineNode(node, nodesById)
  ), [nodesById]);
  const visibleNodes = useMemo(
    () => state.nodes.filter((node) => isNodeProjectionVisible(node)),
    [isNodeProjectionVisible, state.nodes],
  );
  const sidebarNodeHasChildren = useCallback(
    (nodeId: string) => hoistedVisibleChildren(childrenByParent, nodeId, isSidebarProjectionVisible).length > 0
      || nodeHasChildren(nodeId),
    [childrenByParent, isSidebarProjectionVisible, nodeHasChildren],
  );
  useEffect(() => {
    // レンダー中に optimistic な希望状態を上書きすると、remove→add の間に
    // 古いrelationが復活する。通信中のnodeはmutation queueを唯一の根拠にする。
    for (const node of state.nodes) {
      if (tagMutationQueueRef.current.has(node.id)) continue;
      nodeTagIdsRef.current.set(node.id, (nodeTags.get(node.id) ?? []).map((tag) => tag.id));
    }
  }, [nodeTags, state.nodes]);
  useEffect(() => {
    for (const node of state.nodes) {
      if (hasMeaningfulBlockTitle(node.title) && !pageTitleCanonicalRef.current.has(node.id)) {
        pageTitleCanonicalRef.current.set(node.id, node.title);
      }
    }
  }, [state.nodes]);
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
  // Personal subtree shares carry an effective ACL on every hydrated node.
  // Keep UI mutations disabled locally for read recipients instead of
  // surfacing avoidable 403 toasts from the API.
  const canWriteNode = (node: DocsNode | null | undefined) =>
    !docsReadOnly && node?.permission !== "read";
  const selectedNodeCanWrite = canWriteNode(selectedNode);
  const canEditDefinitions = canEditDocsDefinitions(
    selectedNode,
    selectedNodeCanWrite,
    actorDocsLibraryIdRef.current,
  );
  const activeTagPage = tagPageId && tagPageId !== SUPERTAGS_OVERVIEW_ID ? tagById.get(tagPageId) ?? null : null;
  const canEditActiveTagDefinitions = canEditDefinitions
    && (!activeTagPage || activeTagPage.docs_library_id === actorDocsLibraryIdRef.current);
  const roots = useMemo(
    () => sortNodesByPosition(
      state.nodes
        .filter((node) => !node.parent_id)
        .flatMap((node) => isSidebarProjectionVisible(node)
          ? [node]
          : !isDocsNodeTitleVisible(node)
            ? hoistedVisibleChildren(childrenByParent, node.id, isSidebarProjectionVisible)
            : []),
    ),
    [childrenByParent, isSidebarProjectionVisible, state.nodes],
  );
  // Project writers can create descendants and edit existing nodes, but a
  // foreign Project library must never receive a new unparented root. An
  // owner/legacy node without an explicit permission still permits the
  // Personal root action for backwards-compatible bootstrap fixtures.
  const foreignLibraryFocused = Boolean(
    activeDocsLibraryId
      && actorDocsLibraryIdRef.current
      && activeDocsLibraryId !== actorDocsLibraryIdRef.current,
  );
  const canCreateRootNode = !docsReadOnly && !foreignLibraryFocused && (
    roots.length === 0
    || roots.some((root) => root.permission !== "write" && root.permission !== "read")
  );
  // 削除は子孫ごとアーカイブされるので、ゴミ箱には削除操作の起点だけを出す。
  // 親もアーカイブ済みのノードは、その親を復元すれば一緒に戻る。
  const archivedNodes = useMemo(() => {
    const archivedIds = new Set(state.nodes.filter((node) => !!node.archived_at).map((node) => node.id));
    return state.nodes
      .filter((node) => !!node.archived_at && isDocsNodeTitleVisible(node) && !isLegacyEmailEmptyLineNode(node, nodesById) && !(node.parent_id && archivedIds.has(node.parent_id)))
      .sort((a, b) => (b.archived_at ?? "").localeCompare(a.archived_at ?? ""));
  }, [nodesById, state.nodes]);
  const currentRows = useMemo(
    () => (focusNode ? outlineRows(focusNode.id, childrenByParent, collapsed, 0, new Set<string>(), isNodeProjectionVisible) : []),
    [childrenByParent, collapsed, focusNode, isNodeProjectionVisible],
  );
  const splitNode = splitNodeId ? nodesById.get(splitNodeId) ?? null : null;
  const splitRows = useMemo(
    () => (splitNode ? outlineRows(splitNode.id, childrenByParent, collapsed, 0, new Set<string>(), isNodeProjectionVisible) : []),
    [childrenByParent, collapsed, isNodeProjectionVisible, splitNode],
  );
  const parentIdByNodeId = useMemo(() => {
    const map = new Map<string, string | null>();
    for (const node of state.nodes) {
      map.set(node.id, node.parent_id);
    }
    return map;
  }, [state.nodes]);
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
      .filter((node) => node.id !== selectedNode.id && isNodeProjectionVisible(node))
      .filter((node) => {
        const tags = tagSetByNode.get(node.id) ?? new Set<string>();
        return Array.from(acceptedTags).some((tagId) => tags.has(tagId));
      })
      .slice(0, 20);
  }, [isNodeProjectionVisible, selectedNode, state.nodes, tagById, tagSetByNode]);
  const commandMoveTargets = useMemo(() => {
    if (!selectedNode) return [];
    return state.nodes
      .filter((node) => !selectedNodeIdSet.has(node.id) && !selectedNodeIdSet.has(node.parent_id ?? "") && isNodeProjectionVisible(node))
      .filter((node) => {
        let parentId = node.parent_id;
        while (parentId) {
          if (selectedNodeIdSet.has(parentId)) return false;
          parentId = nodesById.get(parentId)?.parent_id ?? null;
        }
        return true;
      })
      .slice(0, 500);
  }, [isNodeProjectionVisible, nodesById, selectedNode, selectedNodeIdSet, state.nodes]);
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

  const load = useCallback(async (options: LoadOptions = {}): Promise<DocsLoadStatus> => {
    // `load` replaces the canonical snapshot.  Keep this guard here as a
    // second line of defence for refresh/reload callers that do not go through
    // one of the explicit navigation handlers below.
    try {
      await flushPendingDocsEditorWritesBeforeNavigation();
    } catch (error) {
      // The editor commit owns draft preservation and normally emits the
      // detailed PATCH error toast.  Do not enter the normal load catch path:
      // it would clear a direct-route snapshot and lose the still-visible
      // draft.  Return a failed status while leaving state untouched.
      toast.error(error instanceof Error ? error.message : "Docsの保存に失敗しました。入力内容を確認してください");
      return "failed";
    }
    const loadGeneration = ++docsLoadGenerationRef.current;
    // Navigation callers share this token with openDocsNode/openToday.  A
    // newer navigation must prevent this state-replacing load from publishing
    // a stale snapshot after its own save barrier has completed.
    const navigationGeneration = openDocsNodeGenerationRef.current;
    const isCurrentNavigation = () => navigationGeneration === openDocsNodeGenerationRef.current;
    networkSettledGenerationRef.current = null;
    setNetworkSettledGeneration(null);
    const bootstrapCacheKey = `${DOCS_BOOTSTRAP_CACHE_PREFIX}${String(docsSource ?? "local")}`;

    // 通常ロード（focusToday/date/nodeId 指定なし・非 readOnly）では、
    // 永続キャッシュ済み bootstrap を即描画し skeleton を出さない（低帯域配慮）。
    // その後ネットワーク取得結果で差し替える（stale-while-revalidate）。
    let hydratedFromCache = false;
    let cachedBootstrap: DocsState | null = null;
    let cachedBootstrapEtag: string | null = null;
    // A direct node route must not paint the actor's cached Home while the
    // focused tree is resolving; that would briefly mix two libraries and
    // also let stale mutations target the wrong sidebar selection.
    if (!docsReadOnly && !initialNodeId && !options.focusToday && !options.date && !options.nodeId) {
      try {
        const cachedValue = await readCachedSnapshot<DocsBootstrapCacheEntry | DocsState>(bootstrapCacheKey);
        const cached = isDocsBootstrapCacheEntry(cachedValue)
          ? cachedValue.data
          : cachedValue;
        cachedBootstrap = cached
          && cached.nodes.every((node) => typeof node.docs_library_id === "string")
          ? cached
          : null;
        cachedBootstrapEtag = cachedBootstrap && isDocsBootstrapCacheEntry(cachedValue)
          ? cachedValue.etag
          : null;
        if (
          cachedBootstrap &&
          cachedBootstrap.nodes.length > 0 &&
          loadGeneration === docsLoadGenerationRef.current &&
          isCurrentNavigation()
        ) {
          actorDocsLibraryIdRef.current = docsLibraryIdForState(cachedBootstrap);
          const cachedState = mergeLoadedDocsState(EMPTY_STATE, cachedBootstrap);
          const cachedDefaultNodeId =
            (initialNodeId &&
            cachedState.nodes.some((node) => node.id === initialNodeId)
              ? initialNodeId
              : cachedState.nodes.find((node) => node.system_key === "home")?.id) ??
            cachedState.nodes[0]?.id ??
            null;
          setState(cachedState);
          setFocusNodeId((current) => current ?? cachedDefaultNodeId);
          setSelectedNodeId((current) => {
            const nextId = current ?? cachedDefaultNodeId;
            selectedNodeIdsRef.current = nextId ? [nextId] : [];
            selectionAnchorNodeIdRef.current = nextId;
            setSelectedNodeIds(nextId ? [nextId] : []);
            setSelectionAnchorNodeId(nextId);
            return nextId;
          });
          setLoading(false);
          hydratedFromCache = true;
        }
      } catch {
        // キャッシュ読み出し失敗は無視して通常ロードへ。
      }
    }
    if (!hydratedFromCache) setLoading(true);
    try {
      if (docsReadOnly) {
        const tree = await getRemoteDocsTree(docsSource);
        if (loadGeneration !== docsLoadGenerationRef.current || !remoteConnectionEnabled || !isCurrentNavigation()) return "superseded";
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
            .filter((node) => (
              node.body_json?.format === "doc_block"
              && (node.body_json?.block_type === "markdown" || node.body_json?.block_type === "code")
              && typeof node.body_json?.content === "string"
            ) || Boolean(node.body_json?.bookmark))
              .map((node) => node.id),
          ])),
        } as DocsState;
        if (options.nodeId && !remoteNodes.some((node) => node.id === options.nodeId)) {
          throw new Error("対象のDocsノードを読み込めませんでした");
        }
        const defaultNodeId =
          (options.nodeId && remoteNodes.some((node) => node.id === options.nodeId)
            ? options.nodeId
            : initialNodeId && remoteNodes.some((node) => node.id === initialNodeId)
              ? initialNodeId
              : remoteNodes[0]?.id) ?? null;
        if (!isCurrentNavigation()) return "superseded";
        setState(nextState);
        setFocusNodeId(defaultNodeId);
        selectedNodeIdsRef.current = defaultNodeId ? [defaultNodeId] : [];
        selectionAnchorNodeIdRef.current = defaultNodeId;
        setSelectedNodeId(defaultNodeId);
        setSelectedNodeIds(defaultNodeId ? [defaultNodeId] : []);
        setSelectionAnchorNodeId(defaultNodeId);
        return "success";
      }
      const requestTimeoutMs = docsOpenNodeTimeoutMs;
      const today = options.focusToday || options.date
        ? await apiFetch<TodayResponse>(`/api/docs/today${options.date ? `?date=${encodeURIComponent(options.date)}` : ""}`)
        : null;
      // `/api/docs/today` returns only the Day node itself.  Hydrate the same
      // lightweight neighborhood that a focused page receives so a repeat
      // Today navigation cannot replace a visible child with an empty outline.
      // Keep these requests in this load generation; the publish guard below
      // still gives latest-navigation-wins semantics if navigation changes
      // while any of them is in flight.
      const todayNeighborhoodPromise = today
        ? Promise.allSettled([
            fetchDocsPathWithTimeout<DocsNeighborhoodResponse>(
              apiFetch,
              `/api/docs/nodes/${today.node.id}/tree`,
              requestTimeoutMs,
            ),
            fetchDocsPathWithTimeout<DocsNeighborhoodResponse>(
              apiFetch,
              `/api/docs/nodes/${today.node.id}/children`,
              requestTimeoutMs,
            ),
            fetchDocsPathWithTimeout<DocsNeighborhoodResponse>(
              apiFetch,
              `/api/docs/nodes/${today.node.id}/details`,
              requestTimeoutMs,
            ),
          ])
        : null;
      // The canonical editor and the route-independent quick panel share one
      // single-flight bootstrap request. `force` preserves the editor's
      // existing refresh semantics while still deduping a healthy in-flight
      // quick panel request during a route transition.
      // Stop waiting after the open timeout so one hung GET cannot pin the
      // initial skeleton, then abandon that shared flight so the next
      // fetchDocsBootstrap can start a new request.
      const requestedFocusId = options.nodeId ?? initialNodeId ?? null;
      const bootstrapPromise = fetchDocsBootstrap({
        cached: cachedBootstrap,
        etag: cachedBootstrapEtag,
        force: true,
      });
      const earlyNeighborhoodPromise = requestedFocusId && !options.focusToday && !options.date
        ? Promise.allSettled([
            fetchDocsPathWithTimeout<DocsNeighborhoodResponse>(
              apiFetch,
              `/api/docs/nodes/${requestedFocusId}/tree`,
              requestTimeoutMs,
            ),
            fetchDocsPathWithTimeout<DocsNeighborhoodResponse>(
              apiFetch,
              `/api/docs/nodes/${requestedFocusId}/children`,
              requestTimeoutMs,
            ),
            fetchDocsPathWithTimeout<DocsNeighborhoodResponse>(
              apiFetch,
              `/api/docs/nodes/${requestedFocusId}/details`,
              requestTimeoutMs,
            ),
          ])
        : null;
      const bootstrapResult = await settleWithTimeout(bootstrapPromise, requestTimeoutMs);
      if (bootstrapResult.status === "rejected") {
        abandonDocsBootstrapInFlight();
      }
      const data = settledValue(bootstrapResult);
      if (data) {
        actorDocsLibraryIdRef.current = docsLibraryIdForState(data) ?? actorDocsLibraryIdRef.current;
      }
      const firstNodeId = requestedFocusId
        ?? data?.nodes.find((node) => node.system_key === "home")?.id
        ?? data?.nodes[0]?.id
        ?? null;
      let focusedTree: DocsNeighborhoodResponse | null = null;
      let focusedChildren: DocsNeighborhoodResponse | null = null;
      let focusedDetails: DocsNeighborhoodResponse | null = null;
      if (earlyNeighborhoodPromise) {
        const [treeResult, childrenResult, detailsResult] = await earlyNeighborhoodPromise;
        focusedTree = settledValue(treeResult);
        focusedChildren = settledValue(childrenResult);
        focusedDetails = settledValue(detailsResult);
      } else if (firstNodeId && !options.focusToday && !options.date) {
        const [treeResult, childrenResult, detailsResult] = await Promise.allSettled([
          fetchDocsPathWithTimeout<DocsNeighborhoodResponse>(
            apiFetch,
            `/api/docs/nodes/${firstNodeId}/tree`,
            requestTimeoutMs,
          ),
          fetchDocsPathWithTimeout<DocsNeighborhoodResponse>(
            apiFetch,
            `/api/docs/nodes/${firstNodeId}/children`,
            requestTimeoutMs,
          ),
          fetchDocsPathWithTimeout<DocsNeighborhoodResponse>(
            apiFetch,
            `/api/docs/nodes/${firstNodeId}/details`,
            requestTimeoutMs,
          ),
        ]);
        focusedTree = settledValue(treeResult);
        focusedChildren = settledValue(childrenResult);
        focusedDetails = settledValue(detailsResult);
      }
      let todayTree: DocsNeighborhoodResponse | null = null;
      let todayChildren: DocsNeighborhoodResponse | null = null;
      let todayDetails: DocsNeighborhoodResponse | null = null;
      if (todayNeighborhoodPromise) {
        const [treeResult, childrenResult, detailsResult] = await todayNeighborhoodPromise;
        todayTree = settledValue(treeResult);
        todayChildren = settledValue(childrenResult);
        todayDetails = settledValue(detailsResult);
      }
      if (loadGeneration !== docsLoadGenerationRef.current || !isCurrentNavigation()) return "superseded";
      if (!data && !focusedTree && !today) {
        throw new Error("Docsの読み込みに失敗しました");
      }
      const focusedTreeState = focusedTree
        ? {
            ...focusedTree,
            loaded_children_parent_ids: focusedTree.loaded_children_parent_ids ?? (firstNodeId ? [firstNodeId] : []),
          }
        : null;
      let nextState = mergeLoadedDocsState(EMPTY_STATE, data ?? EMPTY_STATE);
      if (focusedTreeState) nextState = mergeLoadedDocsState(nextState, focusedTreeState);
      if (focusedChildren) nextState = mergeLoadedDocsState(nextState, focusedChildren);
      if (focusedDetails) nextState = mergeLoadedDocsState(nextState, focusedDetails);
      if (todayTree) nextState = mergeLoadedDocsState(nextState, todayTree);
      if (todayChildren) {
        nextState = mergeLoadedDocsState(nextState, {
          ...todayChildren,
          // Older API responses may omit this marker.  Treat a successful
          // first page as loaded so the eager/lazy effects do not issue a
          // duplicate request immediately after Today navigation.
          loaded_children_parent_ids: todayChildren.loaded_children_parent_ids ?? [today?.node.id ?? ""].filter(Boolean),
        });
      }
      if (todayDetails) nextState = mergeLoadedDocsState(nextState, todayDetails);
      if (today) {
        nextState = mergeLoadedDocsState(nextState, {
          nodes: [today.node],
          supertags: today.supertag ? [today.supertag] : [],
          node_supertags: today.node_supertags ?? [],
        });
      }
      // The focused tree is the authoritative scope for an initial node
      // route.  Children/details are independent requests; a stale response
      // from another generation/library must not switch the state back.
      const focusedLibraryId = focusedTreeState ? docsLibraryIdForState(focusedTreeState) : null;
      if (focusedLibraryId) {
        nextState = rebaseDocsStateToLibrary(nextState, focusedLibraryId, focusedTreeState?.library);
      }
      if (options.nodeId && !nextState.nodes.some((node) => node.id === options.nodeId)) {
        throw new Error("対象のDocsノードを読み込めませんでした");
      }
      const requestedNodeId = focusedTreeState?.focus_node_id ?? options.nodeId ?? initialNodeId;
      const requestedPresent = Boolean(
        requestedNodeId && nextState.nodes.some((node) => node.id === requestedNodeId),
      );
      if (initialNodeId && !requestedPresent && !options.nodeId && (data || focusedTree || today)) {
        toast.error("対象のDocsノードを読み込めませんでした");
      }
      const initialNodeChanged = previousInitialNodeIdRef.current !== initialNodeId;
      previousInitialNodeIdRef.current = initialNodeId;
      const defaultNodeId = today?.node.id
        ?? (requestedPresent ? requestedNodeId : null)
        ?? (firstNodeId && nextState.nodes.some((node) => node.id === firstNodeId) ? firstNodeId : null)
        ?? nextState.nodes.find((node) => node.system_key === "home")?.id
        ?? nextState.nodes[0]?.id
        ?? null;
      // state を作り直した直後に記録を捨てる。読込前に捨てると、この setState で
      // 上書きされる先読み結果を「復元済み」と誤認して二度と展開されない。
      expandRestoreDoneRef.current = new Set();
      expandRestoreLoadCountRef.current = 0;
      if (!isCurrentNavigation()) return "superseded";
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
      return "success";
    } catch (error) {
      const superseded = loadGeneration !== docsLoadGenerationRef.current || !isCurrentNavigation();
      if (!superseded && (!docsReadOnly || remoteConnectionEnabled)) {
        toast.error(error instanceof Error ? error.message : "Docsの読み込みに失敗しました");
      }
      if (!superseded && initialNodeId) {
        // Never leave an actor-scoped Home snapshot visible after a direct
        // foreign-node route fails to resolve.  The route is either empty or
        // focused on the requested library, never a stale mixed state.
        setState(EMPTY_STATE);
        setFocusNodeId(null);
        selectedNodeIdsRef.current = [];
        selectionAnchorNodeIdRef.current = null;
        setSelectedNodeId(null);
        setSelectedNodeIds([]);
        setSelectionAnchorNodeId(null);
        setPageReferences(EMPTY_REFERENCES);
        setPageReferencesLoading(false);
      }
      return superseded ? "superseded" : "failed";
    } finally {
      if (loadGeneration === docsLoadGenerationRef.current && isCurrentNavigation()) {
        networkSettledGenerationRef.current = loadGeneration;
        setNetworkSettledGeneration(loadGeneration);
        setLoading(false);
      }
    }
  }, [
    apiFetch,
    docsReadOnly,
    docsSource,
    flushPendingDocsEditorWritesBeforeNavigation,
    initialNodeId,
    remoteConnectionEnabled,
  ]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadNodeNeighborhood = useCallback((nodeId: string, options?: {
    quiet?: boolean;
    shouldCommit?: () => boolean;
  }) => {
    const autoMerge = options?.shouldCommit == null;
    const existing = nodeNeighborhoodInFlightRef.current.get(nodeId);
    // Sidebar/default loads may share an auto-merge flight. Navigation waits
    // must not reuse that merge, or a superseded open can still rebase state.
    if (autoMerge && existing?.autoMerge) return existing.promise;
    if (existing) existing.controller.abort();
    const requestGeneration = docsLoadGenerationRef.current;
    const controller = new AbortController();
    const isCurrentScope = () => requestGeneration === docsLoadGenerationRef.current;
    const shouldCommit = options?.shouldCommit;
    const request = apiFetch<DocsNeighborhoodResponse>(`/api/docs/nodes/${nodeId}/tree`, { signal: controller.signal })
      .then((data) => {
        const normalized: DocsNeighborhoodResponse = {
          ...data,
          // 旧クライアントやリモートDocsがメタデータを返さなくても、要求した親の取得完了を記録する。
          loaded_children_parent_ids: data.loaded_children_parent_ids ?? [nodeId],
        };
        // A stale tree request must never merge into a newer bootstrap state.
        // Re-check inside the updater: shouldCommit() can flip after this
        // then() runs and before React applies the merge.
        if (!isCurrentScope()) return {};
        setState((current) => {
          if (!isCurrentScope() || (shouldCommit && !shouldCommit())) return current;
          return mergeLoadedDocsState(current, normalized);
        });
        return normalized;
      })
      .catch((error) => {
        if (controller.signal.aborted || !isCurrentScope()) return {};
        if (!options?.quiet) {
          toast.error(error instanceof Error ? error.message : "Docsノードの読み込みに失敗しました");
        }
        return {};
      })
      .finally(() => {
        const current = nodeNeighborhoodInFlightRef.current.get(nodeId);
        if (current?.promise === request) nodeNeighborhoodInFlightRef.current.delete(nodeId);
      });
    nodeNeighborhoodInFlightRef.current.set(nodeId, { controller, promise: request, autoMerge });
    return request;
  }, [apiFetch]);

  const loadNodeChildren = useCallback((nodeId: string, cursor?: string | null) => {
    const requestKey = `${nodeId}:${cursor ?? "first"}`;
    const inFlight = nodeChildrenInFlightRef.current.get(requestKey);
    if (inFlight) return inFlight.promise;
    const requestGeneration = docsLoadGenerationRef.current;
    const isCurrentScope = () => requestGeneration === docsLoadGenerationRef.current;
    const controller = new AbortController();
    markNodeLoading(nodeId, true);
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    const promise = apiFetch<DocsNeighborhoodResponse>(`/api/docs/nodes/${nodeId}/children${query}`, { signal: controller.signal })
      .then((data) => {
        if (!isCurrentScope()) return {};
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
        if (controller.signal.aborted || !isCurrentScope() || (error instanceof DOMException && error.name === "AbortError")) return {};
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

  // Blank legacy parents are intentionally omitted from every visible
  // projection.  Do not wait for a user click on the hidden row to trigger
  // lazy loading: bootstrap/children responses may contain the bridge row
  // with only a `has_children_ids` marker, so eagerly hydrate those children
  // once the network snapshot is settled.  This keeps meaningful
  // grandchildren reachable even when the blank parent itself is invisible.
  useEffect(() => {
    if (!networkLoadSettled || loading) return;
    let issued = 0;
    for (const node of state.nodes) {
      if (issued >= 8) break;
      if (
        node.archived_at
        || (isDocsNodeTitleVisible(node) && !isLegacyEmailEmptyLineNode(node, nodesById))
        || !nodeHasChildren(node.id)
        || loadedChildrenParentIds.has(node.id)
        || loadingNodeIds.has(node.id)
      ) continue;
      void loadNodeChildren(node.id);
      issued += 1;
    }
  }, [
    loadNodeChildren,
    loadedChildrenParentIds,
    loading,
    loadingNodeIds,
    nodesById,
    networkLoadSettled,
    nodeHasChildren,
    state.nodes,
  ]);

  const ensureNodeChildrenLoaded = useCallback((nodeId: string) => {
    if ((state.loaded_children_parent_ids ?? []).includes(nodeId)) return Promise.resolve(null);
    return loadNodeChildren(nodeId);
  }, [loadNodeChildren, state.loaded_children_parent_ids]);

  // Sidebar expansion is persisted independently from the outline expansion.
  // A fresh bootstrap can clear the node snapshot, so rehydrate children for
  // expanded sidebar branches immediately instead of waiting for the user to
  // collapse/expand the branch a second time.
  useEffect(() => {
    if (!networkLoadSettled || loading || docsReadOnly || sidebarCollapsed.size === 0) return;
    let issued = 0;
    for (const nodeId of sidebarCollapsed) {
      if (issued >= 8) break;
      if (!nodesById.has(nodeId) || !nodeHasChildren(nodeId) || loadedChildrenParentIds.has(nodeId)) continue;
      void loadNodeChildren(nodeId);
      issued += 1;
    }
  }, [docsReadOnly, loadNodeChildren, loadedChildrenParentIds, loading, networkLoadSettled, nodeHasChildren, nodesById, sidebarCollapsed]);

  useEffect(() => {
    if (!networkLoadSettled) return;
    for (const documentNode of [focusNode, splitNode]) {
      if (!documentNode) continue;
      const documentTags = nodeTags.get(documentNode.id) ?? [];
      for (const child of childrenByParent.get(documentNode.id) ?? []) {
        if (
          isLegacyEmailOutlineCandidate(documentNode, documentTags, child)
          && nodeHasChildren(child.id)
          && !loadedChildrenParentIds.has(child.id)
        ) {
          void loadNodeChildren(child.id);
        }
      }
    }
  }, [
    childrenByParent,
    focusNode,
    loadNodeChildren,
    loadedChildrenParentIds,
    networkLoadSettled,
    nodeHasChildren,
    nodeTags,
    splitNode,
  ]);

  const loadNodeDetails = useCallback((nodeId: string) => {
    const inFlight = nodeDetailsInFlightRef.current.get(nodeId);
    if (inFlight) return inFlight.promise;
    const requestGeneration = docsLoadGenerationRef.current;
    const isCurrentScope = () => requestGeneration === docsLoadGenerationRef.current;
    const controller = new AbortController();
    markNodeLoading(nodeId, true);
    const promise = apiFetch<DocsNeighborhoodResponse>(`/api/docs/nodes/${nodeId}/details`, { signal: controller.signal })
      .then((data) => {
        if (!isCurrentScope()) return {};
        setState((current) => mergeLoadedDocsState(current, data));
        return data;
      })
      .catch((error) => {
        if (controller.signal.aborted || !isCurrentScope() || (error instanceof DOMException && error.name === "AbortError")) return {};
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

  // 展開状態の永続化。子は遅延読込のため「collapsed に無い＝展開」とは判定できず
  // （未読込は折りたたみ表示になる）、実際に開いたノードを別キーで持つ。
  const persistExpanded = useCallback((nodeId: string, expanded: boolean) => {
    const next = new Set(expandedRef.current);
    if (expanded) {
      if (next.has(nodeId)) return;
      next.add(nodeId);
    } else if (!next.delete(nodeId)) {
      return;
    }
    // localStorage が際限なく膨らまないよう、古い展開記録から捨てる（Set は挿入順）。
    while (next.size > 500) next.delete(next.values().next().value as string);
    expandedRef.current = next;
    writeCollapsed(next, EXPANDED_KEY);
  }, [expandedRef]);

  // 展開復元。前回開いていたノードの子/詳細を先読みして、再訪時に全部畳まれた状態に戻さない。
  // 1回のパスでは少数だけ投げ、読込完了で state が変わるたびに再評価されて下の階層へ伝播する。
  useEffect(() => {
    if (!networkLoadSettled || loading) return;
    if (expandRestoreLoadCountRef.current >= EXPANDED_RESTORE_LIMIT) return;
    // このパスで実際に投げたリクエスト数だけを数える。何も投げずに終わると state が
    // 変わらず次のパスが来ないため、読込不要のノードは打ち切らずその場で消化する。
    let issued = 0;
    for (const nodeId of expandedRef.current) {
      if (issued >= 8) break;
      if (expandRestoreLoadCountRef.current >= EXPANDED_RESTORE_LIMIT) break;
      if (expandRestoreDoneRef.current.has(nodeId)) continue;
      // 祖先が未読込のノードは対象外。祖先が開かれた後のパスで拾う。
      if (!nodesById.has(nodeId)) continue;
      expandRestoreDoneRef.current.add(nodeId);
      if (collapsed.has(nodeId)) continue;
      if (nodeHasChildren(nodeId) && !loadedChildrenParentIds.has(nodeId)) {
        void loadNodeChildren(nodeId);
        issued += 1;
        expandRestoreLoadCountRef.current += 1;
      }
      if (nodeHasDetails(nodeId) && !detailsLoadedIds.has(nodeId)) {
        void loadNodeDetails(nodeId);
        issued += 1;
      }
    }
  }, [
    collapsed,
    detailsLoadedIds,
    expandedRef,
    loadNodeChildren,
    loadNodeDetails,
    loadedChildrenParentIds,
    loading,
    networkLoadSettled,
    nodeHasChildren,
    nodeHasDetails,
    nodesById,
  ]);

  const expandNode = useCallback((nodeId: string) => {
    const wasCollapsed = nodeIsCollapsed(nodeId);
    persistExpanded(nodeId, true);
    setCollapsed((current) => {
      const next = new Set(current);
      next.delete(nodeId);
      writeCollapsed(next);
      return next;
    });
    expandRestoreDoneRef.current.add(nodeId);
    void Promise.all([
      ensureNodeChildrenLoaded(nodeId),
      ensureNodeDetailsLoaded(nodeId),
    ]).finally(() => {
      // 遅延読込で行DOMが差し替わっても、キーボード操作の現在地を失わない。
      setFocusRequestNodeId(nodeId);
    });
    const nextCursor = state.children_next_cursor_by_parent?.[nodeId];
    if (!wasCollapsed && nextCursor) void loadNodeChildren(nodeId, nextCursor);
  }, [ensureNodeChildrenLoaded, ensureNodeDetailsLoaded, loadNodeChildren, nodeIsCollapsed, persistExpanded, state.children_next_cursor_by_parent]);

  // Ctrl+→ / Ctrl+← の2回連続押しで、表示中の行をまとめて展開/格納する。
  // 対象は「今表示している行の1階層だけ」に限る。子孫まで再帰的に開くと Home など
  // 上位ノードが重くなるため、広げるかどうかの判断は毎回ユーザーの操作に委ねる。
  const visibleBulkTargets = useCallback((expand: boolean) => currentRows
    .map((row) => row.node.id)
    .filter((nodeId) => nodeHasExpandableContent(nodeId) && nodeIsCollapsed(nodeId) === expand),
  [currentRows, nodeHasExpandableContent, nodeIsCollapsed]);

  const expandVisibleNodes = useCallback(() => {
    const nodeIds = visibleBulkTargets(true);
    if (nodeIds.length === 0) return;
    setCollapsed((current) => {
      const next = new Set(current);
      for (const nodeId of nodeIds) next.delete(nodeId);
      writeCollapsed(next);
      return next;
    });
    for (const nodeId of nodeIds) {
      persistExpanded(nodeId, true);
      expandRestoreDoneRef.current.add(nodeId);
      void ensureNodeChildrenLoaded(nodeId);
      void ensureNodeDetailsLoaded(nodeId);
    }
    toast.success(`表示中の${nodeIds.length}ノードを展開しました`);
  }, [ensureNodeChildrenLoaded, ensureNodeDetailsLoaded, persistExpanded, setCollapsed, visibleBulkTargets]);

  const collapseVisibleNodes = useCallback(() => {
    const nodeIds = visibleBulkTargets(false);
    if (nodeIds.length === 0) return;
    setCollapsed((current) => {
      const next = new Set(current);
      for (const nodeId of nodeIds) next.add(nodeId);
      writeCollapsed(next);
      return next;
    });
    for (const nodeId of nodeIds) {
      persistExpanded(nodeId, false);
      abortBodyNodeLoads(nodeId);
    }
    toast.success(`表示中の${nodeIds.length}ノードを格納しました`);
  }, [abortBodyNodeLoads, persistExpanded, setCollapsed, visibleBulkTargets]);

  // サイドバーは本文アウトラインと別の展開状態を持つ。DOMに現在表示
  // されている行だけを対象にすることで、未表示の巨大な子孫を一括で
  // 読み込まず、Ctrl+→を段階的な展開操作として扱う。
  const visibleSidebarBulkTargets = useCallback((expand: boolean) => {
    if (typeof document === "undefined") return [];
    const visibleIds = Array.from(document.querySelectorAll<HTMLElement>("[data-docs-sidebar-node-id]"))
      .map((element) => element.getAttribute("data-docs-sidebar-node-id"))
      .filter((nodeId): nodeId is string => Boolean(nodeId));
    return Array.from(new Set(visibleIds))
      .filter((nodeId) => sidebarNodeHasChildren(nodeId) && sidebarCollapsed.has(nodeId) !== expand);
  }, [sidebarCollapsed, sidebarNodeHasChildren]);

  const expandSidebarNode = useCallback((nodeId: string) => {
    void ensureNodeChildrenLoaded(nodeId);
    setSidebarCollapsed((current) => {
      if (current.has(nodeId)) return current;
      const next = new Set(current);
      next.add(nodeId);
      return next;
    });
  }, [ensureNodeChildrenLoaded, setSidebarCollapsed]);

  const collapseSidebarNode = useCallback((nodeId: string) => {
    setSidebarCollapsed((current) => {
      if (!current.has(nodeId)) return current;
      const next = new Set(current);
      next.delete(nodeId);
      return next;
    });
  }, [setSidebarCollapsed]);

  const expandVisibleSidebarNodes = useCallback(() => {
    const nodeIds = visibleSidebarBulkTargets(true);
    if (nodeIds.length === 0) return;
    setSidebarCollapsed((current) => {
      const next = new Set(current);
      for (const nodeId of nodeIds) next.add(nodeId);
      return next;
    });
    for (const nodeId of nodeIds) void ensureNodeChildrenLoaded(nodeId);
    toast.success(`表示中の${nodeIds.length}サイドバーノードを展開しました`);
  }, [ensureNodeChildrenLoaded, setSidebarCollapsed, visibleSidebarBulkTargets]);

  const collapseVisibleSidebarNodes = useCallback(() => {
    const nodeIds = visibleSidebarBulkTargets(false);
    if (nodeIds.length === 0) return;
    setSidebarCollapsed((current) => {
      const next = new Set(current);
      for (const nodeId of nodeIds) next.delete(nodeId);
      return next;
    });
    toast.success(`表示中の${nodeIds.length}サイドバーノードを格納しました`);
  }, [setSidebarCollapsed, visibleSidebarBulkTargets]);

  const toggleSidebarCollapsed = useCallback((nodeId: string) => {
    const expanding = !sidebarCollapsed.has(nodeId);
    if (expanding) void ensureNodeChildrenLoaded(nodeId);
    setSidebarCollapsed((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
  }, [ensureNodeChildrenLoaded, setSidebarCollapsed, sidebarCollapsed]);

  useEffect(() => {
    expandNodeRef.current = expandNode;
  }, [expandNode]);

  const applyOpenDocsNodeSelection = useCallback((nodeId: string) => {
    setMainView(null);
    setTagPageId(null);
    setFocusNodeId(nodeId);
    selectSingleNode(nodeId);
  }, [selectSingleNode]);

  const openDocsNode = useCallback(async (nodeId: string): Promise<DocsLoadStatus> => {
    if (!workspaceMountedRef.current) {
      throw new Error(DOCS_WORKSPACE_UNMOUNTED_MESSAGE);
    }
    const generation = ++openDocsNodeGenerationRef.current;
    const finishIfCurrent = (status: DocsLoadStatus): DocsLoadStatus => {
      if (!workspaceMountedRef.current) {
        throw new Error(DOCS_WORKSPACE_UNMOUNTED_MESSAGE);
      }
      if (generation !== openDocsNodeGenerationRef.current) return "superseded";
      return status;
    };

    const shouldCommitOpen = () => generation === openDocsNodeGenerationRef.current;

    try {
      await flushPendingDocsEditorWritesBeforeNavigation();
    } catch (error) {
      // Keep the current selection and editor draft intact when a blur/PATCH
      // failed.  The commit path already reports its own error; this fallback
      // also covers non-PATCH editor operations that reject before persistence.
      if (generation === openDocsNodeGenerationRef.current) {
        toast.error(error instanceof Error ? error.message : "Docsの保存に失敗しました。入力内容を確認してください");
      }
      return finishIfCurrent("failed");
    }

    if (nodesByIdRef.current.has(nodeId)) {
      applyOpenDocsNodeSelection(nodeId);
      if (!docsReadOnly) {
        void loadNodeNeighborhood(nodeId, { shouldCommit: shouldCommitOpen });
      }
      void ensureNodeChildrenLoaded(nodeId);
      void ensureNodeDetailsLoaded(nodeId);
      return finishIfCurrent("success");
    }

    if (docsReadOnly) {
      toast.error(DOCS_OPEN_NODE_FAILED_MESSAGE);
      return finishIfCurrent("failed");
    }

    let neighborhood: DocsNeighborhoodResponse | Record<string, never> = {};
    let timeoutId: ReturnType<typeof setTimeout> | undefined;
    try {
      const request = loadNodeNeighborhood(nodeId, {
        quiet: true,
        shouldCommit: shouldCommitOpen,
      });
      const inFlight = nodeNeighborhoodInFlightRef.current.get(nodeId);
      neighborhood = await Promise.race([
        request,
        new Promise<never>((_, reject) => {
          timeoutId = setTimeout(() => {
            inFlight?.controller.abort();
            reject(new DOMException("The operation timed out.", "TimeoutError"));
          }, docsOpenNodeTimeoutMs);
        }),
      ]);
    } catch {
      neighborhood = {};
    } finally {
      if (timeoutId !== undefined) clearTimeout(timeoutId);
    }

    const currentStatus = finishIfCurrent("success");
    if (currentStatus !== "success") return currentStatus;

    const nodeReady = nodesByIdRef.current.has(nodeId)
      || Boolean(neighborhood.nodes?.some((node) => node.id === nodeId));
    if (!nodeReady) {
      toast.error(DOCS_OPEN_NODE_FAILED_MESSAGE);
      return "failed";
    }

    applyOpenDocsNodeSelection(nodeId);
    void ensureNodeChildrenLoaded(nodeId);
    void ensureNodeDetailsLoaded(nodeId);
    return "success";
  }, [
    applyOpenDocsNodeSelection,
    docsReadOnly,
    ensureNodeChildrenLoaded,
    ensureNodeDetailsLoaded,
    flushPendingDocsEditorWritesBeforeNavigation,
    loadNodeNeighborhood,
  ]);

  const openDocsNodeRef = useRef(openDocsNode);
  openDocsNodeRef.current = openDocsNode;

  useEffect(() => {
    // handlerはWorkspaceのmount中は同一identityで登録し、openerだけ
    // ref経由で最新化する。state更新のたびに再登録すると、in-flight中に
    // 旧handlerがstale扱いとなり、意図しないroute fallbackを起こすため。
    const openClipResultNode = async (nodeId: string) => {
      if (!workspaceMountedRef.current) {
        throw new Error(DOCS_WORKSPACE_UNMOUNTED_MESSAGE);
      }
      await openDocsNodeRef.current(nodeId);
      if (!workspaceMountedRef.current) {
        throw new Error(DOCS_WORKSPACE_UNMOUNTED_MESSAGE);
      }
    };
    setOpenNodeHandler(openClipResultNode);
    return () => setOpenNodeHandler(null);
  }, [setOpenNodeHandler]);

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

  const openToday = useCallback(async (date?: string): Promise<DocsLoadStatus> => {
    // Today and page navigation share one latest-navigation-wins token. A
    // newer openDocsNode/openToday invalidates this request while it is
    // waiting for the editor save barrier or network load.
    const generation = ++openDocsNodeGenerationRef.current;
    const isCurrentNavigation = () => generation === openDocsNodeGenerationRef.current;
    try {
      await flushPendingDocsEditorWritesBeforeNavigation();
    } catch (error) {
      // Do not change the view until the active editor write is known to have
      // succeeded.  Keeping the existing view mounted preserves the draft so
      // the user can retry after the error toast.
      if (isCurrentNavigation()) {
        toast.error(error instanceof Error ? error.message : "Docsの保存に失敗しました。入力内容を確認してください");
      }
      return isCurrentNavigation() ? "failed" : "superseded";
    }
    if (!isCurrentNavigation()) return "superseded";
    setMainView(null);
    setTagPageId(null);
    const status = await load({ focusToday: true, date });
    return isCurrentNavigation() ? status : "superseded";
  }, [flushPendingDocsEditorWritesBeforeNavigation, load]);

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
    const currentTargetNode = () => {
      const activeElement = document.activeElement as HTMLElement | null;
      const bodyNodeId = activeElement?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
      if (bodyNodeId) return { nodeId: bodyNodeId, surface: "body" as const };
      // The sidebar row itself carries the data attribute, while its chevron
      // button is a sibling. Treat a focused chevron as the same sidebar row
      // instead of falling back to the body selection.
      const sidebarNodeElement = activeElement?.closest("[data-docs-sidebar-node-id]")
        ?? activeElement?.parentElement?.querySelector("[data-docs-sidebar-node-id]");
      const sidebarNodeId = sidebarNodeElement?.getAttribute("data-docs-sidebar-node-id");
      if (sidebarNodeId) return { nodeId: sidebarNodeId, surface: "sidebar" as const };
      const fallbackNodeId = selectedNodeIdsRef.current[0] ?? selectionAnchorNodeIdRef.current ?? null;
      return fallbackNodeId ? { nodeId: fallbackNodeId, surface: "body" as const } : null;
    };
    const handleGlobalKey = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      const target = event.target instanceof Element ? event.target : null;
      const keyboardTarget = target ?? (document.activeElement instanceof Element ? document.activeElement : null);
      if (shouldArchiveSelectionFromKeyboard({
        key: event.key,
        ctrlKey: event.ctrlKey,
        altKey: event.altKey,
        metaKey: event.metaKey,
        shiftKey: event.shiftKey,
        selectedCount: selectedNodeIdsRef.current.length,
        target: keyboardTarget,
        hasNonCollapsedTextSelection: hasCodeMirrorRangeSelection(keyboardTarget),
      })) {
        event.preventDefault();
        event.stopImmediatePropagation();
        void archiveSelectedNodesRef.current();
        return;
      }
      const key = event.key.toLowerCase();
      const editableTarget = target?.closest(".cm-editor, input, textarea, select, [contenteditable='true']");
      const docsEditorTarget = target?.closest("[data-testid='docs-block-editor']");
      if (
        event.ctrlKey
        && event.altKey
        && !event.shiftKey
        && !event.metaKey
        && key === "i"
        && !docsReadOnly
      ) {
        event.preventDefault();
        event.stopImmediatePropagation();
        openClipIngest();
        return;
      }
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
        const target = currentTargetNode();
        if (target) {
          event.preventDefault();
          event.stopImmediatePropagation();
          const direction = event.key === "ArrowRight" ? "expand" : "collapse";
          // 長押しのオートリピートを2回目と数えると、押しっぱなしで一括操作が暴発する。
          const previous = event.repeat ? null : bulkArrowRef.current;
          const isSecondPress = previous?.direction === direction
            && previous.surface === target.surface
            && Date.now() - previous.at <= 500;
          bulkArrowRef.current = event.repeat || isSecondPress
            ? null
            : { direction, surface: target.surface, at: Date.now() };
          if (isSecondPress) {
            if (target.surface === "sidebar") {
              if (direction === "expand") expandVisibleSidebarNodes();
              else collapseVisibleSidebarNodes();
            } else if (direction === "expand") expandVisibleNodes();
            else collapseVisibleNodes();
            return;
          }
          if (target.surface === "sidebar") {
            if (direction === "expand") expandSidebarNode(target.nodeId);
            else collapseSidebarNode(target.nodeId);
          } else if (direction === "expand") {
            expandNodeRef.current(target.nodeId);
          } else {
            persistExpanded(target.nodeId, false);
            setCollapsed((current) => {
              const next = new Set(current);
              next.add(target.nodeId);
              writeCollapsed(next);
              return next;
            });
            abortBodyNodeLoads(target.nodeId);
          }
        }
        return;
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && key === "\\") {
        event.preventDefault();
        event.stopImmediatePropagation();
        const target = currentTargetNode();
        if (target) {
          setSidebarCollapsed((current) => {
            const next = new Set(current);
            if (next.has(target.nodeId)) next.delete(target.nodeId);
            else next.add(target.nodeId);
            return next;
          });
        }
        return;
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && (event.key === "." || key === ".")) {
        const target = currentTargetNode();
        if (target) {
          event.preventDefault();
          event.stopImmediatePropagation();
          openDocsNode(target.nodeId);
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
  }, [abortBodyNodeLoads, collapseSidebarNode, collapseVisibleNodes, collapseVisibleSidebarNodes, currentRows, docsReadOnly, expandSidebarNode, expandVisibleNodes, expandVisibleSidebarNodes, extendNodeSelection, focusNode, nodesById, openClipIngest, openDocsNode, openToday, persistExpanded, selectRangeToNode, selectSingleNode, selectionAnchorNodeId, splitRows]);

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
    const target = nodesByIdRef.current.get(nodeId);
    if (docsReadOnly || target?.permission === "read") {
      if (target) return target;
      throw new Error("このDocsは閲覧専用です");
    }
    try {
      return await docsSaveQueue.enqueue(nodeId, {
        execute: async () => {
          await nodeCreateInFlightRef.current.get(nodeId);
          const data = await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${nodeId}`, {
            method: "PATCH",
            // Send the library/project identity alongside content edits. The
            // server still treats the node's persisted library as canonical,
            // but explicit scope keeps foreign Project writes from falling
            // back to the actor's Personal Library in mixed-version clients.
            body: JSON.stringify({
              ...(target?.docs_library_id ? { docs_library_id: target.docs_library_id } : {}),
              ...(target ? { project_id: target.project_id } : {}),
              ...patch,
            }),
            keepalive: true,
          });
          return data.node;
        },
        apply: (node) => {
          if (!node || typeof node.id !== "string") return;
          setState((current) => ({ ...current, nodes: validDocsNodes(current.nodes).map((item) => (item.id === node.id ? node : item)) }));
        },
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Docsの保存に失敗しました。もう一度確定してください");
      throw error;
    }
  }, [apiFetch, docsReadOnly, docsSaveQueue]);

  const archiveNode = useCallback((nodeId: string) => {
    const target = nodesByIdRef.current.get(nodeId);
    if (docsReadOnly || target?.permission === "read") return Promise.resolve(target);
    return (
    docsSaveQueue.enqueue(nodeId, {
      execute: async () => {
        await nodeCreateInFlightRef.current.get(nodeId);
        const data = await apiFetch<{
          node: DocsNode;
          committed?: boolean;
          task_binding_error?: string | null;
        }>(`/api/docs/nodes/${nodeId}`, { method: "DELETE", keepalive: true });
        if (data.task_binding_error) {
          toast.error("ノードは削除されましたが、タスク連携解除に失敗しました");
        }
        return data.node;
      },
      apply: (node) => {
        if (!node || typeof node.id !== "string") return;
        setState((current) => ({ ...current, nodes: validDocsNodes(current.nodes).map((item) => (item.id === node.id ? node : item)) }));
      },
    })
    );
  }, [apiFetch, docsReadOnly, docsSaveQueue]);

  const restoreNode = useCallback((nodeId: string) => {
    const target = nodesByIdRef.current.get(nodeId);
    if (docsReadOnly || target?.permission === "read") return Promise.resolve(target);
    return (
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
        if (!node || typeof node.id !== "string") return;
        setState((current) => ({ ...current, nodes: validDocsNodes(current.nodes).map((item) => (item.id === node.id ? node : item)) }));
      },
    })
    );
  }, [apiFetch, docsReadOnly, docsSaveQueue]);

  const permanentlyDeleteNode = useCallback(async (nodeId: string) => {
    const target = nodesByIdRef.current.get(nodeId);
    if (docsReadOnly || target?.permission === "read") return;
    await apiFetch<{ ok: boolean }>(`/api/docs/nodes/${nodeId}?permanent=1`, { method: "DELETE" });
    setState((current) => ({ ...current, nodes: current.nodes.filter((node) => node.id !== nodeId) }));
  }, [apiFetch, docsReadOnly]);

  const createNode = useCallback((
    parentId: string | null,
    afterNode?: DocsNode | null,
    title = "",
    options: {
      bodyJson?: Record<string, unknown>;
      displayProps?: Record<string, unknown>;
      nodeType?: DocsNode["node_type"];
      optimistic?: boolean;
      focusOnCreate?: boolean;
      /** Insert before the first sibling when no afterNode is supplied. */
      insertAtStart?: boolean;
      onPersistenceError?: (nodeId: string, error: unknown) => void;
      supertagIds?: string[];
    } = {},
  ) => {
    const parent = parentId ? nodesById.get(parentId) ?? null : null;
    if (docsReadOnly || (parentId && parent?.permission === "read")) {
      return Promise.reject(new Error("このDocsは閲覧専用です"));
    }
    const id = globalThis.crypto?.randomUUID?.() ?? `docs-node-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const siblings = parentId ? childrenByParent.get(parentId) ?? [] : roots;
    const insertAtStart = options.insertAtStart === true && !afterNode;
    const afterIndex = afterNode
      ? siblings.findIndex((node) => node.id === afterNode.id)
      : insertAtStart
        ? -1
        : siblings.length - 1;
    const previous = afterIndex >= 0 ? siblings[afterIndex] : null;
    const next = afterIndex >= 0 ? siblings[afterIndex + 1] : siblings[0];
    const sortOrder = midpointSortOrder(previous?.sort_order, next?.sort_order);
    const now = new Date().toISOString();
    const optimisticNode: DocsNode = {
      id,
      docs_library_id: parent?.docs_library_id
        ?? afterNode?.docs_library_id
        ?? activeDocsLibraryId
        ?? roots[0]?.docs_library_id
        ?? "",
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
    const optimisticSupertags = (options.supertagIds ?? []).map((supertagId) => ({
      node_id: id,
      supertag_id: supertagId,
    }));
    nodeTagIdsRef.current.set(id, optimisticSupertags.map((relation) => relation.supertag_id));
    flushSync(() => {
      setState((current) => ({
        ...current,
        nodes: [...validDocsNodes(current.nodes), optimisticNode],
        node_supertags: [...current.node_supertags, ...optimisticSupertags],
      }));
    });
    const persistNode = apiFetch<{ node: DocsNode }>("/api/docs", {
      method: "POST",
      body: JSON.stringify({
        id,
        parent_id: parentId,
        ...(optimisticNode.docs_library_id ? { docs_library_id: optimisticNode.docs_library_id } : {}),
        project_id: optimisticNode.project_id,
        title,
        body_text: title,
        body_json: optimisticNode.body_json,
        display_props: optimisticNode.display_props,
        node_type: optimisticNode.node_type,
        sort_order: sortOrder,
        supertag_ids: options.supertagIds ?? [],
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
        nodes: validDocsNodes(current.nodes).map((node) => (
          node.id === id
            ? {
                ...data.node,
                ...node,
                docs_library_id: data.node.docs_library_id || node.docs_library_id,
                created_at: data.node.created_at ?? node.created_at,
              }
            : node
        )),
      }));
      if (!options.optimistic && options.focusOnCreate !== false) {
        selectSingleNode(data.node.id);
        setFocusRequestNodeId(data.node.id);
      }
      return data.node;
    };
    const rollbackNode = (error: unknown) => {
      nodeTagIdsRef.current.delete(id);
      setState((current) => ({
        ...current,
        nodes: current.nodes.filter((node) => node.id !== id),
        node_supertags: current.node_supertags.filter((relation) => relation.node_id !== id),
      }));
      throw error;
    };
    if (options.optimistic) {
      void persistNode.then(applyPersistedNode).catch((error) => {
        options.onPersistenceError?.(id, error);
        nodeTagIdsRef.current.delete(id);
        setState((current) => ({
          ...current,
          nodes: validDocsNodes(current.nodes).filter((node) => node.id !== id),
          node_supertags: current.node_supertags.filter((relation) => relation.node_id !== id),
        }));
        toast.error(error instanceof Error ? error.message : "Docsノードの作成に失敗しました");
      });
      if (options.focusOnCreate !== false) {
        selectSingleNode(optimisticNode.id);
        setFocusRequestNodeId(optimisticNode.id);
      }
      return optimisticNode;
    }
    return persistNode.then(applyPersistedNode).catch(rollbackNode);
  }, [activeDocsLibraryId, apiFetch, childrenByParent, docsReadOnly, nodesById, roots, selectSingleNode]);

  const createRootNode = useCallback(async () => {
    if (!canCreateRootNode) return;
    // The root action is explicit creation (unlike Enter's editor blank row),
    // so seed it with a meaningful title rather than persisting an empty node.
    const node = await createNode(null, null, "新しいノート");
    openDocsNode(node.id);
    setFocusRequestNodeId(node.id);
  }, [canCreateRootNode, createNode, openDocsNode]);

  const pushHistory = useCallback((entry: BlockHistoryEntry) => {
    if (applyingHistoryRef.current || entry.patches.length === 0) return;
    undoStackRef.current = [...undoStackRef.current, entry].slice(-100);
    redoStackRef.current = [];
  }, []);

  const foldIntoPendingCreateHistory = useCallback((node: DocsNode) => {
    const previousEntry = undoStackRef.current.at(-1);
    const previousPatch = previousEntry?.patches.length === 1 ? previousEntry.patches[0] : null;
    const createEntry = undoStackRef.current.at(-2);
    const createPatch = createEntry?.patches.length === 1 ? createEntry.patches[0] : null;
    // A just-created blank can receive an initial empty commit while its
    // editor mounts. Fold that update back into the create entry as well, so
    // the first Ctrl+Z still archives the row instead of undoing a no-op
    // update and leaving the blank node behind.
    if (
      previousPatch?.type === "update"
      && previousPatch.id === node.id
      && previousPatch.after.title === ""
      && previousPatch.after.body_json?.blank === true
      && createPatch?.type === "create"
      && createPatch.node.id === node.id
    ) {
      undoStackRef.current = [
        ...undoStackRef.current.slice(0, -2),
        {
          label: createEntry?.label ?? "ノード作成",
          patches: [{ type: "create", node: snapshotDocsNode(node) }],
        },
      ];
      redoStackRef.current = [];
      return true;
    }
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
      nodes: validDocsNodes(current.nodes).some((item) => item.id === node.id)
        ? validDocsNodes(current.nodes).map((item) => (item.id === node.id ? node : item))
        : [...validDocsNodes(current.nodes), node],
    }));
    return node;
  }, [patchNode, restoreNode]);

  const applyHistoryEntry = useCallback(async (entry: BlockHistoryEntry) => {
    const nodeIds = entry.patches.map((patch) => patch.type === "update" ? patch.id : patch.node.id);
    const titles = Object.fromEntries(
      entry.patches
        .filter((patch): patch is Extract<BlockHistoryEntry["patches"][number], { type: "update" | "create" }> => patch.type === "update" || patch.type === "create")
        .map((patch) => [patch.type === "update" ? patch.id : patch.node.id, patch.type === "update" ? patch.after.title : patch.node.title]),
    );
    // Publish the intended canonical title before awaiting network writes. The
    // outline can then update an already-mounted CodeMirror document even if
    // the user blurs during the request.
    setHistorySync((current) => ({ revision: current.revision + 1, nodeIds, titles }));
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
      // Each history patch updates the local projection as part of its queue
      // operation.  Do not re-run the full Docs loader here: it temporarily
      // unmounts the outline (and its pending blank editor row), which drops
      // focus exactly when Undo/Redo is expected to remain in the editor.
      // Keep the intended titles attached to the session through the final
      // projection render. Clearing the hint in a second state update lets a
      // stale pre-PATCH row overwrite the mounted CM view during the narrow
      // response/render gap; the next history operation replaces this
      // revision, while the server response itself still updates `rows`.
      setHistorySync((current) => ({ revision: current.revision + 1, nodeIds, titles: current.titles ?? titles }));
    } finally {
      applyingHistoryRef.current = false;
    }
  }, [archiveNode, createNodeFromSnapshot, patchNode]);

  const undoDocsOperation = useCallback(async () => {
    await editorCommitInFlightRef.current?.catch(() => false);
    // Structural actions and line commits share the node save queue.  Wait for
    // all already-enqueued writes before applying the inverse so a late PATCH
    // cannot overwrite the state restored by Undo.
    await docsSaveQueue.flush();
    const entry = undoStackRef.current.at(-1);
    console.log("DEBUG workspace undo entry", JSON.stringify(entry?.patches.map((patch) => ({ type: patch.type, id: patch.type === "update" ? patch.id : patch.node.id, title: patch.type === "update" ? patch.after.title : patch.node.title }))));
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
  }, [applyHistoryEntry, docsSaveQueue]);

  const redoDocsOperation = useCallback(async () => {
    await editorCommitInFlightRef.current?.catch(() => false);
    await docsSaveQueue.flush();
    const entry = redoStackRef.current.at(-1);
    if (!entry) return;
    redoStackRef.current = redoStackRef.current.slice(0, -1);
    await applyHistoryEntry(entry);
    undoStackRef.current = [...undoStackRef.current, entry];
  }, [applyHistoryEntry, docsSaveQueue]);

  const archiveNodesWithHistory = useCallback(async (inputNodes: DocsNode[]) => {
    if (inputNodes.length === 0) return false;
    const normalizedIds = resolveArchiveTargets(inputNodes.map((node) => node.id), parentIdByNodeId);
    const normalizedNodes = normalizedIds
      .map((nodeId) => nodesByIdRef.current.get(nodeId))
      .filter((node): node is DocsNode => node != null && !node.archived_at);
    if (normalizedNodes.length === 0) return false;
    if (normalizedNodes.some((node) => !canWriteNode(node))) {
      toast.error("このDocsは閲覧専用です");
      return false;
    }

    const archiveResults = await Promise.all(
      normalizedNodes.map(async (node) => {
        try {
          await archiveNode(node.id);
          return { ok: true as const, node };
        } catch {
          return { ok: false as const, node };
        }
      }),
    );
    const successNodes = archiveResults.filter((result) => result.ok).map((result) => result.node);
    if (successNodes.length === 0) {
      toast.error("ノードのアーカイブに失敗しました");
      return false;
    }

    const successIds = successNodes.map((node) => node.id);
    pushHistory({
      label: successNodes.length > 1 ? "ノード一括削除" : "ノード削除",
      patches: successNodes.map((node) => ({
        type: "archive" as const,
        node: snapshotDocsNode(node),
      })),
    });

    const removedIds = expandArchivedNodeIds(successIds, parentIdByNodeId);
    if (splitNodeId && removedIds.has(splitNodeId)) {
      setSplitNodeId(null);
    }

    const deletedFocusPageId = focusNodeIdRef.current && removedIds.has(focusNodeIdRef.current)
      ? focusNodeIdRef.current
      : null;
    if (deletedFocusPageId) {
      const deletedPage = nodesByIdRef.current.get(deletedFocusPageId);
      const fallbackPageId = deletedPage?.parent_id
        && nodesByIdRef.current.get(deletedPage.parent_id)
        && !nodesByIdRef.current.get(deletedPage.parent_id)?.archived_at
        ? deletedPage.parent_id
        : roots.find((root) => !removedIds.has(root.id))?.id ?? null;
      if (fallbackPageId) openDocsNode(fallbackPageId);
      else selectSingleNode(null);
      if (successNodes.length < normalizedNodes.length) {
        toast.error("ノードのアーカイブに失敗しました");
        return false;
      }
      return true;
    }

    const visibleRowIds = currentRows.map((row) => row.node.id);
    const currentFocusId = selectedNodeId
      ?? selectedNodeIdsRef.current.at(-1)
      ?? selectionAnchorNodeIdRef.current
      ?? null;
    const nextFocusId = resolveFocusAfterArchive({
      removedIds,
      visibleRowIds,
      currentFocusId,
      parentIdByNodeId,
    });

    const survivingSelected = selectedNodeIdsRef.current.filter((nodeId) => !removedIds.has(nodeId));
    if (survivingSelected.length > 0) {
      selectedNodeIdsRef.current = survivingSelected;
      setSelectedNodeIds(survivingSelected);
      const anchor = selectionAnchorNodeIdRef.current;
      const nextAnchor = anchor && !removedIds.has(anchor) ? anchor : survivingSelected[0];
      selectionAnchorNodeIdRef.current = nextAnchor;
      setSelectionAnchorNodeId(nextAnchor);
      const lastSurviving = survivingSelected[survivingSelected.length - 1];
      const focusId = currentFocusId
        && survivingSelected.includes(currentFocusId)
        && !removedIds.has(currentFocusId)
        ? currentFocusId
        : lastSurviving;
      setSelectedNodeId(focusId ?? null);
      setFocusRequestNodeId(focusId ?? null);
    } else if (nextFocusId) {
      selectSingleNode(nextFocusId);
      setFocusRequestNodeId(nextFocusId);
    } else {
      selectSingleNode(null);
      setFocusRequestNodeId(null);
    }

    if (successNodes.length < normalizedNodes.length) {
      toast.error("ノードのアーカイブに失敗しました");
      return false;
    }
    return true;
  }, [
    archiveNode,
    currentRows,
    openDocsNode,
    parentIdByNodeId,
    pushHistory,
    roots,
    selectSingleNode,
    selectedNodeId,
    setFocusRequestNodeId,
    setSelectedNodeIds,
    setSelectionAnchorNodeId,
    splitNodeId,
  ]);

  const archiveSelectedNodes = useCallback(async () => {
    const fallbackId = selectedNodeId
      ?? selectedNodeIdsRef.current.at(-1)
      ?? selectionAnchorNodeIdRef.current
      ?? null;
    if (!fallbackId) return;
    const actionIds = resolveActionNodeIds(selectedNodeIdsRef.current, fallbackId);
    const nodes = actionIds
      .map((nodeId) => nodesByIdRef.current.get(nodeId))
      .filter((node): node is DocsNode => Boolean(node));
    await archiveNodesWithHistory(nodes);
  }, [archiveNodesWithHistory, selectedNodeId]);

  useEffect(() => {
    undoDocsOperationRef.current = undoDocsOperation;
    redoDocsOperationRef.current = redoDocsOperation;
    archiveSelectedNodesRef.current = archiveSelectedNodes;
  }, [archiveSelectedNodes, redoDocsOperation, undoDocsOperation]);

  const toggleCollapsed = (nodeId: string) => {
    const expanding = nodeIsCollapsed(nodeId);
    if (expanding) {
      expandNode(nodeId);
      return;
    }
    persistExpanded(nodeId, false);
    setCollapsed((current) => {
      const next = new Set(current);
      next.add(nodeId);
      writeCollapsed(next);
      return next;
    });
    abortBodyNodeLoads(nodeId);
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
    if (!selectedNodeIdsRef.current.includes(node.id)) {
      selectSingleNode(node.id);
    }
    setSidebarContextMenu({ x: event.clientX, y: event.clientY, nodeId: node.id });
  }, [selectSingleNode]);

  const openDocumentContextMenu = useCallback((event: ReactMouseEvent<HTMLElement>, node: DocsNode) => {
    event.preventDefault();
    event.stopPropagation();
    selectSingleNode(node.id);
    setDocumentContextMenu({ nodeId: node.id, x: event.clientX, y: event.clientY });
  }, [selectSingleNode]);

  const dropSidebarNode = useCallback(async (targetNode: DocsNode) => {
    if (!dragSidebarNodeId || dragSidebarNodeId === targetNode.id) {
      setDragSidebarNodeId(null);
      return;
    }
    try {
      await flushPendingDocsEditorWritesBeforeNavigation();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Docsの保存に失敗しました。入力内容を確認してください");
      return;
    }
    const draggedNode = nodesById.get(dragSidebarNodeId);
    if (!draggedNode) return;
    if (!canWriteNode(targetNode) || !canWriteNode(draggedNode)) return;
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
  }, [dragSidebarNodeId, flushPendingDocsEditorWritesBeforeNavigation, load, nodesById]);

  const archiveSidebarNode = useCallback(async (node: DocsNode) => {
    setSidebarContextMenu(null);
    try {
      const actionIds = resolveActionNodeIds(selectedNodeIdsRef.current, node.id);
      const nodes = actionIds
        .map((nodeId) => nodesById.get(nodeId))
        .filter((item): item is DocsNode => Boolean(item));
      const archived = await archiveNodesWithHistory(nodes);
      if (archived) toast.success("ノードをアーカイブしました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "ノードのアーカイブに失敗しました");
    }
  }, [archiveNodesWithHistory, nodesById]);

  async function renameSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    if (!canWriteNode(node)) return;
    const nextTitle = window.prompt("ノード名を変更", nodeText(node));
    if (!nextTitle || nextTitle.trim() === node.title) return;
    await patchNode(node.id, { title: nextTitle.trim() });
  }

  async function duplicateSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    if (!canWriteNode(node)) return;
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

  async function copySidebarNodeReference(node: DocsNode) {
    setSidebarContextMenu(null);
    await navigator.clipboard.writeText(
      createDocsNodeWikilink(node.id, nodeText(node)),
    );
    toast.success("チャット用参照をコピーしました");
  }

  async function copySidebarNodeId(node: DocsNode) {
    setSidebarContextMenu(null);
    await navigator.clipboard.writeText(node.id);
    toast.success("ノードIDをコピーしました");
  }

  async function pinSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    if (!canWriteNode(node)) return;
    await updateDisplayProps(node, { pinned_sidebar: node.display_props?.pinned_sidebar !== true });
  }

  function moveSidebarNodeWithReference(node: DocsNode) {
    setSidebarContextMenu(null);
    if (!canWriteNode(node)) return;
    selectSingleNode(node.id);
    window.setTimeout(() => requestDocsCommand({ kind: "move", leaveReference: true }), 0);
  }

  const mutateTag = async (node: DocsNode, tagId: string, mode: "add" | "remove") => {
    if (!canWriteNode(node)) return;
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
    if (!canWriteNode(node)) return;
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
    const node = nodesByIdRef.current.get(attachment.node_id);
    if (!canWriteNode(node)) return;
    await apiFetch<{ ok: true }>(`/api/docs/attachments/${attachment.id}`, { method: "DELETE" });
    setState((current) => ({
      ...current,
      attachments: current.attachments.filter((item) => item.id !== attachment.id),
    }));
  };

  /** Upload pasted/dropped images through the same node attachment pipeline. */
  const insertDocsImages = useCallback(async (
    node: DocsNode,
    files: readonly File[],
    source: "paste" | "drop",
  ) => {
    if (docsReadOnly || node.permission === "read") return;
    const imageFiles = getImageFiles(files);
    if (imageFiles.length === 0) return;

    let uploadedCount = 0;
    let failedCount = 0;
    for (const file of imageFiles) {
      try {
        const form = new FormData();
        form.append("file", file, file.name || "pasted-image");
        // Do not use docs-utils/apiFetch here: it intentionally supplies an
        // application/json Content-Type, which would prevent the browser from
        // adding the multipart boundary required by FormData.
        const response = await fetch(`/api/docs/nodes/${node.id}/attachments`, {
          method: "POST",
          credentials: "include",
          body: form,
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
          const detail = typeof payload?.detail === "string" ? payload.detail : response.statusText;
          throw new Error(detail || `画像の保存に失敗しました (${response.status})`);
        }
        const attachment = await response.json() as DocsAttachment;
        if (!attachment || attachment.node_id !== node.id || typeof attachment.id !== "string") {
          throw new Error("画像添付の応答が不正です");
        }
        uploadedCount += 1;
        // Publish each completed upload immediately. Sequential requests keep
        // the visible order identical to the paste/drop file order.
        setState((current) => {
          const existingIndex = current.attachments.findIndex((item) => item.id === attachment.id);
          if (existingIndex < 0) {
            return { ...current, attachments: [...current.attachments, attachment] };
          }
          const attachments = [...current.attachments];
          attachments[existingIndex] = attachment;
          return { ...current, attachments };
        });
      } catch (error) {
        failedCount += 1;
        // Continue uploading later files so one rejected image does not hide
        // otherwise valid images from a multi-file paste/drop.
        console.warn("Docs画像添付の保存に失敗しました", error);
      }
    }

    if (failedCount > 0) {
      const sourceLabel = source === "paste" ? "貼り付けた" : "ドロップした";
      if (uploadedCount === 0) {
        toast.error(`${sourceLabel}画像を保存できませんでした`);
      } else {
        toast.error(`${failedCount}件の${sourceLabel}画像を保存できませんでした`);
      }
    }
  }, [docsReadOnly]);

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
    if (!canWriteNode(node)) return;
    const canonical = nodesById.get(node.id) ?? node;
    await patchNode(node.id, { display_props: { ...safeNodeDisplayProps(canonical), ...patch } });
  };

  const tableSupertagIdFor = (parent: DocsNode, rows: DocsNode[]) => {
    const configured = safeNodeDisplayProps(parent).table_supertag_id;
    if (typeof configured === "string" && tagById.has(configured)) return configured;
    const counts = new Map<string, number>();
    for (const row of rows) {
      for (const tag of nodeTags.get(row.id) ?? []) {
        counts.set(tag.id, (counts.get(tag.id) ?? 0) + 1);
      }
    }
    const winner = [...counts.entries()].sort((left, right) => right[1] - left[1])[0];
    return winner && winner[1] >= Math.max(1, Math.ceil(rows.length / 2)) ? winner[0] : null;
  };

  const createChildTableRow = async (parent: DocsNode, rows: DocsNode[]) => {
    const supertagId = tableSupertagIdFor(parent, rows);
    try {
      return await createNode(parent.id, rows.at(-1) ?? null, "新しい項目", {
        supertagIds: supertagId ? [supertagId] : [],
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "表の行を作成できませんでした");
      return null;
    }
  };

  const commitTitle = async (node: DocsNode, title: string) => {
    const matchedTags = titleTagNames(title)
      .map((name) => state.supertags.find((tag) => tag.name.toLowerCase() === name.toLowerCase()))
      .filter((tag): tag is DocsSupertag => Boolean(tag));
    const nextTitle = matchedTags.length > 0 ? titleWithoutTagTokens(title) : title;
    if (!hasMeaningfulBlockTitle(nextTitle)) {
      const canonical = pageTitleCanonicalRef.current.get(node.id);
      if (canonical && hasMeaningfulBlockTitle(canonical)) {
        setState((current) => ({
          ...current,
          nodes: current.nodes.map((item) => item.id === node.id
            ? { ...item, title: canonical, body_text: item.body_text || canonical }
            : item),
        }));
      }
      return;
    }
    // 入力中は同じ node.title を楽観更新しているため、確定時の比較では
    // 永続化済みかを判定できない。blur / Enter では必ず保存キューへ送る。
    const bodyPatch = node.body_json?.blank === true
      ? { body_json: clearBlankParagraphMarker(node.body_json), body_text: nextTitle }
      : {};
    await patchNode(node.id, { title: nextTitle, ...bodyPatch });
    pageTitleCanonicalRef.current.set(node.id, nextTitle);
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
    if (!canEditDefinitions) return;
    const name = newTagName.replace(/^#/, "").trim();
    if (!name) return;
    const data = await apiFetch<{ supertag: DocsSupertag }>("/api/docs/supertags", {
      method: "POST",
      body: JSON.stringify({
        ...(activeDocsLibraryId ? { docs_library_id: activeDocsLibraryId } : {}),
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
    if (!canEditDefinitions) throw new Error("Supertagを変更できません");
    const data = await apiFetch<{ supertag: DocsSupertag }>("/api/docs/supertags", {
      method: "POST",
      body: JSON.stringify({
        ...(activeDocsLibraryId ? { docs_library_id: activeDocsLibraryId } : {}),
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
    if (!canEditDefinitions) return undefined;
    const tag = state.supertags.find((item) => item.id === tagId);
    if (!tag || tag.docs_library_id !== activeDocsLibraryId) return undefined;
    const trimmed = name.trim();
    if (!trimmed) return undefined;
    const data = await apiFetch<{ field: DocsField; supertag_field: DocsState["supertag_fields"][number] }>("/api/docs/fields", {
      method: "POST",
      body: JSON.stringify({
        ...(activeDocsLibraryId ? { docs_library_id: activeDocsLibraryId } : {}),
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
    const targetIds = resolveActionNodeIds(selectedNodeIdsRef.current, fallbackNode.id);
    const targets = targetIds
      .map((nodeId) => nodesById.get(nodeId))
      .filter((node): node is DocsNode => Boolean(node));
    for (const node of targets) {
      await applyTag(node, tagId);
    }
  };

  const updateField = async (fieldId: string, patch: Partial<Pick<DocsField, "name" | "field_type" | "required" | "options_json" | "sort_order">> & { default_value_json?: unknown }) => {
    if (!canEditDefinitions) return;
    const field = state.fields.find((item) => item.id === fieldId);
    if (!field || field.docs_library_id !== activeDocsLibraryId) return;
    const data = await apiFetch<{ field: DocsField }>(`/api/docs/fields/${fieldId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setState((current) => ({ ...current, fields: current.fields.map((field) => (field.id === data.field.id ? data.field : field)) }));
  };

  const updateSupertag = async (tagId: string, patch: Partial<Pick<DocsSupertag, "name" | "description" | "color" | "icon" | "template_json" | "config_json" | "title_template" | "ai_instructions" | "parent_supertag_id">>) => {
    if (!canEditDefinitions) return;
    const tag = state.supertags.find((item) => item.id === tagId);
    if (!tag || tag.docs_library_id !== activeDocsLibraryId) return;
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
    if (!canEditDefinitions || tag.docs_library_id !== activeDocsLibraryId) throw new Error("Viewを変更できません");
    const data = await apiFetch<{ view: DocsSavedView }>("/api/docs/views", {
      method: "POST",
      body: JSON.stringify({
        ...(activeDocsLibraryId ? { docs_library_id: activeDocsLibraryId } : {}),
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
    if (!canEditDefinitions) throw new Error("Viewを変更できません");
    const view = state.views.find((item) => item.id === viewId);
    if (!view || view.docs_library_id !== activeDocsLibraryId) throw new Error("Viewを変更できません");
    const data = await apiFetch<{ view: DocsSavedView }>(`/api/docs/views/${viewId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setState((current) => ({ ...current, views: current.views.map((view) => (view.id === data.view.id ? data.view : view)) }));
    return data.view;
  };

  const createTaggedNode = async (tag: DocsSupertag) => {
    // Tag creation is an explicit node action, not an editor blank row. Use a
    // meaningful temporary title so the API invariant is preserved; callers
    // can immediately rename it in the outline editor.
    const node = await createNode(focusNode?.id ?? null, null, "新しいノード");
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
    try {
      await flushPendingDocsEditorWritesBeforeNavigation();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Docsの保存に失敗しました。入力内容を確認してください");
      return;
    }
    const targetIds = resolveActionNodeIds(selectedNodeIdsRef.current, fallbackNode.id);
    const targets = targetIds
      .map((nodeId) => nodesById.get(nodeId))
      .filter((node): node is DocsNode => Boolean(node));
    const targetParent = nodesById.get(targetParentId);
    if (!canWriteNode(targetParent) || targets.some((node) => !canWriteNode(node))) return;
    for (const node of targets) {
      await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${node.id}/move`, {
        method: "POST",
        body: JSON.stringify({ new_parent_id: targetParentId, leave_reference: leaveReference }),
      });
    }
    await load();
  };

  const setSearchNodeView = async (node: DocsNode, view: SearchView) => {
    await patchNode(node.id, { view_json: { ...node.view_json, view } });
  };

  const setSearchNodeSort = async (node: DocsNode, sort: SearchSort) => {
    const nextQuery = normalizeSearchQuery(node.query_json);
    if (sort) {
      nextQuery.sort = sort;
    } else {
      delete nextQuery.sort;
    }
    await patchNode(node.id, { query_json: nextQuery });
  };

  const setSearchNodeQuery = async (node: DocsNode, query: Record<string, unknown>) => {
    await patchNode(node.id, { query_json: normalizeSearchQuery(query) });
  };

  const createBlockNode = (input: BlockCreateInput) => {
    // `insertAtStart` is added by the outline editor for an Enter-at-start
    // split. Keep this intersection while older editor test fixtures omit the
    // optional field; the ordinary null-afterNode path must remain append.
    const insertAtStart = (input as BlockCreateInput & { insertAtStart?: boolean }).insertAtStart === true;
    const explicitBlank = input.blank === true
      && input.title === ""
      && (input.kind ?? "paragraph") === "paragraph";
    if (!hasMeaningfulBlockTitle(input.title) && !explicitBlank) {
      throw new Error("空行はDocs nodeとして保存できません");
    }
    const nextTitle = explicitBlank ? "" : input.title;
    const afterNode = input.afterNodeId ? nodesById.get(input.afterNodeId) ?? null : null;
    const displayPatch = input.kind === "checkbox"
      ? { show_checkbox: true, checked: input.checked === true }
      : {};
    const bodyJson = explicitBlank
      ? blankParagraphBodyJson({})
      : {
          format: "doc_block",
          block_type: input.kind === "heading_1" || input.kind === "heading_2" || input.kind === "heading_3" || input.kind === "checkbox" || input.kind === "quote"
            ? input.kind
            : "paragraph",
          ...(input.kind === "checkbox" ? { checked: input.checked === true } : {}),
        };
    const created = createNode(input.parentId, afterNode, nextTitle, {
      bodyJson,
      displayProps: displayPatch,
      nodeType: input.kind === "search" ? "search" : "node",
      optimistic: true,
      focusOnCreate: input.focusOnCreate !== false,
      insertAtStart,
      onPersistenceError: (nodeId, error) => {
        // A failed optimistic POST must not leave a phantom create in the
        // Workspace history.  Otherwise the next Ctrl+Z would try to archive
        // a node that was already rolled back from state.
        const touchesFailedNode = (entry: BlockHistoryEntry) => entry.patches.some((patch) => {
          if (patch.type === "create" || patch.type === "archive") return patch.node.id === nodeId;
          return patch.id === nodeId;
        });
        // Remove every entry that references the failed optimistic id, not
        // only the stack tail. A fast follow-up edit may already have folded
        // an update onto the create entry before the POST rejection arrives.
        undoStackRef.current = undoStackRef.current.filter((entry) => !touchesFailedNode(entry));
        redoStackRef.current = redoStackRef.current.filter((entry) => !touchesFailedNode(entry));
        pendingCreateHistoryIdsRef.current.delete(nodeId);
        input.onPersistenceError?.(nodeId, error);
      },
    }) as DocsNode;
    pushHistory({
      label: "ノード作成",
      patches: [{ type: "create", node: snapshotDocsNode({ ...created, body_json: bodyJson, display_props: { ...created.display_props, ...displayPatch } }) }],
    });
    pendingCreateHistoryIdsRef.current.add(created.id);
    return { ...created, body_json: bodyJson, body_text: nextTitle, display_props: { ...created.display_props, ...displayPatch } };
  };

  const moveBlockNode = async (input: BlockMoveInput) => {
    const target = nodesById.get(input.nodeId);
    if (!target) return;
    const parentId = input.parentId;
    const siblings = parentId ? childrenByParent.get(parentId) ?? [] : roots;
    const previous = input.afterNodeId ? nodesById.get(input.afterNodeId) ?? null : null;
    const previousIndex = previous ? siblings.findIndex((node) => node.id === previous.id) : -1;
    const next = previousIndex >= 0 ? siblings[previousIndex + 1] : siblings[0];
    const sortOrder = midpointSortOrder(previous?.sort_order, next?.sort_order);
    const beforeSnapshot = snapshotDocsNode(target);
    const afterSnapshot: DocsBlockSnapshot = {
      ...snapshotDocsNode(target),
      parent_id: parentId,
      sort_order: sortOrder,
    };
    pushHistory({
      label: "ノード移動",
      patches: [{ type: "update", id: target.id, before: beforeSnapshot, after: afterSnapshot }],
    });
    const parentChildrenLoaded = !parentId || loadedChildrenParentIds.has(parentId);
    // Tab/Shift-Tab/ドラッグの度に全体を読み直すと、表示中ページの子ノードが
    // 遅延読込の state から落ちて「空の別ページへ飛ばされた」ように見える。
    // 移動はローカルへ楽観反映し、必要な範囲だけ後から取り込む。
    setState((current) => {
      const loadedParentIds = current.loaded_children_parent_ids ?? [];
      const hasChildrenIds = current.has_children_ids ?? [];
      return {
        ...current,
        nodes: current.nodes.map((item) => (
          item.id === target.id ? { ...item, parent_id: parentId, sort_order: sortOrder } : item
        )),
        // 移動先が「子あり・未読込」と判定されるとシェブロンが折りたたみ表示のままになる。
        loaded_children_parent_ids: parentId && !loadedParentIds.includes(parentId)
          ? [...loadedParentIds, parentId]
          : loadedParentIds,
        has_children_ids: parentId && !hasChildrenIds.includes(parentId)
          ? [...hasChildrenIds, parentId]
          : hasChildrenIds,
      };
    });
    // 折りたたみ中のノードへ入れると移動した行自体が消えるため展開しておく。
    if (parentId) {
      persistExpanded(parentId, true);
      setCollapsed((current) => {
        if (!current.has(parentId)) return current;
        const nextCollapsed = new Set(current);
        nextCollapsed.delete(parentId);
        writeCollapsed(nextCollapsed);
        return nextCollapsed;
      });
    }
    try {
      await patchNode(input.nodeId, {
        parent_id: parentId,
        sort_order: sortOrder,
      });
      // 未読込の親へ入れた時だけ、その親の既存の子を取り込む（全体再読込はしない）。
      if (parentId && !parentChildrenLoaded) await loadNodeChildren(parentId);
    } catch (error) {
      // Structural mutation is optimistic, but a failed PATCH must not leave
      // the row stranded under a phantom parent. Restore only the moved row;
      // keep the rest of the lazily-loaded snapshot intact and let the
      // structural queue stop subsequent operations on the rejection.
      setState((current) => ({
        ...current,
        nodes: current.nodes.map((item) => item.id === target.id
          ? { ...item, parent_id: beforeSnapshot.parent_id, sort_order: beforeSnapshot.sort_order }
          : item),
      }));
      throw error;
    }
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
    persistExpanded(parent.id, true);
    setCollapsed((current) => {
      const next = new Set(current);
      next.delete(parent.id);
      return next;
    });
    selectSingleNode(node.id);
    setFocusRequestNodeId(node.id);
  };

  // `/field` で選択したFieldへ値を保存し、成功後に入力用の子ノードを削除する。
  const applyFieldShorthand = async (row: OutlineEditorRow, field: DocsField, rawValue: string): Promise<boolean> => {
    const parentId = row.node.parent_id;
    if (!parentId) return false;
    const parent = nodesById.get(parentId);
    if (!parent) return false;
    const resolvedRawValue = rawValue.trimStart();
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
    if (!parent) return null;
    const tags = nodeTags.get(parent.id) ?? [];
    if (tags.length !== 1) {
      toast.error("Fieldの所属先を一意に決められません。対象ノードにSupertagを1つ設定してください");
      return null;
    }
    return await createField(tags[0].id, name, "text") ?? null;
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
        if (!hasMeaningfulBlockTitle(rawLine)) continue;
        const taskTag = state.supertags.find((tag) => tag.system_key === "task");
        const decisionTag = state.supertags.find((tag) => tag.system_key === "decision");
        const shouldBindTask = command === "extract_tasks" || /#(?:Task|タスク)(?=\s|$|[.,;:!?、。])/i.test(rawLine);
        const shouldTagDecision = /#(?:Decision|決定)(?=\s|$|[.,;:!?、。])/i.test(rawLine);
        const nextTitle = shouldBindTask || shouldTagDecision ? titleWithoutTagTokens(rawLine) : rawLine;
        if (!hasMeaningfulBlockTitle(nextTitle)) continue;
        const created = await createNode(targetParent.id, afterNode, nextTitle);
        if (shouldBindTask && taskTag) await applyTag(created, taskTag.id);
        if (shouldTagDecision && decisionTag) await applyTag(created, decisionTag.id);
        afterNode = created;
      }
      persistExpanded(node.id, true);
      persistExpanded(targetParent.id, true);
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
    const target = event.target instanceof Element ? event.target : null;
    const keyboardTarget = target ?? (document.activeElement instanceof Element ? document.activeElement : null);
    if (shouldArchiveSelectionFromKeyboard({
      key: event.key,
      ctrlKey: event.ctrlKey,
      altKey: event.altKey,
      metaKey: event.metaKey,
      shiftKey: event.shiftKey,
      selectedCount: selectedNodeIdsRef.current.length,
      target: keyboardTarget,
      hasNonCollapsedTextSelection: hasCodeMirrorRangeSelection(keyboardTarget),
    })) {
      if (event.nativeEvent.defaultPrevented) return;
      event.preventDefault();
      event.stopPropagation();
      void archiveSelectedNodesRef.current();
      return;
    }
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
    readOnly: docsReadOnly,
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
    historySync,
    onUndo: undoDocsOperation,
    onRedo: redoDocsOperation,
    onCommitPending: (operation) => {
      editorCommitInFlightRef.current = operation;
    },
    onCommitTitle: async (target, title, patch) => {
      // Empty titles are editor-only transient state.  Do not optimistically
      // replace the existing row or call the API; this also keeps table/blur
      // writers from turning a 400 response into a disappearing node.  The
      // sole exception is the explicit persisted blank paragraph contract
      // emitted by the outline editor.
      const explicitBlank = isExplicitBlankParagraphPatch(target, title, patch);
      if (!hasMeaningfulBlockTitle(title) && !explicitBlank) return;
      const nextTitle = explicitBlank ? "" : title;
      const effectivePatch = explicitBlank
        ? normalizeExplicitBlankParagraphPatch(patch ?? {})
        : patch?.body_json?.blank === true
          ? {
              ...patch,
              body_json: clearBlankParagraphMarker(patch.body_json),
            }
          : patch;
      const beforeSnapshot = snapshotDocsNode(target);
      if (effectivePatch) {
        const afterNode = { ...target, ...effectivePatch, title: nextTitle };
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
              ? { ...item, ...effectivePatch, title: nextTitle }
              : item
          )),
        }));
        try {
          await patchNode(target.id, { ...effectivePatch, title: nextTitle });
          pageTitleCanonicalRef.current.set(target.id, nextTitle);
        } catch (error) {
          // Keep the optimistic draft/state visible after a failed PATCH. The
          // save queue and editor retain the draft so the user can retry;
          // restoring `target` here would silently discard their input.
          throw error;
        }
        return;
      }
      const afterNode = { ...target, title: nextTitle, body_text: nextTitle };
      const foldedCreate = foldIntoPendingCreateHistory(afterNode);
      if (target.title !== nextTitle || target.body_text !== nextTitle || target.body_json.blank === true) {
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
              ? {
                  ...item,
                  title: nextTitle,
                  body_text: nextTitle,
                  body_json: clearBlankParagraphMarker(item.body_json),
                }
              : item
          )),
        }));
      }
      try {
        await commitTitle(target, nextTitle);
        pageTitleCanonicalRef.current.set(target.id, nextTitle);
      } catch (error) {
        // As above, do not roll back the in-memory title: the active editor
        // draft must remain recoverable after a navigation-blocking failure.
        throw error;
      }
    },
    onDraftChange: (target, title) => {
      // Empty is a meaningful user draft while the editor is open: it may be
      // the explicit blank paragraph that is about to be persisted.
      latestEditorDraftRef.current.set(target.id, title);
      foldIntoPendingCreateHistory({ ...target, title, body_text: title });
    },
    onCommitSuccess: (nodeId, committedDraft) => {
      if (latestEditorDraftRef.current.get(nodeId) === committedDraft) {
        latestEditorDraftRef.current.delete(nodeId);
      }
      // Keep a just-created explicit blank paragraph folded into its create
      // history until it receives meaningful text.  Otherwise the initial
      // empty commit would leave a standalone update at the top of the stack,
      // and Ctrl+Z could no longer archive the created blank row.
      if (hasMeaningfulBlockTitle(committedDraft)) {
        pendingCreateHistoryIdsRef.current.delete(nodeId);
      }
    },
    onCreateNode: createBlockNode,
    onArchiveNode: async (target) => {
      const actionIds = resolveActionNodeIds(selectedNodeIdsRef.current, target.id);
      const nodes = actionIds
        .map((id) => nodesByIdRef.current.get(id))
        .filter((node): node is DocsNode => Boolean(node));
      const archived = await archiveNodesWithHistory(nodes);
      if (!archived) {
        // Outline structural edits must not continue as if the sibling was
        // archived when its DELETE failed.  Propagate the failure so the
        // editor queue surfaces the error and keeps the merged draft retryable.
        throw new Error("ノードのアーカイブに失敗しました");
      }
    },
    onMoveNode: moveBlockNode,
    onToggleCheckbox: (target) => void toggleNodeCheckbox(target),
    onToggleCollapsed: toggleCollapsed,
    onDuplicateNode: (target) => void duplicateSidebarNode(target),
    onApplyTag: (target, tag) => void applyTagToActionNodes(target, tag.id),
    onRemoveTag: (target, tag) => void removeTag(target, tag.id),
    onOpenTag: (tag) => {
      setPropertiesTagId(tag.id);
      setRightPanel("tags");
      setPropertiesOpen(true);
    },
    onSaveField: (target, field, value) => void saveField(target, field, value),
    onDeleteAttachment: deleteAttachment,
    onInsertImages: insertDocsImages,
    onMoveToPage: async (target, page) => {
      await flushPendingDocsEditorWritesBeforeNavigation();
      await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${target.id}/move`, {
        method: "POST",
        body: JSON.stringify({ new_parent_id: page.id, leave_reference: false }),
      });
      await load({ nodeId: focusNodeIdRef.current ?? undefined });
      toast.success(`「${page.title}」へ移動しました`);
    },
    onReplaceTitles: (updates) => void replaceNodeTitles(updates),
    onFieldShorthand: (row, field, rawValue) => applyFieldShorthand(row, field, rawValue),
    onOpenAliasEditor: (row) => setAliasEditorNode(row.node),
    onCreateSearchNode: (row) => void createSearchNodeForRow(row),
    onSuggestFields: (row) => void runDocsAiCommand(row.node, "fill_fields"),
    onCreateFieldCandidate: createFieldCandidateForRow,
    onSuggestionStatus: updateSuggestionStatus,
  }), [
    apiFetch,
    applyFieldShorthand,
    applyTagToActionNodes,
    archiveNodesWithHistory,
    commitTitle,
    createBlockNode,
    createFieldCandidateForRow,
    createSearchNodeForRow,
    docsReadOnly,
    deleteAttachment,
    duplicateSidebarNode,
    foldIntoPendingCreateHistory,
    flushPendingDocsEditorWritesBeforeNavigation,
    historySync,
    insertDocsImages,
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
    undoDocsOperation,
    redoDocsOperation,
    // 以下はフックから受け取る安定参照（恒等が保たれるため再計算契機にはならない）
    preserveSelectionOnNextFocusRef,
    selectedNodeIdsRef,
    selectionAnchorNodeIdRef,
    setSelectedNodeId,
    setSelectedNodeIds,
    setSelectionAnchorNodeId,
  ]);

  const renderRightPanel = () => (
    <RightPanel
      mode={rightPanel}
      selectedNode={selectedNode}
      readOnly={!selectedNodeCanWrite}
      definitionReadOnly={!canEditDefinitions || Boolean(propertiesTagId && tagById.get(propertiesTagId)?.docs_library_id !== activeDocsLibraryId)}
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
  );

  const renderPanel = (node: DocsNode | null, rows: Array<{ node: DocsNode; depth: number }>, compact = false) => {
    const panelBreadcrumb = buildBreadcrumb(node, nodesById);
    const directChildren = node
      ? hoistedVisibleChildren(childrenByParent, node.id, isNodeProjectionVisible)
      : [];
    const childrenLayout = node?.display_props?.children_layout === "table" ? "table" : "outline";
    const documentFields = node ? fieldsForNode(node, nodeTags, fieldsByTag) : [];
    const documentFieldValues = node ? fieldValuesByNodeId.get(node.id) ?? [] : [];
    const visibleRows = node
      ? suppressLegacyEmailOutlineRows(
        rows,
        node,
        nodeTags.get(node.id) ?? [],
        documentFields,
        documentFieldValues,
        childrenByParent,
        nodeHasChildren,
      )
      : rows;
    const outlineEditorRows: OutlineEditorRow[] = visibleRows.map((row) => ({
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
      fields: documentFields,
      fieldValues: documentFieldValues,
      attachments: attachmentsByNodeId.get(node.id) ?? [],
      taskBinding: taskBindingsByNodeId.get(node.id) ?? null,
    } : null;
    const showContextRail = wideDocsViewport && !compact && (propertiesOpen || node?.node_type === "search");
    return (
    <>
    <section
      className={cn(
        "flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background",
        compact && "border-r border-border bg-background",
      )}
      data-shell-region="docs-document"
      data-docs-panel={compact ? "split" : "document"}
    >
      <div className="border-b border-border bg-background px-5 py-3">
        <div className={cn("mx-auto w-full", compact ? "max-w-none" : "max-w-5xl")}>
        {compact ? (
          <div className="mb-3 flex h-7 items-center justify-between border-b border-border pb-2 text-[11px] text-muted-foreground">
            <span className="flex min-w-0 items-center gap-2">
              <span className="size-1.5 rounded-full bg-primary" />
              <span className="truncate">{node ? nodeText(node) : "Split context"}</span>
            </span>
            <button type="button" className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground" title="分割表示を閉じる" aria-label="分割表示を閉じる" onClick={() => setSplitNodeId(null)}>
              <X className="size-3.5" />
            </button>
          </div>
        ) : null}
        <div className="flex min-w-0 items-center gap-2 text-[11px] text-muted-foreground">
          {panelBreadcrumb.map((item) => (
            <button key={item.id} type="button" className="truncate transition-colors hover:text-primary" onClick={() => openDocsNode(item.id)}>
              {nodeText(item)}
            </button>
          ))}
        </div>
        {node ? (
           <div className="mt-3 flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <PageTitleEditor
                node={node}
                readOnly={!canWriteNode(node)}
                tags={nodeTags.get(node.id) ?? []}
                requestFocus={focusRequestNodeId === node.id}
                onFocused={() => setFocusRequestNodeId(null)}
                onChangeTitle={(title) => setState((current) => {
                  const previous = current.nodes.find((item) => item.id === node.id);
                  if (previous && hasMeaningfulBlockTitle(previous.title) && !pageTitleCanonicalRef.current.has(node.id)) {
                    pageTitleCanonicalRef.current.set(node.id, previous.title);
                  }
                  return {
                    ...current,
                    nodes: current.nodes.map((item) => (item.id === node.id ? { ...item, title } : item)),
                  };
                })}
                onCommitTitle={(title) => {
                  const canonical = pageTitleCanonicalRef.current.get(node.id) ?? node.title;
                  if (!hasMeaningfulBlockTitle(title)) {
                    if (hasMeaningfulBlockTitle(canonical)) {
                      setState((current) => ({
                        ...current,
                        nodes: current.nodes.map((item) => item.id === node.id
                          ? { ...item, title: canonical, body_text: item.body_text || canonical }
                          : item),
                      }));
                    }
                    return;
                  }
                  void commitTitle(node, title).catch(() => {
                    if (!hasMeaningfulBlockTitle(canonical)) return;
                    setState((current) => ({
                      ...current,
                      nodes: current.nodes.map((item) => item.id === node.id
                        ? { ...item, title: canonical, body_text: item.body_text || canonical }
                        : item),
                    }));
                  });
                }}
                onRemoveTag={(tag) => void removeTag(node, tag.id)}
                onOpenTag={(tag) => {
                  setPropertiesTagId(tag.id);
                  setRightPanel("tags");
                  setPropertiesOpen(true);
                }}
                onNavigateDown={() => {
                  if (outlineEditorRows[0]) setFocusRequestNodeId(outlineEditorRows[0].node.id);
                }}
                onContextMenu={(event) => {
                  if (documentEditorRow) openDocumentContextMenu(event, documentEditorRow.node);
                }}
              />
              {documentEditorRow && documentContextMenu?.nodeId === documentEditorRow.node.id && typeof document !== "undefined"
                ? createPortal(
                    <DocsNodeContextMenu
                      node={documentEditorRow.node}
                      tags={documentEditorRow.tags}
                      position={{ x: documentContextMenu.x, y: documentContextMenu.y }}
                      onClose={() => setDocumentContextMenu(null)}
                      onCopyNodeId={(target) => copySidebarNodeId(target)}
                      onDuplicateNode={(target) => duplicateSidebarNode(target)}
                      onArchiveNode={(target) => docsEditorContextValue.onArchiveNode(target)}
                      onMoveNode={(target) => {
                        if (!canWriteNode(target)) return;
                        selectSingleNode(target.id);
                        requestDocsCommand({ kind: "move", leaveReference: false });
                      }}
                      onTaskifyNode={(target) => {
                        if (!canWriteNode(target)) return;
                        return docsEditorContextValue.onCommitTitle(target, target.title, {
                          body_json: { ...target.body_json, ...blockJsonForKind("checkbox") },
                          display_props: { ...target.display_props, show_checkbox: true },
                        });
                      }}
                      onApplyTag={(target, tag) => applyTag(target, tag.id)}
                      onOpenNode={(target) => openDocsNode(target.id)}
                    />,
                    document.body,
                  )
                : null}
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                {node.node_type === "day" && <StatusBadge tone="info" className="text-xs font-normal">Daily</StatusBadge>}
                <TaskBindingButton task={taskBindingsByNodeId.get(node.id) ?? null} onOpenTask={setTaskModalId} />
                <div className="flex items-center rounded border bg-muted/20 p-0.5" aria-label="子ノードの表示形式">
                  <button
                    type="button"
                    aria-label="子ノードをアウトライン表示"
                    title="アウトライン表示"
                    className={cn(
                      "grid size-6 place-items-center rounded text-muted-foreground hover:text-foreground",
                      childrenLayout === "outline" && "bg-background text-foreground shadow-sm",
                    )}
                    onClick={() => void updateDisplayProps(node, { children_layout: "outline" })}
                  >
                    <ListTree className="size-3.5" />
                  </button>
                  <button
                    type="button"
                    aria-label="子ノードを表表示"
                    title="表表示"
                    className={cn(
                      "grid size-6 place-items-center rounded text-muted-foreground hover:text-foreground",
                      childrenLayout === "table" && "bg-background text-foreground shadow-sm",
                    )}
                    onClick={() => void updateDisplayProps(node, {
                      children_layout: "table",
                      table_supertag_id: tableSupertagIdFor(node, directChildren),
                    })}
                  >
                    <Table2 className="size-3.5" />
                  </button>
                </div>
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
                {node.permission === "owner" ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 text-xs"
                    title="Docsを共有"
                    aria-label="Docsを共有"
                    onClick={() => setShareNode(node)}
                  >
                    <Share2 className="mr-1 size-3.5" />
                    共有
                  </Button>
                ) : null}
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
       <div className="min-h-0 flex-1 overflow-auto px-5 py-4" data-docs-scroll-container>
         {node ? (
          <>
            <div
              data-docs-editor-surface
              className={cn(
                 "mx-auto mt-2 w-full max-w-4xl space-y-0 overflow-x-auto",
                 compact && "max-w-none px-1",
               )}
            >
              {node.system_key?.startsWith("agent_memory") ||
              node.display_props?.managed_domain === "legacy_agent_memory" ? (
                <StatusNote tone="warning" className="mb-3">
                  <p className="font-medium">この旧エージェントメモリは移行済みの読み取り専用データです。</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    新しいメモリの確認・編集・候補承認は
                    <a className="mx-1 underline underline-offset-2" href="/settings#conversation">設定の「メモリ」</a>
                    から行ってください。
                  </p>
                </StatusNote>
              ) : null}
              {childrenLayout === "table" ? (
                <DocsChildrenTable
                  rows={directChildren}
                  fieldsForRow={(row) => fieldsForNode(row, nodeTags, fieldsByTag)}
                  fieldValuesByKey={fieldValuesByKey}
                  nodes={visibleNodes}
                  projects={projects}
                  onCommitTitle={(row, title) => void commitTitle(row, title)}
                  onCommitField={(row, field, value) => void saveField(row, field, value)}
                  onOpenNode={openDocsNode}
                  onAddRow={() => void createChildTableRow(node, directChildren)}
                  hasMoreRows={Boolean(state.children_next_cursor_by_parent?.[node.id])}
                  loadingMore={loadingNodeIds.has(node.id)}
                  onLoadMoreRows={() => {
                    const cursor = state.children_next_cursor_by_parent?.[node.id];
                    if (cursor) void loadNodeChildren(node.id, cursor);
                  }}
                />
              ) : (
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
                nodes={visibleNodes}
                projects={projects}
                supertags={state.supertags}
                users={mentionUsers}
                suggestions={state.ai_suggestions}
                onNavigateToDocumentTitle={() => setFocusRequestNodeId(node.id)}
                renderDocumentMetadata={<DocsClipIngestProvenanceField nodeId={node.id} />}
                isCollapsed={nodeIsCollapsed}
                nodeHasChildren={nodeHasExpandableContent}
                isNodeLoading={(nodeId) => loadingNodeIds.has(nodeId)}
                fieldCandidatesForRow={fieldCandidatesForRow}
                renderBelowRow={(row, _index, { searchExpanded }) =>
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
                      documentExpanded={searchExpanded}
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
              )}
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
    {showContextRail ? (
      <aside className="hidden w-[300px] shrink-0 flex-col border-l border-border bg-card 2xl:flex" data-shell-region="docs-context-rail">
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-border px-4">
          <div className="flex min-w-0 items-center gap-2 text-sm font-semibold text-foreground">
            {node?.node_type === "search" ? <ListFilter className="size-4 text-primary" /> : <Settings2 className="size-4 text-primary" />}
            <span className="truncate">{node?.node_type === "search" ? "Query Metadata" : "Page Properties"}</span>
          </div>
          {node?.node_type !== "search" ? (
            <button type="button" className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground" aria-label="プロパティを閉じる" title="プロパティを閉じる" onClick={() => setPropertiesOpen(false)}>
              <X className="size-4" />
            </button>
          ) : null}
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          {node?.node_type !== "search" && (docsReadOnly || !selectedNodeCanWrite) ? (
            <ReadOnlyBadge className="block rounded-none border-x-0 border-t-0 px-4 py-2 uppercase tracking-wide">
              Read only{docsReadOnly ? " · Enterprise Docs" : ""}
            </ReadOnlyBadge>
          ) : null}
          {node?.node_type === "search" ? <SearchNodeMetadata node={node} tags={state.supertags} /> : null}
          {node?.node_type !== "search" ? renderRightPanel() : null}
        </div>
      </aside>
    ) : null}
    </>
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
        "flex shrink-0 flex-col border-border bg-card",
        portal
          ? "h-full min-h-0 w-full min-w-0"
          : cn("w-60 border-r", isDocsRoute && "hidden md:flex"),
      )}
      data-shell-region="docs-navigation"
      aria-label="Docs navigation"
    >
      <div className="border-b border-border px-4 py-3">
        <div className="mb-3 flex items-center justify-between">
          <span className="text-sm font-semibold text-foreground">Documentation</span>
          {canCreateRootNode ? (
            <button
              type="button"
              className="grid size-6 place-items-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-primary"
              title="ルートノートを作成"
              aria-label="ルートノートを作成"
              onClick={() => void createRootNode()}
            >
              <Plus className="size-4" />
            </button>
          ) : null}
        </div>
        <AppSelect
          value={docsSource}
          onChange={(event) => {
            const nextSource = event.target.value;
            if (nextSource === docsSource) return;
            void flushPendingDocsEditorWritesBeforeNavigation()
              .then(() => {
                setDocsSource(nextSource);
                setMainView(null);
                setTagPageId(null);
              })
              .catch((error) => {
                toast.error(error instanceof Error ? error.message : "Docsの保存に失敗しました。入力内容を確認してください");
              });
          }}
          className="h-8 w-full rounded border border-border bg-background px-2 text-xs"
          aria-label="Docsデータソース"
        >
          <option value="local">Docs</option>
          {remoteProfiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              [EP] {profile.name}
            </option>
          ))}
        </AppSelect>
        {docsReadOnly && <ReadOnlyBadge className="mt-1">Enterprise Docs（読み取り専用）</ReadOnlyBadge>}
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-2 py-3">
        <SidebarButton
          icon={CalendarDays}
          label="Today"
          active={!mainView && !tagPageId && focusNode?.node_type === "day"}
          onClick={() => {
            openToday();
          }}
        />
        {!docsReadOnly ? (
          <>
            <SidebarButton
              icon={FileDown}
              label="クリップ取り込み"
              active={clipIngestOpen}
              onClick={() => {
                openClipIngest();
              }}
            />
            <SidebarButton
              icon={History}
              label="取り込み履歴"
              active={clipIngestPanelOpen}
              onClick={() => {
                setClipIngestPanelOpen(!clipIngestPanelOpen);
              }}
            />
          </>
        ) : null}
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
        <div className="mt-4 flex items-center gap-2 px-2 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          <span className="h-px flex-1 bg-border" />
          <span>Pages</span>
          <span className="h-px flex-1 bg-border" />
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
              nodeHasChildren={sidebarNodeHasChildren}
              isNodeVisible={isSidebarProjectionVisible}
              collapsed={sidebarCollapsed}
              onToggle={toggleSidebarCollapsed}
              onOpen={openSidebarNode}
              onContextMenu={openSidebarNodeContextMenu}
              onDragStart={(nodeId) => setDragSidebarNodeId(nodeId)}
              onDragEnd={() => setDragSidebarNodeId(null)}
              onDropOnNode={(node) => void dropSidebarNode(node)}
            />
          ))}
        </div>
        {sharedRoots.filter((node) => isSidebarProjectionVisible(node)).length > 0 ? (
          <>
            <div className="mt-5 px-2 text-xs font-medium text-muted-foreground">共有されたDocs</div>
            <div className="mt-1 space-y-0.5">
              {sharedRoots.filter((node) => isSidebarProjectionVisible(node)).map((node) => (
                <button
                  key={node.id}
                  type="button"
                  className={cn(
                    "flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-accent",
                    focusNodeId === node.id && "bg-accent text-accent-foreground",
                  )}
                  onClick={() => {
                    openDocsNode(node.id);
                  }}
                  title={node.permission === "write" ? "編集可能" : "閲覧のみ"}
                >
                  <Share2 className="size-3.5 shrink-0 text-muted-foreground" />
                  <span className="truncate">{nodeText(node)}</span>
                </button>
              ))}
            </div>
          </>
        ) : null}
      </div>
    </aside>
  );
  const docsSidebar = renderDocsSidebar(Boolean(docsSidebarSlot));
  // Register a stable DOM slot rather than the stateful navigation element
  // itself.  The sidebar remains owned by this workspace and is portaled into
  // the route slot once Shared Shell mounts it; keeping the registered node
  // prop-free avoids re-registering callback-heavy JSX on every editor edit.
  const docsShellNavigationSlot = (
    <div
      id={DOCS_SIDEBAR_SLOT_ID}
      className="min-h-0 flex-1 flex flex-col"
      data-shell-slot="workspace-navigation"
      data-workspace="docs"
    />
  );

  // Docs owns the document tree navigation. Register it with the Shared Shell
  // so the route can replace the legacy AppSidebar slot without duplicating
  // the same node/outliner data owner. The existing portal remains available
  // when this workspace is embedded outside the shell or during fallback.
  useWorkspaceShellRegistration({
    id: "docs-workspace",
    // Project information embeds DocsWorkspace as a document editor inside
    // its own page; only the canonical /docs route should claim the shell's
    // Workspace Navigation slot.
    workspaceNavigation: isDocsRoute ? docsShellNavigationSlot : undefined,
    priority: 10,
  });

  useRegisterDocsCommand({
    selectedNode,
    selectionCount: actionNodes.length,
    onOpenNode: openDocsNode,
    tags: state.supertags,
    fields: commandFields,
    moveTargets: commandMoveTargets,
    nodeTools: commandNodeTools,
    onAddChild: (node) => void createNode(node.id, null, "新しいノード"),
    onOpenSplit: (node) => setSplitNodeId(node.id),
    onToggleCheckbox: (node) => void toggleNodeCheckbox(node),
    onApplyTag: (node, tag) => void applyTagToActionNodes(node, tag.id),
    onMove: (node, target, leaveReference) => void moveActionNodes(node, target.id, leaveReference),
    onSetView: (node, view) => void setSearchNodeView(node, view),
    onSetField: (node, field, value) => void saveField(node, field, value),
    onRunAi: (node, command) => void runDocsAiCommand(node, command),
    onGoBack: (node) => {
      if (!node.parent_id) return;
      openDocsNode(node.parent_id);
    },
  });

  if (loading) {
    return (
      <div className="h-full min-h-0 space-y-3 p-6" data-shell-workspace="docs-loading">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-[70vh] w-full" />
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex h-full min-h-0 min-w-0 overflow-hidden bg-background",
        !isDocsRoute && "min-h-[640px]",
      )}
      data-shell-workspace="docs"
      onKeyDownCapture={handleWorkspaceKeyDownCapture}
      onKeyUpCapture={handleWorkspaceKeyUpCapture}
    >
      {docsSidebarSlot ? createPortal(docsSidebar, docsSidebarSlot) : docsSidebar}
      <div className="flex h-full min-h-0 min-w-0 flex-1 overflow-hidden bg-background" data-shell-region="docs-canvas">
        {tagPageId ? (
          <SupertagPage
            apiFetch={apiFetch}
            tag={activeTagPage}
            tags={state.supertags}
            views={state.views}
            nodes={visibleNodes}
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
            readOnly={!canEditActiveTagDefinitions}
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
            searchNodes={visibleNodes.filter((node) => node.node_type === "search")}
            onCreateSearchNode={(tag) => void createSearchNode(tag)}
            onOpenNode={(nodeId) => {
              openDocsNode(nodeId);
            }}
          />
        ) : (
          renderPanel(focusNode, currentRows)
        )}
        {!tagPageId && !mainView && splitNode ? <div className="hidden min-w-0 flex-1 border-l border-border bg-background xl:flex" data-docs-split-view>{renderPanel(splitNode, splitRows, true)}</div> : null}
      </div>
      <Sheet open={propertiesOpen && !wideDocsViewport} onOpenChange={setPropertiesOpen}>
        <SheetContent side="right" className="w-[min(92vw,34rem)] overflow-auto sm:max-w-[34rem]">
          <SheetHeader>
            <SheetTitle>Docs設定</SheetTitle>
          </SheetHeader>
          {renderRightPanel()}
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
        onCopyReference={(node) => void copySidebarNodeReference(node)}
        onCopyId={(node) => void copySidebarNodeId(node)}
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
      <DocsShareDialog
        open={Boolean(shareNode)}
        nodeId={shareNode?.id ?? null}
        nodeTitle={shareNode ? nodeText(shareNode) : undefined}
        apiFetch={apiFetch}
        onOpenChange={(open) => {
          if (!open) setShareNode(null);
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

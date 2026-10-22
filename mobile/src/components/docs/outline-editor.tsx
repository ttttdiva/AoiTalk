import React, {
  forwardRef,
  memo,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { Alert, FlatList, Keyboard, Pressable, ScrollView, StyleSheet, TextInput, View, type ScrollViewProps } from "react-native";
import {
  Button,
  Dialog,
  Icon,
  IconButton,
  Portal,
  Surface,
  Text,
} from "react-native-paper";
import { docsRepo } from "../../repositories/docs";
import { randomId } from "../../repositories/outbox";
import { getCachedToken, getTokenAuthScope, getTokenSnapshot } from "../../lib/auth";
import { expandProtectedDocsNodeAncestors } from "../../repositories/docs-reconciliation";
import {
  blankParagraphBodyJson,
  clearBlankParagraphMarker,
  isEditableDocsBlockBody,
  isExplicitBlankParagraph,
} from "../../lib/docs-blank";
import type { DocsNode } from "../../types/api";
import { createOutlineEnterMeasurement, createOutlineInputMeasurement,
  type OutlineEnterMeasurement, type OutlineInputMeasurement } from "./outline-input-measurement";
import { calculateInsertSortOrder, flattenVisibleOutline, placePendingOutlineRows, sortOutlineNodes } from "./outline-editor-model";
import { showOutlineInputKeyboard } from "./outline-native-keyboard";
import { OutlineNativeInput, outlineNativeInputAvailable, type OutlineNativeInputHandle } from "./outline-native-input";
import { OutlineNativeSession, type NativeOutlineValue } from "./outline-native-session";
import { DocBlockEditor, isDocBlockNode } from "./verbatim-blocks";
import { DocBlockDraftStore, serializeDocsNodeEdit, type DocBlockDraft } from "./doc-block-drafts";

function isProtectedProjectNode(node: Pick<DocsNode, "system_key">) {
  const key = String(node.system_key ?? "").trim();
  return key === "project_information_root" || key.startsWith("project_information:");
}

export type OutlineEditorHandle = {
  focusFirstOrCreate: () => Promise<void>;
  reloadFromRepository: () => Promise<void>;
  flushPending: () => Promise<void>;
  hasPendingEdits: () => boolean;
};

type Props = {
  rootNodeId: string;
  showArchived: boolean;
  reloadToken: number;
  onOpen: (nodeId: string) => void;
  onMoveRequest: (nodeId: string) => void;
  onChanged?: () => void;
  listHeader?: React.ReactElement;
  readOnly?: boolean;
  onInputMeasurement?: (measurement: OutlineInputMeasurement) => void;
  onEnterMeasurement?: (measurement: OutlineEnterMeasurement) => void;
  onBodyPendingChange?: (pending: boolean) => void;
};

type OutlineInput = TextInput | OutlineNativeInputHandle;

// VirtualizedList supplies its cells as children. Keep the input beside those
// cells, inside the native scrolling content, without making it a recyclable
// cell. Native scrolling then moves the row and its retained InputConnection
// together; no per-frame JS measurement needs to catch up with a scroll.
const NativeOutlineScrollView = forwardRef<ScrollView, ScrollViewProps & { editor: React.ReactNode }>(
  function NativeOutlineScrollView({ children, editor, ...props }, ref) {
    return <ScrollView {...props} ref={ref} removeClippedSubviews={false}>{children}{editor}</ScrollView>;
  },
);
type OutlineDraftScope = { auth: ReturnType<typeof getTokenSnapshot>; workspaceId: string | null; projectId: string | null };
type KeyboardFocusRequest = {
  nodeId: string;
  rootId: string;
  input: OutlineInput;
  cancelKeyboard?: () => void;
};
type PendingOutlineCreate = {
  node: DocsNode;
  rootId: string;
  afterId: string;
  auth: ReturnType<typeof getTokenSnapshot>;
  flight?: Promise<void>;
};

// Opt-in, numeric-only diagnostics for device QA. No titles, IDs or text leave
// the editor; no persistent store or network transport is involved.
const logInputMeasurement = process.env.EXPO_PUBLIC_DOCS_INPUT_METRICS === "1"
  ? (measurement: OutlineInputMeasurement) => console.info("[DocsInput]", measurement)
  : undefined;
const logEnterMeasurement = process.env.EXPO_PUBLIC_DOCS_INPUT_METRICS === "1"
  ? (measurement: OutlineEnterMeasurement) => console.info("[DocsEnter]", measurement)
  : undefined;

export const OutlineEditor = forwardRef<OutlineEditorHandle, Props>(
  function OutlineEditor(
    {
      rootNodeId,
      showArchived,
      reloadToken,
      onOpen,
      onMoveRequest,
      onChanged,
      listHeader,
      readOnly = false,
      onInputMeasurement = logInputMeasurement,
      onEnterMeasurement = logEnterMeasurement,
      onBodyPendingChange,
    },
    ref,
  ) {
    const [nodes, setNodes] = useState<DocsNode[]>([]);
    const [loading, setLoading] = useState(true);
    const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [pendingFocusId, setPendingFocusId] = useState<string | null>(null);
    const [keyboardVisible, setKeyboardVisible] = useState(false);
    const [moreTargetId, setMoreTargetId] = useState<string | null>(null);
    const [saveErrors, setSaveErrors] = useState<Record<string, string>>({});
    const nativeInputEnabled = useMemo(outlineNativeInputAvailable, []);
    const nativeInputRef = useRef<OutlineNativeInputHandle | null>(null);
    const nativeSessionRef = useRef<OutlineNativeSession | null>(null);
    const nativeScopesRef = useRef(new Map<number, OutlineDraftScope & { rootId: string }>());
    const nativeCacheScopeRef = useRef<{ key: string | null; sequence: number }>({ key: null, sequence: 0 });
    const [nativeCacheScope, setNativeCacheScope] = useState(0);
    const [nativeValue, setNativeValue] = useState<NativeOutlineValue>({ epoch: 0, generation: 0, eventCount: 0, text: "" });
    const [nativeRect, setNativeRect] = useState<{ x: number; y: number; width: number; nodeId: string } | null>(null);
    const [nativeHeight, setNativeHeight] = useState(30);
    const nativeHeightRef = useRef(nativeHeight);
    nativeHeightRef.current = nativeHeight;
    const containerRef = useRef<View | null>(null);
    const scrollContentRef = useRef<View | null>(null);
    const rowAnchorsRef = useRef(new Map<string, View>());
    const nativePositionRequestRef = useRef(0);
    const nativeChangedRef = useRef(false);
    const nodesRef = useRef<DocsNode[]>([]);
    const visibleRowCountRef = useRef(0);
    const rowsRef = useRef<ReturnType<typeof flattenVisibleOutline>>([]);
    const containerFrameRef = useRef<{ y: number; height: number } | null>(null);
    const listFrameRef = useRef<{ y: number; height: number } | null>(null);
    const keyboardTopRef = useRef<number | null>(Keyboard.metrics?.()?.screenY ?? null);
    const keyboardScrollRef = useRef<() => void>(() => undefined);
    const scrollRequestRef = useRef<{
      nodeId: string; rootId: string; generation: number; index: number;
      native: boolean; viewPosition: number; viewOffset: number;
    } | null>(null);
    const editingIdRef = useRef(editingId);
    const focusGenerationRef = useRef(0);
    const scrollRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const listRef = useRef<FlatList>(null);
    const inputRefs = useRef(new Map<string, OutlineInput>());
    const laidOutInputsRef = useRef(new WeakSet<OutlineInput>());
    const enterMeasurementsRef = useRef(new Map<string, ReturnType<typeof createOutlineEnterMeasurement>>());
    const enterSampleRef = useRef(0);
    const keyboardFocusRef = useRef<KeyboardFocusRequest | null>(null);
    const draftsRef = useRef(new Map<string, string>());
    const draftAuthRef = useRef(new Map<string, OutlineDraftScope>());
    const savingTitlesRef = useRef(new Map<string, string>());
    const saveTimersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());
    const nativeSaveWorkRef = useRef(new Map<string, () => void>());
    const nativeSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const saveChainsRef = useRef(new Map<string, Promise<void>>());
    const pendingCreatesRef = useRef(new Map<string, PendingOutlineCreate>());
    const openingIdsRef = useRef(new Set<string>());
    const tapFocusRef = useRef<{ nodeId: string; rootId: string } | null>(null);
    const tapMeasurementRef = useRef<{
      nodeId: string;
      measurement: ReturnType<typeof createOutlineInputMeasurement>;
    } | null>(null);
    const loadRequestRef = useRef(0);
    const editRevisionRef = useRef(0);
    const activeRootIdRef = useRef(rootNodeId);
    const readOnlyRef = useRef(readOnly);
    activeRootIdRef.current = rootNodeId;
    const mountedRef = useRef(false);
    useEffect(() => {
      mountedRef.current = true;
      return () => { mountedRef.current = false; };
    }, []);
    readOnlyRef.current = readOnly;
    nodesRef.current = nodes;
    editingIdRef.current = editingId;
    const blockDrafts = useMemo(() => new DocBlockDraftStore(() => !readOnlyRef.current), [rootNodeId]);

    const notifyPending = useCallback(() => {
      if (mountedRef.current) onBodyPendingChange?.(
        blockDrafts.pending || draftsRef.current.size > 0 || savingTitlesRef.current.size > 0
          || pendingCreatesRef.current.size > 0,
      );
    }, [blockDrafts, onBodyPendingChange]);
    const notifyChanged = useCallback(() => {
      // Ordinary local rows are already rendered. Avoid a full parent metadata
      // read on every Enter while the IME is accepting continuous input.
      if (nativeInputEnabled && editingIdRef.current) nativeChangedRef.current = true;
      else onChanged?.();
    }, [nativeInputEnabled, onChanged]);
    const drainNativeSaves = useCallback(() => {
      if (nativeSaveTimerRef.current) clearTimeout(nativeSaveTimerRef.current);
      nativeSaveTimerRef.current = null;
      const work = [...nativeSaveWorkRef.current.values()];
      nativeSaveWorkRef.current.clear();
      for (const save of work) save();
    }, []);
    const queueNativeSave = useCallback((key: string, save: () => void) => {
      nativeSaveWorkRef.current.set(key, save);
      if (nativeSaveTimerRef.current) clearTimeout(nativeSaveTimerRef.current);
      // SQLite still contains synchronous ACL/outbox transactions. Starting
      // them from the submit microtask can block the first row commit. Share
      // the existing title-save quiet period across native paragraphs so the
      // next character/Enter can render before any of those writes begin.
      nativeSaveTimerRef.current = setTimeout(drainNativeSaves, 450);
    }, [drainNativeSaves]);
    useEffect(() => {
      if (!editingId && nativeChangedRef.current) {
        nativeChangedRef.current = false;
        onChanged?.();
      }
    }, [editingId, onChanged]);
    const positionNativeInput = useCallback(() => {
      const nodeId = editingIdRef.current;
      const anchor = nodeId && rowAnchorsRef.current.get(nodeId);
      const content = scrollContentRef.current;
      if (!nativeInputEnabled || !nodeId || !anchor || !content) return;
      const request = ++nativePositionRequestRef.current;
      const rootId = activeRootIdRef.current;
      // Both views share the same native scrolling content. These coordinates
      // remain correct through native scrolls, including between JS frames.
      anchor.measureLayout(content, (x, y, width) => {
        if (request !== nativePositionRequestRef.current || editingIdRef.current !== nodeId
          || rootId !== activeRootIdRef.current || rowAnchorsRef.current.get(nodeId) !== anchor
          || scrollContentRef.current !== content || width <= 0) return;
        const next = { x, y, width, nodeId };
        setNativeRect((current) => current?.nodeId === nodeId && current.x === next.x
          && current.y === next.y && current.width === width ? current : next);
        enterMeasurementsRef.current.get(nodeId)?.mark("input-mount");
      }, () => undefined);
    }, [nativeInputEnabled]);
    const handleRowLayout = useCallback((nodeId: string) => {
      // Inserting a row lays out every following visible sibling. Those
      // unrelated callbacks must not invalidate the active row's measurement.
      if (nodeId === editingIdRef.current) positionNativeInput();
    }, [positionNativeInput]);
    const registerRowAnchor = useCallback((nodeId: string, anchor: View | null) => {
      if (anchor) rowAnchorsRef.current.set(nodeId, anchor);
      else rowAnchorsRef.current.delete(nodeId);
    }, []);
    useEffect(() => {
      notifyPending();
      return blockDrafts.subscribe(notifyPending);
    }, [blockDrafts, notifyPending]);

    useEffect(() => blockDrafts.retain(), [blockDrafts]);

    const load = useCallback(async () => {
      const requestedRootId = rootNodeId;
      if (activeRootIdRef.current !== requestedRootId) return;
      const requestId = ++loadRequestRef.current;
      const editRevision = editRevisionRef.current;
      try {
        const loadedNodes = await docsRepo.listOutline(requestedRootId, true);
        if (
          activeRootIdRef.current === requestedRootId &&
          requestId === loadRequestRef.current &&
          editRevision === editRevisionRef.current
        ) {
          const loadedIds = new Set(loadedNodes.map((node) => node.id));
          // A list read can omit a newly created/edited row while its local
          // write is still pending. Keep that row and its draft visible.
          const pendingIds = nodesRef.current.filter((node) => draftsRef.current.has(node.id)
            || savingTitlesRef.current.has(node.id)
            || pendingCreatesRef.current.has(node.id)
            || (isDocBlockNode(node) && blockDrafts.get(node).pending)).map((node) => node.id);
          const protectedIds = expandProtectedDocsNodeAncestors(
            nodesRef.current.map((node) => ({ id: node.id, parentId: node.parent_id })), pendingIds,
          );
          const pendingRows = nodesRef.current.filter((node) => !loadedIds.has(node.id)
            && protectedIds.has(node.id));
          for (const entry of pendingCreatesRef.current.values()) {
            if (entry.rootId === requestedRootId && !loadedIds.has(entry.node.id)
              && !pendingRows.some((row) => row.id === entry.node.id)) pendingRows.push(entry.node);
          }
          setNodes(placePendingOutlineRows(
            [...loadedNodes, ...pendingRows].map((node) => {
              const optimisticTitle =
                draftsRef.current.get(node.id) ??
                savingTitlesRef.current.get(node.id);
              return optimisticTitle === undefined
                ? node
                : { ...node, title: optimisticTitle };
            }), [...pendingCreatesRef.current.values()].filter((entry) => entry.rootId === requestedRootId),
          ));
        }
      } catch {
        if (
          activeRootIdRef.current === requestedRootId &&
          requestId === loadRequestRef.current &&
          editRevision === editRevisionRef.current &&
          !blockDrafts.pending && draftsRef.current.size === 0 && savingTitlesRef.current.size === 0
            && pendingCreatesRef.current.size === 0
        ) {
          setNodes([]);
        }
      } finally {
        if (
          activeRootIdRef.current === requestedRootId &&
          requestId === loadRequestRef.current
        ) {
          setLoading(false);
        }
      }
    }, [blockDrafts, rootNodeId]);

    useEffect(() => {
      loadRequestRef.current += 1;
      setNodes([]);
      setSaveErrors({});
      setExpandedIds(new Set());
      setLoading(true);
      if (nativeInputEnabled) {
        nativeSessionRef.current?.clear();
        nativeScopesRef.current.clear();
        setNativeRect(null);
        setEditingId(null);
        editingIdRef.current = null;
      }
    }, [nativeInputEnabled, rootNodeId]);

    useEffect(() => {
      void load();
    }, [load, reloadToken]);

    useEffect(() => {
      const show = Keyboard.addListener("keyboardDidShow", (event) => {
        const screenY = event?.endCoordinates?.screenY ?? Keyboard.metrics?.()?.screenY;
        keyboardTopRef.current = typeof screenY === "number" && Number.isFinite(screenY) ? screenY : null;
        setKeyboardVisible(true);
        keyboardScrollRef.current();
      });
      const hide = Keyboard.addListener("keyboardDidHide", () => {
        keyboardTopRef.current = null;
        setKeyboardVisible(false);
        // Android Back can dismiss IME without blurring the editor. Never let
        // an already queued native show reopen a keyboard the user dismissed.
        if (keyboardFocusRef.current?.cancelKeyboard) {
          keyboardFocusRef.current.cancelKeyboard();
          keyboardFocusRef.current = null;
        }
      });
      return () => {
        show.remove();
        hide.remove();
      };
    }, []);

    const rows = useMemo(
      () => flattenVisibleOutline(nodes, rootNodeId, expandedIds, showArchived),
      [expandedIds, nodes, rootNodeId, showArchived],
    );
    visibleRowCountRef.current = rows.length;
    rowsRef.current = rows;

    const nativeScrollOffset = useCallback(() => {
      const container = containerFrameRef.current;
      const list = listFrameRef.current;
      if (!container || !list) return 64;
      const top = container.y + list.y;
      const height = Math.max(0, Math.min(list.height,
        keyboardTopRef.current === null ? list.height : keyboardTopRef.current - top - 54));
      // The toolbar occupies its own 48dp plus 6dp margin outside the list.
      // Clip a stale viewport during IME resize without subtracting it twice
      // once the list has received its smaller native layout.
      return Math.max(0, height - Math.max(30, nativeHeightRef.current)) * 0.55;
    }, []);

    const scrollToOutlineRow = useCallback((nodeId: string) => {
      const index = rowsRef.current.findIndex((row) => row.node.id === nodeId);
      if (index < 0 || readOnlyRef.current) return;
      const native = nativeInputEnabled && !isDocBlockNode(rowsRef.current[index].node);
      if (native && (editingIdRef.current !== nodeId
        || nativeSessionRef.current?.currentNodeId !== nodeId)) return;
      if (scrollRetryTimerRef.current) clearTimeout(scrollRetryTimerRef.current);
      scrollRetryTimerRef.current = null;
      const request = { nodeId, rootId: activeRootIdRef.current,
        generation: focusGenerationRef.current, index, native,
        viewPosition: native ? 0 : 0.55, viewOffset: native ? nativeScrollOffset() : 0 };
      scrollRequestRef.current = request;
      if (process.env.EXPO_PUBLIC_DOCS_INPUT_METRICS === "1") console.info("[DocsScroll]", {
        action: 0, index, viewOffset: request.viewOffset,
        viewportHeight: listFrameRef.current?.height ?? -1, keyboardTop: keyboardTopRef.current ?? -1,
        generation: nativeSessionRef.current?.currentValue?.generation ?? 0,
      });
      listRef.current?.scrollToIndex({ index, animated: !native,
        viewPosition: request.viewPosition, ...(native ? { viewOffset: request.viewOffset } : {}) });
    }, [nativeInputEnabled, nativeScrollOffset]);

    keyboardScrollRef.current = () => {
      const nodeId = editingIdRef.current;
      const focus = keyboardFocusRef.current;
      const scroll = scrollRequestRef.current;
      const ownsFocus = focus?.nodeId === nodeId && focus.rootId === activeRootIdRef.current;
      const ownsScroll = scroll?.nodeId === nodeId && scroll.rootId === activeRootIdRef.current
        && scroll.generation === focusGenerationRef.current;
      if (nativeInputEnabled && nodeId && (ownsFocus || ownsScroll)) scrollToOutlineRow(nodeId);
    };
    useEffect(() => {
      if (nativeInputEnabled && keyboardTopRef.current !== null) keyboardScrollRef.current();
    }, [nativeHeight, nativeInputEnabled]);

    const handleScrollToIndexFailed = useCallback(({ index, averageItemLength }: {
      index: number; averageItemLength: number;
    }) => {
      const request = scrollRequestRef.current;
      const current = () => request && scrollRequestRef.current === request
        && request.rootId === activeRootIdRef.current
        && request.generation === focusGenerationRef.current && !readOnlyRef.current
        && (!request.native || (editingIdRef.current === request.nodeId
          && nativeSessionRef.current?.currentNodeId === request.nodeId));
      if (!request || request.index !== index || !current()) return;
      if (process.env.EXPO_PUBLIC_DOCS_INPUT_METRICS === "1") console.info("[DocsScroll]", {
        action: 1, index, generation: nativeSessionRef.current?.currentValue?.generation ?? 0,
      });
      if (scrollRetryTimerRef.current) clearTimeout(scrollRetryTimerRef.current);
      listRef.current?.scrollToOffset({
        offset: Math.max(0, averageItemLength * index - request.viewOffset), animated: false,
      });
      scrollRetryTimerRef.current = setTimeout(() => {
        scrollRetryTimerRef.current = null;
        if (!current()) return;
        // Resolve the current identity again: continuous native Enter does not
        // change focusGeneration, and an older index must never reclaim scroll.
        scrollToOutlineRow(request.nodeId);
      }, 120);
    }, [scrollToOutlineRow]);

    const cancelPendingFocus = useCallback(() => {
      scrollRequestRef.current = null;
      nativeInputRef.current?.cancelFocus();
      focusGenerationRef.current += 1;
      if (scrollRetryTimerRef.current) clearTimeout(scrollRetryTimerRef.current);
      scrollRetryTimerRef.current = null;
      setPendingFocusId(null);
      tapFocusRef.current = null;
      keyboardFocusRef.current?.cancelKeyboard?.();
      keyboardFocusRef.current = null;
      tapMeasurementRef.current?.measurement.cancel();
      tapMeasurementRef.current = null;
    }, []);

    const registerInput = useCallback((nodeId: string, input: OutlineInput | null) => {
      if (!input) {
        if (keyboardFocusRef.current?.nodeId === nodeId) {
          keyboardFocusRef.current.cancelKeyboard?.();
          keyboardFocusRef.current = null;
        }
        inputRefs.current.delete(nodeId);
        return;
      }
      inputRefs.current.set(nodeId, input);
      enterMeasurementsRef.current.get(nodeId)?.mark("input-mount");
      const pending = tapFocusRef.current;
      if (pending?.nodeId !== nodeId || pending.rootId !== activeRootIdRef.current
        || readOnlyRef.current) return;
      tapMeasurementRef.current?.measurement.mounted();
      // A JS ref alone does not establish that Android has attached/served this view.
      // Keep the request pending until the native input reports its layout.
    }, []);

    const requestInputFocus = useCallback((nodeId: string, input: OutlineInput) => {
      keyboardFocusRef.current?.cancelKeyboard?.();
      const request: KeyboardFocusRequest = {
        nodeId, rootId: activeRootIdRef.current, input,
      };
      keyboardFocusRef.current = request;
      if (input.isFocused?.()) {
        // RN intentionally makes focus() a no-op for its current input. A
        // keyboard dismissed with Back still needs an explicit native show.
        request.cancelKeyboard = showOutlineInputKeyboard(input);
      } else {
        input.focus();
      }
    }, []);

    const handleInputLayout = useCallback((nodeId: string, input: OutlineInput | null) => {
      if (input) laidOutInputsRef.current.add(input);
      const pending = tapFocusRef.current;
      if (!input || inputRefs.current.get(nodeId) !== input
        || pending?.nodeId !== nodeId || pending.rootId !== activeRootIdRef.current
        || editingIdRef.current !== nodeId || readOnlyRef.current) return;
      tapFocusRef.current = null;
      // Fabric layout is not a served-view signal. Establish view focus here;
      // Android IME show is completed separately after native onFocus.
      requestInputFocus(nodeId, input);
    }, [requestInputFocus]);

    const handleInputFocus = useCallback((nodeId: string, input: OutlineInput | null) => {
      if (!input || inputRefs.current.get(nodeId) !== input
        || editingIdRef.current !== nodeId || readOnlyRef.current) return;
      enterMeasurementsRef.current.get(nodeId)?.mark("native-focus");
      setSelectedId(nodeId);
      const tap = tapFocusRef.current;
      if (tap?.nodeId === nodeId && tap.rootId === activeRootIdRef.current) {
        // Native touch can focus before our layout callback. Consume that
        // explicit tap without re-focusing or replacing the native editor.
        tapFocusRef.current = null;
        keyboardFocusRef.current = { nodeId, rootId: tap.rootId, input };
      }
      const request = keyboardFocusRef.current;
      if (request?.nodeId === nodeId && request.input === input
        && request.rootId === activeRootIdRef.current && !request.cancelKeyboard) {
        request.cancelKeyboard = showOutlineInputKeyboard(input);
      }
      if (tapMeasurementRef.current?.nodeId === nodeId) {
        tapMeasurementRef.current.measurement.focused();
        tapMeasurementRef.current = null;
      }
    }, []);

    useEffect(() => cancelPendingFocus, [cancelPendingFocus, rootNodeId, readOnly]);

    const toggleExpanded = useCallback((nodeId: string) => {
      cancelPendingFocus();
      setExpandedIds((current) => {
        const next = new Set(current);
        if (next.has(nodeId)) next.delete(nodeId);
        else next.add(nodeId);
        return next;
      });
    }, [cancelPendingFocus]);

    const ensureCreated = useCallback(async function persist(nodeId: string): Promise<void> {
      const entry = pendingCreatesRef.current.get(nodeId);
      if (!entry) return;
      if (entry.flight) return entry.flight;
      const flight = (async () => {
        try {
          // Consecutive Enter can reference a row whose SQLite insertion has not
          // started. Only persistence waits for that dependency; input never does.
          await persist(entry.afterId);
          const auth = await entry.auth;
          if (activeRootIdRef.current === entry.rootId && readOnlyRef.current) {
            throw new Error("編集権限が変わったため保存できません。入力内容は保持しています。");
          }
          const created = await docsRepo.createSiblingAfter(entry.afterId, "", {
            id: entry.node.id,
            guard: { auth, workspaceId: entry.node.workspace_id, projectId: entry.node.project_id },
            expectedParentId: entry.node.parent_id,
          });
          enterMeasurementsRef.current.get(nodeId)?.mark("local-commit");
          pendingCreatesRef.current.delete(nodeId);
          editRevisionRef.current += 1;
          if (mountedRef.current && activeRootIdRef.current === entry.rootId) {
            setNodes((current) => current.map((row) => row.id === nodeId ? {
                ...created, id: nodeId,
                title: draftsRef.current.get(nodeId) ?? savingTitlesRef.current.get(nodeId) ?? row.title,
                // Keep one consistent visual ordering until a complete SQLite
                // snapshot arrives. A sync may have renumbered every sibling;
                // adopting only this row's new number would move it to the end.
                sort_order: row.sort_order,
              } : row));
            setSaveErrors((errors) => {
              const next = { ...errors }; delete next[nodeId]; return next;
            });
            notifyChanged();
          }
        } catch (error) {
          enterMeasurementsRef.current.get(nodeId)?.mark("save-error");
          if (mountedRef.current && activeRootIdRef.current === entry.rootId) {
            setSaveErrors((errors) => ({ ...errors, [nodeId]: error instanceof Error ? error.message
              : "新しい行を保存できません。入力内容は保持しています。" }));
          }
          throw error;
        } finally {
          entry.flight = undefined;
          notifyPending();
        }
      })();
      entry.flight = flight;
      return flight;
    }, [notifyPending, notifyChanged]);

    const flushDraft = useCallback(async (nodeId: string) => {
      await ensureCreated(nodeId);
      await blockDrafts.flush(nodeId);
      if (readOnly || readOnlyRef.current) {
        if (draftsRef.current.has(nodeId) || savingTitlesRef.current.has(nodeId)) {
          throw new Error("編集権限が変わったため保存できません。入力内容は保持しています。");
        }
        return;
      }
      const timer = saveTimersRef.current.get(nodeId);
      if (timer) clearTimeout(timer);
      saveTimersRef.current.delete(nodeId);
      const draft = draftsRef.current.get(nodeId);
      if (draft === undefined) {
        return saveChainsRef.current.get(nodeId) ?? Promise.resolve();
      }
      draftsRef.current.delete(nodeId);
      const normalizedRaw = draft.replace(/[\r\n]+/g, " ");
      // Whitespace-only IME drafts are semantically cleared paragraphs.  Do
      // not enqueue a title consisting solely of spaces, which the server
      // normalizes to empty and correctly rejects as a malformed blank row.
      const normalized = normalizedRaw.trim().length === 0 ? "" : normalizedRaw;
      savingTitlesRef.current.set(nodeId, normalized);
      notifyPending();
      const saveRootId = rootNodeId;
      const authAtEdit = draftAuthRef.current.get(nodeId);
      const previous = saveChainsRef.current.get(nodeId) ?? Promise.resolve();
      const next = previous
        .catch(() => undefined)
        .then(() => serializeDocsNodeEdit(nodeId, async () => {
          try {
            const originalAuth = await authAtEdit?.auth;
            const assertActor = async () => {
              const currentAuth = await getTokenSnapshot();
              if (readOnlyRef.current) {
                throw new Error("編集権限が変わったため保存できません。入力内容は保持しています。");
              }
              if (!originalAuth || currentAuth.token !== originalAuth.token
                || currentAuth.revision !== originalAuth.revision) {
                throw new Error("アカウントが変わったため保存できません。入力内容は保持しています。");
              }
            };
            await assertActor();
            // A queued save may start after a live ACL refresh made the
            // outline read-only. Keep the draft, but never enqueue that write.
            if (readOnlyRef.current) {
              throw new Error("編集権限が変わったため保存できません。入力内容は保持しています。");
            }
            // A reload/root change may have removed the row from this render.
            // Never infer an empty paragraph from missing metadata: that could
            // replace a typed block's body when its label draft is empty.
            const cachedNode = nodesRef.current.find((node) => node.id === nodeId);
            const currentNode = cachedNode && !isDocBlockNode(cachedNode)
              ? cachedNode : await docsRepo.getNode(nodeId) ?? cachedNode;
            if (readOnlyRef.current) {
              throw new Error("編集権限が変わったため保存できません。入力内容は保持しています。");
            }
            if (!currentNode) throw new Error("保存対象のノードを確認できません");
            const currentBody = currentNode.body_json;
            const isBlock = isEditableDocsBlockBody(currentBody);
            const currentIsBlank = currentNode
              ? typeof currentNode.is_explicit_blank === "boolean"
                ? currentNode.is_explicit_blank === true
                : isExplicitBlankParagraph(
                    currentNode.title,
                    currentBody,
                    currentNode.node_type,
                  )
              : false;
            const blockLabel = isBlock && normalized.trim().length === 0
              ? (typeof currentBody?.label === "string" && currentBody.label.trim()
                ? currentBody.label
                : currentNode?.title || "本文")
              : normalized;
            const patch = isBlock
              ? {
                  title: blockLabel,
                  bodyText: blockLabel,
                  bodyJson: { ...currentBody, label: blockLabel },
                }
              : normalized === ""
                ? {
                    title: "",
                    bodyText: "",
                    bodyJson: blankParagraphBodyJson(currentBody),
                  }
                : currentIsBlank
                  ? {
                      title: normalized,
                      bodyText: normalized,
                      bodyJson: clearBlankParagraphMarker(currentBody),
                    }
                  : { title: normalized };
            await assertActor();
            const updated = await docsRepo.updateNode(nodeId, patch, {
              auth: originalAuth!,
              workspaceId: authAtEdit!.workspaceId,
              projectId: authAtEdit!.projectId,
            });
            if (mountedRef.current && activeRootIdRef.current === saveRootId) {
              // A read begun before this write must not restore its old row
              // after the draft has left the pending maps.
              editRevisionRef.current += 1;
              setSaveErrors((errors) => {
                if (!(nodeId in errors)) return errors;
                const nextErrors = { ...errors };
                delete nextErrors[nodeId];
                return nextErrors;
              });
              if (isBlock || normalized === "" || currentIsBlank || updated.title !== normalized) {
                // Capture before scheduling React state. These maps may be
                // cleared by the next save before React applies the updater.
                const newerTitle = draftsRef.current.get(nodeId)
                  ?? (saveChainsRef.current.get(nodeId) !== next
                    ? savingTitlesRef.current.get(nodeId) : undefined);
                const title = newerTitle ?? updated.title;
                setNodes((current) => current.map((node) => {
                  if (node.id !== nodeId) return node;
                  return {
                    ...node, title,
                    body_text: updated.body_text,
                    body_json: updated.body_json,
                    is_explicit_blank: title === "" && updated.is_explicit_blank,
                  };
                }));
              }
            }
          } catch (error) {
            if (
              saveChainsRef.current.get(nodeId) === next &&
              !draftsRef.current.has(nodeId)
            ) {
              draftsRef.current.set(nodeId, normalized);
            }
            if (mountedRef.current && activeRootIdRef.current === saveRootId) {
              setSaveErrors((errors) => ({ ...errors, [nodeId]: error instanceof Error
                ? error.message : "本文の保存に失敗しました。入力内容は保持しています。" }));
            }
            throw error;
          }
        }))
        .finally(() => {
          if (saveChainsRef.current.get(nodeId) === next) {
            savingTitlesRef.current.delete(nodeId);
            if (!draftsRef.current.has(nodeId)) draftAuthRef.current.delete(nodeId);
          }
          notifyPending();
        });
      saveChainsRef.current.set(nodeId, next);
      return next;
    }, [blockDrafts, ensureCreated, notifyPending, readOnly, rootNodeId]);

    const beginNativeEditing = useCallback((node: DocsNode) => {
      const cachedToken = typeof getCachedToken === "function" ? getCachedToken() : undefined;
      const scopeKey = cachedToken === undefined ? null : `${rootNodeId}:${getTokenAuthScope(cachedToken)}`;
      const cacheScope = nativeCacheScopeRef.current;
      if (scopeKey !== cacheScope.key) { cacheScope.key = scopeKey; cacheScope.sequence += 1; }
      // Native receives only an opaque numeric namespace. Unknown actors cannot
      // restore native snapshots; account/root switches cannot reuse another scope.
      setNativeCacheScope(scopeKey === null ? 0 : cacheScope.sequence);
      const auth = pendingCreatesRef.current.get(node.id)?.auth ?? getTokenSnapshot();
      void auth.catch(() => undefined);
      const scope = { auth, workspaceId: node.workspace_id, projectId: node.project_id };
      if (!draftsRef.current.has(node.id) && !savingTitlesRef.current.has(node.id)) {
        draftAuthRef.current.set(node.id, scope);
      }
      const value = nativeSessionRef.current!.begin(node.id, node.title);
      nativeScopesRef.current.set(value.epoch, { ...scope, rootId: rootNodeId });
      // A fresh native epoch must remeasure even when the same row ID is
      // reopened; otherwise the retained rect can briefly revive an old
      // coordinate before the new anchor measurement arrives.
      nativePositionRequestRef.current += 1;
      setNativeRect(null);
      setNativeValue(value);
      setNativeHeight(30);
    }, [rootNodeId]);

    const beginEditing = useCallback((nodeId: string) => {
      cancelPendingFocus();
      void blockDrafts.flush().catch(() => undefined);
      const previousId = editingIdRef.current;
      if (previousId && previousId !== nodeId) {
        // Unmounting a native input need not emit blur. Queue its draft before
        // switching, but never await SQLite to mount the next row's editor.
        const save = () => { void flushDraft(previousId).catch(() => undefined); };
        if (nativeInputEnabled) queueNativeSave(`title:${previousId}`, save);
        else save();
      }
      const node = nodesRef.current.find((candidate) => candidate.id === nodeId);
      if (!node) return;
      if (readOnlyRef.current || isProtectedProjectNode(node)) {
        onOpen(nodeId);
        return;
      }
      tapFocusRef.current = { nodeId, rootId: rootNodeId };
      if (onInputMeasurement) {
        tapMeasurementRef.current = {
          nodeId,
          measurement: createOutlineInputMeasurement(visibleRowCountRef.current, onInputMeasurement),
        };
      }
      // A completed press edits immediately. Never wait for a second tap: once
      // mounted, TextInput owns cursor placement and native word selection.
      setSelectedId(nodeId);
      editingIdRef.current = nodeId;
      if (nativeInputEnabled && !isDocBlockNode(node)) {
        beginNativeEditing(node);
      }
      setEditingId(nodeId);
    }, [beginNativeEditing, blockDrafts, cancelPendingFocus, flushDraft, nativeInputEnabled, onInputMeasurement, onOpen, queueNativeSave, rootNodeId]);

    const waitForNodeSave = useCallback(
      async (nodeId: string) => {
        while (true) {
          await flushDraft(nodeId);
          const pending = saveChainsRef.current.get(nodeId);
          if (pending) await pending;
          if (!draftsRef.current.has(nodeId)
            && (!pending || saveChainsRef.current.get(nodeId) === pending)) return;
        }
      },
      [flushDraft],
    );

    const openNode = useCallback(
      async (nodeId: string) => {
        const actionRootId = rootNodeId;
        if (openingIdsRef.current.has(nodeId)) return;
        openingIdsRef.current.add(nodeId);
        try {
          await waitForNodeSave(nodeId);
          if (activeRootIdRef.current !== actionRootId) return;
          onOpen(nodeId);
        } finally {
          openingIdsRef.current.delete(nodeId);
        }
      },
      [onOpen, rootNodeId, waitForNodeSave],
    );

    const requestOpenNode = useCallback(
      (nodeId: string) => {
        void openNode(nodeId).catch(() => undefined);
      },
      [openNode],
    );

    const scheduleSave = useCallback(
      (nodeId: string, title: string, capturedNative = false) => {
        if (readOnly && !capturedNative) return;
        if (!draftAuthRef.current.has(nodeId)) {
          const auth = pendingCreatesRef.current.get(nodeId)?.auth ?? getTokenSnapshot();
          void auth.catch(() => undefined);
          const node = nodesRef.current.find((candidate) => candidate.id === nodeId);
          draftAuthRef.current.set(nodeId, {
            auth, workspaceId: node?.workspace_id ?? null, projectId: node?.project_id ?? null,
          });
        }
        draftsRef.current.set(nodeId, title);
        editRevisionRef.current += 1;
        notifyPending();
        const current = saveTimersRef.current.get(nodeId);
        if (current) clearTimeout(current);
        const save = () => { void flushDraft(nodeId).catch(() => undefined); };
        if (nativeInputEnabled) queueNativeSave(`title:${nodeId}`, save);
        else saveTimersRef.current.set(nodeId, setTimeout(save, 450));
      },
      [flushDraft, nativeInputEnabled, notifyPending, queueNativeSave, readOnly],
    );

    useEffect(
      () => () => {
        drainNativeSaves();
        for (const timer of saveTimersRef.current.values()) clearTimeout(timer);
        for (const nodeId of pendingCreatesRef.current.keys()) void ensureCreated(nodeId).catch(() => undefined);
        for (const nodeId of draftsRef.current.keys()) void flushDraft(nodeId).catch(() => undefined);
        void blockDrafts.flush().catch(() => undefined);
        cancelPendingFocus();
      },
      [blockDrafts, cancelPendingFocus, drainNativeSaves, ensureCreated, flushDraft],
    );

    useEffect(() => {
      if (!pendingFocusId) return;
      const index = rows.findIndex((row) => row.node.id === pendingFocusId);
      if (index < 0) return;
      const generation = focusGenerationRef.current;
      const focusRootId = rootNodeId;
      const target = rows[index].node;
      if (isDocBlockNode(target)) {
        const frame = requestAnimationFrame(() => {
          if (generation !== focusGenerationRef.current || readOnlyRef.current
            || focusRootId !== activeRootIdRef.current) return;
          scrollToOutlineRow(target.id);
          blockDrafts.get(target).requestEdit();
          setSelectedId(target.id);
          setPendingFocusId(null);
        });
        return () => cancelAnimationFrame(frame);
      }
      const nativeAlreadyBound = nativeInputEnabled && nativeSessionRef.current?.currentNodeId === pendingFocusId;
      if (!nativeAlreadyBound) tapFocusRef.current = { nodeId: pendingFocusId, rootId: rootNodeId };
      const explicitNativeResume = nativeInputEnabled && tapFocusRef.current?.nodeId === pendingFocusId;
      const refreshNativeValue = explicitNativeResume && (!nativeInputRef.current?.isFocused()
        || nativeSessionRef.current?.currentValue?.text !== target.title);
      if (nativeInputEnabled && (!nativeAlreadyBound || refreshNativeValue)) {
        beginNativeEditing(target);
      }
      editingIdRef.current = pendingFocusId;
      setEditingId(pendingFocusId);
      const frame = requestAnimationFrame(() => {
        if (generation !== focusGenerationRef.current || readOnlyRef.current
          || focusRootId !== activeRootIdRef.current || editingIdRef.current !== target.id
          || (nativeInputEnabled && nativeSessionRef.current?.currentNodeId !== target.id)) return;
        scrollToOutlineRow(target.id);
        if (nativeInputEnabled) {
          positionNativeInput();
          setPendingFocusId(null);
          return;
        }
        const input = inputRefs.current.get(pendingFocusId);
        if (input && laidOutInputsRef.current.has(input) && tapFocusRef.current?.nodeId === pendingFocusId) {
          tapFocusRef.current = null;
          requestInputFocus(pendingFocusId, input);
          setSelectedId(pendingFocusId);
        }
        setPendingFocusId(null);
      });
      return () => {
        cancelAnimationFrame(frame);
      };
    }, [beginNativeEditing, blockDrafts, nativeInputEnabled, pendingFocusId, positionNativeInput, requestInputFocus, rootNodeId, rows, scrollToOutlineRow]);

    const addChild = useCallback(
      async (parentId: string) => {
        if (readOnly || readOnlyRef.current) return;
        const actionRootId = rootNodeId;
        try {
          cancelPendingFocus();
          const focusGeneration = focusGenerationRef.current;
          await flushDraft(parentId);
          if (readOnlyRef.current || activeRootIdRef.current !== actionRootId) return;
          const parent = nodes.find((node) => node.id === parentId);
          const created = await docsRepo.createNode({
            parentId,
            projectId: parent?.project_id ?? null,
            title: "",
          });
          if (!mountedRef.current || activeRootIdRef.current !== actionRootId) return;
          editRevisionRef.current += 1;
          setNodes((current) => [...current, created]);
          setExpandedIds((current) => {
            const next = new Set(current);
            next.add(parentId);
            return next;
          });
          if (focusGeneration === focusGenerationRef.current) setPendingFocusId(created.id);
          onChanged?.();
        } catch (error) {
          if (mountedRef.current && activeRootIdRef.current === actionRootId) {
            Alert.alert("Docsを作成できません", error instanceof Error ? error.message : "保存先を同期してから再度お試しください");
          }
        }
      },
      [cancelPendingFocus, flushDraft, nodes, onChanged, readOnly, rootNodeId],
    );

    const addSibling = useCallback(
      (nodeId: string, submittedTitle?: string, input?: OutlineInput | null, nativeAdvance = false, activate = true,
        nativeScope?: OutlineDraftScope): string | null => {
        if (!nativeAdvance && (readOnly || readOnlyRef.current)) return null;
        // Reject duplicate/late submit from a native input that was replaced.
        if (!nativeAdvance && (editingIdRef.current !== nodeId || !input || inputRefs.current.get(nodeId) !== input)) return null;
        const source = nodesRef.current.find((row) => row.id === nodeId);
        if (!source || isProtectedProjectNode(source)) return null;
        const measurement = onEnterMeasurement
          ? createOutlineEnterMeasurement(++enterSampleRef.current, visibleRowCountRef.current, onEnterMeasurement) : undefined;
        if (submittedTitle !== undefined && submittedTitle !== source.title) {
          draftsRef.current.set(nodeId, submittedTitle);
          nodesRef.current = nodesRef.current.map((row) => row.id === nodeId ? { ...row, title: submittedTitle } : row);
        }
        if (!nativeAdvance) cancelPendingFocus();
        let siblings = sortOutlineNodes(nodesRef.current.filter((row) => row.parent_id === source.parent_id && !row.archived_at));
        let index = siblings.findIndex((row) => row.id === nodeId);
        let order = calculateInsertSortOrder(source, siblings[index + 1] ?? null);
        if (order === null) {
          const orders = new Map(siblings.map((row, i) => [row.id, (i + 1) * 1024]));
          nodesRef.current = nodesRef.current.map((row) => orders.has(row.id) ? { ...row, sort_order: orders.get(row.id)! } : row);
          siblings = sortOutlineNodes(nodesRef.current.filter((row) => row.parent_id === source.parent_id && !row.archived_at));
          index = siblings.findIndex((row) => row.id === nodeId);
          order = calculateInsertSortOrder(siblings[index], siblings[index + 1] ?? null)!;
        }
        const now = new Date().toISOString();
        const created: DocsNode = { ...source, id: randomId(), title: "", body_text: "",
          workspace_id: nativeScope ? nativeScope.workspaceId : source.workspace_id,
          project_id: nativeScope ? nativeScope.projectId : source.project_id,
          body_json: blankParagraphBodyJson(null), is_explicit_blank: true, node_type: "node",
          system_key: null, aliases: [], description: null, display_props: null, query_json: null,
          view_json: null, day_date: null, sort_order: order, created_by: null, updated_by: null,
          created_at: now, updated_at: now, archived_at: null };
        const auth = nativeScope?.auth ?? draftAuthRef.current.get(nodeId)?.auth ?? getTokenSnapshot();
        void auth.catch(() => undefined);
        if (!nativeAdvance) draftAuthRef.current.set(nodeId, { auth, workspaceId: source.workspace_id, projectId: source.project_id });
        pendingCreatesRef.current.set(created.id, { node: created, rootId: rootNodeId, afterId: nodeId, auth });
        if (measurement) enterMeasurementsRef.current.set(created.id, measurement);
        nodesRef.current = [...nodesRef.current, created];
        if (activate) editingIdRef.current = created.id;
        editRevisionRef.current += 1;
        if (!nativeAdvance) tapFocusRef.current = { nodeId: created.id, rootId: rootNodeId };
        setNodes(nodesRef.current);
        if (activate) {
          if (nativeAdvance) setNativeHeight(30);
          setSelectedId(created.id);
          setEditingId(created.id);
          setPendingFocusId(created.id);
        }
        if (nativeAdvance && nativeInputRef.current?.isFocused()) measurement?.mark("native-focus");
        notifyPending();
        // Capture this root's closures. Navigation/background/unmount can
        // drain immediately; input never waits for the quiet period or SQL.
        const save = () => {
          void flushDraft(nodeId).catch(() => undefined);
          void ensureCreated(created.id).catch(() => undefined);
        };
        if (nativeAdvance && activate) queueNativeSave(`create:${created.id}`, save);
        else save();
        return created.id;
      },
      [cancelPendingFocus, ensureCreated, flushDraft, notifyPending, onEnterMeasurement, queueNativeSave, readOnly, rootNodeId],
    );

    const focusFirstOrCreate = useCallback(async () => {
      if (readOnly || readOnlyRef.current) return;
      const actionRootId = rootNodeId;
      try {
        cancelPendingFocus();
        const focusGeneration = focusGenerationRef.current;
        const editRevision = editRevisionRef.current;
        const latest = await docsRepo.listOutline(actionRootId, true);
        if (readOnlyRef.current || activeRootIdRef.current !== actionRootId
          || focusGeneration !== focusGenerationRef.current
          || editRevision !== editRevisionRef.current) return;
        const latestIds = new Set(latest.map((node) => node.id));
        const pendingIds = nodesRef.current.filter((node) => draftsRef.current.has(node.id)
          || savingTitlesRef.current.has(node.id) || pendingCreatesRef.current.has(node.id)
          || (isDocBlockNode(node) && blockDrafts.get(node).pending)).map((node) => node.id);
        const protectedIds = expandProtectedDocsNodeAncestors(
          nodesRef.current.map((node) => ({ id: node.id, parentId: node.parent_id })), pendingIds,
        );
        const entries = [...pendingCreatesRef.current.values()].filter((entry) => entry.rootId === actionRootId);
        const retained = nodesRef.current.filter((node) => !latestIds.has(node.id) && protectedIds.has(node.id));
        for (const entry of entries) {
          if (!latestIds.has(entry.node.id) && !retained.some((row) => row.id === entry.node.id)) retained.push(entry.node);
        }
        const merged = placePendingOutlineRows([...latest, ...retained].map((node) => {
          const title = draftsRef.current.get(node.id) ?? savingTitlesRef.current.get(node.id);
          return title === undefined ? node : { ...node, title };
        }), entries);
        setNodes(merged);
        const first = merged
          .filter((node) => node.parent_id === actionRootId && !node.archived_at)
          .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))[0];
        if (first) {
          if (focusGeneration === focusGenerationRef.current) {
            // Explicit resume also owns focus when the retained native editor
            // is already bound to this paragraph but has since blurred.
            tapFocusRef.current = { nodeId: first.id, rootId: actionRootId };
            setPendingFocusId(first.id);
          }
          return;
        }
        const root = await docsRepo.getNode(actionRootId);
        if (readOnlyRef.current || activeRootIdRef.current !== actionRootId
          || focusGeneration !== focusGenerationRef.current) return;
        const created = await docsRepo.createNode({
          parentId: actionRootId,
          projectId: root?.project_id ?? null,
          title: "",
        });
        if (!mountedRef.current || activeRootIdRef.current !== actionRootId) return;
        editRevisionRef.current += 1;
        setNodes((current) => [...current, created]);
        if (focusGeneration === focusGenerationRef.current) setPendingFocusId(created.id);
        onChanged?.();
      } catch (error) {
        if (mountedRef.current && activeRootIdRef.current === actionRootId) {
          Alert.alert("Docsを作成できません", error instanceof Error ? error.message : "保存先を同期してから再度お試しください");
        }
      }
    }, [blockDrafts, cancelPendingFocus, onChanged, readOnly, rootNodeId]);

    const flushPending = useCallback(async () => {
      drainNativeSaves();
      do {
        await blockDrafts.flush();
        const pendingNodeIds = new Set([
          ...pendingCreatesRef.current.keys(),
          ...draftsRef.current.keys(),
          ...saveChainsRef.current.keys(),
        ]);
        await Promise.all(
          [...pendingNodeIds].map((nodeId) => waitForNodeSave(nodeId)),
        );
      } while (blockDrafts.pending || draftsRef.current.size > 0 || savingTitlesRef.current.size > 0
        || pendingCreatesRef.current.size > 0);
    }, [blockDrafts, drainNativeSaves, waitForNodeSave]);

    const reloadFromRepository = useCallback(async () => {
      await flushPending();
      await load();
    }, [flushPending, load]);

    useImperativeHandle(
      ref,
      () => ({
        focusFirstOrCreate, reloadFromRepository, flushPending,
        hasPendingEdits: () => blockDrafts.pending || draftsRef.current.size > 0 || savingTitlesRef.current.size > 0
          || pendingCreatesRef.current.size > 0,
      }),
      [blockDrafts, flushPending, focusFirstOrCreate, reloadFromRepository],
    );

    const runStructureChange = useCallback(
      async (action: "indent" | "outdent", nodeId: string) => {
        if (readOnly || readOnlyRef.current) return;
        const actionRootId = rootNodeId;
        cancelPendingFocus();
        const focusGeneration = focusGenerationRef.current;
        await flushDraft(nodeId);
        if (readOnlyRef.current || activeRootIdRef.current !== actionRootId) return;
        const target = nodesRef.current.find((node) => node.id === nodeId);
        if (target && isProtectedProjectNode(target)) return;
        let indentParentId: string | null = null;
        if (action === "indent") {
          await docsRepo.indentNode(nodeId);
          indentParentId = (await docsRepo.getNode(nodeId))?.parent_id ?? null;
        } else {
          await docsRepo.outdentNode(nodeId);
        }
        if (activeRootIdRef.current !== actionRootId) return;
        if (indentParentId) {
          setExpandedIds((current) => new Set(current).add(indentParentId));
        }
        await load();
        if (focusGeneration === focusGenerationRef.current) {
          tapFocusRef.current = { nodeId, rootId: actionRootId };
          setPendingFocusId(nodeId);
        }
        onChanged?.();
      },
      [cancelPendingFocus, flushDraft, load, onChanged, readOnly, rootNodeId],
    );

    const handleTitleChange = useCallback((nodeId: string, title: string, capturedNative = false) => {
      if (capturedNative && nodesRef.current.find((node) => node.id === nodeId)?.title === title) return;
      if (!capturedNative || title !== "") enterMeasurementsRef.current.get(nodeId)?.mark("first-change");
      nodesRef.current = nodesRef.current.map((node) => node.id === nodeId ? { ...node, title } : node);
      const pending = pendingCreatesRef.current.get(nodeId);
      if (pending) pending.node = { ...pending.node, title };
      setNodes((current) => current.map((node) => node.id === nodeId ? { ...node, title } : node));
      scheduleSave(nodeId, title, capturedNative);
    }, [scheduleSave]);

    const nativeCallbacksRef = useRef({
      change: handleTitleChange,
      submit: addSibling,
      activate: (_nodeId: string, _value: NativeOutlineValue) => undefined,
    });
    nativeCallbacksRef.current = {
      change: handleTitleChange,
      submit: addSibling,
      activate(nodeId, value) {
        setNativeValue(value);
        if (editingIdRef.current) {
          if (editingIdRef.current !== nodeId) setNativeHeight(30);
          editingIdRef.current = nodeId;
          setEditingId(nodeId);
          setSelectedId(nodeId);
        }
      },
    };
    if (!nativeSessionRef.current) {
      nativeSessionRef.current = new OutlineNativeSession({
        change(nodeId, text, value) {
          const scope = nativeScopesRef.current.get(value.originEpoch ?? value.epoch);
          if (!scope || scope.rootId !== activeRootIdRef.current) return;
          if (!draftsRef.current.has(nodeId) && !savingTitlesRef.current.has(nodeId)) {
            draftAuthRef.current.set(nodeId, scope);
          }
          nativeCallbacksRef.current.change(nodeId, text, true);
        },
        submit(nodeId, _text, active, event) {
          const scope = nativeScopesRef.current.get(event.epoch);
          if (!scope || scope.rootId !== activeRootIdRef.current) return null;
          // The accepted source change has already been applied. Re-delivered
          // old-epoch submits can create their row without replacing newer text.
          return nativeCallbacksRef.current.submit(nodeId, undefined, undefined, true,
            active && editingIdRef.current !== null, scope);
        },
        activate(nodeId, value) { nativeCallbacksRef.current.activate(nodeId, value); },
      });
    }
    useEffect(() => {
      if (nativeInputEnabled) positionNativeInput();
    }, [editingId, nativeHeight, nativeInputEnabled, nativeValue.epoch, positionNativeInput, rows]);
    useEffect(() => {
      setNativeRect((current) => current?.nodeId === editingId ? current : null);
    }, [editingId]);
    useEffect(() => {
      const input = nativeInputRef.current;
      const tap = tapFocusRef.current;
      if (!nativeInputEnabled || !input || !tap || nativeRect?.nodeId !== tap.nodeId
        || editingId !== tap.nodeId || readOnly
        // An explicit resume may bind a fresh epoch in an earlier effect of
        // this commit. Wait for that value to reach the adapter before focus.
        || nativeValue.epoch !== nativeSessionRef.current?.currentValue?.epoch) return;
      tapFocusRef.current = null;
      tapMeasurementRef.current?.measurement.mounted();
      requestInputFocus(tap.nodeId, input);
      if (input.isFocused()) {
        tapMeasurementRef.current?.measurement.focused();
        tapMeasurementRef.current = null;
      }
    }, [editingId, nativeInputEnabled, nativeRect, nativeValue.epoch, readOnly, requestInputFocus]);

    const handleInputBlur = useCallback((nodeId: string, input: OutlineInput | null) => {
      if (!input || inputRefs.current.get(nodeId) !== input) return;
      if (keyboardFocusRef.current?.nodeId === nodeId) {
        keyboardFocusRef.current.cancelKeyboard?.();
        keyboardFocusRef.current = null;
      }
      void flushDraft(nodeId).catch(() => undefined);
      setEditingId((current) => current === nodeId ? null : current);
    }, [flushDraft]);

    useEffect(() => {
      if (!nativeInputEnabled || !editingId || loading || rows.some((row) => row.node.id === editingId)) return;
      // A collapsed/removed row no longer owns a visible editing surface.
      // The retained input needs the focus cleanup that row unmount used to do.
      cancelPendingFocus();
      nativeInputRef.current?.blur();
      void flushDraft(editingId).catch(() => undefined);
      editingIdRef.current = null;
      setEditingId(null);
    }, [cancelPendingFocus, editingId, flushDraft, loading, nativeInputEnabled, rows]);

    const beginBlockEditing = useCallback((nodeId: string) => {
      cancelPendingFocus();
      const previous = editingIdRef.current;
      if (previous) void flushDraft(previous).catch(() => undefined);
      void blockDrafts.flush().catch(() => undefined);
      setEditingId(null);
      setSelectedId(nodeId);
    }, [blockDrafts, cancelPendingFocus, flushDraft]);

    const handleBlockSaved = useCallback((updated: DocsNode) => {
      editRevisionRef.current += 1;
      setNodes((current) => current.map((node) => node.id === updated.id ? updated : node));
      onChanged?.();
    }, [onChanged]);

    const showMore = useCallback((nodeId: string) => {
      cancelPendingFocus();
      setSelectedId(nodeId);
      setMoreTargetId(nodeId);
    }, [cancelPendingFocus]);

    const selectedRow = rows.find((row) => row.node.id === selectedId) ?? null;
    const moreTargetRow =
      rows.find((row) => row.node.id === moreTargetId) ?? null;

    const nativeEditor = nativeInputEnabled ? (
      <View
        key="retained-outline-editor"
        testID="outline-native-input-frame"
        collapsable={false}
        pointerEvents={editingId && !readOnly && nativeRect?.nodeId === editingId ? "box-none" : "none"}
        style={{ position: "absolute", left: nativeRect?.x ?? 0, top: nativeRect?.y ?? 0,
          width: nativeRect?.width ?? 1, height: nativeHeight,
          opacity: editingId && !readOnly && nativeRect?.nodeId === editingId ? 1 : 0 }}
      >
        <OutlineNativeInput
          ref={nativeInputRef}
          testID="outline-native-input"
          value={nativeValue}
          scope={nativeCacheScope}
          active={Boolean(editingId && !readOnly)}
          accessibilityHint="改行で同じ階層に新しい行を作成"
          style={StyleSheet.absoluteFill}
          onChangeValue={(value) => nativeSessionRef.current?.change(value)}
          onSubmitValue={(value) => nativeSessionRef.current?.submit(value)}
          onFocusValue={(value) => {
            if (value.epoch !== nativeSessionRef.current?.currentValue?.epoch) return;
            const nodeId = nativeSessionRef.current.currentNodeId;
            if (!nodeId) return;
            if (value.focused) {
              const request = keyboardFocusRef.current;
              if (request && !request.cancelKeyboard && request.input === nativeInputRef.current) {
                request.cancelKeyboard = showOutlineInputKeyboard(request.input);
              }
              tapMeasurementRef.current?.measurement.focused();
              tapMeasurementRef.current = null;
            } else {
              drainNativeSaves();
              void flushDraft(nodeId).catch(() => undefined);
              editingIdRef.current = null;
              setEditingId(null);
            }
          }}
          onSizeValue={(value) => {
            const current = nativeSessionRef.current?.currentValue;
            if (value.epoch === current?.epoch && value.generation === current.generation
              && Number.isFinite(value.height)) setNativeHeight(Math.max(30, value.height));
          }}
        />
      </View>
    ) : null;

    return (
      <View ref={containerRef} style={styles.container} onLayout={() => {
        const container = containerRef.current;
        const rootId = activeRootIdRef.current;
        container?.measureInWindow((_x, y, _width, height) => {
          if (containerRef.current !== container || rootId !== activeRootIdRef.current || height <= 0) return;
          containerFrameRef.current = { y, height };
          keyboardScrollRef.current();
        });
        positionNativeInput();
      }}>
        {Object.keys(saveErrors).length > 0 ? (
          <View>
            <Text accessibilityRole="alert" style={styles.saveError}>
              {Object.values(saveErrors).join("\n")}
            </Text>
            <Button compact disabled={readOnly} onPress={() => void flushPending().catch(() => undefined)}>
              本文の保存を再試行
            </Button>
          </View>
        ) : null}
        <FlatList
          testID="outline-list"
          ref={listRef}
          data={rows}
          keyExtractor={(row) => row.node.id}
          keyboardShouldPersistTaps="always"
          keyboardDismissMode="on-drag"
          removeClippedSubviews={nativeInputEnabled ? false : undefined}
          renderScrollComponent={nativeInputEnabled ? (scrollProps) => (
            <NativeOutlineScrollView {...scrollProps}
              innerViewRef={scrollContentRef as React.RefObject<View>}
              editor={nativeEditor}
            />
          ) : undefined}
          onContentSizeChange={nativeInputEnabled ? positionNativeInput : undefined}
          onLayout={(event) => {
            const { y, height } = event.nativeEvent.layout;
            listFrameRef.current = { y, height };
            positionNativeInput();
          }}
          onScrollBeginDrag={() => {
            cancelPendingFocus();
            if (nativeInputEnabled && editingIdRef.current) nativeInputRef.current?.blur();
            else Keyboard.dismiss();
          }}
          contentContainerStyle={styles.listContent}
          ListHeaderComponent={listHeader}
          onScrollToIndexFailed={handleScrollToIndexFailed}
          ListEmptyComponent={
            loading ? (
              <View style={styles.empty}>
                <Text style={styles.loadingText}>読み込み中...</Text>
              </View>
            ) : (
              <Pressable
                style={styles.empty}
                disabled={readOnly}
                onPress={() => void focusFirstOrCreate()}
              >
                <Text style={styles.emptyText}>タップして本文を書き始める</Text>
              </Pressable>
            )
          }
          renderItem={({ item: row }) => (
            <OutlineRowView
              node={row.node}
              depth={row.depth}
              hasChildren={row.hasChildren}
              selected={selectedId === row.node.id}
              editing={editingId === row.node.id}
              nativeEditing={nativeInputEnabled && editingId === row.node.id}
              nativeHeight={nativeHeight}
              registerRowAnchor={registerRowAnchor}
              onRowLayout={handleRowLayout}
              expanded={expandedIds.has(row.node.id)}
              readOnly={readOnly}
              cancelPendingFocus={cancelPendingFocus}
              toggleExpanded={toggleExpanded}
              showMore={showMore}
              requestOpenNode={requestOpenNode}
              beginEditing={beginEditing}
              registerInput={registerInput}
              onTitleChange={handleTitleChange}
              onInputFocus={handleInputFocus}
              onInputLayout={handleInputLayout}
              onInputBlur={handleInputBlur}
              addSibling={addSibling}
              onBlockSaved={handleBlockSaved}
              blockDraft={isDocBlockNode(row.node) ? blockDrafts.get(row.node) : undefined}
              beginBlockEditing={beginBlockEditing}
            />
          )}
        />

        {keyboardVisible && selectedRow ? (
          <Surface style={styles.toolbar} elevation={3}>
            <IconButton
              accessibilityLabel="字上げ"
              accessibilityHint="この行を親と同じ階層へ戻す"
              style={styles.toolbarAction}
              icon="format-indent-decrease"
                disabled={readOnly || selectedRow.depth === 0 || isProtectedProjectNode(selectedRow.node)}
              onPress={() => {
                cancelPendingFocus();
                void runStructureChange("outdent", selectedRow.node.id);
              }}
            />
            <IconButton
              accessibilityLabel="字下げ"
              accessibilityHint="この行を直前の行の子にする"
              style={styles.toolbarAction}
              icon="format-indent-increase"
                disabled={readOnly || selectedRow.siblingIndex === 0 || isProtectedProjectNode(selectedRow.node)}
              onPress={() => {
                cancelPendingFocus();
                void runStructureChange("indent", selectedRow.node.id);
              }}
            />
            <IconButton
              accessibilityLabel="編集操作"
              style={styles.toolbarAction}
              icon="dots-horizontal"
              size={20}
              onPress={() => {
                cancelPendingFocus();
                setMoreTargetId(selectedRow.node.id);
              }}
              disabled={readOnly}
            />
          </Surface>
        ) : null}

        <Portal>
          <Dialog
            visible={moreTargetId !== null}
            onDismiss={() => setMoreTargetId(null)}
            style={styles.dialog}
          >
            <Dialog.Title style={styles.dialogTitle}>ノード操作</Dialog.Title>
            <Dialog.Content>
              <Text style={styles.editingHelp}>改行で新しい行。字下げで直前の行の子に、字上げで階層を戻します。</Text>
              <Button
                icon="open-in-new"
                onPress={() => {
                  const target = moreTargetId;
                  setMoreTargetId(null);
                  if (target) requestOpenNode(target);
                }}
              >
                ノードを開く
              </Button>
              <Button
                icon="subdirectory-arrow-right"
                disabled={readOnly}
                onPress={() => {
                  const target = moreTargetId;
                  setMoreTargetId(null);
                  if (target) void addChild(target);
                }}
              >
                子ノードを追加
              </Button>
              <Button
                icon="format-indent-increase"
                disabled={readOnly || !moreTargetRow || moreTargetRow.siblingIndex === 0}
                onPress={() => {
                  const target = moreTargetId;
                  setMoreTargetId(null);
                  if (target) void runStructureChange("indent", target);
                }}
              >
                字下げ
              </Button>
              <Button
                icon="format-indent-decrease"
                disabled={readOnly || !moreTargetRow || moreTargetRow.depth === 0}
                onPress={() => {
                  const target = moreTargetId;
                  setMoreTargetId(null);
                  if (target) void runStructureChange("outdent", target);
                }}
              >
                字上げ
              </Button>
              <Button
                icon="folder-move-outline"
                disabled={readOnly || Boolean(moreTargetRow && isProtectedProjectNode(moreTargetRow.node))}
                onPress={() => {
                  const target = moreTargetId;
                  setMoreTargetId(null);
                  if (target && !readOnly) {
                    void waitForNodeSave(target).then(() => {
                      if (!readOnlyRef.current) onMoveRequest(target);
                    }).catch(() => undefined);
                  }
                }}
              >
                移動
              </Button>
              <Button
                icon="archive-outline"
                textColor="#f38ba8"
                disabled={readOnly || Boolean(moreTargetRow && isProtectedProjectNode(moreTargetRow.node))}
                onPress={() => {
                  const target = moreTargetId;
                  setMoreTargetId(null);
                  if (!target || readOnly) return;
                  const actionRootId = rootNodeId;
                  void flushDraft(target).then(async () => {
                    if (readOnlyRef.current || activeRootIdRef.current !== actionRootId) return;
                    await docsRepo.archiveNode(target);
                    setNodes((current) =>
                      current.map((node) =>
                        node.id === target
                          ? { ...node, archived_at: new Date().toISOString() }
                          : node,
                      ),
                    );
                    onChanged?.();
                  }).catch((error) => {
                    Alert.alert("Docs", error instanceof Error ? error.message : "ノードのアーカイブに失敗しました");
                  });
                }}
              >
                アーカイブ
              </Button>
            </Dialog.Content>
          </Dialog>
        </Portal>
      </View>
    );
  },
);

type RowProps = {
  node: DocsNode;
  depth: number;
  hasChildren: boolean;
  selected: boolean;
  editing: boolean;
  nativeEditing: boolean;
  nativeHeight: number;
  registerRowAnchor: (nodeId: string, anchor: View | null) => void;
  onRowLayout: (nodeId: string) => void;
  expanded: boolean;
  readOnly: boolean;
  cancelPendingFocus: () => void;
  toggleExpanded: (nodeId: string) => void;
  showMore: (nodeId: string) => void;
  requestOpenNode: (nodeId: string) => void;
  beginEditing: (nodeId: string) => void;
  registerInput: (nodeId: string, input: OutlineInput | null) => void;
  onTitleChange: (nodeId: string, title: string) => void;
  onInputFocus: (nodeId: string, input: OutlineInput | null) => void;
  onInputLayout: (nodeId: string, input: OutlineInput | null) => void;
  onInputBlur: (nodeId: string, input: OutlineInput | null) => void;
  addSibling: (nodeId: string, submittedTitle?: string, input?: OutlineInput | null) => string | null;
  onBlockSaved: (node: DocsNode) => void;
  blockDraft?: DocBlockDraft;
  beginBlockEditing: (nodeId: string) => void;
};

const OutlineRowView = memo(function OutlineRowView({
  node, depth, hasChildren, selected, editing, nativeEditing, nativeHeight, registerRowAnchor, onRowLayout, expanded, readOnly,
  cancelPendingFocus, toggleExpanded, showMore, requestOpenNode, beginEditing,
  registerInput, onTitleChange, onInputFocus, onInputLayout, onInputBlur, addSibling, onBlockSaved, blockDraft, beginBlockEditing,
}: RowProps) {
  const protectedNode = isProtectedProjectNode(node);
  const displayTitle = node.title || (node.is_explicit_blank ? " " : "メモ");
  return (
    <View
      testID={`outline-row-${node.id}`}
      style={[
        styles.row,
        { paddingLeft: Math.min(depth, 8) * 14 },
        selected ? styles.rowSelected : null,
      ]}
    >
      <Pressable
        testID={`outline-gutter-${node.id}`}
        style={styles.gutter}
        accessibilityRole={hasChildren ? "button" : undefined}
        accessibilityLabel={
          hasChildren
            ? `${expanded ? "格納" : "展開"}: ${node.title || "空のノード"}`
            : undefined
        }
        onPressIn={cancelPendingFocus}
        onPress={
          hasChildren
            ? () => toggleExpanded(node.id)
            : undefined
        }
        onLongPress={() => {
          cancelPendingFocus();
          showMore(node.id);
        }}
        delayLongPress={300}
      >
        {hasChildren ? (
          <View
            testID={`outline-chevron-${node.id}`}
            pointerEvents="none"
            style={styles.chevron}
          >
            <Icon
              source={expanded ? "chevron-down" : "chevron-right"}
              size={17}
              color="#a6adc8"
            />
          </View>
        ) : (
          <View style={styles.bullet} />
        )}
      </Pressable>
      <View ref={(view) => registerRowAnchor(node.id, view)} onLayout={() => onRowLayout(node.id)}
        collapsable={false} style={styles.rowContent}>
      {isDocBlockNode(node) ? null : nativeEditing && !protectedNode && !readOnly ? (
        <View style={{ minHeight: nativeHeight }} />
      ) : editing && !protectedNode && !readOnly ? (
        <OutlineRowInput
          node={node}
          registerInput={registerInput}
          onTitleChange={onTitleChange}
          onInputFocus={onInputFocus}
          onInputLayout={onInputLayout}
          onInputBlur={onInputBlur}
          addSibling={addSibling}
        />
      ) : (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`編集: ${node.title || "空のノード"}`}
          style={styles.displayTitle}
          onPress={() => protectedNode
            ? requestOpenNode(node.id)
            : beginEditing(node.id)}
        >
          <Text
            style={
              node.title && !node.is_explicit_blank
                ? styles.displayTitleText
                : styles.displayPlaceholder
            }
          >
            {displayTitle}
          </Text>
        </Pressable>
      )}
      {isDocBlockNode(node) ? (
        <DocBlockEditor
          node={node}
          testIdPrefix="docs-block"
          readOnly={readOnly || protectedNode}
          onSaved={onBlockSaved}
          draftSession={blockDraft}
          onEdit={() => beginBlockEditing(node.id)}
        />
      ) : null}
      </View>
      <IconButton
        testID={`outline-actions-${node.id}`}
        accessibilityLabel={`ノード操作: ${node.title || "空のノード"}`}
        icon="dots-vertical"
        size={19}
        iconColor="#a6adc8"
        style={styles.rowActions}
        hitSlop={0}
        onPress={() => {
          cancelPendingFocus();
          showMore(node.id);
        }}
        disabled={readOnly || protectedNode}
      />
    </View>

  );
});


// Give each mounted native editor its own ref lifetime. A delayed layout from
// a removed editor must not focus a replacement, including the same row ID.
function OutlineRowInput({
  node, registerInput, onTitleChange, onInputFocus, onInputLayout, onInputBlur, addSibling,
}: Pick<RowProps, "node" | "registerInput" | "onTitleChange" | "onInputFocus"
  | "onInputLayout" | "onInputBlur" | "addSibling">) {
  const nativeInputRef = useRef<OutlineInput | null>(null);
  const inputRef = useCallback((input: OutlineInput | null) => {
    nativeInputRef.current = input;
    registerInput(node.id, input);
  }, [node.id, registerInput]);
  return (
    <TextInput
      ref={inputRef}
      onLayout={() => onInputLayout(node.id, nativeInputRef.current)}
      value={node.title}
      onChangeText={(title) => onTitleChange(node.id, title)}
      onFocus={() => onInputFocus(node.id, nativeInputRef.current)}
      onBlur={() => onInputBlur(node.id, nativeInputRef.current)}
      onSubmitEditing={(event) => void addSibling(node.id, event?.nativeEvent?.text, nativeInputRef.current)}
      accessibilityHint="改行で同じ階層に新しい行を作成"
      submitBehavior="submit"
      blurOnSubmit={false}
      multiline
      scrollEnabled={false}
      rejectResponderTermination={false}
      maxLength={500}
      placeholder="メモ"
      placeholderTextColor="#6c7086"
      selectionColor="#89b4fa"
      style={styles.input}
      underlineColorAndroid="transparent"
    />
  );
}


const styles = StyleSheet.create({
  container: { flex: 1, minHeight: 0 },
  saveError: { color: "#f38ba8", fontSize: 12, paddingHorizontal: 14, paddingTop: 4 },
  listContent: { paddingHorizontal: 10, paddingTop: 4, paddingBottom: 90 },
  empty: { padding: 28, alignItems: "center" },
  emptyText: { color: "#89b4fa", fontSize: 14 },
  loadingText: { color: "#a6adc8", fontSize: 13 },
  row: { flexDirection: "row", alignItems: "flex-start", borderRadius: 8 },
  rowSelected: { backgroundColor: "#181825" },
  rowContent: { flex: 1, minWidth: 0 },
  // Keep row controls inside their own line; expanded hitSlop steals taps from
  // adjacent compact rows. The text area remains the main editing target.
  gutter: { width: 26, height: 30, alignItems: "center", justifyContent: "center" },
  chevron: { width: 24, height: 24, alignItems: "center", justifyContent: "center" },
  bullet: { width: 5, height: 5, borderRadius: 3, backgroundColor: "#7c3aed" },
  input: {
    minHeight: 30,
    backgroundColor: "transparent",
    color: "#cdd6f4",
    fontSize: 15,
    lineHeight: 21,
    paddingHorizontal: 4,
    paddingVertical: 3,
    includeFontPadding: false,
    textAlignVertical: "top",
  },
  displayTitle: {
    flex: 1,
    minHeight: 30,
    justifyContent: "center",
    paddingHorizontal: 4,
    paddingVertical: 3,
  },
  displayTitleText: { color: "#cdd6f4", fontSize: 15, lineHeight: 21, includeFontPadding: false },
  displayPlaceholder: { color: "#6c7086", fontSize: 15, lineHeight: 21, includeFontPadding: false },
  rowActions: { width: 30, height: 30, margin: 0 },
  toolbar: {
    marginHorizontal: 8,
    marginBottom: 6,
    minHeight: 48,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-around",
    borderRadius: 12,
    backgroundColor: "#313244",
  },
  toolbarAction: { width: 48, height: 48, margin: 0 },
  editingHelp: { color: "#a6adc8", fontSize: 13, marginBottom: 8 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
});

import React, {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { FlatList, Keyboard, Pressable, StyleSheet, View } from "react-native";
import {
  Button,
  Dialog,
  IconButton,
  Portal,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { docsRepo } from "../../repositories/docs";
import type { DocsNode } from "../../types/api";
import { flattenVisibleOutline } from "./outline-editor-model";

export type OutlineEditorHandle = {
  focusFirstOrCreate: () => Promise<void>;
};

type Props = {
  rootNodeId: string;
  showArchived: boolean;
  reloadToken: number;
  onOpen: (nodeId: string) => void;
  onMoveRequest: (nodeId: string) => void;
  onChanged?: () => void;
  listHeader?: React.ReactElement;
};

type PaperInput = { focus: () => void };

const TITLE_DOUBLE_TAP_MS = 300;

type TitleTap = { nodeId: string; at: number };
type ActiveTitlePress = {
  nodeId: string;
  startedAt: number;
  isSecondTap: boolean;
  cancelled: boolean;
};

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
    },
    ref,
  ) {
    const [nodes, setNodes] = useState<DocsNode[]>([]);
    const [loading, setLoading] = useState(true);
    const [collapsedIds, setCollapsedIds] = useState<Set<string>>(new Set());
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [pendingFocusId, setPendingFocusId] = useState<string | null>(null);
    const [keyboardVisible, setKeyboardVisible] = useState(false);
    const [moreTargetId, setMoreTargetId] = useState<string | null>(null);
    const listRef = useRef<FlatList>(null);
    const inputRefs = useRef(new Map<string, PaperInput>());
    const draftsRef = useRef(new Map<string, string>());
    const saveTimersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());
    const saveChainsRef = useRef(new Map<string, Promise<void>>());
    const siblingCreatesRef = useRef(new Set<string>());
    const openingIdsRef = useRef(new Set<string>());
    const lastTitleTapRef = useRef<TitleTap | null>(null);
    const titleTapTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const activeTitlePressRef = useRef<ActiveTitlePress | null>(null);
    const suppressTitleTapUntilRef = useRef(0);
    const loadRequestRef = useRef(0);

    const load = useCallback(async () => {
      const requestId = ++loadRequestRef.current;
      try {
        const nextNodes = await docsRepo.listOutline(rootNodeId, true);
        if (requestId === loadRequestRef.current) setNodes(nextNodes);
      } catch {
        if (requestId === loadRequestRef.current) setNodes([]);
      } finally {
        if (requestId === loadRequestRef.current) setLoading(false);
      }
    }, [rootNodeId]);

    useEffect(() => {
      loadRequestRef.current += 1;
      setNodes([]);
      setLoading(true);
    }, [rootNodeId]);

    useEffect(() => {
      void load();
    }, [load, reloadToken]);

    useEffect(() => {
      const show = Keyboard.addListener("keyboardDidShow", () => setKeyboardVisible(true));
      const hide = Keyboard.addListener("keyboardDidHide", () => setKeyboardVisible(false));
      return () => {
        show.remove();
        hide.remove();
      };
    }, []);

    const rows = useMemo(
      () => flattenVisibleOutline(nodes, rootNodeId, collapsedIds, showArchived),
      [collapsedIds, nodes, rootNodeId, showArchived],
    );

    const beginEditing = useCallback((nodeId: string) => {
      setSelectedId(nodeId);
      setEditingId(nodeId);
      requestAnimationFrame(() => inputRefs.current.get(nodeId)?.focus());
    }, []);

    const clearTitleTapCandidate = useCallback(() => {
      if (titleTapTimerRef.current) clearTimeout(titleTapTimerRef.current);
      titleTapTimerRef.current = null;
      lastTitleTapRef.current = null;
    }, []);

    const cancelTitleTap = useCallback(() => {
      clearTitleTapCandidate();
      if (activeTitlePressRef.current) {
        activeTitlePressRef.current.cancelled = true;
      }
    }, [clearTitleTapCandidate]);

    const rememberTitleTap = useCallback(
      (nodeId: string, at = Date.now()) => {
        clearTitleTapCandidate();
        lastTitleTapRef.current = { nodeId, at };
        titleTapTimerRef.current = setTimeout(() => {
          const current = lastTitleTapRef.current;
          if (current?.nodeId === nodeId && current.at === at) {
            lastTitleTapRef.current = null;
          }
          titleTapTimerRef.current = null;
        }, TITLE_DOUBLE_TAP_MS);
      },
      [clearTitleTapCandidate],
    );

    const flushDraft = useCallback((nodeId: string) => {
      const timer = saveTimersRef.current.get(nodeId);
      if (timer) clearTimeout(timer);
      saveTimersRef.current.delete(nodeId);
      const draft = draftsRef.current.get(nodeId);
      if (draft === undefined) {
        return saveChainsRef.current.get(nodeId) ?? Promise.resolve();
      }
      draftsRef.current.delete(nodeId);
      const normalized = draft.replace(/[\r\n]+/g, " ").slice(0, 500);
      const previous = saveChainsRef.current.get(nodeId) ?? Promise.resolve();
      const next = previous
        .catch(() => undefined)
        .then(async () => {
          await docsRepo.updateNode(nodeId, { title: normalized });
        });
      saveChainsRef.current.set(nodeId, next);
      return next;
    }, []);

    const waitForNodeSave = useCallback(
      async (nodeId: string) => {
        await flushDraft(nodeId);
        while (true) {
          const pending = saveChainsRef.current.get(nodeId);
          if (!pending) return;
          await pending;
          if (saveChainsRef.current.get(nodeId) === pending) return;
        }
      },
      [flushDraft],
    );

    const openNode = useCallback(
      async (nodeId: string) => {
        if (openingIdsRef.current.has(nodeId)) return;
        openingIdsRef.current.add(nodeId);
        try {
          await waitForNodeSave(nodeId);
          onOpen(nodeId);
        } finally {
          openingIdsRef.current.delete(nodeId);
        }
      },
      [onOpen, waitForNodeSave],
    );

    const requestOpenNode = useCallback(
      (nodeId: string) => {
        void openNode(nodeId).catch(() => undefined);
      },
      [openNode],
    );

    const recordDisplayTitleTap = useCallback(
      (nodeId: string) => {
        const now = Date.now();
        if (now < suppressTitleTapUntilRef.current) return;
        const previous = lastTitleTapRef.current;
        if (
          previous?.nodeId === nodeId &&
          now - previous.at <= TITLE_DOUBLE_TAP_MS
        ) {
          clearTitleTapCandidate();
          suppressTitleTapUntilRef.current = now + TITLE_DOUBLE_TAP_MS;
          requestOpenNode(nodeId);
          return;
        }
        rememberTitleTap(nodeId, now);
      },
      [clearTitleTapCandidate, rememberTitleTap, requestOpenNode],
    );

    const handleEditingTitlePressIn = useCallback((nodeId: string) => {
      const now = Date.now();
      const previous = lastTitleTapRef.current;
      activeTitlePressRef.current = {
        nodeId,
        startedAt: now,
        isSecondTap:
          now >= suppressTitleTapUntilRef.current &&
          previous?.nodeId === nodeId &&
          now - previous.at <= TITLE_DOUBLE_TAP_MS,
        cancelled:
          now < suppressTitleTapUntilRef.current ||
          openingIdsRef.current.has(nodeId),
      };
    }, []);

    const handleEditingTitlePressOut = useCallback(
      (nodeId: string) => {
        const press = activeTitlePressRef.current;
        activeTitlePressRef.current = null;
        if (!press || press.nodeId !== nodeId || press.cancelled) return;
        const now = Date.now();
        if (now - press.startedAt > TITLE_DOUBLE_TAP_MS) {
          clearTitleTapCandidate();
          return;
        }
        if (press.isSecondTap) {
          clearTitleTapCandidate();
          suppressTitleTapUntilRef.current = now + TITLE_DOUBLE_TAP_MS;
          requestOpenNode(nodeId);
          return;
        }
        rememberTitleTap(nodeId, now);
      },
      [clearTitleTapCandidate, rememberTitleTap, requestOpenNode],
    );

    const scheduleSave = useCallback(
      (nodeId: string, title: string) => {
        draftsRef.current.set(nodeId, title);
        const current = saveTimersRef.current.get(nodeId);
        if (current) clearTimeout(current);
        saveTimersRef.current.set(
          nodeId,
          setTimeout(() => void flushDraft(nodeId), 450),
        );
      },
      [flushDraft],
    );

    useEffect(
      () => () => {
        for (const timer of saveTimersRef.current.values()) clearTimeout(timer);
        for (const nodeId of draftsRef.current.keys()) void flushDraft(nodeId);
        clearTitleTapCandidate();
        activeTitlePressRef.current = null;
      },
      [clearTitleTapCandidate, flushDraft],
    );

    useEffect(() => {
      if (!pendingFocusId) return;
      const index = rows.findIndex((row) => row.node.id === pendingFocusId);
      if (index < 0) return;
      let focusTimer: ReturnType<typeof setTimeout> | undefined;
      setEditingId(pendingFocusId);
      const frame = requestAnimationFrame(() => {
        listRef.current?.scrollToIndex({ index, animated: true, viewPosition: 0.55 });
        focusTimer = setTimeout(() => {
          const input = inputRefs.current.get(pendingFocusId);
          if (!input) return;
          input.focus();
          setSelectedId(pendingFocusId);
          setPendingFocusId(null);
        }, 80);
      });
      return () => {
        cancelAnimationFrame(frame);
        if (focusTimer) clearTimeout(focusTimer);
      };
    }, [pendingFocusId, rows]);

    const addChild = useCallback(
      async (parentId: string) => {
        await flushDraft(parentId);
        const parent = nodes.find((node) => node.id === parentId);
        const created = await docsRepo.createNode({
          parentId,
          projectId: parent?.project_id ?? null,
          title: "",
        });
        setNodes((current) => [...current, created]);
        setCollapsedIds((current) => {
          const next = new Set(current);
          next.delete(parentId);
          return next;
        });
        setPendingFocusId(created.id);
        onChanged?.();
      },
      [flushDraft, nodes, onChanged],
    );

    const addSibling = useCallback(
      async (nodeId: string) => {
        // Android IME によっては同じ確定操作で submit が重複通知されるため、
        // 元ノード単位で作成を直列化して空行の二重作成を防ぐ。
        if (siblingCreatesRef.current.has(nodeId)) return;
        siblingCreatesRef.current.add(nodeId);
        try {
          await flushDraft(nodeId);
          const created = await docsRepo.createSiblingAfter(nodeId, "");
          setNodes((current) => [...current, created]);
          setPendingFocusId(created.id);
          onChanged?.();
        } finally {
          siblingCreatesRef.current.delete(nodeId);
        }
      },
      [flushDraft, onChanged],
    );

    const focusFirstOrCreate = useCallback(async () => {
      const latest = await docsRepo.listOutline(rootNodeId, true);
      setNodes(latest);
      const first = latest
        .filter((node) => node.parent_id === rootNodeId && !node.archived_at)
        .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))[0];
      if (first) {
        setPendingFocusId(first.id);
        return;
      }
      const root = await docsRepo.getNode(rootNodeId);
      const created = await docsRepo.createNode({
        parentId: rootNodeId,
        projectId: root?.project_id ?? null,
        title: "",
      });
      setNodes([...latest, created]);
      setPendingFocusId(created.id);
      onChanged?.();
    }, [onChanged, rootNodeId]);

    useImperativeHandle(ref, () => ({ focusFirstOrCreate }), [focusFirstOrCreate]);

    const runStructureChange = useCallback(
      async (action: "indent" | "outdent", nodeId: string) => {
        await flushDraft(nodeId);
        if (action === "indent") await docsRepo.indentNode(nodeId);
        else await docsRepo.outdentNode(nodeId);
        await load();
        setPendingFocusId(nodeId);
        onChanged?.();
      },
      [flushDraft, load, onChanged],
    );

    const selectedRow = rows.find((row) => row.node.id === selectedId) ?? null;

    return (
      <View style={styles.container}>
        <FlatList
          testID="outline-list"
          ref={listRef}
          data={rows}
          keyExtractor={(row) => row.node.id}
          keyboardShouldPersistTaps="always"
          keyboardDismissMode="on-drag"
          onScrollBeginDrag={() => {
            cancelTitleTap();
            Keyboard.dismiss();
          }}
          contentContainerStyle={styles.listContent}
          ListHeaderComponent={listHeader}
          onScrollToIndexFailed={({ index }) => {
            setTimeout(() => listRef.current?.scrollToIndex({ index, animated: true }), 120);
          }}
          ListEmptyComponent={
            loading ? (
              <View style={styles.empty}>
                <Text style={styles.loadingText}>読み込み中...</Text>
              </View>
            ) : (
              <Pressable style={styles.empty} onPress={() => void focusFirstOrCreate()}>
                <Text style={styles.emptyText}>タップして本文を書き始める</Text>
              </Pressable>
            )
          }
          renderItem={({ item: row }) => (
            <View
              style={[
                styles.row,
                { paddingLeft: Math.min(row.depth, 8) * 10 },
                selectedId === row.node.id ? styles.rowSelected : null,
              ]}
            >
              <Pressable
                testID={`outline-gutter-${row.node.id}`}
                style={styles.gutter}
                onPressIn={cancelTitleTap}
                onLongPress={() => {
                  cancelTitleTap();
                  setMoreTargetId(row.node.id);
                }}
                delayLongPress={300}
              >
                {row.hasChildren ? (
                  <IconButton
                    testID={`outline-chevron-${row.node.id}`}
                    icon={collapsedIds.has(row.node.id) ? "chevron-right" : "chevron-down"}
                    size={17}
                    iconColor="#a6adc8"
                    style={styles.chevron}
                    onPress={() => {
                      cancelTitleTap();
                      setCollapsedIds((current) => {
                        const next = new Set(current);
                        if (next.has(row.node.id)) next.delete(row.node.id);
                        else next.add(row.node.id);
                        return next;
                      });
                    }}
                  />
                ) : (
                  <View style={styles.bullet} />
                )}
              </Pressable>
              {editingId === row.node.id ? (
                <TextInput
                  ref={(input: PaperInput | null) => {
                    if (input) inputRefs.current.set(row.node.id, input);
                    else inputRefs.current.delete(row.node.id);
                  }}
                  value={row.node.title}
                  onChangeText={(title) => {
                    setNodes((current) =>
                      current.map((node) =>
                        node.id === row.node.id ? { ...node, title } : node,
                      ),
                    );
                    scheduleSave(row.node.id, title);
                  }}
                  onFocus={() => setSelectedId(row.node.id)}
                  onPressIn={() => handleEditingTitlePressIn(row.node.id)}
                  onPressOut={() => handleEditingTitlePressOut(row.node.id)}
                  onBlur={() => {
                    void flushDraft(row.node.id);
                    setEditingId((current) =>
                      current === row.node.id ? null : current,
                    );
                  }}
                  onSubmitEditing={() => void addSibling(row.node.id)}
                  submitBehavior="submit"
                  blurOnSubmit={false}
                  multiline
                  scrollEnabled={false}
                  rejectResponderTermination={false}
                  maxLength={500}
                  mode="flat"
                  dense
                  placeholder="メモ"
                  style={styles.input}
                  underlineColor="transparent"
                  activeUnderlineColor="transparent"
                />
              ) : (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel={`編集: ${row.node.title || "空のノード"}`}
                  style={styles.displayTitle}
                  onPress={() => {
                    recordDisplayTitleTap(row.node.id);
                    beginEditing(row.node.id);
                  }}
                >
                  <Text
                    style={
                      row.node.title
                        ? styles.displayTitleText
                        : styles.displayPlaceholder
                    }
                  >
                    {row.node.title || "メモ"}
                  </Text>
                </Pressable>
              )}
            </View>
          )}
        />

        {keyboardVisible && selectedRow ? (
          <Surface style={styles.toolbar} elevation={3}>
            <Button
              compact
              icon="format-indent-decrease"
              disabled={selectedRow.depth === 0}
              onPress={() => {
                cancelTitleTap();
                void runStructureChange("outdent", selectedRow.node.id);
              }}
            >
              戻す
            </Button>
            <Button
              compact
              icon="format-indent-increase"
              disabled={selectedRow.siblingIndex === 0}
              onPress={() => {
                cancelTitleTap();
                void runStructureChange("indent", selectedRow.node.id);
              }}
            >
              字下げ
            </Button>
            <Button
              compact
              icon="subdirectory-arrow-right"
              onPress={() => {
                cancelTitleTap();
                void addChild(selectedRow.node.id);
              }}
            >
              子
            </Button>
            <IconButton
              icon="dots-horizontal"
              size={20}
              onPress={() => {
                cancelTitleTap();
                setMoreTargetId(selectedRow.node.id);
              }}
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
                icon="folder-move-outline"
                onPress={() => {
                  const target = moreTargetId;
                  setMoreTargetId(null);
                  if (target) onMoveRequest(target);
                }}
              >
                移動
              </Button>
              <Button
                icon="archive-outline"
                textColor="#f38ba8"
                onPress={() => {
                  const target = moreTargetId;
                  setMoreTargetId(null);
                  if (!target) return;
                  void flushDraft(target).then(async () => {
                    await docsRepo.archiveNode(target);
                    setNodes((current) =>
                      current.map((node) =>
                        node.id === target
                          ? { ...node, archived_at: new Date().toISOString() }
                          : node,
                      ),
                    );
                    onChanged?.();
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

const styles = StyleSheet.create({
  container: { flex: 1, minHeight: 0 },
  listContent: { paddingHorizontal: 10, paddingTop: 4, paddingBottom: 90 },
  empty: { padding: 28, alignItems: "center" },
  emptyText: { color: "#89b4fa", fontSize: 14 },
  loadingText: { color: "#a6adc8", fontSize: 13 },
  row: { flexDirection: "row", alignItems: "flex-start", borderRadius: 8 },
  rowSelected: { backgroundColor: "#181825" },
  gutter: { width: 30, minHeight: 44, alignItems: "center", justifyContent: "center" },
  chevron: { margin: 0 },
  bullet: { width: 5, height: 5, borderRadius: 3, backgroundColor: "#7c3aed" },
  input: {
    flex: 1,
    minHeight: 44,
    backgroundColor: "transparent",
    color: "#cdd6f4",
    fontSize: 15,
  },
  displayTitle: {
    flex: 1,
    minHeight: 44,
    justifyContent: "center",
    paddingHorizontal: 12,
    paddingVertical: 10,
  },
  displayTitleText: { color: "#cdd6f4", fontSize: 15, lineHeight: 21 },
  displayPlaceholder: { color: "#6c7086", fontSize: 15, lineHeight: 21 },
  toolbar: {
    position: "absolute",
    left: 8,
    right: 8,
    bottom: 6,
    minHeight: 48,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-around",
    borderRadius: 12,
    backgroundColor: "#313244",
  },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
});

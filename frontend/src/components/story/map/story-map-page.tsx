"use client";

import "@xyflow/react/dist/style.css";
import "./story-map-theme.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Background,
  Controls,
  MiniMap,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type OnBeforeDelete,
  type OnConnectEnd,
  type OnNodeDrag,
  type XYPosition,
} from "@xyflow/react";
import dagre from "@dagrejs/dagre";
import { GitBranch, Loader2, Search, WandSparkles } from "lucide-react";
import { toast } from "sonner";
import { useSWRConfig } from "swr";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StoryApiError, storyApi } from "@/lib/story/api";
import { objectOf, type StoryEpisodeView, type StoryLinkView } from "@/lib/story/view-model";
import { useStoryGraph } from "@/components/story/hooks/use-story-data";
import { useStoryWorkContext } from "@/components/story/shell/story-workspace-shell";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { useTheme } from "@/contexts/theme-context";
import {
  createStoryMapHighlightStore,
  StoryMapProvider,
  type StoryMapNodeActions,
} from "./story-map-context";
import { StoryMapDialog, type StoryMapDialogRequest } from "./story-map-dialog";
import {
  StoryMapNodeCard,
  STORY_MAP_NODE_HEIGHT,
  STORY_MAP_NODE_WIDTH,
  type StoryMapNode,
} from "./story-map-node";
import { StoryMapNodeMenu, type StoryMapMenuState } from "./story-map-node-menu";
import { StoryMapInspector } from "./story-map-inspector";

const nodeTypes = { story: StoryMapNodeCard };

type PositionMap = Map<string, XYPosition>;

type Rect = { x: number; y: number; width: number; height: number };

function buildLayout(
  episodes: StoryEpisodeView[],
  links: StoryLinkView[],
  direction: "LR" | "TB" = "LR",
): PositionMap {
  const graph = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: direction, nodesep: 56, ranksep: 100, marginx: 32, marginy: 32 });
  for (const episode of episodes) graph.setNode(episode.id, { width: STORY_MAP_NODE_WIDTH, height: STORY_MAP_NODE_HEIGHT });
  for (const link of links) graph.setEdge(link.from, link.to);
  dagre.layout(graph);
  return new Map(
    episodes.map((episode) => {
      const position = graph.node(episode.id);
      return [episode.id, { x: position.x - STORY_MAP_NODE_WIDTH / 2, y: position.y - STORY_MAP_NODE_HEIGHT / 2 }];
    }),
  );
}

function linkToEdge(link: StoryLinkView): Edge {
  return {
    id: link.id,
    source: link.from,
    target: link.to,
    label: link.choiceLabel || undefined,
    animated: !link.isPrimary,
    style: { strokeWidth: link.isPrimary ? 2.5 : 1.2 },
    labelStyle: { fill: "var(--foreground)", fontSize: 11 },
    labelBgStyle: { fill: "var(--card)", fillOpacity: 0.92 },
    labelBgPadding: [4, 2],
  };
}

function extractEpisodeId(value: unknown): string | null {
  const record = objectOf(value);
  for (const item of Array.isArray(record.results) ? record.results : []) {
    const entry = objectOf(item);
    if (typeof entry.episode_id === "string" && entry.episode_id) return entry.episode_id;
  }
  const nested = objectOf(record.created ?? record.episode);
  for (const id of [record.episode_id, record.created_episode_id, record.id, nested.id]) {
    if (typeof id === "string" && id) return id;
  }
  return null;
}

function hasUnplacedResult(value: unknown): boolean {
  const record = objectOf(value);
  return (Array.isArray(record.results) ? record.results : []).some((item) => objectOf(item).unplaced === true);
}

/** サーバの DAG 検証エラー（400 / detail は `{message, op_index}`）から表示用の理由を取り出す。 */
function storyErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof StoryApiError) {
    const detail = objectOf(error.detail).detail;
    const message = typeof detail === "string" ? detail : objectOf(detail).message;
    if (typeof message === "string" && message) {
      if (message.includes("循環")) return "この接続は循環するため作成できません";
      return message;
    }
  }
  return error instanceof Error && error.message ? error.message : fallback;
}

/** 線分と矩形の交差判定（Liang-Barsky）。エッジ上へのノードドロップ検出に使う。 */
function segmentIntersectsRect(from: XYPosition, to: XYPosition, rect: Rect): boolean {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const p = [-dx, dx, -dy, dy];
  const q = [from.x - rect.x, rect.x + rect.width - from.x, from.y - rect.y, rect.y + rect.height - from.y];
  let t0 = 0;
  let t1 = 1;
  for (let index = 0; index < 4; index += 1) {
    if (p[index] === 0) {
      if (q[index] < 0) return false;
      continue;
    }
    const ratio = q[index] / p[index];
    if (p[index] < 0) {
      if (ratio > t1) return false;
      if (ratio > t0) t0 = ratio;
    } else {
      if (ratio < t0) return false;
      if (ratio < t1) t1 = ratio;
    }
  }
  return t0 <= t1;
}

function nodeRect(node: StoryMapNode): Rect {
  return {
    x: node.position.x,
    y: node.position.y,
    width: node.measured?.width ?? STORY_MAP_NODE_WIDTH,
    height: node.measured?.height ?? STORY_MAP_NODE_HEIGHT,
  };
}

export function StoryMapPage({ workId }: { workId: string }) {
  return (
    <ReactFlowProvider>
      <StoryMapCanvas workId={workId} />
    </ReactFlowProvider>
  );
}

function StoryMapCanvas({ workId }: { workId: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { mutate: globalMutate } = useSWRConfig();
  const { work } = useStoryWorkContext();
  const { graph, mutate, isLoading, error } = useStoryGraph(workId);
  const { resolvedTheme } = useTheme();
  const reactFlow = useReactFlow<StoryMapNode, Edge>();
  const [search, setSearch] = useState("");
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(null);
  const [nodes, setNodes, onNodesChange] = useNodesState<StoryMapNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [menu, setMenu] = useState<StoryMapMenuState | null>(null);
  const [dialog, setDialog] = useState<StoryMapDialogRequest | null>(null);
  const [insertTargetId, setInsertTargetId] = useState<string | null>(null);
  const highlightStore = useMemo(() => createStoryMapHighlightStore(), []);
  const dragSaveTimers = useRef(new Map<string, number>());
  const pendingFocusRef = useRef<string | null>(null);
  const dialogSequenceRef = useRef(0);
  const focusId = searchParams.get("focus");
  const startEpisodeId = graph.startEpisodeId ?? work.startEpisodeId;

  const openDialog = useCallback((request: Omit<StoryMapDialogRequest, "key">) => {
    dialogSequenceRef.current += 1;
    setDialog({ ...request, key: `story-map-dialog-${dialogSequenceRef.current}` });
  }, []);

  const openEpisode = useCallback(
    (episodeId: string) =>
      router.push(`/scenarios/${encodeURIComponent(workId)}/manuscript?episode=${encodeURIComponent(episodeId)}`),
    [router, workId],
  );

  const applyNodeSelection = useCallback(
    (episodeId: string | null) => {
      setSelectedEpisodeId(episodeId);
      highlightStore.set(episodeId);
      reactFlow.setNodes((current) =>
        current.map((item) =>
          item.selected === (episodeId !== null && item.id === episodeId)
            ? item
            : { ...item, selected: episodeId !== null && item.id === episodeId },
        ),
      );
    },
    [highlightStore, reactFlow],
  );

  const focusNode = useCallback(
    (episodeId: string) => {
      const node = reactFlow.getNode(episodeId);
      if (!node) return;
      applyNodeSelection(episodeId);
      reactFlow.setCenter(
        node.position.x + STORY_MAP_NODE_WIDTH / 2,
        node.position.y + STORY_MAP_NODE_HEIGHT / 2,
        { zoom: 1.1, duration: 450 },
      );
    },
    [applyNodeSelection, reactFlow],
  );

  const selectNode = useCallback(
    (episodeId: string) => {
      applyNodeSelection(episodeId);
    },
    [applyNodeSelection],
  );

  const clearNodeSelection = useCallback(() => {
    applyNodeSelection(null);
  }, [applyNodeSelection]);

  const refreshGraph = useCallback(async () => {
    await mutate();
    void globalMutate(`story-work-shell:${workId}`);
  }, [globalMutate, mutate, workId]);

  // ── ノード / エッジの構築 ────────────────────────────────────────────────
  const incoming = useMemo(() => {
    const result = new Map<string, number>();
    for (const link of graph.links) result.set(link.to, (result.get(link.to) || 0) + 1);
    return result;
  }, [graph.links]);
  const outgoing = useMemo(() => {
    const result = new Map<string, number>();
    for (const link of graph.links) result.set(link.from, (result.get(link.from) || 0) + 1);
    return result;
  }, [graph.links]);
  const initialPositions = useMemo(() => buildLayout(graph.episodes, graph.links), [graph.episodes, graph.links]);

  useEffect(() => {
    const unsavedDrags = new Set(dragSaveTimers.current.keys());
    setNodes((current) => {
      const previous = new Map(current.map((node) => [node.id, node]));
      return graph.episodes.map((episode) => {
        const before = previous.get(episode.id);
        const stored =
          episode.mapX || episode.mapY
            ? { x: episode.mapX, y: episode.mapY }
            : initialPositions.get(episode.id) || { x: 0, y: 0 };
        return {
          id: episode.id,
          type: "story" as const,
          // Delete キーではノードを消さない（設計書 §4.9）。削除はコンテキストメニュー経由のみ。
          deletable: false,
          selected: before?.selected ?? false,
          // ドラッグ中と保存待ちの間はサーバ座標で巻き戻さない。
          position: before && (before.dragging || unsavedDrags.has(episode.id)) ? before.position : stored,
          data: {
            episode,
            incoming: incoming.get(episode.id) || 0,
            outgoing: outgoing.get(episode.id) || 0,
            isStart: episode.id === startEpisodeId,
          },
        };
      });
    });
    setEdges(graph.links.map(linkToEdge));
  }, [graph.episodes, graph.links, incoming, initialPositions, outgoing, setEdges, setNodes, startEpisodeId]);

  useEffect(() => {
    if (selectedEpisodeId && !graph.episodes.some((episode) => episode.id === selectedEpisodeId)) {
      clearNodeSelection();
    }
  }, [clearNodeSelection, graph.episodes, selectedEpisodeId]);

  useEffect(() => {
    if (focusId) pendingFocusRef.current = focusId;
  }, [focusId]);

  useEffect(() => {
    const target = pendingFocusRef.current;
    if (!target || !nodes.some((node) => node.id === target)) return;
    pendingFocusRef.current = null;
    focusNode(target);
  }, [focusNode, nodes]);

  useEffect(() => {
    const timers = dragSaveTimers.current;
    return () => {
      for (const timer of timers.values()) window.clearTimeout(timer);
    };
  }, []);

  // ── 構造操作 ────────────────────────────────────────────────────────────
  const runStructure = useCallback(
    async (
      ops: Record<string, unknown>[],
      options: { success?: string; failure: string; rollback?: () => void },
    ): Promise<unknown> => {
      try {
        const result = await storyApi.updateStructure(workId, { ops });
        await refreshGraph();
        if (options.success) toast.success(options.success);
        return result;
      } catch (structureError) {
        options.rollback?.();
        toast.error(storyErrorMessage(structureError, options.failure));
        return null;
      }
    },
    [refreshGraph, workId],
  );

  const createBranch = useCallback(
    (episodeId: string, duplicate: boolean) => {
      const episode = graph.episodes.find((item) => item.id === episodeId);
      if (!episode) return;
      const label = graph.links.find((link) => link.to === episodeId)?.choiceLabel ?? "";
      openDialog({
        title: duplicate ? "複製して分岐にする" : "続きの分岐を追加",
        description: duplicate
          ? `「${episode.title}」のコピーを、同じ親（前提章）からの別パターンとして隣に並べます。元の章は変更しません。`
          : `「${episode.title}」の続きとして、別パターンの章を白紙で作ります。`,
        confirmLabel: duplicate ? "複製する" : "作成する",
        fields: duplicate
          ? [
              { name: "title", label: "新しい章のタイトル", defaultValue: `${episode.title}（別パターン）`, required: true },
              { name: "label", label: "選択肢ラベル（任意）", defaultValue: label, placeholder: "王を疑う" },
            ]
          : [
              { name: "title", label: "新しい章のタイトル", defaultValue: "新しい分岐", required: true },
              { name: "label", label: "選択肢ラベル（任意）", placeholder: "王を疑う" },
            ],
        onConfirm: async (values) => {
          const title = values.title.trim() || episode.title;
          const choiceLabel = values.label.trim();
          try {
            const result = duplicate
              ? await storyApi.updateStructure(workId, {
                  ops: [
                    {
                      op: "duplicate_as_branch",
                      episode_id: episodeId,
                      choice_label: choiceLabel,
                      new_title: title,
                    },
                  ],
                })
              : await storyApi.createEpisode(workId, {
                  title,
                  plot: "",
                  body: "",
                  status: "unwritten",
                  sort_hint: 0,
                  after_episode_id: episodeId,
                  choice_label: choiceLabel,
                });
            await refreshGraph();
            const createdId = extractEpisodeId(result);
            if (createdId) pendingFocusRef.current = createdId;
            if (duplicate && hasUnplacedResult(result)) {
              toast.warning("前提となる章が無いため未配置に作成しました");
            } else {
              toast.success(duplicate ? "複製分岐を作成しました" : "白紙の分岐を作成しました");
            }
          } catch (createError) {
            toast.error(storyErrorMessage(createError, "分岐を作成できませんでした"));
          }
        },
      });
    },
    [graph.episodes, graph.links, openDialog, refreshGraph, workId],
  );

  const setStart = useCallback(
    (episodeId: string) => {
      void runStructure([{ op: "set_start", episode_id: episodeId }], {
        success: "開始章を変更しました",
        failure: "開始章を変更できませんでした",
      });
    },
    [runStructure],
  );

  const editPremise = useCallback(
    (episodeId: string) => {
      const episode = graph.episodes.find((item) => item.id === episodeId);
      if (!episode) return;
      openDialog({
        title: "前提メモを編集",
        description: `「${episode.title}」に合流するまでに読者が知っている前提を書きます。AI の文脈にも渡ります。`,
        confirmLabel: "保存",
        fields: [
          {
            name: "premise",
            label: "前提メモ",
            defaultValue: episode.premiseNote,
            multiline: true,
            placeholder: "どのルートから来ても成立する共通の前提を書く",
          },
        ],
        onConfirm: async (values) => {
          try {
            await storyApi.updateEpisode(episodeId, { premise_note: values.premise });
            await refreshGraph();
            toast.success("前提メモを更新しました");
          } catch (premiseError) {
            toast.error(storyErrorMessage(premiseError, "前提メモを更新できませんでした"));
          }
        },
      });
    },
    [graph.episodes, openDialog, refreshGraph],
  );

  const disconnectEpisode = useCallback(
    (episodeId: string) => {
      const removed = graph.links.filter((link) => link.from === episodeId || link.to === episodeId);
      if (!removed.length) return;
      const removedIds = new Set(removed.map((link) => link.id));
      setEdges((current) => current.filter((edge) => !removedIds.has(edge.id)));
      void runStructure(
        removed.map((link) => ({ op: "remove_link", id: link.id })),
        {
          success: "接続をすべて外しました",
          failure: "接続を外せませんでした",
          rollback: () => setEdges((current) => [...current, ...removed.map(linkToEdge)]),
        },
      );
    },
    [graph.links, runStructure, setEdges],
  );

  const deleteEpisode = useCallback(
    (episodeId: string) => {
      const episode = graph.episodes.find((item) => item.id === episodeId);
      if (!episode) return;
      openDialog({
        title: "章を削除",
        description: `「${episode.title}」を削除します。前後のリンクは自動で繋ぎ直されます。この操作は取り消せません。`,
        confirmLabel: "削除する",
        destructive: true,
        onConfirm: async () => {
          const removed = graph.links.filter((link) => link.from === episodeId || link.to === episodeId);
          const removedIds = new Set(removed.map((link) => link.id));
          const removedNode = reactFlow.getNode(episodeId);
          setNodes((current) => current.filter((node) => node.id !== episodeId));
          setEdges((current) => current.filter((edge) => !removedIds.has(edge.id)));
          try {
            await storyApi.deleteEpisode(episodeId);
            await refreshGraph();
            toast.success("章を削除しました");
          } catch (deleteError) {
            if (removedNode) setNodes((current) => [...current, removedNode]);
            setEdges((current) => [...current, ...removed.map(linkToEdge)]);
            toast.error(storyErrorMessage(deleteError, "章を削除できませんでした"));
          }
        },
      });
    },
    [graph.episodes, graph.links, openDialog, reactFlow, refreshGraph, setEdges, setNodes],
  );

  // ── キャンバス操作 ──────────────────────────────────────────────────────
  const onConnect = useCallback(
    (connection: Connection) => {
      const { source, target } = connection;
      if (!source || !target || source === target) return;
      const temporaryId = `pending-${source}-${target}`;
      setEdges((current) => [...current, { id: temporaryId, source, target, style: { strokeWidth: 1.5 } }]);
      void runStructure([{ op: "add_link", from: source, to: target, choice_label: "" }], {
        failure: "この接続は作成できませんでした",
        rollback: () => setEdges((current) => current.filter((edge) => edge.id !== temporaryId)),
      });
    },
    [runStructure, setEdges],
  );

  const onConnectEnd = useCallback<OnConnectEnd>(
    (event, connectionState) => {
      if (connectionState.isValid || !connectionState.fromNode) return;
      const source = connectionState.fromNode.id;
      const point = "changedTouches" in event ? event.changedTouches[0] : event;
      if (!point) return;
      const dropped = reactFlow.screenToFlowPosition({ x: point.clientX, y: point.clientY });
      const position = {
        x: Math.round(dropped.x - STORY_MAP_NODE_WIDTH / 2),
        y: Math.round(dropped.y - STORY_MAP_NODE_HEIGHT / 2),
      };
      const sourceTitle = reactFlow.getNode(source)?.data.episode.title ?? "この章";
      openDialog({
        title: "新しい章を作って接続",
        description: `「${sourceTitle}」の続きとして新しい章を作り、ドロップした位置に置きます。`,
        confirmLabel: "作成する",
        fields: [
          { name: "title", label: "章タイトル", defaultValue: "新しい章", required: true },
          { name: "label", label: "選択肢ラベル（任意）", placeholder: "王を信じる" },
        ],
        onConfirm: async (values) => {
          try {
            const created = await storyApi.createEpisode(workId, {
              title: values.title.trim() || "新しい章",
              plot: "",
              body: "",
              status: "unwritten",
              sort_hint: 0,
              after_episode_id: source,
              choice_label: values.label.trim(),
            });
            const createdId = extractEpisodeId(created);
            if (createdId) {
              await storyApi.updateEpisode(createdId, { map_x: position.x, map_y: position.y });
              pendingFocusRef.current = createdId;
            }
            await refreshGraph();
            toast.success("新しい章を作成して接続しました");
          } catch (createError) {
            toast.error(storyErrorMessage(createError, "新しい章を作成できませんでした"));
          }
        },
      });
    },
    [openDialog, reactFlow, refreshGraph, workId],
  );

  const onEdgesDelete = useCallback(
    (deleted: Edge[]) => {
      const removable = deleted.filter((edge) => !edge.id.startsWith("pending-"));
      if (!removable.length) return;
      void runStructure(
        removable.map((edge) => ({ op: "remove_link", id: edge.id })),
        {
          success: "接続を外しました",
          failure: "接続を外せませんでした",
          rollback: () => setEdges((current) => [...current, ...removable]),
        },
      );
    },
    [runStructure, setEdges],
  );

  // Delete キーはエッジの接続解除だけに使う（設計書 §4.9）。
  const onBeforeDelete = useCallback<OnBeforeDelete<StoryMapNode, Edge>>(
    async ({ edges: deletedEdges }) => ({ nodes: [], edges: deletedEdges }),
    [],
  );

  const onEdgeDoubleClick = useCallback(
    (_event: React.MouseEvent, edge: Edge) => {
      const link = graph.links.find((item) => item.id === edge.id);
      if (!link) return;
      openDialog({
        title: "選択肢ラベル",
        description: "分岐の辺に表示され、通し読みの注記にも使われます。",
        confirmLabel: "保存",
        fields: [{ name: "label", label: "ラベル", defaultValue: link.choiceLabel, placeholder: "王を信じる" }],
        onConfirm: async (values) => {
          const label = values.label.trim();
          setEdges((current) =>
            current.map((item) => (item.id === edge.id ? { ...item, label: label || undefined } : item)),
          );
          await runStructure([{ op: "update_link", id: edge.id, choice_label: label }], {
            failure: "選択肢ラベルを更新できませんでした",
            rollback: () => setEdges((current) => current.map((item) => (item.id === edge.id ? linkToEdge(link) : item))),
          });
        },
      });
    },
    [graph.links, openDialog, runStructure, setEdges],
  );

  /** ドラッグ中のノードが跨いでいるエッジ（= 間に挿入する対象）を探す。 */
  const findInsertTarget = useCallback(
    (dragged: StoryMapNode): string | null => {
      const rect = nodeRect(dragged);
      const byId = new Map(reactFlow.getNodes().map((node) => [node.id, node]));
      for (const edge of reactFlow.getEdges()) {
        if (edge.id.startsWith("pending-")) continue;
        if (edge.source === dragged.id || edge.target === dragged.id) continue;
        const source = byId.get(edge.source);
        const target = byId.get(edge.target);
        if (!source || !target) continue;
        const sourceRect = nodeRect(source);
        const targetRect = nodeRect(target);
        const start = { x: sourceRect.x + sourceRect.width, y: sourceRect.y + sourceRect.height / 2 };
        const end = { x: targetRect.x, y: targetRect.y + targetRect.height / 2 };
        if (segmentIntersectsRect(start, end, rect)) return edge.id;
      }
      return null;
    },
    [reactFlow],
  );

  const onNodeDrag = useCallback<OnNodeDrag<StoryMapNode>>(
    (_event, node) => setInsertTargetId(findInsertTarget(node)),
    [findInsertTarget],
  );

  const onNodeDragStop = useCallback<OnNodeDrag<StoryMapNode>>(
    (_event, node) => {
      setInsertTargetId(null);
      const previous = dragSaveTimers.current.get(node.id);
      if (previous !== undefined) window.clearTimeout(previous);
      const timer = window.setTimeout(() => {
        dragSaveTimers.current.delete(node.id);
        void storyApi
          .updateEpisode(node.id, { map_x: Math.round(node.position.x), map_y: Math.round(node.position.y) })
          .catch((dragError) => toast.error(storyErrorMessage(dragError, "座標を保存できませんでした")));
      }, 500);
      dragSaveTimers.current.set(node.id, timer);

      const targetEdgeId = findInsertTarget(node);
      if (!targetEdgeId) return;
      const link = graph.links.find((item) => item.id === targetEdgeId);
      if (!link) return;
      setEdges((current) => [
        ...current.filter((edge) => edge.id !== targetEdgeId),
        { id: `pending-${link.from}-${node.id}`, source: link.from, target: node.id, style: { strokeWidth: 1.5 } },
        { id: `pending-${node.id}-${link.to}`, source: node.id, target: link.to, style: { strokeWidth: 1.5 } },
      ]);
      void runStructure([{ op: "insert_between", link_id: targetEdgeId, episode_id: node.id }], {
        success: "2つの章の間に挿入しました",
        failure: "間に挿入できませんでした",
        rollback: () =>
          setEdges((current) => [
            ...current.filter((edge) => !edge.id.startsWith("pending-")),
            linkToEdge(link),
          ]),
      });
    },
    [findInsertTarget, graph.links, runStructure, setEdges],
  );

  // ── 自動整列（Undo も座標を API へ書き戻す）────────────────────────────
  const applyLayout = async (
    targets: PositionMap,
    previous: PositionMap,
    message: string,
    undoLabel: string | null,
  ): Promise<void> => {
    setNodes((current) => {
      const next = current.map((node) => {
        const position = targets.get(node.id);
        return position ? { ...node, position } : node;
      });
      return next;
    });
    try {
      await Promise.all(
        Array.from(targets, ([episodeId, position]) =>
          storyApi.updateEpisode(episodeId, { map_x: Math.round(position.x), map_y: Math.round(position.y) }),
        ),
      );
      await mutate();
    } catch (layoutError) {
      setNodes((current) =>
        current.map((node) => {
          const position = previous.get(node.id);
          return position ? { ...node, position } : node;
        }),
      );
      toast.error(storyErrorMessage(layoutError, "レイアウトを保存できませんでした"));
      return;
    }
    toast.success(
      message,
      undoLabel
        ? {
            duration: 10000,
            action: {
              label: undoLabel,
              onClick: () => {
                void applyLayout(previous, targets, "レイアウトを元に戻しました", null);
              },
            },
          }
        : undefined,
    );
  };

  const autoLayout = () => {
    const previous: PositionMap = new Map(reactFlow.getNodes().map((node) => [node.id, { ...node.position }]));
    void applyLayout(buildLayout(graph.episodes, graph.links), previous, "自動整列しました", "元に戻す");
  };

  const focusSearch = () => {
    const term = search.trim().toLocaleLowerCase();
    if (!term) return;
    const match = reactFlow
      .getNodes()
      .find((node) =>
        `${node.data.episode.title} ${node.data.episode.plot} ${node.data.episode.summary}`
          .toLocaleLowerCase()
          .includes(term),
      );
    if (!match) {
      toast.message("該当する章が見つかりませんでした");
      return;
    }
    focusNode(match.id);
  };

  const updateLinkLabel = useCallback(
    async (linkId: string, label: string) => {
      const link = graph.links.find((item) => item.id === linkId);
      if (!link) return;
      setEdges((current) =>
        current.map((item) => (item.id === linkId ? { ...item, label: label || undefined } : item)),
      );
      await runStructure([{ op: "update_link", id: linkId, choice_label: label }], {
        failure: "選択肢ラベルを更新できませんでした",
        rollback: () => setEdges((current) => current.map((item) => (item.id === linkId ? linkToEdge(link) : item))),
      });
    },
    [graph.links, runStructure, setEdges],
  );

  const setPrimaryLink = useCallback(
    async (linkId: string) => {
      await runStructure([{ op: "update_link", id: linkId, is_primary: true }], {
        success: "既定の継続先を変更しました",
        failure: "既定の継続先を変更できませんでした",
      });
    },
    [runStructure],
  );

  const selectedEpisode = selectedEpisodeId
    ? graph.episodes.find((episode) => episode.id === selectedEpisodeId) ?? null
    : null;

  const storyMapInspectorRail = useMemo(
    () =>
      selectedEpisode ? (
        <StoryMapInspector
          key={selectedEpisode.id}
          episode={selectedEpisode}
          episodes={graph.episodes}
          links={graph.links}
          isStart={selectedEpisode.id === startEpisodeId}
          onOpenManuscript={openEpisode}
          onUpdateLinkLabel={updateLinkLabel}
          onSetPrimaryLink={setPrimaryLink}
        />
      ) : undefined,
    [graph.episodes, graph.links, openEpisode, selectedEpisode, setPrimaryLink, startEpisodeId, updateLinkLabel],
  );

  useWorkspaceShellRegistration({
    contextRail: storyMapInspectorRail,
    priority: 60,
    id: `story-map-inspector-${workId}`,
  });

  // ── ノードカードへ渡す操作（同一性を固定して memo を効かせる）──────────
  const nodeHandlersRef = useRef<StoryMapNodeActions>({ openMenu: () => {}, editPremise: () => {} });
  const openNodeMenu = useCallback(
    (episodeId: string, point: { x: number; y: number }) => setMenu({ episodeId, x: point.x, y: point.y }),
    [],
  );
  useEffect(() => {
    nodeHandlersRef.current = { openMenu: openNodeMenu, editPremise };
  }, [editPremise, openNodeMenu]);
  const nodeActions = useMemo<StoryMapNodeActions>(
    () => ({
      openMenu: (episodeId, point) => nodeHandlersRef.current.openMenu(episodeId, point),
      editPremise: (episodeId) => nodeHandlersRef.current.editPremise(episodeId),
    }),
    [],
  );
  const mapContext = useMemo(
    () => ({ highlight: highlightStore, actions: nodeActions }),
    [highlightStore, nodeActions],
  );

  const renderedEdges = useMemo(() => {
    if (!insertTargetId) return edges;
    return edges.map((edge) =>
      edge.id === insertTargetId
        ? {
            ...edge,
            animated: true,
            zIndex: 10,
            label: "ここに挿入",
            style: { ...edge.style, stroke: "var(--primary)", strokeWidth: 3, strokeDasharray: "6 4" },
            labelStyle: { fill: "var(--primary)", fontSize: 11, fontWeight: 600 },
          }
        : edge,
    );
  }, [edges, insertTargetId]);

  const menuEpisode = menu ? graph.episodes.find((episode) => episode.id === menu.episodeId) ?? null : null;

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 size-4 animate-spin" />
        分岐マップを読み込み中…
      </div>
    );
  }
  if (error) {
    return (
      <div className="m-6 rounded-lg border border-destructive/40 bg-card p-5 text-sm text-destructive">
        分岐マップを読み込めませんでした。
      </div>
    );
  }

  const unplaced = graph.episodes.filter(
    (episode) => episode.unplaced || !graph.links.some((link) => link.from === episode.id || link.to === episode.id),
  );

  return (
    <StoryMapProvider value={mapContext}>
      <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-background text-on-surface" data-testid="story-map" data-story-map>
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-container-low px-4 py-2">
          <div className="flex min-w-48 flex-1 items-center gap-2">
            <GitBranch className="size-4 text-muted-foreground" />
            <div>
              <h2 className="text-sm font-semibold">分岐マップ</h2>
              <p className="text-[11px] text-muted-foreground">
                ノード {graph.episodes.length} · 接続 {graph.links.length}
              </p>
            </div>
          </div>
          <div className="relative w-56">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") focusSearch();
              }}
              placeholder="章を検索"
              className="h-8 pl-8"
              aria-label="分岐マップを検索"
            />
          </div>
          <Button variant="outline" size="sm" onClick={autoLayout}>
            <WandSparkles className="size-3.5" />
            自動整列
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden bg-background">
          <ReactFlow
            nodes={nodes}
            edges={renderedEdges}
            nodeTypes={nodeTypes}
            colorMode={resolvedTheme}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onConnectEnd={onConnectEnd}
            onBeforeDelete={onBeforeDelete}
            onEdgesDelete={onEdgesDelete}
            onEdgeDoubleClick={onEdgeDoubleClick}
            onNodeDrag={onNodeDrag}
            onNodeDragStop={onNodeDragStop}
            onNodeClick={(_event, node) => selectNode(node.id)}
            onNodeDoubleClick={(_event, node) => openEpisode(node.id)}
            onNodeContextMenu={(event, node) => {
              event.preventDefault();
              setMenu({ episodeId: node.id, x: event.clientX, y: event.clientY });
            }}
            onPaneContextMenu={(event) => {
              event.preventDefault();
              setMenu(null);
            }}
            onPaneClick={() => clearNodeSelection()}
            fitView
            fitViewOptions={{ padding: 0.18 }}
            deleteKeyCode={["Backspace", "Delete"]}
          >
            <Background color="var(--border)" gap={24} size={1} />
            <Controls />
            <MiniMap
              nodeColor={() => "var(--primary)"}
              maskColor="color-mix(in oklch, var(--background) 74%, transparent)"
              bgColor="var(--card)"
            />
            <Panel
              position="top-left"
              className="max-h-64 w-48 overflow-y-auto rounded-md border border-border bg-card/95 p-2"
            >
              <div className="mb-1 text-[10px] font-semibold tracking-[0.12em] text-muted-foreground uppercase">
                未配置 · {unplaced.length}
              </div>
              {unplaced.length ? (
                unplaced.map((episode) => (
                  <button
                    key={episode.id}
                    type="button"
                    onClick={() => focusNode(episode.id)}
                    onDoubleClick={() => openEpisode(episode.id)}
                    className="block w-full truncate rounded px-2 py-1 text-left text-xs hover:bg-accent"
                  >
                    {episode.title}
                  </button>
                ))
              ) : (
                <p className="px-2 py-1 text-[11px] text-muted-foreground">未配置の章はありません。</p>
              )}
            </Panel>
            <Panel
              position="bottom-left"
              className="rounded-md border border-border bg-card/95 p-2 text-xs text-muted-foreground"
            >
              開始:{" "}
              {startEpisodeId
                ? graph.episodes.find((episode) => episode.id === startEpisodeId)?.title || "設定済み"
                : "未設定"}{" "}
              · ダブルクリックで執筆ビュー · 右クリックでメニュー · Delete で選択中の接続を解除
            </Panel>
          </ReactFlow>
        </div>
      </div>
      <StoryMapNodeMenu
        menu={menu}
        episode={menuEpisode}
        isStart={Boolean(menu && menu.episodeId === startEpisodeId)}
        hasLinks={Boolean(
          menu && graph.links.some((link) => link.from === menu.episodeId || link.to === menu.episodeId),
        )}
        onClose={() => setMenu(null)}
        onOpen={openEpisode}
        onNewBranch={(episodeId) => createBranch(episodeId, false)}
        onDuplicate={(episodeId) => createBranch(episodeId, true)}
        onSetStart={setStart}
        onEditPremise={editPremise}
        onDisconnect={disconnectEpisode}
        onDelete={deleteEpisode}
      />
      <StoryMapDialog request={dialog} onClose={() => setDialog(null)} />
    </StoryMapProvider>
  );
}

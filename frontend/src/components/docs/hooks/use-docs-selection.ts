"use client";

import {
  useCallback,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import type {
  DocsNode,
} from "../types";

// アウトラインの複数選択（selectedNodeId / selectedNodeIds / selectionAnchorNodeId）と
// それに直接付随する選択コールバックをまとめて保持するフック。
//
// 選択の確定にあわせてフォーカス要求を出す必要があるため、フォーカス要求の setter を
// 引数で注入する（focusRequestNodeId 本体はノード作成など選択以外からも操作されるため
// DocsWorkspace 側に残す）。selectedNodes / selectedNodeIdSet の派生は nodesById に依存する
// ため、ここでは生の state と ref・コールバックのみを返し、派生はコンポーネント側に残す。
// state・ref・コールバックの名前と識別子・依存配列は抽出前と同一で、挙動不変を保つ。
export function useDocsSelection(
  setFocusRequestNodeId: Dispatch<SetStateAction<string | null>>,
) {
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedNodeIds, setSelectedNodeIds] = useState<string[]>([]);
  const [selectionAnchorNodeId, setSelectionAnchorNodeId] = useState<string | null>(null);
  const selectedNodeIdsRef = useRef<string[]>([]);
  const selectionAnchorNodeIdRef = useRef<string | null>(null);
  const preserveSelectionOnNextFocusRef = useRef(false);

  const selectSingleNode = useCallback((nodeId: string | null) => {
    selectedNodeIdsRef.current = nodeId ? [nodeId] : [];
    selectionAnchorNodeIdRef.current = nodeId;
    setSelectedNodeId(nodeId);
    setSelectedNodeIds(nodeId ? [nodeId] : []);
    setSelectionAnchorNodeId(nodeId);
  }, []);

  const extendNodeSelection = useCallback((node: DocsNode, rows: Array<{ node: DocsNode; depth: number }>, direction: -1 | 1) => {
    const currentIndex = rows.findIndex((row) => row.node.id === node.id);
    if (currentIndex < 0) return;
    const anchorId = selectionAnchorNodeIdRef.current ?? selectionAnchorNodeId ?? node.id;
    const anchorIndex = Math.max(0, rows.findIndex((row) => row.node.id === anchorId));
    const targetIndex = Math.max(0, Math.min(rows.length - 1, currentIndex + direction));
    const start = Math.min(anchorIndex, targetIndex);
    const end = Math.max(anchorIndex, targetIndex);
    const nextIds = rows.slice(start, end + 1).map((row) => row.node.id);
    const targetNode = rows[targetIndex]?.node;
    selectedNodeIdsRef.current = nextIds;
    selectionAnchorNodeIdRef.current = rows[anchorIndex]?.node.id ?? node.id;
    setSelectedNodeIds(nextIds);
    setSelectedNodeId(targetNode?.id ?? node.id);
    setSelectionAnchorNodeId(rows[anchorIndex]?.node.id ?? node.id);
    if (targetNode) {
      preserveSelectionOnNextFocusRef.current = true;
      setFocusRequestNodeId(targetNode.id);
    }
  }, [selectionAnchorNodeId, setFocusRequestNodeId]);

  const selectRangeToNode = useCallback((node: DocsNode, rows: Array<{ node: DocsNode; depth: number }>) => {
    const currentIndex = rows.findIndex((row) => row.node.id === node.id);
    if (currentIndex < 0) return;
    const anchorId = selectionAnchorNodeIdRef.current ?? selectionAnchorNodeId ?? node.id;
    const anchorIndex = Math.max(0, rows.findIndex((row) => row.node.id === anchorId));
    const start = Math.min(anchorIndex, currentIndex);
    const end = Math.max(anchorIndex, currentIndex);
    const nextIds = rows.slice(start, end + 1).map((row) => row.node.id);
    selectedNodeIdsRef.current = nextIds;
    selectionAnchorNodeIdRef.current = rows[anchorIndex]?.node.id ?? node.id;
    setSelectedNodeIds(nextIds);
    setSelectedNodeId(node.id);
    setSelectionAnchorNodeId(rows[anchorIndex]?.node.id ?? node.id);
  }, [selectionAnchorNodeId]);

  const selectDomRangeById = useCallback((nodeId: string, direction?: -1 | 1) => {
    const visibleIds = Array.from(document.querySelectorAll<HTMLElement>("[data-docs-node-id]"))
      .map((element) => element.getAttribute("data-docs-node-id"))
      .filter((id): id is string => Boolean(id))
      .filter((id, index, all) => all.indexOf(id) === index);
    const currentIndex = visibleIds.indexOf(nodeId);
    if (currentIndex < 0) return false;
    const anchorId = selectionAnchorNodeIdRef.current ?? selectionAnchorNodeId ?? nodeId;
    const anchorIndex = Math.max(0, visibleIds.indexOf(anchorId));
    const targetIndex = typeof direction === "number"
      ? Math.max(0, Math.min(visibleIds.length - 1, currentIndex + direction))
      : currentIndex;
    const start = Math.min(anchorIndex, targetIndex);
    const end = Math.max(anchorIndex, targetIndex);
    const nextIds = visibleIds.slice(start, end + 1);
    const targetId = visibleIds[targetIndex] ?? nodeId;
    selectedNodeIdsRef.current = nextIds;
    selectionAnchorNodeIdRef.current = visibleIds[anchorIndex] ?? nodeId;
    setSelectedNodeIds(nextIds);
    setSelectedNodeId(targetId);
    setSelectionAnchorNodeId(visibleIds[anchorIndex] ?? nodeId);
    if (direction && targetId) {
      preserveSelectionOnNextFocusRef.current = true;
      setFocusRequestNodeId(targetId);
    }
    return true;
  }, [selectionAnchorNodeId, setFocusRequestNodeId]);

  return {
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
  };
}

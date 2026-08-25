"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  ChevronDown,
  ChevronRight,
  Clock3,
  ExternalLink,
  FileText,
  Loader2,
} from "lucide-react";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { sortNodesByPosition } from "@/lib/docs-block-model";
import type { DocsNode } from "./types";
import {
  buildOutlineChildren,
  hoistedVisibleChildren,
  isDocsNodeTitleVisible,
  isLegacyEmailEmptyLineNode,
  nodeText,
  readCollapsed,
  writeCollapsed,
} from "./docs-workspace-shared";
import { isDocsSidebarNodeVisible } from "./docs-sidebar-views";
import {
  configureDocsNavigationScope,
  fetchDocsBootstrap,
  fetchDocsNavigationChildren,
  getDocsNavigationServerSnapshot,
  getDocsNavigationSnapshot,
  subscribeDocsNavigation,
  type DocsNavigationSnapshot,
} from "./docs-navigation-store";

const DOCS_ROUTE = "/docs";
const RECENT_LIMIT = 8;
// The quick panel has independent density and lifecycle from the canonical
// Docs sidebar. Never read/write the canonical sidebar expansion key here.
const QUICK_EXPANDED_KEY = "aoitalk.docs.quick-panel.expanded";

function isDocsPath(pathname: string | null) {
  return pathname === DOCS_ROUTE || pathname?.startsWith(`${DOCS_ROUTE}/`) === true;
}

function useDocsNavigationSnapshot(): DocsNavigationSnapshot {
  return useSyncExternalStore(
    subscribeDocsNavigation,
    getDocsNavigationSnapshot,
    getDocsNavigationServerSnapshot,
  );
}

function displayUpdatedAt(value: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("ja-JP", {
    month: "2-digit",
    day: "2-digit",
  });
}

/**
 * Route-independent, read-only Docs navigation for the shell quick panel.
 *
 * Canonical `/docs` publishes the already-loaded DocsWorkspace state into the
 * external store. On other routes this component owns one bootstrap request,
 * with children loaded lazily only when a user expands a branch. It never
 * writes Docs data, starts a second editor, or changes the current route when
 * a row is selected.
 */
export function DocsQuickPanelNavigation() {
  const pathname = usePathname();
  const router = useRouter();
  const userId = useCurrentUserId();
  const snapshot = useDocsNavigationSnapshot();
  const [expanded, setExpanded] = useState<Set<string>>(() => readCollapsed(QUICK_EXPANDED_KEY));
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [loadingChildren, setLoadingChildren] = useState<Set<string>>(new Set());
  const [childrenError, setChildrenError] = useState<string | null>(null);

  useEffect(() => {
    configureDocsNavigationScope(userId);
    // The canonical editor is the single state owner. It publishes its
    // loading snapshot; fetching here would race the editor bootstrap.
    if (isDocsPath(pathname)) return;
    void fetchDocsBootstrap().catch(() => {
      // The store retains a cached projection and exposes the error state.
    });
  }, [pathname, userId]);

  const state = snapshot.state;
  const nodesById = useMemo(
    () => new Map(state.nodes.map((node) => [node.id, node])),
    [state.nodes],
  );
  const isVisible = useCallback((node: DocsNode) => (
    isDocsSidebarNodeVisible(node)
    && !isLegacyEmailEmptyLineNode(node, nodesById)
  ), [nodesById]);
  const childrenByParent = useMemo(
    () => buildOutlineChildren(state.nodes, state.placements),
    [state.nodes, state.placements],
  );
  const roots = useMemo(() => {
    const rootNodes = state.nodes
      .filter((node) => !node.parent_id)
      .flatMap((node) => isVisible(node)
        ? [node]
        : !isDocsNodeTitleVisible(node)
          ? hoistedVisibleChildren(childrenByParent, node.id, isVisible)
          : []);
    return sortNodesByPosition(rootNodes);
  }, [childrenByParent, isVisible, state.nodes]);
  const recentNodes = useMemo(() => {
    const seen = new Set<string>();
    return [...state.nodes]
      .filter((node) => isVisible(node))
      .sort((left, right) => {
        const rightTime = right.updated_at ?? right.created_at ?? "";
        const leftTime = left.updated_at ?? left.created_at ?? "";
        return rightTime.localeCompare(leftTime) || nodeText(left).localeCompare(nodeText(right));
      })
      .filter((node) => {
        if (seen.has(node.id)) return false;
        seen.add(node.id);
        return true;
      })
      .slice(0, RECENT_LIMIT);
  }, [isVisible, state.nodes]);

  const selectedNode = selectedNodeId ? nodesById.get(selectedNodeId) ?? null : null;
  const hasChildren = useCallback((nodeId: string) => (
    hoistedVisibleChildren(childrenByParent, nodeId, isVisible).length > 0
    || (state.has_children_ids ?? []).includes(nodeId)
  ), [childrenByParent, isVisible, state.has_children_ids]);

  const updateExpanded = useCallback((nodeId: string, nextExpanded: boolean) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (nextExpanded) next.add(nodeId);
      else next.delete(nodeId);
      writeCollapsed(next, QUICK_EXPANDED_KEY);
      return next;
    });
  }, []);

  const toggleNode = useCallback(async (node: DocsNode) => {
    if (!hasChildren(node.id)) return;
    const isCurrentlyExpanded = expanded.has(node.id);
    if (isCurrentlyExpanded) {
      updateExpanded(node.id, false);
      return;
    }
    const isLoaded = (state.loaded_children_parent_ids ?? []).includes(node.id);
    const nextCursor = state.children_next_cursor_by_parent?.[node.id];
    if (!isLoaded || nextCursor) {
      setChildrenError(null);
      setLoadingChildren((current) => new Set(current).add(node.id));
      try {
        await fetchDocsNavigationChildren(node.id, nextCursor);
      } catch (error) {
        setChildrenError(error instanceof Error ? error.message : "Docsの子ノードを読み込めませんでした");
        return;
      } finally {
        setLoadingChildren((current) => {
          const next = new Set(current);
          next.delete(node.id);
          return next;
        });
      }
    }
    updateExpanded(node.id, true);
  }, [expanded, hasChildren, state.children_next_cursor_by_parent, state.loaded_children_parent_ids, updateExpanded]);

  const openInDocs = useCallback((nodeId: string) => {
    router.push(`${DOCS_ROUTE}?node_id=${encodeURIComponent(nodeId)}`);
  }, [router]);

  const renderNode = useCallback((node: DocsNode, depth: number, path: Set<string>): ReactNode => {
    if (path.has(node.id)) return null;
    const nextPath = new Set(path).add(node.id);
    const children = hoistedVisibleChildren(childrenByParent, node.id, isVisible);
    const nodeHasChildren = hasChildren(node.id);
    const isExpanded = expanded.has(node.id);
    const isSelected = selectedNodeId === node.id;
    const loading = loadingChildren.has(node.id);
    return (
      <div key={`${node.id}:${node.parent_id ?? "root"}:${node.sort_order}`}>
        <div className="flex min-w-0 items-center" style={{ paddingLeft: depth * 10 }}>
          <button
            type="button"
            className="grid size-5 shrink-0 place-items-center rounded text-sidebar-foreground/60 hover:bg-sidebar-accent"
            aria-label={nodeHasChildren ? (isExpanded ? "折りたたむ" : "展開する") : undefined}
            disabled={!nodeHasChildren || loading}
            onClick={(event: ReactMouseEvent<HTMLButtonElement>) => {
              event.stopPropagation();
              void toggleNode(node);
            }}
          >
            {loading ? <Loader2 className="size-3 animate-spin" /> : nodeHasChildren ? isExpanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" /> : <span className="size-1 rounded-full border border-current" />}
          </button>
          <button
            type="button"
            className={cn(
              "flex min-w-0 flex-1 items-center gap-1.5 rounded px-1.5 py-1 text-left text-xs hover:bg-sidebar-accent",
              isSelected && "bg-sidebar-accent text-sidebar-foreground",
            )}
            data-docs-quick-node-id={node.id}
            onClick={() => setSelectedNodeId(node.id)}
            title={nodeText(node)}
          >
            <FileText className="size-3.5 shrink-0 text-sidebar-foreground/60" />
            <span className="min-w-0 flex-1 truncate">{nodeText(node)}</span>
          </button>
        </div>
        {nodeHasChildren && isExpanded ? (
          <div className="space-y-0.5">
            {children.map((child) => renderNode(child, depth + 1, nextPath))}
          </div>
        ) : null}
      </div>
    );
  }, [childrenByParent, expanded, hasChildren, isVisible, loadingChildren, selectedNodeId, toggleNode]);

  const initialLoading = snapshot.status === "loading" && state.nodes.length === 0;
  const hasError = snapshot.status === "error" && state.nodes.length === 0;
  return (
    <section
      className="mx-2 mb-3 min-h-0 rounded-lg border border-sidebar-border bg-sidebar-accent/20 p-2"
      data-shell-region="quick-panel-docs"
      data-testid="quick-panel-docs-navigation"
      aria-label="Docs navigation"
    >
      <div className="flex items-center gap-2 px-1 py-1 text-xs font-semibold text-sidebar-foreground">
        <FileText className="size-3.5" />
        <span className="min-w-0 flex-1 truncate">Docs</span>
        {snapshot.status === "loading" && state.nodes.length > 0 ? <Loader2 className="size-3 animate-spin text-sidebar-foreground/60" /> : null}
      </div>
      {initialLoading ? (
        <p className="px-1 py-2 text-xs text-sidebar-foreground/60">Docsを読み込んでいます…</p>
      ) : hasError ? (
        <p className="px-1 py-2 text-xs text-sidebar-foreground/60">Docsを読み込めませんでした。</p>
      ) : (
        <>
          <div className="mt-1 px-1 text-[10px] font-medium uppercase tracking-wide text-sidebar-foreground/55">Pages</div>
          <div className="mt-1 max-h-48 space-y-0.5 overflow-auto">
            {roots.length > 0 ? roots.map((node) => renderNode(node, 0, new Set())) : (
              <p className="px-1 py-2 text-xs text-sidebar-foreground/60">Pagesはまだありません。</p>
            )}
          </div>
          {recentNodes.length > 0 ? (
            <div className="mt-3 border-t border-sidebar-border/70 pt-2">
              <div className="flex items-center gap-1 px-1 text-[10px] font-medium uppercase tracking-wide text-sidebar-foreground/55">
                <Clock3 className="size-3" />
                Recent
              </div>
              <div className="mt-1 space-y-0.5">
                {recentNodes.map((node) => (
                  <button
                    key={`recent:${node.id}`}
                    type="button"
                    className={cn(
                      "flex w-full min-w-0 items-center gap-1.5 rounded px-1.5 py-1 text-left text-xs hover:bg-sidebar-accent",
                      selectedNodeId === node.id && "bg-sidebar-accent text-sidebar-foreground",
                    )}
                    data-docs-quick-recent-id={node.id}
                    onClick={() => setSelectedNodeId(node.id)}
                    title={nodeText(node)}
                  >
                    <Clock3 className="size-3 shrink-0 text-sidebar-foreground/55" />
                    <span className="min-w-0 flex-1 truncate">{nodeText(node)}</span>
                    <span className="shrink-0 text-[10px] text-sidebar-foreground/50">{displayUpdatedAt(node.updated_at ?? node.created_at)}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          {childrenError ? <p className="mt-2 px-1 text-[10px] text-destructive">{childrenError}</p> : null}
          {selectedNode ? (
            <div className="mt-2 border-t border-sidebar-border/70 pt-2" data-testid="quick-panel-docs-selection">
              <div className="flex items-start gap-1.5">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-xs font-medium" title={nodeText(selectedNode)}>{nodeText(selectedNode)}</p>
                  <p className="mt-1 line-clamp-3 text-[11px] text-sidebar-foreground/65">
                    {(selectedNode.description || selectedNode.body_text || "詳細はDocsで確認できます").trim()}
                  </p>
                </div>
                <ExternalLink className="mt-0.5 size-3 shrink-0 text-sidebar-foreground/45" />
              </div>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                className="mt-2 h-7 w-full text-xs"
                data-testid="quick-panel-docs-open"
                onClick={() => openInDocs(selectedNode.id)}
              >
                Docsで開く
              </Button>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

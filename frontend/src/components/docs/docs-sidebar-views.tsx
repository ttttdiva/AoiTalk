"use client";

import { useEffect, type MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";
import {
  Archive,
  ChevronDown,
  ChevronRight,
  Columns2,
  ExternalLink,
  Hash,
  Link2,
  Copy,
  ListFilter,
  Plus,
  Type,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  MenuMnemonicButton,
  MenuMnemonicSurface,
} from "@/components/ui/menu-mnemonic";
import { useContextMenuPosition } from "@/hooks/use-context-menu-position";
import { cn } from "@/lib/utils";
import type { DocsNode, DocsSupertag } from "./types";
import {
  hoistedVisibleChildren,
  isDocsNodeTitleVisible,
  nodeText,
  type SidebarContextMenuState,
} from "./docs-workspace-shared";

export function isDocsSidebarNodeVisible(node: DocsNode): boolean {
  return (
    !node.archived_at
    && isDocsNodeTitleVisible(node)
    && node.display_props?.hidden_from_sidebar !== true
    && !node.system_key?.startsWith("project_mail_management:")
  );
}

export function SidebarButton({ icon: Icon, label, active, onClick, className, iconClassName }: { icon: LucideIcon; label: string; active: boolean; onClick: () => void; className?: string; iconClassName?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "group flex h-7 w-full items-center gap-2 rounded-r border-l-2 border-transparent px-2 text-left text-xs text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground",
        active && "border-primary bg-muted text-primary",
        className,
      )}
    >
      <Icon className={cn("size-3.5 shrink-0 text-muted-foreground group-hover:text-foreground", active && "text-primary", iconClassName)} />
      {label}
    </button>
  );
}

export function TrashMainView({
  archivedNodes,
  onRestoreNode,
  onPermanentDeleteNode,
}: {
  archivedNodes: DocsNode[];
  onRestoreNode: (nodeId: string) => void;
  onPermanentDeleteNode: (nodeId: string) => void;
}) {
  return (
    <section className="flex min-w-0 flex-1 flex-col overflow-hidden bg-background">
      <div className="flex h-11 items-center gap-2 border-b border-border px-6 text-sm font-medium">
        <Archive className="size-4 text-primary" />
        ゴミ箱
        <span className="text-xs font-normal text-muted-foreground">{archivedNodes.length} 件</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-6 py-5">
        {archivedNodes.length === 0 ? (
          <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">ゴミ箱は空です</div>
        ) : (
          <div className="mx-auto grid w-full max-w-3xl gap-2 sm:grid-cols-2">
            {archivedNodes.map((node) => (
              <div key={node.id} className="rounded-md border border-border bg-card p-3">
                <div className="truncate text-sm font-medium">{nodeText(node)}</div>
                <div className="mt-1 text-[11px] text-muted-foreground">{node.archived_at ? `Archived: ${node.archived_at}` : ""}</div>
                <div className="mt-2 flex gap-1">
                  <Button type="button" size="sm" variant="secondary" className="h-7 px-2 text-xs" onClick={() => onRestoreNode(node.id)}>復元</Button>
                  <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs text-destructive" onClick={() => onPermanentDeleteNode(node.id)}>完全削除</Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

export function SearchNodesMainView({
  tags,
  searchNodes,
  onCreateSearchNode,
  onOpenNode,
}: {
  tags: DocsSupertag[];
  searchNodes: DocsNode[];
  onCreateSearchNode: (tag: DocsSupertag) => void;
  onOpenNode: (nodeId: string) => void;
}) {
  return (
    <section className="flex min-w-0 flex-1 flex-col overflow-hidden bg-background">
      <div className="flex h-14 items-center justify-between border-b border-border px-6">
        <div className="flex items-center gap-2 text-sm font-medium">
          <ListFilter className="size-4 text-primary" />
        Search nodes
        <span className="text-xs font-normal text-muted-foreground">{searchNodes.length} 件</span>
        </div>
        <span className="rounded border border-primary/30 bg-primary/5 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-primary">Live query</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-6 py-5">
        <div className="mx-auto w-full max-w-4xl space-y-5">
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              <span className="h-px flex-1 bg-border" />
              <span>タグから検索ノードを作成</span>
              <span className="h-px flex-1 bg-border" />
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              {tags.slice(0, 40).map((tag) => (
                <button key={tag.id} type="button" onClick={() => onCreateSearchNode(tag)} className="flex w-full items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-left text-sm transition-colors hover:border-primary/50 hover:bg-muted/50">
                  <ListFilter className="size-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">List of <span className="text-primary">#{tag.name}</span></span>
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              <span className="h-px flex-1 bg-border" />
              <span>既存の検索ノード</span>
              <span className="h-px flex-1 bg-border" />
            </div>
            {searchNodes.length === 0 ? (
              <div className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">検索ノードはまだありません</div>
            ) : (
              <div className="grid gap-2 sm:grid-cols-2">
                {searchNodes.map((node) => (
                  <button key={node.id} type="button" onClick={() => onOpenNode(node.id)} className="flex w-full min-w-0 items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-left text-sm transition-colors hover:border-primary/50 hover:bg-muted/50">
                    <ListFilter className="size-3.5 shrink-0 text-primary" />
                    <span className="min-w-0 flex-1 truncate">{node.title}</span>
                    <span className="shrink-0 rounded border border-primary/40 bg-primary/5 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                      Live query
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

export function DocsSidebarNode({
  node,
  depth,
  focusNodeId,
  selectedNodeId,
  selectedNodeIds,
  dragNodeId,
  childrenByParent,
  collapsed,
  path = new Set<string>(),
  onToggle,
  onOpen,
  onContextMenu,
  onDragStart,
  onDragEnd,
  onDropOnNode,
  isNodeDraggable,
  canDropOnNode,
  nodeHasChildren,
  isNodeVisible = isDocsSidebarNodeVisible,
}: {
  node: DocsNode;
  depth: number;
  focusNodeId: string | null;
  selectedNodeId: string | null;
  selectedNodeIds: string[];
  dragNodeId: string | null;
  childrenByParent: Map<string | null, DocsNode[]>;
  collapsed: Set<string>;
  path?: Set<string>;
  onToggle: (nodeId: string) => void;
  onOpen: (node: DocsNode, event?: ReactMouseEvent<HTMLElement>) => void;
  onContextMenu: (event: ReactMouseEvent<HTMLElement>, node: DocsNode) => void;
  onDragStart: (nodeId: string) => void;
  onDragEnd?: () => void;
  onDropOnNode: (node: DocsNode) => void;
  isNodeDraggable?: (node: DocsNode) => boolean;
  canDropOnNode?: (node: DocsNode) => boolean;
  nodeHasChildren?: (nodeId: string) => boolean;
  isNodeVisible?: (node: DocsNode) => boolean;
}) {
  if (path.has(node.id)) return null;
  const nextPath = new Set([...path, node.id]);
  const children = hoistedVisibleChildren(childrenByParent, node.id, isNodeVisible);
  const hasChildren = children.length > 0 || nodeHasChildren?.(node.id) === true;
  const expanded = collapsed.has(node.id);
  return (
    <div>
      <div className="flex min-w-0 items-center" style={{ paddingLeft: depth * 14 }}>
        <button
          type="button"
          className="grid size-5 shrink-0 place-items-center rounded text-muted-foreground hover:bg-muted"
          aria-label={hasChildren ? expanded ? "折りたたむ" : "展開する" : undefined}
          disabled={!hasChildren}
          onClick={(event) => {
            event.stopPropagation();
            if (hasChildren) onToggle(node.id);
          }}
        >
          {hasChildren ? expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" /> : <span className="size-1.5 rounded-full border border-current" />}
        </button>
        <button
          type="button"
          data-docs-sidebar-node-id={node.id}
          draggable={isNodeDraggable ? isNodeDraggable(node) : true}
          onClick={(event) => onOpen(node, event)}
          onContextMenu={(event) => onContextMenu(event, node)}
          onDragStart={(event) => {
            event.dataTransfer.effectAllowed = "move";
            onDragStart(node.id);
          }}
          onDragEnd={() => onDragEnd?.()}
          onDragOver={(event) => {
            if (canDropOnNode?.(node) === false) return;
            if (dragNodeId && dragNodeId !== node.id) {
              event.preventDefault();
              event.dataTransfer.dropEffect = "move";
            }
          }}
          onDrop={(event) => {
            event.preventDefault();
            if (canDropOnNode?.(node) === false) return;
            onDropOnNode(node);
          }}
          className={cn(
            "group flex h-7 min-w-0 flex-1 items-center gap-2 rounded-r border-l-2 border-transparent px-2 text-left text-xs text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground",
            focusNodeId === node.id && "border-primary bg-muted text-primary",
            selectedNodeIds.includes(node.id) && focusNodeId !== node.id && "bg-muted/60 text-foreground",
          )}
        >
          {node.node_type === "search" ? (
            <ListFilter className="size-3.5 shrink-0 text-primary" />
          ) : (
            <Hash className="size-3.5 shrink-0 text-muted-foreground" />
          )}
          <span className="truncate">{nodeText(node)}</span>
          {node.node_type === "search" ? (
            <span className="shrink-0 rounded border border-primary/40 bg-primary/5 px-1.5 py-0.5 text-[10px] font-medium text-primary">
              Live query
            </span>
          ) : null}
        </button>
      </div>
      {hasChildren && expanded ? (
        <div className="space-y-0.5">
          {children.map((child) => (
            <DocsSidebarNode
              key={`${child.id}:${child.parent_id ?? "root"}:${child.sort_order}`}
              node={child}
              depth={depth + 1}
              focusNodeId={focusNodeId}
              selectedNodeId={selectedNodeId}
              selectedNodeIds={selectedNodeIds}
              dragNodeId={dragNodeId}
              childrenByParent={childrenByParent}
              collapsed={collapsed}
              path={nextPath}
              onToggle={onToggle}
              onOpen={onOpen}
              onContextMenu={onContextMenu}
              onDragStart={onDragStart}
              onDragEnd={onDragEnd}
              onDropOnNode={onDropOnNode}
              isNodeDraggable={isNodeDraggable}
              canDropOnNode={canDropOnNode}
              nodeHasChildren={nodeHasChildren}
              isNodeVisible={isNodeVisible}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function DocsSidebarContextMenu({
  menu,
  node,
  onClose,
  onOpen,
  onOpenSplit,
  onRename,
  onDuplicate,
  onMoveWithReference,
  onExport,
  onCopyReference,
  onCopyId,
  onPin,
  onArchive,
}: {
  menu: SidebarContextMenuState | null;
  node: DocsNode | null;
  onClose: () => void;
  onOpen: (node: DocsNode) => void;
  onOpenSplit: (node: DocsNode) => void;
  onRename: (node: DocsNode) => void;
  onDuplicate: (node: DocsNode) => void;
  onMoveWithReference: (node: DocsNode) => void;
  onExport: (node: DocsNode) => void;
  onCopyReference: (node: DocsNode) => void;
  onCopyId: (node: DocsNode) => void;
  onPin: (node: DocsNode) => void;
  onArchive: (node: DocsNode) => void;
}) {
  const { ref, style } = useContextMenuPosition(
    menu ? { x: menu.x, y: menu.y } : null,
    { fallbackWidth: 192, fallbackHeight: 132 },
  );

  useEffect(() => {
    if (!menu) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menu, onClose, ref]);

  if (!menu || !node || typeof document === "undefined") return null;

  return createPortal(
    <MenuMnemonicSurface
      ref={ref}
      className="fixed z-50 min-w-48 rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
      style={style}
      onContextMenu={(event) => event.preventDefault()}
    >
      <MenuMnemonicButton
        type="button"
        mnemonic="O"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        onClick={() => onOpen(node)}
      >
        <ExternalLink className="size-4" />
        開く
      </MenuMnemonicButton>
      <MenuMnemonicButton
        type="button"
        mnemonic="S"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        onClick={() => onOpenSplit(node)}
      >
        <Columns2 className="size-4" />
        右パネルで開く
      </MenuMnemonicButton>
      <div className="my-1 h-px bg-border" />
      <MenuMnemonicButton
        type="button"
        mnemonic="L"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        onClick={() => onCopyReference(node)}
      >
        <Copy className="size-4" />
        チャット用参照をコピー
      </MenuMnemonicButton>
      <MenuMnemonicButton
        type="button"
        mnemonic="C"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        onClick={() => onCopyId(node)}
      >
        <Hash className="size-4" />
        ノードIDをコピー
      </MenuMnemonicButton>
      <div className="my-1 h-px bg-border" />
      <MenuMnemonicButton
        type="button"
        mnemonic="R"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        onClick={() => onRename(node)}
      >
        <Type className="size-4" />
        名前の変更
      </MenuMnemonicButton>
      <MenuMnemonicButton
        type="button"
        mnemonic="U"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        onClick={() => onDuplicate(node)}
      >
        <Plus className="size-4" />
        複製
      </MenuMnemonicButton>
      <MenuMnemonicButton
        type="button"
        mnemonic="M"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        onClick={() => onMoveWithReference(node)}
      >
        <Link2 className="size-4" />
        参照を残して移動
      </MenuMnemonicButton>
      <MenuMnemonicButton
        type="button"
        mnemonic="E"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        onClick={() => onExport(node)}
      >
        <ExternalLink className="size-4" />
        エクスポート
      </MenuMnemonicButton>
      <MenuMnemonicButton
        type="button"
        mnemonic="P"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        onClick={() => onPin(node)}
      >
        <Hash className="size-4" />
        {node.display_props?.pinned_sidebar === true ? "ピン留め解除" : "ピン留め"}
      </MenuMnemonicButton>
      <div className="my-1 h-px bg-border" />
      <MenuMnemonicButton
        type="button"
        mnemonic="A"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-destructive hover:bg-destructive/10"
        onClick={() => onArchive(node)}
      >
        <Archive className="size-4" />
        アーカイブ
      </MenuMnemonicButton>
    </MenuMnemonicSurface>,
    document.body,
  );
}

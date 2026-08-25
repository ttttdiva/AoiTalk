"use client";

import { memo } from "react";
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { MoreHorizontal, Play, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { StoryEpisodeView } from "@/lib/story/view-model";
import { useStoryMapHighlighted, useStoryMapNodeActions } from "./story-map-context";

export const STORY_MAP_NODE_WIDTH = 224;
export const STORY_MAP_NODE_HEIGHT = 132;

/** 本文を持たない軽量 DTO だけを載せる（設計書 §10.2 規約 3）。 */
export type StoryMapNodeData = {
  episode: StoryEpisodeView;
  incoming: number;
  outgoing: number;
  isStart: boolean;
};

export type StoryMapNode = Node<StoryMapNodeData, "story">;

const statusText: Record<string, string> = {
  unwritten: "未着手",
  draft: "下書き",
  in_progress: "下書き",
  editing: "推敲中",
  revising: "推敲中",
  completed: "完成",
  done: "完成",
  on_hold: "保留",
};

/**
 * 分岐マップのカード。`data` は graph が変わったときだけ作り直され、選択とハイライトは
 * `selected` prop と購読ストアで受けるので、無関係なノードは再描画されない（§10.2 規約 1/2）。
 */
export const StoryMapNodeCard = memo(function StoryMapNodeCard({ id, data, selected }: NodeProps<StoryMapNode>) {
  const actions = useStoryMapNodeActions();
  const highlighted = useStoryMapHighlighted(id);
  const { episode, incoming, isStart } = data;
  const needsPremise = incoming > 1 && !episode.premiseNote;

  return (
    <div
      className={cn(
        "w-56 rounded-lg border bg-card p-3 text-card-foreground",
        selected || highlighted ? "border-primary ring-2 ring-primary/30" : "border-border",
      )}
      data-testid={`story-map-node-${episode.id}`}
      data-highlighted={highlighted ? "true" : undefined}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-primary" />
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-primary" />
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-1">
            {isStart ? <Play className="size-3 shrink-0 fill-primary text-primary" aria-label="開始章" /> : null}
            <span className="truncate text-sm font-semibold">{episode.title}</span>
          </div>
          <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground">
            <span>
              {episode.charCount.toLocaleString("ja-JP")} / {episode.targetChars.toLocaleString("ja-JP")}字
            </span>
            <span>{statusText[episode.status] || episode.status}</span>
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon-xs"
          className="nodrag"
          aria-label={`${episode.title}のメニュー`}
          onClick={(event) => {
            event.stopPropagation();
            const rect = event.currentTarget.getBoundingClientRect();
            actions.openMenu(episode.id, { x: rect.left, y: rect.bottom + 4 });
          }}
        >
          <MoreHorizontal className="size-3.5" />
        </Button>
      </div>
      <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted-foreground">
        {episode.plot || episode.summary || "プロットなし"}
      </p>
      {needsPremise ? (
        <button
          type="button"
          className="nodrag mt-2 flex w-full items-center gap-1 rounded border border-chart-4/40 bg-chart-4/10 px-1.5 py-1 text-left text-[10px] text-chart-4 hover:bg-chart-4/20"
          onClick={(event) => {
            event.stopPropagation();
            actions.editPremise(episode.id);
          }}
        >
          <TriangleAlert className="size-3 shrink-0" />
          合流点です。前提メモが未入力
        </button>
      ) : null}
    </div>
  );
});

"use client";

import "@xyflow/react/dist/style.css";
import { memo, useMemo, useState } from "react";
import { Background, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import dagre from "@dagrejs/dagre";
import { CornerDownRight, GitBranch, GitMerge } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { StoryRequestBody } from "@/lib/story/api";
import { objectOf } from "@/lib/story/view-model";

type ComposeEpisode = { id: string; title: string; plot: string };
type ComposeLink = { from: string; to: string; choiceLabel: string; isPrimary: boolean };
type ComposeProposal = { episodes: ComposeEpisode[]; links: ComposeLink[] };
/** compose/apply の生成済みリクエスト型（§9.9: 手書き DTO を作らない）。 */
type ComposeApplyBody = StoryRequestBody<"/api/story/works/{work_id}/compose/apply", "post">;

type ComposeNodeData = { index: number; title: string; fork: boolean; merge: boolean };
type ComposeNode = Node<ComposeNodeData, "compose">;

/**
 * 章構成ジョブの result を編集可能な提案 DTO に正規化する。
 * バックエンドは episode に id を持たせないため、index 文字列を暫定 id にする
 * （§9 の compose/apply は index / title を別名として解決する）。
 */
function readProposal(value: unknown): ComposeProposal {
  const record = objectOf(value);
  const result = objectOf(record.result);
  const source = Object.keys(result).length ? result : record;
  const episodes = (Array.isArray(source.episodes) ? source.episodes : []).map((item, index) => {
    const entry = objectOf(item);
    return {
      id: String(entry.id ?? entry.key ?? index),
      title: typeof entry.title === "string" ? entry.title : `第${index + 1}章`,
      plot: typeof entry.plot === "string" ? entry.plot : "",
    };
  });
  const links = (Array.isArray(source.links) ? source.links : []).map((item) => {
    const entry = objectOf(item);
    return {
      from: String(entry.from ?? entry.from_episode_id ?? ""),
      to: String(entry.to ?? entry.to_episode_id ?? ""),
      choiceLabel: typeof entry.choice_label === "string" ? entry.choice_label : "",
      isPrimary: entry.is_primary === true,
    };
  }).filter((link) => link.from && link.to);
  return { episodes, links };
}

/** 画面の camelCase state を compose/apply の wire DTO（snake_case）へ変換する。 */
function toApplyBody(proposal: ComposeProposal): ComposeApplyBody {
  return {
    episodes: proposal.episodes.map((episode) => ({ id: episode.id, title: episode.title, plot: episode.plot })),
    links: proposal.links.map((link) => ({
      from: link.from,
      to: link.to,
      choice_label: link.choiceLabel || null,
      is_primary: link.isPrimary,
    })),
  };
}

/** 接続を辿って各案の階層（第何段か）を求める。循環データでも回数上限で停止する。 */
function depthsOf(episodeIds: readonly string[], links: readonly ComposeLink[]): Map<string, number> {
  const depths = new Map(episodeIds.map((id) => [id, 0]));
  for (let round = 0; round < episodeIds.length; round += 1) {
    let changed = false;
    for (const link of links) {
      const from = depths.get(link.from);
      const to = depths.get(link.to);
      if (from === undefined || to === undefined || to >= from + 1) continue;
      depths.set(link.to, from + 1);
      changed = true;
    }
    if (!changed) break;
  }
  return depths;
}

function degreesOf(links: readonly ComposeLink[]) {
  const incoming = new Map<string, number>();
  const outgoing = new Map<string, number>();
  for (const link of links) {
    incoming.set(link.to, (incoming.get(link.to) || 0) + 1);
    outgoing.set(link.from, (outgoing.get(link.from) || 0) + 1);
  }
  return { incoming, outgoing };
}

/** §10.2 に合わせ、ミニグラフの座標は分岐マップと同じ dagre で決める。 */
function buildLayout(episodeIds: readonly string[], links: readonly ComposeLink[]): Map<string, { x: number; y: number }> {
  const graph = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "LR", nodesep: 32, ranksep: 72, marginx: 24, marginy: 24 });
  for (const id of episodeIds) graph.setNode(id, { width: 176, height: 60 });
  for (const link of links) {
    if (graph.hasNode(link.from) && graph.hasNode(link.to)) graph.setEdge(link.from, link.to);
  }
  dagre.layout(graph);
  return new Map(episodeIds.map((id) => {
    const position = graph.node(id);
    return [id, { x: (position?.x ?? 0) - 88, y: (position?.y ?? 0) - 30 }];
  }));
}

const ComposeNodeCard = memo(function ComposeNodeCard({ data }: NodeProps<ComposeNode>) {
  return (
    <div className="w-44 rounded-lg border border-border bg-card px-2.5 py-2 text-card-foreground">
      <Handle type="target" position={Position.Left} className="!size-1.5 !border-0 !bg-primary" />
      <Handle type="source" position={Position.Right} className="!size-1.5 !border-0 !bg-primary" />
      <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
        <span>案{data.index + 1}</span>
        {data.fork ? <GitBranch className="size-3 text-primary" /> : null}
        {data.merge ? <GitMerge className="size-3 text-primary" /> : null}
      </div>
      <div className="truncate text-xs font-semibold">{data.title || "無題の章案"}</div>
    </div>
  );
});

const nodeTypes = { compose: ComposeNodeCard };

/**
 * §4.11「章構成提案プレビュー」。AI が返した章案と接続案を読み取り専用のミニグラフで
 * 示しつつ、タイトルとプロットをその場で編集して一括適用する。
 */
export function StoryComposePreview({ value, onApply }: { value: unknown; onApply: (proposal: ComposeApplyBody) => Promise<void> }) {
  const incomingProposal = useMemo(() => readProposal(value), [value]);
  const [source, setSource] = useState(value);
  const [proposal, setProposal] = useState<ComposeProposal>(incomingProposal);
  const [applying, setApplying] = useState(false);
  // props が差し替わったらレンダー中に同期する（React 公式の「props 変化で state を調整する」形）。
  if (source !== value) {
    setSource(value);
    setProposal(incomingProposal);
  }

  // タイトル編集のたびに再レイアウトしないよう、構造（id 並びと接続）だけを依存にする。
  const episodeIdsKey = JSON.stringify(proposal.episodes.map((episode) => episode.id));
  const episodeIds = useMemo(() => JSON.parse(episodeIdsKey) as string[], [episodeIdsKey]);
  const depths = useMemo(() => depthsOf(episodeIds, proposal.links), [episodeIds, proposal.links]);
  const degrees = useMemo(() => degreesOf(proposal.links), [proposal.links]);
  const layout = useMemo(() => buildLayout(episodeIds, proposal.links), [episodeIds, proposal.links]);
  const titleById = useMemo(() => new Map(proposal.episodes.map((episode, index) => [episode.id, episode.title || `案${index + 1}`])), [proposal.episodes]);
  const indexById = useMemo(() => new Map(proposal.episodes.map((episode, index) => [episode.id, index])), [proposal.episodes]);

  const nodes = useMemo<ComposeNode[]>(() => proposal.episodes.map((episode, index) => ({
    id: episode.id,
    type: "compose" as const,
    position: layout.get(episode.id) ?? { x: (index % 3) * 220, y: Math.floor(index / 3) * 110 },
    data: {
      index,
      title: episode.title,
      fork: (degrees.outgoing.get(episode.id) || 0) > 1,
      merge: (degrees.incoming.get(episode.id) || 0) > 1,
    },
  })), [proposal.episodes, layout, degrees]);

  const edges = useMemo<Edge[]>(() => proposal.links.map((link, index) => ({
    id: `${link.from}-${link.to}-${index}`,
    source: link.from,
    target: link.to,
    label: link.choiceLabel || undefined,
    animated: !link.isPrimary,
    style: { stroke: "var(--border)" },
    labelStyle: { fill: "var(--muted-foreground)", fontSize: 10 },
    labelBgStyle: { fill: "var(--card)" },
  })), [proposal.links]);

  const updateEpisode = (id: string, patch: Partial<ComposeEpisode>) => {
    setProposal((current) => ({
      ...current,
      episodes: current.episodes.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    }));
  };

  const forkCount = proposal.episodes.filter((episode) => (degrees.outgoing.get(episode.id) || 0) > 1).length;
  const mergeCount = proposal.episodes.filter((episode) => (degrees.incoming.get(episode.id) || 0) > 1).length;

  return (
    <div className="space-y-4" data-testid="story-compose-preview">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span>提案された章 {proposal.episodes.length}件</span>
        <span>接続 {proposal.links.length}件</span>
        {forkCount ? <span className="inline-flex items-center gap-1"><GitBranch className="size-3 text-primary" />分岐点 {forkCount}</span> : null}
        {mergeCount ? <span className="inline-flex items-center gap-1"><GitMerge className="size-3 text-primary" />合流 {mergeCount}</span> : null}
      </div>

      {proposal.episodes.length ? (
        <div className="h-56 overflow-hidden rounded-lg border border-border bg-card">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
            panOnDrag
            zoomOnScroll={false}
          >
            <Background color="var(--border)" gap={20} size={1} />
          </ReactFlow>
        </div>
      ) : (
        <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">提案された章案がありません。</div>
      )}

      <div className="space-y-3">
        {proposal.episodes.map((episode, index) => {
          const depth = depths.get(episode.id) || 0;
          const outgoing = proposal.links.filter((link) => link.from === episode.id);
          const isFork = outgoing.length > 1;
          const isMerge = (degrees.incoming.get(episode.id) || 0) > 1;
          return (
            <div key={episode.id} className="rounded-lg border border-border bg-card p-3" style={{ marginLeft: `${Math.min(depth, 6) * 14}px` }}>
              <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
                <span className="font-medium text-muted-foreground">第{index + 1}章案</span>
                {depth > 0 ? <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground"><CornerDownRight className="size-3" />第{depth + 1}段</span> : null}
                {isFork ? <span className="inline-flex items-center gap-1 rounded-full border border-primary/60 px-2 py-0.5 text-[10px] text-primary"><GitBranch className="size-3" />分岐点</span> : null}
                {isMerge ? <span className="inline-flex items-center gap-1 rounded-full border border-primary/60 px-2 py-0.5 text-[10px] text-primary"><GitMerge className="size-3" />合流</span> : null}
              </div>
              <Label className="text-[11px] text-muted-foreground" htmlFor={`compose-title-${index}`}>タイトル</Label>
              <Input
                id={`compose-title-${index}`}
                className="mt-1"
                value={episode.title}
                onChange={(event) => updateEpisode(episode.id, { title: event.target.value })}
                placeholder="章タイトル"
              />
              <Label className="mt-2 block text-[11px] text-muted-foreground" htmlFor={`compose-plot-${index}`}>プロット</Label>
              <Textarea
                id={`compose-plot-${index}`}
                className="mt-1 min-h-20"
                value={episode.plot}
                onChange={(event) => updateEpisode(episode.id, { plot: event.target.value })}
                placeholder="章プロット"
              />
              {outgoing.length ? (
                <ul className="mt-2 space-y-1">
                  {outgoing.map((link, linkIndex) => (
                    <li key={`${link.to}-${linkIndex}`} className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                      <CornerDownRight className="size-3 shrink-0" />
                      <span className="truncate">
                        {link.choiceLabel ? `「${link.choiceLabel}」→ ` : "→ "}
                        案{(indexById.get(link.to) ?? 0) + 1} {titleById.get(link.to) || "未知の章案"}
                      </span>
                      {link.isPrimary ? <span className="rounded-full border border-border px-1.5 text-[10px]">主ルート</span> : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center justify-end gap-3">
        <p className="mr-auto text-[11px] text-muted-foreground">適用すると章案と接続が新規作成されます。既存の本文には触れません。</p>
        <Button
          onClick={async () => {
            setApplying(true);
            try {
              await onApply(toApplyBody(proposal));
            } finally {
              setApplying(false);
            }
          }}
          disabled={applying || !proposal.episodes.length}
        >
          一括適用
        </Button>
      </div>
    </div>
  );
}

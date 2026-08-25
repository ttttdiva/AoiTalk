"use client";

import Link from "next/link";
import { useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Download, Eye, GitBranch, Loader2, PenLine } from "lucide-react";
import { toast } from "sonner";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { storyApi } from "@/lib/story/api";
import { normalizeEpisode, type StoryEpisodeView } from "@/lib/story/view-model";
import { countRouteCharacters, resolveStoryRoute, routeLinkChoices } from "@/lib/story/route";
import { useStoryGraph } from "@/components/story/hooks/use-story-data";
import { useStoryWorkContext } from "@/components/story/shell/story-workspace-shell";

export function StoryReviewPage({ workId }: { workId: string }) {
  const { work } = useStoryWorkContext();
  const { graph, isLoading: graphLoading, error: graphError } = useStoryGraph(workId);
  const route = useMemo(() => resolveStoryRoute({ startEpisodeId: graph.startEpisodeId ?? work.startEpisodeId, episodes: graph.episodes, links: graph.links, currentRoute: work.currentRoute }), [graph.episodes, graph.links, graph.startEpisodeId, work.currentRoute, work.startEpisodeId]);
  const routeIds = route.map((episode) => episode.id);
  const { data: details, isLoading: detailsLoading } = useSWR(routeIds.length ? `story-review:${routeIds.join(",")}` : null, async () => Promise.all(routeIds.map((id) => storyApi.getEpisode(id).then(normalizeEpisode))));
  const scrollRef = useRef<HTMLDivElement>(null);
  const [exportScope, setExportScope] = useState<"route" | "all">("route");
  const virtualizer = useVirtualizer({ count: details?.length || 0, getScrollElement: () => scrollRef.current, estimateSize: () => 300, overscan: 2 });

  // §4.10: 分岐点を通過した章の末尾に「どの選択肢でこのルートに来たか」を注記する。
  const forkNotes = useMemo(() => {
    const notes = new Map<number, string>();
    route.forEach((episode, index) => {
      const next = route[index + 1];
      if (!next?.linkFromPrevious) return;
      if (routeLinkChoices(episode.id, graph.links, graph.episodes).length < 2) return;
      const label = next.linkFromPrevious.choiceLabel?.trim();
      notes.set(index, label || (next.title?.trim() || `第${index + 2}章`));
    });
    return notes;
  }, [route, graph.links, graph.episodes]);

  const manuscriptHref = (episodeId: string) =>
    `/scenarios/${encodeURIComponent(workId)}/manuscript?episode=${encodeURIComponent(episodeId)}`;

  const exportRoute = async () => {
    try {
      const blob = await storyApi.exportWork(workId, exportScope);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${work.title || "story"}-${exportScope}.txt`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "TXTを書き出せませんでした");
    }
  };

  if (graphLoading || detailsLoading) return <div className="flex h-full items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 size-4 animate-spin" />通し読みを準備中…</div>;
  if (graphError) return <div className="m-6 rounded-lg border border-destructive/40 bg-card p-5 text-sm text-destructive">通し読みの構成を取得できませんでした。</div>;

  const items = virtualizer.getVirtualItems();
  return <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background text-on-surface" data-testid="story-review">
    <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border-subtle bg-surface-container-low px-6 py-4">
      <div className="flex min-w-0 flex-1 items-center gap-3"><Eye className="size-5 text-muted-foreground" /><div><h2 className="font-heading text-xl">通し読み</h2><p className="text-xs text-muted-foreground">現在ルート · {route.length}章 · {countRouteCharacters(route).toLocaleString("ja-JP")}字{forkNotes.size ? ` · 分岐 ${forkNotes.size}箇所` : ""}</p></div></div>
      <div className="flex items-center gap-2"><AppSelect aria-label="TXT書き出し範囲" size="sm" value={exportScope} onChange={(event) => setExportScope(event.target.value === "all" ? "all" : "route")}><option value="route">このルート</option><option value="all">全エピソード</option></AppSelect><Button variant="outline" size="sm" onClick={() => void exportRoute()}><Download className="size-3.5" />TXT書き出し</Button></div>
    </header>
    <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-8 py-6">
      <div className="relative mx-auto max-w-3xl" style={{ height: virtualizer.getTotalSize() }}>
        {items.map((item) => {
          const episode = details?.[item.index] as StoryEpisodeView | undefined;
          if (!episode) return null;
          const forkLabel = forkNotes.get(item.index);
          return <article key={episode.id} data-index={item.index} ref={virtualizer.measureElement} className="absolute left-0 top-0 w-full pb-12" style={{ transform: `translateY(${item.start}px)` }}>
            <Link
              href={manuscriptHref(episode.id)}
              className="group -mx-2 mb-3 block rounded-md px-2 py-1 transition-colors hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              data-testid={`story-review-episode-link-${episode.id}`}
              aria-label={`第${item.index + 1}章「${episode.title}」を執筆ビューで開く`}
            >
              <div className="flex items-center gap-2 text-xs text-muted-foreground group-hover:text-foreground">
                <span>第{item.index + 1}章</span><span>·</span><span>{episode.title}</span>
                <PenLine className="size-3 opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden="true" />
              </div>
              <h3 className="font-heading text-2xl tracking-tight group-hover:text-primary">{episode.title}</h3>
            </Link>
            {episode.plot && <p className="mt-3 border-l-2 border-primary/50 pl-3 text-sm italic text-muted-foreground">{episode.plot}</p>}
            <div className="mt-6 whitespace-pre-wrap text-[0.98rem] leading-8">{episode.body || <span className="text-muted-foreground">本文はまだありません。</span>}</div>
            {forkLabel && <div className="mt-6 flex items-start gap-2 rounded-lg border border-dashed border-primary/60 bg-primary/5 px-3 py-2 text-xs text-muted-foreground" data-testid={`story-review-fork-${episode.id}`}><GitBranch className="mt-0.5 size-3.5 shrink-0 text-primary" /><span>⑂ ここで「<span className="font-medium text-foreground">{forkLabel}</span>」を選択 — 別のパターンは分岐マップでルートを切り替えると読めます。</span></div>}
          </article>;
        })}
      </div>
      {!details?.length && <div className="py-20 text-center text-sm text-muted-foreground">現在ルートに章がありません。</div>}
    </div>
  </div>;
}

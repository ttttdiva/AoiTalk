"use client";

import Link from "next/link";
import { ExternalLink, Loader2, MessageCircle, PenLine } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useStoryEpisode, useStoryWork } from "@/components/story/hooks/use-story-data";

export function StoryChatAuthoringWorkspace({
  workId,
  episodeId,
  writingSessionId,
  onAskAgent,
  className,
}: {
  workId: string;
  episodeId?: string | null;
  writingSessionId?: string;
  onAskAgent?: (instruction: string) => void | Promise<void>;
  className?: string;
}) {
  const { work, isLoading } = useStoryWork(workId);
  const { episode, isLoading: episodeLoading } = useStoryEpisode(episodeId ?? null);
  // 対象章が分かればその章を開く。分からない場合は作品の執筆ビューを開く（§4.12）。
  const studioHref = `/scenarios/${encodeURIComponent(workId)}/manuscript${episodeId ? `?episode=${encodeURIComponent(episodeId)}` : ""}`;
  const episodeLabel = episodeId
    ? episodeLoading
      ? "章を読み込み中…"
      : episode?.title || "無題の章"
    : "対象章は未設定です";

  return (
    <section className={`flex h-full min-h-0 flex-col bg-sidebar ${className || ""}`} data-testid="story-chat-authoring-workspace">
      <header className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-3">
        <PenLine className="size-4 text-primary" />
        <div className="min-w-0 flex-1">
          <div className="text-xs font-semibold">スタジオ執筆</div>
          <div className="truncate text-[11px] text-muted-foreground">{isLoading ? "作品を読み込み中…" : work.title || `作品 ${workId}`}</div>
        </div>
        <Link href={studioHref} className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-card px-2 text-xs hover:bg-accent" data-testid="story-chat-open-studio">
          <ExternalLink className="size-3" />スタジオで開く
        </Link>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mb-3 rounded-lg border border-border bg-card px-3 py-2" data-testid="story-chat-target-episode">
          <div className="text-[10px] uppercase tracking-[0.14em] text-muted-foreground">対象エピソード</div>
          <div className="mt-0.5 flex items-center gap-1.5 text-sm font-medium">
            {episodeId && episodeLoading && <Loader2 className="size-3.5 shrink-0 animate-spin text-muted-foreground" />}
            <span className="truncate">{episodeLabel}</span>
          </div>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="flex items-start gap-3">
            <MessageCircle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <div>
              <h3 className="text-sm font-medium">チャットから本文を編集</h3>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">チャットの指示を受けて、Story Studio の本文と履歴を更新します。対象章はスタジオで確認できます。</p>
            </div>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Link href={studioHref} className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-primary px-2.5 text-sm text-primary-foreground hover:bg-primary/90">
              <PenLine className="size-3.5" />執筆ビューを開く
            </Link>
            {onAskAgent && (
              <Button variant="outline" size="sm" onClick={() => void onAskAgent("Story Studioで対象章の本文を確認し、必要なら執筆を続けてください。")}>
                チャットに依頼
              </Button>
            )}
          </div>
        </div>
        {writingSessionId && <p className="mt-3 text-[11px] text-muted-foreground">執筆セッション: {writingSessionId}</p>}
      </div>
    </section>
  );
}

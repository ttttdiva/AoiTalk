"use client";

import Image from "next/image";
import useSWR from "swr";
import { ImageIcon, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { storyApi } from "@/lib/story/api";
import { normalizeIllustrations, type StoryIllustrationView } from "@/lib/story/view-model";

export type StoryIllustrationSegment = {
  kind: "text" | "illustration";
  text?: string;
  illustration?: StoryIllustrationView;
};

/** 本文を失わず、挿絵位置へ illustration セグメントを差し込む。 */
export function buildSegments(body: string, illustrations: StoryIllustrationView[]): StoryIllustrationSegment[] {
  const positioned = illustrations
    .filter((item) => !item.stale && typeof item.resolvedIndex === "number" && item.resolvedIndex >= 0)
    .sort((left, right) => (left.resolvedIndex ?? 0) - (right.resolvedIndex ?? 0));
  if (!positioned.length) {
    return body ? [{ kind: "text", text: body }] : [];
  }
  const segments: StoryIllustrationSegment[] = [];
  let cursor = 0;
  for (const illustration of positioned) {
    const index = illustration.resolvedIndex ?? 0;
    const splitAt = Math.min(Math.max(index, cursor), body.length);
    if (splitAt > cursor) {
      segments.push({ kind: "text", text: body.slice(cursor, splitAt) });
    }
    segments.push({ kind: "illustration", illustration });
    const anchorEnd = Math.min(body.length, splitAt + illustration.anchorQuote.length);
    if (anchorEnd > splitAt) {
      segments.push({ kind: "text", text: body.slice(splitAt, anchorEnd) });
    }
    cursor = anchorEnd;
  }
  if (cursor < body.length) {
    segments.push({ kind: "text", text: body.slice(cursor) });
  }
  return segments;
}

function IllustrationCard({
  illustration,
  onMutate,
}: {
  illustration: StoryIllustrationView;
  onMutate: () => Promise<unknown>;
}) {
  const busy = illustration.status === "pending";
  const failed = illustration.status === "failed";
  const imageUrl = illustration.imageUrl;

  const regenerate = async () => {
    try {
      await storyApi.regenerateIllustration(illustration.id);
      await onMutate();
      toast.success("挿絵を再生成しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "挿絵の再生成に失敗しました");
    }
  };

  const remove = async () => {
    try {
      await storyApi.deleteIllustration(illustration.id);
      await onMutate();
      toast.success("挿絵を削除しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "挿絵の削除に失敗しました");
    }
  };

  return (
    <figure className="my-4 overflow-hidden rounded-md border border-border-subtle bg-surface-container-lowest" data-testid={`story-illustration-${illustration.id}`}>
      <div className="relative flex min-h-32 items-center justify-center bg-muted/20">
        {busy ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />生成中…</div>
        ) : imageUrl ? (
          <Image src={imageUrl} alt={illustration.sceneDescription || "挿絵"} width={960} height={540} className="h-auto max-h-80 w-full object-contain" unoptimized />
        ) : (
          <div className="flex items-center gap-2 text-sm text-muted-foreground"><ImageIcon className="size-4" />画像なし</div>
        )}
      </div>
      <figcaption className="space-y-2 border-t border-border-subtle px-3 py-2 text-xs text-muted-foreground">
        {illustration.sceneDescription ? <p>{illustration.sceneDescription}</p> : null}
        {failed && illustration.errorMessage ? <p className="text-destructive">{illustration.errorMessage}</p> : null}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => void regenerate()} disabled={busy}><RefreshCw className="size-3.5" />再生成</Button>
          <Button variant="outline" size="sm" onClick={() => void remove()} disabled={busy}><Trash2 className="size-3.5" />削除</Button>
        </div>
      </figcaption>
    </figure>
  );
}

export function StoryIllustrationLayer({
  episodeId,
  body,
  variant = "reading",
}: {
  episodeId: string;
  body: string;
  /** reading: 本文＋挿絵の読み表示。manage: 挿絵操作のみ（本文は描画しない）。 */
  variant?: "reading" | "manage";
}) {
  const { data, mutate, isLoading } = useSWR(
    episodeId ? `story-illustrations:${episodeId}` : null,
    () => storyApi.listIllustrations(episodeId),
    { refreshInterval: (current) => {
      const payload = normalizeIllustrations(current);
      const pending = [...payload.active, ...payload.stale].some((item) => item.status === "pending");
      return pending ? 2000 : 0;
    } },
  );
  const { active, stale } = normalizeIllustrations(data);
  const segments = buildSegments(body, active);

  const generate = async () => {
    try {
      await storyApi.generateIllustrations(episodeId);
      await mutate();
      toast.success("挿絵生成を開始しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "挿絵生成に失敗しました");
    }
  };

  if (isLoading && !data) {
    return null;
  }

  const hasContent = active.length > 0 || stale.length > 0;
  const generateButton = (
    <Button variant="outline" size="sm" onClick={() => void generate()} data-testid="story-illustration-generate">
      <ImageIcon className="size-3.5" />{hasContent ? "挿絵を追加生成" : "挿絵を生成"}
    </Button>
  );

  if (!hasContent) {
    return (
      <div className="mb-4 flex justify-end" data-testid="story-illustration-manage">
        {generateButton}
      </div>
    );
  }

  const staleList = stale.length > 0 ? (
    <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3" data-testid="story-illustration-stale-list">
      <div className="text-sm font-medium text-amber-700 dark:text-amber-300">位置を失った挿絵 ({stale.length})</div>
      <p className="mt-1 text-xs text-muted-foreground">本文の編集で引用箇所が見つからなくなった挿絵です。再生成または削除できます。</p>
      <div className="mt-3 space-y-3">
        {stale.map((illustration) => (
          <IllustrationCard key={illustration.id} illustration={illustration} onMutate={mutate} />
        ))}
      </div>
    </div>
  ) : null;

  if (variant === "manage") {
    return (
      <div className="mb-4 space-y-4" data-testid="story-illustration-manage">
        <div className="flex items-center justify-between gap-2">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">挿絵</div>
          {generateButton}
        </div>
        {active.length > 0 ? (
          <div className="space-y-3">
            {active.map((illustration) => (
              <IllustrationCard key={illustration.id} illustration={illustration} onMutate={mutate} />
            ))}
          </div>
        ) : null}
        {staleList}
      </div>
    );
  }

  return (
    <div className="mb-4 space-y-4" data-testid="story-illustration-reading">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">挿絵プレビュー</div>
        {generateButton}
      </div>
      <div className="rounded-md border border-dashed border-border-subtle bg-surface-container-lowest/40 px-4 py-3 text-sm leading-relaxed text-muted-foreground">
        {segments.map((segment, index) => {
          if (segment.kind === "text") {
            return <p key={`text-${index}`} className="whitespace-pre-wrap">{segment.text}</p>;
          }
          return <IllustrationCard key={segment.illustration?.id || index} illustration={segment.illustration!} onMutate={mutate} />;
        })}
      </div>
      {staleList}
    </div>
  );
}

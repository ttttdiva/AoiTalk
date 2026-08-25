"use client";

import { useMemo, useState } from "react";
import { ArrowRight, GitBranch, Loader2, Star } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import type { StoryEpisodeView, StoryLinkView } from "@/lib/story/view-model";

function statusLabel(status: string): string {
  return (
    ({
      unwritten: "未着手",
      draft: "下書き",
      revising: "推敲中",
      done: "完成",
      on_hold: "保留",
    }) as Record<string, string>
  )[status] || status;
}

type StoryMapInspectorProps = {
  episode: StoryEpisodeView;
  episodes: StoryEpisodeView[];
  links: StoryLinkView[];
  isStart: boolean;
  onOpenManuscript: (episodeId: string) => void;
  onUpdateLinkLabel: (linkId: string, label: string) => Promise<void>;
  onSetPrimaryLink: (linkId: string) => Promise<void>;
};

function episodeTitle(episodes: StoryEpisodeView[], episodeId: string): string {
  return episodes.find((item) => item.id === episodeId)?.title || "無題の章";
}

export function StoryMapInspector({
  episode,
  episodes,
  links,
  isStart,
  onOpenManuscript,
  onUpdateLinkLabel,
  onSetPrimaryLink,
}: StoryMapInspectorProps) {
  const incoming = useMemo(
    () => links.filter((link) => link.to === episode.id),
    [episode.id, links],
  );
  const outgoing = useMemo(
    () =>
      [...links.filter((link) => link.from === episode.id)].sort((left, right) => {
        const leftPrimary = left.isPrimary ? 0 : 1;
        const rightPrimary = right.isPrimary ? 0 : 1;
        if (leftPrimary !== rightPrimary) return leftPrimary - rightPrimary;
        return left.position - right.position;
      }),
    [episode.id, links],
  );
  const [editingLinkId, setEditingLinkId] = useState<string | null>(null);
  const [labelDraft, setLabelDraft] = useState("");
  const [busyLinkId, setBusyLinkId] = useState<string | null>(null);

  const startEditLabel = (link: StoryLinkView) => {
    setEditingLinkId(link.id);
    setLabelDraft(link.choiceLabel);
  };

  const saveLabel = async (linkId: string) => {
    setBusyLinkId(linkId);
    try {
      await onUpdateLinkLabel(linkId, labelDraft.trim());
      setEditingLinkId(null);
    } finally {
      setBusyLinkId(null);
    }
  };

  const setPrimary = async (linkId: string) => {
    setBusyLinkId(linkId);
    try {
      await onSetPrimaryLink(linkId);
    } finally {
      setBusyLinkId(null);
    }
  };

  return (
    <aside
      className="flex min-h-0 flex-1 flex-col bg-surface-charcoal text-on-surface"
      data-testid="story-map-inspector-rail"
      data-shell-workspace="story"
    >
      <div className="border-b border-border-subtle px-4 py-3">
        <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-on-surface-variant">
          Map Inspector
        </div>
        <div className="mt-1 truncate text-sm font-medium" title={episode.title}>
          {episode.title}
        </div>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 text-xs">
        <section className="space-y-2 rounded-sm border border-border-subtle bg-surface-container-lowest p-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary" className="text-[10px]">
              {statusLabel(episode.status)}
            </Badge>
            {isStart ? (
              <Badge variant="outline" className="text-[10px]">
                開始章
              </Badge>
            ) : null}
          </div>
          <div className="text-on-surface-variant">
            {episode.charCount.toLocaleString("ja-JP")} / {episode.targetChars.toLocaleString("ja-JP")}字
          </div>
          <p className="line-clamp-4 text-sm leading-5 text-on-surface">
            {episode.plot || episode.summary || "プロット・要約は未入力です。"}
          </p>
          {episode.premiseNote ? (
            <div className="rounded-sm border border-border-subtle bg-surface-container-low p-2">
              <div className="text-[10px] font-semibold text-on-surface-variant">前提メモ</div>
              <p className="mt-1 line-clamp-4 text-sm leading-5">{episode.premiseNote}</p>
            </div>
          ) : null}
          <Button variant="outline" size="sm" className="w-full" onClick={() => onOpenManuscript(episode.id)}>
            執筆ビューで開く
          </Button>
        </section>

        <section className="space-y-2">
          <div className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-on-surface-variant">
            <ArrowRight className="size-3 rotate-180" />
            流入 ({incoming.length})
          </div>
          {incoming.length ? (
            <ul className="space-y-1">
              {incoming.map((link) => (
                <li
                  key={link.id}
                  className="rounded-sm border border-border-subtle bg-surface-container-lowest px-2 py-1.5"
                >
                  <div className="truncate font-medium">{episodeTitle(episodes, link.from)}</div>
                  {link.choiceLabel ? (
                    <div className="mt-0.5 text-[11px] text-on-surface-variant">「{link.choiceLabel}」</div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-[11px] text-on-surface-variant">流入リンクはありません。</p>
          )}
        </section>

        <section className="space-y-2">
          <div className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-on-surface-variant">
            <GitBranch className="size-3" />
            後続リンク ({outgoing.length})
          </div>
          {outgoing.length ? (
            <ul className="space-y-2">
              {outgoing.map((link) => {
                const busy = busyLinkId === link.id;
                const editing = editingLinkId === link.id;
                return (
                  <li
                    key={link.id}
                    className="space-y-2 rounded-sm border border-border-subtle bg-surface-container-lowest p-2"
                    data-testid={`story-map-outgoing-link-${link.id}`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate font-medium">{episodeTitle(episodes, link.to)}</div>
                        {link.isPrimary ? (
                          <div className="mt-0.5 flex items-center gap-1 text-[10px] text-primary">
                            <Star className="size-3 fill-primary" />
                            既定の継続先
                          </div>
                        ) : null}
                      </div>
                      {!link.isPrimary && outgoing.length > 1 ? (
                        <Button
                          variant="ghost"
                          size="xs"
                          disabled={busy}
                          onClick={() => void setPrimary(link.id)}
                        >
                          {busy ? <Loader2 className="size-3 animate-spin" /> : "既定にする"}
                        </Button>
                      ) : null}
                    </div>
                    {editing ? (
                      <div className="space-y-1">
                        <Label className="text-[10px]">選択肢ラベル</Label>
                        <Input
                          value={labelDraft}
                          onChange={(event) => setLabelDraft(event.target.value)}
                          className="h-7 text-xs"
                          aria-label="選択肢ラベル"
                        />
                        <div className="flex gap-1">
                          <Button
                            size="xs"
                            disabled={busy}
                            aria-label="選択肢ラベルを保存"
                            onClick={() => void saveLabel(link.id)}
                          >
                            {busy ? <Loader2 className="size-3 animate-spin" /> : "保存"}
                          </Button>
                          <Button
                            variant="ghost"
                            size="xs"
                            disabled={busy}
                            aria-label="選択肢ラベル編集をキャンセル"
                            onClick={() => setEditingLinkId(null)}
                          >
                            キャンセル
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[11px] text-on-surface-variant">
                          {link.choiceLabel || "（ラベルなし）"}
                        </span>
                        <Button variant="link" size="xs" className="h-6 px-0" onClick={() => startEditLabel(link)}>
                          編集
                        </Button>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="text-[11px] text-on-surface-variant">後続リンクはありません。</p>
          )}
        </section>
      </div>
    </aside>
  );
}

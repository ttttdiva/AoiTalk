"use client";

import { useMemo } from "react";
import useSWR from "swr";
import { Check, Loader2, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { storyApi } from "@/lib/story/api";
import { normalizeContext, type StoryContextView } from "@/lib/story/view-model";

/** §8.8「使用モデルの可視化」の解決層ラベル。 */
const LAYER_LABELS: Record<string, string> = {
  runtime: "実行時",
  work: "作品設定",
  writing_class: "執筆クラス",
  main_llm_inherited: "メインLLM継承",
};

/** §8.1 の注入素材 kind を日本語見出しに落とす。表示順もこの並びに従う。 */
const KIND_LABELS: Array<[string, string]> = [
  ["model", "モデル"],
  ["work", "作品設定"],
  ["rulebook", "ルールブック"],
  ["character", "人物"],
  ["note", "資料"],
  ["premise", "前提メモ"],
  ["body", "祖先章の本文"],
  ["summary", "祖先章の要約"],
];

type Chiclet = { key: string; kind: string; kindLabel: string; title: string };

function chicletsOf(context: StoryContextView): Chiclet[] {
  const order = new Map(KIND_LABELS.map(([kind], index) => [kind, index]));
  return context.injected
    .map((item, index) => {
      const kind = typeof item.kind === "string" ? item.kind : "other";
      const title = String(item.title ?? item.label ?? item.name ?? kind);
      return {
        key: `${index}-${kind}-${title}`,
        kind,
        kindLabel: KIND_LABELS.find(([value]) => value === kind)?.[1] || "素材",
        title,
      };
    })
    .sort((left, right) => (order.get(left.kind) ?? KIND_LABELS.length) - (order.get(right.kind) ?? KIND_LABELS.length));
}

export function StoryPromptPreview({ episodeId }: { episodeId: string | null }) {
  const { data, isLoading, error } = useSWR(
    episodeId ? `story-context-preview:${episodeId}` : null,
    () => storyApi.contextPreview(episodeId as string),
  );
  const context = useMemo(() => normalizeContext(data), [data]);
  const chiclets = useMemo(() => chicletsOf(context), [context]);
  const counts = useMemo(() => {
    const totals = new Map<string, number>();
    for (const chiclet of chiclets) {
      if (chiclet.kind === "model") continue;
      totals.set(chiclet.kindLabel, (totals.get(chiclet.kindLabel) || 0) + 1);
    }
    return Array.from(totals.entries());
  }, [chiclets]);

  if (isLoading) {
    return <div className="py-8 text-center text-sm text-muted-foreground"><Loader2 className="mx-auto mb-2 size-4 animate-spin" />文脈を組み立て中…</div>;
  }
  if (error) return <p className="text-sm text-destructive">文脈プレビューを取得できませんでした。</p>;

  const modelLabel = [context.provider, context.model].filter(Boolean).join("/") || context.resolvedModel || "未設定";
  const layerKey = context.layer || context.modelLayer || "";
  const layerLabel = LAYER_LABELS[layerKey] || layerKey || "不明";
  const promptLength = Array.from(context.prompt).length;

  return (
    <div className="space-y-4" data-testid="story-prompt-preview">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-primary/40 bg-primary/5 px-3 py-2">
        <Sparkles className="size-4 text-primary" />
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">使用モデル</div>
          <div className="truncate text-sm font-medium">{modelLabel}</div>
        </div>
        <Badge variant="outline" className="ml-auto border-primary/50 text-primary">解決層: {layerLabel}</Badge>
      </div>

      <div>
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          <span>このリクエストに含まれる素材</span>
          {counts.length ? <span>{counts.map(([label, total]) => `${label}${total}`).join(" · ")}</span> : null}
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {chiclets.length ? chiclets.map((chiclet) => (
            <span
              key={chiclet.key}
              className={`inline-flex max-w-full items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] ${chiclet.kind === "model" ? "border-primary/60 text-primary" : "border-border text-foreground"}`}
              title={`${chiclet.kindLabel}: ${chiclet.title}`}
            >
              <Check className="size-3 shrink-0 text-primary" />
              <span className="text-muted-foreground">{chiclet.kindLabel}</span>
              <span className="truncate">{chiclet.title}</span>
            </span>
          )) : <span className="text-xs text-muted-foreground">注入される素材はありません。</span>}
        </div>
        <p className="mt-2 text-[11px] text-muted-foreground">非公開メモは送信されません。参照モードが「参照しない」「明示時のみ」の素材は、明示指定がない限りこの一覧に現れません。</p>
      </div>

      <div>
        <div className="flex items-center justify-between text-[11px] text-muted-foreground">
          <span>組み立てられたプロンプト</span>
          <span>推定 {promptLength.toLocaleString("ja-JP")}文字</span>
        </div>
        <pre className="mt-1.5 max-h-[46vh] overflow-auto whitespace-pre-wrap rounded-md border border-border bg-muted/30 p-3 text-xs leading-6">{context.prompt || "プロンプトは空です。"}</pre>
      </div>
    </div>
  );
}

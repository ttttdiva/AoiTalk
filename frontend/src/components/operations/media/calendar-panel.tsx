"use client";

import { CalendarDays, Loader2, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import {
  mediaOverviewApi,
  type MediaCalendarEvent,
} from "@/lib/media-operations-overview-api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/lib/utils";

function readableError(error: unknown): string {
  return error instanceof Error && error.message
    ? error.message
    : "カレンダーの読み込みに失敗しました";
}

function statusLabel(value: string): string {
  const labels: Record<string, string> = {
    scheduled: "予約済み",
    draft: "下書き",
    proposed: "提案",
    approved: "承認済み",
    running: "実行中",
    succeeded: "成功",
    failed: "失敗",
    uncertain: "要照合",
    review_required: "レビュー待ち",
    pending_review: "レビュー待ち",
    blocked: "ブロック",
    configured: "設定済み",
    recorded: "記録済み",
  };
  return labels[value] ?? value;
}

function kindLabel(value: string): string {
  const labels: Record<string, string> = {
    publication: "公開",
    editorial_draft: "編集・コンテンツ",
    generation: "生成",
    research: "調査",
    review: "人手レビュー",
  };
  return labels[value] ?? value;
}

function EventRow({ event }: { event: MediaCalendarEvent }) {
  const startsAt = new Date(event.starts_at);
  const displayTime = Number.isNaN(startsAt.valueOf())
    ? event.starts_at
    : startsAt.toLocaleString("ja-JP", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });

  return (
    <li className="flex flex-wrap items-center gap-3 rounded-lg border border-border/70 bg-background/40 px-3 py-2.5" data-testid="media-calendar-event">
      <time className="w-28 shrink-0 text-xs tabular-nums text-muted-foreground">{displayTime}</time>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-sm font-medium">{event.title}</span>
          <span className="rounded-full border border-border bg-muted/50 px-2 py-0.5 text-[10px] text-muted-foreground">{kindLabel(event.kind)}</span>
          {event.platform ? <span className="text-[11px] text-muted-foreground">{event.platform}</span> : null}
        </div>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {event.persona_label ?? "ペルソナ未設定"}
          {event.requires_human_action ? " · 人手アクションが必要" : ""}
        </p>
      </div>
      <span className={cn("rounded-full border px-2 py-0.5 text-[11px]", event.requires_human_action ? "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200" : "border-border bg-muted/50 text-muted-foreground")}>
        {statusLabel(event.status)}
      </span>
    </li>
  );
}

export function MediaCalendarPanel() {
  const [events, setEvents] = useState<MediaCalendarEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await mediaOverviewApi.listCalendar();
      setEvents(result.items);
      setTotal(result.total);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const task = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(task);
  }, [load]);

  return (
    <div className="space-y-4" data-testid="media-calendar-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight">カレンダー</h2>
          <p className="mt-1 text-sm text-muted-foreground">調査・編集・生成・公開・人手レビューの次の予定をまとめて確認します。</p>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
          <RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> 更新
        </Button>
      </div>
      <Card size="sm">
        <CardHeader className="border-b border-border/70">
          <CardTitle className="flex items-center gap-1.5 text-sm"><CalendarDays className="size-4 text-primary" /> MediaOpsの予定</CardTitle>
          <CardDescription>{total}件 · 日付はMediaOpsの保存済み予定と監査時刻から生成されます。</CardDescription>
        </CardHeader>
        <CardContent className="pt-3">
          {error ? <div role="alert" className="mb-3 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{readableError(error)}</div> : null}
          {loading && !events.length ? (
            <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div>
          ) : events.length ? (
            <ol className="space-y-2">{events.map((event) => <EventRow key={`${event.source}-${event.id}`} event={event} />)}</ol>
          ) : (
            <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-muted-foreground">予定はまだありません。ResearchRoutineまたはContentVariantに予定を追加すると、ここに表示されます。</div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

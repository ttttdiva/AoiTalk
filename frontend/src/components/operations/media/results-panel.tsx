"use client";

import { BarChart3, CircleDollarSign, FlaskConical, Loader2, RefreshCw, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import {
  mediaOverviewApi,
  type MediaResultsResponse,
} from "@/lib/media-operations-overview-api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

function readableError(error: unknown): string {
  return error instanceof Error && error.message
    ? error.message
    : "Resultsの読み込みに失敗しました";
}

function number(value: number): string {
  return new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 2 }).format(value);
}

function statusLabel(value: string): string {
  const labels: Record<string, string> = {
    draft: "下書き",
    running: "実行中",
    completed: "完了",
    inconclusive: "判定保留",
    cancelled: "キャンセル",
    pending_review: "レビュー待ち",
    accepted: "採用",
    rejected: "却下",
    stale: "期限切れ",
  };
  return labels[value] ?? value;
}

function StatCard({ label, value, icon: Icon }: { label: string; value: string; icon: typeof BarChart3 }) {
  return (
    <Card size="sm">
      <CardContent className="flex items-center gap-3 pt-4">
        <span className="rounded-md border border-primary/20 bg-primary/10 p-2 text-primary"><Icon className="size-4" /></span>
        <div><p className="text-xs text-muted-foreground">{label}</p><p className="mt-0.5 text-xl font-semibold tabular-nums">{value}</p></div>
      </CardContent>
    </Card>
  );
}

export function MediaResultsPanel() {
  const [result, setResult] = useState<MediaResultsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setResult(await mediaOverviewApi.getResults());
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
    <div className="space-y-4" data-testid="media-results-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight">結果</h2>
          <p className="mt-1 text-sm text-muted-foreground">Persona・媒体・実験・Revenueの保存済み結果を、証拠の件数とともに確認します。</p>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={() => void load()} disabled={loading}><RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> 更新</Button>
      </div>
      {error ? <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{readableError(error)}</div> : null}
      {loading && !result ? (
        <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div>
      ) : result ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard label="メトリクス観測" value={number(result.metric_snapshots.count)} icon={BarChart3} />
            <StatCard label="売上イベント" value={number(result.revenue.event_count)} icon={CircleDollarSign} />
            <StatCard label="実験" value={`${number(result.experiments.count)}（結果 ${number(result.experiments.result_count)}）`} icon={FlaskConical} />
            <StatCard label="レビュー待ち" value={number(result.learning.pending_review_count)} icon={ShieldCheck} />
          </div>
          <div className="grid gap-4 xl:grid-cols-4">
            <Card size="sm">
              <CardHeader className="border-b border-border/70"><CardTitle className="text-sm">媒体別メトリクス</CardTitle><CardDescription>正規化された値のみを集計しています。Providerの生レスポンスは表示しません。</CardDescription></CardHeader>
              <CardContent className="space-y-2 pt-3">
                {result.metric_snapshots.by_platform.length ? result.metric_snapshots.by_platform.map((item) => <div key={item.platform} className="rounded-md border border-border/70 px-3 py-2"><div className="flex items-center justify-between gap-2"><span className="font-medium">{item.platform}</span><span className="text-xs text-muted-foreground">{item.snapshot_count}件</span></div><p className="mt-1 text-xs text-muted-foreground">{Object.entries(item.metrics).map(([key, value]) => `${key}: ${number(value)}`).join(" · ") || "値なし"}</p></div>) : <p className="text-sm text-muted-foreground">Metric snapshotはまだありません。</p>}
              </CardContent>
            </Card>
            <Card size="sm">
              <CardHeader className="border-b border-border/70"><CardTitle className="text-sm">アカウント / コンテンツ</CardTitle><CardDescription>アカウントとContentVariant単位の集計です。</CardDescription></CardHeader>
              <CardContent className="space-y-2 pt-3">
                {result.metric_snapshots.by_account.map((item) => <div key={`account-${item.account_ref}`} className="rounded-md border border-border/70 px-3 py-2"><div className="flex items-center justify-between gap-2"><span className="font-medium">{item.account_label || "アカウント"}</span><span className="text-xs text-muted-foreground">{item.snapshot_count}件</span></div><p className="mt-1 text-xs text-muted-foreground">{Object.entries(item.metrics).map(([key, value]) => `${key}: ${number(value)}`).join(" · ") || "値なし"}</p></div>)}
                {result.metric_snapshots.by_content.map((item) => <div key={`content-${item.content_ref}`} className="rounded-md border border-border/70 px-3 py-2"><div className="flex items-center justify-between gap-2"><span className="font-medium">{item.content_label || "コンテンツ"}</span><span className="text-xs text-muted-foreground">{item.snapshot_count}件</span></div><p className="mt-1 text-xs text-muted-foreground">{Object.entries(item.metrics).map(([key, value]) => `${key}: ${number(value)}`).join(" · ") || "値なし"}</p></div>)}
                {!result.metric_snapshots.by_account.length && !result.metric_snapshots.by_content.length ? <p className="text-sm text-muted-foreground">アカウント / コンテンツ別のMetric snapshotはまだありません。</p> : null}
              </CardContent>
            </Card>
            <Card size="sm">
              <CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Persona別メトリクス</CardTitle><CardDescription>Persona名を優先表示し、参照可能な証拠だけを集計します。</CardDescription></CardHeader>
              <CardContent className="space-y-2 pt-3">
                {result.metric_snapshots.by_persona.length ? result.metric_snapshots.by_persona.map((item) => <div key={item.persona_ref} className="rounded-md border border-border/70 px-3 py-2"><div className="flex items-center justify-between gap-2"><span className="font-medium">{item.persona_label || "Persona"}</span><span className="text-xs text-muted-foreground">{item.snapshot_count}件</span></div><p className="mt-1 text-xs text-muted-foreground">{Object.entries(item.metrics).map(([key, value]) => `${key}: ${number(value)}`).join(" · ") || "値なし"}</p></div>) : <p className="text-sm text-muted-foreground">Persona別のMetric snapshotはまだありません。</p>}
              </CardContent>
            </Card>
            <Card size="sm">
              <CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Revenue / 学習</CardTitle><CardDescription>通貨別の売上集計と、提案された学習のレビュー状態です。</CardDescription></CardHeader>
              <CardContent className="space-y-3 pt-3">
                {result.revenue.by_currency.length ? result.revenue.by_currency.map((item) => <div key={item.currency} className="flex items-center justify-between rounded-md border border-border/70 px-3 py-2 text-sm"><span>{item.currency} <span className="text-xs text-muted-foreground">{item.event_count}件</span></span><span className="tabular-nums">Net {number(item.net)} / Gross {number(item.gross)}</span></div>) : <p className="text-sm text-muted-foreground">Revenue eventはまだありません。</p>}
                <div className="rounded-md border border-border/70 bg-muted/20 px-3 py-2"><p className="text-xs font-medium">Experimentの状態</p><p className="mt-1 text-xs text-muted-foreground">{Object.entries(result.experiments.by_status).map(([key, value]) => `${statusLabel(key)}: ${value}`).join(" · ") || "記録なし"}</p></div>
                <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2"><p className="text-xs font-medium">学習提案</p><p className="mt-1 text-xs text-muted-foreground">{Object.entries(result.learning.by_status).map(([key, value]) => `${statusLabel(key)}: ${value}`).join(" · ") || "記録なし"}</p></div>
                <p className="text-[11px] text-muted-foreground">参照可能なEvidence: {number(result.evidence_count)}件</p>
              </CardContent>
            </Card>
          </div>
        </>
      ) : null}
    </div>
  );
}

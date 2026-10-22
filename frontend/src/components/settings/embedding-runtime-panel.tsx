"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { useEmbeddingRuntime, type EmbeddingRuntimeStatus } from "@/hooks/use-embedding-runtime";

const LABELS: Record<EmbeddingRuntimeStatus["state"], string> = {
  unloaded: "未ロード", loading: "読み込み中", loaded: "待機中", busy: "処理中",
  releasing: "解放中", failed: "エラー", closed: "終了",
};

export function EmbeddingRuntimePanel() {
  const { status, loaded, pending, error, message, refresh, release } = useEmbeddingRuntime();
  const busy = Boolean(status && (status.pending_requests > 0 || ["busy", "loading", "releasing", "closed"].includes(status.state)));
  const canRelease = loaded && status?.process_running && !busy && !pending && !error;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2 text-sm">
          <span className="inline-flex items-center gap-1">
            EmbeddingGemma 2
            <HelpPopover label="EmbeddingGemma 2">
              <p>検索・索引作成で共有する埋め込みモデルです。状態確認ではモデルを読み込みません。</p>
              <p>解放後は次の推論で再ロードします。処理中・待機中のリクエストは中断しません。</p>
            </HelpPopover>
          </span>
          <Badge variant={status?.state === "failed" ? "destructive" : "secondary"}>
            {loaded ? status ? LABELS[status.state] : "未起動" : "確認中"}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        {status && <dl className="grid grid-cols-2 gap-2">
          <dt>実行 / 待機リクエスト</dt><dd>{status.active_requests} / {status.queued_requests}</dd>
          <dt>配置 / 精度</dt><dd>{status.effective_device ?? status.configured_device} / {status.precision ?? "未ロード"}</dd>
          <dt>アイドル自動解放</dt><dd>{status.idle_timeout_seconds === 0 ? "OFF" : `${status.idle_timeout_seconds} 秒`}</dd>
          <dt>Worker PID</dt><dd>{status.pid ?? "なし"}</dd>
        </dl>}
        {status?.metrics_source === "last_worker_response" && typeof status.metrics.rss_bytes === "number" && (
          <p className="text-muted-foreground">メモリ: {(status.metrics.rss_bytes / 1024 / 1024).toFixed(1)} MiB（直近の推論応答時点）</p>
        )}
        {status?.last_error && <p role="alert" className="break-words text-destructive">{status.last_error}</p>}
        {error && <p role="alert" className="text-destructive">{error}</p>}
        {message && <p role="status">{message}</p>}
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="outline" disabled={Boolean(pending)} onClick={() => void refresh()}>状態を更新</Button>
          <Button type="button" size="sm" variant="outline" disabled={!canRelease} onClick={() => void release()}>
            {pending === "release" ? "解放中…" : "メモリを解放"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { useEmbeddingRuntime, type EmbeddingRuntimeStatus } from "@/hooks/use-embedding-runtime";
import { embeddingMasterPhase } from "@/lib/embedding-master-setting";
import { cn } from "@/lib/utils";

const LABELS: Record<EmbeddingRuntimeStatus["state"], string> = {
  unloaded: "未ロード", loading: "読み込み中", loaded: "待機中", busy: "処理中",
  releasing: "解放中", failed: "エラー", closed: "終了",
};

export function EmbeddingRuntimePanel({ isAdmin = false }: { isAdmin?: boolean } = {}) {
  const canManage = isAdmin === true;
  const {
    status, loaded, pending, error, message, refresh, release,
    enabled, settingsError, saving, saveError, saveMessage, setEnabled,
  } = useEmbeddingRuntime({ canManage });
  const busy = Boolean(status && (status.pending_requests > 0 || ["busy", "loading", "releasing", "closed"].includes(status.state)));
  const canRelease = loaded && status?.process_running && !busy && !pending && !error;
  const phase = embeddingMasterPhase(enabled, status, loaded);
  const switchLabel = enabled === null ? "確認中" : enabled ? "ON" : "OFF";
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2 text-sm">
          <span className="inline-flex items-center gap-1">
            EmbeddingGemma 2
            <HelpPopover label="EmbeddingGemma 2">
              <p>検索・索引作成で共有する埋め込みモデルです。状態確認ではモデルを読み込みません。</p>
              <p>解放後は、意味検索・索引作成が ON のとき次の推論で再ロードします。処理中・待機中のリクエストは中断しません。</p>
            </HelpPopover>
          </span>
          {canManage && (
            <Badge variant={status?.state === "failed" ? "destructive" : "secondary"}>
              {loaded ? status ? LABELS[status.state] : "未起動" : "確認中"}
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        <div className="flex items-center justify-between gap-2 rounded-md border px-3 py-2">
          <span className="inline-flex items-center gap-1 font-medium">
            意味検索・索引作成
            <HelpPopover label="意味検索・索引作成">
              <p>ON: 検索と索引作成で意味検索（埋め込み）を使います。モデルは必要になった時点で読み込みます。</p>
              <p>OFF: 新しい意味検索・索引作成の受付を止めます。進行中の推論は完了を待ちます。OFF中の索引更新は保留し、ONに戻すと再開します。OFF の間もキーワード（字句）検索と、Docs・タスク・ファイルなどの通常操作は使えます。</p>
              <p>他の用途が GPU を使用している間は、ON にしても索引処理は待機することがあります。</p>
              <p>下の「メモリを解放」は一度だけ解放する操作で、この設定とは別です。ON のままなら次の推論で再読み込みします。</p>
            </HelpPopover>
          </span>
          <span className="inline-flex items-center gap-2">
            <span className="text-muted-foreground">{saving ? "保存中…" : switchLabel}</span>
            <button
              type="button"
              role="switch"
              aria-checked={enabled === null ? "mixed" : enabled}
              aria-label="意味検索・索引作成"
              disabled={!canManage || enabled === null || saving}
              onClick={() => { if (canManage && enabled !== null) void setEnabled(!enabled); }}
              className={cn(
                "relative h-5 w-9 shrink-0 rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
                enabled ? "bg-primary" : "bg-muted-foreground/35",
              )}
            >
              <span className={cn(
                "absolute top-0.5 size-4 rounded-full bg-background shadow transition-transform",
                enabled ? "left-[18px]" : "left-0.5",
              )} />
            </button>
          </span>
        </div>
        {!canManage && <p role="status" className="text-muted-foreground">この設定の変更には管理者権限が必要です。</p>}
        {settingsError && <p role="alert" className="text-destructive">{settingsError}</p>}
        {canManage && saveError && <p role="alert" className="text-destructive">{saveError}</p>}
        {canManage && saveMessage && <p role="status">{saveMessage}</p>}
        {canManage && phase === "off_checking" && <p role="status">OFF: モデルの状態を確認しています。</p>}
        {canManage && phase === "off_draining" && status && (
          <p role="status">OFF: 受付済みの推論が完了するまで待っています（実行 {status.active_requests} / 待機 {status.queued_requests}）。完了後にモデルを解放します。保留中の索引更新はONに戻すと再開します。</p>
        )}
        {canManage && phase === "off_unloading" && <p role="status">OFF: モデルの解放を待っています。解放を確認できるまで、読み込み済みの状態が表示されます。</p>}
        {canManage && phase === "off_released" && <p role="status">OFF: モデルは解放済みです。意味検索と索引作成は停止し、キーワード（字句）検索のみ動作します。</p>}
        {canManage && status && <dl className="grid grid-cols-2 gap-2">
          <dt>実行 / 待機リクエスト</dt><dd>{status.active_requests} / {status.queued_requests}</dd>
          <dt>配置 / 精度</dt><dd>{status.effective_device ?? status.configured_device} / {status.precision ?? "未ロード"}</dd>
          <dt>アイドル自動解放</dt><dd>{status.idle_timeout_seconds === 0 ? "OFF" : `${status.idle_timeout_seconds} 秒`}</dd>
          <dt>Worker PID</dt><dd>{status.pid ?? "なし"}</dd>
        </dl>}
        {canManage && status?.metrics_source === "last_worker_response" && typeof status.metrics.rss_bytes === "number" && (
          <p className="text-muted-foreground">メモリ: {(status.metrics.rss_bytes / 1024 / 1024).toFixed(1)} MiB（直近の推論応答時点）</p>
        )}
        {canManage && status?.last_error && <p role="alert" className="break-words text-destructive">{status.last_error}</p>}
        {canManage && error && <p role="alert" className="text-destructive">{error}</p>}
        {canManage && message && <p role="status">{message}</p>}
        {canManage && (
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" variant="outline" disabled={Boolean(pending) || saving} onClick={() => void refresh()}>状態を更新</Button>
            <Button type="button" size="sm" variant="outline" disabled={!canRelease} onClick={() => void release()}>
              {pending === "release" ? "解放中…" : "メモリを解放"}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

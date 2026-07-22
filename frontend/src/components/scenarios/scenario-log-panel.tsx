"use client";

import { useState, useEffect, useCallback } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Loader2, Trash2 } from "lucide-react";
import { chatApi, type ScenarioLogEntry } from "@/lib/chat-api";
import { pyFetch } from "@/lib/scenarios-page-utils";
import { useConfirm } from "@/hooks/use-confirm";

function ScenarioLogPanel({ scenarioId }: { scenarioId: string }) {
  const confirm = useConfirm();
  const [logs, setLogs] = useState<ScenarioLogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [deletingLogId, setDeletingLogId] = useState<string | null>(null);

  const loadLogs = useCallback(async () => {
    setLoading(true);
    try {
      const data = await chatApi.getScenarioLogs(scenarioId);
      setLogs(data.logs ?? []);
    } catch (err) {
      console.error("シナリオログ取得失敗:", err);
      setLogs([]);
    } finally {
      setLoading(false);
    }
  }, [scenarioId]);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  const openLog = (log: ScenarioLogEntry) => {
    if (!log.href) return;
    window.location.href = log.href;
  };

  const deleteTRPGLog = useCallback(
    async (log: ScenarioLogEntry) => {
      const roomId = log.room_id || log.id;
      if (!roomId) return;
      if (
        !(await confirm({
          description: `TRPGセッション「${log.title || log.target_label}」を削除しますか？`,
          destructive: true,
        }))
      ) {
        return;
      }

      setDeletingLogId(log.id);
      try {
        await pyFetch<{ ok: boolean }>(`/api/trpg/rooms/${roomId}`, {
          method: "DELETE",
        });
        await loadLogs();
      } catch (err) {
        console.error("TRPGセッション削除失敗:", err);
        alert("TRPGセッションの削除に失敗しました。ホストまたは管理者のみ削除できます。");
      } finally {
        setDeletingLogId(null);
      }
    },
    [loadLogs, confirm],
  );

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold">ログ</h3>
          <p className="text-xs text-muted-foreground">
            このシナリオに紐づく執筆、ロールプレイ、TRPGの履歴です。
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={loadLogs} disabled={loading}>
          {loading && <Loader2 className="mr-1 size-3.5 animate-spin" />}
          更新
        </Button>
      </div>

      {loading && logs.length === 0 ? (
        <div className="rounded border px-4 py-8 text-center text-sm text-muted-foreground">
          読み込み中...
        </div>
      ) : logs.length === 0 ? (
        <div className="rounded border px-4 py-8 text-center text-sm text-muted-foreground">
          ログはまだありません
        </div>
      ) : (
        <div className="overflow-hidden rounded border">
          <div className="grid grid-cols-[7rem_minmax(0,1fr)_7rem_4rem_7rem] gap-2 border-b bg-muted/40 px-3 py-2 text-xs font-medium text-muted-foreground">
            <span>種別</span>
            <span>対象</span>
            <span>最終更新</span>
            <span className="text-right">件数</span>
            <span className="text-right">操作</span>
          </div>
          <div className="divide-y">
            {logs.map((log) => (
              <div
                key={`${log.type}:${log.id}`}
                className="grid grid-cols-[7rem_minmax(0,1fr)_7rem_4rem_7rem] items-center gap-2 px-3 py-2 text-sm"
              >
                <div>
                  <Badge variant={log.type === "trpg" ? "default" : "secondary"}>
                    {log.type_label}
                  </Badge>
                </div>
                <div className="min-w-0">
                  <p className="truncate font-medium">
                    {log.target_label || log.title}
                  </p>
                  {log.title && log.title !== log.target_label && (
                    <p className="truncate text-xs text-muted-foreground">
                      {log.title}
                    </p>
                  )}
                </div>
                <span className="text-xs text-muted-foreground">
                  {log.updated_at
                    ? new Date(log.updated_at).toLocaleDateString("ja-JP", {
                        month: "numeric",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })
                    : "-"}
                </span>
                <span className="text-right text-xs text-muted-foreground">
                  {log.count}件
                </span>
                <div className="flex justify-end gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => openLog(log)}
                    disabled={!log.href}
                  >
                    開く
                  </Button>
                  {log.type === "trpg" && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-destructive hover:text-destructive"
                      disabled={deletingLogId === log.id}
                      onClick={() => void deleteTRPGLog(log)}
                    >
                      {deletingLogId === log.id ? (
                        <Loader2 className="mr-1 size-3.5 animate-spin" />
                      ) : (
                        <Trash2 className="mr-1 size-3.5" />
                      )}
                      削除
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export { ScenarioLogPanel };

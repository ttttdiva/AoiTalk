"use client";

import { useState, useEffect, useCallback } from "react";
import { useExplorer } from "@/contexts/explorer-context";
import {
  gitStatus,
  gitCommit,
  gitLog,
  type GitStatusResponse,
  type GitLogEntry,
} from "@/lib/explorer-api";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { GitBranch, Check, Clock } from "lucide-react";

interface GitPanelProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function GitPanel({ open, onOpenChange }: GitPanelProps) {
  const { storageCtx } = useExplorer();
  const [status, setStatus] = useState<GitStatusResponse | null>(null);
  const [logs, setLogs] = useState<GitLogEntry[]>([]);
  const [commitMsg, setCommitMsg] = useState("");
  const [committing, setCommitting] = useState(false);
  const [commitResult, setCommitResult] = useState<string | null>(null);

  const ctxType = storageCtx?.type || "personal";
  const ctxId = storageCtx?.id || undefined;

  const fetchStatus = useCallback(async () => {
    try {
      const data = await gitStatus(ctxType, ctxId);
      setStatus(data);
    } catch {
      setStatus(null);
    }
  }, [ctxType, ctxId]);

  const fetchLogs = useCallback(async () => {
    try {
      const data = await gitLog(ctxType, ctxId);
      setLogs(data.commits);
    } catch {
      setLogs([]);
    }
  }, [ctxType, ctxId]);

  useEffect(() => {
    if (open) {
      fetchStatus();
      fetchLogs();
    }
  }, [open, fetchStatus, fetchLogs]);

  const handleCommit = async () => {
    if (!commitMsg.trim()) return;
    setCommitting(true);
    setCommitResult(null);
    try {
      const res = await gitCommit(commitMsg.trim(), ctxType, ctxId);
      setCommitResult(
        res.success
          ? `コミット成功: ${res.commit_hash || ""}`
          : `エラー: ${res.message}`
      );
      setCommitMsg("");
      fetchStatus();
      fetchLogs();
    } catch {
      setCommitResult("コミットに失敗しました");
    } finally {
      setCommitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <GitBranch className="size-4" />
            Git
          </DialogTitle>
        </DialogHeader>

        <Tabs defaultValue="status">
          <TabsList>
            <TabsTrigger value="status">ステータス</TabsTrigger>
            <TabsTrigger value="commit">コミット</TabsTrigger>
            <TabsTrigger value="log">履歴</TabsTrigger>
          </TabsList>

          <TabsContent value="status">
            <div className="max-h-60 overflow-auto">
              {status?.clean ? (
                <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
                  <Check className="size-4 text-green-500" />
                  変更はありません
                </div>
              ) : status?.changes ? (
                <div className="space-y-1 py-2">
                  {status.changes.map((c) => (
                    <div
                      key={c.path}
                      className="flex items-center gap-2 text-xs"
                    >
                      <span className="w-6 shrink-0 text-center font-mono text-muted-foreground">
                        {c.status}
                      </span>
                      <span className="truncate">{c.path}</span>
                    </div>
                  ))}
                  <div className="pt-1 text-[10px] text-muted-foreground">
                    {status.total_changes}件の変更
                  </div>
                </div>
              ) : (
                <div className="py-4 text-xs text-muted-foreground">
                  ステータスを取得できません
                </div>
              )}
            </div>
          </TabsContent>

          <TabsContent value="commit">
            <div className="space-y-2 py-2">
              <Input
                placeholder="コミットメッセージ"
                value={commitMsg}
                onChange={(e) => setCommitMsg(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleCommit();
                }}
              />
              <Button
                onClick={handleCommit}
                disabled={!commitMsg.trim() || committing}
                className="w-full"
                size="sm"
              >
                {committing ? "コミット中..." : "コミット"}
              </Button>
              {commitResult && (
                <div className="text-xs text-muted-foreground">{commitResult}</div>
              )}
            </div>
          </TabsContent>

          <TabsContent value="log">
            <div className="max-h-60 overflow-auto">
              {logs.length === 0 ? (
                <div className="py-4 text-xs text-muted-foreground">
                  コミット履歴がありません
                </div>
              ) : (
                <div className="space-y-1 py-2">
                  {logs.map((entry) => (
                    <div key={entry.hash} className="rounded-md border p-2">
                      <div className="flex items-start gap-1.5">
                        <Clock className="mt-0.5 size-3 shrink-0 text-muted-foreground" />
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-xs font-medium">
                            {entry.message}
                          </div>
                          <div className="flex gap-2 text-[10px] text-muted-foreground">
                            <span className="font-mono">{entry.short_hash}</span>
                            <span>{entry.author}</span>
                            <span>
                              {new Date(entry.date).toLocaleDateString("ja-JP")}
                            </span>
                          </div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

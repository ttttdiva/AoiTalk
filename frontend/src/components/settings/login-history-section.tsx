"use client";

import { useState, useCallback } from "react";
import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { History, ChevronDown, ChevronUp, Trash2, Loader2 } from "lucide-react";
import { useConfirm } from "@/hooks/use-confirm";

interface LoginLog {
  id: string;
  username: string;
  action: string;
  ip_address: string | null;
  user_agent: string | null;
  success: boolean;
  failure_reason: string | null;
  session_duration_seconds: number | null;
  created_at: string;
}

async function pyFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
  return res.json();
}

interface LoginHistoryData {
  logs: LoginLog[];
  total_count: number;
}

const EMPTY_LOGIN_HISTORY: LoginHistoryData = { logs: [], total_count: 0 };

export function LoginHistorySection({ isAdmin }: { isAdmin: boolean }) {
  const confirm = useConfirm();
  // ログイン履歴（サーバー状態）は SWR で管理。取得タイミングは従来どおり
  // 呼び出し側（トグル/更新）で駆動するため自動 revalidation は無効化する。
  const { data = EMPTY_LOGIN_HISTORY, mutate: mutateLogs } = useSWR<LoginHistoryData>(
    isAdmin ? "settings/login-history" : null,
    async () => {
      try {
        return await pyFetch<LoginHistoryData>("/auth/login-history?limit=50");
      } catch {
        return EMPTY_LOGIN_HISTORY;
      }
    },
    {
      revalidateOnMount: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      keepPreviousData: true,
      dedupingInterval: 0,
    },
  );
  const logs = data.logs;
  const totalCount = data.total_count;
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    try {
      await mutateLogs();
    } finally {
      setLoading(false);
    }
  }, [mutateLogs]);

  const handleToggle = useCallback(() => {
    if (!expanded && logs.length === 0) fetchLogs();
    setExpanded((v) => !v);
  }, [expanded, logs.length, fetchLogs]);

  const handleClear = useCallback(async () => {
    if (
      !(await confirm({
        description: "ログイン履歴をすべて削除しますか？",
        destructive: true,
      }))
    )
      return;
    setClearing(true);
    try {
      await pyFetch("/auth/login-history/clear", { method: "DELETE" });
      // 楽観的更新：クリア成功後は再取得せずローカルキャッシュを空にする。
      await mutateLogs(EMPTY_LOGIN_HISTORY, { revalidate: false });
    } catch {
      // ignore
    } finally {
      setClearing(false);
    }
  }, [confirm, mutateLogs]);

  const formatDate = (iso: string) => {
    try {
      return new Date(iso).toLocaleString("ja-JP", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return iso;
    }
  };

  if (!isAdmin) return null;

  return (
    <Card size="sm" className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={handleToggle}
      >
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <History className="size-4" />
            ログイン履歴
            {totalCount > 0 && (
              <Badge variant="secondary" className="text-[10px]">
                {totalCount}件
              </Badge>
            )}
          </span>
          {expanded ? (
            <ChevronUp className="size-4" />
          ) : (
            <ChevronDown className="size-4" />
          )}
        </CardTitle>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-3">
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              取得中...
            </div>
          ) : logs.length === 0 ? (
            <p className="text-sm text-muted-foreground">履歴がありません</p>
          ) : (
            <>
              <div className="max-h-64 overflow-auto rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-28">日時</TableHead>
                      <TableHead className="w-20">ユーザー</TableHead>
                      <TableHead className="w-16">操作</TableHead>
                      <TableHead className="w-12">結果</TableHead>
                      <TableHead>IP</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {logs.map((log) => (
                      <TableRow key={log.id}>
                        <TableCell className="text-xs">
                          {formatDate(log.created_at)}
                        </TableCell>
                        <TableCell className="text-xs">{log.username}</TableCell>
                        <TableCell className="text-xs">
                          {log.action === "login" ? "ログイン" : "ログアウト"}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={log.success ? "default" : "destructive"}
                            className="text-[10px]"
                          >
                            {log.success ? "成功" : "失敗"}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {log.ip_address || "-"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              <div className="flex items-center justify-between">
                <Button variant="outline" size="sm" onClick={fetchLogs}>
                  更新
                </Button>
                {isAdmin && (
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={handleClear}
                    disabled={clearing}
                  >
                    {clearing ? (
                      <Loader2 className="size-3 animate-spin mr-1" />
                    ) : (
                      <Trash2 className="size-3 mr-1" />
                    )}
                    履歴クリア
                  </Button>
                )}
              </div>
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}

"use client";

import { useState, useCallback } from "react";
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

export function LoginHistorySection({ isAdmin }: { isAdmin: boolean }) {
  const [logs, setLogs] = useState<LoginLog[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    try {
      const data = await pyFetch<{ logs: LoginLog[]; total_count: number }>(
        "/auth/login-history?limit=50"
      );
      setLogs(data.logs);
      setTotalCount(data.total_count);
    } catch {
      setLogs([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleToggle = useCallback(() => {
    if (!expanded && logs.length === 0) fetchLogs();
    setExpanded((v) => !v);
  }, [expanded, logs.length, fetchLogs]);

  const handleClear = useCallback(async () => {
    if (!window.confirm("ログイン履歴をすべて削除しますか？")) return;
    setClearing(true);
    try {
      await pyFetch("/auth/login-history/clear", { method: "DELETE" });
      setLogs([]);
      setTotalCount(0);
    } catch {
      // ignore
    } finally {
      setClearing(false);
    }
  }, []);

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

  return (
    <Card size="sm">
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

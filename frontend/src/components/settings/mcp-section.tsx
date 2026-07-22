"use client";

import { useState, useCallback } from "react";
import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Plug,
  ChevronDown,
  ChevronUp,
  Loader2,
  RefreshCw,
  Wrench,
} from "lucide-react";

interface McpTool {
  name: string;
  description: string;
}

interface McpServer {
  name: string;
  command: string;
  args: string[];
  status: string;
  tools: McpTool[];
  error?: string;
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

const STATUS_STYLES: Record<string, { label: string; variant: "default" | "secondary" | "destructive" }> = {
  running: { label: "実行中", variant: "default" },
  stopped: { label: "停止", variant: "secondary" },
  error: { label: "エラー", variant: "destructive" },
};

export function McpSection() {
  // MCPサーバー一覧（サーバー状態）は SWR で管理。取得タイミングは従来どおり
  // 呼び出し側（トグル/更新/再起動後）で駆動するため自動 revalidation は無効化する。
  const { data: servers = [], mutate: mutateServers } = useSWR<McpServer[]>(
    "settings/mcp-servers",
    async () => {
      try {
        return (await pyFetch<{ servers: McpServer[] }>("/mcp/servers")).servers || [];
      } catch {
        return [];
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
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [expandedServer, setExpandedServer] = useState<string | null>(null);
  const [restarting, setRestarting] = useState<string | null>(null);

  const runningCount = servers.filter((s) => s.status === "running").length;

  const fetchServers = useCallback(async () => {
    setLoading(true);
    try {
      await mutateServers();
    } finally {
      setLoading(false);
    }
  }, [mutateServers]);

  const handleToggle = useCallback(() => {
    if (!expanded && servers.length === 0) fetchServers();
    setExpanded((v) => !v);
  }, [expanded, servers.length, fetchServers]);

  const handleRestart = useCallback(
    async (serverName: string) => {
      setRestarting(serverName);
      try {
        await pyFetch(`/mcp/servers/${encodeURIComponent(serverName)}/restart`, {
          method: "POST",
        });
        // ステータス更新のため再取得
        setTimeout(() => fetchServers(), 1500);
      } catch {
        // ignore
      } finally {
        setRestarting(null);
      }
    },
    [fetchServers]
  );

  const toggleServerExpand = useCallback((name: string) => {
    setExpandedServer((prev) => (prev === name ? null : name));
  }, []);

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={handleToggle}
      >
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <Plug className="size-4" />
            MCPサーバー
            {servers.length > 0 && (
              <Badge variant="secondary" className="text-[10px]">
                {runningCount}/{servers.length} 稼働中
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
          {/* ヘルスサマリー */}
          {servers.length > 0 && (
            <div className="flex items-center gap-3 rounded-md bg-muted/50 p-2 text-xs">
              <span className="flex items-center gap-1.5">
                <span className="size-2 rounded-full bg-green-500" />
                稼働中: {runningCount}
              </span>
              <span className="flex items-center gap-1.5">
                <span className="size-2 rounded-full bg-gray-400" />
                停止: {servers.filter((s) => s.status === "stopped").length}
              </span>
              <span className="flex items-center gap-1.5">
                <span className="size-2 rounded-full bg-red-500" />
                エラー: {servers.filter((s) => s.status === "error").length}
              </span>
            </div>
          )}

          <div className="flex items-center">
            <Button variant="outline" size="sm" onClick={fetchServers}>
              更新
            </Button>
          </div>

          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              取得中...
            </div>
          ) : servers.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              MCPサーバーが登録されていません
            </p>
          ) : (
            <div className="max-h-96 space-y-2 overflow-auto">
              {servers.map((server) => {
                const statusInfo = STATUS_STYLES[server.status] || {
                  label: server.status || "不明",
                  variant: "secondary" as const,
                };
                const isExpanded = expandedServer === server.name;
                return (
                  <div
                    key={server.name}
                    className="rounded-md border"
                  >
                    <div
                      className="flex items-start justify-between p-2.5 cursor-pointer"
                      onClick={() => toggleServerExpand(server.name)}
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="text-sm font-medium">{server.name}</span>
                          <Badge variant={statusInfo.variant} className="text-[10px]">
                            {statusInfo.label}
                          </Badge>
                          {server.tools.length > 0 && (
                            <span className="text-[10px] text-muted-foreground">
                              {server.tools.length}ツール
                            </span>
                          )}
                        </div>
                        <p className="text-[10px] text-muted-foreground mt-0.5 font-mono">
                          {server.command} {server.args.join(" ")}
                        </p>
                        {server.error && (
                          <p className="text-xs text-destructive mt-0.5">
                            {server.error}
                          </p>
                        )}
                      </div>
                      <div className="flex gap-1 shrink-0">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleRestart(server.name);
                          }}
                          disabled={restarting === server.name}
                          title="再起動"
                        >
                          {restarting === server.name ? (
                            <Loader2 className="size-3 animate-spin" />
                          ) : (
                            <RefreshCw className="size-3" />
                          )}
                        </Button>
                        {isExpanded ? (
                          <ChevronUp className="size-4 mt-1.5 text-muted-foreground" />
                        ) : (
                          <ChevronDown className="size-4 mt-1.5 text-muted-foreground" />
                        )}
                      </div>
                    </div>

                    {/* ツール一覧 */}
                    {isExpanded && server.tools.length > 0 && (
                      <div className="border-t px-2.5 py-2 space-y-1.5">
                        <div className="flex items-center gap-1 text-[10px] text-muted-foreground font-medium">
                          <Wrench className="size-3" />
                          ツール一覧
                        </div>
                        <div className="space-y-1">
                          {server.tools.map((tool) => (
                            <div
                              key={tool.name}
                              className="rounded bg-muted/50 px-2 py-1"
                            >
                              <span className="text-xs font-medium font-mono">
                                {tool.name}
                              </span>
                              {tool.description && (
                                <p className="text-[10px] text-muted-foreground">
                                  {tool.description}
                                </p>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {isExpanded && server.tools.length === 0 && (
                      <div className="border-t px-2.5 py-2">
                        <p className="text-[10px] text-muted-foreground">
                          ツールが登録されていません
                        </p>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      )}
    </Card>
  );
}

"use client";

import { useState, useCallback } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";
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

export function McpSection({
  loading: settingsLoading,
  enabled,
  onToggle,
}: {
  loading: boolean;
  enabled: boolean | null;
  onToggle: (enabled: boolean) => void;
}) {
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
  const [serversLoading, setServersLoading] = useState(false);
  const [expandedServer, setExpandedServer] = useState<string | null>(null);
  const [restarting, setRestarting] = useState<string | null>(null);

  const runningCount = servers.filter((s) => s.status === "running").length;

  const fetchServers = useCallback(async () => {
    setServersLoading(true);
    try {
      await mutateServers();
    } finally {
      setServersLoading(false);
    }
  }, [mutateServers]);

  const handleOpenChange = useCallback(
    (open: boolean) => {
      if (open && servers.length === 0) void fetchServers();
    },
    [fetchServers, servers.length],
  );

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
    <SettingsDisclosure
      title="MCP"
      icon={<Plug className="size-4" />}
      targetId="mcp"
      summary={
        <span className="flex items-center gap-2">
          <Badge variant={enabled ? "default" : "secondary"}>
            {enabled === null ? "未取得" : enabled ? "ON" : "OFF"}
          </Badge>
          {servers.length > 0 && (
            <Badge variant="secondary" className="text-[10px]">
              {runningCount}/{servers.length} 稼働中
            </Badge>
          )}
        </span>
      }
      onOpenChange={handleOpenChange}
    >
      {settingsLoading ? (
        <Skeleton className="h-10 w-full rounded" />
      ) : (
        <div className="flex items-start justify-between gap-3 rounded border p-3">
          <div className="space-y-1">
            <Label htmlFor="mcp-enabled" className="text-sm font-medium">
              MCPを有効化
            </Label>
            <p className="text-xs text-muted-foreground">
              MCP連携と登録済みMCPサーバーの利用を切り替えます。
            </p>
          </div>
          <Checkbox
            id="mcp-enabled"
            checked={enabled === true}
            onCheckedChange={(checked) => onToggle(checked === true)}
          />
        </div>
      )}

      <div className="space-y-3 border-t pt-3">
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm font-medium">MCPサーバー</p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void fetchServers()}
            disabled={serversLoading}
          >
            {serversLoading && <Loader2 className="mr-1.5 size-3 animate-spin" />}
            更新
          </Button>
        </div>

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

        {serversLoading ? (
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
              const detailsId = `mcp-server-${encodeURIComponent(server.name)}-details`;
              return (
                <div key={server.name} className="rounded-md border">
                  <div className="flex items-start">
                    <button
                      type="button"
                      aria-label={`${server.name}の詳細を${isExpanded ? "閉じる" : "開く"}`}
                      aria-expanded={isExpanded}
                      aria-controls={detailsId}
                      className="flex min-w-0 flex-1 cursor-pointer items-start justify-between rounded-l-md p-2.5 pr-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
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
                        <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                          {server.command} {server.args.join(" ")}
                        </p>
                        {server.error && (
                          <p className="mt-0.5 text-xs text-destructive">{server.error}</p>
                        )}
                      </div>
                      {isExpanded ? (
                        <ChevronUp className="mt-1.5 size-4 shrink-0 text-muted-foreground" />
                      ) : (
                        <ChevronDown className="mt-1.5 size-4 shrink-0 text-muted-foreground" />
                      )}
                    </button>
                    <div className="shrink-0 p-2.5 pl-1">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          void handleRestart(server.name);
                        }}
                        disabled={restarting === server.name}
                        aria-label={`${server.name}を再起動`}
                        title="再起動"
                      >
                        {restarting === server.name ? (
                          <Loader2 className="size-3 animate-spin" />
                        ) : (
                          <RefreshCw className="size-3" />
                        )}
                      </Button>
                    </div>
                  </div>

                  {/* ツール一覧 */}
                  {isExpanded && server.tools.length > 0 && (
                    <div id={detailsId} className="space-y-1.5 border-t px-2.5 py-2">
                      <div className="flex items-center gap-1 text-[10px] font-medium text-muted-foreground">
                        <Wrench className="size-3" />
                        ツール一覧
                      </div>
                      <div className="space-y-1">
                        {server.tools.map((tool) => (
                          <div key={tool.name} className="rounded bg-muted/50 px-2 py-1">
                            <span className="font-mono text-xs font-medium">{tool.name}</span>
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
                    <div id={detailsId} className="border-t px-2.5 py-2">
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
      </div>
    </SettingsDisclosure>
  );
}

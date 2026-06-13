"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  Loader2,
  Plug,
  Plus,
  Server,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  createRemoteServer,
  deleteRemoteServer,
  listRemoteServers,
  testRemoteServer,
  type RemoteServerProfile,
} from "@/lib/remote-servers";

const DEFAULT_COLOR = "#3b82f6";

function statusBadge(status?: string | null) {
  if (status === "ok")
    return <Badge variant="secondary">接続OK</Badge>;
  if (status === "error")
    return <Badge variant="destructive">接続エラー</Badge>;
  return <Badge variant="outline">未確認</Badge>;
}

export function RemoteServerSection() {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [profiles, setProfiles] = useState<RemoteServerProfile[]>([]);
  const [creating, setCreating] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [authToken, setAuthToken] = useState("");
  const [color, setColor] = useState(DEFAULT_COLOR);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setProfiles(await listRemoteServers());
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "接続先の読み込みに失敗しました",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (expanded && profiles.length === 0) void load();
  }, [expanded, load, profiles.length]);

  const handleCreate = useCallback(async () => {
    if (!name.trim() || !baseUrl.trim()) {
      toast.error("名前とサーバーURLを入力してください");
      return;
    }
    setCreating(true);
    try {
      const created = await createRemoteServer({
        name: name.trim(),
        base_url: baseUrl.trim(),
        auth_token: authToken.trim() || null,
        display_color: color,
      });
      setProfiles((prev) => [created, ...prev]);
      setName("");
      setBaseUrl("");
      setAuthToken("");
      setColor(DEFAULT_COLOR);
      toast.success("接続先を追加しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "追加に失敗しました");
    } finally {
      setCreating(false);
    }
  }, [name, baseUrl, authToken, color]);

  const handleTest = useCallback(async (id: string) => {
    setTestingId(id);
    try {
      const result = await testRemoteServer(id);
      if (result.success) {
        const cap = result.capabilities;
        toast.success(
          `接続成功 (version: ${cap?.version ?? "?"}, profile: ${
            cap?.profile ?? "?"
          })`,
        );
      } else {
        toast.error(`接続失敗: ${result.error ?? "unknown"}`);
      }
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "接続テストに失敗しました");
    } finally {
      setTestingId(null);
    }
  }, [load]);

  const handleDelete = useCallback(async (id: string) => {
    setDeletingId(id);
    try {
      await deleteRemoteServer(id);
      setProfiles((prev) => prev.filter((p) => p.id !== id));
      toast.success("接続先を削除しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "削除に失敗しました");
    } finally {
      setDeletingId(null);
    }
  }, []);

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={() => setExpanded((v) => !v)}
      >
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <Server className="size-4" />
            外部AoiTalkサーバー接続
            {profiles.length > 0 ? (
              <span className="text-xs font-normal text-muted-foreground">
                {profiles.length} 件
              </span>
            ) : null}
          </span>
          {expanded ? (
            <ChevronUp className="size-4" />
          ) : (
            <ChevronDown className="size-4" />
          )}
        </CardTitle>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-4">
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              Loading...
            </div>
          ) : (
            <>
              <div className="space-y-2">
                {profiles.length === 0 ? (
                  <p className="text-sm text-muted-foreground">
                    接続先が登録されていません。会社版などの外部AoiTalkサーバーを追加できます。
                  </p>
                ) : (
                  profiles.map((p) => (
                    <div
                      key={p.id}
                      className="flex items-center justify-between gap-2 rounded-md border p-2"
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <span
                          className="size-3 shrink-0 rounded-full"
                          style={{ backgroundColor: p.display_color || DEFAULT_COLOR }}
                        />
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium">{p.name}</p>
                          <p className="truncate text-xs text-muted-foreground">
                            {p.base_url}
                          </p>
                        </div>
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        {statusBadge(p.last_status)}
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => handleTest(p.id)}
                          disabled={testingId === p.id}
                        >
                          {testingId === p.id ? (
                            <Loader2 className="mr-1 size-3 animate-spin" />
                          ) : (
                            <Plug className="mr-1 size-3" />
                          )}
                          テスト
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() => handleDelete(p.id)}
                          disabled={deletingId === p.id}
                        >
                          {deletingId === p.id ? (
                            <Loader2 className="size-3 animate-spin" />
                          ) : (
                            <Trash2 className="size-3" />
                          )}
                        </Button>
                      </div>
                    </div>
                  ))
                )}
              </div>

              <div className="space-y-2 rounded-md border border-dashed p-3">
                <p className="text-xs font-medium text-muted-foreground">
                  接続先を追加
                </p>
                <div className="space-y-2">
                  <div className="space-y-1">
                    <Label htmlFor="remote-name" className="text-xs">
                      名前
                    </Label>
                    <Input
                      id="remote-name"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="会社版AoiTalk"
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="remote-url" className="text-xs">
                      サーバーURL
                    </Label>
                    <Input
                      id="remote-url"
                      value={baseUrl}
                      onChange={(e) => setBaseUrl(e.target.value)}
                      placeholder="https://aoitalk.example.com"
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="remote-token" className="text-xs">
                      長期APIトークン
                    </Label>
                    <Input
                      id="remote-token"
                      type="password"
                      value={authToken}
                      onChange={(e) => setAuthToken(e.target.value)}
                      placeholder="aoitpat_..."
                      className="h-8"
                    />
                  </div>
                  <div className="flex items-center gap-2">
                    <Label htmlFor="remote-color" className="text-xs">
                      表示色
                    </Label>
                    <input
                      id="remote-color"
                      type="color"
                      value={color}
                      onChange={(e) => setColor(e.target.value)}
                      className="h-8 w-12 cursor-pointer rounded border bg-transparent"
                    />
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    onClick={handleCreate}
                    disabled={creating}
                  >
                    {creating ? (
                      <Loader2 className="mr-1 size-3 animate-spin" />
                    ) : (
                      <Plus className="mr-1 size-3" />
                    )}
                    追加
                  </Button>
                </div>
              </div>
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";
import {
  Loader2,
  Plug,
  Plus,
  Server,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
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
import { useUserSettings } from "@/contexts/user-settings-context";
import { getRemoteServerConnectionEnabled } from "@/lib/user-settings";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";

const DEFAULT_COLOR = "#3b82f6";

function statusBadge(status?: string | null) {
  if (status === "ok")
    return <Badge variant="secondary">接続OK</Badge>;
  if (status === "error")
    return <Badge variant="destructive">接続エラー</Badge>;
  return <Badge variant="outline">未確認</Badge>;
}

export function RemoteServerSection() {
  const { settings, patch } = useUserSettings();
  const remoteConnectionEnabled = getRemoteServerConnectionEnabled(settings);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  // 接続先一覧（サーバー状態）は SWR で管理。取得タイミングは従来どおり
  // 呼び出し側（展開/テスト後）で駆動するため自動 revalidation は無効化する。
  // 取得失敗時は従来同様に直前値を保持する。
  const profilesRef = useRef<RemoteServerProfile[]>([]);
  const { data: profiles = [], mutate: mutateProfiles } = useSWR<RemoteServerProfile[]>(
    "settings/remote-servers",
    async () => {
      try {
        return await listRemoteServers();
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : "接続先の読み込みに失敗しました",
        );
        return profilesRef.current;
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
  profilesRef.current = profiles;
  const [creating, setCreating] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [savingConnectionSetting, setSavingConnectionSetting] = useState(false);

  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [authToken, setAuthToken] = useState("");
  const [color, setColor] = useState(DEFAULT_COLOR);

  const handleConnectionSettingChange = useCallback(
    async (checked: boolean) => {
      setSavingConnectionSetting(true);
      try {
        await patch({ remote_server_connection_enabled: checked });
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : "接続設定の保存に失敗しました",
        );
      } finally {
        setSavingConnectionSetting(false);
      }
    },
    [patch],
  );

  const load = useCallback(async () => {
    setLoading(true);
    try {
      await mutateProfiles();
    } finally {
      setLoading(false);
    }
  }, [mutateProfiles]);

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
      // 楽観的更新：作成成功後は再取得せずローカルキャッシュへ追加する。
      await mutateProfiles((prev = []) => [created, ...prev], { revalidate: false });
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
  }, [name, baseUrl, authToken, color, mutateProfiles]);

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
      // 楽観的更新：削除成功後は再取得せずローカルキャッシュから除去する。
      await mutateProfiles((prev = []) => prev.filter((p) => p.id !== id), {
        revalidate: false,
      });
      toast.success("接続先を削除しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "削除に失敗しました");
    } finally {
      setDeletingId(null);
    }
  }, [mutateProfiles]);

  return (
    <SettingsDisclosure
      title="外部AoiTalkサーバー接続"
      icon={<Server className="size-4" />}
      summary={
        profiles.length > 0 ? (
          <span className="text-xs font-normal text-muted-foreground">
            {profiles.length} 件
          </span>
        ) : undefined
      }
      onOpenChange={setExpanded}
      contentClassName="space-y-4"
    >
        <label className="flex cursor-pointer items-start gap-3 rounded-md border p-3">
          <input
            type="checkbox"
            className="mt-0.5 size-4 accent-primary"
            checked={remoteConnectionEnabled}
            onChange={(event) => void handleConnectionSettingChange(event.target.checked)}
            disabled={savingConnectionSetting}
          />
          <span className="space-y-1">
            <span className="block text-sm font-medium">Enterpriseサーバーへの自動接続</span>
            <span className="block text-xs text-muted-foreground">
              ONのときだけ、登録済みの接続先からSpace・Project・Docsなどを取得します。
              OFFでも接続先の登録と手動テストは利用できます。
            </span>
          </span>
        </label>
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
    </SettingsDisclosure>
  );
}

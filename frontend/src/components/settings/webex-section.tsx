"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import {
  ChevronDown,
  ChevronUp,
  Loader2,
  MessageCircle,
  RefreshCw,
  Search,
  Unplug,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  disconnectWebex,
  getWebexSettings,
  listWebexSpaces,
  startWebexConnect,
  updateWebexSpaces,
  type WebexSettings,
  type WebexSpace,
} from "@/lib/webex-integration";

export function WebexSection() {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [spacesLoading, setSpacesLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [spaces, setSpaces] = useState<WebexSpace[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const popupRef = useRef<Window | null>(null);
  const settingsRef = useRef<WebexSettings | null>(null);

  const { data: settings = null, mutate } = useSWR<WebexSettings | null>(
    "settings/webex",
    async () => {
      try {
        return await getWebexSettings();
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : "Webex設定を取得できませんでした",
        );
        return settingsRef.current;
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
  settingsRef.current = settings;

  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      await mutate();
    } finally {
      setLoading(false);
    }
  }, [mutate]);

  const loadSpaces = useCallback(async () => {
    setSpacesLoading(true);
    try {
      const nextSpaces = await listWebexSpaces();
      setSpaces(nextSpaces);
      setSelectedIds(
        new Set(nextSpaces.filter((space) => space.selected).map((space) => space.id)),
      );
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "Webexスペースを取得できませんでした",
      );
    } finally {
      setSpacesLoading(false);
    }
  }, []);

  useEffect(() => {
    if (expanded && !settings) void loadSettings();
  }, [expanded, loadSettings, settings]);

  useEffect(() => {
    if (expanded && settings?.connected && spaces.length === 0) {
      void loadSpaces();
    }
  }, [expanded, loadSpaces, settings?.connected, spaces.length]);

  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const data = event.data as
        | { source?: string; success?: boolean; error?: string }
        | undefined;
      if (
        data?.source !== "aoitalk-webex" ||
        !popupRef.current ||
        event.source !== popupRef.current ||
        !settingsRef.current?.callback_origin ||
        event.origin !== settingsRef.current.callback_origin
      ) {
        return;
      }
      setConnecting(false);
      popupRef.current = null;
      if (data.success) {
        toast.success("Webexに接続しました");
        void loadSettings().then(loadSpaces);
      } else {
        toast.error(data.error || "Webexへの接続に失敗しました");
      }
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [loadSettings, loadSpaces]);

  useEffect(() => {
    if (!connecting) return;
    const timer = window.setInterval(() => {
      if (popupRef.current?.closed) {
        popupRef.current = null;
        setConnecting(false);
      }
    }, 500);
    return () => window.clearInterval(timer);
  }, [connecting]);

  const handleConnect = useCallback(async () => {
    setConnecting(true);
    const popup = window.open(
      "",
      "aoitalk-webex",
      "popup=yes,width=560,height=760",
    );
    if (!popup) {
      setConnecting(false);
      toast.error("Webex認証用ポップアップを開けませんでした");
      return;
    }
    popupRef.current = popup;
    try {
      const url = await startWebexConnect();
      if (popup.closed) {
        popupRef.current = null;
        setConnecting(false);
        return;
      }
      popup.location.href = url;
    } catch (error) {
      popup.close();
      popupRef.current = null;
      setConnecting(false);
      toast.error(error instanceof Error ? error.message : "Webexに接続できませんでした");
    }
  }, []);

  const handleDisconnect = useCallback(async () => {
    setDisconnecting(true);
    try {
      const next = await disconnectWebex();
      await mutate(next, { revalidate: false });
      setSpaces([]);
      setSelectedIds(new Set());
      toast.success("Webex接続を解除しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "接続解除に失敗しました");
    } finally {
      setDisconnecting(false);
    }
  }, [mutate]);

  const toggleSpace = useCallback(
    (roomId: string, checked: boolean) => {
      setSelectedIds((current) => {
        const next = new Set(current);
        if (checked) {
          if (
            settings &&
            next.size >= settings.max_selected_spaces &&
            !next.has(roomId)
          ) {
            toast.error(`選択できるスペースは最大${settings.max_selected_spaces}件です`);
            return current;
          }
          next.add(roomId);
        } else {
          next.delete(roomId);
        }
        return next;
      });
    },
    [settings],
  );

  const saveSpaces = useCallback(async () => {
    setSaving(true);
    try {
      await updateWebexSpaces([...selectedIds]);
      setSpaces((current) =>
        current.map((space) => ({
          ...space,
          selected: selectedIds.has(space.id),
        })),
      );
      if (settings) {
        await mutate(
          {
            ...settings,
            selected_space_count: selectedIds.size,
            selected_room_ids: [...selectedIds],
          },
          { revalidate: false },
        );
      }
      toast.success("Webexの検索対象を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存に失敗しました");
    } finally {
      setSaving(false);
    }
  }, [mutate, selectedIds, settings]);

  const visibleSpaces = useMemo(() => {
    const needle = filter.trim().toLocaleLowerCase();
    if (!needle) return spaces;
    return spaces.filter((space) =>
      space.title.toLocaleLowerCase().includes(needle),
    );
  }, [filter, spaces]);

  const hasUnsavedChanges = useMemo(() => {
    const saved = new Set(spaces.filter((space) => space.selected).map((space) => space.id));
    return (
      saved.size !== selectedIds.size ||
      [...selectedIds].some((roomId) => !saved.has(roomId))
    );
  }, [selectedIds, spaces]);

  return (
    <Card size="sm" className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0" data-settings-surface="webex">
      <CardHeader
        className="cursor-pointer select-none border-b border-border dark:border-[#333335] px-3 py-3 transition-colors hover:bg-muted dark:bg-[#242426]"
        onClick={() => setExpanded((value) => !value)}
      >
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <MessageCircle className="size-4" />
            Webex
            {settings ? (
              <span className="text-xs font-normal text-muted-foreground">
                {settings.connected
                  ? `接続済み・${settings.selected_space_count}件`
                  : "未接続"}
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

      {expanded ? (
      <CardContent className="space-y-4 px-3 py-3">
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              読み込み中...
            </div>
          ) : settings ? (
            <>
              <div className="space-y-1 text-sm">
                <p>
                  状態:{" "}
                  <span className="font-medium">
                    {settings.connected ? "接続済み" : "未接続"}
                  </span>
                </p>
                {settings.display_name || settings.email ? (
                  <p>
                    アカウント:{" "}
                    {[settings.display_name, settings.email]
                      .filter(Boolean)
                      .join(" / ")}
                  </p>
                ) : null}
                <p className="text-xs text-muted-foreground">
                  本人が参加しているスペースを読み取り専用で検索します。
                  メッセージ本文はAoiTalkへ常時保存しません。
                </p>
                {!settings.configured ? (
                  <p className="text-xs text-destructive">
                    サーバーにWebex OAuth環境変数が設定されていません。
                  </p>
                ) : null}
              </div>

              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  onClick={handleConnect}
                  disabled={connecting || !settings.configured}
                  variant={settings.connected ? "secondary" : "default"}
                >
                  {connecting ? (
                    <Loader2 className="mr-1 size-3 animate-spin" />
                  ) : (
                    <MessageCircle className="mr-1 size-3" />
                  )}
                  {settings.connected ? "再接続" : "Webexに接続"}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={handleDisconnect}
                  disabled={disconnecting || !settings.connected}
                >
                  {disconnecting ? (
                    <Loader2 className="mr-1 size-3 animate-spin" />
                  ) : (
                    <Unplug className="mr-1 size-3" />
                  )}
                  接続解除
                </Button>
              </div>

              {settings.connected ? (
                <div className="space-y-3 rounded-md border p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <p className="text-sm font-medium">検索を許可するスペース</p>
                      <p className="text-xs text-muted-foreground">
                        {selectedIds.size}/{settings.max_selected_spaces}件を選択
                      </p>
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={loadSpaces}
                      disabled={spacesLoading}
                    >
                      {spacesLoading ? (
                        <Loader2 className="mr-1 size-3 animate-spin" />
                      ) : (
                        <RefreshCw className="mr-1 size-3" />
                      )}
                      更新
                    </Button>
                  </div>

                  <div className="relative">
                    <Search className="absolute left-2.5 top-2.5 size-3.5 text-muted-foreground" />
                    <Input
                      value={filter}
                      onChange={(event) => setFilter(event.target.value)}
                      placeholder="スペース名で絞り込み"
                      className="h-9 pl-8"
                    />
                  </div>

                  {spacesLoading && spaces.length === 0 ? (
                    <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
                      <Loader2 className="size-4 animate-spin" />
                      Webexスペースを取得中...
                    </div>
                  ) : visibleSpaces.length > 0 ? (
                    <div className="max-h-72 space-y-1 overflow-y-auto pr-1">
                      {visibleSpaces.map((space) => (
                        <label
                          key={space.id}
                          className="flex cursor-pointer items-start gap-3 rounded-md px-2 py-2 hover:bg-muted/60"
                        >
                          <Checkbox
                            checked={selectedIds.has(space.id)}
                            onCheckedChange={(checked) =>
                              toggleSpace(space.id, checked === true)
                            }
                            disabled={saving}
                          />
                          <span className="min-w-0">
                            <span className="block truncate text-sm">
                              {space.title}
                            </span>
                            <span className="block text-xs text-muted-foreground">
                              {space.type === "direct" ? "ダイレクト" : "グループ"}
                            </span>
                          </span>
                        </label>
                      ))}
                    </div>
                  ) : (
                    <p className="py-3 text-sm text-muted-foreground">
                      該当するスペースがありません。
                    </p>
                  )}

                  <Button
                    type="button"
                    size="sm"
                    onClick={saveSpaces}
                    disabled={saving || !hasUnsavedChanges}
                  >
                    {saving ? (
                      <Loader2 className="mr-1 size-3 animate-spin" />
                    ) : null}
                    検索対象を保存
                  </Button>
                </div>
              ) : null}
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              Webex設定を取得できませんでした。
            </p>
          )}
        </CardContent>
      ) : null}
    </Card>
  );
}

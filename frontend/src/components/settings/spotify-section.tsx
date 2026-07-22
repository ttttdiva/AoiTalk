"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { ChevronDown, ChevronUp, Music2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";

interface SettingsPayload {
  settings?: {
    spotify?: { enabled?: boolean };
    agents?: { spotify?: { enabled?: boolean } };
  };
}

async function pyFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `API Error: ${res.status}`);
  }
  return res.json();
}

interface SpotifyState {
  enabled: boolean | null;
  agentEnabled: boolean | null;
}

const INITIAL_SPOTIFY_STATE: SpotifyState = { enabled: null, agentEnabled: null };

export function SpotifySection() {
  const [expanded, setExpanded] = useState(false);
  // Spotify設定（サーバー状態）は SWR で管理。取得タイミングは従来どおりマウント時に
  // 駆動するため自動 revalidation は無効化する。取得失敗時は従来同様に直前値を保持する。
  const stateRef = useRef<SpotifyState>(INITIAL_SPOTIFY_STATE);
  const { data = INITIAL_SPOTIFY_STATE, mutate: mutateSpotify } = useSWR<SpotifyState>(
    "settings/spotify",
    async () => {
      try {
        const payload = await pyFetch<SettingsPayload>("/settings");
        return {
          enabled: payload.settings?.spotify?.enabled ?? true,
          agentEnabled: payload.settings?.agents?.spotify?.enabled ?? false,
        };
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Spotify設定を取得できませんでした");
        return stateRef.current;
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
  stateRef.current = data;
  const { enabled, agentEnabled } = data;
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      await mutateSpotify();
    } finally {
      setLoading(false);
    }
  }, [mutateSpotify]);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const updateSetting = useCallback(async (
    key: "spotify.enabled" | "agents.spotify.enabled",
    value: boolean,
  ) => {
    // 楽観的更新：保存中はローカルキャッシュを即時反映する。
    await mutateSpotify(
      (current = INITIAL_SPOTIFY_STATE) =>
        key === "spotify.enabled"
          ? { ...current, enabled: value }
          : { ...current, agentEnabled: value },
      { revalidate: false },
    );
    setSaving(true);
    try {
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key, value }),
      });
      toast.success("Spotify設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Spotify設定を保存できませんでした");
      void mutateSpotify();
    } finally {
      setSaving(false);
    }
  }, [mutateSpotify]);

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={() => setExpanded((value) => !value)}
      >
        <CardTitle className="flex items-center justify-between gap-3 text-sm">
          <span className="flex min-w-0 items-center gap-2">
            <Music2 className="size-4" />
            <span>Spotify</span>
            <Badge variant={enabled ? "default" : "secondary"}>
              {enabled === null ? "読込中" : enabled ? "ON" : "OFF"}
            </Badge>
          </span>
          {expanded ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
        </CardTitle>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-3">
          {loading ? (
            <Skeleton className="h-8 w-64 rounded" />
          ) : (
            <div className="space-y-3">
              <div className="flex items-start justify-between gap-3 rounded border p-3">
                <div className="space-y-1">
                  <Label htmlFor="spotify-enabled" className="text-sm font-medium">
                    Spotify連携を有効にする
                  </Label>
                  <p className="text-xs text-muted-foreground">
                    Spotifyの検索、再生制御、キーワード検出をアプリ全体で利用します。
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant={enabled ? "default" : "secondary"}>
                    {enabled === null ? "読込中" : enabled ? "ON" : "OFF"}
                  </Badge>
                  <Checkbox
                    id="spotify-enabled"
                    checked={enabled === true}
                    disabled={saving || enabled === null}
                    onCheckedChange={(value) =>
                      updateSetting("spotify.enabled", value === true)
                    }
                  />
                </div>
              </div>
              <div className="flex items-start justify-between gap-3 rounded border p-3">
                <div className="space-y-1">
                  <Label htmlFor="spotify-agent-enabled" className="text-sm font-medium">
                    Spotifyエージェント
                  </Label>
                  <p className="text-xs text-muted-foreground">
                    会話中にSpotify操作用エージェントを呼び出せるようにします。
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant={agentEnabled ? "default" : "secondary"}>
                    {agentEnabled === null ? "読込中" : agentEnabled ? "ON" : "OFF"}
                  </Badge>
                  <Checkbox
                    id="spotify-agent-enabled"
                    checked={agentEnabled === true}
                    disabled={saving || agentEnabled === null}
                    onCheckedChange={(value) =>
                      updateSetting("agents.spotify.enabled", value === true)
                    }
                  />
                </div>
              </div>
            </div>
          )}
        </CardContent>
      )}
    </Card>
  );
}

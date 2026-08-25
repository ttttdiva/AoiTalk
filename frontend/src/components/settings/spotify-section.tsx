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
    /** Canonical shared-integration settings (schema v3). */
    integrations?: { spotify?: { enabled?: boolean } };
    /** Legacy read-only fallback for settings created before schema v3. */
    spotify?: { enabled?: boolean };
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
}

// Spotify is a shared integration, not an Agent/Team toggle.  It is disabled
// by default when no canonical value has been persisted yet.
const INITIAL_SPOTIFY_STATE: SpotifyState = { enabled: null };

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
          // Read the v3 location first.  Legacy locations are accepted only
          // while reading so existing installs can be migrated without
          // keeping their old write paths alive.
          enabled:
            payload.settings?.integrations?.spotify?.enabled
            ?? payload.settings?.spotify?.enabled
            ?? false,
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
  const { enabled } = data;
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

  const updateSetting = useCallback(async (value: boolean) => {
    // 楽観的更新：保存中はローカルキャッシュを即時反映する。
    await mutateSpotify(
      (current = INITIAL_SPOTIFY_STATE) => ({ ...current, enabled: value }),
      { revalidate: false },
    );
    setSaving(true);
    try {
      await pyFetch("/settings", {
        method: "PATCH",
        // Canonical v3 setting.  Never write spotify.enabled or
        // agents.spotify.enabled from this UI.
        body: JSON.stringify({ key: "integrations.spotify.enabled", value }),
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
    <Card size="sm" className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0" data-settings-surface="spotify">
      <CardHeader
        className="cursor-pointer select-none border-b border-border dark:border-[#333335] px-3 py-3 transition-colors hover:bg-muted dark:bg-[#242426]"
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
      <CardContent className="space-y-3 px-3 py-3">
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
                      updateSetting(value === true)
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

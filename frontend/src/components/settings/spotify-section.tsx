"use client";

import { useCallback, useEffect, useState } from "react";
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

export function SpotifySection() {
  const [expanded, setExpanded] = useState(false);
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [agentEnabled, setAgentEnabled] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      const data = await pyFetch<SettingsPayload>("/settings");
      setEnabled(data.settings?.spotify?.enabled ?? true);
      setAgentEnabled(data.settings?.agents?.spotify?.enabled ?? false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Spotify設定を取得できませんでした");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const updateSetting = useCallback(async (
    key: "spotify.enabled" | "agents.spotify.enabled",
    value: boolean,
  ) => {
    if (key === "spotify.enabled") setEnabled(value);
    else setAgentEnabled(value);
    setSaving(true);
    try {
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key, value }),
      });
      toast.success("Spotify設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Spotify設定を保存できませんでした");
      void loadSettings();
    } finally {
      setSaving(false);
    }
  }, [loadSettings]);

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

"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Loader2, Plug, RefreshCw, Trash2, XCircle } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface HydrusSettingsResponse {
  configured: boolean;
  apiUrl: string | null;
  displayName: string | null;
}

async function hydrusSettingsFetch<T>(init?: RequestInit): Promise<T> {
  const response = await fetch("/api/hydrus/settings", {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : `API Error: ${response.status}`);
  }
  return body as T;
}

/** Per-user Hydrus URL/access-key management (the key is never re-displayed). */
export function HydrusSettingsSection() {
  const [expanded, setExpanded] = useState(false);
  const [settings, setSettings] = useState<HydrusSettingsResponse | null>(null);
  const [apiUrl, setApiUrl] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [accessKey, setAccessKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [checking, setChecking] = useState(false);
  const [connection, setConnection] = useState<"ok" | "error" | null>(null);

  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      const next = await hydrusSettingsFetch<HydrusSettingsResponse>();
      setSettings(next);
      setApiUrl(next.apiUrl ?? "");
      setDisplayName(next.displayName ?? "");
      setConnection(null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Hydrus設定を取得できませんでした");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (expanded && settings === null) void loadSettings();
  }, [expanded, loadSettings, settings]);

  const save = async () => {
    if (!apiUrl.trim()) {
      toast.error("Hydrus API URLを入力してください");
      return;
    }
    if (!accessKey.trim()) {
      toast.error("Access Keyを入力してください（再表示はされません）");
      return;
    }
    setSaving(true);
    try {
      await hydrusSettingsFetch({
        method: "PUT",
        body: JSON.stringify({ apiUrl: apiUrl.trim(), accessKey: accessKey.trim(), displayName: displayName.trim() }),
      });
      setAccessKey("");
      await loadSettings();
      toast.success("Hydrus設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Hydrus設定を保存できませんでした");
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!window.confirm("Hydrus設定を削除しますか？")) return;
    setDeleting(true);
    try {
      await hydrusSettingsFetch({ method: "DELETE" });
      setSettings({ configured: false, apiUrl: null, displayName: null });
      setApiUrl("");
      setDisplayName("");
      setAccessKey("");
      setConnection(null);
      toast.success("Hydrus設定を削除しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Hydrus設定を削除できませんでした");
    } finally {
      setDeleting(false);
    }
  };

  const checkConnection = async () => {
    setChecking(true);
    try {
      const response = await fetch("/api/python-proxy/hydrus/health", { credentials: "include" });
      const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
      if (!response.ok || body.ok === false) {
        throw new Error(typeof body.detail === "string" ? body.detail : "Hydrusに接続できませんでした");
      }
      setConnection("ok");
      toast.success("Hydrusへの接続を確認しました");
    } catch (error) {
      setConnection("error");
      toast.error(error instanceof Error ? error.message : "Hydrusへの接続に失敗しました");
    } finally {
      setChecking(false);
    }
  };

  return (
    <Card className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0" data-settings-surface="hydrus">
      <CardHeader className="cursor-pointer border-b border-border dark:border-[#333335] px-3 py-3 transition-colors hover:bg-muted dark:bg-[#242426]" onClick={() => setExpanded((value) => !value)}>
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Plug className="size-4" />
            <CardTitle className="text-sm">Hydrus Browser連携</CardTitle>
          </div>
          {settings?.configured ? <Badge>設定済み</Badge> : <Badge variant="secondary">未設定</Badge>}
        </div>
        <CardDescription>
          ユーザーごとのHydrus Client URLとAccess Keyを暗号化保存します。
        </CardDescription>
      </CardHeader>
      {expanded && (
      <CardContent className="space-y-4 px-3 py-3">
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> 設定を読み込み中...
            </div>
          ) : (
            <>
              {!settings?.configured && (
                <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                  未設定です。Hydrus Client APIを有効にし、URL（例: http://127.0.0.1:45869）とAccess Keyを登録してください。
                  ローカル/private接続は管理者のサーバーポリシーで許可されている場合のみ利用できます。
                </p>
              )}
              <div className="space-y-1">
                <Label htmlFor="hydrus-api-url">Hydrus API URL</Label>
                <Input id="hydrus-api-url" value={apiUrl} onChange={(event) => setApiUrl(event.target.value)} placeholder="http://127.0.0.1:45869" />
              </div>
              <div className="space-y-1">
                <Label htmlFor="hydrus-display-name">表示名（任意）</Label>
                <Input id="hydrus-display-name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="自分のHydrus" />
              </div>
              <div className="space-y-1">
                <Label htmlFor="hydrus-access-key">Access Key</Label>
                <Input
                  id="hydrus-access-key"
                  type="password"
                  value={accessKey}
                  onChange={(event) => setAccessKey(event.target.value)}
                  placeholder={settings?.configured ? "設定済み（変更時のみ入力）" : "Access Key"}
                  autoComplete="new-password"
                />
                <p className="text-[11px] text-muted-foreground">保存済みのキーは再表示されません。</p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" onClick={() => void save()} disabled={saving}>
                  {saving && <Loader2 className="mr-1 size-3 animate-spin" />} 保存
                </Button>
                <Button variant="outline" size="sm" onClick={() => void checkConnection()} disabled={checking || !settings?.configured}>
                  {checking ? <Loader2 className="mr-1 size-3 animate-spin" /> : <RefreshCw className="mr-1 size-3" />} 接続確認
                </Button>
                {settings?.configured && (
                  <Button variant="outline" size="sm" onClick={() => void remove()} disabled={deleting}>
                    {deleting ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Trash2 className="mr-1 size-3" />} 削除
                  </Button>
                )}
                {connection === "ok" && <span className="flex items-center gap-1 text-xs text-green-600"><CheckCircle2 className="size-3" />接続OK</span>}
                {connection === "error" && <span className="flex items-center gap-1 text-xs text-destructive"><XCircle className="size-3" />接続失敗</span>}
              </div>
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}

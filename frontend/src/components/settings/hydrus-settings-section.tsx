"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  ExternalLink,
  Loader2,
  Plug,
  RefreshCw,
  Trash2,
  Upload,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  HydrusApiError,
  hydrusDeleteSettings,
  hydrusErrorMessage,
  hydrusGetSettings,
  hydrusHealth,
  hydrusMigrateLegacySettings,
  hydrusSaveSettings,
  type HydrusUserSettingsResponse,
} from "@/lib/hf-api";

type ConnectionState = "ok" | "error" | null;

interface HydrusStatus {
  kind: "error" | "success";
  message: string;
  code?: string;
  traceId?: string;
  retryable?: boolean;
}

function errorStatus(error: unknown, fallback: string): HydrusStatus {
  if (error instanceof HydrusApiError) {
    return {
      kind: "error",
      message: hydrusErrorMessage(error, fallback),
      code: String(error.code),
      traceId: error.traceId,
      retryable: error.retryable,
    };
  }
  // Do not render arbitrary upstream/network error text.  The proxy's
  // structured details are the only browser-visible diagnostic source.
  return { kind: "error", message: fallback, retryable: true };
}

/** Per-user Hydrus URL/access-key management (the key is never re-displayed). */
export function HydrusSettingsSection() {
  const [expanded, setExpanded] = useState(false);
  const [settings, setSettings] = useState<HydrusUserSettingsResponse | null>(
    null,
  );
  const [apiUrl, setApiUrl] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [accessKey, setAccessKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [checking, setChecking] = useState(false);
  const [migrating, setMigrating] = useState(false);
  const [connection, setConnection] = useState<ConnectionState>(null);
  const [status, setStatus] = useState<HydrusStatus | null>(null);
  const retryRef = useRef<(() => void) | null>(null);
  const loadSettingsRef = useRef<(() => Promise<void>) | null>(null);
  const saveRef = useRef<(() => Promise<void>) | null>(null);
  const removeRef = useRef<(() => Promise<void>) | null>(null);
  const checkConnectionRef = useRef<(() => Promise<void>) | null>(null);
  const migrateLegacyRef = useRef<(() => Promise<void>) | null>(null);

  const loadSettings = useCallback(async () => {
    setLoading(true);
    retryRef.current = () => void loadSettingsRef.current?.();
    try {
      const next = await hydrusGetSettings();
      setSettings(next);
      setApiUrl(next.apiUrl ?? "");
      setDisplayName(next.displayName ?? "");
      setConnection(null);
      setStatus(null);
    } catch (error) {
      const nextStatus = errorStatus(
        error,
        "Hydrus設定を取得できません。しばらくしてから再試行してください。",
      );
      setStatus(nextStatus);
      toast.error(nextStatus.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSettingsRef.current = loadSettings;
  }, [loadSettings]);

  useEffect(() => {
    if (expanded && settings === null) void loadSettings();
  }, [expanded, loadSettings, settings]);

  const save = useCallback(async () => {
    if (!apiUrl.trim()) {
      toast.error("Hydrus API URLを入力してください");
      return;
    }
    if (!accessKey.trim()) {
      toast.error("Access Keyを入力してください（再表示はされません）");
      return;
    }
    setSaving(true);
    retryRef.current = () => void saveRef.current?.();
    try {
      await hydrusSaveSettings({
        apiUrl: apiUrl.trim(),
        accessKey: accessKey.trim(),
        displayName: displayName.trim(),
      });
      setAccessKey("");
      await loadSettings();
      setStatus({ kind: "success", message: "Hydrus設定を保存しました" });
      toast.success("Hydrus設定を保存しました");
    } catch (error) {
      const nextStatus = errorStatus(
        error,
        "Hydrus設定を保存できません。入力内容を確認して再試行してください。",
      );
      setStatus(nextStatus);
      toast.error(nextStatus.message);
    } finally {
      setSaving(false);
    }
  }, [accessKey, apiUrl, displayName, loadSettings]);

  const remove = useCallback(async () => {
    if (!window.confirm("Hydrus設定を削除しますか？")) return;
    setDeleting(true);
    retryRef.current = () => void removeRef.current?.();
    try {
      await hydrusDeleteSettings();
      setSettings({
        configured: false,
        apiUrl: null,
        displayName: null,
        legacyAvailable: false,
      });
      setApiUrl("");
      setDisplayName("");
      setAccessKey("");
      setConnection(null);
      setStatus({ kind: "success", message: "Hydrus設定を削除しました" });
      toast.success("Hydrus設定を削除しました");
    } catch (error) {
      const nextStatus = errorStatus(
        error,
        "Hydrus設定を削除できません。しばらくしてから再試行してください。",
      );
      setStatus(nextStatus);
      toast.error(nextStatus.message);
    } finally {
      setDeleting(false);
    }
  }, []);

  const checkConnection = useCallback(async () => {
    setChecking(true);
    retryRef.current = () => void checkConnectionRef.current?.();
    try {
      const response = await hydrusHealth();
      if (!response.ok) {
        throw new Error("Hydrusへの接続に失敗しました");
      }
      setConnection("ok");
      setStatus({ kind: "success", message: "Hydrusへの接続を確認しました" });
      toast.success("Hydrusへの接続を確認しました");
    } catch (error) {
      const nextStatus = errorStatus(
        error,
        "Hydrus Clientに接続できません。Clientの起動状態とURLを確認して再試行してください。",
      );
      setConnection("error");
      setStatus(nextStatus);
      toast.error(nextStatus.message);
    } finally {
      setChecking(false);
    }
  }, []);

  const migrateLegacy = useCallback(async () => {
    if (!settings?.legacyAvailable || migrating) return;
    if (
      !window.confirm(
        "このアカウントで既存のローカルHydrus設定を取り込みますか？\nAccess Keyは表示・送信確認画面へ再表示されません。",
      )
    ) {
      return;
    }
    setMigrating(true);
    retryRef.current = () => void migrateLegacyRef.current?.();
    try {
      const result = await hydrusMigrateLegacySettings();
      setSettings((current) => ({
        ...(current ?? { configured: false, apiUrl: null, displayName: null }),
        configured: result.configured,
        apiUrl: result.apiUrl ?? null,
        legacyAvailable: false,
      }));
      setApiUrl(result.apiUrl ?? "");
      setAccessKey("");
      setStatus({
        kind: "success",
        message: result.alreadyMigrated
          ? "既存のHydrus設定は取り込み済みです"
          : "既存のHydrus設定を取り込みました",
      });
      toast.success(
        result.alreadyMigrated
          ? "既存のHydrus設定は取り込み済みです"
          : "既存のHydrus設定を取り込みました",
      );
    } catch (error) {
      const nextStatus = errorStatus(
        error,
        "既存のHydrus設定を取り込めません。Access Keyを設定画面から登録してください。",
      );
      setStatus(nextStatus);
      toast.error(nextStatus.message);
    } finally {
      setMigrating(false);
    }
  }, [migrating, settings]);

  useEffect(() => {
    saveRef.current = save;
    removeRef.current = remove;
    checkConnectionRef.current = checkConnection;
    migrateLegacyRef.current = migrateLegacy;
  }, [checkConnection, migrateLegacy, remove, save]);

  const retry = () => {
    const callback = retryRef.current;
    if (callback) callback();
  };

  return (
    <Card
      className="rounded-md border-border bg-card py-0 dark:border-[#333335] dark:bg-[#1a1a1b]"
      data-settings-surface="hydrus"
    >
      <CardHeader
        className="cursor-pointer border-b border-border px-3 py-3 transition-colors hover:bg-muted dark:border-[#333335] dark:bg-[#242426]"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Plug className="size-4" />
            <CardTitle className="text-sm">Hydrus Browser連携</CardTitle>
          </div>
          {settings?.configured ? (
            <Badge>設定済み</Badge>
          ) : (
            <Badge variant="secondary">未設定</Badge>
          )}
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
              {status && (
                <div
                  className={
                    status.kind === "error"
                      ? "space-y-1 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
                      : "space-y-1 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-700 dark:text-emerald-300"
                  }
                  role={status.kind === "error" ? "alert" : "status"}
                  aria-live="polite"
                >
                  <div className="flex items-start gap-2">
                    {status.kind === "error" ? (
                      <XCircle className="mt-0.5 size-3.5 shrink-0" />
                    ) : (
                      <CheckCircle2 className="mt-0.5 size-3.5 shrink-0" />
                    )}
                    <span>{status.message}</span>
                  </div>
                  {status.code === "hydrus_endpoint_policy_rejected" && (
                    <p className="pl-5">
                      同じPCのHydrus Clientは loopback（localhost / 127.0.0.1 / ::1）を利用してください。LAN上のprivate endpointは安全ポリシーで制限されます。
                    </p>
                  )}
                  {status.code === "hydrus_auth_failed" && (
                    <p className="pl-5">
                      Hydrus Client APIのAccess Keyと権限を確認してください。
                    </p>
                  )}
                  {status.retryable && (
                    <div className="flex flex-wrap items-center gap-2 pl-5 pt-1">
                      <Button type="button" size="xs" variant="outline" onClick={retry}>
                        <RefreshCw className="size-3" /> 再試行
                      </Button>
                    </div>
                  )}
                  {status.traceId && (
                    <p className="pl-5 text-[10px] opacity-80">
                      トレースID: {status.traceId}
                    </p>
                  )}
                </div>
              )}
              {!settings?.configured && (
                <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                  未設定です。Hydrus Client APIを有効にし、URL（例: http://127.0.0.1:45869）とAccess Keyを登録してください。
                  同じPCの通常のHydrus Clientにはlocalhost / 127.0.0.1 / ::1などのloopback接続を利用できます。LAN上のprivate接続は管理者ポリシーで許可されている場合のみ利用できます。
                </p>
              )}
              {settings?.legacyAvailable && (
                <div className="space-y-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200">
                  <p>
                    以前のローカルHydrus設定が見つかりました。所有者をこのアカウントとして確認できる場合のみ、安全に取り込めます。
                  </p>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => void migrateLegacy()}
                    disabled={migrating}
                  >
                    {migrating ? (
                      <Loader2 className="size-3 animate-spin" />
                    ) : (
                      <Upload className="size-3" />
                    )}
                    既存のローカルHydrus設定をこのユーザーへ引き継ぐ
                  </Button>
                </div>
              )}
              <div className="space-y-1">
                <Label htmlFor="hydrus-api-url">Hydrus API URL</Label>
                <Input
                  id="hydrus-api-url"
                  value={apiUrl}
                  onChange={(event) => setApiUrl(event.target.value)}
                  placeholder="http://127.0.0.1:45869"
                />
                <p className="text-[11px] text-muted-foreground">
                  同じPCのHydrus Clientは通常 http://127.0.0.1:45869 です。埋め込み認証情報付きURLは利用できません。
                </p>
              </div>
              <div className="space-y-1">
                <Label htmlFor="hydrus-display-name">表示名（任意）</Label>
                <Input
                  id="hydrus-display-name"
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  placeholder="自分のHydrus"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="hydrus-access-key">Access Key</Label>
                <Input
                  id="hydrus-access-key"
                  type="password"
                  value={accessKey}
                  onChange={(event) => setAccessKey(event.target.value)}
                  placeholder={
                    settings?.configured
                      ? "設定済み（変更時のみ入力）"
                      : "Access Key"
                  }
                  autoComplete="new-password"
                />
                <p className="text-[11px] text-muted-foreground">
                  保存済みのキーは再表示されません。
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  onClick={() => void save()}
                  disabled={saving}
                >
                  {saving && <Loader2 className="mr-1 size-3 animate-spin" />} 保存
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => void checkConnection()}
                  disabled={checking || !settings?.configured}
                >
                  {checking ? (
                    <Loader2 className="mr-1 size-3 animate-spin" />
                  ) : (
                    <RefreshCw className="mr-1 size-3" />
                  )} 接続確認
                </Button>
                {settings?.configured && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void remove()}
                    disabled={deleting}
                  >
                    {deleting ? (
                      <Loader2 className="mr-1 size-3 animate-spin" />
                    ) : (
                      <Trash2 className="mr-1 size-3" />
                    )} 削除
                  </Button>
                )}
                {connection === "ok" && (
                  <span className="flex items-center gap-1 text-xs text-green-600">
                    <CheckCircle2 className="size-3" />接続OK
                  </span>
                )}
                {connection === "error" && (
                  <span className="flex items-center gap-1 text-xs text-destructive">
                    <XCircle className="size-3" />接続失敗
                  </span>
                )}
                <a
                  href="/settings#hydrus"
                  className="ml-auto inline-flex items-center gap-1 text-[11px] text-muted-foreground underline-offset-2 hover:underline"
                >
                  設定の共有リンク <ExternalLink className="size-3" />
                </a>
              </div>
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}

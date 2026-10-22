"use client";

import { useState } from "react";
import useSWR from "swr";
import { Globe } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { HelpPopover } from "@/components/ui/help-popover";
import { SettingsDisclosure } from "./settings-disclosure";

type BrowserSettings = {
  enabled: boolean;
  jev_enabled: boolean;
  dedicated_enabled: boolean;
  max_steps: number;
  timeout_seconds: number;
  jev_model: string;
};
type SettingsResponse = {
  settings: { browser_agent: BrowserSettings & { jev_configured: boolean; pc_bridge_required: boolean } };
};

async function loadSettings(): Promise<SettingsResponse> {
  const response = await fetch("/api/python-proxy/settings", { credentials: "include" });
  if (!response.ok) throw new Error("ブラウザ操作設定を取得できませんでした");
  return response.json();
}

export function BrowserAgentSettingsSection() {
  const { data, error, mutate, isLoading } = useSWR("settings/browser-agent", loadSettings);
  const [draft, setDraft] = useState<BrowserSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [saveError, setSaveError] = useState("");
  const current = data?.settings.browser_agent;
  const settings = draft ?? current;
  const busy = saving || isLoading || !settings;

  function change(patch: Partial<BrowserSettings>) {
    if (!settings) return;
    setDraft({
      enabled: settings.enabled, jev_enabled: settings.jev_enabled, dedicated_enabled: settings.dedicated_enabled ?? false,
      max_steps: settings.max_steps, timeout_seconds: settings.timeout_seconds,
      jev_model: settings.jev_model, ...patch,
    });
    setMessage("");
  }

  async function save() {
    if (!settings) return;
    setSaving(true);
    setSaveError("");
    setMessage("");
    const value: BrowserSettings = {
      enabled: settings.enabled, jev_enabled: settings.jev_enabled, dedicated_enabled: settings.dedicated_enabled ?? false,
      max_steps: settings.max_steps, timeout_seconds: settings.timeout_seconds,
      jev_model: settings.jev_model,
    };
    try {
      const response = await fetch("/api/python-proxy/settings", {
        method: "PATCH", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: "browser_agent", value }),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(typeof detail?.detail === "string" ? detail.detail : "ブラウザ操作設定を保存できませんでした");
      }
      await mutate();
      setDraft(null);
      setMessage("ブラウザ操作設定を保存しました");
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "保存できませんでした");
    } finally {
      setSaving(false);
    }
  }

  const valid = settings && Number.isInteger(settings.max_steps) && settings.max_steps >= 1 && settings.max_steps <= 60
    && Number.isInteger(settings.timeout_seconds) && settings.timeout_seconds >= 15 && settings.timeout_seconds <= 900;

  return (
    <SettingsDisclosure
      title="ブラウザ操作" targetId="browser-agent" icon={<Globe className="size-4" />}
      summary={<Badge variant="secondary">{current?.enabled ? "有効" : "無効"}</Badge>}
      help={(
        <>
          <p>通常Chatから依頼できます。URLを省略すると、Bridgeで選択済みのtargetを操作します。</p>
          <p>初回は「PC接続」でexeと接続設定を取得し、Bridgeから操作するブラウザを選択してください。</p>
          <p>専用ブラウザを使う場合、選択したPCのBridgeが専用ブラウザを開きます。ログインが必要な場合は、そのPCの画面でログインして作業を再開します。</p>
        </>
      )}
    >
      {error ? <div role="alert" className="space-y-2 text-sm text-destructive">
        <p>ブラウザ操作設定を取得できませんでした。</p>
        <Button size="sm" variant="outline" onClick={() => void mutate()}>再試行</Button>
      </div> : isLoading ? <p className="text-sm text-muted-foreground">読み込み中...</p> : (
        <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <div className="flex max-w-xl items-center justify-between gap-3">
            <Label htmlFor="browser-agent-enabled">ブラウザ操作を有効にする</Label>
            <Checkbox id="browser-agent-enabled" checked={settings?.enabled ?? false} disabled={busy} onCheckedChange={(enabled) => change({ enabled: enabled === true })} />
          </div>
          <div className="flex max-w-xl items-center justify-between gap-3">
            <span className="inline-flex items-center gap-1">
              <Label htmlFor="browser-agent-jev">Jevを優先する</Label>
              <HelpPopover label="Jevを優先する">
                専用ブラウザではJevを優先し、無効時は現在のLLMで判断します。通常ブラウザのhelperは判断できない場合に停止します。
              </HelpPopover>
            </span>
            <Checkbox id="browser-agent-jev" checked={settings?.jev_enabled ?? true} disabled={busy} onCheckedChange={(jev_enabled) => change({ jev_enabled: jev_enabled === true })} />
          </div>
          <p className="text-xs text-muted-foreground" role="status">
            {current?.jev_configured ? "Jevキー設定済み。" : "Jevキー未設定。"}
          </p>
          <div className="rounded border p-3 text-sm space-y-2">
            <p role="status">PC接続で操作先を選択してください</p>
            <p>操作するPCでポータブルBridgeを起動します。そのPCの既存Edgeタブとログイン状態を利用します。</p>
            <a className="underline" href="#pc-bridge">PC接続を開く</a>
          </div>
          <div className="flex max-w-xl items-center justify-between gap-3">
            <Label htmlFor="browser-agent-dedicated">専用ブラウザを利用する</Label>
            <Checkbox id="browser-agent-dedicated" checked={settings?.dedicated_enabled ?? false}
              disabled={busy || !settings?.enabled} onCheckedChange={(value) => change({ dedicated_enabled: value === true })} />
          </div>
          <details className="text-sm">
            <summary className="cursor-pointer text-muted-foreground">実行上限</summary>
            <div className="mt-2 flex flex-wrap gap-3">
              <div className="space-y-1.5"><Label htmlFor="browser-agent-steps">最大ステップ数</Label>
                <Input id="browser-agent-steps" type="number" min={1} max={60} className="w-32" value={settings?.max_steps ?? 24}
                  disabled={busy} onChange={(event) => change({ max_steps: event.target.valueAsNumber })} />
              </div>
              <div className="space-y-1.5"><Label htmlFor="browser-agent-timeout">実行上限（秒）</Label>
                <Input id="browser-agent-timeout" type="number" min={15} max={900} className="w-32" value={settings?.timeout_seconds ?? 300}
                  disabled={busy} onChange={(event) => change({ timeout_seconds: event.target.valueAsNumber })} />
              </div>
            </div>
          </details>
          {saveError && <p role="alert" className="text-sm text-destructive">{saveError}</p>}
          <div className="flex items-center gap-3">
            <Button type="submit" size="sm" disabled={busy || !valid || !draft}>{saving ? "保存中..." : "保存"}</Button>
            <span role="status" className="text-xs text-muted-foreground">{message}</span>
          </div>
        </form>
      )}
    </SettingsDisclosure>
  );
}

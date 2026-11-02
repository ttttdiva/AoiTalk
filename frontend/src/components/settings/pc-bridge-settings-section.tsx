"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import Image from "next/image";
import { Monitor } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { withBasePath } from "@/lib/base-path";
import { buildPcBridgeServerUrl } from "@/lib/websocket-url";
import { SettingsDisclosure } from "./settings-disclosure";

type SettingsPayload = { settings?: { pc_bridge?: { remote_enabled?: unknown } } };
type Device = { id: string; name: string; online: boolean; edge_connected: boolean; capabilities: string[] };
type State = { devices: Device[]; selected_device_id: string | null };
const settingsUrl = withBasePath("/api/python-proxy/settings");
const bridgeBase = withBasePath("/api/python-proxy/pc-bridge");

async function settingsRequest(path = "", init?: RequestInit) {
  const response = await fetch(settingsUrl + path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : `設定を保存できませんでした (${response.status})`);
  }
  return response.json();
}

async function loadRemoteEnabled(): Promise<boolean> {
  const payload = await settingsRequest() as SettingsPayload;
  return payload.settings?.pc_bridge?.remote_enabled === true;
}

export function PcBridgeSettingsSection({ isAdmin = true }: { isAdmin?: boolean }) {
  const { data: remoteEnabled, error: loadError, mutate } = useSWR<boolean>(
    "settings/pc-bridge/remote-enabled",
    loadRemoteEnabled,
  );
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  async function updateRemoteEnabled(enabled: boolean) {
    if (saving || remoteEnabled === undefined || !isAdmin) return;
    setSaving(true);
    setSaveError("");
    try {
      await settingsRequest("", {
        method: "PATCH",
        body: JSON.stringify({ key: "pc_bridge.remote_enabled", value: enabled, persist: true }),
      });
      await mutate(enabled, { revalidate: false });
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "設定を保存できませんでした");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <Card size="sm" className="rounded-xl border-border bg-card py-0" data-settings-surface="pc-bridge-remote">
        <CardHeader className="border-b border-border/70 px-3 py-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Monitor className="size-4" />
                他の PC の制御
                <HelpPopover label="他の PC の制御">
                  <p>有効にすると、Bridge exeを起動した別のPCを登録して操作先にできます。</p>
                  <p>無効にすると、接続中のBridgeを切断します。登録情報は保持されます。</p>
                </HelpPopover>
              </CardTitle>
              <p className="text-xs leading-5 text-muted-foreground">
                別のPC上のBridgeを操作先として使う設定です。既定ではオフです。
              </p>
            </div>
            <label className="inline-flex shrink-0 cursor-pointer items-center" aria-label="他の PC の制御">
              <input
                type="checkbox"
                role="switch"
                aria-label="他の PC の制御"
                className="peer sr-only"
                checked={remoteEnabled === true}
                disabled={saving || remoteEnabled === undefined || !isAdmin || Boolean(loadError)}
                onChange={(event) => void updateRemoteEnabled(event.currentTarget.checked)}
              />
              <span aria-hidden="true" className="relative h-5 w-9 rounded-full border border-input bg-muted transition-colors after:absolute after:left-0.5 after:top-0.5 after:size-3.5 after:rounded-full after:bg-background after:shadow after:transition-transform peer-checked:border-primary peer-checked:bg-primary peer-checked:after:translate-x-4 peer-focus-visible:ring-2 peer-focus-visible:ring-ring peer-disabled:cursor-not-allowed peer-disabled:opacity-50" />
            </label>
          </div>
        </CardHeader>
        {(remoteEnabled !== true || loadError || saveError) && (
          <CardContent className="space-y-2 px-3 py-3">
            {loadError ? <p role="alert" className="text-sm text-destructive">「他の PC の制御」の設定を取得できませんでした。</p> : null}
            {saveError ? <p role="alert" className="break-words text-sm text-destructive">{saveError}</p> : null}
            {remoteEnabled === undefined && !loadError ? <p role="status" className="text-sm text-muted-foreground">設定を読み込み中です。</p> : null}
            {remoteEnabled === false && !loadError ? (
              <p role="status" className="text-sm text-muted-foreground">
                PC接続はオフです。別のPCを登録するには、この設定をオンにしてください。
              </p>
            ) : null}
          </CardContent>
        )}
      </Card>
      {remoteEnabled === true ? <PcBridgeConnectionSettings /> : null}
    </>
  );
}

async function api(path: string, method = "GET", body?: unknown) {
  const response = await fetch(bridgeBase + path, {
    method, credentials: "include", headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : `PC接続に失敗しました (${response.status})`);
  }
  return (await response.json()).result;
}

function PcBridgeConnectionSettings() {
  const { data, error, mutate, isLoading } = useSWR<State>("pc-bridge/devices", () => api("/devices"), { refreshInterval: 5000 });
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [screen, setScreen] = useState("");
  const screenRef = useRef("");
  useEffect(() => () => { if (screenRef.current) URL.revokeObjectURL(screenRef.current); }, []);

  async function run(work: () => Promise<void>) {
    setBusy(true); setMessage("");
    try { await work(); } catch (error) { setMessage(error instanceof Error ? error.message : "操作に失敗しました"); }
    finally { setBusy(false); }
  }
  function saveConfig() {
    const serverUrl = buildPcBridgeServerUrl();
    const url = URL.createObjectURL(new Blob([JSON.stringify({ server_url: serverUrl, token, browser_backend: "agent_browser" }, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "aoitalk-pc-connection.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function showScreen() {
    const selected = data?.selected_device_id;
    if (!selected) return;
    const response = await fetch(`${bridgeBase}/devices/${selected}/screen`, { credentials: "include", cache: "no-store" });
    if (!response.ok) throw new Error("選択したPCの画面を取得できませんでした。Bridgeの接続を確認してください。");
    if (screenRef.current) URL.revokeObjectURL(screenRef.current);
    screenRef.current = URL.createObjectURL(await response.blob());
    setScreen(screenRef.current);
  }

  return <SettingsDisclosure
    title="PC接続"
    targetId="pc-bridge"
    icon={<Monitor className="size-4" />}
    summary={data?.devices.find(d => d.id === data.selected_device_id)?.name ?? "PC未選択"}
    help={(
      <>
        <p>操作するPCでポータブルBridgeを起動すると、そのPCのEdgeとWindowsをChatから操作できます。</p>
        <p>このユーザーのChat操作先です。未接続でも別PCやサーバーPCへ自動で切り替わりません。</p>
        <p>対応版Bridgeの「設定ファイル読込」で開き、「接続」を押してください。OperationsではProject・人格・アカウントを選び「専用ブラウザを開く」を押します。本人がログインして対象タブを選んでください。従来版は専用profileに対応していません。</p>
      </>
    )}
  >
    <div className="w-full min-w-0 space-y-3">
      <a className="text-sm underline" href={`${bridgeBase}/download`}>AoiTalk-PC-Bridge.exe を取得</a>
      {error && <p role="alert" className="text-sm text-destructive">PC一覧を取得できませんでした。</p>}
      <div className="space-y-1">
        <Label htmlFor="pc-bridge-device">操作するPC</Label>
        <AppSelect id="pc-bridge-device" className="h-9 w-full rounded-md border bg-background px-2 text-sm" disabled={busy || isLoading}
          value={data?.selected_device_id ?? ""} onChange={event => void run(async () => {
            await api("/selection", "PUT", { device_id: event.target.value || null });
            setScreen(""); if(screenRef.current) URL.revokeObjectURL(screenRef.current);screenRef.current="";
            await mutate();
          })}>
          <option value="">PCを選択してください</option>
          {data?.devices.map(device => <option key={device.id} value={device.id}>{device.name} — {device.online ? "接続済み" : "未接続"}{device.online ? (device.edge_connected ? " / Edge接続済み" : " / Edge未接続") : ""}</option>)}
        </AppSelect>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(async () => { await mutate(); })}>接続を更新</Button>
        <Button size="sm" variant="outline" disabled={busy || !data?.devices.some(d => d.id === data.selected_device_id && d.online)} onClick={() => void run(showScreen)}>画面を確認</Button>
        <Button size="sm" variant="outline" disabled={busy || !data?.selected_device_id} onClick={() => void run(async () => {
          await api(`/devices/${data!.selected_device_id}`, "DELETE"); setScreen(""); await mutate();
        })}>選択PCの登録を解除</Button>
      </div>
      <div className="flex items-end gap-2">
        <div className="grow space-y-1"><Label htmlFor="pc-bridge-name">追加するPCの名前</Label><Input id="pc-bridge-name" value={name} maxLength={80} onChange={event => setName(event.target.value)} placeholder="ノートPC" /></div>
        <Button size="sm" disabled={busy || !name.trim()} onClick={() => void run(async () => {
          const registered = await api("/devices", "POST", { name: name.trim() }); setToken(registered.token); setName(""); await mutate();
        })}>PCを登録</Button>
      </div>
      {token && <div className="space-y-2 rounded-md border p-3">
        <Label htmlFor="pc-bridge-token">接続コード（この画面でのみ表示）</Label>
        <Input id="pc-bridge-token" type="password" value={token} readOnly />
        <div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => void run(async () => { await navigator.clipboard.writeText(token); setMessage("接続コードをコピーしました"); })}>コードをコピー</Button><Button size="sm" onClick={saveConfig}>Bridge設定ファイルを保存</Button></div>
      </div>}
      {message && <p role="status" className="text-sm">{message}</p>}
      {screen && <Image src={screen} unoptimized width={1600} height={1000} alt="選択したPCの画面" className="h-auto w-full rounded-md border" />}
    </div>
  </SettingsDisclosure>;
}

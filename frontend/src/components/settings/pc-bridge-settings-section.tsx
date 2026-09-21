"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import Image from "next/image";
import { Monitor } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SettingsDisclosure } from "./settings-disclosure";

type Device = { id: string; name: string; online: boolean; edge_connected: boolean; capabilities: string[] };
type State = { devices: Device[]; selected_device_id: string | null };
const base = "/api/python-proxy/pc-bridge";

async function api(path: string, method = "GET", body?: unknown) {
  const response = await fetch(base + path, {
    method, credentials: "include", headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : `PC接続に失敗しました (${response.status})`);
  }
  return (await response.json()).result;
}

export function PcBridgeSettingsSection() {
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
    const url = URL.createObjectURL(new Blob([JSON.stringify({ server_url: window.location.origin, token }, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "aoitalk-pc-connection.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function showScreen() {
    const selected = data?.selected_device_id;
    if (!selected) return;
    const response = await fetch(`${base}/devices/${selected}/screen`, { credentials: "include", cache: "no-store" });
    if (!response.ok) throw new Error("選択したPCの画面を取得できませんでした。Bridgeの接続を確認してください。");
    if (screenRef.current) URL.revokeObjectURL(screenRef.current);
    screenRef.current = URL.createObjectURL(await response.blob());
    setScreen(screenRef.current);
  }

  return <SettingsDisclosure title="PC接続" targetId="pc-bridge" icon={<Monitor className="size-4" />} summary={data?.devices.find(d => d.id === data.selected_device_id)?.name ?? "PC未選択"}>
    <div className="max-w-2xl space-y-3">
      <p className="text-sm">操作するPCでポータブルBridgeを起動すると、そのPCのEdgeとWindowsをChatから操作できます。</p>
      <a className="text-sm underline" href={`${base}/download`}>AoiTalk-PC-Bridge.exe を取得</a>
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
        <p className="text-xs text-muted-foreground">このユーザーのChat操作先です。未接続でも別PCやサーバーPCへ自動で切り替わりません。</p>
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
        <p className="text-xs text-muted-foreground">exeの「設定ファイル読込」で開き、「接続」を押してください。Edge操作はexeの「Edge拡張を準備」も実行します。</p>
      </div>}
      {message && <p role="status" className="text-sm">{message}</p>}
      {screen && <Image src={screen} unoptimized width={1600} height={1000} alt="選択したPCの画面" className="h-auto w-full rounded-md border" />}
    </div>
  </SettingsDisclosure>;
}

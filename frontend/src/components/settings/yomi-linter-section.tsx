"use client";

import { useCallback, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";

type Settings = {
  enabled: boolean;
  model_id: string;
  device: string;
  quantization: string;
  confidence_threshold: number;
  log_detections: boolean;
};
type Status = {
  model_loaded: boolean;
  download_status: string;
  effective_quantization?: string | null;
  error?: string | null;
};
type DictionaryItem = {
  id: string; surface: string; reading: string; enabled: boolean;
  target_tts: string[]; accent_type?: number | null; notes?: string;
};
type Candidate = {
  id: string; detected_text: string; confidence: number; tts_engine: string;
  original_text: string; occurrence_count: number;
};

const DEFAULTS: Settings = {
  enabled: false,
  model_id: "ayousanz/yomi-linter-modernbert-ja-130m",
  device: "cpu",
  quantization: "int8",
  confidence_threshold: 0.5,
  log_detections: true,
};

async function pyFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) throw new Error(await response.text());
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function YomiLinterSection() {
  const [settings, setSettings] = useState(DEFAULTS);
  const [status, setStatus] = useState<Status | null>(null);
  const [dictionary, setDictionary] = useState<DictionaryItem[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [surface, setSurface] = useState("");
  const [reading, setReading] = useState("");
  const [accentType, setAccentType] = useState("");
  const [notes, setNotes] = useState("");
  const [targets, setTargets] = useState<string[]>(["voicevox", "aivisspeech"]);
  const [advanced, setAdvanced] = useState(false);
  const [message, setMessage] = useState("");
  const [initialLoading, setInitialLoading] = useState(false);
  const [ready, setReady] = useState(false);
  const loadedRef = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const [settingsResponse, statusResponse] = await Promise.all([
        pyFetch<{ settings: { tts?: { yomi_linter?: Partial<Settings> } } }>("/settings"),
        pyFetch<Status>("/tts/yomi-linter/status"),
      ]);
      setSettings({ ...DEFAULTS, ...settingsResponse.settings.tts?.yomi_linter });
      setStatus(statusResponse);
      try {
        const [dictionaryResponse, candidatesResponse] = await Promise.all([
          pyFetch<{ items: DictionaryItem[] }>("/tts/yomi-dictionary"),
          pyFetch<{ items: Candidate[] }>("/tts/yomi-candidates?status=unresolved"),
        ]);
        setDictionary(dictionaryResponse.items);
        setCandidates(candidatesResponse.items);
      } catch {
        setDictionary([]);
        setCandidates([]);
      }
      setReady(true);
      setMessage("");
    } catch (error) {
      loadedRef.current = false;
      setReady(false);
      setMessage(error instanceof Error ? error.message : "読み設定の取得に失敗しました");
    }
  }, []);

  const loadWhenOpened = (open: boolean) => {
    if (!open || loadedRef.current) return;
    loadedRef.current = true;
    setInitialLoading(true);
    void refresh().finally(() => setInitialLoading(false));
  };

  const saveSetting = async (key: keyof Settings, value: Settings[typeof key]) => {
    setSettings((current) => ({ ...current, [key]: value }));
    try {
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: `tts.yomi_linter.${key}`, value, persist: true }),
      });
      setMessage("保存しました。再起動は不要です。");
      const nextStatus = await pyFetch<Status>("/tts/yomi-linter/status");
      setStatus(nextStatus);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保存に失敗しました");
    }
  };

  const addEntry = async () => {
    if (!surface.trim() || !reading.trim()) return;
    try {
      await pyFetch("/tts/yomi-dictionary", {
        method: "POST",
        body: JSON.stringify({
          surface: surface.trim(), reading: reading.trim(), enabled: true,
          accent_type: accentType === "" ? null : Number(accentType),
          target_tts: targets, notes: notes.trim(),
        }),
      });
      setSurface(""); setReading(""); setAccentType(""); setNotes(""); await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "辞書登録に失敗しました");
    }
  };

  const setEntryEnabled = async (item: DictionaryItem, enabled: boolean) => {
    await pyFetch(`/tts/yomi-dictionary/${item.id}`, {
      method: "PATCH", body: JSON.stringify({ enabled }),
    });
    await refresh();
  };

  const deleteEntry = async (id: string) => {
    await pyFetch(`/tts/yomi-dictionary/${id}`, { method: "DELETE" });
    await refresh();
  };

  const toggleTarget = (target: string, enabled: boolean) => {
    setTargets((current) => enabled
      ? Array.from(new Set([...current, target]))
      : current.filter((value) => value !== target));
  };

  const resolveCandidate = async (id: string, candidateStatus: "resolved" | "ignored") => {
    await pyFetch(`/tts/yomi-candidates/${id}`, {
      method: "PATCH", body: JSON.stringify({ status: candidateStatus }),
    });
    await refresh();
  };

  return (
    <div className="space-y-3">
      <SettingsDisclosure
        title="誤読リスク検出"
        contentClassName="space-y-4"
        onOpenChange={loadWhenOpened}
      >
        {initialLoading && <p className="text-xs text-muted-foreground">読み設定を取得中...</p>}
        <fieldset disabled={!ready} className="contents disabled:opacity-60">
          <div className="flex items-center justify-between gap-4">
            <div><Label>Yomi Linter</Label><p className="text-xs text-muted-foreground">誤読候補を検出します。読みの推測や原文の書換えは行いません。</p></div>
            <Checkbox checked={settings.enabled} onCheckedChange={(value) => void saveSetting("enabled", value === true)} />
          </div>
          <div className="flex flex-wrap gap-2 text-xs">
            <Badge variant={status?.model_loaded ? "default" : "secondary"}>モデル: {status?.model_loaded ? "ロード済み" : "未ロード"}</Badge>
            <Badge variant="outline">取得状態: {status?.download_status ?? "未確認"}</Badge>
            <Badge variant="outline">デバイス: {settings.device}</Badge>
            <Badge variant="outline">再起動不要</Badge>
          </div>
          <div className="space-y-1">
            <Label htmlFor="yomi-threshold">検出しきい値: {settings.confidence_threshold.toFixed(2)}</Label>
            <Input id="yomi-threshold" type="range" min="0" max="1" step="0.05" value={settings.confidence_threshold} onChange={(event) => setSettings((current) => ({ ...current, confidence_threshold: Number(event.target.value) }))} onMouseUp={() => void saveSetting("confidence_threshold", settings.confidence_threshold)} onTouchEnd={() => void saveSetting("confidence_threshold", settings.confidence_threshold)} />
          </div>
          <div className="flex items-center gap-2"><Checkbox checked={settings.log_detections} onCheckedChange={(value) => void saveSetting("log_detections", value === true)} /><Label>検出を構造化ログへ記録</Label></div>
          <Button variant="ghost" size="sm" onClick={() => setAdvanced((value) => !value)}>詳細設定 {advanced ? "を閉じる" : "を開く"}</Button>
          {advanced && <div className="grid gap-3 md:grid-cols-3">
            <div><Label>モデルID</Label><Input value={settings.model_id} onChange={(event) => setSettings((current) => ({ ...current, model_id: event.target.value }))} onBlur={() => void saveSetting("model_id", settings.model_id)} /></div>
            <div><Label>デバイス</Label><select className="h-9 w-full rounded-md border bg-background px-3 text-sm" value={settings.device} onChange={(event) => void saveSetting("device", event.target.value)}><option value="cpu">CPU</option><option value="auto">自動</option><option value="cuda">CUDA</option></select></div>
            <div><Label>量子化</Label><select className="h-9 w-full rounded-md border bg-background px-3 text-sm" value={settings.quantization} onChange={(event) => void saveSetting("quantization", event.target.value)}><option value="int8">INT8</option><option value="none">なし</option></select></div>
          </div>}
          {status?.error && <p className="text-xs text-destructive">{status.error}</p>}
          {message && <p className="text-xs text-muted-foreground">{message}</p>}
        </fieldset>
      </SettingsDisclosure>

      <SettingsDisclosure title="共通読み辞書" onOpenChange={loadWhenOpened}>
        {initialLoading && <p className="text-xs text-muted-foreground">読み設定を取得中...</p>}
        {!ready && !initialLoading && message && <p className="text-xs text-destructive">{message}</p>}
        <fieldset disabled={!ready} className="contents disabled:opacity-60">
        <div className="grid gap-2 md:grid-cols-[1fr_1fr_8rem_auto]"><Input placeholder="表記（例: 魔王魂）" value={surface} onChange={(e) => setSurface(e.target.value)} /><Input placeholder="読み（例: マオウダマシイ）" value={reading} onChange={(e) => setReading(e.target.value)} /><Input type="number" min="0" placeholder="アクセント任意" value={accentType} onChange={(e) => setAccentType(e.target.value)} /><Button onClick={() => void addEntry()}>登録</Button></div>
        <Input placeholder="備考（任意）" value={notes} onChange={(event) => setNotes(event.target.value)} />
        <div className="flex flex-wrap gap-4 text-xs"><span className="text-muted-foreground">適用対象:</span>{["voicevox", "aivisspeech", "irodori_tts", "miotts", "voiceroid", "aivoice", "cevio", "nijivoice"].map((target) => <label key={target} className="flex items-center gap-1"><Checkbox checked={targets.includes(target)} onCheckedChange={(value) => toggleTarget(target, value === true)} />{target}</label>)}</div>
        <p className="text-xs text-muted-foreground">初期状態ではVOICEVOX / AivisSpeechのユーザー辞書へ反映します。他のTTSは検出のみです。</p>
        {dictionary.map((item) => <div key={item.id} className="flex items-center justify-between gap-3 rounded-md border p-2 text-sm"><div><span className="font-medium">{item.surface}</span><span className="mx-2 text-muted-foreground">→</span>{item.reading}{item.accent_type != null && <span className="ml-2 text-xs text-muted-foreground">アクセント {item.accent_type}</span>}<p className="text-xs text-muted-foreground">{item.target_tts.join(", ") || "すべて"}{item.notes ? ` / ${item.notes}` : ""}</p></div><div className="flex items-center gap-2"><Checkbox checked={item.enabled} onCheckedChange={(value) => void setEntryEnabled(item, value === true)} /><Button size="sm" variant="ghost" onClick={() => void deleteEntry(item.id)}>削除</Button></div></div>)}
        {!dictionary.length && <p className="text-xs text-muted-foreground">辞書項目はありません。</p>}
        </fieldset>
      </SettingsDisclosure>

      <SettingsDisclosure
        title="未解決の誤読候補"
        contentClassName="space-y-2"
        onOpenChange={loadWhenOpened}
      >
        {initialLoading && <p className="text-xs text-muted-foreground">読み設定を取得中...</p>}
        {!ready && !initialLoading && message && <p className="text-xs text-destructive">{message}</p>}
        <fieldset disabled={!ready} className="contents disabled:opacity-60">
        {candidates.map((item) => <div key={item.id} className="rounded-md border p-2 text-sm"><div className="flex items-center justify-between gap-3"><div><span className="font-medium">{item.detected_text}</span><span className="ml-2 text-xs text-muted-foreground">{item.tts_engine} / {(item.confidence * 100).toFixed(1)}% / {item.occurrence_count}回</span></div><div className="flex gap-1"><Button size="sm" variant="outline" onClick={() => void resolveCandidate(item.id, "resolved")}>解決済み</Button><Button size="sm" variant="ghost" onClick={() => void resolveCandidate(item.id, "ignored")}>無視</Button></div></div><p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{item.original_text}</p></div>)}
        {!candidates.length && <p className="text-xs text-muted-foreground">未解決候補はありません。</p>}
        </fieldset>
      </SettingsDisclosure>
    </div>
  );
}

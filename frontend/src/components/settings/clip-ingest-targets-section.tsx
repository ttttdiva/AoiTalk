"use client";

import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  ClipIngestTarget,
  normalizeClipIngestTargets,
  parseClipIngestTargets,
  selectClipIngestFallback,
} from "@/lib/clip-ingest-settings";
import { cn } from "@/lib/utils";
import { ChevronRight, FileText, Loader2, Plus, Tags, Trash2 } from "lucide-react";
import { toast } from "sonner";

type PageHit = {
  id: string;
  system_key: string | null;
  title: string;
  aliases: string[];
  breadcrumb: string[];
};

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || response.statusText);
  }
  return response.json();
}

export function ClipIngestTargetsSection() {
  const [targets, setTargets] = useState<ClipIngestTarget[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [pages, setPages] = useState<PageHit[]>([]);
  const [sectionOpen, setSectionOpen] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggleExpanded = (nodeId: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
  };

  useEffect(() => {
    apiFetch<{ settings: unknown }>("/api/users/me/settings")
      .then((data) => setTargets(parseClipIngestTargets(data.settings)))
      .catch((error) => toast.error(`取り込み先設定を取得できません: ${error.message}`))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!pickerOpen) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      fetch(`/api/docs/pages?q=${encodeURIComponent(query)}&limit=30`, {
        signal: controller.signal,
      })
        .then((response) => response.ok ? response.json() as Promise<{ pages: PageHit[] }> : { pages: [] })
        .then((data) => setPages(data.pages ?? []))
        .catch(() => { if (!controller.signal.aborted) setPages([]); });
    }, 80);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [pickerOpen, query]);

  const addPage = (page: PageHit) => {
    if (targets.some((target) => target.node_id === page.id)) {
      toast.info("このDocsノードは登録済みです");
      return;
    }
    setTargets((current) => [...current, {
      node_id: page.id,
      ...(page.system_key ? { node_system_key: page.system_key } : {}),
      label: page.title,
      breadcrumb: page.breadcrumb,
      routing_hint: "",
      enabled: true,
      fallback: false,
    }]);
    setExpanded((current) => new Set(current).add(page.id));
    setPickerOpen(false);
    setQuery("");
  };

  const updateTarget = (nodeId: string, patch: Partial<ClipIngestTarget>) => {
    setTargets((current) => current.map((target) => target.node_id === nodeId ? { ...target, ...patch } : target));
  };

  const save = async () => {
    setSaving(true);
    try {
      const normalized = normalizeClipIngestTargets(targets);
      const result = await apiFetch<{ settings: unknown }>("/api/users/me/settings", {
        method: "PATCH",
        body: JSON.stringify({ clip_ingest: { targets: normalized } }),
      });
      setTargets(parseClipIngestTargets(result.settings));
      toast.success("/clip 取り込み先を保存しました");
    } catch (error) {
      toast.error(`保存できません: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader
        role="button"
        tabIndex={0}
        aria-expanded={sectionOpen}
        onClick={() => setSectionOpen((v) => !v)}
        onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSectionOpen((v) => !v); } }}
        className="flex-row items-center justify-between gap-3 cursor-pointer"
      >
        <div className="flex min-w-0 items-start gap-2">
          <ChevronRight className={cn("mt-1 size-4 shrink-0 text-muted-foreground transition-transform", sectionOpen && "rotate-90")} />
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-base">/clip 取り込み先{!loading && targets.length ? <Badge variant="secondary">{targets.length}件</Badge> : null}</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">/clip が保存先として選べるDocsノードを限定します。</p>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={(event) => { event.stopPropagation(); setSectionOpen(true); setPickerOpen(true); }} disabled={loading}>
          <Plus className="size-4" />追加
        </Button>
      </CardHeader>
      {sectionOpen ? (
      <CardContent className="space-y-4">
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />読み込み中...</div>
        ) : targets.length === 0 ? (
          <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">取り込み先は未登録です。この状態では /clip はDocsを変更しません。</p>
        ) : targets.map((target) => {
          const isOpen = expanded.has(target.node_id);
          return (
          <div key={target.node_id} className="rounded-lg border">
            <div
              role="button"
              tabIndex={0}
              aria-expanded={isOpen}
              onClick={() => toggleExpanded(target.node_id)}
              onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleExpanded(target.node_id); } }}
              className="flex cursor-pointer items-start justify-between gap-3 rounded-lg p-4 hover:bg-muted/40"
            >
              <div className="flex min-w-0 items-start gap-2">
                <ChevronRight className={cn("mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform", isOpen && "rotate-90")} />
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{target.label}</span>
                    {target.fallback ? <Badge variant="secondary">未分類時</Badge> : null}
                    {!target.enabled ? <Badge variant="outline">無効</Badge> : null}
                  </div>
                  <p className="truncate text-xs text-muted-foreground">{[...target.breadcrumb, target.label].join(" / ")}</p>
                  {isOpen ? <p className="mt-1 font-mono text-[10px] text-muted-foreground">{target.node_id}</p> : null}
                </div>
              </div>
              <Button variant="ghost" size="icon-sm" aria-label={`${target.label}を削除`} onClick={(event) => { event.stopPropagation(); setTargets((current) => current.filter((item) => item.node_id !== target.node_id)); }}><Trash2 className="size-4" /></Button>
            </div>
            {isOpen ? (
              <div className="space-y-3 border-t p-4 pt-3">
                <div className="space-y-1.5">
                  <Label htmlFor={`clip-hint-${target.node_id}`}>ルーティング説明</Label>
                  <Textarea id={`clip-hint-${target.node_id}`} value={target.routing_hint} onChange={(event) => updateTarget(target.node_id, { routing_hint: event.target.value })} placeholder="この保存先に適した話題や情報を説明してください" rows={2} />
                </div>
                <div className="flex flex-wrap gap-5 text-sm">
                  <Label className="flex items-center gap-2"><Checkbox checked={target.enabled} onCheckedChange={(checked) => updateTarget(target.node_id, { enabled: checked === true })} />有効</Label>
                  <Label className="flex items-center gap-2"><Checkbox checked={target.fallback} onCheckedChange={(checked) => setTargets((current) => selectClipIngestFallback(current, checked === true ? target.node_id : null))} />未分類時の保存先</Label>
                </div>
              </div>
            ) : null}
          </div>
          );
        })}
        <div className="flex justify-end"><Button onClick={save} disabled={loading || saving}>{saving ? <Loader2 className="size-4 animate-spin" /> : null}{saving ? "保存中..." : "保存"}</Button></div>
      </CardContent>
      ) : null}

      <CommandDialog open={pickerOpen} onOpenChange={setPickerOpen} title="/clip 取り込み先を追加" description="既存のDocsノードをタイトルまたはエイリアスで検索します">
        <Command shouldFilter={false}>
          <CommandInput value={query} onValueChange={setQuery} placeholder="ページ名またはエイリアス..." />
          <CommandList><CommandEmpty>該当するページがありません</CommandEmpty><CommandGroup heading={query.trim() ? "ページを検索" : "最近のDocsページ"}>
            {pages.map((page) => <CommandItem key={page.id} value={`${page.title} ${page.aliases.join(" ")}`} onSelect={() => addPage(page)} className="items-start">
              <FileText className="mt-0.5 size-4 text-muted-foreground" /><div className="min-w-0"><div className="truncate font-medium">{page.title}</div><div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground"><span className="truncate">{page.breadcrumb.join(" / ") || "Docs"}</span>{page.aliases.length ? <span className="inline-flex min-w-0 items-center gap-1 truncate"><Tags className="size-3" />{page.aliases.join(", ")}</span> : null}</div></div>
            </CommandItem>)}
          </CommandGroup></CommandList>
        </Command>
      </CommandDialog>
    </Card>
  );
}

"use client";

import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  clipIngestFallbackId,
  ClipIngestTarget,
  formatClipIngestBreadcrumb,
  isAllowedClipIngestTarget,
  normalizeClipIngestTargets,
  parseClipIngestTargets,
  removeClipIngestTarget,
  selectClipIngestFallback,
  setClipIngestTargetEnabled,
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
        .then((data) => setPages(
          (data.pages ?? []).filter((page) =>
            isAllowedClipIngestTarget(page),
          ),
        ))
        .catch(() => { if (!controller.signal.aborted) setPages([]); });
    }, 80);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [pickerOpen, query]);

  const addPage = (page: PageHit) => {
    if (!isAllowedClipIngestTarget(page)) {
      toast.error("Film配下はクリップ取り込み先にできません");
      return;
    }
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

  const fallbackId = clipIngestFallbackId(targets);

  const save = async () => {
    setSaving(true);
    try {
      const normalized = normalizeClipIngestTargets(targets);
      const result = await apiFetch<{ settings: unknown }>("/api/users/me/settings", {
        method: "PATCH",
        body: JSON.stringify({ clip_ingest: { targets: normalized } }),
      });
      setTargets(parseClipIngestTargets(result.settings));
      toast.success("クリップ取り込み先を保存しました");
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
            <CardTitle className="flex items-center gap-2 text-base">クリップ取り込み先{!loading && targets.length ? <Badge variant="secondary">{targets.length}件</Badge> : null}</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">クリップを自動分類して保存するDocsノードを設定します。</p>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={(event) => { event.stopPropagation(); setSectionOpen(true); setPickerOpen(true); }} disabled={loading}>
          <Plus className="size-4" />追加
        </Button>
      </CardHeader>
      {sectionOpen ? (
      <CardContent className="space-y-3">
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />読み込み中...</div>
        ) : targets.length === 0 ? (
          <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">取り込み先は未登録です。この状態ではクリップ取り込みはDocsを変更しません。</p>
        ) : (
          <div className="overflow-hidden rounded-lg border">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b bg-muted/20 px-3 py-2">
              <Label htmlFor="clip-ingest-fallback" className="text-sm">分類できなかった場合</Label>
              <Select
                value={fallbackId ?? "none"}
                onValueChange={(value) => setTargets((current) =>
                  selectClipIngestFallback(current, value && value !== "none" ? value : null),
                )}
              >
                <SelectTrigger id="clip-ingest-fallback" data-testid="clip-ingest-fallback-select" className="w-full max-w-64">
                  <SelectValue>
                    {targets.find((target) => target.node_id === fallbackId)?.label ?? "保存しない"}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">保存しない</SelectItem>
                  {targets.filter((target) => target.enabled).map((target) => (
                    <SelectItem key={target.node_id} value={target.node_id}>{target.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="divide-y">
            {targets.map((target) => {
           const isOpen = expanded.has(target.node_id);
           const breadcrumb = formatClipIngestBreadcrumb(target);
           return (
          <div key={target.node_id} data-testid={`clip-ingest-target-${target.node_id}`}>
            <div
              role="button"
              tabIndex={0}
              aria-expanded={isOpen}
              onClick={() => toggleExpanded(target.node_id)}
              onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleExpanded(target.node_id); } }}
              className="flex min-h-11 cursor-pointer items-center justify-between gap-2 px-3 py-2 hover:bg-muted/40"
            >
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <ChevronRight className={cn("size-4 shrink-0 text-muted-foreground transition-transform", isOpen && "rotate-90")} />
                <div className="flex min-w-0 flex-1 flex-col gap-0.5 sm:flex-row sm:items-center sm:gap-3">
                  <span className="shrink-0 font-medium">{target.label}</span>
                  <p className="truncate text-xs text-muted-foreground" title={breadcrumb}>{breadcrumb}</p>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <button
                  type="button"
                  role="switch"
                  aria-checked={target.enabled}
                  aria-label={`${target.label}を${target.enabled ? "無効" : "有効"}にする`}
                  onClick={(event) => {
                    event.stopPropagation();
                    setTargets((current) => setClipIngestTargetEnabled(current, target.node_id, !target.enabled));
                  }}
                  className={cn(
                    "relative h-5 w-9 rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    target.enabled ? "bg-primary" : "bg-muted-foreground/35",
                  )}
                >
                  <span className={cn(
                    "absolute top-0.5 size-4 rounded-full bg-background shadow transition-transform",
                    target.enabled ? "left-[18px]" : "left-0.5",
                  )} />
                </button>
                <Button variant="ghost" size="icon-sm" aria-label={`${target.label}を削除`} onClick={(event) => { event.stopPropagation(); setTargets((current) => removeClipIngestTarget(current, target.node_id)); }}><Trash2 className="size-4" /></Button>
              </div>
            </div>
            {isOpen ? (
              <div className="space-y-1.5 border-t bg-muted/10 px-3 py-2">
                <div className="space-y-1.5">
                  <Label htmlFor={`clip-hint-${target.node_id}`}>保存先判定用の説明</Label>
                  <Textarea id={`clip-hint-${target.node_id}`} value={target.routing_hint} onChange={(event) => updateTarget(target.node_id, { routing_hint: event.target.value })} placeholder="この保存先に適した話題や情報を説明してください" rows={2} />
                  <p className="text-xs text-muted-foreground">この説明は保存先の判定にだけ使用されます。保存される見出しやノード階層の生成規則は変更しません。</p>
                </div>
              </div>
            ) : null}
          </div>
           );
        })}
            </div>
          </div>
        )}
        <div className="flex justify-end"><Button onClick={save} disabled={loading || saving}>{saving ? <Loader2 className="size-4 animate-spin" /> : null}{saving ? "保存中..." : "保存"}</Button></div>
      </CardContent>
      ) : null}

      <CommandDialog open={pickerOpen} onOpenChange={setPickerOpen} title="クリップ取り込み先を追加" description="既存のDocsノードをタイトルまたはエイリアスで検索します">
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

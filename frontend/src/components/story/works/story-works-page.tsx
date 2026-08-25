"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";
import useSWR from "swr";
import {
  Archive,
  Clock3,
  Loader2,
  MoreVertical,
  PenLine,
  Plus,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { useConfirm } from "@/hooks/use-confirm";
import { storyApi } from "@/lib/story/api";
import { listFrom, normalizeWork, objectOf, type StoryWorkView } from "@/lib/story/view-model";
import { StoryStudioWorkspaceNavigation } from "@/components/story/shell/story-studio-workspace-navigation";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";

function formatUpdatedAt(value: string | null): string {
  if (!value) return "更新日時不明";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "更新日時不明";
  return new Intl.DateTimeFormat("ja-JP", { month: "short", day: "numeric" }).format(date);
}

function kindLabel(kind: string): string {
  const value = kind.trim().toLowerCase();
  if (value === "series") return "SERIES";
  if (value === "episode") return "EPISODE";
  if (value === "trpg") return "TRPG";
  return "SCENARIO";
}

function statusLabel(status: string): string {
  if (status === "completed" || status === "done") return "完成";
  if (status === "in_progress" || status === "writing") return "執筆中";
  if (status === "archived") return "アーカイブ";
  return "企画中";
}

export function StoryWorksPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const confirm = useConfirm();
  const kindFilter = searchParams.get("kind")?.trim().toLowerCase() || "all";
  const { data, error, isLoading, mutate } = useSWR("story-works", () => storyApi.listWorks());
  const works = useMemo(
    () => listFrom(data, "works").map(normalizeWork).sort((left, right) => (right.updatedAt || "").localeCompare(left.updatedAt || "")),
    [data],
  );
  const [query, setQuery] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [selected, setSelected] = useState<StoryWorkView | null>(null);
  const [rename, setRename] = useState("");
  const [busy, setBusy] = useState(false);
  const filtered = works.filter((work) => {
    if (kindFilter !== "all" && work.kind.toLowerCase() !== kindFilter) return false;
    return !query.trim() || `${work.title} ${work.synopsis}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  });
  const activeWorks = works.filter((work) => !["completed", "done", "archived"].includes(work.status));
  const completedWorks = works.filter((work) => ["completed", "done"].includes(work.status));
  const totalCharacters = works.reduce((sum, work) => sum + work.charactersCount, 0);

  useWorkspaceShellRegistration({
    id: "story-works-workspace",
    workspaceNavigation: <StoryStudioWorkspaceNavigation />,
    // Library has no persistent inspector; selected work details stay in the card and route.
    contextRail: null,
    priority: 20,
  });

  const createWork = async () => {
    if (!title.trim()) return;
    setBusy(true);
    try {
      const result = await storyApi.createWork({ kind: "novel", title: title.trim(), synopsis: "", plot: "", style_guide: "", status: "planning", target_episode_chars: 6000, planned_episode_count: null, model_override: {}, ui_state: {} });
      const record = objectOf(result);
      const work = objectOf(record.work ?? result);
      const workId = typeof work.id === "string" ? work.id : typeof record.id === "string" ? record.id : null;
      await mutate();
      setCreateOpen(false);
      setTitle("");
      if (workId) window.location.assign(`/scenarios/${encodeURIComponent(workId)}/settings`);
    } catch (createError) {
      toast.error(createError instanceof Error ? createError.message : "作品を作成できませんでした");
    } finally {
      setBusy(false);
    }
  };

  const renameWork = async () => {
    if (!selected || !rename.trim()) return;
    setBusy(true);
    try {
      await storyApi.updateWork(selected.id, { title: rename.trim() });
      await mutate();
      setSelected(null);
      toast.success("作品名を変更しました");
    } catch (renameError) {
      toast.error(renameError instanceof Error ? renameError.message : "作品名を変更できませんでした");
    } finally {
      setBusy(false);
    }
  };

  const archiveWork = async (work: StoryWorkView) => {
    const approved = await confirm({ title: "作品を削除", description: `「${work.title}」を削除しますか？\n削除しても内容は保持され、一覧から外れます。`, confirmLabel: "削除", destructive: true });
    if (!approved) return;
    try {
      await storyApi.deleteWork(work.id);
      await mutate();
      toast.success("作品を削除しました");
    } catch (deleteError) {
      toast.error(deleteError instanceof Error ? deleteError.message : "作品を削除できませんでした");
    }
  };

  return (
    <div className="flex min-h-full min-w-0 flex-col bg-background text-on-surface" data-testid="story-works-page">
      <header className="flex shrink-0 items-end justify-between gap-4 border-b border-border-subtle bg-background px-6 py-6">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-on-surface-variant">Scenario Studio</p>
          <h1 className="mt-1 text-[24px] font-semibold leading-8 tracking-tight text-foreground">Story Works</h1>
          <p className="mt-1 text-[13px] leading-[18px] text-muted-foreground">作品を整理し、分岐と執筆コンテキストを一つのワークスペースで管理します。</p>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <Button className="h-8 rounded-sm bg-primary-container px-4 text-xs font-medium text-on-primary-container hover:bg-primary" onClick={() => setCreateOpen(true)}>
            <Plus className="size-4" />新しい作品
          </Button>
        </div>
      </header>
      <div className="min-h-0 flex-1 overflow-auto px-6 py-6">
        <div className="grid gap-4 sm:grid-cols-3">
          <div className="rounded-md border border-border-subtle bg-surface-charcoal p-4">
            <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-on-surface-variant">Total Works</p>
            <p className="mt-2 text-[24px] font-semibold leading-8 text-foreground">{works.length}</p>
          </div>
          <div className="rounded-md border border-border-subtle bg-surface-charcoal p-4">
            <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-on-surface-variant">Active Scenarios</p>
            <div className="mt-2 flex items-end gap-2"><p className="text-[24px] font-semibold leading-8 text-foreground">{activeWorks.length}</p><span className="pb-1 text-xs text-muted-foreground">{completedWorks.length} 完成</span></div>
          </div>
          <div className="rounded-md border border-border-subtle bg-surface-charcoal p-4">
            <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-on-surface-variant">Characters in works</p>
            <p className="mt-2 text-[24px] font-semibold leading-8 text-foreground">{totalCharacters.toLocaleString("ja-JP")}</p>
          </div>
        </div>
        <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative w-64">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input className="h-8 rounded-sm border-border-subtle bg-surface-container-low pl-8 text-xs text-on-surface placeholder:text-muted-foreground focus-visible:border-primary focus-visible:ring-0" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="作品を検索…" aria-label="作品を検索" />
            </div>
            <AppSelect className="h-8 min-w-28 rounded-sm border-border-subtle bg-surface-container-high text-xs text-on-surface" value={kindFilter} onValueChange={(next) => { router.replace(next === "all" ? "/scenarios" : `/scenarios?kind=${encodeURIComponent(next)}`); }} aria-label="作品種別">
              <option value="all">すべての種類</option><option value="novel">小説</option><option value="trpg">TRPG</option>
            </AppSelect>
          </div>
          <span className="text-[11px] text-muted-foreground">{filtered.length}作品 · 更新日時順</span>
        </div>
        {isLoading && !data ? (
          <div className="flex items-center justify-center py-24 text-sm text-muted-foreground"><Loader2 className="mr-2 size-4 animate-spin" />作品を読み込み中…</div>
        ) : error ? (
          <div className="mx-auto mt-10 max-w-lg rounded-md border border-error-container bg-surface-charcoal p-6 text-center text-sm text-destructive">作品一覧を読み込めませんでした。<Button variant="outline" size="sm" className="mt-4 border-border-subtle" onClick={() => void mutate()}>再読み込み</Button></div>
        ) : (
          <div className="mt-4 grid gap-4 pb-8 sm:grid-cols-2 xl:grid-cols-3">
            {filtered.map((work) => (
              <article key={work.id} className="group flex min-h-[280px] flex-col overflow-hidden rounded-md border border-border-subtle bg-surface-charcoal transition-colors hover:border-outline" data-testid={`story-work-card-${work.id}`}>
                <div className="flex items-start justify-between border-b border-border-subtle px-4 py-3">
                  <span className="rounded-sm border border-primary/30 bg-primary/10 px-2 py-0.5 font-mono text-[10px] font-medium tracking-wide text-primary">{kindLabel(work.kind)}</span>
                  <DropdownMenu>
                    <DropdownMenuTrigger render={<Button variant="ghost" size="icon-xs" className="-mr-1 text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface" aria-label={`${work.title}のメニュー`} />}><MoreVertical className="size-4" /></DropdownMenuTrigger>
                    <DropdownMenuContent align="end"><DropdownMenuItem onClick={() => { setSelected(work); setRename(work.title); }}><PenLine className="size-3.5" />名前を変更</DropdownMenuItem><DropdownMenuSeparator /><DropdownMenuItem variant="destructive" onClick={() => void archiveWork(work)}><Trash2 className="size-3.5" />削除</DropdownMenuItem></DropdownMenuContent>
                  </DropdownMenu>
                </div>
                <Link href={`/scenarios/${encodeURIComponent(work.id)}`} className="flex flex-1 flex-col p-4">
                  <h2 className="line-clamp-2 text-[16px] font-semibold leading-6 text-on-surface transition-colors group-hover:text-primary">{work.title}</h2>
                  <p className="mt-2 line-clamp-3 text-[13px] leading-[18px] text-on-surface-variant">{work.synopsis || "あらすじはまだありません。"}</p>
                  <div className="mt-auto grid grid-cols-2 gap-2 pt-4">
                    <div><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Episodes</p><p className="mt-1 font-mono text-[13px] text-on-surface">{work.episodeCount}</p></div>
                    <div><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Characters</p><p className="mt-1 font-mono text-[13px] text-on-surface">{work.charactersCount}</p></div>
                  </div>
                </Link>
                <div className="flex items-center justify-between border-t border-border-subtle bg-surface-container-lowest/50 px-4 py-3 text-[11px] text-muted-foreground">
                  <span className="flex items-center gap-1.5"><Clock3 className="size-3.5" />{formatUpdatedAt(work.updatedAt)}</span>
                  <span className="flex items-center gap-1.5"><Sparkles className="size-3.5 text-primary" />{statusLabel(work.status)}</span>
                </div>
              </article>
            ))}
            <button type="button" className="flex min-h-[280px] flex-col items-center justify-center rounded-md border border-dashed border-border-subtle bg-surface-charcoal text-on-surface-variant transition-colors hover:border-primary/50 hover:bg-surface-container-high hover:text-primary" onClick={() => setCreateOpen(true)}>
              <Plus className="mb-2 size-10" /><span className="text-xs font-medium">Create New Work</span>
            </button>
          </div>
        )}
        {!isLoading && !error && !filtered.length && <div className="mt-8 rounded-md border border-dashed border-border-subtle bg-surface-charcoal p-10 text-center text-sm text-muted-foreground"><Archive className="mx-auto size-8 opacity-60" /><p className="mt-3">条件に一致する作品がありません。</p><Button className="mt-4" onClick={() => setCreateOpen(true)}><Plus className="size-3.5" />作品を作成</Button></div>}
      </div>
      <Dialog open={createOpen} onOpenChange={setCreateOpen}><DialogContent><DialogHeader><DialogTitle>新しい作品</DialogTitle></DialogHeader><Input autoFocus value={title} onChange={(event) => setTitle(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void createWork(); }} placeholder="作品名" aria-label="新しい作品名" /><DialogFooter><Button variant="outline" onClick={() => setCreateOpen(false)}>キャンセル</Button><Button onClick={() => void createWork()} disabled={busy || !title.trim()}>{busy && <Loader2 className="size-3.5 animate-spin" />}作成</Button></DialogFooter></DialogContent></Dialog>
      <Dialog open={Boolean(selected)} onOpenChange={(open) => { if (!open) setSelected(null); }}><DialogContent><DialogHeader><DialogTitle>作品名を変更</DialogTitle></DialogHeader><Input autoFocus value={rename} onChange={(event) => setRename(event.target.value)} /><DialogFooter><Button variant="outline" onClick={() => setSelected(null)}>キャンセル</Button><Button onClick={() => void renameWork()} disabled={busy || !rename.trim()}>保存</Button></DialogFooter></DialogContent></Dialog>
    </div>
  );
}

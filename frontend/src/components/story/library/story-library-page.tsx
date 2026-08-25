"use client";

import { useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";
import { BookOpen, Library, Loader2, Plus, Users } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { storyApi } from "@/lib/story/api";
import { normalizeCharacters, normalizeRulebooks, type StoryCharacterView, type StoryRulebookView } from "@/lib/story/view-model";
import { StoryStudioWorkspaceNavigation } from "@/components/story/shell/story-studio-workspace-navigation";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";

export function StoryLibraryPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [tab, setTab] = useState(searchParams.get("tab") === "rules" ? "rules" : "cast");
  const [query, setQuery] = useState("");
  const { data: charactersData, isLoading: charactersLoading, mutate: mutateCharacters } = useSWR("story-library-characters", () => storyApi.listCharacters());
  const { data: rulesData, isLoading: rulesLoading, mutate: mutateRules } = useSWR("story-library-rules", () => storyApi.listRulebooks());
  const characters = useMemo(() => (charactersData ? normalizeCharacters(charactersData) : []).filter((item) => !query.trim() || `${item.name} ${item.summary} ${item.keywords.join(" ")}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [charactersData, query]);
  const rules = useMemo(() => (rulesData ? normalizeRulebooks(rulesData) : []).filter((item) => !query.trim() || `${item.name} ${item.content}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [rulesData, query]);
  const [newCharacterOpen, setNewCharacterOpen] = useState(false);
  const [newRuleOpen, setNewRuleOpen] = useState(false);
  const [name, setName] = useState("");
  const [content, setContent] = useState("");

  useWorkspaceShellRegistration({
    id: "story-library-workspace",
    workspaceNavigation: <StoryStudioWorkspaceNavigation />,
    priority: 20,
  });

  const switchTab = (next: string) => { setTab(next); router.replace(`/scenarios/library?tab=${next}`); };
  const createCharacter = async () => { if (!name.trim()) return; try { await storyApi.createCharacter({ name: name.trim(), aliases: [], summary: "", description: "", notes: "", ai_mode: "keyword", keywords: [] }); await mutateCharacters(); setName(""); setNewCharacterOpen(false); toast.success("人物を作成しました"); } catch (error) { toast.error(error instanceof Error ? error.message : "人物を作成できませんでした"); } };
  const createRule = async () => { if (!name.trim()) return; try { await storyApi.createRulebook({ name: name.trim(), content }); await mutateRules(); setName(""); setContent(""); setNewRuleOpen(false); toast.success("ルールブックを作成しました"); } catch (error) { toast.error(error instanceof Error ? error.message : "ルールブックを作成できませんでした"); } };

  return <div className="min-h-full bg-background p-6 text-on-surface" data-testid="story-library-page"><div className="mx-auto max-w-6xl"><header className="flex flex-wrap items-end gap-4 border-b border-border-subtle pb-5"><div className="min-w-0 flex-1"><div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-on-surface-variant">Scenario Studio</div><h1 className="mt-1 flex items-center gap-2 text-2xl font-semibold text-foreground"><Library className="size-5" />共有ライブラリ</h1><p className="mt-2 text-[13px] text-muted-foreground">作品をまたいで再利用する人物とルールブックを管理します。</p></div><Button variant="outline" onClick={() => router.push("/scenarios")}>← 作品一覧</Button></header><div className="mt-5 flex flex-wrap items-center gap-2"><div className="flex rounded-sm border border-border-subtle bg-surface-charcoal p-1"><Button variant={tab === "cast" ? "secondary" : "ghost"} size="sm" onClick={() => switchTab("cast")}><Users className="size-3.5" />人物</Button><Button variant={tab === "rules" ? "secondary" : "ghost"} size="sm" onClick={() => switchTab("rules")}><BookOpen className="size-3.5" />ルールブック</Button></div><div className="ml-auto flex min-w-56 items-center gap-2"><Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="ライブラリを検索" aria-label="ライブラリを検索" /></div>{tab === "cast" ? <Button onClick={() => setNewCharacterOpen(true)}><Plus className="size-3.5" />人物を作成</Button> : <Button onClick={() => setNewRuleOpen(true)}><Plus className="size-3.5" />ルールを作成</Button>}</div>{tab === "cast" ? charactersLoading ? <Loading /> : <LibraryGrid items={characters} empty="人物はまだありません。" render={(character) => <Card key={character.id} className="rounded-md border-border-subtle bg-surface-charcoal"><CardHeader><CardTitle>{character.name}</CardTitle></CardHeader><CardContent><p className="line-clamp-3 text-sm text-muted-foreground">{character.description || character.summary || "説明なし"}</p><div className="mt-3 text-xs text-muted-foreground">{character.aiMode} · {character.keywords.length}キーワード</div></CardContent></Card>} /> : rulesLoading ? <Loading /> : <LibraryGrid items={rules} empty="ルールブックはまだありません。" render={(rule) => <Card key={rule.id} className="rounded-md border-border-subtle bg-surface-charcoal"><CardHeader><CardTitle>{rule.name}</CardTitle></CardHeader><CardContent><p className="line-clamp-5 whitespace-pre-wrap text-sm text-muted-foreground">{rule.content || "本文なし"}</p></CardContent></Card>} />}</div><SimpleLibraryDialog open={newCharacterOpen} onOpenChange={setNewCharacterOpen} title="人物を作成" name={name} setName={setName} content="" setContent={setContent} onSubmit={() => void createCharacter()} contentLabel={null} /><SimpleLibraryDialog open={newRuleOpen} onOpenChange={setNewRuleOpen} title="ルールブックを作成" name={name} setName={setName} content={content} setContent={setContent} onSubmit={() => void createRule()} contentLabel="ルール本文" /></div>;
}

function Loading() { return <div className="flex items-center justify-center py-20 text-sm text-muted-foreground"><Loader2 className="mr-2 size-4 animate-spin" />読み込み中…</div>; }

function LibraryGrid<T extends StoryCharacterView | StoryRulebookView>({ items, empty, render }: { items: T[]; empty: string; render: (item: T) => React.ReactNode }) { return items.length ? <div className="mt-6 grid gap-4 md:grid-cols-2 lg:grid-cols-3">{items.map(render)}</div> : <div className="mt-8 rounded-lg border border-dashed border-border p-10 text-center text-sm text-muted-foreground">{empty}</div>; }

function SimpleLibraryDialog({ open, onOpenChange, title, name, setName, content, setContent, onSubmit, contentLabel }: { open: boolean; onOpenChange: (open: boolean) => void; title: string; name: string; setName: (value: string) => void; content: string; setContent: (value: string) => void; onSubmit: () => void; contentLabel: string | null }) {
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent><DialogHeader><DialogTitle>{title}</DialogTitle></DialogHeader><Input autoFocus value={name} onChange={(event) => setName(event.target.value)} placeholder="名前" aria-label="名前" onKeyDown={(event) => { if (event.key === "Enter") onSubmit(); }} />{contentLabel && <Textarea className="mt-3 min-h-32" value={content} onChange={(event) => setContent(event.target.value)} placeholder={contentLabel} aria-label={contentLabel} />}<DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>キャンセル</Button><Button onClick={onSubmit} disabled={!name.trim()}>作成</Button></DialogFooter></DialogContent></Dialog>;
}

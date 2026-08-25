"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { BookOpen, GripVertical, Loader2, Plus } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { storyApi } from "@/lib/story/api";
import { readStoryDrag, reorderStoryIds, serializeStoryDrag, STORY_EPISODE_DND_MIME } from "@/lib/story/dnd";
import { normalizeRulebooks, type StoryRulebookView } from "@/lib/story/view-model";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { StoryKnowledgeNav } from "@/components/story/story-knowledge-nav";
import { StoryAssistDialog } from "@/components/story/assist/story-assist-dialog";
import { StoryAssistField } from "@/components/story/assist/story-assist-field";
import { useStoryAssist } from "@/components/story/assist/use-story-assist";

export function StoryRulesPage({ workId }: { workId: string }) {
  const { data: allData, isLoading: allLoading, mutate: mutateAll } = useSWR("story-rulebooks", () => storyApi.listRulebooks());
  const { data: workData, isLoading: workLoading, mutate: mutateWork } = useSWR(`story-work-rulebooks:${workId}`, () => storyApi.getWorkRulebooks(workId));
  const all = useMemo(() => (allData ? normalizeRulebooks(allData) : []), [allData]);
  const applied = useMemo(() => (workData ? normalizeRulebooks(workData) : []), [workData]);
  const appliedById = useMemo(() => new Map(applied.map((rule) => [rule.id, rule])), [applied]);
  const rulebooks = useMemo(() => all.map((rule) => ({ ...rule, ...(appliedById.get(rule.id) || {}), applied: appliedById.has(rule.id), enabled: appliedById.get(rule.id)?.enabled ?? false })), [all, appliedById]);
  const ruleIdsKey = rulebooks.map((rule) => rule.id).join("|");
  const [ruleOrder, setRuleOrder] = useState<string[]>([]);
  const [draggingRuleId, setDraggingRuleId] = useState<string | null>(null);
  useEffect(() => { setRuleOrder(ruleIdsKey ? ruleIdsKey.split("|") : []); }, [ruleIdsKey]);
  const orderedRulebooks = useMemo(() => {
    const byId = new Map(rulebooks.map((rule) => [rule.id, rule]));
    return [...ruleOrder.map((id) => byId.get(id)).filter((rule): rule is (typeof rulebooks)[number] => Boolean(rule)), ...rulebooks.filter((rule) => !ruleOrder.includes(rule.id))];
  }, [ruleOrder, rulebooks]);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<StoryRulebookView | null>(null);
  const [name, setName] = useState("");
  const [content, setContent] = useState("");
  const [saving, setSaving] = useState(false);
  const assist = useStoryAssist();
  const openNew = () => { setEditing(null); setName(""); setContent(""); setOpen(true); };
  const openEdit = (rule: StoryRulebookView) => { setEditing(rule); setName(rule.name); setContent(rule.content); setOpen(true); };
  const saveRule = async () => { if (!name.trim()) return; setSaving(true); try { if (editing) await storyApi.updateRulebook(editing.id, { name: name.trim(), content }); else await storyApi.createRulebook({ name: name.trim(), content }); await mutateAll(); setOpen(false); toast.success(editing ? "ルールブックを更新しました" : "ルールブックを作成しました"); } catch (error) { toast.error(error instanceof Error ? error.message : "ルールブックを保存できませんでした"); } finally { setSaving(false); } };
  const toggle = async (rule: StoryRulebookView) => { const next = orderedRulebooks.filter((item) => item.applied !== (item.id === rule.id)).map((item, position) => ({ rulebook_id: item.id, enabled: item.id === rule.id ? !rule.enabled : item.enabled, position })); try { await storyApi.updateWorkRulebooks(workId, next); await mutateWork(); toast.success(rule.enabled ? "適用を解除しました" : "適用しました"); } catch (error) { toast.error(error instanceof Error ? error.message : "適用状態を更新できませんでした"); } };
  const handleRuleDrop = async (event: React.DragEvent<HTMLDivElement>, targetId: string) => {
    event.preventDefault();
    const movingId = readStoryDrag(event.dataTransfer);
    if (!movingId) return;
    const previous = orderedRulebooks.map((rule) => rule.id);
    const rect = event.currentTarget.getBoundingClientRect();
    const next = reorderStoryIds(previous, movingId, targetId, event.clientY - rect.top < rect.height / 2 ? "before" : "after");
    if (!next) return;
    setRuleOrder(next);
    try {
      const byId = new Map(orderedRulebooks.map((rule) => [rule.id, rule]));
      await storyApi.updateWorkRulebooks(workId, next.map((id) => byId.get(id)).filter((rule): rule is (typeof orderedRulebooks)[number] => rule != null && rule.applied).map((rule, position) => ({ rulebook_id: rule.id, enabled: rule.enabled, position })));
      await mutateWork();
    } catch (error) {
      setRuleOrder(previous);
      toast.error(error instanceof Error ? error.message : "ルールブックの並べ替えに失敗しました");
    }
  };
  const [selectedRule, setSelectedRule] = useState<StoryRulebookView | null>(null);
  const contextRail = useMemo(() => (
    <aside className="flex min-h-0 flex-1 flex-col bg-surface-charcoal text-on-surface" data-testid="story-rules-context-rail">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-border-subtle bg-surface-container px-4"><div className="flex items-center gap-2"><span className="size-2 rounded-full bg-primary" /><span className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">Rulebook context</span></div><BookOpen className="size-4 text-muted-foreground" /></div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">{selectedRule ? <><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Inspecting</p><h3 className="mt-1 text-base font-semibold">{selectedRule.name}</h3><dl className="mt-4 space-y-2 rounded-sm border border-border-subtle bg-surface-container-lowest p-3 font-mono text-[11px] text-on-surface-variant"><div className="flex justify-between gap-2"><dt>ID:</dt><dd className="text-primary">{selectedRule.id || "—"}</dd></div><div className="flex justify-between gap-2"><dt>Applied:</dt><dd>{selectedRule.applied ? "Yes" : "No"}</dd></div><div className="flex justify-between gap-2"><dt>Entries:</dt><dd>{selectedRule.content.split("\n").filter(Boolean).length}</dd></div></dl><p className="mt-4 text-xs leading-5 text-on-surface">{selectedRule.content || "本文なし"}</p></> : <p className="text-xs text-muted-foreground">ルールブックを選択すると、適用状態と本文を表示します。</p>}</div>
    </aside>
  ), [selectedRule]);
  useWorkspaceShellRegistration({ id: `story-rules-${workId}`, contextRail, priority: 60 });

  return <div className="flex min-h-full min-w-0 flex-col bg-background text-on-surface" data-testid="story-rules-page"><StoryKnowledgeNav workId={workId} active="rules" actions={<Button size="sm" className="h-7 rounded-sm bg-primary-container text-xs text-on-primary-container hover:bg-primary" onClick={openNew}><Plus className="size-3.5" />ルールブックを追加</Button>} /><div className="min-h-0 flex-1 overflow-y-auto p-6"><div className="mb-5"><p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-on-surface-variant">共有ルール</p><h2 className="mt-1 flex items-center gap-2 text-xl font-semibold"><BookOpen className="size-5 text-primary" />ルールブック</h2><p className="mt-1 max-w-2xl text-[13px] leading-[18px] text-muted-foreground">適用中のルールだけが本文生成のプロンプトへ、position順で注入されます。</p></div>{allLoading || workLoading ? <div className="flex items-center justify-center py-16 text-sm text-muted-foreground"><Loader2 className="mr-2 size-4 animate-spin" />ルールブックを読み込み中…</div> : <div className="grid gap-4 xl:grid-cols-2">{orderedRulebooks.map((rule) => <Card key={rule.id} draggable onClick={() => setSelectedRule(rule)} onDragStart={(event) => { setDraggingRuleId(rule.id); const payload = serializeStoryDrag(rule.id); event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData(STORY_EPISODE_DND_MIME, payload); event.dataTransfer.setData("text/plain", payload); }} onDragEnd={() => setDraggingRuleId(null)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => void handleRuleDrop(event, rule.id)} className={`cursor-pointer rounded-md border bg-surface-charcoal p-4 transition-colors ${rule.enabled ? "border-primary" : "border-border-subtle hover:border-outline"} ${draggingRuleId === rule.id ? "opacity-60" : ""}`}><CardHeader className="p-0 pb-3"><div className="flex items-start gap-2"><GripVertical className="mt-1 size-4 shrink-0 cursor-grab text-muted-foreground" /><div className="min-w-0 flex-1"><CardTitle className="text-base">{rule.name}</CardTitle><p className="mt-1 text-xs text-muted-foreground">{rule.content.split("\n").filter(Boolean).length}項目 · {rule.enabled ? "適用中" : "未適用"}</p></div><Button variant={rule.enabled ? "secondary" : "outline"} size="sm" className="h-7 rounded-sm text-xs" onClick={(event) => { event.stopPropagation(); void toggle(rule); }}>{rule.enabled ? "適用を解除" : "この作品に適用"}</Button></div></CardHeader><CardContent className="p-0"><div className="border-t border-border-subtle pt-3"><p className={`line-clamp-4 whitespace-pre-wrap text-[13px] leading-[18px] ${rule.enabled ? "text-on-surface" : "text-muted-foreground"}`}>{rule.content || "本文なし"}</p></div><div className="mt-4 flex justify-end"><Button variant="ghost" size="sm" className="h-7 rounded-sm text-xs text-on-surface-variant" onClick={(event) => { event.stopPropagation(); openEdit(rule); }}>編集</Button></div></CardContent></Card>)}</div>}{!rulebooks.length && <div className="rounded-md border border-dashed border-border-subtle p-8 text-center text-sm text-muted-foreground">共有ルールブックはまだありません。</div>}</div>
    <Dialog open={open} onOpenChange={setOpen}><DialogContent><DialogHeader><DialogTitle>{editing ? "ルールブックを編集" : "ルールブックを作成"}</DialogTitle></DialogHeader><div className="space-y-3"><div><Label>名前</Label><Input className="mt-1" value={name} onChange={(event) => setName(event.target.value)} /></div><div><Label>ルール本文</Label><StoryAssistField assist={assist} target={{ fieldKind: "rulebook", fieldLabel: "ルール本文", workId, rulebookId: editing?.id, getCurrentText: () => content }}><Textarea className="mt-1 min-h-48" value={content} onChange={(event) => setContent(event.target.value)} placeholder="例: 会話文は過度に説明的にしない" /></StoryAssistField></div></div><DialogFooter><Button variant="outline" onClick={() => setOpen(false)}>キャンセル</Button><Button onClick={() => void saveRule()} disabled={saving || !name.trim()}>{saving && <Loader2 className="size-3.5 animate-spin" />}保存</Button></DialogFooter></DialogContent></Dialog>
    <StoryAssistDialog assist={assist} onApplied={async (nextText) => { setContent(nextText); toast.success("ルール本文のAI修正案を適用しました（保存ボタンで確定してください）"); }} /></div>;
}

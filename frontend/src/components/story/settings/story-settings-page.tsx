"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";
import { GripVertical, ImageIcon, Loader2, Plus, Sparkles, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { Textarea } from "@/components/ui/textarea";
import { useConfirm } from "@/hooks/use-confirm";
import { storyApi } from "@/lib/story/api";
import { readStoryDrag, reorderStoryIds, serializeStoryDrag, STORY_EPISODE_DND_MIME } from "@/lib/story/dnd";
import { normalizeJob, normalizeNotes, type StoryNoteView } from "@/lib/story/view-model";
import { useStoryWorkContext } from "@/components/story/shell/story-workspace-shell";
import { StoryModelSelect } from "@/components/story/generate/story-model-select";
import { StoryComposePreview } from "@/components/story/generate/story-compose-preview";
import { StoryKnowledgeNav } from "@/components/story/story-knowledge-nav";
import { StoryAssistDialog } from "@/components/story/assist/story-assist-dialog";
import { StoryAssistField } from "@/components/story/assist/story-assist-field";
import { toAssistSelection } from "@/components/story/assist/assist-selection";
import { useStoryAssist } from "@/components/story/assist/use-story-assist";
import { TrpgPlayOpenButton } from "@/components/trpg/play/trpg-play-open-button";

/**
 * §4.5 の AI 参照バッジ。色トークンは変えず、線種（実線 / 破線 / 点線）で参照モードを
 * 区別する。「参照しない」だけはグレー（`--border` + `--muted-foreground`）にする。
 */
const AI_MODES = [
  ["always", "常時", "border-solid border-primary text-primary"],
  ["keyword", "キーワード一致", "border-dashed border-primary text-primary"],
  ["explicit", "明示時のみ", "border-dotted border-primary text-primary"],
  ["off", "参照しない", "border-solid border-border text-muted-foreground"],
] as const;

/** §8.8「使用モデルの可視化」の解決層ラベル。 */
const MODEL_LAYER_LABELS: Record<string, string> = {
  runtime: "実行時",
  work: "作品設定",
  writing_class: "執筆クラス",
  main_llm_inherited: "メインLLM継承",
};

function aiModeBadge(mode: string) {
  const found = AI_MODES.find(([value]) => value === mode);
  return { label: found?.[1] || mode, className: found?.[2] || "border-solid border-border text-muted-foreground" };
}

export function StorySettingsPage({ workId }: { workId: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const activeTab = searchParams.get("tab") === "notes" ? "notes" : "work";
  const { work, saveWork, markDirty, registerSaveScope, flushAllScopes } = useStoryWorkContext();
  const confirm = useConfirm();
  const assist = useStoryAssist();
  const { data: notesData, mutate: mutateNotes } = useSWR(`story-notes:${workId}`, () => storyApi.listNotes(workId));
  const notes = useMemo(() => (notesData ? normalizeNotes(notesData) : []), [notesData]);
  const noteIdsKey = notes.map((note) => note.id).join("|");
  const [noteOrder, setNoteOrder] = useState<string[]>([]);
  const [draggingNoteId, setDraggingNoteId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [synopsis, setSynopsis] = useState("");
  const [plot, setPlot] = useState("");
  const [plotSelection, setPlotSelection] = useState<{ from: number; to: number; text: string } | null>(null);
  const [styleGuide, setStyleGuide] = useState("");
  const [plannedCount, setPlannedCount] = useState("");
  const [targetChars, setTargetChars] = useState("6000");
  const [model, setModel] = useState("");
  const [imageEnabled, setImageEnabled] = useState(false);
  const [imageMax, setImageMax] = useState("3");
  const [imageStyle, setImageStyle] = useState("");
  const [imageNegative, setImageNegative] = useState("");
  const [saving, setSaving] = useState(false);
  const [composeOpen, setComposeOpen] = useState(false);
  const [composeResult, setComposeResult] = useState<unknown>(null);
  const [composeJobId, setComposeJobId] = useState<string | null>(null);
  const [composeBusy, setComposeBusy] = useState(false);
  const [noteOpen, setNoteOpen] = useState(false);
  const [editingNote, setEditingNote] = useState<StoryNoteView | null>(null);
  const [noteTitle, setNoteTitle] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const [noteMode, setNoteMode] = useState("keyword");
  const [noteKeywords, setNoteKeywords] = useState("");
  const loadedSettingsRef = useRef({
    title: "",
    synopsis: "",
    plot: "",
    styleGuide: "",
    plannedCount: "",
    targetChars: "6000",
    model: "",
    imageEnabled: false,
    imageMax: "3",
    imageStyle: "",
    imageNegative: "",
  });
  const syncedWorkIdRef = useRef<string | null>(null);

  // §4.11 の章構成ジョブは SWR のポーリングで追う（手書きの待機ループを持たない）。
  const { data: composeJobData } = useSWR(
    composeJobId ? `story-compose-job:${composeJobId}` : null,
    () => storyApi.getJob(composeJobId as string),
    {
      refreshInterval: 1500,
      revalidateOnFocus: false,
      onSuccess: (payload) => {
        const job = normalizeJob(payload);
        if (job.status === "done") {
          setComposeJobId(null);
          setComposeResult(job.result ?? {});
          setComposeOpen(true);
          return;
        }
        if (job.status === "error" || job.status === "canceled") {
          setComposeJobId(null);
          toast.error(job.error || `章構成ジョブが${job.status}になりました`);
        }
      },
      onError: (jobError: unknown) => {
        setComposeJobId(null);
        toast.error(jobError instanceof Error ? jobError.message : "章構成ジョブの状態を取得できませんでした");
      },
    },
  );
  const composeJob = composeJobId && composeJobData ? normalizeJob(composeJobData) : null;

  useEffect(() => {
    setNoteOrder(noteIdsKey ? noteIdsKey.split("|") : []);
  }, [noteIdsKey]);

  const orderedNotes = useMemo(() => {
    const byId = new Map(notes.map((note) => [note.id, note]));
    return [...noteOrder.map((id) => byId.get(id)).filter((note): note is StoryNoteView => Boolean(note)), ...notes.filter((note) => !noteOrder.includes(note.id))];
  }, [noteOrder, notes]);

  const isSettingsDirty = useCallback(() => {
    const loaded = loadedSettingsRef.current;
    return (
      title !== loaded.title
      || synopsis !== loaded.synopsis
      || plot !== loaded.plot
      || styleGuide !== loaded.styleGuide
      || plannedCount !== loaded.plannedCount
      || targetChars !== loaded.targetChars
      || model !== loaded.model
      || imageEnabled !== loaded.imageEnabled
      || imageMax !== loaded.imageMax
      || imageStyle !== loaded.imageStyle
      || imageNegative !== loaded.imageNegative
    );
  }, [imageEnabled, imageMax, imageNegative, imageStyle, model, plannedCount, plot, styleGuide, synopsis, targetChars, title]);

  useEffect(() => {
    const workIdChanged = syncedWorkIdRef.current !== work.id;
    if (!workIdChanged && isSettingsDirty()) return;
    syncedWorkIdRef.current = work.id;

    const nextTitle = work.title === "作品を読み込み中" ? "" : work.title;
    const overrideProvider = work.modelOverride.provider;
    const overrideModel = work.modelOverride.model;
    const nextModel =
      typeof overrideProvider === "string" && typeof overrideModel === "string"
        ? `${overrideProvider}::${overrideModel}`
        : "";
    const nextPlannedCount = work.plannedEpisodeCount == null ? "" : String(work.plannedEpisodeCount);
    const nextTargetChars = String(work.targetEpisodeChars || 6000);
    setTitle(nextTitle);
    setSynopsis(work.synopsis);
    setPlot(work.plot);
    setStyleGuide(work.styleGuide);
    setPlannedCount(nextPlannedCount);
    setTargetChars(nextTargetChars);
    setModel(nextModel);
    setImageEnabled(work.imageSettings.enabled);
    setImageMax(String(work.imageSettings.maxImagesPerEpisode || 3));
    setImageStyle(work.imageSettings.style);
    setImageNegative(work.imageSettings.negativePrompt);
    loadedSettingsRef.current = {
      title: nextTitle,
      synopsis: work.synopsis,
      plot: work.plot,
      styleGuide: work.styleGuide,
      plannedCount: nextPlannedCount,
      targetChars: nextTargetChars,
      model: nextModel,
      imageEnabled: work.imageSettings.enabled,
      imageMax: String(work.imageSettings.maxImagesPerEpisode || 3),
      imageStyle: work.imageSettings.style,
      imageNegative: work.imageSettings.negativePrompt,
    };
  }, [isSettingsDirty, work.id, work.title, work.synopsis, work.plot, work.styleGuide, work.plannedEpisodeCount, work.targetEpisodeChars, work.modelOverride, work.imageSettings]);

  const flushSettings = useCallback(async (): Promise<boolean> => {
    if (!isSettingsDirty()) return true;
    if (!title.trim()) return false;
    setSaving(true);
    try {
      const [provider, selectedModel] = model.split("::", 2);
      const model_override = provider && selectedModel ? { provider, model: selectedModel } : {};
      await saveWork({
        title: title.trim(),
        synopsis,
        plot,
        style_guide: styleGuide,
        planned_episode_count: plannedCount ? Number(plannedCount) : null,
        target_episode_chars: Number(targetChars) || 6000,
        model_override,
        image_settings: {
          enabled: imageEnabled,
          engine: "comfyui",
          max_images_per_episode: Number(imageMax) || 3,
          style: imageStyle,
          negative_prompt: imageNegative,
        },
      });
      loadedSettingsRef.current = {
        title: title.trim(),
        synopsis,
        plot,
        styleGuide,
        plannedCount,
        targetChars,
        model,
        imageEnabled,
        imageMax,
        imageStyle,
        imageNegative,
      };
      return true;
    } catch {
      return false;
    } finally {
      setSaving(false);
    }
  }, [imageEnabled, imageMax, imageNegative, imageStyle, isSettingsDirty, model, plannedCount, plot, saveWork, styleGuide, synopsis, targetChars, title]);

  useEffect(() => {
    return registerSaveScope({
      id: "settings",
      isDirty: isSettingsDirty,
      isSaving: () => saving,
      flush: flushSettings,
    });
  }, [flushSettings, isSettingsDirty, registerSaveScope, saving]);

  const save = async () => {
    if (!title.trim()) return;
    const ok = await flushSettings();
    if (ok) toast.success("作品設定を保存しました");
  };

  const navigateSettingsTab = useCallback(
    async (href: string) => {
      const ok = await flushAllScopes();
      if (!ok) {
        toast.error("未保存の変更を保存できませんでした。内容を確認してから再度お試しください。");
        return;
      }
      router.push(href);
    },
    [flushAllScopes, router],
  );

  const openNewNote = () => {
    setEditingNote(null);
    setNoteTitle("");
    setNoteContent("");
    setNoteMode("keyword");
    setNoteKeywords("");
    setNoteOpen(true);
  };

  const openEditNote = (note: StoryNoteView) => {
    setEditingNote(note);
    setNoteTitle(note.title);
    setNoteContent(note.content);
    setNoteMode(note.aiMode);
    setNoteKeywords(note.keywords.join(", "));
    setNoteOpen(true);
  };

  const saveNote = async () => {
    if (!noteTitle.trim()) return;
    const payload = { title: noteTitle.trim(), content: noteContent, ai_mode: noteMode, keywords: noteKeywords.split(",").map((item) => item.trim()).filter(Boolean), position: editingNote?.position ?? notes.length };
    try {
      if (editingNote) await storyApi.updateNote(editingNote.id, payload);
      else await storyApi.createNote(workId, payload);
      await mutateNotes();
      setNoteOpen(false);
      toast.success(editingNote ? "資料を更新しました" : "資料を追加しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "資料を保存できませんでした");
    }
  };

  const deleteNote = async (note: StoryNoteView) => {
    const approved = await confirm({ title: "資料を削除", description: `「${note.title}」を削除しますか？`, confirmLabel: "削除", destructive: true });
    if (!approved) return;
    try {
      await storyApi.deleteNote(note.id);
      await mutateNotes();
      toast.success("資料を削除しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "資料を削除できませんでした");
    }
  };

  const handleNoteDrop = async (event: React.DragEvent<HTMLDivElement>, targetId: string) => {
    event.preventDefault();
    const movingId = readStoryDrag(event.dataTransfer);
    if (!movingId) return;
    const previous = orderedNotes.map((note) => note.id);
    const rect = event.currentTarget.getBoundingClientRect();
    const next = reorderStoryIds(previous, movingId, targetId, event.clientY - rect.top < rect.height / 2 ? "before" : "after");
    if (!next) return;
    setNoteOrder(next);
    try {
      await Promise.all(next.map((id, position) => storyApi.updateNote(id, { position })));
      await mutateNotes();
    } catch (error) {
      setNoteOrder(previous);
      toast.error(error instanceof Error ? error.message : "資料の並べ替えに失敗しました");
    }
  };

  const compose = async () => {
    setComposeBusy(true);
    try {
      const queued = normalizeJob(await storyApi.compose(workId, { mode: "continue", episode_count: plannedCount ? Number(plannedCount) : undefined, instruction: "作品設定に基づき章構成を提案してください。" }));
      if (!queued.id) throw new Error("章構成ジョブIDが返りませんでした");
      setComposeJobId(queued.id);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "章構成の提案を取得できませんでした");
    } finally {
      setComposeBusy(false);
    }
  };

  const resolvedLayer = MODEL_LAYER_LABELS[work.resolvedModelLayer || ""] || work.resolvedModelLayer || "執筆クラス";

  return <div className="flex min-h-full min-w-0 flex-col bg-background text-on-surface" data-testid="story-settings-page">
    <StoryKnowledgeNav workId={workId} active={activeTab === "notes" ? "notes" : null} />
    <div className="min-h-0 flex-1 overflow-y-auto p-6">
    <div className="mb-6 flex flex-wrap items-center gap-2 border-b border-border-subtle pb-4" role="tablist" aria-label="作品設定セクション">
      <button
        type="button"
        role="tab"
        aria-selected={activeTab === "work"}
        className={`rounded-sm px-3 py-1.5 text-sm transition-colors ${activeTab === "work" ? "bg-surface-container-highest font-medium text-primary" : "text-muted-foreground hover:text-on-surface"}`}
        onClick={() => {
          if (activeTab !== "work") void navigateSettingsTab(`/scenarios/${encodeURIComponent(workId)}/settings`);
        }}
      >
        企画設定
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={activeTab === "notes"}
        className={`rounded-sm px-3 py-1.5 text-sm transition-colors ${activeTab === "notes" ? "bg-surface-container-highest font-medium text-primary" : "text-muted-foreground hover:text-on-surface"}`}
        onClick={() => {
          if (activeTab !== "notes") void navigateSettingsTab(`/scenarios/${encodeURIComponent(workId)}/settings?tab=notes`);
        }}
      >
        設定資料
      </button>
    </div>
    {activeTab === "work" ? <>
    <div className="flex flex-wrap items-start gap-3"><div className="min-w-0 flex-1"><div className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">作品設定</div><h2 className="mt-1 text-xl font-semibold text-foreground">企画設定</h2><p className="mt-1 text-sm text-muted-foreground">企画・プロット・文体を設定し、AI文脈の基礎を整えます。</p></div><div className="flex flex-wrap items-center gap-2">{work.kind === "trpg" ? <TrpgPlayOpenButton workId={workId} workTitle={title} /> : null}<Button onClick={() => void save()} disabled={saving || !title.trim()}>{saving && <Loader2 className="size-3.5 animate-spin" />}保存</Button></div></div>
    <Card className="rounded-md border-border-subtle bg-surface-charcoal"><CardHeader><CardTitle>企画と執筆設定</CardTitle></CardHeader><CardContent className="space-y-5"><div className="grid gap-4 md:grid-cols-2"><div><Label>作品名</Label><Input className="mt-1" value={title} onChange={(event) => { setTitle(event.target.value); markDirty(); }} placeholder="作品名" /></div><div><Label>予定章数（任意）</Label><Input className="mt-1" type="number" min="1" value={plannedCount} onChange={(event) => { setPlannedCount(event.target.value); markDirty(); }} placeholder="例: 12" /></div></div><div><Label>企画・あらすじ</Label><Textarea className="mt-1 min-h-28" value={synopsis} onChange={(event) => { setSynopsis(event.target.value); markDirty(); }} placeholder="AI文脈の先頭に注入される作品の概要" /></div><div><Label>全体プロット</Label><div className="mt-1 rounded-sm border border-border-subtle bg-surface-container-lowest p-2"><StoryAssistField assist={assist} target={{ fieldKind: "work_plot", fieldLabel: "全体プロット", workId, getCurrentText: () => plot, getSelection: () => toAssistSelection(plotSelection) }}><LongTextEditor value={plot} onChange={(value) => { setPlot(value); markDirty(); }} onSelectionChange={setPlotSelection} placeholder="物語全体の流れ" minHeight={150} maxHeight={360} /></StoryAssistField></div></div><div><Label>文体・執筆指示</Label><Textarea className="mt-1 min-h-28" value={styleGuide} onChange={(event) => { setStyleGuide(event.target.value); markDirty(); }} placeholder="文体、視点、避けたい表現など" /></div><div className="grid gap-4 md:grid-cols-2"><div><Label>1章の目標文字数</Label><Input className="mt-1" type="number" min="0" value={targetChars} onChange={(event) => { setTargetChars(event.target.value); markDirty(); }} /></div><div><Label>執筆モデル</Label><StoryModelSelect value={model} onChange={(value) => { setModel(value); markDirty(); }} /><p className="mt-1 text-xs text-muted-foreground">現在の解決結果: {work.resolvedModel || "メインLLMを継承"}（{resolvedLayer}）</p></div></div><div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4"><div><h3 className="text-sm font-semibold">章構成をAIに提案させる</h3><p className="mt-1 text-xs text-muted-foreground">作品設定をもとに章案と接続案を作成します。適用前に編集できます。</p></div><Button onClick={() => void compose()} disabled={composeBusy || Boolean(composeJobId)}>{composeBusy || composeJobId ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}AIで章構成を提案</Button></div></CardContent></Card>
    <Card className="mt-4 rounded-md border-border-subtle bg-surface-charcoal" data-testid="story-image-settings-card"><CardHeader><CardTitle className="flex items-center gap-2"><ImageIcon className="size-4" />挿絵設定</CardTitle></CardHeader><CardContent className="space-y-4"><label className="flex items-start gap-2 text-sm"><Checkbox checked={imageEnabled} onCheckedChange={(checked) => { setImageEnabled(checked === true); markDirty(); }} /><span><span className="font-medium">章ごとの自動挿絵を有効化</span><span className="mt-1 block text-xs text-muted-foreground">本文生成後に ComfyUI で挿絵候補を抽出します。既定は OFF です。</span></span></label><div className="grid gap-4 md:grid-cols-2"><div><Label>1章あたりの最大枚数</Label><Input className="mt-1" type="number" min="0" max="10" value={imageMax} disabled={!imageEnabled} onChange={(event) => { setImageMax(event.target.value); markDirty(); }} /></div><div><Label>画像エンジン</Label><Input className="mt-1" value="ComfyUI" disabled readOnly /></div></div><div><Label>作風プロンプト</Label><Textarea className="mt-1 min-h-20" value={imageStyle} disabled={!imageEnabled} onChange={(event) => { setImageStyle(event.target.value); markDirty(); }} placeholder="例: watercolor, soft lighting" /></div><div><Label>ネガティブプロンプト</Label><Textarea className="mt-1 min-h-20" value={imageNegative} disabled={!imageEnabled} onChange={(event) => { setImageNegative(event.target.value); markDirty(); }} placeholder="避けたい要素" /></div></CardContent></Card>
    </> : <>
    <div className="mb-4"><h2 className="text-xl font-semibold text-foreground">設定資料</h2><p className="mt-1 text-sm text-muted-foreground">作品固有の設定資料。適用された資料だけがAI文脈に注入されます。</p></div>
    <Card className="rounded-md border-border-subtle bg-surface-charcoal"><CardHeader className="flex-row items-center justify-between"><div><CardTitle>設定・資料</CardTitle><p className="mt-1 text-xs text-muted-foreground">バッジの線種が参照モードを表します（実線=常時 / 破線=キーワード / 点線=明示時のみ / グレー=参照しない）。</p></div><Button variant="outline" size="sm" onClick={openNewNote}><Plus className="size-3.5" />資料を追加</Button></CardHeader><CardContent className="space-y-3">{orderedNotes.length ? orderedNotes.map((note) => { const badge = aiModeBadge(note.aiMode); return <div key={note.id} draggable onDragStart={(event) => { setDraggingNoteId(note.id); const payload = serializeStoryDrag(note.id); event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData(STORY_EPISODE_DND_MIME, payload); event.dataTransfer.setData("text/plain", payload); }} onDragEnd={() => setDraggingNoteId(null)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => void handleNoteDrop(event, note.id)} className={`group rounded-lg border bg-card p-3 ${draggingNoteId === note.id ? "opacity-60" : "border-border"}`}><div className="flex items-start gap-2"><GripVertical className="mt-1 size-4 shrink-0 cursor-grab text-muted-foreground/60" /><button type="button" className="min-w-0 flex-1 text-left" onClick={() => openEditNote(note)}><div className="flex flex-wrap items-center gap-2"><span className="font-medium">{note.title}</span><span className={`rounded-full border px-2 py-0.5 text-[10px] ${badge.className}`} title={`AI参照モード: ${badge.label}`}>{badge.label}</span></div><p className="mt-1 line-clamp-2 text-sm text-muted-foreground">{note.content || "本文なし"}</p>{note.keywords.length > 0 && <p className="mt-2 text-[11px] text-muted-foreground">参照キーワード: {note.keywords.join(", ")}</p>}</button><Button variant="ghost" size="icon-sm" aria-label={`${note.title}を削除`} onClick={() => void deleteNote(note)}><Trash2 className="size-3.5 text-destructive" /></Button></div></div>; }) : <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">設定・資料はまだありません。</div>}</CardContent></Card>
    </>}
    <Dialog open={noteOpen} onOpenChange={setNoteOpen}><DialogContent size="2xl"><DialogHeader><DialogTitle>{editingNote ? "資料を編集" : "資料を追加"}</DialogTitle></DialogHeader><div className="space-y-3"><div><Label>タイトル</Label><Input className="mt-1" value={noteTitle} onChange={(event) => setNoteTitle(event.target.value)} /></div><div><Label>本文</Label><StoryAssistField assist={assist} target={{ fieldKind: "world_note", fieldLabel: "設定資料本文", workId, noteId: editingNote?.id, getCurrentText: () => noteContent }}><Textarea className="mt-1 min-h-36" value={noteContent} onChange={(event) => setNoteContent(event.target.value)} placeholder="AIに渡す設定資料" /></StoryAssistField></div><div className="grid gap-3 md:grid-cols-2"><div><Label>AI参照モード</Label><AppSelect className="mt-1 w-full" aria-label="AI参照モード" value={noteMode} onChange={(event) => setNoteMode(event.target.value)}>{AI_MODES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</AppSelect></div><div><Label>参照キーワード</Label><Input className="mt-1" value={noteKeywords} onChange={(event) => setNoteKeywords(event.target.value)} placeholder="名前, 場所" /></div></div></div><DialogFooter><Button variant="outline" onClick={() => setNoteOpen(false)}>キャンセル</Button><Button onClick={() => void saveNote()} disabled={!noteTitle.trim()}>保存</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={composeOpen} onOpenChange={setComposeOpen}><DialogContent size="3xl" className="max-h-[80vh] overflow-y-auto"><DialogHeader><DialogTitle>章構成提案プレビュー</DialogTitle></DialogHeader><p className="text-sm text-muted-foreground">提案ジョブが完了しました。タイトルとプロットを編集してから適用できます。</p><StoryComposePreview value={composeResult} onApply={async (proposal) => { try { await storyApi.applyCompose(workId, proposal); await mutateNotes(); setComposeOpen(false); toast.success("章構成を適用しました"); } catch (error) { toast.error(error instanceof Error ? error.message : "章構成を適用できませんでした"); } }} /><DialogFooter><Button variant="outline" onClick={() => setComposeOpen(false)}>閉じる</Button></DialogFooter></DialogContent></Dialog>
    {composeJob && composeJob.status !== "done" && <div className="fixed bottom-4 right-4 z-20 rounded-lg border border-border bg-card p-3 text-xs shadow-lg"><div className="flex items-center gap-2"><Loader2 className="size-3.5 animate-spin" />章構成を生成中: {composeJob.status}</div><div className="mt-1 text-muted-foreground">完了すると提案プレビューが開きます。</div></div>}
    <StoryAssistDialog
      assist={assist}
      onApplied={async (nextText) => {
        if (assist.target?.fieldKind === "work_plot") {
          setPlot(nextText);
          markDirty();
          toast.success("全体プロットのAI修正案を適用しました");
          return;
        }
        if (assist.target?.fieldKind === "world_note") {
          setNoteContent(nextText);
          toast.success("設定資料本文のAI修正案を適用しました");
        }
      }}
    />
    </div>
  </div>;
}

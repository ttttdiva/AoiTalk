"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import Image from "next/image";
import { BookOpen, GripVertical, Loader2, Plus, Search, User, Users } from "lucide-react";
import { toast } from "sonner";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { storyApi } from "@/lib/story/api";
import { readStoryDrag, reorderStoryIds, serializeStoryDrag, STORY_EPISODE_DND_MIME } from "@/lib/story/dnd";
import { normalizeCharacters, objectOf, type StoryCharacterView } from "@/lib/story/view-model";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { StoryKnowledgeNav } from "@/components/story/story-knowledge-nav";
import { StoryAssistDialog } from "@/components/story/assist/story-assist-dialog";
import { StoryAssistField } from "@/components/story/assist/story-assist-field";
import { useStoryAssist } from "@/components/story/assist/use-story-assist";

const modes = [
  ["always", "常時", "border-solid border-primary text-primary"],
  ["keyword", "キーワード一致", "border-dashed border-primary text-primary"],
  ["explicit", "明示時のみ", "border-dotted border-primary text-primary"],
  ["off", "参照しない", "border-solid border-border text-muted-foreground"],
] as const;
const TEMPLATE = "## 口調\n\n## 心理\n\n## 背景\n\n## 関係性\n\n## アーク\n\n## 台詞例\n";

function aiModeBadge(mode: string) {
  const found = modes.find(([value]) => value === mode);
  return { label: found?.[1] || mode, className: found?.[2] || "border-solid border-border text-muted-foreground" };
}

function CharacterThumbnail({ character, size = "card" }: { character: StoryCharacterView; size?: "card" | "dialog" }) {
  const dimension = size === "dialog" ? "size-20" : "size-16";
  const textSize = size === "dialog" ? "text-2xl" : "text-xl";
  if (character.imageUrl) {
    return (
      <div className={`relative ${dimension} shrink-0 overflow-hidden rounded-sm border border-border-subtle bg-surface-container-high`}>
        <Image src={character.imageUrl} alt="" fill className="object-cover" sizes={size === "dialog" ? "80px" : "64px"} unoptimized />
      </div>
    );
  }
  return (
    <div className={`flex ${dimension} shrink-0 items-center justify-center rounded-sm border border-border-subtle bg-surface-container-high ${textSize} font-semibold text-primary`}>
      {Array.from(character.name)[0] || <User className="size-6" aria-hidden />}
    </div>
  );
}

export function StoryCastPage({ workId }: { workId: string }) {
  const { data: allData, isLoading: allLoading, mutate: mutateAll } = useSWR("story-characters", () => storyApi.listCharacters());
  const { data: workData, isLoading: workLoading, mutate: mutateWork } = useSWR(`story-work-characters:${workId}`, () => storyApi.getWorkCharacters(workId));
  const all = useMemo(() => (allData ? normalizeCharacters(allData) : []), [allData]);
  const included = useMemo(() => (workData ? normalizeCharacters(workData) : []), [workData]);
  const includedById = useMemo(() => new Map(included.map((character) => [character.id, character])), [included]);
  const characters = useMemo(
    () => all.map((character) => ({ ...character, ...(includedById.get(character.id) || {}), included: includedById.has(character.id) })),
    [all, includedById],
  );
  const characterIdsKey = characters.map((character) => character.id).join("|");
  const [characterOrder, setCharacterOrder] = useState<string[]>([]);
  const [draggingCharacterId, setDraggingCharacterId] = useState<string | null>(null);
  useEffect(() => { setCharacterOrder(characterIdsKey ? characterIdsKey.split("|") : []); }, [characterIdsKey]);
  const orderedCharacters = useMemo(() => {
    const byId = new Map(characters.map((character) => [character.id, character]));
    return [
      ...characterOrder.map((id) => byId.get(id)).filter((character): character is (typeof characters)[number] => Boolean(character)),
      ...characters.filter((character) => !characterOrder.includes(character.id)),
    ];
  }, [characterOrder, characters]);

  const [showPool, setShowPool] = useState(false);
  const [poolSearch, setPoolSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<StoryCharacterView | null>(null);
  const [name, setName] = useState("");
  const [aliases, setAliases] = useState("");
  const [summary, setSummary] = useState("");
  const [description, setDescription] = useState("");
  const [notes, setNotes] = useState("");
  const [aiMode, setAiMode] = useState("keyword");
  const [keywords, setKeywords] = useState("");
  const [roleNote, setRoleNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [imageUploading, setImageUploading] = useState(false);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const assist = useStoryAssist();

  const includedCharacters = useMemo(
    () => orderedCharacters.filter((character) => character.included),
    [orderedCharacters],
  );
  const poolQuery = poolSearch.trim().toLocaleLowerCase();
  const poolCharacters = useMemo(() => {
    const source = showPool ? orderedCharacters : includedCharacters;
    if (!poolQuery) return source;
    return source.filter((character) =>
      `${character.name} ${character.summary} ${character.aliases.join(" ")}`.toLocaleLowerCase().includes(poolQuery),
    );
  }, [includedCharacters, orderedCharacters, poolQuery, showPool]);

  const openNew = () => {
    setEditing(null);
    setSelectedId(null);
    setName("");
    setAliases("");
    setSummary("");
    setDescription(TEMPLATE);
    setNotes("");
    setAiMode("keyword");
    setKeywords("");
    setRoleNote("");
    setOpen(true);
  };

  const openEdit = (character: StoryCharacterView) => {
    setEditing(character);
    setSelectedId(character.id);
    setName(character.name);
    setAliases(character.aliases.join(", "));
    setSummary(character.summary);
    setDescription(character.description);
    setNotes(character.notes);
    setAiMode(character.aiMode);
    setKeywords(character.keywords.join(", "));
    setRoleNote(character.roleNote);
    setOpen(true);
  };

  const handleCardClick = (character: StoryCharacterView) => {
    setSelectedId(character.id);
    openEdit(character);
  };

  const saveCharacter = async () => {
    if (!name.trim()) return;
    setSaving(true);
    const payload = {
      name: name.trim(),
      aliases: aliases.split(",").map((item) => item.trim()).filter(Boolean),
      summary,
      description,
      notes,
      ai_mode: aiMode,
      keywords: keywords.split(",").map((item) => item.trim()).filter(Boolean),
    };
    try {
      if (editing) {
        await storyApi.updateCharacter(editing.id, payload);
        if (editing.included && roleNote !== editing.roleNote) {
          const next = orderedCharacters
            .filter((character) => character.included)
            .map((character, position) => ({
              character_id: character.id,
              role_note: character.id === editing.id ? roleNote : character.roleNote,
              position,
            }));
          await storyApi.updateWorkCharacters(workId, next);
        }
      } else {
        const created = await storyApi.createCharacter(payload);
        const createdId = typeof objectOf(created).id === "string" ? String(objectOf(created).id) : null;
        if (!createdId) {
          throw new Error("作成した人物のIDを取得できませんでした");
        }
        const includedChars = orderedCharacters.filter((character) => character.included);
        await storyApi.updateWorkCharacters(
          workId,
          [
            ...includedChars.map((character, position) => ({
              character_id: character.id,
              role_note: character.roleNote,
              position,
            })),
            { character_id: createdId, role_note: roleNote, position: includedChars.length },
          ],
        );
      }
      await mutateAll();
      await mutateWork();
      setOpen(false);
      toast.success(editing ? "人物を更新しました" : "人物を作成しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "人物を保存できませんでした");
    } finally {
      setSaving(false);
    }
  };

  const uploadImage = async (file: File) => {
    if (!editing?.id) return;
    setImageUploading(true);
    try {
      const form = new FormData();
      form.set("file", file);
      const response = await fetch(`/api/story/characters/${encodeURIComponent(editing.id)}/image`, {
        method: "POST",
        credentials: "include",
        body: form,
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof payload?.detail === "string" ? payload.detail : "画像をアップロードできませんでした");
      }
      await mutateAll();
      await mutateWork();
      toast.success("画像を更新しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "画像をアップロードできませんでした");
    } finally {
      setImageUploading(false);
    }
  };

  const removeImage = async () => {
    if (!editing?.id) return;
    setImageUploading(true);
    try {
      const response = await fetch(`/api/story/characters/${encodeURIComponent(editing.id)}/image`, {
        method: "DELETE",
        credentials: "include",
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof payload?.detail === "string" ? payload.detail : "画像を削除できませんでした");
      }
      await mutateAll();
      await mutateWork();
      toast.success("画像を削除しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "画像を削除できませんでした");
    } finally {
      setImageUploading(false);
    }
  };

  const toggleIncluded = async (character: StoryCharacterView) => {
    const next = orderedCharacters
      .filter((item) => item.included !== (item.id === character.id))
      .map((item, position) => ({ character_id: item.id, role_note: item.roleNote, position }));
    try {
      await storyApi.updateWorkCharacters(workId, next);
      await mutateWork();
      toast.success(character.included ? "作品から外しました" : "作品に追加しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "参加設定を更新できませんでした");
    }
  };

  const handleCharacterDrop = async (event: React.DragEvent<HTMLDivElement>, targetId: string) => {
    event.preventDefault();
    const movingId = readStoryDrag(event.dataTransfer);
    if (!movingId) return;
    const previous = orderedCharacters.map((character) => character.id);
    const rect = event.currentTarget.getBoundingClientRect();
    const next = reorderStoryIds(previous, movingId, targetId, event.clientY - rect.top < rect.height / 2 ? "before" : "after");
    if (!next) return;
    setCharacterOrder(next);
    try {
      const byId = new Map(orderedCharacters.map((character) => [character.id, character]));
      await storyApi.updateWorkCharacters(
        workId,
        next
          .map((id) => byId.get(id))
          .filter((character): character is (typeof orderedCharacters)[number] => character != null && character.included)
          .map((character, position) => ({ character_id: character.id, role_note: character.roleNote, position })),
      );
      await mutateWork();
    } catch (error) {
      setCharacterOrder(previous);
      toast.error(error instanceof Error ? error.message : "人物の並べ替えに失敗しました");
    }
  };

  const selectedCharacter = useMemo(
    () => orderedCharacters.find((character) => character.id === selectedId) || null,
    [orderedCharacters, selectedId],
  );
  const editingCharacter = useMemo(
    () => (editing ? orderedCharacters.find((character) => character.id === editing.id) || editing : null),
    [editing, orderedCharacters],
  );

  const contextRail = useMemo(() => (
    <aside className="flex min-h-0 flex-1 flex-col bg-surface-charcoal text-on-surface" data-testid="story-cast-context-rail">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-border-subtle bg-surface-container px-4">
        <div className="flex items-center gap-2">
          <span className="size-2 rounded-full bg-primary" />
          <span className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">選択中の人物</span>
        </div>
        <Users className="size-4 text-muted-foreground" />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {selectedCharacter ? (
          <>
            <div className="flex items-center gap-3 border-b border-border-subtle pb-4">
              <CharacterThumbnail character={selectedCharacter} size="dialog" />
              <div className="min-w-0">
                <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">詳細</p>
                <p className="truncate text-sm font-medium">{selectedCharacter.name}</p>
              </div>
            </div>
            <dl className="mt-4 space-y-2 rounded-sm border border-border-subtle bg-surface-container-lowest p-3 font-mono text-[11px] text-on-surface-variant">
              <div className="flex justify-between gap-2"><dt>参加:</dt><dd>{selectedCharacter.included ? "参加中" : "未参加"}</dd></div>
              <div className="flex justify-between gap-2"><dt>AI参照:</dt><dd>{aiModeBadge(selectedCharacter.aiMode).label}</dd></div>
            </dl>
            <Button
              variant="outline"
              size="sm"
              className="mt-4 h-8 w-full rounded-sm text-xs"
              onClick={() => openEdit(selectedCharacter)}
            >
              編集
            </Button>
            <section className="mt-4">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.12em] text-on-surface-variant">概要</h3>
              <div className="mt-2 space-y-3 text-xs">
                {selectedCharacter.aliases.length > 0 && (
                  <div><p className="text-muted-foreground">別名</p><p className="mt-1 text-on-surface">{selectedCharacter.aliases.join(", ")}</p></div>
                )}
                {selectedCharacter.roleNote && (
                  <div><p className="text-muted-foreground">この作品での役割</p><p className="mt-1 text-on-surface">{selectedCharacter.roleNote}</p></div>
                )}
                <div><p className="text-muted-foreground">一言サマリ</p><p className="mt-1 leading-5 text-on-surface">{selectedCharacter.summary || "—"}</p></div>
              </div>
            </section>
          </>
        ) : (
          <p className="text-xs text-muted-foreground">人物を選択すると、参照設定の概要を表示します。</p>
        )}
      </div>
    </aside>
  ), [selectedCharacter]);
  useWorkspaceShellRegistration({ id: `story-cast-${workId}`, contextRail, priority: 60 });

  return (
    <div className="flex min-h-full min-w-0 flex-col bg-background text-on-surface" data-testid="story-cast-page">
      <StoryKnowledgeNav
        workId={workId}
        active="cast"
        actions={(
          <Button size="sm" className="h-7 rounded-sm bg-primary-container text-xs text-on-primary-container hover:bg-primary" onClick={openNew}>
            <Plus className="size-3.5" />
            この作品に人物を追加
          </Button>
        )}
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-on-surface-variant">この作品の登場人物</p>
            <h2 className="mt-1 flex items-center gap-2 text-xl font-semibold">
              <Users className="size-5 text-primary" />
              登場人物
              <span className="text-sm font-normal text-muted-foreground">（{includedCharacters.length}人）</span>
            </h2>
            <p className="mt-1 max-w-2xl text-[13px] leading-[18px] text-muted-foreground">
              この作品に参加中の人物を管理します。共有プールから追加することもできます。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant={showPool ? "secondary" : "outline"}
              size="sm"
              className="h-8 rounded-sm text-xs"
              onClick={() => setShowPool((value) => !value)}
            >
              {showPool ? "参加中のみ表示" : "プールから追加"}
            </Button>
            {(showPool || includedCharacters.length > 0) && (
              <div className="relative">
                <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="h-8 w-48 rounded-sm pl-8 text-xs"
                  placeholder="名前で検索…"
                  value={poolSearch}
                  onChange={(event) => setPoolSearch(event.target.value)}
                  aria-label="人物を検索"
                />
              </div>
            )}
          </div>
        </div>

        {allLoading || workLoading ? (
          <div className="flex items-center justify-center py-16 text-sm text-muted-foreground">
            <Loader2 className="mr-2 size-4 animate-spin" />
            人物を読み込み中…
          </div>
        ) : poolCharacters.length ? (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
            {poolCharacters.map((character) => (
              <Card
                key={character.id}
                draggable={character.included}
                onClick={() => handleCardClick(character)}
                onDragStart={(event) => {
                  if (!character.included) return;
                  setDraggingCharacterId(character.id);
                  const payload = serializeStoryDrag(character.id);
                  event.dataTransfer.effectAllowed = "move";
                  event.dataTransfer.setData(STORY_EPISODE_DND_MIME, payload);
                  event.dataTransfer.setData("text/plain", payload);
                }}
                onDragEnd={() => setDraggingCharacterId(null)}
                onDragOver={(event) => { if (character.included) event.preventDefault(); }}
                onDrop={(event) => { if (character.included) void handleCharacterDrop(event, character.id); }}
                className={`cursor-pointer rounded-md border bg-surface-charcoal p-3 transition-colors hover:border-outline ${
                  selectedId === character.id ? "border-primary ring-1 ring-primary/30" : "border-border-subtle"
                } ${draggingCharacterId === character.id ? "opacity-60" : ""}`}
                data-testid={`story-cast-card-${character.id}`}
              >
                <CardHeader className="p-0 pb-2">
                  <div className="flex items-start gap-3">
                    {character.included ? (
                      <GripVertical className="mt-4 size-4 shrink-0 cursor-grab text-muted-foreground" aria-hidden />
                    ) : (
                      <span className="mt-4 size-4 shrink-0" aria-hidden />
                    )}
                    <CharacterThumbnail character={character} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-start justify-between gap-2">
                        <CardTitle className="line-clamp-2 text-sm font-semibold leading-5 text-on-surface">{character.name}</CardTitle>
                      </div>
                      {character.aliases.length > 0 && (
                        <p className="mt-1 line-clamp-1 text-[11px] text-on-surface-variant">{character.aliases.join(" · ")}</p>
                      )}
                      <div className="mt-2">
                        <span className={`rounded-sm border px-2 py-0.5 text-[10px] ${aiModeBadge(character.aiMode).className}`}>
                          {aiModeBadge(character.aiMode).label}
                        </span>
                      </div>
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="p-0">
                  <p className="line-clamp-2 text-[12px] leading-[16px] text-on-surface-variant">
                    {character.summary || character.description || "説明なし"}
                  </p>
                  <div className="mt-3 flex items-center justify-between gap-2">
                    <Button
                      variant={character.included ? "secondary" : "outline"}
                      size="sm"
                      className="h-7 rounded-sm text-[11px]"
                      onClick={(event) => { event.stopPropagation(); void toggleIncluded(character); }}
                    >
                      {character.included ? "参加中" : "この作品に追加"}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 rounded-sm text-[11px]"
                      onClick={(event) => { event.stopPropagation(); openEdit(character); }}
                    >
                      編集
                    </Button>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : !showPool && includedCharacters.length === 0 ? (
          <div className="rounded-md border border-dashed border-border-subtle p-10 text-center">
            <p className="text-sm text-muted-foreground">この作品に参加中の人物はまだいません。</p>
            <Button variant="outline" size="sm" className="mt-4" onClick={() => setShowPool(true)}>
              共有プールから追加
            </Button>
          </div>
        ) : (
          <div className="rounded-md border border-dashed border-border-subtle p-8 text-center text-sm text-muted-foreground">
            条件に一致する人物はありません。
          </div>
        )}
        {!characters.length && (
          <div className="mt-4 rounded-md border border-dashed border-border-subtle p-8 text-center text-sm text-muted-foreground">
            共有人物はまだありません。右上から新規作成できます。
          </div>
        )}
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent size="3xl" className="max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editing ? "人物を編集" : "共有人物を作成"}</DialogTitle>
          </DialogHeader>
          <div className="space-y-6">
            <section className="space-y-3 rounded-md border border-border-subtle p-4">
              <h3 className="text-sm font-semibold text-foreground">画像・識別</h3>
              <div className="flex flex-wrap items-start gap-4">
                {editingCharacter ? <CharacterThumbnail character={editingCharacter} size="dialog" /> : (
                  <div className="flex size-20 items-center justify-center rounded-sm border border-dashed border-border-subtle bg-surface-container-high text-muted-foreground">
                    <User className="size-8" />
                  </div>
                )}
                {editing && (
                  <div className="flex flex-col gap-2">
                    <input
                      ref={imageInputRef}
                      type="file"
                      accept="image/jpeg,image/png,image/gif,image/webp"
                      className="hidden"
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) void uploadImage(file);
                        event.target.value = "";
                      }}
                    />
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={imageUploading}
                      onClick={() => imageInputRef.current?.click()}
                    >
                      {imageUploading ? <Loader2 className="size-3.5 animate-spin" /> : null}
                      画像を選ぶ
                    </Button>
                    {editingCharacter?.imageUrl && (
                      <Button type="button" variant="ghost" size="sm" disabled={imageUploading} onClick={() => void removeImage()}>
                        画像を削除
                      </Button>
                    )}
                  </div>
                )}
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <div>
                  <Label>名前</Label>
                  <Input className="mt-1" value={name} onChange={(event) => setName(event.target.value)} />
                </div>
                <div>
                  <Label>別名（カンマ区切り）</Label>
                  <Input className="mt-1" value={aliases} onChange={(event) => setAliases(event.target.value)} />
                </div>
              </div>
            </section>

            <section className="space-y-3 rounded-md border border-border-subtle p-4">
              <h3 className="text-sm font-semibold text-foreground">共有基本</h3>
              <div>
                <Label>一言サマリ</Label>
                <StoryAssistField
                  assist={assist}
                  target={{
                    fieldKind: "character_summary",
                    fieldLabel: "一言サマリ",
                    workId,
                    characterId: editing?.id,
                    getCurrentText: () => summary,
                  }}
                >
                  <Input className="mt-1" value={summary} onChange={(event) => setSummary(event.target.value)} placeholder="AI文脈に載る短い紹介" />
                </StoryAssistField>
              </div>
            </section>

            <section className="space-y-3 rounded-md border border-border-subtle p-4">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-sm font-semibold text-foreground">詳細設定</h3>
                <Button variant="link" size="sm" className="h-6" onClick={() => setDescription((value) => value ? `${value}\n\n${TEMPLATE}` : TEMPLATE)}>
                  <BookOpen className="size-3.5" />
                  テンプレを挿入
                </Button>
              </div>
              <StoryAssistField
                assist={assist}
                target={{
                  fieldKind: "character_description",
                  fieldLabel: "人物説明",
                  workId,
                  characterId: editing?.id,
                  getCurrentText: () => description,
                }}
              >
                <Textarea className="min-h-40" value={description} onChange={(event) => setDescription(event.target.value)} />
              </StoryAssistField>
            </section>

            <section className="space-y-3 rounded-md border border-border-subtle p-4">
              <h3 className="text-sm font-semibold text-foreground">非公開メモ</h3>
              <p className="text-xs text-muted-foreground">AIには渡しません</p>
              <StoryAssistField
                assist={assist}
                target={{
                  fieldKind: "character_notes",
                  fieldLabel: "非公開メモ",
                  workId,
                  characterId: editing?.id,
                  getCurrentText: () => notes,
                  requiresNotesConfirmation: true,
                }}
              >
                <Textarea className="min-h-20" value={notes} onChange={(event) => setNotes(event.target.value)} />
              </StoryAssistField>
            </section>

            <section className="space-y-3 rounded-md border border-border-subtle p-4">
              <h3 className="text-sm font-semibold text-foreground">AI参照</h3>
              <div className="grid gap-3 md:grid-cols-2">
                <div>
                  <Label>AI参照モード</Label>
                  <AppSelect className="mt-1 w-full" aria-label="AI参照モード" value={aiMode} onChange={(event) => setAiMode(event.target.value)}>
                    {modes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                  </AppSelect>
                </div>
                <div>
                  <Label>参照キーワード</Label>
                  <Input className="mt-1" value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder="カンマ区切り" />
                </div>
              </div>
            </section>

            <section className="space-y-3 rounded-md border border-border-subtle p-4">
              <h3 className="text-sm font-semibold text-foreground">この作品での役割</h3>
              {editing?.included ? (
                <StoryAssistField
                  assist={assist}
                  target={{
                    fieldKind: "character_role_note",
                    fieldLabel: "この作品での役割",
                    workId,
                    characterId: editing?.id,
                    getCurrentText: () => roleNote,
                  }}
                >
                  <Input className="mt-1" value={roleNote} onChange={(event) => setRoleNote(event.target.value)} placeholder="例: 主人公、語り手" />
                </StoryAssistField>
              ) : (
                <p className="text-sm text-muted-foreground">
                  {editing ? "作品に参加すると、この作品専用の役割メモを設定できます。" : "作成後、作品に参加すると役割メモを設定できます。"}
                </p>
              )}
            </section>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>キャンセル</Button>
            <Button onClick={() => void saveCharacter()} disabled={saving || !name.trim()}>
              {saving && <Loader2 className="size-3.5 animate-spin" />}
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <StoryAssistDialog
        assist={assist}
        onApplied={async (nextText) => {
          if (assist.target?.fieldKind === "character_summary") {
            setSummary(nextText);
          } else if (assist.target?.fieldKind === "character_description") {
            setDescription(nextText);
          } else if (assist.target?.fieldKind === "character_notes") {
            setNotes(nextText);
          } else if (assist.target?.fieldKind === "character_role_note") {
            setRoleNote(nextText);
          }
          toast.success("AI修正案を適用しました（保存ボタンで確定してください）");
        }}
      />
    </div>
  );
}

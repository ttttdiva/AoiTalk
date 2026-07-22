"use client";

/* eslint-disable @next/next/no-img-element */

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Plus,
  Play,
  Trash2,
  Loader2,
  BookOpen,
  Users,
  Film,
  BookText,
  Database,
  ChevronDown,
  ChevronUp,
  PenLine,
  MessageCircle,
  FileText,
  BookMarked,
} from "lucide-react";
import {
  pyFetch,
  unwrapScenario,
  scenarioDefaultImage,
  selectClassName,
  GENRES,
  SCENARIO_KINDS,
  TRPG_RULESETS,
  DIFFICULTIES,
  type Scenario,
  type ScenarioDetail,
  type ScenarioEpisode,
  type ScenarioPayload,
} from "@/lib/scenarios-page-utils";
import { CharacterEditor } from "@/components/scenarios/character-editor";
import { SceneEditor } from "@/components/scenarios/scene-editor";
import { EpisodeEditor } from "@/components/scenarios/episode-editor";
import { LoreBookEditor } from "@/components/scenarios/lorebook-editor";
import { CanonEditor } from "@/components/scenarios/canon-editor";
import { ScenarioLogPanel } from "@/components/scenarios/scenario-log-panel";
import { TRPGDocumentEditor } from "@/components/scenarios/trpg-document-editor";
import { useScenarioForm } from "@/components/scenarios/hooks/use-scenario-form";

// ─── Main page ───

export default function ScenariosPage() {
  const router = useRouter();
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ScenarioDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [startingWritingChat, setStartingWritingChat] = useState(false);

  // Form state（use-scenario-form フックへ抽出）
  const {
    formTitle,
    setFormTitle,
    formScenarioKind,
    setFormScenarioKind,
    formRuleset,
    setFormRuleset,
    formDescription,
    setFormDescription,
    formGenre,
    setFormGenre,
    formPerspective,
    setFormPerspective,
    formSetting,
    setFormSetting,
    formOpeningText,
    setFormOpeningText,
    formTags,
    setFormTags,
    formDifficulty,
    setFormDifficulty,
    voiceTone,
    setVoiceTone,
    voiceTenseRules,
    setVoiceTenseRules,
    voiceVocabulary,
    setVoiceVocabulary,
    voiceBannedExpressions,
    setVoiceBannedExpressions,
    voiceExamplePassages,
    setVoiceExamplePassages,
    voiceExpanded,
    setVoiceExpanded,
    populateFromScenario,
    resetToDefaults,
  } = useScenarioForm();

  // Episodes state
  const [episodes, setEpisodes] = useState<ScenarioEpisode[]>([]);

  // ─── Load scenarios ───

  const loadScenarios = useCallback(async () => {
    try {
      const data = await pyFetch<{ success: boolean; scenarios: Scenario[] }>(
        "/scenarios",
      );
      setScenarios(data.scenarios ?? []);
    } catch (err) {
      console.error("シナリオ一覧取得失敗:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadScenarios();
  }, [loadScenarios]);

  // ─── Load detail ───

  const loadDetail = useCallback(async (id: string) => {
    setDetailLoading(true);
    try {
      const data = await pyFetch<ScenarioPayload<ScenarioDetail>>(
        `/scenarios/${id}`,
      );
      const scenario = unwrapScenario(data);
      setDetail(scenario);
      setEpisodes(scenario.episodes ?? []);
      // エピソード取得
      try {
        const epData = await pyFetch<{ episodes: ScenarioEpisode[] }>(
          `/scenarios/${id}/episodes`,
        );
        setEpisodes(epData.episodes ?? []);
      } catch {
        setEpisodes(scenario.episodes ?? []);
      }
    } catch (err) {
      console.error("シナリオ詳細取得失敗:", err);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  // ─── Open editor ───

  const openEditor = useCallback(
    (scenario?: Scenario & Record<string, unknown>) => {
      if (scenario) {
        setSelectedId(scenario.id);
        populateFromScenario(scenario);
        setIsNew(false);
        loadDetail(scenario.id);
      } else {
        setSelectedId(null);
        resetToDefaults();
        setIsNew(true);
        setDetail(null);
        setEpisodes([]);
      }
      setDialogOpen(true);
    },
    [loadDetail, populateFromScenario, resetToDefaults],
  );
  // ─── Save scenario ───

  const handleSave = async () => {
    if (!formTitle.trim()) return;
    setSaving(true);
    try {
      const body = {
        title: formTitle,
        scenario_kind: formScenarioKind,
        ruleset: formScenarioKind === "trpg" ? formRuleset : "",
        description: formDescription,
        genre: formGenre,
        perspective: formPerspective,
        setting: formSetting,
        opening_text: formOpeningText,
        tags: formTags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        difficulty: formDifficulty,
        voice_tone: voiceTone,
        voice_tense_rules: voiceTenseRules,
        voice_vocabulary_register: voiceVocabulary,
        voice_banned_expressions: voiceBannedExpressions
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        voice_example_passages: voiceExamplePassages,
      };

      if (isNew) {
        const data = await pyFetch<ScenarioPayload<Scenario>>("/scenarios", {
          method: "POST",
          body: JSON.stringify(body),
        });
        const scenario = unwrapScenario(data);
        setSelectedId(scenario.id);
        setIsNew(false);
        loadDetail(scenario.id);
      } else if (selectedId) {
        await pyFetch(`/scenarios/${selectedId}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      }
      loadScenarios();
    } catch (err) {
      console.error("シナリオ保存失敗:", err);
    } finally {
      setSaving(false);
    }
  };

  // ─── Delete scenario ───

  const handleDelete = async () => {
    if (!selectedId) return;
    try {
      await pyFetch(`/scenarios/${selectedId}`, { method: "DELETE" });
      setDialogOpen(false);
      setSelectedId(null);
      loadScenarios();
    } catch (err) {
      console.error("シナリオ削除失敗:", err);
    }
  };

  // ─── Start linked workflows ───

  const handleStartWritingChat = async () => {
    if (!selectedId) return;
    setStartingWritingChat(true);
    try {
      const data = await pyFetch<{
        conversation_session_id?: string;
      }>(`/scenarios/${selectedId}/write`, {
        method: "POST",
        body: JSON.stringify({ user_id: "default" }),
      });
      if (data.conversation_session_id) {
        router.push(`/chat?s=${data.conversation_session_id}`);
      }
    } catch (err) {
      console.error("AI執筆チャット開始失敗:", err);
    } finally {
      setStartingWritingChat(false);
    }
  };

  const handleStartTrpg = () => {
    if (!selectedId || formScenarioKind !== "trpg") return;
    setPlaying(true);
    router.push(`/trpg?scenario_id=${selectedId}&create=1`);
  };

  return (
    <div className="flex h-full overflow-hidden">
      {/* Left panel: scenario list */}
      <div className="flex w-full flex-col border-r">
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 className="text-base font-semibold">シナリオ</h2>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => openEditor()}>
              <Plus className="mr-1 size-3.5" />
              新規
            </Button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {loading ? (
            <div className="flex items-center justify-center py-20">
              <Loader2 className="size-5 animate-spin text-muted-foreground" />
            </div>
          ) : scenarios.length === 0 ? (
            <div className="mx-auto flex max-w-xl flex-col items-center overflow-hidden rounded-2xl border border-border bg-card text-center text-sm text-muted-foreground">
              <img
                src="/images/ui/scene-portal.png"
                alt=""
                className="aspect-[16/7] w-full object-cover"
              />
              <div className="px-6 py-5">
                シナリオがありません。新規作成してください。
              </div>
            </div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {scenarios.map((scenario) => (
                <Card
                  key={scenario.id}
                  className="cursor-pointer overflow-hidden transition hover:border-primary/70 hover:bg-accent"
                  onClick={() => openEditor(scenario)}
                >
                  <img
                    src={scenarioDefaultImage(scenario)}
                    alt=""
                    className="aspect-[16/7] w-full object-cover"
                    loading="lazy"
                  />
                  <CardHeader className="pb-2">
                    <div className="flex items-start justify-between gap-2">
                      <CardTitle className="text-sm leading-snug">
                        {scenario.title}
                      </CardTitle>
                      <Badge variant="secondary" className="shrink-0">
                        {scenario.scenario_kind === "trpg"
                          ? scenario.ruleset || "trpg"
                          : scenario.genre}
                      </Badge>
                    </div>
                  </CardHeader>
                  <CardContent>
                    <p className="text-xs text-muted-foreground line-clamp-2">
                      {scenario.description || "説明なし"}
                    </p>
                    {scenario.tags && scenario.tags.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {scenario.tags.slice(0, 3).map((tag) => (
                          <Badge
                            key={tag}
                            variant="outline"
                            className="text-[10px]"
                          >
                            {tag}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Editor Dialog */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="flex h-[calc(100svh-2rem)] w-[calc(100vw-2rem)] max-w-[72rem] flex-col overflow-hidden sm:max-w-[72rem]">
          <DialogHeader className="shrink-0 pr-10">
            <DialogTitle>
              {isNew ? "新規シナリオ" : formTitle || "シナリオ編集"}
            </DialogTitle>
          </DialogHeader>

          <Tabs key={formScenarioKind} defaultValue="overview" className="min-h-0 flex-1 overflow-hidden">
            <TabsList className="h-auto w-full flex-wrap justify-start group-data-horizontal/tabs:h-auto">
              <TabsTrigger value="overview">
                <BookOpen className="mr-1 size-3.5" />
                概要
              </TabsTrigger>
              {formScenarioKind === "writing" && (
                <TabsTrigger value="episodes" disabled={isNew && !selectedId}>
                  <BookText className="mr-1 size-3.5" />
                  エピソード
                </TabsTrigger>
              )}
              {formScenarioKind === "trpg" && (
                <TabsTrigger value="trpgDocument" disabled={isNew && !selectedId}>
                  <FileText className="mr-1 size-3.5" />
                  TRPG本文
                </TabsTrigger>
              )}
              <TabsTrigger value="characters" disabled={isNew && !selectedId}>
                <Users className="mr-1 size-3.5" />
                キャラクター
              </TabsTrigger>
              {formScenarioKind === "writing" && (
                <TabsTrigger value="scenes" disabled={isNew && !selectedId}>
                  <Film className="mr-1 size-3.5" />
                  シーン
                </TabsTrigger>
              )}
              <TabsTrigger value="canon" disabled={isNew && !selectedId}>
                <Database className="mr-1 size-3.5" />
                Canon
              </TabsTrigger>
              <TabsTrigger value="lore" disabled={isNew && !selectedId}>
                <BookMarked className="mr-1 size-3.5" />
                ロア
              </TabsTrigger>
              <TabsTrigger value="logs" disabled={isNew && !selectedId}>
                <MessageCircle className="mr-1 size-3.5" />
                ログ
              </TabsTrigger>
            </TabsList>

            {/* ─── Overview Tab ─── */}
            <TabsContent value="overview" className="min-h-0 overflow-y-auto pr-1">
              <div className="space-y-4 pt-2">
                <div className="space-y-1.5">
                  <Label>タイトル</Label>
                  <Input
                    value={formTitle}
                    onChange={(e) => setFormTitle(e.target.value)}
                    placeholder="シナリオタイトル"
                  />
                </div>

                <div className="space-y-1.5">
                  <Label>説明</Label>
                  <LongTextEditor
                    value={formDescription}
                    onChange={setFormDescription}
                    placeholder="シナリオの概要"
                    minHeight={96}
                    maxHeight={220}
                  />
                </div>

                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label>シナリオ種別</Label>
                    <div className="flex gap-2">
                      {SCENARIO_KINDS.map((kind) => (
                        <Button
                          key={kind.value}
                          type="button"
                          variant={formScenarioKind === kind.value ? "default" : "outline"}
                          size="sm"
                          className="flex-1"
                          onClick={() => setFormScenarioKind(kind.value)}
                        >
                          {kind.label}
                        </Button>
                      ))}
                    </div>
                  </div>

                  {formScenarioKind === "trpg" && (
                    <div className="space-y-1.5">
                      <Label>TRPGシステム</Label>
                      <select
                        value={formRuleset}
                        onChange={(e) => setFormRuleset(e.target.value)}
                        className={selectClassName}
                      >
                        {TRPG_RULESETS.map((r) => (
                          <option key={r.value} value={r.value}>
                            {r.label}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                </div>

                <div className="grid gap-4 md:grid-cols-2">
                  {formScenarioKind === "writing" && (
                    <div className="space-y-1.5">
                      <Label>ジャンル</Label>
                      <select
                        value={formGenre}
                        onChange={(e) => setFormGenre(e.target.value)}
                        className={selectClassName}
                      >
                        {GENRES.map((g) => (
                          <option key={g} value={g}>
                            {g}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}

                  <div className="space-y-1.5">
                    <Label>視点</Label>
                    <div className="flex gap-2">
                      <Button
                        type="button"
                        variant={
                          formPerspective === "first_person"
                            ? "default"
                            : "outline"
                        }
                        size="sm"
                        className="flex-1"
                        onClick={() => setFormPerspective("first_person")}
                      >
                        一人称
                      </Button>
                      <Button
                        type="button"
                        variant={
                          formPerspective === "third_person"
                            ? "default"
                            : "outline"
                        }
                        size="sm"
                        className="flex-1"
                        onClick={() => setFormPerspective("third_person")}
                      >
                        三人称
                      </Button>
                    </div>
                  </div>
                </div>

                <div className="space-y-1.5">
                  <Label>舞台設定</Label>
                  <LongTextEditor
                    value={formSetting}
                    onChange={setFormSetting}
                    placeholder="物語の舞台・世界観"
                    minHeight={120}
                    maxHeight={300}
                  />
                </div>

                <div className="space-y-1.5">
                  <Label>オープニングテキスト</Label>
                  <LongTextEditor
                    value={formOpeningText}
                    onChange={setFormOpeningText}
                    placeholder="シナリオ開始時のテキスト"
                    minHeight={120}
                    maxHeight={300}
                  />
                </div>

                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label>タグ (カンマ区切り)</Label>
                    <Input
                      value={formTags}
                      onChange={(e) => setFormTags(e.target.value)}
                      placeholder="例: 冒険, 魔法, ドラゴン"
                    />
                  </div>

                  <div className="space-y-1.5">
                    <Label>難易度</Label>
                    <select
                      value={formDifficulty}
                      onChange={(e) => setFormDifficulty(e.target.value)}
                      className={selectClassName}
                    >
                      {DIFFICULTIES.map((d) => (
                        <option key={d} value={d}>
                          {d}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                {/* 文体設定（Voice） */}
                <div className="rounded-lg border">
                  <button
                    type="button"
                    className="flex w-full items-center justify-between px-3 py-2 text-sm font-medium"
                    onClick={() => setVoiceExpanded(!voiceExpanded)}
                  >
                    <span>文体設定（Voice）</span>
                    {voiceExpanded ? (
                      <ChevronUp className="size-4" />
                    ) : (
                      <ChevronDown className="size-4" />
                    )}
                  </button>
                  {voiceExpanded && (
                    <div className="space-y-3 border-t px-3 py-3">
                      <div className="space-y-1.5">
                        <Label>トーン</Label>
                        <LongTextEditor
                          value={voiceTone}
                          onChange={setVoiceTone}
                          placeholder="物語全体のトーン・雰囲気"
                          minHeight={96}
                          maxHeight={220}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label>時制ルール</Label>
                        <LongTextEditor
                          value={voiceTenseRules}
                          onChange={setVoiceTenseRules}
                          placeholder="過去形/現在形の使い分けルール"
                          minHeight={72}
                          maxHeight={180}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label>語彙レベル</Label>
                        <Input
                          value={voiceVocabulary}
                          onChange={(e) => setVoiceVocabulary(e.target.value)}
                          placeholder="例: カジュアル、文語調、中世風"
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label>禁止表現（カンマ区切り）</Label>
                        <Input
                          value={voiceBannedExpressions}
                          onChange={(e) =>
                            setVoiceBannedExpressions(e.target.value)
                          }
                          placeholder="例: 突然, いきなり, まるで"
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label>文体サンプル</Label>
                        <LongTextEditor
                          value={voiceExamplePassages}
                          onChange={setVoiceExamplePassages}
                          placeholder="目指す文体のサンプル文章"
                          minHeight={160}
                          maxHeight={340}
                        />
                      </div>
                    </div>
                  )}
                </div>

                <div className="sticky bottom-0 z-10 -mx-1 flex flex-wrap items-center justify-between gap-3 border-t bg-background/95 px-1 py-2 backdrop-blur">
                  <div className="flex flex-wrap gap-2">
                    {!isNew && selectedId && (
                      <>
                        <Button
                          variant="destructive"
                          size="sm"
                          onClick={handleDelete}
                        >
                          <Trash2 className="mr-1 size-3.5" />
                          削除
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={handleStartWritingChat}
                          disabled={startingWritingChat || formScenarioKind !== "writing"}
                        >
                          {startingWritingChat ? (
                            <Loader2 className="mr-1 size-3.5 animate-spin" />
                          ) : (
                            <PenLine className="mr-1 size-3.5" />
                          )}
                          AI執筆チャット
                        </Button>
                        {formScenarioKind === "trpg" && (
                          <Button
                            size="sm"
                            onClick={handleStartTrpg}
                            disabled={playing}
                          >
                            {playing ? (
                              <Loader2 className="mr-1 size-3.5 animate-spin" />
                            ) : (
                              <Play className="mr-1 size-3.5" />
                            )}
                            TRPGで遊ぶ
                          </Button>
                        )}
                      </>
                    )}
                  </div>
                  <Button
                    size="sm"
                    onClick={handleSave}
                    disabled={saving || !formTitle.trim()}
                  >
                    {saving && (
                      <Loader2 className="mr-1 size-3.5 animate-spin" />
                    )}
                    保存
                  </Button>
                </div>
              </div>
            </TabsContent>

            {/* ─── Episodes Tab ─── */}
            {formScenarioKind === "writing" && (
              <TabsContent value="episodes" className="min-h-0 overflow-y-auto pr-1">
                <div className="pt-2">
                  {detailLoading ? (
                    <div className="flex justify-center py-8">
                      <Loader2 className="size-5 animate-spin text-muted-foreground" />
                    </div>
                  ) : detail ? (
                    <EpisodeEditor
                      episodes={episodes}
                      scenarioId={detail.id}
                      onUpdate={() => loadDetail(detail.id)}
                    />
                  ) : (
                    <p className="py-4 text-center text-xs text-muted-foreground">
                      先にシナリオを保存してください
                    </p>
                  )}
                </div>
              </TabsContent>
            )}

            {/* ─── TRPG Document Tab ─── */}
            {formScenarioKind === "trpg" && (
              <TabsContent value="trpgDocument" className="min-h-0 overflow-y-auto pr-1">
                <div className="pt-2">
                  {detailLoading ? (
                    <div className="flex justify-center py-8">
                      <Loader2 className="size-5 animate-spin text-muted-foreground" />
                    </div>
                  ) : detail ? (
                    <TRPGDocumentEditor
                      documents={detail.trpg_documents ?? []}
                      scenarioId={detail.id}
                      onUpdate={() => loadDetail(detail.id)}
                    />
                  ) : (
                    <p className="py-4 text-center text-xs text-muted-foreground">
                      先にシナリオを保存してください
                    </p>
                  )}
                </div>
              </TabsContent>
            )}

            {/* ─── Characters Tab ─── */}
            <TabsContent value="characters" className="min-h-0 overflow-y-auto pr-1">
              <div className="pt-2">
                {detailLoading ? (
                  <div className="flex justify-center py-8">
                    <Loader2 className="size-5 animate-spin text-muted-foreground" />
                  </div>
                ) : detail ? (
                  <CharacterEditor
                    characters={detail.characters}
                    scenarioId={detail.id}
                    scenarioKind={formScenarioKind}
                    ruleset={formRuleset}
                    onUpdate={() => loadDetail(detail.id)}
                  />
                ) : (
                  <p className="py-4 text-center text-xs text-muted-foreground">
                    先にシナリオを保存してください
                  </p>
                )}
              </div>
            </TabsContent>

            {/* ─── Scenes Tab ─── */}
            {formScenarioKind === "writing" && (
              <TabsContent value="scenes" className="min-h-0 overflow-y-auto pr-1">
                <div className="pt-2">
                  {detailLoading ? (
                    <div className="flex justify-center py-8">
                      <Loader2 className="size-5 animate-spin text-muted-foreground" />
                    </div>
                  ) : detail ? (
                    <SceneEditor
                      scenes={detail.scenes}
                      scenarioId={detail.id}
                      episodes={episodes}
                      onUpdate={() => loadDetail(detail.id)}
                    />
                  ) : (
                    <p className="py-4 text-center text-xs text-muted-foreground">
                      先にシナリオを保存してください
                    </p>
                  )}
                </div>
              </TabsContent>
            )}

            {/* ─── Canon Tab ─── */}
            <TabsContent value="canon" className="min-h-0 overflow-y-auto pr-1">
              <div className="pt-2">
                {detailLoading ? (
                  <div className="flex justify-center py-8">
                    <Loader2 className="size-5 animate-spin text-muted-foreground" />
                  </div>
                ) : detail ? (
                  <CanonEditor scenarioId={detail.id} />
                ) : (
                  <p className="py-4 text-center text-xs text-muted-foreground">
                    先にシナリオを保存してください
                  </p>
                )}
              </div>
            </TabsContent>

            {/* ─── Lore Tab ─── */}
            <TabsContent value="lore" className="min-h-0 overflow-y-auto pr-1">
              <div className="pt-2">
                {detailLoading ? (
                  <div className="flex justify-center py-8">
                    <Loader2 className="size-5 animate-spin text-muted-foreground" />
                  </div>
                ) : detail ? (
                  <LoreBookEditor scenarioId={detail.id} />
                ) : (
                  <p className="py-4 text-center text-xs text-muted-foreground">
                    先にシナリオを保存してください
                  </p>
                )}
              </div>
            </TabsContent>

            {/* ─── Logs Tab ─── */}
            <TabsContent value="logs" className="min-h-0 overflow-y-auto pr-1">
              <div className="pt-2">
                {detailLoading ? (
                  <div className="flex justify-center py-8">
                    <Loader2 className="size-5 animate-spin text-muted-foreground" />
                  </div>
                ) : detail ? (
                  <ScenarioLogPanel scenarioId={detail.id} />
                ) : (
                  <p className="py-4 text-center text-xs text-muted-foreground">
                    先にシナリオを保存してください
                  </p>
                )}
              </div>
            </TabsContent>

          </Tabs>
        </DialogContent>
      </Dialog>
    </div>
  );
}

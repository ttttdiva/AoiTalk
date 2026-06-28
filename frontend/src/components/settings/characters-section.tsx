"use client";

import { useEffect, useMemo, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Users,
  ChevronDown,
  ChevronUp,
  Plus,
  Pencil,
  Trash2,
  Loader2,
  Download,
  Upload,
  ExternalLink,
  MessageCircle,
} from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { getLlmModelCatalog } from "@/lib/chat-api";
import type {
  LlmCatalogModelOption,
  LlmCatalogProvider,
} from "@/lib/chat-api";

interface Character {
  id: string;
  name: string;
  slug: string;
  character_type: string;
  system_prompt: string;
  model: string;
  allowed_tools: string[];
  is_enabled: boolean;
  // 音声
  voice_engine: string;
  voice_name: string;
  voice_id: string;
  speaker_id: number | null;
  voice_parameters: Record<string, number>;
  // 性格
  greeting: string;
  invalid_content_reply: string;
  fallback_reply: string;
  goodbye_reply: string;
  recognition_aliases: string[];
  // ロールプレイ
  description: string;
  personality_summary: string;
  first_message: string;
  alternate_greetings: string[];
  example_messages: string;
  scenario: string;
  // 外見
  appearance_tags: string;
  negative_tags: string;
  image_gen_engine: string;
  comfyui_config: Record<string, unknown>;
  avatar_image_path: string;
  // RP画像自動生成
  auto_image_gen: boolean;
  image_gen_trigger: string;
  image_gen_interval: number;
}

type CharacterModelOption = {
  value: string;
  label: string;
};

function buildCharacterModelOptions(
  providers: LlmCatalogProvider[],
  currentValue: string,
): CharacterModelOption[] {
  const options: CharacterModelOption[] = [{ value: "", label: "デフォルト" }];
  const seen = new Set([""]);

  const add = (
    provider: LlmCatalogProvider,
    modelId: string | undefined,
    model: LlmCatalogModelOption | undefined,
  ) => {
    const value = modelId?.trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    options.push({
      value,
      label: `${provider.label || provider.id} / ${model?.label || value}`,
    });
  };

  for (const provider of providers) {
    const configuredModel = provider.configured_model?.trim();
    if (configuredModel) {
      add(
        provider,
        configuredModel,
        provider.models.find((model) => model.id === configuredModel),
      );
    }
    for (const model of provider.models) {
      add(provider, model.id, model);
    }
  }

  const current = currentValue.trim();
  if (current && !seen.has(current)) {
    options.push({ value: current, label: current });
  }

  return options;
}

const TYPE_OPTIONS = [
  { value: "assistant", label: "アシスタント" },
  { value: "roleplay", label: "ロールプレイ" },
  { value: "trpg_npc", label: "TRPG NPC" },
  { value: "gm", label: "ゲームマスター" },
];

const TOOL_OPTIONS = [
  { value: "web_search", label: "Web検索" },
  { value: "read_workspace_file", label: "ファイル読込" },
  { value: "search_files", label: "ファイル検索" },
  { value: "execute_command", label: "コマンド実行" },
  { value: "list_project_information", label: "案件情報参照" },
  { value: "organize_project_information_from_folder", label: "案件DB更新" },
  { value: "create_task", label: "タスク作成" },
  { value: "media_assistant", label: "メディア" },
  { value: "utility_assistant", label: "ユーティリティ" },
  { value: "spotify_assistant", label: "Spotify" },
];

const VOICE_ENGINE_OPTIONS = [
  { value: "", label: "なし" },
  { value: "voicevox", label: "VOICEVOX" },
  { value: "voiceroid", label: "VOICEROID" },
  { value: "cevio", label: "CeVIO AI" },
  { value: "aivoice", label: "A.I.VOICE" },
  { value: "aivisspeech", label: "AivisSpeech" },
  { value: "nijivoice", label: "NijiVoice" },
  { value: "miotts", label: "MioTTS" },
];

const DEFAULT_VOICE_PARAMETER_FIELDS = [
  "volume",
  "pitch",
  "speed",
  "intonation",
] as const;

const MIOTTS_VOICE_PARAMETER_FIELDS = [
  "temperature",
  "top_p",
  "max_tokens",
  "repetition_penalty",
  "presence_penalty",
  "frequency_penalty",
  "best_of_n_n",
] as const;

const getVoiceParameterFields = (engine: string) =>
  engine === "miotts"
    ? MIOTTS_VOICE_PARAMETER_FIELDS
    : DEFAULT_VOICE_PARAMETER_FIELDS;

const IMAGE_GEN_OPTIONS = [
  { value: "", label: "なし" },
  { value: "comfyui", label: "ComfyUI" },
  { value: "gemini", label: "Gemini" },
];

async function pyFetch<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
  return res.json();
}

const EMPTY_CHAR: Character = {
  id: "",
  name: "",
  slug: "",
  character_type: "assistant",
  system_prompt: "",
  model: "",
  allowed_tools: [],
  is_enabled: true,
  description: "",
  personality_summary: "",
  first_message: "",
  alternate_greetings: [],
  example_messages: "",
  scenario: "",
  voice_engine: "",
  voice_name: "",
  voice_id: "",
  speaker_id: null,
  voice_parameters: {},
  greeting: "",
  invalid_content_reply: "",
  fallback_reply: "",
  goodbye_reply: "",
  recognition_aliases: [],
  appearance_tags: "",
  negative_tags: "",
  image_gen_engine: "",
  comfyui_config: {},
  avatar_image_path: "",
  auto_image_gen: false,
  image_gen_trigger: "scene_change",
  image_gen_interval: 5,
};

export function CharactersSection() {
  const router = useRouter();
  const [characters, setCharacters] = useState<Character[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [editChar, setEditChar] = useState<Character | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [toggling, setToggling] = useState<string | null>(null);

  const [importing, setImporting] = useState(false);
  const [importDialogOpen, setImportDialogOpen] = useState(false);
  // フォーム状態
  const [form, setForm] = useState<Character>({ ...EMPTY_CHAR });
  const [aliasInput, setAliasInput] = useState("");
  const [greetingInput, setGreetingInput] = useState("");
  const [modelProviders, setModelProviders] = useState<LlmCatalogProvider[]>([]);

  const enabledCount = characters.filter((c) => c.is_enabled).length;
  const modelOptions = useMemo(
    () => buildCharacterModelOptions(modelProviders, form.model),
    [form.model, modelProviders],
  );

  useEffect(() => {
    let cancelled = false;
    getLlmModelCatalog()
      .then((catalog) => {
        if (!cancelled) setModelProviders(catalog.providers);
      })
      .catch(() => {
        if (!cancelled) setModelProviders([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const fetchCharacters = useCallback(async () => {
    setLoading(true);
    try {
      const data = await pyFetch<{ success: boolean; characters: Character[] }>(
        "/characters/manage",
      );
      setCharacters(data.characters || []);
    } catch {
      setCharacters([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleToggle = useCallback(() => {
    if (!expanded && characters.length === 0) fetchCharacters();
    setExpanded((v) => !v);
  }, [expanded, characters.length, fetchCharacters]);

  const openEditor = useCallback((char: Character | null) => {
    if (char) {
      setIsNew(false);
      setForm({ ...char });
    } else {
      setIsNew(true);
      setForm({ ...EMPTY_CHAR });
    }
    setAliasInput("");
    setGreetingInput("");
    setEditChar(char || ({ id: "" } as Character));
  }, []);

  const updateForm = useCallback(
    <K extends keyof Character>(key: K, value: Character[K]) => {
      setForm((prev) => ({ ...prev, [key]: value }));
    },
    [],
  );

  const handleSave = useCallback(async () => {
    if (!form.name.trim() || !form.slug.trim()) return;
    setSaving(true);
    try {
      const body = { ...form };
      if (isNew) {
        await pyFetch("/characters/manage", {
          method: "POST",
          body: JSON.stringify(body),
        });
      } else {
        await pyFetch(
          `/characters/manage/${encodeURIComponent(editChar?.id || "")}`,
          {
            method: "PUT",
            body: JSON.stringify(body),
          },
        );
      }
      setEditChar(null);
      await fetchCharacters();
    } catch (err) {
      alert(err instanceof Error ? err.message : "保存に失敗しました");
    } finally {
      setSaving(false);
    }
  }, [form, isNew, editChar, fetchCharacters]);

  const handleDelete = useCallback(
    async (char: Character) => {
      if (!window.confirm(`キャラクター「${char.name}」を削除しますか？`))
        return;
      setDeleting(char.id);
      try {
        await pyFetch(`/characters/manage/${encodeURIComponent(char.id)}`, {
          method: "DELETE",
        });
        await fetchCharacters();
      } catch {
        /* ignore */
      } finally {
        setDeleting(null);
      }
    },
    [fetchCharacters],
  );

  const handleToggleEnabled = useCallback(
    async (char: Character) => {
      setToggling(char.id);
      try {
        await pyFetch(
          `/characters/manage/${encodeURIComponent(char.id)}/toggle`,
          {
            method: "POST",
          },
        );
        await fetchCharacters();
      } catch {
        /* ignore */
      } finally {
        setToggling(null);
      }
    },
    [fetchCharacters],
  );

  const handleStartRoleplay = useCallback(
    async (char: Character) => {
      try {
        const res = await fetch("/api/conversations", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ character_name: char.slug }),
        });
        if (!res.ok) throw new Error(`API Error: ${res.status}`);
        const data = (await res.json()) as { session?: { id?: string } };
        if (data.session?.id) {
          router.push(`/chat?s=${data.session.id}`);
        }
      } catch (err) {
        alert(err instanceof Error ? err.message : "ロールプレイ開始に失敗しました");
      }
    },
    [router],
  );

  const toggleTool = useCallback((tool: string) => {
    setForm((prev) => ({
      ...prev,
      allowed_tools: prev.allowed_tools.includes(tool)
        ? prev.allowed_tools.filter((t) => t !== tool)
        : [...prev.allowed_tools, tool],
    }));
  }, []);

  const addAlias = useCallback(() => {
    const alias = aliasInput.trim();
    if (!alias) return;
    setForm((prev) => ({
      ...prev,
      recognition_aliases: [...prev.recognition_aliases, alias],
    }));
    setAliasInput("");
  }, [aliasInput]);

  const removeAlias = useCallback((index: number) => {
    setForm((prev) => ({
      ...prev,
      recognition_aliases: prev.recognition_aliases.filter(
        (_, i) => i !== index,
      ),
    }));
  }, []);

  const addGreeting = useCallback(() => {
    const text = greetingInput.trim();
    if (!text) return;
    setForm((prev) => ({
      ...prev,
      alternate_greetings: [...prev.alternate_greetings, text],
    }));
    setGreetingInput("");
  }, [greetingInput]);

  const removeGreeting = useCallback((index: number) => {
    setForm((prev) => ({
      ...prev,
      alternate_greetings: prev.alternate_greetings.filter(
        (_, i) => i !== index,
      ),
    }));
  }, []);

  // ─── エクスポート ───
  const handleExportJson = useCallback(async (char: Character) => {
    try {
      const res = await fetch(
        `/api/python-proxy/characters/manage/${encodeURIComponent(char.id)}/export`,
        { credentials: "include" },
      );
      if (!res.ok) throw new Error("Export failed");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${char.slug}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(err instanceof Error ? err.message : "エクスポートに失敗しました");
    }
  }, []);

  const handleExportPng = useCallback(async (char: Character) => {
    try {
      const res = await fetch(
        `/api/python-proxy/characters/manage/${encodeURIComponent(char.id)}/export-png`,
        { credentials: "include" },
      );
      if (!res.ok) throw new Error("Export PNG failed");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${char.slug}.png`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(
        err instanceof Error ? err.message : "PNGエクスポートに失敗しました",
      );
    }
  }, []);

  // ─── インポート ───
  const handleImport = useCallback(
    async (file: File) => {
      setImporting(true);
      try {
        const formData = new FormData();
        formData.append("file", file);
        const res = await fetch("/api/python-proxy/characters/manage/import", {
          method: "POST",
          credentials: "include",
          body: formData,
        });
        if (!res.ok) throw new Error("Import failed");
        await fetchCharacters();
        setImportDialogOpen(false);
      } catch (err) {
        alert(err instanceof Error ? err.message : "インポートに失敗しました");
      } finally {
        setImporting(false);
      }
    },
    [fetchCharacters],
  );

  const selectClass =
    "h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30";

  return (
    <>
      <Card size="sm">
        <CardHeader
          className="cursor-pointer select-none"
          onClick={handleToggle}
        >
          <CardTitle className="flex items-center justify-between text-sm">
            <span className="flex items-center gap-2">
              <Users className="size-4" />
              キャラクター
              {characters.length > 0 && (
                <Badge variant="secondary" className="text-[10px]">
                  {enabledCount}/{characters.length}件 有効
                </Badge>
              )}
            </span>
            {expanded ? (
              <ChevronUp className="size-4" />
            ) : (
              <ChevronDown className="size-4" />
            )}
          </CardTitle>
        </CardHeader>
        {expanded && (
          <CardContent className="space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex gap-1.5">
                <Button variant="outline" size="sm" onClick={fetchCharacters}>
                  更新
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={importing}
                  onClick={() => setImportDialogOpen(true)}
                >
                  {importing ? (
                    <Loader2 className="size-3 animate-spin mr-1" />
                  ) : (
                    <Upload className="size-3 mr-1" />
                  )}
                  インポート
                </Button>
              </div>
              <Button size="sm" onClick={() => openEditor(null)}>
                <Plus className="size-3 mr-1" />
                新規作成
              </Button>
            </div>
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="size-3 animate-spin" />
                取得中...
              </div>
            ) : characters.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                キャラクターが登録されていません
              </p>
            ) : (
              <div className="max-h-80 space-y-2 overflow-auto">
                {characters.map((char) => (
                  <div
                    key={char.id}
                    className="flex items-start justify-between rounded-md border p-2.5"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="text-sm font-medium">{char.name}</span>
                        <Badge variant="outline" className="text-[10px]">
                          {TYPE_OPTIONS.find(
                            (o) => o.value === char.character_type,
                          )?.label || char.character_type}
                        </Badge>
                        {char.voice_engine && (
                          <Badge variant="outline" className="text-[10px]">
                            {VOICE_ENGINE_OPTIONS.find(
                              (o) => o.value === char.voice_engine,
                            )?.label || char.voice_engine}
                          </Badge>
                        )}
                        <Badge
                          variant={char.is_enabled ? "default" : "secondary"}
                          className="text-[10px]"
                        >
                          {char.is_enabled ? "有効" : "無効"}
                        </Badge>
                      </div>
                      <div className="text-xs text-muted-foreground mt-0.5">
                        {char.slug}
                        {char.model && ` / ${char.model}`}
                      </div>
                    </div>
                    <div className="flex gap-1 shrink-0">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleStartRoleplay(char)}
                        disabled={!char.is_enabled}
                        title="ロールプレイ開始"
                      >
                        <MessageCircle className="size-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleToggleEnabled(char)}
                        disabled={toggling === char.id}
                        title={char.is_enabled ? "無効にする" : "有効にする"}
                      >
                        {toggling === char.id ? (
                          <Loader2 className="size-3 animate-spin" />
                        ) : (
                          <span
                            className={`size-3 rounded-full border-2 ${
                              char.is_enabled
                                ? "bg-green-500 border-green-500"
                                : "border-muted-foreground"
                            }`}
                          />
                        )}
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleExportJson(char)}
                        title="JSONエクスポート"
                      >
                        <Download className="size-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleExportPng(char)}
                        title="PNGエクスポート"
                      >
                        <Download className="size-3 text-blue-500" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => openEditor(char)}
                      >
                        <Pencil className="size-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleDelete(char)}
                        disabled={deleting === char.id}
                      >
                        {deleting === char.id ? (
                          <Loader2 className="size-3 animate-spin" />
                        ) : (
                          <Trash2 className="size-3" />
                        )}
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        )}
      </Card>

      {/* インポートダイアログ */}
      <Dialog open={importDialogOpen} onOpenChange={setImportDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>キャラクターをインポート</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label className="text-xs text-muted-foreground">
                ローカルファイルから
              </Label>
              <Button
                variant="outline"
                className="w-full justify-start"
                disabled={importing}
                onClick={() => {
                  const input = document.createElement("input");
                  input.type = "file";
                  input.accept = ".json,.png";
                  input.onchange = (e) => {
                    const file = (e.target as HTMLInputElement).files?.[0];
                    if (file) handleImport(file);
                  };
                  input.click();
                }}
              >
                {importing ? (
                  <Loader2 className="size-4 animate-spin mr-2" />
                ) : (
                  <Upload className="size-4 mr-2" />
                )}
                ファイルを選択 (.json / .png)
              </Button>
              <p className="text-[11px] text-muted-foreground">
                対応形式: Character Card V2
              </p>
            </div>

            <div className="space-y-2">
              <Label className="text-xs text-muted-foreground">
                キャラクターを探す
              </Label>
              <div className="flex flex-col gap-1.5">
                <a
                  href="https://chub.ai/characters"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-between rounded-md border px-3 py-2 text-sm hover:bg-accent transition-colors"
                >
                  <span className="flex flex-col">
                    <span className="font-medium">Chub.ai</span>
                    <span className="text-[11px] text-muted-foreground">
                      CC V2カード配布の最大手
                    </span>
                  </span>
                  <ExternalLink className="size-3.5 text-muted-foreground" />
                </a>
                <a
                  href="https://character-tavern.com/"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-between rounded-md border px-3 py-2 text-sm hover:bg-accent transition-colors"
                >
                  <span className="flex flex-col">
                    <span className="font-medium">Character Tavern</span>
                    <span className="text-[11px] text-muted-foreground">
                      SillyTavern系キャラクター共有
                    </span>
                  </span>
                  <ExternalLink className="size-3.5 text-muted-foreground" />
                </a>
              </div>
              <p className="text-[11px] text-muted-foreground">
                外部サイトでDLしたファイルを上から読み込んでください
              </p>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* 編集ダイアログ */}
      <Dialog open={!!editChar} onOpenChange={(v) => !v && setEditChar(null)}>
        <DialogContent className="sm:max-w-2xl max-h-[85vh] overflow-auto">
          <DialogHeader>
            <DialogTitle>
              {isNew ? "キャラクター作成" : `キャラクター編集: ${form.name}`}
            </DialogTitle>
          </DialogHeader>
          <Tabs defaultValue="basic">
            <TabsList>
              <TabsTrigger value="basic">基本情報</TabsTrigger>
              <TabsTrigger value="roleplay">RP設定</TabsTrigger>
              <TabsTrigger value="voice">音声</TabsTrigger>
              <TabsTrigger value="appearance">外見</TabsTrigger>
              <TabsTrigger value="personality">性格</TabsTrigger>
            </TabsList>

            {/* ── 基本情報 ── */}
            <TabsContent value="basic" className="space-y-3 mt-3">
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <Label className="text-xs">表示名</Label>
                  <Input
                    value={form.name}
                    onChange={(e) => updateForm("name", e.target.value)}
                    placeholder="琴葉葵"
                  />
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">slug（ID）</Label>
                  <Input
                    value={form.slug}
                    onChange={(e) => updateForm("slug", e.target.value)}
                    disabled={!isNew}
                    placeholder="kotonoha_aoi"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <Label className="text-xs">タイプ</Label>
                  <select
                    value={form.character_type}
                    onChange={(e) =>
                      updateForm("character_type", e.target.value)
                    }
                    className={selectClass}
                  >
                    {TYPE_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">モデル</Label>
                  <select
                    value={form.model}
                    onChange={(e) => updateForm("model", e.target.value)}
                    className={selectClass}
                  >
                    {modelOptions.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="space-y-1">
                <Label className="text-xs">システムプロンプト</Label>
                <LongTextEditor
                  value={form.system_prompt}
                  onChange={(value) => updateForm("system_prompt", value)}
                  minHeight={180}
                  maxHeight={420}
                  placeholder="キャラクターの性格、口調、役割などを記述..."
                  fontFamily="monospace"
                  fontSize={12}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">使用可能ツール</Label>
                <div className="flex flex-wrap gap-1.5">
                  {TOOL_OPTIONS.map((tool) => (
                    <button
                      key={tool.value}
                      type="button"
                      onClick={() => toggleTool(tool.value)}
                      className={`rounded-md border px-2 py-0.5 text-xs transition-colors ${
                        form.allowed_tools.includes(tool.value)
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-input bg-transparent hover:bg-muted"
                      }`}
                    >
                      {tool.label}
                    </button>
                  ))}
                </div>
              </div>
            </TabsContent>

            {/* ── RP設定 ── */}
            <TabsContent value="roleplay" className="space-y-3 mt-3">
              <div className="space-y-1">
                <Label className="text-xs">キャラクター設定</Label>
                <LongTextEditor
                  value={form.description}
                  onChange={(value) => updateForm("description", value)}
                  minHeight={140}
                  maxHeight={320}
                  placeholder="キャラクターの性格、外見、バックストーリーなど..."
                  fontFamily="monospace"
                  fontSize={12}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">性格要約</Label>
                <LongTextEditor
                  value={form.personality_summary}
                  onChange={(value) => updateForm("personality_summary", value)}
                  minHeight={96}
                  maxHeight={220}
                  placeholder="明るく元気、ツンデレ、冷静沈着..."
                  fontSize={12}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">シナリオ</Label>
                <LongTextEditor
                  value={form.scenario}
                  onChange={(value) => updateForm("scenario", value)}
                  minHeight={96}
                  maxHeight={220}
                  placeholder="舞台設定や状況の説明..."
                  fontSize={12}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">初回メッセージ</Label>
                <LongTextEditor
                  value={form.first_message}
                  onChange={(value) => updateForm("first_message", value)}
                  minHeight={96}
                  maxHeight={220}
                  placeholder="チャット開始時にキャラクターが最初に送るメッセージ..."
                  fontSize={12}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">代替グリーティング</Label>
                <div className="space-y-1.5 mb-1.5">
                  {form.alternate_greetings.map((g, i) => (
                    <div key={i} className="flex items-start gap-1.5">
                      <div className="flex-1 rounded-md border p-2 text-xs whitespace-pre-wrap">
                        {g}
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => removeGreeting(i)}
                        className="shrink-0 mt-0.5"
                      >
                        <Trash2 className="size-3" />
                      </Button>
                    </div>
                  ))}
                </div>
                <div className="flex gap-1.5">
                  <LongTextEditor
                    value={greetingInput}
                    onChange={setGreetingInput}
                    placeholder="代替グリーティングを追加..."
                    minHeight={68}
                    maxHeight={160}
                    className="flex-1"
                    fontSize={12}
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={addGreeting}
                    className="shrink-0 self-end"
                  >
                    追加
                  </Button>
                </div>
              </div>
              <div className="space-y-1">
                <Label className="text-xs">会話例</Label>
                <LongTextEditor
                  value={form.example_messages}
                  onChange={(value) => updateForm("example_messages", value)}
                  minHeight={220}
                  maxHeight={440}
                  placeholder={
                    "{{char}}: こんにちは！今日はいい天気ですね。\n{{user}}: そうだね、散歩でも行こうか。\n{{char}}: いいですね！*嬉しそうに微笑む*"
                  }
                  fontFamily="monospace"
                  fontSize={12}
                />
              </div>

              {/* RP画像自動生成設定 */}
              <div className="space-y-2 rounded-md border p-2.5">
                <Label className="text-xs font-medium">画像自動生成</Label>
                <div className="flex items-center gap-2">
                  <Checkbox
                    checked={form.auto_image_gen}
                    onCheckedChange={(checked) =>
                      updateForm("auto_image_gen", !!checked)
                    }
                  />
                  <Label className="text-xs cursor-pointer">
                    RP中に画像を自動生成する
                  </Label>
                </div>
                {form.auto_image_gen && (
                  <div className="space-y-2 mt-1.5">
                    <div className="space-y-1">
                      <Label className="text-[10px] text-muted-foreground">
                        トリガー
                      </Label>
                      <select
                        value={form.image_gen_trigger}
                        onChange={(e) =>
                          updateForm("image_gen_trigger", e.target.value)
                        }
                        className={selectClass}
                      >
                        <option value="scene_change">シーン変更時</option>
                        <option value="every_n">N回ごと</option>
                        <option value="emotion_change">感情変化時</option>
                      </select>
                    </div>
                    {form.image_gen_trigger === "every_n" && (
                      <div className="space-y-1">
                        <Label className="text-[10px] text-muted-foreground">
                          間隔（メッセージ数）
                        </Label>
                        <Input
                          type="number"
                          min={1}
                          max={100}
                          value={form.image_gen_interval}
                          onChange={(e) =>
                            updateForm(
                              "image_gen_interval",
                              Number(e.target.value) || 5,
                            )
                          }
                          placeholder="5"
                        />
                      </div>
                    )}
                  </div>
                )}
              </div>
            </TabsContent>

            {/* ── 音声 ── */}
            <TabsContent value="voice" className="space-y-3 mt-3">
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <Label className="text-xs">音声エンジン</Label>
                  <select
                    value={form.voice_engine}
                    onChange={(e) => updateForm("voice_engine", e.target.value)}
                    className={selectClass}
                  >
                    {VOICE_ENGINE_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">ボイス名</Label>
                  <Input
                    value={form.voice_name}
                    onChange={(e) => updateForm("voice_name", e.target.value)}
                    placeholder="Kotonoha Aoi"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <Label className="text-xs">
                    {form.voice_engine === "miotts" ? "プリセットID" : "ボイスID"}
                  </Label>
                  <Input
                    value={form.voice_id}
                    onChange={(e) => updateForm("voice_id", e.target.value)}
                    placeholder={
                      form.voice_engine === "miotts" ? "jp_female" : "aoi_emo_44"
                    }
                  />
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">スピーカーID</Label>
                  <Input
                    type="number"
                    value={form.speaker_id ?? ""}
                    onChange={(e) =>
                      updateForm(
                        "speaker_id",
                        e.target.value ? Number(e.target.value) : null,
                      )
                    }
                    placeholder="3"
                  />
                </div>
              </div>
              <div className="space-y-1">
                <Label className="text-xs">音声パラメータ</Label>
                <div className="grid grid-cols-2 gap-3">
                  {getVoiceParameterFields(form.voice_engine).map(
                    (param) => (
                      <div key={param} className="space-y-1">
                        <Label className="text-[10px] text-muted-foreground">
                          {param}
                        </Label>
                        <Input
                          type="number"
                          step={
                            param === "max_tokens" || param === "best_of_n_n"
                              ? "1"
                              : "0.1"
                          }
                          value={form.voice_parameters[param] ?? ""}
                          onChange={(e) => {
                            const newParams = { ...form.voice_parameters };
                            if (e.target.value) {
                              newParams[param] = Number(e.target.value);
                            } else {
                              delete newParams[param];
                            }
                            updateForm("voice_parameters", newParams);
                          }}
                          placeholder="1.0"
                        />
                      </div>
                    ),
                  )}
                </div>
              </div>
            </TabsContent>

            {/* ── 外見 ── */}
            <TabsContent value="appearance" className="space-y-3 mt-3">
              <div className="space-y-1">
                <Label className="text-xs">画像生成エンジン</Label>
                <select
                  value={form.image_gen_engine}
                  onChange={(e) =>
                    updateForm("image_gen_engine", e.target.value)
                  }
                  className={selectClass}
                >
                  {IMAGE_GEN_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="space-y-1">
                <Label className="text-xs">外見タグ（Danbooruタグ形式）</Label>
                <LongTextEditor
                  value={form.appearance_tags}
                  onChange={(value) => updateForm("appearance_tags", value)}
                  minHeight={88}
                  maxHeight={180}
                  placeholder="1girl, blue_hair, short_hair, blue_eyes, school_uniform"
                  fontFamily="monospace"
                  fontSize={12}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">ネガティブタグ</Label>
                <LongTextEditor
                  value={form.negative_tags}
                  onChange={(value) => updateForm("negative_tags", value)}
                  minHeight={68}
                  maxHeight={160}
                  placeholder="low quality, blurry, worst quality"
                  fontFamily="monospace"
                  fontSize={12}
                />
              </div>
              {form.image_gen_engine === "comfyui" && (
                <div className="space-y-2 rounded-md border p-2.5">
                  <Label className="text-xs font-medium">ComfyUI設定</Label>
                  <div className="grid grid-cols-2 gap-2">
                    <div className="space-y-1">
                      <Label className="text-[10px] text-muted-foreground">
                        チェックポイント
                      </Label>
                      <Input
                        value={(form.comfyui_config.checkpoint as string) || ""}
                        onChange={(e) =>
                          updateForm("comfyui_config", {
                            ...form.comfyui_config,
                            checkpoint: e.target.value,
                          })
                        }
                        placeholder="waiNSFWIllustrious_v150.safetensors"
                        className="text-xs"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-[10px] text-muted-foreground">
                        LoRA
                      </Label>
                      <Input
                        value={(form.comfyui_config.lora as string) || ""}
                        onChange={(e) =>
                          updateForm("comfyui_config", {
                            ...form.comfyui_config,
                            lora: e.target.value,
                          })
                        }
                        placeholder="ChuugokuUsagi.safetensors"
                        className="text-xs"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-[10px] text-muted-foreground">
                        幅
                      </Label>
                      <Input
                        type="number"
                        value={(form.comfyui_config.width as number) || ""}
                        onChange={(e) =>
                          updateForm("comfyui_config", {
                            ...form.comfyui_config,
                            width: Number(e.target.value) || undefined,
                          })
                        }
                        placeholder="1280"
                        className="text-xs"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-[10px] text-muted-foreground">
                        高さ
                      </Label>
                      <Input
                        type="number"
                        value={(form.comfyui_config.height as number) || ""}
                        onChange={(e) =>
                          updateForm("comfyui_config", {
                            ...form.comfyui_config,
                            height: Number(e.target.value) || undefined,
                          })
                        }
                        placeholder="1536"
                        className="text-xs"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-[10px] text-muted-foreground">
                        ステップ数
                      </Label>
                      <Input
                        type="number"
                        value={(form.comfyui_config.steps as number) || ""}
                        onChange={(e) =>
                          updateForm("comfyui_config", {
                            ...form.comfyui_config,
                            steps: Number(e.target.value) || undefined,
                          })
                        }
                        placeholder="25"
                        className="text-xs"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-[10px] text-muted-foreground">
                        CFG
                      </Label>
                      <Input
                        type="number"
                        step="0.5"
                        value={(form.comfyui_config.cfg as number) || ""}
                        onChange={(e) =>
                          updateForm("comfyui_config", {
                            ...form.comfyui_config,
                            cfg: Number(e.target.value) || undefined,
                          })
                        }
                        placeholder="8.0"
                        className="text-xs"
                      />
                    </div>
                  </div>
                </div>
              )}
            </TabsContent>

            {/* ── 性格 ── */}
            <TabsContent value="personality" className="space-y-3 mt-3">
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <Label className="text-xs">挨拶</Label>
                  <Input
                    value={form.greeting}
                    onChange={(e) => updateForm("greeting", e.target.value)}
                    placeholder="どうしたの？"
                  />
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">お別れ</Label>
                  <Input
                    value={form.goodbye_reply}
                    onChange={(e) =>
                      updateForm("goodbye_reply", e.target.value)
                    }
                    placeholder="バイバイ！"
                  />
                </div>
              </div>
              <div className="space-y-1">
                <Label className="text-xs">不適切コンテンツ時の返答</Label>
                <Input
                  value={form.invalid_content_reply}
                  onChange={(e) =>
                    updateForm("invalid_content_reply", e.target.value)
                  }
                  placeholder="そういうことを言うと..."
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">エラー時の返答</Label>
                <Input
                  value={form.fallback_reply}
                  onChange={(e) => updateForm("fallback_reply", e.target.value)}
                  placeholder="エラーが起きちゃったみたい！"
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">認識エイリアス</Label>
                <div className="flex gap-1.5 flex-wrap mb-1.5">
                  {form.recognition_aliases.map((alias, i) => (
                    <Badge
                      key={i}
                      variant="secondary"
                      className="text-[10px] cursor-pointer hover:bg-destructive/20"
                      onClick={() => removeAlias(i)}
                    >
                      {alias} ×
                    </Badge>
                  ))}
                </div>
                <div className="flex gap-1.5">
                  <Input
                    value={aliasInput}
                    onChange={(e) => setAliasInput(e.target.value)}
                    placeholder="エイリアスを追加..."
                    onKeyDown={(e) =>
                      e.key === "Enter" && (e.preventDefault(), addAlias())
                    }
                    className="flex-1"
                  />
                  <Button variant="outline" size="sm" onClick={addAlias}>
                    追加
                  </Button>
                </div>
              </div>
            </TabsContent>
          </Tabs>

          <div className="flex items-center justify-end gap-2 mt-3">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setEditChar(null)}
            >
              キャンセル
            </Button>
            <Button
              size="sm"
              onClick={handleSave}
              disabled={saving || !form.name.trim() || !form.slug.trim()}
            >
              {saving && <Loader2 className="size-3 animate-spin mr-1" />}
              保存
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

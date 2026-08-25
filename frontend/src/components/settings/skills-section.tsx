"use client";

import { AppSelect } from "@/components/ui/app-select";

import { useState, useCallback, useEffect } from "react";
import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { useConfirm } from "@/hooks/use-confirm";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Wand2,
  ChevronDown,
  ChevronUp,
  Plus,
  Pencil,
  Trash2,
  Loader2,
  TestTube,
  Video,
} from "lucide-react";
import { SkillRecorderDialog } from "@/components/skills/skill-recorder-dialog";
import { isScreenRecordingSupported } from "@/lib/skill-recording";

interface Skill {
  name: string;
  description: string;
  prompt_template: string;
  trigger_mode: string;
  aliases: string[];
  bound_tools: string[];
  tags: string[];
  parameters: Record<string, unknown>[];
}

async function pyFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
  return res.json();
}

export function SkillsSection() {
  const confirm = useConfirm();
  // スキル一覧（サーバー状態）は SWR で管理。取得タイミングは従来どおり
  // 呼び出し側（トグル/更新/保存・削除後）で駆動するため自動 revalidation は無効化する。
  const { data: skills = [], mutate: mutateSkills } = useSWR<Skill[]>(
    "settings/skills",
    async () => {
      try {
        return (await pyFetch<{ skills: Skill[] }>("/skills")).skills || [];
      } catch {
        return [];
      }
    },
    {
      revalidateOnMount: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      keepPreviousData: true,
      dedupingInterval: 0,
    },
  );
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [editSkill, setEditSkill] = useState<Skill | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [recorderOpen, setRecorderOpen] = useState(false);
  // 画面録画対応可否はクライアントでのみ判定する（SSR ミスマッチ回避）。
  const [recordingSupported, setRecordingSupported] = useState(false);
  useEffect(() => {
    setRecordingSupported(isScreenRecordingSupported());
  }, []);

  // フォーム状態
  const [formName, setFormName] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [formPrompt, setFormPrompt] = useState("");
  const [formTrigger, setFormTrigger] = useState("manual");
  const [formAliases, setFormAliases] = useState("");
  const [formTags, setFormTags] = useState("");

  const fetchSkills = useCallback(async () => {
    setLoading(true);
    try {
      await mutateSkills();
    } finally {
      setLoading(false);
    }
  }, [mutateSkills]);

  const handleToggle = useCallback(() => {
    if (!expanded && skills.length === 0) fetchSkills();
    setExpanded((v) => !v);
  }, [expanded, skills.length, fetchSkills]);

  const openEditor = useCallback((skill: Skill | null) => {
    if (skill) {
      setIsNew(false);
      setFormName(skill.name);
      setFormDesc(skill.description);
      setFormPrompt(skill.prompt_template);
      setFormTrigger(skill.trigger_mode);
      setFormAliases(skill.aliases.join(", "));
      setFormTags(skill.tags.join(", "));
    } else {
      setIsNew(true);
      setFormName("");
      setFormDesc("");
      setFormPrompt("");
      setFormTrigger("manual");
      setFormAliases("");
      setFormTags("");
    }
    setTestResult(null);
    setEditSkill(skill || ({ name: "" } as Skill));
  }, []);

  const handleSave = useCallback(async () => {
    if (!formName.trim()) return;
    setSaving(true);
    try {
      const body = {
        name: formName.trim(),
        description: formDesc.trim(),
        prompt_template: formPrompt.trim(),
        trigger_mode: formTrigger,
        aliases: formAliases
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        tags: formTags
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      };
      if (isNew) {
        await pyFetch("/skills", {
          method: "POST",
          body: JSON.stringify(body),
        });
      } else {
        await pyFetch(`/skills/${encodeURIComponent(formName.trim())}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      }
      setEditSkill(null);
      await fetchSkills();
    } catch (err) {
      alert(err instanceof Error ? err.message : "保存に失敗しました");
    } finally {
      setSaving(false);
    }
  }, [formName, formDesc, formPrompt, formTrigger, formAliases, formTags, isNew, fetchSkills]);

  const handleDelete = useCallback(
    async (name: string) => {
      if (
        !(await confirm({
          description: `スキル「${name}」を削除しますか？`,
          destructive: true,
        }))
      )
        return;
      setDeleting(name);
      try {
        await pyFetch(`/skills/${encodeURIComponent(name)}`, { method: "DELETE" });
        await fetchSkills();
      } catch {
        // ignore
      } finally {
        setDeleting(null);
      }
    },
    [fetchSkills, confirm]
  );

  const handleTest = useCallback(async () => {
    if (!formName.trim()) return;
    setTestResult(null);
    try {
      const data = await pyFetch<{ rendered: string }>(
        `/skills/${encodeURIComponent(formName.trim())}/test`,
        {
          method: "POST",
          body: JSON.stringify({ parameters: {} }),
        }
      );
      setTestResult(data.rendered);
    } catch (err) {
      setTestResult(`エラー: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [formName]);

  const TRIGGER_LABELS: Record<string, string> = {
    manual: "手動",
    auto: "自動",
    keyword: "キーワード",
  };

  return (
    <>
      <Card size="sm" className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0">
        <CardHeader
          className="cursor-pointer select-none"
          onClick={handleToggle}
        >
          <CardTitle className="flex items-center justify-between text-sm">
            <span className="flex items-center gap-2">
              <Wand2 className="size-4" />
              スキル管理
              {skills.length > 0 && (
                <Badge variant="secondary" className="text-[10px]">
                  {skills.length}件
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
              <Button variant="outline" size="sm" onClick={fetchSkills}>
                更新
              </Button>
              <div className="flex gap-2">
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger
                      render={
                        <span className={recordingSupported ? "" : "cursor-not-allowed"} />
                      }
                    >
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setRecorderOpen(true)}
                        disabled={!recordingSupported}
                      >
                        <Video className="size-3 mr-1" />
                        録画して作成
                      </Button>
                    </TooltipTrigger>
                    {!recordingSupported && (
                      <TooltipContent>
                        このブラウザは画面録画に対応していません。
                      </TooltipContent>
                    )}
                  </Tooltip>
                </TooltipProvider>
                <Button size="sm" onClick={() => openEditor(null)}>
                  <Plus className="size-3 mr-1" />
                  新規作成
                </Button>
              </div>
            </div>
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="size-3 animate-spin" />
                取得中...
              </div>
            ) : skills.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                スキルが登録されていません
              </p>
            ) : (
              <div className="max-h-80 space-y-2 overflow-auto">
                {skills.map((skill) => (
                  <div
                    key={skill.name}
                    className="flex items-start justify-between rounded-md border p-2.5"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="text-sm font-medium">{skill.name}</span>
                        <Badge variant="outline" className="text-[10px]">
                          {TRIGGER_LABELS[skill.trigger_mode] || skill.trigger_mode}
                        </Badge>
                      </div>
                      {skill.description && (
                        <p className="text-xs text-muted-foreground mt-0.5">
                          {skill.description}
                        </p>
                      )}
                      {skill.aliases.length > 0 && (
                        <div className="flex gap-1 mt-1 flex-wrap">
                          {skill.aliases.map((a) => (
                            <Badge key={a} variant="secondary" className="text-[10px]">
                              {a}
                            </Badge>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="flex gap-1 shrink-0">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => openEditor(skill)}
                      >
                        <Pencil className="size-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleDelete(skill.name)}
                        disabled={deleting === skill.name}
                      >
                        {deleting === skill.name ? (
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

      {/* 編集ダイアログ */}
      <Dialog open={!!editSkill} onOpenChange={(v) => !v && setEditSkill(null)}>
        <DialogContent size="lg" className="max-h-[80vh] overflow-auto">
          <DialogHeader>
            <DialogTitle>{isNew ? "スキル作成" : "スキル編集"}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label className="text-xs">名前</Label>
              <Input
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                disabled={!isNew}
                placeholder="my-skill"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">説明</Label>
              <Input
                value={formDesc}
                onChange={(e) => setFormDesc(e.target.value)}
                placeholder="このスキルの説明"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">プロンプトテンプレート</Label>
              <LongTextEditor
                value={formPrompt}
                onChange={setFormPrompt}
                minHeight={160}
                maxHeight={360}
                placeholder="{{input}}に対して..."
                fontFamily="monospace"
                fontSize={12}
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label className="text-xs">トリガーモード</Label>
                <AppSelect
                  value={formTrigger}
                  onChange={(e) => setFormTrigger(e.target.value)}
                  className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                >
                  <option value="manual">手動</option>
                  <option value="auto">自動</option>
                  <option value="keyword">キーワード</option>
                </AppSelect>
              </div>
              <div className="space-y-1">
                <Label className="text-xs">エイリアス（カンマ区切り）</Label>
                <Input
                  value={formAliases}
                  onChange={(e) => setFormAliases(e.target.value)}
                  placeholder="alias1, alias2"
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">タグ（カンマ区切り）</Label>
              <Input
                value={formTags}
                onChange={(e) => setFormTags(e.target.value)}
                placeholder="tag1, tag2"
              />
            </div>
            {testResult && (
              <div className="rounded-md bg-muted p-2 text-xs font-mono whitespace-pre-wrap max-h-32 overflow-auto">
                {testResult}
              </div>
            )}
            <div className="flex items-center justify-between">
              {!isNew && (
                <Button variant="outline" size="sm" onClick={handleTest}>
                  <TestTube className="size-3 mr-1" />
                  テスト
                </Button>
              )}
              <div className="flex gap-2 ml-auto">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setEditSkill(null)}
                >
                  キャンセル
                </Button>
                <Button size="sm" onClick={handleSave} disabled={saving || !formName.trim()}>
                  {saving && <Loader2 className="size-3 animate-spin mr-1" />}
                  保存
                </Button>
              </div>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* 録画してスキルを作成 */}
      <SkillRecorderDialog
        open={recorderOpen}
        onOpenChange={setRecorderOpen}
        onSaved={fetchSkills}
      />
    </>
  );
}

"use client";

import { AppSelect } from "@/components/ui/app-select";

// 録画解析ドラフトのレビュー・編集・保存画面。
// この画面を必ず経由してからでないと Skill を保存できない（自動保存は禁止）。

import { useMemo, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { useConfirm } from "@/hooks/use-confirm";
import {
  ChevronDown,
  ChevronUp,
  Loader2,
  Save,
  Trash2,
  FileText,
  ListVideo,
} from "lucide-react";
import {
  deleteSkillRecording,
  saveSkillRecording,
  formatDuration,
  type SkillRecordingDraft,
} from "@/lib/skill-recording";

type ProjectOption = { id: string; name: string };

interface SkillRecordingReviewProps {
  recordingId: string;
  draft: SkillRecordingDraft;
  /** 呼び出し元にプロジェクト文脈がある場合の初期プロジェクト。 */
  contextProjectId?: string | null;
  /** 保存成功時（Skill 一覧の再取得などに使う）。 */
  onSaved: (location: string) => void;
  /** 破棄（DELETE 実行後にダイアログを閉じる）。 */
  onDiscarded: () => void;
}

async function fetchProjects(): Promise<ProjectOption[]> {
  try {
    const res = await fetch("/api/projects", {
      credentials: "include",
      headers: { "Content-Type": "application/json" },
    });
    if (!res.ok) return [];
    const data = (await res.json()) as { projects?: ProjectOption[] };
    return (data.projects || []).map((p) => ({ id: p.id, name: p.name }));
  } catch {
    return [];
  }
}

export function SkillRecordingReview({
  recordingId,
  draft,
  contextProjectId,
  onSaved,
  onDiscarded,
}: SkillRecordingReviewProps) {
  const confirm = useConfirm();

  const [name, setName] = useState(draft.name || "");
  const [description, setDescription] = useState(draft.description || "");
  const [triggerMode, setTriggerMode] = useState(draft.trigger_mode || "both");
  const [markdown, setMarkdown] = useState(draft.markdown || "");

  const [target, setTarget] = useState<"global" | "project">(
    contextProjectId ? "project" : "global",
  );
  const [projectId, setProjectId] = useState<string>(contextProjectId || "");
  const [deleteRecording, setDeleteRecording] = useState(true);

  const [saving, setSaving] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [showTranscript, setShowTranscript] = useState(false);
  const [showFrames, setShowFrames] = useState(false);

  // 保存先プロジェクト候補。project ターゲット選択時のみ取得する。
  const { data: projects = [] } = useSWR<ProjectOption[]>(
    target === "project" ? "skill-recording/projects" : null,
    fetchProjects,
    { revalidateOnFocus: false },
  );

  // 文脈プロジェクトが候補に無い場合でも選択維持できるよう初期値を補う。
  const projectOptions = useMemo(() => {
    if (
      contextProjectId &&
      !projects.some((p) => p.id === contextProjectId)
    ) {
      return [{ id: contextProjectId, name: "現在のプロジェクト" }, ...projects];
    }
    return projects;
  }, [projects, contextProjectId]);

  const canSave =
    name.trim().length > 0 &&
    !saving &&
    !discarding &&
    (target === "global" || projectId.trim().length > 0);

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      const result = await saveSkillRecording(recordingId, {
        name: name.trim(),
        description: description.trim(),
        markdown,
        trigger_mode: triggerMode,
        target,
        project_id: target === "project" ? projectId : undefined,
        delete_recording: deleteRecording,
      });
      onSaved(result.location);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存に失敗しました。");
    } finally {
      setSaving(false);
    }
  };

  const handleDiscard = async () => {
    if (
      !(await confirm({
        description:
          "このドラフトと録画データを破棄しますか？この操作は取り消せません。",
        destructive: true,
      }))
    ) {
      return;
    }
    setDiscarding(true);
    setError(null);
    try {
      await deleteSkillRecording(recordingId);
      onDiscarded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "破棄に失敗しました。");
      setDiscarding(false);
    }
  };

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        解析結果を確認・編集してから保存してください。内容は保存するまで Skill
        に反映されません。
      </p>

      <div className="space-y-1">
        <Label className="text-xs">名前</Label>
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="my-skill"
        />
      </div>

      <div className="space-y-1">
        <Label className="text-xs">説明</Label>
        <Input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="このスキルの説明"
        />
      </div>

      <div className="space-y-1">
        <Label className="text-xs">トリガーモード</Label>
        <AppSelect
          value={triggerMode}
          onChange={(e) => setTriggerMode(e.target.value)}
          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
        >
          <option value="manual">手動</option>
          <option value="auto">自動</option>
          <option value="both">両方</option>
        </AppSelect>
      </div>

      <div className="space-y-1">
        <Label className="text-xs">スキル本文（Markdown）</Label>
        <LongTextEditor
          value={markdown}
          onChange={setMarkdown}
          minHeight={180}
          maxHeight={360}
          placeholder="# スキルの手順..."
          fontFamily="monospace"
          fontSize={12}
        />
      </div>

      {draft.bound_tools.length > 0 && (
        <div className="space-y-1">
          <Label className="text-xs">紐づくツール</Label>
          <div className="flex flex-wrap gap-1">
            {draft.bound_tools.map((tool) => (
              <Badge key={tool} variant="secondary" className="text-[10px]">
                {tool}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {/* 文字起こし（折りたたみ） */}
      {draft.transcript && (
        <div className="rounded-md border">
          <button
            type="button"
            onClick={() => setShowTranscript((v) => !v)}
            className="flex w-full items-center justify-between px-2.5 py-2 text-xs font-medium"
          >
            <span className="flex items-center gap-1.5">
              <FileText className="size-3.5" />
              文字起こし全文
            </span>
            {showTranscript ? (
              <ChevronUp className="size-3.5" />
            ) : (
              <ChevronDown className="size-3.5" />
            )}
          </button>
          {showTranscript && (
            <div className="max-h-48 overflow-auto whitespace-pre-wrap border-t px-2.5 py-2 text-xs text-muted-foreground">
              {draft.transcript}
            </div>
          )}
        </div>
      )}

      {/* フレームノート タイムライン（折りたたみ） */}
      {draft.frame_notes.length > 0 && (
        <div className="rounded-md border">
          <button
            type="button"
            onClick={() => setShowFrames((v) => !v)}
            className="flex w-full items-center justify-between px-2.5 py-2 text-xs font-medium"
          >
            <span className="flex items-center gap-1.5">
              <ListVideo className="size-3.5" />
              画面ノート（{draft.frame_notes.length}件）
            </span>
            {showFrames ? (
              <ChevronUp className="size-3.5" />
            ) : (
              <ChevronDown className="size-3.5" />
            )}
          </button>
          {showFrames && (
            <div className="max-h-48 space-y-1.5 overflow-auto border-t px-2.5 py-2">
              {draft.frame_notes.map((frame, index) => (
                <div key={index} className="flex gap-2 text-xs">
                  <Badge variant="outline" className="shrink-0 text-[10px] font-mono">
                    {formatDuration(frame.time_sec)}
                  </Badge>
                  <span className="text-muted-foreground">{frame.note}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 保存先 */}
      <div className="space-y-2 rounded-md border p-2.5">
        <Label className="text-xs">保存先</Label>
        <div className="flex gap-4">
          <label className="flex items-center gap-1.5 text-sm">
            <input
              type="radio"
              name="skill-recording-target"
              checked={target === "global"}
              onChange={() => setTarget("global")}
            />
            グローバル
          </label>
          <label className="flex items-center gap-1.5 text-sm">
            <input
              type="radio"
              name="skill-recording-target"
              checked={target === "project"}
              onChange={() => setTarget("project")}
            />
            プロジェクト
          </label>
        </div>
        {target === "project" && (
          <AppSelect
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
          >
            <option value="">プロジェクトを選択...</option>
            {projectOptions.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </AppSelect>
        )}
      </div>

      <label className="flex items-center gap-2 text-sm">
        <Checkbox
          checked={deleteRecording}
          onCheckedChange={(checked) => setDeleteRecording(checked === true)}
        />
        保存後に元動画を削除する
      </label>

      {error && (
        <p className="rounded-md bg-destructive/10 px-2.5 py-2 text-xs text-destructive">
          {error}
        </p>
      )}

      <div className="flex items-center justify-between pt-1">
        <Button
          variant="outline"
          size="sm"
          onClick={handleDiscard}
          disabled={discarding || saving}
        >
          {discarding ? (
            <Loader2 className="size-3 animate-spin mr-1" />
          ) : (
            <Trash2 className="size-3 mr-1" />
          )}
          破棄
        </Button>
        <Button size="sm" onClick={handleSave} disabled={!canSave}>
          {saving ? (
            <Loader2 className="size-3 animate-spin mr-1" />
          ) : (
            <Save className="size-3 mr-1" />
          )}
          Skill として保存
        </Button>
      </div>
    </div>
  );
}

"use client";

import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import { Button } from "@/components/ui/button";
import {
  SlashCommandInput,
  type CommandCandidateSelection,
} from "@/components/tasks/slash-command-input";
import { TaskDescriptionEditor } from "@/components/editor/task-description-editor";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import { Plus, MoreHorizontal, Trash2, Settings2 } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { taskApi, type Tag, type Project } from "@/lib/task-api";
import { cn } from "@/lib/utils";
import { TaskDatePicker } from "@/components/tasks/task-date-picker";
import {
  buildAutoEstimateTaskPatch,
  buildTaskCommandCandidates,
  buildTaskSlashCommandFormPatch,
  normalizeTaskTitle,
  resolveTaskTagIds,
  taskValueCompletion,
  taskValuePreview,
} from "@/components/tasks/task-form-utils";
import { toTaskDatePayloadValue } from "@/lib/date-time";

const TAG_PRESET_COLORS = [
  "#ef4444",
  "#f97316",
  "#eab308",
  "#22c55e",
  "#06b6d4",
  "#3b82f6",
  "#8b5cf6",
  "#ec4899",
  "#6b7280",
];

/** /t 候補ドロップダウンのアイテムに表示する三点リーダーPopover */
function TagCandidateAction({
  tag,
  onUpdated,
}: {
  tag: Tag;
  onUpdated?: () => void;
}) {
  const [open, setOpen] = useState(false);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <button
            type="button"
            className="size-5 rounded flex items-center justify-center hover:bg-muted/80 transition-colors"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
            }}
          >
            <MoreHorizontal className="size-3.5" />
          </button>
        }
      />
      <PopoverContent className="w-auto p-2" align="end" side="right">
        <div className="grid gap-2">
          <div>
            <p className="text-[10px] text-muted-foreground mb-1.5">色を変更</p>
            <div className="flex gap-1">
              {TAG_PRESET_COLORS.map((color) => (
                <button
                  key={color}
                  type="button"
                  className={cn(
                    "size-5 rounded-full border-2 transition-transform hover:scale-110",
                    tag.color === color
                      ? "border-foreground scale-110"
                      : "border-transparent",
                  )}
                  style={{ backgroundColor: color }}
                  onClick={async () => {
                    try {
                      await taskApi.updateTag(tag.id, { color });
                      setOpen(false);
                      onUpdated?.();
                    } catch {}
                  }}
                />
              ))}
            </div>
          </div>
          <hr className="border-border" />
          <button
            type="button"
            className="flex items-center gap-1.5 text-xs text-destructive hover:bg-destructive/10 rounded px-1.5 py-1 transition-colors"
            onClick={async () => {
              setOpen(false);
              try {
                await taskApi.deleteTag(tag.id);
                onUpdated?.();
              } catch {}
            }}
          >
            <Trash2 className="size-3" />
            タグを削除
          </button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** タグセクションヘッダーの管理Popover（色変更・削除） */
function TagManagerPopover({
  tags,
  onUpdated,
}: {
  tags: Tag[];
  onUpdated?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  return (
    <Popover
      open={open}
      onOpenChange={(v) => {
        setOpen(v);
        if (!v) setEditingId(null);
      }}
    >
      <PopoverTrigger
        render={
          <button
            type="button"
            className="size-5 rounded flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
            title="タグを管理"
          >
            <Settings2 className="size-3.5" />
          </button>
        }
      />
      <PopoverContent className="w-64 p-3" align="end">
        <p className="text-xs font-medium mb-2">タグ管理</p>
        {tags.length === 0 ? (
          <p className="text-xs text-muted-foreground">タグなし</p>
        ) : (
          <div className="space-y-1">
            {tags.map((tag) => (
              <div
                key={tag.id}
                className="flex items-center gap-2 rounded px-1.5 py-1 hover:bg-muted/50"
              >
                {editingId === tag.id ? (
                  <div className="flex-1 space-y-1.5">
                    <div className="flex gap-1">
                      {TAG_PRESET_COLORS.map((color) => (
                        <button
                          key={color}
                          type="button"
                          className={cn(
                            "size-4 rounded-full border-2 transition-transform hover:scale-110",
                            tag.color === color
                              ? "border-foreground scale-110"
                              : "border-transparent",
                          )}
                          style={{ backgroundColor: color }}
                          onClick={async () => {
                            try {
                              await taskApi.updateTag(tag.id, { color });
                              setEditingId(null);
                              onUpdated?.();
                            } catch {}
                          }}
                        />
                      ))}
                    </div>
                    <button
                      type="button"
                      className="flex items-center gap-1 text-[10px] text-destructive hover:underline"
                      onClick={async () => {
                        try {
                          await taskApi.deleteTag(tag.id);
                          setEditingId(null);
                          onUpdated?.();
                        } catch {}
                      }}
                    >
                      <Trash2 className="size-2.5" />
                      削除
                    </button>
                  </div>
                ) : (
                  <>
                    <span
                      className="size-3 rounded-full shrink-0"
                      style={{ backgroundColor: tag.color || "#6b7280" }}
                    />
                    <span className="text-xs flex-1 truncate">{tag.name}</span>
                    <button
                      type="button"
                      className="size-5 rounded flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-muted"
                      onClick={() => setEditingId(tag.id)}
                    >
                      <MoreHorizontal className="size-3" />
                    </button>
                  </>
                )}
              </div>
            ))}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}

interface CreateTaskDialogProps {
  projectId: string;
  tags: Tag[];
  onCreated: () => void;
  onTagsUpdated?: () => void;
  externalOpen?: boolean;
  onExternalOpenChange?: (open: boolean) => void;
  /** ダイアログを開いた時にプリセットする開始日時（datetime-local形式） */
  defaultStartAt?: string;
  /** ダイアログを開いた時にプリセットする終日フラグ */
  defaultAllDay?: boolean;
  /** /m コマンドでプロジェクト移動する際に使うプロジェクト一覧 */
  projects?: Project[];
}

export function CreateTaskDialog({
  projectId,
  tags,
  onCreated,
  onTagsUpdated,
  externalOpen,
  onExternalOpenChange,
  defaultStartAt,
  defaultAllDay,
  projects,
}: CreateTaskDialogProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const open = externalOpen ?? internalOpen;
  const setOpen = onExternalOpenChange ?? setInternalOpen;
  const [loading, setLoading] = useState(false);

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState("open");
  const [priority, setPriority] = useState("none");
  const [startAt, setStartAt] = useState("");
  const [endAt, setEndAt] = useState("");
  const [allDay, setAllDay] = useState(false);
  const [autoCloseOnDue, setAutoCloseOnDue] = useState(false);
  const [selectedTagIds, setSelectedTagIds] = useState<string[]>([]);
  const [localTags, setLocalTags] = useState<Tag[]>(tags);
  const [targetProjectId, setTargetProjectId] = useState<string | null>(null);
  const selectedTagIdsRef = useRef<string[]>([]);
  const pendingTagResolutionRef = useRef<Promise<void> | null>(null);

  useEffect(() => {
    selectedTagIdsRef.current = selectedTagIds;
  }, [selectedTagIds]);

  useEffect(() => {
    setLocalTags((prev) => {
      const merged = new Map<string, Tag>();
      for (const tag of prev) merged.set(tag.id, tag);
      for (const tag of tags) merged.set(tag.id, tag);
      return Array.from(merged.values());
    });
  }, [tags]);

  const availableTags = localTags;
  const commandCandidates = useMemo(
    () =>
      buildTaskCommandCandidates({
        projects,
        tags: availableTags,
        selectedTagIds,
      }),
    [availableTags, projects, selectedTagIds],
  );

  // ダイアログが開いた時にデフォルト値をセット
  useEffect(() => {
    if (open) {
      if (defaultStartAt) setStartAt(defaultStartAt);
      if (defaultAllDay !== undefined) setAllDay(defaultAllDay);
    }
  }, [open, defaultStartAt, defaultAllDay]);

  // タイトルからスラッシュコマンドを抽出してフォームに反映（共通処理）
  const applySlashPatches = useCallback(
    (
      text: string,
      options?: {
        preserveTrailingSpace?: boolean;
        selection?: CommandCandidateSelection;
      },
    ): string => {
      if (!text.includes("/")) return text;
      const patch = buildTaskSlashCommandFormPatch({
        text,
        currentStartAt: startAt || null,
        currentEndAt: endAt || null,
        projects,
        preserveTrailingSpace: options?.preserveTrailingSpace,
        selection: options?.selection,
      });
      if (patch.startAt !== undefined) setStartAt(patch.startAt);
      if (patch.endAt !== undefined) setEndAt(patch.endAt);
      if (patch.status) setStatus(patch.status);
      if (patch.priority) setPriority(patch.priority);
      if (patch.allDay !== undefined) setAllDay(patch.allDay);
      if (patch.targetProjectId) setTargetProjectId(patch.targetProjectId);
      if (patch.targetProjectId && patch.targetProjectId !== projectId) {
        selectedTagIdsRef.current = [];
        setSelectedTagIds([]);
      }

      const tagNames = patch.tagNames;
      if (tagNames && tagNames.length > 0) {
        const pending = (async () => {
          const tagProjectId =
            patch.targetProjectId || targetProjectId || projectId;
          let tagOptions = availableTags;
          if (tagProjectId !== projectId) {
            try {
              tagOptions = await taskApi.listTags(tagProjectId);
            } catch {
              console.error("移動先プロジェクトのタグ取得に失敗しました");
              tagOptions = [];
            }
          }
          const { tagIds, createdTags } = await resolveTaskTagIds({
            tagNames,
            currentTagIds:
              tagProjectId === projectId ? selectedTagIdsRef.current : [],
            availableTags: tagOptions,
            createTag: async (name) => {
              try {
                return await taskApi.createTag(tagProjectId, { name });
              } catch {
                console.error(`タグ作成失敗: ${name}`);
                return null;
              }
            },
          });
          if (createdTags.length > 0 && tagProjectId === projectId) {
            setLocalTags((prev) => {
              const existingIds = new Set(prev.map((tag) => tag.id));
              const nextCreated = createdTags.filter(
                (tag) => !existingIds.has(tag.id),
              );
              return nextCreated.length > 0 ? [...prev, ...nextCreated] : prev;
            });
            onTagsUpdated?.();
          }
          selectedTagIdsRef.current = tagIds;
          setSelectedTagIds(tagIds);
        })();
        pendingTagResolutionRef.current = pending.finally(() => {
          if (pendingTagResolutionRef.current === pending) {
            pendingTagResolutionRef.current = null;
          }
        });
      }
      return patch.title;
    },
    [
      startAt,
      endAt,
      availableTags,
      projectId,
      targetProjectId,
      onTagsUpdated,
      projects,
    ],
  );

  // blur時: パースしてタイトルも更新
  const handleTitleBlur = useCallback(() => {
    const newTitle = applySlashPatches(title);
    if (newTitle !== title) setTitle(newTitle);
  }, [title, applySlashPatches]);

  // Enter時: パースしてタイトル返す（フォーカス維持）
  // パース後にスペースを残し、次のスラッシュコマンドを即座に入力可能にする
  const handleParseSlash = useCallback(
    (text: string, selection?: CommandCandidateSelection): string => {
      return applySlashPatches(text, {
        preserveTrailingSpace: true,
        selection,
      });
    },
    [applySlashPatches],
  );

  const resetForm = useCallback(() => {
    setTitle("");
    setDescription("");
    setStatus("open");
    setPriority("none");
    setStartAt("");
    setEndAt("");
    setAllDay(false);
    setAutoCloseOnDue(false);
    setSelectedTagIds([]);
    selectedTagIdsRef.current = [];
    pendingTagResolutionRef.current = null;
    setTargetProjectId(null);
    setLocalTags(tags);
  }, [tags]);

  const toggleTag = useCallback((tagId: string) => {
    setSelectedTagIds((prev) =>
      prev.includes(tagId)
        ? prev.filter((id) => id !== tagId)
        : [...prev, tagId],
    );
  }, []);

  const handleSubmit = useCallback(
    async (e?: React.FormEvent) => {
      e?.preventDefault();
      let submitTitle = title;
      let submitStatus = status;
      let submitPriority = priority;
      let submitStartAt = startAt;
      let submitEndAt = endAt;
      let submitAllDay = allDay;
      let submitProjectId = targetProjectId || projectId;
      let submitTagIds = selectedTagIdsRef.current;

      if (title.includes("/")) {
        const patch = buildTaskSlashCommandFormPatch({
          text: title,
          currentStartAt: startAt || null,
          currentEndAt: endAt || null,
          projects,
        });
        submitTitle = patch.title;
        if (patch.title !== title) setTitle(patch.title);
        if (patch.startAt !== undefined) {
          submitStartAt = patch.startAt;
          setStartAt(patch.startAt);
        }
        if (patch.endAt !== undefined) {
          submitEndAt = patch.endAt;
          setEndAt(patch.endAt);
        }
        if (patch.status) {
          submitStatus = patch.status;
          setStatus(patch.status);
        }
        if (patch.priority) {
          submitPriority = patch.priority;
          setPriority(patch.priority);
        }
        if (patch.allDay !== undefined) {
          submitAllDay = patch.allDay;
          setAllDay(patch.allDay);
        }
        if (patch.targetProjectId) {
          submitProjectId = patch.targetProjectId;
          setTargetProjectId(patch.targetProjectId);
          if (patch.targetProjectId !== projectId) {
            selectedTagIdsRef.current = [];
            setSelectedTagIds([]);
          }
        }
        if (patch.tagNames && patch.tagNames.length > 0) {
          const tagProjectId =
            patch.targetProjectId || targetProjectId || projectId;
          let tagOptions = availableTags;
          if (tagProjectId !== projectId) {
            try {
              tagOptions = await taskApi.listTags(tagProjectId);
            } catch {
              console.error("Failed to load tags for target project");
              tagOptions = [];
            }
          }
          const { tagIds, createdTags } = await resolveTaskTagIds({
            tagNames: patch.tagNames,
            currentTagIds:
              tagProjectId === projectId ? selectedTagIdsRef.current : [],
            availableTags: tagOptions,
            createTag: async (name) => {
              try {
                return await taskApi.createTag(tagProjectId, { name });
              } catch {
                console.error(`Failed to create tag: ${name}`);
                return null;
              }
            },
          });
          submitTagIds = tagIds;
          selectedTagIdsRef.current = tagIds;
          setSelectedTagIds(tagIds);
          if (createdTags.length > 0 && tagProjectId === projectId) {
            setLocalTags((prev) => {
              const existingIds = new Set(prev.map((tag) => tag.id));
              const nextCreated = createdTags.filter(
                (tag) => !existingIds.has(tag.id),
              );
              return nextCreated.length > 0 ? [...prev, ...nextCreated] : prev;
            });
            onTagsUpdated?.();
          }
        }
      }

      const normalizedTitle = normalizeTaskTitle(submitTitle);
      if (!normalizedTitle) return;

      setLoading(true);
      try {
        if (pendingTagResolutionRef.current) {
          await pendingTagResolutionRef.current;
          submitTagIds = selectedTagIdsRef.current;
        }
        const payloadStartAt = toTaskDatePayloadValue(submitStartAt || null, {
          allDay: submitAllDay,
        });
        const payloadEndAt = toTaskDatePayloadValue(submitEndAt || null, {
          allDay: submitAllDay,
        });
        const payload: Record<string, unknown> = {
          project_id: submitProjectId,
          title: normalizedTitle,
          description: description.trim() || undefined,
          status: submitStatus,
          priority: submitPriority,
          start_at: payloadStartAt || undefined,
          end_at: payloadEndAt || undefined,
          all_day: submitAllDay,
          auto_close_on_due: autoCloseOnDue,
          tag_ids: submitTagIds,
        };
        if (payloadStartAt || payloadEndAt) {
          Object.assign(
            payload,
            buildAutoEstimateTaskPatch({
              startAt: payloadStartAt,
              endAt: payloadEndAt,
              allDay: submitAllDay,
              currentEstimatedHours: null,
              currentMetadata: {},
              forceAuto: true,
            }),
          );
        }
        await taskApi.createTask(payload);
        resetForm();
        setOpen(false);
        onCreated();
      } catch (err) {
        console.error("タスク作成失敗:", err);
      } finally {
        setLoading(false);
      }
    },
    [
      description,
      status,
      priority,
      startAt,
      endAt,
      allDay,
      autoCloseOnDue,
      title,
      projectId,
      targetProjectId,
      projects,
      availableTags,
      onCreated,
      onTagsUpdated,
      resetForm,
      setOpen,
    ],
  );

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {/* 外部制御時（externalOpenが指定されている場合）はトリガーボタンを非表示 */}
      {externalOpen === undefined && (
        <DialogTrigger
          render={
            <Button size="sm">
              <Plus className="size-4" />
              新規タスク
            </Button>
          }
        />
      )}
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>新規タスク作成</DialogTitle>
          <DialogDescription>
            タスクの詳細を入力してください。
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="task-title">タイトル *</Label>
            <SlashCommandInput
              id="task-title"
              value={title}
              onChange={setTitle}
              onBlur={handleTitleBlur}
              getValuePreview={taskValuePreview}
              getValueCompletion={taskValueCompletion}
              onSubmitIntent={() => {
                void handleSubmit();
              }}
              submitOnEnter={false}
              onParseSlashCommands={handleParseSlash}
              commandCandidates={commandCandidates}
              renderCandidateAction={(cmd, candidate) => {
                if (cmd !== "/t") return null;
                const tag = availableTags.find(
                  (t) => t.name === candidate.value,
                );
                if (!tag) return null;
                return (
                  <TagCandidateAction tag={tag} onUpdated={onTagsUpdated} />
                );
              }}
              placeholder="タスクのタイトル（ /due /status /t /m で補完）"
              required
            />
          </div>

          <div className="grid gap-2">
            <Label>説明</Label>
            <TaskDescriptionEditor
              value={description}
              onChange={setDescription}
              placeholder="タスクの説明（任意）"
              minHeight={64}
              onSubmitIntent={() => {
                void handleSubmit();
              }}
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label>ステータス</Label>
              <Select value={status} onValueChange={(v) => v && setStatus(v)}>
                <SelectTrigger className="w-full">
                  <span>
                    {{
                      open: "未着手",
                      in_progress: "進行中",
                      on_hold: "保留",
                      review: "確認待ち",
                      closed: "完了",
                    }[status] || status}
                  </span>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="open">未着手</SelectItem>
                  <SelectItem value="in_progress">進行中</SelectItem>
                  <SelectItem value="on_hold">保留</SelectItem>
                  <SelectItem value="review">確認待ち</SelectItem>
                  <SelectItem value="closed">完了</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label>優先度</Label>
              <Select
                value={priority}
                onValueChange={(v) => v && setPriority(v)}
              >
                <SelectTrigger className="w-full">
                  <span>
                    {{
                      urgent: "Urgent",
                      high: "High",
                      medium: "Medium",
                      low: "Low",
                      none: "None",
                    }[priority] || priority}
                  </span>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="urgent">Urgent</SelectItem>
                  <SelectItem value="high">High</SelectItem>
                  <SelectItem value="medium">Medium</SelectItem>
                  <SelectItem value="low">Low</SelectItem>
                  <SelectItem value="none">None</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="grid gap-2">
            <Label>日時</Label>
            <TaskDatePicker
              startAt={startAt || null}
              endAt={endAt || null}
              onStartAtChange={(v) => setStartAt(v || "")}
              onEndAtChange={(v) => setEndAt(v || "")}
              allDay={allDay}
            />
          </div>

          <div className="flex items-center gap-2">
            <Checkbox
              id="task-allday"
              checked={allDay}
              onCheckedChange={(checked) => setAllDay(!!checked)}
            />
            <Label htmlFor="task-allday" className="cursor-pointer">
              終日
            </Label>
          </div>

          <div className="flex items-start gap-2">
            <Checkbox
              id="task-auto-close-on-due"
              checked={autoCloseOnDue}
              onCheckedChange={(checked) => setAutoCloseOnDue(!!checked)}
            />
            <div className="grid gap-0.5">
              <Label
                htmlFor="task-auto-close-on-due"
                className="cursor-pointer"
              >
                期日で自動完了
              </Label>
              <p className="text-[11px] text-muted-foreground">
                期日になると自動的に完了にします。終日は23:59までです。
              </p>
            </div>
          </div>

          <div className="grid gap-2">
            <div className="flex items-center justify-between">
              <Label>タグ</Label>
              <TagManagerPopover
                tags={availableTags}
                onUpdated={onTagsUpdated}
              />
            </div>
            <Popover>
              <PopoverTrigger
                render={
                  <div className="flex items-center gap-1.5 min-h-[32px] px-2 border rounded-md cursor-pointer hover:bg-muted/50 transition-colors flex-wrap">
                    {selectedTagIds.length === 0 ? (
                      <span className="text-xs text-muted-foreground">
                        クリックしてタグを選択...
                      </span>
                    ) : (
                      selectedTagIds.map((id) => {
                        const tag = availableTags.find((t) => t.id === id);
                        if (!tag) return null;
                        return (
                          <span
                            key={id}
                            className="inline-flex items-center text-[10px] px-1.5 h-5 rounded font-medium text-white"
                            style={{ backgroundColor: tag.color || "#6B7280" }}
                          >
                            {tag.name}
                          </span>
                        );
                      })
                    )}
                  </div>
                }
              />
              <PopoverContent className="w-56 p-0" align="start">
                {selectedTagIds.length > 0 && (
                  <div className="flex flex-wrap gap-1 p-2 border-b">
                    {selectedTagIds.map((id) => {
                      const tag = availableTags.find((t) => t.id === id);
                      if (!tag) return null;
                      return (
                        <span
                          key={id}
                          className="inline-flex items-center gap-0.5 text-[10px] px-1.5 h-5 rounded font-medium text-white cursor-pointer hover:opacity-80"
                          style={{ backgroundColor: tag.color || "#6B7280" }}
                          onClick={() => toggleTag(id)}
                        >
                          {tag.name} ×
                        </span>
                      );
                    })}
                  </div>
                )}
                <div className="max-h-48 overflow-y-auto p-1">
                  <p className="px-2 py-1 text-[10px] text-muted-foreground">
                    Select an option
                  </p>
                  {availableTags.map((tag) => (
                    <button
                      key={tag.id}
                      type="button"
                      className={cn(
                        "flex items-center gap-2 w-full rounded px-2 py-1 text-left hover:bg-accent transition-colors",
                        selectedTagIds.includes(tag.id) && "bg-accent/50",
                      )}
                      onClick={() => toggleTag(tag.id)}
                    >
                      <span
                        className="inline-flex items-center text-[10px] px-1.5 h-5 rounded font-medium text-white"
                        style={{ backgroundColor: tag.color || "#6B7280" }}
                      >
                        {tag.name}
                      </span>
                    </button>
                  ))}
                  {availableTags.length === 0 && (
                    <p className="px-2 py-1.5 text-xs text-muted-foreground">
                      タグなし（/t で作成できます）
                    </p>
                  )}
                </div>
              </PopoverContent>
            </Popover>
          </div>

          {targetProjectId && targetProjectId !== projectId && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground bg-muted/50 rounded-md px-3 py-2">
              <span>📁</span>
              <span>
                プロジェクト:{" "}
                <span className="font-medium text-foreground">
                  {projects?.find((p) => p.id === targetProjectId)?.name ??
                    targetProjectId}
                </span>
                に作成されます
              </span>
              <button
                type="button"
                className="ml-auto text-xs hover:text-foreground"
                onClick={() => setTargetProjectId(null)}
              >
                ✕
              </button>
            </div>
          )}

          <DialogFooter>
            <Button type="submit" disabled={loading || !title.trim()}>
              {loading ? "作成中..." : "作成"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

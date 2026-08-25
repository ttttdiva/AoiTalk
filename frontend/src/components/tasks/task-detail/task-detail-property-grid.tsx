"use client";

import {
  Calendar,
  Flag,
  Tag as TagIcon,
  Timer,
  Bell,
  Hourglass,
  CheckCircle,
  Repeat,
  BookOpen,
  ExternalLink,
  CircleDot,
  Users2,
  Play,
  Square,
  ChevronDown,
} from "lucide-react";

import type { RecurrenceSkipMode } from "@/lib/recurrence-preview";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { RecurrenceRule, Tag, Task } from "@/lib/task-api";
import { toLocalDateTimeInputValue } from "@/lib/date-time";
import { cn } from "@/lib/utils";
import { formatTimerClock } from "@/lib/task-time";
import { TaskDatePicker } from "@/components/tasks/task-date-picker";
import { formatTaskDateLabel } from "@/lib/task-date-label";
import { AssigneeSelector } from "@/components/tasks/task-detail/assignee-selector";

import { PropertyRow } from "@/components/tasks/task-detail/property-row";
import { TagSelector } from "@/components/tasks/task-detail/tag-selector";
import { TaskStatusMenuItems } from "@/components/tasks/task-status-menu-items";
import {
  STATUS_DOT_COLORS,
  formatDuration,
} from "@/components/tasks/task-detail/task-detail-utils";

type DateUpdatePartial = {
  start_at?: string | null;
  end_at?: string | null;
  all_day?: boolean;
};

export function TaskDetailPropertyGrid({
  task,
  effectiveTaskId,
  assigneeSelectorKey,
  tags,
  spaces,
  currentSpaceId,
  displayTaskTags,
  displayStartAt,
  displayEndAt,
  activeOccurrenceContext,
  editEstHours,
  setEditEstHours,
  estHoursSaving,
  handleEstHoursBlur,
  elapsedSeconds,
  timerLoading,
  handleTimer,
  docsNodeLoading,
  draftTagIds,
  setStatusSelectOpen,
  immediateUpdate,
  applyLocalDraftUpdate,
  buildDateTaskUpdate,
  moveOccurrenceDateRange,
  resolveTagUpdates,
  handleRenameTag,
  handleChangeTagColor,
  handleDeleteTag,
  handleCopyTagToSpace,
  handleOpenDocsNode,
  handleOpenMeetingNote,
  recurrenceRule,
  recFreq,
  recInterval,
  recByDay,
  recTriggerStatus,
  recCreateNew,
  recRecurForever,
  recResetStatusTo,
  recEndCount,
  recEndDate,
  recSkipWeekend,
  recSkipHoliday,
  recSkipMode,
  recurrenceSaving,
  handleFreqChange,
  setRecInterval,
  toggleWeekday,
  setRecTriggerStatus,
  setRecCreateNew,
  setRecRecurForever,
  setRecResetStatusTo,
  setRecEndCount,
  setRecEndDate,
  setRecSkipWeekend,
  setRecSkipHoliday,
  setRecSkipMode,
  handleSaveRecurrence,
  handleDeleteRecurrence,
  readOnly = false,
}: {
  task: Task;
  effectiveTaskId: string | null;
  assigneeSelectorKey: string;
  tags: Tag[];
  spaces: { id: string; name: string; slug: string }[];
  currentSpaceId: string | null;
  displayTaskTags: Tag[];
  displayStartAt: string | null | undefined;
  displayEndAt: string | null | undefined;
  activeOccurrenceContext: { start_at?: string | null } | null;
  editEstHours: string;
  setEditEstHours: (value: string) => void;
  estHoursSaving: boolean;
  handleEstHoursBlur: () => void;
  elapsedSeconds: number;
  timerLoading: boolean;
  handleTimer: () => void;
  docsNodeLoading: boolean;
  draftTagIds: string[];
  setStatusSelectOpen: (open: boolean) => void;
  immediateUpdate: (
    data: Record<string, unknown>,
  ) => Promise<Task | null> | void;
  applyLocalDraftUpdate: (data: Record<string, unknown>) => void;
  buildDateTaskUpdate: (partial: DateUpdatePartial) => Record<string, unknown>;
  moveOccurrenceDateRange: (values: {
    startAt: string | null;
    endAt: string | null;
  }) => Promise<void>;
  resolveTagUpdates: (
    tagNames: string[],
    targetProjectId?: string | null,
  ) => Promise<{ tag_ids: string[] }>;
  handleRenameTag: (tagId: string, name: string) => Promise<void>;
  handleChangeTagColor: (tagId: string, color: string) => Promise<void>;
  handleDeleteTag: (tagId: string) => Promise<void>;
  handleCopyTagToSpace: (tagId: string, spaceId: string) => Promise<void>;
  handleOpenDocsNode: () => void;
  handleOpenMeetingNote: () => void;
  recurrenceRule: RecurrenceRule | null;
  recFreq: string;
  recInterval: number;
  recByDay: string[];
  recTriggerStatus: string;
  recCreateNew: boolean;
  recRecurForever: boolean;
  recResetStatusTo: string;
  recEndCount: number | null;
  recEndDate: string | null;
  recSkipWeekend: boolean;
  recSkipHoliday: boolean;
  recSkipMode: RecurrenceSkipMode;
  recurrenceSaving: boolean;
  handleFreqChange: (newFreq: string) => void;
  setRecInterval: (value: number) => void;
  toggleWeekday: (dayKey: string) => void;
  setRecTriggerStatus: (value: string) => void;
  setRecCreateNew: (value: boolean) => void;
  setRecRecurForever: (value: boolean) => void;
  setRecResetStatusTo: (value: string) => void;
  setRecEndCount: (value: number | null) => void;
  setRecEndDate: (value: string | null) => void;
  setRecSkipWeekend: (value: boolean) => void;
  setRecSkipHoliday: (value: boolean) => void;
  setRecSkipMode: (value: RecurrenceSkipMode) => void;
  handleSaveRecurrence: () => void;
  handleDeleteRecurrence: () => void;
  readOnly?: boolean;
}) {
  return (
    <div
      className="grid grid-cols-1 divide-y border-y md:grid-cols-2 md:gap-x-6 md:divide-y-0 md:[&>div:nth-child(even)]:border-l md:[&>div:nth-child(even)]:pl-6 md:[&>div:nth-child(n+3)]:border-t [&>div]:min-h-10 [&>div]:py-2"
      data-task-detail-property-grid="true"
    >
      {/* Status */}
      <PropertyRow icon={<CircleDot className="size-3.5" />} label="ステータス">
        <div className="flex items-center gap-1">
          <DropdownMenu onOpenChange={setStatusSelectOpen}>
            <DropdownMenuTrigger
              disabled={readOnly}
              className="inline-flex h-7 w-auto items-center gap-1 rounded-md px-1.5 text-xs font-medium outline-none transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:ring-1 focus-visible:ring-ring data-[state=open]:bg-accent disabled:pointer-events-none"
            >
              <span className="flex items-center gap-1.5">
                <span
                  className={cn(
                    "size-2 rounded-full border-2",
                    STATUS_DOT_COLORS[task.status] || STATUS_DOT_COLORS.open,
                  )}
                />
                {{
                  todo: "未着手",
                  open: "未着手",
                  in_progress: "進行中",
                  on_hold: "保留",
                  review: "確認待ち",
                  closed: "完了",
                }[task.status] || task.status}
              </span>
              <ChevronDown className="size-3 opacity-50" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-40">
              <TaskStatusMenuItems
                currentStatus={task.status}
                onSelect={(status) => void immediateUpdate({ status })}
              />
            </DropdownMenuContent>
          </DropdownMenu>
          <button
            type="button"
            disabled={readOnly}
            title={task.status === "closed" ? "未着手に戻す" : "完了にする"}
            className={cn(
              "flex items-center justify-center size-6 rounded transition-colors",
              task.status === "closed"
                ? "bg-green-500/20 text-green-500 hover:bg-green-500/30"
                : "bg-muted/50 text-muted-foreground hover:bg-green-500/20 hover:text-green-500",
            )}
            onClick={() =>
              immediateUpdate({
                status: task.status === "closed" ? "open" : "closed",
              })
            }
          >
            <CheckCircle
              className={cn(
                "size-3.5",
                task.status === "closed" && "fill-green-500 text-green-50",
              )}
            />
          </button>
        </div>
      </PropertyRow>

      {/* Assignees */}
      <PropertyRow icon={<Users2 className="size-3.5" />} label="担当者">
        {readOnly ? (
          task.assignees && task.assignees.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {task.assignees.map((assignee) => (
                <span key={assignee.id} className="text-xs">
                  {assignee.display_name ||
                    assignee.username ||
                    assignee.user_id}
                </span>
              ))}
            </div>
          ) : (
            <span className="text-xs text-muted-foreground">Empty</span>
          )
        ) : (
          <AssigneeSelector
            key={assigneeSelectorKey}
            projectId={task.project_id}
            assignees={task.assignees || []}
            disabled={!effectiveTaskId}
            onChange={(assigneeIds) =>
              immediateUpdate({ assignee_ids: assigneeIds })
            }
          />
        )}
      </PropertyRow>

      {/* Dates */}
      <PropertyRow icon={<Calendar className="size-3.5" />} label="日時">
        <div className="flex items-center gap-1">
          {task.has_recurrence && (
            <Repeat
              className="size-3 shrink-0 text-muted-foreground"
              aria-label="繰り返しタスク"
            />
          )}
          {readOnly ? (
            <span className="text-xs text-muted-foreground">
              {formatTaskDateLabel(displayStartAt ?? null, {
                allDay: task.all_day,
                absoluteStyle: "long",
              })}
              {displayEndAt
                ? ` → ${formatTaskDateLabel(displayEndAt, {
                    allDay: task.all_day,
                    absoluteStyle: "long",
                  })}`
                : ""}
            </span>
          ) : (
            <TaskDatePicker
              startAt={toLocalDateTimeInputValue(displayStartAt, {
                allDay: task.all_day,
              })}
              endAt={toLocalDateTimeInputValue(displayEndAt, {
                allDay: task.all_day,
              })}
              allDay={task.all_day}
              deferCommitUntilClose={!!activeOccurrenceContext?.start_at}
              onRangeChange={
                activeOccurrenceContext?.start_at
                  ? moveOccurrenceDateRange
                  : ({ startAt, endAt }) =>
                      immediateUpdate(
                        buildDateTaskUpdate({
                          start_at: startAt,
                          end_at: endAt,
                        }),
                      )
              }
              onStartAtChange={(v) =>
                immediateUpdate(
                  buildDateTaskUpdate({
                    start_at: v,
                  }),
                )
              }
              onEndAtChange={(v) =>
                immediateUpdate(
                  buildDateTaskUpdate({
                    end_at: v,
                  }),
                )
              }
              recurrence={{
                recurrenceRule,
                freq: recFreq,
                interval: recInterval,
                byDay: recByDay,
                triggerStatus: recTriggerStatus,
                createNew: recCreateNew,
                recurForever: recRecurForever,
                resetStatusTo: recResetStatusTo,
                endCount: recEndCount,
                endDate: recEndDate,
                skipWeekend: recSkipWeekend,
                skipHoliday: recSkipHoliday,
                skipMode: recSkipMode,
                saving: recurrenceSaving,
                onFreqChange: handleFreqChange,
                onIntervalChange: setRecInterval,
                onToggleWeekday: toggleWeekday,
                onTriggerStatusChange: setRecTriggerStatus,
                onCreateNewChange: setRecCreateNew,
                onRecurForeverChange: setRecRecurForever,
                onResetStatusToChange: setRecResetStatusTo,
                onEndCountChange: setRecEndCount,
                onEndDateChange: setRecEndDate,
                onSkipWeekendChange: setRecSkipWeekend,
                onSkipHolidayChange: setRecSkipHoliday,
                onSkipModeChange: setRecSkipMode,
                onSave: handleSaveRecurrence,
                onDelete: handleDeleteRecurrence,
              }}
            />
          )}
        </div>
      </PropertyRow>

      {/* Auto-close on due */}
      <PropertyRow
        icon={<CheckCircle className="size-3.5" />}
        label="期日で自動完了"
      >
        <div className="flex items-start gap-2">
          <Checkbox
            id="task-detail-auto-close-on-due"
            checked={task.auto_close_on_due === true}
            disabled={readOnly}
            onCheckedChange={(checked) => {
              const next = !!checked;
              if (effectiveTaskId) {
                void immediateUpdate({ auto_close_on_due: next });
              } else {
                applyLocalDraftUpdate({ auto_close_on_due: next });
              }
            }}
          />
          <div className="grid gap-0.5">
            <label
              htmlFor="task-detail-auto-close-on-due"
              className={cn(
                "text-xs cursor-pointer",
                readOnly && "cursor-default",
              )}
            >
              期日で自動完了
            </label>
            <p className="text-[10px] text-muted-foreground">
              期日になると自動的に完了にします。終日は23:59までです。
            </p>
          </div>
        </div>
      </PropertyRow>

      {/* Priority */}
      <PropertyRow icon={<Flag className="size-3.5" />} label="優先度">
        <Select
          value={task.priority}
          disabled={readOnly}
          onValueChange={(v) => v && immediateUpdate({ priority: v })}
        >
          <SelectTrigger className="h-7 w-auto border-none shadow-none px-1.5 text-xs">
            <span>
              {{
                urgent: "Urgent",
                high: "High",
                medium: "Medium",
                low: "Low",
                none: "None",
              }[task.priority] || task.priority}
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
      </PropertyRow>

      {/* Time estimate */}
      <PropertyRow icon={<Hourglass className="size-3.5" />} label="見積工数">
        <div className="flex items-center gap-1">
          <Input
            type="number"
            value={editEstHours}
            onChange={(e) => setEditEstHours(e.target.value)}
            onBlur={handleEstHoursBlur}
            placeholder="-"
            className="h-6 w-16 text-xs border-none shadow-none px-1"
            min="0"
            step="0.5"
            disabled={estHoursSaving || readOnly}
          />
          {editEstHours && (
            <span className="text-[10px] text-muted-foreground">h</span>
          )}
        </div>
      </PropertyRow>

      {/* Track time */}
      <PropertyRow icon={<Timer className="size-3.5" />} label="時間計測">
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant={task.active_time_entry ? "destructive" : "outline"}
            className="h-6 text-xs px-2 gap-1"
            onClick={handleTimer}
            disabled={timerLoading || readOnly}
          >
            {task.active_time_entry ? (
              <>
                <Square className="size-3" />
                Stop
              </>
            ) : (
              <>
                <Play className="size-3" />
                Start
              </>
            )}
          </Button>
          {(() => {
            const completedSec = (task.activities || []).reduce(
              (sum: number, a: { duration_seconds?: number | null }) =>
                sum + (a.duration_seconds || 0),
              0,
            );
            if (completedSec <= 0 && !task.active_time_entry) return null;
            const isActive = !!task.active_time_entry;
            return (
              <span
                className={`text-xs font-mono tabular-nums ${
                  isActive
                    ? "text-green-600 dark:text-green-400"
                    : "text-muted-foreground"
                }`}
              >
                {isActive
                  ? formatTimerClock(elapsedSeconds)
                  : formatDuration(completedSec)}
              </span>
            );
          })()}
        </div>
      </PropertyRow>

      {/* Tags — ClickUp風: エリアクリックでタグ選択ドロップダウン */}
      <PropertyRow icon={<TagIcon className="size-3.5" />} label="タグ">
        {readOnly ? (
          <div className="flex flex-wrap gap-1">
            {displayTaskTags.length > 0 ? (
              displayTaskTags.map((tag) => (
                <Badge key={tag.id} variant="secondary">
                  {tag.name}
                </Badge>
              ))
            ) : (
              <span className="text-xs text-muted-foreground">Empty</span>
            )}
          </div>
        ) : (
          <TagSelector
            taskTags={displayTaskTags}
            allTags={tags}
            spaces={spaces}
            currentSpaceId={currentSpaceId}
            onToggle={(tagId) => {
              const current = effectiveTaskId
                ? (task.tags || []).map((t) => t.id)
                : draftTagIds;
              const newTagIds = current.includes(tagId)
                ? current.filter((id) => id !== tagId)
                : [...current, tagId];
              if (effectiveTaskId) {
                void immediateUpdate({ tag_ids: newTagIds });
                return;
              }
              applyLocalDraftUpdate({ tag_ids: newTagIds });
            }}
            onClear={() => {
              if (effectiveTaskId) {
                void immediateUpdate({ tag_ids: [] });
                return;
              }
              applyLocalDraftUpdate({ tag_ids: [] });
            }}
            onCreate={async (name) => {
              const updates = await resolveTagUpdates([name]);
              if (effectiveTaskId) {
                await immediateUpdate(updates);
                return;
              }
              applyLocalDraftUpdate(updates);
            }}
            onRenameTag={handleRenameTag}
            onChangeTagColor={handleChangeTagColor}
            onDeleteTag={handleDeleteTag}
            onCopyTagToSpace={handleCopyTagToSpace}
          />
        )}
      </PropertyRow>

      <PropertyRow icon={<BookOpen className="size-3.5" />} label="Docs">
        {effectiveTaskId && (!readOnly || task.knowledge_node_id) ? (
          <div className="flex flex-wrap gap-1">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-6 gap-1 px-2 text-xs"
              onClick={() => void handleOpenDocsNode()}
              disabled={docsNodeLoading}
            >
              {task.knowledge_node_id ? (
                <ExternalLink className="size-3" />
              ) : (
                <BookOpen className="size-3" />
              )}
              {task.knowledge_node_id ? "Docsで開く" : "Docsノート化"}
            </Button>
            {!readOnly && (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                className="h-6 gap-1 px-2 text-xs"
                onClick={() => void handleOpenMeetingNote()}
                disabled={docsNodeLoading}
              >
                <BookOpen className="size-3" />
                議事メモを開く
              </Button>
            )}
          </div>
        ) : (
          <span className="text-xs text-muted-foreground">
            保存後に作成できます
          </span>
        )}
      </PropertyRow>

      {/* Reminders */}
      <PropertyRow icon={<Bell className="size-3.5" />} label="リマインダー">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant={task.notifications_enabled ? "outline" : "default"}
              size="sm"
              className="h-6 px-2 text-[10px]"
              onClick={() =>
                immediateUpdate({
                  notifications_enabled: !task.notifications_enabled,
                })
              }
              disabled={readOnly}
            >
              {task.notifications_enabled
                ? "このタスクは通知しない"
                : "通知を再開"}
            </Button>
            <span className="text-[10px] text-muted-foreground">
              {task.notifications_enabled ? "通知中" : "このタスクの通知は無効"}
            </span>
          </div>
          <div
            className={cn(
              "flex flex-wrap gap-1",
              !task.notifications_enabled && "opacity-50",
            )}
          >
            {[
              { label: "5分前", value: 5 },
              { label: "15分前", value: 15 },
              { label: "30分前", value: 30 },
              { label: "1時間前", value: 60 },
              { label: "1日前", value: 1440 },
            ].map((preset) => {
              const offsets = task.reminder_offsets || [];
              const isActive = offsets.includes(preset.value);
              return (
                <Badge
                  key={preset.value}
                  variant={isActive ? "default" : "outline"}
                  className={cn(
                    "text-[10px] px-1.5 h-5",
                    task.notifications_enabled &&
                      !readOnly &&
                      "cursor-pointer hover:opacity-80",
                  )}
                  onClick={() => {
                    if (!task.notifications_enabled || readOnly) return;
                    const newOffsets = isActive
                      ? offsets.filter((o) => o !== preset.value)
                      : [...offsets, preset.value].sort((a, b) => a - b);
                    immediateUpdate({
                      reminder_offsets: newOffsets,
                    });
                  }}
                >
                  {preset.label}
                </Badge>
              );
            })}
          </div>
        </div>
      </PropertyRow>
    </div>
  );
}

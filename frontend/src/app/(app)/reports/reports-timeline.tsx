/* eslint-disable react-hooks/refs -- ドラッグ状態ref(isDragging/isResizing/isMoving/dayCol)を描画時のカーソル表示に参照する。page.tsx のインライン実装と等価で、親から RefObject として受け取るためルールが誤検知する。 */
import { type RefObject } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type Task, type TimeEntry } from "@/lib/task-api";
import {
  resolveProjectColorTokens,
  type ProjectColorTheme,
} from "@/lib/project-colors";
import {
  formatSeconds,
  formatHours,
  DAY_LABELS,
  HOUR_START,
  HOUR_END,
  TOTAL_HOURS,
  DEFAULT_ENTRY_COLOR,
  timelineBlockStyle,
  formatTimeWindow,
  toLocalYMD,
  getEntryDurationSeconds,
  getEntryHourRange,
  getTaskScheduleSegmentForDay,
  formatTaskScheduleLabel,
  getDayTotalSeconds,
  type EntryColumnLayout,
  type ResizeState,
  type MoveState,
} from "./reports-utils";

type DragState = {
  dayIndex: number;
  startHour: number;
  currentHour: number;
};
type DragForm = {
  dayIndex: number;
  startHour: number;
  endHour: number;
  topPct: number;
};

export function ReportsTimeline({
  weekDays,
  entriesByDay,
  now,
  entryLayoutsByDay,
  moveState,
  timeEntries,
  dragState,
  dragForm,
  visibleScheduledTasks,
  dayColRefs,
  handleDragMouseDown,
  handleDragMouseMove,
  handleDragMouseUp,
  isDraggingRef,
  isResizingRef,
  isMovingRef,
  dragFormInputRef,
  dragTaskName,
  setDragTaskName,
  dragSelectedTaskId,
  setDragSelectedTaskId,
  dragCreating,
  handleDragFormSubmit,
  handleDragFormCancel,
  selectedDragTask,
  dragTaskLoading,
  matchingDragTasks,
  resizeState,
  resolvedTheme,
  handleEntryMouseDown,
  openEditDialog,
  handleEntryContextMenu,
  handleResizeMouseDown,
}: {
  weekDays: Date[];
  entriesByDay: Map<string, TimeEntry[]>;
  now: Date;
  entryLayoutsByDay: Map<string, Map<string, EntryColumnLayout>>;
  moveState: MoveState | null;
  timeEntries: TimeEntry[];
  dragState: DragState | null;
  dragForm: DragForm | null;
  visibleScheduledTasks: Task[];
  dayColRefs: RefObject<Array<HTMLDivElement | null>>;
  handleDragMouseDown: (
    e: React.MouseEvent<HTMLDivElement>,
    dayIndex: number,
  ) => void;
  handleDragMouseMove: (
    e: React.MouseEvent<HTMLDivElement>,
    dayIndex: number,
  ) => void;
  handleDragMouseUp: () => void;
  isDraggingRef: RefObject<boolean>;
  isResizingRef: RefObject<boolean>;
  isMovingRef: RefObject<boolean>;
  dragFormInputRef: RefObject<HTMLInputElement | null>;
  dragTaskName: string;
  setDragTaskName: (value: string) => void;
  dragSelectedTaskId: string | null;
  setDragSelectedTaskId: (value: string | null) => void;
  dragCreating: boolean;
  handleDragFormSubmit: () => void;
  handleDragFormCancel: () => void;
  selectedDragTask: Task | null;
  dragTaskLoading: boolean;
  matchingDragTasks: Task[];
  resizeState: ResizeState | null;
  resolvedTheme: ProjectColorTheme;
  handleEntryMouseDown: (
    e: React.MouseEvent<HTMLDivElement>,
    entry: TimeEntry,
    dayIndex: number,
  ) => void;
  openEditDialog: (entry: TimeEntry) => void;
  handleEntryContextMenu: (
    e: React.MouseEvent<HTMLDivElement>,
    entry: TimeEntry,
  ) => void;
  handleResizeMouseDown: (
    e: React.MouseEvent<HTMLDivElement>,
    entry: TimeEntry,
    edge: "top" | "bottom",
    dayIndex: number,
  ) => void;
}) {
  return (
    <Card size="sm" className="overflow-visible">
      <CardHeader>
        <CardTitle className="text-sm">
          週間タイムライン
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            (クリック:編集 / ドラッグ:移動 / 上下端:リサイズ /
            右クリック:メニュー)
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <div className="min-w-[800px]">
          {/* 曜日ヘッダー */}
          <div className="grid grid-cols-[60px_repeat(7,1fr)] border-b border-border">
            <div className="p-2" />
            {weekDays.map((day, i) => {
              const dateKey = toLocalYMD(day);
              const dayEntries = entriesByDay.get(dateKey) || [];
              const totalSec = getDayTotalSeconds(dayEntries, now);
              const isToday = dateKey === toLocalYMD(new Date());
              return (
                <div
                  key={dateKey}
                  className={`p-2 text-center border-l border-border ${
                    isToday ? "bg-primary/10" : ""
                  }`}
                >
                  <div
                    className={`text-xs font-semibold ${
                      isToday
                        ? "text-primary"
                        : i >= 5
                          ? "text-muted-foreground"
                          : ""
                    }`}
                  >
                    {DAY_LABELS[i]}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {day.getMonth() + 1}/{day.getDate()}
                  </div>
                  {totalSec > 0 && (
                    <div className="text-xs font-medium text-primary mt-0.5">
                      {formatHours(totalSec)}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* タイムグリッド */}
          <div
            className="grid grid-cols-[60px_repeat(7,1fr)] relative"
            style={{ height: `${TOTAL_HOURS * 48}px` }}
          >
            <div className="relative">
              {Array.from({ length: TOTAL_HOURS + 1 }, (_, i) => (
                <div
                  key={i}
                  className="absolute right-2 text-[10px] text-muted-foreground -translate-y-1/2"
                  style={{ top: `${(i / TOTAL_HOURS) * 100}%` }}
                >
                  {HOUR_START + i}:00
                </div>
              ))}
            </div>

            {weekDays.map((day, dayIndex) => {
              const dateKey = toLocalYMD(day);
              let dayEntries = entriesByDay.get(dateKey) || [];
              const entryLayouts =
                entryLayoutsByDay.get(dateKey) ?? new Map();
              if (moveState?.moving) {
                dayEntries = dayEntries.filter(
                  (e) => e.id !== moveState.entryId,
                );
                if (
                  moveState.currentDayIndex === dayIndex &&
                  moveState.originalDayIndex !== dayIndex
                ) {
                  const movingEntry = timeEntries.find(
                    (e) => e.id === moveState.entryId,
                  );
                  if (movingEntry)
                    dayEntries = [...dayEntries, movingEntry];
                }
              }
              const isToday = dateKey === toLocalYMD(new Date());

              const showDragPreview =
                dragState && dragState.dayIndex === dayIndex;
              let dragPreviewTop = 0;
              let dragPreviewHeight = 0;
              if (showDragPreview && dragState) {
                const s = Math.min(
                  dragState.startHour,
                  dragState.currentHour,
                );
                const e = Math.max(
                  dragState.startHour,
                  dragState.currentHour,
                );
                dragPreviewTop = ((s - HOUR_START) / TOTAL_HOURS) * 100;
                dragPreviewHeight = ((e - s) / TOTAL_HOURS) * 100;
              }

              const showDragForm =
                dragForm && dragForm.dayIndex === dayIndex;
              let dragFormTop = 0;
              let dragFormHeight = 0;
              if (showDragForm && dragForm) {
                dragFormTop =
                  ((dragForm.startHour - HOUR_START) / TOTAL_HOURS) *
                  100;
                dragFormHeight =
                  ((dragForm.endHour - dragForm.startHour) /
                    TOTAL_HOURS) *
                  100;
              }

              const nowDateKey = toLocalYMD(now);
              const nowHour = now.getHours() + now.getMinutes() / 60;
              const showNowLine =
                dateKey === nowDateKey &&
                nowHour >= HOUR_START &&
                nowHour <= HOUR_END;
              const nowLineTop =
                ((nowHour - HOUR_START) / TOTAL_HOURS) * 100;
              const dayScheduleFrames = visibleScheduledTasks
                .map((task) => {
                  const segment = getTaskScheduleSegmentForDay(
                    task,
                    day,
                  );
                  if (!segment) return null;
                  return { task, ...segment };
                })
                .filter(
                  (
                    value,
                  ): value is {
                    task: Task;
                    startHour: number;
                    endHour: number;
                  } => value !== null,
                );

              return (
                <div
                  key={dateKey}
                  ref={(el) => {
                    dayColRefs.current[dayIndex] = el;
                  }}
                  data-day-col={dayIndex}
                  className={`relative border-l border-border select-none ${
                    isToday ? "bg-primary/5" : ""
                  }`}
                  onMouseDown={(e) => handleDragMouseDown(e, dayIndex)}
                  onMouseMove={(e) => handleDragMouseMove(e, dayIndex)}
                  onMouseUp={handleDragMouseUp}
                  onMouseLeave={() => {
                    if (isDraggingRef.current) handleDragMouseUp();
                  }}
                  style={{
                    cursor: isResizingRef.current
                      ? "ns-resize"
                      : isMovingRef.current
                        ? "grabbing"
                        : isDraggingRef.current
                          ? "ns-resize"
                          : "crosshair",
                  }}
                >
                  {/* 時間区切り */}
                  {Array.from({ length: TOTAL_HOURS + 1 }, (_, i) => (
                    <div
                      key={i}
                      className="absolute left-0 right-0 border-t border-border/40"
                      style={{
                        top: `${(i / TOTAL_HOURS) * 100}%`,
                      }}
                    />
                  ))}

                  {showNowLine && (
                    <div
                      className="absolute left-0 right-0 z-10 border-t-2 border-red-500/80 pointer-events-none"
                      style={{ top: `${nowLineTop}%` }}
                    >
                      <div className="absolute -left-1 -top-1 size-2 rounded-full bg-red-500" />
                    </div>
                  )}

                  {/* 新規作成プレビュー */}
                  {dayScheduleFrames.map(
                    ({ task, startHour, endHour }) => {
                      const clampedStart = Math.max(
                        startHour,
                        HOUR_START,
                      );
                      const clampedEnd = Math.min(endHour, HOUR_END);
                      if (
                        clampedEnd <= HOUR_START ||
                        clampedStart >= HOUR_END
                      ) {
                        return null;
                      }
                      const endPct =
                        ((clampedEnd - HOUR_START) / TOTAL_HOURS) * 100;
                      const heightPct =
                        ((clampedEnd - clampedStart) / TOTAL_HOURS) *
                        100;
                      return (
                        <div
                          key={`schedule-${task.id}`}
                          className="absolute left-1 right-1 z-[1] overflow-hidden rounded-md border border-dashed border-primary/55 bg-primary/8 pointer-events-none"
                          style={{
                            top: `${endPct}%`,
                            height: `${heightPct}%`,
                            minHeight: "18px",
                            transform: "translateY(-100%)",
                          }}
                          title={formatTaskScheduleLabel(task)}
                        >
                          {heightPct > 4 && (
                            <div className="px-1 py-0.5 text-[9px] font-medium leading-tight text-primary/90 truncate">
                              {task.title}
                            </div>
                          )}
                        </div>
                      );
                    },
                  )}

                  {showDragPreview && dragPreviewHeight > 0 && (
                    <div
                      className="absolute left-0.5 right-0.5 rounded bg-primary/30 border-2 border-primary/50 z-20 pointer-events-none"
                      style={{
                        top: `${dragPreviewTop}%`,
                        height: `${Math.max(dragPreviewHeight, 1)}%`,
                        minHeight: "12px",
                      }}
                    />
                  )}

                  {/* 新規作成フォーム */}
                  {showDragForm && dragForm && (
                    <>
                      <div
                        className="absolute left-0.5 right-0.5 rounded bg-primary/20 border-2 border-primary/60 z-20 pointer-events-none"
                        style={{
                          top: `${dragFormTop}%`,
                          height: `${Math.max(dragFormHeight, 1)}%`,
                          minHeight: "18px",
                        }}
                      >
                        <div className="text-[9px] text-primary px-1 pt-0.5 font-medium">
                          {Math.floor(dragForm.startHour)}:
                          {String(
                            Math.round((dragForm.startHour % 1) * 60),
                          ).padStart(2, "0")}
                          {" ~ "}
                          {Math.floor(dragForm.endHour)}:
                          {String(
                            Math.round((dragForm.endHour % 1) * 60),
                          ).padStart(2, "0")}
                        </div>
                      </div>
                      <div
                        data-drag-form
                        className="absolute left-0 right-0 z-30"
                        style={{
                          top: `calc(${dragFormTop + dragFormHeight}% + 4px)`,
                        }}
                      >
                        <div className="mx-0.5 bg-popover border border-border rounded-md shadow-lg p-2">
                          <input
                            ref={dragFormInputRef}
                            type="text"
                            className="w-full text-xs bg-transparent border border-input rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-primary"
                            placeholder="タスク名を入力..."
                            value={dragTaskName}
                            onChange={(e) => {
                              setDragTaskName(e.target.value);
                              if (dragSelectedTaskId) {
                                setDragSelectedTaskId(null);
                              }
                            }}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                e.preventDefault();
                                handleDragFormSubmit();
                              } else if (e.key === "Escape") {
                                handleDragFormCancel();
                              }
                            }}
                            disabled={dragCreating}
                          />
                          {selectedDragTask && (
                            <div className="mt-1 text-[10px] text-primary">
                              選択中: {selectedDragTask.title}
                            </div>
                          )}
                          <div className="mt-1 space-y-1">
                            <div className="text-[10px] text-muted-foreground">
                              既存タスクを選ぶか、タスク名を入力して新規作成します。
                            </div>
                            {dragTaskLoading ? (
                              <div className="text-[10px] text-muted-foreground">
                                読み込み中...
                              </div>
                            ) : matchingDragTasks.length > 0 ? (
                              <div className="flex flex-wrap gap-1">
                                {matchingDragTasks.map((task) => (
                                  <button
                                    key={task.id}
                                    type="button"
                                    className={`rounded border px-2 py-0.5 text-[10px] ${
                                      dragSelectedTaskId === task.id
                                        ? "border-primary bg-primary/15 text-primary"
                                        : "border-border bg-muted/40 text-muted-foreground"
                                    }`}
                                    onClick={() => {
                                      setDragSelectedTaskId(task.id);
                                      setDragTaskName(task.title);
                                    }}
                                    disabled={dragCreating}
                                  >
                                    {task.title}
                                  </button>
                                ))}
                              </div>
                            ) : (
                              <div className="text-[10px] text-muted-foreground">
                                一致する既存タスクは見つかりません
                              </div>
                            )}
                          </div>
                          <div className="flex gap-1 mt-1">
                            <button
                              className="flex-1 text-[10px] bg-primary text-primary-foreground rounded px-2 py-0.5 hover:bg-primary/90 disabled:opacity-50"
                              onClick={handleDragFormSubmit}
                              disabled={
                                dragCreating ||
                                (!dragSelectedTaskId &&
                                  !dragTaskName.trim())
                              }
                            >
                              {dragCreating ? "作成中..." : "作成"}
                            </button>
                            <button
                              className="flex-1 text-[10px] bg-muted text-muted-foreground rounded px-2 py-0.5 hover:bg-muted/80"
                              onClick={handleDragFormCancel}
                              disabled={dragCreating}
                            >
                              キャンセル
                            </button>
                          </div>
                        </div>
                      </div>
                    </>
                  )}

                  {/* タイムエントリ */}
                  {dayEntries.map((entry) => {
                    if (!entry.started_at) return null;
                    const range = getEntryHourRange(entry, now);
                    if (!range) return null;
                    let { startHour, endHour } = range;

                    // リサイズ中の見た目
                    if (
                      resizeState &&
                      resizeState.entryId === entry.id
                    ) {
                      if (resizeState.edge === "top") {
                        startHour = Math.min(
                          resizeState.currentHour,
                          resizeState.originalEndHour - 0.25,
                        );
                      } else {
                        endHour = Math.max(
                          resizeState.currentHour,
                          resizeState.originalStartHour + 0.25,
                        );
                      }
                    }

                    // 移動中の見た目（位置オーバーライド）
                    const isMovingThis =
                      moveState?.moving &&
                      moveState.entryId === entry.id &&
                      moveState.currentDayIndex === dayIndex;
                    if (isMovingThis && moveState) {
                      startHour = moveState.currentStartHour;
                      endHour = startHour + moveState.durationHours;
                    }

                    const clampedStart = Math.max(
                      startHour,
                      HOUR_START,
                    );
                    const clampedEnd = Math.min(endHour, HOUR_END);
                    if (
                      clampedEnd <= HOUR_START ||
                      clampedStart >= HOUR_END
                    )
                      return null;

                    const endPct =
                      ((clampedEnd - HOUR_START) / TOTAL_HOURS) * 100;
                    const heightPct =
                      ((clampedEnd - clampedStart) / TOTAL_HOURS) * 100;

                    const colorTokens = resolveProjectColorTokens(
                      entry.project_color,
                      resolvedTheme,
                      DEFAULT_ENTRY_COLOR,
                    )!;
                    const title =
                      entry.task_title || entry.note || "タスク";
                    const durSec = getEntryDurationSeconds(entry, now);
                    const durText = formatSeconds(durSec);
                    const hoverText = [
                      `プロジェクト: ${entry.project_name || "未設定"}`,
                      `タスク: ${title}`,
                      `時間: ${formatTimeWindow(entry)}`,
                      `経過: ${durText}`,
                      entry.original_started_at
                        ? "(編集済み — クリックで詳細)"
                        : "",
                    ]
                      .filter(Boolean)
                      .join("\n");
                    const isEdited = !!entry.original_started_at;
                    const isActive = !entry.ended_at;
                    const layout = entryLayouts.get(entry.id) ?? {
                      columnIndex: 0,
                      columnCount: 1,
                    };
                    const columnWidthPct = 100 / layout.columnCount;
                    const cursorCls = isActive
                      ? "cursor-pointer"
                      : isMovingThis
                        ? "cursor-grabbing"
                        : "cursor-grab";
                    return (
                      <div
                        key={entry.id}
                        data-entry
                        className={`absolute z-10 ${cursorCls} overflow-hidden rounded border text-foreground transition-all hover:brightness-[0.98] dark:hover:brightness-110 ${
                          isEdited ? "ring-1 ring-yellow-300/70" : ""
                        } ${isMovingThis ? "opacity-80 ring-2 ring-primary/60" : ""}`}
                        style={{
                          left: `calc(${layout.columnIndex * columnWidthPct}% + 2px)`,
                          width: `calc(${columnWidthPct}% - 4px)`,
                          top: `${endPct}%`,
                          height: `${heightPct}%`,
                          minHeight: isActive ? "2px" : "18px",
                          transform: "translateY(-100%)",
                          ...timelineBlockStyle(colorTokens),
                        }}
                        title={hoverText}
                        onMouseDown={(e) => {
                          if (isActive) return;
                          handleEntryMouseDown(e, entry, dayIndex);
                        }}
                        onClick={(e) => {
                          if (isActive) {
                            e.stopPropagation();
                            openEditDialog(entry);
                          }
                        }}
                        onContextMenu={(e) =>
                          handleEntryContextMenu(e, entry)
                        }
                      >
                        {/* 上端リサイズハンドル */}
                        {!isActive && (
                          <div
                            data-resize-handle
                            className="absolute left-0 right-0 top-0 h-1.5 cursor-ns-resize hover:bg-white/40 z-10"
                            onMouseDown={(e) =>
                              handleResizeMouseDown(
                                e,
                                entry,
                                "top",
                                dayIndex,
                              )
                            }
                          />
                        )}

                        <div className="px-1.5 py-1 pointer-events-none">
                          <div className="text-[13px] leading-tight font-medium truncate">
                            {title}
                          </div>
                          {entry.project_name && heightPct > 4.5 && (
                            <div
                              className="text-[11px] leading-tight truncate"
                              style={{ color: colorTokens.mutedText }}
                            >
                              {entry.project_name}
                            </div>
                          )}
                          {heightPct > 3 && (
                            <div
                              className="text-[11px] leading-tight truncate"
                              style={{ color: colorTokens.mutedText }}
                            >
                              {durText}
                            </div>
                          )}
                        </div>

                        {/* 下端リサイズハンドル */}
                        {!isActive && (
                          <div
                            data-resize-handle
                            className="absolute left-0 right-0 bottom-0 h-1.5 cursor-ns-resize hover:bg-white/40 z-10"
                            onMouseDown={(e) =>
                              handleResizeMouseDown(
                                e,
                                entry,
                                "bottom",
                                dayIndex,
                              )
                            }
                          />
                        )}
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

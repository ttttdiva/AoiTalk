"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { taskApi, type Task, type TimeEntry } from "@/lib/task-api";
import { useContextMenuPosition } from "@/hooks/use-context-menu-position";
import { formatLocalDateTime } from "@/lib/date-time";
import {
  HOUR_START,
  HOUR_END,
  TOTAL_HOURS,
  type ResizeState,
  type MoveState,
  type CtxMenuState,
} from "./reports-utils";

type ActivePointer = {
  pointerId: number;
  target: HTMLElement;
};

function capturePointer(
  event: React.PointerEvent<HTMLElement>,
  activePointerRef: React.MutableRefObject<ActivePointer | null>,
) {
  activePointerRef.current = {
    pointerId: event.pointerId,
    target: event.currentTarget,
  };
  try {
    event.currentTarget.setPointerCapture?.(event.pointerId);
  } catch {
    // The pointer may already have been cancelled by the browser.
  }
}

function releasePointer(
  activePointerRef: React.MutableRefObject<ActivePointer | null>,
) {
  const activePointer = activePointerRef.current;
  activePointerRef.current = null;
  if (!activePointer) return;
  try {
    if (
      !activePointer.target.hasPointerCapture ||
      activePointer.target.hasPointerCapture(activePointer.pointerId)
    ) {
      activePointer.target.releasePointerCapture?.(activePointer.pointerId);
    }
  } catch {
    // The element or pointer can disappear while an interaction is cancelled.
  }
}

export function useReportsTimeline({
  remoteReadOnly,
  createReadOnly,
  isEntryReadOnly,
  selectedProjectId,
  weekDays,
  timeEntries,
  fetchReport,
  openEditDialog,
  setSelectedEntry,
  setSelectedTaskId,
  editingEntry,
  period,
  setWeekOffset,
}: {
  remoteReadOnly: boolean;
  createReadOnly: boolean;
  isEntryReadOnly: (entry: TimeEntry) => boolean;
  selectedProjectId: string | null;
  weekDays: Date[];
  timeEntries: TimeEntry[];
  fetchReport: () => void;
  openEditDialog: (entry: TimeEntry) => void;
  setSelectedEntry: (entry: TimeEntry | null) => void;
  setSelectedTaskId: (taskId: string | null) => void;
  editingEntry: TimeEntry | null;
  period: string;
  setWeekOffset: React.Dispatch<React.SetStateAction<number>>;
}) {
  // 新規作成ドラッグ
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
  const [dragState, setDragState] = useState<DragState | null>(null);
  const [dragForm, setDragForm] = useState<DragForm | null>(null);
  const [dragTaskName, setDragTaskName] = useState("");
  const [dragTaskOptions, setDragTaskOptions] = useState<Task[]>([]);
  const [dragSelectedTaskId, setDragSelectedTaskId] = useState<string | null>(
    null,
  );
  const [dragTaskLoading, setDragTaskLoading] = useState(false);
  const [dragCreating, setDragCreating] = useState(false);
  const dragFormInputRef = useRef<HTMLInputElement>(null);
  const isDraggingRef = useRef(false);
  const activeDragPointerRef = useRef<ActivePointer | null>(null);

  // リサイズD&D
  const [resizeState, setResizeState] = useState<ResizeState | null>(null);
  const isResizingRef = useRef(false);
  const dayColRefs = useRef<Array<HTMLDivElement | null>>([]);
  const activeResizePointerRef = useRef<ActivePointer | null>(null);

  // 移動D&D
  const [moveState, setMoveState] = useState<MoveState | null>(null);
  const moveStateRef = useRef<MoveState | null>(null);
  const isMovingRef = useRef(false);
  const activeMovePointerRef = useRef<ActivePointer | null>(null);

  // 右クリックコンテキストメニュー
  const [ctxMenu, setCtxMenu] = useState<CtxMenuState | null>(null);
  const { ref: ctxMenuRef, style: ctxMenuStyle } = useContextMenuPosition(
    ctxMenu ? { x: ctxMenu.x, y: ctxMenu.y } : null,
    { fallbackWidth: 180, fallbackHeight: 150 },
  );

  useEffect(() => {
    if (!remoteReadOnly) return;
    releasePointer(activeDragPointerRef);
    releasePointer(activeResizePointerRef);
    releasePointer(activeMovePointerRef);
    isDraggingRef.current = false;
    isResizingRef.current = false;
    isMovingRef.current = false;
    moveStateRef.current = null;
    setDragState(null);
    setDragForm(null);
    setDragTaskName("");
    setDragSelectedTaskId(null);
    setResizeState(null);
    setMoveState(null);
    setCtxMenu(null);
  }, [remoteReadOnly]);

  useEffect(
    () => () => {
      releasePointer(activeDragPointerRef);
      releasePointer(activeResizePointerRef);
      releasePointer(activeMovePointerRef);
    },
    [],
  );

  useEffect(() => {
    if (!createReadOnly || remoteReadOnly) return;
    releasePointer(activeDragPointerRef);
    isDraggingRef.current = false;
    setDragState(null);
    setDragForm(null);
    setDragTaskName("");
    setDragSelectedTaskId(null);
  }, [createReadOnly, remoteReadOnly]);

  useEffect(() => {
    if (!dragForm || !selectedProjectId) {
      setDragTaskOptions([]);
      setDragSelectedTaskId(null);
      return;
    }

    let cancelled = false;
    setDragTaskLoading(true);
    void taskApi
      .listTasks(selectedProjectId)
      .then((list) => {
        if (cancelled) return;
        setDragTaskOptions(list.filter((task) => !task.parent_task_id));
      })
      .catch((err) => {
        if (!cancelled) {
          console.error("既存タスク取得失敗", err);
          setDragTaskOptions([]);
        }
      })
      .finally(() => {
        if (!cancelled) setDragTaskLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [dragForm, selectedProjectId]);

  // 左右キーで週移動
  useEffect(() => {
    if (period !== "this_week") return;
    const handleKeyDown = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (dragForm || editingEntry || resizeState || moveState || ctxMenu)
        return;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        setWeekOffset((o) => o - 1);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        setWeekOffset((o) => Math.min(o + 1, 0));
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    period,
    dragForm,
    editingEntry,
    resizeState,
    moveState,
    ctxMenu,
    setWeekOffset,
  ]);

  const matchingDragTasks = useMemo(() => {
    const keyword = dragTaskName.trim().toLowerCase();
    const candidates = dragTaskOptions.filter((task) => !task.parent_task_id);
    if (!keyword) return candidates.slice(0, 6);
    return candidates
      .filter((task) => task.title.toLowerCase().includes(keyword))
      .slice(0, 6);
  }, [dragTaskName, dragTaskOptions]);

  const selectedDragTask = useMemo(
    () =>
      dragTaskOptions.find((task) => task.id === dragSelectedTaskId) || null,
    [dragSelectedTaskId, dragTaskOptions],
  );

  // マウスY座標 → 時間(15分単位)
  const calcHourFromMouseY = useCallback(
    (clientY: number, columnEl: HTMLElement): number => {
      const rect = columnEl.getBoundingClientRect();
      const y = clientY - rect.top;
      const pct = Math.max(0, Math.min(1, y / rect.height));
      const rawHour = HOUR_START + pct * TOTAL_HOURS;
      return Math.round(rawHour * 4) / 4;
    },
    [],
  );

  const handleDragMouseDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>, dayIndex: number) => {
      if (remoteReadOnly || createReadOnly) return;
      if (e.button !== 0 || e.isPrimary === false) return;
      if ((e.target as HTMLElement).closest("[data-entry]")) return;
      if ((e.target as HTMLElement).closest("[data-drag-form]")) return;
      e.preventDefault();
      capturePointer(e, activeDragPointerRef);
      const hour = calcHourFromMouseY(
        e.clientY,
        e.currentTarget as HTMLDivElement,
      );
      isDraggingRef.current = true;
      setDragState({ dayIndex, startHour: hour, currentHour: hour });
      setDragForm(null);
    },
    [calcHourFromMouseY, createReadOnly, remoteReadOnly],
  );

  const handleDragMouseMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>, dayIndex: number) => {
      const activePointer = activeDragPointerRef.current;
      if (
        remoteReadOnly ||
        createReadOnly ||
        !activePointer ||
        activePointer.pointerId !== e.pointerId ||
        !isDraggingRef.current ||
        !dragState ||
        dragState.dayIndex !== dayIndex
      )
        return;
      const hour = calcHourFromMouseY(
        e.clientY,
        e.currentTarget as HTMLDivElement,
      );
      setDragState((prev) => (prev ? { ...prev, currentHour: hour } : null));
    },
    [calcHourFromMouseY, createReadOnly, dragState, remoteReadOnly],
  );

  const handleDragMouseUp = useCallback((e?: React.PointerEvent<HTMLDivElement>) => {
    const activePointer = activeDragPointerRef.current;
    if (e && activePointer && activePointer.pointerId !== e.pointerId) return;
    const cancelled = e?.type === "pointercancel";
    releasePointer(activeDragPointerRef);
    if (remoteReadOnly || createReadOnly) {
      isDraggingRef.current = false;
      setDragState(null);
      return;
    }
    if (!isDraggingRef.current || !dragState) return;
    isDraggingRef.current = false;
    if (cancelled) {
      setDragState(null);
      return;
    }

    const startH = Math.min(dragState.startHour, dragState.currentHour);
    const endH = Math.max(dragState.startHour, dragState.currentHour);

    if (endH - startH < 0.25) {
      setDragState(null);
      return;
    }

    const topPct = ((startH - HOUR_START) / TOTAL_HOURS) * 100;

    setDragForm({
      dayIndex: dragState.dayIndex,
      startHour: startH,
      endHour: endH,
      topPct,
    });
    setDragState(null);
    setDragTaskName("");
    setDragSelectedTaskId(null);
    setTimeout(() => dragFormInputRef.current?.focus(), 50);
  }, [createReadOnly, dragState, remoteReadOnly]);

  const handleDragFormSubmit = useCallback(async () => {
    if (!dragForm) return;
    if (remoteReadOnly || createReadOnly) return;
    const targetProjectId = selectedProjectId;
    if (!targetProjectId) {
      alert("プロジェクトを選択してください。");
      return;
    }
    setDragCreating(true);
    try {
      const trimmedName = dragTaskName.trim();
      let taskId: string;

      if (dragSelectedTaskId) {
        taskId = dragSelectedTaskId;
      } else {
        if (!trimmedName) return;
        const newTask = await taskApi.createTask({
          project_id: targetProjectId,
          title: trimmedName,
          status: "open",
          priority: "normal",
        });
        taskId = newTask.id;
      }

      const day = weekDays[dragForm.dayIndex];
      const startDate = new Date(day);
      const startHourInt = Math.floor(dragForm.startHour);
      const startMin = Math.round((dragForm.startHour - startHourInt) * 60);
      startDate.setHours(startHourInt, startMin, 0, 0);

      const endDate = new Date(day);
      const endHourInt = Math.floor(dragForm.endHour);
      const endMin = Math.round((dragForm.endHour - endHourInt) * 60);
      endDate.setHours(endHourInt, endMin, 0, 0);

      await taskApi.createTimeEntry({
        task_id: taskId,
        started_at: formatLocalDateTime(startDate),
        ended_at: formatLocalDateTime(endDate),
      });

      setDragForm(null);
      setDragTaskName("");
      setDragSelectedTaskId(null);
      fetchReport();
    } catch (err) {
      console.error("タイムエントリ作成失敗:", err);
    } finally {
      setDragCreating(false);
    }
  }, [
    dragForm,
    dragSelectedTaskId,
    dragTaskName,
    selectedProjectId,
    weekDays,
    fetchReport,
    createReadOnly,
    remoteReadOnly,
  ]);

  const handleDragFormCancel = useCallback(() => {
    setDragForm(null);
    setDragTaskName("");
    setDragSelectedTaskId(null);
  }, []);

  // --- リサイズD&D ---
  const handleResizeMouseDown = useCallback(
    (
      e: React.PointerEvent<HTMLDivElement>,
      entry: TimeEntry,
      edge: "top" | "bottom",
      dayIndex: number,
    ) => {
      e.stopPropagation();
      e.preventDefault();
      if (e.button !== 0 || e.isPrimary === false) return;
      if (!entry.started_at || !entry.ended_at) return;
      if (remoteReadOnly || isEntryReadOnly(entry)) return;
      capturePointer(e, activeResizePointerRef);
      const start = new Date(entry.started_at);
      const end = new Date(entry.ended_at);
      const startHour = start.getHours() + start.getMinutes() / 60;
      const endHour = end.getHours() + end.getMinutes() / 60;
      isResizingRef.current = true;
      setResizeState({
        entryId: entry.id,
        edge,
        dayIndex,
        originalStartHour: startHour,
        originalEndHour: endHour,
        currentHour: edge === "top" ? startHour : endHour,
      });
    },
    [isEntryReadOnly, remoteReadOnly],
  );

  useEffect(() => {
    if (!resizeState) return;
    if (remoteReadOnly) {
      setResizeState(null);
      return;
    }
    const handleMove = (e: PointerEvent) => {
      if (activeResizePointerRef.current?.pointerId !== e.pointerId) return;
      const col = dayColRefs.current[resizeState.dayIndex];
      if (!col) return;
      const hour = calcHourFromMouseY(e.clientY, col);
      setResizeState((prev) => (prev ? { ...prev, currentHour: hour } : null));
    };
    const handleUp = async (e: PointerEvent) => {
      if (activeResizePointerRef.current?.pointerId !== e.pointerId) return;
      const cancelled = e.type === "pointercancel";
      releasePointer(activeResizePointerRef);
      const state = resizeState;
      if (!state) return;
      isResizingRef.current = false;
      if (cancelled) {
        setResizeState(null);
        return;
      }

      const entry = timeEntries.find((x) => x.id === state.entryId);
      if (!entry || !entry.started_at || !entry.ended_at) {
        setResizeState(null);
        return;
      }
      if (isEntryReadOnly(entry)) {
        setResizeState(null);
        return;
      }

      let newStartHour = state.originalStartHour;
      let newEndHour = state.originalEndHour;
      if (state.edge === "top") {
        newStartHour = Math.min(
          state.currentHour,
          state.originalEndHour - 0.25,
        );
      } else {
        newEndHour = Math.max(
          state.currentHour,
          state.originalStartHour + 0.25,
        );
      }

      setResizeState(null);

      // 変化なしならAPIを呼ばない
      if (
        (state.edge === "top" && newStartHour === state.originalStartHour) ||
        (state.edge === "bottom" && newEndHour === state.originalEndHour)
      ) {
        openEditDialog(entry);
        return;
      }

      const baseDate = new Date(
        state.edge === "top" ? entry.started_at : entry.ended_at,
      );
      baseDate.setHours(0, 0, 0, 0);

      const applyHour = (d: Date, h: number) => {
        const intH = Math.floor(h);
        const m = Math.round((h - intH) * 60);
        d.setHours(intH, m, 0, 0);
        return d;
      };

      const payload: { started_at?: string; ended_at?: string } = {};
      if (state.edge === "top") {
        const d = new Date(baseDate);
        applyHour(d, newStartHour);
        payload.started_at = formatLocalDateTime(d);
      } else {
        const d = new Date(baseDate);
        applyHour(d, newEndHour);
        payload.ended_at = formatLocalDateTime(d);
      }

      try {
        await taskApi.updateTimeEntry(entry.id, payload);
        fetchReport();
      } catch (err) {
        console.error("タイムエントリ更新失敗:", err);
      }
    };
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);
    window.addEventListener("pointercancel", handleUp);
    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      window.removeEventListener("pointercancel", handleUp);
    };
  }, [
    resizeState,
    timeEntries,
    calcHourFromMouseY,
    fetchReport,
    isEntryReadOnly,
    openEditDialog,
    remoteReadOnly,
  ]);

  // --- 移動D&D ---
  const handleEntryMouseDown = useCallback(
    (
      e: React.PointerEvent<HTMLDivElement>,
      entry: TimeEntry,
      dayIndex: number,
    ) => {
      if (e.button !== 0 || e.isPrimary === false) return;
      if ((e.target as HTMLElement).closest("[data-resize-handle]")) return;
      if (!entry.started_at || !entry.ended_at) return;
      if (remoteReadOnly || isEntryReadOnly(entry)) return;
      e.stopPropagation();
      e.preventDefault();
      e.currentTarget.focus({ preventScroll: true });
      capturePointer(e, activeMovePointerRef);
      const start = new Date(entry.started_at);
      const end = new Date(entry.ended_at);
      const startHour = start.getHours() + start.getMinutes() / 60;
      const endHour = end.getHours() + end.getMinutes() / 60;
      const durationHours = endHour - startHour;
      const col = dayColRefs.current[dayIndex];
      const cursorHour = col ? calcHourFromMouseY(e.clientY, col) : startHour;
      isMovingRef.current = true;
      const initial: MoveState = {
        entryId: entry.id,
        originalStartedAt: entry.started_at,
        originalEndedAt: entry.ended_at,
        originalDayIndex: dayIndex,
        originalStartHour: startHour,
        durationHours,
        pointerOffsetHours: cursorHour - startHour,
        mouseStartX: e.clientX,
        mouseStartY: e.clientY,
        currentDayIndex: dayIndex,
        currentStartHour: startHour,
        moving: false,
      };
      moveStateRef.current = initial;
      setMoveState(initial);
    },
    [calcHourFromMouseY, isEntryReadOnly, remoteReadOnly],
  );

  const moveActive = !!moveState;
  useEffect(() => {
    if (!moveActive) return;
    const THRESHOLD_PX = 4;

    const handleMove = (e: PointerEvent) => {
      if (activeMovePointerRef.current?.pointerId !== e.pointerId) return;
      const prev = moveStateRef.current;
      if (!prev) return;
      const next = { ...prev };
      if (!next.moving) {
        const dx = e.clientX - next.mouseStartX;
        const dy = e.clientY - next.mouseStartY;
        if (Math.hypot(dx, dy) < THRESHOLD_PX) return;
        next.moving = true;
      }
      let targetDay = next.currentDayIndex;
      for (let i = 0; i < dayColRefs.current.length; i++) {
        const c = dayColRefs.current[i];
        if (!c) continue;
        const r = c.getBoundingClientRect();
        if (e.clientX >= r.left && e.clientX <= r.right) {
          targetDay = i;
          break;
        }
      }
      const col = dayColRefs.current[targetDay];
      if (!col) {
        moveStateRef.current = next;
        setMoveState(next);
        return;
      }
      const cursorHour = calcHourFromMouseY(e.clientY, col);
      let newStart = cursorHour - next.pointerOffsetHours;
      newStart = Math.max(
        HOUR_START,
        Math.min(HOUR_END - next.durationHours, newStart),
      );
      newStart = Math.round(newStart * 4) / 4;
      next.currentDayIndex = targetDay;
      next.currentStartHour = newStart;
      moveStateRef.current = next;
      setMoveState(next);
    };

    const handleUp = async (e: PointerEvent) => {
      if (activeMovePointerRef.current?.pointerId !== e.pointerId) return;
      const cancelled = e.type === "pointercancel";
      releasePointer(activeMovePointerRef);
      const state = moveStateRef.current;
      moveStateRef.current = null;
      isMovingRef.current = false;
      setMoveState(null);
      if (!state) return;
      if (cancelled) return;

      const entry = timeEntries.find((x) => x.id === state.entryId);
      if (!entry) return;
      if (remoteReadOnly || isEntryReadOnly(entry)) return;

      if (!state.moving) {
        // 実質クリック: 記録編集を開く
        openEditDialog(entry);
        return;
      }

      // 変化がなければ何もしない
      if (
        state.currentDayIndex === state.originalDayIndex &&
        state.currentStartHour === state.originalStartHour
      ) {
        return;
      }

      const day = weekDays[state.currentDayIndex];
      if (!day) return;
      const sH = Math.floor(state.currentStartHour);
      const sM = Math.round((state.currentStartHour - sH) * 60);
      const newStart = new Date(day);
      newStart.setHours(sH, sM, 0, 0);
      const newEnd = new Date(
        newStart.getTime() + state.durationHours * 3600 * 1000,
      );

      try {
        await taskApi.updateTimeEntry(entry.id, {
          started_at: formatLocalDateTime(newStart),
          ended_at: formatLocalDateTime(newEnd),
        });
        fetchReport();
      } catch (err) {
        console.error("タイムエントリ移動失敗:", err);
      }
    };

    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);
    window.addEventListener("pointercancel", handleUp);
    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      window.removeEventListener("pointercancel", handleUp);
    };
  }, [
    moveActive,
    timeEntries,
    weekDays,
    calcHourFromMouseY,
    openEditDialog,
    fetchReport,
    isEntryReadOnly,
    remoteReadOnly,
  ]);

  // --- 右クリックメニュー ---
  const handleEntryContextMenu = useCallback(
    (e: React.MouseEvent<HTMLDivElement>, entry: TimeEntry) => {
      e.preventDefault();
      e.stopPropagation();
      if (remoteReadOnly || isEntryReadOnly(entry)) return;
      setCtxMenu({ entry, x: e.clientX, y: e.clientY });
    },
    [isEntryReadOnly, remoteReadOnly],
  );

  const handleCtxOpenDetail = useCallback(() => {
    if (!ctxMenu) return;
    if (remoteReadOnly || isEntryReadOnly(ctxMenu.entry)) {
      setCtxMenu(null);
      return;
    }
    setSelectedEntry(ctxMenu.entry);
    setSelectedTaskId(ctxMenu.entry.task_id);
    setCtxMenu(null);
  }, [ctxMenu, isEntryReadOnly, remoteReadOnly, setSelectedEntry, setSelectedTaskId]);

  const handleCtxEdit = useCallback(() => {
    if (!ctxMenu) return;
    const entry = ctxMenu.entry;
    setCtxMenu(null);
    if (entry.ended_at) openEditDialog(entry);
  }, [ctxMenu, openEditDialog]);

  const handleCtxDuplicate = useCallback(async () => {
    if (!ctxMenu) return;
    if (remoteReadOnly || isEntryReadOnly(ctxMenu.entry)) return;
    const entry = ctxMenu.entry;
    setCtxMenu(null);
    if (!entry.started_at || !entry.ended_at) {
      alert("計測中のエントリは複製できません");
      return;
    }
    try {
      await taskApi.createTimeEntry({
        task_id: entry.task_id,
        started_at: entry.started_at,
        ended_at: entry.ended_at,
        note: entry.note || undefined,
      });
      fetchReport();
    } catch (err) {
      console.error("タイムエントリ複製失敗:", err);
      alert("複製に失敗しました");
    }
  }, [ctxMenu, fetchReport, isEntryReadOnly, remoteReadOnly]);

  const handleCtxDelete = useCallback(async () => {
    if (!ctxMenu) return;
    if (remoteReadOnly || isEntryReadOnly(ctxMenu.entry)) return;
    const entry = ctxMenu.entry;
    setCtxMenu(null);
    try {
      await taskApi.deleteTimeEntry(entry.id);
      fetchReport();
    } catch (err) {
      console.error("タイムエントリ削除失敗:", err);
      alert("削除に失敗しました");
    }
  }, [ctxMenu, fetchReport, isEntryReadOnly, remoteReadOnly]);

  // メニュー外クリック / Esc で閉じる
  useEffect(() => {
    if (!ctxMenu) return;
    const onDown = (e: MouseEvent) => {
      if (!(e.target as HTMLElement | null)?.closest("[data-ctx-menu]")) {
        setCtxMenu(null);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setCtxMenu(null);
    };
    const onScroll = () => setCtxMenu(null);
    // contextmenu イベント発火直後の同じ mousedown で閉じないよう次tick
    const t = window.setTimeout(() => {
      window.addEventListener("mousedown", onDown);
      window.addEventListener("keydown", onKey);
      window.addEventListener("scroll", onScroll, true);
    }, 0);
    return () => {
      window.clearTimeout(t);
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [ctxMenu]);

  return {
    dragState,
    dragForm,
    dragTaskName,
    setDragTaskName,
    dragTaskOptions,
    dragSelectedTaskId,
    setDragSelectedTaskId,
    dragTaskLoading,
    dragCreating,
    dragFormInputRef,
    isDraggingRef,
    resizeState,
    isResizingRef,
    dayColRefs,
    moveState,
    isMovingRef,
    ctxMenu,
    ctxMenuRef,
    ctxMenuStyle,
    matchingDragTasks,
    selectedDragTask,
    calcHourFromMouseY,
    handleDragMouseDown,
    handleDragMouseMove,
    handleDragMouseUp,
    handleDragFormSubmit,
    handleDragFormCancel,
    handleResizeMouseDown,
    handleEntryMouseDown,
    handleEntryContextMenu,
    handleCtxOpenDetail,
    handleCtxEdit,
    handleCtxDuplicate,
    handleCtxDelete,
  };
}

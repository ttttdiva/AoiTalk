"use client";

import {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import {
  taskApi,
  type Project,
  type Space,
  type TimeEntry,
} from "@/lib/task-api";
import { formatLocalDateTime } from "@/lib/date-time";
import {
  toLocalHM,
  toLocalYMD,
  parseTimeInput,
  parseDurationInput,
  formatDurationInput,
  combineDateTime,
} from "./reports-utils";

export function useReportsEditEntry({
  remoteReadOnly,
  allProjects,
  spaces,
  fetchReport,
  setSelectedEntry,
  setSelectedTaskId,
}: {
  remoteReadOnly: boolean;
  allProjects: Project[];
  spaces: Space[];
  fetchReport: () => void;
  setSelectedEntry: (entry: TimeEntry | null) => void;
  setSelectedTaskId: (taskId: string | null) => void;
}) {
  // 既存エントリ編集ダイアログ
  const [editingEntry, setEditingEntry] = useState<TimeEntry | null>(null);
  const [editDate, setEditDate] = useState("");
  const [editStart, setEditStart] = useState("");
  const [editEnd, setEditEnd] = useState("");
  const [editDuration, setEditDuration] = useState("0:00:00");
  const [editNote, setEditNote] = useState("");
  const [editSaving, setEditSaving] = useState(false);

  const currentEditingProject = useMemo(
    () =>
      editingEntry?.project_id
        ? (allProjects.find(
            (project) => project.id === editingEntry.project_id,
          ) ?? null)
        : null,
    [allProjects, editingEntry?.project_id],
  );

  const currentEditingSpace = useMemo(() => {
    const spaceId = currentEditingProject?.space_id ?? editingEntry?.space_id;
    return spaceId
      ? (spaces.find((space) => space.id === spaceId) ?? null)
      : null;
  }, [currentEditingProject?.space_id, editingEntry?.space_id, spaces]);

  const projectsForEditingSpace = useMemo(() => {
    if (currentEditingSpace?.id) {
      return allProjects.filter(
        (project) => project.space_id === currentEditingSpace.id,
      );
    }
    if (editingEntry?.space_id) {
      return allProjects.filter(
        (project) => project.space_id === editingEntry.space_id,
      );
    }
    return allProjects.filter((project) => !project.space_id);
  }, [allProjects, currentEditingSpace?.id, editingEntry?.space_id]);

  // --- エントリクリック編集 ---
  const openEditDialog = useCallback((entry: TimeEntry) => {
    if (remoteReadOnly) return;
    if (!entry.started_at) return;
    const start = new Date(entry.started_at);
    const end = entry.ended_at ? new Date(entry.ended_at) : new Date();
    const durationSec = Math.max(
      0,
      Math.floor((end.getTime() - start.getTime()) / 1000),
    );
    setEditingEntry(entry);
    setEditDate(toLocalYMD(start));
    setEditStart(toLocalHM(start));
    setEditEnd(toLocalHM(end));
    setEditDuration(formatDurationInput(durationSec));
    setEditNote(entry.note || "");
  }, [remoteReadOnly]);

  const closeEditDialog = useCallback(() => {
    setEditingEntry(null);
    setEditSaving(false);
  }, []);

  const isEditingRunning = !!editingEntry && !editingEntry.ended_at;

  const handleEditStartBlur = useCallback(() => {
    const parsed = parseTimeInput(editStart);
    if (!parsed) {
      // 不正値は元の状態へ戻す
      if (editingEntry?.started_at) {
        setEditStart(toLocalHM(new Date(editingEntry.started_at)));
      }
      return;
    }
    setEditStart(parsed);
    const startDt = combineDateTime(editDate, parsed);
    const endDt = combineDateTime(editDate, editEnd);
    const diffSec = Math.floor((endDt.getTime() - startDt.getTime()) / 1000);
    if (diffSec >= 0) setEditDuration(formatDurationInput(diffSec));
  }, [editStart, editDate, editEnd, editingEntry]);

  const handleEditEndBlur = useCallback(() => {
    const parsed = parseTimeInput(editEnd);
    if (!parsed) {
      if (editingEntry?.ended_at) {
        setEditEnd(toLocalHM(new Date(editingEntry.ended_at)));
      }
      return;
    }
    setEditEnd(parsed);
    const startDt = combineDateTime(editDate, editStart);
    const endDt = combineDateTime(editDate, parsed);
    const diffSec = Math.floor((endDt.getTime() - startDt.getTime()) / 1000);
    if (diffSec >= 0) setEditDuration(formatDurationInput(diffSec));
  }, [editEnd, editDate, editStart, editingEntry]);

  const handleEditDurationBlur = useCallback(() => {
    const sec = parseDurationInput(editDuration);
    if (sec === null || sec < 0) {
      // 復元
      const startDt = combineDateTime(editDate, editStart);
      const endDt = combineDateTime(editDate, editEnd);
      const diffSec = Math.floor((endDt.getTime() - startDt.getTime()) / 1000);
      setEditDuration(formatDurationInput(Math.max(0, diffSec)));
      return;
    }
    setEditDuration(formatDurationInput(sec));
    if (isEditingRunning) {
      // 計測中: 開始時刻をずらす（end は現在時刻として扱う）
      const endDt = combineDateTime(editDate, editEnd);
      const newStart = new Date(endDt.getTime() - sec * 1000);
      setEditDate(toLocalYMD(newStart));
      setEditStart(toLocalHM(newStart));
    } else {
      // 停止中: 終了時刻を伸ばす
      const startDt = combineDateTime(editDate, editStart);
      const newEnd = new Date(startDt.getTime() + sec * 1000);
      setEditEnd(toLocalHM(newEnd));
    }
  }, [editDuration, editDate, editStart, editEnd, isEditingRunning]);

  const saveEditEntry = useCallback(
    async (keepOpen: boolean) => {
      if (!editingEntry) return;
      if (remoteReadOnly) return;
      if (!editDate || !editStart) return;
      const newStart = combineDateTime(editDate, editStart);
      setEditSaving(true);
      try {
        if (isEditingRunning) {
          await taskApi.updateTimeEntry(editingEntry.id, {
            started_at: formatLocalDateTime(newStart),
            note: editNote,
          });
        } else {
          if (!editEnd) return;
          const newEnd = combineDateTime(editDate, editEnd);
          if (newEnd <= newStart) {
            alert("終了時刻は開始時刻より後にしてください");
            setEditSaving(false);
            return;
          }
          await taskApi.updateTimeEntry(editingEntry.id, {
            started_at: formatLocalDateTime(newStart),
            ended_at: formatLocalDateTime(newEnd),
            note: editNote,
          });
        }
        if (!keepOpen) closeEditDialog();
        fetchReport();
      } catch (err) {
        console.error("タイムエントリ更新失敗:", err);
        alert("更新に失敗しました");
      } finally {
        setEditSaving(false);
      }
    },
    [
      editingEntry,
      editDate,
      editStart,
      editEnd,
      editNote,
      isEditingRunning,
      closeEditDialog,
      fetchReport,
      remoteReadOnly,
    ],
  );

  const handleEditSave = useCallback(
    () => saveEditEntry(false),
    [saveEditEntry],
  );

  const saveEditEntryRef = useRef(saveEditEntry);
  useEffect(() => {
    saveEditEntryRef.current = saveEditEntry;
  }, [saveEditEntry]);

  const handleEditInputEnter = useCallback(
    (e: ReactKeyboardEvent<HTMLInputElement>) => {
      if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
      e.preventDefault();
      e.currentTarget.blur();
      // blur → onBlur で state が正規化された後に保存するため次tickへ
      setTimeout(() => {
        void saveEditEntryRef.current(true);
      }, 0);
    },
    [],
  );

  const handleEditDelete = useCallback(async () => {
    if (!editingEntry) return;
    if (remoteReadOnly) return;
    setEditSaving(true);
    try {
      await taskApi.deleteTimeEntry(editingEntry.id);
      closeEditDialog();
      fetchReport();
    } catch (err) {
      console.error("タイムエントリ削除失敗:", err);
      alert("削除に失敗しました");
    } finally {
      setEditSaving(false);
    }
  }, [editingEntry, closeEditDialog, fetchReport, remoteReadOnly]);

  const handleEditDuplicate = useCallback(async () => {
    if (!editingEntry) return;
    if (remoteReadOnly) return;
    if (!editingEntry.started_at || !editingEntry.ended_at) {
      alert("計測中のエントリは複製できません");
      return;
    }
    setEditSaving(true);
    try {
      await taskApi.createTimeEntry({
        task_id: editingEntry.task_id,
        started_at: editingEntry.started_at,
        ended_at: editingEntry.ended_at,
        note: editingEntry.note || undefined,
      });
      closeEditDialog();
      fetchReport();
    } catch (err) {
      console.error("タイムエントリ複製失敗:", err);
      alert("複製に失敗しました");
    } finally {
      setEditSaving(false);
    }
  }, [editingEntry, closeEditDialog, fetchReport, remoteReadOnly]);

  const handleEditRestartTimer = useCallback(async () => {
    if (!editingEntry) return;
    if (remoteReadOnly) return;
    setEditSaving(true);
    try {
      const started = await taskApi.startTimer(editingEntry.task_id);
      closeEditDialog();
      fetchReport();
      window.dispatchEvent(
        new CustomEvent("timer-changed", {
          detail: { activeEntry: started },
        }),
      );
      window.dispatchEvent(new Event("task-list-refresh"));
    } catch (err) {
      console.error("タイマー開始失敗:", err);
      alert("タイマー開始に失敗しました");
    } finally {
      setEditSaving(false);
    }
  }, [editingEntry, closeEditDialog, fetchReport, remoteReadOnly]);

  const handleEditStopTimer = useCallback(async () => {
    if (!editingEntry) return;
    if (remoteReadOnly) return;
    setEditSaving(true);
    try {
      await taskApi.stopTimer(editingEntry.id);
      closeEditDialog();
      fetchReport();
      window.dispatchEvent(
        new CustomEvent("timer-changed", {
          detail: { activeEntry: null },
        }),
      );
      window.dispatchEvent(new Event("task-list-refresh"));
    } catch (err) {
      console.error("タイマー停止失敗:", err);
      alert("タイマー停止に失敗しました");
    } finally {
      setEditSaving(false);
    }
  }, [editingEntry, closeEditDialog, fetchReport, remoteReadOnly]);

  const handleEditRevertToOriginal = useCallback(() => {
    if (!editingEntry?.original_started_at || !editingEntry?.original_ended_at)
      return;
    const origStart = new Date(editingEntry.original_started_at);
    const origEnd = new Date(editingEntry.original_ended_at);
    const durationSec = Math.max(
      0,
      Math.floor((origEnd.getTime() - origStart.getTime()) / 1000),
    );
    setEditDate(toLocalYMD(origStart));
    setEditStart(toLocalHM(origStart));
    setEditEnd(toLocalHM(origEnd));
    setEditDuration(formatDurationInput(durationSec));
  }, [editingEntry]);

  const handleOpenTaskDetail = useCallback(() => {
    if (!editingEntry) return;
    setSelectedEntry(editingEntry);
    setSelectedTaskId(editingEntry.task_id);
    setEditingEntry(null);
  }, [editingEntry, setSelectedEntry, setSelectedTaskId]);

  const handleEditMoveTaskProject = useCallback(
    async (projectId: string) => {
      if (remoteReadOnly) return;
      if (
        !editingEntry ||
        !projectId ||
        projectId === editingEntry.project_id
      ) {
        return;
      }
      setEditSaving(true);
      try {
        await taskApi.moveTask(editingEntry.task_id, {
          project_id: projectId,
        });
        const nextProject =
          allProjects.find((project) => project.id === projectId) ?? null;
        const nextSpace = nextProject?.space_id
          ? (spaces.find((space) => space.id === nextProject.space_id) ?? null)
          : null;
        setEditingEntry((prev) =>
          prev
            ? {
                ...prev,
                project_id: projectId,
                project_name: nextProject?.name ?? prev.project_name,
                space_id: nextProject?.space_id ?? null,
                space_name: nextSpace?.name ?? null,
              }
            : prev,
        );
        fetchReport();
        window.dispatchEvent(new Event("task-list-refresh"));
      } catch (err) {
        console.error("タスクのプロジェクト移動に失敗", err);
        alert("プロジェクトの変更に失敗しました");
      } finally {
        setEditSaving(false);
      }
    },
    [allProjects, editingEntry, fetchReport, remoteReadOnly, spaces],
  );

  const handleEditMoveTaskSpace = useCallback(
    async (spaceId: string) => {
      if (!spaceId) return;
      const currentSpaceId =
        currentEditingSpace?.id ?? editingEntry?.space_id ?? null;
      if (spaceId === currentSpaceId) return;
      const targetProject = allProjects.find(
        (project) => project.space_id === spaceId,
      );
      if (!targetProject) {
        alert("このスペースに移動できるプロジェクトがありません");
        return;
      }
      await handleEditMoveTaskProject(targetProject.id);
    },
    [
      allProjects,
      currentEditingSpace?.id,
      editingEntry?.space_id,
      handleEditMoveTaskProject,
    ],
  );

  return {
    editingEntry,
    editDate,
    editStart,
    editEnd,
    editDuration,
    editNote,
    editSaving,
    setEditDate,
    setEditStart,
    setEditEnd,
    setEditDuration,
    setEditNote,
    currentEditingProject,
    currentEditingSpace,
    projectsForEditingSpace,
    isEditingRunning,
    openEditDialog,
    closeEditDialog,
    handleEditStartBlur,
    handleEditEndBlur,
    handleEditDurationBlur,
    handleEditSave,
    handleEditInputEnter,
    handleEditDelete,
    handleEditDuplicate,
    handleEditRestartTimer,
    handleEditStopTimer,
    handleEditRevertToOriginal,
    handleOpenTaskDetail,
    handleEditMoveTaskProject,
    handleEditMoveTaskSpace,
  };
}

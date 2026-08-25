import type {
  ScheduleData,
  SchedulePhaseRecord,
  SchedulePlacementRecord,
} from "@/lib/server/task-schedule";

export type SchedulePhase = SchedulePhaseRecord;
export type TaskSchedulePlacement = SchedulePlacementRecord;
export type TaskSchedule = ScheduleData;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      payload && typeof payload.detail === "string"
        ? payload.detail
        : response.statusText || "スケジュール操作に失敗しました";
    throw new Error(detail);
  }
  return payload as T;
}

export const scheduleApi = {
  get(projectId: string) {
    return request<ScheduleData>(
      `/api/projects/${encodeURIComponent(projectId)}/schedule`,
    );
  },
  createPhase(
    projectId: string,
    payload: Pick<SchedulePhase, "name" | "start_on" | "end_on"> &
      Partial<Pick<SchedulePhase, "sort_order">>,
  ) {
    return request<SchedulePhase>(
      `/api/projects/${encodeURIComponent(projectId)}/schedule`,
      { method: "POST", body: JSON.stringify(payload) },
    );
  },
  updatePhase(
    projectId: string,
    phaseId: string,
    payload: Partial<Pick<SchedulePhase, "name" | "start_on" | "end_on" | "sort_order">>,
  ) {
    return request<SchedulePhase>(
      `/api/projects/${encodeURIComponent(projectId)}/schedule/phases/${encodeURIComponent(phaseId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    );
  },
  deletePhase(projectId: string, phaseId: string) {
    return request<{ success: true }>(
      `/api/projects/${encodeURIComponent(projectId)}/schedule/phases/${encodeURIComponent(phaseId)}`,
      { method: "DELETE" },
    );
  },
  upsertPlacement(
    projectId: string,
    taskId: string,
    payload: Pick<TaskSchedulePlacement, "phase_id" | "x_ratio" | "y">,
  ) {
    return request<TaskSchedulePlacement>(
      `/api/projects/${encodeURIComponent(projectId)}/schedule/placements/${encodeURIComponent(taskId)}`,
      { method: "PUT", body: JSON.stringify(payload) },
    );
  },
  deletePlacement(projectId: string, taskId: string) {
    return request<{ success: true }>(
      `/api/projects/${encodeURIComponent(projectId)}/schedule/placements/${encodeURIComponent(taskId)}`,
      { method: "DELETE" },
    );
  },
};

/** Parse a date-only DB value in local calendar space (never UTC conversion). */
export function scheduleDateToLocal(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  if (
    date.getFullYear() !== Number(match[1]) ||
    date.getMonth() !== Number(match[2]) - 1 ||
    date.getDate() !== Number(match[3])
  ) {
    return null;
  }
  date.setHours(0, 0, 0, 0);
  return date;
}

export function scheduleDateToInput(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function clampSchedulePlacement(
  xRatio: number,
  y: number,
): { x_ratio: number; y: number } {
  return {
    x_ratio: Math.max(0, Math.min(1, Number.isFinite(xRatio) ? xRatio : 0)),
    y: Math.max(-100000, Math.min(100000, Number.isFinite(y) ? y : 0)),
  };
}

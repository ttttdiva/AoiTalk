/**
 * 外部AoiTalkサーバーのタスク中継クライアント（モバイル）。
 * バックエンドプロキシ /api/remote-servers/{id}/... を fetchApi 経由で叩く。
 * 取得したリモートタスクは表示用に保持するだけで、ローカルDBには保存しない。
 */

import { fetchApi } from "./api-client";
import { normalizeTaskStatus } from "./task-status";
import type { Task } from "../types/api";

export type RemoteTask = Task & {
  /** 由来サーバーの識別用（表示・操作の出し分けに使う）。 */
  remote_server_id: string;
  remote_server_name: string;
  remote_server_color?: string | null;
};

function buildQuery(params?: Record<string, string | undefined>): string {
  if (!params) return "";
  const usp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v) usp.set(k, v);
  });
  const qs = usp.toString();
  return qs ? `?${qs}` : "";
}

export async function listRemoteTasks(
  profileId: string,
  params?: { project_id?: string; space_id?: string; status?: string },
): Promise<Task[]> {
  const data = await fetchApi<{ data: Task[] | { tasks?: Task[] } }>(
    `/api/remote-servers/${profileId}/tasks${buildQuery(params)}`,
  );
  const payload = data.data;
  const tasks = Array.isArray(payload)
    ? payload
    : payload && Array.isArray(payload.tasks)
      ? payload.tasks
      : [];
  return tasks.map((task) => ({
    ...task,
    status: normalizeTaskStatus(task.status),
  }));
}

export async function patchRemoteTask(
  profileId: string,
  taskId: string,
  patch: {
    status?: string;
    start_at?: string | null;
    end_at?: string | null;
    priority?: string;
  },
): Promise<Task | null> {
  const data = await fetchApi<{ data: Task }>(
    `/api/remote-servers/${profileId}/tasks/${taskId}`,
    { method: "PATCH", body: JSON.stringify(patch) },
  );
  return data.data ?? null;
}

export async function addRemoteTaskComment(
  profileId: string,
  taskId: string,
  content: string,
): Promise<void> {
  await fetchApi(`/api/remote-servers/${profileId}/tasks/${taskId}/comments`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}

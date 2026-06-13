/**
 * 外部AoiTalkサーバーのタスク中継クライアント。
 * Python側プロキシ(/api/remote-servers/{id}/...)を /api/python-proxy 経由で叩く。
 * 取得したリモートタスクは表示用に保持するだけで、ローカルDBには保存しない。
 */

import type { Task } from "@/lib/task-api";

export type RemoteTask = Task & {
  /** 由来サーバーの識別用（表示・操作の出し分けに使う）。 */
  remote_server_id: string;
  remote_server_name: string;
  remote_server_color?: string | null;
};

async function proxy<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res
      .json()
      .catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

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
  const data = await proxy<{ data: Task[] | { tasks?: Task[] } }>(
    `/remote-servers/${profileId}/tasks${buildQuery(params)}`,
  );
  const payload = data.data;
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.tasks)) return payload.tasks;
  return [];
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
  const data = await proxy<{ data: Task }>(
    `/remote-servers/${profileId}/tasks/${taskId}`,
    { method: "PATCH", body: JSON.stringify(patch) },
  );
  return data.data ?? null;
}

export async function addRemoteTaskComment(
  profileId: string,
  taskId: string,
  content: string,
): Promise<void> {
  await proxy(`/remote-servers/${profileId}/tasks/${taskId}/comments`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}

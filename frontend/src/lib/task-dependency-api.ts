export type TaskDependency = {
  id: string;
  /** 後続タスク。depends_on_task_id の完了を前提とする。 */
  task_id: string;
  /** 前提タスク。依存フローではこのタスクが source になる。 */
  depends_on_task_id: string;
  created_at: string | null;
};

export type CreateTaskDependencyInput = {
  task_id: string;
  depends_on_task_id: string;
};

export class TaskDependencyApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly responseBody: unknown = null,
  ) {
    super(message);
    this.name = "TaskDependencyApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const responseBody = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    const message =
      responseBody &&
      typeof responseBody === "object" &&
      "detail" in responseBody &&
      typeof responseBody.detail === "string"
        ? responseBody.detail
        : response.statusText || "依存関係APIの呼び出しに失敗しました";
    throw new TaskDependencyApiError(message, response.status, responseBody);
  }
  return response.json() as Promise<T>;
}

export function listTaskDependencies(
  filters: { projectId?: string; taskId?: string },
  options: { signal?: AbortSignal } = {},
): Promise<TaskDependency[]> {
  const query = new URLSearchParams();
  if (filters.projectId) query.set("project_id", filters.projectId);
  if (filters.taskId) query.set("task_id", filters.taskId);
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return request<TaskDependency[]>(`/api/task-dependencies${suffix}`, {
    signal: options.signal,
  });
}

export function createTaskDependency(
  input: CreateTaskDependencyInput,
): Promise<TaskDependency> {
  return request<TaskDependency>("/api/task-dependencies", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function deleteTaskDependency(id: string): Promise<void> {
  await request<{ success: true }>(
    `/api/task-dependencies/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
}

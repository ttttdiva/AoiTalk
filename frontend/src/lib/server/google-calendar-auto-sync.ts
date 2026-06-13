import { fetchPythonApi } from "@/lib/server/python-api-proxy";

type SessionUser = {
  username?: string | null;
};

export type GoogleCalendarSyncResult = {
  status: "created" | "updated" | "deleted" | "skipped" | "warning";
  reason?: string;
  message?: string;
  event_id?: string | null;
  html_link?: string | null;
  calendar_id?: string | null;
  metadata?: Record<string, unknown>;
};

function logQueuedGoogleCalendarFailure(
  action: string,
  taskId: string,
  error: unknown,
) {
  console.warn(
    `Queued Google Calendar ${action} failed for task ${taskId}:`,
    error,
  );
}

async function callGoogleCalendarTaskAction(
  taskId: string,
  user: SessionUser,
  action: "google-calendar-auto-sync" | "google-calendar-auto-delete",
): Promise<GoogleCalendarSyncResult> {
  const username = user.username?.trim();
  if (!username) {
    return { status: "warning", message: "Missing user session username" };
  }

  try {
    const res = await fetchPythonApi(`/api/tasks/${taskId}/${action}`, {
      method: "POST",
      signal: AbortSignal.timeout(30000),
      user: { username },
      headers: {
        "Content-Type": "application/json",
      },
    });
    if (!res.ok) {
      const detail = await res
        .json()
        .catch(() => ({ detail: `${res.status} ${res.statusText}` }));
      return {
        status: "warning",
        message: String(detail.detail || res.statusText),
      };
    }
    return (await res.json()) as GoogleCalendarSyncResult;
  } catch (error) {
    return {
      status: "warning",
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

export function autoSyncGoogleCalendarForTask(
  taskId: string,
  user: SessionUser,
): Promise<GoogleCalendarSyncResult> {
  return callGoogleCalendarTaskAction(
    taskId,
    user,
    "google-calendar-auto-sync",
  );
}

export function enqueueAutoSyncGoogleCalendarForTask(
  taskId: string,
  user: SessionUser,
): void {
  void autoSyncGoogleCalendarForTask(taskId, user).catch((error) =>
    logQueuedGoogleCalendarFailure("auto sync", taskId, error),
  );
}

export function deleteAutoGoogleCalendarForTask(
  taskId: string,
  user: SessionUser,
): Promise<GoogleCalendarSyncResult> {
  return callGoogleCalendarTaskAction(
    taskId,
    user,
    "google-calendar-auto-delete",
  );
}

export function enqueueDeleteAutoGoogleCalendarForTask(
  taskId: string,
  user: SessionUser,
): void {
  void deleteAutoGoogleCalendarForTask(taskId, user).catch((error) =>
    logQueuedGoogleCalendarFailure("auto delete", taskId, error),
  );
}

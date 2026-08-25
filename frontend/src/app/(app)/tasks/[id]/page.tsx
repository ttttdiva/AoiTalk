"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";

import { useProject } from "@/contexts/project-context";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";
import { TaskDetailWorkspaceNavigation } from "@/components/tasks/task-detail-workspace-navigation";
import {
  RemoteTaskDialog,
  type RemoteTaskDialogTarget,
} from "@/components/tasks/remote-task-dialog";
import type { Task } from "@/lib/task-api";
import { listRemoteServers } from "@/lib/remote-servers";
import { listRemoteTasks, toRemoteTask } from "@/lib/remote-tasks";
import { resourceId } from "@/lib/remote-resource";

async function resolveRemoteTask(taskId: string): Promise<Task | null> {
  const profiles = (await listRemoteServers()).filter(
    (profile) => profile.enabled,
  );
  const remoteMatch = taskId.match(/^remote:([^:]+):(.+)$/);
  const requestedProfileId = remoteMatch?.[1] ?? null;
  const requestedResourceId = resourceId(taskId) ?? taskId;
  const candidates = requestedProfileId
    ? profiles.filter((profile) => profile.id === requestedProfileId)
    : profiles;

  for (const profile of candidates) {
    try {
      const tasks = await listRemoteTasks(profile.id);
      const match = tasks.find(
        (candidate) =>
          candidate.id === requestedResourceId || candidate.id === taskId,
      );
      if (match) return toRemoteTask(profile, match);
    } catch {
      // A failed connection must not prevent another configured profile from
      // resolving a deep link.
    }
  }
  return null;
}

/**
 * The canonical task detail surface lives in TaskDetailModal.  Keep the
 * direct-link route as a thin loader/ACL boundary so links from chat, apps,
 * and notifications receive exactly the same full detail UI as the Tasks
 * workspace instead of maintaining a second, incomplete editor.
 */
export default function TaskDetailPage() {
  const params = useParams<{ id: string | string[] }>();
  const router = useRouter();
  const rawTaskId = params?.id;
  const taskId = Array.isArray(rawTaskId) ? (rawTaskId[0] ?? "") : rawTaskId;
  const { projects, allProjects } = useProject();
  const [task, setTask] = useState<Task | null>(null);
  const [remoteTaskLoading, setRemoteTaskLoading] = useState(false);
  const [remoteTaskError, setRemoteTaskError] = useState<string | null>(null);
  const isRemoteDeepLink = taskId.startsWith("remote:");

  const availableProjects = useMemo(
    () => (allProjects?.length ? allProjects : projects),
    [allProjects, projects],
  );
  const taskProject = useMemo(
    () =>
      task
        ? availableProjects.find((project) => project.id === task.project_id)
        : null,
    [availableProjects, task],
  );

  const closeDetail = useCallback(() => {
    router.push("/tasks");
  }, [router]);

  // The modal registers the richer local detail rail at the default priority.
  // This route-level fallback keeps the shared shell populated for remote
  // deep links, where the compact RemoteTaskDialog is the detail surface.
  useWorkspaceShellRegistration({
    id: `task-detail-route-${taskId}`,
    priority: -1,
    workspaceNavigation: (
      <TaskDetailWorkspaceNavigation
        title={task?.title ?? "タスク"}
        status={task?.status}
        onBack={closeDetail}
      />
    ),
  });

  const loadRemoteTask = useCallback(
    async (showLoading = true) => {
      if (!isRemoteDeepLink || !taskId) return;
      if (showLoading) setRemoteTaskLoading(true);
      setRemoteTaskError(null);
      try {
        const remoteTask = await resolveRemoteTask(taskId);
        if (remoteTask) {
          setTask(remoteTask);
          return;
        }
        if (showLoading) setTask(null);
        setRemoteTaskError("リモートタスクが見つかりません");
      } catch (error) {
        if (showLoading) setTask(null);
        setRemoteTaskError(
          error instanceof Error
            ? error.message
            : "リモートタスクを取得できませんでした",
        );
      } finally {
        if (showLoading) setRemoteTaskLoading(false);
      }
    },
    [isRemoteDeepLink, taskId],
  );

  useEffect(() => {
    setTask(null);
    setRemoteTaskError(null);
    if (isRemoteDeepLink) void loadRemoteTask(true);
  }, [isRemoteDeepLink, loadRemoteTask]);

  const handleTaskLoaded = useCallback(
    (loaded: Task) => {
      if (loaded.id === taskId) setTask(loaded);
    },
    [taskId],
  );
  const handleTaskUpdated = useCallback(
    (updated?: Task | null) => {
      if (updated?.id === taskId) setTask(updated);
    },
    [taskId],
  );

  const taskReadOnly =
    !task ||
    !taskProject ||
    task.source === "remote" ||
    taskProject?.source === "remote" ||
    taskProject?.can_write === false;
  const remoteDialogTarget: RemoteTaskDialogTarget | null =
    task?.source === "remote" && task.remote_server_id && task.resource_id
      ? {
          profileId: task.remote_server_id,
          profileName: task.remote_server_name ?? "Remote",
          profileColor: task.remote_server_color,
          baseUrl: task.remote_server_base_url ?? "",
          taskId: task.resource_id,
          title: task.title,
          status: task.status,
          priority: task.priority,
          startAt: task.start_at,
          endAt: task.end_at,
        }
      : null;

  if (isRemoteDeepLink && remoteTaskLoading) {
    return (
      <div className="flex h-full min-h-0 flex-col gap-4 bg-background p-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-[min(70vh,640px)] w-full" />
      </div>
    );
  }

  if (isRemoteDeepLink && !task) {
    return (
      <div className="flex h-full min-h-0 flex-col items-center justify-center gap-3 bg-background p-8 text-center">
        <p className="text-sm text-muted-foreground">
          {remoteTaskError ?? "リモートタスクが見つかりません"}
        </p>
        <Button type="button" variant="outline" onClick={closeDetail}>
          タスク一覧へ戻る
        </Button>
      </div>
    );
  }

  return (
    <div
      className="flex h-full min-h-0 items-start justify-center bg-background"
      data-shell-workspace="task-detail"
      data-shell-region="task-detail-canvas"
    >
      {remoteDialogTarget ? (
        <RemoteTaskDialog
          target={remoteDialogTarget}
          onClose={closeDetail}
          onUpdated={() => void loadRemoteTask(false)}
        />
      ) : (
        <TaskDetailModal
          taskId={taskId}
          open
          readOnly={taskReadOnly}
          onOpenChange={(open) => {
            if (!open) closeDetail();
          }}
          onTaskLoaded={handleTaskLoaded}
          onTaskUpdated={handleTaskUpdated}
          onOpenTask={(nestedTaskId) =>
            router.push(`/tasks/${encodeURIComponent(nestedTaskId)}`)
          }
        />
      )}
    </div>
  );
}

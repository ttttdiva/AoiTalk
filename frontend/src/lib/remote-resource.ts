import type {
  Project,
  Space,
  Task,
  TaskOccurrence,
  TimeEntry,
  TimeReport,
} from "@/lib/task-api";

export type ResourceSource = "local" | "remote";

export type SourcedResource = {
  source?: ResourceSource | string;
  remote_server_id?: string;
  resource_id?: string;
};

export function localResourceKey(resourceId: string): string {
  return `local:${resourceId}`;
}

export function remoteResourceKey(profileId: string, resourceId: string): string {
  return `remote:${profileId}:${resourceId}`;
}

export function resourceId(value: string | null | undefined): string | null {
  if (!value) return null;
  const match = value.match(/^remote:[^:]+:(.+)$/);
  return match?.[1] ?? value.replace(/^local:/, "");
}

export function isRemoteResource(
  resource: SourcedResource | null | undefined,
): resource is SourcedResource & {
  source: "remote";
  remote_server_id: string;
  resource_id: string;
} {
  return Boolean(
    resource?.source === "remote" &&
      resource.remote_server_id &&
      resource.resource_id,
  );
}

export function decorateRemoteSpace<T extends Space>(
  profileId: string,
  profileName: string,
  profileColor: string | null | undefined,
  profileBaseUrl: string | undefined,
  space: T,
): T {
  return {
    ...space,
    id: remoteResourceKey(profileId, space.id),
    source: "remote",
    remote_server_id: profileId,
    remote_server_name: profileName,
    remote_server_color: profileColor,
    remote_server_base_url: profileBaseUrl,
    resource_id: space.id,
    can_write: false,
  } as T;
}

export function decorateRemoteProject<T extends Project>(
  profileId: string,
  profileName: string,
  profileColor: string | null | undefined,
  profileBaseUrl: string | undefined,
  project: T,
): T {
  return {
    ...project,
    id: remoteResourceKey(profileId, project.id),
    space_id: project.space_id
      ? remoteResourceKey(profileId, project.space_id)
      : null,
    source: "remote",
    remote_server_id: profileId,
    remote_server_name: profileName,
    remote_server_color: profileColor,
    remote_server_base_url: profileBaseUrl,
    resource_id: project.id,
    can_write: false,
    can_manage_settings: false,
  } as T;
}

export function decorateRemoteTask<T extends Task>(
  profileId: string,
  profileName: string,
  profileColor: string | null | undefined,
  profileBaseUrl: string | undefined,
  task: T,
): T {
  return {
    ...task,
    id: remoteResourceKey(profileId, task.id),
    project_id: remoteResourceKey(profileId, task.project_id),
    parent_task_id: task.parent_task_id
      ? remoteResourceKey(profileId, task.parent_task_id)
      : null,
    source: "remote",
    remote_server_id: profileId,
    remote_server_name: profileName,
    remote_server_color: profileColor,
    remote_server_base_url: profileBaseUrl,
    resource_id: task.id,
    metadata: {
      ...(task.metadata ?? {}),
      remote_server_id: profileId,
      remote_resource_id: task.id,
    },
    subtasks: task.subtasks?.map((child) =>
      decorateRemoteTask(profileId, profileName, profileColor, profileBaseUrl, child),
    ),
  } as T;
}

export function decorateRemoteOccurrence<T extends TaskOccurrence>(
  profileId: string,
  occurrence: T,
): T {
  return {
    ...occurrence,
    id: remoteResourceKey(profileId, occurrence.id),
    task_id: remoteResourceKey(profileId, occurrence.task_id),
    project_id: occurrence.project_id
      ? remoteResourceKey(profileId, occurrence.project_id)
      : occurrence.project_id,
    source: "remote",
    remote_server_id: profileId,
    resource_id: occurrence.id,
  } as T;
}

export function decorateRemoteTimeEntry<T extends TimeEntry>(
  profileId: string,
  profileName: string,
  profileColor: string | null | undefined,
  profileBaseUrl: string | undefined,
  entry: T,
): T {
  return {
    ...entry,
    id: remoteResourceKey(profileId, entry.id),
    task_id: remoteResourceKey(profileId, entry.task_id),
    project_id: entry.project_id
      ? remoteResourceKey(profileId, entry.project_id)
      : entry.project_id,
    space_id: entry.space_id
      ? remoteResourceKey(profileId, entry.space_id)
      : entry.space_id,
    source: "remote",
    remote_server_id: profileId,
    remote_server_name: profileName,
    remote_server_color: profileColor,
    remote_server_base_url: profileBaseUrl,
    resource_id: entry.id,
  } as T;
}

export function decorateRemoteTimeReport(
  profileId: string,
  report: TimeReport,
): TimeReport {
  const decorateBucket = (bucket: TimeReport["by_task"][number], resourceKey: boolean) => ({
    ...bucket,
    key: resourceKey ? remoteResourceKey(profileId, bucket.key) : bucket.key,
    project_id: bucket.project_id
      ? remoteResourceKey(profileId, bucket.project_id)
      : bucket.project_id,
    source: "remote",
    remote_server_id: profileId,
    resource_id: resourceKey ? bucket.key : undefined,
  });
  return {
    ...report,
    by_task: report.by_task.map((bucket) => decorateBucket(bucket, true)),
    by_project: report.by_project.map((bucket) => decorateBucket(bucket, false)),
    by_day: report.by_day.map((bucket) => ({ ...bucket, source: "remote", remote_server_id: profileId })),
    by_user: report.by_user.map((bucket) => ({ ...bucket, source: "remote", remote_server_id: profileId })),
  };
}

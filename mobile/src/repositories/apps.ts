/**
 * Apps の mobile local-first repository。
 *
 * 読み取りは認証スコープごとの AsyncStorage snapshot を先に返し、オンライン
 * refresh が成功した場合だけ authoritative replace する。refresh が失敗した場合
 * は既存 snapshot をそのまま保持し、Apps の mutation を outbox へ送らない。
 */

import AsyncStorage from "@react-native-async-storage/async-storage";
import { and, eq } from "drizzle-orm";
import {
  appsApi,
  assertRunnerPermission,
  AppsOfflineMutationError,
  AppsPermissionError,
  AppRuntimeContractError,
  normalizeAppRuntimeFile,
  normalizeAppRuntimeDeclaration,
  normalizeAppRuntimeRun,
  permissionAtLeast,
  type AppContext,
  type AppFile,
  type AppFileContent,
  type AppJob,
  type AppJobType,
  type AppRelease,
  type AppRuntimeDeclaration,
  type AppRuntimeFileModel,
  type AppRuntimeFileAsset,
  type AppRuntimeInputFile,
  type AppRuntimeRun,
  type AppSummary,
  type AppTarget,
  type CreateAppInput,
  type ProjectAppBinding,
  type ProjectAppInput,
  type TaskAppInput,
  type TaskAppLink,
} from "../lib/apps-api";
import { getToken, getTokenAuthScope } from "../lib/auth";
import { canAttemptAoiTalkServer } from "../stores/network";
import { getDb, schema } from "../db/client";
import { ensureSchema } from "../db/migrate";

const STORAGE_PREFIX = "aoitalk:apps-cache:v1:";
const RUNTIME_PENDING_PREFIX = "aoitalk:app-runtime-pending:v1:";
const EMPTY_FETCH_MARKER_PREFIX = "aoitalk:apps-empty-fetch:v1:";

type EmptyFetchSection = "targets" | "files" | "releases" | "jobs";

type AppsCacheState = {
  apps: AppSummary[];
  projectViews: Record<string, string[]>;
  projectApps: Record<string, ProjectAppBinding[]>;
  taskApps: Record<string, TaskAppLink[]>;
  appTasks: Record<string, Array<TaskAppLink & { task?: Record<string, unknown> }>>;
  contexts: Record<string, AppContext>;
  targets: Record<string, AppTarget[]>;
  files: Record<string, AppFile[]>;
  fileContents: Record<string, AppFileContent>;
  releases: Record<string, AppRelease[]>;
  jobs: Record<string, AppJob[]>;
};

export type AppsDetailReadSection =
  | "context"
  | "targets"
  | "files"
  | "releases"
  | "jobs"
  | "runtime"
  | "runtimeRuns";

export type AppsReadReason =
  | "offline"
  | "auth"
  | "forbidden"
  | "not_found"
  | "network"
  | "unknown"
  | "not_loaded"
  | "legacy";

export type AppsReadStatus = {
  state: "fresh" | "cached" | "unavailable";
  reason: AppsReadReason | null;
  legacy?: boolean;
};

export type AppsDetailReadStatuses = {
  [section in AppsDetailReadSection]: AppsReadStatus;
};

export type PendingRuntimeAction = {
  action_id: string;
  idempotency_key: string;
  run_id?: string;
  created_at: number;
};

// The in-memory mirror avoids repeatedly parsing AsyncStorage while a screen is
// focused. It is still keyed by auth scope and hydrated lazily from storage.
const memoryCache = new Map<string, AppsCacheState>();
const hydrateInFlight = new Map<string, Promise<AppsCacheState>>();
const cacheCommitTails = new Map<string, Promise<void>>();

// Runtime declarations and Action runs are deliberately kept out of the
// generic Apps cache schema. They are immutable/read-only projections and
// must never become an offline execution queue. A stale declaration may be
// shown as unavailable, but an Action is always revalidated online.
const runtimeCache = new Map<string, AppRuntimeDeclaration | null>();
const runtimeRunsCache = new Map<string, AppRuntimeRun[]>();
const detailReadStatusCache = new Map<string, AppsDetailReadStatuses>();
type DetailListReadSection = "targets" | "releases";
type DetailListReadTicket = { requestId: number; epoch: number };
type ActiveDetailListReadTicket = DetailListReadTicket & { key: string };
const latestDetailListReads = new Map<string, number>();
let nextDetailListReadRequestId = 0;
let detailListReadEpoch = 0;

type AppsSqlite = {
  db: ReturnType<typeof getDb>;
  schema: typeof schema;
};

function sqliteOrNull(): AppsSqlite | null {
  try {
    // Migration is additive and isolated from the generic sync/outbox tables.
    ensureSchema();
    return { db: getDb(), schema };
  } catch {
    // Jest/browser-only screens may not provide Expo SQLite. AsyncStorage is
    // retained as a compatibility fallback for those environments.
    return null;
  }
}

function jsonObject(value: unknown): Record<string, unknown> | undefined {
  return isRecord(value) ? value : undefined;
}

function appProjectKey(projectId?: string | null): string {
  return projectId || "__global__";
}

function appScopedKey(appId: string, projectId?: string | null): string {
  return `${appId}:${appProjectKey(projectId)}`;
}

function runtimeScopedKey(scope: string, appId: string, projectId?: string | null): string {
  return `${scope}:${appScopedKey(appId, projectId)}`;
}

function defaultDetailReadStatuses(): AppsDetailReadStatuses {
  return {
    context: { state: "unavailable", reason: "not_loaded" },
    targets: { state: "unavailable", reason: "not_loaded" },
    files: { state: "unavailable", reason: "not_loaded" },
    releases: { state: "unavailable", reason: "not_loaded" },
    jobs: { state: "unavailable", reason: "not_loaded" },
    runtime: { state: "unavailable", reason: "not_loaded" },
    runtimeRuns: { state: "unavailable", reason: "not_loaded" },
  };
}

function detailReadStatusKey(scope: string, appId: string, projectId?: string | null): string {
  return runtimeScopedKey(scope, appId, projectId);
}

function createDetailListReadTicket(): DetailListReadTicket {
  return { requestId: ++nextDetailListReadRequestId, epoch: detailListReadEpoch };
}

function activateDetailListRead(
  ticket: DetailListReadTicket,
  scope: string,
  appId: string,
  projectId: string | null | undefined,
  section: DetailListReadSection,
): ActiveDetailListReadTicket {
  const key = `${detailReadStatusKey(scope, appId, projectId)}:${section}`;
  if (ticket.epoch === detailListReadEpoch) {
    const latestRequestId = latestDetailListReads.get(key) ?? 0;
    if (ticket.requestId > latestRequestId) latestDetailListReads.set(key, ticket.requestId);
  }
  return { ...ticket, key };
}

function isCurrentDetailListRead(ticket: ActiveDetailListReadTicket): boolean {
  return ticket.epoch === detailListReadEpoch && latestDetailListReads.get(ticket.key) === ticket.requestId;
}

function setDetailReadStatus(
  scope: string,
  appId: string,
  projectId: string | null | undefined,
  section: AppsDetailReadSection,
  status: AppsReadStatus,
): void {
  const key = detailReadStatusKey(scope, appId, projectId);
  const previous = detailReadStatusCache.get(key) ?? defaultDetailReadStatuses();
  detailReadStatusCache.set(key, { ...previous, [section]: status });
}

function getDetailReadStatusesSnapshot(
  scope: string,
  appId: string,
  projectId?: string | null,
): AppsDetailReadStatuses {
  const key = detailReadStatusKey(scope, appId, projectId);
  const current = detailReadStatusCache.get(key) ?? defaultDetailReadStatuses();
  return {
    context: { ...current.context },
    targets: { ...current.targets },
    files: { ...current.files },
    releases: { ...current.releases },
    jobs: { ...current.jobs },
    runtime: { ...current.runtime },
    runtimeRuns: { ...current.runtimeRuns },
  };
}

function hasCacheEntry(record: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function statusFromCache(
  cached: boolean,
  reason: Exclude<AppsReadReason, "legacy" | "not_loaded" | null>,
  legacy = false,
): AppsReadStatus {
  return {
    state: cached ? "cached" : "unavailable",
    reason,
    ...(legacy ? { legacy: true } : {}),
  };
}

function errorStatus(error: unknown): number | undefined {
  if (!error || typeof error !== "object") return undefined;
  const status = (error as { status?: unknown }).status;
  return typeof status === "number" ? status : undefined;
}

function readErrorReason(error: unknown): Exclude<AppsReadReason, "legacy" | "not_loaded" | null> {
  const status = errorStatus(error);
  if (status === 401) return "auth";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 0 || status === 408) return "network";

  const record = error && typeof error === "object"
    ? error as { code?: unknown; name?: unknown }
    : undefined;
  const code = typeof record?.code === "string" ? record.code.toLowerCase() : "";
  const name = typeof record?.name === "string" ? record.name.toLowerCase() : "";
  if (
    code === "network" ||
    code === "network_changed" ||
    code === "fetch_failed" ||
    code === "timeout" ||
    code.includes("network") ||
    code.includes("timeout") ||
    code === "econnrefused" ||
    code === "enotfound" ||
    name === "apinetworkchangederror" ||
    name === "apiroutingchangederror" ||
    name === "apitimeouterror" ||
    name === "apiendpointunavailableerror"
  ) return "network";

  // Network adapters commonly surface an unbranded TypeError or Error. The
  // message is used only for local classification and is never returned.
  const message = error instanceof Error ? error.message.toLowerCase() : "";
  if (/network|fetch|connection|timed? ?out|unreachable|offline/.test(message)) {
    return "network";
  }
  return "unknown";
}

async function readAvailabilityReason(): Promise<"offline" | "auth" | null> {
  const token = await getToken();
  if (!token) return "auth";
  return canAttemptAoiTalkServer() ? null : "offline";
}

function isLegacyRuntimeNotConfigured(error: unknown): boolean {
  if (errorStatus(error) !== 409 || !error || typeof error !== "object") return false;
  const responseBody = (error as { responseBody?: unknown }).responseBody;
  if (typeof responseBody !== "string") return false;
  try {
    const payload: unknown = JSON.parse(responseBody);
    return isRecord(payload) && isRecord(payload.detail) && payload.detail.code === "app_runtime_not_configured";
  } catch {
    return false;
  }
}

function cloneEmptyCache(): AppsCacheState {
  return {
    apps: [],
    projectViews: {},
    projectApps: {},
    taskApps: {},
    appTasks: {},
    contexts: {},
    targets: {},
    files: {},
    fileContents: {},
    releases: {},
    jobs: {},
  };
}

function storageKey(scope: string): string {
  return `${STORAGE_PREFIX}${encodeURIComponent(scope)}`;
}

function emptyFetchMarkerKey(
  scope: string,
  appId: string,
  projectId: string | null | undefined,
  section: EmptyFetchSection,
): string {
  return `${EMPTY_FETCH_MARKER_PREFIX}${encodeURIComponent(scope)}:${encodeURIComponent(appId)}:${encodeURIComponent(appProjectKey(projectId))}:${section}`;
}

async function hasEmptyFetchMarker(
  scope: string,
  appId: string,
  projectId: string | null | undefined,
  section: EmptyFetchSection,
): Promise<boolean> {
  try {
    return (await AsyncStorage.getItem(emptyFetchMarkerKey(scope, appId, projectId, section))) === "1";
  } catch {
    return false;
  }
}

async function updateEmptyFetchMarker(
  scope: string,
  appId: string,
  projectId: string | null | undefined,
  section: EmptyFetchSection,
  isEmpty: boolean,
): Promise<void> {
  try {
    const key = emptyFetchMarkerKey(scope, appId, projectId, section);
    if (isEmpty) await AsyncStorage.setItem(key, "1");
    else await AsyncStorage.removeItem(key);
  } catch {
    // Markers are a best-effort restart hint; the in-memory/SQLite snapshot
    // remains the source of the actual cached rows.
  }
}

async function hasCachedArraySection(
  scope: string,
  record: Record<string, unknown>,
  key: string,
  appId: string,
  projectId: string | null | undefined,
  section: EmptyFetchSection,
): Promise<boolean> {
  if (hasCacheEntry(record, key)) return true;
  return hasEmptyFetchMarker(scope, appId, projectId, section);
}

function runtimePendingKey(scope: string, appId: string, projectId?: string | null): string {
  return `${RUNTIME_PENDING_PREFIX}${encodeURIComponent(scope)}:${encodeURIComponent(appScopedKey(appId, projectId))}`;
}

async function readRuntimePending(scope: string, appId: string, projectId?: string | null): Promise<PendingRuntimeAction[]> {
  try {
    const raw = await AsyncStorage.getItem(runtimePendingKey(scope, appId, projectId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is PendingRuntimeAction => Boolean(
      item && typeof item === "object"
      && typeof (item as PendingRuntimeAction).action_id === "string"
      && typeof (item as PendingRuntimeAction).idempotency_key === "string"
      && typeof (item as PendingRuntimeAction).created_at === "number"
    ));
  } catch {
    return [];
  }
}

async function writeRuntimePending(scope: string, appId: string, projectId: string | null | undefined, actions: PendingRuntimeAction[]): Promise<void> {
  const key = runtimePendingKey(scope, appId, projectId);
  if (actions.length) await AsyncStorage.setItem(key, JSON.stringify(actions));
  else await AsyncStorage.removeItem(key);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCache(value: unknown): value is AppsCacheState {
  if (!isRecord(value)) return false;
  return Array.isArray(value.apps) &&
    isRecord(value.projectViews) &&
    isRecord(value.projectApps) &&
    isRecord(value.taskApps) &&
    isRecord(value.appTasks) &&
    isRecord(value.contexts) &&
    isRecord(value.targets) &&
    isRecord(value.files) &&
    isRecord(value.fileContents) &&
    isRecord(value.releases) &&
    isRecord(value.jobs);
}

function shallowCopyCache(cache: AppsCacheState): AppsCacheState {
  return {
    apps: [...cache.apps],
    projectViews: { ...cache.projectViews },
    projectApps: { ...cache.projectApps },
    taskApps: { ...cache.taskApps },
    appTasks: { ...cache.appTasks },
    contexts: { ...cache.contexts },
    targets: { ...cache.targets },
    files: { ...cache.files },
    fileContents: { ...cache.fileContents },
    releases: { ...cache.releases },
    jobs: { ...cache.jobs },
  };
}

async function loadSqliteCache(scope: string): Promise<AppsCacheState | null> {
  const sqlite = sqliteOrNull();
  if (!sqlite) return null;
  try {
    const { db, schema: dbSchema } = sqlite;
    const [appRows, targetRows, contextRows, projectRows, taskRows, releaseRows, jobRows, fileRows, contentRows] =
      await Promise.all([
        db.select().from(dbSchema.apps).where(eq(dbSchema.apps.authScope, scope)),
        db.select().from(dbSchema.appTargets).where(eq(dbSchema.appTargets.authScope, scope)),
        db.select().from(dbSchema.appContextCache).where(eq(dbSchema.appContextCache.authScope, scope)),
        db.select().from(dbSchema.projectApps).where(eq(dbSchema.projectApps.authScope, scope)),
        db.select().from(dbSchema.taskAppLinks).where(eq(dbSchema.taskAppLinks.authScope, scope)),
        db.select().from(dbSchema.appReleases).where(eq(dbSchema.appReleases.authScope, scope)),
        db.select().from(dbSchema.appJobs).where(eq(dbSchema.appJobs.authScope, scope)),
        db.select().from(dbSchema.appFileIndex).where(eq(dbSchema.appFileIndex.authScope, scope)),
        db.select().from(dbSchema.appFileContentCache).where(eq(dbSchema.appFileContentCache.authScope, scope)),
      ]);

    const cache = cloneEmptyCache();
    const appById = new Map<string, AppSummary>();
    for (const row of appRows) {
      const app: AppSummary = {
        id: row.id,
        owner_user_id: row.ownerUserId ?? null,
        origin_project_id: row.originProjectId ?? null,
        name: row.name,
        slug: row.slug,
        description: row.description ?? null,
        visibility: row.visibility,
        default_target_key: row.defaultTargetKey ?? null,
        permission: row.permission ?? undefined,
        related_project_ids: Array.isArray(row.relatedProjectIds)
          ? row.relatedProjectIds.filter((value): value is string => typeof value === "string")
          : undefined,
        created_at: row.createdAt ?? null,
        updated_at: row.updatedAt ?? null,
        archived_at: row.archivedAt ?? null,
      };
      appById.set(app.id, app);
      cache.apps.push(app);
      for (const projectId of app.related_project_ids ?? []) {
        cache.projectViews[projectId] = [
          ...(cache.projectViews[projectId] ?? []),
          app.id,
        ];
      }
    }
    for (const row of targetRows) {
      const target: AppTarget = {
        id: row.id,
        app_id: row.appId,
        target_key: row.targetKey,
        display_name: row.displayName,
        surface: row.surface,
        runtime: row.runtime,
        execution_host: row.executionHost,
        entrypoint: row.entrypoint,
        manifest_snapshot: jsonObject(row.manifestSnapshot),
        created_at: row.createdAt ?? null,
        updated_at: row.updatedAt ?? null,
      };
      const key = appScopedKey(row.appId, undefined);
      cache.targets[key] = [...(cache.targets[key] ?? []), target];
    }
    for (const row of contextRows) {
      const payload = jsonObject(row.payloadJson);
      if (!payload) continue;
      const context = payload as unknown as AppContext;
      const key = appScopedKey(row.appId, row.projectKey === "__global__" ? null : row.projectKey);
      cache.contexts[key] = context;
      if (context.app) {
        appById.set(context.app.id, context.app);
        cache.apps = upsertApp(cache.apps, context.app);
      }
    }
    for (const row of releaseRows) {
      const release: AppRelease = {
        id: row.id,
        app_id: row.appId,
        version: row.version,
        git_revision: row.gitRevision,
        manifest_hash: row.manifestHash,
        readme_hash: row.readmeHash,
        changelog: row.changelog ?? null,
        status: row.status,
        created_at: row.createdAt ?? null,
      };
      const key = appScopedKey(row.appId, undefined);
      cache.releases[key] = [...(cache.releases[key] ?? []), release];
    }
    for (const row of jobRows) {
      const job: AppJob = {
        id: row.id,
        app_id: row.appId,
        target_id: row.targetId ?? null,
        project_id: row.projectId ?? null,
        release_id: row.releaseId ?? null,
        job_type: row.jobType,
        status: row.status,
        result_json: jsonObject(row.resultJson),
        exit_code: row.exitCode ?? null,
        started_at: row.startedAt ?? null,
        ended_at: row.endedAt ?? null,
      };
      const key = appScopedKey(row.appId, row.projectId);
      cache.jobs[key] = [...(cache.jobs[key] ?? []), job];
    }
    for (const row of fileRows) {
      const file: AppFile = {
        path: row.path,
        is_dir: row.isDirectory ?? false,
        size_bytes: row.sizeBytes ?? undefined,
        sha256: row.sha256 ?? undefined,
        modified_at: row.modifiedAt ?? null,
        ...(jsonObject(row.metadataJson) ?? {}),
      };
      const key = appScopedKey(row.appId, row.projectKey === "__global__" ? null : row.projectKey);
      cache.files[key] = [...(cache.files[key] ?? []), file];
    }
    for (const row of contentRows) {
      const key = `${appScopedKey(row.appId, row.projectKey === "__global__" ? null : row.projectKey)}:${row.path}`;
      cache.fileContents[key] = {
        path: row.path,
        content: row.content ?? "",
        sha256: row.sha256 ?? undefined,
      };
    }
    for (const row of projectRows) {
      const app = appById.get(row.appId) ?? {
        id: row.appId,
        name: row.appId,
        slug: row.appId,
        visibility: "private",
      };
      const binding: ProjectAppBinding = {
        project_id: row.projectId,
        app_id: row.appId,
        binding_mode: row.bindingMode as ProjectAppBinding["binding_mode"],
        installed_release_id: row.installedReleaseId ?? null,
        enabled: row.enabled,
        pinned: row.pinned,
        display_alias: row.displayAlias ?? null,
        config_json: jsonObject(row.configJson),
        capability_grants_json: jsonObject(row.capabilityGrantsJson),
        created_at: row.createdAt ?? null,
        updated_at: row.updatedAt ?? null,
        app,
      };
      cache.projectApps[row.projectId] = [
        ...(cache.projectApps[row.projectId] ?? []),
        binding,
      ];
    }
    const targetsById = new Map(targetRows.map((row) => [row.id, {
      id: row.id,
      app_id: row.appId,
      target_key: row.targetKey,
      display_name: row.displayName,
      surface: row.surface,
      runtime: row.runtime,
      execution_host: row.executionHost,
      entrypoint: row.entrypoint,
    } satisfies AppTarget]));
    for (const row of taskRows) {
      const link: TaskAppLink = {
        id: row.id,
        task_id: row.taskId,
        app_id: row.appId,
        target_id: row.targetId ?? null,
        relation_type: row.relationType,
        app: appById.get(row.appId),
        target: row.targetId ? targetsById.get(row.targetId) ?? null : null,
      };
      cache.taskApps[row.taskId] = [...(cache.taskApps[row.taskId] ?? []), link];
      const appTaskKey = row.appId;
      cache.appTasks[appTaskKey] = [...(cache.appTasks[appTaskKey] ?? []), link];
    }
    return cache;
  } catch {
    return null;
  }
}

type PersistReplace =
  | { kind: "apps" }
  | { kind: "appsIds"; ids: string[] }
  | { kind: "targets"; appId: string }
  | { kind: "context"; appId: string; projectKey: string }
  | { kind: "files"; appId: string; projectKey: string }
  | { kind: "releases"; appId: string }
  | { kind: "jobs"; appId: string; projectId?: string | null }
  | { kind: "projectApps"; projectId: string }
  | { kind: "taskApps"; taskId: string }
  | { kind: "appTaskLinks"; appId: string };

async function clearSqliteResource(scope: string, replace: PersistReplace): Promise<void> {
  const sqlite = sqliteOrNull();
  if (!sqlite) return;
  const { db, schema: dbSchema } = sqlite;
  switch (replace.kind) {
    case "apps":
      await db.delete(dbSchema.apps).where(eq(dbSchema.apps.authScope, scope));
      break;
    case "appsIds":
      for (const id of replace.ids) {
        await db.delete(dbSchema.apps).where(and(eq(dbSchema.apps.authScope, scope), eq(dbSchema.apps.id, id)));
      }
      break;
    case "targets":
      await db.delete(dbSchema.appTargets).where(and(eq(dbSchema.appTargets.authScope, scope), eq(dbSchema.appTargets.appId, replace.appId)));
      break;
    case "context":
      await db.delete(dbSchema.appContextCache).where(and(eq(dbSchema.appContextCache.authScope, scope), eq(dbSchema.appContextCache.appId, replace.appId), eq(dbSchema.appContextCache.projectKey, replace.projectKey)));
      break;
    case "files":
      await db.delete(dbSchema.appFileIndex).where(and(eq(dbSchema.appFileIndex.authScope, scope), eq(dbSchema.appFileIndex.appId, replace.appId), eq(dbSchema.appFileIndex.projectKey, replace.projectKey)));
      break;
    case "releases":
      await db.delete(dbSchema.appReleases).where(and(eq(dbSchema.appReleases.authScope, scope), eq(dbSchema.appReleases.appId, replace.appId)));
      break;
    case "jobs":
      await db.delete(dbSchema.appJobs).where(
        replace.projectId == null
          ? and(eq(dbSchema.appJobs.authScope, scope), eq(dbSchema.appJobs.appId, replace.appId))
          : and(eq(dbSchema.appJobs.authScope, scope), eq(dbSchema.appJobs.appId, replace.appId), eq(dbSchema.appJobs.projectId, replace.projectId)),
      );
      break;
    case "projectApps":
      await db.delete(dbSchema.projectApps).where(and(eq(dbSchema.projectApps.authScope, scope), eq(dbSchema.projectApps.projectId, replace.projectId)));
      break;
    case "taskApps":
      await db.delete(dbSchema.taskAppLinks).where(and(eq(dbSchema.taskAppLinks.authScope, scope), eq(dbSchema.taskAppLinks.taskId, replace.taskId)));
      break;
    case "appTaskLinks":
      await db.delete(dbSchema.taskAppLinks).where(and(eq(dbSchema.taskAppLinks.authScope, scope), eq(dbSchema.taskAppLinks.appId, replace.appId)));
      break;
  }
}

async function persistSqlite(
  scope: string,
  cache: AppsCacheState,
  replace?: PersistReplace,
): Promise<void> {
  if (replace) await clearSqliteResource(scope, replace);
  const sqlite = sqliteOrNull();
  if (!sqlite) return;
  const { db, schema: dbSchema } = sqlite;
  const now = new Date().toISOString();
  for (const app of cache.apps) {
    await db.insert(dbSchema.apps).values({
      authScope: scope,
      id: app.id,
      ownerUserId: app.owner_user_id ?? null,
      originProjectId: app.origin_project_id ?? null,
      name: app.name,
      slug: app.slug,
      description: app.description ?? null,
      visibility: app.visibility,
      defaultTargetKey: app.default_target_key ?? null,
      permission: app.permission ?? null,
      relatedProjectIds: app.related_project_ids ?? null,
      createdAt: app.created_at ?? now,
      updatedAt: app.updated_at ?? now,
      archivedAt: app.archived_at ?? null,
      cachedAt: now,
    }).onConflictDoUpdate({
      target: [dbSchema.apps.authScope, dbSchema.apps.id],
      set: {
        ownerUserId: app.owner_user_id ?? null,
        originProjectId: app.origin_project_id ?? null,
        name: app.name,
        slug: app.slug,
        description: app.description ?? null,
        visibility: app.visibility,
        defaultTargetKey: app.default_target_key ?? null,
        permission: app.permission ?? null,
        relatedProjectIds: app.related_project_ids ?? null,
        updatedAt: app.updated_at ?? now,
        archivedAt: app.archived_at ?? null,
        cachedAt: now,
      },
    });
  }
  for (const [key, targets] of Object.entries(cache.targets)) {
    const appId = key.split(":", 1)[0];
    for (const target of targets) {
      await db.insert(dbSchema.appTargets).values({
        authScope: scope,
        id: target.id,
        appId: target.app_id || appId,
        targetKey: target.target_key,
        displayName: target.display_name,
        surface: target.surface,
        runtime: target.runtime,
        executionHost: target.execution_host,
        entrypoint: target.entrypoint,
        manifestSnapshot: target.manifest_snapshot ?? null,
        createdAt: target.created_at ?? now,
        updatedAt: target.updated_at ?? now,
      }).onConflictDoUpdate({
        target: [dbSchema.appTargets.authScope, dbSchema.appTargets.id],
        set: {
          appId: target.app_id || appId,
          targetKey: target.target_key,
          displayName: target.display_name,
          surface: target.surface,
          runtime: target.runtime,
          executionHost: target.execution_host,
          entrypoint: target.entrypoint,
          manifestSnapshot: target.manifest_snapshot ?? null,
          updatedAt: target.updated_at ?? now,
        },
      });
    }
  }
  for (const [key, context] of Object.entries(cache.contexts)) {
    const separator = key.indexOf(":");
    const appId = separator >= 0 ? key.slice(0, separator) : key;
    const projectKey = separator >= 0 ? key.slice(separator + 1) : "__global__";
    await db.insert(dbSchema.appContextCache).values({
      authScope: scope,
      appId,
      projectKey: projectKey || "__global__",
      projectId: projectKey && projectKey !== "__global__" ? projectKey : null,
      targetKey: context.target_key ?? null,
      payloadJson: context,
      cachedAt: now,
    }).onConflictDoUpdate({
      target: [dbSchema.appContextCache.authScope, dbSchema.appContextCache.appId, dbSchema.appContextCache.projectKey],
      set: {
        projectId: projectKey && projectKey !== "__global__" ? projectKey : null,
        targetKey: context.target_key ?? null,
        payloadJson: context,
        cachedAt: now,
      },
    });
  }
  for (const bindings of Object.values(cache.projectApps)) {
    for (const binding of bindings) {
      await db.insert(dbSchema.projectApps).values({
        authScope: scope,
        projectId: binding.project_id,
        appId: binding.app_id,
        bindingMode: binding.binding_mode,
        installedReleaseId: binding.installed_release_id ?? null,
        enabled: binding.enabled,
        pinned: binding.pinned,
        displayAlias: binding.display_alias ?? null,
        configJson: binding.config_json ?? null,
        capabilityGrantsJson: binding.capability_grants_json ?? null,
        createdAt: binding.created_at ?? now,
        updatedAt: binding.updated_at ?? now,
      }).onConflictDoUpdate({
        target: [dbSchema.projectApps.authScope, dbSchema.projectApps.projectId, dbSchema.projectApps.appId],
        set: {
          bindingMode: binding.binding_mode,
          installedReleaseId: binding.installed_release_id ?? null,
          enabled: binding.enabled,
          pinned: binding.pinned,
          displayAlias: binding.display_alias ?? null,
          configJson: binding.config_json ?? null,
          capabilityGrantsJson: binding.capability_grants_json ?? null,
          updatedAt: binding.updated_at ?? now,
        },
      });
    }
  }
  for (const links of Object.values(cache.taskApps)) {
    for (const link of links) {
      await db.insert(dbSchema.taskAppLinks).values({
        authScope: scope,
        id: link.id,
        taskId: link.task_id,
        appId: link.app_id,
        targetId: link.target_id ?? null,
        relationType: link.relation_type,
        createdAt: null,
      }).onConflictDoUpdate({
        target: [dbSchema.taskAppLinks.authScope, dbSchema.taskAppLinks.id],
        set: {
          taskId: link.task_id,
          appId: link.app_id,
          targetId: link.target_id ?? null,
          relationType: link.relation_type,
        },
      });
    }
  }
  for (const [key, releases] of Object.entries(cache.releases)) {
    const appId = key.split(":", 1)[0];
    for (const release of releases) {
      await db.insert(dbSchema.appReleases).values({
        authScope: scope,
        id: release.id,
        appId: release.app_id || appId,
        version: release.version,
        gitRevision: release.git_revision,
        manifestHash: release.manifest_hash,
        readmeHash: release.readme_hash,
        changelog: release.changelog ?? null,
        status: release.status,
        createdAt: release.created_at ?? now,
      }).onConflictDoUpdate({
        target: [dbSchema.appReleases.authScope, dbSchema.appReleases.id],
        set: {
          appId: release.app_id || appId,
          version: release.version,
          gitRevision: release.git_revision,
          manifestHash: release.manifest_hash,
          readmeHash: release.readme_hash,
          changelog: release.changelog ?? null,
          status: release.status,
          createdAt: release.created_at ?? now,
        },
      });
    }
  }
  for (const [key, jobs] of Object.entries(cache.jobs)) {
    const appId = key.split(":", 1)[0];
    for (const job of jobs) {
      await db.insert(dbSchema.appJobs).values({
        authScope: scope,
        id: job.id,
        appId: job.app_id || appId,
        targetId: job.target_id ?? null,
        projectId: job.project_id ?? null,
        releaseId: job.release_id ?? null,
        jobType: job.job_type,
        status: job.status,
        resultJson: job.result_json ?? null,
        exitCode: job.exit_code ?? null,
        startedAt: job.started_at ?? null,
        endedAt: job.ended_at ?? null,
        cachedAt: now,
      }).onConflictDoUpdate({
        target: [dbSchema.appJobs.authScope, dbSchema.appJobs.id],
        set: {
          appId: job.app_id || appId,
          targetId: job.target_id ?? null,
          projectId: job.project_id ?? null,
          releaseId: job.release_id ?? null,
          jobType: job.job_type,
          status: job.status,
          resultJson: job.result_json ?? null,
          exitCode: job.exit_code ?? null,
          startedAt: job.started_at ?? null,
          endedAt: job.ended_at ?? null,
          cachedAt: now,
        },
      });
    }
  }
  for (const [key, files] of Object.entries(cache.files)) {
    const separator = key.indexOf(":");
    const appId = separator >= 0 ? key.slice(0, separator) : key;
    const projectKey = separator >= 0 ? key.slice(separator + 1) : "__global__";
    for (const file of files) {
      await db.insert(dbSchema.appFileIndex).values({
        authScope: scope,
        appId,
        projectKey: projectKey || "__global__",
        path: file.path,
        isDirectory: file.is_dir ?? false,
        sizeBytes: file.size_bytes ?? file.size ?? null,
        sha256: typeof file.sha256 === "string" ? file.sha256 : null,
        contentType: typeof file.content_type === "string" ? file.content_type : null,
        modifiedAt: file.modified_at ?? null,
        metadataJson: file,
        cachedAt: now,
      }).onConflictDoUpdate({
        target: [dbSchema.appFileIndex.authScope, dbSchema.appFileIndex.appId, dbSchema.appFileIndex.projectKey, dbSchema.appFileIndex.path],
        set: {
          isDirectory: file.is_dir ?? false,
          sizeBytes: file.size_bytes ?? file.size ?? null,
          sha256: typeof file.sha256 === "string" ? file.sha256 : null,
          contentType: typeof file.content_type === "string" ? file.content_type : null,
          modifiedAt: file.modified_at ?? null,
          metadataJson: file,
          cachedAt: now,
        },
      });
    }
  }
  for (const [key, content] of Object.entries(cache.fileContents)) {
    const first = key.indexOf(":");
    const second = first >= 0 ? key.indexOf(":", first + 1) : -1;
    const appId = first >= 0 ? key.slice(0, first) : key;
    const projectKey = second >= 0 ? key.slice(first + 1, second) : "__global__";
    const path = second >= 0 ? key.slice(second + 1) : key.slice(first + 1);
    await db.insert(dbSchema.appFileContentCache).values({
      authScope: scope,
      appId,
      projectKey: projectKey || "__global__",
      path: content.path || path,
      content: content.content,
      sha256: content.sha256 ?? null,
      cachedAt: now,
      updatedAt: now,
    }).onConflictDoUpdate({
      target: [dbSchema.appFileContentCache.authScope, dbSchema.appFileContentCache.appId, dbSchema.appFileContentCache.projectKey, dbSchema.appFileContentCache.path],
      set: {
        content: content.content,
        sha256: content.sha256 ?? null,
        cachedAt: now,
        updatedAt: now,
      },
    });
  }
}

async function currentScope(): Promise<string> {
  return getTokenAuthScope(await getToken());
}

async function hydrate(scope: string): Promise<AppsCacheState> {
  const memory = memoryCache.get(scope);
  if (memory) return memory;
  const running = hydrateInFlight.get(scope);
  if (running) return running;
  const promise = loadSqliteCache(scope).then(async (sqliteCache) => {
    if (sqliteCache) {
      memoryCache.set(scope, sqliteCache);
      return sqliteCache;
    }
    const raw = await AsyncStorage.getItem(storageKey(scope));
    let loaded: unknown;
    try {
      loaded = raw ? JSON.parse(raw) : null;
    } catch {
      loaded = null;
    }
    const cache = isCache(loaded) ? loaded : cloneEmptyCache();
    memoryCache.set(scope, cache);
    return cache;
    })
    .catch(() => {
      const cache = cloneEmptyCache();
      memoryCache.set(scope, cache);
      return cache;
    })
    .finally(() => {
      if (hydrateInFlight.get(scope) === promise) hydrateInFlight.delete(scope);
    });
  hydrateInFlight.set(scope, promise);
  return promise;
}

async function persistSnapshot(
  scope: string,
  cache: AppsCacheState,
  replace?: PersistReplace,
): Promise<void> {
  memoryCache.set(scope, cache);
  try {
    const sqlite = sqliteOrNull();
    if (sqlite) {
      await persistSqlite(scope, cache, replace);
      return;
    }
    await AsyncStorage.setItem(storageKey(scope), JSON.stringify(cache));
  } catch {
    // Cache persistence is best effort; the in-memory snapshot remains useful.
  }
}

async function commitCacheUpdate(
  scope: string,
  update: (latestCache: AppsCacheState) => {
    cache: AppsCacheState;
    replace?: PersistReplace;
  } | null,
  afterPersist?: () => void | Promise<void>,
): Promise<void> {
  const previous = cacheCommitTails.get(scope) ?? Promise.resolve();
  const commit = previous
    .catch(() => undefined)
    .then(async () => {
      const latestCache = memoryCache.get(scope) ?? await hydrate(scope);
      const result = update(latestCache);
      if (!result) return;

      // persistSnapshot installs memory synchronously before its first
      // await. Keeping this call adjacent to update makes the read/merge/set
      // atomic with respect to every commit queued for the same auth scope.
      const persistence = persistSnapshot(scope, result.cache, result.replace);
      await persistence;
      await afterPersist?.();
    });
  cacheCommitTails.set(scope, commit);
  try {
    await commit;
  } finally {
    if (cacheCommitTails.get(scope) === commit) cacheCommitTails.delete(scope);
  }
}

function replaceRecord<T>(
  record: Record<string, T>,
  key: string,
  value: T,
): Record<string, T> {
  return { ...record, [key]: value };
}

function upsertApp(apps: AppSummary[], app: AppSummary): AppSummary[] {
  const index = apps.findIndex((item) => item.id === app.id);
  if (index < 0) return [...apps, app];
  const next = [...apps];
  next[index] = app;
  return next;
}

async function canUseServer(): Promise<boolean> {
  const token = await getToken();
  return Boolean(token) && canAttemptAoiTalkServer();
}

function requireOnlineMutation(online: boolean): void {
  if (!online) throw new AppsOfflineMutationError();
}

/** Clear all in-memory entries. Persistent entries are scoped and intentionally retained. */
export function resetAppsRepositoryMemory(): void {
  memoryCache.clear();
  hydrateInFlight.clear();
  // Keep active commit tails so work started after a reset cannot overtake an
  // SQLite write that was already in progress. Completed tails remove themselves.
  runtimeCache.clear();
  runtimeRunsCache.clear();
  detailReadStatusCache.clear();
  latestDetailListReads.clear();
  detailListReadEpoch += 1;
}

/** Remove every persisted Apps snapshot, used by auth-scope transition tests/tools. */
export async function clearAppsCache(): Promise<void> {
  resetAppsRepositoryMemory();
  const sqlite = sqliteOrNull();
  if (sqlite) {
    const { db, schema: dbSchema } = sqlite;
    await Promise.all([
      db.delete(dbSchema.apps),
      db.delete(dbSchema.appTargets),
      db.delete(dbSchema.appContextCache),
      db.delete(dbSchema.projectApps),
      db.delete(dbSchema.taskAppLinks),
      db.delete(dbSchema.appReleases),
      db.delete(dbSchema.appJobs),
      db.delete(dbSchema.appFileIndex),
      db.delete(dbSchema.appFileContentCache),
    ]).catch(() => undefined);
  }
  const keys = await AsyncStorage.getAllKeys();
  const appKeys = keys.filter((key) =>
    key.startsWith(STORAGE_PREFIX) ||
    key.startsWith(RUNTIME_PENDING_PREFIX) ||
    key.startsWith(EMPTY_FETCH_MARKER_PREFIX),
  );
  if (appKeys.length > 0) await AsyncStorage.multiRemove(appKeys);
}

export const appsRepo = {
  async list(options: {
    projectId?: string | null;
    force?: boolean;
  } = {}): Promise<AppSummary[]> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const local = cache.apps;
    if (!(await canUseServer())) return local;
    // A forced refresh is used by pull-to-refresh; normal focus reads still
    // refresh because the endpoint is small and cache-first output is stable.
    void options.force;
    try {
      const remote = await appsApi.list(options.projectId);
      await commitCacheUpdate(scope, (latestCache) => {
        const next = shallowCopyCache(latestCache);
        const projectId = options.projectId;
        // A project-scoped list is authoritative only for that projection. Keep
        // all-app cache rows that belong to other projects to avoid cross-view loss.
        if (projectId) {
          const previousProjectIds = new Set(next.projectViews[projectId] ?? []);
          const remoteIds = new Set(remote.map((item) => item.id));
          next.projectViews = replaceRecord(next.projectViews, projectId, [...remoteIds]);
          next.apps = [
            ...next.apps.filter(
              (item) =>
                !previousProjectIds.has(item.id) &&
                !item.related_project_ids?.includes(projectId) &&
                item.origin_project_id !== projectId,
            ).filter((item) => !remoteIds.has(item.id)),
            ...remote,
          ];
          const staleIds = [...previousProjectIds].filter((id) => {
            if (remoteIds.has(id)) return false;
            const existing = latestCache.apps.find((item) => item.id === id);
            return !existing?.related_project_ids?.some(
              (relatedId) => relatedId !== projectId,
            ) && existing?.origin_project_id !== projectId;
          });
          return {
            cache: next,
            replace: staleIds.length > 0 ? { kind: "appsIds", ids: staleIds } : undefined,
          };
        }

        // Empty is meaningful: it removes stale/ghost Apps for this scope.
        next.apps = remote;
        return { cache: next, replace: { kind: "apps" } };
      });
      return remote;
    } catch {
      // Failed refresh must not overwrite or clear a valid snapshot.
      return local;
    }
  },

  async getLocal(appId: string): Promise<AppSummary | null> {
    const cache = await hydrate(await currentScope());
    return cache.apps.find((item) => item.id === appId) ?? null;
  },

  async get(appId: string, options: { projectId?: string | null } = {}): Promise<AppSummary | null> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const local = cache.apps.find((item) => item.id === appId) ?? null;
    if (!(await canUseServer())) return local;
    try {
      const remote = await appsApi.get(appId, options.projectId);
      await commitCacheUpdate(scope, (latestCache) => ({
        cache: {
          ...shallowCopyCache(latestCache),
          apps: upsertApp(latestCache.apps, remote),
        },
      }));
      return remote;
    } catch {
      return local;
    }
  },

  async create(input: CreateAppInput): Promise<AppSummary> {
    requireOnlineMutation(await canUseServer());
    const scope = await currentScope();
    await hydrate(scope);
    const app = await appsApi.create(input);
    await commitCacheUpdate(scope, (latestCache) => ({
      cache: {
        ...shallowCopyCache(latestCache),
        apps: upsertApp(latestCache.apps, app),
      },
    }));
    return app;
  },

  async update(
    appId: string,
    input: Partial<CreateAppInput> & { default_target_key?: string | null },
    options: { projectId?: string | null; permission?: string | null } = {},
  ): Promise<AppSummary> {
    requireOnlineMutation(await canUseServer());
    const local = await this.getLocal(appId);
    if (!permissionAtLeast(options.permission ?? local?.permission, "admin")) {
      throw new AppsPermissionError("App設定の変更には admin 権限が必要です");
    }
    const scope = await currentScope();
    await hydrate(scope);
    const app = await appsApi.update(appId, input, options.projectId);
    await commitCacheUpdate(scope, (latestCache) => ({
      cache: {
        ...shallowCopyCache(latestCache),
        apps: upsertApp(latestCache.apps, app),
      },
    }));
    return app;
  },

  async archive(
    appId: string,
    options: { projectId?: string | null; permission?: string | null } = {},
  ): Promise<AppSummary | null> {
    requireOnlineMutation(await canUseServer());
    const local = await this.getLocal(appId);
    if (!permissionAtLeast(options.permission ?? local?.permission, "admin")) {
      throw new AppsPermissionError("Appのアーカイブには admin 権限が必要です");
    }
    const scope = await currentScope();
    await hydrate(scope);
    const archived = await appsApi.archive(appId, options.projectId);
    await commitCacheUpdate(scope, (latestCache) => {
      const next = shallowCopyCache(latestCache);
      next.apps = next.apps.filter((item) => item.id !== appId);
      return { cache: next, replace: { kind: "appsIds", ids: [appId] } };
    });
    return archived;
  },

  async getContext(
    appId: string,
    options: { projectId?: string | null; force?: boolean } = {},
  ): Promise<AppContext | null> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const contextKey = appScopedKey(appId, options.projectId);
    const localCached = hasCacheEntry(cache.contexts, contextKey);
    const local = localCached ? cache.contexts[contextKey] : null;
    const unavailableReason = await readAvailabilityReason();
    if (unavailableReason) {
      setDetailReadStatus(
        scope,
        appId,
        options.projectId,
        "context",
        statusFromCache(localCached, unavailableReason),
      );
      return local;
    }
    try {
      const context = await appsApi.getContext(appId, options.projectId);
      await commitCacheUpdate(
        scope,
        (latestCache) => {
          const next = shallowCopyCache(latestCache);
          next.contexts = replaceRecord(next.contexts, contextKey, context);
          next.apps = upsertApp(next.apps, context.app);
          return {
            cache: next,
            replace: { kind: "context", appId, projectKey: appProjectKey(options.projectId) },
          };
        },
        () => setDetailReadStatus(scope, appId, options.projectId, "context", { state: "fresh", reason: null }),
      );
      return context;
    } catch (error) {
      setDetailReadStatus(
        scope,
        appId,
        options.projectId,
        "context",
        statusFromCache(localCached, readErrorReason(error)),
      );
      return local;
    }
  },

  async getDetailReadStatuses(
    appId: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppsDetailReadStatuses> {
    const scope = await currentScope();
    const key = detailReadStatusKey(scope, appId, options.projectId);
    const current = detailReadStatusCache.get(key);
    const unavailableReason = await readAvailabilityReason();
    if (!unavailableReason) return getDetailReadStatusesSnapshot(scope, appId, options.projectId);

    const cache = await hydrate(scope);
    const next = current ? getDetailReadStatusesSnapshot(scope, appId, options.projectId) : defaultDetailReadStatuses();
    if (next.context.reason === "not_loaded") {
      next.context = statusFromCache(
        hasCacheEntry(cache.contexts, appScopedKey(appId, options.projectId)),
        unavailableReason,
      );
    }
    if (next.targets.reason === "not_loaded") {
      next.targets = statusFromCache(
        await hasCachedArraySection(
          scope,
          cache.targets,
          appScopedKey(appId, options.projectId),
          appId,
          options.projectId,
          "targets",
        ),
        unavailableReason,
      );
    }
    if (next.files.reason === "not_loaded") {
      next.files = statusFromCache(
        await hasCachedArraySection(
          scope,
          cache.files,
          appScopedKey(appId, options.projectId),
          appId,
          options.projectId,
          "files",
        ),
        unavailableReason,
      );
    }
    if (next.releases.reason === "not_loaded") {
      next.releases = statusFromCache(
        await hasCachedArraySection(
          scope,
          cache.releases,
          appScopedKey(appId, options.projectId),
          appId,
          options.projectId,
          "releases",
        ),
        unavailableReason,
      );
    }
    if (next.jobs.reason === "not_loaded") {
      next.jobs = statusFromCache(
        await hasCachedArraySection(
          scope,
          cache.jobs,
          appScopedKey(appId, options.projectId),
          appId,
          options.projectId,
          "jobs",
        ),
        unavailableReason,
      );
    }
    if (next.runtime.reason === "not_loaded") {
      const runtimeCached = runtimeCache.has(key);
      next.runtime = statusFromCache(
        runtimeCached,
        unavailableReason,
        runtimeCached && runtimeCache.get(key) === null,
      );
    }
    if (next.runtimeRuns.reason === "not_loaded") {
      next.runtimeRuns = statusFromCache(
        runtimeRunsCache.has(key),
        unavailableReason,
      );
    }
    detailReadStatusCache.set(key, next);
    return getDetailReadStatusesSnapshot(scope, appId, options.projectId);
  },

  async getRuntime(
    appId: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeDeclaration | null> {
    const scope = await currentScope();
    const key = runtimeScopedKey(scope, appId, options.projectId);
    const localCached = runtimeCache.has(key);
    const local = localCached ? runtimeCache.get(key) ?? null : null;
    const legacyCached = localCached && local === null;
    const unavailableReason = await readAvailabilityReason();
    if (unavailableReason) {
      setDetailReadStatus(
        scope,
        appId,
        options.projectId,
        "runtime",
        statusFromCache(localCached, unavailableReason, legacyCached),
      );
      return local;
    }
    try {
      const response = await appsApi.getRuntime(appId, options);
      // A null response is the legacy Apps runtime shape. Keep the null in
      // the map so a cached legacy response differs from a missing response.
      if (response === null) {
        runtimeCache.set(key, null);
        setDetailReadStatus(scope, appId, options.projectId, "runtime", {
          state: "fresh",
          reason: "legacy",
          legacy: true,
        });
        return null;
      }
      const runtime = normalizeAppRuntimeDeclaration(response);
      runtimeCache.set(key, runtime);
      setDetailReadStatus(scope, appId, options.projectId, "runtime", { state: "fresh", reason: null });
      return runtime;
    } catch (error) {
      if (isLegacyRuntimeNotConfigured(error)) {
        runtimeCache.set(key, null);
        setDetailReadStatus(scope, appId, options.projectId, "runtime", {
          state: "fresh",
          reason: "legacy",
          legacy: true,
        });
        return null;
      }
      setDetailReadStatus(
        scope,
        appId,
        options.projectId,
        "runtime",
        statusFromCache(localCached, readErrorReason(error), legacyCached),
      );
      if (error instanceof AppRuntimeContractError) throw error;
      // A cached declaration is a read-only hint. The UI must still disable
      // every Action while offline and never turn this into an outbox entry.
      return local;
    }
  },

  async activateRelease(
    appId: string,
    releaseId: string,
    options: { permission?: string | null; projectId?: string | null } = {},
  ): Promise<AppRuntimeDeclaration> {
    requireOnlineMutation(await canUseServer());
    if (!permissionAtLeast(options.permission, "maintainer")) {
      throw new AppsPermissionError("Releaseの有効化には maintainer 権限が必要です");
    }
    const scope = await currentScope();
    const key = runtimeScopedKey(scope, appId, options.projectId);
    const runtime = normalizeAppRuntimeDeclaration(await appsApi.activateRelease(appId, releaseId));
    runtimeCache.set(key, runtime);
    return runtime;
  },

  async listRuntimeRuns(
    appId: string,
    options: { projectId?: string | null; limit?: number; idempotencyKey?: string | null } = {},
  ): Promise<AppRuntimeRun[]> {
    const scope = await currentScope();
    const key = runtimeScopedKey(scope, appId, options.projectId);
    const localCached = runtimeRunsCache.has(key);
    const local = localCached ? runtimeRunsCache.get(key) ?? [] : [];
    const unavailableReason = await readAvailabilityReason();
    if (unavailableReason) {
      setDetailReadStatus(
        scope,
        appId,
        options.projectId,
        "runtimeRuns",
        statusFromCache(localCached, unavailableReason),
      );
      return local;
    }
    try {
      const response = await appsApi.listRuntimeRuns(appId, options);
      if (!Array.isArray(response.runs)) throw new AppRuntimeContractError("runtime runsがarrayではありません");
      const runs = response.runs.map(normalizeAppRuntimeRun);
      runtimeRunsCache.set(key, runs);
      setDetailReadStatus(scope, appId, options.projectId, "runtimeRuns", { state: "fresh", reason: null });
      return runs;
    } catch (error) {
      setDetailReadStatus(
        scope,
        appId,
        options.projectId,
        "runtimeRuns",
        statusFromCache(localCached, readErrorReason(error)),
      );
      if (error instanceof AppRuntimeContractError) throw error;
      return local;
    }
  },

  async listRuntimePendingActions(
    appId: string,
    options: { projectId?: string | null } = {},
  ): Promise<PendingRuntimeAction[]> {
    return readRuntimePending(await currentScope(), appId, options.projectId);
  },

  async rememberRuntimeAction(
    appId: string,
    action: PendingRuntimeAction,
    options: { projectId?: string | null } = {},
  ): Promise<void> {
    const scope = await currentScope();
    const current = await readRuntimePending(scope, appId, options.projectId);
    const next = [...current.filter((item) => item.action_id !== action.action_id), action];
    await writeRuntimePending(scope, appId, options.projectId, next);
  },

  async forgetRuntimeAction(
    appId: string,
    actionId: string,
    options: { projectId?: string | null; idempotencyKey?: string } = {},
  ): Promise<void> {
    const scope = await currentScope();
    const current = await readRuntimePending(scope, appId, options.projectId);
    await writeRuntimePending(
      scope,
      appId,
      options.projectId,
      current.filter((item) => item.action_id !== actionId && item.idempotency_key !== options.idempotencyKey),
    );
  },

  async runRuntimeAction(
    appId: string,
    actionId: string,
    input: Record<string, unknown>,
    options: { idempotencyKey: string; projectId?: string | null },
  ): Promise<AppRuntimeRun> {
    // Action execution is online-only. In particular, do not use the generic
    // sync outbox: retrying an unknown POST could execute the same Action twice.
    requireOnlineMutation(await canUseServer());
    const scope = await currentScope();
    const key = runtimeScopedKey(scope, appId, options.projectId);
    const response = await appsApi.runRuntimeAction(appId, actionId, input, options);
    const run = normalizeAppRuntimeRun(response.run);
    const previous = runtimeRunsCache.get(key) ?? [];
    runtimeRunsCache.set(key, [run, ...previous.filter((item) => item.id !== run.id)].slice(0, 50));
    return run;
  },

  async getRuntimeRun(
    appId: string,
    runId: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeRun | null> {
    if (!(await canUseServer())) return null;
    try {
      return normalizeAppRuntimeRun(await appsApi.getRuntimeRun(appId, runId, options));
    } catch (error) {
      if (error instanceof AppRuntimeContractError) throw error;
      return null;
    }
  },

  async stopRuntimeRun(
    appId: string,
    runId: string,
    options: { projectId?: string | null; permission?: string | null } = {},
  ): Promise<AppRuntimeRun> {
    requireOnlineMutation(await canUseServer());
    if (!permissionAtLeast(options.permission, "runner")) {
      throw new AppsPermissionError();
    }
    const scope = await currentScope();
    const key = runtimeScopedKey(scope, appId, options.projectId);
    const run = normalizeAppRuntimeRun(await appsApi.stopRuntimeRun(appId, runId, options));
    const previous = runtimeRunsCache.get(key) ?? [];
    runtimeRunsCache.set(key, [run, ...previous.filter((item) => item.id !== run.id)].slice(0, 50));
    return run;
  },

  async getRuntimeRunLogs(
    appId: string,
    runId: string,
    options: { projectId?: string | null } = {},
  ): Promise<{ run_id: string; logs: string } | null> {
    if (!(await canUseServer())) return null;
    try {
      const response = await appsApi.getRuntimeRunLogs(appId, runId, options);
      return {
        run_id: response.run_id,
        logs: typeof response.logs === "string" ? response.logs : JSON.stringify(response.logs),
      };
    } catch (error) {
      if (error instanceof AppRuntimeContractError) throw error;
      return null;
    }
  },

  async getRuntimeRunFile(
    appId: string,
    runId: string,
    fileId: string,
    options: { projectId?: string | null } = {},
  ) {
    if (!(await canUseServer())) return null;
    try {
      return normalizeAppRuntimeFile(await appsApi.getRuntimeRunFile(appId, runId, fileId, options));
    } catch (error) {
      if (error instanceof AppRuntimeContractError) throw error;
      return null;
    }
  },

  async listRuntimeRunFiles(
    appId: string,
    runId: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeFileModel[]> {
    if (!(await canUseServer())) return [];
    try {
      return (await appsApi.listRuntimeRunFiles(appId, runId, options)).map(normalizeAppRuntimeFile);
    } catch (error) {
      if (error instanceof AppRuntimeContractError) throw error;
      return [];
    }
  },

  async uploadRuntimeInputFile(
    appId: string,
    actionId: string,
    file: AppRuntimeFileAsset,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeInputFile> {
    requireOnlineMutation(await canUseServer());
    return appsApi.uploadRuntimeInputFile(appId, actionId, file, options);
  },

  async downloadRuntimeRunFileBytes(
    appId: string,
    runId: string,
    fileId: string,
    options: { projectId?: string | null } = {},
  ): Promise<ArrayBuffer> {
    requireOnlineMutation(await canUseServer());
    return appsApi.downloadRuntimeRunFileBytes(appId, runId, fileId, options);
  },

  async listTargets(appId: string, options: { projectId?: string | null } = {}): Promise<AppTarget[]> {
    const readTicket = createDetailListReadTicket();
    const scope = await currentScope();
    const activeReadTicket = activateDetailListRead(readTicket, scope, appId, options.projectId, "targets");
    const cache = await hydrate(scope);
    const key = appScopedKey(appId, options.projectId);
    const localCached = await hasCachedArraySection(
      scope,
      cache.targets,
      key,
      appId,
      options.projectId,
      "targets",
    );
    const local = cache.targets[key] ?? [];
    const unavailableReason = await readAvailabilityReason();
    if (unavailableReason) {
      if (isCurrentDetailListRead(activeReadTicket)) {
        setDetailReadStatus(
          scope,
          appId,
          options.projectId,
          "targets",
          statusFromCache(localCached, unavailableReason),
        );
      }
      return local;
    }
    try {
      const remote = await appsApi.getTargets(appId, options.projectId);
      if (isCurrentDetailListRead(activeReadTicket)) {
        await commitCacheUpdate(
          scope,
          (latestCache) => {
            if (!isCurrentDetailListRead(activeReadTicket)) return null;
            const next = shallowCopyCache(latestCache);
            next.targets = replaceRecord(latestCache.targets, key, remote);
            return { cache: next, replace: { kind: "targets", appId } };
          },
          async () => {
            if (!isCurrentDetailListRead(activeReadTicket)) return;
            await updateEmptyFetchMarker(scope, appId, options.projectId, "targets", remote.length === 0);
            if (isCurrentDetailListRead(activeReadTicket)) {
              setDetailReadStatus(scope, appId, options.projectId, "targets", { state: "fresh", reason: null });
            }
          },
        );
      }
      return remote;
    } catch (error) {
      if (isCurrentDetailListRead(activeReadTicket)) {
        setDetailReadStatus(
          scope,
          appId,
          options.projectId,
          "targets",
          statusFromCache(localCached, readErrorReason(error)),
        );
      }
      return local;
    }
  },

  async listFiles(appId: string, options: { projectId?: string | null } = {}): Promise<AppFile[]> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const key = appScopedKey(appId, options.projectId);
    const localCached = await hasCachedArraySection(
      scope,
      cache.files,
      key,
      appId,
      options.projectId,
      "files",
    );
    const local = cache.files[key] ?? [];
    const unavailableReason = await readAvailabilityReason();
    if (unavailableReason) {
      setDetailReadStatus(
        scope,
        appId,
        options.projectId,
        "files",
        statusFromCache(localCached, unavailableReason),
      );
      return local;
    }
    try {
      const remote = await appsApi.getFiles(appId, options.projectId);
      await commitCacheUpdate(
        scope,
        (latestCache) => ({
          cache: {
            ...shallowCopyCache(latestCache),
            files: replaceRecord(latestCache.files, key, remote),
          },
          replace: { kind: "files", appId, projectKey: appProjectKey(options.projectId) },
        }),
        async () => {
          await updateEmptyFetchMarker(scope, appId, options.projectId, "files", remote.length === 0);
          setDetailReadStatus(scope, appId, options.projectId, "files", { state: "fresh", reason: null });
        },
      );
      return remote;
    } catch (error) {
      setDetailReadStatus(
        scope,
        appId,
        options.projectId,
        "files",
        statusFromCache(localCached, readErrorReason(error)),
      );
      return local;
    }
  },

  async getFile(
    appId: string,
    path: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppFileContent | null> {
    const key = `${appScopedKey(appId, options.projectId)}:${path}`;
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const local = cache.fileContents[key] ?? null;
    if (!(await canUseServer())) return local;
    try {
      const remote = await appsApi.getFile(appId, path, options.projectId);
      await commitCacheUpdate(scope, (latestCache) => ({
        cache: {
          ...shallowCopyCache(latestCache),
          fileContents: replaceRecord(latestCache.fileContents, key, remote),
        },
      }));
      return remote;
    } catch {
      return local;
    }
  },

  async listReleases(appId: string, options: { projectId?: string | null } = {}): Promise<AppRelease[]> {
    const readTicket = createDetailListReadTicket();
    const scope = await currentScope();
    const activeReadTicket = activateDetailListRead(readTicket, scope, appId, options.projectId, "releases");
    const cache = await hydrate(scope);
    const key = appScopedKey(appId, options.projectId);
    const localCached = await hasCachedArraySection(
      scope,
      cache.releases,
      key,
      appId,
      options.projectId,
      "releases",
    );
    const local = cache.releases[key] ?? [];
    const unavailableReason = await readAvailabilityReason();
    if (unavailableReason) {
      if (isCurrentDetailListRead(activeReadTicket)) {
        setDetailReadStatus(
          scope,
          appId,
          options.projectId,
          "releases",
          statusFromCache(localCached, unavailableReason),
        );
      }
      return local;
    }
    try {
      const remote = await appsApi.getReleases(appId, options.projectId);
      if (isCurrentDetailListRead(activeReadTicket)) {
        await commitCacheUpdate(
          scope,
          (latestCache) => {
            if (!isCurrentDetailListRead(activeReadTicket)) return null;
            const next = shallowCopyCache(latestCache);
            next.releases = replaceRecord(latestCache.releases, key, remote);
            return { cache: next, replace: { kind: "releases", appId } };
          },
          async () => {
            if (!isCurrentDetailListRead(activeReadTicket)) return;
            await updateEmptyFetchMarker(scope, appId, options.projectId, "releases", remote.length === 0);
            if (isCurrentDetailListRead(activeReadTicket)) {
              setDetailReadStatus(scope, appId, options.projectId, "releases", { state: "fresh", reason: null });
            }
          },
        );
      }
      return remote;
    } catch (error) {
      if (isCurrentDetailListRead(activeReadTicket)) {
        setDetailReadStatus(
          scope,
          appId,
          options.projectId,
          "releases",
          statusFromCache(localCached, readErrorReason(error)),
        );
      }
      return local;
    }
  },

  async listJobs(appId: string, options: { projectId?: string | null } = {}): Promise<AppJob[]> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const key = appScopedKey(appId, options.projectId);
    const localCached = await hasCachedArraySection(
      scope,
      cache.jobs,
      key,
      appId,
      options.projectId,
      "jobs",
    );
    const local = cache.jobs[key] ?? [];
    const unavailableReason = await readAvailabilityReason();
    if (unavailableReason) {
      setDetailReadStatus(
        scope,
        appId,
        options.projectId,
        "jobs",
        statusFromCache(localCached, unavailableReason),
      );
      return local;
    }
    try {
      const remote = await appsApi.getJobs(appId, options.projectId);
      await commitCacheUpdate(
        scope,
        (latestCache) => ({
          cache: {
            ...shallowCopyCache(latestCache),
            jobs: replaceRecord(latestCache.jobs, key, remote),
          },
          replace: { kind: "jobs", appId, projectId: options.projectId },
        }),
        async () => {
          await updateEmptyFetchMarker(scope, appId, options.projectId, "jobs", remote.length === 0);
          setDetailReadStatus(scope, appId, options.projectId, "jobs", { state: "fresh", reason: null });
        },
      );
      return remote;
    } catch (error) {
      setDetailReadStatus(
        scope,
        appId,
        options.projectId,
        "jobs",
        statusFromCache(localCached, readErrorReason(error)),
      );
      return local;
    }
  },

  async startJob(
    appId: string,
    input: {
      target_key: string;
      job_type: AppJobType;
      project_id?: string | null;
      input_json?: Record<string, unknown>;
    },
    permission?: string | null,
  ): Promise<AppJob> {
    requireOnlineMutation(await canUseServer());
    const app = await this.getLocal(appId);
    assertRunnerPermission(permission ?? app?.permission);
    const scope = await currentScope();
    await hydrate(scope);
    const job = await appsApi.startJob(appId, input);
    const key = appScopedKey(appId, input.project_id);
    await commitCacheUpdate(scope, (latestCache) => {
      const jobs = [job, ...(latestCache.jobs[key] ?? []).filter((item) => item.id !== job.id)];
      return {
        cache: {
          ...shallowCopyCache(latestCache),
          jobs: replaceRecord(latestCache.jobs, key, jobs),
        },
      };
    });
    return job;
  },

  async stopJob(
    appId: string,
    jobId: string,
    options: { projectId?: string | null; permission?: string | null } = {},
  ): Promise<AppJob> {
    requireOnlineMutation(await canUseServer());
    const app = await this.getLocal(appId);
    assertRunnerPermission(options.permission ?? app?.permission);
    const scope = await currentScope();
    await hydrate(scope);
    const job = await appsApi.stopJob(appId, jobId, options.projectId);
    const key = appScopedKey(appId, options.projectId);
    await commitCacheUpdate(scope, (latestCache) => {
      const jobs = [job, ...(latestCache.jobs[key] ?? []).filter((item) => item.id !== job.id)];
      return {
        cache: {
          ...shallowCopyCache(latestCache),
          jobs: replaceRecord(latestCache.jobs, key, jobs),
        },
      };
    });
    return job;
  },

  async getJobLogs(
    appId: string,
    jobId: string,
    options: { projectId?: string | null } = {},
  ): Promise<{ job_id: string; logs: string } | null> {
    if (!(await canUseServer())) return null;
    try {
      return await appsApi.getJobLogs(appId, jobId, options.projectId);
    } catch {
      return null;
    }
  },

  async listProjectApps(projectId: string): Promise<ProjectAppBinding[]> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const local = cache.projectApps[projectId] ?? [];
    if (!(await canUseServer())) return local;
    try {
      const remote = await appsApi.getProjectApps(projectId);
      await commitCacheUpdate(scope, (latestCache) => ({
        cache: {
          ...shallowCopyCache(latestCache),
          projectApps: replaceRecord(latestCache.projectApps, projectId, remote),
        },
        replace: { kind: "projectApps", projectId },
      }));
      return remote;
    } catch {
      return local;
    }
  },

  async linkProjectApp(
    projectId: string,
    input: ProjectAppInput,
    permission?: string | null,
  ): Promise<ProjectAppBinding> {
    requireOnlineMutation(await canUseServer());
    assertRunnerPermission(permission);
    const scope = await currentScope();
    await hydrate(scope);
    const binding = await appsApi.linkProjectApp(projectId, input);
    await commitCacheUpdate(scope, (latestCache) => {
      const existing = latestCache.projectApps[projectId] ?? [];
      const nextBindings = [
        ...existing.filter((item) => item.app_id !== binding.app_id),
        binding,
      ];
      return {
        cache: {
          ...shallowCopyCache(latestCache),
          projectApps: replaceRecord(latestCache.projectApps, projectId, nextBindings),
        },
      };
    });
    return binding;
  },

  async updateProjectApp(
    projectId: string,
    appId: string,
    input: Partial<ProjectAppInput>,
    permission?: string | null,
  ): Promise<ProjectAppBinding> {
    requireOnlineMutation(await canUseServer());
    assertRunnerPermission(permission);
    const scope = await currentScope();
    await hydrate(scope);
    const binding = await appsApi.updateProjectApp(projectId, appId, input);
    await commitCacheUpdate(scope, (latestCache) => {
      const nextBindings = [
        ...(latestCache.projectApps[projectId] ?? []).filter((item) => item.app_id !== appId),
        binding,
      ];
      return {
        cache: {
          ...shallowCopyCache(latestCache),
          projectApps: replaceRecord(latestCache.projectApps, projectId, nextBindings),
        },
      };
    });
    return binding;
  },

  async unlinkProjectApp(
    projectId: string,
    appId: string,
    permission?: string | null,
  ): Promise<void> {
    requireOnlineMutation(await canUseServer());
    assertRunnerPermission(permission);
    const scope = await currentScope();
    await hydrate(scope);
    await appsApi.unlinkProjectApp(projectId, appId);
    await commitCacheUpdate(scope, (latestCache) => {
      const next = shallowCopyCache(latestCache);
      next.projectApps = replaceRecord(
        next.projectApps,
        projectId,
        (next.projectApps[projectId] ?? []).filter((item) => item.app_id !== appId),
      );
      return { cache: next };
    });
  },

  async listTaskApps(taskId: string): Promise<TaskAppLink[]> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const local = cache.taskApps[taskId] ?? [];
    if (!(await canUseServer())) return local;
    try {
      const remote = await appsApi.listTaskApps(taskId);
      await commitCacheUpdate(scope, (latestCache) => {
        const next = shallowCopyCache(latestCache);
        next.taskApps = replaceRecord(next.taskApps, taskId, remote);
        // Remove this task from every per-App projection before adding the
        // authoritative response. An empty response is meaningful and must not
        // leave deleted links visible in App detail.
        for (const appId of Object.keys(next.appTasks)) {
          next.appTasks[appId] = next.appTasks[appId].filter(
            (link) => link.task_id !== taskId,
          );
        }
        const appIds = new Set(remote.map((link) => link.app_id));
        for (const appId of appIds) {
          next.appTasks = replaceRecord(
            next.appTasks,
            appId,
            remote.filter((link) => link.app_id === appId),
          );
        }
        return { cache: next, replace: { kind: "taskApps", taskId } };
      });
      return remote;
    } catch {
      return local;
    }
  },

  async linkTaskApp(
    taskId: string,
    input: TaskAppInput,
    permission?: string | null,
  ): Promise<TaskAppLink> {
    requireOnlineMutation(await canUseServer());
    void permission;
    const scope = await currentScope();
    await hydrate(scope);
    const link = await appsApi.linkTaskApp(taskId, input);
    await commitCacheUpdate(scope, (latestCache) => {
      const existing = latestCache.taskApps[taskId] ?? [];
      const key = `${link.app_id}:${link.target_id ?? ""}:${link.relation_type}`;
      const nextLinks = [
        ...existing.filter(
          (item) => `${item.app_id}:${item.target_id ?? ""}:${item.relation_type}` !== key,
        ),
        link,
      ];
      const next = shallowCopyCache(latestCache);
      next.taskApps = replaceRecord(next.taskApps, taskId, nextLinks);
      next.appTasks = replaceRecord(
        next.appTasks,
        link.app_id,
        [...(next.appTasks[link.app_id] ?? []).filter((item) => item.id !== link.id), link],
      );
      return { cache: next };
    });
    return link;
  },

  async unlinkTaskApp(
    taskId: string,
    appId: string,
    options: {
      targetId?: string | null;
      relationType?: string | null;
      permission?: string | null;
    } = {},
  ): Promise<void> {
    requireOnlineMutation(await canUseServer());
    const scope = await currentScope();
    await hydrate(scope);
    await appsApi.unlinkTaskApp(taskId, appId, options);
    await commitCacheUpdate(scope, (latestCache) => {
      const next = shallowCopyCache(latestCache);
      next.taskApps = replaceRecord(
        next.taskApps,
        taskId,
        (next.taskApps[taskId] ?? []).filter(
          (item) =>
            item.app_id !== appId ||
            (options.targetId !== undefined && item.target_id !== options.targetId) ||
            (options.relationType !== undefined && item.relation_type !== options.relationType),
        ),
      );
      next.appTasks = {
        ...next.appTasks,
        [appId]: (next.appTasks[appId] ?? []).filter((item) =>
          item.task_id !== taskId ||
          (options.targetId !== undefined && item.target_id !== options.targetId) ||
          (options.relationType !== undefined && item.relation_type !== options.relationType),
        ),
      };
      return { cache: next };
    });
  },

  async listAppTasks(
    appId: string,
    projectId: string,
    relationType?: string,
  ): Promise<Array<TaskAppLink & { task?: Record<string, unknown> }>> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const local = (cache.appTasks[appId] ?? []).filter(
      (link) => !relationType || link.relation_type === relationType,
    );
    if (!(await canUseServer())) return local;
    try {
      const remote = await appsApi.listAppTasks(appId, projectId, relationType);
      await commitCacheUpdate(scope, (latestCache) => {
        const next = shallowCopyCache(latestCache);
        next.appTasks = replaceRecord(next.appTasks, appId, remote);
        for (const taskId of Object.keys(next.taskApps)) {
          next.taskApps[taskId] = next.taskApps[taskId].filter(
            (link) => link.app_id !== appId,
          );
        }
        for (const link of remote) {
          next.taskApps[link.task_id] = [
            ...(next.taskApps[link.task_id] ?? []).filter(
              (existing) => existing.id !== link.id,
            ),
            link,
          ];
        }
        return { cache: next, replace: { kind: "appTaskLinks", appId } };
      });
      return remote;
    } catch {
      return local;
    }
  },

  permissionAtLeast,
};

export type AppsRepository = typeof appsRepo;

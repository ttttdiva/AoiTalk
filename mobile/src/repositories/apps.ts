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
  permissionAtLeast,
  type AppContext,
  type AppFile,
  type AppFileContent,
  type AppJob,
  type AppJobType,
  type AppRelease,
  type AppSummary,
  type AppTarget,
  type CreateAppInput,
  type ProjectAppBinding,
  type ProjectAppInput,
  type TaskAppInput,
  type TaskAppLink,
} from "../lib/apps-api";
import { getToken, getTokenAuthScope } from "../lib/auth";
import { useNetworkStore } from "../stores/network";
import { getDb, schema } from "../db/client";
import { ensureSchema } from "../db/migrate";

const STORAGE_PREFIX = "aoitalk:apps-cache:v1:";

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

// The in-memory mirror avoids repeatedly parsing AsyncStorage while a screen is
// focused. It is still keyed by auth scope and hydrated lazily from storage.
const memoryCache = new Map<string, AppsCacheState>();
const hydrateInFlight = new Map<string, Promise<AppsCacheState>>();

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

async function persist(
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
  const network = useNetworkStore.getState();
  return Boolean(token) && network.online;
}

function requireOnlineMutation(online: boolean): void {
  if (!online) throw new AppsOfflineMutationError();
}

/** Clear all in-memory entries. Persistent entries are scoped and intentionally retained. */
export function resetAppsRepositoryMemory(): void {
  memoryCache.clear();
  hydrateInFlight.clear();
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
  const appKeys = keys.filter((key) => key.startsWith(STORAGE_PREFIX));
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
      const next = shallowCopyCache(cache);
      // A project-scoped list is authoritative only for that projection. Keep
      // all-app cache rows that belong to other projects to avoid cross-view loss.
      if (options.projectId) {
        const previousProjectIds = new Set(
          next.projectViews[options.projectId] ?? [],
        );
        const remoteIds = new Set(remote.map((item) => item.id));
        next.projectViews = replaceRecord(next.projectViews, options.projectId, [
          ...remoteIds,
        ]);
        next.apps = [
          ...next.apps.filter(
            (item) =>
              !previousProjectIds.has(item.id) &&
              !item.related_project_ids?.includes(options.projectId as string) &&
              item.origin_project_id !== options.projectId,
          ).filter((item) => !remoteIds.has(item.id)),
          ...remote,
        ];
        const staleIds = [...previousProjectIds].filter((id) => {
          if (remoteIds.has(id)) return false;
          const existing = cache.apps.find((item) => item.id === id);
          return !existing?.related_project_ids?.some(
            (relatedId) => relatedId !== options.projectId,
          ) && existing?.origin_project_id !== options.projectId;
        });
        await persist(
          scope,
          next,
          staleIds.length > 0 ? { kind: "appsIds", ids: staleIds } : undefined,
        );
        return remote;
      } else {
        // Empty is meaningful: it removes stale/ghost Apps for this scope.
        next.apps = remote;
      }
      if (!options.projectId) {
        await persist(scope, next, { kind: "apps" });
      }
      return options.projectId ? remote : next.apps;
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
      await persist(scope, { ...shallowCopyCache(cache), apps: upsertApp(cache.apps, remote) });
      return remote;
    } catch {
      return local;
    }
  },

  async create(input: CreateAppInput): Promise<AppSummary> {
    requireOnlineMutation(await canUseServer());
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const app = await appsApi.create(input);
    await persist(scope, { ...shallowCopyCache(cache), apps: upsertApp(cache.apps, app) });
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
    const cache = await hydrate(scope);
    const app = await appsApi.update(appId, input, options.projectId);
    await persist(scope, { ...shallowCopyCache(cache), apps: upsertApp(cache.apps, app) });
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
    const cache = await hydrate(scope);
    const archived = await appsApi.archive(appId, options.projectId);
    const next = shallowCopyCache(cache);
    next.apps = next.apps.filter((item) => item.id !== appId);
    await persist(scope, next, { kind: "appsIds", ids: [appId] });
    return archived;
  },

  async getContext(
    appId: string,
    options: { projectId?: string | null; force?: boolean } = {},
  ): Promise<AppContext | null> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const contextKey = appScopedKey(appId, options.projectId);
    const local = cache.contexts[contextKey] ?? null;
    if (!(await canUseServer())) return local;
    try {
      const context = await appsApi.getContext(appId, options.projectId);
      const next = shallowCopyCache(cache);
      next.contexts = replaceRecord(next.contexts, contextKey, context);
      next.apps = upsertApp(next.apps, context.app);
      if (context.targets) next.targets = replaceRecord(next.targets, contextKey, context.targets);
      if (context.releases) next.releases = replaceRecord(next.releases, contextKey, context.releases);
      await persist(scope, next, { kind: "context", appId, projectKey: appProjectKey(options.projectId) });
      return context;
    } catch {
      return local;
    }
  },

  async listTargets(appId: string, options: { projectId?: string | null } = {}): Promise<AppTarget[]> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const key = appScopedKey(appId, options.projectId);
    const local = cache.targets[key] ?? [];
    if (!(await canUseServer())) return local;
    try {
      const remote = await appsApi.getTargets(appId, options.projectId);
      await persist(scope, { ...shallowCopyCache(cache), targets: replaceRecord(cache.targets, key, remote) }, { kind: "targets", appId });
      return remote;
    } catch {
      return local;
    }
  },

  async listFiles(appId: string, options: { projectId?: string | null } = {}): Promise<AppFile[]> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const key = appScopedKey(appId, options.projectId);
    const local = cache.files[key] ?? [];
    if (!(await canUseServer())) return local;
    try {
      const remote = await appsApi.getFiles(appId, options.projectId);
      await persist(scope, { ...shallowCopyCache(cache), files: replaceRecord(cache.files, key, remote) }, { kind: "files", appId, projectKey: appProjectKey(options.projectId) });
      return remote;
    } catch {
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
      await persist(scope, { ...shallowCopyCache(cache), fileContents: replaceRecord(cache.fileContents, key, remote) });
      return remote;
    } catch {
      return local;
    }
  },

  async listReleases(appId: string, options: { projectId?: string | null } = {}): Promise<AppRelease[]> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const key = appScopedKey(appId, options.projectId);
    const local = cache.releases[key] ?? [];
    if (!(await canUseServer())) return local;
    try {
      const remote = await appsApi.getReleases(appId, options.projectId);
      await persist(scope, { ...shallowCopyCache(cache), releases: replaceRecord(cache.releases, key, remote) }, { kind: "releases", appId });
      return remote;
    } catch {
      return local;
    }
  },

  async listJobs(appId: string, options: { projectId?: string | null } = {}): Promise<AppJob[]> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const key = appScopedKey(appId, options.projectId);
    const local = cache.jobs[key] ?? [];
    if (!(await canUseServer())) return local;
    try {
      const remote = await appsApi.getJobs(appId, options.projectId);
      await persist(scope, { ...shallowCopyCache(cache), jobs: replaceRecord(cache.jobs, key, remote) }, { kind: "jobs", appId, projectId: options.projectId });
      return remote;
    } catch {
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
    const cache = await hydrate(scope);
    const job = await appsApi.startJob(appId, input);
    const key = appScopedKey(appId, input.project_id);
    const jobs = [job, ...(cache.jobs[key] ?? []).filter((item) => item.id !== job.id)];
    await persist(scope, { ...shallowCopyCache(cache), jobs: replaceRecord(cache.jobs, key, jobs) });
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
    const cache = await hydrate(scope);
    const job = await appsApi.stopJob(appId, jobId, options.projectId);
    const key = appScopedKey(appId, options.projectId);
    const jobs = [job, ...(cache.jobs[key] ?? []).filter((item) => item.id !== job.id)];
    await persist(scope, { ...shallowCopyCache(cache), jobs: replaceRecord(cache.jobs, key, jobs) });
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
      await persist(scope, { ...shallowCopyCache(cache), projectApps: replaceRecord(cache.projectApps, projectId, remote) }, { kind: "projectApps", projectId });
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
    const cache = await hydrate(scope);
    const binding = await appsApi.linkProjectApp(projectId, input);
    const existing = cache.projectApps[projectId] ?? [];
    const nextBindings = [
      ...existing.filter((item) => item.app_id !== binding.app_id),
      binding,
    ];
    await persist(scope, { ...shallowCopyCache(cache), projectApps: replaceRecord(cache.projectApps, projectId, nextBindings) });
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
    const cache = await hydrate(scope);
    const binding = await appsApi.updateProjectApp(projectId, appId, input);
    const nextBindings = [
      ...(cache.projectApps[projectId] ?? []).filter((item) => item.app_id !== appId),
      binding,
    ];
    await persist(
      scope,
      {
        ...shallowCopyCache(cache),
        projectApps: replaceRecord(cache.projectApps, projectId, nextBindings),
      },
    );
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
    const cache = await hydrate(scope);
    await appsApi.unlinkProjectApp(projectId, appId);
    const next = shallowCopyCache(cache);
    next.projectApps = replaceRecord(
      next.projectApps,
      projectId,
      (next.projectApps[projectId] ?? []).filter((item) => item.app_id !== appId),
    );
    await persist(scope, next);
  },

  async listTaskApps(taskId: string): Promise<TaskAppLink[]> {
    const scope = await currentScope();
    const cache = await hydrate(scope);
    const local = cache.taskApps[taskId] ?? [];
    if (!(await canUseServer())) return local;
    try {
      const remote = await appsApi.listTaskApps(taskId);
      const next = shallowCopyCache(cache);
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
      await persist(scope, next, { kind: "taskApps", taskId });
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
    const cache = await hydrate(scope);
    const link = await appsApi.linkTaskApp(taskId, input);
    const existing = cache.taskApps[taskId] ?? [];
    const key = `${link.app_id}:${link.target_id ?? ""}:${link.relation_type}`;
    const nextLinks = [
      ...existing.filter(
        (item) => `${item.app_id}:${item.target_id ?? ""}:${item.relation_type}` !== key,
      ),
      link,
    ];
    const next = shallowCopyCache(cache);
    next.taskApps = replaceRecord(next.taskApps, taskId, nextLinks);
    next.appTasks = replaceRecord(
      next.appTasks,
      link.app_id,
      [...(next.appTasks[link.app_id] ?? []).filter((item) => item.id !== link.id), link],
    );
    await persist(scope, next);
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
    const cache = await hydrate(scope);
    await appsApi.unlinkTaskApp(taskId, appId, options);
    const next = shallowCopyCache(cache);
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
    await persist(scope, next);
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
      const next = shallowCopyCache(cache);
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
      await persist(scope, next, { kind: "appTaskLinks", appId });
      return remote;
    } catch {
      return local;
    }
  },

  permissionAtLeast,
};

export type AppsRepository = typeof appsRepo;

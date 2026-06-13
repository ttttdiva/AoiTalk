/**
 * Project Repository.
 *
 * M1 policy:
 *   - Reads: prefer local cache; if empty and online, fall back to remote
 *     and upsert into SQLite. Subsequent online reads refresh in the
 *     background.
 *   - Writes: not supported in mobile yet (mobile can only read projects
 *     at M1, per design doc). M2+ will wire via outbox.
 *
 * Types returned are the on-the-wire API shape (`types/api.Project`) so
 * existing UI code continues to work unchanged.
 */

import { and, eq, isNull } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { getToken } from "../lib/auth";
import { projectApi } from "../lib/project-api";
import { useNetworkStore } from "../stores/network";
import type { Project as ApiProject } from "../types/api";
import { enqueueOutbox, randomId } from "./outbox";

type DbProject = typeof schema.projects.$inferSelect;
const DEFAULT_LOCAL_PROJECT_ID = "local-default-project";

function toApiShape(row: DbProject): ApiProject {
  const metadata =
    row.projectMetadata && typeof row.projectMetadata === "object"
      ? (row.projectMetadata as Record<string, unknown>)
      : null;
  const color =
    metadata && typeof metadata.color === "string" ? metadata.color : null;

  return {
    id: row.id,
    name: row.name,
    slug: row.slug ?? "",
    description: row.description ?? null,
    color,
    metadata,
    owner_id: row.ownerId ?? null,
    space_id: row.spaceId ?? null,
    storage_quota_mb: row.storageQuotaMb ?? undefined,
    storage_used_mb: row.storageUsedMb ?? undefined,
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
    deleted_at: row.deletedAt ?? null,
  };
}

async function canUseServer(): Promise<boolean> {
  const network = useNetworkStore.getState();
  return network.online && network.serverReachable && Boolean(await getToken());
}

async function ensureAnonymousDefaultProject(): Promise<void> {
  if (await getToken()) return;

  const db = getDb();
  const rows = await db
    .select()
    .from(schema.projects)
    .where(isNull(schema.projects.deletedAt));
  if (rows.length > 0) return;

  const now = new Date().toISOString();
  await db
    .insert(schema.projects)
    .values({
      id: DEFAULT_LOCAL_PROJECT_ID,
      name: "ローカル",
      slug: null,
      description: "匿名モード用のローカルプロジェクト",
      ownerId: null,
      spaceId: null,
      storageQuotaMb: null,
      storageUsedMb: null,
      projectMetadata: { local_only: true },
      createdAt: now,
      updatedAt: now,
      deletedAt: null,
    })
    .onConflictDoUpdate({
      target: schema.projects.id,
      set: {
        deletedAt: null,
        updatedAt: now,
      },
    });
}

function buildLocalProject(
  data: {
    name: string;
    description?: string | null;
    aliases?: string[];
    allow_join_requests?: boolean;
    storage_quota_mb?: number;
    project_metadata?: Record<string, unknown> | null;
  },
  id = randomId(),
): ApiProject {
  const now = new Date().toISOString();
  return {
    id,
    name: data.name,
    slug: "",
    description: data.description ?? null,
    color:
      data.project_metadata && typeof data.project_metadata.color === "string"
        ? data.project_metadata.color
        : null,
    metadata: data.project_metadata ?? null,
    owner_id: null,
    space_id: null,
    storage_quota_mb: data.storage_quota_mb,
    storage_used_mb: undefined,
    created_at: now,
    updated_at: now,
    deleted_at: null,
  };
}

export async function applyRemoteProjects(list: ApiProject[]): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const p of list) {
    const metadata =
      (p as { metadata?: Record<string, unknown> | null }).metadata ??
      (p as { project_metadata?: Record<string, unknown> | null })
        .project_metadata ??
      (p.color ? { color: p.color } : null);
    await db
      .insert(schema.projects)
      .values({
        id: p.id,
        name: p.name,
        slug: p.slug ?? null,
        description: (p as { description?: string | null }).description ?? null,
        ownerId: (p as { owner_id?: string }).owner_id ?? null,
        spaceId: (p as { space_id?: string | null }).space_id ?? null,
        storageQuotaMb:
          (p as { storage_quota_mb?: number | null }).storage_quota_mb ?? null,
        storageUsedMb:
          (p as { storage_used_mb?: number | null }).storage_used_mb ?? null,
        projectMetadata: metadata,
        createdAt: (p as { created_at?: string }).created_at ?? now,
        updatedAt: (p as { updated_at?: string }).updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.projects.id,
        set: {
          name: p.name,
          slug: p.slug ?? null,
          description:
            (p as { description?: string | null }).description ?? null,
          ownerId: (p as { owner_id?: string }).owner_id ?? null,
          spaceId: (p as { space_id?: string | null }).space_id ?? null,
          storageQuotaMb:
            (p as { storage_quota_mb?: number | null }).storage_quota_mb ??
            null,
          storageUsedMb:
            (p as { storage_used_mb?: number | null }).storage_used_mb ?? null,
          projectMetadata: metadata,
          updatedAt: (p as { updated_at?: string }).updated_at ?? now,
          deletedAt: null,
        },
      });
  }
}

export async function applyProjectTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    await db
      .update(schema.projects)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.projects.id, item.id));
  }
}

export const projectsRepo = {
  /** Read from local cache (deleted_at IS NULL). */
  async listLocal(): Promise<ApiProject[]> {
    await ensureAnonymousDefaultProject();
    const db = getDb();
    const rows = await db
      .select()
      .from(schema.projects)
      .where(isNull(schema.projects.deletedAt));
    return rows.map(toApiShape);
  },

  /** Online returns fresh data; offline or failed refresh falls back to local cache. */
  async list(): Promise<ApiProject[]> {
    const local = await this.listLocal();
    if (await canUseServer()) {
      try {
        return await this.refresh();
      } catch (error) {
        if (local.length === 0) {
          throw error;
        }
        return local;
      }
    }
    return local;
  },

  /** Force a remote fetch and upsert into local cache. Returns fresh list. */
  async refresh(): Promise<ApiProject[]> {
    const list = await projectApi.list();
    await applyRemoteProjects(list);
    return list;
  },

  /** Local get by id. */
  async getLocal(id: string): Promise<ApiProject | null> {
    const db = getDb();
    const rows = await db
      .select()
      .from(schema.projects)
      .where(
        and(eq(schema.projects.id, id), isNull(schema.projects.deletedAt)),
      );
    return rows[0] ? toApiShape(rows[0]) : null;
  },

  async create(data: {
    name: string;
    description?: string | null;
    aliases?: string[];
    allow_join_requests?: boolean;
    storage_quota_mb?: number;
    project_metadata?: Record<string, unknown> | null;
  }): Promise<ApiProject> {
    if (await canUseServer()) {
      try {
        const created = await projectApi.create(data);
        await applyRemoteProjects([created]);
        return created;
      } catch {
        // Fall back to local-first below.
      }
    }

    const local = buildLocalProject(data);
    await applyRemoteProjects([local]);
    if (await getToken()) {
      await enqueueOutbox({
        table: "projects",
        action: "create",
        entityId: local.id,
        payload: {
          name: local.name,
          description: local.description,
          storage_quota_mb: local.storage_quota_mb ?? null,
          project_metadata: local.metadata ?? null,
        },
      });
    }
    return local;
  },

  async update(
    projectId: string,
    data: {
      name?: string;
      description?: string | null;
      aliases?: string[];
      allow_join_requests?: boolean;
      storage_quota_mb?: number;
      project_metadata?: Record<string, unknown> | null;
    },
  ): Promise<ApiProject> {
    if (await canUseServer()) {
      try {
        const updated = await projectApi.update(projectId, data);
        await applyRemoteProjects([updated]);
        return updated;
      } catch {
        // Fall back to local-first below.
      }
    }

    const db = getDb();
    const now = new Date().toISOString();
    const before = await this.getLocal(projectId);
    const patch: Partial<typeof schema.projects.$inferInsert> = {
      updatedAt: now,
    };
    if ("name" in data && data.name != null) patch.name = data.name;
    if ("description" in data) patch.description = data.description ?? null;
    if ("storage_quota_mb" in data)
      patch.storageQuotaMb = data.storage_quota_mb ?? null;
    if ("project_metadata" in data) {
      const previousMetadata =
        before?.metadata && typeof before.metadata === "object"
          ? before.metadata
          : {};
      patch.projectMetadata = {
        ...previousMetadata,
        ...(data.project_metadata ?? {}),
      };
    }
    await db
      .update(schema.projects)
      .set(patch)
      .where(eq(schema.projects.id, projectId));
    const local = await this.getLocal(projectId);
    if (!local) {
      throw new Error("Project not found");
    }
    if (before && (await getToken())) {
      await enqueueOutbox({
        table: "projects",
        action: "update",
        entityId: projectId,
        payload: data,
        baseUpdatedAt: before.updated_at ?? null,
      });
    }
    return local;
  },

  async delete(projectId: string): Promise<void> {
    let shouldQueue = Boolean(await getToken());
    const before = await this.getLocal(projectId);
    if (await canUseServer()) {
      try {
        await projectApi.delete(projectId);
        shouldQueue = false;
      } catch {
        // Fall back to local-first below.
        shouldQueue = true;
      }
    }

    const db = getDb();
    const now = new Date().toISOString();
    await db
      .update(schema.projects)
      .set({ deletedAt: now, updatedAt: now })
      .where(eq(schema.projects.id, projectId));
    if (shouldQueue) {
      await enqueueOutbox({
        table: "projects",
        action: "delete",
        entityId: projectId,
        payload: {},
        baseUpdatedAt: before?.updated_at ?? null,
      });
    }
  },
};

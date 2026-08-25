/**
 * Project selection store (zustand).
 *
 * Replaces the legacy ProjectContext. The AuthContext is kept as a Context
 * because it exposes side-effectful auth calls; project state is pure data
 * so zustand fits better and re-renders fewer components.
 *
 * Selected project id is persisted to SecureStore via existing `auth.ts`
 * helpers so the UX is unchanged.
 */

import { create } from "zustand";
import { eq, isNull } from "drizzle-orm";
import {
  saveSelectedProjectId,
  getSelectedProjectId,
  saveSelectedSpaceId,
  getSelectedSpaceId,
  getToken,
  getTokenAuthScope,
} from "../lib/auth";
import { enqueueAuthScopeExclusive } from "../lib/auth-scope-queue";
import { taskApi } from "../lib/task-api";
import { getDb, schema } from "../db/client";
import { projectsRepo } from "../repositories/projects";
import { shouldRunFullFetch } from "../repositories/tasks";
import { useNetworkStore } from "./network";
import type { Project, Space } from "../types/api";

export interface RefreshProjectsOptions {
  force?: boolean;
  localOnly?: boolean;
}

interface ProjectStoreState {
  spaces: Space[];
  projects: Project[];
  selectedSpaceId: string | null;
  selectedProjectId: string | null;
  loaded: boolean;
  refreshProjects: (options?: RefreshProjectsOptions) => Promise<void>;
  setSelectedSpaceId: (id: string) => Promise<void>;
  setSelectedProjectId: (id: string | null) => Promise<void>;
  reset: () => void;
}

// projects / spaces のフル取得も 60秒 throttle する（デルタ同期が鮮度を担保）。
let lastProjectFullFetchAt: number | undefined;

// refresh の await 後に古い認証スコープの投影を復元しないための世代。
// 新しい refresh の開始も古い refresh を無効化するので、reset を伴わない
// 重複呼び出しでも後勝ちの投影だけが state に反映される。
let projectRefreshGeneration = 0;

type DbSpace = typeof schema.spaces.$inferSelect;

function spaceRowToApi(row: DbSpace): Space {
  return {
    id: row.id,
    name: row.name,
    slug: row.slug ?? "",
    description: row.description ?? null,
    color: row.color ?? null,
    owner_id: row.ownerId ?? null,
    sort_order: row.sortOrder ?? undefined,
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
  };
}

/** spaces テーブルからローカル先読みする（削除済みは除外）。 */
async function listLocalSpaces(): Promise<Space[]> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.spaces)
    .where(isNull(schema.spaces.deletedAt));
  return rows.map(spaceRowToApi).sort((a, b) => {
    const aSort = a.sort_order ?? Number.POSITIVE_INFINITY;
    const bSort = b.sort_order ?? Number.POSITIVE_INFINITY;
    if (aSort !== bSort) return aSort - bSort;
    return a.name.localeCompare(b.name);
  });
}

type AuthScopeSnapshot = {
  token: string | null;
  authScope: string;
};

async function captureAuthScopeSnapshot(): Promise<AuthScopeSnapshot> {
  const token = await getToken();
  return { token, authScope: getTokenAuthScope(token) };
}

async function isCurrentAuthScope(snapshot: AuthScopeSnapshot): Promise<boolean> {
  const token = await getToken();
  return (
    token === snapshot.token && getTokenAuthScope(token) === snapshot.authScope
  );
}

/**
 * サーバーの listSpaces 全件で spaces テーブルを置き換える。
 * 返らなかった space はローカルでも tombstone し、ゴースト表示を防ぐ。
 */
async function replaceLocalSpaces(
  list: Space[],
  isCurrentGeneration: () => boolean,
): Promise<boolean> {
  if (!isCurrentGeneration()) return false;
  const db = getDb();
  const now = new Date().toISOString();
  const ids = new Set(list.map((space) => space.id));
  for (const space of list) {
    if (!isCurrentGeneration()) return false;
    await db
      .insert(schema.spaces)
      .values({
        id: space.id,
        name: space.name,
        slug: space.slug ?? null,
        description: space.description ?? null,
        color: space.color ?? null,
        ownerId: space.owner_id ?? null,
        sortOrder: space.sort_order ?? null,
        createdAt: space.created_at ?? now,
        updatedAt: space.updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.spaces.id,
        set: {
          name: space.name,
          slug: space.slug ?? null,
          description: space.description ?? null,
          color: space.color ?? null,
          ownerId: space.owner_id ?? null,
          sortOrder: space.sort_order ?? null,
          updatedAt: space.updated_at ?? now,
          deletedAt: null,
        },
      });
    if (!isCurrentGeneration()) return false;
  }
  if (!isCurrentGeneration()) return false;
  const existing = await db
    .select()
    .from(schema.spaces)
    .where(isNull(schema.spaces.deletedAt));
  if (!isCurrentGeneration()) return false;
  for (const row of existing) {
    if (!isCurrentGeneration()) return false;
    if (!ids.has(row.id)) {
      await db
        .update(schema.spaces)
        .set({ deletedAt: now, updatedAt: now })
        .where(eq(schema.spaces.id, row.id));
      if (!isCurrentGeneration()) return false;
    }
  }
  return true;
}

/**
 * Keep the spaces request and every SQLite replacement write on the shared
 * auth-scope queue. null means the captured account is no longer current.
 */
async function refreshRemoteSpaces(
  isCurrentGeneration: () => boolean,
): Promise<Space[] | null> {
  const snapshot = await captureAuthScopeSnapshot();
  return enqueueAuthScopeExclusive(async () => {
    if (!isCurrentGeneration() || !(await isCurrentAuthScope(snapshot))) {
      return null;
    }

    const list = await taskApi.listSpaces();
    if (!isCurrentGeneration() || !(await isCurrentAuthScope(snapshot))) {
      return null;
    }

    const replaced = await replaceLocalSpaces(list, isCurrentGeneration);
    if (!replaced || !isCurrentGeneration()) return null;
    return listLocalSpaces();
  });
}

async function saveProjectSelectionForGeneration(
  id: string,
  isCurrentGeneration: () => boolean,
): Promise<boolean> {
  if (!isCurrentGeneration()) return false;
  await saveSelectedProjectId(id);
  return isCurrentGeneration();
}

export const useProjectStore = create<ProjectStoreState>((set, get) => ({
  spaces: [],
  projects: [],
  selectedSpaceId: null,
  selectedProjectId: null,
  loaded: false,

  refreshProjects: async (options?: RefreshProjectsOptions) => {
    const generation = ++projectRefreshGeneration;
    const isCurrentGeneration = () =>
      generation === projectRefreshGeneration;

    try {
      // ローカル先読みは即時表示。projects/spaces とも SQLite を正とする。
      let list = await projectsRepo.listLocal();
      if (!isCurrentGeneration()) return;
      let spaces = await listLocalSpaces();
      if (!isCurrentGeneration()) return;
      set({ spaces, projects: list, loaded: true });

      const hasToken = Boolean(await getToken());
      if (!isCurrentGeneration()) return;
      const restoreSelection = async () => {
        const storedSpaceId = await getSelectedSpaceId();
        if (!isCurrentGeneration()) return false;
        const storedId = await getSelectedProjectId();
        if (!isCurrentGeneration()) return false;
        const currentProjectId = get().selectedProjectId;
        const currentSpaceId = get().selectedSpaceId;
        if (storedSpaceId && spaces.find((s) => s.id === storedSpaceId)) {
          if (currentSpaceId !== storedSpaceId) {
            set({ selectedSpaceId: storedSpaceId, selectedProjectId: null });
          }
          return true;
        }
        if (storedId && list.find((p) => p.id === storedId)) {
          if (currentProjectId !== storedId) {
            set({ selectedProjectId: storedId, selectedSpaceId: null });
          }
        } else if (!hasToken && list.length > 0) {
          const firstId = list[0].id;
          if (currentProjectId !== firstId) {
            set({ selectedProjectId: firstId, selectedSpaceId: null });
            if (
              !(await saveProjectSelectionForGeneration(
                firstId,
                isCurrentGeneration,
              ))
            ) {
              return false;
            }
            if (!isCurrentGeneration()) return false;
          }
        } else if (currentProjectId !== null || storedId) {
          set({ selectedProjectId: null, selectedSpaceId: null });
          if (
            !(await saveProjectSelectionForGeneration(
              "",
              isCurrentGeneration,
            ))
          ) {
            return false;
          }
          if (!isCurrentGeneration()) return false;
        }
        return true;
      };

      if (options?.localOnly) {
        await restoreSelection();
        if (!isCurrentGeneration()) return;
        return;
      }

      const network = useNetworkStore.getState();
      // serverReachable は直近の別API失敗で stale になり得る。端末がオンラインで
      // 認証済みなら一覧APIを直接試し、通信層の実応答で到達性を更新する。
      const canServer = hasToken && network.online;

      // フル取得は空初回・明示refresh・60秒throttleのみ。
      if (
        canServer &&
        shouldRunFullFetch(
          lastProjectFullFetchAt,
          Date.now(),
          list.length === 0,
          options?.force ?? false,
        )
      ) {
        const [freshSpaces, freshProjects] = await Promise.all([
          refreshRemoteSpaces(isCurrentGeneration).catch(() => null),
          projectsRepo.refresh().catch(() => null),
        ]);
        if (!isCurrentGeneration()) return;
        if (freshProjects) list = freshProjects;
        if (freshSpaces) spaces = freshSpaces;
        if (!isCurrentGeneration()) return;
        lastProjectFullFetchAt = Date.now();
        set({ spaces, projects: list });
      }

      await restoreSelection();
      if (!isCurrentGeneration()) return;
    } catch {
      // Offline / unauthenticated → leave state as-is; UI reads from local cache.
      if (isCurrentGeneration()) set({ loaded: true });
    }
  },

  setSelectedSpaceId: async (id: string) => {
    set({ selectedSpaceId: id, selectedProjectId: null });
    await saveSelectedSpaceId(id);
    await saveSelectedProjectId("");
  },

  setSelectedProjectId: async (id: string | null) => {
    set({ selectedProjectId: id, selectedSpaceId: null });
    await saveSelectedProjectId(id ?? "");
    await saveSelectedSpaceId("");
  },

  reset: () => {
    // 認証遷移では次回のフル取得を強制する。
    projectRefreshGeneration += 1;
    lastProjectFullFetchAt = undefined;
    set({
      spaces: [],
      projects: [],
      selectedSpaceId: null,
      selectedProjectId: null,
      loaded: false,
    });
  },
}));

/** Derived selector: the currently selected Project object, or null. */
export function useSelectedProject(): Project | null {
  return useProjectStore((s) =>
    s.selectedProjectId
      ? (s.projects.find((p) => p.id === s.selectedProjectId) ?? null)
      : null,
  );
}

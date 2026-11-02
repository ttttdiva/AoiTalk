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
import { isParticipatingProject, participatingScopeSpaces } from "../lib/project-list-visibility";
import { getDb, schema } from "../db/client";
import { projectsRepo } from "../repositories/projects";
import { shouldRunFullFetch } from "../repositories/tasks";
import { canAttemptAoiTalkServer } from "./network";
import type { Project, Space } from "../types/api";

export interface RefreshProjectsOptions {
  force?: boolean;
  localOnly?: boolean;
  /**
   * Re-read Projects from SQLite, then fetch only the Space catalog when it is
   * empty or a Project references a missing Space. Root sync uses this after
   * the delta pull so it does not add
   * a redundant canonical /api/projects request.
   */
  spacesOnly?: boolean;
}

interface ProjectStoreState {
  spaces: Space[];
  projects: Project[];
  selectedSpaceId: string | null;
  selectedProjectId: string | null;
  scopeHydrated: boolean;
  loaded: boolean;
  refreshProjects: (options?: RefreshProjectsOptions) => Promise<void>;
  setSelectedSpaceId: (id: string) => Promise<void>;
  setSelectedProjectId: (id: string | null) => Promise<void>;
  reset: () => void;
}

// Project の canonical full fetch は 60秒 throttle する。
// Space は delta sync 対象外なので別時刻で管理し、失敗時は成功扱いにしない。
let lastProjectFullFetchAt: number | undefined;
let lastSpaceFullFetchAt: number | undefined;

// refresh の await 後に古い認証スコープの投影を復元しないための世代。
// 新しい refresh の開始も古い refresh を無効化するので、reset を伴わない
// 重複呼び出しでも後勝ちの投影だけが state に反映される。
let projectRefreshGeneration = 0;

// SecureStore の読み取り中や remote refresh 待ちに行われた明示的な
// 範囲変更を、後段の restoreSelection が上書きしないための版。
let projectSelectionRevision = 0;

// SecureStore の選択保存は順序を保ち、古い選択の書き込みが新しい選択の
// 後から着地しないようにする。保存中の版は refresh の復元を抑止する。
let projectSelectionPersistenceTail: Promise<void> = Promise.resolve();
let pendingProjectSelectionPersistenceRevision: number | null = null;

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

function enqueueProjectSelectionPersistence(
  revision: number,
  projectId: string,
  spaceId: string,
): Promise<void> {
  pendingProjectSelectionPersistenceRevision = revision;
  const operation = projectSelectionPersistenceTail.then(async () => {
    if (revision !== projectSelectionRevision) return;
    await saveSelectedProjectId(projectId);
    if (revision !== projectSelectionRevision) return;
    await saveSelectedSpaceId(spaceId);
  });
  projectSelectionPersistenceTail = operation.then(
    () => {
      if (pendingProjectSelectionPersistenceRevision === revision) {
        pendingProjectSelectionPersistenceRevision = null;
      }
    },
    () => {
      if (pendingProjectSelectionPersistenceRevision === revision) {
        pendingProjectSelectionPersistenceRevision = null;
      }
    },
  );
  return operation;
}

async function saveSelectionForGeneration(
  projectId: string,
  spaceId: string,
  isCurrentGeneration: () => boolean,
  selectionRevision: number,
): Promise<boolean> {
  if (
    !isCurrentGeneration() ||
    selectionRevision !== projectSelectionRevision
  ) {
    return false;
  }
  await enqueueProjectSelectionPersistence(
    selectionRevision,
    projectId,
    spaceId,
  );
  return (
    isCurrentGeneration() &&
    selectionRevision === projectSelectionRevision
  );
}

export const useProjectStore = create<ProjectStoreState>((set, get) => ({
  spaces: [],
  projects: [],
  selectedSpaceId: null,
  selectedProjectId: null,
  scopeHydrated: false,
  loaded: false,

  refreshProjects: async (options?: RefreshProjectsOptions) => {
    // A local projection (including the post-sync Space-only pass) must not
    // cancel an already-running remote refresh. Only a newer full remote
    // refresh or reset/auth transition supersedes the previous generation.
    const generation =
      options?.localOnly || options?.spacesOnly
        ? projectRefreshGeneration
        : ++projectRefreshGeneration;
    const isCurrentGeneration = () =>
      generation === projectRefreshGeneration;
    const refreshSelectionRevision = projectSelectionRevision;

    try {
      // ローカル先読みは即時表示。projects/spaces とも SQLite を正とする。
      const token = await getToken();
      if (!isCurrentGeneration()) return;
      const authScope = getTokenAuthScope(token);
      let localProjects = await projectsRepo.listLocal();
      let list = localProjects.filter((project) =>
        isParticipatingProject(project, authScope),
      );
      if (!isCurrentGeneration()) return;
      let spaces = await listLocalSpaces();
      if (!isCurrentGeneration()) return;
      let visibleSpaces = participatingScopeSpaces(spaces, list, authScope);

      const hasToken = Boolean(token);
      if (!isCurrentGeneration()) return;
      const restoreSelection = async (options: {
        clearMissingProject: boolean;
        clearMissingSpace: boolean;
        publishCatalog?: { spaces: Space[]; projects: Project[] };
      }) => {
        const restoreRevision = projectSelectionRevision;
        const canApply = () =>
          isCurrentGeneration() &&
          restoreRevision === projectSelectionRevision &&
          refreshSelectionRevision === projectSelectionRevision;
        if (!isCurrentGeneration()) return false;
        const catalogPatch = options.publishCatalog
          ? {
              spaces: options.publishCatalog.spaces,
              projects: options.publishCatalog.projects,
              loaded: true,
            }
          : {};
        if (
          pendingProjectSelectionPersistenceRevision === projectSelectionRevision &&
          refreshSelectionRevision === projectSelectionRevision
        ) {
          if (options.publishCatalog && canApply()) {
            set({ ...catalogPatch, scopeHydrated: true });
          }
          return true;
        }
        let storedSpaceId: string | null;
        let storedId: string | null;
        try {
          storedSpaceId = await getSelectedSpaceId();
          if (!canApply()) return false;
          storedId = await getSelectedProjectId();
        } catch {
          // A SecureStore read failure must not block the canonical catalog
          // refresh or cause the in-memory selection to be cleared.
          // 未確定/エラー扱いとし、他scopeデータを勝手に見せないよう scopeHydrated: false を保つ。
          if (options.publishCatalog && canApply()) {
            set({ ...catalogPatch, scopeHydrated: false });
          }
          return false;
        }
        if (!canApply()) return false;
        const currentProjectId = get().selectedProjectId;
        const currentSpaceId = get().selectedSpaceId;
        const hasStoredSpace = Boolean(storedSpaceId);
        const hasStoredProject = Boolean(storedId);
        const storedSpace = storedSpaceId
          ? visibleSpaces.find((space) => space.id === storedSpaceId)
          : undefined;
        const storedProject = storedId
          ? list.find((project) => project.id === storedId)
          : undefined;
        const storedSpaceIsLocallyExcluded = Boolean(
          storedSpaceId &&
          spaces.some((space) => space.id === storedSpaceId) &&
          !visibleSpaces.some((space) => space.id === storedSpaceId) &&
          localProjects.some((project) => project.space_id === storedSpaceId),
        );

        if (storedSpace) {
          if (currentSpaceId !== storedSpaceId || options.publishCatalog) {
            set({
              ...catalogPatch,
              selectedSpaceId: storedSpaceId,
              selectedProjectId: null,
              scopeHydrated: true,
            });
          }
          return true;
        }

        // A saved Space is the user's more specific scope.  When its local
        // catalog is incomplete, keep the preference pending instead of
        // falling back to a different saved Project before canonical refresh.
        if (
          hasStoredSpace &&
          !options.clearMissingSpace &&
          !storedSpaceIsLocallyExcluded
        ) {
          if (options.publishCatalog) {
            set({
              ...catalogPatch,
              selectedSpaceId: null,
              selectedProjectId: null,
              scopeHydrated: false,
            });
          }
          return true;
        }

        if (storedProject) {
          if (currentProjectId !== storedId || options.publishCatalog) {
            set({
              ...catalogPatch,
              selectedProjectId: storedId,
              selectedSpaceId: null,
              scopeHydrated: true,
            });
          }
        } else if (
          hasStoredProject &&
          !options.clearMissingProject &&
          !localProjects.find((project) => project.id === storedId)
        ) {
          if (options.publishCatalog) {
            set({
              ...catalogPatch,
              selectedProjectId: null,
              selectedSpaceId: null,
              scopeHydrated: false,
            });
          }
          return true;
        } else if (!hasToken && !hasStoredProject && !hasStoredSpace && list.length > 0) {
          const firstId = list[0].id;
          if (currentProjectId !== firstId || options.publishCatalog) {
            set({
              ...catalogPatch,
              selectedProjectId: firstId,
              selectedSpaceId: null,
              scopeHydrated: true,
            });
            if (
              !(await saveSelectionForGeneration(
                firstId,
                "",
                isCurrentGeneration,
                restoreRevision,
              ))
            ) {
              return false;
            }
            if (!canApply()) return false;
          }
        } else if (
          currentProjectId !== null ||
          currentSpaceId !== null ||
          hasStoredProject ||
          hasStoredSpace ||
          options.publishCatalog
        ) {
          if (!canApply()) return false;
          set({
            ...catalogPatch,
            selectedProjectId: null,
            selectedSpaceId: null,
            scopeHydrated: true,
          });
          if (hasStoredProject || hasStoredSpace) {
            if (
              !(await saveSelectionForGeneration(
                "",
                "",
                isCurrentGeneration,
                restoreRevision,
              ))
            ) {
              return false;
            }
            if (!canApply()) return false;
          }
        }
        return true;
      };

      // ローカルカタログと保存scopeを同時に公開する。
      // remote 完了を初期描画 gate に戻さず、ローカルで確定できる scope を即時確定する。
      await restoreSelection({
        clearMissingProject: !hasToken,
        clearMissingSpace: !hasToken,
        publishCatalog: { spaces: visibleSpaces, projects: list },
      });
      if (!isCurrentGeneration()) return;

      // `online` only describes Internet reachability.  The API can be on the
      // same LAN, so project refreshes use the actual AoiTalk network-path
      // gate; a recent transport failure remains a short-lived retry throttle.
      const canServer = hasToken && canAttemptAoiTalkServer();

      const now = Date.now();
      const force = options?.force ?? false;
      const projectFetchDue =
        !options?.spacesOnly &&
        shouldRunFullFetch(
          lastProjectFullFetchAt,
          now,
          list.length === 0,
          force,
        );
      const localSpaceIds = new Set(spaces.map((space) => space.id));
      const hasMissingReferencedSpace = list.some(
        (project) =>
          Boolean(project.space_id) && !localSpaceIds.has(project.space_id!),
      );
      // Root's spacesOnly pass is intentionally demand-driven: fill an empty
      // cache or a Project-referenced hole, but do not turn the 60s root sync
      // into an unconditional Space catalog poll. Normal/manual refreshes keep
      // the existing 60s freshness behavior.
      const spaceFetchDue =
        spaces.length === 0 ||
        hasMissingReferencedSpace ||
        (!options?.spacesOnly &&
          shouldRunFullFetch(lastSpaceFullFetchAt, now, false, force));

      let remoteProjects: Project[] | null = null;
      let remoteSpaces: Space[] | null = null;

      if (options?.localOnly || !canServer || !(projectFetchDue || spaceFetchDue)) {
        return;
      }

      if (canServer && (projectFetchDue || spaceFetchDue)) {
        const [freshSpaces, freshProjects] = await Promise.all([
          spaceFetchDue
            ? refreshRemoteSpaces(isCurrentGeneration).catch(() => null)
            : Promise.resolve(null),
          projectFetchDue
            ? projectsRepo.refresh().catch(() => null)
            : Promise.resolve(null),
        ]);
        remoteSpaces = freshSpaces;
        remoteProjects = freshProjects;
        if (!isCurrentGeneration()) return;
        if (freshProjects) {
          localProjects = freshProjects;
          list = freshProjects.filter((project) =>
            isParticipatingProject(project, authScope),
          );
          // Failed / cancelled canonical reads must remain immediately
          // retryable instead of poisoning the throttle window.
          lastProjectFullFetchAt = Date.now();
        }
        if (freshSpaces) {
          spaces = freshSpaces;
          lastSpaceFullFetchAt = Date.now();
        }
        if (!isCurrentGeneration()) return;
        visibleSpaces = participatingScopeSpaces(spaces, list, authScope);
        set({
          spaces: visibleSpaces,
          projects: list,
        });
      }

      await restoreSelection({
        clearMissingProject: remoteProjects !== null,
        clearMissingSpace: remoteSpaces !== null,
      });
      if (!isCurrentGeneration()) return;
    } catch {
      // Offline / unauthenticated → leave state as-is; UI reads from local cache.
      if (isCurrentGeneration()) set({ loaded: true });
    }
  },

  setSelectedSpaceId: async (id: string) => {
    const selectionRevision = ++projectSelectionRevision;
    set({ selectedSpaceId: id, selectedProjectId: null, scopeHydrated: true });
    await enqueueProjectSelectionPersistence(selectionRevision, "", id);
  },

  setSelectedProjectId: async (id: string | null) => {
    const selectionRevision = ++projectSelectionRevision;
    set({ selectedProjectId: id, selectedSpaceId: null, scopeHydrated: true });
    await enqueueProjectSelectionPersistence(
      selectionRevision,
      id ?? "",
      "",
    );
  },

  reset: () => {
    // 認証遷移では次回のフル取得を強制する。
    projectRefreshGeneration += 1;
    projectSelectionRevision += 1;
    pendingProjectSelectionPersistenceRevision = null;
    lastProjectFullFetchAt = undefined;
    lastSpaceFullFetchAt = undefined;
    set({
      spaces: [],
      projects: [],
      selectedSpaceId: null,
      selectedProjectId: null,
      scopeHydrated: false,
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

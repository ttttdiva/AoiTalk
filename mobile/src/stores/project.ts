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
import {
  saveSelectedProjectId,
  getSelectedProjectId,
  saveSelectedSpaceId,
  getSelectedSpaceId,
  getToken,
} from "../lib/auth";
import { taskApi } from "../lib/task-api";
import { projectsRepo } from "../repositories/projects";
import { useNetworkStore } from "./network";
import type { Project, Space } from "../types/api";

interface ProjectStoreState {
  spaces: Space[];
  projects: Project[];
  selectedSpaceId: string | null;
  selectedProjectId: string | null;
  loaded: boolean;
  refreshProjects: () => Promise<void>;
  setSelectedSpaceId: (id: string) => Promise<void>;
  setSelectedProjectId: (id: string | null) => Promise<void>;
  reset: () => void;
}

export const useProjectStore = create<ProjectStoreState>((set, get) => ({
  spaces: [],
  projects: [],
  selectedSpaceId: null,
  selectedProjectId: null,
  loaded: false,

  refreshProjects: async () => {
    try {
      const hasToken = Boolean(await getToken());
      const network = useNetworkStore.getState();
      const [spaces, list] = await Promise.all([
        hasToken && network.online && network.serverReachable
          ? taskApi.listSpaces().catch(() => [] as Space[])
          : Promise.resolve([] as Space[]),
        projectsRepo.list(),
      ]);
      set({ spaces, projects: list, loaded: true });

      const storedSpaceId = await getSelectedSpaceId();
      const storedId = await getSelectedProjectId();
      const currentProjectId = get().selectedProjectId;
      const currentSpaceId = get().selectedSpaceId;
      if (storedSpaceId && spaces.find((s) => s.id === storedSpaceId)) {
        if (currentSpaceId !== storedSpaceId) {
          set({ selectedSpaceId: storedSpaceId, selectedProjectId: null });
        }
        return;
      }
      if (storedId && list.find((p) => p.id === storedId)) {
        if (currentProjectId !== storedId) {
          set({ selectedProjectId: storedId, selectedSpaceId: null });
        }
      } else if (!hasToken && list.length > 0) {
        const firstId = list[0].id;
        if (currentProjectId !== firstId) {
          set({ selectedProjectId: firstId, selectedSpaceId: null });
          await saveSelectedProjectId(firstId);
        }
      } else if (currentProjectId !== null || storedId) {
        set({ selectedProjectId: null, selectedSpaceId: null });
        await saveSelectedProjectId("");
      }
    } catch {
      // Offline / unauthenticated → leave state as-is; UI reads from local cache.
      set({ loaded: true });
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

  reset: () =>
    set({
      spaces: [],
      projects: [],
      selectedSpaceId: null,
      selectedProjectId: null,
      loaded: false,
    }),
}));

/** Derived selector: the currently selected Project object, or null. */
export function useSelectedProject(): Project | null {
  return useProjectStore((s) =>
    s.selectedProjectId
      ? (s.projects.find((p) => p.id === s.selectedProjectId) ?? null)
      : null,
  );
}

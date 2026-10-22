import { useEffect, useLayoutEffect, useRef } from "react";
import type {
  ExplorerDirectory,
  ExplorerFile,
  ExplorerListResponse,
  FileInfo,
} from "@/lib/explorer-api";
import {
  explorerErrorMessage,
  explorerInfo,
  explorerList,
} from "@/lib/explorer-api";
import { FILES_OPEN_PATH_EVENT } from "@/lib/files-sidebar-events";
import { buildExplorerFileFallback } from "@/lib/explorer-bookmark-target";
import {
  RECORD_TABLE_EXTENSION,
  RECORD_TABLE_TYPE,
} from "@/lib/record-tables-api";
import { toast } from "sonner";

type ExplorerItem = ExplorerDirectory | ExplorerFile;

export type ExplorerProjectScopeIdentityInput = {
  selectedSpaceId: string | null;
  selectedProjectId: string | null;
  projectSource?: string | null;
  currentPath: string;
};

/**
 * Keep the selected Project and the Project currently displayed as separate
 * boundaries. `currentPath` can lag behind a Project selection change, while
 * admin-only shared targets can temporarily display a Project different from
 * the canonical ProjectContext selection. User Files and personal absolute
 * paths fall back to the selected Project so switching tabs does not cancel a
 * personal bookmark open in flight.
 */
export function buildExplorerProjectScopeIdentity({
  selectedSpaceId,
  selectedProjectId,
  projectSource,
  currentPath,
}: ExplorerProjectScopeIdentityInput): string {
  const normalizedPath = currentPath.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  const displayedProjectId = normalizedPath.match(
    /^_projects\/project_([^/]+)/,
  )?.[1];
  return JSON.stringify([
    projectSource || "local",
    selectedSpaceId || "",
    selectedProjectId || "",
    displayedProjectId || selectedProjectId || "",
  ]);
}

export type ExplorerOpenPathOptions = {
  browseData: ExplorerListResponse | null;
  currentPath: string;
  authIdentity: string;
  scopeIdentity: string;
  handleFileClick: (file: ExplorerFile) => void;
  navigate: (path: string) => void;
};

function isExplorerDirectory(item: ExplorerItem): item is ExplorerDirectory {
  return !("type" in item);
}

function parentPath(path: string): string | null {
  const normalized = path.replace(/[\\/]+$/, "");
  const index = Math.max(normalized.lastIndexOf("/"), normalized.lastIndexOf("\\"));
  return index > 0 ? normalized.slice(0, index) : null;
}

function isFileInfo(value: FileInfo): boolean {
  return value.success === true && typeof value.is_directory === "boolean";
}

function targetUsesProjectBoundary(path: string): boolean {
  const normalized = path.replace(/\\/g, "/").replace(/^\/+/, "");
  return normalized.startsWith("_projects/project_") ||
    normalized.startsWith("aoitalk-record-table:");
}

/**
 * Bridge shell bookmark open events into the canonical Files open behavior.
 * The listener lifetime follows only the authenticated principal. Listing
 * refreshes and UI tab/project transitions are read through refs; a shared
 * target still checks its project boundary while a personal target remains
 * valid across Project/User Files transitions.
 */
export function useExplorerOpenPath(options: ExplorerOpenPathOptions): void {
  const latestOptionsRef = useRef(options);
  const requestGenerationRef = useRef(0);

  useLayoutEffect(() => {
    latestOptionsRef.current = options;
  }, [options]);

  useEffect(() => {
    let effectDisposed = false;
    const effectGeneration = ++requestGenerationRef.current;
    const isCurrentRequest = (
      requestGeneration: number,
      requestAuthIdentity: string,
      requestScopeIdentity: string,
      requiresProjectBoundary: boolean,
    ) => {
      if (effectDisposed || requestGeneration !== requestGenerationRef.current) {
        return false;
      }
      const latest = latestOptionsRef.current;
      return latest.authIdentity === requestAuthIdentity &&
        (!requiresProjectBoundary || latest.scopeIdentity === requestScopeIdentity);
    };

    const resolveCanonicalTarget = async (
      path: string,
      displayName?: string,
    ): Promise<ExplorerItem | null> => {
      const requestOptions = latestOptionsRef.current;
      const recordPrefix = "aoitalk-record-table:";
      if (path.startsWith(recordPrefix)) {
        const [projectId, recordTableId] = path.slice(recordPrefix.length).split(":", 2);
        if (projectId && recordTableId) {
          return {
            name: displayName?.trim() || `${recordTableId}${RECORD_TABLE_EXTENSION}`,
            path,
            type: RECORD_TABLE_TYPE,
            extension: RECORD_TABLE_EXTENSION,
            virtual_kind: "record_table",
            project_id: projectId,
            record_table_id: recordTableId,
          };
        }
      }

      const info = await explorerInfo(path);
      if (!isFileInfo(info)) {
        throw new Error("ファイルまたはフォルダーの情報を取得できませんでした");
      }
      const authoritativeName =
        info.name?.trim() ||
        path.split(/[\\/|]/).filter(Boolean).pop() ||
        path;
      const fallbackName = displayName?.trim() || authoritativeName;
      if (info.is_directory) {
        return { name: fallbackName, path };
      }

      const direct = requestOptions.browseData?.files.find(
        (candidate) => candidate.path === path,
      );
      if (direct) return direct;

      const candidates = [parentPath(path), requestOptions.currentPath].filter(
        (candidate, index, all): candidate is string =>
          Boolean(candidate) && all.indexOf(candidate) === index,
      );
      for (const candidate of candidates) {
        try {
          const data = await explorerList(candidate);
          const exact = data.files.find((file) => file.path === path);
          if (exact) return exact;
        } catch {
          // Try the next authorized parent before using info metadata alone.
        }
      }
      return buildExplorerFileFallback(path, info, displayName);
    };

    const onOpen = async (event: Event) => {
      const detail = (event as CustomEvent<{ path?: unknown; name?: unknown }>).detail;
      const path = detail?.path;
      if (typeof path !== "string" || !path) return;
      const name = typeof detail.name === "string" ? detail.name : undefined;
      const requestOptions = latestOptionsRef.current;
      const requestGeneration = ++requestGenerationRef.current;
      const requestAuthIdentity = requestOptions.authIdentity;
      const requestScopeIdentity = requestOptions.scopeIdentity;
      const requiresProjectBoundary = targetUsesProjectBoundary(path);
      try {
        const item = await resolveCanonicalTarget(path, name);
        if (!isCurrentRequest(
          requestGeneration,
          requestAuthIdentity,
          requestScopeIdentity,
          requiresProjectBoundary,
        )) return;
        const currentOptions = latestOptionsRef.current;
        if (item) {
          if (isExplorerDirectory(item)) {
            currentOptions.navigate(item.path);
          } else {
            currentOptions.handleFileClick(item);
          }
        } else {
          toast.error("ファイルまたはフォルダーの情報を取得できないため開けませんでした");
        }
      } catch (error) {
        if (!isCurrentRequest(
          requestGeneration,
          requestAuthIdentity,
          requestScopeIdentity,
          requiresProjectBoundary,
        )) return;
        toast.error(`ファイルまたはフォルダーを開けませんでした: ${explorerErrorMessage(error)}`);
      }
    };

    window.addEventListener(FILES_OPEN_PATH_EVENT, onOpen);
    return () => {
      effectDisposed = true;
      if (requestGenerationRef.current === effectGeneration) {
        requestGenerationRef.current += 1;
      }
      window.removeEventListener(FILES_OPEN_PATH_EVENT, onOpen);
    };
  }, [options.authIdentity]);
}

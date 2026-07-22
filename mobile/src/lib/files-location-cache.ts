import {
  filesApi,
  type FilesEntry,
  type FilesScope,
  type FilesSource,
} from "./files-api";

export type FilesListResult = {
  currentPath: string;
  parentPath: string | null;
  canGoUp: boolean;
  isAdminMode: boolean;
  items: FilesEntry[];
};

export type FilesLocation = {
  source: FilesSource;
  scope: FilesScope;
  authScope: string;
  path?: string;
  projectId?: string | null;
};

export type FilesLocationLoad = {
  requestKey: string;
  resolvedKey: string;
  result: FilesListResult;
  fromCache: boolean;
  stale?: boolean;
};

type ListFiles = (
  source: FilesSource,
  path?: string,
  scope?: FilesScope,
) => Promise<FilesListResult>;

export function filesLocationKey(location: FilesLocation): string {
  return JSON.stringify([
    location.source,
    location.scope,
    location.authScope,
    location.path ?? "",
    location.projectId ?? "",
  ]);
}

export function isFilesLoadCurrent(
  activeKey: string | null,
  load: Pick<FilesLocationLoad, "requestKey" | "resolvedKey" | "stale">,
): boolean {
  if (load.stale) return false;
  return activeKey === load.requestKey || activeKey === load.resolvedKey;
}

export class FilesLocationCache {
  private readonly cache = new Map<string, FilesListResult>();
  private readonly inFlight = new Map<string, Promise<FilesLocationLoad>>();
  private generation = 0;

  constructor(private readonly listFiles: ListFiles) {}

  peek(location: FilesLocation): FilesListResult | undefined {
    return this.cache.get(filesLocationKey(location));
  }

  load(
    location: FilesLocation,
    options: { revalidate?: boolean } = {},
  ): Promise<FilesLocationLoad> {
    const requestKey = filesLocationKey(location);
    const generation = this.generation;
    const cached = this.cache.get(requestKey);
    if (cached && !options.revalidate) {
      return Promise.resolve({
        requestKey,
        resolvedKey: requestKey,
        result: cached,
        fromCache: true,
      });
    }

    const running = this.inFlight.get(requestKey);
    if (running) return running;

    const flight = this.listFiles(
      location.source,
      location.path || undefined,
      location.scope,
    )
      .then((result) => {
        const resolvedLocation = { ...location, path: result.currentPath };
        const resolvedKey = filesLocationKey(resolvedLocation);
        const stale = generation !== this.generation;
        if (!stale) {
          this.cache.set(requestKey, result);
          this.cache.set(resolvedKey, result);
        }
        return {
          requestKey,
          resolvedKey,
          result,
          fromCache: false,
          stale,
        };
      })
      .finally(() => {
        if (this.inFlight.get(requestKey) === flight) {
          this.inFlight.delete(requestKey);
        }
      });
    this.inFlight.set(requestKey, flight);
    return flight;
  }

  invalidate(location: FilesLocation): void {
    this.cache.delete(filesLocationKey(location));
  }

  clear(): void {
    this.generation += 1;
    this.cache.clear();
    this.inFlight.clear();
  }
}

export const filesLocationCache = new FilesLocationCache((source, path, scope) =>
  filesApi.list(source, path, scope),
);

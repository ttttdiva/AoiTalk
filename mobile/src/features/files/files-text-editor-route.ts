import type { FilesEntry, FilesSource } from "../../lib/files-types";

export const FILES_TEXT_EDITOR_PATHNAME = "/(tabs)/filer/text";

export type FilesTextEditorIdentity = {
  source: FilesSource;
  path: string;
  name: string;
};

export function filesTextEditorParams(entry: FilesEntry) {
  return {
    pathname: FILES_TEXT_EDITOR_PATHNAME,
    params: {
      source: entry.source,
      path: entry.path,
      name: entry.name,
    },
  };
}

function firstParam(
  value: string | string[] | undefined,
): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

export function parseFilesTextEditorParams(
  params: Record<string, string | string[] | undefined>,
): FilesTextEditorIdentity | null {
  const source = firstParam(params.source);
  if (source !== "local" && source !== "server") return null;

  const path = firstParam(params.path);
  if (!path) return null;

  const nameParam = firstParam(params.name);
  const segments = path.split("/");
  const name = nameParam || segments[segments.length - 1] || path;

  return { source, path, name };
}

"use client";

/** Events shared by the route-local Files canvas and its shell sidebar. */
export const FILES_OPEN_PATH_EVENT = "files-sidebar-open-path";
export const FILES_DOWNLOAD_PATH_EVENT = "files-sidebar-download-path";

export function requestFilesOpen(path: string, name?: string) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(FILES_OPEN_PATH_EVENT, { detail: { path, name } }));
}

export function requestFilesDownload(path: string) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(FILES_DOWNLOAD_PATH_EVENT, { detail: { path } }));
}

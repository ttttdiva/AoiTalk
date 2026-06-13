import { fetchApi, getBaseUrl } from "./api-client";
import { getToken } from "./auth";
import type { FilerEntry } from "../types/api";

type FilerConfigPayload = {
  configured?: boolean;
  root_path?: string | null;
};

type FilerBrowsePayload = {
  current_path?: string;
  folders?: Array<{
    name: string;
    path: string;
    item_count?: number;
    thumbnail?: string;
  }>;
  files?: Array<{
    name: string;
    path: string;
    size?: number;
    modified_at?: string;
    type?: string;
    extension?: string;
  }>;
};

function inferMimeType(kind?: string, extension?: string): string | undefined {
  const ext = (extension || "").toLowerCase();

  if (kind === "image") {
    if (ext === ".jpg" || ext === ".jpeg") return "image/jpeg";
    if (ext === ".png") return "image/png";
    if (ext === ".gif") return "image/gif";
    if (ext === ".webp") return "image/webp";
    if (ext === ".bmp") return "image/bmp";
    return "image/*";
  }

  if (kind === "video") {
    if (ext === ".mp4") return "video/mp4";
    if (ext === ".webm") return "video/webm";
    if (ext === ".mov") return "video/quicktime";
    return "video/*";
  }

  if (kind === "audio") {
    if (ext === ".mp3") return "audio/mpeg";
    if (ext === ".wav") return "audio/wav";
    if (ext === ".ogg") return "audio/ogg";
    return "audio/*";
  }

  return undefined;
}

export const filerApi = {
  async getConfig(): Promise<{ paths: { label: string; path: string }[] }> {
    const payload = await fetchApi<FilerConfigPayload>("/api/filer/config");
    const paths = payload.root_path
      ? [{ label: "Root", path: payload.root_path }]
      : [];
    return { paths };
  },

  async listFolder(
    path: string,
  ): Promise<{ items: FilerEntry[]; current_path: string }> {
    const payload = await fetchApi<FilerBrowsePayload>(
      `/api/filer/browse?path=${encodeURIComponent(path)}`,
    );

    const folders: FilerEntry[] = (payload.folders || []).map((entry) => ({
      name: entry.name,
      path: entry.path,
      type: "directory",
      thumbnail: entry.thumbnail,
    }));

    const files: FilerEntry[] = (payload.files || []).map((entry) => ({
      name: entry.name,
      path: entry.path,
      type: "file",
      size: entry.size,
      modified: entry.modified_at,
      mime_type: inferMimeType(entry.type, entry.extension),
    }));

    return {
      items: [...folders, ...files],
      current_path: payload.current_path || path,
    };
  },

  async saveTextFile(
    path: string,
    content = "",
  ): Promise<{ success?: boolean; path?: string; name?: string }> {
    return fetchApi("/api/explorer/save", {
      method: "PUT",
      body: JSON.stringify({ path, content, encoding: "utf-8" }),
    });
  },

  async getFileUrl(filePath: string): Promise<string> {
    const baseUrl = await getBaseUrl();
    const token = await getToken();
    return `${baseUrl}/api/filer/file?path=${encodeURIComponent(filePath)}&token=${token || ""}`;
  },

  async getThumbnailUrl(filePath: string, mimeType?: string): Promise<string> {
    const baseUrl = await getBaseUrl();
    const token = await getToken();
    const endpoint = mimeType?.startsWith("video/")
      ? "video-thumbnail"
      : "image-thumbnail";
    return `${baseUrl}/api/filer/${endpoint}?path=${encodeURIComponent(filePath)}&token=${token || ""}`;
  },
};

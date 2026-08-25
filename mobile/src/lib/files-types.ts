export type FilesSource = "local" | "server";
export type FilesScope = "workspace" | "user";

export type FilesEntry = {
  name: string;
  path: string;
  type: "file" | "directory";
  size?: number;
  modifiedAt?: string | null;
  mimeType?: string;
  extension?: string;
  source: FilesSource;
};

export type FilesUploadInput = {
  uri: string;
  name: string;
  mimeType?: string | null;
};

export type FilesBookmark = {
  id?: string;
  user_id?: string;
  space_id?: string | null;
  name: string;
  path: string;
  icon?: string;
  kind?: "bookmark" | "folder";
  parent_id?: string | null;
  sort_order?: number;
  created_at?: string;
  updated_at?: string;
};

export type FilesEntryMetadata = {
  size?: number;
  modifiedAt?: string | null;
};

export type FilesMediaSource = {
  uri: string;
  headers?: Record<string, string>;
};

export type FilesMediaKind =
  | "image"
  | "video"
  | "audio"
  | "pdf"
  | "text"
  | "other";

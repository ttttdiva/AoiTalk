/**
 * Image-file helpers shared by the CodeMirror editors.
 *
 * Browser clipboard implementations are not consistent about the MIME type
 * or the name assigned to a copied image. Keep the acceptance rule here so
 * paste, drop, and upload handlers do not slowly grow different rules. This
 * module intentionally does not sanitize a user supplied drop filename; the
 * storage/domain boundary owns that policy.
 */

import { getFileServeUrl } from "./explorer-serve-url";

export const IMAGE_FILE_EXTENSIONS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "bmp",
  "avif",
  "svg",
]);

export type EditorImageInsertSource = "paste" | "drop";

/** Whether a Files editor surface contains Markdown image syntax support. */
export function isMarkdownImageExtension(extension: string): boolean {
  const normalized = extension.trim().toLowerCase();
  return normalized === ".md" || normalized === ".markdown";
}

type FileLike = Pick<File, "name" | "type">;

function fileNameExtension(name: string): string {
  const basename = name.split(/[\\/]/u).pop() ?? name;
  const withoutQuery = basename.split(/[?#]/u, 1)[0] ?? basename;
  const dot = withoutQuery.lastIndexOf(".");
  return dot >= 0 ? withoutQuery.slice(dot + 1).toLowerCase() : "";
}

function normalizedMimeType(type: string | null | undefined): string {
  return (type ?? "").trim().toLowerCase();
}

/** Return a stable extension represented by an image MIME type. */
export function imageExtensionForMimeType(type: string | null | undefined): string | null {
  const mime = normalizedMimeType(type);
  if (!mime.startsWith("image/")) return null;
  const subtype = mime.slice("image/".length).split(";", 1)[0]?.trim() ?? "";
  switch (subtype) {
    case "jpeg":
    case "pjpeg":
      return "jpg";
    case "svg+xml":
      return "svg";
    case "x-icon":
    case "vnd.microsoft.icon":
      return "ico";
    default:
      if (IMAGE_FILE_EXTENSIONS.has(subtype)) return subtype;
      // MIME is authoritative for non-empty image/* values. Preserve an
      // unknown but safe subtype for generated clipboard names instead of
      // pretending that the bytes are PNG data.
      return subtype.replace(/[^a-z0-9]+/g, "").slice(0, 24) || "png";
  }
}

/** Return the known image extension represented by a filename. */
export function imageExtensionForFilename(name: string | null | undefined): string | null {
  const extension = fileNameExtension(name ?? "");
  return IMAGE_FILE_EXTENSIONS.has(extension) ? extension : null;
}

/** Filename-only predicate used by upload previews. */
export function isImageFilename(name: string | null | undefined): boolean {
  return imageExtensionForFilename(name) !== null;
}

/**
 * Whether a File should enter the image upload pipeline.
 *
 * A non-empty MIME type is authoritative. Only an empty MIME type gets the
 * conservative filename fallback required for files produced by clipboards.
 */
export function isImageFile(file: FileLike | null | undefined): boolean {
  if (!file) return false;
  const mime = normalizedMimeType(file.type);
  if (mime) return mime.startsWith("image/");
  return imageExtensionForFilename(file.name) !== null;
}

export const isImageFileLike = isImageFile;
export const isImage = isImageFile;

function isGenericClipboardName(name: string): boolean {
  const lower = name.trim().toLowerCase();
  if (!lower) return true;
  // Chrome/Safari commonly expose a copied bitmap as blob/image/file. These
  // are not useful names and, unlike a drop filename, may safely be replaced.
  return new Set(["blob", "image", "file", "untitled", "unknown"]).has(lower)
    && imageExtensionForFilename(lower) === null;
}

export type NormalizeImageFilenameOptions = {
  source?: EditorImageInsertSource;
  index?: number;
  generatedPrefix?: string;
};

/**
 * Keep a meaningful filename byte-for-byte intact and create a deterministic
 * filename only for an anonymous clipboard image. Domain-level sanitization
 * is intentionally not performed here.
 */
export function normalizeImageFilename(
  file: FileLike | string,
  options: NormalizeImageFilenameOptions | EditorImageInsertSource = {},
): string {
  const source = typeof options === "string" ? options : options.source ?? "paste";
  const index = typeof options === "string" ? 0 : Math.max(0, options.index ?? 0);
  const generatedPrefix = typeof options === "string"
    ? "pasted-image"
    : options.generatedPrefix ?? "pasted-image";
  const name = typeof file === "string" ? file : file.name;
  if (source === "drop" || !isGenericClipboardName(name ?? "")) return name ?? "";

  const mime = typeof file === "string" ? "" : file.type;
  const extension = imageExtensionForMimeType(mime)
    ?? imageExtensionForFilename(name)
    ?? "png";
  const suffix = index === 0 ? "" : `-${index + 1}`;
  return `${generatedPrefix}${suffix}.${extension}`;
}

/** Return files with generated names where needed. */
export function normalizeImageFiles(
  files: Iterable<File>,
  source: EditorImageInsertSource = "paste",
): File[] {
  return Array.from(files, (file, index) => {
    const name = normalizeImageFilename(file, { source, index });
    if (name === file.name) return file;
    return new File([file], name, {
      type: file.type,
      lastModified: file.lastModified,
    });
  });
}

export const normalizeEditorImageFiles = normalizeImageFiles;

/** Filter a FileList/iterable to accepted image files while retaining order. */
export function getImageFiles(files: Iterable<File> | null | undefined): File[] {
  if (!files) return [];
  return Array.from(files).filter((file) => isImageFile(file));
}

export const filterImageFiles = getImageFiles;

/**
 * Return the path of an image relative to a markdown document's directory.
 * External/absolute references are returned untouched. This does not
 * URL-encode or sanitize the path so filenames remain domain-owned.
 */
export function resolveRelativeMarkdownImagePath(
  documentPath: string,
  imagePath: string,
): string {
  const source = imagePath.trim();
  if (!source || isExternalOrAbsoluteReference(source)) return source;
  const { path, suffix } = splitReferenceSuffix(source);
  const documentDirectory = dirname(documentPath);
  const combined = normalizePath(
    documentDirectory ? `${documentDirectory}/${path}` : path,
  );
  return `${combined}${suffix}`;
}

export const relativeMarkdownImagePath = resolveRelativeMarkdownImagePath;
export const toRelativeMarkdownImagePath = resolveRelativeMarkdownImagePath;

/**
 * Resolve a markdown image's source for a browser preview. Relative local
 * references are rooted at the markdown document and served by the existing
 * file-serving endpoint. URLs, data/blob references, and absolute paths are
 * passed through unchanged.
 *
 * Argument order follows ReactMarkdown call sites: `(source, documentPath)`.
 */
export function resolveMarkdownImageSource(
  source: string,
  documentPath: string,
): string {
  const trimmed = source.trim();
  const angleWrapped = trimmed.startsWith("<") && trimmed.endsWith(">");
  const unwrapped = angleWrapped ? trimmed.slice(1, -1).trim() : trimmed;
  if (!unwrapped || isExternalOrAbsoluteReference(unwrapped)) return unwrapped;
  const { path, suffix } = angleWrapped
    ? { path: unwrapped, suffix: "" }
    : splitReferenceSuffix(unwrapped);
  const rootedPath = resolveRelativeMarkdownImagePath(documentPath, path);
  const served = getFileServeUrl(rootedPath);
  return `${served}${suffix}`;
}

export const resolveRelativeMarkdownImageUrl = resolveMarkdownImageSource;
export const resolveMarkdownImageUrl = resolveMarkdownImageSource;
export const resolveRelativeMarkdownImage = resolveMarkdownImageSource;

/** Build a markdown image reference using a document-relative target. */
export function createMarkdownImage(
  documentPath: string,
  imagePath: string,
  alt = "",
): string {
  const target = resolveRelativeMarkdownImagePath(documentPath, imagePath);
  return `![${escapeMarkdownImageAlt(alt)}](${target})`;
}

export const markdownImageForPath = createMarkdownImage;

function dirname(path: string): string {
  const normalized = path.replace(/\\/gu, "/").replace(/\/+$/u, "");
  const slash = normalized.lastIndexOf("/");
  return slash < 0 ? "" : normalized.slice(0, slash);
}

function normalizePath(path: string): string {
  const normalizedInput = path.replace(/\\/gu, "/");
  const hasUnixRoot = normalizedInput.startsWith("/");
  const parts: string[] = [];
  for (const part of normalizedInput.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (parts.length > 0 && parts[parts.length - 1] !== "..") parts.pop();
      else parts.push("..");
      continue;
    }
    parts.push(part);
  }
  const normalized = parts.join("/");
  return hasUnixRoot ? `/${normalized}` : normalized;
}

function splitReferenceSuffix(source: string): { path: string; suffix: string } {
  const match = source.match(/[?#]/u);
  if (!match || match.index === undefined) return { path: source, suffix: "" };
  return {
    path: source.slice(0, match.index),
    suffix: source.slice(match.index),
  };
}

function isExternalOrAbsoluteReference(source: string): boolean {
  return /^(?:[a-z][a-z\d+.-]*:|\/\/|\/|[A-Za-z]:[\\/])/iu.test(source);
}

function escapeMarkdownImageAlt(value: string): string {
  return value.replace(/[\\\[\]]/gu, "\\$&");
}

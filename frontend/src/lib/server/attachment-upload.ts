import fs from "node:fs";
import path from "node:path";

/** アップロード可能なファイルサイズの上限 (50 MB) */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

/** ファイルサイズが上限を超えているか判定する */
export function exceedsUploadSizeLimit(size: number): boolean {
  return size > MAX_UPLOAD_BYTES;
}

export function sanitizeUploadFileName(name: string): string {
  const leafName = name.split(/[\\/]/).pop() || name;
  const cleaned = leafName
    // `[[file:path|label]]` 参照の区切り文字も除き、Docsリンクを壊さない。
    .replace(/[/\\:*?"<>|[\]]/g, "")
    .replace(/[\u0000-\u001f]/g, "")
    .trim()
    .replace(/^\.+$/, "");
  return cleaned.slice(0, 180) || "uploaded-file";
}

/** 同名競合を排他的作成で回避し、保存できた絶対pathを返す。 */
export function writeUniqueUploadFile(
  dir: string,
  fileName: string,
  content: Buffer,
): string {
  const parsed = path.parse(fileName);
  for (let index = 0; ; index += 1) {
    const candidate = path.join(
      /*turbopackIgnore: true*/
      dir,
      index === 0 ? fileName : `${parsed.name}-${index}${parsed.ext}`,
    );
    let created = false;
    try {
      const descriptor = fs.openSync(
        /* turbopackIgnore: true */ candidate,
        "wx",
      );
      created = true;
      try {
        fs.writeFileSync(descriptor, content);
      } finally {
        fs.closeSync(descriptor);
      }
      return candidate;
    } catch (error) {
      if (created) {
        fs.rmSync(/* turbopackIgnore: true */ candidate, { force: true });
      }
      if (
        error instanceof Error &&
        "code" in error &&
        error.code === "EEXIST"
      ) {
        continue;
      }
      throw error;
    }
  }
}

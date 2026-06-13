/**
 * HF 仮想パス: ファイラー内で HF リポジトリを擬似的にディレクトリ構造として扱うための
 * パス表現。`|` をセパレータにしてエンコードする。
 *
 *   - ルート:         "HF"
 *   - リポジトリルート: "HF|<accountId>|<repoType>|<repoId>"
 *   - 内部ファイル:   "HF|<accountId>|<repoType>|<repoId>|<subPath>"
 *
 * repoId は "ExampleOrg/Video_1" のように `/` を含むが、外側は `|` で区切るため衝突しない。
 * subPath は HF API が返す相対パス（`/` 区切り）をそのまま保持する。
 */

export const HF_PREFIX = "HF";
export const HF_SEP = "|";

export type RepoType = "model" | "dataset";

export interface HfVirtualPath {
  kind: "root" | "repo";
  accountId?: string;
  repoType?: RepoType;
  repoId?: string;
  subPath?: string;
}

export function isHfPath(p: string): boolean {
  if (!p) return false;
  return p === HF_PREFIX || p.startsWith(HF_PREFIX + HF_SEP);
}

export function parseHfPath(p: string): HfVirtualPath | null {
  if (p === HF_PREFIX) return { kind: "root" };
  if (!p.startsWith(HF_PREFIX + HF_SEP)) return null;
  const parts = p.split(HF_SEP);
  if (parts.length < 4) return null;
  const [, accountId, repoTypeRaw, repoId, ...rest] = parts;
  const repoType: RepoType = repoTypeRaw === "dataset" ? "dataset" : "model";
  return {
    kind: "repo",
    accountId,
    repoType,
    repoId,
    subPath: rest.length ? rest.join(HF_SEP) : "",
  };
}

export function buildHfPath(v: HfVirtualPath): string {
  if (v.kind === "root") return HF_PREFIX;
  const segs = [
    HF_PREFIX,
    v.accountId ?? "",
    v.repoType ?? "model",
    v.repoId ?? "",
  ];
  if (v.subPath) segs.push(v.subPath);
  return segs.join(HF_SEP);
}

/** HF ファイルサーブURL。画像・動画・音声などバイナリ配信。 */
export function hfServeUrl(virtualPath: string): string | null {
  const v = parseHfPath(virtualPath);
  if (!v || v.kind !== "repo" || !v.subPath) return null;
  const qs = new URLSearchParams();
  if (v.accountId) qs.set("accountId", v.accountId);
  qs.set("repoId", v.repoId!);
  qs.set("repoType", v.repoType!);
  qs.set("path", v.subPath);
  return `/api/huggingface/file?${qs.toString()}`;
}

/** HF テキストプレビューURL（mode=text で JSON 応答）。 */
export function hfTextUrl(virtualPath: string): string | null {
  const base = hfServeUrl(virtualPath);
  if (!base) return null;
  return base + "&mode=text";
}

/** ファイル名から media type を推定（type ユーティリティ）。 */
export function inferMediaType(name: string): string {
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  if (["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"].includes(ext)) {
    return "image/" + (ext === "jpg" ? "jpeg" : ext);
  }
  if (["mp4", "webm", "mov", "avi", "mkv"].includes(ext)) {
    return "video/" + ext;
  }
  if (
    ["mp3", "wav", "ogg", "flac", "aac", "m4a", "opus", "wma"].includes(ext)
  ) {
    return "audio/" + ext;
  }
  if (
    [
      "txt",
      "md",
      "json",
      "yaml",
      "yml",
      "csv",
      "py",
      "js",
      "ts",
      "tsx",
      "jsx",
      "html",
      "css",
      "xml",
      "log",
      "ini",
      "cfg",
      "sql",
    ].includes(ext)
  ) {
    return "text/plain";
  }
  if (ext === "parquet") return "application/parquet";
  return "application/octet-stream";
}

/**
 * ファイラーでファイルを配信する URL を、パスの種類に応じて振り分けるヘルパー。
 *
 *   - 絶対パス (Windows/Unix形式) → ファイラーAPI
 *   - それ以外（相対パス）           → エクスプローラAPI
 */

import { withBasePath } from "@/lib/base-path";

function isAbsolutePath(p: string): boolean {
  if (!p) return false;
  if (/^[A-Za-z]:[\\/]/.test(p)) return true;
  if (p.startsWith("/")) return true;
  return false;
}

/** ファイル原本の配信URL(画像・動画ストリームなど)。 */
export function getFileServeUrl(filePath: string): string {
  if (isAbsolutePath(filePath)) {
    return withBasePath(`/api/python-proxy/filer/file?path=${encodeURIComponent(filePath)}`);
  }
  return withBasePath(`/api/python-proxy/explorer/serve?path=${encodeURIComponent(filePath)}`);
}

/** 画像サムネイル(可能ならサーバでリサイズ)。 */
export function getImageThumbnailUrl(filePath: string, size: number): string {
  if (isAbsolutePath(filePath)) {
    return withBasePath(`/api/python-proxy/filer/image-thumbnail?path=${encodeURIComponent(filePath)}&size=${size}`);
  }
  return withBasePath(`/api/python-proxy/explorer/image-thumbnail?path=${encodeURIComponent(filePath)}&size=${size}`);
}

/** 動画サムネイル。 */
export function getVideoThumbnailUrl(filePath: string): string {
  if (isAbsolutePath(filePath)) {
    return withBasePath(`/api/python-proxy/filer/video-thumbnail?path=${encodeURIComponent(filePath)}`);
  }
  return withBasePath(`/api/python-proxy/explorer/video-thumbnail?path=${encodeURIComponent(filePath)}`);
}

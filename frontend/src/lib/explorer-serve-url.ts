/**
 * ファイラーでファイルを配信する URL を、パスの種類に応じて振り分けるヘルパー。
 *
 *   - 絶対パス (D:\... / /home/...) → ファイラーAPI
 *   - HF 仮想パス (HF|...)          → HuggingFace API
 *   - それ以外（相対パス）           → エクスプローラAPI
 */

import { hfServeUrl, isHfPath } from "./hf/virtual-path";
import { isHydrusPath, parseHydrusFileId } from "./hydrus/virtual-path";

function isAbsolutePath(p: string): boolean {
  if (!p) return false;
  if (/^[A-Za-z]:[\\/]/.test(p)) return true;
  if (p.startsWith("/")) return true;
  return false;
}

/** ファイル原本の配信URL（画像・動画ストリームなど）。 */
export function getFileServeUrl(filePath: string): string {
  if (isHfPath(filePath)) {
    return hfServeUrl(filePath) ?? "";
  }
  if (isHydrusPath(filePath)) {
    const id = parseHydrusFileId(filePath);
    return id != null ? `/api/python-proxy/hydrus/file/${id}` : "";
  }
  if (isAbsolutePath(filePath)) {
    return `/api/python-proxy/filer/file?path=${encodeURIComponent(filePath)}`;
  }
  return `/api/python-proxy/explorer/serve?path=${encodeURIComponent(filePath)}`;
}

/** 画像サムネイル（可能ならサーバでリサイズ、HF は原本URLを返す）。 */
export function getImageThumbnailUrl(filePath: string, size: number): string {
  if (isHfPath(filePath)) {
    // HF 側にサムネAPI はないため原本を返す
    return hfServeUrl(filePath) ?? "";
  }
  if (isHydrusPath(filePath)) {
    const id = parseHydrusFileId(filePath);
    return id != null ? `/api/python-proxy/hydrus/thumbnail/${id}` : "";
  }
  if (isAbsolutePath(filePath)) {
    return `/api/python-proxy/filer/image-thumbnail?path=${encodeURIComponent(filePath)}&size=${size}`;
  }
  return `/api/python-proxy/explorer/image-thumbnail?path=${encodeURIComponent(filePath)}&size=${size}`;
}

/** 動画サムネイル（HF は取得できないため空文字を返しフォールバックへ）。 */
export function getVideoThumbnailUrl(filePath: string): string {
  if (isHfPath(filePath)) {
    return ""; // HF には動画サムネイルAPIがない
  }
  if (isHydrusPath(filePath)) {
    const id = parseHydrusFileId(filePath);
    return id != null ? `/api/python-proxy/hydrus/thumbnail/${id}` : "";
  }
  if (isAbsolutePath(filePath)) {
    return `/api/python-proxy/filer/video-thumbnail?path=${encodeURIComponent(filePath)}`;
  }
  return `/api/python-proxy/explorer/video-thumbnail?path=${encodeURIComponent(filePath)}`;
}

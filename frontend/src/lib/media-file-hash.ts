import { sha256 } from "@noble/hashes/sha2";
import { bytesToHex } from "@noble/hashes/utils";

/** Hash one MiB at a time; a 512 MiB video is never copied into one buffer. */
export async function hashMediaFile(file: { size: number; slice: (start: number, end: number) => { arrayBuffer: () => Promise<ArrayBuffer> } }, isActive: () => boolean = () => true, progress?: (percent: number) => void): Promise<string> {
  const digest = sha256.create();
  const chunkSize = 1024 * 1024;
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    if (!isActive()) throw new Error("制作物の選択が変わったため登録を停止しました。");
    const chunk = await file.slice(offset, Math.min(offset + chunkSize, file.size)).arrayBuffer();
    digest.update(new Uint8Array(chunk));
    progress?.(Math.round(Math.min(offset + chunkSize, file.size) / file.size * 100));
  }
  if (!isActive()) throw new Error("制作物の選択が変わったため登録を停止しました。");
  return bytesToHex(digest.digest());
}

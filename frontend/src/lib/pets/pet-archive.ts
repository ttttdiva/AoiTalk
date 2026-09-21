import { ATLAS, parseManifest, safeRelativePath, type PetManifest } from "./codex-pet";

const ZIP_LIMIT = 32 * 1024 * 1024;
const IMAGE_LIMIT = 12 * 1024 * 1024;
const MANIFEST_LIMIT = 16 * 1024;
type Entry = { name: string; flags: number; method: number; crc: number; compressed: number; size: number; offset: number };
const decoder = new TextDecoder("utf-8", { fatal: true });
const error = () => new Error("ZIPが破損しているか、対応していないZIP形式です。");
const crcTable = Array.from({ length: 256 }, (_, n) => {
  let crc = n;
  for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  return crc >>> 0;
});
export function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff;
  for (const b of bytes) crc = (crc >>> 8) ^ crcTable[(crc ^ b) & 255];
  return (crc ^ 0xffffffff) >>> 0;
}
/** Central-directory reader: never extracts paths or executes archive contents. */
function zipEntries(bytes: Uint8Array): { entries: Map<string, Entry>; directory: number } {
  if (bytes.length > ZIP_LIMIT) throw new Error("ZIPは32 MiB以下にしてください。");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
    if (view.getUint32(i, true) === 0x06054b50 && i + 22 + view.getUint16(i + 20, true) === bytes.length) { end = i; break; }
  }
  if (end < 0 || view.getUint16(end + 4, true) !== 0 || view.getUint16(end + 6, true) !== 0) throw error();
  const count = view.getUint16(end + 10, true);
  const size = view.getUint32(end + 12, true);
  const directory = view.getUint32(end + 16, true);
  if (count === 0 || count > 2048 || view.getUint16(end + 8, true) !== count || directory + size !== end) throw error();
  const entries = new Map<string, Entry>();
  let at = directory;
  for (let n = 0; n < count; n++) {
    if (at + 46 > end || view.getUint32(at, true) !== 0x02014b50) throw error();
    const nameLength = view.getUint16(at + 28, true);
    const next = at + 46 + nameLength + view.getUint16(at + 30, true) + view.getUint16(at + 32, true);
    if (next > end || view.getUint16(at + 34, true) !== 0) throw error();
    const rawName = decoder.decode(bytes.subarray(at + 46, at + 46 + nameLength)).replaceAll("\\", "/");
    safeRelativePath(rawName.endsWith("/") ? rawName.slice(0, -1) : rawName);
    // Folder entries and macOS metadata are not payloads.
    if (!rawName.endsWith("/") && !rawName.startsWith("__MACOSX/") && !rawName.split("/").some((p) => p.startsWith("."))) {
      const name = safeRelativePath(rawName);
      if (entries.has(name)) throw new Error("ZIP内に同名ファイルが重複しています。");
      entries.set(name, { name, flags: view.getUint16(at + 8, true), method: view.getUint16(at + 10, true),
        crc: view.getUint32(at + 16, true), compressed: view.getUint32(at + 20, true),
        size: view.getUint32(at + 24, true), offset: view.getUint32(at + 42, true) });
    }
    at = next;
  }
  if (at !== end) throw error();
  return { entries, directory };
}
async function extract(bytes: Uint8Array, entry: Entry, directory: number, limit: number): Promise<Uint8Array> {
  if (entry.size > limit || entry.compressed > ZIP_LIMIT) throw new Error("ペットファイルがサイズ上限を超えています。");
  if ((entry.flags & 1) || ![0, 8].includes(entry.method)) throw new Error("暗号化ZIPや特殊な圧縮には対応していません。");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const at = entry.offset;
  if (at + 30 > directory || view.getUint32(at, true) !== 0x04034b50 || view.getUint16(at + 8, true) !== entry.method || view.getUint16(at + 6, true) !== entry.flags) throw error();
  const nameEnd = at + 30 + view.getUint16(at + 26, true);
  const start = nameEnd + view.getUint16(at + 28, true);
  if (start + entry.compressed > directory || decoder.decode(bytes.subarray(at + 30, nameEnd)).replaceAll("\\", "/") !== entry.name) throw error();
  let data = bytes.slice(start, start + entry.compressed);
  if (entry.method === 8) {
    if (typeof DecompressionStream === "undefined") throw new Error("このブラウザではZIPを展開できません。pet.jsonと画像の2ファイルを選択してください。");
    let stream: DecompressionStream;
    try { stream = new DecompressionStream("deflate-raw"); }
    catch { throw new Error("このブラウザではZIPを展開できません。pet.jsonと画像の2ファイルを選択してください。"); }
    const reader = new Blob([data]).stream().pipeThrough(stream).getReader();
    const chunks: Uint8Array[] = [];
    let total = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        total += value.byteLength;
        if (total > limit || total > entry.size) { await reader.cancel(); throw error(); }
        chunks.push(value);
      }
    } finally { reader.releaseLock(); }
    data = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.length; }
  }
  if (data.length !== entry.size || crc32(data) !== entry.crc) throw error();
  return data;
}
/** Read dimensions BEFORE decoding, including WebP VP8/VP8L/VP8X. */
export function imageType(bytes: Uint8Array): "image/png" | "image/webp" {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let width = 0, height = 0;
  let mime: "image/png" | "image/webp";
  if (bytes.length >= 33 && view.getUint32(0) === 0x89504e47 && view.getUint32(4) === 0x0d0a1a0a && view.getUint32(12) === 0x49484452) {
    width = view.getUint32(16); height = view.getUint32(20); mime = "image/png";
  } else if (bytes.length >= 30 && view.getUint32(0) === 0x52494646 && view.getUint32(8) === 0x57454250) {
    mime = "image/webp";
    const chunk = view.getUint32(12);
    if (chunk === 0x56503858) {
      if (bytes[20] & 2) throw new Error("アニメーションWebPではなく、静止スプライトシートを指定してください。");
      width = 1 + bytes[24] + (bytes[25] << 8) + (bytes[26] << 16);
      height = 1 + bytes[27] + (bytes[28] << 8) + (bytes[29] << 16);
    } else if (chunk === 0x5650384c && bytes[20] === 0x2f) {
      const bits = view.getUint32(21, true); width = (bits & 0x3fff) + 1; height = ((bits >>> 14) & 0x3fff) + 1;
    } else if (chunk === 0x56503820 && bytes[23] === 0x9d && bytes[24] === 1 && bytes[25] === 0x2a) {
      width = view.getUint16(26, true) & 0x3fff; height = view.getUint16(28, true) & 0x3fff;
    }
  } else throw new Error("スプライトシートにはPNGまたはWebP画像を指定してください。");
  if (width !== ATLAS.width || height !== ATLAS.height) throw new Error("Codex v2画像は1536×2288 px（8列×11行）である必要があります。");
  return mime;
}
async function checkedImage(data: Uint8Array): Promise<Blob> {
  if (data.length > IMAGE_LIMIT) throw new Error("画像は12 MiB以下にしてください。");
  const image = new Blob([data.slice().buffer], { type: imageType(data) });
  // Reject truncated/corrupt image data before replacing the saved pet.
  const bitmap = await createImageBitmap(image).catch(() => { throw new Error("画像を読み込めません。破損していない画像を指定してください。"); });
  const valid = bitmap.width === ATLAS.width && bitmap.height === ATLAS.height;
  bitmap.close();
  if (!valid) throw new Error("スプライトシートの寸法が不正です。");
  return image;
}
export async function importPetFiles(files: readonly File[]): Promise<{ manifest: PetManifest; image: Blob }> {
  if (files.length === 0) throw new Error("ZIP、またはpet.jsonと画像を選択してください。");
  if (files.length === 1 && /\.zip$/i.test(files[0].name)) {
    if (files[0].size > ZIP_LIMIT) throw new Error("ZIPは32 MiB以下にしてください。");
    const bytes = new Uint8Array(await files[0].arrayBuffer());
    const { entries, directory } = zipEntries(bytes);
    const manifests = [...entries.values()].filter((e) => e.name.split("/").at(-1) === "pet.json");
    if (manifests.length !== 1) throw new Error("ZIPにはpet.jsonを1つだけ含めてください。");
    const entry = manifests[0];
    const manifest = parseManifest(decoder.decode(await extract(bytes, entry, directory, MANIFEST_LIMIT)));
    const folder = entry.name.slice(0, entry.name.length - "pet.json".length);
    const sprite = entries.get(folder + manifest.spritesheetPath);
    if (!sprite) throw new Error("pet.jsonが指定するスプライトシートがZIP内にありません。");
    return { manifest, image: await checkedImage(await extract(bytes, sprite, directory, IMAGE_LIMIT)) };
  }
  if (files.length !== 2) throw new Error("pet.jsonとスプライトシートの2ファイル、またはZIPを1つ選択してください。");
  const manifests = files.filter((f) => f.name === "pet.json");
  if (manifests.length !== 1 || manifests[0].size > MANIFEST_LIMIT) throw new Error("16 KiB以下のpet.jsonを1つ指定してください。");
  const manifest = parseManifest(await manifests[0].text());
  const image = files.find((f) => f !== manifests[0] && f.name === manifest.spritesheetPath.split("/").at(-1));
  if (!image) throw new Error("pet.jsonが指定する画像を一緒に選択してください。");
  if (image.size > IMAGE_LIMIT) throw new Error("画像は12 MiB以下にしてください。");
  return { manifest, image: await checkedImage(new Uint8Array(await image.arrayBuffer())) };
}

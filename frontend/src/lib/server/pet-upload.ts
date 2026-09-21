import sharp from "sharp";
import { ATLAS, parseManifest } from "../pets/codex-pet";
import { PET_IMAGE_LIMIT, PET_MANIFEST_LIMIT, PET_UPLOAD_LIMIT, PetRequestError } from "../pets/pet-server-contract";

/** Bound the actual stream before the multipart parser allocates file buffers. */
async function boundedBody(request: Request): Promise<Buffer> {
  const declared = Number(request.headers.get("content-length"));
  if (declared > PET_UPLOAD_LIMIT) throw new PetRequestError("アップロードがサイズ上限を超えています。", 413);
  if (!request.body) throw new PetRequestError("ペットファイルがありません。", 400);
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > PET_UPLOAD_LIMIT) { await reader.cancel(); throw new PetRequestError("アップロードがサイズ上限を超えています。", 413); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  return Buffer.concat(chunks, size);
}
function checkStaticPng(bytes: Buffer) {
  let position = 8;
  while (position + 12 <= bytes.length) {
    const length = bytes.readUInt32BE(position);
    const kind = bytes.toString("ascii", position + 4, position + 8);
    if (position + 12 + length > bytes.length) throw new Error("PNGが破損しています。");
    if (kind === "acTL") throw new Error("アニメーションPNGではなく静止画像を指定してください。");
    position += 12 + length;
    if (kind === "IEND") {
      if (length !== 0 || position !== bytes.length) throw new Error("PNGが破損しています。");
      return;
    }
  }
  throw new Error("PNGが途中で切れています。");
}
/** Never trust the browser's validation, MIME type, filename, or image headers alone. */
export async function validatePetImage(bytes: Buffer): Promise<Buffer> {
  if (!bytes.length || bytes.length > PET_IMAGE_LIMIT) throw new PetRequestError("画像は12 MiB以下にしてください。", 413);
  try {
    const png = bytes.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"));
    const webp = bytes.length >= 30 && bytes.toString("ascii", 0, 4) === "RIFF" && bytes.toString("ascii", 8, 12) === "WEBP";
    if (!png && !webp) throw new Error("PNGまたはWebP画像を指定してください。");
    if (png) checkStaticPng(bytes);
    if (webp && bytes.readUInt32LE(4) + 8 !== bytes.length) throw new Error("WebPが破損しています。");
    const pipeline = sharp(bytes, { failOn: "warning", limitInputPixels: ATLAS.width * ATLAS.height });
    const metadata = await pipeline.metadata();
    if (metadata.width !== ATLAS.width || metadata.height !== ATLAS.height || (metadata.pages ?? 1) !== 1) {
      throw new Error("Codex v2の1536×2288 pxの静止画像を指定してください。");
    }
    // Decode all pixels, strip metadata and persist one canonical raster format.
    const output = await pipeline.png().toBuffer();
    if (output.length > PET_IMAGE_LIMIT) throw new PetRequestError("変換後の画像が12 MiBを超えています。", 413);
    return output;
  } catch (error) {
    if (error instanceof PetRequestError) throw error;
    throw new PetRequestError("画像を検証できません。1536×2288 pxの破損していない静止PNG / WebPを指定してください。", 400);
  }
}
export async function readPetUpload(request: Request) {
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().startsWith("multipart/form-data;")) throw new PetRequestError("ペットの送信形式が不正です。", 415);
  const bytes = await boundedBody(request);
  let form: FormData;
  try { form = await new Response(new Uint8Array(bytes), { headers: { "content-type": contentType } }).formData(); }
  catch { throw new PetRequestError("アップロードデータが破損しています。", 400); }
  const manifestText = form.get("manifest");
  const image = form.get("image");
  if ([...form.keys()].length !== 2 || typeof manifestText !== "string" || !(image instanceof Blob)) {
    throw new PetRequestError("定義と画像を1つずつ送信してください。", 400);
  }
  if (Buffer.byteLength(manifestText) > PET_MANIFEST_LIMIT) throw new PetRequestError("pet.jsonは16 KiB以下にしてください。", 413);
  let manifest;
  try { manifest = parseManifest(manifestText); }
  catch { throw new PetRequestError("pet.jsonが不正です。Codex v2の定義と安全な相対パスを指定してください。", 400); }
  if (image.size > PET_IMAGE_LIMIT) throw new PetRequestError("画像は12 MiB以下にしてください。", 413);
  return { manifest, image: await validatePetImage(Buffer.from(await image.arrayBuffer())) };
}

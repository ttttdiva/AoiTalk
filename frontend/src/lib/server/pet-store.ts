import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { link, mkdir, open, rename, unlink, type FileHandle } from "node:fs/promises";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { parseManifest, type PetManifest } from "../pets/codex-pet";
import { PET_IMAGE_LIMIT, PET_MANIFEST_LIMIT, PetRequestError, parseServerPet, type ServerPet } from "../pets/pet-server-contract";

type Header = ServerPet & { format: 1; imageBytes: number; sha256: string };
const MAGIC = Buffer.from("AOIPET01");
const PREFIX_SIZE = MAGIC.length + 4;
const HEADER_LIMIT = PET_MANIFEST_LIMIT + 1024;
const notFound = (error: unknown) => (error as NodeJS.ErrnoException)?.code === "ENOENT";

/** Resolve only a persistent application-owned directory, never .next/public/tmp. */
export function petDataDirectory(): string {
  const configured = process.env.AOITALK_PET_DATA_DIR?.trim();
  if (configured) {
    if (!isAbsolute(configured)) throw new Error("AOITALK_PET_DATA_DIRには絶対パスが必要です。");
    return resolve(configured);
  }
  let root = resolve(process.cwd());
  for (;;) {
    if (existsSync(join(root, "main.py")) && existsSync(join(root, "src"))) return join(root, "data", "web-pets");
    const parent = dirname(root);
    if (parent === root) throw new Error("ペットの永続保存先を特定できません。AOITALK_PET_DATA_DIRを設定してください。");
    root = parent;
  }
}

async function readExactly(file: FileHandle, size: number, position: number): Promise<Buffer> {
  const buffer = Buffer.alloc(size);
  let offset = 0;
  while (offset < size) {
    const result = await file.read(buffer, offset, size - offset, position + offset);
    if (!result.bytesRead) throw new Error("ペット保存ファイルが途中で切れています。");
    offset += result.bytesRead;
  }
  return buffer;
}
async function readHeader(file: FileHandle): Promise<{ header: Header; offset: number }> {
  const stat = await file.stat();
  if (!stat.isFile() || stat.size > PREFIX_SIZE + HEADER_LIMIT + PET_IMAGE_LIMIT) throw new Error("ペット保存形式が不正です。");
  const prefix = await readExactly(file, PREFIX_SIZE, 0);
  const length = prefix.readUInt32BE(MAGIC.length);
  if (!prefix.subarray(0, MAGIC.length).equals(MAGIC) || !length || length > HEADER_LIMIT) throw new Error("ペット保存形式が不正です。");
  const raw = JSON.parse((await readExactly(file, length, PREFIX_SIZE)).toString("utf8")) as Header;
  const pet = parseServerPet(raw);
  if (!pet || raw.format !== 1 || !Number.isSafeInteger(raw.imageBytes) || raw.imageBytes <= 0 || raw.imageBytes > PET_IMAGE_LIMIT ||
      typeof raw.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(raw.sha256) || stat.size !== PREFIX_SIZE + length + raw.imageBytes) {
    throw new Error("ペット保存形式が不正です。");
  }
  return { header: { ...raw, ...pet }, offset: PREFIX_SIZE + length };
}
const publicPet = ({ revision, manifest, updatedAt }: ServerPet): ServerPet => ({ revision, manifest, updatedAt });

/** One server-wide asset. Readers always observe a whole old or whole new file. */
export class ServerPetStore {
  constructor(private readonly directory: () => string = petDataDirectory) {}
  private path() { return join(this.directory(), "registered-pet.asset"); }
  async metadata(): Promise<ServerPet | null> {
    let file: FileHandle;
    try { file = await open(this.path(), "r"); }
    catch (error) { if (notFound(error)) return null; throw error; }
    try { return publicPet((await readHeader(file)).header); }
    finally { await file.close(); }
  }
  async image(revision: string): Promise<Buffer> {
    let file: FileHandle;
    try { file = await open(this.path(), "r"); }
    catch (error) { if (notFound(error)) throw new PetRequestError("サーバーにペットは登録されていません。", 404); throw error; }
    try {
      const { header, offset } = await readHeader(file);
      if (header.revision !== revision) throw new PetRequestError("サーバーのペットが更新されました。再読み込みしてください。", 409);
      const image = await readExactly(file, header.imageBytes, offset);
      if (createHash("sha256").update(image).digest("hex") !== header.sha256) throw new Error("保存画像の検証に失敗しました。");
      return image;
    } finally { await file.close(); }
  }
  /** Call only after full upload validation. createOnly is atomic across processes. */
  async save(manifest: PetManifest, image: Buffer, createOnly = false): Promise<ServerPet> {
    if (!image.length || image.length > PET_IMAGE_LIMIT) throw new PetRequestError("画像は12 MiB以下にしてください。", 413);
    const header: Header = {
      format: 1, revision: randomUUID(), updatedAt: new Date().toISOString(),
      manifest: parseManifest(JSON.stringify(manifest)), imageBytes: image.length,
      sha256: createHash("sha256").update(image).digest("hex"),
    };
    const metadata = Buffer.from(JSON.stringify(header));
    if (metadata.length > HEADER_LIMIT) throw new PetRequestError("ペット情報が大きすぎます。", 413);
    const prefix = Buffer.alloc(PREFIX_SIZE);
    MAGIC.copy(prefix); prefix.writeUInt32BE(metadata.length, MAGIC.length);
    const directory = this.directory();
    await mkdir(directory, { recursive: true, mode: 0o700 });
    const temporary = join(directory, `.upload-${randomUUID()}.tmp`);
    try {
      const file = await open(temporary, "wx", 0o600);
      try { await file.writeFile(Buffer.concat([prefix, metadata, image])); await file.sync(); }
      finally { await file.close(); }
      if (createOnly) {
        try { await link(temporary, this.path()); }
        catch (error) {
          if ((error as NodeJS.ErrnoException).code === "EEXIST") throw new PetRequestError("このサーバーには既にペットが登録されています。確認してから置き換えてください。", 409);
          throw error;
        }
      } else {
        // Never unlink the old asset before rename: any failed write preserves it.
        await rename(temporary, this.path());
      }
      return publicPet(header);
    } finally { await unlink(temporary).catch(() => {}); }
  }
  async remove(): Promise<void> {
    try { await unlink(this.path()); }
    catch (error) { if (!notFound(error)) throw error; }
  }
}

import { Blob } from "node:buffer";
import { createHash } from "node:crypto";
import { expect, it } from "vitest";
import { hashMediaFile } from "./media-file-hash";
it("pins different bytes even when filename/size/date/MIME metadata is identical", async () => {
  const first = new Blob([new Uint8Array([1,2,3])]); const second = new Blob([new Uint8Array([1,2,4])]);
  expect(await hashMediaFile(first)).toBe(createHash("sha256").update(new Uint8Array([1,2,3])).digest("hex"));
  expect(await hashMediaFile(first)).not.toBe(await hashMediaFile(second));
});
it("hashes a multi-chunk video without reading its entire body at once and cancels a changed scope", async () => {
  const bytes = new Uint8Array(2 * 1024 * 1024 + 3).fill(73); const file = new Blob([bytes]);
  const sizes: number[] = []; const original = file.slice.bind(file);
  file.slice = (start,end,type) => { sizes.push(Number(end)-Number(start)); return original(start,end,type); };
  expect(await hashMediaFile(file)).toBe(createHash("sha256").update(bytes).digest("hex"));
  expect(Math.max(...sizes)).toBeLessThanOrEqual(1024 * 1024); expect(sizes).toHaveLength(3);
  await expect(hashMediaFile(file,()=>false)).rejects.toThrow("選択が変わった");
});

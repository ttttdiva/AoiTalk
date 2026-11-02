import * as Crypto from "expo-crypto";

/**
 * Compact the eight per-table row-set receipts into one request-safe token.
 * The server expands its own current receipts and hashes the same ordered
 * representation before accepting the no-op path.
 */
export async function createDocsFastDigestBundle(
  digests: Readonly<Record<string, string>>,
  tables: readonly string[],
): Promise<string | null> {
  if (
    Object.keys(digests).length !== tables.length
    || !tables.every((table) => /^[a-f0-9]{64}$/.test(digests[table] ?? ""))
  ) {
    return null;
  }

  const material = tables.map((table) => `${table}:${digests[table]}`).join("\n");
  const bundle = await Crypto.digestStringAsync(
    Crypto.CryptoDigestAlgorithm.SHA256,
    material,
  );
  return /^[a-f0-9]{64}$/.test(bundle) ? bundle : null;
}

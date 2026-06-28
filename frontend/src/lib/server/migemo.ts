import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import {
  buildFallbackMigemoTerms,
  buildMigemoTermsFromEntries,
  type MigemoDictionaryEntry,
} from "@/lib/migemo-lite";

const DEFAULT_MIGEMO_DIR = "C:/Asr/Plugin/migemo-0.40";
const DEFAULT_LIMIT = 240;

let cachedEntries: MigemoDictionaryEntry[] | null = null;
let loadAttempted = false;

export async function getMigemoTerms(
  query: string,
  limit = DEFAULT_LIMIT,
): Promise<{ terms: string[]; dictionaryAvailable: boolean }> {
  const trimmed = query.trim();
  if (!trimmed) {
    return { terms: [], dictionaryAvailable: false };
  }

  const entries = await loadMigemoDictionary();
  if (!entries.length) {
    return {
      terms: buildFallbackMigemoTerms(trimmed).slice(0, limit),
      dictionaryAvailable: false,
    };
  }

  return {
    terms: buildMigemoTermsFromEntries(trimmed, entries, limit),
    dictionaryAvailable: true,
  };
}

async function loadMigemoDictionary(): Promise<MigemoDictionaryEntry[]> {
  if (cachedEntries) return cachedEntries;
  if (loadAttempted) return [];
  loadAttempted = true;

  const dictPath = await resolveMigemoDictPath();
  if (!dictPath) {
    cachedEntries = [];
    return cachedEntries;
  }

  try {
    const bytes = await readFile(dictPath);
    const view = bytes.buffer.slice(
      bytes.byteOffset,
      bytes.byteOffset + bytes.byteLength,
    );
    const text = new TextDecoder("euc-jp").decode(view);
    cachedEntries = text
      .split(/\r?\n/)
      .filter((line) => line && !line.startsWith(";"))
      .map((line) => {
        const [key, ...values] = line.split("\t").filter(Boolean);
        return key ? { key, values } : null;
      })
      .filter((entry): entry is MigemoDictionaryEntry => !!entry);
  } catch {
    cachedEntries = [];
  }
  return cachedEntries;
}

async function resolveMigemoDictPath(): Promise<string | null> {
  const configured =
    process.env.AOITALK_MIGEMO_DICT_PATH ||
    process.env.AOITALK_MIGEMO_DIR ||
    DEFAULT_MIGEMO_DIR;

  const candidates = [configured, path.join(configured, "migemo-dict")].filter(
    Boolean,
  );

  for (const candidate of candidates) {
    try {
      const candidateStat = await stat(candidate);
      if (candidateStat.isFile()) return candidate;
    } catch {
      // Try next candidate.
    }
  }
  return null;
}

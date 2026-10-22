import { createHash } from "node:crypto";

const POSTGRES_SCHEMA_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

export const EPHEMERAL_DATABASE_PREFIXES = [
  "aoitalk_canonical_",
  "aoitalk_e2e_",
  "aoitalk_test_",
  "aoitalk_gc_",
] as const;

export type VerificationStorageTarget = {
  database: string;
  schema: string;
};

type StorageEnvironment = {
  DATABASE_URL?: string;
  POSTGRES_DB?: string;
  POSTGRES_SCHEMA?: string;
};

function nonEmpty(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function databaseFromUrl(value: string | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const parsed = new URL(value);
    const database = decodeURIComponent(parsed.pathname.replace(/^\//, ""));
    return nonEmpty(database);
  } catch {
    return undefined;
  }
}

export function resolveVerificationStorageTarget(
  environment: StorageEnvironment,
): VerificationStorageTarget | null {
  const database = databaseFromUrl(nonEmpty(environment.DATABASE_URL)) ??
    nonEmpty(environment.POSTGRES_DB);
  if (!database) return null;

  const rawSchema = nonEmpty(environment.POSTGRES_SCHEMA) ?? "public";
  if (!POSTGRES_SCHEMA_RE.test(rawSchema)) return null;

  return {
    // PostgreSQL folds unquoted identifiers to lower case, but quoted
    // database/schema names remain case-sensitive. Keep the exact values in
    // the fingerprint so a differently cased target cannot pass the probe.
    database,
    schema: rawSchema,
  };
}

export function storageFingerprint(target: VerificationStorageTarget): string {
  return createHash("sha256")
    .update(`${target.database}\u0000${target.schema}`, "utf8")
    .digest("hex");
}

export function isEphemeralDatabase(database: string): boolean {
  const normalized = database.trim().toLowerCase();
  return EPHEMERAL_DATABASE_PREFIXES.some((prefix) =>
    normalized.startsWith(prefix),
  );
}

export function assertVerificationStorageTarget(
  target: VerificationStorageTarget | null,
  options: { requireEphemeralDatabase?: boolean } = {},
): asserts target is VerificationStorageTarget {
  if (!target) {
    throw new Error(
      "verification requires a known PostgreSQL database and valid schema; refusing unverifiable storage",
    );
  }

  const schemaIsolated = target.schema.toLowerCase() !== "public";
  if (options.requireEphemeralDatabase && !isEphemeralDatabase(target.database)) {
    throw new Error(
      "verification requires an ephemeral PostgreSQL database; refusing normal or persistent storage",
    );
  }
  if (!schemaIsolated && !isEphemeralDatabase(target.database)) {
    throw new Error(
      "verification requires an ephemeral PostgreSQL database or non-public schema; refusing normal runtime storage",
    );
  }
}

export function assertVerifiedStorageFingerprint(
  expected: string,
  response: unknown,
): void {
  if (!/^[0-9a-f]{64}$/.test(expected)) {
    throw new Error("verification storage probe fingerprint is invalid");
  }
  if (
    !response ||
    typeof response !== "object" ||
    Array.isArray(response) ||
    (response as { verified?: unknown }).verified !== true
  ) {
    throw new Error(
      "verification storage probe could not confirm the isolated server connection",
    );
  }
}

/**
 * Shared retention policy for content deletion tombstones.
 *
 * This module is server-only.  Keep the environment parsing deliberately
 * conservative: a typo must never turn an automatic cleanup into an
 * effectively unbounded purge (or an immediate purge).
 */

export const DEFAULT_DELETION_RETENTION_DAYS = 30;

// Ten years is intentionally generous while still bounding accidental values
// such as milliseconds, an empty-shell expansion, or an untrusted deployment
// override.  Values outside this range fail closed to the documented default.
export const MAX_DELETION_RETENTION_DAYS = 3650;

export function readDeletionRetentionDays(
  rawValue: string | undefined = process.env.AOITALK_DELETION_RETENTION_DAYS,
): number {
  if (typeof rawValue !== "string" || !rawValue.trim()) {
    return DEFAULT_DELETION_RETENTION_DAYS;
  }

  const value = Number(rawValue.trim());
  if (
    !Number.isSafeInteger(value) ||
    value <= 0 ||
    value > MAX_DELETION_RETENTION_DAYS
  ) {
    return DEFAULT_DELETION_RETENTION_DAYS;
  }
  return value;
}

/**
 * Shared ID formatting/matching helpers for entity pickers.
 *
 * UUIDs are accepted in canonical (hyphenated) and compact (32 hex)
 * representations.  A short hexadecimal query is deliberately not treated
 * as an ID prefix: eight characters is the minimum used by the web pickers
 * so ordinary text searches do not fan out across an entire workspace.
 */

export const MIN_ENTITY_ID_PREFIX_LENGTH = 8;
export const SHORT_ENTITY_ID_LENGTH = MIN_ENTITY_ID_PREFIX_LENGTH;

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const COMPACT_UUID_PATTERN = /^[0-9a-f]{32}$/i;
const UUID_PREFIX_PATTERN = /^[0-9a-f]{8,32}$/i;

/** Trim and case-fold an entity id without changing its representation. */
export function normalizeEntityId(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

/** Return true for a canonical, versioned UUID. */
export function isCanonicalUuid(value: unknown): boolean {
  return typeof value === "string" && UUID_PATTERN.test(value.trim());
}

/** Return true for a compact 32-hex UUID. */
export function isCompactUuid(value: unknown): boolean {
  return typeof value === "string" && COMPACT_UUID_PATTERN.test(value.trim());
}

/** Convert a canonical UUID (or a UUID-like value) to its compact form. */
export function compactEntityId(value: unknown): string {
  return normalizeEntityId(value).replace(/-/g, "");
}

/** Convert a full compact UUID to canonical form for typed UUID columns. */
export function canonicalEntityId(value: unknown): string | null {
  const normalized = normalizeEntityId(value);
  if (isCanonicalUuid(normalized)) return normalized;
  if (!isCompactUuid(normalized)) return null;
  return [
    normalized.slice(0, 8),
    normalized.slice(8, 12),
    normalized.slice(12, 16),
    normalized.slice(16, 20),
    normalized.slice(20),
  ].join("-");
}

/**
 * Format the stable short ID shown next to task/Docs candidates.
 * Non-UUID IDs retain their existing spelling (after normalization), while
 * UUIDs use the first eight compact characters.
 */
export function shortEntityId(value: unknown): string {
  const normalized = normalizeEntityId(value);
  if (!isFullUuid(normalized))
    return normalized.slice(0, SHORT_ENTITY_ID_LENGTH) || normalized;
  const compact = compactEntityId(normalized);
  return compact.slice(0, SHORT_ENTITY_ID_LENGTH) || normalized;
}

/**
 * Whether a query is safe to interpret as a UUID prefix.
 * Hyphens are ignored so both canonical prefixes and compact prefixes work,
 * but at least eight hexadecimal characters are required.
 */
export function isUuidPrefixQuery(value: unknown): boolean {
  const compact = compactEntityId(value);
  return UUID_PREFIX_PATTERN.test(compact);
}

export type EntityIdMatch = "exact" | "prefix" | null;

/**
 * Match an entity ID exactly or by a bounded UUID prefix.
 * Full canonical/compact UUIDs are exact matches; an eight-plus hex query is
 * a prefix match.  Malformed/short values return null rather than matching
 * arbitrary text IDs.
 */
export function matchEntityId(
  candidate: unknown,
  query: unknown,
): EntityIdMatch {
  const candidateId = normalizeEntityId(candidate);
  const queryId = normalizeEntityId(query);
  if (!candidateId || !queryId) return null;

  const candidateCompact = compactEntityId(candidateId);
  const queryCompact = compactEntityId(queryId);
  if (!candidateCompact || !queryCompact) return null;

  const candidateIsUuid =
    isCanonicalUuid(candidateId) || isCompactUuid(candidateId);
  const queryIsFullUuid = isCanonicalUuid(queryId) || isCompactUuid(queryId);
  if (candidateIsUuid && queryIsFullUuid && candidateCompact === queryCompact) {
    return "exact";
  }

  if (
    candidateIsUuid &&
    isUuidPrefixQuery(queryId) &&
    candidateCompact.startsWith(queryCompact)
  ) {
    return queryCompact.length === candidateCompact.length ? "exact" : "prefix";
  }

  return null;
}

/** True when a value is a full canonical or compact UUID. */
export function isFullUuid(value: unknown): boolean {
  return isCanonicalUuid(value) || isCompactUuid(value);
}

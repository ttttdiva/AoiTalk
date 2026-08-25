/** URL normalization shared by local clip extraction and outbox provenance. */

const TRACKING_QUERY_PREFIX = "utm_";
// Keep this in lockstep with docs_sync._SOURCE_REF_QUERY_SECRET_RE.  A URL
// containing one of these query keys is rejected rather than persisted: the
// server intentionally returns 400 for the same provenance payload.
const SOURCE_REF_QUERY_SECRET_PATTERN =
  /(?:token|secret|password|passwd|key|auth|signature|sig|cookie|credential)/i;
const PROMPT_URL_PATTERN = /https?:\/\/[^\s<>"'`]+/gi;

function hasSensitiveUrlPart(raw: string): boolean {
  try {
    const parsed = new URL(raw);
    if (parsed.username || parsed.password) return true;
    // canonicalizeIngestUrl decodes query keys through URLSearchParams, so
    // percent-encoded names such as `%61ccess_token` are covered too.
    if (!canonicalizeIngestUrl(raw)) return true;
    // URL fragments are not sent to the server, but they may still contain
    // bearer/refresh credentials before prompt construction. Redact them
    // before a cloud LLM sees the source.
    const decodedHash = decodeURIComponent(parsed.hash);
    return SOURCE_REF_QUERY_SECRET_PATTERN.test(decodedHash);
  } catch {
    // A malformed URL is not safe to forward to a remote model. The caller
    // keeps the original source for local verbatim/hash storage instead.
    return true;
  }
}

function compareCodeUnits(left: string, right: string): number {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

/**
 * Canonicalize an ingest URL while rejecting credentials and secret query
 * keys. Empty string means the URL is unsafe or malformed and must not enter
 * local provenance/outbox payloads.
 */
export function canonicalizeIngestUrl(value: string): string {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  try {
    const parsed = new URL(raw);
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:")
      || !parsed.hostname
      || parsed.username
      || parsed.password
    ) {
      return "";
    }

    const params = Array.from(parsed.searchParams.entries());
    for (const [key] of params) {
      if (SOURCE_REF_QUERY_SECRET_PATTERN.test(key)) return "";
    }
    const canonicalParams = params
      .filter(([key]) => {
        const lower = key.toLocaleLowerCase();
        return !lower.startsWith(TRACKING_QUERY_PREFIX)
          && lower !== "fbclid"
          && lower !== "gclid";
      })
      .sort(([leftKey, leftValue], [rightKey, rightValue]) =>
        compareCodeUnits(leftKey, rightKey) || compareCodeUnits(leftValue, rightValue),
      );
    const query = new URLSearchParams(canonicalParams).toString();
    const pathname = parsed.pathname.replace(/\/+$/, "") || "/";
    return `${parsed.protocol.toLocaleLowerCase()}//${parsed.hostname.toLocaleLowerCase()}${
      parsed.port ? `:${parsed.port}` : ""
    }${pathname}${query ? `?${query}` : ""}`;
  } catch {
    return "";
  }
}

/** Validate URL-looking metadata without changing its original safe text. */
export function isSafeSourceRefUrl(value: string): boolean {
  const raw = String(value ?? "").trim();
  if (!raw.includes("://")) return true;
  return Boolean(canonicalizeIngestUrl(raw));
}

/**
 * Redact only unsafe URL tokens for cloud prompts. Newlines are never added or
 * removed, so numbered source line anchors remain stable; the original source
 * remains untouched for exact verbatim/hash persistence.
 */
export function redactSensitiveUrlsForPrompt(value: string): string {
  return String(value ?? "").replace(PROMPT_URL_PATTERN, (raw) => {
    const candidate = raw.replace(/[),.。、」』】\]]+$/, "");
    return hasSensitiveUrlPart(candidate) ? "[redacted-url]" : raw;
  });
}

/** Recursively redact URL-bearing prompt metadata (evidence/candidates/history). */
export function redactPromptValue<T>(value: T): T {
  if (typeof value === "string") return redactSensitiveUrlsForPrompt(value) as T;
  if (Array.isArray(value)) {
    return value.map((item) => redactPromptValue(item)) as T;
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .map(([key, item]) => [key, redactPromptValue(item)]),
    ) as T;
  }
  return value;
}

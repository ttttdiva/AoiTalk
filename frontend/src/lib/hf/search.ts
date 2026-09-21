/**
 * Shared Hugging Face search primitives.
 *
 * The Files search UI has a bounded result set and matches entry names rather
 * than reading file contents.  Keep the matcher in a small, dependency-free
 * module so the browser loader and the authenticated route cannot drift apart.
 */

export const HF_SEARCH_DEFAULT_LIMIT = 50;
export const HF_SEARCH_MAX_LIMIT = 200;

/**
 * Normalize the result limit at the API boundary.
 *
 * `undefined`, non-finite values, and non-positive values use the same safe
 * defaults as the Files endpoint.  A caller can never request more than the
 * bounded HF result window.
 */
export function normalizeHfSearchLimit(limit?: number): number {
  if (limit === undefined || !Number.isFinite(limit)) {
    return HF_SEARCH_DEFAULT_LIMIT;
  }
  return Math.min(HF_SEARCH_MAX_LIMIT, Math.max(1, Math.floor(limit)));
}

function escapeRegExp(value: string): string {
  return value.replace(/[\\^$.*+?()[\]{}|]/g, "\\$&");
}

/** Convert the Files search `*`/`?` spelling into a case-insensitive glob. */
function compileGlob(query: string): RegExp {
  let source = "^";
  for (const character of query) {
    if (character === "*") {
      source += ".*";
    } else if (character === "?") {
      source += ".";
    } else {
      source += escapeRegExp(character);
    }
  }
  source += "$";
  return new RegExp(source, "i");
}

/**
 * Build the name predicate used by both local/root and remote/repository
 * searches.
 *
 * Plain queries are case-insensitive substring matches.  To mirror the local
 * Files search, a plain query containing `*` or `?` is treated as a glob.  A
 * regex query is compiled with the same case-insensitive semantics and lets
 * its syntax/error reach the caller (the route turns invalid patterns into a
 * 400 response).
 */
export function createHfNameMatcher(
  query: string,
  regex = false,
): (name: string) => boolean {
  const normalizedQuery = String(query ?? "").trim();
  if (regex) {
    let pattern: RegExp;
    try {
      pattern = new RegExp(normalizedQuery, "i");
    } catch {
      throw new Error("正規表現が不正です");
    }
    return (name: string) => pattern.test(name);
  }

  if (normalizedQuery.includes("*") || normalizedQuery.includes("?")) {
    const pattern = compileGlob(normalizedQuery);
    return (name: string) => pattern.test(name);
  }

  const loweredQuery = normalizedQuery.toLowerCase();
  return (name: string) => name.toLowerCase().includes(loweredQuery);
}

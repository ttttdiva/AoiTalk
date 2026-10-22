const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export type CanonicalAppLink = {
  appId: string;
  projectId?: string;
};

export type CanonicalAppLinkOptions = {
  /** Explicit Web base paths accepted in addition to the root UI path. */
  basePaths?: readonly string[];
  /** Absolute origins are accepted only when explicitly allowlisted. */
  allowedOrigins?: readonly string[];
};

function normalizeBasePath(value: string): string {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "/") return "";
  if (!trimmed.startsWith("/")) return `/${trimmed.replace(/\/+$/g, "")}`;
  return trimmed.replace(/\/+$/g, "");
}

function validUuid(value: string | null): value is string {
  return value !== null && UUID_PATTERN.test(value);
}

/**
 * Parse only the canonical App UI link. API JSON URLs, arbitrary absolute
 * links and paths with unknown query parameters stay external links.
 */
export function parseCanonicalAppLink(
  href: string,
  options: CanonicalAppLinkOptions = {},
): CanonicalAppLink | null {
  if (typeof href !== "string" || !href.trim() || href.startsWith("//")) return null;
  const basePaths = Array.from(new Set(["", "/at", ...(options.basePaths ?? [])].map(normalizeBasePath)));
  let parsed: URL;
  let absolute = false;
  try {
    parsed = new URL(href, "https://aoitalk.invalid");
    try {
      absolute = /^https?:$/i.test(new URL(href).protocol);
    } catch {
      absolute = false;
    }
  } catch {
    return null;
  }
  if (absolute) {
    const origin = parsed.origin.toLowerCase();
    const allowed = (options.allowedOrigins ?? []).some((candidate) => {
      try {
        return new URL(candidate).origin.toLowerCase() === origin;
      } catch {
        return false;
      }
    });
    if (!allowed) return null;
  } else if (!href.startsWith("/")) {
    return null;
  }
  if (parsed.username || parsed.password || parsed.hash) return null;
  const pathname = parsed.pathname.replace(/\/+$/g, "") || "/";
  let appId: string | null = null;
  for (const basePath of basePaths) {
    const prefix = `${basePath}/apps/`;
    if (!pathname.startsWith(prefix)) continue;
    const candidate = pathname.slice(prefix.length);
    if (candidate && !candidate.includes("/") && validUuid(candidate)) {
      appId = candidate;
      break;
    }
  }
  if (!appId) return null;
  const queryKeys = Array.from(parsed.searchParams.keys());
  if (queryKeys.some((key) => key !== "project_id") || parsed.searchParams.getAll("project_id").length > 1) return null;
  const projectId = parsed.searchParams.get("project_id");
  if (projectId !== null && !validUuid(projectId)) return null;
  return projectId ? { appId, projectId } : { appId };
}

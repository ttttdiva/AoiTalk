const APP_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

/** Build the only user-facing App URL accepted from a trusted App reference. */
export function appHref(appId: string | null | undefined, projectId?: string | null): string | null {
  if (!appId || !APP_ID_RE.test(appId)) return null;
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  // Next's Link/router applies the configured basePath exactly once.
  return `/apps/${appId}${query}`;
}

/** Validate a relative Markdown App URL before handing it to Next Link. */
export function canonicalAppHref(value: string | null | undefined): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return null;
  try {
    const url = new URL(value, "https://aoitalk.invalid");
    if (url.hash) return null;
    const pathname = url.pathname.startsWith("/at/apps/") ? url.pathname.slice("/at".length) : url.pathname;
    const match = pathname.match(/^\/apps\/([^/]+)$/iu);
    if (!match || !APP_ID_RE.test(match[1])) return null;
    const projectIds = url.searchParams.getAll("project_id");
    if (Array.from(url.searchParams.keys()).some((key) => key !== "project_id") || projectIds.length > 1) return null;
    return appHref(match[1], projectIds[0] || null);
  } catch {
    return null;
  }
}

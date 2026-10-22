import { withBasePath } from "@/lib/base-path";

const DEFAULT_TITLE = "AoiTalk";
const DEFAULT_ICON = "/favicon.ico";
export const CANONICAL_PORTAL_HOSTNAME = "az-vnetspace.com";
export const CANONICAL_PORTAL_TITLE = "Azure ネットワーク技術 Wiki | AZ VNet Space";
export const CANONICAL_PORTAL_ICON = "/images/ui/az-vnetspace.svg";

export const EXTERNAL_BROWSER_TITLE =
  process.env.NEXT_PUBLIC_BROWSER_TITLE?.trim() || DEFAULT_TITLE;
export const EXTERNAL_BROWSER_ICON = withBasePath(
  process.env.NEXT_PUBLIC_BROWSER_ICON?.trim() || DEFAULT_ICON,
);

function normalizeHostname(value: string): string {
  return value.toLowerCase().replace(/\.$/u, "");
}

function parseConfiguredOrigin(value: string): URL | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    url.hostname = normalizeHostname(url.hostname);
    return url;
  } catch {
    return null;
  }
}

const EXTERNAL_BRANDING_URLS = (process.env.NEXT_PUBLIC_BROWSER_BRANDING_ORIGINS ?? "")
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean)
  .map(parseConfiguredOrigin)
  .filter((value): value is URL => value !== null);

const EXTERNAL_BRANDING_ORIGINS = new Set(
  EXTERNAL_BRANDING_URLS.map((url) => url.origin),
);
const EXTERNAL_BRANDING_HOSTNAMES = new Set(
  EXTERNAL_BRANDING_URLS.map((url) => normalizeHostname(url.hostname)),
);

export function isExternalBrandingHostname(hostname: string): boolean {
  return EXTERNAL_BRANDING_HOSTNAMES.has(normalizeHostname(hostname));
}

export function isCanonicalPortalHostname(hostname: string): boolean {
  return normalizeHostname(hostname) === CANONICAL_PORTAL_HOSTNAME;
}

function isConfiguredOrigin(value: string): boolean {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" && url.protocol !== "http:") return false;
    url.hostname = normalizeHostname(url.hostname);
    return EXTERNAL_BRANDING_ORIGINS.has(url.origin);
  } catch {
    return false;
  }
}

// Presentation only: these headers must never be used for authorization.
export function usesExternalBrowserBranding(
  requestHeaders: Pick<Headers, "get">,
): boolean {
  if (EXTERNAL_BRANDING_URLS.length === 0) return false;
  const forwardedOrigin =
    requestHeaders.get("x-gateway-client-origin") ??
    requestHeaders.get("x-aoitalk-client-origin") ??
    requestHeaders.get("x-forwarded-origin");
  if (forwardedOrigin) return isConfiguredOrigin(forwardedOrigin);

  const forwardedHost =
    requestHeaders.get("x-forwarded-host") ?? requestHeaders.get("host");
  if (!forwardedHost) return false;
  const firstHost = forwardedHost.split(",", 1)[0]?.trim();
  if (!firstHost) return false;
  try {
    return isExternalBrandingHostname(new URL(`https://${firstHost}`).hostname);
  } catch {
    return false;
  }
}

function requestHostname(requestHeaders: Pick<Headers, "get">): string | null {
  const forwardedOrigin =
    requestHeaders.get("x-gateway-client-origin") ??
    requestHeaders.get("x-aoitalk-client-origin") ??
    requestHeaders.get("x-forwarded-origin");
  if (forwardedOrigin) {
    return parseConfiguredOrigin(forwardedOrigin)?.hostname ?? null;
  }

  const forwardedHost =
    requestHeaders.get("x-forwarded-host") ?? requestHeaders.get("host");
  if (!forwardedHost) return null;
  const firstHost = forwardedHost.split(",", 1)[0]?.trim();
  if (!firstHost) return null;
  try {
    return normalizeHostname(new URL(`https://${firstHost}`).hostname);
  } catch {
    return null;
  }
}

export function usesCanonicalPortalBranding(
  requestHeaders: Pick<Headers, "get">,
): boolean {
  // Deployment-provided branding always wins, even if the configured origin
  // happens to use the canonical portal hostname.
  if (usesExternalBrowserBranding(requestHeaders)) return false;
  return requestHostname(requestHeaders) === CANONICAL_PORTAL_HOSTNAME;
}

export type ResolvedBrowserBranding = {
  kind: "configured" | "canonical-portal";
  title: string;
  icon: string;
};

export function resolveBrowserBranding(
  requestHeaders: Pick<Headers, "get">,
): ResolvedBrowserBranding | null {
  if (usesExternalBrowserBranding(requestHeaders)) {
    return {
      kind: "configured",
      title: EXTERNAL_BROWSER_TITLE,
      icon: EXTERNAL_BROWSER_ICON,
    };
  }
  if (usesCanonicalPortalBranding(requestHeaders)) {
    return {
      kind: "canonical-portal",
      title: CANONICAL_PORTAL_TITLE,
      // This root URL intentionally bypasses withBasePath: the canonical host
      // serves the shared portal asset directly at the origin root.
      icon: CANONICAL_PORTAL_ICON,
    };
  }
  return null;
}

// Backward-compatible exports for components and tests while deployment-owned
// values move to 85_CaddyGateway configuration.
export const AZURE_WIKI_TITLE = EXTERNAL_BROWSER_TITLE;
export const AZURE_WIKI_ICON = EXTERNAL_BROWSER_ICON;
export function isAzureWikiHostname(hostname: string): boolean {
  return (
    isExternalBrandingHostname(hostname) || isCanonicalPortalHostname(hostname)
  );
}
export function usesAzureWikiBranding(
  requestHeaders: Pick<Headers, "get">,
): boolean {
  return resolveBrowserBranding(requestHeaders) !== null;
}

export const AZURE_WIKI_TITLE = "Azure ネットワーク技術 Wiki | AZ VNet Space";
export const AZURE_WIKI_ICON = "/images/ui/az-vnetspace.svg";

export function isAzureWikiHostname(hostname: string): boolean {
  return hostname.toLowerCase().replace(/\.$/, "") === "az-vnetspace.com";
}

// Presentation only: these headers must never be used for authorization.
export function usesAzureWikiBranding(headers: Pick<Headers, "get">): boolean {
  // The Wiki Worker supplies the visible origin; Tunnel/Caddy changes Host
  // and X-Forwarded-Host to localhost before the request reaches Next.js.
  const origin =
    headers.get("x-aoitalk-client-origin") ?? headers.get("x-forwarded-origin");
  const host = headers.get("x-forwarded-host") ?? headers.get("host");
  try {
    const url = new URL(origin ?? `https://${host?.split(",")[0].trim()}`);
    return (
      (url.protocol === "https:" || url.protocol === "http:") &&
      isAzureWikiHostname(url.hostname)
    );
  } catch {
    return false;
  }
}

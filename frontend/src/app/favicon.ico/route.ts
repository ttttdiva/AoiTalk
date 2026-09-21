import { NextRequest, NextResponse } from "next/server";
import { AZURE_WIKI_ICON, usesAzureWikiBranding } from "@/lib/browser-branding";

export function GET(request: NextRequest) {
  const icon = usesAzureWikiBranding(request.headers)
    ? AZURE_WIKI_ICON
    : "/images/ui/aoitalk.ico";
  // Relative redirects survive the Wiki Worker's server-side origin hop.
  return new NextResponse(null, {
    status: 307,
    headers: { Location: icon, "Cache-Control": "private, no-store" },
  });
}

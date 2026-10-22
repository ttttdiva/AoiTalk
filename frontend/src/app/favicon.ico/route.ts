import { NextRequest, NextResponse } from "next/server";
import { resolveBrowserBranding } from "@/lib/browser-branding";
import { withBasePath } from "@/lib/base-path";

export function GET(request: NextRequest) {
  const icon =
    resolveBrowserBranding(request.headers)?.icon ??
    withBasePath("/images/ui/app.ico");
  // Relative redirects survive server-side origin hops through a gateway.
  return new NextResponse(null, {
    status: 307,
    headers: { Location: icon, "Cache-Control": "private, no-store" },
  });
}

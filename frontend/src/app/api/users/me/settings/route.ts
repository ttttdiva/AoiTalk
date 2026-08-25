import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

const NO_STORE_HEADERS = {
  "Cache-Control": "no-store, no-cache, must-revalidate",
};

async function proxyUserSettings(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "認証が必要です" },
      { status: 401, headers: NO_STORE_HEADERS },
    );
  }

  return proxyRequestToPythonApi(request, {
    path: ["users", "me", "settings"],
    user,
  });
}

export async function GET(request: NextRequest) {
  return proxyUserSettings(request);
}

export async function PATCH(request: NextRequest) {
  return proxyUserSettings(request);
}

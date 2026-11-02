import { NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getExternalAuthHandoffClientRegistryStatus } from "@/lib/server/auth-handoff-clients";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

function noStoreJson(body: unknown, status = 200) {
  return NextResponse.json(body, {
    status,
    headers: {
      "cache-control": "no-store, no-cache, must-revalidate",
      pragma: "no-cache",
      expires: "0",
    },
  });
}

export async function GET() {
  const user = await getSession();
  if (!user) {
    return noStoreJson({ detail: "認証が必要です" }, 401);
  }
  if (user.role !== "admin") {
    return noStoreJson({ detail: "管理者権限が必要です" }, 403);
  }

  return noStoreJson(getExternalAuthHandoffClientRegistryStatus());
}

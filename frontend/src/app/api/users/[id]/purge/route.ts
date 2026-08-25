import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type RouteContext = {
  params: Promise<{ id: string }>;
};

async function requireAdmin() {
  const user = await getSession();
  if (!user) {
    return {
      error: NextResponse.json({ detail: "認証が必要です" }, { status: 401 }),
      user: null,
    };
  }
  if (user.role !== "admin") {
    return {
      error: NextResponse.json({ detail: "管理者権限が必要です" }, { status: 403 }),
      user: null,
    };
  }
  return { error: null, user };
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  const guard = await requireAdmin();
  if (guard.error) return guard.error;
  const { id } = await context.params;
  const encodedId = encodeURIComponent(id);
  return proxyRequestToPythonApi(request, {
    path: ["users", encodedId, "purge"],
    user: guard.user,
  });
}

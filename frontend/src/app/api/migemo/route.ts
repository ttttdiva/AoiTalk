import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getMigemoTerms } from "@/lib/server/migemo";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const query = request.nextUrl.searchParams.get("q") ?? "";
  const limit = Number.parseInt(
    request.nextUrl.searchParams.get("limit") ?? "240",
    10,
  );
  const safeLimit = Number.isFinite(limit)
    ? Math.max(20, Math.min(500, limit))
    : 240;
  const result = await getMigemoTerms(query, safeLimit);

  return NextResponse.json({
    success: true,
    query,
    terms: result.terms,
    dictionary_available: result.dictionaryAvailable,
  });
}

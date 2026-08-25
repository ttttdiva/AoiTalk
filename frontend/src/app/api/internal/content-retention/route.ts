import { NextResponse } from "next/server";
import { db } from "@/db";
import { docsLibraries } from "@/db/schema";
import { cleanupExpiredDeletedConversations } from "@/lib/server/conversation-retention-cleanup";
import { purgeExpiredDocsArchive } from "@/lib/server/knowledge-docs-utils";

/**
 * Internal housekeeping entrypoint called by the tracked FastAPI retention
 * worker.  Next.js remains the canonical owner of Chat/Docs SQL cleanup; the
 * Python worker only schedules this endpoint and never reimplements its SQL.
 */
export async function POST(request: Request) {
  const expected = process.env.INTERNAL_API_KEY?.trim();
  const supplied = request.headers.get("x-internal-auth")?.trim();
  if (!expected || !supplied || supplied !== expected) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const conversations = await cleanupExpiredDeletedConversations();
  const libraries = await db
    .select({ id: docsLibraries.id })
    .from(docsLibraries);
  let docsPurged = 0;
  for (const library of libraries) {
    docsPurged += await purgeExpiredDocsArchive(String(library.id));
  }

  return NextResponse.json({
    conversations,
    docs_purged: docsPurged,
  });
}

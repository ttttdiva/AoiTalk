import { and, eq, ilike, isNull, ne, or, sql } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { avatarUrl } from "@/lib/server/user-avatar";

/** Safe user lookup for mention/share pickers (never exposes credentials/roles). */
export async function GET(request: NextRequest) {
  const session = await getSession();
  if (!session) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const q = request.nextUrl.searchParams.get("q")?.trim() ?? "";
  // This endpoint is a search surface, not a directory listing.  Keeping the
  // empty-query contract explicit also protects older clients which may still
  // call the route while a picker is opening.
  if (!q) return NextResponse.json({ users: [] });
  const limit = Math.min(Math.max(Number(request.nextUrl.searchParams.get("limit")) || 20, 1), 50);
  const pattern = `%${q}%`;
  const rows = await db
    .select({
      id: users.id,
      username: users.username,
      email: users.email,
      displayName: users.displayName,
      avatarPath: users.avatarPath,
    })
    .from(users)
    .where(
      and(
        or(eq(users.isActive, true), isNull(users.isActive)),
        // The authenticated principal is not an add-share candidate.  The
        // share POST remains the ACL authority, while this keeps the picker
        // aligned with the other participant pickers.
        ne(users.id, session.id),
        // Verification provenance is server-owned and explicit.  The JSON
        // marker on users is attribution metadata, not deletion/visibility
        // authority (and can be changed through the settings API).  Consult
        // the durable artifact ledger by exact user UUID instead; legacy B/C
        // rows with only suspicious names or unproven metadata remain
        // searchable.
        sql`NOT EXISTS (
          SELECT 1
          FROM verification_artifact_provenance AS verification_artifact
          WHERE verification_artifact.entity_type = 'user'
            AND verification_artifact.entity_id = ${users.id}::text
            AND verification_artifact.disposable IS TRUE
        )`,
        or(
          ilike(users.username, pattern),
          ilike(users.email, pattern),
          ilike(users.displayName, pattern),
        ),
      ),
    )
    .orderBy(users.username)
    .limit(limit);
  return NextResponse.json({
    users: rows.map((row) => ({
      id: row.id,
      username: row.username,
      email: row.email,
      display_name: row.displayName,
      avatar_url: avatarUrl(row.id, row.avatarPath),
    })),
  });
}

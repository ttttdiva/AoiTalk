import { sql } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";

import { db } from "@/db";
import {
  assertVerifiedStorageFingerprint,
  storageFingerprint,
  type VerificationStorageTarget,
} from "@/lib/verification-storage";
import {
  fetchPythonApi,
  NO_STORE_HEADERS,
} from "@/lib/server/python-api-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

const FINGERPRINT_HEADER = "x-aoitalk-storage-fingerprint";

function failure(status: number): NextResponse {
  return NextResponse.json(
    { verified: false },
    { status, headers: NO_STORE_HEADERS },
  );
}

function readIdentity(row: unknown): VerificationStorageTarget | null {
  if (!row || typeof row !== "object" || Array.isArray(row)) return null;
  const record = row as {
    database_name?: unknown;
    schema_name?: unknown;
  };
  if (
    typeof record.database_name !== "string" ||
    typeof record.schema_name !== "string" ||
    !record.database_name.trim() ||
    !record.schema_name.trim()
  ) {
    return null;
  }
  return {
    database: record.database_name.trim(),
    schema: record.schema_name.trim(),
  };
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const expected = request.headers.get(FINGERPRINT_HEADER)?.trim() || "";
  try {
    assertVerifiedStorageFingerprint(expected, { verified: true });
  } catch {
    return failure(400);
  }

  let nextIdentity: VerificationStorageTarget | null = null;
  try {
    const result = await db.execute(sql`
      select current_database() as database_name,
             current_schema() as schema_name
    `);
    nextIdentity = readIdentity(result[0]);
  } catch {
    return failure(503);
  }
  if (!nextIdentity || storageFingerprint(nextIdentity) !== expected) {
    return failure(409);
  }

  let backendResponse: Response;
  try {
    backendResponse = await fetchPythonApi("/api/verification/storage", {
      method: "GET",
      headers: { [FINGERPRINT_HEADER]: expected },
      user: {},
    });
  } catch {
    return failure(503);
  }
  if (!backendResponse.ok) return failure(409);

  const backendBody = await backendResponse.json().catch(() => null);
  try {
    assertVerifiedStorageFingerprint(expected, backendBody);
  } catch {
    return failure(409);
  }
  return NextResponse.json(
    { verified: true },
    { status: 200, headers: NO_STORE_HEADERS },
  );
}

import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

const CLEANUP_CONFIRMATION = "DELETE VERIFIED TEST DATA";
// 500 selectors with the backend's 256-byte identity ceiling fit below this
// bound while chunked/hostile requests remain tightly bounded in memory.
const MAX_BODY_BYTES = 256 * 1024;
const MAX_SELECTORS = 500;
const SELECTOR_TYPES = new Set(["legacy_manifest", "verification_run"]);
const LEGACY_SELECTOR_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

type VerificationSelector = {
  type: "legacy_manifest" | "verification_run";
  id: string;
};

type CleanupPayload = {
  selectors: VerificationSelector[];
  preview_digest: string;
  confirmation: typeof CLEANUP_CONFIRMATION;
};

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

async function requireAdmin() {
  const user = await getSession();
  if (!user) {
    return {
      error: noStoreJson({ detail: "認証が必要です" }, 401),
      user: null,
    };
  }
  if (user.role !== "admin") {
    return {
      error: noStoreJson({ detail: "管理者権限が必要です" }, 403),
      user: null,
    };
  }
  return { error: null, user };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function parseSelector(value: unknown): VerificationSelector | null {
  if (!isRecord(value)) return null;
  const type = value.type;
  const id = value.id;
  if (
    typeof type !== "string" ||
    !SELECTOR_TYPES.has(type) ||
    typeof id !== "string" ||
    id.trim().length === 0 ||
    id.length > 256
  ) {
    return null;
  }
  if (type === "legacy_manifest" && !LEGACY_SELECTOR_RE.test(id.trim())) return null;
  return { type: type as VerificationSelector["type"], id: id.trim() };
}

/**
 * Keep the destructive contract deliberately narrow.  The backend performs
 * the authoritative A-manifest and preview-digest re-read; this route merely
 * prevents arbitrary proxy calls and malformed/ambiguous confirmations from
 * crossing the Next→FastAPI boundary.
 */
function parseCleanupPayload(value: unknown): CleanupPayload | null {
  if (!isRecord(value)) return null;
  if (
    typeof value.preview_digest !== "string" ||
    value.preview_digest.trim().length === 0 ||
    value.preview_digest.length > 512 ||
    value.confirmation !== CLEANUP_CONFIRMATION ||
    !Array.isArray(value.selectors) ||
    value.selectors.length === 0 ||
    value.selectors.length > MAX_SELECTORS
  ) {
    return null;
  }

  const selectors: VerificationSelector[] = [];
  const seen = new Set<string>();
  for (const valueSelector of value.selectors) {
    const selector = parseSelector(valueSelector);
    if (!selector) return null;
    const key = `${selector.type}:${selector.id}`;
    if (seen.has(key)) return null;
    seen.add(key);
    selectors.push(selector);
  }

  // Reject extension fields in the cleanup body.  The backend may add fields
  // to its response over time, but a destructive request must remain explicit.
  const allowedKeys = new Set(["selectors", "preview_digest", "confirmation"]);
  if (Object.keys(value).some((key) => !allowedKeys.has(key))) return null;

  return {
    selectors,
    preview_digest: value.preview_digest.trim(),
    confirmation: CLEANUP_CONFIRMATION,
  };
}

async function readJsonBody(request: NextRequest): Promise<unknown | null> {
  const declaredLength = Number(request.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_BODY_BYTES) {
    return null;
  }

  // This endpoint only accepts a tiny JSON command.  Bound the body before
  // parsing so an untrusted client cannot make the Next process allocate an
  // unbounded request string.
  const stream = request.body;
  if (!stream) return null;
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > MAX_BODY_BYTES) {
        await reader.cancel();
        return null;
      }
      chunks.push(value);
    }
  } catch {
    return null;
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  const body = new TextDecoder().decode(bytes);
  try {
    return JSON.parse(body);
  } catch {
    return null;
  }
}

export async function GET(request: NextRequest) {
  const guard = await requireAdmin();
  if (guard.error) return guard.error;
  return proxyRequestToPythonApi(request, {
    path: ["admin", "verification-data", "preview"],
    user: guard.user,
  });
}

export async function POST(request: NextRequest) {
  const guard = await requireAdmin();
  if (guard.error) return guard.error;
  const payload = parseCleanupPayload(await readJsonBody(request));
  if (!payload) {
    return noStoreJson(
      {
        detail:
          "リクエスト形式が不正です。プレビューを再取得し、確認文を正確に入力してください。",
      },
      400,
    );
  }

  return proxyRequestToPythonApi(request, {
    path: ["admin", "verification-data", "cleanup"],
    user: guard.user,
    body: JSON.stringify(payload),
  });
}

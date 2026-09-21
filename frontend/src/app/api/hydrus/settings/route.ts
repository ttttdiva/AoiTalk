import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  deleteUserHydrusSettings,
  getLegacyHydrusAvailability,
  getUserHydrusSettings,
  saveUserHydrusSettings,
} from "@/lib/hf/user-store";
import { effectiveHydrusProfile } from "@/lib/server/hydrus-policy";

const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

type HydrusSettingsErrorCode =
  | "authentication_required"
  | "hydrus_endpoint_policy_rejected"
  | "hydrus_not_configured"
  | "hydrus_credential_store_unavailable";

function errorResponse(
  status: number,
  code: HydrusSettingsErrorCode,
  message: string,
) {
  return NextResponse.json(
    { detail: { category: "hydrus", code, message } },
    { status, headers: PRIVATE_HEADERS },
  );
}

const AUTHENTICATION_ERROR = "認証が必要です";
const ENDPOINT_POLICY_ERROR = "Hydrus API URLが安全ポリシーで許可されていません";
const NOT_CONFIGURED_ERROR = "Hydrus接続が設定されていません。Settingsで接続を設定してください";
const STORE_ERROR = "Hydrus接続設定を一時的に読み取れません";

function enterpriseDisabled() {
  return effectiveHydrusProfile() === "enterprise";
}

function enterpriseResponse() {
  return errorResponse(
    404,
    "hydrus_not_configured",
    "EnterpriseプロファイルではHydrusを利用できません",
  );
}

/**
 * Hydrus接続設定。access keyはDBへ暗号化保存し、レスポンスには一切含めない。
 * 通常ユーザー自身のintegrationだけを操作でき、adminでも他ユーザーを指定できない。
 */
export async function GET() {
  if (enterpriseDisabled()) return enterpriseResponse();
  const user = await getSession();
  if (!user) return errorResponse(401, "authentication_required", AUTHENTICATION_ERROR);
  let settings: Awaited<ReturnType<typeof getUserHydrusSettings>>;
  let legacyAvailable = false;
  try {
    settings = await getUserHydrusSettings(String(user.id));
    legacyAvailable = await getLegacyHydrusAvailability(String(user.id));
  } catch {
    return errorResponse(503, "hydrus_credential_store_unavailable", STORE_ERROR);
  }
  return NextResponse.json(
    {
      configured: Boolean(settings),
      apiUrl: settings?.apiUrl ?? null,
      displayName: settings?.displayName ?? null,
      // This is a boolean capability hint only.  The legacy URL/key are never
      // sent to the browser; the explicit migration action performs the
      // authenticated owner claim server-side.
      legacyAvailable,
    },
    { headers: PRIVATE_HEADERS },
  );
}

export async function PUT(request: NextRequest) {
  if (enterpriseDisabled()) return enterpriseResponse();
  const user = await getSession();
  if (!user) return errorResponse(401, "authentication_required", AUTHENTICATION_ERROR);
  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
  const apiUrl = typeof body?.apiUrl === "string" ? body.apiUrl : "";
  const accessKey = typeof body?.accessKey === "string" ? body.accessKey : "";
  const displayName = typeof body?.displayName === "string" ? body.displayName : undefined;
  try {
    await saveUserHydrusSettings(String(user.id), { apiUrl, accessKey, displayName });
    return NextResponse.json({ success: true }, { headers: PRIVATE_HEADERS });
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (message.startsWith("Hydrus API URL")) {
      return errorResponse(422, "hydrus_endpoint_policy_rejected", ENDPOINT_POLICY_ERROR);
    }
    if (message === "Hydrus access keyが必要です") {
      return errorResponse(409, "hydrus_not_configured", NOT_CONFIGURED_ERROR);
    }
    return errorResponse(503, "hydrus_credential_store_unavailable", STORE_ERROR);
  }
}

export async function DELETE() {
  if (enterpriseDisabled()) return enterpriseResponse();
  const user = await getSession();
  if (!user) return errorResponse(401, "authentication_required", AUTHENTICATION_ERROR);
  try {
    await deleteUserHydrusSettings(String(user.id));
    return NextResponse.json({ success: true }, { headers: PRIVATE_HEADERS });
  } catch {
    return errorResponse(503, "hydrus_credential_store_unavailable", STORE_ERROR);
  }
}

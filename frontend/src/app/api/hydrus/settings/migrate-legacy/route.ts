import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  migrateLegacyHydrusSettings,
  type LegacyHydrusMigrationResult,
  type LegacyHydrusMigrationStatus,
} from "@/lib/hf/user-store";

const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };
const CONFIRMATION = "claim-existing-local-hydrus";

export type LegacyHydrusRouteCode =
  | "hydrus_legacy_claim_confirmation_required"
  | "authentication_required"
  | "csrf_rejected"
  | "hydrus_legacy_unavailable"
  | "hydrus_legacy_claim_conflict"
  | "hydrus_endpoint_policy_rejected"
  | "hydrus_credential_store_unavailable";

/** The migration action is intentionally POST-only and requires Origin. */
function isSameOrigin(request: NextRequest): boolean {
  const origin = request.headers.get("origin")?.trim();
  if (!origin) return false;
  try {
    const forwardedHost = request.headers
      .get("x-forwarded-host")
      ?.split(",")[0]
      ?.trim();
    const host = forwardedHost || request.headers.get("host")?.trim();
    const forwardedProto = request.headers
      .get("x-forwarded-proto")
      ?.split(",")[0]
      ?.trim();
    const expected =
      host && forwardedProto
        ? `${forwardedProto}://${host}`
        : host
          ? `${request.nextUrl.protocol}//${host}`
          : request.nextUrl.origin;
    return new URL(origin).origin === new URL(expected).origin;
  } catch {
    return false;
  }
}

function safeStatusDetail(status: LegacyHydrusMigrationStatus): string {
  switch (status) {
    case "migrated":
      return "既存のHydrus設定をこのユーザーへ移行しました";
    case "already_migrated":
      return "既存のHydrus設定はすでにこのユーザーへ移行済みです";
    case "legacy_unavailable":
      return "移行できる既存のHydrus設定がありません";
    case "endpoint_rejected":
      return "既存のHydrus接続先が管理ポリシーで許可されていません";
    case "credential_conflict":
      return "このユーザーには既存のHydrus設定があるため移行できません";
    case "enterprise_disabled":
      return "EnterpriseプロファイルではHydrusを利用できません";
    case "profile_not_personal":
      return "Personalプロファイルでのみ既存設定を移行できます";
    case "native_local_required":
      return "Windowsのnative local実行でのみ既存のローカルHydrus設定を移行できます";
    case "invalid_user":
      return "認証ユーザーが不正です";
    case "internal_error":
    default:
      return "Hydrus設定を移行できませんでした";
  }
}

const STATUS_CODES: Record<LegacyHydrusMigrationStatus, number> = {
  migrated: 200,
  already_migrated: 200,
  legacy_unavailable: 409,
  endpoint_rejected: 422,
  credential_conflict: 409,
  enterprise_disabled: 404,
  profile_not_personal: 403,
  native_local_required: 403,
  invalid_user: 401,
  internal_error: 503,
};

function codeForStatus(status: LegacyHydrusMigrationStatus): LegacyHydrusRouteCode {
  switch (status) {
    case "legacy_unavailable":
      return "hydrus_legacy_unavailable";
    case "credential_conflict":
      return "hydrus_legacy_claim_conflict";
    case "endpoint_rejected":
      return "hydrus_endpoint_policy_rejected";
    case "enterprise_disabled":
    case "profile_not_personal":
    case "native_local_required":
      return "hydrus_legacy_unavailable";
    case "invalid_user":
      return "authentication_required";
    case "internal_error":
    default:
      return "hydrus_credential_store_unavailable";
  }
}

function errorResponse(
  statusCode: number,
  code: LegacyHydrusRouteCode,
  message: string,
  status: string,
): NextResponse {
  return NextResponse.json(
    {
      // Keep the legacy status field for existing callers.  The nested detail
      // is the canonical typed, secret-free error contract consumed by
      // HydrusApiError.
      code,
      status,
      migrated: false,
      detail: { category: "hydrus", code, message },
    },
    { status: statusCode, headers: PRIVATE_HEADERS },
  );
}

function responseForResult(result: LegacyHydrusMigrationResult): NextResponse {
  const success = result.status === "migrated" || result.status === "already_migrated";
  if (!success) {
    return errorResponse(
      STATUS_CODES[result.status],
      codeForStatus(result.status),
      safeStatusDetail(result.status),
      result.status,
    );
  }
  return NextResponse.json(
    {
      success: true,
      configured: true,
      status: result.status,
      migrated: result.status === "migrated",
      alreadyMigrated: result.status === "already_migrated",
      apiUrl: result.apiUrl ?? null,
    },
    { status: STATUS_CODES[result.status], headers: PRIVATE_HEADERS },
  );
}

export async function POST(request: NextRequest) {
  if (!isSameOrigin(request)) {
    return errorResponse(
      403,
      "csrf_rejected",
      "同一オリジンからの操作が必要です",
      "csrf_failed",
    );
  }

  const user = await getSession();
  if (!user) {
    return errorResponse(
      401,
      "authentication_required",
      "認証が必要です",
      "unauthenticated",
    );
  }

  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
  if (body?.confirm !== CONFIRMATION) {
    return errorResponse(
      400,
      "hydrus_legacy_claim_confirmation_required",
      "移行確認が必要です",
      "confirmation_required",
    );
  }

  const result = await migrateLegacyHydrusSettings(String(user.id));
  return responseForResult(result);
}

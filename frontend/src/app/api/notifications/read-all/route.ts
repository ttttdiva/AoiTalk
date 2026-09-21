import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";
import { cancelProjectStewardNotifications } from "@/lib/server/project-steward-notification-access";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  // The Python service also suppresses Steward rows, but cancel all legacy
  // alerts in the BFF first so a mixed-version backend cannot mark them read
  // or report them in the unread count.  This helper only targets Steward
  // rows; ordinary task notifications continue through the existing proxy.
  await cancelProjectStewardNotifications(user.id);

  return proxyRequestToPythonApi(request, {
    path: ["notifications", "read-all"],
    user,
  });
}

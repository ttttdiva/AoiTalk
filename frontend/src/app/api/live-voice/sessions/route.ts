import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";
import {
  MAX_LIVE_VOICE_SESSION_BODY_BYTES,
  proxyBoundedLiveVoiceRequest,
} from "@/lib/server/live-voice-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

async function proxy(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  if (request.method === "POST") {
    return proxyBoundedLiveVoiceRequest(request, {
      path: ["live-voice", "sessions"],
      user,
      maxBodyBytes: MAX_LIVE_VOICE_SESSION_BODY_BYTES,
    });
  }
  return proxyRequestToPythonApi(request, {
    path: ["live-voice", "sessions"],
    user,
  });
}

export async function GET(request: NextRequest) {
  return proxy(request);
}

export async function POST(request: NextRequest) {
  return proxy(request);
}

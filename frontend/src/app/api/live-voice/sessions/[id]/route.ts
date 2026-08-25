import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";
import {
  MAX_LIVE_VOICE_END_BODY_BYTES,
  proxyBoundedLiveVoiceRequest,
} from "@/lib/server/live-voice-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

async function proxy(request: NextRequest, id: string) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  if (request.method === "DELETE") {
    return proxyBoundedLiveVoiceRequest(request, {
      path: ["live-voice", "sessions", id],
      user,
      maxBodyBytes: MAX_LIVE_VOICE_END_BODY_BYTES,
    });
  }
  return proxyRequestToPythonApi(request, {
    path: ["live-voice", "sessions", id],
    user,
  });
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  return proxy(request, (await params).id);
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  return proxy(request, (await params).id);
}

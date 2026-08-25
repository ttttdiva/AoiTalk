import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  MAX_LIVE_VOICE_EVENT_BODY_BYTES,
  proxyBoundedLiveVoiceRequest,
} from "@/lib/server/live-voice-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  const { id } = await params;
  return proxyBoundedLiveVoiceRequest(request, {
    path: ["live-voice", "sessions", id, "events"],
    user,
    maxBodyBytes: MAX_LIVE_VOICE_EVENT_BODY_BYTES,
  });
}

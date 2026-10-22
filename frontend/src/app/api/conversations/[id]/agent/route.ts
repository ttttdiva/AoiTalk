import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { fetchPythonApi } from "@/lib/server/python-api-proxy";

type RouteContext = { params: Promise<{ id: string }> };

/**
 * Agent binding is owned by FastAPI's session identity service.  Keep this
 * BFF route deliberately thin: it authenticates the browser session, forwards
 * only the two CAS fields, and never writes a client supplied Agent snapshot
 * into the Next/Drizzle projection.
 */
export async function PATCH(request: NextRequest, { params }: RouteContext) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const body = await request.json().catch(() => null) as {
    agent_id?: unknown;
    expected_agent_id?: unknown;
  } | null;
  const hasAgentField = Boolean(
    body && Object.prototype.hasOwnProperty.call(body, "agent_id"),
  );
  if (!hasAgentField) {
    return NextResponse.json(
      { detail: "agent_idはnullまたはAgent IDで明示してください" },
      { status: 400 },
    );
  }
  const agentId =
    body?.agent_id === null
      ? null
      : typeof body?.agent_id === "string" && body.agent_id.trim()
        ? body.agent_id.trim()
        : undefined;
  const expectedAgentId =
    body?.expected_agent_id === null
      ? null
      : typeof body?.expected_agent_id === "string" && body.expected_agent_id.trim()
        ? body.expected_agent_id.trim()
        : undefined;

  if (agentId === undefined && body?.agent_id !== undefined) {
    return NextResponse.json({ detail: "agent_idの形式が不正です" }, { status: 400 });
  }
  if (expectedAgentId === undefined && body?.expected_agent_id !== undefined) {
    return NextResponse.json(
      { detail: "expected_agent_idの形式が不正です" },
      { status: 400 },
    );
  }
  if (agentId === undefined && expectedAgentId === undefined) {
    return NextResponse.json(
      { detail: "agent_idまたはexpected_agent_idが必要です" },
      { status: 400 },
    );
  }

  let response: Response;
  try {
    response = await fetchPythonApi(
      `/api/conversations/${encodeURIComponent(id)}/agent`,
      {
        method: "PATCH",
        user,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          ...(agentId !== undefined ? { agent_id: agentId } : {}),
          ...(expectedAgentId !== undefined
            ? { expected_agent_id: expectedAgentId }
            : {}),
        }),
      },
    );
  } catch (error) {
    return NextResponse.json(
      {
        detail:
          error instanceof Error
            ? error.message
            : "会話サービスに接続できません",
      },
      { status: 502 },
    );
  }

  const responseBody = await response.text();
  return new NextResponse(responseBody, {
    status: response.status,
    headers: {
      "content-type": response.headers.get("content-type") ?? "application/json",
      "cache-control": "no-store",
    },
  });
}

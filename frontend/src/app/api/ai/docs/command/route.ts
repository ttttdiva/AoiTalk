import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { knowledgeAiSuggestions } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  cleanString,
  ensureDocsWorkspace,
  requireDocsNode,
  serializeSuggestion,
} from "@/lib/server/knowledge-docs-utils";
import { fetchPythonApi } from "@/lib/server/python-api-proxy";

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const workspace = await ensureDocsWorkspace(user);
  const nodeId = cleanOptionalString(body.node_id, 80);
  const command = cleanString(body.command, "continue", 80);
  const prompt = cleanString(body.prompt, "", 8000);

  if (nodeId) {
    const nodeAccess = await requireDocsNode(nodeId, user, "read");
    if (!nodeAccess || nodeAccess.workspace.id !== workspace.id) {
      return NextResponse.json({ detail: "Docs node not found" }, { status: 404 });
    }
  }

  const upstream = await fetchPythonApi("/api/ai/docs/command", {
    method: "POST",
    user,
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      node_id: nodeId,
      command,
      prompt,
    }),
  });
  const upstreamBody = await upstream.json().catch(() => ({}));
  if (!upstream.ok) {
    return NextResponse.json(
      {
        detail: upstreamBody.detail ?? "Docs AI command failed",
        error: upstreamBody.error,
      },
      { status: upstream.status },
    );
  }

  const result =
    upstreamBody && typeof upstreamBody === "object" && "result" in upstreamBody
      ? (upstreamBody.result as Record<string, unknown>)
      : {};
  const payload: Record<string, unknown> = {
    command,
    prompt,
    node_id: nodeId,
    ...result,
  };

  const [row] = await db
    .insert(knowledgeAiSuggestions)
    .values({
      workspaceId: workspace.id,
      nodeId,
      suggestionType: `docs_command:${command}`,
      payloadJson: payload,
      status: "proposed",
      confidence: typeof upstreamBody.confidence === "number" ? upstreamBody.confidence : 0.72,
      createdBy: user.id,
    })
    .returning();

  return NextResponse.json({
    suggestion: serializeSuggestion(row),
    result: payload,
  });
}

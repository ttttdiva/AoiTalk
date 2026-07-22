import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeAiSuggestions, knowledgeNodes } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  cleanString,
  decryptNodeBodyText,
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

  if (command === "generate_minutes" && nodeId) {
    const nodes = await db
      .select()
      .from(knowledgeNodes)
      .where(and(eq(knowledgeNodes.workspaceId, workspace.id), isNull(knowledgeNodes.archivedAt)))
      .orderBy(asc(knowledgeNodes.sortOrder));
    const children = new Map<string | null, typeof nodes>();
    for (const node of nodes) {
      const list = children.get(node.parentId) ?? [];
      list.push(node);
      children.set(node.parentId, list);
    }
    const source = nodes.find((node) => node.id === nodeId);
    const collected: string[] = [];
    const walk = (id: string, depth = 0) => {
      for (const child of children.get(id) ?? []) {
        if ((child.title || "").endsWith("議事録")) continue;
        const text = child.title || decryptNodeBodyText(child.bodyText ?? "");
        if (text.trim()) collected.push(`${"  ".repeat(depth)}${text.trim()}`);
        walk(child.id, depth + 1);
      }
    };
    walk(nodeId);
    const decisions = collected.filter((line) => /決定|決まり|承認|方針/.test(line)).slice(0, 8);
    const tasksToCreate = collected.filter((line) => /宿題|TODO|対応|確認|追記|作業|依頼/.test(line)).slice(0, 8);
    const lines = [
      `元メモ [[node:${nodeId}|${source?.title || "議事メモ"}]]`,
      "質疑応答",
      ...(collected.length ? collected.slice(0, 4).map((line) => `Q/A: ${line}`) : ["Q/A: メモ内容を確認し、質疑を追記する。"]),
      "決定事項",
      ...(decisions.length ? decisions.map((line) => `決定: ${line} #決定`) : ["決定: 今回のメモから決定事項を確認する。 #決定"]),
      "宿題",
      ...(tasksToCreate.length ? tasksToCreate.map((line) => `宿題: ${line} #タスク`) : ["宿題: 次回までの確認事項を整理する。 #タスク"]),
    ];
    const payload: Record<string, unknown> = {
      command,
      prompt,
      node_id: nodeId,
      mode: "insert_children",
      lines,
      summary: "議事録候補を生成しました",
    };
    const [row] = await db
      .insert(knowledgeAiSuggestions)
      .values({
        workspaceId: workspace.id,
        nodeId,
        suggestionType: "docs_command:generate_minutes",
        payloadJson: payload,
        status: "proposed",
        confidence: 0.7,
        createdBy: user.id,
      })
      .returning();
    return NextResponse.json({ suggestion: serializeSuggestion(row), result: payload });
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

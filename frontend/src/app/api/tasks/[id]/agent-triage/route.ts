import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { taskComments, tasks } from "@/db/schema";
import { eq } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded, encryptText } from "@/lib/server/field-crypto";
import {
  canWriteMembership,
  getProjectMembership,
} from "@/lib/server/task-route-utils";

const TRIAGE_MARKER = "[aoitalk-agent-triage]";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : {};
}

function triageTask(task: {
  id: string;
  title: string;
  description: string | null;
  status: string | null;
}) {
  const text = `${task.title}\n${task.description ?? ""}`.trim();
  const questions: string[] = [];
  const investigation: string[] = [];
  const execution: string[] = [];

  if (text.length < 24) {
    questions.push("Please clarify the goal, target surface, and expected done state.");
  }
  if (/irodori|tts|voice|watermark|gpu|checkpoint/i.test(text)) {
    investigation.push("Check external dependencies, GPU/VRAM, checkpoints, and audible watermark risk.");
  }
  if (/mobile|expo|android|ios/i.test(text)) {
    execution.push("Review the mobile screen, API client, and offline/pending behavior.");
  }
  if (/webui|frontend|next|browser|ui/i.test(text)) {
    execution.push("Update the web UI component and add focused regression checks.");
  }
  if (/backend|api|db|database|migration/i.test(text)) {
    execution.push("Align backend API, DB model/migration, and permission checks.");
  }
  if (/upload|file|attachment/i.test(text)) {
    execution.push("Implement storage path, blocked-extension handling, UI refresh, and failure handling.");
  }
  if (/bug|fix|overflow|position|failure|error/i.test(text)) {
    execution.push("Lock down reproduction and make the smallest compatible fix.");
  }

  if (execution.length === 0 && investigation.length === 0) {
    execution.push("Inspect the target code and turn the request into concrete implementation units.");
  }

  const status = questions.length > 0 ? "needs_user" : "ready";
  const summary = [
    `Goal: ${task.title}`,
    investigation.length ? `Investigation: ${investigation.join(" / ")}` : null,
    `Execution: ${execution.join(" / ")}`,
  ]
    .filter(Boolean)
    .join("\n");

  return { status, summary, questions, investigation, execution };
}

export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id } = await params;
  const [task] = await db
    .select()
    .from(tasks)
    .where(eq(tasks.id, id))
    .limit(1);
  if (!task) {
    return NextResponse.json({ detail: "Task not found" }, { status: 404 });
  }
  const membership = await getProjectMembership(user.id, task.projectId);
  if (!canWriteMembership(user, membership)) {
    return NextResponse.json({ detail: "Permission denied" }, { status: 403 });
  }

  const triage = triageTask({
    id: task.id,
    title: task.title,
    description: task.description,
    status: task.status,
  });
  const runId = crypto.randomUUID();
  const nextMetadata = {
    ...asRecord(task.taskMetadata),
    agent_triage_status: triage.status,
    agent_triage_summary: triage.summary,
    agent_triage_questions: triage.questions,
    agent_triage_checked_at: new Date().toISOString(),
    agent_triage_run_id: runId,
    agent_triage_error: null,
  };

  const [updated] = await db
    .update(tasks)
    .set({ taskMetadata: nextMetadata, updatedAt: new Date() })
    .where(eq(tasks.id, id))
    .returning();

  const commentContent = `${TRIAGE_MARKER}\n${triage.summary}${
    triage.questions.length
      ? `\n\nQuestions:\n${triage.questions.map((q) => `- ${q}`).join("\n")}`
      : ""
  }`;
  const taskCommentRows = await db
    .select()
    .from(taskComments)
    .where(eq(taskComments.taskId, id));
  const existingComment = taskCommentRows.find((comment) =>
    (decryptTextIfNeeded(comment.content, "task_comments.content") || "").startsWith(
      TRIAGE_MARKER,
    ),
  );
  if (existingComment) {
    await db
      .update(taskComments)
      .set({
        content: encryptText(commentContent, "task_comments.content"),
        updatedAt: new Date(),
      })
      .where(eq(taskComments.id, existingComment.id));
  } else {
    await db.insert(taskComments).values({
      taskId: id,
      userId: user.id,
      content: encryptText(commentContent, "task_comments.content"),
    });
  }

  return NextResponse.json({
    task_id: id,
    status: triage.status,
    summary: triage.summary,
    questions: triage.questions,
    metadata: nextMetadata,
    task: updated,
  });
}

import { createHash } from "node:crypto";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeCaptureCandidates,
  projectKnowledgeCaptureSettings,
  taskActivities,
} from "@/db/schema";

type KnowledgeCaptureTransaction = Parameters<
  Parameters<typeof db.transaction>[0]
>[0];

type ActivityRecord = {
  id: string;
  createdAt: Date;
};

type CompletionInput = {
  taskId: string;
  projectId: string;
  status: string;
  completedAt?: Date | string | null;
  activity: ActivityRecord;
  triggerUserId?: string | null;
};

export function canonicalCompletionFingerprint(input: {
  projectId: string;
  taskId: string;
  status: string;
  activityId: string;
}): string {
  const payload = Object.fromEntries(
    Object.entries({
      project_id: input.projectId,
      task_id: input.taskId,
      status: input.status,
      completion_marker: input.activityId,
    }).sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0)),
  );
  return createHash("sha256")
    .update(JSON.stringify(payload), "utf8")
    .digest("hex");
}

export async function recordTaskActivity(
  tx: KnowledgeCaptureTransaction,
  input: {
    taskId: string;
    userId?: string | null;
    activityType: string;
    payload?: Record<string, unknown>;
    createdAt?: Date;
  },
): Promise<ActivityRecord> {
  const activity = {
    id: crypto.randomUUID(),
    taskId: input.taskId,
    userId: input.userId ?? null,
    activityType: input.activityType,
    payload: input.payload ?? {},
    createdAt: input.createdAt ?? new Date(),
  };
  await tx.insert(taskActivities).values(activity);
  return { id: activity.id, createdAt: activity.createdAt };
}

export async function enqueueKnowledgeCaptureForCompletion(
  tx: KnowledgeCaptureTransaction,
  input: CompletionInput,
): Promise<string | null> {
  if (String(input.status).trim().toLowerCase() !== "closed") return null;

  const [setting] = await tx
    .select({ mode: projectKnowledgeCaptureSettings.mode, updatedAt: projectKnowledgeCaptureSettings.updatedAt })
    .from(projectKnowledgeCaptureSettings)
    .where(eq(projectKnowledgeCaptureSettings.projectId, input.projectId))
    .limit(1);
  const mode = setting?.mode ?? "suggest";
  if (mode === "off") return null;
  if (setting && setting.updatedAt > input.activity.createdAt) return null;

  const fingerprint = canonicalCompletionFingerprint({
    projectId: input.projectId,
    taskId: input.taskId,
    status: "closed",
    activityId: input.activity.id,
  });
  const completedAt =
    input.completedAt == null
      ? null
      : input.completedAt instanceof Date
        ? input.completedAt
        : new Date(input.completedAt);

  await tx
    .insert(knowledgeCaptureCandidates)
    .values({
      id: crypto.randomUUID(),
      projectId: input.projectId,
      seedTaskId: input.taskId,
      taskActivityId: input.activity.id,
      triggerUserId: input.triggerUserId ?? null,
      completionFingerprint: fingerprint,
      terminalStatus: "closed",
      status: "queued",
      modeSnapshot: mode,
      version: 1,
      evidenceRefs: [
        { type: "task", id: input.taskId },
        { type: "task_activity", id: input.activity.id },
      ],
      attemptCount: 0,
      maxAttempts: 5,
      completedAt,
    })
    .onConflictDoNothing({
      target: knowledgeCaptureCandidates.completionFingerprint,
    });
  return fingerprint;
}

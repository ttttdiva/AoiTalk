import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { db } from "@/db";
import { taskAttachments, tasks } from "@/db/schema";
import { and, eq } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { ensureProjectStorageRoot } from "@/lib/server/project-workspace-management";
import {
  canWriteMembership,
  getProjectMembership,
} from "@/lib/server/task-route-utils";

async function loadAttachment(taskId: string, attachmentId: string) {
  const [row] = await db
    .select({
      id: taskAttachments.id,
      taskId: taskAttachments.taskId,
      projectId: taskAttachments.projectId,
      filePath: taskAttachments.filePath,
      displayName: taskAttachments.displayName,
      mimeType: taskAttachments.mimeType,
      taskProjectId: tasks.projectId,
    })
    .from(taskAttachments)
    .innerJoin(tasks, eq(taskAttachments.taskId, tasks.id))
    .where(
      and(
        eq(taskAttachments.id, attachmentId),
        eq(taskAttachments.taskId, taskId),
      ),
    )
    .limit(1);
  return row ?? null;
}

function resolveAttachmentPath(storageRoot: string, filePath: string): string | null {
  const target = path.resolve(
    /*turbopackIgnore: true*/
    storageRoot,
    filePath.replace(/\//g, path.sep),
  );
  const relative = path.relative(storageRoot, target);
  if (relative.startsWith("..") || path.isAbsolute(relative)) return null;
  return target;
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string; attachmentId: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id, attachmentId } = await params;
  const attachment = await loadAttachment(id, attachmentId);
  if (!attachment) {
    return NextResponse.json({ detail: "Attachment not found" }, { status: 404 });
  }
  const membership = await getProjectMembership(user.id, attachment.taskProjectId);
  if (!membership && user.role !== "admin") {
    return NextResponse.json({ detail: "Permission denied" }, { status: 403 });
  }

  const storageRoot = ensureProjectStorageRoot(attachment.taskProjectId);
  const targetPath = resolveAttachmentPath(storageRoot, attachment.filePath);
  if (!targetPath || !fs.existsSync(targetPath) || !fs.statSync(targetPath).isFile()) {
    return NextResponse.json({ detail: "File not found" }, { status: 404 });
  }

  const data = fs.readFileSync(targetPath);
  const encodedName = encodeURIComponent(attachment.displayName);
  return new NextResponse(new Uint8Array(data), {
    headers: {
      "Content-Type": attachment.mimeType || "application/octet-stream",
      "Content-Disposition": `inline; filename="${attachment.displayName.replace(/"/g, "")}"; filename*=UTF-8''${encodedName}`,
      "Content-Length": String(data.byteLength),
    },
  });
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string; attachmentId: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id, attachmentId } = await params;
  const attachment = await loadAttachment(id, attachmentId);
  if (!attachment) {
    return NextResponse.json({ detail: "Attachment not found" }, { status: 404 });
  }
  const membership = await getProjectMembership(user.id, attachment.taskProjectId);
  if (!canWriteMembership(user, membership)) {
    return NextResponse.json({ detail: "Permission denied" }, { status: 403 });
  }

  await db
    .delete(taskAttachments)
    .where(
      and(
        eq(taskAttachments.id, attachmentId),
        eq(taskAttachments.taskId, id),
      ),
    );

  return new NextResponse(null, { status: 204 });
}

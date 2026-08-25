import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { db } from "@/db";
import { projectMembers, projects, taskAttachments, tasks, users } from "@/db/schema";
import { and, eq, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  calculateProjectStorageUsage,
  ensureProjectStorageRoot,
  isPathInsideProjectStorageRoot,
} from "@/lib/server/project-workspace-management";
import { hasProjectPermission } from "@/lib/server/project-permissions";
import {
  canReadProjectId,
  canWriteProjectId,
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
        isNull(tasks.deletedAt),
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
  if (!(await canReadProjectId(user, attachment.taskProjectId))) {
    return NextResponse.json({ detail: "Permission denied" }, { status: 403 });
  }

  const storageRoot = ensureProjectStorageRoot(attachment.taskProjectId);
  const targetPath = resolveAttachmentPath(storageRoot, attachment.filePath);
  let targetStat: fs.Stats | null = null;
  try {
    targetStat = targetPath ? fs.lstatSync(targetPath) : null;
  } catch {
    targetStat = null;
  }
  if (
    !targetPath ||
    !isPathInsideProjectStorageRoot(storageRoot, targetPath) ||
    !targetStat ||
    targetStat.isSymbolicLink() ||
    !targetStat.isFile()
  ) {
    return NextResponse.json({ detail: "File not found" }, { status: 404 });
  }

  const data = fs.readFileSync(targetPath);
  const encodedName = encodeURIComponent(attachment.displayName);
  const storedMimeType = (attachment.mimeType || "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
  const inlineMimeTypes = new Set([
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
  ]);
  const canRenderInline = inlineMimeTypes.has(storedMimeType);
  return new NextResponse(new Uint8Array(data), {
    headers: {
      "Content-Type": canRenderInline
        ? storedMimeType
        : "application/octet-stream",
      "Content-Disposition": `${canRenderInline ? "inline" : "attachment"}; filename="${attachment.displayName.replace(/"/g, "")}"; filename*=UTF-8''${encodedName}`,
      "Content-Length": String(data.byteLength),
      "X-Content-Type-Options": "nosniff",
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
  if (!(await canWriteProjectId(user, attachment.taskProjectId))) {
    return NextResponse.json({ detail: "Permission denied" }, { status: 403 });
  }

  const storageRoot = ensureProjectStorageRoot(attachment.taskProjectId);
  const targetPath = resolveAttachmentPath(storageRoot, attachment.filePath);

  try {
    await db.transaction(async (tx) => {
      const [project] = await tx
        .select()
        .from(projects)
        .where(eq(projects.id, attachment.taskProjectId))
        .limit(1)
        .for("update");
      if (!project || project.deletedAt) throw new Error("Project not found");

      const [principal] = await tx
        .select({ role: users.role })
        .from(users)
        .where(eq(users.id, user.id))
        .limit(1);
      const [membership] = await tx
        .select({ permissions: projectMembers.permissions })
        .from(projectMembers)
        .where(
          and(
            eq(projectMembers.projectId, attachment.taskProjectId),
            eq(projectMembers.userId, user.id),
          ),
        )
        .limit(1);
      if (
        principal?.role !== "admin" &&
        project.ownerId !== user.id &&
        !hasProjectPermission(membership?.permissions, "write")
      ) {
        throw new ProjectPermissionError();
      }

      if (targetPath && isPathInsideProjectStorageRoot(storageRoot, targetPath)) {
        try {
          const targetStat = fs.lstatSync(targetPath);
          if (targetStat.isSymbolicLink() || !targetStat.isFile()) {
            throw new Error("Invalid attachment path");
          }
          fs.unlinkSync(targetPath);
        } catch (error) {
          if (error instanceof Error && error.message === "Invalid attachment path") {
            throw error;
          }
          if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        }
      }

      await tx
        .delete(taskAttachments)
        .where(
          and(
            eq(taskAttachments.id, attachmentId),
            eq(taskAttachments.taskId, id),
          ),
        );
      const usage = calculateProjectStorageUsage(storageRoot);
      await tx
        .update(projects)
        .set({ storageUsedMb: usage.totalMb, updatedAt: new Date() })
        .where(eq(projects.id, attachment.taskProjectId));
    });
  } catch (error) {
    if (error instanceof ProjectPermissionError) {
      return NextResponse.json({ detail: error.message }, { status: 403 });
    }
    if (error instanceof Error && error.message === "Project not found") {
      return NextResponse.json({ detail: error.message }, { status: 404 });
    }
    throw error;
  }

  return new NextResponse(null, { status: 204 });
}

class ProjectPermissionError extends Error {
  constructor() {
    super("Permission denied");
    this.name = "ProjectPermissionError";
  }
}

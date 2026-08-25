import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { db } from "@/db";
import { projectMembers, projects, taskAttachments, tasks, users } from "@/db/schema";
import { and, desc, eq, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  ensureProjectStorageRoot,
  isPathInsideProjectStorageRoot,
  calculateProjectStorageUsage,
} from "@/lib/server/project-workspace-management";
import { hasProjectPermission } from "@/lib/server/project-permissions";
import {
  canReadProjectId,
  canWriteProjectId,
} from "@/lib/server/task-route-utils";
import {
  exceedsUploadSizeLimit,
  sanitizeUploadFileName,
  writeUniqueUploadFile,
} from "@/lib/server/attachment-upload";

function projectRelativePath(storageRoot: string, targetPath: string): string {
  return path.relative(storageRoot, targetPath).replace(/\\/g, "/");
}

function attachmentKind(mimeType: string | null, fileName: string): "image" | "file" {
  if (mimeType?.startsWith("image/")) return "image";
  const ext = path.extname(fileName).toLowerCase();
  return [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"].includes(ext)
    ? "image"
    : "file";
}

class ProjectQuotaExceededError extends Error {
  constructor() {
    super("Project storage quota exceeded");
    this.name = "ProjectQuotaExceededError";
  }
}

class ProjectPermissionError extends Error {
  constructor() {
    super("Permission denied");
    this.name = "ProjectPermissionError";
  }
}

function serializeAttachment(
  row: typeof taskAttachments.$inferSelect,
  taskId: string,
) {
  return {
    id: row.id,
    task_id: row.taskId,
    project_id: row.projectId,
    file_path: row.filePath,
    display_name: row.displayName,
    mime_type: row.mimeType,
    size_bytes: row.sizeBytes,
    kind: row.kind,
    created_by: row.createdBy,
    created_at: row.createdAt,
    metadata: row.attachmentMetadata ?? {},
    url: `/api/tasks/${taskId}/attachments/${row.id}`,
  };
}

async function loadTask(id: string) {
  const [task] = await db
    .select({ id: tasks.id, projectId: tasks.projectId })
    .from(tasks)
    .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
    .limit(1);
  return task ?? null;
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id } = await params;
  const task = await loadTask(id);
  if (!task) {
    return NextResponse.json({ detail: "Task not found" }, { status: 404 });
  }
  if (!(await canReadProjectId(user, task.projectId))) {
    return NextResponse.json({ detail: "Permission denied" }, { status: 403 });
  }

  const rows = await db
    .select()
    .from(taskAttachments)
    .where(eq(taskAttachments.taskId, id))
    .orderBy(desc(taskAttachments.createdAt));

  return NextResponse.json(rows.map((row) => serializeAttachment(row, id)));
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id } = await params;
  const task = await loadTask(id);
  if (!task) {
    return NextResponse.json({ detail: "Task not found" }, { status: 404 });
  }
  if (!(await canWriteProjectId(user, task.projectId))) {
    return NextResponse.json({ detail: "Permission denied" }, { status: 403 });
  }

  const form = await request.formData();
  const file = form.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ detail: "file is required" }, { status: 400 });
  }
  if (exceedsUploadSizeLimit(file.size)) {
    return NextResponse.json(
      { detail: "ファイルサイズは 50 MB までです" },
      { status: 413 },
    );
  }

  const fileName = sanitizeUploadFileName(file.name || "uploaded-file");

  const storageRoot = ensureProjectStorageRoot(task.projectId);
  const targetDir = path.resolve(
    /*turbopackIgnore: true*/
    storageRoot,
    "attachments",
    "tasks",
    id,
  );
  const relativeToRoot = path.relative(storageRoot, targetDir);
  if (relativeToRoot.startsWith("..") || path.isAbsolute(relativeToRoot)) {
    return NextResponse.json({ detail: "Invalid attachment path" }, { status: 400 });
  }
  if (!isPathInsideProjectStorageRoot(storageRoot, targetDir)) {
    return NextResponse.json({ detail: "Invalid attachment path" }, { status: 400 });
  }

  const buffer = Buffer.from(await file.arrayBuffer());
  let createdPath: string | null = null;
  let transactionCommitted = false;
  try {
    const attachment = await db.transaction(async (tx) => {
      const [project] = await tx
        .select()
        .from(projects)
        .where(eq(projects.id, task.projectId))
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
            eq(projectMembers.projectId, task.projectId),
            eq(projectMembers.userId, user.id),
          ),
        )
        .limit(1);
      const canWrite =
        principal?.role === "admin" ||
        project.ownerId === user.id ||
        hasProjectPermission(membership?.permissions, "write");
      if (!canWrite) throw new ProjectPermissionError();

      const lockedStorageRoot = ensureProjectStorageRoot(task.projectId);
      const lockedTargetDir = path.resolve(
        /*turbopackIgnore: true*/
        lockedStorageRoot,
        "attachments",
        "tasks",
        id,
      );
      if (!isPathInsideProjectStorageRoot(lockedStorageRoot, lockedTargetDir)) {
        throw new Error("Invalid attachment path");
      }

      const usage = calculateProjectStorageUsage(lockedStorageRoot);
      const quotaMb =
        project.storageQuotaMb === null
          ? 1000
          : Math.max(0, Number(project.storageQuotaMb));
      if (usage.totalBytes + buffer.byteLength > quotaMb * 1024 * 1024) {
        throw new ProjectQuotaExceededError();
      }

      fs.mkdirSync(lockedTargetDir, { recursive: true });
      if (!isPathInsideProjectStorageRoot(lockedStorageRoot, lockedTargetDir)) {
        throw new Error("Invalid attachment path");
      }
      createdPath = writeUniqueUploadFile(lockedTargetDir, fileName, buffer);
      if (!isPathInsideProjectStorageRoot(lockedStorageRoot, createdPath)) {
        throw new Error("Invalid attachment path");
      }

      const relativePath = projectRelativePath(lockedStorageRoot, createdPath);
      const mimeType = file.type || null;
      const [row] = await tx
        .insert(taskAttachments)
        .values({
          taskId: id,
          projectId: task.projectId,
          filePath: relativePath,
          displayName: path.basename(createdPath),
          mimeType,
          sizeBytes: buffer.byteLength,
          kind: attachmentKind(mimeType, fileName),
          createdBy: user.id,
          attachmentMetadata: {},
        })
        .returning();
      await tx
        .update(projects)
        .set({
          storageUsedMb: (usage.totalBytes + buffer.byteLength) / (1024 * 1024),
          updatedAt: new Date(),
        })
        .where(eq(projects.id, task.projectId));
      return row;
    });
    transactionCommitted = true;
    return NextResponse.json(serializeAttachment(attachment, id), { status: 201 });
  } catch (error) {
    if (!transactionCommitted && createdPath) {
      try {
        if (isPathInsideProjectStorageRoot(storageRoot, createdPath)) {
          const item = fs.lstatSync(createdPath);
          if (!item.isSymbolicLink() && item.isFile()) {
            fs.rmSync(createdPath, { force: true });
          }
        }
      } catch {
        console.warn("Failed to remove orphaned task attachment", createdPath);
      }
    }
    if (error instanceof ProjectQuotaExceededError) {
      return NextResponse.json({ detail: error.message }, { status: 413 });
    }
    if (error instanceof ProjectPermissionError) {
      return NextResponse.json({ detail: error.message }, { status: 403 });
    }
    if (error instanceof Error && error.message === "Project not found") {
      return NextResponse.json({ detail: error.message }, { status: 404 });
    }
    throw error;
  }
}

import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { db } from "@/db";
import { taskAttachments, tasks } from "@/db/schema";
import { desc, eq } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { ensureProjectStorageRoot } from "@/lib/server/project-workspace-management";
import {
  canWriteMembership,
  getProjectMembership,
} from "@/lib/server/task-route-utils";

const BLOCKED_EXTENSIONS = new Set([
  ".exe",
  ".bat",
  ".cmd",
  ".sh",
  ".ps1",
  ".vbs",
  ".scr",
  ".com",
]);

function sanitizeFileName(name: string): string {
  const cleaned = name
    .replace(/[/\\:*?"<>|]/g, "")
    .replace(/[\u0000-\u001f]/g, "")
    .trim()
    .replace(/^\.+$/, "");
  return cleaned.slice(0, 180) || "uploaded-file";
}

function uniqueTargetPath(dir: string, fileName: string): string {
  const parsed = path.parse(fileName);
  let candidate = path.join(/*turbopackIgnore: true*/ dir, fileName);
  let index = 1;
  while (fs.existsSync(candidate)) {
    candidate = path.join(
      /*turbopackIgnore: true*/
      dir,
      `${parsed.name}-${index}${parsed.ext}`,
    );
    index += 1;
  }
  return candidate;
}

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
    .where(eq(tasks.id, id))
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
  const membership = await getProjectMembership(user.id, task.projectId);
  if (!membership && user.role !== "admin") {
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
  const membership = await getProjectMembership(user.id, task.projectId);
  if (!canWriteMembership(user, membership)) {
    return NextResponse.json({ detail: "Permission denied" }, { status: 403 });
  }

  const form = await request.formData();
  const file = form.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ detail: "file is required" }, { status: 400 });
  }

  const fileName = sanitizeFileName(file.name || "uploaded-file");
  const ext = path.extname(fileName).toLowerCase();
  if (BLOCKED_EXTENSIONS.has(ext)) {
    return NextResponse.json(
      { detail: "This file extension cannot be uploaded" },
      { status: 400 },
    );
  }

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

  fs.mkdirSync(targetDir, { recursive: true });
  const buffer = Buffer.from(await file.arrayBuffer());
  const targetPath = uniqueTargetPath(targetDir, fileName);
  fs.writeFileSync(targetPath, buffer);

  const relativePath = projectRelativePath(storageRoot, targetPath);
  const mimeType = file.type || null;
  const [attachment] = await db
    .insert(taskAttachments)
    .values({
      taskId: id,
      projectId: task.projectId,
      filePath: relativePath,
      displayName: path.basename(targetPath),
      mimeType,
      sizeBytes: buffer.byteLength,
      kind: attachmentKind(mimeType, fileName),
      createdBy: user.id,
      attachmentMetadata: {},
    })
    .returning();

  return NextResponse.json(serializeAttachment(attachment, id), { status: 201 });
}

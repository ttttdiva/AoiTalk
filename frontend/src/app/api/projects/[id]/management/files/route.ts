import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { db } from "@/db";
import { projectMembers, projects, users } from "@/db/schema";
import { and, eq } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  getAccessibleProject,
  getWritableProject,
} from "@/lib/server/project-access";
import {
  calculateProjectStorageUsage,
  ensureProjectStorageRoot,
  isPathInsideProjectStorageRoot,
  mergeManagementConfigIntoMetadata,
  normalizeProjectFilePath,
  normalizeProjectManagementConfig,
} from "@/lib/server/project-workspace-management";
import { hasProjectPermission } from "@/lib/server/project-permissions";
import {
  exceedsUploadSizeLimit,
  sanitizeUploadFileName,
  writeUniqueUploadFile,
} from "@/lib/server/attachment-upload";

const MANAGEMENT_KINDS = new Set([
  "wbs",
  "issue",
  "risk",
  "request",
  "attachment",
]);

const IDEMPOTENCY_HEADER = "x-idempotency-key";
const IDEMPOTENCY_METADATA_KEY = "_upload_idempotency";
const MAX_IDEMPOTENCY_KEY_LENGTH = 512;

type IdempotencyRecord = {
  relativePath: string;
  name: string;
  size: number;
  kind: string;
};

function recordMap(value: unknown): Record<string, IdempotencyRecord> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const result: Record<string, IdempotencyRecord> = Object.create(null) as Record<
    string,
    IdempotencyRecord
  >;
  for (const [key, candidate] of Object.entries(value)) {
    if (
      candidate &&
      typeof candidate === "object" &&
      !Array.isArray(candidate) &&
      typeof candidate.relativePath === "string" &&
      typeof candidate.name === "string" &&
      typeof candidate.size === "number" &&
      typeof candidate.kind === "string"
    ) {
      result[key] = {
        relativePath: candidate.relativePath,
        name: candidate.name,
        size: candidate.size,
        kind: candidate.kind,
      };
    }
  }
  return result;
}

function metadataRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : {};
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

function kindFromValue(value: FormDataEntryValue | null): string {
  const kind = typeof value === "string" ? value.trim().toLowerCase() : "";
  return MANAGEMENT_KINDS.has(kind) ? kind : "attachment";
}

function resolveProjectDirectory(
  storageRoot: string,
  requestedPath: unknown,
): string | null {
  const relativePath = normalizeProjectFilePath(requestedPath) || "";
  const targetDir = path.resolve(
    /*turbopackIgnore: true*/ storageRoot,
    relativePath.replace(/\//g, path.sep),
  );
  const relativeToRoot = path.relative(storageRoot, targetDir);
  if (relativeToRoot.startsWith("..") || path.isAbsolute(relativeToRoot)) {
    return null;
  }
  if (!isPathInsideProjectStorageRoot(storageRoot, targetDir)) return null;
  return targetDir;
}

function projectRelativePath(storageRoot: string, targetPath: string): string {
  return path.relative(storageRoot, targetPath).replace(/\\/g, "/");
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const result = await getAccessibleProject(id, user.id);
  if (!result) {
    return NextResponse.json(
      { detail: "プロジェクトが見つからないか権限がありません" },
      { status: 404 },
    );
  }

  const storageRoot = ensureProjectStorageRoot(id);
  const targetDir = resolveProjectDirectory(
    storageRoot,
    request.nextUrl.searchParams.get("path"),
  );
  if (!targetDir) {
    return NextResponse.json(
      { detail: "保存先がプロジェクトフォルダ外です" },
      { status: 400 },
    );
  }

  if (!fs.existsSync(targetDir) || !fs.statSync(targetDir).isDirectory()) {
    return NextResponse.json(
      { detail: "フォルダが見つかりません" },
      { status: 404 },
    );
  }

  const directories: Array<{ name: string; path: string; modifiedAt: string }> =
    [];
  const files: Array<{
    name: string;
    path: string;
    size: number;
    modifiedAt: string;
    extension: string;
  }> = [];

  for (const entry of fs.readdirSync(targetDir, { withFileTypes: true })) {
    const absolutePath = path.join(targetDir, entry.name);
    let stat: fs.Stats;
    try {
      stat = fs.lstatSync(absolutePath);
    } catch {
      continue;
    }
    if (stat.isSymbolicLink()) continue;
    const relativePath = projectRelativePath(storageRoot, absolutePath);
    if (entry.isDirectory()) {
      directories.push({
        name: entry.name,
        path: relativePath,
        modifiedAt: stat.mtime.toISOString(),
      });
    } else if (entry.isFile()) {
      files.push({
        name: entry.name,
        path: relativePath,
        size: stat.size,
        modifiedAt: stat.mtime.toISOString(),
        extension: path.extname(entry.name).toLowerCase(),
      });
    }
  }

  directories.sort((a, b) => a.name.localeCompare(b.name, "ja"));
  files.sort((a, b) => a.name.localeCompare(b.name, "ja"));

  const currentPath = projectRelativePath(storageRoot, targetDir);
  const parentPath =
    currentPath && path.dirname(currentPath) !== "."
      ? path.dirname(currentPath).replace(/\\/g, "/")
      : currentPath
        ? ""
        : null;

  return NextResponse.json({
    currentPath,
    parentPath,
    directories,
    files,
  });
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const result = await getWritableProject(id, user);
  if (!result) {
    return NextResponse.json(
      { detail: "プロジェクトが見つからないか権限がありません" },
      { status: 404 },
    );
  }

  const form = await request.formData();
  const file = form.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ detail: "file が必要です" }, { status: 400 });
  }
  if (exceedsUploadSizeLimit(file.size)) {
    return NextResponse.json(
      { detail: "ファイルサイズは 50 MB までです" },
      { status: 413 },
    );
  }

  const fileName = sanitizeUploadFileName(file.name || "uploaded-file");
  const idempotencyKey = request.headers.get(IDEMPOTENCY_HEADER)?.trim() || null;
  if (idempotencyKey && idempotencyKey.length > MAX_IDEMPOTENCY_KEY_LENGTH) {
    return NextResponse.json(
      { detail: "Idempotency key is too long" },
      { status: 400 },
    );
  }
  if (idempotencyKey && /[\u0000-\u001f\u007f]/u.test(idempotencyKey)) {
    return NextResponse.json(
      { detail: "Idempotency key contains invalid characters" },
      { status: 400 },
    );
  }

  const kind = kindFromValue(form.get("kind"));
  const directory =
    normalizeProjectFilePath(form.get("directory")) ||
    (kind === "attachment" ? "attachments" : "management");
  const storageRoot = ensureProjectStorageRoot(id);
  const targetDir = path.resolve(
    /*turbopackIgnore: true*/ storageRoot,
    directory.replace(/\//g, path.sep),
  );
  const relativeToRoot = path.relative(storageRoot, targetDir);
  if (relativeToRoot.startsWith("..") || path.isAbsolute(relativeToRoot)) {
    return NextResponse.json(
      { detail: "保存先がプロジェクトフォルダ外です" },
      { status: 400 },
    );
  }
  if (!isPathInsideProjectStorageRoot(storageRoot, targetDir)) {
    return NextResponse.json(
      { detail: "保存先に不正なシンボリックリンクがあります" },
      { status: 400 },
    );
  }

  const buffer = Buffer.from(await file.arrayBuffer());
  let createdPath: string | null = null;
  let transactionCommitted = false;
  try {
    const upload = await db.transaction(async (tx) => {
      const [project] = await tx
        .select()
        .from(projects)
        .where(eq(projects.id, id))
        .limit(1)
        .for("update");
      if (!project || project.deletedAt) {
        throw new Error("Project not found");
      }

      const lockedStorageRoot = ensureProjectStorageRoot(id);
      const projectMetadata = metadataRecord(project.projectMetadata);
      const idempotency = recordMap(
        projectMetadata[IDEMPOTENCY_METADATA_KEY],
      );
      // The row lock orders this ACL check with membership mutations.  Do
      // not reuse the pre-transaction session snapshot after waiting for the
      // lock; read the principal and membership in this transaction.
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
            eq(projectMembers.projectId, id),
            eq(projectMembers.userId, user.id),
          ),
        )
        .limit(1);
      const canWrite =
        principal?.role === "admin" ||
        project.ownerId === user.id ||
        hasProjectPermission(membership?.permissions, "write");
      if (!canWrite) throw new ProjectPermissionError();

      if (idempotencyKey && idempotency[idempotencyKey]) {
        const existing = idempotency[idempotencyKey];
        const existingPath = path.resolve(
          /*turbopackIgnore: true*/ lockedStorageRoot,
          existing.relativePath.replace(/\//g, path.sep),
        );
        if (
          isPathInsideProjectStorageRoot(lockedStorageRoot, existingPath) &&
          fs.existsSync(existingPath) &&
          fs.statSync(existingPath).isFile()
        ) {
          return {
            relativePath: existing.relativePath,
            config: normalizeProjectManagementConfig(project.projectMetadata),
            name: existing.name,
            size: existing.size,
            kind: existing.kind,
            registered: existing.kind !== "attachment",
          };
        }
        // A stale map entry must not make a successful retry return a missing
        // file.  It is replaced by the new committed upload below.
        delete idempotency[idempotencyKey];
      }

      const lockedTargetDir = path.resolve(
        /*turbopackIgnore: true*/ lockedStorageRoot,
        directory.replace(/\\/g, path.sep),
      );
      const lockedRelativeToRoot = path.relative(
        lockedStorageRoot,
        lockedTargetDir,
      );
      if (
        lockedRelativeToRoot.startsWith("..") ||
        path.isAbsolute(lockedRelativeToRoot) ||
        !isPathInsideProjectStorageRoot(lockedStorageRoot, lockedTargetDir)
      ) {
        throw new Error("Project storage path changed outside the workspace");
      }

      const usage = calculateProjectStorageUsage(lockedStorageRoot);
      const quotaMb = Math.max(0, Number(project.storageQuotaMb ?? 1000));
      const quotaBytes = quotaMb * 1024 * 1024;
      if (usage.totalBytes + buffer.byteLength > quotaBytes) {
        throw new ProjectQuotaExceededError();
      }

      fs.mkdirSync(lockedTargetDir, { recursive: true });
      if (!isPathInsideProjectStorageRoot(lockedStorageRoot, lockedTargetDir)) {
        throw new Error("Project storage path changed outside the workspace");
      }
      createdPath = writeUniqueUploadFile(lockedTargetDir, fileName, buffer);
      if (!isPathInsideProjectStorageRoot(lockedStorageRoot, createdPath)) {
        throw new Error("Project upload path changed outside the workspace");
      }

      const relativePath = path
        .relative(lockedStorageRoot, createdPath)
        .replace(/\\/g, "/");
      let config = normalizeProjectManagementConfig(project.projectMetadata);
      const updates: {
        storageUsedMb: number;
        projectMetadata?: unknown;
        updatedAt: Date;
      } = {
        storageUsedMb:
          (usage.totalBytes + buffer.byteLength) / (1024 * 1024),
        updatedAt: new Date(),
      };
      if (kind !== "attachment") {
        const nextRequestFiles =
          kind === "request"
            ? [...new Set([...config.requestFiles, relativePath])]
            : config.requestFiles;
        const metadata = mergeManagementConfigIntoMetadata(
          project.projectMetadata,
          {
            workspaceRoot: null,
            wbsFile: kind === "wbs" ? relativePath : config.wbsFile,
            issueFile: kind === "issue" ? relativePath : config.issueFile,
            riskFile: kind === "risk" ? relativePath : config.riskFile,
            requestFiles: nextRequestFiles,
            taskRules: config.taskRules,
          },
        );
        updates.projectMetadata = metadata;
        config = normalizeProjectManagementConfig(metadata);
      }

      if (idempotencyKey) {
        const nextIdempotency: Record<string, IdempotencyRecord> = {
          ...idempotency,
          [idempotencyKey]: {
            relativePath,
            name: fileName,
            size: buffer.byteLength,
            kind,
          },
        };
        // Keep metadata bounded; keys are only a retry ledger and should not
        // grow a project row without limit.
        const recentEntries = Object.entries(nextIdempotency).slice(-64);
        const nextMetadata = {
          ...metadataRecord(updates.projectMetadata ?? project.projectMetadata),
          [IDEMPOTENCY_METADATA_KEY]: Object.fromEntries(recentEntries),
        };
        updates.projectMetadata = nextMetadata;
      }

      await tx
        .update(projects)
        .set(updates)
        .where(eq(projects.id, id));

      return {
        relativePath,
        config,
        name: fileName,
        size: buffer.byteLength,
        kind,
        registered: kind !== "attachment",
      };
    });
    transactionCommitted = true;

    return NextResponse.json({
      success: true,
      kind,
      name: upload.name,
      path: upload.relativePath,
      size: upload.size,
      registered: upload.registered,
      config: upload.config,
    });
  } catch (error) {
    if (!transactionCommitted && createdPath) {
      try {
        if (isPathInsideProjectStorageRoot(storageRoot, createdPath)) {
          fs.rmSync(createdPath, { force: true });
        }
      } catch {
        console.warn("Failed to remove orphaned project upload", createdPath);
      }
    }
    if (error instanceof ProjectQuotaExceededError) {
      return NextResponse.json(
        { detail: error.message },
        { status: 413 },
      );
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

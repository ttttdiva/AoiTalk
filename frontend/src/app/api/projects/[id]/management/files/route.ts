import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { db } from "@/db";
import { projects } from "@/db/schema";
import { eq } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  getAccessibleProject,
  getWritableProject,
} from "@/lib/server/project-access";
import {
  ensureProjectStorageRoot,
  mergeManagementConfigIntoMetadata,
  normalizeProjectFilePath,
  normalizeProjectManagementConfig,
} from "@/lib/server/project-workspace-management";

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

const MANAGEMENT_KINDS = new Set([
  "wbs",
  "issue",
  "risk",
  "request",
  "attachment",
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
  let candidate = path.join(dir, fileName);
  let index = 1;
  while (fs.existsSync(candidate)) {
    candidate = path.join(dir, `${parsed.name}-${index}${parsed.ext}`);
    index += 1;
  }
  return candidate;
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
    const stat = fs.statSync(absolutePath);
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

  const fileName = sanitizeFileName(file.name || "uploaded-file");
  const ext = path.extname(fileName).toLowerCase();
  if (BLOCKED_EXTENSIONS.has(ext)) {
    return NextResponse.json(
      { detail: "この拡張子のファイルはアップロードできません" },
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

  fs.mkdirSync(targetDir, { recursive: true });
  const targetPath = uniqueTargetPath(targetDir, fileName);
  const buffer = Buffer.from(await file.arrayBuffer());
  fs.writeFileSync(targetPath, buffer);
  const relativePath = path
    .relative(storageRoot, targetPath)
    .replace(/\\/g, "/");

  let config = normalizeProjectManagementConfig(result.project.projectMetadata);
  if (kind !== "attachment") {
    const nextRequestFiles =
      kind === "request"
        ? [...new Set([...config.requestFiles, relativePath])]
        : config.requestFiles;
    const metadata = mergeManagementConfigIntoMetadata(
      result.project.projectMetadata,
      {
        workspaceRoot: null,
        wbsFile: kind === "wbs" ? relativePath : config.wbsFile,
        issueFile: kind === "issue" ? relativePath : config.issueFile,
        riskFile: kind === "risk" ? relativePath : config.riskFile,
        requestFiles: nextRequestFiles,
        taskRules: config.taskRules,
      },
    );
    const [updated] = await db
      .update(projects)
      .set({ projectMetadata: metadata, updatedAt: new Date() })
      .where(eq(projects.id, id))
      .returning();
    config = normalizeProjectManagementConfig(updated.projectMetadata);
  }

  return NextResponse.json({
    success: true,
    kind,
    name: fileName,
    path: relativePath,
    size: buffer.byteLength,
    registered: kind !== "attachment",
    config,
  });
}

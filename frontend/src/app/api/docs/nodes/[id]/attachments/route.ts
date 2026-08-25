import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { db } from "@/db";
import { knowledgeAttachments } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  requireDocsNode,
  serializeAttachment,
} from "@/lib/server/knowledge-docs-utils";
import {
  getWorkspacesBaseDir,
  isPathInsideProjectStorageRoot,
} from "@/lib/server/project-workspace-management";
import {
  exceedsUploadSizeLimit,
  sanitizeUploadFileName,
  writeUniqueUploadFile,
} from "@/lib/server/attachment-upload";

function mimeTypeForFile(fileName: string, supplied: string): string | null {
  const normalized = supplied.trim().toLowerCase();
  if (normalized) return normalized;
  const extension = path.extname(fileName).toLowerCase();
  return {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
  }[extension] ?? null;
}

/**
 * Store one Docs attachment in the workspace-relative Docs attachment area.
 * The filesystem write intentionally happens before the metadata insert; a
 * failed insert removes the unreferenced file in the catch block below.
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await requireDocsNode(id, user, "write");
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }

  const form = await request.formData();
  const file = form.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ detail: "fileが必要です" }, { status: 400 });
  }
  if (exceedsUploadSizeLimit(file.size)) {
    return NextResponse.json(
      { detail: "ファイルサイズは 50 MB までです" },
      { status: 413 },
    );
  }

  const workspacesRoot = path.resolve(/* turbopackIgnore: true */ getWorkspacesBaseDir());
  const relativeDir = path.join("_docs", "attachments", access.node.id);
  const targetDir = path.resolve(/* turbopackIgnore: true */ workspacesRoot, relativeDir);
  try {
    const rootInfo = fs.lstatSync(workspacesRoot);
    if (rootInfo.isSymbolicLink()) throw new Error("workspace root is a symlink");
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && (error as { code?: string }).code === "ENOENT") {
      // The trusted workspace root may be created on first upload.
    } else {
      return NextResponse.json({ detail: "添付ファイルの保存先が不正です" }, { status: 400 });
    }
  }
  fs.mkdirSync(workspacesRoot, { recursive: true });
  if (!isPathInsideProjectStorageRoot(workspacesRoot, targetDir)) {
    return NextResponse.json({ detail: "添付ファイルの保存先が不正です" }, { status: 400 });
  }

  const fileName = sanitizeUploadFileName(file.name || "uploaded-file");
  const buffer = Buffer.from(await file.arrayBuffer());
  let createdPath: string | null = null;
  try {
    fs.mkdirSync(targetDir, { recursive: true });
    if (!isPathInsideProjectStorageRoot(workspacesRoot, targetDir)) {
      throw new Error("添付ファイルの保存先が不正です");
    }
    createdPath = writeUniqueUploadFile(targetDir, fileName, buffer);
    if (!isPathInsideProjectStorageRoot(workspacesRoot, createdPath)) {
      throw new Error("添付ファイルの保存先が不正です");
    }

    const relativePath = path.relative(workspacesRoot, createdPath).replace(/\\/g, "/");
    const [row] = await db
      .insert(knowledgeAttachments)
      .values({
        nodeId: access.node.id,
        fileName: path.basename(createdPath),
        filePath: relativePath,
        mimeType: mimeTypeForFile(path.basename(createdPath), file.type),
        sizeBytes: buffer.byteLength,
        attachmentMetadata: {},
        createdBy: user.id,
      })
      .returning();
    if (!row) throw new Error("添付ファイル情報を保存できませんでした");
    return NextResponse.json(serializeAttachment(row), { status: 201 });
  } catch (error) {
    if (createdPath) {
      try {
        const info = fs.lstatSync(createdPath);
        if (
          info.isFile() &&
          !info.isSymbolicLink() &&
          isPathInsideProjectStorageRoot(workspacesRoot, createdPath)
        ) {
          fs.rmSync(createdPath, { force: true });
        }
      } catch {
        console.warn("Docs添付ファイルの孤立ファイルを削除できませんでした", createdPath);
      }
    }
    throw error;
  }
}

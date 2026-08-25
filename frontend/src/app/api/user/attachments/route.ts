import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { getSession } from "@/lib/auth";
import {
  exceedsUploadSizeLimit,
  sanitizeUploadFileName,
  writeUniqueUploadFile,
} from "@/lib/server/attachment-upload";
import { getWorkspacesBaseDir } from "@/lib/server/project-workspace-management";

/** プロジェクト未選択時のチャット添付をユーザー個人ストレージへ保存する */
export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
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

  const relativeDir = `_users/user_${user.id}/attachments`;
  const workspacesRoot = getWorkspacesBaseDir();
  const targetDir = path.resolve(
    /*turbopackIgnore: true*/ workspacesRoot,
    relativeDir.replace(/\//g, path.sep),
  );
  fs.mkdirSync(targetDir, { recursive: true });

  const buffer = Buffer.from(await file.arrayBuffer());
  const targetPath = writeUniqueUploadFile(targetDir, fileName, buffer);

  const savedName = path.basename(targetPath);
  return NextResponse.json({
    name: savedName,
    path: `${relativeDir}/${savedName}`,
    size: buffer.byteLength,
  });
}

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  AVATAR_MIME_EXTENSIONS,
  MAX_AVATAR_BYTES,
  avatarUrl,
  ensureAvatarStorageRoot,
  getAvatarRelativePath,
  removeAvatarFile,
  validateAvatarImage,
} from "@/lib/server/user-avatar";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function errorResponse(detail: string, status: number) {
  return NextResponse.json({ detail }, { status });
}

async function getAvatarFile(request: NextRequest): Promise<File | NextResponse> {
  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return errorResponse("multipart/form-data が必要です", 400);
  }
  const file = form.get("file");
  if (!(file instanceof File)) return errorResponse("file が必要です", 400);
  if (!Number.isFinite(file.size) || file.size <= 0) {
    return errorResponse("画像ファイルが空です", 400);
  }
  if (file.size > MAX_AVATAR_BYTES) {
    return errorResponse("アイコン画像は5MB以下にしてください", 413);
  }

  const declaredMime = file.type.trim().toLowerCase().split(";", 1)[0];
  if (!(declaredMime in AVATAR_MIME_EXTENSIONS)) {
    return errorResponse("JPEG、PNG、GIF、WebP画像のみ対応しています", 415);
  }
  return file;
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) return errorResponse("認証が必要です", 401);

  const fileResult = await getAvatarFile(request);
  if (fileResult instanceof NextResponse) return fileResult;
  const file = fileResult;
  const bytes = Buffer.from(await file.arrayBuffer());
  const declaredMime = file.type.trim().toLowerCase().split(";", 1)[0];
  let imageMetadata: Awaited<ReturnType<typeof validateAvatarImage>>;
  try {
    imageMetadata = await validateAvatarImage(bytes, declaredMime);
  } catch {
    return errorResponse("有効な画像データではありません", 400);
  }

  const extension = AVATAR_MIME_EXTENSIONS[imageMetadata.mime];
  const fileName = `${crypto.randomUUID()}${extension}`;
  let targetPath: string | null = null;
  let targetCreated = false;
  try {
    const storageRoot = ensureAvatarStorageRoot(user.id);
    targetPath = path.join(storageRoot, fileName);
    // UUID names are random, but exclusive creation also protects against an
    // accidental collision and makes a partial write impossible to publish.
    const descriptor = fs.openSync(targetPath, "wx", 0o600);
    targetCreated = true;
    try {
      fs.writeFileSync(descriptor, bytes);
    } finally {
      fs.closeSync(descriptor);
    }
  } catch {
    if (targetCreated && targetPath) {
      try {
        fs.rmSync(targetPath, { force: true });
      } catch {
        // Preserve the upload error; an unreferenced random file can be
        // removed by subsequent storage cleanup.
      }
    }
    return errorResponse("アイコン画像を保存できませんでした", 500);
  }

  if (!targetPath) return errorResponse("アイコン画像を保存できませんでした", 500);

  const relativePath = getAvatarRelativePath(user.id, fileName);
  let updated: { id: string; avatarPath: string | null } | undefined;
  try {
    [updated] = await db
      .update(users)
      .set({ avatarPath: relativePath, updatedAt: new Date() })
      .where(eq(users.id, user.id))
      .returning({ id: users.id, avatarPath: users.avatarPath });
  } catch {
    try {
      fs.rmSync(targetPath, { force: true });
    } catch {
      // Preserve the original DB error response; a later cleanup can remove
      // this unreferenced random file safely.
    }
    return errorResponse("アイコン情報を更新できませんでした", 500);
  }

  if (!updated) {
    try {
      fs.rmSync(targetPath, { force: true });
    } catch {
      // Best effort cleanup for an unexpected missing user row.
    }
    return errorResponse("ユーザーが見つかりません", 404);
  }

  const previousPath = user.avatarPath;
  if (previousPath && previousPath !== updated.avatarPath) {
    try {
      removeAvatarFile(user.id, previousPath);
    } catch {
      // The new DB reference is already durable.  Invalid/stale old files are
      // intentionally not allowed to make a successful update fail.
    }
  }

  return NextResponse.json({ avatar_url: avatarUrl(updated.id, updated.avatarPath) });
}

export async function DELETE() {
  const user = await getSession();
  if (!user) return errorResponse("認証が必要です", 401);

  const previousPath = user.avatarPath;
  try {
    await db
      .update(users)
      .set({ avatarPath: null, updatedAt: new Date() })
      .where(eq(users.id, user.id));
  } catch {
    return errorResponse("アイコン情報を削除できませんでした", 500);
  }

  if (previousPath) {
    try {
      removeAvatarFile(user.id, previousPath);
    } catch {
      // Keep the response successful once the DB reference is removed.
    }
  }
  return NextResponse.json({ avatar_url: null });
}

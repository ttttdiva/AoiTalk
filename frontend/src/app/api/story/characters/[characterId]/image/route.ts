import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { fetchPythonApi } from "@/lib/server/python-api-proxy";
import {
  AVATAR_MIME_EXTENSIONS,
  detectAvatarMime,
  ensureStoryCharacterImageStorageRoot,
  getStoryCharacterImageRelativePath,
  removeStoryCharacterImageFile,
  resolveStoredStoryCharacterImagePath,
  storyCharacterImageUrl,
  validateAvatarImage,
} from "@/lib/server/story-character-image";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type StoryCharacterRecord = {
  id: string;
  user_id: string;
  image_path?: string | null;
};

function errorResponse(detail: string, status: number) {
  return NextResponse.json({ detail }, { status });
}

async function loadOwnedCharacter(
  characterId: string,
  userId: string,
): Promise<StoryCharacterRecord | NextResponse> {
  const response = await fetchPythonApi(`/api/story/characters/${encodeURIComponent(characterId)}`, {
    method: "GET",
    user: { id: userId },
  });
  if (response.status === 404) {
    return errorResponse("登場人物が見つかりません", 404);
  }
  if (!response.ok) {
    return errorResponse("登場人物を取得できませんでした", response.status);
  }
  const character = (await response.json()) as StoryCharacterRecord;
  if (character.user_id !== userId) {
    return errorResponse("権限がありません", 403);
  }
  return character;
}

async function patchCharacterImagePath(
  characterId: string,
  userId: string,
  imagePath: string | null,
): Promise<NextResponse | null> {
  const response = await fetchPythonApi(`/api/story/characters/${encodeURIComponent(characterId)}`, {
    method: "PATCH",
    user: { id: userId },
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image_path: imagePath }),
  });
  if (!response.ok) {
    return errorResponse("画像参照を更新できませんでした", response.status);
  }
  return null;
}

async function getImageFile(request: NextRequest): Promise<File | NextResponse> {
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
  const declaredMime = file.type.trim().toLowerCase().split(";", 1)[0];
  if (!(declaredMime in AVATAR_MIME_EXTENSIONS)) {
    return errorResponse("JPEG、PNG、GIF、WebP画像のみ対応しています", 415);
  }
  return file;
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ characterId: string }> },
) {
  const sessionUser = await getSession();
  if (!sessionUser) return errorResponse("認証が必要です", 401);

  const { characterId } = await params;
  const characterResult = await loadOwnedCharacter(characterId, sessionUser.id);
  if (characterResult instanceof NextResponse) return characterResult;

  const targetPath = resolveStoredStoryCharacterImagePath(
    sessionUser.id,
    characterId,
    characterResult.image_path,
  );
  if (!targetPath) {
    return errorResponse("画像が設定されていません", 404);
  }

  let stat: fs.Stats;
  try {
    stat = fs.lstatSync(targetPath);
  } catch {
    return errorResponse("画像ファイルが見つかりません", 404);
  }
  if (stat.isSymbolicLink() || !stat.isFile()) {
    return errorResponse("画像ファイルが不正です", 404);
  }

  let data: Buffer;
  try {
    data = fs.readFileSync(targetPath);
  } catch {
    return errorResponse("画像ファイルを読み込めません", 404);
  }

  const mime = detectAvatarMime(data);
  if (!mime) {
    return errorResponse("許可されていない画像形式です", 415);
  }

  return new NextResponse(new Uint8Array(data), {
    headers: {
      "Content-Type": mime,
      "Content-Length": String(data.byteLength),
      "Cache-Control": "private, max-age=31536000, immutable",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ characterId: string }> },
) {
  const sessionUser = await getSession();
  if (!sessionUser) return errorResponse("認証が必要です", 401);

  const { characterId } = await params;
  const characterResult = await loadOwnedCharacter(characterId, sessionUser.id);
  if (characterResult instanceof NextResponse) return characterResult;

  const fileResult = await getImageFile(request);
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
    const storageRoot = ensureStoryCharacterImageStorageRoot(sessionUser.id, characterId);
    targetPath = path.join(storageRoot, fileName);
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
        // best effort
      }
    }
    return errorResponse("画像を保存できませんでした", 500);
  }

  const relativePath = getStoryCharacterImageRelativePath(sessionUser.id, characterId, fileName);
  const patchError = await patchCharacterImagePath(characterId, sessionUser.id, relativePath);
  if (patchError) {
    try {
      fs.rmSync(targetPath, { force: true });
    } catch {
      // best effort
    }
    return patchError;
  }

  const previousPath = characterResult.image_path;
  if (previousPath && previousPath !== relativePath) {
    try {
      removeStoryCharacterImageFile(sessionUser.id, characterId, previousPath);
    } catch {
      // DB reference is already updated.
    }
  }

  return NextResponse.json({
    image_path: relativePath,
    image_url: storyCharacterImageUrl(characterId, relativePath),
  });
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ characterId: string }> },
) {
  const sessionUser = await getSession();
  if (!sessionUser) return errorResponse("認証が必要です", 401);

  const { characterId } = await params;
  const characterResult = await loadOwnedCharacter(characterId, sessionUser.id);
  if (characterResult instanceof NextResponse) return characterResult;

  const previousPath = characterResult.image_path;
  const patchError = await patchCharacterImagePath(characterId, sessionUser.id, null);
  if (patchError) return patchError;

  if (previousPath) {
    try {
      removeStoryCharacterImageFile(sessionUser.id, characterId, previousPath);
    } catch {
      // DB reference is already cleared.
    }
  }

  return NextResponse.json({ image_url: null });
}

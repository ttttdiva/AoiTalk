import { lstat, readFile, realpath, rename, stat, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { isAbsolute, relative, resolve } from "node:path";
import { NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeAttachments } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { requireDocsNode } from "@/lib/server/knowledge-docs-utils";

function workspaceRoots() {
  return Array.from(new Set([
    process.env.AOITALK_WORKSPACES_DIR,
    resolve(process.cwd(), "workspaces"),
    resolve(process.cwd(), "../workspaces"),
  ]
    .filter((root): root is string => Boolean(root))
    .map((root) => resolve(root))));
}

function isWithinRoot(root: string, target: string) {
  const child = relative(root, target);
  return child !== "" && !child.startsWith("..") && !isAbsolute(child);
}

/** Resolve only relative paths and legacy absolute paths inside workspaces. */
function attachmentPathCandidates(filePath: string) {
  const raw = filePath.trim();
  if (!raw) return [] as Array<{ path: string; root: string }>;
  const normalized = raw.replace(/\\/g, "/");
  if (/^[A-Za-z]:/.test(normalized)) return [];
  const roots = workspaceRoots();
  if (isAbsolute(raw) || normalized.startsWith("/")) {
    const target = resolve(raw);
    const root = roots.find((candidate) => isWithinRoot(candidate, target));
    return root ? [{ path: target, root }] : [];
  }
  const parts = normalized.split("/").filter(Boolean);
  if (parts.some((part) => part === "." || part === "..")) return [];
  return roots
    .map((root) => ({ path: resolve(root, ...parts), root }))
    .filter(({ path, root }) => isWithinRoot(root, path));
}

async function resolveAttachmentPath(filePath: string) {
  for (const candidate of attachmentPathCandidates(filePath)) {
    try {
      const info = await lstat(candidate.path);
      // Reject symlinks rather than following them (including an escaped
      // symlinked directory component resolved by realpath below).
      if (info.isSymbolicLink()) continue;
      const [rootReal, targetReal] = await Promise.all([
        realpath(candidate.root),
        realpath(candidate.path),
      ]);
      if (!isWithinRoot(rootReal, targetReal)) continue;
      return { ...candidate, path: targetReal, info };
    } catch {
      // Try the next configured root for relative paths.
    }
  }
  return null;
}

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });

  const { id } = await params;
  const [attachment] = await db
    .select()
    .from(knowledgeAttachments)
    .where(eq(knowledgeAttachments.id, id))
    .limit(1);
  if (!attachment) return NextResponse.json({ detail: "添付ファイルが見つかりません" }, { status: 404 });

  const access = await requireDocsNode(attachment.nodeId, user, "read");
  if (!access) return NextResponse.json({ detail: "添付ファイルを閲覧できません" }, { status: 404 });

  try {
    const resolvedAttachment = await resolveAttachmentPath(attachment.filePath);
    if (!resolvedAttachment || !resolvedAttachment.info.isFile()) throw new Error("invalid attachment path");
    const info = await stat(resolvedAttachment.path);
    if (!info.isFile()) throw new Error("not a file");
    const data = await readFile(resolvedAttachment.path);
    const mimeType = attachment.mimeType || "application/octet-stream";
    const headers: Record<string, string> = {
      "Content-Type": mimeType,
      "Content-Length": String(data.byteLength),
      "Content-Disposition": `inline; filename*=UTF-8''${encodeURIComponent(attachment.fileName)}`,
      "Cache-Control": "private, max-age=3600",
      "X-Content-Type-Options": "nosniff",
    };
    if (mimeType.toLowerCase() === "image/svg+xml") {
      // Keep SVG preview support while preventing a same-origin attachment
      // opened in a new tab from executing embedded script or loading remote
      // active content.
      headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'";
    }
    return new NextResponse(data, {
      headers,
    });
  } catch {
    return NextResponse.json({ detail: "添付ファイルの実体がありません" }, { status: 404 });
  }
}

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });

  const { id } = await params;
  const [attachment] = await db
    .select()
    .from(knowledgeAttachments)
    .where(eq(knowledgeAttachments.id, id))
    .limit(1);
  if (!attachment) return NextResponse.json({ detail: "添付ファイルが見つかりません" }, { status: 404 });

  const access = await requireDocsNode(attachment.nodeId, user, "write");
  if (!access) return NextResponse.json({ detail: "添付ファイルを削除できません" }, { status: 404 });

  const resolvedAttachment = await resolveAttachmentPath(attachment.filePath);
  if (!resolvedAttachment) {
    return NextResponse.json({ detail: "添付ファイルの実体が不正です" }, { status: 404 });
  }
  const storedPath = resolvedAttachment.path;
  const isWorkspaceFile = true;

  // DB とファイルシステムは同一トランザクションにできないため、実体を同じ
  // ディレクトリの一時名へ退避してからDB行を消す。DB側が失敗した場合は
  // 元の名前へ戻し、片方だけが確定する orphan / broken reference を避ける。
  let quarantinedPath: string | undefined;
  if (isWorkspaceFile) {
    try {
      const info = await lstat(storedPath);
      if (!info.isFile() && !info.isSymbolicLink()) {
        return NextResponse.json({ detail: "添付ファイルの実体が不正です" }, { status: 500 });
      }
      quarantinedPath = `${storedPath}.deleting-${randomUUID()}`;
      await rename(storedPath, quarantinedPath);
    } catch (error) {
      const code = error && typeof error === "object" && "code" in error
        ? (error as { code?: string }).code
        : undefined;
      if (code !== "ENOENT") {
        console.error("Failed to remove Docs attachment file", storedPath, error);
        return NextResponse.json({ detail: "添付ファイルを削除できません" }, { status: 500 });
      }
    }
  }

  try {
    await db.delete(knowledgeAttachments).where(eq(knowledgeAttachments.id, id));
  } catch (error) {
    if (quarantinedPath) {
      try {
        await rename(quarantinedPath, storedPath);
      } catch (restoreError) {
        console.error("Failed to restore Docs attachment after metadata failure", storedPath, restoreError);
      }
    }
    console.error("Failed to remove Docs attachment metadata", id, error);
    return NextResponse.json({ detail: "添付ファイル情報を削除できません" }, { status: 500 });
  }

  if (quarantinedPath) {
    // DB削除が確定した後の最終unlinkに失敗しても、metadataは既に消えて
    // いるので次回のworkspace GCが一時ファイルを回収できる。
    await unlink(quarantinedPath).catch((error) => {
      console.error("Failed to remove quarantined Docs attachment file", quarantinedPath, error);
    });
  }

  return NextResponse.json({ ok: true });
}

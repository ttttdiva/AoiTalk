import { readFile, stat, unlink } from "node:fs/promises";
import { isAbsolute, relative, resolve } from "node:path";
import { NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeAttachments } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { requireDocsNode } from "@/lib/server/knowledge-docs-utils";

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
    const info = await stat(attachment.filePath);
    if (!info.isFile()) throw new Error("not a file");
    const data = await readFile(attachment.filePath);
    return new NextResponse(data, {
      headers: {
        "Content-Type": attachment.mimeType || "application/octet-stream",
        "Content-Length": String(data.byteLength),
        "Content-Disposition": `inline; filename*=UTF-8''${encodeURIComponent(attachment.fileName)}`,
        "Cache-Control": "private, max-age=3600",
      },
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

  await db.delete(knowledgeAttachments).where(eq(knowledgeAttachments.id, id));

  const workspaceRoots = [resolve(process.cwd(), "workspaces"), resolve(process.cwd(), "../workspaces")];
  const storedPath = resolve(attachment.filePath);
  const isWorkspaceFile = workspaceRoots.some((root) => {
    const child = relative(root, storedPath);
    return child !== "" && !child.startsWith("..") && !isAbsolute(child);
  });
  if (isWorkspaceFile) await unlink(storedPath).catch(() => undefined);

  return NextResponse.json({ ok: true });
}

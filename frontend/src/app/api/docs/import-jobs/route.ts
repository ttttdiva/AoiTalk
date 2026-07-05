import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { knowledgeImportItems, knowledgeImportJobs } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  cleanString,
  ensureDocsWorkspace,
  ensureProjectWritable,
  normalizeImportStatus,
  normalizeJsonObject,
  serializeImportItem,
  serializeImportJob,
} from "@/lib/server/knowledge-docs-utils";

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const workspace = await ensureDocsWorkspace(user);
  const projectId = cleanOptionalString(body.project_id, 80);
  if (projectId) {
    const access = await ensureProjectWritable(projectId, user);
    if (!access) {
      return NextResponse.json(
        { detail: "Projectへの書き込み権限がありません" },
        { status: 403 },
      );
    }
  }

  const sourceType = cleanString(body.source_type, "pasted_text", 40);
  const sourceName = cleanString(body.source_name, "Untitled import", 1000);
  const rawItems = Array.isArray(body.items) ? body.items : [];

  const result = await db.transaction(async (tx) => {
    const [job] = await tx
      .insert(knowledgeImportJobs)
      .values({
        workspaceId: workspace.id,
        projectId,
        sourceType,
        sourceName,
        status: normalizeImportStatus(body.status),
        optionsJson: normalizeJsonObject(body.options_json),
        summaryJson: normalizeJsonObject(body.summary_json),
        createdBy: user.id,
      })
      .returning();

    const itemValues = rawItems.flatMap((item: unknown, index: number) => {
      if (!item || typeof item !== "object") return [];
      const record = item as Record<string, unknown>;
      return [
        {
          jobId: job.id,
          sourceRef:
            cleanOptionalString(record.source_ref, 2000) ??
            `${sourceType}:${index + 1}`,
          title: cleanString(record.title, `Import item ${index + 1}`, 500),
          itemType: cleanString(record.item_type, "page", 40),
          status: normalizeImportStatus(record.status),
          previewJson: normalizeJsonObject(record.preview_json),
        },
      ];
    });

    const items =
      itemValues.length > 0
        ? await tx.insert(knowledgeImportItems).values(itemValues).returning()
        : [];
    return { job, items };
  });

  return NextResponse.json(
    {
      import_job: serializeImportJob(result.job),
      import_items: result.items.map(serializeImportItem),
    },
    { status: 201 },
  );
}

import { NextRequest, NextResponse } from "next/server";
import { and, eq, isNull, min } from "drizzle-orm";
import { db } from "@/db";
import {
  recordFields,
  recordRows,
  recordTables,
  recordViews,
  tags,
  taskAssignees,
  tasks,
  taskTags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getWritableProject } from "@/lib/server/project-access";
import {
  asRecord as asRecordValue,
  encryptRecordRowStorage,
  materializeRow,
} from "@/lib/server/record-table-utils";
import {
  normalizeProjectManagementConfig,
  readWbsRows,
  type WbsRow,
} from "@/lib/server/project-workspace-management";
import { toDbLocalTimestamp } from "@/lib/server/db-time";
import { canReadProjectId } from "@/lib/server/task-route-utils";

type JsonRecord = Record<string, unknown>;
const AI_GENERATED_TAG_NAME = "ai_generated";
const AI_GENERATED_TAG_COLOR = "#64748b";

function asRecord(value: unknown): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return { ...(value as JsonRecord) };
}

function rowDescription(row: WbsRow): string {
  const lines = [
    row.description,
    "",
    "WBS同期情報:",
    row.wbsId ? `- WBS: ${row.wbsId}` : null,
    `- シート: ${row.sheetName}`,
    `- 行: ${row.rowNumber}`,
    row.assignee ? `- 担当: ${row.assignee}` : null,
    row.progress != null ? `- 進捗: ${Math.round(row.progress * 100)}%` : null,
    row.requestText ? `- 確認事項: ${row.requestText}` : null,
  ].filter((line): line is string => line != null);
  return lines.join("\n").trim();
}

function toTaskTimestamp(value: string | null): string | null {
  return value ? `${value}T00:00:00` : null;
}

function buildWbsMetadata(row: WbsRow): JsonRecord {
  return {
    source: "wbs",
    wbs: {
      source_key: row.sourceKey,
      row_hash: row.rowHash,
      file_path: row.filePath,
      sheet_name: row.sheetName,
      row_number: row.rowNumber,
      wbs_id: row.wbsId,
      assignee: row.assignee,
      progress: row.progress,
      request_text: row.requestText,
      last_synced_at: new Date().toISOString(),
    },
  };
}

async function ensureAiGeneratedTag(
  spaceId: string,
  userId: string,
): Promise<string> {
  const [existing] = await db
    .select({ id: tags.id })
    .from(tags)
    .where(and(eq(tags.spaceId, spaceId), eq(tags.name, AI_GENERATED_TAG_NAME)))
    .limit(1);
  if (existing) return existing.id;

  const [tag] = await db
    .insert(tags)
    .values({
      spaceId,
      name: AI_GENERATED_TAG_NAME,
      color: AI_GENERATED_TAG_COLOR,
      createdBy: userId,
    })
    .returning({ id: tags.id });
  return tag.id;
}

const WBS_FIELD_DEFS = [
  {
    key: "title",
    label: "Title",
    fieldType: "text",
    isTitle: true,
    sortOrder: 0,
  },
  { key: "wbs_id", label: "WBS ID", fieldType: "text", sortOrder: 1 },
  { key: "status", label: "Status", fieldType: "select", sortOrder: 2 },
  { key: "priority", label: "Priority", fieldType: "select", sortOrder: 3 },
  {
    key: "planned_start",
    label: "Planned start",
    fieldType: "date",
    sortOrder: 4,
  },
  {
    key: "planned_end",
    label: "Planned end",
    fieldType: "date",
    isDue: true,
    sortOrder: 5,
  },
  {
    key: "actual_start",
    label: "Actual start",
    fieldType: "date",
    sortOrder: 6,
  },
  { key: "actual_end", label: "Actual end", fieldType: "date", sortOrder: 7 },
  { key: "assignee", label: "Assignee", fieldType: "text", sortOrder: 8 },
  { key: "progress", label: "Progress", fieldType: "number", sortOrder: 9 },
  {
    key: "request_text",
    label: "Request",
    fieldType: "long_text",
    sortOrder: 10,
  },
  { key: "sheet_name", label: "Sheet", fieldType: "text", sortOrder: 11 },
  { key: "row_number", label: "Row", fieldType: "number", sortOrder: 12 },
] as const;

function wbsRecordValues(row: WbsRow): JsonRecord {
  return {
    title: row.title,
    wbs_id: row.wbsId,
    status: row.status,
    priority: row.priority,
    planned_start: row.plannedStart,
    planned_end: row.plannedEnd,
    actual_start: row.actualStart,
    actual_end: row.actualEnd,
    assignee: row.assignee,
    progress: row.progress == null ? null : Math.round(row.progress * 100),
    request_text: row.requestText,
    sheet_name: row.sheetName,
    row_number: row.rowNumber,
  };
}

async function syncWbsRecordTable(
  projectId: string,
  userId: string,
  rows: WbsRow[],
  dryRun: boolean,
) {
  let table = (
    await db
      .select()
      .from(recordTables)
      .where(
        and(
          eq(recordTables.projectId, projectId),
          eq(recordTables.name, "WBS"),
          isNull(recordTables.deletedAt),
        ),
      )
      .limit(1)
  )[0];

  const createdRows: JsonRecord[] = [];
  const updatedRows: JsonRecord[] = [];
  let unchanged = 0;

  if (!table) {
    if (dryRun) {
      return {
        table_created: true,
        created: rows.length,
        updated: 0,
        unchanged: 0,
      };
    }
    [table] = await db
      .insert(recordTables)
      .values({
        projectId,
        name: "WBS",
        description: "Imported from project WBS Excel.",
        sortOrder: 0,
        memoryPolicy: "project_only",
        defaultSensitivity: "normal",
        createdBy: userId,
        tableMetadata: { source: "wbs" },
      })
      .returning();
    await db.insert(recordViews).values({
      tableId: table.id,
      name: "Grid",
      viewType: "grid",
      config: {},
      sortOrder: 0,
      createdBy: userId,
    });
  }

  let fields = await db
    .select()
    .from(recordFields)
    .where(
      and(eq(recordFields.tableId, table.id), isNull(recordFields.deletedAt)),
    );
  const existingKeys = new Set(fields.map((field) => field.key));
  const missingFields = WBS_FIELD_DEFS.filter(
    (field) => !existingKeys.has(field.key),
  );
  if (missingFields.length > 0 && !dryRun) {
    await db.insert(recordFields).values(
      missingFields.map((field) => ({
        tableId: table.id,
        key: field.key,
        label: field.label,
        fieldType: field.fieldType,
        sortOrder: field.sortOrder,
        isTitle: "isTitle" in field && field.isTitle === true,
        isDue: "isDue" in field && field.isDue === true,
        options: {},
      })),
    );
    fields = await db
      .select()
      .from(recordFields)
      .where(
        and(eq(recordFields.tableId, table.id), isNull(recordFields.deletedAt)),
      );
  }

  const existingRows = await db
    .select()
    .from(recordRows)
    .where(and(eq(recordRows.tableId, table.id), isNull(recordRows.deletedAt)));
  const bySourceKey = new Map<string, (typeof existingRows)[number]>();
  for (const row of existingRows) {
    const metadata = asRecordValue(row.rowMetadata);
    const sourceKey =
      typeof metadata.source_key === "string" ? metadata.source_key : null;
    if (sourceKey) bySourceKey.set(sourceKey, row);
  }

  for (const row of rows) {
    const existing = bySourceKey.get(row.sourceKey);
    const values = wbsRecordValues(row);
    const materialized = materializeRow(values, fields);
    const encryptedStorage = encryptRecordRowStorage(values, materialized);
    const metadata = {
      source: "wbs",
      source_key: row.sourceKey,
      row_hash: row.rowHash,
      file_path: row.filePath,
      sheet_name: row.sheetName,
      row_number: row.rowNumber,
      last_synced_at: new Date().toISOString(),
    };

    if (!existing) {
      createdRows.push({ title: row.title, wbs_id: row.wbsId });
      if (!dryRun) {
        await db.insert(recordRows).values({
          tableId: table.id,
          projectId,
          createdBy: userId,
          values: encryptedStorage.values,
          title: encryptedStorage.title,
          dueAt: materialized.dueAt,
          searchText: encryptedStorage.searchText,
          sensitivity: table.defaultSensitivity ?? "normal",
          rowMetadata: metadata,
        });
      }
      continue;
    }

    const oldMetadata = asRecordValue(existing.rowMetadata);
    if (oldMetadata.row_hash === row.rowHash) {
      unchanged += 1;
      continue;
    }

    updatedRows.push({
      row_id: existing.id,
      title: row.title,
      wbs_id: row.wbsId,
    });
    if (!dryRun) {
      await db
        .update(recordRows)
        .set({
          values: encryptedStorage.values,
          title: encryptedStorage.title,
          dueAt: materialized.dueAt,
          searchText: encryptedStorage.searchText,
          rowMetadata: { ...oldMetadata, ...metadata },
          updatedAt: new Date(),
        })
        .where(eq(recordRows.id, existing.id));
    }
  }

  return {
    table_id: table.id,
    table_created: false,
    created: createdRows.length,
    updated: updatedRows.length,
    unchanged,
  };
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  const userId = user.id;

  const { id } = await params;
  const projectResult = await getWritableProject(id, user);
  if (!projectResult) {
    return NextResponse.json(
      { detail: "プロジェクトが見つからないか権限がありません" },
      { status: 404 },
    );
  }
  const project = projectResult.project;

  const body = await request.json().catch(() => ({}));
  const dryRun = body.dry_run === true || body.dryRun === true;
  const syncTasks = body.sync_tasks === true || body.syncTasks === true;
  const config = normalizeProjectManagementConfig(project.projectMetadata);
  const scan = readWbsRows(id, config);
  if (scan.errors.length > 0 && scan.rows.length === 0) {
    return NextResponse.json(
      { detail: scan.errors.join("\n"), errors: scan.errors },
      { status: 400 },
    );
  }

  const created: JsonRecord[] = [];
  const updated: JsonRecord[] = [];
  const unchanged: JsonRecord[] = [];
  let aiGeneratedTagged = 0;
  let aiGeneratedTagId: string | null | undefined;

  async function getAiGeneratedTagId() {
    if (aiGeneratedTagId !== undefined) return aiGeneratedTagId;
    const spaceId = project.spaceId;
    aiGeneratedTagId = spaceId
      ? await ensureAiGeneratedTag(spaceId, userId)
      : null;
    return aiGeneratedTagId;
  }

  if (syncTasks) {
    const existingTasks = await db
      .select()
      .from(tasks)
      .where(and(eq(tasks.projectId, id), eq(tasks.source, "wbs")));

    const bySourceKey = new Map<string, (typeof existingTasks)[number]>();
    for (const task of existingTasks) {
      const metadata = asRecord(task.taskMetadata);
      const wbs = asRecord(metadata.wbs);
      const sourceKey =
        typeof wbs.source_key === "string" ? wbs.source_key : null;
      if (sourceKey) bySourceKey.set(sourceKey, task);
    }

    const hasNewTasks = scan.rows.some(
      (row) => !bySourceKey.has(row.sourceKey),
    );
    if (!dryRun && hasNewTasks && !project.spaceId) {
      return NextResponse.json(
        {
          detail:
            "プロジェクトがスペースに所属していないため、ai_generatedタグを付与できません",
        },
        { status: 400 },
      );
    }
    if (!dryRun && hasNewTasks && !(await canReadProjectId(user, id))) {
      return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
    }

    const [minRow] = await db
      .select({ minSort: min(tasks.sortOrder) })
      .from(tasks)
      .where(eq(tasks.projectId, id));
    let nextSortOrder = (minRow?.minSort ?? 0) - 1;

    for (const row of scan.rows) {
      const existing = bySourceKey.get(row.sourceKey);
      const description = rowDescription(row);
      const nextMetadata = buildWbsMetadata(row);

      if (!existing) {
        created.push({
          title: row.title,
          wbs_id: row.wbsId,
          due_at: row.plannedEnd,
        });
        if (!dryRun) {
          const [task] = await db
            .insert(tasks)
            .values({
              projectId: id,
              title: row.title,
              description,
              status: row.status,
              priority: row.priority,
              startAt: toTaskTimestamp(row.plannedStart ?? row.actualStart),
              endAt: toTaskTimestamp(row.plannedEnd ?? row.actualEnd),
              allDay: true,
              reminderOffsets: null,
              notificationsEnabled: true,
              source: "wbs",
              createdBy: userId,
              completedAt:
                row.status === "closed"
                  ? toDbLocalTimestamp(new Date())
                  : null,
              taskMetadata: nextMetadata,
              sortOrder: nextSortOrder,
            })
            .returning();
          nextSortOrder -= 1;
          await db.insert(taskAssignees).values({
            taskId: task.id,
            userId,
            isPrimary: true,
          });
          const tagId = await getAiGeneratedTagId();
          if (tagId) {
            await db.insert(taskTags).values({
              taskId: task.id,
              tagId,
            });
            aiGeneratedTagged += 1;
          }
        }
        continue;
      }

      const metadata = asRecord(existing.taskMetadata);
      const wbs = asRecord(metadata.wbs);
      if (wbs.row_hash === row.rowHash) {
        unchanged.push({
          task_id: existing.id,
          title: row.title,
          wbs_id: row.wbsId,
        });
        continue;
      }

      updated.push({
        task_id: existing.id,
        title: row.title,
        wbs_id: row.wbsId,
      });
      if (!dryRun) {
        await db
          .update(tasks)
          .set({
            title: row.title,
            description,
            status: row.status,
            priority: row.priority,
            startAt: toTaskTimestamp(row.plannedStart ?? row.actualStart),
            endAt: toTaskTimestamp(row.plannedEnd ?? row.actualEnd),
            allDay: true,
            completedAt:
              row.status === "closed" ? toDbLocalTimestamp(new Date()) : null,
            taskMetadata: {
              ...metadata,
              ...nextMetadata,
              wbs: {
                ...asRecord(metadata.wbs),
                ...asRecord(nextMetadata.wbs),
              },
            },
            updatedAt: new Date(),
          })
          .where(eq(tasks.id, existing.id));
      }
    }
  }

  const recordSync = await syncWbsRecordTable(id, user.id, scan.rows, dryRun);

  return NextResponse.json({
    dry_run: dryRun,
    sync_tasks: syncTasks,
    file_path: scan.filePath,
    errors: scan.errors,
    created,
    updated,
    unchanged_count: unchanged.length,
    record_sync: recordSync,
    ai_generated_tag: {
      name: AI_GENERATED_TAG_NAME,
      applied: aiGeneratedTagged,
      available: project.spaceId != null,
    },
    summary: {
      scanned: scan.rows.length,
      created: created.length,
      updated: updated.length,
      unchanged: unchanged.length,
    },
  });
}

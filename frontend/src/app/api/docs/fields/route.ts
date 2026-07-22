import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, max, sql } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeFields, knowledgeSupertagFields } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { normalizeDocsFieldType } from "@/lib/docs-model";
import {
  cleanString,
  ensureDocsWorkspace,
  normalizeJsonObject,
  requireDocsSupertag,
  serializeField,
  serializeSupertagField,
} from "@/lib/server/knowledge-docs-utils";

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const workspace = await ensureDocsWorkspace(user);
  const supertagId = cleanString(body.supertag_id, "", 80);
  const name = cleanString(body.name, "", 120);
  if (!supertagId || !name) {
    return NextResponse.json({ detail: "supertag_idとnameは必須です" }, { status: 400 });
  }

  const supertag = await requireDocsSupertag(supertagId, workspace.id);
  if (!supertag) {
    return NextResponse.json({ detail: "supertagが見つかりません" }, { status: 404 });
  }

  const fieldType = normalizeDocsFieldType(body.field_type);
  const optionsJson = normalizeJsonObject(body.options_json);
  const defaultValueJson = body.default_value_json ?? null;
  const result = await db.transaction(async (tx) => {
    // 同じField定義の並行作成を直列化し、同名重複を作らない。
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${workspace.id}:${name.toLowerCase()}:${fieldType}`}))`);
    const candidates = await tx
      .select()
      .from(knowledgeFields)
      .where(and(
        eq(knowledgeFields.workspaceId, workspace.id),
        sql`lower(${knowledgeFields.name}) = lower(${name})`,
        eq(knowledgeFields.fieldType, fieldType),
      ))
      .orderBy(asc(knowledgeFields.createdAt));
    const existing = candidates.find((candidate) => (
      canonicalJson(candidate.optionsJson ?? {}) === canonicalJson(optionsJson)
      && canonicalJson(candidate.defaultValueJson ?? null) === canonicalJson(defaultValueJson)
    ));
    if (candidates.length > 0 && !existing) {
      return { conflict: true as const };
    }
    const [maxRow] = await tx
      .select({ maxSort: max(knowledgeSupertagFields.sortOrder) })
      .from(knowledgeSupertagFields)
      .where(eq(knowledgeSupertagFields.supertagId, supertag.id));

    if (existing) {
      await tx
        .insert(knowledgeSupertagFields)
        .values({
          supertagId: supertag.id,
          fieldId: existing.id,
          sortOrder: (maxRow?.maxSort ?? 0) + 1,
          required: !!body.required,
          showInTemplate: true,
          optional: false,
        })
        .onConflictDoNothing();
      const [relation] = await tx.select().from(knowledgeSupertagFields).where(and(
        eq(knowledgeSupertagFields.supertagId, supertag.id),
        eq(knowledgeSupertagFields.fieldId, existing.id),
      ));
      return { row: existing, relation, reused: true as const, conflict: false as const };
    }

    const [created] = await tx
      .insert(knowledgeFields)
      .values({
        workspaceId: workspace.id,
        supertagId: supertag.id,
        name,
        fieldType,
        required: !!body.required,
        optionsJson,
        defaultValueJson,
        sortOrder: (maxRow?.maxSort ?? 0) + 1,
      })
      .returning();
    await tx
      .insert(knowledgeSupertagFields)
      .values({
        supertagId: supertag.id,
        fieldId: created.id,
        sortOrder: created.sortOrder ?? 0,
        required: !!created.required,
        showInTemplate: true,
        optional: false,
      })
      .onConflictDoNothing();
    const [relation] = await tx.select().from(knowledgeSupertagFields).where(and(
      eq(knowledgeSupertagFields.supertagId, supertag.id),
      eq(knowledgeSupertagFields.fieldId, created.id),
    ));
    return { row: created, relation, reused: false as const, conflict: false as const };
  });

  if (result.conflict) {
    return NextResponse.json(
      { detail: `同名Field「${name}」には異なる設定が既にあります` },
      { status: 409 },
    );
  }

  return NextResponse.json(
    {
      field: serializeField(result.row),
      supertag_field: serializeSupertagField(result.relation),
      reused: result.reused,
    },
    { status: result.reused ? 200 : 201 },
  );
}

import { and, eq } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes, knowledgeSupertags, projects, knowledgeFields, knowledgeFieldValues } from "@/db/schema";
import { normalizeFieldValueInput } from "@/lib/server/knowledge-docs-utils";

export async function ensureProjectInformationFieldValues(
  node: typeof knowledgeNodes.$inferSelect,
  supertag: typeof knowledgeSupertags.$inferSelect,
  project: typeof projects.$inferSelect,
  userId: string,
  client: Pick<typeof db, "select" | "insert"> = db,
) {
  const fields = (await client
    .select()
    .from(knowledgeFields)
    .where(
      and(
        eq(knowledgeFields.supertagId, supertag.id),
        // A malformed field can point at the canonical supertag while still
        // belonging to another Docs Library.  Definitions used for the
        // canonical Project node must stay inside the tag/library boundary.
        eq(knowledgeFields.docsLibraryId, supertag.docsLibraryId),
        eq(knowledgeFields.docsLibraryId, node.docsLibraryId),
      ),
    ))
    // Keep a defensive in-memory boundary as well.  It protects repair code
    // if a malformed row is returned by a compatibility/migration query or
    // a future query refactor accidentally drops one of the SQL predicates.
    .filter(
      (field: typeof knowledgeFields.$inferSelect) =>
        field.docsLibraryId === node.docsLibraryId &&
        field.docsLibraryId === supertag.docsLibraryId,
    );
  const projectField = fields.find(
    (field: typeof knowledgeFields.$inferSelect) => field.name === "Project" || field.systemKey === "project",
  );
  if (projectField) {
    const fieldValue = {
      ...normalizeFieldValueInput(projectField, project.id),
      nodeId: node.id,
      targetNodeId: null,
      updatedBy: userId,
    };
    await client
      .insert(knowledgeFieldValues)
      .values(fieldValue)
      .onConflictDoUpdate({
        target: [knowledgeFieldValues.nodeId, knowledgeFieldValues.fieldId],
        set: {
          valueJson: fieldValue.valueJson,
          valueText: fieldValue.valueText,
          valueNumber: fieldValue.valueNumber,
          valueDatetime: fieldValue.valueDatetime,
          targetNodeId: fieldValue.targetNodeId,
          updatedBy: fieldValue.updatedBy,
        },
      });
  }

  const pageRoleField = fields.find(
    (field: typeof knowledgeFields.$inferSelect) => field.name === "Page Role" || field.systemKey === "page_role",
  );
  if (pageRoleField) {
    await client
      .insert(knowledgeFieldValues)
      .values({
        ...normalizeFieldValueInput(pageRoleField, "canonical"),
        nodeId: node.id,
        targetNodeId: null,
        updatedBy: userId,
      })
      .onConflictDoNothing();
  }
}

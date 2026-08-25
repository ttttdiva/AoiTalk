import { and, eq, sql } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes } from "@/db/schema";

type ManagedNodeLike = {
  id: string;
  docsLibraryId: string;
  parentId: string | null;
  systemKey: string | null;
  displayProps: unknown;
};

type ManagedDocsQueryClient = Pick<typeof db, "select">;
type ManagedDocsTransactionClient = ManagedDocsQueryClient & Pick<typeof db, "execute">;

const MANAGED_PREFIXES: ReadonlyArray<readonly [string, string]> = [
  ["agent_memory", "legacy_agent_memory"],
  ["project_inbox", "project_inbox"],
  ["project_mail", "project_mail"],
  ["workspace_file_reference:", "workspace_file_reference"],
];

export function managedDocsDomain(
  node: Pick<ManagedNodeLike, "systemKey" | "displayProps">,
): string | null {
  const props =
    node.displayProps && typeof node.displayProps === "object" && !Array.isArray(node.displayProps)
      ? (node.displayProps as Record<string, unknown>)
      : {};
  if (props.system_managed === true && typeof props.managed_domain === "string") {
    return props.managed_domain.trim() || "system_managed";
  }
  const key = node.systemKey ?? "";
  for (const [prefix, domain] of MANAGED_PREFIXES) {
    if (key === prefix || key.startsWith(prefix)) return domain;
  }
  return null;
}

export class ManagedDocsMutationError extends Error {
  readonly status = 409;

  constructor(readonly domain: string) {
    super(`${domain} は専用機能が管理しているため、通常のDocs編集では変更できません`);
  }
}

/**
 * Generic Docs routes fail closed for the node and each ancestor. This also
 * protects legacy descendants created before managed metadata was introduced.
 */
export async function assertGenericDocsMutationAllowed(
  node: ManagedNodeLike,
  queryClient: ManagedDocsQueryClient = db,
): Promise<void> {
  let current: ManagedNodeLike | undefined = node;
  const visited = new Set<string>();
  while (current && !visited.has(current.id)) {
    visited.add(current.id);
    const domain = managedDocsDomain(current);
    if (domain) throw new ManagedDocsMutationError(domain);
    if (!current.parentId) return;
    const [parent] = await queryClient
      .select({
        id: knowledgeNodes.id,
        docsLibraryId: knowledgeNodes.docsLibraryId,
        parentId: knowledgeNodes.parentId,
        systemKey: knowledgeNodes.systemKey,
        displayProps: knowledgeNodes.displayProps,
      })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, current.parentId),
          eq(knowledgeNodes.docsLibraryId, current.docsLibraryId),
        ),
      )
      .limit(1);
    if (!parent) throw new ManagedDocsMutationError("unresolved_docs_ancestor");
    current = parent;
  }
}

/** Lock and reload the target before applying the ancestor policy in a write transaction. */
export async function lockAndAssertGenericDocsMutationAllowed(
  node: ManagedNodeLike,
  transaction: ManagedDocsTransactionClient,
): Promise<void> {
  await transaction.execute(
    sql`select id from knowledge_nodes where id=${node.id} and docs_library_id=${node.docsLibraryId} for update`,
  );
  const [current] = await transaction
    .select({
      id: knowledgeNodes.id,
      docsLibraryId: knowledgeNodes.docsLibraryId,
      parentId: knowledgeNodes.parentId,
      systemKey: knowledgeNodes.systemKey,
      displayProps: knowledgeNodes.displayProps,
    })
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.id, node.id),
        eq(knowledgeNodes.docsLibraryId, node.docsLibraryId),
      ),
    )
    .limit(1);
  if (!current) throw new ManagedDocsMutationError("unresolved_docs_node");
  await assertGenericDocsMutationAllowed(current, transaction);
}

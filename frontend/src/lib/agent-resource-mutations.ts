import type { AgentResourceMutation } from "@/lib/chat-api";

export const AGENT_RESOURCE_OPERATION_LABELS: Record<
  AgentResourceMutation["operation"],
  string
> = {
  created: "作成",
  updated: "更新",
  moved: "移動",
  archived: "アーカイブ",
  deleted: "削除",
};

export function dedupeAgentResourceMutations(
  mutations: AgentResourceMutation[] | null | undefined,
): AgentResourceMutation[] {
  const byResource = new Map<string, AgentResourceMutation>();
  for (const mutation of mutations ?? []) {
    if (
      mutation == null ||
      mutation.success === false ||
      !mutation.resource_id ||
      (mutation.resource_type !== "task" &&
        mutation.resource_type !== "docs_node")
    ) {
      continue;
    }
    const key = `${mutation.resource_type}:${mutation.resource_id}`;
    const previous = byResource.get(key);
    byResource.set(key, previous ? mergeMutation(previous, mutation) : mutation);
  }
  return [...byResource.values()];
}

/**
 * Compact only the inline assistant-message card projection.
 *
 * docs_create_nodes creates one durable node per outline line. A node is
 * hidden here only when its canonical parent is another outline-created node
 * from the same AgentRun. Missing provenance or hierarchy metadata is
 * intentionally fail-open; hierarchy is never inferred from titles, order,
 * indentation, or timestamps.
 */
export function compactAgentResourceMutationsForChatCards(
  mutations: AgentResourceMutation[] | null | undefined,
): AgentResourceMutation[] {
  const deduped = dedupeAgentResourceMutations(mutations);
  const outlineCreatedIds = new Set(
    deduped
      .filter(
        (mutation) =>
          mutation.resource_type === "docs_node" &&
          mutation.created_in_outline === true,
      )
      .map((mutation) => mutation.resource_id),
  );

  if (outlineCreatedIds.size === 0) return deduped;

  return deduped.filter((mutation) => {
    if (
      mutation.resource_type !== "docs_node" ||
      mutation.created_in_outline !== true
    ) {
      return true;
    }
    const parentId = mutation.parent_id?.trim();
    return (
      !parentId ||
      parentId === mutation.resource_id ||
      !outlineCreatedIds.has(parentId)
    );
  });
}

function mergeMutation(
  previous: AgentResourceMutation,
  current: AgentResourceMutation,
): AgentResourceMutation {
  return {
    ...previous,
    ...Object.fromEntries(
      Object.entries(current).filter(([, value]) => value != null && value !== ""),
    ),
    operation: current.operation,
    success: true,
  } as AgentResourceMutation;
}

export function agentResourceMutationDate(
  mutation: AgentResourceMutation,
): string | null {
  if (mutation.resource_type === "task") {
    return mutation.start_at ?? mutation.due_date ?? mutation.end_at ?? null;
  }
  return mutation.updated_at ?? mutation.occurred_at ?? null;
}

/** Keep the stored ISO value's calendar day/time stable across server/client timezones. */
export function formatAgentResourceMutationDate(
  value: string | null | undefined,
  allDay = false,
): string | null {
  const normalized = value?.trim();
  if (!normalized) return null;
  const match = normalized.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
  if (!match) return null;
  const [, , month, day, hour, minute] = match;
  if (allDay || !hour || !minute) return `${Number(month)}/${Number(day)}`;
  return `${Number(month)}/${Number(day)} ${hour}:${minute}`;
}

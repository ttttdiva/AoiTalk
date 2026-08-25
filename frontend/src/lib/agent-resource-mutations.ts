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

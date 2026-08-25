"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Archive,
  CalendarDays,
  FileText,
  ListTodo,
  Move,
  Pencil,
  Plus,
  Trash2,
} from "lucide-react";

import { useAgentRun } from "@/hooks/use-agent-run";
import type { AgentResourceMutation } from "@/lib/chat-api";
import {
  AGENT_RESOURCE_OPERATION_LABELS,
  agentResourceMutationDate,
  dedupeAgentResourceMutations,
  formatAgentResourceMutationDate,
} from "@/lib/agent-resource-mutations";
import { cn } from "@/lib/utils";

const MAX_VISIBLE_MUTATIONS = 6;

type AgentResourceMutationListProps = {
  runId: string | null | undefined;
  onTaskClick?: (taskId: string) => void;
};

function OperationIcon({
  operation,
}: {
  operation: AgentResourceMutation["operation"];
}) {
  const props = { className: "mr-0.5 inline size-2.5", "aria-hidden": true };
  if (operation === "created") return <Plus {...props} />;
  if (operation === "updated") return <Pencil {...props} />;
  if (operation === "moved") return <Move {...props} />;
  if (operation === "archived") return <Archive {...props} />;
  return <Trash2 {...props} />;
}

function operationClass(operation: AgentResourceMutation["operation"]): string {
  if (operation === "created") {
    return "bg-emerald-500/12 text-emerald-700 dark:text-emerald-300";
  }
  if (operation === "deleted" || operation === "archived") {
    return "bg-muted text-muted-foreground";
  }
  return "bg-primary/10 text-primary";
}

function resourceLabel(resourceType: AgentResourceMutation["resource_type"]): string {
  return resourceType === "task" ? "タスク" : "Docs";
}

function mutationIsOpenable(
  mutation: AgentResourceMutation,
  onTaskClick?: (taskId: string) => void,
): boolean {
  if (mutation.operation === "deleted" || mutation.operation === "archived") {
    return false;
  }
  return mutation.resource_type === "docs_node" || Boolean(onTaskClick);
}

function AgentResourceMutationCard({
  mutation,
  onTaskClick,
}: {
  mutation: AgentResourceMutation;
  onTaskClick?: (taskId: string) => void;
}) {
  const router = useRouter();
  const openable = mutationIsOpenable(mutation, onTaskClick);
  const date = formatAgentResourceMutationDate(
    agentResourceMutationDate(mutation),
    Boolean(mutation.all_day),
  );
  const disabledReason =
    mutation.operation === "deleted" || mutation.operation === "archived"
      ? `${resourceLabel(mutation.resource_type)}は${AGENT_RESOURCE_OPERATION_LABELS[mutation.operation]}済みのため開けません`
      : `${resourceLabel(mutation.resource_type)}を開けません`;
  const ariaLabel = openable
    ? `${mutation.title || "名称未取得"}（${resourceLabel(mutation.resource_type)}、${AGENT_RESOURCE_OPERATION_LABELS[mutation.operation]}）を開く`
    : `${mutation.title || "名称未取得"}（${disabledReason}）`;

  const content = (
    <>
      <span
        className={cn(
          "flex size-7 shrink-0 items-center justify-center rounded-md",
          operationClass(mutation.operation),
        )}
        aria-hidden="true"
      >
        {mutation.resource_type === "task" ? (
          <ListTodo className="size-3.5" />
        ) : (
          <FileText className="size-3.5" />
        )}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-1.5 text-[10px] leading-4 text-muted-foreground">
          <span
            className={cn(
              "shrink-0 rounded px-1 py-0.5 font-medium",
              operationClass(mutation.operation),
            )}
          >
            <OperationIcon operation={mutation.operation} />
            {AGENT_RESOURCE_OPERATION_LABELS[mutation.operation]}
          </span>
          <span className="truncate">{resourceLabel(mutation.resource_type)}</span>
        </span>
        <span className="mt-0.5 block truncate text-xs font-medium text-foreground">
          {mutation.title || "名称未取得"}
        </span>
        {(date || mutation.project_name) && (
          <span className="mt-0.5 flex min-w-0 items-center gap-1 truncate text-[10px] leading-4 text-muted-foreground">
            {date && (
              <span className="inline-flex shrink-0 items-center gap-0.5">
                <CalendarDays className="size-2.5" aria-hidden="true" />
                {date}
              </span>
            )}
            {mutation.project_name && (
              <span className="truncate">{mutation.project_name}</span>
            )}
          </span>
        )}
      </span>
    </>
  );

  if (openable) {
    return (
      <button
        type="button"
        className="flex min-w-0 flex-[1_1_14rem] items-center gap-2 rounded-lg border border-border/70 bg-card/60 px-2.5 py-2 text-left transition-colors hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:max-w-[22rem]"
        aria-label={ariaLabel}
        title={ariaLabel}
        data-testid="agent-resource-mutation-card"
        onClick={() => {
          if (mutation.resource_type === "task") {
            onTaskClick?.(mutation.resource_id);
          } else {
            router.push(`/docs/${mutation.resource_id}`);
          }
        }}
      >
        {content}
      </button>
    );
  }

  return (
    <div
      className="flex min-w-0 flex-[1_1_14rem] items-center gap-2 rounded-lg border border-border/50 bg-muted/30 px-2.5 py-2 opacity-75 sm:max-w-[22rem]"
      aria-label={ariaLabel}
      aria-disabled="true"
      title={ariaLabel}
      data-testid="agent-resource-mutation-card"
    >
      {content}
    </div>
  );
}

export function AgentResourceMutationList({
  runId,
  onTaskClick,
}: AgentResourceMutationListProps) {
  const [expanded, setExpanded] = useState(false);
  const { run } = useAgentRun(runId, {
    // AgentRunTimeline/関連情報パネルと同じRunストアを購読する。
    poll: Boolean(runId),
    pollTimeoutMs: 30_000,
  });
  const mutations = useMemo(
    () => dedupeAgentResourceMutations(run?.resource_mutations),
    [run?.resource_mutations],
  );

  if (!mutations.length) return null;

  const visibleMutations = expanded
    ? mutations
    : mutations.slice(0, MAX_VISIBLE_MUTATIONS);
  const hiddenCount = mutations.length - visibleMutations.length;

  return (
    <div
      className="mt-0.5 flex min-w-0 max-w-full flex-wrap gap-1.5"
      aria-label="Agentが操作した項目"
      data-testid="agent-resource-mutation-list"
    >
      {visibleMutations.map((mutation) => (
        <AgentResourceMutationCard
          key={`${mutation.resource_type}:${mutation.resource_id}`}
          mutation={mutation}
          onTaskClick={onTaskClick}
        />
      ))}
      {hiddenCount > 0 && (
        <button
          type="button"
          className="h-8 self-center rounded-md border border-border/60 px-2 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => setExpanded(true)}
          aria-label={`ほか${hiddenCount}件の操作結果を表示`}
        >
          ほか{hiddenCount}件
        </button>
      )}
    </div>
  );
}

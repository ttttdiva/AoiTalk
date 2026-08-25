import type { Task } from "@/lib/task-api";

function pushLine(lines: string[], label: string, value: unknown) {
  if (value === null || value === undefined) return;
  if (typeof value === "string" && value.trim().length === 0) return;
  lines.push(`- ${label}: ${String(value)}`);
}

export function buildTaskChatDraft(task: Task): string {
  const lines: string[] = [
    "Take over the following task.",
    "First inspect the task context and any relevant project/repository context available to you.",
    "Do not make code, data, schedule, or project state changes yet.",
    "Produce a concise execution proposal with scope, likely files/systems, risks, verification, and any blocking questions.",
    "Ask for a single approval before starting implementation or state-changing work.",
    "After approval, continue autonomously as much as possible and keep progress updates, decisions, blockers, and the final result in this conversation.",
    "",
    "Task details:",
  ];

  pushLine(lines, "Task ID", task.id);
  pushLine(lines, "Project ID", task.project_id);
  pushLine(lines, "Title", task.title);
  pushLine(lines, "Status", task.status);
  pushLine(lines, "Priority", task.priority);
  pushLine(lines, "Start", task.start_at);
  pushLine(lines, "Due", task.end_at);
  pushLine(lines, "Estimate hours", task.estimated_hours);

  const tagNames = (task.tags || []).map((tag) => tag.name).filter(Boolean);
  if (tagNames.length > 0) {
    lines.push(`- Tags: ${tagNames.join(", ")}`);
  }

  const assigneeNames = (task.assignees || [])
    .map((assignee) => assignee.display_name || assignee.username || "")
    .filter(Boolean);
  if (assigneeNames.length > 0) {
    lines.push(`- Assignees: ${assigneeNames.join(", ")}`);
  }

  if (task.description?.trim()) {
    lines.push("");
    lines.push("Description:");
    lines.push("```text");
    lines.push(task.description.trim());
    lines.push("```");
  }

  const metadata = task.metadata || {};
  const triageSummary =
    typeof metadata.agent_triage_summary === "string"
      ? metadata.agent_triage_summary.trim()
      : "";
  const triageQuestions = Array.isArray(metadata.agent_triage_questions)
    ? metadata.agent_triage_questions.filter(
        (item): item is string =>
          typeof item === "string" && item.trim().length > 0,
      )
    : [];
  if (triageSummary || triageQuestions.length > 0) {
    lines.push("");
    lines.push("Agent preparation:");
    pushLine(lines, "Summary", triageSummary);
    if (triageQuestions.length > 0) {
      lines.push("- Questions:");
      for (const question of triageQuestions) {
        lines.push(`  - ${question.trim()}`);
      }
    }
  }

  const subtasks = (task.subtasks || []).filter((subtask) =>
    subtask.title?.trim(),
  );
  if (subtasks.length > 0) {
    lines.push("");
    lines.push("Subtasks:");
    for (const subtask of subtasks) {
      lines.push(
        `- [${subtask.status === "closed" ? "x" : " "}] ${subtask.title.trim()}`,
      );
    }
  }

  return lines.join("\n");
}

export function buildTaskChatSessionTitle(title: string): string {
  const normalized = title.trim() || "Task";
  const clipped =
    normalized.length > 60
      ? `${normalized.slice(0, 57).trimEnd()}...`
      : normalized;
  return `Task: ${clipped}`;
}

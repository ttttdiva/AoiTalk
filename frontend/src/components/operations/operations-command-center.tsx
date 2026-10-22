"use client";

/* The command-center projection is synchronized from a read-only API snapshot. */

import Link from "next/link";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Bot,
  CalendarClock,
  Clock3,
  ExternalLink,
  Filter,
  Loader2,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogPortal, DialogTitle } from "@/components/ui/dialog";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { OperationsWorkExecution } from "./operations-work-execution";
import { OperationsMapCanvas } from "./operations-map-canvas";
import type { OperationsMapEdge, OperationsMapNode, OperationsMapPage } from "./operations-map-model";
import {
  executionAttentionLabel,
  executionDuration,
  executionTime,
} from "@/lib/operations-execution-api";
import { formatTimestamp } from "@/lib/agent-run-timeline-format";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";

export type OperationsCommandCenterView =
  | "overview"
  | "agents"
  | "work"
  | "activity";

type FilterScope = "all" | "space" | "project" | "agent";
type CommandWorkFilter = "all" | "open" | "completed";
type CommandPageKind = "work" | "agent";
type DeepLinkSearchStatus = "searching" | "stopped" | "missing" | null;
type SearchParamReader =
  | { get: (key: string) => string | null }
  | null
  | undefined;
type CommandFilters = {
  scope: FilterScope;
  spaceId: string;
  projectId: string;
  agentId: string;
  domain: string;
  state: string;
  range: string;
};

type CommandSummary = {
  activeAgents: number;
  working: number;
  idle: number;
  blocked: number;
  awaitingApproval: number;
  uncertain: number;
  failedStale: number;
  budgetUsed: number | null;
  budgetLimit: number | null;
  budgetPercent: number | null;
};

type CommandAgent = {
  id: string;
  name: string;
  state: string;
  jobTitle: string;
  responsibility: string;
  managerAgentId: string;
  managerRelation: "agent" | "outside_scope" | "human" | "unrecorded" | "unknown";
  primarySpaceId: string;
  space: string;
  spaceAssignmentIds: string[];
  projectGrantIds: string[];
  projectGrants: string[];
  currentWork: string;
  currentWorkId: string;
  latestRun: string;
  team: string;
  profile: string;
  personas: string[];
  character: string;
  capabilities: string[];
  budget: string;
  concurrency: string;
  blockers: string[];
  failures: string[];
  runHistory: Array<{ id: string; state: string; createdAt: string }>;
};

type CommandWork = {
  id: string;
  spaceId: string;
  projectId: string;
  assignedAgentId: string;
  parentWorkItemId: string;
  taskId: string;
  latestRunId: string;
  sourceType: string;
  sourceId: string;
  domain: string;
  space: string;
  project: string;
  agent: string;
  task: string;
  persona: string;
  app: string;
  state: string;
  updatedAt: string;
  priority: string;
  notBefore: string;
  deadline: string;
  leaseHealth: string;
  attempts: string;
  approval: string;
  budget: string;
  latestRun: string;
  elapsed: string;
  phase: string;
  tool: string;
  lastActivity: string;
  maxAttempts: string;
  blocker: string;
  attentionReasons: string[];
  href?: string;
};

type CommandActivity = {
  id: string;
  kind: string;
  event: string;
  actor: string;
  domain: string;
  state: string;
  createdAt: string;
  detail: string;
  href?: string;
};

type CommandAttention = {
  projectId: string;
  originWorkId: string;
  originAgentId: string;
  currentAssigneeId: string;
  currentAssignee?: string; currentAssigneeName?: string;
  actorName?: string; workName?: string; operationLabel?: string; targetKind?: string; targetName?: string;
  authorizationMode?: string;
  actor?: string; work?: string; operation?: string; target?: string; consequence?: string; decision?: string; version?: string;
  id: string;
  kind: string;
  title: string;
  detail: string;
  state: string;
  href?: string;
};

type CommandSpace = {
  id: string;
  name: string;
  state: string;
  detail: string;
};

type CommandProject = {
  id: string;
  name: string;
  state: string;
  detail: string;
  spaceId: string;
  href?: string;
};

type CommandPage = {
  available: number | null;
  hasMore: boolean;
  nextCursor: string | null;
};

type CommandSnapshot = {
  organization: { name: string; state: string; detail: string };
  summary: CommandSummary;
  agents: CommandAgent[];
  work: CommandWork[];
  agentsPage: CommandPage;
  workPage: CommandPage;
  activity: CommandActivity[];
  attention: CommandAttention[];
  spaces: CommandSpace[];
  projects: CommandProject[];
  schedules: Array<{ id: string; name: string; due: string; state: string }>;
  artifacts: Array<{
    id: string;
    name: string;
    kind: string;
    createdAt: string;
  }>;
  evidence: Array<{
    id: string;
    kind: string;
    detail: string;
    createdAt: string;
  }>;
  mediaStatus: Array<{ label: string; value: string; detail: string }>;
  runtimeUsage: Array<{ label: string; value: string; detail: string }>;
};

const EMPTY_SUMMARY: CommandSummary = {
  activeAgents: 0,
  working: 0,
  idle: 0,
  blocked: 0,
  awaitingApproval: 0,
  uncertain: 0,
  failedStale: 0,
  budgetUsed: null,
  budgetLimit: null,
  budgetPercent: null,
};

const EMPTY_SNAPSHOT: CommandSnapshot = {
  organization: { name: "Organization", state: "—", detail: "—" },
  summary: EMPTY_SUMMARY,
  agents: [],
  work: [],
  agentsPage: { available: null, hasMore: false, nextCursor: null },
  workPage: { available: null, hasMore: false, nextCursor: null },
  activity: [],
  attention: [],
  spaces: [],
  projects: [],
  schedules: [],
  artifacts: [],
  evidence: [],
  mediaStatus: [],
  runtimeUsage: [],
};

const UUID_OR_SAFE_ID = /^[a-z0-9][a-z0-9._:-]{0,127}$/iu;
const SECRET_KEY =
  /(?:password|secret|token|credential|api[_-]?key|authorization|prompt|model|provider|filesystem|file[_-]?path|storage[_-]?path|environment|env)/iu;

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function array(value: unknown, keys: string[] = []): unknown[] {
  if (Array.isArray(value)) return value;
  const root = record(value);
  for (const key of keys) {
    if (Array.isArray(root[key])) return root[key] as unknown[];
  }
  return [];
}

function firstRecord(value: unknown, keys: string[]): Record<string, unknown> {
  const root = record(value);
  for (const key of keys) {
    const candidate = record(root[key]);
    if (Object.keys(candidate).length) return candidate;
  }
  return root;
}

function nestedRecord(value: unknown, keys: string[]): Record<string, unknown> {
  const root = record(value);
  for (const key of keys) {
    const candidate = record(root[key]);
    if (Object.keys(candidate).length) return candidate;
  }
  return {};
}

function valueAt(root: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    if (root[key] !== undefined && root[key] !== null) return root[key];
  }
  return undefined;
}

function safeText(value: unknown, fallback = "—", max = 160): string {
  if (typeof value !== "string" && typeof value !== "number") return fallback;
  const text = String(value).replace(/[\u0000-\u001f\u007f]/gu, " ").trim();
  if (!text) return fallback;
  if (/(?:password|secret|token|api[_-]?key|authorization)\s*[:=]|bearer\s+|[a-z][a-z0-9+.-]*:\/\/[^/\s:@]+:[^/\s@]+@/iu.test(text)) return "[redacted]";
  const scrubbed = text.replace(/(?:[a-z]:[\\/]|\\\\|\/(?:home|users|var|tmp|workspace)\/)[^\s,;]*/giu, "[redacted]");
  return scrubbed.length > max ? `${scrubbed.slice(0, max - 1)}…` : scrubbed;
}

function safeId(value: unknown): string {
  const candidate = safeText(value, "", 128);
  return UUID_OR_SAFE_ID.test(candidate) ? candidate : "";
}

function safeList(value: unknown, max = 12): string[] {
  return array(value)
    .map((item) => {
      if (typeof item === "object" && item !== null) {
        const itemRecord = record(item);
        return safeText(valueAt(itemRecord, ["display_name", "name", "title", "id", "project_id", "persona_id"]), "", 80);
      }
      return safeText(item, "", 80);
    })
    .filter(Boolean)
    .slice(0, max);
}

function safeIdList(value: unknown, keys: string[], max = 32): string[] {
  return array(value)
    .map((item) => safeId(typeof item === "string" ? item : valueAt(record(item), keys)))
    .filter(Boolean)
    .slice(0, max);
}

function safeCompactValue(value: unknown, keys: string[]): string {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return safeText(value);
  const root = record(value);
  const direct = valueAt(root, keys);
  if (direct !== undefined && direct !== null) return safeText(direct);
  return Object.entries(root)
    .filter(([key]) => !SECRET_KEY.test(key))
    .slice(0, 4)
    .map(([key, item]) => `${safeText(key, "", 40)}: ${safeText(item, "", 60)}`)
    .filter(Boolean)
    .join(" · ") || "—";
}

function label(value: unknown, fallback = "—"): string {
  return safeText(value, fallback, 120);
}

function numberValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return null;
}

function count(root: Record<string, unknown>, keys: string[]): number {
  const value = valueAt(root, keys);
  return numberValue(value) ?? (Array.isArray(value) ? value.length : 0);
}

function percent(used: number | null, limit: number | null, root: Record<string, unknown>): number | null {
  const explicit = numberValue(valueAt(root, ["budget_percent", "budget_usage_percent", "usage_percent", "percent"]));
  if (explicit !== null) return Math.max(0, Math.min(100, explicit));
  if (used !== null && limit !== null && limit > 0) return Math.max(0, Math.min(100, (used / limit) * 100));
  return null;
}

function dateLabel(value: unknown): string {
  const text = safeText(value, "", 64);
  if (!text) return "—";
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  return date.toLocaleString("ja-JP", { dateStyle: "short", timeStyle: "short" });
}

function hrefFor(kind: string, id: string): string | undefined {
  if (!id) return undefined;
  if (kind === "task") return `/tasks?detail=${encodeURIComponent(id)}`;
  if (kind === "project") return `/projects?project_id=${encodeURIComponent(id)}`;
  if (kind === "agent") return `/operations?tab=agents&agent=${encodeURIComponent(id)}`;
  if (kind === "action" || kind === "external_action") return `/operations?tab=actions&action=${encodeURIComponent(id)}`;
  if (kind === "persona" || kind === "character") return `/operations?tab=personas&persona=${encodeURIComponent(id)}`;
  return undefined;
}

function normaliseSummary(payload: unknown): CommandSummary {
  const root = firstRecord(payload, ["summary", "metrics", "counts"]);
  const budget = nestedRecord(root, ["budget", "budget_usage", "usage"]);
  const budgetUsed = numberValue(valueAt(root, ["budget_used", "used_budget"])) ?? numberValue(valueAt(budget, ["used", "used_amount", "reserved"]));
  const budgetLimit = numberValue(valueAt(root, ["budget_limit", "budget_total"])) ?? numberValue(valueAt(budget, ["limit", "total"]));
  const failedStaleValue = numberValue(valueAt(root, ["failed_stale", "failedStale"]));
  return {
    activeAgents: count(root, ["active_agents", "activeAgents", "agents_active", "active_agent_count"]),
    working: count(root, ["working", "working_count", "working_agents", "running"]),
    idle: count(root, ["idle", "idle_count"]),
    blocked: count(root, ["blocked", "blocked_count"]),
    awaitingApproval: count(root, ["awaiting_approval", "awaitingApproval", "awaiting_approval_count", "approval_pending"]),
    uncertain: count(root, ["uncertain", "uncertain_count"]),
    failedStale: failedStaleValue ?? count(root, ["failed"]) + count(root, ["stale"]),
    budgetUsed,
    budgetLimit,
    budgetPercent: percent(budgetUsed, budgetLimit, { ...root, ...budget }),
  };
}

function normaliseAgents(payload: unknown): CommandAgent[] {
  return array(payload, ["agents", "items"]).map((entry) => {
    const root = record(entry);
    const id = safeId(valueAt(root, ["id", "agent_id"]));
    const latest = nestedRecord(root, ["latest_run", "latest_agent_run", "run"]);
    const work = nestedRecord(root, ["current_work", "work_item"]);
    const history = array(root.run_history ?? root.runs, ["items"]).map((run) => {
      const item = record(run);
      return {
        id: safeId(valueAt(item, ["id", "run_id"])),
        state: label(valueAt(item, ["state", "status"])),
        createdAt: dateLabel(valueAt(item, ["created_at", "started_at"])),
      };
    }).filter((item) => item.id).slice(0, 12);
    return {
      id,
      name: label(valueAt(root, ["display_name", "name", "agent_name"]), id || "Agent"),
      state: label(valueAt(root, ["state", "lifecycle_state", "status"])),
      jobTitle: label(valueAt(root, ["job_title", "title", "role"])),
      responsibility: label(valueAt(root, ["responsibility_summary", "mission", "summary"]), ""),
      managerAgentId: safeId(root.manager_agent_id),
      managerRelation: ["agent", "outside_scope", "human", "unrecorded"].includes(String(root.manager_relation))
        ? root.manager_relation as CommandAgent["managerRelation"] : "unknown",
      primarySpaceId: safeId(valueAt(root, ["primary_space_id", "space_id"])),
      space: label(valueAt(root, ["primary_space_name", "space_name", "primary_space_id", "space_id"])),
      spaceAssignmentIds: safeIdList(valueAt(root, ["space_assignments", "spaces"]), ["space_id", "id"]),
      projectGrantIds: safeIdList(valueAt(root, ["project_grants", "projects", "grants"]), ["project_id", "id"]),
      projectGrants: safeList(valueAt(root, ["project_grants", "projects", "grants"])),
      currentWork: label(valueAt(work, ["title", "name", "id", "work_item_id"])),
      currentWorkId: safeId(valueAt(work, ["id", "work_item_id"])),
      latestRun: label(valueAt(latest, ["id", "run_id", "status"])),
      team: label(valueAt(root, ["team_name", "agent_team_id", "team_id", "team"])),
      profile: label(valueAt(root, ["execution_profile_name", "execution_profile_id", "execution_profile", "profile_id"])),
      personas: safeList(valueAt(root, ["personas", "persona_assignments", "persona_names"])),
      character: label(valueAt(root, ["character_name", "character_id"])),
      capabilities: safeList(valueAt(root, ["capabilities", "effective_capabilities", "capability_ceiling"])),
      budget: label(valueAt(root, ["budget", "budget_summary", "budget_usage"])),
      concurrency: label(valueAt(root, ["concurrency", "concurrency_summary"])),
      blockers: safeList(valueAt(root, ["blockers", "blocker_reasons"])),
      failures: safeList(valueAt(root, ["failures", "failure_codes"])),
      runHistory: history,
    };
  }).filter((item) => item.id || item.name !== "Agent");
}

function normaliseWork(payload: unknown): CommandWork[] {
  return array(payload, ["work", "work_items", "items", "current_work"])
    .map((entry) => {
      const root = record(entry);
      const source = nestedRecord(root, ["source", "source_ref"]);
      const run = nestedRecord(root, ["latest_run", "latest_agent_run", "run"]);
      const id = safeId(valueAt(root, ["id", "work_item_id"]));
      const sourceType = label(
        valueAt(root, ["source_type", "sourceType"]) ??
          valueAt(source, ["type", "source_type"]),
      );
      const sourceId = safeId(
        valueAt(root, ["source_id", "sourceId"]) ??
          valueAt(source, ["id", "source_id"]),
      );
      const taskId = safeId(
        valueAt(root, ["task_id"]) ?? valueAt(source, ["task_id"]),
      );
      const personaId = safeId(valueAt(root, ["persona_id"]));
      const actionId = safeId(
        valueAt(root, ["external_action_id", "action_id"]),
      );
      const sourceKind = sourceType.toLowerCase();
      const inferredKind = actionId
        ? "action"
        : taskId || sourceKind.includes("task")
          ? "task"
          : personaId || sourceKind.includes("persona")
            ? "persona"
            : sourceKind.includes("project")
              ? "project"
              : sourceKind;
      const href = hrefFor(
        inferredKind,
        actionId || taskId || personaId || sourceId,
      );
      return {
        id,
        spaceId: safeId(root.space_id),
        projectId: safeId(root.project_id),
        assignedAgentId: safeId(root.assigned_agent_id),
        parentWorkItemId: safeId(root.parent_work_item_id),
        taskId,
        latestRunId: safeId(valueAt(run, ["id", "run_id"]) ?? valueAt(root, ["latest_agent_run_id", "latest_run_id", "active_agent_run_id"])),
        sourceType,
        sourceId,
        domain: label(valueAt(root, ["domain", "work_domain"])),
        space: label(valueAt(root, ["space_name", "space", "space_id"])),
        project: label(
          valueAt(root, ["project_name", "project", "project_id"]),
        ),
        agent: label(
          valueAt(root, [
            "agent_name",
            "agent",
            "assigned_agent_name",
            "assigned_agent_id",
          ]),
        ),
        task: label(valueAt(root, ["task_title", "task_name", "task_id"])),
        persona: label(valueAt(root, ["persona_name", "persona_id"])),
        app: label(valueAt(root, ["app_name", "app_id"])),
        state: label(valueAt(root, ["state", "status"])),
        updatedAt: dateLabel(valueAt(root, ["updated_at", "created_at"])),
        priority: label(valueAt(root, ["priority"])),
        notBefore: dateLabel(valueAt(root, ["not_before", "available_at"])),
        deadline: dateLabel(valueAt(root, ["deadline", "due_at"])),
        leaseHealth: label(
          valueAt(root, ["lease_health", "lease_status", "lease_expires_at"]),
        ),
        attempts: label(valueAt(root, ["attempt_count", "attempts"]), "0"),
        approval: label(valueAt(root, ["approval_state", "approval_status"])),
        budget: label(valueAt(root, ["budget_reservation", "budget"])),
        latestRun: label(
          valueAt(run, ["id", "run_id", "status"]) ??
            valueAt(root, ["latest_agent_run_id", "latest_run_id"]),
        ),
        elapsed: executionDuration(
          numberValue(
            record(root.execution_summary).elapsed_seconds ??
              root.elapsed_seconds ??
              root.elapsed,
          ),
        ),
        phase: label(record(root.execution_summary).phase, "未記録"),
        tool: label(record(root.execution_summary).tool, "未記録"),
        lastActivity: executionTime(
          typeof record(root.execution_summary).last_activity_at === "string"
            ? String(record(root.execution_summary).last_activity_at)
            : null,
        ),
        maxAttempts: label(
          record(root.execution_summary).max_attempts ?? root.max_attempts,
          "?",
        ),
        blocker: label(
          record(root.execution_summary).blocker_code ??
            record(root.execution_summary).safe_error_code ??
            root.blocker_code ??
            root.safe_error_code,
        ),
        attentionReasons: array(record(root.execution_summary).attention_reasons).filter(
          (item): item is string => typeof item === "string",
        ),
        href,
      };
    })
    .filter((item) => item.id || item.sourceId);
}

function normaliseAttention(payload: unknown): CommandAttention[] {
  return array(payload, ["attention", "requires_attention", "items"]).map(
    (entry) => {
      const root = record(entry);
      const kind = label(
        valueAt(root, ["kind", "type", "category"]),
        "attention",
      );
      const id = safeId(
        valueAt(root, ["id", "work_item_id", "action_id", "task_id"]),
      );
      const lowerKind = kind.toLowerCase();
      const href = hrefFor(
        lowerKind.includes("action") || lowerKind.includes("approval")
          ? "action"
          : lowerKind.includes("task")
            ? "task"
            : lowerKind === "work"
              ? "work"
              : "",
        id,
      );
      return {
        id,
        kind,
        projectId: safeId(root.project_id),
        originWorkId: safeId(record(root.approval_summary).work_item_id),
        originAgentId: safeId(record(root.approval_summary).actor),
        currentAssigneeId: safeId(record(root.approval_summary).current_assignee),
        authorizationMode: label(record(root.approval_summary).authorization_mode, "未記録"),
        currentAssignee: label(record(root.approval_summary).current_assignee, "未記録"),
        currentAssigneeName: label(record(root.approval_summary).current_assignee_name, "担当名未取得"),
        actorName: label(record(root.approval_summary).actor_name, "提案者名未取得"),
        workName: label(record(root.approval_summary).work_name, "仕事名未取得"),
        operationLabel: label(record(root.approval_summary).operation_label, "承認内容の詳細未取得"),
        targetKind: label(record(root.approval_summary).target_kind, "対象種別未記録"),
        targetName: label(record(root.approval_summary).target_name, "対象名未取得"),
        actor: label(record(root.approval_summary).actor, "未記録"),
        work: label(record(root.approval_summary).work_item_id, "未記録"),
        operation: label(record(root.approval_summary).operation, "未記録"),
        target: label(record(root.approval_summary).target, "未記録"),
        consequence: label(record(root.approval_summary).consequence, "未記録"),
        decision: label(record(root.approval_summary).decision, "正本で対象・内容・影響を確認"),
        version: label(record(root.approval_summary).action_version, "未記録"),
        title: label(
          valueAt(root, ["title", "name", "summary"]),
          kind || "要対応",
        ),
        detail: label(
          valueAt(root, [
            "detail",
            "reason",
            "message",
            "safe_error_code",
            "error_code",
          ]),
        ),
        state: label(valueAt(root, ["state", "status"])),
        href,
      };
    },
  );
}

function normaliseActivity(payload: unknown): CommandActivity[] {
  return array(payload, ["activity", "activities", "events", "items"]).map((entry) => {
    const root = record(entry);
    const source = nestedRecord(root, ["source", "source_ref"]);
    const id = safeId(valueAt(root, ["id", "event_id"]));
    const conversationSessionId = safeId(
      valueAt(root, ["conversation_session_id", "session_id"]) ??
        valueAt(source, ["conversation_session_id", "session_id"]),
    );
    const workItemId = safeId(
      valueAt(root, ["work_item_id"]) ?? valueAt(source, ["work_item_id"]),
    );
    const assignedAgentId = safeId(
      valueAt(root, ["assigned_agent_id"]) ??
        valueAt(source, ["assigned_agent_id"]),
    );
    const taskId = safeId(valueAt(root, ["task_id"]) ?? valueAt(source, ["task_id"]));
    const actionId = safeId(
      valueAt(root, ["action_id", "external_action_id"]) ??
        valueAt(source, ["action_id", "external_action_id"]),
    );
    // Canonical source links are typed. Never use an arbitrary event/entity ID
    // as an Agent ID: agent_run and work_item IDs are distinct identities.
    const href = conversationSessionId
      ? `/chat?s=${encodeURIComponent(conversationSessionId)}`
      : workItemId
        ? workDetailHref(workItemId)
        : assignedAgentId
          ? hrefFor("agent", assignedAgentId)
          : actionId
            ? hrefFor("action", actionId)
            : taskId
              ? hrefFor("task", taskId)
              : undefined;
    return {
      id,
      kind: label(valueAt(root, ["kind", "entity_type", "type", "source_type"]), "activity"),
      event: label(valueAt(root, ["event", "event_type", "activity_type", "type", "kind"]), "activity"),
      actor: label(valueAt(root, ["actor_name", "actor_label", "actor_id", "actor_type"])),
      domain: label(valueAt(root, ["domain", "source_type", "entity_type"])),
      state: label(valueAt(root, ["state", "status"])),
      createdAt: formatTimestamp(
        safeText(
          valueAt(root, ["created_at", "occurred_at", "started_at", "finished_at", "timestamp"]),
          "",
        ),
      ) || dateLabel(valueAt(root, ["created_at", "occurred_at", "started_at", "finished_at", "timestamp"])),
      detail: [label(valueAt(root, ["detail", "summary", "safe_error_code", "message"]), ""),
        ...["event_id", "work_item_id", "run_id", "action_id", "attempt_id", "receipt_id"].map(key => safeId(root[key]) ? `${key}: ${safeId(root[key])}` : ""),
        root.authorization_mode === "bounded_policy" ? "範囲内ポリシーによる許可" : root.authorization_mode === "human_approval" ? "人間承認" : "",
      ].filter(Boolean).join(" · ") || "—",
      href,
    };
  }).filter((item) => item.id || item.event !== "activity");
}

function normaliseSimpleList(payload: unknown, keys: string[], kind: "project" | "schedule" | "artifact" | "evidence"): Array<{ id: string; name?: string; state?: string; detail?: string; due?: string; kind?: string; createdAt?: string }> {
  return array(payload, keys).map((entry) => {
    const root = record(entry);
    const id = safeId(valueAt(root, ["id", `${kind}_id`, "item_id"]));
    return {
      id,
      name: label(valueAt(root, ["name", "title", "display_name", `${kind}_name`])),
      state: label(valueAt(root, ["state", "status"])),
      detail: label(valueAt(root, ["detail", "summary", "description"])),
      due: dateLabel(valueAt(root, ["due", "due_at", "next_due_at"])),
      kind: label(valueAt(root, ["kind", "type"])),
      createdAt: dateLabel(valueAt(root, ["created_at", "updated_at"])),
    };
  }).filter((item) => item.id || item.name !== "—");
}

function normaliseSpaces(payload: unknown): CommandSpace[] {
  return array(payload, ["spaces", "items"]).map((entry) => {
    const root = record(entry);
    const id = safeId(valueAt(root, ["id", "space_id"]));
    return {
      id,
      name: label(valueAt(root, ["name", "display_name", "title"]), id || "Space"),
      state: label(valueAt(root, ["state", "status"]), ""),
      detail: label(valueAt(root, ["description", "summary", "detail"]), ""),
    };
  }).filter((item) => item.id || item.name !== "Space");
}

function normaliseProjects(payload: unknown): CommandProject[] {
  return array(payload, ["projects", "items"]).map((entry) => {
    const root = record(entry);
    const id = safeId(valueAt(root, ["id", "project_id"]));
    return {
      id,
      name: label(valueAt(root, ["name", "display_name", "title"]), id || "Project"),
      state: label(valueAt(root, ["state", "status"]), ""),
      detail: label(valueAt(root, ["description", "summary", "detail"]), ""),
      spaceId: safeId(valueAt(root, ["space_id"])),
      href: hrefFor("project", id),
    };
  }).filter((item) => item.id || item.name !== "Project");
}

function normalisePage(value: unknown): CommandPage {
  const page = record(value);
  const available = numberValue(page.available);
  const cursor = page.next_cursor;
  return {
    available: available !== null && Number.isSafeInteger(available) && available >= 0 ? available : null,
    hasMore: page.has_more === true,
    nextCursor: typeof cursor === "string" && /^[a-z0-9_-]{1,4096}$/iu.test(cursor) ? cursor : null,
  };
}

function mergeRecords<T extends { id: string }>(previous: T[], incoming: T[]): T[] {
  const records = new Map(previous.map((item) => [item.id, item]));
  for (const item of incoming) records.set(item.id, item);
  return [...records.values()];
}

function normaliseSnapshot(payload: unknown): CommandSnapshot {
  const payloadRoot = record(payload);
  const outer = firstRecord(payload, [
    "command_center",
    "overview",
    "snapshot",
    "data",
  ]);
  const root = outer === payloadRoot ? outer : { ...payloadRoot, ...outer };
  const simple = (keys: string[]) => nestedRecord(root, keys);
  const media = simple(["media_status", "mediaOps", "media_operations"]);
  const usage = simple(["runtime_usage", "usage", "runtime"]);
  const organization = simple(["organization", "organization_summary"]);
  const agents = normaliseAgents(
    valueAt(root, ["agents", "agent_states", "agent_list"]) ?? [],
  );
  const work = normaliseWork(
    valueAt(root, [
      "work",
      "work_items",
      "current_work",
      "current_work_items",
    ]) ?? [],
  );
  for (const agent of agents) {
    const current = agent.currentWorkId
      ? work.find((item) => item.id === agent.currentWorkId)
      : undefined;
    if (current)
      agent.currentWork =
        current.task !== "—"
          ? current.task
          : `${current.sourceType} · ${current.state}`;
  }
  const spaces = normaliseSpaces(valueAt(root, ["spaces", "space_list"]) ?? []);
  const projects = normaliseProjects(
    valueAt(root, ["projects", "project_health"]) ?? [],
  );
  const summary = normaliseSummary(root);
  return {
    organization: {
      name: label(
        valueAt(organization, ["name", "display_name", "company_name"]),
        "Organization",
      ),
      state: label(valueAt(organization, ["state", "status"])),
      detail: label(valueAt(organization, ["detail", "policy_version", "id"])),
    },
    summary,
    agents,
    work,
    agentsPage: normalisePage(root.agents_page),
    workPage: normalisePage(root.work_page),
    activity: normaliseActivity(
      valueAt(root, [
        "activity",
        "activities",
        "events",
        "activity_timeline",
      ]) ?? [],
    ),
    attention: normaliseAttention(
      valueAt(root, ["attention", "requires_attention", "attention_items"]) ??
        [],
    ),
    spaces,
    projects,
    schedules: normaliseSimpleList(
      valueAt(root, ["schedules", "due_work"]) ?? [],
      ["schedules", "due_work", "items"],
      "schedule",
    ).map((item) => ({
      id: item.id,
      name: item.name ?? "—",
      due: item.due ?? "—",
      state: item.state ?? "—",
    })),
    artifacts: normaliseSimpleList(
      valueAt(root, ["artifacts", "recent_artifacts"]) ?? [],
      ["artifacts", "recent_artifacts", "items"],
      "artifact",
    ).map((item) => ({
      id: item.id,
      name: item.name ?? "—",
      kind: item.kind ?? "—",
      createdAt: item.createdAt ?? "—",
    })),
    evidence: normaliseSimpleList(
      valueAt(root, ["evidence", "recent_evidence"]) ?? [],
      ["evidence", "recent_evidence", "items"],
      "evidence",
    ).map((item) => ({
      id: item.id,
      kind: item.kind ?? "—",
      detail: item.detail ?? "—",
      createdAt: item.createdAt ?? "—",
    })),
    mediaStatus: Object.entries(media)
      .filter(([key]) => !SECRET_KEY.test(key))
      .slice(0, 12)
      .map(([key, value]) => ({
        label: safeText(key, "status", 60),
        value: safeCompactValue(value, ["value", "status", "count"]),
        detail:
          typeof value === "object"
            ? safeText(valueAt(record(value), ["detail", "summary"]))
            : "",
      })),
    runtimeUsage: Object.entries(usage)
      .filter(([key]) => !SECRET_KEY.test(key))
      .slice(0, 12)
      .map(([key, value]) => ({
        label: safeText(key, "usage", 60),
        value: safeCompactValue(value, [
          "value",
          "used",
          "count",
          "running",
          "claimed",
        ]),
        detail:
          typeof value === "object"
            ? safeText(valueAt(record(value), ["detail", "limit", "window"]))
            : "",
      })),
  };
}

async function fetchJson(path: string, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(`/api/python-proxy${path}`, {
    cache: "no-store",
    credentials: "include",
    headers: { accept: "application/json" },
    signal,
  });
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  if (response.ok === false) {
    const detail = record(body).detail;
    const error = new Error(typeof detail === "string" ? detail : `Command Center API failed (${response.status})`);
    Object.assign(error, { status: response.status });
    throw error;
  }
  return body;
}

function filtersFromSearchParams(searchParams: SearchParamReader): CommandFilters {
  const get = (key: string): string => searchParams?.get(key)?.trim() ?? "";
  const scope = get("scope");
  return {
    scope: scope === "space" || scope === "project" || scope === "agent" ? scope : "all",
    spaceId: get("space_id"),
    projectId: get("project_id"),
    agentId: get("agent_id") || get("agent"),
    domain: get("domain"),
    state: get("state"),
    range: get("range") || get("time_range"),
  };
}

function queryString(searchParams: SearchParamReader & { toString?: () => string }): string {
  if (!searchParams || typeof searchParams.toString !== "function") return "";
  const value = searchParams.toString();
  return value === "[object Object]" ? "" : value;
}

function filterQuery(filters: CommandFilters): string {
  const params = new URLSearchParams();
  if (filters.scope !== "all") params.set("scope", filters.scope);
  if (filters.spaceId) params.set("space_id", filters.spaceId);
  if (filters.projectId) params.set("project_id", filters.projectId);
  if (filters.agentId) params.set("agent_id", filters.agentId);
  if (filters.domain) params.set("domain", filters.domain);
  if (filters.state) params.set("state", filters.state);
  if (filters.range) params.set("range", filters.range);
  return params.toString();
}

function queryWithTimeRange(filters: CommandFilters): string {
  const params = new URLSearchParams(filterQuery(filters));
  const ageHours = filters.range === "24h" ? 24 : filters.range === "7d" ? 24 * 7 : filters.range === "30d" ? 24 * 30 : 0;
  if (ageHours > 0) {
    params.set("time_range", filters.range);
    params.set("from", new Date(Date.now() - ageHours * 60 * 60 * 1000).toISOString());
  }
  return params.toString();
}

function statusClass(status: string): string {
  const value = status.toLowerCase();
  if (value.includes("fail") || value.includes("block") || value.includes("stale")) return "text-destructive";
  if (value.includes("uncertain") || value.includes("approval") || value.includes("await")) return "text-amber-700 dark:text-amber-300";
  if (value.includes("success") || value.includes("active") || value.includes("working") || value.includes("running")) return "text-emerald-700 dark:text-emerald-300";
  return "text-muted-foreground";
}

function Metric({ label: metricLabel, value, tone = "default" }: { label: string; value: string | number; tone?: "default" | "good" | "warn" | "bad" }) {
  return (
    <div className="rounded-lg border border-border/70 bg-background/40 p-3" data-testid={`operations-metric-${metricLabel.toLowerCase().replace(/[^a-z0-9]+/gu, "-")}`}>
      <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{metricLabel}</p>
      <p className={cn("mt-1 text-2xl font-semibold tracking-tight", tone === "good" && "text-emerald-700 dark:text-emerald-300", tone === "warn" && "text-amber-700 dark:text-amber-300", tone === "bad" && "text-destructive")}>{value}</p>
    </div>
  );
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="rounded-md border border-dashed border-border/70 px-3 py-6 text-center text-sm text-muted-foreground">{children}</div>;
}

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">最新の状況を読み込めませんでした。「更新」で再試行してください。解消しない場合は権限またはサーバーの設定を確認してください。</div>;
}

function FilterBar({
  filters,
  onChange,
  compact = false,
}: {
  filters: CommandFilters;
  onChange: (key: keyof CommandFilters, value: string) => void;
  compact?: boolean;
}) {
  return (
    <details
      className={cn("rounded-lg border border-border/70 bg-card", compact && "relative")}
      data-testid="operations-command-center-filters"
      open={!compact && Boolean(filterQuery(filters)) || undefined}
    >
      <summary className={cn("cursor-pointer text-muted-foreground", compact ? "px-2.5 py-1.5 text-xs" : "px-4 py-3 text-sm")}>
        <Filter className="mr-2 inline size-3.5" />
        表示を絞り込む
        {filterQuery(filters) ? "（絞り込み中）" : compact ? "" : " · すべての範囲"}
      </summary>
      <div className={cn("flex flex-wrap items-end gap-2 border-t border-border/60 p-4", compact && "absolute right-0 top-full z-30 mt-1 w-[min(38rem,calc(100vw-2rem))] rounded-lg border bg-card shadow-lg")}>
        <div className="space-y-1 text-xs">
          <div className="inline-flex items-center gap-1 text-muted-foreground">
            <label htmlFor="operations-command-center-scope">範囲</label>
            {filters.scope === "space" ? (
              <HelpPopover label="Space範囲">
                Workの記録上のSpaceで絞ります。閲覧可能なProject未設定Workも含みます。
              </HelpPopover>
            ) : null}
            {filters.scope === "project" ? (
              <HelpPopover label="Project範囲">
                指定ProjectのWorkが対象です。Project未設定Workは含みません。
              </HelpPopover>
            ) : null}
          </div>
          <AppSelect
            id="operations-command-center-scope"
            aria-label="Scope"
            className="h-8 min-w-36 rounded-md border border-input bg-card px-2 text-xs"
            value={filters.scope}
            onChange={(event) => onChange("scope", event.target.value)}
          >
            <option value="all">すべて</option>
            <option value="space">Space</option>
            <option value="project">Project</option>
            <option value="agent">Agent</option>
          </AppSelect>
        </div>
        {filters.scope === "space" ? (
          <label className="space-y-1 text-xs">
            <span className="block text-muted-foreground">Space ID</span>
            <Input
              aria-label="Space ID"
              className="h-8 w-44 text-xs"
              value={filters.spaceId}
              onChange={(event) => onChange("spaceId", event.target.value)}
              placeholder="space-id"
            />
          </label>
        ) : null}
        {filters.scope === "project" ? (
          <label className="space-y-1 text-xs">
            <span className="block text-muted-foreground">Project ID</span>
            <Input
              aria-label="Project ID"
              className="h-8 w-44 text-xs"
              value={filters.projectId}
              onChange={(event) => onChange("projectId", event.target.value)}
              placeholder="project-id"
            />
          </label>
        ) : null}
        {filters.scope === "agent" ? (
          <label className="space-y-1 text-xs">
            <span className="block text-muted-foreground">Agent ID</span>
            <Input
              aria-label="Agent ID"
              className="h-8 w-44 text-xs"
              value={filters.agentId}
              onChange={(event) => onChange("agentId", event.target.value)}
              placeholder="agent-id"
            />
          </label>
        ) : null}
        <label className="space-y-1 text-xs">
          <span className="block text-muted-foreground">分野</span>
          <AppSelect
            aria-label="Domain"
            className="h-8 min-w-40 rounded-md border border-input bg-card px-2 text-xs"
            value={filters.domain}
            onChange={(event) => onChange("domain", event.target.value)}
          >
            <option value="">すべての分野</option>
            <option value="internal">Internal / company</option>
            <option value="media">MediaOps</option>
            <option value="code">Code agent</option>
            <option value="apps">Apps</option>
          </AppSelect>
        </label>
        <label className="space-y-1 text-xs">
          <span className="block text-muted-foreground">状態</span>
          <AppSelect
            aria-label="State"
            className="h-8 min-w-40 rounded-md border border-input bg-card px-2 text-xs"
            value={filters.state}
            onChange={(event) => onChange("state", event.target.value)}
          >
            <option value="">すべての状態</option>
            <option value="running">Working</option>
            <option value="claimed">Claimed</option>
            <option value="blocked">Blocked</option>
            <option value="awaiting_approval">Awaiting approval</option>
            <option value="uncertain">Uncertain</option>
            <option value="failed">Failed</option>
            <option value="dead_letter">Dead letter</option>
            <option value="stale">Stale</option>
          </AppSelect>
        </label>
        <label className="space-y-1 text-xs">
          <span className="block text-muted-foreground">期間</span>
          <AppSelect
            aria-label="Time range"
            className="h-8 min-w-32 rounded-md border border-input bg-card px-2 text-xs"
            value={filters.range}
            onChange={(event) => onChange("range", event.target.value)}
          >
            <option value="">すべての期間</option>
            <option value="24h">Last 24 hours</option>
            <option value="7d">Last 7 days</option>
            <option value="30d">Last 30 days</option>
          </AppSelect>
        </label>
      </div>
    </details>
  );
}

const WORK_STATE_LABELS: Record<string, string> = {
  pending: "待機中",
  claimed: "着手中",
  running: "作業中",
  retry_wait: "再試行待ち",
  blocked: "対応待ち",
  awaiting_approval: "承認待ち",
  uncertain: "結果の確認が必要",
  succeeded: "完了",
  failed: "失敗・要確認",
  cancelled: "取消済み",
  dead_letter: "再試行上限",
  stale: "実行状況の確認が必要",
};

function workStateLabel(state: string): string {
  return WORK_STATE_LABELS[state] ?? state;
}

// Failed/uncertain work stays actionable until it is resolved, rather than
// being counted as completed. Never infer progress from elapsed time/attempts.
function isFinishedWork(item: CommandWork): boolean {
  return (
    (item.state === "succeeded" || item.state === "cancelled") &&
    !item.attentionReasons.some((reason) =>
      ["ready_for_review", "human_action_required", "verification_failed"].includes(
        reason,
      ),
    )
  );
}

function workTitle(item: CommandWork): string {
  return item.task !== "—"
    ? item.task
    : item.persona !== "—"
      ? item.persona
      : item.app !== "—"
        ? item.app
        : `${item.sourceType !== "—" ? item.sourceType : "仕事"} · ${item.sourceId || item.id}`;
}

function workDetailHref(workId: string, scopeQuery = ""): string {
  const query = new URLSearchParams(scopeQuery);
  for (const key of ["graph_node", "graph_work", "execution_focus", "execution_node"]) query.delete(key);
  query.set("tab", "work");
  query.set("work_id", workId);
  return `/operations?${query.toString()}`;
}

function visibleWorkItems(snapshot: CommandSnapshot, workFilter: string): CommandWork[] {
  return snapshot.work.filter((item) =>
    workFilter === "all" ||
    (workFilter === "completed" ? isFinishedWork(item) : !isFinishedWork(item)),
  );
}

function isActionAttention(item: CommandAttention): boolean {
  return /action|approval/iu.test(item.kind);
}

function isHumanApproval(item: CommandAttention): boolean {
  return isActionAttention(item) &&
    ["proposed", "awaiting_approval"].includes(item.state) &&
    item.authorizationMode !== "bounded_policy";
}

/**
 * Project only recorded relationships. Names are presentation labels, never
 * join keys. Missing endpoints remain disconnected rather than
 * acquiring a synthetic parent, assignee, run, or authorization.
 */
function buildOperationsMap(snapshot: CommandSnapshot): {
  nodes: OperationsMapNode[];
  edges: OperationsMapEdge[];
} {
  const nodes: OperationsMapNode[] = [];
  const edges: OperationsMapEdge[] = [];
  // Keep every fetched entity and recorded relation in the model. Display
  // filters and staged rendering belong to the Canvas projection alone.
  const fetchedWork = snapshot.work;
  const allWorkFetched = !snapshot.workPage.hasMore && snapshot.workPage.available !== null &&
    snapshot.workPage.available === snapshot.work.length;
  const workByAgent = new Map<string, CommandWork[]>();
  for (const work of snapshot.work) {
    if (!work.assignedAgentId) continue;
    const assigned = workByAgent.get(work.assignedAgentId) || [];
    assigned.push(work);
    workByAgent.set(work.assignedAgentId, assigned);
  }
  const assignedStates = (work: CommandWork[]) => {
    const states = new Map<string, number>();
    for (const item of work) states.set(item.state, (states.get(item.state) || 0) + 1);
    return [...states].map(([state, total]) => `${workStateLabel(state)} ${total}件`).join(" / ") || "なし";
  };
  const actions = snapshot.attention.filter(isActionAttention);
  const relatedProjectIds = new Set([
    ...fetchedWork.map((work) => work.projectId),
    ...snapshot.agents.flatMap((agent) => agent.projectGrantIds),
    ...actions.map((action) => action.projectId),
  ].filter(Boolean));
  const relatedSpaceIds = new Set([
    ...fetchedWork.map((work) => work.spaceId),
    ...snapshot.agents.flatMap((agent) => [agent.primarySpaceId, ...agent.spaceAssignmentIds]),
    ...snapshot.projects.filter((project) => relatedProjectIds.has(project.id)).map((project) => project.spaceId),
  ].filter(Boolean));
  const key = (kind: OperationsMapNode["kind"], id: string) => `${kind}:${id}`;
  const addNode = (node: OperationsMapNode) => {
    if (node.entityId && !nodes.some((existing) => existing.id === node.id)) nodes.push(node);
  };
  for (const space of snapshot.spaces.filter((item) => relatedSpaceIds.has(item.id))) addNode({
    id: key("space", space.id), kind: "space", entityId: space.id,
    label: space.name, state: space.state, detail: space.detail,
  });
  for (const project of snapshot.projects.filter((item) => relatedProjectIds.has(item.id))) addNode({
    id: key("project", project.id), kind: "project", entityId: project.id,
    label: project.name, state: project.state, detail: project.detail, href: project.href,
    metadata: [{ label: "Space ID", value: project.spaceId || "未記録" }],
  });
  const managerNote = (agent: CommandAgent) => {
    const manager = agent.managerRelation === "agent" ? snapshot.agents.find(item => item.id === agent.managerAgentId) : undefined;
    if (manager) return `上司 ${manager.name}`;
    if (agent.managerRelation === "human") return "上司は人間（Agent階層の対象外）";
    if (agent.managerRelation === "outside_scope" || agent.managerRelation === "agent") return "上司Agentは取得範囲外";
    return agent.managerRelation === "unrecorded" ? "上司関係は未記録" : "上司関係を取得できていません";
  };
  for (const agent of snapshot.agents) addNode({
    id: key("agent", agent.id), kind: "agent", entityId: agent.id,
    label: agent.name, state: agent.state,
    note: managerNote(agent),
    detail: agent.jobTitle !== "—" ? agent.jobTitle : agent.responsibility || "役割未設定",
    href: hrefFor("agent", agent.id),
    workId: fetchedWork.some((work) => work.id === agent.currentWorkId) ? agent.currentWorkId : undefined,
    metadata: [
      { label: "上司関係", value: managerNote(agent) },
      { label: "取得済みの担当Work", value: String(workByAgent.get(agent.id)?.length || 0) },
      { label: "全対象の担当Work", value: allWorkFetched ? String(workByAgent.get(agent.id)?.length || 0) : "未取得（全ページ取得後に確定）" },
      { label: "担当Workの状態（取得済み）", value: assignedStates(workByAgent.get(agent.id) || []) },
      { label: "役割", value: agent.jobTitle },
      { label: "担当領域", value: agent.responsibility || "未記録" },
      { label: "現在の仕事", value: agent.currentWork },
      { label: "Space ID", value: agent.primarySpaceId || "未記録" },
      { label: "Project IDs", value: agent.projectGrantIds.join(", ") || "未記録" },
    ],
  });
  for (const work of fetchedWork) addNode({
    id: key("work", work.id), kind: "work", entityId: work.id,
    label: workTitle(work), state: work.state, finished: isFinishedWork(work),
    detail: work.agent === "—" ? "担当未記録" : `担当 ${work.agent}`,
    note: !work.assignedAgentId ? "担当Agentは未記録" : !snapshot.agents.some(agent => agent.id === work.assignedAgentId) ? "担当Agentは取得範囲外" : undefined,
    href: work.href, workId: work.id,
    metadata: [
      { label: "委任元Work", value: !work.parentWorkItemId ? "未記録" : fetchedWork.some(item => item.id === work.parentWorkItemId) ? work.parentWorkItemId : "取得範囲外" },
      { label: "現在の担当", value: work.agent },
      { label: "担当Agent ID", value: work.assignedAgentId || "未記録" },
      { label: "Project", value: work.project },
      { label: "Project ID", value: work.projectId || "未記録" },
      { label: "Space ID", value: work.spaceId || "未記録" },
      { label: "工程", value: work.phase },
      { label: "Tool", value: work.tool },
      { label: "経過", value: work.elapsed },
      { label: "最終活動", value: work.lastActivity },
      { label: "実行回数", value: `${work.attempts}/${work.maxAttempts}` },
      { label: "最新Run ID", value: work.latestRunId || "未記録" },
      { label: "確認が必要", value: work.attentionReasons.map(executionAttentionLabel).join(" / ") || "—" },
      { label: "停止理由", value: work.blocker },
    ],
  });
  for (const action of actions) addNode({
    id: key("action", action.id), kind: "action", entityId: action.id,
    label: [
      action.operationLabel && action.operationLabel !== "承認内容の詳細未取得"
        ? action.operationLabel : "外部操作",
      action.targetName && action.targetName !== "対象名未取得"
        ? action.targetName : action.workName && action.workName !== "仕事名未取得"
          ? action.workName : "",
    ].filter(Boolean).join(" · "),
    state: action.state,
    detail: action.targetName && action.targetName !== "対象名未取得"
      ? action.targetName : action.workName && action.workName !== "仕事名未取得"
        ? action.workName : action.consequence,
    href: action.href,
    workId: fetchedWork.some((work) => work.id === action.originWorkId) ? action.originWorkId : undefined,
    metadata: [
      { label: "提案者", value: action.actorName || "未記録" },
      { label: "提案者Agent ID", value: action.originAgentId || "未記録" },
      { label: "元の仕事", value: action.workName || "未記録" },
      { label: "元の仕事ID", value: action.originWorkId || "未記録" },
      { label: "現在の仕事担当", value: action.currentAssigneeName || "未記録" },
      { label: "現在の担当Agent ID", value: action.currentAssigneeId || "未記録" },
      { label: "対象", value: `${action.targetKind || "未記録"} · ${action.targetName || "未記録"}` },
      { label: "対象ID", value: action.target || "未記録" },
      { label: "外部への影響", value: action.consequence || "未記録" },
      { label: "判断すること", value: action.decision || "正本で確認" },
      { label: "承認方式", value: action.authorizationMode === "bounded_policy" ? "範囲内ポリシーによる許可" : action.authorizationMode === "human_approval" ? "人間承認" : "未記録" },
      { label: "版", value: action.version || "未記録" },
    ],
  });
  const nodeIds = new Set(nodes.map((node) => node.id));
  const connect = (
    sourceKind: OperationsMapNode["kind"], sourceId: string,
    targetKind: OperationsMapNode["kind"], targetId: string,
    relation: OperationsMapEdge["relation"], edgeLabel: string,
  ) => {
    if (!sourceId || !targetId) return;
    const source = key(sourceKind, sourceId);
    const target = key(targetKind, targetId);
    if (source === target || !nodeIds.has(source) || !nodeIds.has(target)) return;
    const id = `${relation}:${source}:${target}`;
    if (!edges.some((edge) => edge.id === id)) edges.push({ id, source, target, relation, label: edgeLabel });
  };
  for (const project of snapshot.projects) connect("space", project.spaceId, "project", project.id, "membership", "所属");
  for (const agent of snapshot.agents) {
    for (const spaceId of new Set([agent.primarySpaceId, ...agent.spaceAssignmentIds])) {
      connect("space", spaceId, "agent", agent.id, "membership", "所属");
    }
    for (const projectId of agent.projectGrantIds) connect("project", projectId, "agent", agent.id, "membership", "参加");
    if (agent.managerRelation === "agent") connect("agent", agent.managerAgentId, "agent", agent.id, "supervision", "上司 → Agent");
  }
  for (const work of fetchedWork) {
    connect("agent", work.assignedAgentId, "work", work.id, "assignment", "担当");
    connect("project", work.projectId, "work", work.id, "membership", "仕事");
    if (!snapshot.projects.some((project) => project.id === work.projectId && project.spaceId === work.spaceId)) {
      connect("space", work.spaceId, "work", work.id, "membership", "仕事");
    }
    connect("work", work.parentWorkItemId, "work", work.id, "parent", "仕事の委任");
  }
  for (const action of actions) {
    connect("work", action.originWorkId, "action", action.id, "approval", "外部操作");
    connect("agent", action.originAgentId, "action", action.id, "approval", "提案者");
    // The action's origin actor is historical. It must never be replaced by
    // the current assignee of its work.
    if (!nodeIds.has(key("work", action.originWorkId))) {
      connect("project", action.projectId, "action", action.id, "membership", "外部操作");
    }
  }
  return { nodes, edges };
}

function SupportingList({ snapshot }: { snapshot: CommandSnapshot }) {
  return <div className="grid gap-4 xl:grid-cols-2"><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Organization summary</CardTitle></CardHeader><CardContent className="space-y-1 pt-3 text-xs"><p className="font-medium">{snapshot.organization.name}</p><p className={statusClass(snapshot.organization.state)}>{snapshot.organization.state}</p><p className="text-muted-foreground">{snapshot.organization.detail}</p></CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Project health</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.projects.length ? snapshot.projects.slice(0, 8).map((project) => <div key={project.id} className="flex items-center justify-between gap-2 rounded-md border border-border/60 p-2 text-xs">{project.href ? <Link href={project.href} className="min-w-0 truncate font-medium hover:underline">{project.name}</Link> : <span className="min-w-0 truncate font-medium">{project.name}</span>}<span className={cn("shrink-0", statusClass(project.state))}>{project.state}</span></div>) : <EmptyState>Project healthはありません。</EmptyState>}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><CalendarClock className="size-4 text-primary" /> Schedules / due work</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.schedules.length ? snapshot.schedules.slice(0, 8).map((schedule) => <div key={schedule.id} className="flex items-center justify-between gap-2 rounded-md border border-border/60 p-2 text-xs"><span className="min-w-0 truncate font-medium">{schedule.name}</span><span className="shrink-0 text-muted-foreground">{schedule.due}</span></div>) : <EmptyState>予定・期限のprojectionはありません。</EmptyState>}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Recent artifacts</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.artifacts.length ? snapshot.artifacts.slice(0, 8).map((item) => <div key={item.id} className="rounded-md border border-border/60 p-2 text-xs"><span className="font-medium">{item.name}</span><span className="ml-2 text-muted-foreground">{item.kind} · {item.createdAt}</span></div>) : <EmptyState>Artifactはありません。</EmptyState>}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Recent evidence</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.evidence.length ? snapshot.evidence.slice(0, 8).map((item) => <div key={item.id} className="rounded-md border border-border/60 p-2 text-xs"><span className="font-medium">{item.kind}</span><span className="ml-2 text-muted-foreground">{item.detail} · {item.createdAt}</span></div>) : <EmptyState>Evidenceはありません。</EmptyState>}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">MediaOps status</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.mediaStatus.length ? snapshot.mediaStatus.map((item) => <div key={item.label} className="flex items-center justify-between gap-2 text-xs"><span>{item.label}</span><span className="text-muted-foreground">{item.value}{item.detail ? ` · ${item.detail}` : ""}</span></div>) : <EmptyState>MediaOps statusはありません。</EmptyState>}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Runtime usage</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.runtimeUsage.length ? snapshot.runtimeUsage.map((item) => <div key={item.label} className="flex items-center justify-between gap-2 text-xs"><span>{item.label}</span><span className="text-muted-foreground">{item.value}{item.detail ? ` · ${item.detail}` : ""}</span></div>) : <EmptyState>Runtime usageはありません。</EmptyState>}</CardContent></Card></div>;
}

function RecordPagination({
  snapshot, loading, error, onLoadMore, matchingWork,
}: {
  snapshot: CommandSnapshot;
  loading: boolean;
  error: unknown;
  onLoadMore: () => void;
  matchingWork?: number;
}) {
  const hasMore = snapshot.workPage.hasMore || snapshot.agentsPage.hasMore;
  const hasCursor = (!snapshot.workPage.hasMore || Boolean(snapshot.workPage.nextCursor)) &&
    (!snapshot.agentsPage.hasMore || Boolean(snapshot.agentsPage.nextCursor));
  const count = (loaded: number, page: CommandPage) => page.available === null
    ? `取得済み ${loaded}件（全対象件数は未取得）`
    : `取得済み ${loaded} / 取得可能 ${page.available}件`;
  return <div className="shrink-0 border-t border-border px-3 py-2 text-[11px] text-muted-foreground" data-testid="operations-record-pagination">
    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
      <p aria-live="polite">
        Agent: {count(snapshot.agents.length, snapshot.agentsPage)} · 仕事: {count(snapshot.work.length, snapshot.workPage)}
        {matchingWork !== undefined ? ` · 条件に一致 ${matchingWork}件` : ""}
      </p>
      {hasMore ? <Button type="button" variant="outline" size="sm" className="ml-auto" disabled={loading || !hasCursor} onClick={onLoadMore}>
        {loading ? <Loader2 className="size-3 animate-spin" /> : null}続きを取得
      </Button> : null}
    </div>
    <p>この条件・権限内の取得時点の件数です。外部操作 {snapshot.attention.filter(isActionAttention).length}件取得。</p>
    {error ? <p role="alert" className="mt-1 text-destructive">続きを取得できませんでした。表示中の記録は保持しています。「続きを取得」で再試行してください。</p> : null}
    {hasMore && !hasCursor ? <p role="alert">追加取得の情報がありません。「更新」で取り直してください。</p> : null}
  </div>;
}

function OverviewView({
  snapshot,
  workFilter,
  onWorkFilterChange,
  scopeQuery,
  paginationLoading,
  paginationError,
  onLoadMore,
  deepLinkSearchStatus,
}: {
  snapshot: CommandSnapshot;
  workFilter: CommandWorkFilter;
  onWorkFilterChange: (value: string) => void;
  scopeQuery: string;
  paginationLoading: boolean;
  paginationError: unknown;
  onLoadMore: () => void;
  deepLinkSearchStatus: DeepLinkSearchStatus;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams();
  const graph = useMemo(() => buildOperationsMap(snapshot), [snapshot]);
  const visibleWork = visibleWorkItems(snapshot, workFilter);
  const requestedNodeId = search?.get("graph_node") || "";
  const selectedNodeId = graph.nodes.some((node) => node.id === requestedNodeId) ? requestedNodeId : null;
  const requestedWorkId = safeId(search?.get("graph_work"));
  // A URL is a focus request, not permission to retrieve a different scope.
  const selectedWork = snapshot.work.find((work) => work.id === requestedWorkId);
  const workPage: OperationsMapPage = { fetched: snapshot.work.length, available: snapshot.workPage.available, hasMore: snapshot.workPage.hasMore };
  const agentPage: OperationsMapPage = { fetched: snapshot.agents.length, available: snapshot.agentsPage.available, hasMore: snapshot.agentsPage.hasMore };
  const revealNodeIds = useMemo(() => [...new Set([
    ...(selectedNodeId ? [selectedNodeId] : []),
    ...(selectedWork ? [`work:${selectedWork.id}`] : []),
  ])], [selectedNodeId, selectedWork]);
  const overviewRef = useRef<HTMLDivElement>(null);
  const executionCloseRef = useRef<HTMLButtonElement>(null);
  const selectNode = (id: string | null) => {
    if (id && !graph.nodes.some((node) => node.id === id)) return;
    const next = new URLSearchParams(scopeQuery);
    if (id) next.set("graph_node", id); else next.delete("graph_node");
    for (const key of ["graph_work", "execution_focus", "execution_node"]) next.delete(key);
    next.set("tab", "overview");
    next.set("overview_view", "map");
    router.push(`${pathname || "/operations"}${next.size ? `?${next}` : ""}`, { scroll: false });
  };
  const selectWork = (id: string) => {
    if (!snapshot.work.some((work) => work.id === id)) return;
    const next = new URLSearchParams(scopeQuery);
    next.set("graph_work", id);
    if (!selectedNodeId) next.set("graph_node", `work:${id}`);
    next.delete("execution_focus");
    next.delete("execution_node");
    next.set("tab", "overview");
    next.set("overview_view", "map");
    router.push(`${pathname || "/operations"}?${next}`, { scroll: false });
  };
  const closeExecution = () => {
    const next = new URLSearchParams(scopeQuery);
    for (const key of ["graph_work", "execution_focus", "execution_node"]) next.delete(key);
    next.set("tab", "overview");
    next.set("overview_view", "map");
    router.push(`${pathname || "/operations"}${next.size ? `?${next}` : ""}`, { scroll: false });
  };
  const approvals = snapshot.attention.filter(isHumanApproval);
  const attentionWork = snapshot.work.filter((work) =>
    work.attentionReasons.length ||
    ["blocked", "uncertain", "retry_wait", "dead_letter", "awaiting_approval", "failed"].includes(work.state) ||
    (["running", "claimed"].includes(work.state) && work.leaseHealth === "expired"),
  );
  const attentionActions = snapshot.attention.filter((item) =>
    isHumanApproval(item) || (isActionAttention(item) && ["uncertain", "failed"].includes(item.state)),
  );
  const attentionNodes = new Set([
    ...attentionWork.map((work) => `work:${work.id}`),
    ...attentionActions.map((action) => `action:${action.id}`),
  ]);
  const attentionChoices = graph.nodes.filter((node) => attentionNodes.has(node.id));
  const unfinished = snapshot.work.filter((work) => !isFinishedWork(work));
  const approvalWork = snapshot.work.filter((work) => work.state === "awaiting_approval");
  const listQuery = new URLSearchParams(filterQuery(filtersFromSearchParams(new URLSearchParams(scopeQuery))));
  listQuery.set("tab", "work");
  const summary = snapshot.summary;
  const budget = summary.budgetPercent === null ? "未設定" : `${summary.budgetPercent.toFixed(0)}%`;

  return (
    <div ref={overviewRef} tabIndex={-1} className="relative flex min-h-0 min-w-0 flex-1 flex-col outline-none" data-testid="operations-command-center-overview">
      {/* Shell navigation remains available while the covered overview is inert. */}
      <div inert={Boolean(selectedWork)} className="relative flex min-h-0 flex-1 flex-col" data-testid="operations-overview-background">
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-border/70 bg-card px-3 py-2">
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs" data-testid="operations-attention-strip">
            <span className="text-[10px] text-muted-foreground">取得済み</span>
            <span className={cn("inline-flex items-center gap-1.5 font-medium", attentionNodes.size ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground")}>
              <AlertTriangle className="size-3.5" aria-hidden="true" />
              要確認 {attentionNodes.size}件
            </span>
            <span className="text-muted-foreground" data-testid="operations-unfinished-count">未完了 {unfinished.length}件</span>
            <Link href="/operations?tab=actions" className="text-primary hover:underline" aria-label="全ての承認">
              承認待ち {approvals.length}操作 / {approvalWork.length}仕事
            </Link>
            {attentionChoices.slice(0, 2).map((node) => (
              <button key={node.id} type="button" onClick={() => selectNode(node.id)} className="max-w-44 truncate text-left text-muted-foreground hover:text-foreground" title={`${node.label} · ${workStateLabel(node.state)}`}>
                {node.label}
              </button>
            ))}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <div className="flex gap-0.5 rounded-md bg-muted p-0.5" role="group" aria-label="仕事の表示">
              {[["open", "未完了"], ["all", "すべて"], ["completed", "完了履歴"]].map(([value, title]) => (
                <button key={value} type="button" aria-pressed={workFilter === value} onClick={() => onWorkFilterChange(value)} className={cn("rounded px-2 py-1 text-xs transition-colors", workFilter === value ? "bg-card font-medium text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground")}>{title}</button>
              ))}
            </div>
            <Link href={`/operations?${listQuery}`} className="text-xs text-primary hover:underline">仕事一覧</Link>
          </div>
        </div>
        {(requestedWorkId && !selectedWork) || (requestedNodeId && !selectedNodeId) ? (
          <p role="status" className="px-1 text-xs text-amber-700 dark:text-amber-300">
            {deepLinkSearchStatus === "searching"
              ? "選択された記録を後続ページで探しています。見つかるまでこの条件のページを取得します。"
              : deepLinkSearchStatus === "stopped"
                ? "選択された記録の自動取得を停止しました。「続きを取得」で再試行するか、「更新」で取り直してください。"
                : deepLinkSearchStatus === "missing"
                  ? "選択した項目はこの条件・権限内の全ページを取得しても見つかりませんでした。"
                  : "選択した項目はこの条件の取得済み一覧には含まれていません。表示中のノードを選択してください。"}
          </p>
        ) : null}
        <section id="operations-overview-map" className="flex min-h-0 flex-1 flex-col" aria-label="Agent・仕事・外部操作の関係図" data-testid="operations-overview-map">
          <OperationsMapCanvas
            nodes={graph.nodes}
            edges={graph.edges}
            selectedNodeId={selectedNodeId}
            workPage={workPage}
            agentPage={agentPage}
            workFilter={workFilter}
            revealNodeIds={revealNodeIds}
            onSelectNode={selectNode}
            onSelectWork={selectWork}
            executionOpen={Boolean(selectedWork)}
          />
        </section>
        <RecordPagination snapshot={snapshot} matchingWork={visibleWork.length} loading={paginationLoading} error={paginationError} onLoadMore={onLoadMore} />
        <details className="absolute bottom-10 left-3 z-10 max-h-[70%] max-w-[calc(100%-1.5rem)] overflow-auto rounded-lg border border-border bg-card">
          <summary className="cursor-pointer px-3 py-2 text-xs font-medium">取得済みの稼働状況・関連する記録</summary>
          <div className="space-y-4 border-t border-border p-4">
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              <Metric label="Active Agents" value={summary.activeAgents} tone="good" />
              <Metric label="実行中のWork" value={summary.working} tone="good" />
              <Metric label="稼働WorkなしのAgent" value={summary.idle} />
              <Metric label="Blocked" value={summary.blocked} tone="bad" />
              <Metric label="Awaiting Approval" value={summary.awaitingApproval} tone="warn" />
              <Metric label="Uncertain" value={summary.uncertain} tone="warn" />
              <Metric label="Failed/Stale" value={summary.failedStale} tone="bad" />
              <Metric label="Budget usage" value={budget} />
            </div>
            <SupportingList snapshot={snapshot} />
          </div>
        </details>
      </div>
      <Dialog
        open={Boolean(selectedWork)}
        modal={false}
        disablePointerDismissal
        onOpenChange={(open, details) => {
          if (open) return;
          if (details.reason === "escape-key" && (details.event.defaultPrevented || details.event.cancelBubble)) {
            details.cancel();
            return;
          }
          closeExecution();
        }}
      >
        <DialogPortal container={overviewRef}>
          {selectedWork ? (
            <DialogPrimitive.Popup
              id="operations-execution-workspace"
              aria-modal={false}
              initialFocus={executionCloseRef}
              finalFocus={() => {
                const root = overviewRef.current;
                const dismiss = root?.querySelector<HTMLElement>('[data-testid="operations-map-inspector-dismiss"]');
                if (root?.querySelector('.operations-map-main[inert]') && dismiss && !dismiss.closest('[inert], [hidden]')) return dismiss;
                return root?.querySelector<HTMLElement>(`.react-flow__node[data-id="${selectedNodeId || `work:${selectedWork.id}`}"]`) || root || false;
              }}
              className="absolute inset-0 z-30 overflow-auto space-y-3 bg-card p-4 outline-none"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <DialogTitle className="min-w-0 break-words text-sm font-semibold">
                  <span className="sr-only">選択した仕事の実行詳細: </span>{workTitle(selectedWork)}
                </DialogTitle>
                <DialogClose ref={executionCloseRef} className="text-xs text-primary hover:underline">実行詳細を閉じる</DialogClose>
              </div>
              <OperationsWorkExecution key={selectedWork.id} workId={selectedWork.id} workName={workTitle(selectedWork)} backHref={`/operations?${listQuery}`} embedded />
            </DialogPrimitive.Popup>
          ) : null}
        </DialogPortal>
      </Dialog>
    </div>
  );
}

function AgentsView({ agents }: { agents: CommandAgent[] }) {
  return <div className="space-y-3" data-testid="operations-command-center-agents"><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><Bot className="size-4 text-primary" /> <span className="inline-flex items-center gap-1">Agents<HelpPopover label="Agents">Agent identity、lifecycle、authority ceilingと実行履歴のsafe projection。</HelpPopover></span></CardTitle></CardHeader><CardContent className="space-y-3 pt-3">{agents.length ? agents.map((agent) => <article key={agent.id || agent.name} className="rounded-lg border border-border/70 bg-background/40 p-3"><div className="flex flex-wrap items-start justify-between gap-2"><div className="min-w-0"><h3 className="truncate text-sm font-semibold">{agent.name}</h3><p className="mt-0.5 text-xs text-muted-foreground">{agent.jobTitle} · primary Space {agent.space}</p></div><span className={cn("text-xs font-medium", statusClass(agent.state))}>{agent.state}</span></div><div className="mt-3 grid gap-x-4 gap-y-1 text-xs text-muted-foreground sm:grid-cols-2 lg:grid-cols-4"><span>Project grants: {agent.projectGrants.join(", ") || "—"}</span><span>Current work: {agent.currentWork}</span><span>Latest AgentRun: {agent.latestRun}</span><span>Execution Team: {agent.team}</span><span>Execution Profile: {agent.profile}</span><span>Persona: {agent.personas.join(", ") || "—"}</span><span>Character link: {agent.character}</span><span>Capabilities: {agent.capabilities.join(", ") || "—"}</span><span>Budget: {agent.budget}</span><span>Concurrency: {agent.concurrency}</span></div>{agent.blockers.length || agent.failures.length ? <div className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-xs"><span className="font-medium">Blockers / failures: </span>{[...agent.blockers, ...agent.failures].join(" · ")}</div> : null}{agent.runHistory.length ? <details className="mt-2 text-xs"><summary className="cursor-pointer text-muted-foreground">Run history ({agent.runHistory.length})</summary><ul className="mt-1 space-y-1 pl-4 text-muted-foreground">{agent.runHistory.map((run) => <li key={run.id}>{run.id} · {run.state} · {run.createdAt}</li>)}</ul></details> : null}</article>) : <EmptyState>Agentのprojectionはありません。</EmptyState>}</CardContent></Card></div>;
}

function WorkView({ work, scopeQuery }: { work: CommandWork[]; scopeQuery: string }) {
  return <div className="space-y-3" data-testid="operations-command-center-work"><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><Clock3 className="size-4 text-primary" /> <span className="inline-flex items-center gap-1">Work<HelpPopover label="Work">全ドメインのAgentWorkItemを、canonical sourceへのリンク付きで表示します。</HelpPopover></span></CardTitle></CardHeader><CardContent className="pt-3">{work.length ? <div className="overflow-x-auto"><table className="w-full min-w-[1060px] text-left text-xs"><thead className="border-b border-border/70 text-[10px] uppercase tracking-[0.1em] text-muted-foreground"><tr><th className="px-2 py-2">Source / domain</th><th className="px-2 py-2">Space / Project</th><th className="px-2 py-2">Agent / Task</th><th className="px-2 py-2">State</th><th className="px-2 py-2">Priority / due</th><th className="px-2 py-2">Lease / attempts</th><th className="px-2 py-2">Approval / budget</th></tr></thead><tbody>{work.map((item, index) => <tr key={item.id || `${item.sourceId}-${index}`} className="border-b border-border/50 align-top last:border-0"><td className="px-2 py-2"><div className="font-medium">{item.sourceType || "WorkItem"}</div><div className="mt-0.5 font-mono text-[10px] text-muted-foreground">{item.sourceId || item.id || "—"}</div><div className="text-[10px] text-muted-foreground">{item.domain}</div><Link href={workDetailHref(item.id, scopeQuery)} className="mt-1 block text-primary hover:underline">実行の詳細</Link>{item.href ? <Link href={item.href} className="mt-1 inline-flex items-center gap-1 text-primary hover:underline">Open source <ExternalLink className="size-3" /></Link> : null}</td><td className="px-2 py-2 text-muted-foreground">{item.space}<br />{item.project}</td><td className="px-2 py-2"><div>{item.agent}</div><div className="mt-0.5 text-muted-foreground">{item.task}</div><div className="text-muted-foreground">Persona: {item.persona}</div></td><td className={cn("px-2 py-2 font-medium", statusClass(item.state))}>{item.state}</td><td className="px-2 py-2 text-muted-foreground">{item.priority}<br />{item.notBefore}<br />{item.deadline}</td><td className="px-2 py-2 text-muted-foreground">{item.leaseHealth}<br />{item.attempts}<br />Run: {item.latestRun}</td><td className="px-2 py-2 text-muted-foreground">{item.approval}<br />{item.budget}</td></tr>)}</tbody></table></div> : <EmptyState>WorkItemはありません。</EmptyState>}</CardContent></Card></div>;
}

function ActivityView({ activity }: { activity: CommandActivity[] }) {
  return <div className="space-y-3" data-testid="operations-command-center-activity"><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><Activity className="size-4 text-primary" /> <span className="inline-flex items-center gap-1">Activity<HelpPopover label="Activity">AgentWorkEvent、AgentRunEvent、TaskActivity、Heartbeat履歴、ExternalAction、Media evidenceのprojection。</HelpPopover></span></CardTitle></CardHeader><CardContent className="pt-3">{activity.length ? <ol className="space-y-2 border-l border-border pl-4">{activity.slice(0, 50).map(item => <li key={`${item.kind}:${item.id || JSON.stringify([item.createdAt, item.event, item.actor, item.detail])}`} className="relative rounded-md border border-border/60 bg-background/30 p-2.5 text-xs"><span className="absolute -left-[1.33rem] top-3 size-2 rounded-full border border-primary bg-background" /><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{item.event}</span><time className="text-muted-foreground">{item.createdAt}</time></div><div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-muted-foreground"><span>Actor: {item.actor}</span><span>Domain: {item.domain}</span><span>State: <span className={statusClass(item.state)}>{item.state}</span></span></div>{item.detail !== "—" ? <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{item.detail}</p> : null}{item.href ? <Link href={item.href} className="mt-1 inline-flex items-center gap-1 text-primary hover:underline">Canonical source <ArrowRight className="size-3" /></Link> : null}</li>)}</ol> : <EmptyState>Activity eventはありません。</EmptyState>}</CardContent></Card></div>;
}

export function OperationsCommandCenter({
  view = "overview",
  agentId,
}: {
  view?: OperationsCommandCenterView;
  agentId?: string;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const runtime = useOptionalRuntimeContext();
  const applicationFeatures = runtime?.runtimeFeatures?.application_features;
  // The command-center company views are fail-closed when capability
  // metadata is unavailable.  Low-level adapter flags do not carry the
  // virtual-company/autonomous-runtime policy.
  const autonomousCompanyPanelsEnabled =
    applicationFeatures?.virtual_company === true &&
    applicationFeatures?.autonomous_agent_runtime === true;
  const panelDisabled =
    !autonomousCompanyPanelsEnabled && (view === "agents" || view === "work");
  const persistedQuery = queryString(searchParams);
  const stableSearchParams = useMemo(
    () => new URLSearchParams(persistedQuery),
    [persistedQuery],
  );
  const scopeQuery = filterQuery(filtersFromSearchParams(stableSearchParams));
  const filters = useMemo(() => {
    const scoped = filtersFromSearchParams(new URLSearchParams(scopeQuery));
    return agentId ? { ...scoped, scope: "agent" as const, agentId, spaceId: "", projectId: "" } : scoped;
  }, [scopeQuery, agentId]);
  const detailWorkId = view === "work" ? safeId(stableSearchParams.get("work_id")) : "";
  const observationScope = JSON.stringify(filters);
  const [snapshotScope, setSnapshotScope] = useState<string | null>(null);
  const [storedSnapshot, setSnapshot] = useState<CommandSnapshot>(EMPTY_SNAPSHOT);
  const scopeReady = snapshotScope === observationScope;
  const snapshot = scopeReady ? storedSnapshot : EMPTY_SNAPSHOT;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);
  const requestVersion = useRef(0);
  const requestController = useRef<AbortController | null>(null);
  const pageQuery = useRef<string | null>(null);
  const pageRequestPending = useRef(false);
  const consumedCursors = useRef({ work: new Set<string>(), agent: new Set<string>() });
  const automaticPageRequest = useRef<{ selectionKey: string } | null>(null);
  const [paginationLoading, setPaginationLoading] = useState(false);
  const [paginationError, setPaginationError] = useState<unknown>(null);
  const requestedWorkFilter = stableSearchParams.get("work_view");
  const workFilter = requestedWorkFilter === "open" || requestedWorkFilter === "completed"
    ? requestedWorkFilter : "all";
  const requestedGraphNode = stableSearchParams.get("graph_node") || "";
  const requestedGraphWork = safeId(stableSearchParams.get("graph_work"));
  const selectionKey = JSON.stringify([observationScope, view, requestedGraphNode, requestedGraphWork]);
  const selectionTargets = useMemo(() => {
    if (view !== "overview") return [];
    const targets: { kind: CommandPageKind; id: string }[] = [];
    if (requestedGraphWork) targets.push({ kind: "work", id: requestedGraphWork });
    const match = /^(work|agent):(.+)$/u.exec(requestedGraphNode);
    const nodeId = match ? safeId(match[2]) : "";
    if (match && nodeId && !targets.some(target => target.kind === match[1] && target.id === nodeId)) {
      targets.push({ kind: match[1] as CommandPageKind, id: nodeId });
    }
    return targets;
  }, [view, requestedGraphWork, requestedGraphNode]);
  const missingSelectionTargets = selectionTargets.filter(target => target.kind === "work"
    ? !snapshot.work.some(work => work.id === target.id)
    : !snapshot.agents.some(agent => agent.id === target.id));
  const searchableTarget = missingSelectionTargets.find(target => (
    target.kind === "work" ? snapshot.workPage : snapshot.agentsPage
  ).hasMore);
  const targetPage = searchableTarget?.kind === "work" ? snapshot.workPage : snapshot.agentsPage;
  const deepLinkSearchStatus: DeepLinkSearchStatus = !missingSelectionTargets.length ? null
    : !searchableTarget ? missingSelectionTargets.every(target => (
      target.kind === "work" ? snapshot.workPage : snapshot.agentsPage
    ).available !== null) ? "missing" : null
      : paginationError || !targetPage.nextCursor ? "stopped" : "searching";

  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    pageQuery.current = null;
    pageRequestPending.current = false;
    automaticPageRequest.current = null;
    consumedCursors.current = { work: new Set(), agent: new Set() };
    setPaginationLoading(false);
    setPaginationError(null);
    if (panelDisabled || detailWorkId) {
      setSnapshot(EMPTY_SNAPSHOT);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    const query = queryWithTimeRange(filters);
    const suffix = query ? `?${query}` : "";
    try {
      let payload: unknown;
      try {
        payload = await fetchJson(`/operations/command-center${suffix}`, controller.signal);
      } catch (firstError) {
        const status = numberValue(
          (firstError as { status?: unknown })?.status,
        );
        if (status !== 404) throw firstError;
        payload = await fetchJson(`/operations/overview${suffix}`, controller.signal);
      }
      if (version !== requestVersion.current) return;
      setSnapshot(normaliseSnapshot(payload));
      setSnapshotScope(observationScope);
      pageQuery.current = query;
      setCheckedAt(
        new Date().toLocaleString("ja-JP", {
          dateStyle: "short",
          timeStyle: "short",
        }),
      );
    } catch (loadError) {
      if (version !== requestVersion.current) return;
      setSnapshot(EMPTY_SNAPSHOT);
      setSnapshotScope(null);
      setError(loadError);
    } finally {
      if (version === requestVersion.current) setLoading(false);
    }
  }, [filters, panelDisabled, detailWorkId, observationScope]);

  const loadMore = useCallback(async (kind?: CommandPageKind, automaticSelectionKey?: string) => {
    if (!scopeReady || loading || pageRequestPending.current || pageQuery.current === null) return;
    const advanceWork = kind !== "agent" && storedSnapshot.workPage.hasMore;
    const advanceAgents = kind !== "work" && storedSnapshot.agentsPage.hasMore;
    const workCursor = advanceWork ? storedSnapshot.workPage.nextCursor : null;
    const agentsCursor = advanceAgents ? storedSnapshot.agentsPage.nextCursor : null;
    if ((!workCursor && !agentsCursor) ||
      (advanceWork && !workCursor) || (advanceAgents && !agentsCursor)) return;
    pageRequestPending.current = true;
    automaticPageRequest.current = automaticSelectionKey ? { selectionKey: automaticSelectionKey } : null;
    setPaginationLoading(true);
    setPaginationError(null);
    const version = ++requestVersion.current;
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    // Keep the original time bounds for relative ranges and cursor validation.
    const query = new URLSearchParams(pageQuery.current);
    if (workCursor) query.set("work_cursor", workCursor);
    if (agentsCursor) query.set("agents_cursor", agentsCursor);
    try {
      const page = normaliseSnapshot(await fetchJson(`/operations/command-center?${query}`, controller.signal));
      if (version !== requestVersion.current) return;
      if (workCursor) consumedCursors.current.work.add(workCursor);
      if (agentsCursor) consumedCursors.current.agent.add(agentsCursor);
      setSnapshot((previous) => ({
        ...page,
        work: workCursor ? mergeRecords(previous.work, page.work) : previous.work,
        agents: agentsCursor ? mergeRecords(previous.agents, page.agents) : previous.agents,
        workPage: workCursor ? page.workPage : previous.workPage,
        agentsPage: agentsCursor ? page.agentsPage : previous.agentsPage,
        projects: mergeRecords(previous.projects, page.projects),
        spaces: mergeRecords(previous.spaces, page.spaces),
      }));
      setCheckedAt(new Date().toLocaleString("ja-JP", { dateStyle: "short", timeStyle: "short" }));
      for (const [cursor, next, consumed] of [
        [workCursor, page.workPage, consumedCursors.current.work],
        [agentsCursor, page.agentsPage, consumedCursors.current.agent],
      ] as const) {
        if (cursor && next.hasMore && (!next.nextCursor || consumed.has(next.nextCursor))) {
          throw new Error("追加取得のcursorが進みません。「更新」で取り直してください。");
        }
      }
    } catch (pageError) {
      if (version === requestVersion.current) setPaginationError(pageError);
    } finally {
      if (version === requestVersion.current) {
        pageRequestPending.current = false;
        automaticPageRequest.current = null;
        setPaginationLoading(false);
      }
    }
  }, [scopeReady, loading, storedSnapshot]);

  useEffect(() => {
    void load();
    return () => {
      requestVersion.current += 1;
      requestController.current?.abort();
      pageRequestPending.current = false;
      automaticPageRequest.current = null;
    };
  }, [load]);

  useEffect(() => {
    // A pending automatic read belongs to this exact URL selection and scope.
    // Manual continuation remains useful when only selection changes.
    if (automaticPageRequest.current && automaticPageRequest.current.selectionKey !== selectionKey) {
      requestVersion.current += 1;
      requestController.current?.abort();
      automaticPageRequest.current = null;
      pageRequestPending.current = false;
      setPaginationLoading(false);
    }
  }, [selectionKey]);

  useEffect(() => {
    if (!scopeReady || loading || paginationLoading || paginationError || !searchableTarget || !targetPage.nextCursor) return;
    void loadMore(searchableTarget.kind, selectionKey);
  }, [scopeReady, loading, paginationLoading, paginationError, searchableTarget, targetPage.nextCursor, loadMore, selectionKey]);

  const updateFilter = useCallback(
    (key: keyof CommandFilters, value: string) => {
      const next = new URLSearchParams(queryString(searchParams));
      next.delete("graph_node");
      next.delete("graph_work");
      next.delete("execution_focus");
      next.delete("execution_node");
      if (key === "scope") {
        if (value && value !== "all") next.set("scope", value);
        else next.delete("scope");
        if (value !== "space") next.delete("space_id");
        if (value !== "project") next.delete("project_id");
        if (value !== "agent") {
          next.delete("agent_id");
          next.delete("agent");
        }
      } else {
        const queryKey =
          key === "spaceId"
            ? "space_id"
            : key === "projectId"
              ? "project_id"
              : key === "agentId"
                ? "agent_id"
                : key === "range"
                  ? "range"
                  : key;
        if (value) next.set(queryKey, value);
        else next.delete(queryKey);
      }
      next.set("tab", view);
      if (view === "overview") next.set("overview_view", "map");
      const query = next.toString();
      router.replace(
        `${pathname || "/operations"}${query ? `?${query}` : ""}`,
        { scroll: false },
      );
    },
    [pathname, router, searchParams, view],
  );

  useEffect(() => {
    if (view !== "overview" || stableSearchParams.get("graph_work") || !stableSearchParams.get("graph_node")) return;
    // Refresh disables its button and browsers may move focus to body. Keep
    // Escape available throughout the overview, respecting dialog handlers.
    const clearSelection = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const next = new URLSearchParams(persistedQuery);
      next.delete("graph_node");
      next.set("tab", "overview");
    next.set("overview_view", "map");
      router.push(`${pathname || "/operations"}${next.size ? `?${next}` : ""}`, { scroll: false });
    };
    window.addEventListener("keydown", clearSelection);
    return () => window.removeEventListener("keydown", clearSelection);
  }, [view, stableSearchParams, persistedQuery, router, pathname]);

  if (detailWorkId && !panelDisabled) {
    const backQuery = new URLSearchParams(persistedQuery);
    backQuery.delete("work_id");
    return <OperationsWorkExecution key={detailWorkId} workId={detailWorkId} backHref={`/operations?${backQuery}`} />;
  }

  return (
    <div
      className={view === "overview" ? "flex min-h-0 min-w-0 flex-1 flex-col" : "space-y-4"}
      data-testid="operations-command-center"
      data-command-center-view={view}
    >
      <header className={cn("flex shrink-0 flex-wrap items-center justify-between gap-2", view === "overview" && "border-b border-border px-3 py-2")}>
        <div className={view === "overview" ? "flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1" : ""}>
          {view !== "overview" ? <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">
            Operations
          </p> : null}
          <h1 className={cn("font-semibold tracking-tight", view === "overview" ? "text-base" : "mt-1 text-2xl")}>
            {view === "overview"
              ? "Operations Map"
              : view[0].toUpperCase() + view.slice(1)}
          </h1>
          <p className={cn("text-muted-foreground", view === "overview" ? "text-[10px]" : "mt-2 text-sm")}>
            {loading ? "更新中…" : checkedAt ? `最終確認 ${checkedAt}` : "最新の状況を確認"}
          </p>
        </div>
        <div className="flex items-center gap-2">
        {view === "overview" && !agentId ? <FilterBar compact filters={filters} onChange={updateFilter} /> : null}
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={loading}
          onClick={() => void load()}
        >
          {loading ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <RefreshCw className="size-3.5" />
          )}
          更新
        </Button>
        </div>
      </header>
      {!agentId && view !== "overview" ? (
        <FilterBar filters={filters} onChange={updateFilter} />
      ) : null}
      {panelDisabled ? (
        <Card size="sm" data-testid="operations-command-center-disabled">
          <CardContent className="flex items-start gap-2 pt-4 text-sm text-muted-foreground">
            <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-600" />
            <p>
              Company Agent
              runtimeは現在無効です。この画面では履歴を変更せず、手動のMediaOps
              / EngagementOpsだけを利用できます。
            </p>
          </CardContent>
        </Card>
      ) : null}
      <ErrorNotice error={error} />
      {!panelDisabled && !scopeReady && !error ? (
        <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> 仕事の状況を読み込み中…
        </div>
      ) : null}
      {!panelDisabled &&
      scopeReady &&
      !error ? (
        view === "overview" ? (
          <OverviewView
            key={observationScope}
            snapshot={snapshot}
            paginationLoading={loading || paginationLoading}
            paginationError={paginationError}
            deepLinkSearchStatus={deepLinkSearchStatus}
            onLoadMore={() => void loadMore()}
            scopeQuery={persistedQuery}
            workFilter={workFilter}
            onWorkFilterChange={(value) => {
              const next = new URLSearchParams(persistedQuery);
              for (const key of ["graph_node", "graph_work", "execution_focus", "execution_node"]) next.delete(key);
              if (value === "all") next.delete("work_view");
              else next.set("work_view", value);
              next.set("tab", "overview");
    next.set("overview_view", "map");
              const query = next.toString();
              router.replace(
                `${pathname || "/operations"}${query ? `?${query}` : ""}`,
                { scroll: false },
              );
            }}
          />
        ) : view === "agents" ? (
          <><AgentsView agents={snapshot.agents} /><RecordPagination snapshot={snapshot} loading={loading || paginationLoading} error={paginationError} onLoadMore={() => void loadMore()} /></>
        ) : view === "work" ? (
          <><WorkView work={snapshot.work} scopeQuery={persistedQuery} /><RecordPagination snapshot={snapshot} loading={loading || paginationLoading} error={paginationError} onLoadMore={() => void loadMore()} /></>
        ) : (
          <ActivityView activity={snapshot.activity} />
        )
      ) : null}
    </div>
  );
}

export const __operationsCommandCenterTestUtils = {
  normaliseSnapshot,
  normaliseSummary,
  normaliseAgents,
  normaliseWork,
  normaliseActivity,
  normaliseAttention,
  buildOperationsMap,
  visibleWorkItems,
  normalizeSnapshot: normaliseSnapshot,
  normalizeSummary: normaliseSummary,
  normalizeAgents: normaliseAgents,
  normalizeWork: normaliseWork,
  normalizeActivity: normaliseActivity,
  filterQuery,
  queryWithTimeRange,
  isFinishedWork,
  workStateLabel,
};

"use client";

/* The command-center projection is synchronized from a read-only API snapshot. */
/* eslint-disable react-hooks/set-state-in-effect */

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
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
  ShieldCheck,
  TimerReset,
} from "lucide-react";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";

export type OperationsCommandCenterView = "overview" | "agents" | "work" | "activity";

type FilterScope = "all" | "space" | "project" | "agent";
type SearchParamReader = { get: (key: string) => string | null } | null | undefined;
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
  space: string;
  projectGrants: string[];
  currentWork: string;
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
  priority: string;
  notBefore: string;
  deadline: string;
  leaseHealth: string;
  attempts: string;
  approval: string;
  budget: string;
  latestRun: string;
  elapsed: string;
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
  id: string;
  kind: string;
  title: string;
  detail: string;
  state: string;
  href?: string;
};

type CommandSnapshot = {
  organization: { name: string; state: string; detail: string };
  summary: CommandSummary;
  agents: CommandAgent[];
  work: CommandWork[];
  activity: CommandActivity[];
  attention: CommandAttention[];
  projects: Array<{ id: string; name: string; state: string; detail: string; href?: string }>;
  schedules: Array<{ id: string; name: string; due: string; state: string }>;
  artifacts: Array<{ id: string; name: string; kind: string; createdAt: string }>;
  evidence: Array<{ id: string; kind: string; detail: string; createdAt: string }>;
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
  activity: [],
  attention: [],
  projects: [],
  schedules: [],
  artifacts: [],
  evidence: [],
  mediaStatus: [],
  runtimeUsage: [],
};

const UUID_OR_SAFE_ID = /^[a-z0-9][a-z0-9._:-]{0,127}$/iu;
const SECRET_KEY = /(?:password|secret|token|credential|api[_-]?key|authorization|prompt|model|provider|filesystem|file[_-]?path|storage[_-]?path|environment|env)/iu;

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
      space: label(valueAt(root, ["primary_space_name", "space_name", "primary_space_id", "space_id"])),
      projectGrants: safeList(valueAt(root, ["project_grants", "projects", "grants"])),
      currentWork: label(valueAt(work, ["title", "name", "id", "work_item_id"])),
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
  return array(payload, ["work", "work_items", "items", "current_work"]).map((entry) => {
    const root = record(entry);
    const source = nestedRecord(root, ["source", "source_ref"]);
    const run = nestedRecord(root, ["latest_run", "latest_agent_run", "run"]);
    const id = safeId(valueAt(root, ["id", "work_item_id"]));
    const sourceType = label(valueAt(root, ["source_type", "sourceType"]) ?? valueAt(source, ["type", "source_type"]));
    const sourceId = safeId(valueAt(root, ["source_id", "sourceId"]) ?? valueAt(source, ["id", "source_id"]));
    const taskId = safeId(valueAt(root, ["task_id"]) ?? valueAt(source, ["task_id"]));
    const personaId = safeId(valueAt(root, ["persona_id"]));
    const actionId = safeId(valueAt(root, ["external_action_id", "action_id"]));
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
    const href = hrefFor(inferredKind, actionId || taskId || personaId || sourceId);
    return {
      id,
      sourceType,
      sourceId,
      domain: label(valueAt(root, ["domain", "work_domain"])),
      space: label(valueAt(root, ["space_name", "space", "space_id"])),
      project: label(valueAt(root, ["project_name", "project", "project_id"])),
      agent: label(valueAt(root, ["agent_name", "agent", "assigned_agent_name", "assigned_agent_id"])),
      task: label(valueAt(root, ["task_title", "task_name", "task_id"])),
      persona: label(valueAt(root, ["persona_name", "persona_id"])),
      app: label(valueAt(root, ["app_name", "app_id"])),
      state: label(valueAt(root, ["state", "status"])),
      priority: label(valueAt(root, ["priority"])),
      notBefore: dateLabel(valueAt(root, ["not_before", "available_at"])),
      deadline: dateLabel(valueAt(root, ["deadline", "due_at"])),
      leaseHealth: label(valueAt(root, ["lease_health", "lease_status", "lease_expires_at"])),
      attempts: label(valueAt(root, ["attempt_count", "attempts"]), "0"),
      approval: label(valueAt(root, ["approval_state", "approval_status"])),
      budget: label(valueAt(root, ["budget_reservation", "budget"])),
      latestRun: label(valueAt(run, ["id", "run_id", "status"]) ?? valueAt(root, ["latest_agent_run_id", "latest_run_id"])),
      elapsed: label(valueAt(root, ["elapsed", "elapsed_seconds", "duration", "elapsed_duration"])),
      href,
    };
  }).filter((item) => item.id || item.sourceId);
}

function normaliseAttention(payload: unknown): CommandAttention[] {
  return array(payload, ["attention", "requires_attention", "items"]).map((entry) => {
    const root = record(entry);
    const kind = label(valueAt(root, ["kind", "type", "category"]), "attention");
    const id = safeId(valueAt(root, ["id", "work_item_id", "action_id", "task_id"]));
    const lowerKind = kind.toLowerCase();
    const href = hrefFor(lowerKind.includes("action") || lowerKind.includes("approval") ? "action" : lowerKind.includes("task") ? "task" : "", id);
    return {
      id,
      kind,
      title: label(valueAt(root, ["title", "name", "summary"]), kind || "要対応"),
      detail: label(valueAt(root, ["detail", "reason", "message", "safe_error_code"])),
      state: label(valueAt(root, ["state", "status"])),
      href,
    };
  });
}

function normaliseActivity(payload: unknown): CommandActivity[] {
  return array(payload, ["activity", "activities", "events", "items"]).map((entry) => {
    const root = record(entry);
    const id = safeId(valueAt(root, ["id", "event_id"]));
    const entityType = label(valueAt(root, ["entity_type", "source_type", "kind"]), "");
    const entityId = safeId(valueAt(root, ["entity_id", "source_id", "task_id", "work_item_id", "action_id"]));
    const lowerType = entityType.toLowerCase();
    const href = hrefFor(lowerType.includes("task") ? "task" : lowerType.includes("action") ? "action" : lowerType.includes("agent") ? "agent" : "", entityId);
    return {
      id,
      kind: label(valueAt(root, ["kind", "entity_type", "type", "source_type"]), "activity"),
      event: label(valueAt(root, ["event", "event_type", "activity_type", "type", "kind"]), "activity"),
      actor: label(valueAt(root, ["actor_name", "actor_label", "actor_id", "actor_type"])),
      domain: label(valueAt(root, ["domain", "source_type", "entity_type"])),
      state: label(valueAt(root, ["state", "status"])),
      createdAt: dateLabel(valueAt(root, ["created_at", "occurred_at", "started_at", "finished_at", "timestamp"])),
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

function normaliseSnapshot(payload: unknown): CommandSnapshot {
  const payloadRoot = record(payload);
  const outer = firstRecord(payload, ["command_center", "overview", "snapshot", "data"]);
  const root = outer === payloadRoot ? outer : { ...payloadRoot, ...outer };
  const simple = (keys: string[]) => nestedRecord(root, keys);
  const media = simple(["media_status", "mediaOps", "media_operations"]);
  const usage = simple(["runtime_usage", "usage", "runtime"]);
  const organization = simple(["organization", "organization_summary"]);
  const agents = normaliseAgents(valueAt(root, ["agents", "agent_states", "agent_list"]) ?? []);
  const summary = normaliseSummary(root);
  if (summary.activeAgents === 0 && agents.length) {
    const states = agents.map((agent) => agent.state.toLowerCase());
    summary.activeAgents = states.filter((state) => ["active", "working", "running"].includes(state)).length;
    summary.working = states.filter((state) => ["working", "running"].includes(state)).length;
    summary.idle = states.filter((state) => ["idle", "ready"].includes(state)).length;
    summary.blocked = states.filter((state) => state === "blocked").length;
  }
  return {
    organization: {
      name: label(valueAt(organization, ["name", "display_name", "company_name"]), "Organization"),
      state: label(valueAt(organization, ["state", "status"])),
      detail: label(valueAt(organization, ["detail", "policy_version", "id"])),
    },
    summary,
    agents,
    work: normaliseWork(valueAt(root, ["work", "work_items", "current_work", "current_work_items"]) ?? []),
    activity: normaliseActivity(valueAt(root, ["activity", "activities", "events", "activity_timeline"]) ?? []),
    attention: normaliseAttention(valueAt(root, ["attention", "requires_attention", "attention_items"]) ?? []),
    projects: normaliseSimpleList(valueAt(root, ["projects", "project_health"]) ?? [], ["projects", "project_health", "items"], "project").map((item) => ({ id: item.id, name: item.name ?? "—", state: item.state ?? "—", detail: item.detail ?? "—", href: hrefFor("project", item.id) })),
    schedules: normaliseSimpleList(valueAt(root, ["schedules", "due_work"]) ?? [], ["schedules", "due_work", "items"], "schedule").map((item) => ({ id: item.id, name: item.name ?? "—", due: item.due ?? "—", state: item.state ?? "—" })),
    artifacts: normaliseSimpleList(valueAt(root, ["artifacts", "recent_artifacts"]) ?? [], ["artifacts", "recent_artifacts", "items"], "artifact").map((item) => ({ id: item.id, name: item.name ?? "—", kind: item.kind ?? "—", createdAt: item.createdAt ?? "—" })),
    evidence: normaliseSimpleList(valueAt(root, ["evidence", "recent_evidence"]) ?? [], ["evidence", "recent_evidence", "items"], "evidence").map((item) => ({ id: item.id, kind: item.kind ?? "—", detail: item.detail ?? "—", createdAt: item.createdAt ?? "—" })),
    mediaStatus: Object.entries(media).filter(([key]) => !SECRET_KEY.test(key)).slice(0, 12).map(([key, value]) => ({ label: safeText(key, "status", 60), value: safeCompactValue(value, ["value", "status", "count"]), detail: typeof value === "object" ? safeText(valueAt(record(value), ["detail", "summary"])) : "" })),
    runtimeUsage: Object.entries(usage).filter(([key]) => !SECRET_KEY.test(key)).slice(0, 12).map(([key, value]) => ({ label: safeText(key, "usage", 60), value: safeCompactValue(value, ["value", "used", "count", "running", "claimed"]), detail: typeof value === "object" ? safeText(valueAt(record(value), ["detail", "limit", "window"])) : "" })),
  };
}

async function fetchJson(path: string): Promise<unknown> {
  const response = await fetch(`/api/python-proxy${path}`, {
    cache: "no-store",
    credentials: "include",
    headers: { accept: "application/json" },
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
  return <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">Command Centerの状態を読み込めませんでした。権限またはサーバーの設定を確認してください。</div>;
}

function FilterBar({ filters, onChange, onRefresh, loading }: { filters: CommandFilters; onChange: (key: keyof CommandFilters, value: string) => void; onRefresh: () => void; loading: boolean }) {
  return (
    <Card size="sm" data-testid="operations-command-center-filters">
      <CardHeader className="border-b border-border/70 pb-3"><CardTitle className="flex items-center gap-1.5 text-sm"><Filter className="size-4 text-primary" /> Scope / filters</CardTitle><CardDescription>Organizationはインストール単位で固定です。Space・Project・Agentの範囲だけを絞り込みます。</CardDescription></CardHeader>
      <CardContent className="flex flex-wrap items-end gap-2 pt-3">
        <label className="space-y-1 text-xs"><span className="block text-muted-foreground">Scope</span><AppSelect aria-label="Scope" className="h-8 min-w-36 rounded-md border border-input bg-card px-2 text-xs" value={filters.scope} onChange={(event) => onChange("scope", event.target.value)}><option value="all">All company</option><option value="space">Space</option><option value="project">Project</option><option value="agent">Agent</option></AppSelect></label>
        {filters.scope === "space" ? <label className="space-y-1 text-xs"><span className="block text-muted-foreground">Space ID</span><Input aria-label="Space ID" className="h-8 w-44 text-xs" value={filters.spaceId} onChange={(event) => onChange("spaceId", event.target.value)} placeholder="space-id" /></label> : null}
        {filters.scope === "project" ? <label className="space-y-1 text-xs"><span className="block text-muted-foreground">Project ID</span><Input aria-label="Project ID" className="h-8 w-44 text-xs" value={filters.projectId} onChange={(event) => onChange("projectId", event.target.value)} placeholder="project-id" /></label> : null}
        {filters.scope === "agent" ? <label className="space-y-1 text-xs"><span className="block text-muted-foreground">Agent ID</span><Input aria-label="Agent ID" className="h-8 w-44 text-xs" value={filters.agentId} onChange={(event) => onChange("agentId", event.target.value)} placeholder="agent-id" /></label> : null}
        <label className="space-y-1 text-xs"><span className="block text-muted-foreground">Domain</span><AppSelect aria-label="Domain" className="h-8 min-w-40 rounded-md border border-input bg-card px-2 text-xs" value={filters.domain} onChange={(event) => onChange("domain", event.target.value)}><option value="">All domains</option><option value="internal">Internal / company</option><option value="media">MediaOps</option><option value="code">Code agent</option><option value="apps">Apps</option></AppSelect></label>
        <label className="space-y-1 text-xs"><span className="block text-muted-foreground">State</span><AppSelect aria-label="State" className="h-8 min-w-40 rounded-md border border-input bg-card px-2 text-xs" value={filters.state} onChange={(event) => onChange("state", event.target.value)}><option value="">All states</option><option value="running">Working</option><option value="claimed">Claimed</option><option value="blocked">Blocked</option><option value="awaiting_approval">Awaiting approval</option><option value="uncertain">Uncertain</option><option value="failed">Failed</option><option value="dead_letter">Dead letter</option><option value="stale">Stale</option></AppSelect></label>
        <label className="space-y-1 text-xs"><span className="block text-muted-foreground">Time range</span><AppSelect aria-label="Time range" className="h-8 min-w-32 rounded-md border border-input bg-card px-2 text-xs" value={filters.range} onChange={(event) => onChange("range", event.target.value)}><option value="">Any time</option><option value="24h">Last 24 hours</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option></AppSelect></label>
        <Button type="button" variant="outline" size="sm" className="h-8" onClick={onRefresh} disabled={loading}>{loading ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} 更新</Button>
      </CardContent>
    </Card>
  );
}

function AttentionList({ items }: { items: CommandAttention[] }) {
  return <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><ShieldAlert className="size-4 text-amber-600" /> Requires attention</CardTitle><CardDescription>承認、ブロッカー、stale lease、不確実な結果を確認します。</CardDescription></CardHeader><CardContent className="space-y-2 pt-3">{items.length ? items.slice(0, 12).map((item, index) => <div key={item.id || `${item.kind}-${index}`} className="flex items-start gap-2 rounded-md border border-border/70 bg-background/40 p-2.5"><AlertTriangle className={cn("mt-0.5 size-4 shrink-0", statusClass(item.state))} /><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="text-xs font-semibold">{item.title}</span><span className={cn("text-[10px]", statusClass(item.state))}>{item.kind}</span></div><p className="mt-0.5 text-xs text-muted-foreground">{item.detail}</p></div>{item.href ? <Link href={item.href} aria-label={`${item.title}を開く`} className="shrink-0 text-primary hover:underline"><ArrowRight className="size-4" /></Link> : null}</div>) : <EmptyState>対応が必要な項目はありません。</EmptyState>}</CardContent></Card>;
}

function CurrentWorkList({ items }: { items: CommandWork[] }) {
  return <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><TimerReset className="size-4 text-primary" /> Current work</CardTitle><CardDescription>AgentWorkItemと最新AgentRunの読み取り専用projection。</CardDescription></CardHeader><CardContent className="pt-3">{items.length ? <div className="space-y-2">{items.slice(0, 20).map((item, index) => <div key={item.id || `${item.sourceType}-${item.sourceId}-${index}`} className="rounded-md border border-border/70 bg-background/40 p-2.5"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex min-w-0 items-center gap-2"><span className="truncate text-sm font-medium">{item.task !== "—" ? item.task : item.sourceId || "WorkItem"}</span><span className={cn("text-xs", statusClass(item.state))}>{item.state}</span></div>{item.href ? <Link href={item.href} className="text-xs text-primary hover:underline">Canonical source <ExternalLink className="inline size-3" /></Link> : null}</div><div className="mt-1 grid gap-x-4 gap-y-1 text-[11px] text-muted-foreground sm:grid-cols-2 lg:grid-cols-4"><span>Agent: {item.agent}</span><span>Space: {item.space}</span><span>Project: {item.project}</span><span>Run: {item.latestRun}</span><span>Elapsed: {item.elapsed}</span><span>Lease: {item.leaseHealth}</span><span>Attempt: {item.attempts}</span><span>Approval: {item.approval}</span></div></div>)}</div> : <EmptyState>現在実行中のWorkItemはありません。</EmptyState>}</CardContent></Card>;
}

function SupportingList({ snapshot }: { snapshot: CommandSnapshot }) {
  return <div className="grid gap-4 xl:grid-cols-2"><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Organization summary</CardTitle></CardHeader><CardContent className="space-y-1 pt-3 text-xs"><p className="font-medium">{snapshot.organization.name}</p><p className={statusClass(snapshot.organization.state)}>{snapshot.organization.state}</p><p className="text-muted-foreground">{snapshot.organization.detail}</p></CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Project health</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.projects.length ? snapshot.projects.slice(0, 8).map((project) => <div key={project.id} className="flex items-center justify-between gap-2 rounded-md border border-border/60 p-2 text-xs">{project.href ? <Link href={project.href} className="min-w-0 truncate font-medium hover:underline">{project.name}</Link> : <span className="min-w-0 truncate font-medium">{project.name}</span>}<span className={cn("shrink-0", statusClass(project.state))}>{project.state}</span></div>) : <EmptyState>Project healthはありません。</EmptyState>}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><CalendarClock className="size-4 text-primary" /> Schedules / due work</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.schedules.length ? snapshot.schedules.slice(0, 8).map((schedule) => <div key={schedule.id} className="flex items-center justify-between gap-2 rounded-md border border-border/60 p-2 text-xs"><span className="min-w-0 truncate font-medium">{schedule.name}</span><span className="shrink-0 text-muted-foreground">{schedule.due}</span></div>) : <EmptyState>予定・期限のprojectionはありません。</EmptyState>}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Recent artifacts</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.artifacts.length ? snapshot.artifacts.slice(0, 8).map((item) => <div key={item.id} className="rounded-md border border-border/60 p-2 text-xs"><span className="font-medium">{item.name}</span><span className="ml-2 text-muted-foreground">{item.kind} · {item.createdAt}</span></div>) : <EmptyState>Artifactはありません。</EmptyState>}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Recent evidence</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.evidence.length ? snapshot.evidence.slice(0, 8).map((item) => <div key={item.id} className="rounded-md border border-border/60 p-2 text-xs"><span className="font-medium">{item.kind}</span><span className="ml-2 text-muted-foreground">{item.detail} · {item.createdAt}</span></div>) : <EmptyState>Evidenceはありません。</EmptyState>}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">MediaOps status</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.mediaStatus.length ? snapshot.mediaStatus.map((item) => <div key={item.label} className="flex items-center justify-between gap-2 text-xs"><span>{item.label}</span><span className="text-muted-foreground">{item.value}{item.detail ? ` · ${item.detail}` : ""}</span></div>) : <EmptyState>MediaOps statusはありません。</EmptyState>}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Runtime usage</CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{snapshot.runtimeUsage.length ? snapshot.runtimeUsage.map((item) => <div key={item.label} className="flex items-center justify-between gap-2 text-xs"><span>{item.label}</span><span className="text-muted-foreground">{item.value}{item.detail ? ` · ${item.detail}` : ""}</span></div>) : <EmptyState>Runtime usageはありません。</EmptyState>}</CardContent></Card></div>;
}

function OverviewView({ snapshot }: { snapshot: CommandSnapshot }) {
  const summary = snapshot.summary;
  const budget = summary.budgetPercent === null
    ? "—"
    : `${summary.budgetPercent.toFixed(0)}%${summary.budgetUsed !== null && summary.budgetLimit !== null ? ` (${summary.budgetUsed}/${summary.budgetLimit})` : ""}`;
  return <div className="space-y-4" data-testid="operations-command-center-overview"><div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8"><Metric label="Active Agents" value={summary.activeAgents} tone="good" /><Metric label="Working" value={summary.working} tone="good" /><Metric label="Idle" value={summary.idle} /><Metric label="Blocked" value={summary.blocked} tone="bad" /><Metric label="Awaiting Approval" value={summary.awaitingApproval} tone="warn" /><Metric label="Uncertain" value={summary.uncertain} tone="warn" /><Metric label="Failed/Stale" value={summary.failedStale} tone="bad" /><Metric label="Budget usage" value={budget} tone={summary.budgetPercent !== null && summary.budgetPercent >= 90 ? "bad" : summary.budgetPercent !== null && summary.budgetPercent >= 75 ? "warn" : "default"} /></div><div className="grid gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(20rem,0.7fr)]"><CurrentWorkList items={snapshot.work} /><AttentionList items={snapshot.attention} /></div><SupportingList snapshot={snapshot} /></div>;
}

function AgentsView({ agents }: { agents: CommandAgent[] }) {
  return <div className="space-y-3" data-testid="operations-command-center-agents"><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><Bot className="size-4 text-primary" /> Agents</CardTitle><CardDescription>Agent identity、lifecycle、authority ceilingと実行履歴のsafe projection。</CardDescription></CardHeader><CardContent className="space-y-3 pt-3">{agents.length ? agents.map((agent) => <article key={agent.id || agent.name} className="rounded-lg border border-border/70 bg-background/40 p-3"><div className="flex flex-wrap items-start justify-between gap-2"><div className="min-w-0"><h3 className="truncate text-sm font-semibold">{agent.name}</h3><p className="mt-0.5 text-xs text-muted-foreground">{agent.jobTitle} · primary Space {agent.space}</p></div><span className={cn("text-xs font-medium", statusClass(agent.state))}>{agent.state}</span></div><div className="mt-3 grid gap-x-4 gap-y-1 text-xs text-muted-foreground sm:grid-cols-2 lg:grid-cols-4"><span>Project grants: {agent.projectGrants.join(", ") || "—"}</span><span>Current work: {agent.currentWork}</span><span>Latest AgentRun: {agent.latestRun}</span><span>Agent Team: {agent.team}</span><span>Execution Profile: {agent.profile}</span><span>Persona: {agent.personas.join(", ") || "—"}</span><span>Character link: {agent.character}</span><span>Capabilities: {agent.capabilities.join(", ") || "—"}</span><span>Budget: {agent.budget}</span><span>Concurrency: {agent.concurrency}</span></div>{agent.blockers.length || agent.failures.length ? <div className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-xs"><span className="font-medium">Blockers / failures: </span>{[...agent.blockers, ...agent.failures].join(" · ")}</div> : null}{agent.runHistory.length ? <details className="mt-2 text-xs"><summary className="cursor-pointer text-muted-foreground">Run history ({agent.runHistory.length})</summary><ul className="mt-1 space-y-1 pl-4 text-muted-foreground">{agent.runHistory.map((run) => <li key={run.id}>{run.id} · {run.state} · {run.createdAt}</li>)}</ul></details> : null}</article>) : <EmptyState>Agentのprojectionはありません。</EmptyState>}</CardContent></Card></div>;
}

function WorkView({ work }: { work: CommandWork[] }) {
  return <div className="space-y-3" data-testid="operations-command-center-work"><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><Clock3 className="size-4 text-primary" /> Work</CardTitle><CardDescription>全ドメインのAgentWorkItemを、canonical sourceへのリンク付きで表示します。</CardDescription></CardHeader><CardContent className="pt-3">{work.length ? <div className="overflow-x-auto"><table className="w-full min-w-[1060px] text-left text-xs"><thead className="border-b border-border/70 text-[10px] uppercase tracking-[0.1em] text-muted-foreground"><tr><th className="px-2 py-2">Source / domain</th><th className="px-2 py-2">Space / Project</th><th className="px-2 py-2">Agent / Task</th><th className="px-2 py-2">State</th><th className="px-2 py-2">Priority / due</th><th className="px-2 py-2">Lease / attempts</th><th className="px-2 py-2">Approval / budget</th></tr></thead><tbody>{work.map((item, index) => <tr key={item.id || `${item.sourceId}-${index}`} className="border-b border-border/50 align-top last:border-0"><td className="px-2 py-2"><div className="font-medium">{item.sourceType || "WorkItem"}</div><div className="mt-0.5 font-mono text-[10px] text-muted-foreground">{item.sourceId || item.id || "—"}</div><div className="text-[10px] text-muted-foreground">{item.domain}</div>{item.href ? <Link href={item.href} className="mt-1 inline-flex items-center gap-1 text-primary hover:underline">Open source <ExternalLink className="size-3" /></Link> : null}</td><td className="px-2 py-2 text-muted-foreground">{item.space}<br />{item.project}</td><td className="px-2 py-2"><div>{item.agent}</div><div className="mt-0.5 text-muted-foreground">{item.task}</div><div className="text-muted-foreground">Persona: {item.persona}</div></td><td className={cn("px-2 py-2 font-medium", statusClass(item.state))}>{item.state}</td><td className="px-2 py-2 text-muted-foreground">{item.priority}<br />{item.notBefore}<br />{item.deadline}</td><td className="px-2 py-2 text-muted-foreground">{item.leaseHealth}<br />{item.attempts}<br />Run: {item.latestRun}</td><td className="px-2 py-2 text-muted-foreground">{item.approval}<br />{item.budget}</td></tr>)}</tbody></table></div> : <EmptyState>WorkItemはありません。</EmptyState>}</CardContent></Card></div>;
}

function ActivityView({ activity }: { activity: CommandActivity[] }) {
  return <div className="space-y-3" data-testid="operations-command-center-activity"><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><Activity className="size-4 text-primary" /> Activity</CardTitle><CardDescription>AgentWorkEvent、AgentRunEvent、TaskActivity、Heartbeat履歴、ExternalAction、Media evidenceのprojection。</CardDescription></CardHeader><CardContent className="pt-3">{activity.length ? <ol className="space-y-2 border-l border-border pl-4">{activity.slice(0, 50).map(item => <li key={`${item.kind}:${item.id || JSON.stringify([item.createdAt, item.event, item.actor, item.detail])}`} className="relative rounded-md border border-border/60 bg-background/30 p-2.5 text-xs"><span className="absolute -left-[1.33rem] top-3 size-2 rounded-full border border-primary bg-background" /><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{item.event}</span><time className="text-muted-foreground">{item.createdAt}</time></div><div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-muted-foreground"><span>Actor: {item.actor}</span><span>Domain: {item.domain}</span><span>State: <span className={statusClass(item.state)}>{item.state}</span></span></div>{item.detail !== "—" ? <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{item.detail}</p> : null}{item.href ? <Link href={item.href} className="mt-1 inline-flex items-center gap-1 text-primary hover:underline">Canonical source <ArrowRight className="size-3" /></Link> : null}</li>)}</ol> : <EmptyState>Activity eventはありません。</EmptyState>}</CardContent></Card></div>;
}

export function OperationsCommandCenter({ view = "overview", agentId }: { view?: OperationsCommandCenterView; agentId?: string }) {
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
  const panelDisabled = !autonomousCompanyPanelsEnabled && (view === "agents" || view === "work");
  const persistedQuery = queryString(searchParams);
  const stableSearchParams = useMemo(() => new URLSearchParams(persistedQuery), [persistedQuery]);
  const filters = useMemo(() => agentId ? { ...filtersFromSearchParams(stableSearchParams), scope: "agent" as const, agentId, spaceId: "", projectId: "" } : filtersFromSearchParams(stableSearchParams), [stableSearchParams, agentId]);
  const [snapshot, setSnapshot] = useState<CommandSnapshot>(EMPTY_SNAPSHOT);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    if (panelDisabled) {
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
        payload = await fetchJson(`/operations/command-center${suffix}`);
      } catch (firstError) {
        const status = numberValue((firstError as { status?: unknown })?.status);
        if (status !== 404) throw firstError;
        payload = await fetchJson(`/operations/overview${suffix}`);
      }
      setSnapshot(normaliseSnapshot(payload));
    } catch (loadError) {
      setSnapshot(EMPTY_SNAPSHOT);
      setError(loadError);
    } finally {
      setLoading(false);
    }
  }, [filters, panelDisabled]);

  useEffect(() => {
    void load();
  }, [load]);

  const updateFilter = useCallback((key: keyof CommandFilters, value: string) => {
    const next = new URLSearchParams(queryString(searchParams));
    if (key === "scope") {
      if (value && value !== "all") next.set("scope", value);
      else next.delete("scope");
      if (value !== "space") next.delete("space_id");
      if (value !== "project") next.delete("project_id");
      if (value !== "agent") next.delete("agent_id");
    } else {
      const queryKey = key === "spaceId" ? "space_id" : key === "projectId" ? "project_id" : key === "agentId" ? "agent_id" : key === "range" ? "range" : key;
      if (value) next.set(queryKey, value);
      else next.delete(queryKey);
    }
    if (view === "overview") next.delete("tab");
    else next.set("tab", view);
    const query = next.toString();
    router.replace(`${pathname || "/operations"}${query ? `?${query}` : ""}`, { scroll: false });
  }, [pathname, router, searchParams, view]);

  return (
    <div className="space-y-4" data-testid="operations-command-center" data-command-center-view={view}>
      <header className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">Operations / Command Center</p><h1 className="mt-1 text-2xl font-semibold tracking-tight">{view === "overview" ? "Overview" : view[0].toUpperCase() + view.slice(1)}</h1><p className="mt-1 max-w-3xl text-sm text-muted-foreground">会社全体のAgent、Work、Activityを既存のcanonical ledgerから安全に確認します。ここから権限や実行状態を直接変更することはありません。</p></div><div className="flex items-center gap-2 text-xs text-muted-foreground"><ShieldCheck className="size-4 text-emerald-600" /> Read-only projection</div></header>
      {agentId ? <Button type="button" variant="outline" size="sm" disabled={loading} onClick={() => void load()}>活動状態を更新</Button> : <FilterBar filters={filters} onChange={updateFilter} onRefresh={() => void load()} loading={loading} />}
      {panelDisabled ? <Card size="sm" data-testid="operations-command-center-disabled"><CardContent className="flex items-start gap-2 pt-4 text-sm text-muted-foreground"><ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-600" /><p>Company Agent runtimeは現在無効です。この画面では履歴を変更せず、手動のMediaOps / EngagementOpsだけを利用できます。</p></CardContent></Card> : null}
      <ErrorNotice error={error} />
      {loading && !snapshot.agents.length && !snapshot.work.length && !snapshot.activity.length ? <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Command Centerを読み込み中…</div> : null}
      {!panelDisabled && (!loading || snapshot.agents.length || snapshot.work.length || snapshot.activity.length) ? (view === "overview" ? <OverviewView snapshot={snapshot} /> : view === "agents" ? <AgentsView agents={snapshot.agents} /> : view === "work" ? <WorkView work={snapshot.work} /> : <ActivityView activity={snapshot.activity} />) : null}
    </div>
  );
}

export const __operationsCommandCenterTestUtils = {
  normaliseSnapshot,
  normaliseSummary,
  normaliseAgents,
  normaliseWork,
  normaliseActivity,
  normalizeSnapshot: normaliseSnapshot,
  normalizeSummary: normaliseSummary,
  normalizeAgents: normaliseAgents,
  normalizeWork: normaliseWork,
  normalizeActivity: normaliseActivity,
  filterQuery,
  queryWithTimeRange,
};

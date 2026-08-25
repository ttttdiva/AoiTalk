import {
  getTaskCommandVariants,
  normalizeCommandValue,
  resolveCommandCandidate,
  type CommandCandidate,
  type CommandCandidateSelection,
  type ValuePreviewFn,
} from "@/components/tasks/slash-command-input";
import { formatTaskDateLabel } from "@/lib/task-date-label";
import { parseLocalDateTime } from "@/lib/date-time";
import type { Project, Tag, Task } from "@/lib/task-api";

export function formatDateTimeLocal(d: Date): string {
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(
    d.getDate(),
  )}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function parseTaskDateValue(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = parseLocalDateTime(value) ?? new Date(value);
  return isNaN(date.getTime()) ? null : date;
}

export const TASK_SCHEDULE_ESTIMATE_MODE_KEY = "schedule_estimate_mode";

export function buildDraftTask(draftTask?: Partial<Task> | null): Task {
  const now = formatDateTimeLocal(new Date());
  return {
    id: "draft",
    project_id: draftTask?.project_id || "",
    title: draftTask?.title || "",
    description: draftTask?.description || null,
    status: draftTask?.status || "open",
    priority: draftTask?.priority || "medium",
    start_at: draftTask?.start_at || null,
    end_at: draftTask?.end_at || null,
    all_day: draftTask?.all_day === true,
    auto_close_on_due: draftTask?.auto_close_on_due === true,
    reminder_offsets: Array.isArray(draftTask?.reminder_offsets)
      ? draftTask.reminder_offsets
      : [],
    notifications_enabled: draftTask?.notifications_enabled !== false,
    source: draftTask?.source || "web",
    created_by: null,
    completed_at: null,
    created_at: now,
    updated_at: now,
    metadata: draftTask?.metadata || {},
    assignees: [],
    tags: draftTask?.tags || [],
    active_time_entry: null,
    estimated_hours: draftTask?.estimated_hours ?? null,
    total_time_seconds: draftTask?.total_time_seconds ?? 0,
    parent_task_id: draftTask?.parent_task_id || null,
  };
}

const WEEKDAY_ALIASES: Record<string, number> = {
  sunday: 0,
  sun: 0,
  monday: 1,
  mon: 1,
  tuesday: 2,
  tue: 2,
  tues: 2,
  wednesday: 3,
  wed: 3,
  thursday: 4,
  thu: 4,
  thur: 4,
  thurs: 4,
  friday: 5,
  fri: 5,
  saturday: 6,
  sat: 6,
};

const DATE_KEYWORD_ALIASES: Record<string, string[]> = {
  today: ["today", "tod"],
  tomorrow: ["tomorrow", "tomo", "tom"],
  sunday: ["sunday", "sun"],
  monday: ["monday", "mon"],
  tuesday: ["tuesday", "tues", "tue"],
  wednesday: ["wednesday", "wed"],
  thursday: ["thursday", "thurs", "thur", "thu"],
  friday: ["friday", "fri"],
  saturday: ["saturday", "sat"],
};

function resolveDateKeyword(input: string): string | null {
  const query = normalizeCommandValue(input);
  if (!query) return null;
  const entries = Object.entries(DATE_KEYWORD_ALIASES);
  const exact = entries.filter(([, aliases]) => aliases.includes(query));
  if (exact.length === 1) return exact[0][0];
  const prefixes = entries.filter(([, aliases]) =>
    aliases.some((alias) => alias.startsWith(query)),
  );
  return prefixes.length === 1 ? prefixes[0][0] : null;
}

const WEEKDAY_LABELS = [
  "Sunday",
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
];

export function parseFlexibleDate(raw: string): string | null {
  let lower = raw
    .trim()
    .toLowerCase()
    .replace(/,\s*/g, " ")
    .replace(/\s+/g, " ");
  if (!lower) return null;
  const now = new Date();

  const keywordMatch = lower.match(
    /^(next\s+)?([a-z]+)(?:\s+(\d{1,2}:\d{2}))?$/,
  );
  if (keywordMatch) {
    const keyword = resolveDateKeyword(keywordMatch[2]);
    const next = !!keywordMatch[1];
    if (keyword && (!next || Object.hasOwn(WEEKDAY_ALIASES, keyword))) {
      lower = `${next ? "next " : ""}${keyword}${
        keywordMatch[3] ? ` ${keywordMatch[3]}` : ""
      }`;
    }
  }

  if (lower === "today" || lower === "tod") {
    const d = new Date(now);
    d.setHours(0, 0, 0, 0);
    return formatDateTimeLocal(d);
  }
  if (lower === "tomorrow" || lower === "tomo" || lower === "tom") {
    const d = new Date(now);
    d.setDate(d.getDate() + 1);
    d.setHours(0, 0, 0, 0);
    return formatDateTimeLocal(d);
  }
  if (lower === "next week") {
    const d = new Date(now);
    d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
    d.setHours(0, 0, 0, 0);
    return formatDateTimeLocal(d);
  }
  if (lower === "next month") {
    const d = new Date(now.getFullYear(), now.getMonth() + 1, 1, 0, 0, 0);
    return formatDateTimeLocal(d);
  }

  const weekdayMatch = lower.match(
    /^(next\s+)?(sunday|sun|monday|mon|tuesday|tue|tues|wednesday|wed|thursday|thu|thur|thurs|friday|fri|saturday|sat)(?:\s+(\d{1,2}):(\d{2}))?$/,
  );
  if (weekdayMatch) {
    const targetDay = WEEKDAY_ALIASES[weekdayMatch[2]];
    let daysUntil = (targetDay - now.getDay() + 7) % 7;
    if (weekdayMatch[1] && daysUntil === 0) daysUntil = 7;
    const d = new Date(now);
    d.setDate(now.getDate() + daysUntil);
    d.setHours(
      weekdayMatch[3] ? parseInt(weekdayMatch[3]) : 0,
      weekdayMatch[4] ? parseInt(weekdayMatch[4]) : 0,
      0,
      0,
    );
    return formatDateTimeLocal(d);
  }

  const todayTimeMatch = lower.match(/^(?:today|tod)\s+(\d{1,2}):(\d{2})$/);
  if (todayTimeMatch) {
    const d = new Date(now);
    d.setHours(parseInt(todayTimeMatch[1]), parseInt(todayTimeMatch[2]), 0, 0);
    return formatDateTimeLocal(d);
  }

  const tomoTimeMatch = lower.match(
    /^(?:tomorrow|tomo|tom)\s+(\d{1,2}):(\d{2})$/,
  );
  if (tomoTimeMatch) {
    const d = new Date(now);
    d.setDate(d.getDate() + 1);
    d.setHours(parseInt(tomoTimeMatch[1]), parseInt(tomoTimeMatch[2]), 0, 0);
    return formatDateTimeLocal(d);
  }

  const dateMatch = lower.match(
    /^(\d{1,2})\/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$/,
  );
  if (dateMatch) {
    const month = parseInt(dateMatch[1]) - 1;
    const day = parseInt(dateMatch[2]);
    const hour = dateMatch[3] ? parseInt(dateMatch[3]) : 0;
    const min = dateMatch[4] ? parseInt(dateMatch[4]) : 0;
    const d = new Date(now.getFullYear(), month, day, hour, min, 0);
    return formatDateTimeLocal(d);
  }

  const isoDateMatch = lower.match(
    /^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ t](\d{1,2}):(\d{2}))?$/,
  );
  if (isoDateMatch) {
    const year = parseInt(isoDateMatch[1]);
    const month = parseInt(isoDateMatch[2]) - 1;
    const day = parseInt(isoDateMatch[3]);
    const hour = isoDateMatch[4] ? parseInt(isoDateMatch[4]) : 0;
    const min = isoDateMatch[5] ? parseInt(isoDateMatch[5]) : 0;
    return formatDateTimeLocal(new Date(year, month, day, hour, min, 0));
  }

  const timeOnly = lower.match(/^(\d{1,2}):(\d{2})$/);
  if (timeOnly) {
    const d = new Date(now);
    d.setHours(parseInt(timeOnly[1]), parseInt(timeOnly[2]), 0, 0);
    return formatDateTimeLocal(d);
  }

  const parsed = parseTaskDateValue(raw.trim());
  return parsed ? formatDateTimeLocal(parsed) : null;
}

function hasExplicitTime(raw: string): boolean {
  return /(^|\s)\d{1,2}:\d{2}$/.test(raw.trim());
}

export function hasNonMidnightTime(value: string | null | undefined): boolean {
  if (!value) return false;
  const d = parseTaskDateValue(value);
  if (!d) return false;
  return d.getHours() !== 0 || d.getMinutes() !== 0;
}

function buildSlashCommandRegex(command: string, global = false): RegExp {
  const variants = getTaskCommandVariants(command)
    .sort((a, b) => b.length - a.length)
    .map((variant) => variant.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|");
  return new RegExp(
    `(^|\\s)(?:${variants})\\s+(.+?)(?=\\s+\\/[a-z][a-z0-9_-]*\\b|$)`,
    global ? "ig" : "i",
  );
}

type SlashCommandRange = {
  start: number;
  end: number;
  hasLeadingSpace: boolean;
};

function removeSlashCommandRanges(
  value: string,
  ranges: SlashCommandRange[],
): string {
  if (ranges.length === 0) return value;

  const sorted = [...ranges].sort((a, b) => a.start - b.start);
  let result = "";
  let cursor = 0;

  for (const range of sorted) {
    if (range.start < cursor) continue;
    result += value.slice(cursor, range.start);
    if (range.hasLeadingSpace) result += " ";
    cursor = range.end;
  }

  result += value.slice(cursor);
  return result;
}

export function normalizeStatus(raw: string): string {
  const map: Record<string, string> = {
    open: "open",
    todo: "open",
    new: "open",
    in: "in_progress",
    in_progress: "in_progress",
    progress: "in_progress",
    ip: "in_progress",
    wip: "in_progress",
    on_hold: "on_hold",
    hold: "on_hold",
    pending: "on_hold",
    pause: "on_hold",
    paused: "on_hold",
    review: "review",
    reviewing: "review",
    check: "review",
    done: "closed",
    completed: "closed",
    complete: "closed",
    close: "closed",
    closed: "closed",
  };
  return map[raw.toLowerCase()] || raw;
}

const STATUS_ALIASES: Record<string, string[]> = {
  open: ["open", "todo", "new"],
  in_progress: ["in_progress", "in", "progress", "ip", "wip"],
  on_hold: ["on_hold", "hold", "pending", "pause", "paused"],
  review: ["review", "reviewing", "check"],
  closed: ["closed", "done", "completed", "complete", "close"],
};

function resolveAliasedValue(
  raw: string,
  aliasesByValue: Record<string, string[]>,
): string | null {
  const query = normalizeCommandValue(raw).replace(/ /g, "_");
  if (!query) return null;
  const entries = Object.entries(aliasesByValue);
  const exact = entries.filter(([, aliases]) => aliases.includes(query));
  if (exact.length === 1) return exact[0][0];
  const prefixes = entries.filter(([, aliases]) =>
    aliases.some((alias) => alias.startsWith(query)),
  );
  return prefixes.length === 1 ? prefixes[0][0] : null;
}

export function resolveTaskStatus(raw: string): string | null {
  return resolveAliasedValue(raw, STATUS_ALIASES);
}

const PRIORITY_VALUES = ["urgent", "high", "medium", "low", "none"];

export function resolveTaskPriority(raw: string): string | null {
  const query = normalizeCommandValue(raw);
  if (!query) return null;
  const exact = PRIORITY_VALUES.find((value) => value === query);
  if (exact) return exact;
  const prefixes = PRIORITY_VALUES.filter((value) => value.startsWith(query));
  return prefixes.length === 1 ? prefixes[0] : null;
}

export function normalizeTaskTitle(value: string): string | null {
  const title = value.trim();
  if (!title) return null;
  if (title === "無題のタスク" || title === "Untitled task") return null;
  return title;
}

export interface SlashCommandPatches {
  status?: string;
  priority?: string;
  startAt?: string;
  endAt?: string;
  endAtDateOnly?: boolean;
  startAtDateOnly?: boolean;
  tagNames?: string[];
  moveToProject?: string;
}

export function parseSlashCommands(
  input: string,
  options?: {
    preserveTrailingSpace?: boolean;
    projects?: Project[];
    selection?: CommandCandidateSelection;
  },
): {
  title: string;
  patches: SlashCommandPatches;
} {
  const preserveTrailingSpace = !!options?.preserveTrailingSpace;
  const patches: SlashCommandPatches = {};
  const normalizeTitle = (value: string) => {
    const collapsed = value.replace(/ {2,}/g, " ").replace(/^\s+/, "");
    return preserveTrailingSpace ? collapsed : collapsed.trim();
  };
  let title = preserveTrailingSpace ? input.replace(/^\s+/, "") : input.trim();

  const dateCommandRanges: SlashCommandRange[] = [];
  for (const command of ["/start", "/due"] as const) {
    const regex = buildSlashCommandRegex(command, true);
    for (const match of title.matchAll(regex)) {
      if (match.index === undefined) continue;
      const rawVal = match[2].trim();
      const parsed = parseFlexibleDate(rawVal);
      if (parsed) {
        if (command === "/start") {
          patches.startAt = parsed;
          patches.startAtDateOnly = !hasExplicitTime(rawVal);
        } else {
          patches.endAt = parsed;
          patches.endAtDateOnly = !hasExplicitTime(rawVal);
        }
      }
      if (parsed) {
        dateCommandRanges.push({
          start: match.index,
          end: match.index + match[0].length,
          hasLeadingSpace: !!match[1],
        });
      }
    }
  }
  if (dateCommandRanges.length > 0) {
    title = normalizeTitle(removeSlashCommandRanges(title, dateCommandRanges));
  }

  const statusMatch = title.match(buildSlashCommandRegex("/status"));
  if (statusMatch) {
    const resolved = resolveTaskStatus(statusMatch[2]);
    if (resolved) {
      patches.status = resolved;
      title = normalizeTitle(
        title.replace(statusMatch[0], statusMatch[1] ? " " : ""),
      );
    }
  }

  const priorityMatch = title.match(buildSlashCommandRegex("/priority"));
  if (priorityMatch) {
    const resolved = resolveTaskPriority(priorityMatch[2]);
    if (resolved) {
      patches.priority = resolved;
      title = normalizeTitle(
        title.replace(priorityMatch[0], priorityMatch[1] ? " " : ""),
      );
    }
  }

  const tagNames: string[] = [];
  const tagRegex = buildSlashCommandRegex("/t", true);
  for (const tagMatch of title.matchAll(tagRegex)) {
    tagNames.push(tagMatch[2].trim());
  }
  if (tagNames.length > 0) {
    patches.tagNames = tagNames;
    title = normalizeTitle(title.replace(tagRegex, " "));
  }

  const moveMatch = title.match(buildSlashCommandRegex("/m"));
  if (moveMatch) {
    const selectedProjectId =
      options?.selection?.command === "/m"
        ? options.selection.candidate.projectId
        : undefined;
    const project = selectedProjectId
      ? options?.projects?.find((item) => item.id === selectedProjectId)
      : findTaskProjectMoveTarget(options?.projects, moveMatch[2]);
    if (project) {
      patches.moveToProject = project.id;
      title = normalizeTitle(
        title.replace(moveMatch[0], moveMatch[1] ? " " : ""),
      );
    }
  }

  return { title: normalizeTitle(title), patches };
}

const STATUS_LABELS: Record<string, string> = {
  open: "未着手",
  in_progress: "進行中",
  on_hold: "保留",
  review: "確認待ち",
  closed: "完了",
};

const PRIORITY_LABELS: Record<string, string> = {
  urgent: "Urgent",
  high: "High",
  medium: "Medium",
  low: "Low",
  none: "None",
};

const DATE_KEYWORD_LABELS: Record<string, string> = {
  today: "Today",
  tomorrow: "Tomorrow",
  sunday: "Sunday",
  monday: "Monday",
  tuesday: "Tuesday",
  wednesday: "Wednesday",
  thursday: "Thursday",
  friday: "Friday",
  saturday: "Saturday",
};

const DATE_KEYWORDS = Object.keys(DATE_KEYWORD_LABELS);

function resolveKeywordPrefix(input: string): string | null {
  if (!input) return null;
  const exact = DATE_KEYWORD_LABELS[input];
  if (exact) return exact;
  const matches = DATE_KEYWORDS.filter((k) => k.startsWith(input));
  if (matches.length === 1) return DATE_KEYWORD_LABELS[matches[0]];
  return null;
}

function getDateKeywordCompletion(rawValue: string): string | null {
  const lower = normalizeCommandValue(rawValue);

  if (!lower) return null;

  const withTimeMatch = lower.match(
    /^(next\s+)?([a-z]+)(?:\s+(\d{1,2}:\d{2}))?$/,
  );
  if (!withTimeMatch) return null;

  const nextPrefix = withTimeMatch[1] ? "Next " : "";
  const keywordLabel = resolveKeywordPrefix(withTimeMatch[2]);
  if (!keywordLabel) return null;

  if (
    withTimeMatch[1] &&
    keywordLabel !== "Today" &&
    keywordLabel !== "Tomorrow"
  ) {
    const base = `${nextPrefix}${keywordLabel}`;
    return withTimeMatch[3] ? `${base} ${withTimeMatch[3]}` : base;
  }
  if (withTimeMatch[1]) return null;

  return withTimeMatch[3]
    ? `${keywordLabel} ${withTimeMatch[3]}`
    : keywordLabel;
}

export const taskValuePreview: ValuePreviewFn = (command, rawValue) => {
  const val = rawValue.trim();
  if (!val) return null;

  switch (command) {
    case "/due":
    case "/start": {
      const parsed = parseFlexibleDate(val);
      const dateOnly = !hasExplicitTime(val);
      return parsed
        ? formatTaskDateLabel(parsed, {
            allDay: dateOnly,
            absoluteStyle: "long",
          })
        : null;
    }
    case "/status": {
      const normalized = resolveTaskStatus(val);
      return normalized ? STATUS_LABELS[normalized] : null;
    }
    case "/priority": {
      const resolved = resolveTaskPriority(val);
      return resolved ? PRIORITY_LABELS[resolved] : null;
    }
    case "/t":
      return `🏷️ ${val}`;
    case "/m":
      return `📁 ${val}`;
    default:
      return null;
  }
};

export const taskValueCompletion: ValuePreviewFn = (command, rawValue) => {
  const val = rawValue.trim();
  if (!val) return null;

  switch (command) {
    case "/due":
    case "/start":
      return getDateKeywordCompletion(val);
    default:
      return null;
  }
};

export function applyStartTimeToEndAt(
  endAt: string,
  endAtDateOnly: boolean,
  startAt: string | null | undefined,
): string {
  if (!endAtDateOnly || !startAt || !startAt.includes("T")) return endAt;
  const startDate = parseTaskDateValue(startAt);
  if (!startDate) return endAt;
  const startH = startDate.getHours();
  const startM = startDate.getMinutes();
  if (startH === 0 && startM === 0) return endAt;
  const dueDate = parseTaskDateValue(endAt);
  if (!dueDate) return endAt;
  dueDate.setHours(startH + 1, startM, 0, 0);
  return formatDateTimeLocal(dueDate);
}

function normalizeTaskMetadata(
  metadata: Record<string, unknown> | null | undefined,
): Record<string, unknown> {
  if (!metadata || Array.isArray(metadata)) return {};
  return { ...metadata };
}

export function inferTaskScheduleEstimateMode(
  estimatedHours: number | null | undefined,
  metadata: Record<string, unknown> | null | undefined,
): "auto" | "manual" {
  const rawMode =
    normalizeTaskMetadata(metadata)[TASK_SCHEDULE_ESTIMATE_MODE_KEY];
  return rawMode === "auto" || rawMode === "manual"
    ? rawMode
    : estimatedHours == null
      ? "auto"
      : "manual";
}

export function computeEstimatedHoursFromSchedule(
  startAt: string | null | undefined,
  endAt: string | null | undefined,
  allDay = false,
): number | null {
  if (allDay) return null;
  if (!startAt || !endAt) return null;
  const start = parseTaskDateValue(startAt);
  const end = parseTaskDateValue(endAt);
  if (!start || !end || end <= start) {
    return null;
  }
  return Math.round(((end.getTime() - start.getTime()) / 3600000) * 100) / 100;
}

export function buildAutoEstimateTaskPatch({
  startAt,
  endAt,
  allDay = false,
  currentEstimatedHours,
  currentMetadata,
  forceAuto = false,
}: {
  startAt: string | null | undefined;
  endAt: string | null | undefined;
  allDay?: boolean;
  currentEstimatedHours: number | null | undefined;
  currentMetadata: Record<string, unknown> | null | undefined;
  forceAuto?: boolean;
}): Record<string, unknown> {
  const mode = forceAuto
    ? "auto"
    : inferTaskScheduleEstimateMode(currentEstimatedHours, currentMetadata);
  if (mode !== "auto") return {};
  return {
    estimated_hours: computeEstimatedHoursFromSchedule(startAt, endAt, allDay),
    metadata: {
      ...normalizeTaskMetadata(currentMetadata),
      [TASK_SCHEDULE_ESTIMATE_MODE_KEY]: "auto",
    },
  };
}

export function buildManualEstimateTaskPatch({
  estimatedHours,
  currentMetadata,
}: {
  estimatedHours: number | null;
  currentMetadata: Record<string, unknown> | null | undefined;
}): Record<string, unknown> {
  return {
    estimated_hours: estimatedHours,
    metadata: {
      ...normalizeTaskMetadata(currentMetadata),
      [TASK_SCHEDULE_ESTIMATE_MODE_KEY]: "manual",
    },
  };
}

export function buildTaskCommandCandidates({
  projects,
  projectSpaceNames,
  tags,
  selectedTagIds,
}: {
  projects?: Project[];
  projectSpaceNames?: ReadonlyMap<string, string>;
  tags: Tag[];
  selectedTagIds: string[];
}): Record<string, CommandCandidate[]> | undefined {
  const result: Record<string, CommandCandidate[]> = {};

  if (projects && projects.length > 0) {
    const uniqueProjects = new Map<string, Project>();
    for (const project of projects) {
      if (project.can_write === false || uniqueProjects.has(project.id)) {
        continue;
      }
      uniqueProjects.set(project.id, project);
    }

    result["/m"] = Array.from(uniqueProjects.values()).map(
      (project): CommandCandidate => {
        const aliasHint =
          project.aliases && project.aliases.length > 0
            ? ` (${project.aliases.join(", ")})`
            : "";
        const spaceName = project.space_id
          ? projectSpaceNames?.get(project.space_id)
          : undefined;
        const projectLabel = spaceName
          ? `${spaceName} / ${project.name}`
          : project.name;
        const keywords = [
          project.name,
          project.slug,
          ...(project.aliases || []),
        ];
        return {
          value: project.name,
          label: projectLabel + aliasHint,
          projectId: project.id,
          keywords,
        };
      },
    );
  }

  if (tags.length > 0) {
    result["/t"] = tags.map((tag) => ({
      value: tag.name,
      label: tag.name,
      color: tag.color || "#6B7280",
      checked: selectedTagIds.includes(tag.id),
    }));
  }

  result["/status"] = [
    { value: "open", label: "open (未着手)", keywords: STATUS_ALIASES.open },
    {
      value: "in_progress",
      label: "in_progress (進行中)",
      keywords: STATUS_ALIASES.in_progress,
    },
    {
      value: "on_hold",
      label: "on_hold (保留)",
      keywords: STATUS_ALIASES.on_hold,
    },
    {
      value: "review",
      label: "review (確認待ち)",
      keywords: STATUS_ALIASES.review,
    },
    {
      value: "closed",
      label: "closed (完了)",
      keywords: STATUS_ALIASES.closed,
    },
  ];

  result["/priority"] = [
    { value: "urgent", label: "urgent (緊急)" },
    { value: "high", label: "high (高)" },
    { value: "medium", label: "medium (中)" },
    { value: "low", label: "low (低)" },
  ];

  return Object.keys(result).length > 0 ? result : undefined;
}

export function findTaskProjectMoveTarget(
  projects: Project[] | undefined,
  rawValue: string,
): Project | undefined {
  if (!projects || projects.length === 0) return undefined;
  const candidates = projects
    .filter((project) => project.can_write !== false)
    .map(
      (project): CommandCandidate => ({
        value: project.name,
        projectId: project.id,
        keywords: [project.name, project.slug, ...(project.aliases || [])],
      }),
    );
  const resolved = resolveCommandCandidate(candidates, rawValue);
  return resolved?.projectId
    ? projects.find((project) => project.id === resolved.projectId)
    : undefined;
}

export interface TaskSlashCommandFormPatch {
  title: string;
  status?: string;
  priority?: string;
  startAt?: string;
  endAt?: string;
  allDay?: boolean;
  targetProjectId?: string;
  tagNames?: string[];
}

export function buildTaskSlashCommandFormPatch({
  text,
  currentStartAt,
  currentEndAt,
  projects,
  preserveTrailingSpace = false,
  selection,
}: {
  text: string;
  currentStartAt: string | null | undefined;
  currentEndAt: string | null | undefined;
  projects?: Project[];
  preserveTrailingSpace?: boolean;
  selection?: CommandCandidateSelection;
}): TaskSlashCommandFormPatch {
  const { title, patches } = parseSlashCommands(text, {
    preserveTrailingSpace,
    projects,
    selection,
  });
  const resolvedStartAt = patches.startAt || currentStartAt || null;
  const resolvedEndAt = patches.endAt
    ? applyStartTimeToEndAt(
        patches.endAt,
        !!patches.endAtDateOnly,
        resolvedStartAt,
      )
    : (currentEndAt ?? null);
  const nextEndAt = patches.endAt
    ? (resolvedEndAt ?? patches.endAt)
    : undefined;

  return {
    title,
    status: patches.status,
    priority: patches.priority,
    startAt: patches.startAt,
    endAt: nextEndAt,
    allDay:
      patches.endAtDateOnly || patches.startAtDateOnly
        ? !hasNonMidnightTime(resolvedStartAt) &&
          !hasNonMidnightTime(resolvedEndAt)
        : undefined,
    targetProjectId: patches.moveToProject,
    tagNames: patches.tagNames,
  };
}

export async function resolveTaskTagIds({
  tagNames,
  currentTagIds,
  availableTags,
  createTag,
}: {
  tagNames: string[];
  currentTagIds: string[];
  availableTags: Tag[];
  createTag: (name: string) => Promise<Tag | null>;
}): Promise<{ tagIds: string[]; createdTags: Tag[] }> {
  const nextTagIds = new Set(currentTagIds);
  const knownTags = [...availableTags];
  const createdTags: Tag[] = [];
  const uniqueNames = tagNames.reduce<string[]>((acc, rawName) => {
    const name = rawName.trim();
    if (!name) return acc;
    if (
      acc.some((current) => current.toLowerCase() === name.toLowerCase())
    ) {
      return acc;
    }
    return [...acc, name];
  }, []);

  for (const name of uniqueNames) {
    let tag: Tag | null | undefined = knownTags.find(
      (current) => current.name.toLowerCase() === name.toLowerCase(),
    );
    if (!tag) {
      tag = await createTag(name);
      if (tag) {
        knownTags.push(tag);
        createdTags.push(tag);
      }
    }
    if (!tag) continue;
    if (nextTagIds.has(tag.id)) {
      nextTagIds.delete(tag.id);
    } else {
      nextTagIds.add(tag.id);
    }
  }

  return { tagIds: Array.from(nextTagIds), createdTags };
}

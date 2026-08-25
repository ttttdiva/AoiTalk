import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import * as XLSX from "xlsx";

type JsonRecord = Record<string, unknown>;

export type ProjectManagementConfig = {
  workspaceRoot: string | null;
  wbsFile: string | null;
  issueFile: string | null;
  riskFile: string | null;
  requestFiles: string[];
  taskRules: {
    autoCreateFollowup: boolean;
    autoCreateDueTask: boolean;
    requireConfirmationForWbsChange: boolean;
  };
};

export type WbsRow = {
  sourceKey: string;
  rowHash: string;
  filePath: string;
  sheetName: string;
  rowNumber: number;
  wbsId: string | null;
  title: string;
  description: string | null;
  assignee: string | null;
  status: "open" | "in_progress" | "on_hold" | "review" | "closed";
  priority: "low" | "medium" | "high" | "urgent";
  plannedStart: string | null;
  plannedEnd: string | null;
  actualStart: string | null;
  actualEnd: string | null;
  progress: number | null;
  requestText: string | null;
  raw: JsonRecord;
};

export type WorkspaceRequestItem = {
  title: string;
  target: "customer" | "internal" | "vendor" | "unknown";
  reason: string;
  sourceType: "wbs" | "issue" | "risk" | "file";
  sourcePath: string;
  sourceRef: string;
  dueAt: string | null;
  status: "draft" | "waiting" | "blocked";
};

const MANAGEMENT_DEFAULTS = {
  autoCreateFollowup: true,
  autoCreateDueTask: false,
  requireConfirmationForWbsChange: true,
};

const PROJECT_STORAGE_PREFIX_RE = /^_projects\/project_([^/]+)\/?/i;

const HEADER_ALIASES: Record<string, string[]> = {
  wbsId: ["wbs", "wbs番号", "id", "no", "番号", "#"],
  title: [
    "タスク名",
    "作業項目",
    "作業内容",
    "項目",
    "件名",
    "task",
    "name",
    "title",
  ],
  description: ["説明", "備考", "内容", "詳細", "description", "note"],
  assignee: ["担当", "担当者", "担当部署", "assignee", "owner"],
  status: ["状態", "ステータス", "status", "状況"],
  plannedStart: [
    "予定開始日",
    "開始予定",
    "開始日",
    "計画開始",
    "planned start",
    "start",
  ],
  plannedEnd: [
    "予定終了日",
    "終了予定",
    "終了日",
    "期限",
    "期日",
    "due",
    "planned end",
    "end",
  ],
  actualStart: ["実績開始日", "実開始", "actual start"],
  actualEnd: ["実績終了日", "実終了", "完了日", "actual end"],
  progress: ["進捗率", "進捗", "progress", "%"],
  requestText: ["確認事項", "要確認", "依頼事項", "顧客確認", "確認先", "qa"],
};

const SKIP_DIRS = new Set([
  ".git",
  ".next",
  "node_modules",
  "venv",
  ".venv",
  "__pycache__",
  "cache",
  "logs",
  "dist",
  "build",
  "android",
  "ios",
]);

function asRecord(value: unknown): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return { ...(value as JsonRecord) };
}

function cleanString(value: unknown): string | null {
  const text = String(value ?? "").trim();
  return text || null;
}

function normalizePath(value: unknown): string | null {
  const text = cleanString(value);
  if (!text) return null;
  return text.replace(/\\/g, "/").replace(/\/+$/, "");
}

function getRepoRoot(): string {
  const cwd = process.cwd();
  return path.basename(cwd) === "frontend"
    ? path.resolve(/*turbopackIgnore: true*/ cwd, "..")
    : cwd;
}

export function getWorkspacesBaseDir(): string {
  const configured = process.env.AOITALK_WORKSPACES_DIR || "workspaces";
  const projectRoot = process.env.AOITALK_PROJECT_ROOT || getRepoRoot();
  return path.resolve(
    path.isAbsolute(configured)
      ? configured
      : path.join(/*turbopackIgnore: true*/ projectRoot, configured),
  );
}

export function getProjectStoragePath(projectId: string): string {
  return `_projects/project_${projectId}`;
}

export function getProjectStorageRoot(projectId: string): string {
  return path.resolve(
    /*turbopackIgnore: true*/ getWorkspacesBaseDir(),
    getProjectStoragePath(projectId),
  );
}

export type ProjectStorageUsage = {
  totalBytes: number;
  totalMb: number;
  fileCount: number;
  dirCount: number;
};

const MAX_PROJECT_STORAGE_SCAN_ENTRIES = 100_000;

function isStorageLink(stat: fs.Stats): boolean {
  if (stat.isSymbolicLink()) return true;
  const attributes = (stat as fs.Stats & { st_file_attributes?: number })
    .st_file_attributes;
  return typeof attributes === "number" && (attributes & 0x400) !== 0;
}

/**
 * Calculate usage without following symlinks/junctions/reparse points.
 *
 * This intentionally fails closed on an unreadable or changing workspace so
 * callers cannot turn a scan error into an unbounded quota bypass.
 */
export function calculateProjectStorageUsage(
  storageRoot: string,
): ProjectStorageUsage {
  const root = path.resolve(storageRoot);
  let rootStat: fs.Stats;
  try {
    rootStat = fs.lstatSync(root);
  } catch (error) {
    throw new Error(`Project storage root could not be inspected: ${root}`, {
      cause: error,
    });
  }
  if (isStorageLink(rootStat) || !rootStat.isDirectory()) {
    throw new Error("Project storage root is not a real directory");
  }

  const pending = [root];
  let scannedEntries = 0;
  let totalBytes = 0;
  let fileCount = 0;
  let dirCount = 0;

  while (pending.length > 0) {
    const current = pending.pop()!;
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch (error) {
      throw new Error(`Project storage directory could not be scanned: ${current}`, {
        cause: error,
      });
    }

    for (const entry of entries) {
      scannedEntries += 1;
      if (scannedEntries > MAX_PROJECT_STORAGE_SCAN_ENTRIES) {
        throw new Error("Project storage contains too many entries to scan safely");
      }

      const candidate = path.join(current, entry.name);
      let itemStat: fs.Stats;
      try {
        itemStat = fs.lstatSync(candidate);
      } catch (error) {
        throw new Error(`Project storage entry could not be inspected: ${candidate}`, {
          cause: error,
        });
      }
      if (isStorageLink(itemStat)) continue;

      if (itemStat.isDirectory()) {
        pending.push(candidate);
        dirCount += 1;
      } else if (itemStat.isFile()) {
        totalBytes += itemStat.size;
        fileCount += 1;
      }
    }
  }

  return {
    totalBytes,
    totalMb: Number((totalBytes / (1024 * 1024)).toFixed(2)),
    fileCount,
    dirCount,
  };
}

export function normalizeProjectFilePath(
  value: unknown,
  projectId?: string,
): string | null {
  let text = normalizePath(value);
  if (!text) return null;

  if (projectId) {
    const storagePrefix = `${getProjectStoragePath(projectId)}/`;
    if (text === getProjectStoragePath(projectId)) return null;
    if (text.startsWith(storagePrefix)) {
      text = text.slice(storagePrefix.length);
    } else if (text.startsWith("_projects/project_")) {
      return null;
    }
  } else {
    const match = text.match(PROJECT_STORAGE_PREFIX_RE);
    if (match) {
      text = text.slice(match[0].length);
    }
  }

  if (
    /^[A-Za-z]:\//.test(text) ||
    text.startsWith("/") ||
    text.startsWith("//")
  ) {
    return null;
  }

  const normalized = path.posix.normalize(text).replace(/^\/+/, "");
  if (
    !normalized ||
    normalized === "." ||
    normalized === ".." ||
    normalized.startsWith("../")
  ) {
    return null;
  }
  return normalized;
}

export function ensureProjectStorageRoot(projectId: string): string {
  const root = getProjectStorageRoot(projectId);
  const base = getWorkspacesBaseDir();
  fs.mkdirSync(base, { recursive: true });
  if (!isPathInsideProjectStorageRoot(base, root)) {
    throw new Error("Project storage root crosses a symlink or escapes the workspace");
  }
  fs.mkdirSync(root, { recursive: true });
  if (!isPathInsideProjectStorageRoot(base, root)) {
    throw new Error("Project storage root changed outside the workspace");
  }
  const relative = path.relative(fs.realpathSync(base), fs.realpathSync(root));
  if (
    relative === "" ||
    relative.startsWith("..") ||
    path.isAbsolute(relative) ||
    path.basename(relative).toLowerCase() !== `project_${projectId}`.toLowerCase()
  ) {
    throw new Error("Project storage root escapes the workspace");
  }
  return root;
}

/** Return false when a project-relative path resolves through a symlink. */
export function isPathInsideProjectStorageRoot(
  storageRoot: string,
  candidate: string,
): boolean {
  const root = path.resolve(storageRoot);
  const lexical = path.resolve(candidate);
  const lexicalRelative = path.relative(root, lexical);
  if (
    lexicalRelative.startsWith("..") ||
    path.isAbsolute(lexicalRelative)
  ) {
    return false;
  }

  let cursor = root;
  let lastExisting = root;
  try {
    if (isStorageLink(fs.lstatSync(root))) return false;
  } catch {
    return false;
  }

  // Inspect every existing lexical component with lstat.  This catches a
  // dangling symlink that existsSync would incorrectly treat as absent.
  for (const component of lexicalRelative
    ? lexicalRelative.split(path.sep).filter(Boolean)
    : []) {
    cursor = path.join(cursor, component);
    try {
      if (isStorageLink(fs.lstatSync(cursor))) return false;
      lastExisting = cursor;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "ENOENT" || code === "ENOTDIR") break;
      return false;
    }
  }

  let realRoot: string;
  let realExisting: string;
  try {
    realRoot = fs.realpathSync(root);
    realExisting = fs.realpathSync(lastExisting);
  } catch {
    return false;
  }
  const relative = path.relative(realRoot, realExisting);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function coerceBool(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    const lowered = value.trim().toLowerCase();
    if (["1", "true", "yes", "on"].includes(lowered)) return true;
    if (["0", "false", "no", "off"].includes(lowered)) return false;
  }
  return value == null ? fallback : Boolean(value);
}

function normalizeList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [
    ...new Set(
      value
        .map((item) => normalizeProjectFilePath(item))
        .filter((item): item is string => !!item),
    ),
  ];
}

export function normalizeProjectManagementConfig(
  metadata: unknown,
): ProjectManagementConfig {
  const root = asRecord(metadata);
  const links = asRecord(root.links);
  const management = asRecord(root.management);
  const taskRules = asRecord(management.task_rules ?? root.task_rules);

  return {
    workspaceRoot: normalizePath(links.workspace_root ?? root.workspace_root),
    wbsFile: normalizeProjectFilePath(management.wbs_file ?? root.wbs_file),
    issueFile: normalizeProjectFilePath(
      management.issue_file ?? root.issue_file,
    ),
    riskFile: normalizeProjectFilePath(management.risk_file ?? root.risk_file),
    requestFiles: normalizeList(management.request_files ?? root.request_files),
    taskRules: {
      autoCreateFollowup: coerceBool(
        taskRules.auto_create_followup,
        MANAGEMENT_DEFAULTS.autoCreateFollowup,
      ),
      autoCreateDueTask: coerceBool(
        taskRules.auto_create_due_task,
        MANAGEMENT_DEFAULTS.autoCreateDueTask,
      ),
      requireConfirmationForWbsChange: coerceBool(
        taskRules.require_confirmation_for_wbs_change,
        MANAGEMENT_DEFAULTS.requireConfirmationForWbsChange,
      ),
    },
  };
}

export function mergeManagementConfigIntoMetadata(
  metadata: unknown,
  config: Partial<ProjectManagementConfig>,
): JsonRecord {
  const root = asRecord(metadata);
  const links = asRecord(root.links);
  const management = asRecord(root.management);
  const activeManagement = { ...management };
  delete activeManagement.status_file;
  const existing = normalizeProjectManagementConfig(root);
  const next = { ...existing, ...config };

  return {
    ...root,
    schema_version: Number(root.schema_version ?? 1),
    links: {
      ...links,
      workspace_root: null,
    },
    management: {
      ...activeManagement,
      wbs_file: normalizeProjectFilePath(next.wbsFile),
      issue_file: normalizeProjectFilePath(next.issueFile),
      risk_file: normalizeProjectFilePath(next.riskFile),
      request_files: [
        ...new Set(
          (next.requestFiles ?? existing.requestFiles)
            .map((item) => normalizeProjectFilePath(item))
            .filter((item): item is string => !!item),
        ),
      ],
      task_rules: {
        auto_create_followup: next.taskRules?.autoCreateFollowup,
        auto_create_due_task: next.taskRules?.autoCreateDueTask,
        require_confirmation_for_wbs_change:
          next.taskRules?.requireConfirmationForWbsChange,
      },
    },
  };
}

export function resolveManagedPath(
  projectId: string,
  candidate: string | null,
): { absolutePath: string; relativePath: string } | null {
  const relativePath = normalizeProjectFilePath(candidate, projectId);
  if (!relativePath) return null;

  const storageRoot = getProjectStorageRoot(projectId);
  const resolved = path.resolve(
    storageRoot,
    relativePath.replace(/\//g, path.sep),
  );
  const relative = path.relative(storageRoot, resolved);
  if (
    relative === "" ||
    (!relative.startsWith("..") && !path.isAbsolute(relative))
  ) {
    if (!isPathInsideProjectStorageRoot(storageRoot, resolved)) return null;
    return { absolutePath: resolved, relativePath };
  }
  return null;
}

function normalizeHeader(value: unknown): string {
  return String(value ?? "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ");
}

function detectHeader(rows: unknown[][]): {
  headerIndex: number;
  columns: Partial<Record<keyof typeof HEADER_ALIASES, number>>;
} | null {
  let best: {
    headerIndex: number;
    score: number;
    columns: Partial<Record<string, number>>;
  } | null = null;
  const limit = Math.min(rows.length, 20);

  for (let rowIndex = 0; rowIndex < limit; rowIndex += 1) {
    const row = rows[rowIndex] ?? [];
    const columns: Partial<Record<string, number>> = {};
    let score = 0;
    for (const [field, aliases] of Object.entries(HEADER_ALIASES)) {
      const colIndex = row.findIndex((cell) => {
        const header = normalizeHeader(cell);
        return aliases.some((alias) => header === normalizeHeader(alias));
      });
      if (colIndex >= 0) {
        columns[field] = colIndex;
        score += field === "title" ? 3 : 1;
      }
    }
    if (!best || score > best.score) {
      best = { headerIndex: rowIndex, score, columns };
    }
  }

  if (!best || best.score < 3 || best.columns.title == null) return null;
  return {
    headerIndex: best.headerIndex,
    columns: best.columns as Partial<
      Record<keyof typeof HEADER_ALIASES, number>
    >,
  };
}

type LayeredWbsHeader = {
  headerIndex: number;
  firstDataIndex: number;
  titleColumns: number[];
  assigneeColumns: { index: number; label: string | null }[];
  plannedStart: number | null;
  plannedEnd: number | null;
  actualStart: number | null;
  actualEnd: number | null;
  progress: number | null;
};

function findHeaderColumn(
  row: unknown[],
  aliases: string[],
  start = 0,
  end = row.length,
): number | null {
  const normalizedAliases = aliases.map((alias) => normalizeHeader(alias));
  for (let index = start; index < end; index += 1) {
    const header = normalizeHeader(row[index]);
    if (normalizedAliases.includes(header)) return index;
  }
  return null;
}

function minDefined(
  ...values: Array<number | null | undefined>
): number | null {
  const candidates = values.filter((value): value is number => value != null);
  if (candidates.length === 0) return null;
  return Math.min(...candidates);
}

function detectLayeredWbsHeader(rows: unknown[][]): LayeredWbsHeader | null {
  const limit = Math.min(rows.length, 30);

  for (let rowIndex = 0; rowIndex < limit; rowIndex += 1) {
    const row = rows[rowIndex] ?? [];
    const nextRow = rows[rowIndex + 1] ?? [];
    const wbsIndex = findHeaderColumn(row, ["wbs", "wbs番号"]);
    if (wbsIndex == null) continue;

    const assigneeStart = findHeaderColumn(row, HEADER_ALIASES.assignee);
    const plannedStartGroup = findHeaderColumn(row, ["予定", "計画"]);
    const actualStartGroup = findHeaderColumn(row, ["実績"]);
    const progress =
      findHeaderColumn(row, HEADER_ALIASES.progress) ??
      findHeaderColumn(nextRow, HEADER_ALIASES.progress);
    const plannedEndBoundary =
      minDefined(actualStartGroup, progress, row.length) ?? row.length;
    const actualEndBoundary = minDefined(progress, row.length) ?? row.length;

    const plannedStart =
      plannedStartGroup == null
        ? null
        : findHeaderColumn(
            nextRow,
            ["開始日", "開始予定", "planned start", "start"],
            plannedStartGroup,
            plannedEndBoundary,
          );
    const plannedEnd =
      plannedStartGroup == null
        ? null
        : findHeaderColumn(
            nextRow,
            [
              "終了日",
              "終了予定",
              "予定終了日",
              "期限",
              "due",
              "planned end",
              "end",
            ],
            plannedStartGroup,
            plannedEndBoundary,
          );
    const actualStart =
      actualStartGroup == null
        ? null
        : findHeaderColumn(
            nextRow,
            ["開始日", "実績開始日", "actual start"],
            actualStartGroup,
            actualEndBoundary,
          );
    const actualEnd =
      actualStartGroup == null
        ? null
        : findHeaderColumn(
            nextRow,
            ["終了日", "完了日", "実績終了日", "actual end"],
            actualStartGroup,
            actualEndBoundary,
          );

    if (plannedStart == null && plannedEnd == null && actualStart == null) {
      continue;
    }

    const titleEnd =
      minDefined(
        assigneeStart,
        plannedStartGroup,
        actualStartGroup,
        progress,
      ) ?? row.length;
    const titleColumns = Array.from(
      { length: Math.max(0, titleEnd - wbsIndex) },
      (_, offset) => wbsIndex + offset,
    );
    if (titleColumns.length === 0) continue;

    const assigneeEnd =
      minDefined(plannedStartGroup, actualStartGroup, progress) ?? row.length;
    const assigneeColumns =
      assigneeStart == null
        ? []
        : Array.from(
            { length: Math.max(0, assigneeEnd - assigneeStart) },
            (_, offset) => assigneeStart + offset,
          ).map((index) => ({
            index,
            label:
              cleanString(nextRow[index]) ??
              (index === assigneeStart ? null : cleanString(row[index])),
          }));

    return {
      headerIndex: rowIndex,
      firstDataIndex: rowIndex + 2,
      titleColumns,
      assigneeColumns,
      plannedStart,
      plannedEnd,
      actualStart,
      actualEnd,
      progress,
    };
  }

  return null;
}

function excelDateToIso(value: unknown): string | null {
  if (value instanceof Date && !Number.isNaN(value.getTime())) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(
      2,
      "0",
    )}-${String(value.getDate()).padStart(2, "0")}`;
  }
  if (typeof value === "number") {
    const parsed = XLSX.SSF.parse_date_code(value);
    if (!parsed) return null;
    return `${parsed.y}-${String(parsed.m).padStart(2, "0")}-${String(
      parsed.d,
    ).padStart(2, "0")}`;
  }
  const text = cleanString(value);
  if (!text) return null;
  const normalized = text
    .replace(/[年月.]/g, "-")
    .replace(/日/g, "")
    .replace(/\//g, "-");
  const match = normalized.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (!match) return null;
  const [, y, m, d] = match;
  return `${y}-${m.padStart(2, "0")}-${d.padStart(2, "0")}`;
}

function parseProgress(value: unknown): number | null {
  if (typeof value === "number") return value > 1 ? value / 100 : value;
  const text = cleanString(value);
  if (!text) return null;
  const match = text.match(/(\d+(?:\.\d+)?)/);
  if (!match) return null;
  const numeric = Number(match[1]);
  if (!Number.isFinite(numeric)) return null;
  return text.includes("%") || numeric > 1 ? numeric / 100 : numeric;
}

function mapStatus(
  statusValue: unknown,
  progress: number | null,
  actualEnd: string | null,
) {
  const text = String(statusValue ?? "")
    .trim()
    .toLowerCase();
  if (actualEnd || (progress != null && progress >= 1))
    return "closed" as const;
  if (/確認|review|承認|回答待ち/.test(text)) return "review" as const;
  if (/保留|hold|中断|待ち/.test(text)) return "on_hold" as const;
  if (/進行|対応中|着手|doing|progress/.test(text))
    return "in_progress" as const;
  return "open" as const;
}

function mapPriority(plannedEnd: string | null, status: WbsRow["status"]) {
  if (!plannedEnd || status === "closed") return "medium" as const;
  const due = new Date(`${plannedEnd}T23:59:59`);
  const now = new Date();
  const days = Math.ceil((due.getTime() - now.getTime()) / 86_400_000);
  if (days < 0) return "urgent" as const;
  if (days <= 3) return "high" as const;
  return "medium" as const;
}

function hashRow(value: unknown): string {
  return crypto.createHash("sha1").update(JSON.stringify(value)).digest("hex");
}

function getCell(
  row: unknown[],
  columns: Partial<Record<keyof typeof HEADER_ALIASES, number>>,
  field: keyof typeof HEADER_ALIASES,
): unknown {
  const index = columns[field];
  return index == null ? undefined : row[index];
}

function isWbsNumberPart(value: string): boolean {
  return /^\d+(?:\.\d+)?$/.test(value.trim());
}

function formatWbsNumberPart(value: string): string {
  const trimmed = value.trim();
  return /^\d+\.0+$/.test(trimmed) ? String(Number(trimmed)) : trimmed;
}

function extractLayeredAssignee(
  row: unknown[],
  columns: LayeredWbsHeader["assigneeColumns"],
): string | null {
  const assignees: string[] = [];
  for (const column of columns) {
    const marker = cleanString(row[column.index]);
    if (!marker) continue;
    if (/^[●○〇◎◯✓✔■]+$/.test(marker)) {
      if (column.label) assignees.push(column.label);
    } else {
      assignees.push(column.label ? `${column.label}: ${marker}` : marker);
    }
  }
  return assignees.length > 0 ? [...new Set(assignees)].join(", ") : null;
}

function readLayeredWbsRows(
  matrix: unknown[][],
  header: LayeredWbsHeader,
  filePath: string,
  sheetName: string,
): WbsRow[] {
  const rows: WbsRow[] = [];
  const hierarchy = new Map<number, string>();

  for (let i = header.firstDataIndex; i < matrix.length; i += 1) {
    const row = matrix[i] ?? [];
    for (const column of header.titleColumns) {
      const value = cleanString(row[column]);
      if (!value) continue;
      hierarchy.set(column, value);
      for (const deeperColumn of header.titleColumns) {
        if (deeperColumn > column) hierarchy.delete(deeperColumn);
      }
    }

    const hierarchyValues = header.titleColumns
      .map((column) => hierarchy.get(column))
      .filter((value): value is string => !!value);
    const title = [...hierarchyValues]
      .reverse()
      .find((value) => !isWbsNumberPart(value));
    if (!title) continue;

    const plannedStart = excelDateToIso(
      header.plannedStart == null ? undefined : row[header.plannedStart],
    );
    const plannedEnd = excelDateToIso(
      header.plannedEnd == null ? undefined : row[header.plannedEnd],
    );
    const actualStart = excelDateToIso(
      header.actualStart == null ? undefined : row[header.actualStart],
    );
    const actualEnd = excelDateToIso(
      header.actualEnd == null ? undefined : row[header.actualEnd],
    );
    const progress = parseProgress(
      header.progress == null ? undefined : row[header.progress],
    );
    const assignee = extractLayeredAssignee(row, header.assigneeColumns);
    if (
      !plannedStart &&
      !plannedEnd &&
      !actualStart &&
      !actualEnd &&
      progress == null &&
      !assignee
    ) {
      continue;
    }

    const rowNumber = i + 1;
    const wbsIdParts = hierarchyValues
      .filter(isWbsNumberPart)
      .map(formatWbsNumberPart);
    const wbsId = wbsIdParts.length > 0 ? wbsIdParts.join(".") : null;
    const descriptionParts = hierarchyValues.filter(
      (value) => !isWbsNumberPart(value) && value !== title,
    );
    const raw = {
      hierarchy: hierarchyValues,
      assignee,
      plannedStart:
        header.plannedStart == null ? null : row[header.plannedStart],
      plannedEnd: header.plannedEnd == null ? null : row[header.plannedEnd],
      actualStart: header.actualStart == null ? null : row[header.actualStart],
      actualEnd: header.actualEnd == null ? null : row[header.actualEnd],
      progress: header.progress == null ? null : row[header.progress],
    };
    const status = mapStatus(undefined, progress, actualEnd);
    const sourceKey = `${filePath}::${sheetName}::${wbsId || `row-${rowNumber}`}`;

    rows.push({
      sourceKey,
      rowHash: hashRow(raw),
      filePath,
      sheetName,
      rowNumber,
      wbsId,
      title,
      description:
        descriptionParts.length > 0 ? descriptionParts.join(" > ") : null,
      assignee,
      status,
      priority: mapPriority(plannedEnd, status),
      plannedStart,
      plannedEnd,
      actualStart,
      actualEnd,
      progress,
      requestText: null,
      raw,
    });
  }

  return rows;
}

export function readWbsRows(
  projectId: string,
  config: ProjectManagementConfig,
): {
  rows: WbsRow[];
  errors: string[];
  filePath: string | null;
} {
  const managedPath = resolveManagedPath(projectId, config.wbsFile);
  if (!managedPath) {
    return {
      rows: [],
      errors: ["WBSファイルが設定されていません"],
      filePath: null,
    };
  }
  if (!fs.existsSync(managedPath.absolutePath)) {
    return {
      rows: [],
      errors: [`WBSファイルが見つかりません: ${managedPath.relativePath}`],
      filePath: managedPath.relativePath,
    };
  }

  const workbook = XLSX.read(fs.readFileSync(managedPath.absolutePath), {
    type: "buffer",
    cellDates: true,
  });
  const rows: WbsRow[] = [];
  const errors: string[] = [];

  for (const sheetName of workbook.SheetNames) {
    const sheet = workbook.Sheets[sheetName];
    const matrix = XLSX.utils.sheet_to_json<unknown[]>(sheet, {
      header: 1,
      raw: true,
      blankrows: false,
    });
    const header = detectHeader(matrix);
    if (!header) {
      const layeredHeader = detectLayeredWbsHeader(matrix);
      if (layeredHeader) {
        rows.push(
          ...readLayeredWbsRows(
            matrix,
            layeredHeader,
            managedPath.relativePath,
            sheetName,
          ),
        );
      }
      continue;
    }

    for (let i = header.headerIndex + 1; i < matrix.length; i += 1) {
      const row = matrix[i] ?? [];
      const title = cleanString(getCell(row, header.columns, "title"));
      if (!title) continue;

      const plannedStart = excelDateToIso(
        getCell(row, header.columns, "plannedStart"),
      );
      const plannedEnd = excelDateToIso(
        getCell(row, header.columns, "plannedEnd"),
      );
      const actualStart = excelDateToIso(
        getCell(row, header.columns, "actualStart"),
      );
      const actualEnd = excelDateToIso(
        getCell(row, header.columns, "actualEnd"),
      );
      const progress = parseProgress(getCell(row, header.columns, "progress"));
      const status = mapStatus(
        getCell(row, header.columns, "status"),
        progress,
        actualEnd,
      );
      const rowNumber = i + 1;
      const wbsId = cleanString(getCell(row, header.columns, "wbsId"));
      const raw: JsonRecord = {};
      for (const [field, colIndex] of Object.entries(header.columns)) {
        if (colIndex != null) raw[field] = row[colIndex];
      }
      const sourceKey = `${managedPath.relativePath}::${sheetName}::${wbsId || `row-${rowNumber}`}`;
      rows.push({
        sourceKey,
        rowHash: hashRow(raw),
        filePath: managedPath.relativePath,
        sheetName,
        rowNumber,
        wbsId,
        title,
        description: cleanString(getCell(row, header.columns, "description")),
        assignee: cleanString(getCell(row, header.columns, "assignee")),
        status,
        priority: mapPriority(plannedEnd, status),
        plannedStart,
        plannedEnd,
        actualStart,
        actualEnd,
        progress,
        requestText: cleanString(getCell(row, header.columns, "requestText")),
        raw,
      });
    }
  }

  if (rows.length === 0) {
    errors.push("WBSとして読めるシートまたはタスク行が見つかりませんでした");
  }
  return { rows, errors, filePath: managedPath.relativePath };
}

export function selectUpcomingWbsRows(rows: WbsRow[], limit = 20): WbsRow[] {
  const now = new Date();
  return rows
    .filter((row) => row.status !== "closed")
    .sort((a, b) => {
      const aTime = a.plannedEnd
        ? new Date(`${a.plannedEnd}T00:00:00`).getTime()
        : Infinity;
      const bTime = b.plannedEnd
        ? new Date(`${b.plannedEnd}T00:00:00`).getTime()
        : Infinity;
      const aOverdue = aTime < now.getTime() ? 0 : 1;
      const bOverdue = bTime < now.getTime() ? 0 : 1;
      return (
        aOverdue - bOverdue ||
        aTime - bTime ||
        a.title.localeCompare(b.title, "ja")
      );
    })
    .slice(0, limit);
}

function inferTarget(text: string): WorkspaceRequestItem["target"] {
  if (/顧客|お客様|客先|customer|client/i.test(text)) return "customer";
  if (/ベンダ|vendor|メーカー|外部/i.test(text)) return "vendor";
  if (/社内|内部|internal/i.test(text)) return "internal";
  return "unknown";
}

function requestFromWbs(row: WbsRow): WorkspaceRequestItem | null {
  const haystack = [row.title, row.description, row.requestText, row.status]
    .filter(Boolean)
    .join(" ");
  if (!/(確認|依頼|回答待ち|未回答|QA|Q&A|問い合わせ)/i.test(haystack))
    return null;
  return {
    title: row.requestText || row.title,
    target: inferTarget(haystack),
    reason: row.requestText
      ? row.title
      : "WBS上で確認または依頼が必要な状態です",
    sourceType: "wbs",
    sourcePath: row.filePath,
    sourceRef: `${row.sheetName}!${row.rowNumber}`,
    dueAt: row.plannedEnd,
    status: row.status === "on_hold" ? "blocked" : "draft",
  };
}

function walkTextFiles(root: string, limit = 200): string[] {
  const out: string[] = [];
  const stack = [root];
  while (stack.length > 0 && out.length < limit) {
    const current = stack.pop();
    if (!current) continue;
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) {
        if (!SKIP_DIRS.has(entry.name)) stack.push(full);
        continue;
      }
      if (!entry.isFile()) continue;
      const ext = path.extname(entry.name).toLowerCase();
      if (![".md", ".txt", ".csv", ".tsv"].includes(ext)) continue;
      try {
        if (fs.statSync(full).size <= 512_000) out.push(full);
      } catch {
        // ignore unreadable files
      }
      if (out.length >= limit) break;
    }
  }
  return out;
}

function extractRequestsFromTextFile(
  filePath: string,
  sourcePath: string,
): WorkspaceRequestItem[] {
  let text = "";
  try {
    text = fs.readFileSync(filePath, "utf8");
  } catch {
    return [];
  }
  const items: WorkspaceRequestItem[] = [];
  const lines = text.split(/\r?\n/);
  const pattern =
    /(要確認|確認待ち|顧客確認|お客様確認|回答待ち|未回答|要依頼|依頼事項|問い合わせ|QA|Q&A)/i;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].trim();
    if (!line || !pattern.test(line)) continue;
    const compact = line.replace(/^[-*・\s]+/, "").slice(0, 180);
    items.push({
      title: compact,
      target: inferTarget(compact),
      reason: "案件ワークスペース内のメモから抽出",
      sourceType: "file",
      sourcePath,
      sourceRef: `L${index + 1}`,
      dueAt: excelDateToIso(compact),
      status: /回答待ち|未回答|確認待ち/.test(compact) ? "waiting" : "draft",
    });
    if (items.length >= 50) break;
  }
  return items;
}

export function summarizeWorkspaceRequests(
  projectId: string,
  config: ProjectManagementConfig,
  wbsRows: WbsRow[],
): WorkspaceRequestItem[] {
  const items = wbsRows
    .map(requestFromWbs)
    .filter((item): item is WorkspaceRequestItem => !!item);

  const configuredFiles = [
    config.issueFile,
    config.riskFile,
    ...config.requestFiles,
  ]
    .map((item) => resolveManagedPath(projectId, item))
    .filter(
      (item): item is { absolutePath: string; relativePath: string } =>
        !!item && fs.existsSync(item.absolutePath),
    );

  const riskPath = normalizeProjectFilePath(config.riskFile, projectId);
  for (const filePath of configuredFiles) {
    if (
      [".xlsx", ".xlsm", ".xls"].includes(
        path.extname(filePath.absolutePath).toLowerCase(),
      )
    ) {
      const tempConfig = { ...config, wbsFile: filePath.relativePath };
      const parsed = readWbsRows(projectId, tempConfig);
      for (const row of parsed.rows) {
        const item = requestFromWbs(row);
        if (item) {
          items.push({
            ...item,
            sourceType: filePath.relativePath === riskPath ? "risk" : "issue",
          });
        }
      }
    } else {
      items.push(
        ...extractRequestsFromTextFile(
          filePath.absolutePath,
          filePath.relativePath,
        ),
      );
    }
  }

  const storageRoot = getProjectStorageRoot(projectId);
  if (fs.existsSync(storageRoot)) {
    for (const filePath of walkTextFiles(storageRoot)) {
      const sourcePath = path
        .relative(storageRoot, filePath)
        .replace(/\\/g, "/");
      items.push(...extractRequestsFromTextFile(filePath, sourcePath));
      if (items.length >= 100) break;
    }
  }

  const seen = new Set<string>();
  return items
    .filter((item) => {
      const key = `${item.sourcePath}:${item.sourceRef}:${item.title}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 100);
}

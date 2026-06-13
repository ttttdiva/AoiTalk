import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(fileURLToPath(new URL("..", import.meta.url)));
const sourceRoot = path.join(repoRoot, "src");

const targetFiles = [
  "src/app/(app)/reports/page.tsx",
  "src/app/api/notifications/route.ts",
  "src/app/api/reports/time/route.ts",
  "src/app/api/task-occurrences/route.ts",
  "src/app/api/tasks/route.ts",
  "src/app/api/tasks/[id]/route.ts",
  "src/app/api/tasks/[id]/occurrence/route.ts",
  "src/app/api/tasks/[id]/recurrence/route.ts",
  "src/app/api/time-entries/route.ts",
  "src/app/api/time-entries/[id]/route.ts",
  "src/app/api/time-entries/active/route.ts",
  "src/app/api/time-entries/start/route.ts",
  "src/app/api/time-entries/stop/route.ts",
  "src/components/tasks/calendar-view.tsx",
  "src/components/tasks/task-date-picker.tsx",
  "src/components/tasks/task-detail-modal.tsx",
  "src/components/tasks/task-form-utils.ts",
  "src/lib/date-time.ts",
  "src/lib/recurrence-exceptions.ts",
  "src/lib/recurrence-preview.ts",
  "src/lib/recurrence-schedule.ts",
  "src/lib/server/db-time.ts",
  "src/lib/server/task-route-utils.ts",
  "src/lib/task-date-label.ts",
];

const forbidden = [
  {
    pattern:
      /\b(started_at|ended_at|start_at|end_at|completed_at|original_started_at|original_ended_at)\s*:\s*.*\.toISOString\(/,
    message:
      "user-facing task/time fields must use local wall-clock helpers, not toISOString()",
  },
  {
    pattern: /\b(dateFrom|dateTo)\s*=\s*.*\.toISOString\(/,
    message: "report ranges must use local wall-clock helpers",
  },
  {
    pattern: /\bendDate\s*:\s*.*\.toISOString\(/,
    message: "recurrence endDate must use local wall-clock helpers",
  },
  {
    pattern:
      /new Date\(\s*body\.(start_at|end_at|started_at|ended_at|end_date|completed_at)\s*\)/,
    message:
      "API task/time request fields must be parsed by db-time/task-route-utils helpers",
  },
  {
    pattern:
      /new Date\(\s*(row|task|updated|existing|entry)\.(startAt|endAt|startedAt|endedAt|completedAt|createdAt|updatedAt|endDate)\s*\)/,
    message:
      "DB timestamp fields must be converted by dbTimestampToLocalDate(), not Date's local-time parser",
  },
  {
    pattern:
      /\b(row|task|updated|existing|entry)\.(startAt|endAt|startedAt|endedAt|completedAt|createdAt|updatedAt|endDate)\.getTime\(\)/,
    message:
      "DB timestamp fields must be converted by dbTimestampToLocalDate() before date math",
  },
  {
    pattern:
      /new Date\(\s*(customFrom|customTo)\s*\+\s*["'`][^"'`]+["'`]\s*\)\.toISOString\(/,
    message: "custom report date ranges must remain local wall-clock strings",
  },
  {
    pattern:
      /\b(candidateOriginalStarts|futureStarts|originalStarts)\.map\([^=]*=>\s*[^)]*\.toISOString\(/,
    message: "recurrence occurrence keys must use local wall-clock serialization",
  },
  {
    pattern:
      /\bnewMeta\.(original_started_at|original_ended_at|edited_at)\s*=\s*.*\.toISOString\(/,
    message: "time-entry edit metadata must use local wall-clock serialization",
  },
  {
    pattern: /\bDB_WALL_CLOCK_FORMATTER\b|\bformatToParts\(value\)/,
    message:
      "DB timestamp fields must preserve timestamp-without-time-zone wall-clock components, not reinterpret them through a fixed timezone",
  },
];

function listFiles(dir) {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  return entries.flatMap((entry) => {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) return listFiles(fullPath);
    return entry.isFile() ? [fullPath] : [];
  });
}

const files = new Set(
  targetFiles.map((file) => path.join(repoRoot, file)).filter(fs.existsSync),
);

for (const file of listFiles(sourceRoot)) {
  const normalized = path.relative(repoRoot, file).replaceAll(path.sep, "/");
  if (
    normalized.startsWith("src/app/api/tasks/") ||
    normalized.startsWith("src/app/api/time-entries/") ||
    normalized.startsWith("src/app/api/task-occurrences/")
  ) {
    files.add(file);
  }
}

const failures = [];
for (const file of [...files].sort()) {
  const rel = path.relative(repoRoot, file).replaceAll(path.sep, "/");
  const lines = fs.readFileSync(file, "utf8").split(/\r?\n/);
  lines.forEach((line, index) => {
    for (const rule of forbidden) {
      if (rule.pattern.test(line)) {
        failures.push(`${rel}:${index + 1}: ${rule.message}`);
      }
    }
  });
}

if (failures.length > 0) {
  console.error("Time safety check failed:");
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}

console.log("Time safety check passed.");

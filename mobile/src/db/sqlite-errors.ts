/**
 * Return whether an SQLite write failed because another connection currently
 * owns a lock.
 *
 * Expo/Drizzle may wrap the native SQLite error several times. In particular,
 * Drizzle's error for a failed transaction commit exposes the native error as
 * `cause`, while its own message only says "Failed to run the query 'commit'".
 * Keep this helper free of logging so callers can retry without exposing SQL,
 * bound values, or user content.
 */

const SQLITE_BUSY_CODES = new Set([
  "SQLITE_BUSY",
  "SQLITE_BUSY_RECOVERY",
  "SQLITE_BUSY_SNAPSHOT",
  "SQLITE_BUSY_TIMEOUT",
  "SQLITE_LOCKED",
  "SQLITE_LOCKED_SHAREDCACHE",
  "SQLITE_LOCKED_VTAB",
]);

// SQLite primary and extended result codes for BUSY/LOCKED.
const SQLITE_BUSY_NUMERIC_CODES = new Set([5, 6, 261, 262, 517, 518, 773]);

const SQLITE_BUSY_MESSAGE = /(?:database|table|schema)\s+(?:is\s+)?locked|SQLITE_(?:BUSY|LOCKED)(?:\b|[_-])/i;

export const SQLITE_DIAGNOSTICS_ENV_KEY = "EXPO_PUBLIC_AOITALK_SQLITE_DIAGNOSTICS";
export const SQLITE_DIAGNOSTICS_LOG_PREFIX = "AOITALK_SQLITE_DIAGNOSTIC";

export type SqliteErrorClassification = "busy" | "locked" | "full" | "ioerr" | "readonly" | "corrupt" | "other";

// Android Expo errors use a generic bridge code and put the SQLite failure in
// message. Recognize only fixed SQLite phrases; never retain the raw message.
const SQLITE_FAILURE_CLASSIFICATIONS = [
  { classification: "full", primaryCode: 13, code: /^SQLITE_FULL(?:_|$)/i, message: /\bdatabase or disk is full\b/i },
  { classification: "ioerr", primaryCode: 10, code: /^SQLITE_IOERR(?:_|$)/i, message: /\bdisk I\/O error\b/i },
  { classification: "readonly", primaryCode: 8, code: /^SQLITE_READONLY(?:_|$)/i, message: /\battempt to write a readonly database\b/i },
  { classification: "corrupt", primaryCode: 11, code: /^SQLITE_CORRUPT(?:_|$)/i, message: /\bdatabase disk image is malformed\b/i },
] as const;

export interface SqliteErrorDiagnosticNode {
  depth: number;
  className: string;
  classification: SqliteErrorClassification;
  code?: string | number;
  extendedCode?: number;
  errno?: number;
  stackFrames: string[];
}

export interface SqliteErrorDiagnostic {
  operation: string;
  retryable: boolean;
  journalMode: string | null;
  causeChain: SqliteErrorDiagnosticNode[];
}

function isRetryableSqliteCode(code: unknown): boolean {
  if (typeof code === "number") return SQLITE_BUSY_NUMERIC_CODES.has(code);
  if (typeof code !== "string") return false;
  const normalized = code.trim().toUpperCase();
  return SQLITE_BUSY_CODES.has(normalized) || /(?:^|[_:-])SQLITE_(?:BUSY|LOCKED)(?:$|[_:-])/.test(normalized);
}

function numericField(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function sqliteCodeValues(object: Record<string, unknown>): unknown[] {
  return [
    object.code,
    object.sqliteCode,
    object.sqlite_errorcode,
    object.extendedCode,
    object.sqliteExtendedCode,
    object.errno,
  ].filter((value) => value !== undefined);
}

function safeCode(value: unknown): string | number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const normalized = value.trim().toUpperCase();
    if (
      /^SQLITE_[A-Z0-9_]+$/.test(normalized) ||
      /^ERR_SQLITE(?:_[A-Z0-9_]+)?$/.test(normalized) ||
      normalized === "ERR_INTERNAL_SQLITE_ERROR" ||
      normalized === "ERR_INVALID_SHARED_OBJECT_ID"
    ) {
      return normalized;
    }
  }
  return undefined;
}

function canonicalCodeFor(object: Record<string, unknown>): string | number | undefined {
  const codes = sqliteCodeValues(object)
    .map(safeCode)
    .filter((value): value is string | number => value !== undefined);
  return codes.find(
    (value) => typeof value === "string" && /^SQLITE_/.test(value),
  ) ?? codes.find(
    (value) => typeof value === "string" && /^ERR_SQLITE/.test(value),
  ) ?? codes[0];
}

function classificationFor(object: Record<string, unknown>): SqliteErrorClassification {
  const codes = sqliteCodeValues(object);
  if (codes.some((code) => typeof code === "string" && /LOCKED/i.test(code))) return "locked";
  if (codes.some((code) => typeof code === "number" && [6, 262, 518].includes(code))) return "locked";
  if (codes.some(isRetryableSqliteCode)) {
    return "busy";
  }
  for (const failure of SQLITE_FAILURE_CLASSIFICATIONS) {
    if (codes.some((code) =>
      (typeof code === "string" && failure.code.test(code.trim())) ||
      (typeof code === "number" && Number.isInteger(code) && code >= 0 && code <= 0x7fffffff &&
        (code & 0xff) === failure.primaryCode),
    )) return failure.classification;
  }
  if (typeof object.message === "string" && /(?:table|schema|database)\s+(?:is\s+)?locked/i.test(object.message)) {
    return "busy";
  }
  if (typeof object.message === "string") {
    for (const failure of SQLITE_FAILURE_CLASSIFICATIONS) {
      if (failure.message.test(object.message)) return failure.classification;
    }
  }
  return "other";
}

function safeStackFrames(value: unknown): string[] {
  if (typeof value !== "string") return [];
  return value
    .split("\n")
    .slice(1, 9)
    .map((line) => line.trim())
    .filter((line) => line.startsWith("at "))
    .map((line) => {
      const withFunction = line.match(/^at\s+(.+?)\s+\(.*:(\d+):(\d+)\)$/);
      if (withFunction) {
        const functionName = /^(?:(?:async|new)\s+)?[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$/.test(withFunction[1])
          ? withFunction[1]
          : "<function>";
        return "at " + functionName + ":" + withFunction[2] + ":" + withFunction[3];
      }
      const location = line.match(/^at\s+.*:(\d+):(\d+)$/);
      return location ? `at <anonymous>:${location[1]}:${location[2]}` : "at <unknown>";
    });
}

function classNameOf(object: object): string {
  const constructorName = (object as { constructor?: { name?: unknown } }).constructor?.name;
  return typeof constructorName === "string" && /^[A-Za-z_$][A-Za-z0-9_$]{0,63}$/.test(constructorName)
    ? constructorName
    : "UnknownError";
}

const SQLITE_JOURNAL_MODES = new Set([
  "delete",
  "truncate",
  "persist",
  "memory",
  "wal",
  "off",
]);

export function normalizeSqliteJournalMode(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  return SQLITE_JOURNAL_MODES.has(normalized) ? normalized : null;
}

/**
 * Inspect only structured error codes/messages and their causes. The bounded
 * traversal also handles malformed/cyclic error objects without stringifying
 * an error (which could include SQL or bound values).
 */
export function isSqliteBusyError(error: unknown): boolean {
  const seen = new Set<object>();
  let current: unknown = error;

  for (let depth = 0; depth < 16 && current != null; depth += 1) {
    if (typeof current === "string") {
      return SQLITE_BUSY_MESSAGE.test(current);
    }
    if (typeof current !== "object" && typeof current !== "function") {
      return false;
    }

    const object = current as Record<string, unknown>;
    if (seen.has(object)) return false;
    seen.add(object);

    if (sqliteCodeValues(object).some(isRetryableSqliteCode)) return true;
    if (typeof object.message === "string" && SQLITE_BUSY_MESSAGE.test(object.message)) {
      return true;
    }

    current = object.cause;
  }

  return false;
}

/** Explicit opt-in for a single device reproduction. Disabled by default. */
export function isSqliteDiagnosticsEnabled(
  value = process.env.EXPO_PUBLIC_AOITALK_SQLITE_DIAGNOSTICS,
): boolean {
  return /^(1|true|yes|on)$/i.test(value?.trim() ?? "");
}

/**
 * Produce a value-free cause/code/stack summary suitable for logcat. It never
 * includes error messages, SQL text, bind parameters, IDs, or user content.
 */
export function describeSqliteError(
  error: unknown,
  options: { operation?: string; journalMode?: unknown } = {},
): SqliteErrorDiagnostic {
  const seen = new Set<object>();
  const causeChain: SqliteErrorDiagnosticNode[] = [];
  let current: unknown = error;

  for (let depth = 0; depth < 16 && current != null; depth += 1) {
    if (typeof current !== "object" && typeof current !== "function") break;
    const object = current as Record<string, unknown>;
    if (seen.has(object)) break;
    seen.add(object);
    const node: SqliteErrorDiagnosticNode = {
      depth,
      className: classNameOf(object),
      classification: classificationFor(object),
      stackFrames: safeStackFrames(object.stack),
    };
    const code = canonicalCodeFor(object);
    const extendedCode = numericField(object.extendedCode ?? object.sqliteExtendedCode);
    const errno = numericField(object.errno);
    if (code !== undefined) node.code = code;
    if (extendedCode !== undefined) node.extendedCode = extendedCode;
    if (errno !== undefined) node.errno = errno;
    causeChain.push(node);
    current = object.cause;
  }

  return {
    operation: options.operation && /^[A-Za-z0-9_.:-]{1,80}$/.test(options.operation)
      ? options.operation
      : "sqlite_write",
    retryable: isSqliteBusyError(error),
    journalMode: normalizeSqliteJournalMode(options.journalMode),
    causeChain,
  };
}

export function logSqliteErrorDiagnostic(
  error: unknown,
  options: {
    operation?: string;
    journalMode?: unknown;
    log?: (message: string) => void;
  } = {},
): string | null {
  if (!isSqliteDiagnosticsEnabled()) return null;
  const payload = describeSqliteError(error, options);
  const message = `${SQLITE_DIAGNOSTICS_LOG_PREFIX} ${JSON.stringify(payload)}`;
  (options.log ?? console.warn)(message);
  return message;
}


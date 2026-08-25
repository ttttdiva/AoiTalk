export interface UploadFailureDisplay {
  name: string;
  relativePath?: string;
  status?: number;
  message?: string;
  error?: string;
}

const MAX_FAILURE_DETAIL_LENGTH = 500;
const MAX_FAILURE_ITEMS = 20;

function safeFailureDetail(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const normalized = value
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized) return undefined;
  return normalized.length > MAX_FAILURE_DETAIL_LENGTH
    ? `${normalized.slice(0, MAX_FAILURE_DETAIL_LENGTH - 1)}…`
    : normalized;
}

function failurePath(failure: UploadFailureDisplay): string {
  const value = failure.relativePath?.trim() || failure.name.trim();
  const normalized = value
    .replaceAll("\\", "/")
    .replace(/^\/+/, "")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized) return "（ファイル名不明）";
  return normalized.length > MAX_FAILURE_DETAIL_LENGTH
    ? `${normalized.slice(0, MAX_FAILURE_DETAIL_LENGTH - 1)}…`
    : normalized;
}

function failureDescription(failure: UploadFailureDisplay): string | undefined {
  const detail = safeFailureDetail(failure.message) ?? safeFailureDetail(failure.error);
  const statusCode = failure.status;
  const status =
    typeof statusCode === "number" &&
    Number.isInteger(statusCode) &&
    statusCode > 0
      ? `HTTP ${statusCode}`
      : undefined;
  if (detail && status) {
    if (new RegExp(`\\bHTTP\\s+${statusCode}\\b`, "i").test(detail)) {
      return detail;
    }
    return `${detail} (${status})`;
  }
  return detail || status;
}

export function uploadFailureToastOptions(
  failures: readonly UploadFailureDisplay[],
): { description: string; descriptionClassName: string } | undefined {
  if (failures.length === 0) return undefined;
  const visibleFailures = failures.slice(0, MAX_FAILURE_ITEMS);
  const omittedCount = failures.length - visibleFailures.length;

  return {
    description: [
      "失敗したファイル:",
      ...visibleFailures.map((failure) => {
        const detail = failureDescription(failure);
        return `・${failurePath(failure)}${detail ? ` — ${detail}` : ""}`;
      }),
      ...(omittedCount > 0 ? [`・ほか${omittedCount}件`] : []),
    ].join("\n"),
    descriptionClassName: "whitespace-pre-wrap break-all",
  };
}

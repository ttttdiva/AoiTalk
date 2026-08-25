export const X_COOKIE_ENDPOINT = "/api/python-proxy/users/me/x-cookie";

export const COOKIE_STATUSES = [
  "unconfigured",
  "available",
  "invalid_format",
  "missing_required_cookie",
  "expired",
  "unavailable",
] as const;

export type CookieStatus = (typeof COOKIE_STATUSES)[number];

export const COOKIE_SOURCES = ["personal", "server_shared", "none"] as const;
export type CookieSource = (typeof COOKIE_SOURCES)[number];

export type XCookieStatusResponse = {
  service: "x";
  status: CookieStatus;
  configured: boolean;
  source: CookieSource;
  updated_at?: string;
};

export type CookieUploadSource = "netscape" | "har";

export type PreparedCookieUpload = {
  body: string;
  source: CookieUploadSource;
};

export class CookieUploadPreparationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CookieUploadPreparationError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCookieStatus(value: unknown): value is CookieStatus {
  return typeof value === "string" && (COOKIE_STATUSES as readonly string[]).includes(value);
}

function isCookieSource(value: unknown): value is CookieSource {
  return typeof value === "string" && (COOKIE_SOURCES as readonly string[]).includes(value);
}

/**
 * The BFF deliberately returns a small safe object. Normalize defensively so
 * future backend fields or malformed responses cannot accidentally render a
 * secret/detail string in the settings page.
 */
export function normalizeXCookieStatus(value: unknown): XCookieStatusResponse {
  const record = isRecord(value) ? value : {};
  const status = isCookieStatus(record.status) ? record.status : "unavailable";
  const source = isCookieSource(record.source) ? record.source : "none";
  return {
    service: "x",
    status,
    configured: record.configured === true,
    source,
    ...(typeof record.updated_at === "string" ? { updated_at: record.updated_at } : {}),
  };
}

function isXHost(hostname: string): boolean {
  const host = hostname.toLowerCase();
  return (
    host === "x.com" ||
    host.endsWith(".x.com") ||
    host === "twitter.com" ||
    host.endsWith(".twitter.com")
  );
}

function parseCookieHeader(value: string): Map<string, string> {
  const pairs = new Map<string, string>();
  for (const rawPair of value.split(";")) {
    const pair = rawPair.trim();
    const separator = pair.indexOf("=");
    if (separator <= 0) continue;
    const name = pair.slice(0, separator).trim();
    const cookieValue = pair.slice(separator + 1).trim();
    if (!name || !cookieValue || pairs.has(name)) continue;
    // Intentionally do not URL-decode cookie values. The server receives the
    // exact value from the browser's Cookie header for final validation.
    pairs.set(name, cookieValue);
  }
  return pairs;
}

function minimalNetscapeMaterial(authToken: string, ct0: string): string {
  return [
    "# Netscape HTTP Cookie File",
    `.x.com\tTRUE\t/\tTRUE\t0\tauth_token\t${authToken}`,
    `.x.com\tTRUE\t/\tTRUE\t0\tct0\t${ct0}`,
    "",
  ].join("\n");
}

/**
 * Extract only the two required cookies from one X/Twitter HAR request.
 * Returns null when no single matching request contains both credentials.
 */
export function extractXCookieMaterialFromHar(value: unknown): string | null {
  if (!isRecord(value)) return null;
  const log = value.log;
  if (!isRecord(log) || !Array.isArray(log.entries)) return null;

  for (const entry of log.entries) {
    if (!isRecord(entry) || !isRecord(entry.request)) continue;
    const request = entry.request;
    if (typeof request.url !== "string") continue;

    let url: URL;
    try {
      url = new URL(request.url);
    } catch {
      continue;
    }
    if (!isXHost(url.hostname) || !Array.isArray(request.headers)) continue;

    const cookieHeader = request.headers
      .filter(
        (header): header is Record<string, unknown> =>
          isRecord(header) &&
          typeof header.name === "string" &&
          header.name.trim().toLowerCase() === "cookie" &&
          typeof header.value === "string",
      )
      .map((header) => header.value as string)
      .join("; ");
    if (!cookieHeader) continue;

    const cookies = parseCookieHeader(cookieHeader);
    const authToken = cookies.get("auth_token");
    const ct0 = cookies.get("ct0");
    if (!authToken || !ct0) continue;
    // Cookie values cannot contain line separators in a valid request. Reject
    // them rather than allowing a malformed HAR to create extra Netscape rows.
    if (/[\r\n\t]/.test(authToken) || /[\r\n\t]/.test(ct0)) continue;
    return minimalNetscapeMaterial(authToken, ct0);
  }
  return null;
}

function looksLikeHarFile(contents: string, filename?: string): boolean {
  if (typeof filename === "string" && /\.har$/i.test(filename.trim())) return true;
  const first = contents.trimStart();
  return first.startsWith("{") || first.startsWith("[");
}

/**
 * Prepare a file for PUT. Netscape text is returned byte-for-byte unchanged;
 * HAR is parsed in the browser and reduced to two canonical Netscape rows.
 */
export function prepareXCookieUpload(
  contents: string,
  options: { filename?: string } = {},
): PreparedCookieUpload {
  if (!looksLikeHarFile(contents, options.filename)) {
    return { body: contents, source: "netscape" };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(contents);
  } catch {
    throw new CookieUploadPreparationError("HARを読み取れませんでした。別のファイルを選択してください。");
  }

  const material = extractXCookieMaterialFromHar(parsed);
  if (!material) {
    throw new CookieUploadPreparationError(
      "HAR内のX/Twitterリクエストから必要なCookieを見つけられませんでした。",
    );
  }
  return { body: material, source: "har" };
}

export function cookieStatusLabel(status: CookieStatus): string {
  switch (status) {
    case "unconfigured":
      return "未設定";
    case "available":
      return "利用可能";
    case "invalid_format":
      return "不正な形式";
    case "missing_required_cookie":
      return "必須Cookie不足";
    case "expired":
      return "期限切れ";
    case "unavailable":
    default:
      return "利用できません";
  }
}

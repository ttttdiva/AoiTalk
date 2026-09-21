export function normalizeApiUrl(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "";

  try {
    const url = new URL(trimmed);

    url.pathname = "";
    url.search = "";
    url.hash = "";

    return url.toString().replace(/\/+$/, "");
  } catch {
    return trimmed.replace(/\/+$/, "");
  }
}

/**
 * Return a normalized configured API URL or fail before a request is built.
 * HTTP is intentionally allowed for local/LAN development; public endpoints
 * may use HTTPS and therefore retain the platform's normal TLS validation.
 */
export class InvalidApiUrlError extends Error {
  readonly value: string;

  constructor(value: string) {
    super("API URLが未設定または無効です");
    this.name = "InvalidApiUrlError";
    this.value = value;
  }
}

export function isInvalidApiUrlError(
  error: unknown,
): error is InvalidApiUrlError {
  return error instanceof InvalidApiUrlError;
}

export function requireConfiguredApiUrl(value: string): string {
  const normalized = normalizeApiUrl(value);
  if (!normalized) throw new InvalidApiUrlError(value);

  try {
    const parsed = new URL(normalized);
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
      !parsed.hostname
    ) {
      throw new InvalidApiUrlError(value);
    }
  } catch (error) {
    if (error instanceof InvalidApiUrlError) throw error;
    throw new InvalidApiUrlError(value);
  }

  return normalized;
}

export function looksLikeHtml(text: string): boolean {
  return /^\s*(?:<!doctype\s+html|<html[\s>])/i.test(text);
}

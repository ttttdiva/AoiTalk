/**
 * Only a failure attributable to the incoming request's cancellation is quiet.
 * In Node/Next the rejection may be signal.reason (ResponseAborted), a normal
 * AbortError, or a fetch error wrapping that reason in cause. An aborted signal
 * alone is not enough: an independent upstream failure can race a disconnect.
 */
export function isClientRequestAbort(error: unknown, signal: AbortSignal): boolean {
  if (!signal.aborted) return false;

  const reason: unknown = signal.reason;
  if (
    typeof reason === "object" && reason !== null &&
    "name" in reason && reason.name === "TimeoutError"
  ) {
    return false;
  }

  const seen = new Set<unknown>();
  let current: unknown = error;
  let matchesAbort = false;
  while (current !== undefined && current !== null && !seen.has(current)) {
    seen.add(current);
    if (current === reason) matchesAbort = true;
    if (typeof current !== "object") break;

    const name = "name" in current ? current.name : undefined;
    const code = "code" in current ? current.code : undefined;
    // A transport deadline is still a failure, even if an outer error happens
    // to use an abort-like name. Do not turn timeouts into quiet cancellations.
    if (
      name === "TimeoutError" || code === "ETIMEDOUT" ||
      (typeof code === "string" && /^UND_ERR_.*TIMEOUT$/.test(code))
    ) {
      return false;
    }
    if (name === "AbortError" || name === "ResponseAborted") matchesAbort = true;
    current = "cause" in current ? current.cause : undefined;
  }
  return matchesAbort;
}

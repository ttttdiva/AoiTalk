/**
 * Mobile-side outbound privacy transaction.
 *
 * The server runtime has a Python ``OutboundPrivacyGateway``.  Direct mobile
 * providers cannot call that process, so this module keeps the same boundary
 * shape locally: classify the destination, derive one immutable candidate,
 * optionally obtain a transaction-scoped review decision, then invoke the
 * transport exactly once with the approved request.  Callers must not invoke
 * ``fetch`` outside ``execute`` for user/project/agent-derived model payloads.
 */

export type MobilePrivacyMode = "direct" | "protected" | "local_only";
export type MobileReviewPolicy = "never" | "high_risk" | "always";

export interface MobileEgressDescriptor {
  action: string;
  transport: string;
  destination: string;
  provider: string;
  tool?: string;
  model?: string;
}

export interface MobileOutboundRequest {
  url: string;
  init: RequestInit;
  /** Parsed provider body. When omitted, ``init.body`` is parsed as JSON. */
  payload?: unknown;
}

export interface MobileEgressReviewRequest {
  contractVersion: 2;
  descriptor: MobileEgressDescriptor;
  originalPayload: unknown;
  candidatePayload: unknown;
  request: MobileOutboundRequest;
}

export interface MobileEgressReviewDecision {
  approved: boolean;
  /** Optional exact payload to send after approval. */
  finalPayload?: unknown;
}

export type MobileEgressReviewCallback = (
  request: MobileEgressReviewRequest,
) => MobileEgressReviewDecision | boolean | Promise<MobileEgressReviewDecision | boolean>;

export interface MobileEgressOptions {
  descriptor: MobileEgressDescriptor;
  mode?: MobilePrivacyMode;
  reviewPolicy?: MobileReviewPolicy;
  trustedLocalHosts?: readonly string[];
  redactionTerms?: readonly string[];
  review?: MobileEgressReviewCallback;
}

export class MobileEgressDeniedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MobileEgressDeniedError";
  }
}

/** Fetch helper used only by gateway senders; redirects are never followed. */
export async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  const upstreamSignal = init.signal;
  if (upstreamSignal?.aborted) controller.abort();
  else upstreamSignal?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(abort, timeoutMs);
  try {
    return await fetch(url, {
      ...init,
      redirect: "error",
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
    upstreamSignal?.removeEventListener("abort", abort);
  }
}

const DEFAULT_LOCAL_HOSTS = new Set([
  "localhost",
  "127.0.0.1",
  "::1",
  "0.0.0.0",
]);

const REDACTED = "[REDACTED]";
const SECRET_PATTERNS: readonly RegExp[] = [
  /\bsk-[A-Za-z0-9_-]{12,}\b/g,
  /\b(?:bearer|token|api[_ -]?key|secret|password)\s*[:=]\s*[^\s,;]+/gi,
  /\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{10,}\b/g,
  /\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b/g,
];

function cloneValue<T>(value: T): T {
  if (value === undefined) return value;
  try {
    return JSON.parse(JSON.stringify(value)) as T;
  } catch {
    // Request payloads are JSON by contract.  A non-serializable value is not
    // safe to send through a protected transaction, so preserve a scalar
    // marker for review rather than stringifying arbitrary objects.
    return REDACTED as T;
  }
}

function redactText(value: string, terms: readonly string[]): string {
  let output = value;
  for (const pattern of SECRET_PATTERNS) output = output.replace(pattern, REDACTED);
  for (const term of terms) {
    const normalized = String(term ?? "");
    if (normalized) output = output.split(normalized).join(REDACTED);
  }
  return output;
}

function redactValue(value: unknown, terms: readonly string[]): unknown {
  if (typeof value === "string") return redactText(value, terms);
  if (Array.isArray(value)) return value.map((item) => redactValue(item, terms));
  if (value && typeof value === "object") {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      result[key] = redactValue(item, terms);
    }
    return result;
  }
  return value;
}

function destinationFromUrl(url: string): string {
  try {
    const parsed = new URL(url);
    // Query strings can contain provider API keys (Gemini), so descriptors
    // intentionally bind only the origin/path and never credential material.
    return `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, "");
  } catch {
    return String(url || "").split(/[?#]/, 1)[0].replace(/\/+$/, "");
  }
}

function isTrustedLocalHost(
  hostname: string,
  trustedLocalHosts: readonly string[],
): boolean {
  const normalized = hostname.trim().toLowerCase().replace(/[\[\]]/g, "");
  if (DEFAULT_LOCAL_HOSTS.has(normalized)) return true;
  if (normalized.endsWith(".local")) return true;
  return trustedLocalHosts.some((entry) => {
    const host = String(entry ?? "").trim().toLowerCase();
    return Boolean(host) && (normalized === host || normalized.endsWith(`.${host}`));
  });
}

function isLocalDestination(url: string, trustedLocalHosts: readonly string[]): boolean {
  try {
    return isTrustedLocalHost(new URL(url).hostname, trustedLocalHosts);
  } catch {
    return false;
  }
}

function normalizeDescriptor(descriptor: MobileEgressDescriptor, url: string): MobileEgressDescriptor {
  if (!descriptor || typeof descriptor !== "object" || Array.isArray(descriptor)) {
    throw new MobileEgressDeniedError("Malformed mobile egress descriptor.");
  }
  for (const key of ["action", "transport", "destination", "provider", "tool", "model"] as const) {
    if (key in descriptor && typeof descriptor[key] !== "string") {
      throw new MobileEgressDeniedError("Malformed mobile egress descriptor.");
    }
  }
  const provider = descriptor.provider.trim();
  if (!provider) throw new MobileEgressDeniedError("Mobile egress provider is required.");
  const destination = descriptor.destination.trim() || destinationFromUrl(url);
  const action = descriptor.action.trim() || "external_egress";
  const transport = descriptor.transport.trim() || "fetch";
  return {
    action,
    transport,
    destination: destinationFromUrl(destination),
    provider,
    ...(descriptor.tool ? { tool: descriptor.tool } : {}),
    ...(descriptor.model ? { model: descriptor.model } : {}),
  };
}

function parsePayload(request: MobileOutboundRequest): unknown {
  if (request.payload !== undefined) return cloneValue(request.payload);
  if (typeof request.init.body !== "string") return request.init.body ?? null;
  try {
    return JSON.parse(request.init.body);
  } catch {
    return request.init.body;
  }
}

function withPayload(
  request: MobileOutboundRequest,
  payload: unknown,
): MobileOutboundRequest {
  const init: RequestInit = {
    ...request.init,
    // Fetch defaults to following redirects.  A reviewed URL must not be
    // silently retargeted by a redirect chain.
    redirect: "error",
  };
  if (payload !== null && payload !== undefined) {
    init.body = JSON.stringify(payload);
  } else {
    // An explicit null/undefined final payload means no request body.  Do not
    // retain the candidate body from the spread above, otherwise the network
    // request would differ from the value shown/approved in the dialog.
    delete init.body;
  }
  return { ...request, init, payload: cloneValue(payload) };
}

function cloneRequest(request: MobileOutboundRequest): MobileOutboundRequest {
  const headers = request.init.headers;
  let clonedHeaders = headers;
  if (headers && typeof headers === "object" && !(headers instanceof Headers)) {
    clonedHeaders = { ...(headers as Record<string, string>) };
  } else if (headers instanceof Headers) {
    clonedHeaders = new Headers(headers);
  }
  return {
    url: request.url,
    init: { ...request.init, headers: clonedHeaders },
    payload: cloneValue(request.payload),
  };
}

/**
 * Execute one mobile egress transaction and invoke ``sender`` once.
 *
 * Direct mode preserves the historical mobile behavior.  ``always`` (and
 * high-risk edits under ``high_risk``) require an explicit review callback for
 * external destinations; absent callbacks fail closed.  ``protected`` only
 * controls masking, while ``never`` preserves the no-new-modal behavior.
 * ``local_only`` always blocks external destinations and never weakens that
 * rule for a caller callback.
 */
export async function executeMobileEgress<T>(
  request: MobileOutboundRequest,
  options: MobileEgressOptions,
  sender: (request: MobileOutboundRequest) => T | Promise<T>,
): Promise<T> {
  if (!request || typeof request.url !== "string" || !request.url.trim()) {
    throw new MobileEgressDeniedError("Mobile egress request URL is required.");
  }
  if (typeof sender !== "function") {
    throw new MobileEgressDeniedError("Mobile egress sender is required.");
  }
  const descriptor = normalizeDescriptor(options.descriptor, request.url);
  const mode = options.mode ?? "direct";
  const reviewPolicy = options.reviewPolicy ?? "high_risk";
  if (mode !== "direct" && mode !== "protected" && mode !== "local_only") {
    throw new MobileEgressDeniedError("Malformed mobile privacy mode.");
  }
  if (
    reviewPolicy !== "never" &&
    reviewPolicy !== "high_risk" &&
    reviewPolicy !== "always"
  ) {
    throw new MobileEgressDeniedError("Malformed mobile review policy.");
  }
  const trustedLocalHosts = options.trustedLocalHosts ?? [];
  const local = isLocalDestination(request.url, trustedLocalHosts);
  if (mode === "local_only" && !local) {
    throw new MobileEgressDeniedError("External provider is blocked by local-only privacy mode.");
  }

  const originalPayload = parsePayload(request);
  const candidatePayload =
    !local && mode === "protected"
      ? redactValue(originalPayload, options.redactionTerms ?? [])
      : cloneValue(originalPayload);
  const candidateRequest = withPayload(request, candidatePayload);
  const reviewRequired =
    !local &&
    (reviewPolicy === "always" ||
      (reviewPolicy === "high_risk" && JSON.stringify(candidatePayload) !== JSON.stringify(originalPayload)));

  let finalRequest = candidateRequest;
  if (reviewRequired) {
    if (typeof options.review !== "function") {
      throw new MobileEgressDeniedError("External egress review callback is required.");
    }
    const reviewed = await options.review({
      contractVersion: 2,
      descriptor: { ...descriptor },
      originalPayload: cloneValue(originalPayload),
      candidatePayload: cloneValue(candidatePayload),
      request: cloneRequest(candidateRequest),
    });
    const decision = typeof reviewed === "boolean" ? { approved: reviewed } : reviewed;
    if (!decision || decision.approved !== true) {
      throw new MobileEgressDeniedError("External egress review was denied.");
    }
    if (Object.prototype.hasOwnProperty.call(decision, "finalPayload")) {
      if (decision.finalPayload === undefined) {
        throw new MobileEgressDeniedError("External egress review omitted final payload.");
      }
      finalRequest = withPayload(candidateRequest, cloneValue(decision.finalPayload));
    }
  }

  // The only transport commit point.  ``sender`` receives the exact request
  // that was reviewed; no retries or redirect-following happen behind it.
  return sender(finalRequest);
}

export function descriptorForUrl(
  descriptor: Omit<MobileEgressDescriptor, "destination"> & { destination?: string },
  url: string,
): MobileEgressDescriptor {
  return normalizeDescriptor(
    { ...descriptor, destination: descriptor.destination || destinationFromUrl(url) },
    url,
  );
}

import { withBasePath } from "@/lib/base-path";

function resolveWebSocketAuthority(): string {
  const isHttps = window.location.protocol === "https:";
  const configuredPort = process.env.NEXT_PUBLIC_AOITALK_WS_PORT?.trim();
  return isHttps
    ? window.location.host
    : configuredPort
      ? `${window.location.hostname}:${configuredPort}`
      : `${window.location.hostname}:3000`;
}

function resolveWebSocketProtocol(): "wss:" | "ws:" {
  return window.location.protocol === "https:" ? "wss:" : "ws:";
}

export function buildPcBridgeServerUrl(): string {
  const isHttps = window.location.protocol === "https:";
  // Direct HTTP connects to FastAPI's root-mounted, bearer-authenticated
  // Bridge route. Only the public HTTPS gateway uses the browser's mount.
  const path = isHttps ? withBasePath("/").replace(/\/$/u, "") : "";
  return `${isHttps ? "https:" : "http:"}//${resolveWebSocketAuthority()}${path}`;
}

export function buildWebSocketUrl(sessionId: string): string {
  const protocol = resolveWebSocketProtocol();
  const authority = resolveWebSocketAuthority();
  const path = withBasePath("/ws");
  return `${protocol}//${authority}${path}?session_id=${encodeURIComponent(sessionId)}`;
}

export function buildPlayWebSocketUrl(sessionId: string): string {
  const protocol = resolveWebSocketProtocol();
  const authority = resolveWebSocketAuthority();
  const path = withBasePath(`/ws/play/${encodeURIComponent(sessionId)}`);
  return `${protocol}//${authority}${path}`;
}

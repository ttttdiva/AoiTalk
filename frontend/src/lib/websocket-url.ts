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

export function buildWebSocketUrl(sessionId: string): string {
  const protocol = resolveWebSocketProtocol();
  const authority = resolveWebSocketAuthority();
  return `${protocol}//${authority}/ws?session_id=${encodeURIComponent(sessionId)}`;
}

export function buildPlayWebSocketUrl(sessionId: string): string {
  const protocol = resolveWebSocketProtocol();
  const authority = resolveWebSocketAuthority();
  return `${protocol}//${authority}/ws/play/${encodeURIComponent(sessionId)}`;
}

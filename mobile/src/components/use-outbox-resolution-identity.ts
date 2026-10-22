import { AppState, type AppStateStatus } from "react-native";
import { useEffect, useState } from "react";
import { getOutboxResolutionIdentity } from "../repositories/outbox-resolution";

export type OutboxResolutionIdentity = {
  authScope: string;
  serverFingerprint: string;
  credentialIdentityRevision: string;
};

export type OutboxResolutionIdentityOptions = {
  active?: boolean;
  pollMs?: number;
};

function safeIdentity(value: unknown): OutboxResolutionIdentity | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<OutboxResolutionIdentity>;
  if (typeof candidate.authScope !== "string" || !candidate.authScope.trim()) return null;
  if (typeof candidate.serverFingerprint !== "string" || !candidate.serverFingerprint.trim()) return null;
  if (typeof candidate.credentialIdentityRevision !== "string" || !candidate.credentialIdentityRevision.trim()) return null;
  return {
    authScope: candidate.authScope,
    serverFingerprint: candidate.serverFingerprint,
    credentialIdentityRevision: candidate.credentialIdentityRevision,
  };
}

export function useOutboxResolutionIdentity(
  enabled: boolean,
  refreshHint = "",
  options: OutboxResolutionIdentityOptions = {},
) {
  const activeRequested = options.active ?? true;
  const pollMs = Number.isFinite(options.pollMs) && (options.pollMs ?? 0) > 0
    ? Math.max(250, Math.floor(options.pollMs as number))
    : 1000;
  const [appState, setAppState] = useState<AppStateStatus>(AppState.currentState);
  const [identity, setIdentity] = useState<OutboxResolutionIdentity | null>(null);
  const [settledHint, setSettledHint] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const subscription = AppState.addEventListener("change", setAppState);
    return () => subscription.remove();
  }, []);

  useEffect(() => {
    const appStateIsActive = appState == null || appState === "active" || appState === "unknown";
    const isActive = enabled && activeRequested && appStateIsActive;
    const generation = { value: 0 };
    let disposed = false;
    let inFlight = false;
    if (!isActive) {
      setIdentity(null);
      setSettledHint(null);
      setLoading(false);
      return () => { disposed = true; };
    }
    setSettledHint(null);

    const refresh = async () => {
      if (disposed || inFlight) return;
      inFlight = true;
      const requestGeneration = ++generation.value;
      setLoading(true);
      try {
        const result = await getOutboxResolutionIdentity();
        if (disposed || requestGeneration !== generation.value) return;
        const next = result && result.ok ? safeIdentity(result.value) : null;
        setIdentity((previous) => {
          if (
            previous && next
            && previous.authScope === next.authScope
            && previous.serverFingerprint === next.serverFingerprint
            && previous.credentialIdentityRevision === next.credentialIdentityRevision
          ) return previous;
          return next;
        });
        setSettledHint(refreshHint);
      } catch {
        if (!disposed && requestGeneration === generation.value) {
          setIdentity(null);
          setSettledHint(refreshHint);
        }
      } finally {
        inFlight = false;
        if (!disposed && requestGeneration === generation.value) setLoading(false);
      }
    };

    void refresh();
    const timer = setInterval(() => { void refresh(); }, pollMs);
    return () => {
      disposed = true;
      generation.value += 1;
      clearInterval(timer);
    };
  }, [activeRequested, appState, enabled, pollMs, refreshHint]);

  const appStateIsActive = appState == null || appState === "active" || appState === "unknown";
  return {
    identity: enabled && activeRequested && appStateIsActive && settledHint === refreshHint
      ? identity
      : null,
    loading,
  };
}

export function outboxResolutionNamespaceKey(identity: OutboxResolutionIdentity): string {
  return `${identity.authScope}|${identity.serverFingerprint}`;
}

export function outboxResolutionDomainKey(identity: OutboxResolutionIdentity): string {
  return `${identity.authScope}|${identity.serverFingerprint}|${identity.credentialIdentityRevision}`;
}

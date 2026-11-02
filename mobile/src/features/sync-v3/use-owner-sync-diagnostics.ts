import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { outboxResolutionDomainKey, type OutboxResolutionIdentity } from "../../components/use-outbox-resolution-identity";
import { getOwnerSyncDiagnostics, type OwnerSyncDiagnostics } from "../../sync-v3/owner-diagnostics";

type OwnerMetadataSnapshot = {
  identityKey: string;
  generation: number;
  value: OwnerSyncDiagnostics;
};

/** Keep read-only owner counts bound to the exact live auth/server/credential revision. */
export function useOwnerSyncDiagnostics(
  identity: OutboxResolutionIdentity | null,
  active: boolean,
): OwnerSyncDiagnostics | null {
  const identityKey = active && identity ? outboxResolutionDomainKey(identity) : null;
  const currentIdentityKey = useRef<string | null>(null);
  const generation = useRef(0);
  const [snapshot, setSnapshot] = useState<OwnerMetadataSnapshot | null>(null);

  // Invalidate as soon as a committed identity changes. The generation makes
  // A→B→A distinct from the first A even though the identity key is equal again.
  useLayoutEffect(() => {
    currentIdentityKey.current = identityKey;
    generation.current += 1;
    return () => {
      currentIdentityKey.current = null;
      generation.current += 1;
    };
  }, [identityKey]);

  useEffect(() => {
    if (!identity || !identityKey || !active) return;
    const requestGeneration = ++generation.current;
    let live = true;
    void getOwnerSyncDiagnostics(identity.authScope, identity).then((value) => {
      if (!live || generation.current !== requestGeneration || currentIdentityKey.current !== identityKey) return;
      setSnapshot({ identityKey, generation: requestGeneration, value });
    }).catch(() => {
      if (!live || generation.current !== requestGeneration || currentIdentityKey.current !== identityKey) return;
      setSnapshot(null);
    });
    return () => {
      live = false;
      generation.current += 1;
    };
  }, [active, identity, identityKey]);

  return snapshot?.identityKey === identityKey && snapshot.generation === generation.current
    ? snapshot.value
    : null;
}

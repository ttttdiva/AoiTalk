import AsyncStorage from "@react-native-async-storage/async-storage";
import { getToken, getTokenAuthScope } from "../../lib/auth";
import { getConfiguredApiServerFingerprint } from "../../lib/api-client";
import type { AoiDotsProfile, AoiDotsRootPointer } from "./types";

const STORAGE_PREFIX = "aoitalk:aoidots-root:v1:";

function scopeKey(authScope: string, serverFingerprint: string): string {
  return `${STORAGE_PREFIX}${encodeURIComponent(authScope)}:${encodeURIComponent(serverFingerprint)}`;
}

export async function readAoiDotsRootPointer(
  authScope: string,
  serverFingerprint: string,
): Promise<AoiDotsRootPointer | null> {
  const raw = await AsyncStorage.getItem(scopeKey(authScope, serverFingerprint));
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<AoiDotsRootPointer>;
    if (
      value.auth_scope !== authScope ||
      value.server_fingerprint !== serverFingerprint ||
      typeof value.root_id !== "string" ||
      typeof value.session_id !== "string" ||
      typeof value.agent_id !== "string"
    ) {
      return null;
    }
    return value as AoiDotsRootPointer;
  } catch {
    return null;
  }
}

export async function writeAoiDotsRootPointer(
  authScope: string,
  serverFingerprint: string,
  profile: AoiDotsProfile,
): Promise<AoiDotsRootPointer> {
  const pointer: AoiDotsRootPointer = {
    auth_scope: authScope,
    server_fingerprint: serverFingerprint,
    root_id: profile.root_id,
    session_id: profile.session_id,
    agent_id: profile.agent_id,
    cached_at: new Date().toISOString(),
  };
  await AsyncStorage.setItem(
    scopeKey(authScope, serverFingerprint),
    JSON.stringify(pointer),
  );
  return pointer;
}

/** The pointer is only a reconnect hint; callers must bootstrap the server. */
export async function readCurrentAoiDotsRootPointer(): Promise<AoiDotsRootPointer | null> {
  const [token, serverFingerprint] = await Promise.all([
    getToken(),
    getConfiguredApiServerFingerprint(),
  ]);
  return readAoiDotsRootPointer(getTokenAuthScope(token), serverFingerprint);
}

export async function clearAoiDotsRootPointer(
  authScope: string,
  serverFingerprint: string,
): Promise<void> {
  await AsyncStorage.removeItem(scopeKey(authScope, serverFingerprint));
}

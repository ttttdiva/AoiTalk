import { useCallback, useEffect, useState } from "react";
import { AppState } from "react-native";
import * as FileSystem from "expo-file-system/legacy";
import type { components } from "../../types/api-types.gen";
import { isApiConnectionError } from "../../lib/api-client";
import {
  assertRigScope,
  rigCacheRoot,
  rigPath,
  rigRequest,
  type RigScope,
} from "./rig-api";

export type PlaybackSettingsView =
  components["schemas"]["PlaybackSettingsView"];
export type PlaybackSettingsUpdate =
  components["schemas"]["PlaybackSettingsUpdate"];
export type PlaybackSettingsReset =
  components["schemas"]["PlaybackSettingsReset"];
export const playbackSettingsPath = (character: string, revision: string) =>
  `${rigPath(character)}/builds/${encodeURIComponent(revision)}/playback-settings?mode=conversation-v1`;
const settingsKey = (scope: RigScope, character: string, revision: string) =>
  JSON.stringify([
    scope.server,
    scope.authScope,
    character,
    revision,
    "conversation-v1",
  ]);
const snapshots = new Map<string, PlaybackSettingsView>();
const writes = new Map<string, Promise<void>>();
const listeners = new Map<string, Set<(value: PlaybackSettingsView) => void>>();
async function cachePath(scope: RigScope, character: string, revision: string) {
  return (
    (await rigCacheRoot(scope)) +
    encodeURIComponent(character) +
    "/" +
    encodeURIComponent(revision) +
    "/playback-conversation-v1.json"
  );
}
function publish(key: string, value: PlaybackSettingsView) {
  const previous = snapshots.get(key);
  // A slow read from before a save must not replace that acknowledged save.
  if (
    previous &&
    previous.manifest_sha256 === value.manifest_sha256 &&
    previous.revision > value.revision
  )
    return previous;
  snapshots.set(key, value);
  listeners.get(key)?.forEach((listener) => listener(value));
  return value;
}
async function persist(
  scope: RigScope,
  character: string,
  revision: string,
  value: PlaybackSettingsView,
  signal: AbortSignal,
) {
  const key = settingsKey(scope, character, revision);
  const work = (writes.get(key) ?? Promise.resolve())
    .catch(() => undefined)
    .then(async () => {
      await assertRigScope(scope, signal);
      const path = await cachePath(scope, character, revision);
      await FileSystem.makeDirectoryAsync(
        path.slice(0, path.lastIndexOf("/") + 1),
        { intermediates: true },
      );
      await assertRigScope(scope, signal);
      // Serialize writers and select the newest acknowledged snapshot at write time.
      await FileSystem.writeAsStringAsync(
        path,
        JSON.stringify(snapshots.get(key) ?? value),
      );
    });
  writes.set(key, work);
  try {
    await work;
  } finally {
    if (writes.get(key) === work) writes.delete(key);
  }
}
export async function readPlaybackSettings(
  scope: RigScope,
  character: string,
  revision: string,
  signal: AbortSignal,
): Promise<PlaybackSettingsView> {
  const key = settingsKey(scope, character, revision);
  await assertRigScope(scope, signal);
  try {
    const result = await rigRequest<PlaybackSettingsView>(
      scope,
      playbackSettingsPath(character, revision),
      { signal },
    );
    const value = publish(key, result);
    await persist(scope, character, revision, value, signal);
    return value;
  } catch (error) {
    if (!isApiConnectionError(error)) throw error;
    await assertRigScope(scope, signal);
    const cached =
      snapshots.get(key) ??
      (JSON.parse(
        await FileSystem.readAsStringAsync(
          await cachePath(scope, character, revision),
        ),
      ) as PlaybackSettingsView);
    if (
      cached.character_id !== character ||
      cached.build_id !== revision ||
      cached.mode !== "conversation-v1"
    )
      throw new Error("再生設定のキャッシュが一致しません");
    await assertRigScope(scope, signal);
    return publish(key, cached);
  }
}
export async function savePlaybackSettings(
  scope: RigScope,
  character: string,
  revision: string,
  body: PlaybackSettingsUpdate | PlaybackSettingsReset,
  reset: boolean,
  signal: AbortSignal,
) {
  const result = await rigRequest<PlaybackSettingsView>(
    scope,
    playbackSettingsPath(character, revision),
    { method: reset ? "DELETE" : "PATCH", body: JSON.stringify(body), signal },
  );
  await assertRigScope(scope, signal);
  const value = publish(settingsKey(scope, character, revision), result);
  // Remote persistence has succeeded. Do not misreport it as a failed save if the
  // optional offline cache is unavailable; remove an older disk snapshot instead.
  try {
    await persist(scope, character, revision, value, signal);
  } catch {
    await FileSystem.deleteAsync(await cachePath(scope, character, revision), {
      idempotent: true,
    }).catch(() => undefined);
  }
  return value;
}
export function useRigPlaybackSettings(
  scope: RigScope,
  character: string,
  revision: string,
) {
  const key = settingsKey(scope, character, revision);
  const [state, setState] = useState<{
    key: string;
    data?: PlaybackSettingsView;
    error?: string;
  }>({ key, data: snapshots.get(key) });
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const update = (data: PlaybackSettingsView) => {
      if (!controller.signal.aborted) setState({ key, data });
    };
    let set = listeners.get(key);
    if (!set) {
      set = new Set();
      listeners.set(key, set);
    }
    set.add(update);
    setState({ key, data: snapshots.get(key) });
    void readPlaybackSettings(scope, character, revision, controller.signal)
      .then(update)
      .catch((error) => {
        if (!controller.signal.aborted)
          setState((current) => ({
            key,
            data: current.key === key ? current.data : undefined,
            error:
              error instanceof Error
                ? error.message
                : "再生設定を取得できません",
          }));
      });
    return () => {
      controller.abort();
      set?.delete(update);
      if (!set?.size) listeners.delete(key);
    };
  }, [key, scope.server, scope.authScope, character, revision, refresh]);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (next) => {
      if (next === "active") setRefresh((v) => v + 1);
    });
    return () => subscription.remove();
  }, []);
  const reload = useCallback(() => setRefresh((v) => v + 1), []);
  return { ...(state.key === key ? state : {}), reload };
}

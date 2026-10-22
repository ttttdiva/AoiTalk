import * as FileSystem from "expo-file-system/legacy";
import { File } from "expo-file-system";
import { protectedFetch } from "./protected-fetch";

type Revision = string | number | null | undefined;
type RevisionReader = () => Revision | Promise<Revision>;
type ProtectedMediaRequest = (
  send: (headers: Record<string, string>) => Promise<Response>,
  signal?: AbortSignal,
) => Promise<Response>;

const inFlight = new Map<string, Promise<string>>();

function assertNotAborted(signal?: AbortSignal): void {
  if (signal?.aborted) {
    const error = new Error("メディア取得を中止しました");
    error.name = "AbortError";
    throw error;
  }
}

async function assertRevision(
  reader: RevisionReader | undefined,
  expected: Revision,
): Promise<void> {
  if (reader && (await reader()) !== expected) {
    throw new Error("認証または接続設定が変更されたため、メディア取得を破棄しました");
  }
}

async function removeQuietly(uri: string): Promise<void> {
  await FileSystem.deleteAsync(uri, { idempotent: true }).catch(() => undefined);
}

/** Stream protected media into a private temporary file, then publish it atomically. */
export async function cacheProtectedMediaResource(
  url: string,
  targetUri: string,
  headers: Record<string, string>,
  signal?: AbortSignal,
  options: {
    revision?: Revision;
    revisionReader?: RevisionReader;
    request?: ProtectedMediaRequest;
  } = {},
): Promise<string> {
  assertNotAborted(signal);
  const expectedRevision = options.revisionReader
    ? await options.revisionReader()
    : options.revision;
  await assertRevision(options.revisionReader, expectedRevision);

  const existing = await FileSystem.getInfoAsync(targetUri).catch(() => ({ exists: false }));
  if (existing.exists) {
    await assertRevision(options.revisionReader, expectedRevision);
    return targetUri;
  }

  const running = inFlight.get(targetUri);
  if (running) return running;

  const task = (async () => {
    const temporary = `${targetUri}.${Date.now().toString(36)}-${Math.random().toString(16).slice(2)}.partial`;
    let handle: ReturnType<File["open"]> | null = null;
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    let abortHandler: (() => void) | null = null;
    let published = false;
    try {
      assertNotAborted(signal);
      await assertRevision(options.revisionReader, expectedRevision);
      const response = options.request
        ? await options.request((requestHeaders) => protectedFetch(url, {
            method: "GET",
            headers: requestHeaders,
            signal,
            credentials: "omit",
            redirect: "manual",
          }), signal)
        : await protectedFetch(url, {
            method: "GET",
            headers,
            signal,
            credentials: "omit",
            redirect: "manual",
          });
      if (!response.ok) {
        throw new Error(`保護されたメディア取得に失敗しました (HTTP ${response.status})`);
      }
      if (!response.body) {
        throw new Error("保護されたメディアのstream bodyを利用できません");
      }
      const file = new File(temporary);
      file.create({ intermediates: true, overwrite: true });
      handle = file.open();
      reader = response.body.getReader();
      if (signal) {
        abortHandler = () => {
          void reader?.cancel().catch(() => undefined);
        };
        signal.addEventListener("abort", abortHandler, { once: true });
      }
      for (;;) {
        assertNotAborted(signal);
        const chunk = await reader.read();
        if (chunk.done) break;
        if (chunk.value?.length) handle.writeBytes(chunk.value);
      }
      await assertRevision(options.revisionReader, expectedRevision);
      assertNotAborted(signal);
      // Expo's native file handle must be released before the atomic move.
      // Keeping it open can leave a platform-specific partial file behind or
      // make the final path visible before all bytes are durable.
      handle.close();
      handle = null;
      await FileSystem.moveAsync({ from: temporary, to: targetUri });
      published = true;
      await assertRevision(options.revisionReader, expectedRevision);
      return targetUri;
    } catch (error) {
      await reader?.cancel().catch(() => undefined);
      if (published) await removeQuietly(targetUri);
      throw error;
    } finally {
      if (signal && abortHandler) signal.removeEventListener("abort", abortHandler);
      reader?.releaseLock?.();
      // Release native handles before deleting a failed partial file.
      handle?.close();
      await removeQuietly(temporary);
    }
  })();
  inFlight.set(targetUri, task);
  void task.then(
    () => { if (inFlight.get(targetUri) === task) inFlight.delete(targetUri); },
    () => { if (inFlight.get(targetUri) === task) inFlight.delete(targetUri); },
  );
  return task;
}

export function clearProtectedMediaFlights(): void {
  inFlight.clear();
}

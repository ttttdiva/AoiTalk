import * as FileSystem from "expo-file-system/legacy";
import { File } from "expo-file-system";
import { FILES_DOWNLOAD_IDLE_TIMEOUT_MS } from "../constants/config";
import { protectedFetch } from "./protected-fetch";

type Revision = string | number | null | undefined;
type RevisionReader = () => Revision | Promise<Revision>;
type ProtectedMediaRequest = (
  send: (
    headers: Record<string, string>,
    signal?: AbortSignal,
  ) => Promise<Response>,
  signal?: AbortSignal,
) => Promise<Response>;

export type MediaCacheProgress = {
  receivedBytes: number;
  totalBytes?: number;
  percent?: number;
};

type MediaCacheProgressCallback = (progress: MediaCacheProgress) => void;

const PROGRESS_THROTTLE_MS = 100;

const inFlight = new Map<string, Promise<string>>();

function assertNotAborted(signal?: AbortSignal): void {
  if (signal?.aborted) {
    throw createAbortError();
  }
}

function createAbortError(): Error {
  const error = new Error("ファイル取得を中止しました");
  error.name = "AbortError";
  return error;
}

class MediaTransferIdleTimeoutError extends Error {
  readonly timeoutMs: number;

  constructor(timeoutMs: number) {
    super(`ファイル受信が${Math.ceil(timeoutMs / 1000)}秒間進まなかったため中止しました`);
    this.name = "TimeoutError";
    this.timeoutMs = timeoutMs;
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
  try {
    await FileSystem.deleteAsync(uri, { idempotent: true });
  } catch {
    // Cleanup is best effort and must not mask the transfer error.
  }
}

function cancelResponseBodyQuietly(response: Response): void {
  try {
    void Promise.resolve(response.body?.cancel()).catch(() => undefined);
  } catch {
    // A late response must not mask the timeout or abort that already won.
  }
}

function waitForAbortablePromise<T>(
  operation: Promise<T>,
  signal?: AbortSignal,
): Promise<T> {
  if (!signal) return operation;
  if (signal.aborted) return Promise.reject(createAbortError());
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      signal.removeEventListener("abort", onAbort);
      reject(createAbortError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
    void operation.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        if (signal.aborted) reject(createAbortError());
        else resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(signal.aborted ? createAbortError() : error);
      },
    );
    if (signal.aborted) onAbort();
  });
}

function readContentLength(response: Response): number | undefined {
  const getHeader = response.headers?.get;
  if (typeof getHeader !== "function") return undefined;
  const raw = getHeader.call(response.headers, "content-length");
  if (!raw || !/^\d+$/.test(raw.trim())) return undefined;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

function progressEvent(
  receivedBytes: number,
  totalBytes: number | undefined,
): MediaCacheProgress {
  // A mismatched Content-Length cannot be used to calculate a trustworthy
  // percentage.  Keep the byte count visible while omitting the invalid total.
  const canUseTotal =
    totalBytes !== undefined && receivedBytes <= totalBytes;
  const event: MediaCacheProgress = { receivedBytes };
  if (canUseTotal) {
    event.totalBytes = totalBytes;
    if (totalBytes > 0) {
      event.percent = Math.min(100, (receivedBytes / totalBytes) * 100);
    }
  }
  return event;
}

function reportProgress(
  callback: MediaCacheProgressCallback | undefined,
  event: MediaCacheProgress,
): void {
  try {
    callback?.(event);
  } catch {
    // Progress reporting must never change the native file operation result.
  }
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
    onProgress?: MediaCacheProgressCallback;
    idleTimeoutMs?: number;
  } = {},
): Promise<string> {
  assertNotAborted(signal);
  const expectedRevision = options.revisionReader
    ? await options.revisionReader()
    : options.revision;
  assertNotAborted(signal);
  await assertRevision(options.revisionReader, expectedRevision);

  const existing = await FileSystem.getInfoAsync(targetUri).catch(() => ({ exists: false }));
  assertNotAborted(signal);
  if (existing.exists) {
    await assertRevision(options.revisionReader, expectedRevision);
    assertNotAborted(signal);
    return targetUri;
  }

  const running = inFlight.get(targetUri);
  if (running) return waitForAbortablePromise(running, signal);

  const task = (async () => {
    const temporary = `${targetUri}.${Date.now().toString(36)}-${Math.random().toString(16).slice(2)}.partial`;
    const transferController = new AbortController();
    let handle: ReturnType<File["open"]> | null = null;
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    let published = false;
    let cancelStarted = false;
    let idleTimer: ReturnType<typeof setTimeout> | null = null;
    let interruptionError: Error | null = null;
    let rejectInterrupted!: (error: Error) => void;
    const interrupted = new Promise<never>((_resolve, reject) => {
      rejectInterrupted = reject;
    });
    // A rejection can happen before the first Promise.race is attached.
    void interrupted.catch(() => undefined);
    const cancelReaderQuietly = () => {
      if (!reader || cancelStarted) return;
      cancelStarted = true;
      try {
        void Promise.resolve(reader.cancel()).catch(() => undefined);
      } catch {
        // Native stream cancellation is best effort during cleanup.
      }
    };
    const interrupt = (error: Error) => {
      if (interruptionError) return;
      interruptionError = error;
      rejectInterrupted(error);
      transferController.abort();
      cancelReaderQuietly();
    };
    const externalAbortHandler = () => interrupt(createAbortError());
    const configuredIdleTimeout =
      options.idleTimeoutMs ?? FILES_DOWNLOAD_IDLE_TIMEOUT_MS;
    const idleTimeoutMs = Math.max(1, configuredIdleTimeout);
    const resetIdleTimeout = () => {
      if (idleTimer !== null) clearTimeout(idleTimer);
      idleTimer = setTimeout(
        () => interrupt(new MediaTransferIdleTimeoutError(idleTimeoutMs)),
        idleTimeoutMs,
      );
    };
    const awaitActivity = <T>(
      operation: () => Promise<T>,
      onLateResolve?: (value: T) => void,
    ): Promise<T> => {
      if (interruptionError) return Promise.reject(interruptionError);
      resetIdleTimeout();
      const activity = Promise.resolve().then(operation);
      if (onLateResolve) {
        void activity.then(
          (value) => {
            if (interruptionError) onLateResolve(value);
          },
          () => undefined,
        ).catch(() => undefined);
      }
      return Promise.race([
        activity,
        interrupted,
      ]);
    };
    signal?.addEventListener("abort", externalAbortHandler, { once: true });
    if (signal?.aborted) externalAbortHandler();
    try {
      assertNotAborted(signal);
      await assertRevision(options.revisionReader, expectedRevision);
      const requestResponse = () => {
        if (options.request) {
          return options.request((requestHeaders, requestSignal) => protectedFetch(url, {
            method: "GET",
            headers: requestHeaders,
            signal: requestSignal ?? transferController.signal,
            credentials: "omit",
            redirect: "manual",
          }), transferController.signal);
        }
        return protectedFetch(url, {
          method: "GET",
          headers,
          signal: transferController.signal,
          credentials: "omit",
          redirect: "manual",
        });
      };
      const response = await awaitActivity(
        requestResponse,
        cancelResponseBodyQuietly,
      );
      if (interruptionError) throw interruptionError;
      if (!response.ok) {
        throw new Error(`保護されたメディア取得に失敗しました (HTTP ${response.status})`);
      }
      if (!response.body) {
        throw new Error("保護されたメディアのstream bodyを利用できません");
      }
      const totalBytes = readContentLength(response);
      let receivedBytes = 0;
      let lastReportedAt = Number.NEGATIVE_INFINITY;
      const reportReceivedBytes = (force = false) => {
        const now = Date.now();
        if (!force && now - lastReportedAt < PROGRESS_THROTTLE_MS) return;
        lastReportedAt = now;
        reportProgress(
          options.onProgress,
          progressEvent(receivedBytes, totalBytes),
        );
      };
      const file = new File(temporary);
      file.create({ intermediates: true, overwrite: true });
      handle = file.open();
      reader = response.body.getReader();
      for (;;) {
        assertNotAborted(signal);
        const chunk = await awaitActivity(() => reader!.read());
        if (chunk.done) break;
        if (chunk.value?.length) {
          handle.writeBytes(chunk.value);
          receivedBytes += chunk.value.length;
          reportReceivedBytes();
        }
      }
      if (idleTimer !== null) {
        clearTimeout(idleTimer);
        idleTimer = null;
      }
      // The final byte count must be observable even when all chunks arrived
      // within the throttle window or the final chunk was empty.
      reportReceivedBytes(true);
      await assertRevision(options.revisionReader, expectedRevision);
      assertNotAborted(signal);
      // Expo's native file handle must be released before the atomic move.
      // Keeping it open can leave a platform-specific partial file behind or
      // make the final path visible before all bytes are durable.
      handle.close();
      handle = null;
      await FileSystem.moveAsync({ from: temporary, to: targetUri });
      published = true;
      if (interruptionError) throw interruptionError;
      assertNotAborted(signal);
      await assertRevision(options.revisionReader, expectedRevision);
      if (interruptionError) throw interruptionError;
      assertNotAborted(signal);
      return targetUri;
    } catch (error) {
      cancelReaderQuietly();
      if (published) await removeQuietly(targetUri);
      throw error;
    } finally {
      if (idleTimer !== null) clearTimeout(idleTimer);
      signal?.removeEventListener("abort", externalAbortHandler);
      try {
        reader?.releaseLock?.();
      } catch {
        // Still close the native file and remove the scratch file below.
      }
      // Release native handles before deleting a failed partial file.
      try {
        handle?.close();
      } catch {
        // A close error must not prevent temporary-file cleanup.
      }
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

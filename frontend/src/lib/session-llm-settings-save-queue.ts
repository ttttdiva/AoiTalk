type SessionQueueEntry = {
  tail: Promise<unknown>;
  lastError: Error | null;
};

const queuesBySession = new Map<string, SessionQueueEntry>();

function getOrCreateQueue(sessionId: string): SessionQueueEntry {
  let entry = queuesBySession.get(sessionId);
  if (!entry) {
    entry = { tail: Promise.resolve(), lastError: null };
    queuesBySession.set(sessionId, entry);
  }
  return entry;
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

function chainTail(sessionId: string, entry: SessionQueueEntry, tailPromise: Promise<unknown>): void {
  entry.tail = tailPromise;
  tailPromise.finally(() => {
    if (entry.tail !== tailPromise) return;
    if (entry.lastError) {
      entry.tail = Promise.resolve();
      return;
    }
    queuesBySession.delete(sessionId);
  });
}

/**
 * 同一 session の PUT を直列化する。呼び出し時点でキューに積まれた操作はすべて完了するまで
 * `awaitSessionLlmSettingsReady` が待機する。
 */
export function enqueueSessionLlmSettingsSave<T>(
  sessionId: string,
  operation: () => Promise<T>,
): Promise<T> {
  const cleanSessionId = sessionId.trim();
  if (!cleanSessionId) {
    return operation();
  }

  const entry = getOrCreateQueue(cleanSessionId);
  const resultPromise = entry.tail.catch(() => undefined).then(() => operation());

  const tailPromise = resultPromise
    .then(() => {
      entry.lastError = null;
    })
    .catch((error) => {
      entry.lastError = toError(error);
    });

  chainTail(cleanSessionId, entry, tailPromise);
  return resultPromise;
}

/** 既に開始済みの Promise をキュー末尾へつなぐ（レガシー呼び出し互換）。 */
export function registerSessionLlmSettingsSave(
  sessionId: string,
  promise: Promise<unknown>,
): void {
  const cleanSessionId = sessionId.trim();
  if (!cleanSessionId) return;

  const entry = getOrCreateQueue(cleanSessionId);
  const tailPromise = entry.tail
    .catch(() => undefined)
    .then(() => promise)
    .then(() => {
      entry.lastError = null;
    })
    .catch((error) => {
      entry.lastError = toError(error);
    });

  chainTail(cleanSessionId, entry, tailPromise);
}

export async function awaitSessionLlmSettingsReady(sessionId: string): Promise<void> {
  const cleanSessionId = sessionId.trim();
  if (!cleanSessionId) return;

  const entry = queuesBySession.get(cleanSessionId);
  if (!entry) return;

  await entry.tail;
  if (entry.lastError) {
    throw entry.lastError;
  }
}

export function resetSessionLlmSettingsSaveQueue(): void {
  queuesBySession.clear();
}

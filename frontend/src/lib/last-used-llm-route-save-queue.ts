type UserQueueEntry = {
  tail: Promise<unknown>;
  lastError: Error | null;
};

const queuesByUser = new Map<string, UserQueueEntry>();

function getOrCreateQueue(userKey: string): UserQueueEntry {
  let entry = queuesByUser.get(userKey);
  if (!entry) {
    entry = { tail: Promise.resolve(), lastError: null };
    queuesByUser.set(userKey, entry);
  }
  return entry;
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

function chainTail(userKey: string, entry: UserQueueEntry, tailPromise: Promise<unknown>): void {
  entry.tail = tailPromise;
  tailPromise.finally(() => {
    if (entry.tail !== tailPromise) return;
    if (entry.lastError) {
      entry.tail = Promise.resolve();
      return;
    }
    queuesByUser.delete(userKey);
  });
}

/**
 * 同一ユーザーの last-used PUT を直列化する。呼び出し時点の操作は完了まで
 * `awaitLastUsedLlmRouteReady` が待機する。
 */
export function enqueueLastUsedLlmRouteSave<T>(
  userId: string,
  operation: () => Promise<T>,
): Promise<T> {
  const userKey = userId.trim() || "__default__";

  const entry = getOrCreateQueue(userKey);
  const resultPromise = entry.tail.catch(() => undefined).then(() => operation());

  const tailPromise = resultPromise
    .then(() => {
      entry.lastError = null;
    })
    .catch((error) => {
      entry.lastError = toError(error);
    });

  chainTail(userKey, entry, tailPromise);
  return resultPromise;
}

export async function awaitLastUsedLlmRouteReady(userId?: string | null): Promise<void> {
  const userKey = (userId ?? "").trim() || "__default__";
  const entry = queuesByUser.get(userKey);
  if (!entry) return;

  await entry.tail;
  if (entry.lastError) {
    throw entry.lastError;
  }
}

export function resetLastUsedLlmRouteSaveQueue(): void {
  queuesByUser.clear();
}

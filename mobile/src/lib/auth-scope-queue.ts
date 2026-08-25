/**
 * Serialize every operation that can read or write auth-scoped SQLite data.
 *
 * Authentication transitions and remote cache refreshes share this tail so a
 * transition cannot clear a scope in the middle of another scope's fetch and
 * SQLite projection.  Rejections are deliberately swallowed only on the
 * tail; the caller still receives the original operation result/error.
 */

type AsyncOperation<T> = () => T | PromiseLike<T>;

let authScopeExclusiveTail: Promise<void> = Promise.resolve();

export function enqueueAuthScopeExclusive<T>(
  operation: AsyncOperation<T>,
): Promise<T> {
  const queued = authScopeExclusiveTail.catch(() => undefined).then(operation);
  authScopeExclusiveTail = queued.then(
    () => undefined,
    () => undefined,
  );
  return queued;
}

export function runAuthScopeTransition<T>(
  callback: AsyncOperation<T>,
): Promise<T> {
  return enqueueAuthScopeExclusive(callback);
}

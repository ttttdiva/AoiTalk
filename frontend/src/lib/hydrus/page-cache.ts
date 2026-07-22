export function getOrCreatePagePromise<T>(
  cache: Map<string, Promise<T>>,
  key: string,
  loader: () => Promise<T>,
  maxEntries = 8,
): Promise<T> {
  const cached = cache.get(key);
  if (cached) return cached;
  const pending = loader().catch((error) => {
    if (cache.get(key) === pending) cache.delete(key);
    throw error;
  });
  while (cache.size >= maxEntries) {
    const oldest = cache.keys().next().value as string | undefined;
    if (oldest == null) break;
    cache.delete(oldest);
  }
  cache.set(key, pending);
  return pending;
}

/**
 * TanStack Query client + AsyncStorage persister.
 *
 * The query cache is persisted across app restarts so the UI can paint with
 * the last known server state even before the Sync Engine completes its pull.
 * Combined with the local SQLite cache this gives Local-First reads while
 * keeping React Query's in-memory query dedupe/refetch semantics.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import { QueryClient } from '@tanstack/react-query';
import { createAsyncStoragePersister } from '@tanstack/query-async-storage-persister';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Aggressive caching: rely on Sync Engine + invalidation, not auto refetch.
      staleTime: 30_000,
      gcTime: 1000 * 60 * 60 * 24, // 24h — keep persisted entries alive a day
      retry: 2,
      refetchOnWindowFocus: false,
    },
    mutations: {
      retry: 0,
    },
  },
});

export const asyncStoragePersister = createAsyncStoragePersister({
  storage: AsyncStorage,
  key: 'aoitalk-rq-cache',
  throttleTime: 1000,
});

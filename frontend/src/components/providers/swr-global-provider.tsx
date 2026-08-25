"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { SWRConfig, useSWRConfig, type Cache } from "swr";
import {
  configurePersistentCacheUser,
  discardLegacyPersistentCache,
  PersistentSwrCache,
  getPersistentStore,
  hydrateSwrCacheMap,
  type SwrState,
} from "@/lib/persistent-cache";
import { resetChatMessageCacheMemory } from "@/lib/chat-message-cache";

// AppLayout の getSession() で確定したユーザー ID をクライアントツリーへ
// 渡す。localStorage を使う機能が認証状態を再取得する競合を避けるため、
// SWR の永続キャッシュ Provider と同じ境界で公開する。
const CurrentUserIdContext = createContext<string | null | undefined>(undefined);

export function useCurrentUserId(): string | null | undefined {
  return useContext(CurrentUserIdContext);
}

function PersistentCacheHydrator() {
  const { cache, mutate } = useSWRConfig();

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await discardLegacyPersistentCache();
      const hydrated = new Map<string, SwrState>();
      await hydrateSwrCacheMap(hydrated);
      if (cancelled) return;

      for (const [key, state] of hydrated) {
        // 先にネットワーク応答が届いていれば、新しい値を古い永続値で上書きしない。
        if (cache.get(key)?.data !== undefined || state.data === undefined) {
          continue;
        }
        await mutate(key, state.data, { revalidate: false });
        if (cancelled) return;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [cache, mutate]);

  return null;
}

/**
 * アプリ全体を SWRConfig でラップし、SWR キャッシュを IndexedDB に永続化する。
 * 低帯域配慮の共通設定（フォーカス再検証オフ・前回データ保持・重複排除）を既定にする。
 *
 * アプリシェルは待たずに描画し、IndexedDB のデータをバックグラウンドで SWR に
 * 注入する。低速・不調な IndexedDB が画面全体をブロックしない。
 */
export function SwrGlobalProvider({
  children,
  userId,
}: {
  children: React.ReactNode;
  userId: string | null;
}) {
  // 子コンポーネントが Docs / Chat キャッシュを読む前にユーザー専用 DB を選ぶ。
  configurePersistentCacheUser(userId);

  const [cacheMap] = useState(() => new Map<string, SwrState>());
  const [persistentCache] = useState(() => {
    // Provider は userId を key にして再生成される。セッション別のメモリ cursor も
    // 同時に破棄し、別ユーザーへ持ち越さない。
    resetChatMessageCacheMemory();
    return new PersistentSwrCache(
      cacheMap,
      getPersistentStore(),
    );
  });

  const provider = useCallback(
    (): Cache => persistentCache as unknown as Cache,
    [persistentCache],
  );
  useEffect(
    () => () => {
      void persistentCache.dispose();
    },
    [persistentCache],
  );

  return (
    <CurrentUserIdContext.Provider value={userId}>
      <SWRConfig
        value={{
          provider,
          revalidateOnFocus: false,
          revalidateOnReconnect: true,
          keepPreviousData: true,
          dedupingInterval: 5000,
        }}
      >
        <PersistentCacheHydrator />
        {children}
      </SWRConfig>
    </CurrentUserIdContext.Provider>
  );
}

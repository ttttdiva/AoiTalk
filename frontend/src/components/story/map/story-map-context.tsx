"use client";

import { createContext, useContext, useSyncExternalStore } from "react";

/**
 * 検索ハイライトを購読制で配る軽量ストア。
 *
 * ハイライトを各ノードの `data` に埋めると、1 件の変化で全ノードの `data` を作り直すことになり
 * `React.memo` が無効化される（設計書 §10.2 規約 1/2 違反）。ストア + `useSyncExternalStore` の
 * セレクタ購読にすることで、真偽値が変わったノードだけを再描画する。
 */
export type StoryMapHighlightStore = {
  subscribe: (listener: () => void) => () => void;
  read: () => string | null;
  set: (episodeId: string | null) => void;
};

export function createStoryMapHighlightStore(): StoryMapHighlightStore {
  let current: string | null = null;
  const listeners = new Set<() => void>();
  return {
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    read: () => current,
    set: (episodeId) => {
      if (current === episodeId) return;
      current = episodeId;
      for (const listener of listeners) listener();
    },
  };
}

/**
 * ノードカードから呼ぶ操作。関数の同一性を固定してキャンバス側の再生成と切り離すため、
 * `data` ではなく context 経由で配る。
 */
export type StoryMapNodeActions = {
  openMenu: (episodeId: string, point: { x: number; y: number }) => void;
  editPremise: (episodeId: string) => void;
};

type StoryMapContextValue = {
  highlight: StoryMapHighlightStore;
  actions: StoryMapNodeActions;
};

const StoryMapContext = createContext<StoryMapContextValue | null>(null);

export const StoryMapProvider = StoryMapContext.Provider;

const noopSubscribe = () => () => {};
const readFalse = () => false;

export function useStoryMapNodeActions(): StoryMapNodeActions {
  const value = useContext(StoryMapContext);
  if (!value) throw new Error("StoryMapContext がありません");
  return value.actions;
}

export function useStoryMapHighlighted(episodeId: string): boolean {
  const store = useContext(StoryMapContext)?.highlight;
  return useSyncExternalStore(
    store?.subscribe ?? noopSubscribe,
    store ? () => store.read() === episodeId : readFalse,
    readFalse,
  );
}

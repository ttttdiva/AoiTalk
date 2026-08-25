"use client";

import { useEffect, useLayoutEffect, useMemo } from "react";
import {
  createIncrementalSearchController,
  type IncrementalSearchController,
  type IncrementalSearchSurface,
} from "@/lib/explorer/incremental-search-controller";

/**
 * Files のインクリメンタル検索を各 UI へ共有する。
 *
 * 返される controller の identity は安定しているため、キーボードハンドラの依存配列を
 * 汚さない。surface は commit 後の layout effect で差し替える。render 本体からは
 * controller の永続状態を触らないので、commit されなかった render が検索条件を
 * 壊さない。layout effect は paint 前・通常の非同期 response より先に走るため、
 * commit 後〜passive effect の窓で古い surface へ Migemo 結果が載ることもない。
 */
export function useIncrementalSearch(
  surface: IncrementalSearchSurface,
): IncrementalSearchController {
  const controller = useMemo(
    () => createIncrementalSearchController(surface),
    // 初期 surface だけを使って一度だけ生成する。以降は setSurface で更新する。
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  useLayoutEffect(() => {
    controller.setSurface(surface);
  }, [controller, surface]);

  useEffect(() => () => controller.reset(), [controller]);

  return controller;
}

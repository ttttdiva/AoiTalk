import { InteractionManager } from "react-native";
import { runSync } from "../sync/engine";

/**
 * 画面遷移やdrawerのアニメーションが終わるまで同期を開始しない。
 *
 * 同期完了は画面操作の完了条件にしない。完了後のローカル再読込が必要な
 * callerだけがcallbackを渡し、画面が離れた場合はcallbackも破棄する。
 */
export function scheduleSyncAfterInteractions(
  callback?: () => void | Promise<void>,
): () => void {
  let cancelled = false;
  const task = InteractionManager.runAfterInteractions(() => {
    if (cancelled) return;
    void runSync()
      .then(() => {
        if (!cancelled) return callback?.();
        return undefined;
      })
      .catch(() => undefined);
  });

  return () => {
    cancelled = true;
    task.cancel();
  };
}

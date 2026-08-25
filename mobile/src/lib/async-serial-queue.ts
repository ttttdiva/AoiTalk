export type AsyncSerialQueue = {
  enqueue<T>(operation: () => Promise<T>): Promise<T>;
};

/**
 * 非同期更新を呼び出し順に直列化する。
 *
 * 1件が失敗しても後続を止めず、サーバーへ古い更新が後着することを防ぐ。
 */
export function createAsyncSerialQueue(): AsyncSerialQueue {
  let tail: Promise<void> = Promise.resolve();

  return {
    enqueue<T>(operation: () => Promise<T>): Promise<T> {
      const result = tail.then(operation, operation);
      tail = result.then(
        () => undefined,
        () => undefined,
      );
      return result;
    },
  };
}

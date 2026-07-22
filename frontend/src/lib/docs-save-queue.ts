export type DocsSaveOperation<T> = {
  execute: () => Promise<T>;
  apply?: (value: T) => void;
};

type FailedOperation<T> = DocsSaveOperation<T> & {
  revision: number;
};

/**
 * ノード単位で書き込みを直列化するキュー。
 *
 * API は同じノードへの PATCH/DELETE を同じ順序で処理し、古い応答は
 * state に適用しない。サーバー側の更新自体は順番に実行されるため、
 * 最後のリクエストが最終状態を決める。
 */
export class DocsSaveQueue<T> {
  private readonly chains = new Map<string, Promise<void>>();
  private readonly revisions = new Map<string, number>();
  private readonly failed = new Map<string, FailedOperation<T>>();

  enqueue(nodeId: string, operation: DocsSaveOperation<T>): Promise<T> {
    const revision = (this.revisions.get(nodeId) ?? 0) + 1;
    this.revisions.set(nodeId, revision);
    this.failed.delete(nodeId);

    let resolveResult!: (value: T | PromiseLike<T>) => void;
    let rejectResult!: (reason?: unknown) => void;
    const result = new Promise<T>((resolve, reject) => {
      resolveResult = resolve;
      rejectResult = reject;
    });

    const previous = this.chains.get(nodeId) ?? Promise.resolve();
    const run = previous.catch(() => undefined).then(async () => {
      try {
        const value = await operation.execute();
        if (this.revisions.get(nodeId) === revision) {
          operation.apply?.(value);
          this.failed.delete(nodeId);
        }
        resolveResult(value);
      } catch (error) {
        if (this.revisions.get(nodeId) === revision) {
          this.failed.set(nodeId, { ...operation, revision });
        }
        rejectResult(error);
      }
    });
    const settled = run.finally(() => {
      if (this.chains.get(nodeId) === settled) this.chains.delete(nodeId);
    });
    this.chains.set(nodeId, settled);
    return result;
  }

  retry(nodeId: string): Promise<T> | null {
    const failed = this.failed.get(nodeId);
    if (!failed || this.revisions.get(nodeId) !== failed.revision) return null;
    return this.enqueue(nodeId, failed);
  }

  hasPending(nodeId?: string): boolean {
    return nodeId ? this.chains.has(nodeId) : this.chains.size > 0;
  }

  hasFailed(nodeId: string): boolean {
    return this.failed.has(nodeId);
  }

  async flush(): Promise<void> {
    await Promise.all(Array.from(this.chains.values()).map((chain) => chain.catch(() => undefined)));
  }
}

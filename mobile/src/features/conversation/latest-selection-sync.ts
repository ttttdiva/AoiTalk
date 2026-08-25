export type LatestSelectionTask<T> = {
  scope: string;
  value: T;
  revision: number;
};

export type LatestSelectionSyncEvent<T, TResult> =
  | { status: "idle" }
  | { status: "pending"; task: LatestSelectionTask<T> }
  | { status: "syncing"; task: LatestSelectionTask<T> }
  | { status: "success"; task: LatestSelectionTask<T>; result: TResult }
  | { status: "failure"; task: LatestSelectionTask<T>; error: unknown };

type EnqueueOptions = {
  /** offline既知時は永続pendingだけを作り、reconnectのretryまで通信しない。 */
  defer?: boolean;
  immediate?: boolean;
};

/**
 * 最新選択だけを同期する小さなsingle-flight queue。
 *
 * - debounce中の連続選択は最後の1件へcoalesceする。
 * - request A送信後にBを選んだ場合、A完了をUIへ適用せず、その後Bを送る。
 * - scope変更後に旧account/serverのresponseが完了しても通知しない。
 * - failureは自動loopせず、reconnect等からretryされた時だけ最新値を再送する。
 */
export class LatestSelectionSynchronizer<T, TResult> {
  private scope: string | null = null;
  private revision = 0;
  private desiredTask: LatestSelectionTask<T> | null = null;
  private activeTask: LatestSelectionTask<T> | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;

  constructor(
    private readonly execute: (task: LatestSelectionTask<T>) => Promise<TResult>,
    private readonly onEvent: (event: LatestSelectionSyncEvent<T, TResult>) => void,
    private readonly debounceMs = 120,
  ) {}

  setScope(scope: string): void {
    if (this.scope === scope) return;
    this.clearTimer();
    this.scope = scope;
    this.revision += 1;
    this.desiredTask = null;
    // 旧scopeのflightはcancelできないが、参照を切って新scopeを待たせない。
    this.activeTask = null;
    this.onEvent({ status: "idle" });
  }

  enqueue(scope: string, value: T, options: EnqueueOptions = {}): LatestSelectionTask<T> {
    if (this.disposed) throw new Error("LatestSelectionSynchronizer is disposed");
    this.setScope(scope);
    const task = { scope, value, revision: ++this.revision };
    this.desiredTask = task;
    this.onEvent({ status: "pending", task });
    if (!options.defer) {
      this.schedule(options.immediate ? 0 : this.debounceMs);
    }
    return task;
  }

  retry(): void {
    if (!this.desiredTask || this.disposed) return;
    this.onEvent({ status: "pending", task: this.desiredTask });
    this.schedule(0);
  }

  pendingTask(): LatestSelectionTask<T> | null {
    return this.desiredTask;
  }

  hasPending(scope?: string): boolean {
    return Boolean(
      this.desiredTask && (!scope || this.desiredTask.scope === scope),
    );
  }

  dispose(): void {
    this.disposed = true;
    this.clearTimer();
    this.desiredTask = null;
    this.activeTask = null;
  }

  private clearTimer(): void {
    if (!this.timer) return;
    clearTimeout(this.timer);
    this.timer = null;
  }

  private schedule(delay: number): void {
    this.clearTimer();
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.flush();
    }, delay);
  }

  private isCurrent(task: LatestSelectionTask<T>): boolean {
    return (
      !this.disposed &&
      this.scope === task.scope &&
      this.desiredTask?.revision === task.revision
    );
  }

  private async flush(): Promise<void> {
    const task = this.desiredTask;
    if (!task || this.disposed || task.scope !== this.scope) return;
    if (this.activeTask?.scope === task.scope) return;
    this.activeTask = task;
    if (this.isCurrent(task)) this.onEvent({ status: "syncing", task });

    try {
      const result = await this.execute(task);
      if (this.isCurrent(task)) {
        this.desiredTask = null;
        this.onEvent({ status: "success", task, result });
      }
    } catch (error) {
      if (this.isCurrent(task)) {
        this.onEvent({ status: "failure", task, error });
      }
    } finally {
      if (this.activeTask === task) this.activeTask = null;
      const next = this.desiredTask;
      if (
        next &&
        next.scope === this.scope &&
        next.revision > task.revision
      ) {
        this.schedule(0);
      }
    }
  }
}

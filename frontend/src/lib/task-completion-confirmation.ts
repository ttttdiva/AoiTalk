export type IncompleteSubtaskSummary = {
  id: string;
  title: string;
  status: string;
};

export type TaskCompletionConfirmationRequest = {
  taskId: string;
  incompleteSubtasks: IncompleteSubtaskSummary[];
};

type TaskCompletionConfirmationHandler = (
  request: TaskCompletionConfirmationRequest,
) => Promise<boolean>;

let confirmationHandler: TaskCompletionConfirmationHandler | null = null;
const pendingByTaskId = new Map<string, Promise<boolean>>();

export function registerTaskCompletionConfirmationHandler(
  handler: TaskCompletionConfirmationHandler,
): () => void {
  confirmationHandler = handler;
  return () => {
    if (confirmationHandler === handler) confirmationHandler = null;
  };
}

export function requestTaskCompletionConfirmation(
  request: TaskCompletionConfirmationRequest,
): Promise<boolean> {
  const existing = pendingByTaskId.get(request.taskId);
  if (existing) return existing;
  if (!confirmationHandler) return Promise.resolve(false);

  const pending = confirmationHandler(request);
  pendingByTaskId.set(request.taskId, pending);
  const clearPending = () => {
    if (pendingByTaskId.get(request.taskId) === pending) {
      pendingByTaskId.delete(request.taskId);
    }
  };
  void pending.then(clearPending, clearPending);
  return pending;
}

export class TaskCompletionCancelledError extends Error {
  constructor() {
    super("タスクの完了をキャンセルしました");
    this.name = "TaskCompletionCancelledError";
  }
}

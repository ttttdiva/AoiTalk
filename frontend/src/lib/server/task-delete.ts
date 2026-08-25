/**
 * @deprecated Task deletion is owned by FastAPI. This compatibility module
 * remains only for older test/mocking imports; it contains no Drizzle
 * hard-delete implementation.
 */

export async function collectTaskTreeIds(_rootTaskId: string): Promise<never> {
  throw new Error("Task deletion is delegated to the canonical FastAPI service");
}

export async function deleteTaskTreeRows(_taskIds: string[]): Promise<never> {
  throw new Error("Task deletion is delegated to the canonical FastAPI service");
}

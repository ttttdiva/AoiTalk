export async function measureTaskApi<T extends Response>(operation: "list" | "detail" | "create", handler: () => Promise<T>): Promise<T> {
  const start = Date.now();
  try {
    const response = await handler();
    const elapsed = Date.now() - start;
    if (elapsed >= 1000 || response.status >= 400) console.info("Task API", { operation, elapsed, status: response.status, timestamp: new Date().toISOString() });
    return response;
  } catch (error) {
    console.info("Task API", { operation, elapsed: Date.now() - start, status: 500, timestamp: new Date().toISOString(), ...(error instanceof Error && /^(Error|TypeError|RangeError|SyntaxError|ReferenceError|URIError|EvalError)$/.test(error.name) ? { errorName: error.name } : {}) });
    throw error;
  }
}

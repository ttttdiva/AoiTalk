/** Transfer cancellation ownership to the response after upstream headers arrive.
 * Next can abort the incoming GET signal when its handler returns, before a
 * large download drains. Real downstream cancellation must still stop fetch.
 */
export function createDownstreamResponseLifetime(requestSignal: AbortSignal) {
  const upstream = new AbortController();
  const abort = () => upstream.abort(requestSignal.reason);
  if (requestSignal.aborted) abort();
  else requestSignal.addEventListener("abort", abort, { once: true });

  const detach = () => requestSignal.removeEventListener("abort", abort);
  return {
    signal: upstream.signal,
    detach,
    wrap(body: ReadableStream<Uint8Array>): ReadableStream<Uint8Array<ArrayBuffer>> {
      const reader = body.getReader();
      return new ReadableStream<Uint8Array<ArrayBuffer>>({
        async pull(controller) {
          try {
            const { done, value } = await reader.read();
            if (done) controller.close();
            else controller.enqueue(value as Uint8Array<ArrayBuffer>);
          } catch {
            controller.error(new Error("Upstream response stream interrupted"));
          }
        },
        async cancel(reason) {
          upstream.abort(reason);
          await reader.cancel(reason).catch(() => undefined);
        },
      });
    },
  };
}

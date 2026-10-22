export type AfterNavigationTask = {
  cancel: () => void;
};

/**
 * Yield a couple of native frames before starting list/database work.
 * InteractionManager is intentionally avoided here because RN 0.81 may run
 * it through the immediate queue when the interaction handle is disabled.
 */
export function scheduleAfterNavigation(
  callback: () => void,
  frameCount = 2,
): AfterNavigationTask {
  let cancelled = false;
  let frameId: number | null = null;
  let timerId: ReturnType<typeof setTimeout> | null = null;
  let remaining = Math.max(1, Math.trunc(frameCount));

  const run = () => {
    if (cancelled) return;
    if (remaining <= 0) {
      callback();
      return;
    }
    remaining -= 1;
    if (typeof globalThis.requestAnimationFrame === "function") {
      frameId = globalThis.requestAnimationFrame(run);
    } else {
      timerId = setTimeout(run, 16);
    }
  };

  run();
  return {
    cancel: () => {
      cancelled = true;
      if (frameId !== null && typeof globalThis.cancelAnimationFrame === "function") {
        globalThis.cancelAnimationFrame(frameId);
        frameId = null;
      }
      if (timerId !== null) {
        clearTimeout(timerId);
        timerId = null;
      }
    },
  };
}

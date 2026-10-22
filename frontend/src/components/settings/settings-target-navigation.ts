import { scrollSettingsElement } from "./settings-scroll-navigation";

function requestTargetFrame(callback: () => void): number {
  if (typeof window === "undefined") return -1;
  if (typeof window.requestAnimationFrame === "function") {
    return window.requestAnimationFrame(callback);
  }
  return window.setTimeout(callback, 0);
}

/**
 * Open and focus a settings target after a direct settings hash has
 * been resolved. The DOM lookup is intentionally scoped to the page so a
 * matching sidebar link can never steal focus from the real panel.
 */
export function openSettingsTarget(
  targetId: string,
  options: { isCurrent?: () => boolean } = {},
) {
  if (typeof window === "undefined" || typeof document === "undefined") return;
  const isCurrent = options.isCurrent ?? (() => true);
  if (!isCurrent()) return;
  window.dispatchEvent(new CustomEvent("settings:open-target", { detail: targetId }));
  const selector = `[data-settings-target="${targetId}"]`;
  const page = document.querySelector<HTMLElement>("[data-settings-page]");
  const target = page?.querySelector<HTMLElement>(selector) || document.getElementById(targetId);
  if (!target) return;
  // Only section-owned activation hooks may toggle a target.  Falling back to
  // the first arbitrary `[aria-expanded]` descendant can activate an unrelated
  // nested control (or a destructive action) in rich settings panels.
  const explicitActivation = target.matches(
    `[data-settings-target-activation="${targetId}"]`,
  )
    ? target
    : target.querySelector<HTMLElement>(
        `[data-settings-target-activation="${targetId}"]`,
      );
  const disclosure = target.matches(
    `[data-settings-disclosure="true"][data-settings-target="${targetId}"]`,
  )
    ? target
    : target.querySelector<HTMLElement>(
        `[data-settings-disclosure="true"][data-settings-target="${targetId}"]`,
      );
  const trigger =
    explicitActivation ||
    disclosure?.querySelector<HTMLElement>("[aria-expanded]") ||
    (target.matches("[aria-expanded]") ? target : null);
  const usesDisclosureEvent = Boolean(disclosure) && !explicitActivation;
  if (
    !usesDisclosureEvent &&
    trigger?.getAttribute("aria-expanded") === "false"
  ) {
    trigger.click();
  }
  if (!isCurrent()) return;
  const focusTarget =
    trigger ||
    target.querySelector<HTMLElement>(
      `[data-settings-target-focus-anchor="${targetId}"]`,
    ) ||
    target;
  focusTarget.focus({ preventScroll: true });

  let attempts = 0;
  const settleTarget = () => {
    if (!isCurrent()) return;
    const currentTarget =
      page?.querySelector<HTMLElement>(selector) ||
      document.getElementById(targetId);
    const scrollTarget = currentTarget?.classList.contains("contents")
      ? (currentTarget.firstElementChild as HTMLElement | null) || currentTarget
      : currentTarget;
    const didScroll = scrollTarget
      ? scrollSettingsElement(scrollTarget, {
          behavior: "smooth",
          focus: false,
        })
      : false;
    if (!didScroll && attempts < 4) {
      attempts += 1;
      requestTargetFrame(settleTarget);
      return;
    }

    // Disclosure content can settle one paint after the trigger opens. Watch
    // the page briefly and remeasure only when the scrollable height changes;
    // this preserves one smooth scroll while still correcting late mounts.
    if (didScroll && typeof ResizeObserver !== "undefined" && page) {
      const container = scrollTarget
        ? scrollTarget.closest<HTMLElement>(".ao-main-scroll")
        : null;
      if (!container) return;
      let lastScrollHeight = container.scrollHeight;
      const observer = new ResizeObserver(() => {
        if (!isCurrent()) {
          observer.disconnect();
          return;
        }
        if (window.location.hash !== `#${targetId}`) {
          observer.disconnect();
          return;
        }
        const nextScrollHeight = container.scrollHeight;
        if (nextScrollHeight === lastScrollHeight) return;
        lastScrollHeight = nextScrollHeight;
        const latestTarget =
          page.querySelector<HTMLElement>(selector) ||
          document.getElementById(targetId);
        const latestScrollTarget = latestTarget?.classList.contains("contents")
          ? (latestTarget.firstElementChild as HTMLElement | null) || latestTarget
          : latestTarget;
        if (latestScrollTarget) {
          scrollSettingsElement(latestScrollTarget, {
            behavior: "auto",
            focus: false,
          });
        }
      });
      observer.observe(page);
      window.setTimeout(() => observer.disconnect(), 1_500);
    }
  };

  // Give the disclosure state update a paint before measuring its content.
  requestTargetFrame(settleTarget);
}

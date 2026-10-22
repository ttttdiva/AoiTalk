"use client";

import { useEffect, useRef } from "react";

const TRIGGER_SELECTOR = 'button[aria-keyshortcuts="Alt+Shift+D"]';

function isRestorableTarget(element: Element | null, panel: HTMLElement | null): element is HTMLElement {
  return element instanceof HTMLElement && element.isConnected && !element.hasAttribute("disabled")
    && !(panel?.contains(element) ?? false) && element.getClientRects().length > 0;
}

/** Use the shell's existing toggle; focus is a one-shot keyboard request. */
export function useAoiDotsShortcut(
  open: boolean,
  toggle: () => void,
  composerReady: boolean,
) {
  const panelRef = useRef<HTMLElement>(null);
  const focusRequest = useRef<{ previous: Element | null } | null>(null);
  const lastOutsideFocus = useRef<HTMLElement | null>(null);
  const focusWasInPanel = useRef(false);
  const wasOpen = useRef(open);

  // Remember where focus lived outside the panel so a close can hand it back.
  // Also remember whether it was inside: a browser blurs a focused element as
  // soon as its subtree becomes inert, which can happen before the close
  // effect below observes document.activeElement.
  useEffect(() => {
    const handleFocusIn = (event: FocusEvent) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const inside = panelRef.current?.contains(target) ?? false;
      focusWasInPanel.current = inside;
      if (!inside) lastOutsideFocus.current = target;
    };
    document.addEventListener("focusin", handleFocusIn);
    return () => document.removeEventListener("focusin", handleFocusIn);
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented || event.isComposing || event.keyCode === 229 ||
        !event.altKey || !event.shiftKey || event.ctrlKey || event.metaKey ||
        (event.code !== "KeyD" && event.key.toLowerCase() !== "d")
      ) return;
      event.preventDefault();
      if (event.repeat) return;
      focusRequest.current = open ? null : { previous: document.activeElement };
      toggle();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, toggle]);

  useEffect(() => {
    const closed = wasOpen.current && !open;
    wasOpen.current = open;
    if (closed) {
      // The closed panel is only translated off-screen, so focus left inside it
      // would keep typing into an invisible composer.
      const active = document.activeElement;
      const panel = panelRef.current;
      const insidePanel = active instanceof HTMLElement && (panel?.contains(active) ?? false);
      const blurredByInert = focusWasInPanel.current
        && (active === null || active === document.body || active === document.documentElement);
      focusWasInPanel.current = false;
      if (insidePanel || blurredByInert) {
        const remembered = lastOutsideFocus.current;
        const target = isRestorableTarget(remembered, panel)
          ? remembered
          : document.querySelector<HTMLElement>(TRIGGER_SELECTOR);
        if (isRestorableTarget(target, panel)) target.focus({ preventScroll: true });
        else if (insidePanel) (active as HTMLElement).blur();
      }
    }
    if (!open) {
      focusRequest.current = null;
      return;
    }
    const request = focusRequest.current;
    if (!request || !composerReady) return;
    focusRequest.current = null;
    // Bootstrap may finish later. Respect focus moved elsewhere in the meantime.
    if (document.activeElement !== request.previous) return;
    panelRef.current
      ?.querySelector<HTMLTextAreaElement>("textarea[data-chat-composer-input]")
      ?.focus({ preventScroll: true });
  }, [open, composerReady]);

  return panelRef;
}

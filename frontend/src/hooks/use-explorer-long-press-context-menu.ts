"use client";

import {
  useCallback,
  useEffect,
  useRef,
  type MouseEventHandler,
  type PointerEventHandler,
} from "react";

const LONG_PRESS_MS = 550;
const MOVE_CANCEL_PX = 12;
const POST_LONG_PRESS_CLICK_SUPPRESSION_MS = 750;

type ActivePress = {
  pointerId: number;
  path: string;
  startX: number;
  startY: number;
  fired: boolean;
  timer: number;
};

type SuppressedClick = {
  path: string;
  until: number;
};

type LongPressHandlers = {
  onPointerDown: PointerEventHandler<HTMLElement>;
  onPointerMove: PointerEventHandler<HTMLElement>;
  onPointerUp: PointerEventHandler<HTMLElement>;
  onPointerCancel: PointerEventHandler<HTMLElement>;
  onClickCapture: MouseEventHandler<HTMLElement>;
  onDoubleClickCapture: MouseEventHandler<HTMLElement>;
};

function explorerItemFromTarget(
  target: EventTarget | null,
  container: HTMLElement,
): HTMLElement | null {
  if (!(target instanceof Element)) return null;
  const item = target.closest<HTMLElement>("[data-explorer-item-path]");
  return item && container.contains(item) ? item : null;
}

/**
 * Converts a touch/pen long press on a descendant explorer item into the same
 * contextmenu event used by desktop right-click. Event delegation keeps the
 * pointer handlers on the list/grid surface instead of every item.
 */
export function useExplorerLongPressContextMenu(): LongPressHandlers {
  const activePressRef = useRef<ActivePress | null>(null);
  const suppressedClickRef = useRef<SuppressedClick | null>(null);

  const clearActivePress = useCallback((pointerId?: number) => {
    const active = activePressRef.current;
    if (!active || (pointerId != null && active.pointerId !== pointerId)) return;
    window.clearTimeout(active.timer);
    activePressRef.current = null;
  }, []);

  const finishActivePress = useCallback(
    (pointerId: number) => {
      const active = activePressRef.current;
      if (!active || active.pointerId !== pointerId) return;
      if (active.fired) {
        suppressedClickRef.current = {
          path: active.path,
          until: Date.now() + POST_LONG_PRESS_CLICK_SUPPRESSION_MS,
        };
      }
      clearActivePress(pointerId);
    },
    [clearActivePress],
  );

  const onPointerDown = useCallback<PointerEventHandler<HTMLElement>>(
    (event) => {
      if (event.pointerType === "mouse") return;
      if (!event.isPrimary) {
        clearActivePress();
        return;
      }

      const target = explorerItemFromTarget(event.target, event.currentTarget);
      const path = target?.dataset.explorerItemPath;
      if (!target || !path) return;

      clearActivePress();
      const active: ActivePress = {
        pointerId: event.pointerId,
        path,
        startX: event.clientX,
        startY: event.clientY,
        fired: false,
        timer: 0,
      };
      active.timer = window.setTimeout(() => {
        if (activePressRef.current !== active) return;
        active.fired = true;
        // Keep this generous until pointerup in case the platform delays
        // release after presenting its touch interaction.
        suppressedClickRef.current = {
          path,
          until: Date.now() + 3000,
        };
        target.dispatchEvent(
          new MouseEvent("contextmenu", {
            bubbles: true,
            cancelable: true,
            clientX: active.startX,
            clientY: active.startY,
            button: 2,
          }),
        );
      }, LONG_PRESS_MS);
      activePressRef.current = active;
    },
    [clearActivePress],
  );

  const onPointerMove = useCallback<PointerEventHandler<HTMLElement>>(
    (event) => {
      const active = activePressRef.current;
      if (!active || active.pointerId !== event.pointerId || active.fired) return;
      const dx = event.clientX - active.startX;
      const dy = event.clientY - active.startY;
      if (dx * dx + dy * dy > MOVE_CANCEL_PX * MOVE_CANCEL_PX) {
        clearActivePress(event.pointerId);
      }
    },
    [clearActivePress],
  );

  const suppressPostLongPressMouseEvent = useCallback<
    MouseEventHandler<HTMLElement>
  >((event) => {
    const suppressed = suppressedClickRef.current;
    if (!suppressed) return;
    if (Date.now() > suppressed.until) {
      suppressedClickRef.current = null;
      return;
    }

    const target = explorerItemFromTarget(event.target, event.currentTarget);
    if (target?.dataset.explorerItemPath !== suppressed.path) return;
    event.preventDefault();
    event.stopPropagation();
  }, []);

  useEffect(
    () => () => {
      clearActivePress();
      suppressedClickRef.current = null;
    },
    [clearActivePress],
  );

  return {
    onPointerDown,
    onPointerMove,
    onPointerUp: (event) => finishActivePress(event.pointerId),
    onPointerCancel: (event) => finishActivePress(event.pointerId),
    onClickCapture: suppressPostLongPressMouseEvent,
    onDoubleClickCapture: suppressPostLongPressMouseEvent,
  };
}

"use client";

import * as React from "react";

import {
  handleMenuMnemonicKeyDown,
  MENU_MNEMONIC_SURFACE_ATTRIBUTE,
  normalizeMenuMnemonic,
} from "@/lib/menu-mnemonics";
import { cn } from "@/lib/utils";

function composeRefs<T>(
  ...refs: Array<React.Ref<T> | undefined>
): React.RefCallback<T> {
  return (node) => {
    for (const ref of refs) {
      if (!ref) continue;
      if (typeof ref === "function") {
        ref(node);
      } else {
        ref.current = node;
      }
    }
  };
}

function MenuMnemonicShortcut({
  className,
  ...props
}: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="menu-mnemonic-shortcut"
      className={cn(
        "ml-auto text-[10px] font-medium text-muted-foreground opacity-70",
        className,
      )}
      {...props}
    />
  );
}

const MenuMnemonicSurface = React.forwardRef<
  HTMLDivElement,
  React.ComponentProps<"div"> & {
    autoFocus?: boolean;
  }
>(function MenuMnemonicSurface(
  {
    autoFocus = true,
    onKeyDownCapture,
    role = "menu",
    tabIndex = -1,
    ...props
  },
  forwardedRef,
) {
  const localRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (!autoFocus) return;
    const frame = window.requestAnimationFrame(() => {
      localRef.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [autoFocus]);

  return (
    <div
      ref={composeRefs(localRef, forwardedRef)}
      role={role}
      tabIndex={tabIndex}
      {...{ [MENU_MNEMONIC_SURFACE_ATTRIBUTE]: "" }}
      onKeyDownCapture={(event) => {
        onKeyDownCapture?.(event);
        if (!event.defaultPrevented) {
          handleMenuMnemonicKeyDown(event, event.currentTarget);
        }
      }}
      {...props}
    />
  );
});

const MenuMnemonicButton = React.forwardRef<
  HTMLButtonElement,
  React.ComponentProps<"button"> & {
    mnemonic?: string;
    showMnemonic?: boolean;
  }
>(function MenuMnemonicButton(
  {
    children,
    className,
    mnemonic,
    role = "menuitem",
    showMnemonic = true,
    type = "button",
    ...props
  },
  forwardedRef,
) {
  const normalizedMnemonic = normalizeMenuMnemonic(mnemonic);

  return (
    <button
      ref={forwardedRef}
      type={type}
      role={role}
      data-menu-mnemonic={normalizedMnemonic ?? undefined}
      aria-keyshortcuts={normalizedMnemonic ?? undefined}
      className={className}
      {...props}
    >
      {children}
      {showMnemonic && normalizedMnemonic ? (
        <MenuMnemonicShortcut>{normalizedMnemonic}</MenuMnemonicShortcut>
      ) : null}
    </button>
  );
});

export {
  MenuMnemonicButton,
  MenuMnemonicShortcut,
  MenuMnemonicSurface,
  normalizeMenuMnemonic,
};

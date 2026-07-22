"use client";

export const MENU_MNEMONIC_ATTRIBUTE = "data-menu-mnemonic";
export const MENU_MNEMONIC_SURFACE_ATTRIBUTE = "data-menu-mnemonic-surface";

export type MenuMnemonicCandidate<T = unknown> = {
  key?: string | null;
  disabled?: boolean;
  label?: string;
  value: T;
};

export type MenuMnemonicResolution<T = unknown> =
  | { type: "none" }
  | { type: "disabled"; key: string }
  | { type: "duplicate"; key: string; labels: string[] }
  | { type: "match"; key: string; value: T };

type KeyboardEventLike = Pick<
  KeyboardEvent,
  "altKey" | "ctrlKey" | "key" | "metaKey"
> & {
  defaultPrevented?: boolean;
  isComposing?: boolean;
  shiftKey?: boolean;
};

type MenuMnemonicKeyboardEvent = KeyboardEventLike & {
  target: EventTarget | null;
  currentTarget: EventTarget | null;
  nativeEvent?: KeyboardEvent;
  preventDefault(): void;
  stopPropagation(): void;
};

export function normalizeMenuMnemonic(value?: string | null): string | null {
  const trimmed = value?.trim();
  if (!trimmed) return null;

  const chars = Array.from(trimmed);
  if (chars.length !== 1) return null;

  return chars[0].toUpperCase();
}

export function getMenuMnemonicEventKey(
  event: KeyboardEventLike,
): string | null {
  if (
    event.defaultPrevented ||
    event.isComposing ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey
  ) {
    return null;
  }

  return normalizeMenuMnemonic(event.key);
}

export function resolveMenuMnemonic<T>(
  candidates: Array<MenuMnemonicCandidate<T>>,
  rawKey: string,
): MenuMnemonicResolution<T> {
  const key = normalizeMenuMnemonic(rawKey);
  if (!key) return { type: "none" };

  const matches = candidates.filter(
    (candidate) => normalizeMenuMnemonic(candidate.key) === key,
  );
  if (matches.length === 0) return { type: "none" };

  const enabledMatches = matches.filter((candidate) => !candidate.disabled);
  if (enabledMatches.length === 0) return { type: "disabled", key };

  if (enabledMatches.length > 1) {
    return {
      type: "duplicate",
      key,
      labels: enabledMatches.map((candidate) => candidate.label ?? key),
    };
  }

  return { type: "match", key, value: enabledMatches[0].value };
}

export function isEditableMenuTarget(target: EventTarget | null): boolean {
  if (typeof HTMLElement === "undefined") return false;
  if (!(target instanceof HTMLElement)) return false;

  if (
    target.closest(
      'input, textarea, select, [contenteditable="true"], [contenteditable=""], [role="textbox"]',
    )
  ) {
    return true;
  }

  return target.isContentEditable;
}

export function isMenuMnemonicElementDisabled(element: HTMLElement): boolean {
  if (element.hasAttribute("disabled")) return true;
  if (element.getAttribute("aria-disabled") === "true") return true;
  if (element.hasAttribute("data-disabled")) return true;
  return element.closest("[data-disabled], [aria-disabled='true']") !== null;
}

function stopMenuMnemonicEvent(event: MenuMnemonicKeyboardEvent) {
  event.preventDefault();
  event.stopPropagation();
  event.nativeEvent?.stopImmediatePropagation?.();
}

function getOwningMnemonicSurface(element: Element): Element | null {
  return element.closest(`[${MENU_MNEMONIC_SURFACE_ATTRIBUTE}]`);
}

function shouldHandleMnemonicEventInRoot(
  event: MenuMnemonicKeyboardEvent,
  root: HTMLElement,
): boolean {
  if (!root.hasAttribute(MENU_MNEMONIC_SURFACE_ATTRIBUTE)) return true;
  if (!(event.target instanceof Element)) return true;

  const targetSurface = getOwningMnemonicSurface(event.target);
  return targetSurface === null || targetSurface === root;
}

function isMnemonicElementInRootSurface(
  element: HTMLElement,
  root: HTMLElement,
): boolean {
  if (!root.hasAttribute(MENU_MNEMONIC_SURFACE_ATTRIBUTE)) return true;
  return getOwningMnemonicSurface(element) === root;
}

export function handleMenuMnemonicKeyDown(
  event: MenuMnemonicKeyboardEvent,
  root: HTMLElement,
): boolean {
  if (isEditableMenuTarget(event.target)) return false;
  if (!shouldHandleMnemonicEventInRoot(event, root)) return false;

  const key = getMenuMnemonicEventKey(event);
  if (!key) return false;

  const elements = Array.from(
    root.querySelectorAll<HTMLElement>(`[${MENU_MNEMONIC_ATTRIBUTE}]`),
  ).filter((element) => isMnemonicElementInRootSurface(element, root));
  const resolution = resolveMenuMnemonic(
    elements.map((element) => ({
      key: element.getAttribute(MENU_MNEMONIC_ATTRIBUTE),
      disabled: isMenuMnemonicElementDisabled(element),
      label: element.textContent?.trim() || undefined,
      value: element,
    })),
    key,
  );

  if (resolution.type === "none") return false;

  stopMenuMnemonicEvent(event);

  if (resolution.type === "duplicate") {
    if (process.env.NODE_ENV !== "production") {
      console.warn(
        `Duplicate menu mnemonic "${resolution.key}" ignored.`,
        resolution.labels,
      );
    }
    return true;
  }

  if (resolution.type === "disabled") return true;

  resolution.value.click();
  return true;
}

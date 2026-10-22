/**
 * Scroll Settings sections inside the Shared Shell's content viewport.
 *
 * The app shell deliberately keeps the document root fixed and gives the
 * `.ao-main-scroll` element ownership of vertical scrolling.  Calling
 * `Element.scrollIntoView()` here would allow the browser to pick an outer
 * ancestor (or the document) and is what caused the settings page to leave a
 * blank-looking tail after selecting a late section.  Keep all category
 * movement scoped to this one container instead.
 */

export type SettingsScrollBehavior = "auto" | "smooth";

const SETTINGS_SCROLL_MARGIN = 20;

export type SettingsOuterScrollSnapshot = {
  shell: HTMLElement | null;
  shellTop: number;
  documentTop: number;
  bodyTop: number;
  windowY: number;
};

/** Capture scroll owners outside Settings so native history restoration cannot move them. */
export function captureSettingsOuterScroll(
  root?: ParentNode,
): SettingsOuterScrollSnapshot {
  if (typeof document === "undefined") {
    return { shell: null, shellTop: 0, documentTop: 0, bodyTop: 0, windowY: 0 };
  }
  const searchRoot = root ?? document;
  const shell = searchRoot.querySelector<HTMLElement>(
    '[data-shell="contextual-workspace"]',
  );
  return {
    shell,
    shellTop: shell?.scrollTop ?? 0,
    documentTop: document.documentElement.scrollTop,
    bodyTop: document.body.scrollTop,
    windowY: typeof window !== "undefined" ? window.scrollY : 0,
  };
}

/** Restore only the outer scroll owners; Settings content remains owned by `.ao-main-scroll`. */
export function restoreSettingsOuterScroll(
  snapshot: SettingsOuterScrollSnapshot,
): void {
  if (typeof document === "undefined") return;
  if (snapshot.shell?.isConnected !== false) {
    if (snapshot.shell) snapshot.shell.scrollTop = snapshot.shellTop;
  }
  document.documentElement.scrollTop = snapshot.documentTop;
  document.body.scrollTop = snapshot.bodyTop;
  if (
    typeof window !== "undefined" &&
    snapshot.windowY !== 0 &&
    window.scrollY !== snapshot.windowY &&
    typeof window.scrollTo === "function"
  ) {
    window.scrollTo(0, snapshot.windowY);
  }
}

/** Update a category hash without triggering the browser's native scroll. */
export function pushSettingsCategoryHash(category: string): boolean {
  if (typeof window === "undefined") return false;
  const nextHash = `#${category}`;
  if (window.location.hash === nextHash) return false;
  window.history.pushState(
    null,
    "",
    `${window.location.pathname}${window.location.search}${nextHash}`,
  );
  return true;
}

/** Update a direct target hash without creating a duplicate history entry. */
export function pushSettingsTargetHash(targetId: string): boolean {
  if (typeof window === "undefined") return false;
  const nextHash = `#${targetId}`;
  if (window.location.hash === nextHash) return false;
  window.history.pushState(
    null,
    "",
    `${window.location.pathname}${window.location.search}${nextHash}`,
  );
  return true;
}

export function getSettingsScrollContainer(
  root?: ParentNode,
): HTMLElement | null {
  if (typeof document === "undefined" && !root) return null;
  const searchRoot = root ?? document;
  return (
    searchRoot.querySelector<HTMLElement>(
      "[data-shell-region='main-canvas'] .ao-main-scroll",
    ) ?? searchRoot.querySelector<HTMLElement>(".ao-main-scroll")
  );
}

function getSettingsSection(
  category: string,
  root?: ParentNode,
): HTMLElement | null {
  if (typeof document === "undefined" && !root) return null;
  const searchRoot = root ?? document;
  // Category ids are registry-owned values, so an id selector is safe here;
  // use the data attribute as a fallback for test/embedded shell mounts.
  return (
    (searchRoot.querySelector<HTMLElement>(`#${category}`) as HTMLElement | null) ??
    searchRoot.querySelector<HTMLElement>(
      `[data-settings-group="${category}"]`,
    )
  );
}

/**
 * Move one Settings category into view and focus its heading without moving
 * any outer/root scroller.  Returns false when the page or target has not
 * mounted yet so callers can retry after an async section render.
 */
export function scrollSettingsCategory(
  category: string,
  options: {
    behavior?: SettingsScrollBehavior;
    focus?: boolean;
    root?: ParentNode;
  } = {},
): boolean {
  const { behavior = "smooth", focus = true, root } = options;
  const container = getSettingsScrollContainer(root);
  const section = getSettingsSection(category, root);
  if (!container || !section || !container.contains(section)) return false;

  const didScroll = scrollSettingsElement(section, {
    behavior,
    focus: false,
    root,
  });
  if (!didScroll) return false;

  if (focus) {
    const heading = section.querySelector<HTMLElement>("h2") ?? section;
    heading.focus?.({ preventScroll: true });
  }
  return true;
}

/**
 * Scroll an already-mounted direct target (including a nested target frame)
 * inside the Shared Shell viewport.  Disclosure opening/focus remains the
 * caller's responsibility; this helper only performs the clamped container
 * movement and never delegates to browser ancestor selection.
 */
export function scrollSettingsElement(
  target: HTMLElement,
  options: {
    behavior?: SettingsScrollBehavior;
    focus?: boolean;
    root?: ParentNode;
  } = {},
): boolean {
  const { behavior = "smooth", focus = false, root } = options;
  const container = getSettingsScrollContainer(root);
  if (!container || !container.contains(target)) return false;

  const containerRect = container.getBoundingClientRect();
  const sectionRect = target.getBoundingClientRect();
  const desiredTop =
    container.scrollTop +
    sectionRect.top -
    containerRect.top -
    SETTINGS_SCROLL_MARGIN;
  const maxTop = Math.max(0, container.scrollHeight - container.clientHeight);
  const top = Math.min(Math.max(desiredTop, 0), maxTop);

  // `scrollTo` is supported by every browser used by the app.  The fallback
  // keeps embedded/test DOMs deterministic without touching scrollIntoView.
  if (typeof container.scrollTo === "function") {
    container.scrollTo({ top, behavior });
  } else {
    container.scrollTop = top;
  }

  if (focus) {
    target.focus?.({ preventScroll: true });
  }
  return true;
}

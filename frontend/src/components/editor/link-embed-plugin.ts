import {
  ViewPlugin,
  Decoration,
  type DecorationSet,
  WidgetType,
  type EditorView,
  type ViewUpdate,
} from "@codemirror/view";
import { type Range, StateEffect } from "@codemirror/state";
import { fetchOgp, type OgpData } from "@/lib/explorer-api";

const URL_REGEX = /https?:\/\/[^\s<>"')\]]+/g;
const TWITTER_WIDGETS_SCRIPT = "https://platform.twitter.com/widgets.js";

const ogpCache = new Map<string, OgpData>();
const ogpInFlight = new Map<string, Promise<OgpData>>();

const EMBED_INTERSECTION_ROOT_MARGIN = "800px 0px";
const LINK_EMBED_CARD_HEIGHT = 56;
const X_EMBED_FALLBACK_HEIGHT = 420;
const X_EMBED_MIN_HEIGHT = 180;
const X_EMBED_MAX_HEIGHT = 1400;
const X_EMBED_HEIGHT_STORAGE_KEY = "aoitalk.linkEmbed.xHeights.v1";
const X_EMBED_HEIGHT_MAX_ENTRIES = 128;
const xEmbedHeightCache = new Map<string, number>();
let xEmbedHeightStorageLoaded = false;
let xEmbedHeightStorageWriteScheduled = false;

const viewOgpSubscriptions = new WeakMap<EditorView, Set<string>>();
const viewRebuildFrames = new WeakMap<EditorView, number>();
const destroyedViews = new WeakSet<EditorView>();

function isDestroyedView(view: EditorView) {
  const internalView = view as unknown as { destroyed?: boolean };
  return (
    destroyedViews.has(view) ||
    !view.dom.isConnected ||
    internalView.destroyed === true
  );
}

function clampXEmbedHeight(value: number) {
  return Math.max(
    X_EMBED_MIN_HEIGHT,
    Math.min(X_EMBED_MAX_HEIGHT, Math.round(value)),
  );
}

function loadXEmbedHeightCache() {
  if (xEmbedHeightStorageLoaded) return;
  xEmbedHeightStorageLoaded = true;
  if (typeof window === "undefined") return;
  try {
    const raw = window.sessionStorage.getItem(X_EMBED_HEIGHT_STORAGE_KEY);
    if (!raw) return;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return;
    for (const [url, value] of Object.entries(parsed)) {
      if (typeof value !== "number" || !Number.isFinite(value)) continue;
      xEmbedHeightCache.set(url, clampXEmbedHeight(value));
      if (xEmbedHeightCache.size >= X_EMBED_HEIGHT_MAX_ENTRIES) break;
    }
  } catch {
    // sessionStorage is best effort and may be unavailable in privacy modes.
  }
}

function persistXEmbedHeightCache() {
  if (xEmbedHeightStorageWriteScheduled || typeof window === "undefined") return;
  xEmbedHeightStorageWriteScheduled = true;
  const write = () => {
    xEmbedHeightStorageWriteScheduled = false;
    try {
      const values = Object.fromEntries(xEmbedHeightCache);
      window.sessionStorage.setItem(
        X_EMBED_HEIGHT_STORAGE_KEY,
        JSON.stringify(values),
      );
    } catch {
      // Ignore storage quota/security failures; memory cache still works.
    }
  };
  if (typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(write, { timeout: 500 });
  } else {
    window.setTimeout(write, 0);
  }
}

function getCachedXEmbedHeight(url: string) {
  loadXEmbedHeightCache();
  return xEmbedHeightCache.get(url);
}

function cacheXEmbedHeight(url: string, value: number) {
  const height = clampXEmbedHeight(value);
  const previous = xEmbedHeightCache.get(url);
  if (previous !== undefined && Math.abs(previous - height) < 3) return;
  xEmbedHeightCache.delete(url);
  xEmbedHeightCache.set(url, height);
  while (xEmbedHeightCache.size > X_EMBED_HEIGHT_MAX_ENTRIES) {
    const oldest = xEmbedHeightCache.keys().next().value as string | undefined;
    if (oldest === undefined) break;
    xEmbedHeightCache.delete(oldest);
  }
  persistXEmbedHeightCache();
}

function requestOgp(url: string): Promise<OgpData> {
  const cached = ogpCache.get(url);
  if (cached) return Promise.resolve(cached);
  const existing = ogpInFlight.get(url);
  if (existing) return existing;

  const request = fetchOgp(url)
    .catch((): OgpData => ({ success: false, url }))
    .then((data) => {
      ogpCache.set(url, data);
      return data;
    })
    .finally(() => {
      ogpInFlight.delete(url);
    });
  ogpInFlight.set(url, request);
  return request;
}

function observeNearViewport(
  element: HTMLElement,
  callback: () => void,
): () => void {
  if (typeof IntersectionObserver === "undefined") {
    callback();
    return () => {};
  }

  let fired = false;
  // `scrollMargin` is newer than `rootMargin` and is not present in older
  // TypeScript DOM declarations.  Keep the option in an intersection type so
  // modern browsers can expand nested scroll clips while older browsers can
  // safely ignore it (or fall back to the rootMargin-only constructor if an
  // implementation rejects the dictionary member).
  type IntersectionObserverInitWithScrollMargin = IntersectionObserverInit & {
    scrollMargin?: string;
  };
  const options: IntersectionObserverInitWithScrollMargin = {
    root: null,
    rootMargin: EMBED_INTERSECTION_ROOT_MARGIN,
    scrollMargin: EMBED_INTERSECTION_ROOT_MARGIN,
  };
  let observer: IntersectionObserver;
  const onIntersect: IntersectionObserverCallback = (entries) => {
    if (
      fired ||
      !entries.some(
        (entry) => entry.isIntersecting || entry.intersectionRatio > 0,
      )
    ) {
      return;
    }
    fired = true;
    observer.disconnect();
    callback();
  };
  try {
    observer = new IntersectionObserver(onIntersect, options);
  } catch {
    observer = new IntersectionObserver(onIntersect, {
      root: null,
      rootMargin: EMBED_INTERSECTION_ROOT_MARGIN,
    });
  }
  observer.observe(element);
  return () => observer.disconnect();
}

function scheduleLinkEmbedRebuild(view: EditorView) {
  if (isDestroyedView(view)) return;
  if (viewRebuildFrames.has(view)) return;
  const run = () => {
    viewRebuildFrames.delete(view);
    if (isDestroyedView(view)) return;
    view.dispatch({ effects: rebuildEffect.of(null) });
  };
  if (typeof requestAnimationFrame === "function") {
    viewRebuildFrames.set(view, requestAnimationFrame(run));
  } else {
    queueMicrotask(run);
    viewRebuildFrames.set(view, -1);
  }
}

function requestOgpForView(view: EditorView, url: string) {
  if (isDestroyedView(view)) return;
  let subscribed = viewOgpSubscriptions.get(view);
  if (!subscribed) {
    subscribed = new Set<string>();
    viewOgpSubscriptions.set(view, subscribed);
  }
  if (subscribed.has(url)) return;
  if (ogpCache.has(url)) {
    // Another EditorView may have populated the shared cache before this
    // widget's observer fired.  Rebuild this view so its placeholder observes
    // the cached result instead of remaining stuck indefinitely.
    scheduleLinkEmbedRebuild(view);
    return;
  }
  subscribed.add(url);
  void requestOgp(url).finally(() => {
    subscribed?.delete(url);
    if (isDestroyedView(view)) return;
    scheduleLinkEmbedRebuild(view);
  });
}

function isXStatusUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    if (parsed.username || parsed.password) return false;
    const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
    if (
      ![
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
      ].includes(host)
    ) {
      return false;
    }
    return /^\/[^/]+\/status(?:es)?\/\d+(?:\/|$)/.test(parsed.pathname);
  } catch {
    return false;
  }
}

function parseIpv4Literal(hostname: string): number[] | null {
  const parts = hostname.split(".");
  if (parts.length !== 4) return null;
  const octets = parts.map((part) => Number(part));
  return octets.every(
    (part) => Number.isInteger(part) && part >= 0 && part <= 255,
  )
    ? octets
    : null;
}

function parseIpv6Words(hostname: string): number[] | null {
  const value = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (!value.includes(":")) return null;

  let expanded = value;
  const lastColon = value.lastIndexOf(":");
  const dottedTail = value.slice(lastColon + 1);
  if (dottedTail.includes(".")) {
    const octets = parseIpv4Literal(dottedTail);
    if (!octets) return null;
    const high = ((octets[0] ?? 0) << 8) | (octets[1] ?? 0);
    const low = ((octets[2] ?? 0) << 8) | (octets[3] ?? 0);
    expanded = `${value.slice(0, lastColon + 1)}${high.toString(16)}:${low.toString(16)}`;
  }

  const halves = expanded.split("::");
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(":") : [];
  const right = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const parseWord = (word: string) =>
    /^[0-9a-f]{1,4}$/.test(word) ? Number.parseInt(word, 16) : null;
  const leftWords = left.map(parseWord);
  const rightWords = right.map(parseWord);
  if (
    leftWords.some((word) => word === null) ||
    rightWords.some((word) => word === null)
  ) {
    return null;
  }

  const missing = 8 - leftWords.length - rightWords.length;
  if (missing < 0 || (halves.length === 1 && missing !== 0)) return null;
  return [
    ...leftWords,
    ...Array.from({ length: missing }, () => 0),
    ...rightWords,
  ] as number[];
}

function isUnsafeIpv4(octets: number[]): boolean {
  const [first = -1, second = -1, third = -1] = octets;
  return (
    first === 0 ||
    first === 10 ||
    first === 127 ||
    (first === 100 && second >= 64 && second <= 127) ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168) ||
    (first === 192 && second === 0 && (third === 0 || third === 2)) ||
    (first === 198 && second >= 18 && second <= 19) ||
    (first === 198 && second === 51 && third === 100) ||
    (first === 203 && second === 0 && third === 113) ||
    first >= 224
  );
}

function isUnsafeIpLiteral(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/\.$/, "");
  if (
    normalized === "localhost" ||
    normalized === "localhost.localdomain" ||
    normalized.endsWith(".local") ||
    normalized.endsWith(".localhost") ||
    (!normalized.includes(".") && !normalized.includes(":"))
  ) {
    return true;
  }

  const ipv4 = parseIpv4Literal(normalized);
  if (ipv4) return isUnsafeIpv4(ipv4);

  const words = parseIpv6Words(normalized);
  if (!words) return false;
  const isZeroPrefix = words.slice(0, 6).every((word) => word === 0);
  const low32 = [
    (words[6] ?? 0) >> 8,
    (words[6] ?? 0) & 0xff,
    (words[7] ?? 0) >> 8,
    (words[7] ?? 0) & 0xff,
  ];
  const isMapped =
    words.slice(0, 5).every((word) => word === 0) && words[5] === 0xffff;
  const isIpv4Compatible = isZeroPrefix;
  const isNat64 =
    words[0] === 0x64 &&
    words[1] === 0xff9b &&
    words.slice(2, 6).every((word) => word === 0);
  const isSixToFour = words[0] === 0x2002;
  if (isMapped || isIpv4Compatible || isNat64 || isSixToFour) {
    const embedded = isSixToFour
      ? [
          (words[1] ?? 0) >> 8,
          (words[1] ?? 0) & 0xff,
          (words[2] ?? 0) >> 8,
          (words[2] ?? 0) & 0xff,
        ]
      : low32;
    return isUnsafeIpv4(embedded);
  }

  const first = words[0] ?? 0;
  return (
    (first === 0 && words.slice(1).every((word) => word === 0)) ||
    (first === 0 &&
      words.slice(1, 7).every((word) => word === 0) &&
      words[7] === 1) ||
    (first >= 0xfc00 && first <= 0xfdff) ||
    (first >= 0xfe80 && first <= 0xfebf) ||
    (first >= 0xfec0 && first <= 0xfeff) ||
    first >= 0xff00 ||
    (first === 0x2001 && words[1] === 0x0db8)
  );
}

/**
 * Return an OGP image/icon URL safe to assign to an HTMLImageElement.
 *
 * Backend metadata is validated too, but this browser-side check protects
 * callers that provide cached or mocked OGP data directly.  Relative values
 * are only accepted when they resolve same-origin against the page URL.
 */
export function sanitizeMetadataImageUrl(
  value: string | undefined,
  pageUrl?: string,
): string | null {
  const raw = value?.trim();
  if (!raw) return null;
  if (raw.startsWith("/api/python-proxy/ogp/media?")) return raw;

  let base: URL | undefined;
  try {
    if (pageUrl) {
      base = new URL(pageUrl);
      if (base.protocol !== "http:" && base.protocol !== "https:") return null;
      if (base.username || base.password || isUnsafeIpLiteral(base.hostname)) {
        return null;
      }
    }
    const hasScheme = /^[a-z][a-z\d+.-]*:/i.test(raw);
    const isNetworkPath = raw.startsWith("//");
    if (!hasScheme && !isNetworkPath && !base) return null;
    const parsed = new URL(raw, base);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:")
      return null;
    if (
      parsed.username ||
      parsed.password ||
      isUnsafeIpLiteral(parsed.hostname)
    ) {
      return null;
    }
    if (
      parsed.pathname === "/api/python-proxy/ogp/media" &&
      typeof window !== "undefined" &&
      parsed.origin === window.location.origin
    ) {
      return `${parsed.pathname}${parsed.search}`;
    }
    if (!hasScheme && !isNetworkPath && base && parsed.origin !== base.origin) {
      return null;
    }
    return `/api/python-proxy/ogp/media?url=${encodeURIComponent(parsed.toString())}`;
  } catch {
    return null;
  }
}

export type LinkDisplayMode = "embed" | "link";
type DisplayMode = LinkDisplayMode;

export type LinkDisplayModeMap = Record<string, LinkDisplayMode>;
export type LinkDisplayModeChangeHandler = (
  url: string,
  mode: LinkDisplayMode,
) => void;

type LinkDisplayModeController = {
  modes: Map<string, DisplayMode>;
  defaultDisplayMode: DisplayMode;
  onChange?: LinkDisplayModeChangeHandler;
  readOnly: boolean;
};

const rebuildEffect = StateEffect.define<null>();

export type LinkEmbedRuntimeConfig = {
  defaultDisplayMode: LinkDisplayMode;
  displayModes?: LinkDisplayModeMap | null;
  onDisplayModeChange?: LinkDisplayModeChangeHandler;
  readOnly?: boolean;
};

export const updateLinkEmbedConfigEffect =
  StateEffect.define<LinkEmbedRuntimeConfig>();

function getDisplayMode(url: string, controller: LinkDisplayModeController) {
  const existingMode = controller.modes.get(url);
  if (existingMode) {
    return existingMode;
  }

  return controller.defaultDisplayMode;
}

function toggleDisplayMode(
  url: string,
  view: EditorView,
  controller: LinkDisplayModeController,
) {
  const current = getDisplayMode(url, controller);
  const next = current === "embed" ? "link" : "embed";
  controller.modes.set(url, next);
  controller.onChange?.(url, next);
  view.dispatch({ effects: [rebuildEffect.of(null)] });
}

function copyToClipboard(text: string) {
  navigator.clipboard.writeText(text).catch(() => {});
}

type TwitterWidgets = {
  widgets?: {
    load?: (element?: HTMLElement) => void;
  };
};

let twitterScriptPromise: Promise<void> | null = null;

declare global {
  interface Window {
    twttr?: TwitterWidgets;
  }
}

function loadTwitterWidgets(container: HTMLElement) {
  if (typeof window === "undefined") return;

  const render = () => window.twttr?.widgets?.load?.(container);
  if (window.twttr?.widgets?.load) {
    render();
    return;
  }

  if (!twitterScriptPromise) {
    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${TWITTER_WIDGETS_SCRIPT}"]`,
    );
    if (existing) {
      twitterScriptPromise = new Promise<void>((resolve) => {
        if (window.twttr?.widgets?.load) {
          resolve();
        } else {
          existing.addEventListener("load", () => resolve(), { once: true });
          existing.addEventListener("error", () => resolve(), { once: true });
        }
      });
    } else {
      twitterScriptPromise = new Promise<void>((resolve) => {
        const script = document.createElement("script");
        script.src = TWITTER_WIDGETS_SCRIPT;
        script.async = true;
        script.charset = "utf-8";
        script.addEventListener("load", () => resolve(), { once: true });
        script.addEventListener("error", () => resolve(), { once: true });
        document.head.appendChild(script);
      });
    }
  }
  void twitterScriptPromise.then(render);
}

/** URL のデコレーション範囲を削除してテキストごと消す */
function deleteUrl(url: string, view: EditorView) {
  const doc = view.state.doc;
  for (let i = 1; i <= doc.lines; i++) {
    const line = doc.line(i);
    URL_REGEX.lastIndex = 0;
    const match = URL_REGEX.exec(line.text);
    if (match && match[0] === url) {
      // 行全体が URL だけなら行ごと削除、そうでなければ URL 部分だけ
      const from =
        line.text.trim() === url ? line.from : line.from + match.index;
      const to =
        line.text.trim() === url
          ? line.to + 1
          : line.from + match.index + url.length;
      view.dispatch({
        changes: { from, to: Math.min(to, doc.length), insert: "" },
      });
      return;
    }
  }
}

// ─── SVG アイコン（軽量インライン） ───

const ICON_COPY = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
const ICON_LINK = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`;
const ICON_EMBED = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>`;
const ICON_TRASH = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>`;

function makeIconButton(
  svgHtml: string,
  title: string,
  onClick: () => void,
): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.title = title;
  btn.innerHTML = svgHtml;
  btn.style.cssText = `
    width: 24px; height: 24px;
    display: flex; align-items: center; justify-content: center;
    border: 1px solid var(--border, #444);
    border-radius: 4px;
    background: var(--muted, #1e1e2e);
    color: var(--muted-foreground, #888);
    cursor: pointer;
    transition: color 0.1s, background 0.1s;
  `.replace(/\n\s*/g, " ");
  btn.addEventListener("mouseenter", () => {
    btn.style.color = "var(--foreground, #cdd6f4)";
    btn.style.background = "var(--accent, #313244)";
  });
  btn.addEventListener("mouseleave", () => {
    btn.style.color = "var(--muted-foreground, #888)";
    btn.style.background = "var(--muted, #1e1e2e)";
  });
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    e.preventDefault();
    onClick();
  });
  return btn;
}

/** ホバー時に右上に表示するアクションバー */
function appendActionBar(
  container: HTMLElement,
  url: string,
  view: EditorView,
  mode: DisplayMode,
  controller: LinkDisplayModeController,
) {
  const bar = document.createElement("div");
  bar.className = "cm-le-actions";
  bar.style.cssText = `
    position: absolute; top: 4px; right: 4px;
    display: flex; gap: 2px;
    opacity: 0; transition: opacity 0.15s;
    z-index: 2;
  `.replace(/\n\s*/g, " ");

  // コピー
  bar.appendChild(
    makeIconButton(ICON_COPY, "URLをコピー", () => copyToClipboard(url)),
  );

  if (!controller.readOnly) {
    // embed/link 切替
    if (mode === "embed") {
      bar.appendChild(
        makeIconButton(ICON_LINK, "リンクに戻す", () =>
          toggleDisplayMode(url, view, controller),
        ),
      );
    } else {
      bar.appendChild(
        makeIconButton(ICON_EMBED, "埋め込みで表示", () =>
          toggleDisplayMode(url, view, controller),
        ),
      );
    }

    // 削除
    bar.appendChild(
      makeIconButton(ICON_TRASH, "削除", () => deleteUrl(url, view)),
    );
  }

  container.addEventListener("mouseenter", () => (bar.style.opacity = "1"));
  container.addEventListener("mouseleave", () => (bar.style.opacity = "0"));
  container.appendChild(bar);
}

function findNearestScrollableAncestor(element: HTMLElement): HTMLElement | null {
  let parent = element.parentElement;
  while (parent) {
    try {
      const style = window.getComputedStyle(parent);
      const overflowY = style.overflowY;
      if (
        (overflowY === "auto" ||
          overflowY === "scroll" ||
          overflowY === "overlay") &&
        parent.scrollHeight > parent.clientHeight + 1
      ) {
        return parent;
      }
    } catch {
      // Ignore detached/partially mocked DOM nodes.
    }
    parent = parent.parentElement;
  }
  return null;
}

function findOverflowAncestors(element: HTMLElement): HTMLElement[] {
  const ancestors: HTMLElement[] = [];
  let parent = element.parentElement;
  while (parent) {
    try {
      const overflowY = window.getComputedStyle(parent).overflowY;
      if (
        overflowY === "auto" ||
        overflowY === "scroll" ||
        overflowY === "overlay"
      ) {
        // Scroll listeners are only installed on ancestors that can actually
        // scroll.  This keeps the hot scroll path geometry-free; the one-time
        // overflow check happens while the widget is being mounted instead.
        if (parent.scrollHeight > parent.clientHeight + 1) {
          ancestors.push(parent);
        }
      }
    } catch {
      // Ignore detached/partially mocked DOM nodes.
    }
    parent = parent.parentElement;
  }
  return ancestors;
}

function readScrollTop(container: HTMLElement | null) {
  if (container) return container.scrollTop;
  return typeof window !== "undefined"
    ? window.scrollY || window.pageYOffset || 0
    : 0;
}

function readElementHeight(element: HTMLElement | null) {
  if (!element) return 0;
  const rectHeight = element.getBoundingClientRect().height;
  // jsdom and a few embedded browser implementations report zero rects while
  // still exposing offset/scroll dimensions.  Prefer the largest available
  // content measurement, but never use the X shell wrapper (whose min-height
  // is deliberately reserved) as the source of truth.
  return Math.max(rectHeight, element.offsetHeight, element.scrollHeight);
}

function readXEmbedContentHeight(embed: HTMLElement) {
  const rendered = embed.querySelector<HTMLElement>(
    "iframe, .twitter-tweet-rendered, [data-twitter-widget]",
  );
  return Math.max(readElementHeight(rendered), readElementHeight(embed));
}

function preserveScrollAnchor(
  element: HTMLElement,
  previousHeight: number,
  nextHeight: number,
  previousScrollTop: number,
  container: HTMLElement | null,
) {
  const delta = nextHeight - previousHeight;
  if (Math.abs(delta) < 2 || typeof window === "undefined") return;

  const rect = element.getBoundingClientRect();
  const viewportTop = container
    ? container.getBoundingClientRect().top
    : 0;
  const viewportBottom = container
    ? container.getBoundingClientRect().bottom
    : window.innerHeight;
  // Measure visibility against the pre-resize footprint.  After a widget
  // expands, its current bottom can overlap the viewport even though the
  // entire previous placeholder was above it; using the current bottom would
  // incorrectly skip the compensation and let the reader jump.
  const previousBottom = rect.top + previousHeight;
  if (previousBottom > viewportTop + 2 || rect.top >= viewportBottom) return;

  const currentScrollTop = readScrollTop(container);
  const expectedScrollTop = previousScrollTop + delta;
  if (Math.abs(currentScrollTop - expectedScrollTop) <= 4) return;
  if (Math.abs(currentScrollTop - previousScrollTop) > 4) return;

  if (container) {
    container.scrollTop = expectedScrollTop;
  } else if (typeof window.scrollTo === "function") {
    window.scrollTo(window.scrollX || 0, expectedScrollTop);
  }
}

type WidgetCleanup = () => void;

// ─── Embed モード: OGP プレビューカード ───

class EmbedWidget extends WidgetType {
  private cleanup: WidgetCleanup | null = null;
  readonly readOnlySnapshot: boolean;

  constructor(
    readonly url: string,
    readonly data: OgpData | null,
    readonly loading: boolean,
    readonly view: EditorView,
    readonly controller: LinkDisplayModeController,
  ) {
    super();
    this.readOnlySnapshot = controller.readOnly;
  }

  eq(other: EmbedWidget) {
    return (
      this.url === other.url &&
      this.loading === other.loading &&
      this.data === other.data &&
      this.readOnlySnapshot === other.readOnlySnapshot &&
      this.controller === other.controller
    );
  }

  toDOM() {
    const xStatus = isXStatusUrl(this.url);
    const xPost =
      this.data?.success &&
      this.data.embed_type === "x-post" &&
      this.data.embed_html;

    // Keep every X status URL in the X shell, including metadata failures or
    // responses that are not an x-post.  Falling through to the generic 56px
    // OGP card after a failed request causes a visible collapse and can make a
    // previously cached 760px reservation disappear.
    if (xStatus) {
      const wrap = document.createElement("div");
      wrap.className = "cm-link-embed cm-link-embed-x-post";
      wrap.style.cssText =
        "position: relative; max-width: 550px; margin: 6px 0; user-select: none; overflow-anchor: none;";
      const reservedHeight =
        getCachedXEmbedHeight(this.url) ?? X_EMBED_FALLBACK_HEIGHT;
      wrap.style.minHeight = `${reservedHeight}px`;

      const embed = document.createElement("div");
      embed.style.cssText =
        "overflow: hidden; border-radius: 8px;";
      if (typeof xPost === "string") {
        embed.innerHTML = xPost;
      } else {
        embed.textContent = this.data
          ? "Xポストを表示できませんでした。リンクを開いて確認してください。"
          : "Xポストを読み込み中...";
        embed.style.cssText +=
          "min-height: 0; height: 100%; display: flex; align-items: center; justify-content: center; color: var(--muted-foreground, #888); padding: 12px; text-align: center;";
      }
      wrap.appendChild(embed);

      wrap.addEventListener("click", (e) => {
        if ((e.target as HTMLElement).closest(".cm-le-actions")) return;
        if (typeof xPost !== "string") {
          window.open(this.url, "_blank", "noopener,noreferrer");
        }
      });
      appendActionBar(wrap, this.url, this.view, "embed", this.controller);

      // A resolved failure is terminal for this editor session: requestOgp
      // caches the failed result, and this branch intentionally does not
      // install another observer that could retry it indefinitely.
      if (typeof xPost !== "string" && this.data) {
        return wrap;
      }
      if (typeof xPost !== "string") {
        this.cleanup = observeNearViewport(wrap, () =>
          requestOgpForView(this.view, this.url),
        );
        return wrap;
      }

      let scrollContainer = findNearestScrollableAncestor(wrap);
      const overflowAncestors = findOverflowAncestors(wrap);
      let renderStarted = false;
      let lastHeight = reservedHeight;
      let lastScrollTop = readScrollTop(scrollContainer);
      let lastMeasuredScrollTop = lastScrollTop;
      let resizePending = false;
      let windowScrollListenerAttached = false;
      const trackScrollPosition = (event: Event) => {
        const target = event.currentTarget;
        if (typeof Window !== "undefined" && target instanceof Window) {
          lastScrollTop = readScrollTop(null);
          if (!resizePending) {
            lastMeasuredScrollTop = lastScrollTop;
          }
          return;
        }
        const element = target as HTMLElement | null;
        if (!element) return;
        // `overflowAncestors` is filtered for scrollability at mount time;
        // keep this hot event path free of synchronous layout reads.  The
        // nearest container remains the anchor source when nested scroll roots
        // also receive the bubbled event.
        if (scrollContainer && element !== scrollContainer) return;
        lastScrollTop = readScrollTop(scrollContainer ?? element);
        if (!resizePending) {
          lastMeasuredScrollTop = lastScrollTop;
        }
      };
      for (const ancestor of overflowAncestors) {
        ancestor.addEventListener("scroll", trackScrollPosition, {
          passive: true,
        });
      }
      if (overflowAncestors.length === 0 && typeof window !== "undefined") {
        window.addEventListener("scroll", trackScrollPosition, {
          passive: true,
        });
        windowScrollListenerAttached = true;
      }
      const measure = () => {
        if (!renderStarted) return;
        if (!scrollContainer) {
          scrollContainer = findNearestScrollableAncestor(wrap);
          if (scrollContainer && !resizePending) {
            lastScrollTop = readScrollTop(scrollContainer);
            lastMeasuredScrollTop = lastScrollTop;
          }
        }
        if (
          scrollContainer &&
          !overflowAncestors.includes(scrollContainer)
        ) {
          if (windowScrollListenerAttached && typeof window !== "undefined") {
            window.removeEventListener("scroll", trackScrollPosition);
            windowScrollListenerAttached = false;
          }
          overflowAncestors.push(scrollContainer);
          scrollContainer.addEventListener("scroll", trackScrollPosition, {
            passive: true,
          });
        }
        const ready = Boolean(
          embed.querySelector(
            "iframe, .twitter-tweet-rendered, [data-twitter-widget]",
          ),
        );
        if (!ready) {
          const shellHeight = Math.round(readElementHeight(wrap));
          if (shellHeight > 0 && Math.abs(shellHeight - lastHeight) < 2) {
            resizePending = false;
            lastScrollTop = readScrollTop(scrollContainer);
            lastMeasuredScrollTop = lastScrollTop;
          }
          return;
        }

        // The shell reserves the previous height before the widget renders.
        // Remove that reservation before measuring the rendered content so a
        // cached 760px value can legitimately shrink to a 300px iframe.
        const reservedMinHeight = wrap.style.minHeight;
        wrap.style.removeProperty("min-height");
        const measured = Math.round(readXEmbedContentHeight(embed));
        if (!measured) {
          wrap.style.minHeight = reservedMinHeight;
          return;
        }
        const nextHeight = clampXEmbedHeight(measured);
        const currentScrollTop = readScrollTop(scrollContainer);
        const delta = nextHeight - lastHeight;
        const expectedNativeScrollTop = lastMeasuredScrollTop + delta;
        if (Math.abs(currentScrollTop - lastMeasuredScrollTop) <= 4) {
          preserveScrollAnchor(
            wrap,
            lastHeight,
            nextHeight,
            lastMeasuredScrollTop,
            scrollContainer,
          );
        } else if (Math.abs(currentScrollTop - expectedNativeScrollTop) > 4) {
          // A user scroll happened while the widget was resizing.  Do not
          // force the reader back to an older position.
          lastMeasuredScrollTop = currentScrollTop;
        }
        if (Math.abs(nextHeight - lastHeight) >= 3) {
          cacheXEmbedHeight(this.url, nextHeight);
          lastHeight = nextHeight;
        }
        // Keep a stable reservation after measuring.  Since it is derived
        // from content rather than the wrapper, this assignment also records
        // shrinkage instead of pinning the old cached height forever.
        wrap.style.minHeight = `${nextHeight}px`;
        lastScrollTop = readScrollTop(scrollContainer);
        lastMeasuredScrollTop = lastScrollTop;
        resizePending = false;
      };
      const resizeObserver =
        typeof ResizeObserver === "undefined"
          ? null
          : new ResizeObserver(() => {
              resizePending = true;
              measure();
            });
      resizeObserver?.observe(wrap);
      // Observe the content independently of the reserved shell.  A cached
      // 760px wrapper can hide a 300px iframe resize, so observing only the
      // wrapper would never deliver the shrink measurement.
      resizeObserver?.observe(embed);
      const mutationObserver =
        typeof MutationObserver === "undefined"
          ? null
          : new MutationObserver(() => {
              resizePending = true;
              queueMicrotask(measure);
            });
      mutationObserver?.observe(embed, { childList: true, subtree: true });
      const stopNearViewport = observeNearViewport(wrap, () => {
        renderStarted = true;
        loadTwitterWidgets(embed);
        queueMicrotask(measure);
      });
      this.cleanup = () => {
        stopNearViewport();
        resizeObserver?.disconnect();
        mutationObserver?.disconnect();
        for (const ancestor of overflowAncestors) {
          ancestor.removeEventListener("scroll", trackScrollPosition);
        }
        if (windowScrollListenerAttached && typeof window !== "undefined") {
          window.removeEventListener("scroll", trackScrollPosition);
          windowScrollListenerAttached = false;
        }
      };
      return wrap;
    }

    const wrap = document.createElement("div");
    wrap.className = "cm-link-embed";
    wrap.style.cssText =
      "position: relative; max-width: 500px; margin: 2px 0; border: 1px solid var(--border, #333); border-radius: 8px; background: var(--muted, #1e1e2e); cursor: pointer; font-size: 12px; user-select: none;";
    wrap.style.minHeight = `${LINK_EMBED_CARD_HEIGHT}px`;

    if (this.loading || !this.data) {
      wrap.style.cssText +=
        "padding: 8px 12px; color: var(--muted-foreground, #888);";
      wrap.textContent = "リンク情報を取得中...";
      this.cleanup = observeNearViewport(wrap, () =>
        requestOgpForView(this.view, this.url),
      );
      return wrap;
    }

    const hasOgp = this.data.success;
    const card = document.createElement("div");
    card.style.cssText =
      "display: flex; gap: 10px; align-items: center; min-height: 56px; padding: 8px 12px;";

    const pageUrl = this.data.url || this.url;
    const faviconUrl = sanitizeMetadataImageUrl(this.data.favicon, pageUrl);
    if (faviconUrl) {
      const icon = document.createElement("img");
      icon.src = faviconUrl;
      icon.width = 16;
      icon.height = 16;
      icon.style.cssText = "flex-shrink: 0; border-radius: 2px;";
      icon.onerror = () => (icon.style.display = "none");
      card.appendChild(icon);
    }

    if (hasOgp && (this.data.title || this.data.description)) {
      const textWrap = document.createElement("div");
      textWrap.style.cssText = "flex: 1; min-width: 0; overflow: hidden;";
      if (this.data.title) {
        const title = document.createElement("div");
        title.textContent = this.data.title;
        title.style.cssText =
          "font-weight: 500; color: var(--foreground, #cdd6f4); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;";
        textWrap.appendChild(title);
      }
      if (this.data.description) {
        const desc = document.createElement("div");
        const d = this.data.description;
        desc.textContent = d.length > 100 ? d.slice(0, 100) + "..." : d;
        desc.style.cssText =
          "color: var(--muted-foreground, #888); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px;";
        textWrap.appendChild(desc);
      }
      card.appendChild(textWrap);
    } else {
      const urlEl = document.createElement("span");
      urlEl.textContent = this.url;
      urlEl.style.cssText =
        "color: var(--primary, #7c93f0); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;";
      card.appendChild(urlEl);
    }

    const imageUrl = sanitizeMetadataImageUrl(this.data.image, pageUrl);
    if (imageUrl) {
      const img = document.createElement("img");
      img.src = imageUrl;
      img.style.cssText =
        "width: 60px; height: 40px; object-fit: cover; border-radius: 4px; flex-shrink: 0;";
      img.onerror = () => (img.style.display = "none");
      card.appendChild(img);
    }
    wrap.appendChild(card);
    wrap.addEventListener("click", (e) => {
      if ((e.target as HTMLElement).closest(".cm-le-actions")) return;
      window.open(this.url, "_blank", "noopener,noreferrer");
    });
    appendActionBar(wrap, this.url, this.view, "embed", this.controller);
    return wrap;
  }

  destroy() {
    this.cleanup?.();
    this.cleanup = null;
  }

  ignoreEvent() {
    return true;
  }
}

// ─── Link モード: ハイパーリンク表示 ───

class LinkWidget extends WidgetType {
  readonly readOnlySnapshot: boolean;

  constructor(
    readonly url: string,
    readonly view: EditorView,
    readonly controller: LinkDisplayModeController,
  ) {
    super();
    this.readOnlySnapshot = controller.readOnly;
  }

  eq(other: LinkWidget) {
    return (
      this.url === other.url &&
      this.readOnlySnapshot === other.readOnlySnapshot &&
      this.controller === other.controller
    );
  }

  toDOM() {
    const wrap = document.createElement("span");
    wrap.className = "cm-link-embed cm-link-inline";
    wrap.style.cssText =
      "position: relative; display: inline-flex; align-items: center; gap: 4px; user-select: none; padding: 2px 0;";

    const link = document.createElement("a");
    link.href = this.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = this.url;
    link.style.cssText =
      "color: var(--primary, #7c93f0); text-decoration: underline; cursor: pointer;";
    wrap.appendChild(link);

    appendActionBar(wrap, this.url, this.view, "link", this.controller);
    return wrap;
  }

  ignoreEvent() {
    return true;
  }
}

// ─── デコレーション構築 ───

function buildDecorations(
  view: EditorView,
  controller: LinkDisplayModeController,
): DecorationSet {
  const decorations: Range<Decoration>[] = [];

  for (let i = 1; i <= view.state.doc.lines; i++) {
    const line = view.state.doc.line(i);
    const text = line.text;
    URL_REGEX.lastIndex = 0;
    let match: RegExpExecArray | null;

    while ((match = URL_REGEX.exec(text)) !== null) {
      const url = match[0];
      const from = line.from + match.index;
      const to = from + url.length;
      const cached = ogpCache.get(url) ?? null;
      const loading = !cached && ogpInFlight.has(url);
      const mode = getDisplayMode(url, controller);

      if (mode === "embed") {
        decorations.push(
          Decoration.replace({
            widget: new EmbedWidget(url, cached, loading, view, controller),
            inclusive: false,
          }).range(from, to),
        );
      } else {
        decorations.push(
          Decoration.replace({
            widget: new LinkWidget(url, view, controller),
            inclusive: false,
          }).range(from, to),
        );
      }

      break; // 1行につき1つ
    }
  }

  return Decoration.set(decorations, true);
}

// ─── ViewPlugin ───

export function createLinkEmbedPlugin(
  defaultDisplayMode: DisplayMode = "embed",
  options: {
    displayModes?: LinkDisplayModeMap | null;
    onDisplayModeChange?: LinkDisplayModeChangeHandler;
    readOnly?: boolean;
  } = {},
) {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;
      controller: LinkDisplayModeController;
      readonly view: EditorView;

      constructor(view: EditorView) {
        this.view = view;
        this.controller = {
          modes: new Map(
            Object.entries(options.displayModes ?? {}).filter(
              (entry): entry is [string, DisplayMode] =>
                entry[1] === "embed" || entry[1] === "link",
            ),
          ),
          defaultDisplayMode,
          onChange: options.onDisplayModeChange,
          readOnly: options.readOnly ?? false,
        };
        this.decorations = buildDecorations(view, this.controller);
      }

      update(update: ViewUpdate) {
        const hasRebuild = update.transactions.some((tr) =>
          tr.effects.some((e) => e.is(rebuildEffect)),
        );
        const configEffects = update.transactions.flatMap((tr) =>
          tr.effects.filter((e) => e.is(updateLinkEmbedConfigEffect)),
        );
        let configChanged = false;
        for (const effect of configEffects) {
          const config = effect.value;
          const nextModes = new Map(
            Object.entries(config.displayModes ?? {}).filter(
              (entry): entry is [string, DisplayMode] =>
                entry[1] === "embed" || entry[1] === "link",
            ),
          );
          if (this.controller.defaultDisplayMode !== config.defaultDisplayMode) {
            this.controller.defaultDisplayMode = config.defaultDisplayMode;
            configChanged = true;
          }
          if (this.controller.readOnly !== (config.readOnly ?? false)) {
            this.controller.readOnly = config.readOnly ?? false;
            configChanged = true;
          }
          if (this.controller.onChange !== config.onDisplayModeChange) {
            this.controller.onChange = config.onDisplayModeChange;
          }
          if (
            this.controller.modes.size !== nextModes.size ||
            [...this.controller.modes].some(
              ([url, mode]) => nextModes.get(url) !== mode,
            )
          ) {
            this.controller.modes.clear();
            for (const [url, mode] of nextModes) {
              this.controller.modes.set(url, mode);
            }
            configChanged = true;
          }
        }
        if (update.docChanged || hasRebuild || configChanged) {
          this.decorations = buildDecorations(update.view, this.controller);
        }
      }

      destroy() {
        const frame = viewRebuildFrames.get(this.view);
        if (frame !== undefined && frame > 0 && typeof cancelAnimationFrame === "function") {
          cancelAnimationFrame(frame);
        }
        viewRebuildFrames.delete(this.view);
        // ViewPlugin.destroy also runs when its Compartment is reconfigured.
        // Defer the view-disposal check until EditorView.destroy() has set its
        // internal flag, so linkPreviews false→true can reuse the same view.
        queueMicrotask(() => {
          if (isDestroyedView(this.view)) destroyedViews.add(this.view);
        });
      }

    },
    {
      decorations: (v) => v.decorations,
    },
  );
}

export const linkEmbedPlugin = createLinkEmbedPlugin();

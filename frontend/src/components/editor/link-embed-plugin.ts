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
};

const rebuildEffect = StateEffect.define<null>();

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

  const existing = document.querySelector<HTMLScriptElement>(
    `script[src="${TWITTER_WIDGETS_SCRIPT}"]`,
  );
  if (existing) {
    existing.addEventListener("load", render, { once: true });
    return;
  }

  const script = document.createElement("script");
  script.src = TWITTER_WIDGETS_SCRIPT;
  script.async = true;
  script.charset = "utf-8";
  script.addEventListener("load", render, { once: true });
  document.head.appendChild(script);
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

  container.addEventListener("mouseenter", () => (bar.style.opacity = "1"));
  container.addEventListener("mouseleave", () => (bar.style.opacity = "0"));
  container.appendChild(bar);
}

// ─── Embed モード: OGP プレビューカード ───

class EmbedWidget extends WidgetType {
  constructor(
    readonly url: string,
    readonly data: OgpData | null,
    readonly loading: boolean,
    readonly view: EditorView,
    readonly controller: LinkDisplayModeController,
  ) {
    super();
  }

  eq(other: EmbedWidget) {
    return (
      this.url === other.url &&
      this.loading === other.loading &&
      this.data?.success === other.data?.success &&
      this.controller === other.controller
    );
  }

  toDOM() {
    if (
      this.data?.success &&
      this.data.embed_type === "x-post" &&
      this.data.embed_html
    ) {
      const wrap = document.createElement("div");
      wrap.className = "cm-link-embed cm-link-embed-x-post";
      wrap.style.cssText =
        "position: relative; max-width: 550px; margin: 6px 0; user-select: none;";

      const embed = document.createElement("div");
      embed.style.cssText =
        "min-height: 160px; overflow: hidden; border-radius: 8px;";
      embed.innerHTML = this.data.embed_html;
      wrap.appendChild(embed);

      wrap.addEventListener("click", (e) => {
        if ((e.target as HTMLElement).closest(".cm-le-actions")) return;
      });

      appendActionBar(wrap, this.url, this.view, "embed", this.controller);
      queueMicrotask(() => loadTwitterWidgets(embed));
      return wrap;
    }

    const wrap = document.createElement("div");
    wrap.className = "cm-link-embed";
    wrap.style.cssText =
      "position: relative; max-width: 500px; margin: 2px 0; border: 1px solid var(--border, #333); border-radius: 8px; background: var(--muted, #1e1e2e); cursor: pointer; font-size: 12px; user-select: none;";

    if (this.loading) {
      wrap.style.cssText +=
        "padding: 8px 12px; color: var(--muted-foreground, #888);";
      wrap.textContent = "リンク情報を取得中...";
      return wrap;
    }

    const hasOgp = this.data?.success;

    const card = document.createElement("div");
    card.style.cssText =
      "display: flex; gap: 10px; align-items: center; padding: 8px 12px;";

    if (this.data?.favicon) {
      const icon = document.createElement("img");
      icon.src = this.data.favicon;
      icon.width = 16;
      icon.height = 16;
      icon.style.cssText = "flex-shrink: 0; border-radius: 2px;";
      icon.onerror = () => (icon.style.display = "none");
      card.appendChild(icon);
    }

    if (hasOgp && (this.data!.title || this.data!.description)) {
      const textWrap = document.createElement("div");
      textWrap.style.cssText = "flex: 1; min-width: 0; overflow: hidden;";

      if (this.data!.title) {
        const title = document.createElement("div");
        title.textContent = this.data!.title;
        title.style.cssText =
          "font-weight: 500; color: var(--foreground, #cdd6f4); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;";
        textWrap.appendChild(title);
      }
      if (this.data!.description) {
        const desc = document.createElement("div");
        const d = this.data!.description;
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

    if (this.data?.image) {
      const img = document.createElement("img");
      img.src = this.data.image;
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

  ignoreEvent() {
    return true;
  }
}

// ─── Link モード: ハイパーリンク表示 ───

class LinkWidget extends WidgetType {
  constructor(
    readonly url: string,
    readonly view: EditorView,
    readonly controller: LinkDisplayModeController,
  ) {
    super();
  }

  eq(other: LinkWidget) {
    return this.url === other.url && this.controller === other.controller;
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
  fetching: Set<string>,
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
      const loading = fetching.has(url) && !cached;
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
  } = {},
) {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;
      fetching = new Set<string>();
      controller: LinkDisplayModeController;

      constructor(view: EditorView) {
        this.controller = {
          modes: new Map(
            Object.entries(options.displayModes ?? {}).filter(
              (entry): entry is [string, DisplayMode] =>
                entry[1] === "embed" || entry[1] === "link",
            ),
          ),
          defaultDisplayMode,
          onChange: options.onDisplayModeChange,
        };
        this.decorations = buildDecorations(
          view,
          this.fetching,
          this.controller,
        );
        this.fetchMissing(view);
      }

      update(update: ViewUpdate) {
        const hasRebuild = update.transactions.some((tr) =>
          tr.effects.some((e) => e.is(rebuildEffect)),
        );
        if (update.docChanged || update.viewportChanged || hasRebuild) {
          this.decorations = buildDecorations(
            update.view,
            this.fetching,
            this.controller,
          );
          if (update.docChanged) {
            this.fetchMissing(update.view);
          }
        }
      }

      fetchMissing(view: EditorView) {
        for (let i = 1; i <= view.state.doc.lines; i++) {
          const text = view.state.doc.line(i).text;
          URL_REGEX.lastIndex = 0;
          const match = URL_REGEX.exec(text);
          if (match) {
            const url = match[0];
            if (!ogpCache.has(url) && !this.fetching.has(url)) {
              this.fetching.add(url);
              fetchOgp(url)
                .then((data) => {
                  ogpCache.set(url, data);
                  this.fetching.delete(url);
                  view.dispatch({ effects: [rebuildEffect.of(null)] });
                })
                .catch(() => {
                  this.fetching.delete(url);
                  ogpCache.set(url, { success: false, url });
                  view.dispatch({ effects: [rebuildEffect.of(null)] });
                });
            }
          }
        }
      }
    },
    {
      decorations: (v) => v.decorations,
    },
  );
}

export const linkEmbedPlugin = createLinkEmbedPlugin();

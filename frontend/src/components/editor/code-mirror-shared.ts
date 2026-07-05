"use client";

import { autocompletion, completionKeymap } from "@codemirror/autocomplete";
import {
  defaultKeymap,
  history,
  historyKeymap,
  indentWithTab,
} from "@codemirror/commands";
import { css } from "@codemirror/lang-css";
import { html } from "@codemirror/lang-html";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import {
  bracketMatching,
  defaultHighlightStyle,
  foldKeymap,
  indentOnInput,
  syntaxHighlighting,
} from "@codemirror/language";
import { searchKeymap } from "@codemirror/search";
import { EditorState, type Extension } from "@codemirror/state";
import {
  crosshairCursor,
  drawSelection,
  dropCursor,
  EditorView,
  keymap,
  rectangularSelection,
} from "@codemirror/view";

export type EditorLanguage =
  | "plain"
  | "markdown"
  | "json"
  | "javascript"
  | "typescript"
  | "python"
  | "html"
  | "css";

export function getLanguageExtension(language: string): Extension | null {
  switch (language) {
    case ".js":
    case ".jsx":
    case "javascript":
      return javascript({ jsx: true });
    case ".ts":
    case ".tsx":
    case "typescript":
      return javascript({ jsx: true, typescript: true });
    case ".py":
    case "python":
      return python();
    case ".json":
    case "json":
      return json();
    case ".html":
    case "html":
      return html();
    case ".css":
    case "css":
      return css();
    case ".md":
    case ".markdown":
    case "markdown":
      return markdown();
    default:
      return null;
  }
}

export const selectLineKeymap = keymap.of([
  {
    key: "Ctrl-l",
    run(view) {
      const { state } = view;
      const selection = state.selection.main;
      const line = state.doc.lineAt(selection.head);
      view.dispatch({
        selection: {
          anchor: line.from,
          head: Math.min(line.to + 1, state.doc.length),
        },
      });
      return true;
    },
  },
]);

export const selectNextOccurrenceKeymap = keymap.of([
  {
    key: "Ctrl-d",
    run(view) {
      const { state } = view;
      const selection = state.selection.main;
      if (selection.empty) {
        const word = state.wordAt(selection.head);
        if (!word) return true;
        view.dispatch({ selection: { anchor: word.from, head: word.to } });
        return true;
      }

      const selectedText = state.sliceDoc(selection.from, selection.to);
      if (!selectedText) return true;
      const afterSelection = state.sliceDoc(selection.to);
      const nextIndex = afterSelection.indexOf(selectedText);
      if (nextIndex < 0) return true;

      const from = selection.to + nextIndex;
      const to = from + selectedText.length;
      view.dispatch({ selection: { anchor: from, head: to } });
      return true;
    },
  },
]);

export function markdownHeadingKeymap(): Extension {
  return keymap.of([
    {
      key: "Ctrl-Shift-]",
      run(view) {
        const { state } = view;
        const pos = state.selection.main.head;
        const line = state.doc.lineAt(pos);
        const text = line.text;
        const match = text.match(/^(#{0,6})\s*/);
        const level = match ? match[1].length : 0;
        if (level >= 6) return true;
        const rest = text.replace(/^#{0,6}\s*/, "");
        const newLine = `${"#".repeat(level + 1)} ${rest}`;
        const newPos = line.from + newLine.length;
        view.dispatch({
          changes: { from: line.from, to: line.to, insert: newLine },
          selection: { anchor: newPos, head: newPos },
        });
        return true;
      },
    },
    {
      key: "Ctrl-Shift-[",
      run(view) {
        const { state } = view;
        const pos = state.selection.main.head;
        const line = state.doc.lineAt(pos);
        const text = line.text;
        const match = text.match(/^(#{1,6})\s*/);
        if (!match) return true;
        const level = match[1].length;
        const rest = text.replace(/^#{1,6}\s*/, "");
        const newLine = level <= 1 ? rest : `${"#".repeat(level - 1)} ${rest}`;
        const newPos = line.from + newLine.length;
        view.dispatch({
          changes: { from: line.from, to: line.to, insert: newLine },
          selection: { anchor: newPos, head: newPos },
        });
        return true;
      },
    },
  ]);
}

export function baseTextEditorExtensions(options?: {
  language?: string;
  includeSearch?: boolean;
  includeCompletion?: boolean;
}): Extension[] {
  const language = options?.language ?? "markdown";
  const languageExtension = getLanguageExtension(language);
  return [
    history(),
    drawSelection(),
    dropCursor(),
    EditorState.allowMultipleSelections.of(true),
    indentOnInput(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    bracketMatching(),
    rectangularSelection(),
    crosshairCursor(),
    EditorView.lineWrapping,
    selectLineKeymap,
    selectNextOccurrenceKeymap,
    ...(language === "markdown" || language === ".md" || language === ".markdown"
      ? [markdownHeadingKeymap()]
      : []),
    keymap.of([
      ...defaultKeymap,
      ...(options?.includeSearch ? searchKeymap : []),
      ...historyKeymap,
      ...(options?.includeCompletion ? completionKeymap : []),
      ...foldKeymap,
      indentWithTab,
    ]),
    ...(options?.includeCompletion ? [autocompletion()] : []),
    ...(languageExtension ? [languageExtension] : []),
  ];
}

export function textEditorTheme(options?: {
  minHeight?: number;
  maxHeight?: number;
  fontSize?: number;
  fontFamily?: string;
  compact?: boolean;
  surface?: "solid" | "glass";
}) {
  const minHeight = options?.minHeight ?? 120;
  const maxHeight = options?.maxHeight;
  const fontSize = options?.fontSize ?? 13;
  const fontFamily = options?.fontFamily ?? "inherit";
  const padding = options?.compact ? "6px 10px" : "8px 12px";
  const surface = options?.surface ?? "solid";
  const rootStyles: Record<string, string> = {
    minHeight: `${minHeight}px`,
    fontSize: `${fontSize}px`,
    border: "1px solid var(--input, var(--border, #333))",
    borderRadius: "0.5rem",
    background:
      surface === "glass"
        ? "color-mix(in oklab, var(--background, #0f172a) 72%, transparent)"
        : "var(--background, #0f172a)",
    boxShadow:
      surface === "glass"
        ? "inset 0 1px color-mix(in oklab, var(--foreground, #e5e7eb) 16%, transparent)"
        : "none",
    backdropFilter: surface === "glass" ? "blur(18px)" : "none",
    overflow: "hidden",
  };
  const scrollerStyles: Record<string, string> = {
    minHeight: `${minHeight}px`,
    background: "transparent",
    overflow: "auto",
    fontFamily,
  };

  if (maxHeight) {
    rootStyles.maxHeight = `${maxHeight}px`;
    scrollerStyles.maxHeight = `${maxHeight}px`;
  }

  return EditorView.theme({
    "&": rootStyles,
    "&.cm-focused": {
      outline: "2px solid var(--ring, #7c93f0)",
      outlineOffset: "-1px",
    },
    ".cm-scroller": scrollerStyles,
    ".cm-content": {
      minHeight: `${minHeight}px`,
      background: "transparent",
      padding,
      caretColor: "var(--foreground, #cdd6f4)",
    },
    ".cm-line": {
      padding: "0",
    },
  });
}

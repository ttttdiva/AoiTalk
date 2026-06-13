"use client";

import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { Compartment, EditorState, Prec } from "@codemirror/state";
import {
  EditorView,
  keymap,
  placeholder as cmPlaceholder,
} from "@codemirror/view";
import { oneDark } from "@codemirror/theme-one-dark";
import { cn } from "@/lib/utils";
import {
  createLinkEmbedPlugin,
  type LinkDisplayModeChangeHandler,
  type LinkDisplayModeMap,
} from "./link-embed-plugin";
import { useUserSettings } from "@/contexts/user-settings-context";
import { useTheme } from "@/contexts/theme-context";
import {
  baseTextEditorExtensions,
  textEditorTheme,
  type EditorLanguage,
} from "./code-mirror-shared";

type LongTextEditorProps = {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  minHeight?: number;
  maxHeight?: number;
  className?: string;
  readOnly?: boolean;
  language?: EditorLanguage;
  fontFamily?: string;
  fontSize?: number;
  compact?: boolean;
  linkPreviews?: boolean;
  linkDisplayModes?: LinkDisplayModeMap | null;
  onLinkDisplayModeChange?: LinkDisplayModeChangeHandler;
  onSubmitIntent?: (value: string) => void;
  onArrowUpFromStart?: () => void;
};

export type LongTextEditorHandle = {
  focus: () => void;
};

export const LongTextEditor = forwardRef<
  LongTextEditorHandle,
  LongTextEditorProps
>(function LongTextEditor(
  {
    id,
    value,
    onChange,
    placeholder = "",
    minHeight = 120,
    maxHeight,
    className,
    readOnly = false,
    language = "markdown",
    fontFamily,
    fontSize,
    compact,
    linkPreviews = false,
    linkDisplayModes,
    onLinkDisplayModeChange,
    onSubmitIntent,
    onArrowUpFromStart,
  },
  ref,
) {
  const { editorLinkDefaultDisplayMode } = useUserSettings();
  const { resolvedTheme } = useTheme();
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const linkEmbedCompartmentRef = useRef(new Compartment());
  const onChangeRef = useRef(onChange);
  const onSubmitIntentRef = useRef(onSubmitIntent);
  const onArrowUpFromStartRef = useRef(onArrowUpFromStart);
  const externalValueRef = useRef(value);

  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  useEffect(() => {
    onSubmitIntentRef.current = onSubmitIntent;
  }, [onSubmitIntent]);

  useEffect(() => {
    onArrowUpFromStartRef.current = onArrowUpFromStart;
  }, [onArrowUpFromStart]);

  useImperativeHandle(
    ref,
    () => ({
      focus: () => {
        viewRef.current?.focus();
      },
    }),
    [],
  );

  useEffect(() => {
    if (!containerRef.current) return;

    const state = EditorState.create({
      doc: value,
      extensions: [
        ...baseTextEditorExtensions({ language }),
        EditorView.updateListener.of((update) => {
          if (!update.docChanged) return;
          const nextValue = update.state.doc.toString();
          externalValueRef.current = nextValue;
          onChangeRef.current(nextValue);
        }),
        Prec.high(
          keymap.of([
            {
              key: "Mod-Enter",
              run: (view) => {
                const submit = onSubmitIntentRef.current;
                if (!submit) return false;
                submit(view.state.doc.toString());
                return true;
              },
            },
            {
              key: "ArrowUp",
              run: (view) => {
                const moveFocusUp = onArrowUpFromStartRef.current;
                if (!moveFocusUp) return false;
                const selection = view.state.selection.main;
                if (!selection.empty) return false;
                const line = view.state.doc.lineAt(selection.head);
                if (line.number !== 1) return false;
                moveFocusUp();
                return true;
              },
            },
          ]),
        ),
        ...(placeholder ? [cmPlaceholder(placeholder)] : []),
        ...(linkPreviews
          ? [
              linkEmbedCompartmentRef.current.of(
                createLinkEmbedPlugin(editorLinkDefaultDisplayMode, {
                  displayModes: linkDisplayModes,
                  onDisplayModeChange: onLinkDisplayModeChange,
                }),
              ),
            ]
          : []),
        ...(readOnly ? [EditorState.readOnly.of(true)] : []),
        ...(resolvedTheme === "dark" ? [oneDark] : []),
        textEditorTheme({
          minHeight,
          maxHeight,
          fontFamily,
          fontSize,
          compact,
        }),
      ],
    });

    const view = new EditorView({ state, parent: containerRef.current });
    viewRef.current = view;

    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // Recreate only for static editor options and theme; content is synchronized below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedTheme]);

  useEffect(() => {
    if (!linkPreviews || !viewRef.current) return;
    viewRef.current.dispatch({
      effects: linkEmbedCompartmentRef.current.reconfigure(
        createLinkEmbedPlugin(editorLinkDefaultDisplayMode, {
          displayModes: linkDisplayModes,
          onDisplayModeChange: onLinkDisplayModeChange,
        }),
      ),
    });
  }, [
    editorLinkDefaultDisplayMode,
    linkDisplayModes,
    linkPreviews,
    onLinkDisplayModeChange,
  ]);

  useEffect(() => {
    if (!viewRef.current) return;
    if (value === externalValueRef.current) return;
    externalValueRef.current = value;
    const current = viewRef.current.state.doc.toString();
    if (current !== value) {
      viewRef.current.dispatch({
        changes: { from: 0, to: current.length, insert: value },
      });
    }
  }, [value]);

  return (
    <div id={id} ref={containerRef} className={cn("min-w-0", className)} />
  );
});

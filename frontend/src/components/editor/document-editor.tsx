"use client";

/* eslint-disable @next/next/no-img-element */

import { useRef, useEffect, useState, useCallback } from "react";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import {
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
} from "@codemirror/view";
import { foldGutter } from "@codemirror/language";
import { highlightSelectionMatches } from "@codemirror/search";
import {
  autocompletion,
  completionKeymap,
  closeBrackets,
  closeBracketsKeymap,
} from "@codemirror/autocomplete";
import { oneDark } from "@codemirror/theme-one-dark";
import { snippetCompletionSource } from "./snippet-completion";
import { createLinkEmbedPlugin } from "./link-embed-plugin";
import { Save, X, MessageSquare, Eye, Code2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { explorerSave } from "@/lib/explorer-api";
import { cn } from "@/lib/utils";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { baseTextEditorExtensions } from "./code-mirror-shared";
import { useUserSettings } from "@/contexts/user-settings-context";

interface DocumentEditorProps {
  filePath: string;
  initialContent: string;
  extension: string;
  onSave?: (path: string, content: string) => Promise<void>;
  onClose: () => void;
  onAskAI?: (selectedText: string, filePath: string) => void;
  snippets?: import("@/lib/snippets-api").Snippet[];
}

const documentEditorTheme = EditorView.theme({
  "&": {
    height: "100%",
    minHeight: "0",
  },
  ".cm-scroller": {
    overflow: "auto",
  },
  ".cm-content": {
    minHeight: "100%",
  },
});

export function DocumentEditor({
  filePath,
  initialContent,
  extension,
  onSave,
  onClose,
  onAskAI,
  snippets,
}: DocumentEditorProps) {
  const { editorLinkDefaultDisplayMode } = useUserSettings();
  const editorRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const linkEmbedCompartmentRef = useRef(new Compartment());
  const [isDirty, setIsDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [cursorInfo, setCursorInfo] = useState({ line: 1, col: 1 });
  const [previewMode, setPreviewMode] = useState(false);
  const [previewContent, setPreviewContent] = useState(initialContent);
  const contentRef = useRef(initialContent);
  const filePathRef = useRef(filePath);

  const isMarkdown = extension === ".md" || extension === ".markdown";

  // Keep refs in sync
  useEffect(() => {
    filePathRef.current = filePath;
  }, [filePath]);

  // Toggle markdown preview with Ctrl+Shift+V
  const togglePreview = useCallback(() => {
    if (!isMarkdown) return;
    setPreviewMode((prev) => {
      if (!prev && viewRef.current) {
        // Entering preview: capture latest editor content
        setPreviewContent(viewRef.current.state.doc.toString());
      }
      return !prev;
    });
  }, [isMarkdown]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && e.key === "V") {
        e.preventDefault();
        togglePreview();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [togglePreview]);

  const handleSave = useCallback(async () => {
    if (!viewRef.current) return;
    const content = viewRef.current.state.doc.toString();
    setSaving(true);
    try {
      if (onSave) {
        await onSave(filePathRef.current, content);
      } else {
        const result = await explorerSave(filePathRef.current, content);
        if (!result.success) {
          console.error("Save failed:", result.message);
          return;
        }
      }
      setIsDirty(false);
      contentRef.current = content;
    } finally {
      setSaving(false);
    }
  }, [onSave]);

  const handleAskAI = useCallback(() => {
    if (!viewRef.current || !onAskAI) return;
    const { state } = viewRef.current;
    const sel = state.selection.main;
    const selectedText = sel.empty ? "" : state.sliceDoc(sel.from, sel.to);
    onAskAI(selectedText, filePathRef.current);
  }, [onAskAI]);

  // Initialize CodeMirror
  useEffect(() => {
    if (!editorRef.current) return;

    const extensions: Extension[] = [
      lineNumbers(),
      highlightActiveLineGutter(),
      foldGutter(),
      closeBrackets(),
      autocompletion(
        snippets?.length
          ? { override: [snippetCompletionSource(snippets)] }
          : undefined,
      ),
      highlightActiveLine(),
      highlightSelectionMatches(),
      ...baseTextEditorExtensions({ language: extension, includeSearch: true }),
      keymap.of([
        ...closeBracketsKeymap,
        ...completionKeymap,
        // Ctrl+S save
        {
          key: "Ctrl-s",
          run() {
            handleSave();
            return true;
          },
        },
      ]),
      // Track changes
      EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          setIsDirty(true);
        }
        if (update.selectionSet) {
          const pos = update.state.selection.main.head;
          const line = update.state.doc.lineAt(pos);
          setCursorInfo({
            line: line.number,
            col: pos - line.from + 1,
          });
        }
      }),
      // Link embed (URL → OGP preview card)
      linkEmbedCompartmentRef.current.of(
        createLinkEmbedPlugin(editorLinkDefaultDisplayMode),
      ),
      // Theme
      oneDark,
      documentEditorTheme,
    ];

    const state = EditorState.create({
      doc: initialContent,
      extensions,
    });

    const view = new EditorView({
      state,
      parent: editorRef.current,
    });

    viewRef.current = view;

    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!viewRef.current) return;
    viewRef.current.dispatch({
      effects: linkEmbedCompartmentRef.current.reconfigure(
        createLinkEmbedPlugin(editorLinkDefaultDisplayMode),
      ),
    });
  }, [editorLinkDefaultDisplayMode]);

  // Update content when a new file is loaded (initialContent prop changes)
  // NOTE: isDirty is intentionally NOT in deps — triggering on isDirty=false after
  // save would overwrite the editor with stale initialContent, making text disappear.
  useEffect(() => {
    if (!viewRef.current) return;
    const currentContent = viewRef.current.state.doc.toString();
    if (currentContent !== initialContent) {
      viewRef.current.dispatch({
        changes: {
          from: 0,
          to: currentContent.length,
          insert: initialContent,
        },
      });
      contentRef.current = initialContent;
      setIsDirty(false);
    }
  }, [initialContent]);

  const fileName = filePath.split("/").pop() || filePath;
  const langLabel =
    extension === ".md"
      ? "Markdown"
      : extension === ".py"
        ? "Python"
        : extension === ".js" || extension === ".jsx"
          ? "JavaScript"
          : extension === ".ts" || extension === ".tsx"
            ? "TypeScript"
            : extension === ".json"
              ? "JSON"
              : extension === ".html"
                ? "HTML"
                : extension === ".css"
                  ? "CSS"
                  : extension.replace(".", "").toUpperCase() || "Text";

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      {/* Toolbar */}
      <div className="flex shrink-0 items-center justify-between border-b px-3 py-1.5">
        <div className="flex items-center gap-2">
          <span
            className="text-sm font-medium truncate max-w-[300px]"
            title={filePath}
          >
            {fileName}
          </span>
          {isDirty && (
            <span
              className="size-2 rounded-full bg-orange-400"
              title="未保存の変更があります"
            />
          )}
        </div>
        <div className="flex items-center gap-1">
          {isMarkdown && (
            <Button
              variant={previewMode ? "secondary" : "ghost"}
              size="sm"
              onClick={togglePreview}
              className="h-7 gap-1 text-xs"
              title="Markdownプレビュー (Ctrl+Shift+V)"
            >
              {previewMode ? (
                <>
                  <Code2 className="size-3.5" />
                  編集
                </>
              ) : (
                <>
                  <Eye className="size-3.5" />
                  プレビュー
                </>
              )}
            </Button>
          )}
          {onAskAI && (
            <Button
              variant="ghost"
              size="sm"
              onClick={handleAskAI}
              className="h-7 gap-1 text-xs"
            >
              <MessageSquare className="size-3.5" />
              AIに質問
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={handleSave}
            disabled={!isDirty || saving}
            className="h-7 gap-1 text-xs"
          >
            <Save className="size-3.5" />
            {saving ? "保存中..." : "保存"}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            onClick={onClose}
            className="size-7"
          >
            <X className="size-3.5" />
          </Button>
        </div>
      </div>

      {/* Editor / Preview */}
      {previewMode && isMarkdown ? (
        <div className="min-h-0 flex-1 overflow-auto p-6">
          <div className="max-w-none text-sm leading-relaxed">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                h1: ({ children }) => (
                  <h1 className="mt-6 mb-4 border-b border-border/40 pb-2 text-3xl font-bold">
                    {children}
                  </h1>
                ),
                h2: ({ children }) => (
                  <h2 className="mt-5 mb-3 border-b border-border/40 pb-1.5 text-2xl font-bold">
                    {children}
                  </h2>
                ),
                h3: ({ children }) => (
                  <h3 className="mt-4 mb-2 text-xl font-semibold">
                    {children}
                  </h3>
                ),
                h4: ({ children }) => (
                  <h4 className="mt-3 mb-2 text-lg font-semibold">
                    {children}
                  </h4>
                ),
                h5: ({ children }) => (
                  <h5 className="mt-3 mb-1 text-base font-semibold">
                    {children}
                  </h5>
                ),
                h6: ({ children }) => (
                  <h6 className="mt-2 mb-1 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                    {children}
                  </h6>
                ),
                p: ({ children }) => (
                  <p className="my-3 leading-7">{children}</p>
                ),
                ul: ({ children }) => (
                  <ul className="my-3 list-disc pl-6 space-y-1">{children}</ul>
                ),
                ol: ({ children }) => (
                  <ol className="my-3 list-decimal pl-6 space-y-1">
                    {children}
                  </ol>
                ),
                blockquote: ({ children }) => (
                  <blockquote className="my-3 border-l-4 border-primary/40 pl-4 italic text-muted-foreground">
                    {children}
                  </blockquote>
                ),
                code: ({ className, children, ...rest }) => {
                  const isInline = !/language-/.test(className || "");
                  if (isInline) {
                    return (
                      <code
                        className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em] text-primary"
                        {...rest}
                      >
                        {children}
                      </code>
                    );
                  }
                  return (
                    <code className={cn("font-mono", className)} {...rest}>
                      {children}
                    </code>
                  );
                },
                pre: ({ children }) => (
                  <pre className="my-3 overflow-x-auto rounded-md border border-border bg-muted p-3 text-xs">
                    {children}
                  </pre>
                ),
                a: ({ children, href }) => (
                  <a
                    href={href}
                    className="text-blue-400 hover:underline"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {children}
                  </a>
                ),
                table: ({ children }) => (
                  <div className="my-3 overflow-x-auto">
                    <table className="min-w-full border border-border text-sm">
                      {children}
                    </table>
                  </div>
                ),
                th: ({ children }) => (
                  <th className="border border-border bg-muted px-3 py-1.5 text-left font-semibold">
                    {children}
                  </th>
                ),
                td: ({ children }) => (
                  <td className="border border-border px-3 py-1.5">
                    {children}
                  </td>
                ),
                hr: () => <hr className="my-5 border-border" />,
                img: ({ src, alt }) => (
                  <img
                    src={src}
                    alt={alt}
                    className="my-3 max-w-full rounded-lg"
                  />
                ),
              }}
            >
              {previewContent}
            </ReactMarkdown>
          </div>
        </div>
      ) : (
        <div ref={editorRef} className="min-h-0 flex-1 overflow-hidden" />
      )}

      {/* Status Bar */}
      <div className="flex shrink-0 items-center justify-between border-t bg-muted/50 px-3 py-0.5 text-xs text-muted-foreground">
        <div className="flex items-center gap-3">
          {previewMode ? (
            <span>プレビュー表示中</span>
          ) : (
            <span>
              行 {cursorInfo.line}, 列 {cursorInfo.col}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {isMarkdown && (
            <span className="text-muted-foreground/60">
              Ctrl+Shift+V: プレビュー切替
            </span>
          )}
          <span>{langLabel}</span>
          <span>UTF-8</span>
        </div>
      </div>
    </div>
  );
}

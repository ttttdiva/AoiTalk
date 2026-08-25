"use client";

/* eslint-disable @next/next/no-img-element */

import { useRef, useEffect, useState, useCallback } from "react";
import { Compartment, EditorState, StateEffect, type Extension } from "@codemirror/state";
import {
  Decoration,
  type DecorationSet,
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
  ViewPlugin,
  WidgetType,
  type ViewUpdate,
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
import {
  createLinkEmbedPlugin,
  updateLinkEmbedConfigEffect,
  type LinkDisplayModeChangeHandler,
  type LinkDisplayModeMap,
} from "./link-embed-plugin";
import { Save, X, MessageSquare, Eye, Code2, FileCode2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  ExplorerUploadError,
  explorerSave,
  explorerUpload,
} from "@/lib/explorer-api";
import type { ExplorerUploadBatchResult } from "@/lib/explorer-api";
import { cn } from "@/lib/utils";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { baseTextEditorExtensions } from "./code-mirror-shared";
import type { EditorImageInsertHandler } from "./image-insert-extension";
import {
  isMarkdownImageExtension,
  resolveRelativeMarkdownImage,
} from "@/lib/editor-image-files";
import { useUserSettings } from "@/contexts/user-settings-context";
import { useTheme } from "@/contexts/theme-context";
import { toast } from "sonner";

interface DocumentEditorProps {
  filePath: string;
  initialContent: string;
  extension: string;
  onSave?: (path: string, content: string) => Promise<void>;
  onClose: () => void;
  onAskAI?: (selectedText: string, filePath: string) => void;
  snippets?: import("@/lib/snippets-api").Snippet[];
  /** Focus the CodeMirror surface after it is mounted. */
  autoFocus?: boolean;
  /** Disable image uploads while keeping the existing editor surface intact. */
  readOnly?: boolean;
  /** Per-URL link preview display overrides, when supplied by the caller. */
  linkDisplayModes?: LinkDisplayModeMap | null;
  /** Called when a user toggles a link preview's display mode. */
  onLinkDisplayModeChange?: LinkDisplayModeChangeHandler;
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

const markdownImagePreviewRefresh = StateEffect.define<void>();

type MarkdownImagePreviewContext = {
  filePathRef: { current: string };
  enabledRef: { current: boolean };
};

const MARKDOWN_IMAGE_PATTERN = /!\[((?:\\.|[^\]\\\n])*)\]\((<[^>\n]+>|[^)\n]+)\)/g;

function markdownImageReference(fileName: string): string {
  const safeName = fileName.replace(/[\r\n]/g, " ");
  const alt = safeName.replace(/[\\[\]]/g, "\\$&");
  const destination = `./${safeName}`;
  const markdownDestination = /[\s()?#]/.test(destination)
    ? `<${destination}>`
    : destination;
  return `![${alt}](${markdownDestination})`;
}

function imageSourceWithoutTitle(value: string): string {
  const trimmed = value.trim();
  if (trimmed.startsWith("<") && trimmed.endsWith(">")) {
    // Keep the angle wrapper so a literal `#`/`?` in a local filename is not
    // mistaken for a URL fragment/query by the shared resolver.
    return trimmed;
  }
  // A quoted Markdown title follows the URL.  Keep spaces in filenames while
  // removing only an unambiguous trailing title segment.
  return trimmed
    .replace(/\s+(?:"[^"]*"|'[^']*')\s*$/u, "")
    .trim();
}

class MarkdownImageWidget extends WidgetType {
  constructor(
    private readonly source: string,
    private readonly alt: string,
    private readonly filePathRef: { current: string },
  ) {
    super();
  }

  eq(other: MarkdownImageWidget): boolean {
    return (
      this.source === other.source &&
      this.alt === other.alt &&
      this.filePathRef === other.filePathRef
    );
  }

  toDOM(): HTMLElement {
    const image = document.createElement("img");
    image.className = "cm-markdown-image";
    image.alt = this.alt;
    image.src = resolveRelativeMarkdownImage(this.source, this.filePathRef.current);
    image.draggable = false;
    image.style.maxWidth = "min(100%, 640px)";
    image.style.maxHeight = "360px";
    image.style.objectFit = "contain";
    image.style.borderRadius = "0.5rem";
    image.style.verticalAlign = "middle";
    image.addEventListener("error", () => {
      image.classList.add("cm-markdown-image-error");
    });
    return image;
  }

  ignoreEvent(): boolean {
    // Let CodeMirror map clicks into the replaced range.  The decoration is
    // rebuilt when the selection changes so users can edit the raw Markdown.
    return false;
  }
}

function buildMarkdownImageDecorations(
  view: EditorView,
  context: MarkdownImagePreviewContext,
): DecorationSet {
  if (!context.enabledRef.current) return Decoration.none;

  const ranges = [];
  for (let lineNumber = 1; lineNumber <= view.state.doc.lines; lineNumber += 1) {
    const line = view.state.doc.line(lineNumber);
    MARKDOWN_IMAGE_PATTERN.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = MARKDOWN_IMAGE_PATTERN.exec(line.text)) !== null) {
      const source = imageSourceWithoutTitle(match[2] ?? "");
      if (!source) continue;
      const from = line.from + match.index;
      const to = from + match[0].length;
      const selected = view.state.selection.ranges.some((selection) =>
        selection.empty
          ? selection.head > from && selection.head < to
          : selection.from < to && selection.to > from,
      );
      if (selected) continue;
      ranges.push(
        Decoration.replace({
          widget: new MarkdownImageWidget(
            source,
            match[1] ?? "",
            context.filePathRef,
          ),
          inclusive: false,
        }).range(from, to),
      );
    }
  }

  return Decoration.set(ranges, true);
}

function createMarkdownImagePreviewExtension(
  context: MarkdownImagePreviewContext,
): Extension {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;

      constructor(view: EditorView) {
        this.decorations = buildMarkdownImageDecorations(view, context);
      }

      update(update: ViewUpdate): void {
        if (
          update.docChanged ||
          update.selectionSet ||
          update.viewportChanged ||
          update.transactions.some((transaction) =>
            transaction.effects.some((effect) => effect.is(markdownImagePreviewRefresh)),
          )
        ) {
          this.decorations = buildMarkdownImageDecorations(update.view, context);
        }
      }
    },
    { decorations: (value) => value.decorations },
  );
}

export function DocumentEditor({
  filePath,
  initialContent,
  extension,
  onSave,
  onClose,
  onAskAI,
  snippets,
  autoFocus = false,
  readOnly = false,
  linkDisplayModes,
  onLinkDisplayModeChange,
}: DocumentEditorProps) {
  const { editorLinkDefaultDisplayMode } = useUserSettings();
  const { resolvedTheme } = useTheme();
  const editorRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const linkEmbedCompartmentRef = useRef(new Compartment());
  const themeCompartmentRef = useRef(new Compartment());
  const [isDirty, setIsDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [cursorInfo, setCursorInfo] = useState({ line: 1, col: 1 });
  const [previewMode, setPreviewMode] = useState(false);
  const [previewContent, setPreviewContent] = useState(initialContent);
  const contentRef = useRef(initialContent);
  const filePathRef = useRef(filePath);
  const fileIdentityRef = useRef(0);
  const extensionRef = useRef(extension);
  const readOnlyRef = useRef(readOnly);
  const markdownImagePreviewEnabledRef = useRef(
    isMarkdownImageExtension(extension),
  );

  const isMarkdown = isMarkdownImageExtension(extension);

  // Keep refs in sync
  useEffect(() => {
    filePathRef.current = filePath;
    fileIdentityRef.current += 1;
  }, [filePath]);

  useEffect(() => {
    extensionRef.current = extension;
    markdownImagePreviewEnabledRef.current = isMarkdownImageExtension(extension);
    viewRef.current?.dispatch({ effects: markdownImagePreviewRefresh.of(undefined) });
  }, [extension]);

  useEffect(() => {
    viewRef.current?.dispatch({ effects: markdownImagePreviewRefresh.of(undefined) });
    setPreviewContent(initialContent);
  }, [filePath, initialContent]);

  useEffect(() => {
    readOnlyRef.current = readOnly;
  }, [readOnly]);

  /**
   * Upload pasted/dropped images next to the Markdown file and return only
   * references for files the server actually accepted.  The shared
   * CodeMirror extension owns the insertion position and event semantics;
   * this handler deliberately does not mutate the document itself.
   */
  const handleImageFiles = useCallback<EditorImageInsertHandler>(
    async (files) => {
      if (readOnlyRef.current || !isMarkdownImageExtension(extensionRef.current)) {
        return null;
      }

      const imageFiles = Array.from(files);
      if (imageFiles.length === 0) return null;

      const currentFilePath = filePathRef.current.replace(/\\/g, "/");
      const separator = currentFilePath.lastIndexOf("/");
      const directory = separator >= 0 ? currentFilePath.slice(0, separator) : "";

      let batchResult: ExplorerUploadBatchResult;
      try {
        batchResult = await explorerUpload(directory, imageFiles);
      } catch (error) {
        // explorerUpload rejects only after all files have been attempted.  A
        // batch may still contain successful uploads, which must remain
        // insertable while failed files leave the document untouched.
        if (error instanceof ExplorerUploadError) {
          batchResult = error.batchResult;
          if (batchResult.successCount === 0) {
            toast.error("画像のアップロードに失敗しました");
          } else if (batchResult.failureCount > 0) {
            toast.warning(
              `${batchResult.successCount}件をアップロードし、${batchResult.failureCount}件は失敗しました`,
            );
          }
        } else {
          const candidate =
            error && typeof error === "object" && "batchResult" in error
              ? (error as { batchResult?: unknown }).batchResult
              : null;
          if (
            !candidate ||
            typeof candidate !== "object" ||
            !Array.isArray((candidate as { results?: unknown }).results)
          ) {
            toast.error(
              error instanceof Error
                ? error.message
                : "画像のアップロードに失敗しました",
            );
            return null;
          }
          batchResult = candidate as ExplorerUploadBatchResult;
        }
      }

      const references = batchResult.results
        .map((result) => {
          if (!result || typeof result !== "object") return null;
          const payload = result as Record<string, unknown>;
          if (payload.success === false) return null;

          // Prefer the persisted path's basename (the server's final target)
          // and keep `name` as a fallback for project/remote implementations
          // that omit the path. Always reduce it to a portable basename.
          const nameValue =
            typeof payload.path === "string"
              ? payload.path
              : typeof payload.name === "string"
                ? payload.name
                : "";
          const normalizedName = nameValue.replace(/\\/g, "/");
          const name = normalizedName.slice(normalizedName.lastIndexOf("/") + 1);
          if (!name) return null;
          return markdownImageReference(name);
        })
        .filter((reference): reference is string => Boolean(reference));

      return references.length > 0 ? references.join("\n") : null;
    },
    [],
  );

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
          toast.error(result.message || "ファイルの保存に失敗しました");
          return;
        }
      }
      setIsDirty(false);
      contentRef.current = content;
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "ファイルの保存に失敗しました",
      );
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
      ...baseTextEditorExtensions({
        language: extension,
        includeSearch: true,
        imageInsertHandler: handleImageFiles,
        imageInsertEnabled: () => isMarkdownImageExtension(extensionRef.current),
        imageInsertOwnerKey: () => fileIdentityRef.current,
        imageInsertReadOnly: () => readOnlyRef.current,
      }),
      createMarkdownImagePreviewExtension({
        filePathRef,
        enabledRef: markdownImagePreviewEnabledRef,
      }),
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
        createLinkEmbedPlugin(editorLinkDefaultDisplayMode, {
          displayModes: linkDisplayModes,
          onDisplayModeChange: onLinkDisplayModeChange,
          readOnly,
        }),
      ),
      // Theme
      themeCompartmentRef.current.of(resolvedTheme === "dark" ? oneDark : []),
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
    if (autoFocus) view.focus();

    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // CodeMirror's oneDark extension sets a dark editor surface explicitly. Keep
  // it in a compartment so switching the app theme updates an already-open
  // Files editor without recreating the editor (and losing unsaved content).
  useEffect(() => {
    if (!viewRef.current) return;
    viewRef.current.dispatch({
      effects: themeCompartmentRef.current.reconfigure(
        resolvedTheme === "dark" ? oneDark : [],
      ),
    });
  }, [resolvedTheme]);

  useEffect(() => {
    if (!viewRef.current) return;
    viewRef.current.dispatch({
      effects: updateLinkEmbedConfigEffect.of({
        defaultDisplayMode: editorLinkDefaultDisplayMode,
        displayModes: linkDisplayModes,
        onDisplayModeChange: onLinkDisplayModeChange,
        readOnly,
      }),
    });
  }, [
    editorLinkDefaultDisplayMode,
    linkDisplayModes,
    onLinkDisplayModeChange,
    readOnly,
  ]);

  // Update content when a new file is loaded (initialContent prop changes)
  // NOTE: isDirty is intentionally NOT in deps — triggering on isDirty=false after
  // save would overwrite the editor with stale initialContent, making text disappear.
  useEffect(() => {
    if (!viewRef.current) return;
    const currentContent = viewRef.current.state.doc.toString();
    if (currentContent !== initialContent) {
      fileIdentityRef.current += 1;
      viewRef.current.dispatch({
        changes: {
          from: 0,
          to: currentContent.length,
          insert: initialContent,
        },
      });
      contentRef.current = initialContent;
      setIsDirty(false);
      setPreviewContent(initialContent);
    }
  }, [initialContent]);

  const fileName = filePath.split("/").pop() || filePath;
  const langLabel =
    extension.toLowerCase() === ".md" || extension.toLowerCase() === ".markdown"
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
    <div className="flex h-full min-h-0 flex-col bg-background text-foreground">
      {/* Toolbar */}
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-background px-4">
        <div className="flex min-w-0 items-center gap-2 text-[13px]">
          <span className="truncate text-muted-foreground">Files</span>
          <span className="text-muted-foreground/60">›</span>
          <span
            className="max-w-[300px] truncate font-semibold"
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
        <div className="flex items-center gap-1 text-muted-foreground">
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
            className="size-7 text-muted-foreground hover:bg-muted hover:text-foreground"
            title="閉じる"
          >
            <X className="size-3.5" />
          </Button>
        </div>
      </div>

      <div className="flex h-10 shrink-0 items-stretch overflow-x-auto border-b border-border bg-card/50">
        <div className="flex min-w-[170px] items-center gap-2 border-r border-border border-t-2 border-t-primary bg-background px-4 text-[12px] font-medium">
          <FileCode2 className="size-3.5 text-primary" />
          <span className="truncate">{fileName}</span>
          {isDirty && <span className="size-1.5 rounded-full bg-amber-400" title="未保存" />}
        </div>
      </div>

      {/* Editor / Preview */}
      {previewMode && isMarkdown ? (
        <div className="min-h-0 flex-1 overflow-auto bg-background p-6">
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
                img: ({ src, alt }) => {
                  const resolvedSrc = resolveRelativeMarkdownImage(
                    typeof src === "string" ? src : "",
                    filePath,
                  );
                  return (
                    <img
                      src={resolvedSrc}
                      alt={alt}
                      className="my-3 max-w-full rounded-lg"
                    />
                  );
                },
              }}
            >
              {previewContent}
            </ReactMarkdown>
          </div>
        </div>
      ) : (
        <div ref={editorRef} className="min-h-0 flex-1 overflow-hidden bg-background" />
      )}

      {/* Status Bar */}
      <div className="flex h-6 shrink-0 items-center justify-between border-t border-border bg-card/70 px-3 text-[11px] text-muted-foreground">
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

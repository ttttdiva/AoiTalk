import { type ChangeDesc, type Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import {
  getImageFiles,
  normalizeImageFiles,
  type EditorImageInsertSource,
} from "@/lib/editor-image-files";

/** The one upload/markdown callback shared by paste and native file drops. */
export type EditorImageInsertHandler = (
  files: File[],
  source: EditorImageInsertSource,
) => Promise<string | null> | string | null;

export type EditorImageInsertExtensionOptions = {
  handler?: EditorImageInsertHandler;
  /** Alias accepted by callers that pass the option through editor props. */
  imageInsertHandler?: EditorImageInsertHandler;
  /** Disable the pipeline for non-markdown documents without rebuilding view. */
  enabled?: boolean | (() => boolean);
  /** Keep an async insertion tied to the editor/document that started it. */
  active?: boolean | (() => boolean);
  /** Capture an owner token so a reused view cannot insert into a new document. */
  ownerKey?: () => unknown;
  /** A function keeps a long-lived EditorView in sync with a changing prop. */
  readOnly?: boolean | (() => boolean);
};

type PendingInsertion = {
  from: number;
  to: number;
  ownerKey?: unknown;
};

/**
 * Read image files from clipboard data without consuming text-only pastes.
 * `ClipboardData.files` is preferred because it preserves browser ordering;
 * Firefox may expose only file items, so the item fallback is retained.
 */
export function getClipboardImageFiles(data: DataTransfer | null | undefined): File[] {
  if (!data) return [];
  const files = Array.from(data.files ?? []);
  const imageFiles = getImageFiles(files);
  if (imageFiles.length > 0) return imageFiles;

  const itemFiles: File[] = [];
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind !== "file") continue;
    try {
      const file = item.getAsFile();
      if (file) itemFiles.push(file);
    } catch {
      // A browser may revoke a clipboard item between the event and getAsFile.
    }
  }
  return getImageFiles(itemFiles);
}

/** Read and filter native drop files while retaining their original order. */
export function getDroppedImageFiles(data: DataTransfer | null | undefined): File[] {
  if (!data) return [];
  // Deliberately do not inspect DataTransferItem entries here. Native drops
  // are governed by the FileList contract; text/URL drops must be delegated.
  return getImageFiles(Array.from(data.files ?? []));
}

function optionHandler(
  options: EditorImageInsertExtensionOptions,
): EditorImageInsertHandler | undefined {
  return options.handler ?? options.imageInsertHandler;
}

function isReadOnly(
  view: EditorView,
  readOnly: EditorImageInsertExtensionOptions["readOnly"],
): boolean {
  if (typeof readOnly === "function") {
    try {
      if (readOnly()) return true;
    } catch {
      // A disappearing owner should fail closed rather than start an upload.
      return true;
    }
  } else if (readOnly === true) {
    return true;
  }
  // EditorState.readOnly is authoritative when a Compartment is reconfigured
  // after this extension was created.
  return view.state.readOnly;
}

function isEnabled(enabled: EditorImageInsertExtensionOptions["enabled"]): boolean {
  if (typeof enabled === "function") {
    try {
      return enabled();
    } catch {
      return false;
    }
  }
  return enabled ?? true;
}

function isActive(active: EditorImageInsertExtensionOptions["active"]): boolean {
  if (typeof active === "function") {
    try {
      return active();
    } catch {
      return false;
    }
  }
  return active ?? true;
}

function dataTransferMayContainFiles(data: DataTransfer | null | undefined): boolean {
  if (!data) return false;
  const types = Array.from(data.types ?? []).map((type) => type.toLowerCase());
  return types.includes("files") || Array.from(data.files ?? []).length > 0;
}

function setDropEffect(data: DataTransfer | null | undefined, effect: "copy" | "none"): void {
  if (!data) return;
  try {
    data.dropEffect = effect;
  } catch {
    // Some test/browser DataTransfer implementations expose a read-only field.
  }
}

function preventAndStop(event: Event): void {
  event.preventDefault();
  event.stopPropagation();
}

/**
 * CodeMirror 6 extension that funnels image clipboard paste and native image
 * drops through one asynchronous handler. Non-image events return `false`
 * from the DOM handlers, preserving CodeMirror/browser default semantics.
 */
export function createEditorImageInsertExtension(
  input: EditorImageInsertExtensionOptions | EditorImageInsertHandler = {},
): Extension {
  const options: EditorImageInsertExtensionOptions = typeof input === "function"
    ? { handler: input }
    : input;
  const handler = optionHandler(options);
  if (!handler) return [];

  const pending = new Set<PendingInsertion>();

  const mapPendingPositions = (changes: ChangeDesc): void => {
    for (const insertion of pending) {
      insertion.from = changes.mapPos(insertion.from, 1);
      insertion.to = changes.mapPos(insertion.to, -1);
      if (insertion.to < insertion.from) insertion.to = insertion.from;
    }
  };

  const beginInsert = (
    view: EditorView,
    files: File[],
    source: EditorImageInsertSource,
    from: number,
    to: number,
  ): void => {
    const insertion: PendingInsertion = {
      from,
      to,
      ownerKey: options.ownerKey?.(),
    };
    pending.add(insertion);
    void Promise.resolve()
      .then(() => handler(normalizeImageFiles(files, source), source))
      .then((markdown) => {
        pending.delete(insertion);
        if (
          markdown == null ||
          markdown.length === 0 ||
          !isActive(options.active) ||
          !isEnabled(options.enabled) ||
          isReadOnly(view, options.readOnly) ||
          (options.ownerKey && options.ownerKey() !== insertion.ownerKey)
        ) return;
        // The view can be destroyed while a slow upload is in flight. Reading
        // state in a try/catch keeps this extension harmless during teardown.
        try {
          const currentLength = view.state.doc.length;
          const mappedFrom = Math.max(0, Math.min(insertion.from, currentLength));
          const mappedTo = Math.max(mappedFrom, Math.min(insertion.to, currentLength));
          view.dispatch({
            changes: { from: mappedFrom, to: mappedTo, insert: markdown },
            selection: { anchor: mappedFrom + markdown.length },
          });
        } catch {
          // EditorView.destroy() races are expected for unmounted editors.
        }
      })
      .catch(() => {
        pending.delete(insertion);
        // Upload/handler errors belong to the owning surface (which can show a
        // toast). The shared extension must not create an unhandled rejection.
      });
  };

  const imageDragIntent = (event: DragEvent, view: EditorView): boolean => {
    const data = event.dataTransfer;
    if (!isEnabled(options.enabled) || isReadOnly(view, options.readOnly)) {
      setDropEffect(data, "none");
      return false;
    }
    const files = getDroppedImageFiles(data);
    const hasImage = files.length > 0;
    const mayHaveFiles = dataTransferMayContainFiles(data);
    // During dragover some browsers hide FileList contents. Allow the native
    // drop to proceed for a Files payload, but only consume it once drop gives
    // us an actual image file to inspect.
    setDropEffect(data, hasImage || (files.length === 0 && mayHaveFiles) ? "copy" : "none");
    if (!hasImage) return false;
    event.preventDefault();
    event.stopPropagation();
    return true;
  };

  const domHandlers = EditorView.domEventHandlers({
    paste(event, view) {
      if (!isEnabled(options.enabled) || isReadOnly(view, options.readOnly)) return false;
      const files = getClipboardImageFiles(event.clipboardData);
      if (files.length === 0) return false;
      preventAndStop(event);
      const selection = view.state.selection.main;
      beginInsert(view, files, "paste", selection.from, selection.to);
      return true;
    },
    dragenter(event, view) {
      return imageDragIntent(event, view);
    },
    dragover(event, view) {
      return imageDragIntent(event, view);
    },
    drop(event, view) {
      if (!isEnabled(options.enabled) || isReadOnly(view, options.readOnly)) {
        setDropEffect(event.dataTransfer, "none");
        return false;
      }
      const files = getDroppedImageFiles(event.dataTransfer);
      if (files.length === 0) {
        setDropEffect(event.dataTransfer, "none");
        return false;
      }
      preventAndStop(event);
      setDropEffect(event.dataTransfer, "copy");
      let coords: number | null = null;
      try {
        coords = view.posAtCoords({ x: event.clientX, y: event.clientY });
      } catch {
        // jsdom and a few browser drag payloads do not provide usable coords.
      }
      const position = typeof coords === "number" ? coords : view.state.selection.main.head;
      beginInsert(view, files, "drop", position, position);
      return true;
    },
  });

  return [
    EditorView.updateListener.of((update) => {
      if (update.docChanged) mapPendingPositions(update.changes);
    }),
    domHandlers,
  ];
}

/** Short alias for call sites that already use the image-insert terminology. */
export const createImageInsertExtension = createEditorImageInsertExtension;

// Keep the type available from this module for downstream editor props.
export type { EditorImageInsertSource } from "@/lib/editor-image-files";

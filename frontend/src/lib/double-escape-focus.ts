/**
 * DOM helpers for the Files editor double-Escape boundary.
 *
 * Focus can fall back to <body> without moving to another control: a focused
 * Save button becomes disabled, the Markdown preview hides the editor, a menu
 * unmounts, or the user clicks a non-focusable spot.  Keys then target
 * <body>, which the Files-scoped boundary used to ignore, so Esc×2 worked or
 * not depending on what was clicked last.  These helpers let the boundary
 * accept such orphaned focus only while Files was the last owner.
 */

export function isOrphanedFocusTarget(
  target: EventTarget | null,
  doc: Document = document,
): boolean {
  const active = doc.activeElement;
  if (active && active !== doc.body && active !== doc.documentElement) {
    return false;
  }
  return (
    target === null ||
    target === doc ||
    target === doc.body ||
    target === doc.documentElement
  );
}

/**
 * Track which surface last received focus or a pointer press.  A press on
 * <body>/<html> itself keeps the previous owner; any other target decides by
 * Files membership (so clicking the header or nav rail releases ownership).
 */
export function nextOrphanedFocusOwner(
  previous: boolean,
  target: EventTarget | null,
  isFilesTarget: (target: EventTarget | null) => boolean,
  doc: Document = document,
): boolean {
  if (target === doc.body || target === doc.documentElement || target === doc) {
    return previous;
  }
  return isFilesTarget(target);
}

/** Root marker rendered by DocumentEditor. */
export const DOCUMENT_EDITOR_SELECTOR = "[data-document-editor]";

/**
 * CodeMirror transient surfaces (completion popup, search/goto panels) own
 * their Escape: it closes that surface, so it must neither arm nor complete
 * the editor-close sequence.  The popup check is scoped to the given editor
 * pane so an unrelated CodeMirror elsewhere in the shell cannot block Files.
 */
export function isEditorTransientEscapeSurface(
  target: EventTarget | null,
  editorPane: ParentNode | null,
): boolean {
  if (editorPane?.querySelector(".cm-tooltip-autocomplete")) return true;
  return (
    typeof Element !== "undefined" &&
    target instanceof Element &&
    target.closest(".cm-panels") !== null
  );
}

export type EditorEditState = { dirty: boolean; saveFailed: boolean };

export const CLEAN_EDITOR_EDIT_STATE: EditorEditState = {
  dirty: false,
  saveFailed: false,
};

export const DIRTY_ESCAPE_NOTICE =
  "未保存の変更があります（保存するか×で閉じてください）";

/** True when the Escape came from the visible CodeMirror editing surface. */
export function isEditorBodyEscapeTarget(target: EventTarget | null): boolean {
  if (typeof Element === "undefined" || !(target instanceof Element)) {
    return false;
  }
  const editor = target.closest(".cm-editor");
  if (!editor || !editor.closest(DOCUMENT_EDITOR_SELECTOR)) return false;
  return editor.closest("[hidden]") === null;
}

/**
 * Never lose unsaved edits through a path that did not close before: with a
 * dirty document, Esc×2 closes only from the editing surface itself (the
 * long-standing behavior), and never right after a failed save.  Every other
 * origin (orphaned <body> focus, the pane while the Markdown preview hides the
 * editor, toolbar, file list, sidebar, restored Files root) is held back.
 */
export function shouldHoldDirtyEditorClose(
  state: EditorEditState,
  target: EventTarget | null,
): boolean {
  if (!state.dirty) return false;
  if (state.saveFailed) return true;
  return !isEditorBodyEscapeTarget(target);
}

/** Return focus to the editor (or its pane while the preview hides it). */
export function focusDocumentEditorSurface(editorPane: Element | null): void {
  if (!editorPane) return;
  const content = editorPane.querySelector<HTMLElement>(".cm-content");
  if (content && content.closest("[hidden]") === null) {
    content.focus({ preventScroll: true });
    return;
  }
  (editorPane as HTMLElement).focus?.({ preventScroll: true });
}

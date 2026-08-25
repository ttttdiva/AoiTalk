const ARCHIVE_KEYBOARD_KEYS = new Set(["Backspace", "Delete"]);

const NON_ARCHIVE_EDITABLE_SELECTOR =
  "input, textarea, select, [data-docs-field-control], [data-docs-supertag-chip], [data-docs-attachment-control]";

const OUTLINE_ARCHIVE_TARGET_SELECTOR =
  "[data-docs-node-id], [data-docs-block-id], [data-docs-sidebar-node-id]";

export function shouldArchiveSelectionFromKeyboard({
  key,
  ctrlKey,
  altKey,
  metaKey,
  selectedCount,
  target,
  hasNonCollapsedTextSelection = false,
}: {
  key: string;
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  selectedCount: number;
  target: Element | null;
  hasNonCollapsedTextSelection?: boolean;
}): boolean {
  if (selectedCount <= 1) return false;
  if (!ARCHIVE_KEYBOARD_KEYS.has(key)) return false;
  if (ctrlKey || altKey || metaKey) return false;
  if (!target) return false;
  if (hasNonCollapsedTextSelection) return false;

  if (target.closest(NON_ARCHIVE_EDITABLE_SELECTOR)) return false;
  if (target.closest('[role="dialog"]')) return false;
  if (target.closest("[cmdk-input]")) return false;

  const contentEditable = target.closest('[contenteditable="true"]');
  if (contentEditable && !contentEditable.closest(".cm-editor")) return false;

  if (target.closest(OUTLINE_ARCHIVE_TARGET_SELECTOR)) return true;

  return false;
}

export function expandArchivedNodeIds(
  rootIds: readonly string[],
  parentIdByNodeId: ReadonlyMap<string, string | null>,
): Set<string> {
  const expanded = new Set(rootIds);
  const childrenByParent = new Map<string, string[]>();

  for (const [nodeId, parentId] of parentIdByNodeId) {
    if (!parentId) continue;
    const siblings = childrenByParent.get(parentId) ?? [];
    siblings.push(nodeId);
    childrenByParent.set(parentId, siblings);
  }

  const queue = [...rootIds];
  while (queue.length > 0) {
    const nodeId = queue.pop();
    if (!nodeId) continue;
    for (const childId of childrenByParent.get(nodeId) ?? []) {
      if (expanded.has(childId)) continue;
      expanded.add(childId);
      queue.push(childId);
    }
  }

  return expanded;
}

export function resolveActionNodeIds(selectedIds: readonly string[], fallbackId: string | null): string[] {
  if (selectedIds.length > 1 && fallbackId && selectedIds.includes(fallbackId)) {
    return [...selectedIds];
  }
  if (fallbackId) return [fallbackId];
  return selectedIds.length > 0 ? [...selectedIds] : [];
}

export function resolveArchiveTargets(
  selectedIds: readonly string[],
  parentIdByNodeId: ReadonlyMap<string, string | null>,
): string[] {
  const selectedSet = new Set(selectedIds);
  return selectedIds.filter((nodeId) => {
    let parentId = parentIdByNodeId.get(nodeId) ?? null;
    while (parentId) {
      if (selectedSet.has(parentId)) return false;
      parentId = parentIdByNodeId.get(parentId) ?? null;
    }
    return true;
  });
}

export function resolveFocusAfterArchive({
  removedIds,
  visibleRowIds,
  currentFocusId,
  parentIdByNodeId,
}: {
  removedIds: ReadonlySet<string>;
  visibleRowIds: readonly string[];
  currentFocusId: string | null;
  parentIdByNodeId: ReadonlyMap<string, string | null>;
}): string | null {
  const focusIndex = currentFocusId ? visibleRowIds.indexOf(currentFocusId) : -1;

  if (focusIndex >= 0) {
    for (let index = focusIndex - 1; index >= 0; index -= 1) {
      const nodeId = visibleRowIds[index];
      if (!removedIds.has(nodeId)) return nodeId;
    }
    for (let index = focusIndex + 1; index < visibleRowIds.length; index += 1) {
      const nodeId = visibleRowIds[index];
      if (!removedIds.has(nodeId)) return nodeId;
    }
  } else if (currentFocusId && !removedIds.has(currentFocusId)) {
    return currentFocusId;
  }

  const startId = currentFocusId ?? visibleRowIds.find((nodeId) => removedIds.has(nodeId)) ?? null;
  if (startId) {
    let parentId = parentIdByNodeId.get(startId) ?? null;
    while (parentId) {
      if (!removedIds.has(parentId)) return parentId;
      parentId = parentIdByNodeId.get(parentId) ?? null;
    }
  }

  return visibleRowIds.find((nodeId) => !removedIds.has(nodeId)) ?? null;
}

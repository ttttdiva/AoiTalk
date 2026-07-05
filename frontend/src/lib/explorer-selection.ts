interface ExplorerRangeSelectionInput {
  orderedPaths: string[];
  anchorPath: string | null;
  targetPath: string;
  selectedPaths: Iterable<string>;
  previousShiftRange: Iterable<string>;
  additive?: boolean;
}

interface ExplorerRangeSelectionResult {
  selectedPaths: Set<string>;
  shiftRange: Set<string>;
  anchorPath: string;
}

export function buildExplorerRangeSelection({
  orderedPaths,
  anchorPath,
  targetPath,
  selectedPaths,
  previousShiftRange,
  additive = false,
}: ExplorerRangeSelectionInput): ExplorerRangeSelectionResult {
  const targetIndex = orderedPaths.indexOf(targetPath);
  const anchorIndex = anchorPath ? orderedPaths.indexOf(anchorPath) : -1;

  if (targetIndex < 0 || anchorIndex < 0) {
    return {
      selectedPaths: new Set([targetPath]),
      shiftRange: new Set(),
      anchorPath: targetPath,
    };
  }

  const start = Math.min(anchorIndex, targetIndex);
  const end = Math.max(anchorIndex, targetIndex);
  const shiftRange = new Set(orderedPaths.slice(start, end + 1));
  const nextSelected = new Set(selectedPaths);

  if (!additive) {
    for (const path of previousShiftRange) {
      nextSelected.delete(path);
    }
  }

  for (const path of shiftRange) {
    nextSelected.add(path);
  }

  return {
    selectedPaths: nextSelected,
    shiftRange,
    anchorPath: anchorPath ?? targetPath,
  };
}

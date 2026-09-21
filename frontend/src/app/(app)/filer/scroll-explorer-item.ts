/** Scroll virtual-focus items without changing selection or DOM focus. */
export function scrollExplorerItemIntoView(
  root: HTMLElement | null,
  path: string,
): boolean {
  if (!root || !path) return false;
  const element = Array.from(
    root.querySelectorAll<HTMLElement>("[data-explorer-item-path]"),
  ).find((item) => item.dataset.explorerItemPath === path);
  if (!element) return false;
  element.scrollIntoView({ block: "nearest", inline: "nearest" });
  return true;
}

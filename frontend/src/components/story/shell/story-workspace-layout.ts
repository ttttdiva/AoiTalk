/** Story workspace shell の flex レイアウト契約（DOM 構造テスト用）。 */

export const STORY_WORKSPACE_SHELL_OUTER_CLASS =
  "relative flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-background";

export const STORY_WORKSPACE_SHELL_HEADER_CLASS =
  "flex min-h-11 shrink-0 flex-wrap items-center gap-3 border-b border-border-subtle bg-surface-container px-4 py-2";

export const STORY_WORKSPACE_SHELL_CONTENT_CLASS = "min-h-0 min-w-0 flex-1 overflow-auto";

export function storyWorkspaceShellBusyModifier(writingBusy: boolean): string {
  return writingBusy ? "pointer-events-none select-none" : "";
}

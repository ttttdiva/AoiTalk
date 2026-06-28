export type ChatComposerShortcutAction =
  | "project_context"
  | "generation_profile_menu"
  | "llm_mode_menu"
  | "tools_menu"
  | "web_search";

export type KeyboardShortcutEvent = Pick<
  KeyboardEvent,
  "altKey" | "ctrlKey" | "key" | "metaKey" | "shiftKey"
>;

export type ShortcutHelpItem = {
  keys: string[];
  description: string;
};

export const CHAT_SHORTCUT_HELP_ITEMS: ShortcutHelpItem[] = [
  { keys: ["Ctrl", "Shift", "O"], description: "新規チャットを開始" },
  { keys: ["Ctrl", "J"], description: "チャット入力欄にフォーカス" },
  { keys: ["Ctrl", "F"], description: "会話検索を開く" },
  { keys: ["Ctrl", "M"], description: "動作モードメニューを開く" },
  { keys: ["Ctrl", "."], description: "ツールメニューを開く" },
  {
    keys: ["Ctrl", "Shift", "M"],
    description: "LLM mode/effortメニューを開く",
  },
  {
    keys: ["Ctrl", "Shift", "P"],
    description: "Project contextを切り替え",
  },
  {
    keys: ["Ctrl", "Shift", "F"],
    description: "Web検索を切り替え",
  },
];

export function getChatComposerShortcutAction(
  event: KeyboardShortcutEvent,
): ChatComposerShortcutAction | null {
  const key = event.key.toLowerCase();

  if (
    event.ctrlKey &&
    event.shiftKey &&
    !event.altKey &&
    !event.metaKey &&
    key === "p"
  ) {
    return "project_context";
  }

  // Legacy shortcut kept for users who already learned it.
  if (
    event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.shiftKey &&
    key === "p"
  ) {
    return "project_context";
  }

  if (
    event.ctrlKey &&
    event.shiftKey &&
    !event.altKey &&
    !event.metaKey &&
    key === "m"
  ) {
    return "llm_mode_menu";
  }

  if (
    event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey &&
    !event.metaKey &&
    key === "m"
  ) {
    return "generation_profile_menu";
  }

  if (
    event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey &&
    !event.metaKey &&
    key === "."
  ) {
    return "tools_menu";
  }

  if (
    event.ctrlKey &&
    event.shiftKey &&
    !event.altKey &&
    !event.metaKey &&
    key === "f"
  ) {
    return "web_search";
  }

  return null;
}

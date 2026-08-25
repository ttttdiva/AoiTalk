import type { StoryAssistSelection } from "@/components/story/assist/types";

export type LongTextEditorSelection = {
  from: number;
  to: number;
  text: string;
};

/** LongTextEditor の選択範囲を Story Assist 用に変換する。 */
export function toAssistSelection(
  selection: LongTextEditorSelection | null,
): StoryAssistSelection | null {
  if (!selection) return null;
  return { start: selection.from, end: selection.to, text: selection.text };
}

import type { StoryAssistSelection } from "@/components/story/assist/types";

/** 選択範囲を提案テキストで置換した全文を返す。 */
export function applySelectionReplacement(
  currentText: string,
  selection: StoryAssistSelection,
  proposal: string,
): string {
  const start = Math.max(0, Math.min(selection.start, currentText.length));
  const end = Math.max(start, Math.min(selection.end, currentText.length));
  return `${currentText.slice(0, start)}${proposal}${currentText.slice(end)}`;
}

/** 選択があれば範囲置換、なければ提案でフィールド全体を置換。 */
export function applyStoryAssistProposal(
  currentText: string,
  proposal: string,
  selection: StoryAssistSelection | null,
): string {
  if (selection && selection.start < selection.end) {
    return applySelectionReplacement(currentText, selection, proposal);
  }
  return proposal;
}

/** diff プレビュー用の比較テキスト（選択時は範囲のみ）。 */
export function storyAssistPreviewOldText(
  currentText: string,
  selection: StoryAssistSelection | null,
): string {
  if (selection && selection.start < selection.end) {
    return currentText.slice(selection.start, selection.end);
  }
  return currentText;
}

/** diff プレビュー用の比較テキスト（選択時は proposal スニペット、未選択時は全文 proposal）。 */
export function storyAssistPreviewNewText(
  _currentText: string,
  proposal: string,
  _selection: StoryAssistSelection | null,
): string {
  return proposal;
}

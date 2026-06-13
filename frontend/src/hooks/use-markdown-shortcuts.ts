import { useEffect, type RefObject } from "react";

/**
 * textarea に Markdown 見出しショートカットを追加するフック
 * - Ctrl+Shift+] : 見出しレベルを上げる (プレーンテキスト→#→##→...→######)
 * - Ctrl+Shift+[ : 見出しレベルを下げる (######→...→#→プレーンテキスト)
 */
export function useMarkdownShortcuts(
  ref: RefObject<HTMLTextAreaElement | null>,
) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    function handleKeyDown(e: KeyboardEvent) {
      if (!el) return;
      // e.code を使うことでキーボードレイアウトに依存しない検出
      const isIncrease = e.ctrlKey && e.shiftKey && e.code === "BracketRight";
      const isDecrease = e.ctrlKey && e.shiftKey && e.code === "BracketLeft";
      if (!isIncrease && !isDecrease) return;

      e.preventDefault();
      const { selectionStart, value } = el;

      // カーソルがある行の開始・終了位置
      const lineStart = value.lastIndexOf("\n", selectionStart - 1) + 1;
      const lineEndIdx = value.indexOf("\n", selectionStart);
      const lineEnd = lineEndIdx === -1 ? value.length : lineEndIdx;
      const lineText = value.slice(lineStart, lineEnd);

      const match = lineText.match(/^(#{0,6})\s*/);
      const currentLevel = match ? match[1].length : 0;

      let newLine: string;
      if (isIncrease) {
        if (currentLevel >= 6) return;
        const rest = lineText.replace(/^#{0,6}\s*/, "");
        newLine = "#".repeat(currentLevel + 1) + " " + rest;
      } else {
        if (currentLevel === 0) return;
        const rest = lineText.replace(/^#{1,6}\s*/, "");
        newLine =
          currentLevel <= 1 ? rest : "#".repeat(currentLevel - 1) + " " + rest;
      }

      const newValue =
        value.slice(0, lineStart) + newLine + value.slice(lineEnd);

      // React の onChange をトリガーするため native setter 経由で更新
      const nativeSet = Object.getOwnPropertyDescriptor(
        HTMLTextAreaElement.prototype,
        "value",
      )!.set!;
      nativeSet.call(el, newValue);
      el.dispatchEvent(new Event("input", { bubbles: true }));

      // カーソル位置を調整
      const diff = newLine.length - lineText.length;
      const pos = Math.max(lineStart, selectionStart + diff);
      el.setSelectionRange(pos, pos);
    }

    el.addEventListener("keydown", handleKeyDown);
    return () => el.removeEventListener("keydown", handleKeyDown);
  }, [ref]);
}

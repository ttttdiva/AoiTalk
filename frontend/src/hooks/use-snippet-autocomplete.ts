import {
  useState,
  useCallback,
  useEffect,
  useRef,
  type RefObject,
} from "react";
import type { Snippet } from "@/lib/snippets-api";

export interface SnippetAutocompleteState {
  visible: boolean;
  matches: Snippet[];
  selectedIndex: number;
  position: { top: number; left: number };
}

const INITIAL_STATE: SnippetAutocompleteState = {
  visible: false,
  matches: [],
  selectedIndex: 0,
  position: { top: 0, left: 0 },
};

/**
 * textarea に対して snippet 補完を提供するフック。
 * 返値の `state` をもとにドロップダウンを描画し、
 * Tab で確定・Escape で閉じる。
 */
export function useSnippetAutocomplete(
  ref: RefObject<HTMLTextAreaElement | null>,
  snippets: Snippet[],
) {
  const [state, setState] = useState<SnippetAutocompleteState>(INITIAL_STATE);
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  // 現在のカーソル直前の "単語" を取得
  const getCurrentWord = useCallback(() => {
    const el = ref.current;
    if (!el) return { word: "", from: 0 };
    const { selectionStart, value } = el;
    let start = selectionStart;
    while (start > 0 && /\S/.test(value[start - 1])) start--;
    return { word: value.slice(start, selectionStart), from: start };
  }, [ref]);

  // カーソル位置の計算（簡易版: textarea の位置からオフセット）
  const calcPosition = useCallback(() => {
    const el = ref.current;
    if (!el) return { top: 0, left: 0 };
    const rect = el.getBoundingClientRect();
    const { selectionStart, value } = el;
    // 行番号を数えてざっくり位置推定
    const textBefore = value.slice(0, selectionStart);
    const lines = textBefore.split("\n");
    const lineHeight = parseFloat(getComputedStyle(el).lineHeight) || 20;
    const scrollTop = el.scrollTop;
    return {
      top: rect.top + (lines.length * lineHeight - scrollTop) + 4,
      left: rect.left + 16,
    };
  }, [ref]);

  const updateMatches = useCallback(() => {
    if (snippets.length === 0) {
      setState(INITIAL_STATE);
      return;
    }
    const { word } = getCurrentWord();
    if (!word) {
      setState(INITIAL_STATE);
      return;
    }
    const matches = snippets.filter((s) => s.prefix.startsWith(word));
    if (matches.length === 0) {
      setState(INITIAL_STATE);
      return;
    }
    setState({
      visible: true,
      matches,
      selectedIndex: 0,
      position: calcPosition(),
    });
  }, [snippets, getCurrentWord, calcPosition]);

  // input イベントで候補更新
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const handler = () => updateMatches();
    el.addEventListener("input", handler);
    return () => el.removeEventListener("input", handler);
  }, [ref, updateMatches]);

  // キー操作
  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    function handleKeyDown(e: KeyboardEvent) {
      const s = stateRef.current;
      if (!s.visible) return;

      if (e.key === "Tab" || (e.key === "Enter" && s.visible)) {
        e.preventDefault();
        const snippet = s.matches[s.selectedIndex];
        if (!snippet || !el) return;

        const { from } = getCurrentWord();
        const { selectionStart, value } = el;
        const newValue =
          value.slice(0, from) + snippet.body + value.slice(selectionStart);

        const nativeSet = Object.getOwnPropertyDescriptor(
          HTMLTextAreaElement.prototype,
          "value",
        )!.set!;
        nativeSet.call(el, newValue);
        el.dispatchEvent(new Event("input", { bubbles: true }));

        const newPos = from + snippet.body.length;
        el.setSelectionRange(newPos, newPos);
        setState(INITIAL_STATE);
        return;
      }

      if (e.key === "Escape") {
        e.preventDefault();
        setState(INITIAL_STATE);
        return;
      }

      if (e.key === "ArrowDown") {
        e.preventDefault();
        setState((prev) => ({
          ...prev,
          selectedIndex: (prev.selectedIndex + 1) % prev.matches.length,
        }));
        return;
      }

      if (e.key === "ArrowUp") {
        e.preventDefault();
        setState((prev) => ({
          ...prev,
          selectedIndex:
            (prev.selectedIndex - 1 + prev.matches.length) %
            prev.matches.length,
        }));
        return;
      }
    }

    el.addEventListener("keydown", handleKeyDown);
    return () => el.removeEventListener("keydown", handleKeyDown);
  }, [ref, getCurrentWord]);

  // blur で閉じる
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const handler = () => setState(INITIAL_STATE);
    el.addEventListener("blur", handler);
    return () => el.removeEventListener("blur", handler);
  }, [ref]);

  const dismiss = useCallback(() => setState(INITIAL_STATE), []);

  return { state, dismiss };
}

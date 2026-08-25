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

export type SnippetAutocompleteOptions = {
  /**
   * 候補表示・確定キーを処理してよい入力かを判定する。
   * 省略時は従来どおり常に処理する。
   */
  shouldHandle?: (element: HTMLTextAreaElement) => boolean;
};

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
  options?: SnippetAutocompleteOptions,
) {
  const shouldHandle = options?.shouldHandle;
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
    const el = ref.current;
    if (shouldHandle && (!el || !shouldHandle(el))) {
      setState(INITIAL_STATE);
      return;
    }
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
  }, [snippets, getCurrentWord, calcPosition, ref, shouldHandle]);

  // input イベントで候補更新
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const handler = (event: Event) => {
      if (shouldHandle && !shouldHandle(el)) {
        setState(INITIAL_STATE);
        return;
      }
      if ((event as InputEvent).isComposing) return;
      updateMatches();
    };
    el.addEventListener("input", handler);
    return () => el.removeEventListener("input", handler);
  }, [ref, updateMatches, shouldHandle]);

  // キー操作
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const textarea = element as HTMLTextAreaElement;

    function handleKeyDown(e: KeyboardEvent) {
      // コードブロック等では候補の表示状態も含めて破棄するが、Tab/
      // Enter/Arrow を横取りせず、通常の textarea 操作へ委譲する。
      if (shouldHandle && !shouldHandle(textarea)) {
        setState(INITIAL_STATE);
        return;
      }

      if (e.isComposing || e.keyCode === 229) return;

      const s = stateRef.current;
      if (!s.visible) return;

      if (e.key === "Tab" || (e.key === "Enter" && s.visible)) {
        e.preventDefault();
        const snippet = s.matches[s.selectedIndex];
        if (!snippet) return;

        const { from } = getCurrentWord();
        const { selectionStart, value } = textarea;
        const newValue =
          value.slice(0, from) + snippet.body + value.slice(selectionStart);

        const nativeSet = Object.getOwnPropertyDescriptor(
          HTMLTextAreaElement.prototype,
          "value",
        )!.set!;
        nativeSet.call(textarea, newValue);
        textarea.dispatchEvent(new Event("input", { bubbles: true }));

        const newPos = from + snippet.body.length;
        textarea.setSelectionRange(newPos, newPos);
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

    textarea.addEventListener("keydown", handleKeyDown);
    return () => textarea.removeEventListener("keydown", handleKeyDown);
  }, [ref, getCurrentWord, shouldHandle]);

  // カーソル／選択範囲の移動でコード本文へ入った場合にも、入力イベントを
  // 待たず既存の候補ポップアップを閉じる。通常テキストでは候補を再計算せず、
  // 既存の表示状態を維持する。
  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const closeIfNotHandled = () => {
      if (shouldHandle && !shouldHandle(el)) setState(INITIAL_STATE);
    };
    el.addEventListener("select", closeIfNotHandled);
    el.addEventListener("keyup", closeIfNotHandled);
    el.addEventListener("mouseup", closeIfNotHandled);
    el.addEventListener("focus", closeIfNotHandled);
    closeIfNotHandled();
    return () => {
      el.removeEventListener("select", closeIfNotHandled);
      el.removeEventListener("keyup", closeIfNotHandled);
      el.removeEventListener("mouseup", closeIfNotHandled);
      el.removeEventListener("focus", closeIfNotHandled);
    };
  }, [ref, shouldHandle]);

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

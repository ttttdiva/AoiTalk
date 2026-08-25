import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  type ChangeEvent,
  type ClipboardEventHandler,
  type CompositionEvent,
  type DragEventHandler,
  type FocusEvent,
  type KeyboardEvent,
  type RefCallback,
  type SyntheticEvent,
} from "react";
import { isChatComposerCursorInCodeBlock } from "@/lib/chat-composer-blocks";
import { cn } from "@/lib/utils";

const useIsomorphicLayoutEffect =
  typeof window === "undefined" ? useEffect : useLayoutEffect;

type ChatComposerCursorContextChange = (
  cursor: number,
  isCodeBlock: boolean,
) => void;

type ChatComposerInputProps = {
  value: string;
  placeholder: string;
  onValueChange: (value: string, cursor: number, isCodeBlock: boolean) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onPaste?: ClipboardEventHandler<HTMLTextAreaElement>;
  onInputRef?: (element: HTMLTextAreaElement | null) => void;
  onCursorContextChange?: ChatComposerCursorContextChange;
  onDragOver?: DragEventHandler<HTMLDivElement>;
  onDragLeave?: DragEventHandler<HTMLDivElement>;
  onDrop?: DragEventHandler<HTMLDivElement>;
  isDragOver?: boolean;
};

type CompositionSession = {
  target: HTMLTextAreaElement;
  baseValue: string;
  value: string;
  cursor: number;
  ending: boolean;
};

function clampSelection(
  element: HTMLTextAreaElement,
  start: number,
  end = start,
): void {
  const nextStart = Math.max(0, Math.min(start, element.value.length));
  const nextEnd = Math.max(nextStart, Math.min(end, element.value.length));
  element.setSelectionRange(nextStart, nextEnd);
}

function readSelection(element: HTMLTextAreaElement): {
  start: number;
  end: number;
} {
  return {
    start: element.selectionStart ?? element.value.length,
    end: element.selectionEnd ?? element.value.length,
  };
}

/**
 * The composer intentionally uses an uncontrolled native textarea. The
 * parent still owns the canonical value through `onValueChange`, while the
 * DOM owns selection and IME composition. This avoids React rewriting the
 * textarea value on every draft-store/streaming rerender and preserves a
 * mid-message caret.
 */
export function ChatComposerInput({
  value,
  placeholder,
  onValueChange,
  onKeyDown,
  onPaste,
  onInputRef,
  onCursorContextChange,
  onDragOver,
  onDragLeave,
  onDrop,
  isDragOver = false,
}: ChatComposerInputProps) {
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const latestValueRef = useRef(value);
  const previousPropValueRef = useRef(value);
  const onInputRefRef = useRef(onInputRef);
  const onCursorContextChangeRef =
    useRef<ChatComposerCursorContextChange | undefined>(
      onCursorContextChange,
    );
  const compositionRef = useRef<CompositionSession | null>(null);
  const compositionFlushRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const skipNextChangeRef = useRef<{
    target: HTMLTextAreaElement;
    value: string;
  } | null>(null);

  useEffect(() => {
    onInputRefRef.current = onInputRef;
  }, [onInputRef]);
  useEffect(() => {
    onCursorContextChangeRef.current = onCursorContextChange;
  }, [onCursorContextChange]);

  const emitCursorContext = useCallback(
    (element: HTMLTextAreaElement, cursor = element.selectionStart ?? 0) => {
      const normalizedCursor = Math.max(
        0,
        Math.min(cursor, element.value.length),
      );
      onCursorContextChangeRef.current?.(
        normalizedCursor,
        isChatComposerCursorInCodeBlock(element.value, normalizedCursor),
      );
    },
    [],
  );

  const setInputRef = useCallback<RefCallback<HTMLTextAreaElement>>(
    (element) => {
      inputRef.current = element;
      onInputRefRef.current?.(element);
    },
    [],
  );

  const clearCompositionFlush = useCallback(() => {
    if (compositionFlushRef.current === null) return;
    clearTimeout(compositionFlushRef.current);
    compositionFlushRef.current = null;
  }, []);

  const cancelComposition = useCallback(() => {
    clearCompositionFlush();
    compositionRef.current = null;
    skipNextChangeRef.current = null;
  }, [clearCompositionFlush]);

  const commitComposition = useCallback(
    (element: HTMLTextAreaElement) => {
      const composition = compositionRef.current;
      if (!composition || composition.target !== element) return;

      const latestValue = latestValueRef.current;
      // A controlled external update replaced the value while IME was active.
      // Do not append the old IME buffer to that newer value.
      if (latestValue !== composition.baseValue) {
        cancelComposition();
        return;
      }

      const nextValue = element.value;
      const cursor = element.selectionStart ?? nextValue.length;
      const isCodeBlock = isChatComposerCursorInCodeBlock(nextValue, cursor);

      clearCompositionFlush();
      compositionRef.current = null;
      // Some engines dispatch a duplicate change event after compositionend.
      // Keep a one-event guard scoped to this exact native textarea/value.
      skipNextChangeRef.current = { target: element, value: nextValue };
      if (nextValue !== latestValue) {
        onValueChange(nextValue, cursor, isCodeBlock);
      }
      onCursorContextChangeRef.current?.(cursor, isCodeBlock);
    },
    [cancelComposition, clearCompositionFlush, onValueChange],
  );

  const handleCompositionStart = useCallback(
    (event: CompositionEvent<HTMLTextAreaElement>) => {
      clearCompositionFlush();
      const element = event.currentTarget;
      const selection = readSelection(element);
      compositionRef.current = {
        target: element,
        baseValue: latestValueRef.current,
        value: element.value,
        cursor: selection.start,
        ending: false,
      };
      skipNextChangeRef.current = null;
    },
    [clearCompositionFlush],
  );

  const handleCompositionEnd = useCallback(
    (event: CompositionEvent<HTMLTextAreaElement>) => {
      const composition = compositionRef.current;
      if (!composition || composition.target !== event.currentTarget) return;

      const selection = readSelection(event.currentTarget);
      compositionRef.current = {
        ...composition,
        value: event.currentTarget.value,
        cursor: selection.start,
        ending: true,
      };

      // Most browsers emit a final input/change after compositionend. The
      // zero-delay fallback covers engines that omit that event.
      clearCompositionFlush();
      compositionFlushRef.current = setTimeout(() => {
        compositionFlushRef.current = null;
        const current = compositionRef.current;
        if (!current || !current.ending) return;
        commitComposition(current.target);
      }, 0);
    },
    [clearCompositionFlush, commitComposition],
  );

  const handleChange = useCallback(
    (event: ChangeEvent<HTMLTextAreaElement>) => {
      const element = event.currentTarget;
      if (inputRef.current !== element) return;

      const nativeEvent = event.nativeEvent as Event & {
        isComposing?: boolean;
      };
      const composition = compositionRef.current;
      if (composition?.target === element || nativeEvent.isComposing) {
        const nextSelection = readSelection(element);
        const nextComposition = composition ?? {
          target: element,
          baseValue: latestValueRef.current,
          value: element.value,
          cursor: nextSelection.start,
          ending: false,
        };
        compositionRef.current = {
          ...nextComposition,
          value: element.value,
          cursor: nextSelection.start,
        };
        if (nextComposition.ending) commitComposition(element);
        return;
      }

      const skipped = skipNextChangeRef.current;
      if (skipped?.target === element) {
        skipNextChangeRef.current = null;
        if (skipped.value === element.value) return;
      }

      const cursor = element.selectionStart ?? element.value.length;
      const isCodeBlock = isChatComposerCursorInCodeBlock(
        element.value,
        cursor,
      );
      onValueChange(element.value, cursor, isCodeBlock);
      onCursorContextChangeRef.current?.(cursor, isCodeBlock);
    },
    [commitComposition, onValueChange],
  );

  const handleFocus = useCallback(
    (event: FocusEvent<HTMLTextAreaElement>) => {
      const element = event.currentTarget;
      const cursor = element.selectionStart ?? 0;
      const isCodeBlock = isChatComposerCursorInCodeBlock(element.value, cursor);
      onCursorContextChangeRef.current?.(cursor, isCodeBlock);
    },
    [],
  );

  const handleSelection = useCallback(
    (event: SyntheticEvent<HTMLTextAreaElement>) => {
      emitCursorContext(event.currentTarget);
    },
    [emitCursorContext],
  );

  const handleKeyUp = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      emitCursorContext(event.currentTarget);
    },
    [emitCursorContext],
  );

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      // Enter, Shift+Enter, Backspace/Delete, and arrows are deliberately
      // native textarea editing now. The parent owns only submit shortcuts.
      onKeyDown(event);
    },
    [onKeyDown],
  );

  // Synchronize only genuine external prop changes. Since this textarea is
  // uncontrolled, unrelated rerenders leave the browser's value, selection,
  // focus, and IME buffer untouched.
  useIsomorphicLayoutEffect(() => {
    latestValueRef.current = value;
    const previousValue = previousPropValueRef.current;
    const element = inputRef.current;

    if (value !== previousValue) {
      const composition = compositionRef.current;
      const selection = element ? readSelection(element) : null;
      if (composition) cancelComposition();

      if (element && element.value !== value) {
        const hadFocus = typeof document !== "undefined" &&
          document.activeElement === element;
        element.value = value;
        if (hadFocus && selection) {
          clampSelection(element, selection.start, selection.end);
        }
        if (hadFocus) emitCursorContext(element);
      }
    }

    previousPropValueRef.current = value;
  }, [cancelComposition, emitCursorContext, value]);

  useIsomorphicLayoutEffect(() => {
    const element = inputRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(Math.max(element.scrollHeight, 40), 120)}px`;
  });

  useEffect(
    () => () => {
      clearCompositionFlush();
      compositionRef.current = null;
      skipNextChangeRef.current = null;
    },
    [clearCompositionFlush],
  );

  return (
    <div
      data-chat-composer-editor="true"
      style={{ borderRadius: "0.25rem" }}
      className={cn(
        "w-full overflow-hidden rounded-xl border border-border-subtle bg-surface-charcoal transition-colors",
        "focus-within:border-primary focus-within:ring-1 focus-within:ring-primary/30",
        isDragOver && "border-primary ring-1 ring-primary/40",
      )}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <textarea
        ref={setInputRef}
        defaultValue={value}
        onChange={handleChange}
        onCompositionStart={handleCompositionStart}
        onCompositionEnd={handleCompositionEnd}
        onKeyDown={handleKeyDown}
        onKeyUp={handleKeyUp}
        onSelect={handleSelection}
        onPaste={onPaste}
        onFocus={handleFocus}
        data-chat-composer-input="true"
        aria-label="メッセージ入力"
        placeholder={placeholder}
        rows={1}
        wrap="soft"
        className={cn(
          "block w-full resize-none border-0 bg-transparent px-4 py-3 text-sm leading-5 text-foreground outline-none",
          "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-0",
        )}
        style={{ height: "40px", minHeight: "40px", maxHeight: "120px" }}
      />
    </div>
  );
}

"use client";

import { forwardRef } from "react";
import { LongTextEditor, type LongTextEditorHandle } from "./long-text-editor";
import {
  type LinkDisplayMode,
  type LinkDisplayModeChangeHandler,
  type LinkDisplayModeMap,
} from "./link-embed-plugin";

export type { LinkDisplayMode, LinkDisplayModeMap };

interface TaskDescriptionEditorProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  minHeight?: number;
  maxHeight?: number;
  className?: string;
  readOnly?: boolean;
  linkDisplayModes?: LinkDisplayModeMap | null;
  onLinkDisplayModeChange?: LinkDisplayModeChangeHandler;
  onSubmitIntent?: (value: string) => void;
  onArrowUpFromStart?: () => void;
}

export type TaskDescriptionEditorHandle = LongTextEditorHandle;

export const TaskDescriptionEditor = forwardRef<
  TaskDescriptionEditorHandle,
  TaskDescriptionEditorProps
>(function TaskDescriptionEditor(
  {
    value,
    onChange,
    placeholder = "",
    minHeight = 80,
    maxHeight,
    className,
    readOnly = false,
    linkDisplayModes,
    onLinkDisplayModeChange,
    onSubmitIntent,
    onArrowUpFromStart,
  },
  ref,
) {
  return (
    <LongTextEditor
      ref={ref}
      value={value}
      onChange={onChange}
      placeholder={placeholder}
      minHeight={minHeight}
      maxHeight={maxHeight}
      className={className}
      readOnly={readOnly}
      language="markdown"
      linkPreviews
      linkDisplayModes={linkDisplayModes}
      onLinkDisplayModeChange={onLinkDisplayModeChange}
      onSubmitIntent={onSubmitIntent}
      onArrowUpFromStart={onArrowUpFromStart}
    />
  );
});

"use client";

import type { ComponentProps, KeyboardEvent } from "react";

import { Input } from "@/components/ui/input";

type NewItemNameInputProps = Omit<
  ComponentProps<typeof Input>,
  "onChange" | "onKeyDown" | "value"
> & {
  value: string;
  onValueChange: (value: string) => void;
  onSubmit: () => void;
};

function formatTodayForName(now = new Date()): string {
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}${month}${day}`;
}

export function NewItemNameInput({
  value,
  onValueChange,
  onSubmit,
  ...props
}: NewItemNameInputProps) {
  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    const isTodayShortcut =
      event.altKey &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing &&
      event.code === "KeyD";

    if (isTodayShortcut) {
      event.preventDefault();

      const input = event.currentTarget;
      const selectionStart = input.selectionStart ?? value.length;
      const selectionEnd = input.selectionEnd ?? selectionStart;
      const today = formatTodayForName();
      const nextValue =
        value.slice(0, selectionStart) + today + value.slice(selectionEnd);
      const nextCursor = selectionStart + today.length;

      onValueChange(nextValue);
      window.setTimeout(() => {
        input.setSelectionRange(nextCursor, nextCursor);
      }, 0);
      return;
    }

    if (event.key === "Enter") {
      onSubmit();
    }
  };

  return (
    <Input
      {...props}
      value={value}
      onChange={(event) => onValueChange(event.target.value)}
      onKeyDown={handleKeyDown}
    />
  );
}

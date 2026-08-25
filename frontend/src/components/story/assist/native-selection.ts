import type { StoryAssistSelection } from "@/components/story/assist/types";

export function selectionFromNativeElement(
  element: HTMLInputElement | HTMLTextAreaElement,
): StoryAssistSelection | null {
  const value = element.value;
  const start = element.selectionStart ?? 0;
  const end = element.selectionEnd ?? value.length;
  if (start < end) {
    return { start, end, text: value.slice(start, end) };
  }
  return null;
}

export function resolveNativeSelection(container: HTMLElement): StoryAssistSelection | null {
  const active = document.activeElement;
  if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) {
    if (container.contains(active)) {
      return selectionFromNativeElement(active);
    }
  }
  const fields = container.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]), textarea');
  if (fields.length === 1) {
    const field = fields[0];
    if (field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement) {
      return selectionFromNativeElement(field);
    }
  }
  return null;
}

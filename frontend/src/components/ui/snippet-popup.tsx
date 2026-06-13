"use client";

import { cn } from "@/lib/utils";
import type { SnippetAutocompleteState } from "@/hooks/use-snippet-autocomplete";

interface SnippetPopupProps {
  state: SnippetAutocompleteState;
}

export function SnippetPopup({ state }: SnippetPopupProps) {
  if (!state.visible || state.matches.length === 0) return null;

  return (
    <div
      className="fixed z-[100] min-w-48 max-w-72 rounded-md border bg-popover shadow-lg"
      style={{ top: state.position.top, left: state.position.left }}
    >
      <div className="max-h-48 overflow-y-auto p-1">
        {state.matches.map((snippet, i) => (
          <div
            key={snippet.prefix}
            className={cn(
              "flex flex-col gap-0.5 rounded px-2 py-1.5 text-sm",
              i === state.selectedIndex && "bg-accent text-accent-foreground",
            )}
          >
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs font-semibold text-primary">
                {snippet.prefix}
              </span>
              {snippet.description && (
                <span className="truncate text-xs text-muted-foreground">
                  {snippet.description}
                </span>
              )}
            </div>
            <span className="truncate text-xs text-muted-foreground/70">
              {snippet.body.slice(0, 60)}
              {snippet.body.length > 60 ? "..." : ""}
            </span>
          </div>
        ))}
      </div>
      <div className="border-t px-2 py-1 text-[10px] text-muted-foreground">
        Tab で挿入 / Esc で閉じる
      </div>
    </div>
  );
}

import type {
  CompletionContext,
  CompletionResult,
} from "@codemirror/autocomplete";
import type { Snippet } from "@/lib/snippets-api";

export function snippetCompletionSource(snippets: Snippet[]) {
  return function (context: CompletionContext): CompletionResult | null {
    const word = context.matchBefore(/\S+/);
    if (!word || word.from === word.to) return null;

    const typed = word.text;
    const matches = snippets.filter((s) => s.prefix.startsWith(typed));
    if (matches.length === 0) return null;

    return {
      from: word.from,
      options: matches.map((s) => ({
        label: s.prefix,
        detail: s.description || "",
        apply: s.body,
        type: "snippet" as const,
        boost: 10,
      })),
    };
  };
}

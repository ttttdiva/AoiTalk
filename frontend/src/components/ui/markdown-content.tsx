"use client";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { withHardLineBreaks } from "@/lib/markdown-line-breaks";
import { cn } from "@/lib/utils";

const BASE_COMPONENTS: Components = {
  pre: ({ children }) => (
    <pre className="my-2 max-w-full overflow-x-auto rounded-md bg-black/20 p-3 text-sm">
      {children}
    </pre>
  ),
  code: ({ className, children, ...props }) => {
    const isInline = !className;
    if (isInline) {
      return (
        <code
          className="max-w-full rounded bg-black/20 px-1.5 py-0.5 text-sm [overflow-wrap:anywhere]"
          {...props}
        >
          {children}
        </code>
      );
    }
    return (
      <code className={cn("max-w-full", className)} {...props}>
        {children}
      </code>
    );
  },
  p: ({ children }) => (
    <p className="mb-2 max-w-full [overflow-wrap:anywhere] last:mb-0">
      {children}
    </p>
  ),
  ul: ({ children }) => (
    <ul className="mb-2 ml-4 list-disc space-y-1 last:mb-0">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-2 ml-4 list-decimal space-y-1 last:mb-0">{children}</ol>
  ),
  li: ({ children }) => <li className="text-sm">{children}</li>,
  h1: ({ children }) => <h1 className="mb-2 text-lg font-bold">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-2 text-base font-bold">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-1 text-sm font-bold">{children}</h3>,
  strong: ({ children }) => <strong className="font-bold">{children}</strong>,
  em: ({ children }) => (
    <em
      className="italic text-muted-foreground/80 not-italic"
      style={{ fontStyle: "italic" }}
    >
      {children}
    </em>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-muted-foreground/30 pl-3 italic">
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="my-2 max-w-full overflow-x-auto">
      <table className="min-w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-border bg-muted px-3 py-1.5 text-left font-semibold">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-border px-3 py-1.5">{children}</td>
  ),
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-primary underline [overflow-wrap:anywhere] hover:text-primary/80"
    >
      {children}
    </a>
  ),
  hr: () => <hr className="my-3 border-border" />,
};

/** チャット・タスクコメントなどで共有するMarkdown描画。 */
export function MarkdownContent({
  content,
  components,
  breaks = false,
}: {
  content: string;
  components?: Partial<Components>;
  breaks?: boolean;
}) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={components ? { ...BASE_COMPONENTS, ...components } : BASE_COMPONENTS}
    >
      {breaks ? withHardLineBreaks(content) : content}
    </ReactMarkdown>
  );
}

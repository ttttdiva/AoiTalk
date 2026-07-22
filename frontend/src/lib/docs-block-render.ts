import { docsBlockKind, type DocsBlockKind } from "@/lib/docs-block-model";
import type { DocsNode } from "@/components/docs/types";

export function escapeInlineHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function renderDocsInlineHtml(value: string) {
  let html = escapeInlineHtml(value || "");
  html = html.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a class="text-primary underline underline-offset-2" href="$2" target="_blank" rel="noreferrer">$1</a>');
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/==([^=\n]+)==/g, '<mark class="rounded bg-yellow-400/20 px-0.5">$1</mark>');
  html = html.replace(/`([^`\n]+)`/g, '<code class="rounded bg-muted px-1 py-0.5 text-[0.9em]">$1</code>');
  html = html.replace(/\[\[user:[0-9a-f-]{36}\|([^\]\n]+)\]\]/giu, '<span class="inline-flex items-center rounded-full border bg-primary/10 px-1.5 py-0.5 text-[0.85em] text-primary">@$1</span>');
  html = html.replace(/\[\[node:([0-9a-f-]{36})\|([^\]\n]+)\]\]/giu, (_m, id: string, label: string) => {
    const href = escapeInlineHtml(`/docs/${encodeURIComponent(id)}`);
    return `<a class="inline-flex items-center rounded-full border bg-primary/15 px-1.5 py-0.5 text-[0.85em] text-primary no-underline hover:bg-primary/25" href="${href}">${label}</a>`;
  });
  html = html.replace(/\[\[file:([^\]|\n]+)\|([^\]\n]+)\]\]/giu, (_m, path: string, label: string) => {
    const decodedPath = path.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"');
    const href = escapeInlineHtml(`/filer?open=${encodeURIComponent(decodedPath)}`);
    return `<a class="inline-flex items-center rounded-full border bg-secondary/40 px-1.5 py-0.5 text-[0.85em] text-foreground no-underline hover:bg-secondary/60" href="${href}">📎 ${label}</a>`;
  });
  html = html.replace(/\[\[task:([0-9a-f-]{36})\|([^\]\n]+)\]\]/giu, (_m, id: string, label: string) => (
    `<button type="button" data-docs-task-id="${id.toLowerCase()}" class="inline-flex items-center rounded-full border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-[0.85em] text-primary hover:bg-primary/20">☑ ${label}</button>`
  ));
  html = html.replace(/\[\[([^\]\n]+)\]\]/g, '<span class="rounded bg-muted px-1.5 py-0.5 text-muted-foreground">$1</span>');
  html = html.replace(/(^|\s)#([\p{L}\p{N}_-]+)/gu, '$1<span class="rounded border px-1.5 py-0.5 text-[0.85em]">#$2</span>');
  html = html.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a class="text-primary underline underline-offset-2" href="$2" target="_blank" rel="noreferrer">$2</a>');
  return html;
}

export function docsRowBlockClass(kind: DocsBlockKind) {
  if (kind === "heading_1") return "text-2xl font-bold leading-9 tracking-tight";
  if (kind === "heading_2") return "text-xl font-semibold leading-8 tracking-tight";
  if (kind === "heading_3") return "text-lg font-semibold leading-7";
  if (kind === "quote") return "border-l-2 border-primary/50 pl-3 text-muted-foreground";
  return "text-sm leading-7";
}

export function docsBlockKindForNode(node: Pick<DocsNode, "body_json" | "node_type">) {
  return docsBlockKind(node);
}

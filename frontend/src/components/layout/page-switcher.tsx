"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { FileText, Search, Tags } from "lucide-react";

type PageHit = {
  id: string;
  title: string;
  aliases: string[];
  node_type: string;
  project_id: string | null;
  breadcrumb: string[];
};

export function PageSwitcher() {
  const router = useRouter();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [pages, setPages] = useState<PageHit[]>([]);

  // ファイラーページでは Ctrl+P / Ctrl+Shift+P をファイラー固有のショートカット
  // （パスコピー / ファイル名コピー）に譲るため、ページスイッチャーは発火しない。
  const pathnameRef = useRef(pathname);

  useEffect(() => {
    pathnameRef.current = pathname;
  }, [pathname]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      // Ctrl+P（Shift なし）のみ。Ctrl+Shift+P は横取りしない。
      if (
        (event.ctrlKey || event.metaKey) &&
        !event.shiftKey &&
        event.key.toLowerCase() === "p"
      ) {
        if (pathnameRef.current?.startsWith("/filer")) return;
        event.preventDefault();
        event.stopPropagation();
        setOpen((current) => !current);
      }
    };
    window.addEventListener("keydown", handleKeyDown, true);
    return () => window.removeEventListener("keydown", handleKeyDown, true);
  }, []);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      fetch(`/api/docs/pages?q=${encodeURIComponent(query)}&limit=30`, {
        signal: controller.signal,
      })
        .then((response) => (response.ok ? response.json() as Promise<{ pages: PageHit[] }> : { pages: [] }))
        .then((data) => setPages(data.pages ?? []))
        .catch(() => {
          if (!controller.signal.aborted) setPages([]);
        });
    }, 80);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [open, query]);

  const title = useMemo(() => query.trim() ? "ページを検索" : "最近のDocsページ", [query]);

  const openPage = (pageId: string) => {
    setOpen(false);
    setQuery("");
    router.push(`/docs/${pageId}`);
  };

  return (
    <CommandDialog
      open={open}
      onOpenChange={setOpen}
      title="ページスイッチャー"
      description="Docsページをタイトルまたはエイリアスで開きます"
    >
      <Command shouldFilter={false}>
        <CommandInput
          value={query}
          onValueChange={setQuery}
          placeholder="ページ名またはエイリアス..."
        />
        <CommandList>
          <CommandEmpty>該当するページがありません</CommandEmpty>
          <CommandGroup heading={title}>
            {pages.map((page) => (
              <CommandItem
                key={page.id}
                value={`${page.title} ${page.aliases.join(" ")}`}
                onSelect={() => openPage(page.id)}
                className="items-start"
              >
                {page.node_type === "search" ? (
                  <Search className="mt-0.5 size-4 text-muted-foreground" />
                ) : (
                  <FileText className="mt-0.5 size-4 text-muted-foreground" />
                )}
                <div className="min-w-0">
                  <div className="truncate font-medium">{page.title}</div>
                  <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
                    <span className="truncate">{page.breadcrumb.join(" / ") || "Docs"}</span>
                    {page.aliases.length > 0 ? (
                      <span className="inline-flex min-w-0 items-center gap-1 truncate">
                        <Tags className="size-3" />
                        {page.aliases.join(", ")}
                      </span>
                    ) : null}
                  </div>
                </div>
              </CommandItem>
            ))}
          </CommandGroup>
        </CommandList>
      </Command>
    </CommandDialog>
  );
}

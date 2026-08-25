"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export type PaginationItem = number | "ellipsis-start" | "ellipsis-end";

/**
 * Combine directories/files in their existing display order and split only
 * after the caller has sorted them.  HF's grid uses this helper so one page
 * never creates thumbnail DOM nodes for the rest of the repository.
 */
export function paginateCombinedItems<Directory, File>(
  directories: Directory[],
  files: File[],
  page: number,
  pageSize = 60,
): {
  page: number;
  totalPages: number;
  directories: Directory[];
  files: File[];
} {
  const size = Math.max(1, Math.trunc(pageSize));
  const combined = [
    ...directories.map((item) => ({ kind: "directory" as const, item })),
    ...files.map((item) => ({ kind: "file" as const, item })),
  ];
  const totalPages = Math.max(1, Math.ceil(combined.length / size));
  const current = Math.min(totalPages, Math.max(1, Math.trunc(page)));
  const visible = combined.slice((current - 1) * size, current * size);
  return {
    page: current,
    totalPages,
    directories: visible
      .filter((entry) => entry.kind === "directory")
      .map((entry) => entry.item as Directory),
    files: visible
      .filter((entry) => entry.kind === "file")
      .map((entry) => entry.item as File),
  };
}

/** Compact first/last + nearby page window used by HF and Hydrus. */
export function buildPaginationItems(page: number, totalPages: number): PaginationItem[] {
  const total = Math.max(1, Math.trunc(totalPages));
  const current = Math.min(total, Math.max(1, Math.trunc(page)));
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);
  const values = new Set<number>([1, total, current]);
  for (let offset = 1; offset <= 2; offset += 1) {
    if (current - offset > 1) values.add(current - offset);
    if (current + offset < total) values.add(current + offset);
  }
  const sorted = [...values].sort((a, b) => a - b);
  const items: PaginationItem[] = [];
  sorted.forEach((value, index) => {
    const previous = sorted[index - 1];
    if (index > 0 && value - previous > 1) {
      items.push(index === 1 ? "ellipsis-start" : "ellipsis-end");
    }
    items.push(value);
  });
  return items;
}

interface PaginationProps {
  page: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  className?: string;
  label?: string;
}

export function Pagination({
  page,
  totalPages,
  onPageChange,
  className,
  label = "ページ",
}: PaginationProps) {
  const total = Math.max(1, Math.trunc(totalPages));
  const current = Math.min(total, Math.max(1, Math.trunc(page)));
  const [input, setInput] = useState(String(current));

  useEffect(() => {
    // Keep the direct-jump field synchronized after a clamped page change.
    setInput(String(current));
  }, [current]);

  if (total <= 1) return null;

  const move = () => {
    const parsed = Number.parseInt(input, 10);
    if (!Number.isFinite(parsed)) {
      setInput(String(current));
      return;
    }
    const target = Math.min(total, Math.max(1, parsed));
    setInput(String(target));
    if (target !== current) onPageChange(target);
  };

  return (
    <nav
      aria-label={`${label}移動`}
      className={cn("flex flex-wrap items-center gap-1 text-xs", className)}
    >
      <Button
        type="button"
        size="xs"
        variant="ghost"
        onClick={() => onPageChange(current - 1)}
        disabled={current <= 1}
      >
        前
      </Button>
      {buildPaginationItems(current, total).map((item, index) =>
        typeof item === "number" ? (
          <Button
            key={`${item}-${index}`}
            type="button"
            size="xs"
            variant={item === current ? "secondary" : "ghost"}
            aria-current={item === current ? "page" : undefined}
            aria-label={`${item}${label}`}
            onClick={() => item !== current && onPageChange(item)}
            disabled={item === current}
          >
            {item}
          </Button>
        ) : (
          <span key={`${item}-${index}`} aria-hidden="true" className="px-1">
            …
          </span>
        ),
      )}
      <Button
        type="button"
        size="xs"
        variant="ghost"
        onClick={() => onPageChange(current + 1)}
        disabled={current >= total}
      >
        次
      </Button>
      <form
        className="ml-1 flex items-center gap-1"
        onSubmit={(event) => {
          event.preventDefault();
          move();
        }}
      >
        <Input
          type="number"
          min={1}
          max={total}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          aria-label="移動先ページ"
          className="h-7 w-14 px-1 text-center"
        />
        <Button type="submit" size="xs" variant="outline">
          移動
        </Button>
      </form>
    </nav>
  );
}

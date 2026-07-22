"use client";

import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export function SettingsDisclosure({
  title,
  icon,
  summary,
  children,
  contentClassName,
  onOpenChange,
}: {
  title: string;
  icon?: ReactNode;
  summary?: ReactNode;
  children: ReactNode;
  contentClassName?: string;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(false);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    onOpenChange?.(next);
  };

  return (
    <Card size="sm">
      <CardHeader className="p-0 !px-0">
        <button
          type="button"
          className="-my-3 flex w-full items-center justify-between gap-3 px-3 py-3 text-left text-sm font-semibold"
          aria-expanded={open}
          onClick={toggle}
        >
          <span className="flex min-w-0 items-center gap-2">
            {icon}
            <span>{title}</span>
            {summary}
          </span>
          {open ? (
            <ChevronUp className="size-4 shrink-0" />
          ) : (
            <ChevronDown className="size-4 shrink-0" />
          )}
        </button>
      </CardHeader>
      {open && (
        <CardContent className={cn("space-y-3", contentClassName)}>
          {children}
        </CardContent>
      )}
    </Card>
  );
}

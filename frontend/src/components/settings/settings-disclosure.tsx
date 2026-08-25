"use client";

import { useEffect, useId, useState, type ReactNode } from "react";
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
  id,
  targetId,
  open: controlledOpen,
  defaultOpen = false,
}: {
  title: string;
  icon?: ReactNode;
  summary?: ReactNode;
  children: ReactNode;
  contentClassName?: string;
  onOpenChange?: (open: boolean) => void;
  /** Stable DOM id for direct settings links and tests. */
  id?: string;
  /** Hash target id. The page can open this disclosure from a deep link. */
  targetId?: string;
  /** Controlled state for page-owned disclosures. */
  open?: boolean;
  /** Initial state when `open` is not controlled. */
  defaultOpen?: boolean;
}) {
  const generatedId = useId().replace(/:/g, "");
  const contentId = `${id || targetId || generatedId}-content`;
  const isControlled = controlledOpen !== undefined;
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const open = isControlled ? controlledOpen : uncontrolledOpen;

  // Deep links may target a disclosure rendered by a child settings section.
  // Listening for this small page-local event avoids pushing page state through
  // every existing section while still making the target deterministic.
  useEffect(() => {
    if (!targetId || typeof window === "undefined") return;
    const handleTargetOpen = (event: Event) => {
      const detail = (event as CustomEvent<string>).detail;
      if (detail !== targetId || open) return;
      if (!isControlled) setUncontrolledOpen(true);
      onOpenChange?.(true);
    };
    window.addEventListener("settings:open-target", handleTargetOpen);
    return () => window.removeEventListener("settings:open-target", handleTargetOpen);
  }, [isControlled, onOpenChange, open, targetId]);

  const toggle = () => {
    const next = !open;
    if (!isControlled) setUncontrolledOpen(next);
    onOpenChange?.(next);
  };

  return (
    <Card
      id={id}
      data-settings-disclosure="true"
      data-settings-target={targetId}
      size="sm"
      className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0"
    >
      <CardHeader className="p-0 !px-0">
        <button
          type="button"
          className="flex min-h-10 w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm font-semibold transition-colors hover:bg-muted dark:bg-[#242426]"
          aria-expanded={open}
          aria-controls={contentId}
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
        <CardContent
          id={contentId}
          role="region"
          aria-label={`${title}の設定`}
          className={cn("space-y-3 border-t border-border dark:border-[#333335] bg-background/30 dark:bg-[#131313]/30 px-3 py-3", contentClassName)}
        >
          {children}
        </CardContent>
      )}
    </Card>
  );
}

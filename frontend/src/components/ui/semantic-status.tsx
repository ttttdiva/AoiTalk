import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils";

export type SemanticStatusTone = "info" | "warning";

const BADGE_TONE_CLASSES: Record<SemanticStatusTone, string> = {
  info: "border-info/35 bg-info/10 text-info",
  warning: "border-warning/35 bg-warning/10 text-warning",
};

const NOTE_TONE_CLASSES: Record<SemanticStatusTone, string> = {
  info: "border-info/40 bg-info/10",
  warning: "border-warning/40 bg-warning/10",
};

export function StatusBadge({
  tone = "info",
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: SemanticStatusTone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-2 py-0.5 text-[10px] font-semibold",
        BADGE_TONE_CLASSES[tone],
        className,
      )}
      {...props}
    />
  );
}

export function ReadOnlyBadge({
  children = "Read only",
  ...props
}: Omit<HTMLAttributes<HTMLSpanElement>, "children"> & {
  children?: ReactNode;
}) {
  return (
    <StatusBadge tone="warning" {...props}>
      {children}
    </StatusBadge>
  );
}

export function StatusNote({
  tone = "info",
  className,
  role,
  ...props
}: HTMLAttributes<HTMLDivElement> & { tone?: SemanticStatusTone }) {
  return (
    <div
      role={role ?? "note"}
      className={cn(
        "rounded-md border p-3 text-sm text-foreground",
        NOTE_TONE_CLASSES[tone],
        className,
      )}
      {...props}
    />
  );
}

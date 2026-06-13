"use client";

import type React from "react";

/** タスク詳細のプロパティ1行（アイコン + ラベル + 値）。 */
export function PropertyRow({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-2 py-2 min-h-[36px]">
      <div className="flex items-center gap-1.5 w-24 shrink-0 text-xs text-muted-foreground">
        {icon}
        <span>{label}</span>
      </div>
      <div className="flex-1 min-w-0">{children}</div>
    </div>
  );
}

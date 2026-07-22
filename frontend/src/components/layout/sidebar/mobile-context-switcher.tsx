"use client";

import { FolderOpen, Layers } from "lucide-react";
import { useProject } from "@/contexts/project-context";

// ─── モバイル用スペース/プロジェクト選択 ───
const mobileSelectClassName =
  "h-8 w-full rounded-lg border border-input bg-card px-2 text-sm text-foreground outline-none focus-visible:border-ring";

export function MobileContextSwitcher() {
  const {
    spaces,
    selectedSpaceId,
    setSelectedSpaceId,
    projects,
    selectedProjectId,
    setSelectedProjectId,
  } = useProject();

  if (spaces.length === 0 && projects.length === 0) return null;

  return (
    <div className="flex flex-col gap-2 px-2 pb-2 md:hidden">
      {spaces.length > 0 && (
        <div className="flex items-center gap-2">
          <Layers className="size-4 shrink-0 text-muted-foreground" />
          <select
            value={selectedSpaceId ?? ""}
            onChange={(e) => setSelectedSpaceId(e.target.value)}
            className={mobileSelectClassName}
            aria-label="スペース選択"
          >
            {spaces.map((s) => (
              <option key={s.id} value={s.id}>
                {s.source === "remote" ? "[EP] " : ""}
                {s.name}
              </option>
            ))}
          </select>
        </div>
      )}
      {projects.length > 0 && (
        <div className="flex items-center gap-2">
          <FolderOpen className="size-4 shrink-0 text-muted-foreground" />
          <select
            value={selectedProjectId ?? ""}
            onChange={(e) => setSelectedProjectId(e.target.value)}
            className={mobileSelectClassName}
            aria-label="プロジェクト選択"
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.source === "remote" ? "[EP] " : ""}
                {p.name}
              </option>
            ))}
          </select>
        </div>
      )}
    </div>
  );
}

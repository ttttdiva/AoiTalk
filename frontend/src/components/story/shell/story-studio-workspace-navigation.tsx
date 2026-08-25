"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Grid2X2, Share2 } from "lucide-react";

function isActive(pathname: string | null, href: string) {
  return pathname === href || Boolean(pathname?.startsWith(`${href}/`));
}

/** 一覧・共有ライブラリで使う Story Studio 共通ナビ。 */
export function StoryStudioWorkspaceNavigation() {
  const pathname = usePathname();
  const isLibrary = pathname === "/scenarios/library";
  const isWorks = !isLibrary;
  return (
    <nav
      className="flex h-full min-h-0 w-full flex-col overflow-y-auto bg-surface-charcoal text-on-surface"
      aria-label="Story Studioワークスペース"
      data-shell-workspace="story"
      data-shell-region="story-workspace-navigation"
    >
      <div className="border-b border-border-subtle px-4 py-4">
        <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-on-surface-variant">Story Studio</p>
        <p className="mt-1 truncate text-sm font-medium text-on-surface">{isLibrary ? "Shared Library" : "Works"}</p>
      </div>
      <div className="space-y-0.5 p-3" role="list">
        <p className="mb-2 px-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">Library</p>
        <Link
          href="/scenarios"
          aria-current={isActive(pathname, "/scenarios") && pathname !== "/scenarios/library" ? "page" : undefined}
          className={`flex h-7 items-center gap-3 rounded-sm border-l-2 px-2 text-[13px] transition-colors ${isWorks ? "border-primary bg-surface-container-highest text-primary" : "border-transparent text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface"}`}
        >
          <Grid2X2 className="size-4" aria-hidden="true" />
          All Works
        </Link>
        <Link
          href="/scenarios/library?tab=cast"
          aria-current={pathname === "/scenarios/library" ? "page" : undefined}
          className={`flex h-7 items-center gap-3 rounded-sm border-l-2 px-2 text-[13px] transition-colors ${isLibrary ? "border-primary bg-surface-container-highest text-primary" : "border-transparent text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface"}`}
        >
          <Share2 className="size-4" aria-hidden="true" />
          Shared Library
        </Link>
      </div>
    </nav>
  );
}

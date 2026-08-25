"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BookOpen, FileText, Gamepad2, Search } from "lucide-react";
import type { ReactNode } from "react";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";

type TrpgWorkspaceShellProps = {
  children: ReactNode;
};

function isActivePath(pathname: string | null, href: string): boolean {
  return pathname === href || pathname?.startsWith(`${href}/`) === true;
}

/**
 * TRPG keeps a deliberately small read-only surface. Runtime controls are not
 * part of this nav; writing continues in Story Studio and reference assets stay
 * read-only.
 */
export function TrpgWorkspaceNavigation({
  compact = false,
  playMode = false,
}: {
  compact?: boolean;
  playMode?: boolean;
}) {
  const pathname = usePathname();
  const links = [
    { href: "/trpg", label: "TRPGホーム", icon: BookOpen },
    { href: "/trpg/play", label: "卓をプレイ", icon: Gamepad2 },
    { href: "/trpg/reference", label: "資料を参照", icon: Search },
    { href: "/scenarios?kind=trpg", label: "Story Studio", icon: FileText },
    { href: "/scenarios/library?tab=rules", label: "共有ルールブック", icon: BookOpen },
  ];

  return (
    <nav
      aria-label="TRPGワークスペース"
      data-trpg-capability={playMode ? "play" : "read-only"}
      className={compact
        ? "flex min-w-0 gap-1 overflow-x-auto"
        : "flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-2 py-3"}
    >
      {links.map(({ href, label, icon: Icon }) => {
        const pathOnly = href.split("?")[0];
        const active = pathOnly === "/trpg"
          ? pathname === "/trpg"
          : pathOnly === "/scenarios"
            ? pathname === "/scenarios"
            : isActivePath(pathname, pathOnly);
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={compact
              ? `inline-flex shrink-0 items-center gap-1.5 rounded-[4px] border px-2.5 py-1.5 text-xs ${active ? "border-primary/50 bg-primary/10 text-primary" : "border-transparent text-muted-foreground hover:border-border hover:bg-muted/50"}`
              : `group flex min-h-7 items-center gap-2 rounded-[4px] border-l-2 px-2 py-1.5 text-[13px] leading-4 transition-colors ${active ? "border-primary bg-muted/60 text-foreground" : "border-transparent text-muted-foreground hover:border-border hover:bg-muted/40 hover:text-foreground"}`}
          >
            <Icon className={compact ? "size-3.5 shrink-0" : `size-4 shrink-0 ${active ? "text-primary" : "text-muted-foreground group-hover:text-foreground"}`} aria-hidden="true" />
            <span className="truncate">{label}</span>
          </Link>
        );
      })}
    </nav>
  );
}

export function TrpgWorkspaceShell({
  children,
  playMode = false,
}: TrpgWorkspaceShellProps & { playMode?: boolean }) {
  const slotNavigation = (
    <aside
      className="ao-workspace-nav-panel"
      data-shell-slot="workspace-navigation"
      data-workspace="trpg"
    >
      <div className="flex min-h-0 flex-1 flex-col bg-card/70">
        <div className="border-b border-border px-4 pb-4 pt-4">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold tracking-tight text-foreground">TRPG</h2>
            {playMode ? (
              <span className="rounded-[4px] border border-emerald-500/35 bg-emerald-500/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.14em] text-emerald-700 dark:text-emerald-300">
                Play
              </span>
            ) : (
              <span className="rounded-[4px] border border-primary/35 bg-primary/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.14em] text-primary">
                Read only
              </span>
            )}
          </div>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            {playMode ? "TRPG 卓の実行と共有ログ" : "ルールと関連資料を参照"}
          </p>
        </div>
        <p className="px-4 pb-1 pt-4 text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground/80">
          Workspace
        </p>
        <TrpgWorkspaceNavigation playMode={playMode} />
        <div className="mt-auto border-t border-border px-4 py-3">
          <p className="text-[11px] leading-4 text-muted-foreground">
            作品の編集は Story Studio で行います。
          </p>
        </div>
      </div>
    </aside>
  );

  useWorkspaceShellRegistration({
    id: "trpg-workspace",
    workspaceNavigation: slotNavigation,
    priority: 35,
  });

  return (
    <div data-trpg-workspace={playMode ? "play" : "read-only"} className="min-h-full">
      {children}
    </div>
  );
}

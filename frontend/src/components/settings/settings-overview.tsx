"use client";

import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import {
  getVisibleSettingsCategories,
  type SettingsCategoryId,
} from "./settings-category-navigation";

type SettingsOverviewProps = {
  isAdmin: boolean;
  onSelectCategory?: (category: SettingsCategoryId) => void;
  /** Optional slot for existing quick settings; no domain state is created here. */
  quickSettings?: ReactNode;
};

/**
 * Small entry point for Settings. It keeps quick settings and category
 * navigation together without creating additional domain state.
 */
export function SettingsOverview({
  isAdmin,
  onSelectCategory,
  quickSettings,
}: SettingsOverviewProps) {
  const categories = getVisibleSettingsCategories(isAdmin).filter(
    (category) => category.id !== "overview",
  );

  return (
    <section id="overview" className="scroll-mt-4 space-y-5" data-settings-overview>
      {quickSettings}

      <div className="space-y-2" aria-label="設定カテゴリ一覧">
        <div className="flex items-center justify-between border-b border-border dark:border-[#333335] pb-2">
          <h2
            tabIndex={-1}
            className="text-[11px] font-semibold uppercase tracking-[0.16em] text-muted-foreground"
          >
            Settings
          </h2>
          <span className="text-[11px] text-muted-foreground">{categories.length} categories</span>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {categories.map(({ id, label, description, icon: Icon }) => (
            <a
              key={id}
              href={`#${id}`}
              onClick={(event) => {
                // Keep category movement in the page-level hash/history
                // handler.  Native anchor scrolling would otherwise run in
                // parallel with that handler and move the wrong shell node.
                if (!onSelectCategory) return;
                event.preventDefault();
                onSelectCategory(id);
              }}
              className="group flex min-h-16 items-center gap-3 rounded-md border border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] px-3 py-2.5 text-left transition-colors hover:border-primary/50 hover:bg-muted dark:bg-[#242426]"
            >
              <span className="grid size-8 shrink-0 place-items-center rounded-sm border border-border dark:border-[#333335] bg-muted dark:bg-[#242426] text-muted-foreground group-hover:text-primary">
                <Icon className="size-4" aria-hidden="true" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{label}</span>
                <span className="mt-0.5 block truncate text-xs text-muted-foreground">{description}</span>
              </span>
              <ChevronRight className="size-3.5 shrink-0 text-muted-foreground group-hover:text-primary" aria-hidden="true" />
            </a>
          ))}
        </div>
      </div>
    </section>
  );
}

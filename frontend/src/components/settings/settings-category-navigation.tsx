"use client";

import {
  SETTINGS_CATEGORIES,
  type SettingsCategory,
  type SettingsCategoryId,
} from "./settings-target-registry";

export {
  SETTINGS_CATEGORIES,
  type SettingsCategory,
  type SettingsCategoryId,
} from "./settings-target-registry";

/**
 * Settings is a long, server-backed page. Keep the navigation as a plain
 * anchor contract so it can be mounted in the Shared Shell workspace slot or
 * inline while a page is being migrated. The sections themselves stay owned
 * by settings/page.tsx; this component does not duplicate their state.
 */
export function getVisibleSettingsCategories(isAdmin: boolean): SettingsCategory[] {
  return SETTINGS_CATEGORIES.filter((category) => !category.adminOnly || isAdmin);
}

export function SettingsCategoryNavigation({
  activeCategory = "overview",
  isAdmin = false,
  onSelect,
}: {
  activeCategory?: SettingsCategoryId;
  isAdmin?: boolean;
  onSelect?: (category: SettingsCategoryId) => void;
}) {
  const categories = getVisibleSettingsCategories(isAdmin);

  const groups: Array<{ label: string; ids: SettingsCategoryId[] }> = [
    { label: "QUICK SETTINGS", ids: ["overview", "account"] },
    { label: "WORKSPACE", ids: ["conversation", "tts-yomi", "input", "notifications", "knowledge"] },
    { label: "SYSTEM", ids: ["integrations", "tool-permissions"] },
    { label: "ORGANIZATION", ids: ["admin", "support"] },
  ];

  return (
    <nav aria-label="設定カテゴリ" data-settings-slot="category-navigation">
      <div className="space-y-5">
        {groups.map((group) => {
          const groupCategories = group.ids
            .map((id) => categories.find((category) => category.id === id))
            .filter((category): category is SettingsCategory => Boolean(category));
          if (groupCategories.length === 0) return null;
          return (
            <div key={group.label} className="space-y-1">
              <div className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
                {group.label}
              </div>
              {groupCategories.map(({ id, label, description, icon: Icon }) => {
          const active = id === activeCategory;
          const className = [
            "group relative flex min-h-9 min-w-0 items-center gap-2 rounded-sm border-l-2 px-3 py-1.5 text-left transition-colors",
            active
              ? "border-primary bg-surface-container-highest text-primary"
              : "border-transparent text-muted-foreground hover:border-border hover:bg-muted hover:text-foreground",
          ].join(" ");

          return (
            <a
              key={id}
              href={`#${id}`}
              data-settings-target={id}
              aria-current={active ? "location" : undefined}
              className={className}
              onClick={(event) => {
                // The page owns hash/history updates and the single scroll
                // operation.  Keep the href for copy/link semantics, but do
                // not let native hash scrolling race that handler.
                if (!onSelect) return;
                event.preventDefault();
                onSelect(id);
              }}
            >
              <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              <span className="min-w-0">
                <span className="block truncate text-[13px] font-medium">{label}</span>
                <span className="sr-only">{description}</span>
              </span>
            </a>
              );
              })}
            </div>
          );
        })}
      </div>
    </nav>
  );
}

import {
  BarChart3,
  Boxes,
  Calendar,
  CheckSquare,
  Film,
  FileText,
  Folder,
  FolderOpen,
  MessageSquare,
  Settings,
  Swords,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { DOCS_NAV_LABEL, DOCS_ROUTE } from "@/lib/docs-model";

export type AppNavigationVisibilityKey = "scenarios" | "trpg";

export type AppNavigationVisibility = Record<
  AppNavigationVisibilityKey,
  boolean
>;

export type AppViewTab = {
  id: string;
  title: string;
  href: string;
  icon: LucideIcon;
  shortcut: string;
  available: boolean;
  visibilityKey?: AppNavigationVisibilityKey;
};

export const APP_VIEW_TABS = [
  {
    id: "chat",
    title: "チャット",
    href: "/chat",
    icon: MessageSquare,
    shortcut: "1",
    available: true,
  },
  {
    id: "tasks",
    title: "タスク",
    href: "/tasks",
    icon: CheckSquare,
    shortcut: "2",
    available: true,
  },
  {
    id: "calendar",
    title: "カレンダー",
    href: "/calendar",
    icon: Calendar,
    shortcut: "3",
    available: true,
  },
  {
    id: "docs",
    title: DOCS_NAV_LABEL,
    href: DOCS_ROUTE,
    icon: FileText,
    shortcut: "4",
    available: true,
  },
  {
    id: "filer",
    title: "Files",
    href: "/filer",
    icon: Folder,
    shortcut: "5",
    available: true,
  },
  {
    id: "reports",
    title: "レポート",
    href: "/reports",
    icon: BarChart3,
    shortcut: "6",
    available: true,
  },
  {
    id: "projects",
    title: "プロジェクト",
    href: "/projects",
    icon: FolderOpen,
    shortcut: "7",
    available: true,
  },
  {
    id: "scenarios",
    title: "シナリオ",
    href: "/scenarios",
    icon: Film,
    shortcut: "8",
    available: true,
    visibilityKey: "scenarios",
  },
  {
    id: "trpg",
    title: "TRPG",
    href: "/trpg",
    icon: Swords,
    shortcut: "9",
    available: true,
    visibilityKey: "trpg",
  },
  {
    id: "apps",
    title: "Apps",
    href: "/apps",
    icon: Boxes,
    // Apps used to have a separate rail and therefore had no Alt+digit
    // shortcut. Keep that shortcut contract while exposing it in the shared
    // Global Rail.
    shortcut: "",
    available: true,
  },
  {
    id: "settings",
    title: "設定",
    href: "/settings",
    icon: Settings,
    shortcut: "0",
    available: true,
  },
] as const satisfies readonly AppViewTab[];

export const OPTIONAL_APP_VIEW_TABS = APP_VIEW_TABS.filter(
  (
    tab,
  ): tab is (typeof APP_VIEW_TABS)[number] & {
    visibilityKey: AppNavigationVisibilityKey;
  } => "visibilityKey" in tab,
);

export function assertValidAppViewTabRegistry(tabs: readonly AppViewTab[]) {
  for (const property of ["id", "href", "shortcut"] as const) {
    const values = new Set<string>();
    for (const tab of tabs) {
      if (property === "shortcut" && tab[property] === "") continue;
      if (values.has(tab[property])) {
        throw new Error(
          `Duplicate app navigation ${property}: ${tab[property]}`,
        );
      }
      values.add(tab[property]);
    }
  }
}

export function getVisibleAppViewTabs(
  visibility: AppNavigationVisibility,
): readonly AppViewTab[] {
  return APP_VIEW_TABS.filter(
    (tab) =>
      tab.available &&
      (!("visibilityKey" in tab) || visibility[tab.visibilityKey]),
  );
}

if (process.env.NODE_ENV !== "production") {
  assertValidAppViewTabRegistry(APP_VIEW_TABS);
}

export const APP_ALT_SHORTCUTS: Record<string, string> = APP_VIEW_TABS.reduce<
  Record<string, string>
>((acc, tab) => {
  if (tab.shortcut) acc[tab.shortcut] = tab.href;
  return acc;
}, {});

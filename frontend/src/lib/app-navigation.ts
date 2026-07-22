import {
  BarChart3,
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
import { DOCS_NAV_LABEL, DOCS_ROUTE } from "@/lib/docs-model";

export const APP_VIEW_TABS = [
  { title: "チャット", href: "/chat", icon: MessageSquare, shortcut: "1" },
  { title: "タスク", href: "/tasks", icon: CheckSquare, shortcut: "2" },
  { title: "カレンダー", href: "/calendar", icon: Calendar, shortcut: "3" },
  { title: DOCS_NAV_LABEL, href: DOCS_ROUTE, icon: FileText, shortcut: "4" },
  { title: "ファイラー", href: "/filer", icon: Folder, shortcut: "5" },
  { title: "レポート", href: "/reports", icon: BarChart3, shortcut: "6" },
  { title: "プロジェクト", href: "/projects", icon: FolderOpen, shortcut: "7" },
  { title: "シナリオ", href: "/scenarios", icon: Film, shortcut: "8" },
  { title: "TRPG", href: "/trpg", icon: Swords, shortcut: "9" },
  { title: "設定", href: "/settings", icon: Settings, shortcut: "0" },
] as const;

export const APP_ALT_SHORTCUTS: Record<string, string> =
  APP_VIEW_TABS.reduce<Record<string, string>>((acc, tab) => {
    acc[tab.shortcut] = tab.href;
    return acc;
  }, {});

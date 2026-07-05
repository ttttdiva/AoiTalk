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
  { title: "チャット", href: "/chat", icon: MessageSquare },
  { title: "タスク", href: "/tasks", icon: CheckSquare },
  { title: "カレンダー", href: "/calendar", icon: Calendar },
  { title: DOCS_NAV_LABEL, href: DOCS_ROUTE, icon: FileText },
  { title: "ファイラー", href: "/filer", icon: Folder },
  { title: "レポート", href: "/reports", icon: BarChart3 },
  { title: "プロジェクト", href: "/projects", icon: FolderOpen },
  { title: "シナリオ", href: "/scenarios", icon: Film },
  { title: "TRPG", href: "/trpg", icon: Swords },
  { title: "設定", href: "/settings", icon: Settings },
] as const;

export const APP_ALT_SHORTCUTS: Record<string, string> =
  APP_VIEW_TABS.reduce<Record<string, string>>((acc, tab, index) => {
    acc[String(index + 1)] = tab.href;
    return acc;
  }, {});

"use client";

/* eslint-disable @next/next/no-img-element */

import { usePathname, useRouter } from "next/navigation";
import { useState, useEffect, useCallback, useRef, Suspense } from "react";
import {
  MessageSquare,
  CheckSquare,
  FolderOpen,
  FileText,
  Home,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import { DOCS_ROUTE } from "@/lib/docs-model";
import { ChatSidebar } from "./sidebar/chat-sidebar";
import { TaskSidebar } from "./sidebar/task-sidebar";
import { FilerSidebar } from "./sidebar/filer-sidebar";
import { NotificationPanel } from "./sidebar/notification-panel";
import { MobileContextSwitcher } from "./sidebar/mobile-context-switcher";

type SidebarTab = "chat" | "tasks" | "filer" | "docs";
const SIDEBAR_TABS: SidebarTab[] = ["chat", "tasks", "filer", "docs"];
const DOCS_SIDEBAR_SLOT_ID = "docs-sidebar-slot";

function isDocsPath(pathname: string | null) {
  return pathname === DOCS_ROUTE || pathname?.startsWith(`${DOCS_ROUTE}/`);
}

// ─── メインサイドバー ───
function AppSidebarInner() {
  const router = useRouter();
  const pathname = usePathname();
  const { state: sidebarState, isMobile, openMobile } = useSidebar();
  // サイドバーのタブ状態をlocalStorageで永続化（メイン画面のルートとは独立）
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>(
    () => {
      if (typeof window !== "undefined") {
        const saved = localStorage.getItem("aoitalk-sidebar-tab");
        if (SIDEBAR_TABS.includes(saved as SidebarTab))
          return saved as SidebarTab;
      }
      return "chat";
    },
  );

  const tabBeforeDocsRef = useRef<Exclude<SidebarTab, "docs"> | null>(null);
  const docsPathActive = isDocsPath(pathname);

  useEffect(() => {
    if (docsPathActive) {
      setSidebarTab((current) => {
        if (current !== "docs" && !tabBeforeDocsRef.current) {
          tabBeforeDocsRef.current = current;
        }
        return "docs";
      });
      return;
    }

    setSidebarTab((current) => {
      if (current !== "docs" && !tabBeforeDocsRef.current) return current;
      const next = tabBeforeDocsRef.current ?? "chat";
      tabBeforeDocsRef.current = null;
      return next;
    });
  }, [docsPathActive]);

  const handleSetSidebarTab = useCallback((tab: SidebarTab) => {
    if (tab === "docs") {
      if (sidebarTab !== "docs") router.push(DOCS_ROUTE);
      return;
    }
    setSidebarTab(tab);
    localStorage.setItem("aoitalk-sidebar-tab", tab);
  }, [router, sidebarTab]);

  const handleBrandClick = useCallback(() => {
    setSidebarTab("chat");
    localStorage.setItem("aoitalk-sidebar-tab", "chat");
    localStorage.removeItem("aoitalk_last_session_id");
    if (!navigateChatSessionInPlace("/chat")) {
      window.location.href = "/chat";
    }
  }, []);

  const handleHomeClick = useCallback(() => {
    window.dispatchEvent(new Event("global-open-home"));
  }, []);

  const shouldRenderDocsSlot =
    sidebarTab === "docs" &&
    (isMobile ? openMobile : sidebarState === "expanded");

  return (
    <Sidebar>
      <SidebarHeader className="ao-sidebar-hero justify-center">
        <div className="flex min-w-0 items-center gap-1.5">
          <button
            type="button"
            onClick={handleBrandClick}
            className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-2 text-left transition-colors hover:bg-sidebar-accent focus-visible:ring-2 focus-visible:ring-sidebar-ring focus-visible:outline-none"
            title="新規チャットを開く"
          >
            <img
              src="/images/ui/brand-orb.png"
              alt=""
              className="size-9 shrink-0 rounded-xl object-cover ring-1 ring-sidebar-border"
            />
            <div className="min-w-0">
              <span className="block text-lg font-semibold leading-5 tracking-tight">
                AoiTalk
              </span>
              <span className="block truncate text-[11px] font-semibold leading-4 text-sidebar-foreground/58">
                Crystal workspace
              </span>
            </div>
          </button>
          <Tooltip>
            <TooltipTrigger
              render={
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  className="shrink-0"
                  onClick={handleHomeClick}
                >
                  <Home className="size-4" />
                  <span className="sr-only">Todayを開く</span>
                </Button>
              }
            />
            <TooltipContent side="right">
              Todayを開く (Ctrl+Shift+H)
            </TooltipContent>
          </Tooltip>
        </div>
        <MobileContextSwitcher />
      </SidebarHeader>
      <SidebarContent>
        {/* 通知パネル */}
        <NotificationPanel />

        {/* サイドバー内タブ切り替え（メイン画面は遷移しない） */}
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={sidebarTab === "chat"}
                  onClick={() => handleSetSidebarTab("chat")}
                >
                  <MessageSquare className="size-4" />
                  <span>チャット</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={sidebarTab === "tasks"}
                  onClick={() => handleSetSidebarTab("tasks")}
                >
                  <CheckSquare className="size-4" />
                  <span>タスク</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={sidebarTab === "filer"}
                  onClick={() => handleSetSidebarTab("filer")}
                >
                  <FolderOpen className="size-4" />
                  <span>ファイラー</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={sidebarTab === "docs"}
                  onClick={() => handleSetSidebarTab("docs")}
                >
                  <FileText className="size-4" />
                  <span>Docs</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {/* タブに応じたサイドバーコンテンツ */}
        {sidebarTab === "chat" && <ChatSidebar />}
        {sidebarTab === "tasks" && <TaskSidebar />}
        {sidebarTab === "filer" && <FilerSidebar />}
        {shouldRenderDocsSlot && (
          <div id={DOCS_SIDEBAR_SLOT_ID} className="min-h-0 flex-1 flex flex-col" />
        )}
      </SidebarContent>
    </Sidebar>
  );
}

export function AppSidebar() {
  return (
    <Suspense>
      <AppSidebarInner />
    </Suspense>
  );
}

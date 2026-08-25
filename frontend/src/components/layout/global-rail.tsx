"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Home,
  PanelRightClose,
  PanelRightOpen,
  Sparkles,
  SunMoon,
} from "lucide-react";
import { useSyncExternalStore, type ReactNode } from "react";
import { useTheme } from "@/contexts/theme-context";
import { useUserSettings } from "@/contexts/user-settings-context";
import { getVisibleAppViewTabs } from "@/lib/app-navigation";
import { getAppNavigationVisibility } from "@/lib/user-settings";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import { NotificationBellPopover } from "./sidebar/notification-panel";
import { useShellChrome } from "./shell-context";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

function isActivePath(pathname: string | null, href: string): boolean {
  if (!pathname) return false;
  return pathname === href || pathname.startsWith(`${href}/`);
}

const subscribeToHydration = () => () => {};

function RailButton({
  label,
  children,
  onClick,
  active = false,
  pressed,
}: {
  label: string;
  children: ReactNode;
  onClick?: () => void;
  active?: boolean;
  pressed?: boolean;
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            aria-label={label}
            aria-pressed={pressed}
            onClick={onClick}
            className={`ao-global-rail-button ${active ? "is-active" : ""}`}
          />
        }
      >
        {children}
      </TooltipTrigger>
      <TooltipContent side="right" sideOffset={8}>
        {label}
      </TooltipContent>
    </Tooltip>
  );
}

export function GlobalRail() {
  const pathname = usePathname();
  const { settings } = useUserSettings();
  const { resolvedTheme, setTheme } = useTheme();
  const themeHydrated = useSyncExternalStore(
    subscribeToHydration,
    () => true,
    () => false,
  );
  const {
    contextRailOpen,
    toggleContextRail,
    workspaceShellRegistrations,
  } = useShellChrome();
  const visibility = getAppNavigationVisibility(settings);
  const tabs = getVisibleAppViewTabs(visibility);
  const contextRailAvailable = workspaceShellRegistrations.some(
    (entry) =>
      entry.routeKey === (pathname ?? "/") &&
      entry.contextRail !== undefined &&
      entry.contextRail !== null &&
      !entry.contextRailPersistent,
  );
  const displayedTheme = themeHydrated ? resolvedTheme : "light";

  const handleBrandClick = () => {
    try {
      window.localStorage.removeItem("aoitalk_last_session_id");
      window.localStorage.setItem("aoitalk-sidebar-tab", "chat");
    } catch {
      // localStorageが無効でも新規チャットへの遷移は継続する。
    }
    if (!navigateChatSessionInPlace("/chat")) window.location.href = "/chat";
  };

  return (
    <aside
      className="ao-global-rail flex h-full shrink-0 flex-col border-r border-border text-sidebar-foreground"
      data-shell-region="global-rail"
    >
      <Tooltip>
        <TooltipTrigger
          render={
            <button
              type="button"
              onClick={handleBrandClick}
              aria-label="新規チャットを開く"
              className="ao-global-rail-brand"
            />
          }
        >
          <span className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground">
            <Sparkles className="size-4" />
          </span>
        </TooltipTrigger>
        <TooltipContent side="right" sideOffset={8}>
          新規チャットを開く
        </TooltipContent>
      </Tooltip>

      <nav className="ao-global-rail-nav min-h-0 flex-1 overflow-y-auto px-1.5 py-2" aria-label="Workspace">
        {tabs.map((tab) => {
          const active = isActivePath(pathname, tab.href);
          const Icon = tab.icon;
          return (
            <Tooltip key={tab.id}>
              <TooltipTrigger
                render={
                  <Link
                    href={tab.href}
                    aria-label={tab.title}
                    aria-current={active ? "page" : undefined}
                    className={`ao-global-rail-button ${active ? "is-active" : ""}`}
                  />
                }
              >
                <Icon className="size-[18px]" />
              </TooltipTrigger>
              <TooltipContent side="right" sideOffset={8}>
                {tab.title}
                {tab.shortcut ? ` (Alt+${tab.shortcut})` : ""}
              </TooltipContent>
            </Tooltip>
          );
        })}
      </nav>

      <div className="ao-global-rail-utility space-y-0.5 border-t border-sidebar-border px-1.5 py-2">
        <RailButton label="Todayを開く" onClick={() => window.dispatchEvent(new Event("global-open-home"))}>
          <Home className="size-[17px]" />
        </RailButton>
        {contextRailAvailable && (
          <RailButton
            label={contextRailOpen ? "詳細パネルを閉じる" : "詳細パネルを開く"}
            pressed={contextRailOpen}
            onClick={toggleContextRail}
          >
            {contextRailOpen ? (
              <PanelRightClose className="size-[17px]" />
            ) : (
              <PanelRightOpen className="size-[17px]" />
            )}
          </RailButton>
        )}
        <NotificationBellPopover
          onOpenChange={(open) => {
            if (open) {
              window.dispatchEvent(new Event("global-close-workspace-navigation"));
            }
          }}
        />
        <RailButton
          label={`テーマを${displayedTheme === "dark" ? "ライト" : "ダーク"}に切り替え`}
          onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
        >
          <SunMoon className="size-[17px]" />
        </RailButton>
      </div>
    </aside>
  );
}

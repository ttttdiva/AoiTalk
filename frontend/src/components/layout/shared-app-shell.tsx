"use client";

import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { X } from "lucide-react";
import { useSidebar } from "@/components/ui/sidebar";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { GlobalRail } from "./global-rail";
import {
  ShellChromeProvider,
  type ShellChromeContextValue,
  useShellChrome,
} from "./shell-context";

export type SharedAppShellProps = {
  children: ReactNode;
  contextBar: ReactNode;
  /** Explicit shell-owned slot; takes precedence over page registration. */
  workspaceNavigation?: ReactNode;
  /** Legacy adapter fallback used until a Workspace registers its own slot. */
  legacyWorkspaceNavigation?: ReactNode;
  contextRail?: ReactNode;
};

type WorkspaceShellRegistration =
  ShellChromeContextValue["workspaceShellRegistrations"][number];

const COMPACT_DESKTOP_MEDIA_QUERY = "(min-width: 768px) and (max-width: 1599px)";

function useCompactDesktopViewport(isMobile: boolean): boolean {
  const [isCompactDesktop, setIsCompactDesktop] = useState(false);

  useEffect(() => {
    if (isMobile || typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }

    const mediaQuery = window.matchMedia(COMPACT_DESKTOP_MEDIA_QUERY);
    const update = () => setIsCompactDesktop(mediaQuery.matches);
    update();
    mediaQuery.addEventListener?.("change", update);
    return () => mediaQuery.removeEventListener?.("change", update);
  }, [isMobile]);

  return isCompactDesktop;
}

function resolveRegisteredEntry(
  pathname: string | null,
  registrations: ShellChromeContextValue["workspaceShellRegistrations"],
  slot: "workspaceNavigation" | "contextRail",
): WorkspaceShellRegistration | undefined {
  const route = pathname ?? "/";
  return registrations
    .filter((entry) => entry.routeKey === route && entry[slot] !== undefined)
    .sort((left, right) => right.priority - left.priority || left.id.localeCompare(right.id))[0];
}

function resolveRegisteredSlot(
  pathname: string | null,
  registrations: ShellChromeContextValue["workspaceShellRegistrations"],
  slot: "workspaceNavigation" | "contextRail",
): ReactNode | undefined {
  return resolveRegisteredEntry(pathname, registrations, slot)?.[slot];
}

function SharedAppShellInner({
  children,
  contextBar,
  workspaceNavigation,
  legacyWorkspaceNavigation,
  contextRail,
}: SharedAppShellProps) {
  const pathname = usePathname();
  const { state, isMobile, openMobile, setOpen, setOpenMobile } = useSidebar();
  const isCompactDesktop = useCompactDesktopViewport(isMobile);
  const {
    contextRailOpen,
    setContextRailOpen,
    setNotificationPanelOpen,
    workspaceShellRegistrations,
  } = useShellChrome();
  // Compact desktop keeps the workspace navigation open by default as an
  // inline shell column. The user can still collapse it explicitly.
  const compactDesktopInitializedRef = useRef(false);
  const compactDesktopAutoCollapsedRef = useRef(false);
  const compactDesktopUserOpenedRef = useRef(false);
  const previousNavigationOpenRef = useRef(false);
  const shellRootRef = useRef<HTMLDivElement>(null);
  const isAppsRoute = pathname === "/apps" || pathname?.startsWith("/apps/");
  const registeredWorkspaceNavigation = resolveRegisteredSlot(
    pathname,
    workspaceShellRegistrations,
    "workspaceNavigation",
  );
  const registeredWorkspaceEntry = resolveRegisteredEntry(
    pathname,
    workspaceShellRegistrations,
    "workspaceNavigation",
  );
  const registeredContextRail = resolveRegisteredSlot(
    pathname,
    workspaceShellRegistrations,
    "contextRail",
  );
  const registeredContextRailEntry = resolveRegisteredEntry(
    pathname,
    workspaceShellRegistrations,
    "contextRail",
  );
  const resolvedWorkspaceNavigation =
    workspaceNavigation !== undefined
      ? workspaceNavigation
      : registeredWorkspaceNavigation !== undefined
        ? registeredWorkspaceNavigation
        : legacyWorkspaceNavigation;
  const resolvedContextRail =
    contextRail !== undefined ? contextRail : registeredContextRail;
  const hasWorkspaceNavigation =
    resolvedWorkspaceNavigation != null &&
    !(isAppsRoute &&
      workspaceNavigation === undefined &&
      registeredWorkspaceNavigation === undefined);
  const isDesktopPersistentWorkspaceNavigation =
    !isMobile &&
    isCompactDesktop &&
    workspaceNavigation === undefined &&
    registeredWorkspaceEntry?.desktopPersistent === true &&
    registeredWorkspaceEntry.workspaceNavigation != null;
  const isDesktopPersistentContextRail =
    !isMobile &&
    contextRail === undefined &&
    registeredContextRailEntry?.contextRailPersistent === true &&
    registeredContextRailEntry.contextRail != null;
  // Context Rail is normally a shell transient. Chat's information rail opts
  // into a desktop-persistent registration and remains visible while Runtime,
  // Escape, or the global rail toggle changes transient state.
  const showContextRail =
    resolvedContextRail != null &&
    (contextRailOpen || isDesktopPersistentContextRail);

  useEffect(() => {
    if (isMobile) {
      return;
    }

    const applyCompactDesktopState = () => {
      if (isCompactDesktop) {
        if (isDesktopPersistentWorkspaceNavigation) {
          // A workspace can opt into the inline shell geometry at compact
          // desktop widths. Keep the global sidebar state expanded so the
          // registration cannot be hidden behind the off-canvas frame.
          compactDesktopInitializedRef.current = false;
          compactDesktopAutoCollapsedRef.current = false;
          if (state === "collapsed") setOpen(true);
          return;
        }

        if (!compactDesktopInitializedRef.current) {
          compactDesktopInitializedRef.current = true;
          return;
        }

        if (state === "expanded" && compactDesktopAutoCollapsedRef.current) {
          // The only transition from our automatic collapsed state to
          // expanded is an explicit SidebarTrigger/user action.
          compactDesktopAutoCollapsedRef.current = false;
          compactDesktopUserOpenedRef.current = true;
        } else if (state === "collapsed" && !compactDesktopAutoCollapsedRef.current) {
          compactDesktopUserOpenedRef.current = false;
        }
        return;
      }

      if (!compactDesktopInitializedRef.current) return;
      compactDesktopInitializedRef.current = false;
      if (compactDesktopAutoCollapsedRef.current && state === "collapsed") {
        // Restore the normal inline desktop shell after an automatic compact
        // collapse; an explicit user collapse remains collapsed.
        compactDesktopAutoCollapsedRef.current = false;
        setOpen(true);
      }
    };

    applyCompactDesktopState();
  }, [isCompactDesktop, isDesktopPersistentWorkspaceNavigation, isMobile, setOpen, state]);

  useEffect(() => {
    if (!contextRailOpen) return;
    // Context Rail is the only shell surface that replaces workspace
    // navigation. Runtime is a fixed utility popup anchored to the global
    // header and can coexist with the chat/sidebar navigation, including at
    // compact desktop and mobile breakpoints.
    if (isMobile) {
      if (openMobile) setOpenMobile(false);
    } else if (
      isCompactDesktop &&
      !isDesktopPersistentWorkspaceNavigation &&
      state !== "collapsed"
    ) {
      setOpen(false);
    }
  }, [
    contextRailOpen,
    isCompactDesktop,
    isDesktopPersistentWorkspaceNavigation,
    isMobile,
    openMobile,
    setOpen,
    setOpenMobile,
    state,
  ]);

  useEffect(() => {
    const closeWorkspaceNavigation = () => {
      if (isMobile) {
        setOpenMobile(false);
        return;
      }
      if (
        isCompactDesktop &&
        !isDesktopPersistentWorkspaceNavigation &&
        state !== "collapsed"
      ) {
        setOpen(false);
      }
    };
    window.addEventListener(
      "global-close-workspace-navigation",
      closeWorkspaceNavigation,
    );
    return () =>
      window.removeEventListener(
        "global-close-workspace-navigation",
        closeWorkspaceNavigation,
      );
  }, [
    isCompactDesktop,
    isDesktopPersistentWorkspaceNavigation,
    isMobile,
    setOpen,
    setOpenMobile,
    state,
  ]);

  useEffect(() => {
    const navigationOpen = isMobile ? openMobile : state === "expanded";
    const openedByUser = navigationOpen && !previousNavigationOpenRef.current;
    previousNavigationOpenRef.current = navigationOpen;
    if (openedByUser) setNotificationPanelOpen(false);
  }, [isMobile, openMobile, setNotificationPanelOpen, state]);

  useEffect(() => {
    if (isMobile && openMobile && !hasWorkspaceNavigation) {
      setOpenMobile(false);
    }
  }, [hasWorkspaceNavigation, isMobile, openMobile, setOpenMobile]);

  return (
    <div
      ref={shellRootRef}
      className="ao-shell-root flex h-dvh min-h-0 min-w-0 flex-1 overflow-hidden bg-background"
      data-shell="contextual-workspace"
      data-mobile={isMobile ? "true" : "false"}
    >
      <GlobalRail />
      <div className="ao-shell-stack flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="shrink-0">{contextBar}</div>
        <div
          className="ao-shell-content flex min-h-0 min-w-0 flex-1"
          data-apps-route={isAppsRoute ? "true" : "false"}
          data-nav-state={state}
        >
          {isMobile ? (
            <Sheet
              open={openMobile}
              onOpenChange={setOpenMobile}
              onOpenChangeComplete={(nextOpen) => {
                if (nextOpen) return;
                shellRootRef.current
                  ?.querySelector<HTMLElement>('[data-sidebar="trigger"]')
                  ?.focus({ preventScroll: true });
              }}
            >
              <SheetContent
                side="left"
                initialFocus
                finalFocus={() =>
                  shellRootRef.current?.querySelector<HTMLElement>(
                    '[data-sidebar="trigger"]',
                  ) ?? true
                }
                className="top-14 bottom-0 h-auto w-[min(18rem,92vw)] max-w-none gap-0 overflow-auto border-r p-0"
                data-shell-region="workspace-navigation-frame"
                data-testid="workspace-navigation-frame"
                data-empty={hasWorkspaceNavigation ? "false" : "true"}
                data-sidebar-state={state}
                data-sidebar-persistent="false"
                data-mobile-open={openMobile ? "true" : "false"}
              >
                <SheetHeader className="sr-only">
                  <SheetTitle>Workspace Navigation</SheetTitle>
                  <SheetDescription>
                    現在のワークスペースのナビゲーションメニューです。
                  </SheetDescription>
                </SheetHeader>
                {resolvedWorkspaceNavigation}
              </SheetContent>
            </Sheet>
          ) : (
            <div
              className={`ao-workspace-nav-frame shrink-0 ${hasWorkspaceNavigation ? "" : "is-empty"} ${isDesktopPersistentWorkspaceNavigation ? "is-desktop-persistent" : ""} ${state === "collapsed" && !isDesktopPersistentWorkspaceNavigation ? "is-collapsed" : ""}`}
              data-shell-region="workspace-navigation-frame"
              data-testid="workspace-navigation-frame"
              data-empty={hasWorkspaceNavigation ? "false" : "true"}
              data-sidebar-state={state}
              data-sidebar-persistent={isDesktopPersistentWorkspaceNavigation ? "true" : "false"}
              data-mobile-open="true"
            >
              {resolvedWorkspaceNavigation}
            </div>
          )}

          {!isMobile &&
            !isDesktopPersistentWorkspaceNavigation &&
            state === "expanded" &&
            hasWorkspaceNavigation && (
            <button
              type="button"
              className="ao-workspace-nav-scrim fixed inset-0 z-[60] cursor-default"
              aria-label="Workspace Navigationを閉じる"
              onClick={() => setOpen(false)}
            />
          )}

          <main
            className="ao-main-canvas ao-main-panel flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
            data-shell-region="main-canvas"
          >
            <div className="ao-main-scroll min-h-0 min-w-0 flex-1 overflow-auto">
              {children}
            </div>
          </main>

          {showContextRail && (
            <aside
              className="ao-context-rail shrink-0 overflow-hidden border-l"
              aria-label={
                isDesktopPersistentContextRail ? "チャット情報レール" : "Context Rail"
              }
              data-shell-region="context-rail"
              data-context-rail-persistent={
                isDesktopPersistentContextRail ? "true" : "false"
              }
            >
              {!isDesktopPersistentContextRail && (
                <button
                  type="button"
                  className="ao-context-rail-close"
                  aria-label="Context Railを閉じる"
                  title="Context Railを閉じる"
                  onClick={() => setContextRailOpen(false)}
                >
                  <X className="size-4" />
                </button>
              )}
              <div className="ao-context-rail-content h-full min-h-0 overflow-auto">
                {resolvedContextRail}
              </div>
            </aside>
          )}
        </div>
      </div>

    </div>
  );
}

/**
 * Shared desktop shell. Existing pages are intentionally accepted as children
 * so each Workspace can migrate independently without changing providers or
 * domain state owners.
 */
export function SharedAppShell(props: SharedAppShellProps) {
  return (
    <ShellChromeProvider>
      <SharedAppShellInner {...props} />
    </ShellChromeProvider>
  );
}

/** Adapter marker used while a Workspace still owns its legacy navigation. */
export function LegacyWorkspaceNavigationAdapter({ children }: { children: ReactNode }) {
  return (
    <div data-shell-adapter="legacy-workspace-navigation" className="contents">
      {children}
    </div>
  );
}

/** Slot marker for future Workspace-specific Context/Utility Rail content. */
export function ContextRailSlot({ children }: { children: ReactNode }) {
  return <>{children}</>;
}

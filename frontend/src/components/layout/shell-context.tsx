"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useId,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from "react";
import { usePathname } from "next/navigation";
import { DOUBLE_ESCAPE_RESET_EVENT } from "@/lib/double-escape";

export type WorkspaceShellRegistrationOptions = {
  /** Optional stable key when a page owns more than one registration. */
  id?: string;
  /** Defaults to the current pathname. Explicit values are useful for tests. */
  routeKey?: string;
  /** Higher priority wins when multiple pages register the same slot. */
  priority?: number;
  /** `undefined` leaves the legacy slot untouched; `null` intentionally clears it. */
  workspaceNavigation?: ReactNode | null;
  /**
   * Keep this workspace navigation inline at compact desktop widths instead of
   * letting SharedAppShell auto-collapse it into an overlay. Mobile and wide
   * desktop behavior are unchanged.
   */
  desktopPersistent?: boolean;
  /** `undefined` leaves the static/context fallback untouched; `null` clears it. */
  contextRail?: ReactNode | null;
  /**
   * Keep this route's Context Rail mounted on desktop independently of the
   * transient shell toggle.  Mobile callers must provide their own drawer.
   */
  contextRailPersistent?: boolean;
};

type WorkspaceShellRegistrationEntry = {
  id: string;
  routeKey: string;
  priority: number;
  workspaceNavigation?: ReactNode | null;
  desktopPersistent?: boolean;
  contextRail?: ReactNode | null;
  contextRailPersistent?: boolean;
  token: object;
};

function areReactNodesEquivalent(left: ReactNode | undefined, right: ReactNode | undefined): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((item, index) => areReactNodesEquivalent(item, right[index]));
  }
  if (left == null || right == null) return left == null && right == null;
  // Inline event handlers are recreated on every page render. Their identity
  // must not churn the shell registry; visual/scalar props still participate
  // in the comparison below.
  if (typeof left === "function" && typeof right === "function") return true;
  if (typeof left !== "object" || typeof right !== "object") return false;
  const leftElement = left as ReactElement;
  const rightElement = right as ReactElement;
  if (leftElement.type !== rightElement.type || leftElement.key !== rightElement.key) return false;
  const leftProps = leftElement.props as Record<string, unknown>;
  const rightProps = rightElement.props as Record<string, unknown>;
  const keys = new Set([...Object.keys(leftProps), ...Object.keys(rightProps)]);
  return [...keys].every((key) => {
    if (key === "children") {
      return areReactNodesEquivalent(leftProps[key] as ReactNode, rightProps[key] as ReactNode);
    }
    return areValuesEquivalent(leftProps[key], rightProps[key]);
  });
}

function areValuesEquivalent(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (typeof left === "function" && typeof right === "function") return true;
  if (isValidElement(left) || isValidElement(right)) {
    return isValidElement(left) && isValidElement(right)
      ? areReactNodesEquivalent(left, right)
      : false;
  }
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((item, index) => areValuesEquivalent(item, right[index]));
  }
  if (left instanceof Date || right instanceof Date) {
    return left instanceof Date && right instanceof Date && Object.is(left.getTime(), right.getTime());
  }
  if (left instanceof Map || right instanceof Map) {
    if (!(left instanceof Map) || !(right instanceof Map) || left.size !== right.size) return false;
    const unmatched = [...right.entries()];
    for (const [leftKey, leftValue] of left.entries()) {
      const matchIndex = unmatched.findIndex(
        ([rightKey, rightValue]) =>
          areValuesEquivalent(leftKey, rightKey) && areValuesEquivalent(leftValue, rightValue),
      );
      if (matchIndex < 0) return false;
      unmatched.splice(matchIndex, 1);
    }
    return unmatched.length === 0;
  }
  if (left instanceof Set || right instanceof Set) {
    if (!(left instanceof Set) || !(right instanceof Set) || left.size !== right.size) return false;
    const unmatched = [...right.values()];
    for (const leftValue of left.values()) {
      const matchIndex = unmatched.findIndex((rightValue) => areValuesEquivalent(leftValue, rightValue));
      if (matchIndex < 0) return false;
      unmatched.splice(matchIndex, 1);
    }
    return unmatched.length === 0;
  }
  if (left && right && typeof left === "object" && typeof right === "object") {
    const leftPrototype = Object.getPrototypeOf(left);
    const rightPrototype = Object.getPrototypeOf(right);
    // Only compare plain records structurally. Treating class instances and
    // other opaque objects as `{}` would incorrectly hide meaningful updates.
    const isPlainRecord = (prototype: object | null) =>
      prototype === Object.prototype || prototype === null;
    if (!isPlainRecord(leftPrototype) || !isPlainRecord(rightPrototype)) return false;
    const leftRecord = left as Record<string, unknown>;
    const rightRecord = right as Record<string, unknown>;
    const keys = new Set([...Object.keys(leftRecord), ...Object.keys(rightRecord)]);
    return [...keys].every((key) => areValuesEquivalent(leftRecord[key], rightRecord[key]));
  }
  return false;
}

function useOptionalPathname(): string | null {
  // Some isolated page tests mock next/navigation without usePathname. The
  // registration hook remains a no-op outside the shell provider in that case.
  // eslint-disable-next-line react-hooks/rules-of-hooks
  return typeof usePathname === "function" ? usePathname() : null;
}

function areRegistrationsEquivalent(
  left: WorkspaceShellRegistrationEntry,
  right: WorkspaceShellRegistrationEntry,
): boolean {
  return (
    left.id === right.id &&
    left.routeKey === right.routeKey &&
    left.priority === right.priority &&
    left.desktopPersistent === right.desktopPersistent &&
    left.contextRailPersistent === right.contextRailPersistent &&
    areReactNodesEquivalent(left.workspaceNavigation, right.workspaceNavigation) &&
    areReactNodesEquivalent(left.contextRail, right.contextRail)
  );
}

export type ShellChromeContextValue = {
  runtimePanelOpen: boolean;
  setRuntimePanelOpen: (open: boolean) => void;
  toggleRuntimePanel: () => void;
  /** Notification panel visibility is shell-owned so Global Rail can open the
   * single mounted card without relying on a transient DOM event. */
  notificationPanelOpen: boolean;
  setNotificationPanelOpen: (open: boolean) => void;
  toggleNotificationPanel: () => void;
  /** Unread notification count published by the always-mounted Global Rail card. */
  notificationUnreadCount: number;
  setNotificationUnreadCount: (count: number) => void;
  contextRailOpen: boolean;
  setContextRailOpen: (open: boolean) => void;
  toggleContextRail: () => void;
  /** Internal shell slot registry used by page-level workspace adapters. */
  registerWorkspaceShell: (
    entry: WorkspaceShellRegistrationEntry,
  ) => () => void;
  updateWorkspaceShellRegistration: (entry: WorkspaceShellRegistrationEntry) => void;
  workspaceShellRegistrations: readonly WorkspaceShellRegistrationEntry[];
};

const ShellChromeContext = createContext<ShellChromeContextValue | null>(null);

export function ShellChromeProvider({ children }: { children: ReactNode }) {
  const [runtimePanelOpen, setRuntimePanelOpen] = useState(false);
  const [notificationPanelOpen, setNotificationPanelOpenState] =
    useState(false);
  const [notificationUnreadCount, setNotificationUnreadCount] = useState(0);
  const [contextRailOpen, setContextRailOpenState] = useState(false);
  const [workspaceShellRegistrations, setWorkspaceShellRegistrations] = useState<
    Record<string, WorkspaceShellRegistrationEntry>
  >({});
  const closeShellTransients = useCallback(() => {
    setRuntimePanelOpen(false);
    setNotificationPanelOpenState(false);
    setContextRailOpenState(false);
  }, []);

  const toggleRuntimePanel = useCallback(() => {
    setRuntimePanelOpen((open) => {
      const next = !open;
      if (next) {
        setNotificationPanelOpenState(false);
        setContextRailOpenState(false);
      }
      return next;
    });
  }, []);

  const setNotificationPanelOpen = useCallback((open: boolean) => {
    setNotificationPanelOpenState(open);
    if (open) {
      setRuntimePanelOpen(false);
      setContextRailOpenState(false);
    }
  }, []);

  const toggleNotificationPanel = useCallback(() => {
    setNotificationPanelOpenState((open) => {
      const next = !open;
      if (next) {
        setRuntimePanelOpen(false);
        setContextRailOpenState(false);
      }
      return next;
    });
  }, []);

  const setContextRailOpen = useCallback((open: boolean) => {
    setContextRailOpenState(open);
    if (open) {
      setRuntimePanelOpen(false);
      setNotificationPanelOpenState(false);
    }
  }, []);

  const toggleContextRail = useCallback(() => {
    setContextRailOpenState((open) => {
      const next = !open;
      if (next) {
        setRuntimePanelOpen(false);
        setNotificationPanelOpenState(false);
      }
      return next;
    });
  }, []);

  useEffect(() => {
    // These events open existing global dialogs/overlays which intentionally
    // live above the shared shell. Close shell transients first so their focus
    // traps and Escape handlers remain authoritative. Unrelated workspace
    // events are deliberately not included.
    const overlayEvents = [
      "global-command-palette",
      "docs-command-open",
      "docs-open-clip-ingest",
      "global-create-task",
      "global-shortcuts-help",
      "global-open-memo",
      "global-open-home",
      "global-admin-restart",
    ] as const;
    overlayEvents.forEach((eventName) =>
      window.addEventListener(eventName, closeShellTransients),
    );
    const handleOverlayShortcut = (event: KeyboardEvent) => {
      if (event.altKey || event.shiftKey || (!event.ctrlKey && !event.metaKey)) return;
      const key = event.key.toLowerCase();
      if (key === "k" || key === "p") closeShellTransients();
    };
    window.addEventListener("keydown", handleOverlayShortcut, true);
    return () => {
      overlayEvents.forEach((eventName) =>
        window.removeEventListener(eventName, closeShellTransients),
      );
      window.removeEventListener("keydown", handleOverlayShortcut, true);
    };
  }, [closeShellTransients]);

  const registerWorkspaceShell = useCallback(
    (entry: WorkspaceShellRegistrationEntry) => {
      setWorkspaceShellRegistrations((current) => {
        const previous = current[entry.id];
        if (previous && areRegistrationsEquivalent(previous, entry)) return current;
        return { ...current, [entry.id]: entry };
      });
      return () => {
        setWorkspaceShellRegistrations((current) => {
          const previous = current[entry.id];
          // Route changes and StrictMode replays can run an older cleanup after
          // a newer registration. Never remove the newer token in that case.
          if (!previous || previous.token !== entry.token) return current;
          const next = { ...current };
          delete next[entry.id];
          return next;
        });
      };
    },
    [],
  );

  const updateWorkspaceShellRegistration = useCallback(
    (entry: WorkspaceShellRegistrationEntry) => {
      setWorkspaceShellRegistrations((current) => {
        const previous = current[entry.id];
        if (!previous || previous.token !== entry.token) return current;
        if (areRegistrationsEquivalent(previous, entry)) return current;
        return { ...current, [entry.id]: entry };
      });
    },
    [],
  );

  // Escape closes transient shell panels before a workspace gets a chance to
  // handle it. Files keeps its existing double-Escape listener because this
  // only handles a single Escape when a shell panel is actually open.
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (runtimePanelOpen) {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        setRuntimePanelOpen(false);
        window.dispatchEvent(new Event(DOUBLE_ESCAPE_RESET_EVENT));
        return;
      }
      if (contextRailOpen) {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        setContextRailOpen(false);
        window.dispatchEvent(new Event(DOUBLE_ESCAPE_RESET_EVENT));
        return;
      }
      if (notificationPanelOpen) {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        setNotificationPanelOpen(false);
        window.dispatchEvent(new Event(DOUBLE_ESCAPE_RESET_EVENT));
        return;
      }
    };
    // Capture before workspace/document handlers so a transient shell panel
    // always receives Escape even when the focused workspace control consumes
    // the bubbling event.
    window.addEventListener("keydown", handleKeyDown, true);
    return () => window.removeEventListener("keydown", handleKeyDown, true);
  }, [
    contextRailOpen,
    notificationPanelOpen,
    runtimePanelOpen,
    setContextRailOpen,
    setNotificationPanelOpen,
  ]);

  const value = useMemo<ShellChromeContextValue>(
    () => ({
      runtimePanelOpen,
      setRuntimePanelOpen,
      toggleRuntimePanel,
      notificationPanelOpen,
      setNotificationPanelOpen,
      toggleNotificationPanel,
      notificationUnreadCount,
      setNotificationUnreadCount,
      contextRailOpen,
      setContextRailOpen,
      toggleContextRail,
      registerWorkspaceShell,
      updateWorkspaceShellRegistration,
      workspaceShellRegistrations: Object.values(workspaceShellRegistrations),
    }),
    [
      contextRailOpen,
      runtimePanelOpen,
      notificationPanelOpen,
      notificationUnreadCount,
      setNotificationUnreadCount,
      setNotificationPanelOpen,
      setContextRailOpen,
      toggleRuntimePanel,
      toggleNotificationPanel,
      toggleContextRail,
      registerWorkspaceShell,
      updateWorkspaceShellRegistration,
      workspaceShellRegistrations,
    ],
  );

  return (
    <ShellChromeContext.Provider value={value}>
      {children}
    </ShellChromeContext.Provider>
  );
}

export function useShellChrome(): ShellChromeContextValue {
  const value = useContext(ShellChromeContext);
  if (!value) {
    throw new Error("useShellChrome must be used within ShellChromeProvider.");
  }
  return value;
}

/** Optional variant for Workspace unit tests that render outside AppLayout. */
export function useOptionalShellChrome(): ShellChromeContextValue | null {
  return useContext(ShellChromeContext);
}

/**
 * Register route-local shell slots without changing AppLayout. The registration
 * is scoped to the current pathname, cleaned up on unmount/route change, and
 * intentionally no-ops when a page is rendered in an isolated unit test.
 *
 * Static `SharedAppShell` slot props remain authoritative. Registrations are
 * used when a slot has no explicit static prop (the legacy AppSidebar is a
 * fallback), with priority then generated id resolving collisions.
 */
export function useWorkspaceShellRegistration(
  options: WorkspaceShellRegistrationOptions,
): void {
  const shell = useContext(ShellChromeContext);
  const registerWorkspaceShell = shell?.registerWorkspaceShell;
  const updateWorkspaceShellRegistration = shell?.updateWorkspaceShellRegistration;
  const setContextRailOpen = shell?.setContextRailOpen;
  const pathname = useOptionalPathname();
  const generatedId = useId();
  const registrationId = options.id ?? generatedId;
  const routeKey = options.routeKey ?? pathname ?? "/";
  const priority = options.priority ?? 0;
  const token = useMemo<object>(
    () => ({ registrationId, routeKey }),
    [registrationId, routeKey],
  );
  const registrationEntry = useMemo<WorkspaceShellRegistrationEntry>(
    () => ({
      id: registrationId,
      routeKey,
      priority,
      workspaceNavigation: options.workspaceNavigation,
      desktopPersistent: options.desktopPersistent ?? false,
      contextRail: options.contextRail,
      contextRailPersistent: options.contextRailPersistent ?? false,
      token,
    }),
    [
      options.contextRail,
      options.workspaceNavigation,
      options.desktopPersistent,
      options.contextRailPersistent,
      priority,
      registrationId,
      routeKey,
      token,
    ],
  );

  useEffect(() => {
    if (!registerWorkspaceShell) return;
    // Nodes are refreshed by the second effect without tearing down the
    // registration; only route/id changes should run this cleanup cycle.
    const unregister = registerWorkspaceShell(registrationEntry);
    return () => {
      unregister();
      // A route-local registration owns the lifetime of its Context Rail. Its
      // cleanup closes any open rail before the next route can register one;
      setContextRailOpen?.(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [registerWorkspaceShell, registrationId, routeKey, setContextRailOpen, token]);

  useEffect(() => {
    if (!updateWorkspaceShellRegistration) return;
    updateWorkspaceShellRegistration(registrationEntry);
  }, [
    options.contextRail,
    options.desktopPersistent,
    options.workspaceNavigation,
    priority,
    registrationId,
    routeKey,
    registrationEntry,
    token,
    updateWorkspaceShellRegistration,
  ]);
}

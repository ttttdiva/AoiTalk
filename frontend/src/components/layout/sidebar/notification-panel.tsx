"use client";

import { useRouter } from "next/navigation";
import { useState, useEffect, useCallback, useRef } from "react";
import useSWR from "swr";
import { CheckCheck, Bell, X } from "lucide-react";
import { formatRelativeTime } from "@/lib/utils";
import {
  claimOsNotificationCandidates,
  isFreshUnreadOsNotification,
} from "@/lib/os-notification-dedupe";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
} from "@/components/ui/sidebar";
import {
  Popover,
  PopoverContent,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useOptionalShellChrome } from "../shell-context";

// ─── 通知 ───
const OS_NOTIFICATION_SEEN_KEY = "aoitalk-os-notification-seen";
const OS_NOTIFICATION_SEEN_LIMIT = 200;
const OS_NOTIFICATION_STALE_MS = 24 * 60 * 60 * 1000;
const OS_NOTIFICATION_LOCK_NAME = "aoitalk-os-notification-claim";
const NOTIFICATION_SERVICE_WORKER_URL = "/aoitalk-notifications-sw.js";
const NOTIFICATIONS_SWR_KEY = "layout/notifications";

type OsNotificationPermission = NotificationPermission | "unsupported";

interface InAppNotification {
  id: string;
  type: string;
  title: string;
  message?: string | null;
  task_id?: string | null;
  is_read: boolean;
  created_at: string;
  delivered_at?: string | null;
}

const EMPTY_NOTIFICATIONS: InAppNotification[] = [];

async function getNotificationServiceWorkerRegistration() {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
    return null;
  }
  try {
    const registration = await navigator.serviceWorker.register(
      NOTIFICATION_SERVICE_WORKER_URL,
      { scope: "/" },
    );
    // `register()` may return before the worker controls the page.  Awaiting
    // `ready` avoids subscribing against an installing worker and makes
    // registration/subscription deterministic across tabs and reloads.
    try {
      return await navigator.serviceWorker.ready;
    } catch {
      return registration;
    }
  } catch {
    return null;
  }
}

type NotificationPanelProps = {
  /** Bind open state and unread count to the shared shell chrome. */
  listenGlobal?: boolean;
  /** Render as a standalone notification card anchored to the rail button. */
  presentation?: "sidebar" | "popover";
  /** Optional trigger styling for hosts outside the Global Rail. */
  triggerClassName?: string;
  /** Close the workspace navigation before displaying the popover. */
  onOpenChange?: (open: boolean) => void;
};

type NotificationBellPopoverProps = {
  /** Mount the data-owning popover only for the requested responsive surface. */
  mobileOnly?: boolean;
  triggerClassName?: string;
  onOpenChange?: (open: boolean) => void;
};

const MOBILE_BREAKPOINT = 768;

function useResolvedMobileSurface(): boolean | null {
  const [isMobile, setIsMobile] = useState<boolean | null>(null);

  useEffect(() => {
    const mediaQuery =
      typeof window.matchMedia === "function"
        ? window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`)
        : null;
    const update = () => {
      setIsMobile(mediaQuery ? mediaQuery.matches : window.innerWidth < MOBILE_BREAKPOINT);
    };
    update();
    mediaQuery?.addEventListener("change", update);
    return () => mediaQuery?.removeEventListener("change", update);
  }, []);

  return isMobile;
}

function decodeVapidKey(value: string): BufferSource {
  const padded = `${value}${"=".repeat((4 - (value.length % 4)) % 4)}`
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  const binary = window.atob(padded);
  return Uint8Array.from(
    binary,
    (character) => character.charCodeAt(0),
  ) as unknown as BufferSource;
}

async function ensureWebPushSubscription(
  registration: ServiceWorkerRegistration,
): Promise<boolean> {
  if (!("PushManager" in window) || !registration.pushManager) return false;
  try {
    const response = await fetch("/api/web-push/vapid-public-key", {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) return false;
    const config = (await response.json()) as {
      enabled?: boolean;
      public_key?: string | null;
    };
    if (!config.enabled || !config.public_key) return false;

    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: decodeVapidKey(config.public_key),
      });
    }
    const json = subscription.toJSON();
    const endpoint = json.endpoint;
    const p256dh = json.keys?.p256dh;
    const auth = json.keys?.auth;
    if (!endpoint || !p256dh || !auth) return false;

    const saveResponse = await fetch("/api/web-push/subscription", {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        endpoint,
        expiration_time: subscription.expirationTime,
        p256dh,
        auth,
        content_encoding: "aes128gcm",
      }),
    });
    return saveResponse.ok;
  } catch {
    // Push is an optional enhancement. Browser policy/provider failures fall
    // back to the existing in-app and page Notification API paths.
    return false;
  }
}

function rememberPushedNotification(id: string): void {
  try {
    const current = window.localStorage.getItem(OS_NOTIFICATION_SEEN_KEY);
    const parsed = current ? JSON.parse(current) : [];
    const ids = Array.isArray(parsed)
      ? parsed.filter((value): value is string => typeof value === "string")
      : [];
    if (!ids.includes(id)) ids.push(id);
    window.localStorage.setItem(
      OS_NOTIFICATION_SEEN_KEY,
      JSON.stringify(ids.slice(-OS_NOTIFICATION_SEEN_LIMIT)),
    );
  } catch {
    // localStorage is best-effort; push delivery remains durable in the inbox.
  }
}

export function NotificationPanel({
  listenGlobal = false,
  presentation = "sidebar",
  triggerClassName,
  onOpenChange,
}: NotificationPanelProps) {
  const router = useRouter();
  const shellChrome = useOptionalShellChrome();
  const [localOpen, setLocalOpen] = useState(false);
  const open =
    listenGlobal && shellChrome ? shellChrome.notificationPanelOpen : localOpen;
  const publishUnreadCount = useCallback(
    (nextCount: number) => {
      if (!listenGlobal || !shellChrome) return;
      shellChrome.setNotificationUnreadCount(nextCount);
    },
    [listenGlobal, shellChrome],
  );
  const setPanelOpen = useCallback(
    (next: boolean) => {
      onOpenChange?.(next);
      if (listenGlobal && shellChrome) {
        shellChrome.setNotificationPanelOpen(next);
      } else {
        setLocalOpen(next);
      }
    },
    [listenGlobal, onOpenChange, shellChrome],
  );
  const [osNotificationPermission, setOsNotificationPermission] =
    useState<OsNotificationPermission>(() => {
      // ブラウザ API を初期化子で同期的に読み取る（SSR/未対応環境では unsupported）。
      // 権限依存 UI は open=false の初期描画に現れないため hydration 差異は生じない。
      if (typeof window === "undefined" || !("Notification" in window)) {
        return "unsupported";
      }
      return window.Notification.permission;
    });
  // syncOsNotifications は本フック定義より後方で宣言されるため、ref 経由で
  // SWR の onSuccess から最新実装を呼ぶ（宣言順の循環を避ける）。
  const syncOsNotificationsRef = useRef<(next: InAppNotification[]) => void>(
    () => {},
  );

  // 通知一覧の取得・ポーリングを SWR に委譲。低帯域配慮でポーリングは 60 秒間隔とし、
  // タブ非表示中・オフライン中はポーリングを止めて通信量を抑える。
  // 取得成功時のみ OS 通知同期を行う。
  const { data, mutate, isLoading } = useSWR<InAppNotification[]>(
    NOTIFICATIONS_SWR_KEY,
    async () => {
      const res = await fetch("/api/notifications", {
        credentials: "include",
        signal: AbortSignal.timeout(5000),
      });
      // 非 OK 時は throw して直前データを保持（従来の「何もしない」と同義）。
      if (!res.ok) throw new Error("通知の取得に失敗しました");
      const payload = await res.json();
      return Array.isArray(payload)
        ? payload
        : (payload.notifications ?? []);
    },
    {
      refreshInterval: 60000,
      refreshWhenHidden: false,
      refreshWhenOffline: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      keepPreviousData: true,
      dedupingInterval: 0,
      onSuccess: (nextNotifications) => {
        publishUnreadCount(nextNotifications.filter((notification) => !notification.is_read).length);
        syncOsNotificationsRef.current(nextNotifications);
      },
    },
  );
  const notifications = data ?? EMPTY_NOTIFICATIONS;
  const loading = isLoading;

  const notificationTypeLabel = useCallback((type: string) => {
    switch (type) {
      case "reminder":
        return "リマインダー";
      case "due_soon":
        return "期限間近";
      case "overdue":
        return "期限超過";
      case "assigned":
        return "アサイン";
      case "comment":
        return "コメント";
      default:
        return type;
    }
  }, []);

  const markAsRead = useCallback(
    async (id: string) => {
      try {
        await fetch(`/api/notifications/${id}/read`, {
          method: "POST",
          credentials: "include",
        });
        void mutate(
          (prev) =>
            (prev ?? EMPTY_NOTIFICATIONS).map((n) =>
              n.id === id ? { ...n, is_read: true } : n,
            ),
          { revalidate: false },
        );
        publishUnreadCount(
          notifications.filter((notification) => notification.id !== id && !notification.is_read).length,
        );
      } catch {
        // エラー時は何もしない
      }
    },
    [mutate, notifications, publishUnreadCount],
  );

  const markAllAsRead = useCallback(async () => {
    const unreadIds = notifications.filter((n) => !n.is_read).map((n) => n.id);
    if (unreadIds.length === 0) return;

    const rollback = () => {
      publishUnreadCount(unreadIds.length);
      return mutate(
        (prev) =>
          (prev ?? EMPTY_NOTIFICATIONS).map((n) =>
            unreadIds.includes(n.id) ? { ...n, is_read: false } : n,
          ),
        { revalidate: false },
      );
    };

    // 楽観的に全件既読化。失敗時は対象のみ未読へ戻す。
    void mutate(
      (prev) =>
        (prev ?? EMPTY_NOTIFICATIONS).map((n) => ({ ...n, is_read: true })),
      { revalidate: false },
    );
    publishUnreadCount(0);
    try {
      const res = await fetch("/api/notifications/read-all", {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) void rollback();
    } catch {
      void rollback();
    }
  }, [mutate, notifications, publishUnreadCount]);

  const handleNotificationClick = useCallback(
    async (notification: InAppNotification) => {
      if (!notification.is_read) {
        await markAsRead(notification.id);
      }
      if (notification.task_id) {
        setPanelOpen(false);
        router.push(`/tasks/${notification.task_id}`);
      }
    },
    [markAsRead, router, setPanelOpen],
  );

  const showOsNotification = useCallback(
    async (notification: InAppNotification) => {
      if (
        typeof window === "undefined" ||
        !("Notification" in window) ||
        window.Notification.permission !== "granted"
      ) {
        return;
      }

      const url = notification.task_id
        ? `/tasks/${notification.task_id}`
        : "/";
      const options: NotificationOptions = {
        body: notification.message || notificationTypeLabel(notification.type),
        tag: `aoitalk-${notification.id}`,
        data: {
          url,
          notificationId: notification.id,
        },
        icon: "/favicon.ico",
        requireInteraction: true,
      };

      const registration = await getNotificationServiceWorkerRegistration();
      if (registration) {
        try {
          await registration.showNotification(notification.title, options);
          return;
        } catch {
          // Fall back to the page-level Notification API below.
        }
      }

      const osNotification = new window.Notification(
        notification.title,
        options,
      );
      osNotification.onclick = () => {
        window.focus();
        void markAsRead(notification.id);
        if (notification.task_id) {
          setPanelOpen(false);
          router.push(`/tasks/${notification.task_id}`);
        }
      };
    },
    [markAsRead, notificationTypeLabel, router, setPanelOpen],
  );

  const syncOsNotifications = useCallback(
    async (nextNotifications: InAppNotification[]) => {
      if (typeof window === "undefined") return;

      if (
        !("Notification" in window) ||
        window.Notification.permission !== "granted"
      ) {
        return;
      }

      const registration = await getNotificationServiceWorkerRegistration();
      if (registration) {
        try {
          const now = Date.now();
          const activeTags = new Set(
            nextNotifications
              .filter((notification) =>
                isFreshUnreadOsNotification(
                  notification,
                  now,
                  OS_NOTIFICATION_STALE_MS,
                ),
              )
              .map((notification) => `aoitalk-${notification.id}`),
          );
          const displayed = await registration.getNotifications();
          displayed.forEach((notification) => {
            if (
              notification.tag.startsWith("aoitalk-") &&
              !activeTags.has(notification.tag)
            ) {
              notification.close();
            }
          });
        } catch {
          // Cleanup failure must not block claiming new notifications.
        }
      }

      const claim = () => {
        let storedIds: string[] = [];
        try {
          const stored = window.localStorage.getItem(OS_NOTIFICATION_SEEN_KEY);
          const parsed = stored ? JSON.parse(stored) : [];
          if (Array.isArray(parsed)) {
            storedIds = parsed.filter(
              (id): id is string => typeof id === "string",
            );
          }
        } catch {
          storedIds = [];
        }

        const result = claimOsNotificationCandidates(
          nextNotifications,
          storedIds,
          {
            now: Date.now(),
            staleMs: OS_NOTIFICATION_STALE_MS,
            seenLimit: OS_NOTIFICATION_SEEN_LIMIT,
            displayLimit: 3,
          },
        );
        try {
          window.localStorage.setItem(
            OS_NOTIFICATION_SEEN_KEY,
            JSON.stringify(result.seenIds),
          );
        } catch {
          // Without a durable claim, displaying would repeat on every poll.
          return [];
        }
        return result.claimed;
      };

      if (!("locks" in navigator)) {
        // Without an atomic claim, multiple tabs can show the same OS toast.
        return;
      }

      let unreadNewNotifications: InAppNotification[];
      try {
        unreadNewNotifications = await navigator.locks.request(
          OS_NOTIFICATION_LOCK_NAME,
          claim,
        );
      } catch {
        return;
      }

      await Promise.allSettled(
        unreadNewNotifications.map((notification) =>
          showOsNotification(notification),
        ),
      );
    },
    [showOsNotification],
  );

  // SWR の onSuccess から最新の syncOsNotifications を呼べるよう ref を更新。
  useEffect(() => {
    syncOsNotificationsRef.current = syncOsNotifications;
  }, [syncOsNotifications]);

  useEffect(() => {
    if (!listenGlobal) return;
    const handleGlobalToggle = (event: Event) => {
      const target = (event as CustomEvent<{ target?: string; open?: boolean }>).detail
        ?.target;
      if (target && target !== "notification") return;
      const next = (event as CustomEvent<{ open?: boolean }>).detail?.open;
      if (typeof next === "boolean") {
        setPanelOpen(next);
      } else {
        setPanelOpen(!open);
      }
    };
    window.addEventListener("global-toggle-notifications", handleGlobalToggle);
    return () =>
      window.removeEventListener("global-toggle-notifications", handleGlobalToggle);
  }, [listenGlobal, open, setPanelOpen]);

  // The rail variant is portalled outside the shell DOM. Keep a local
  // capture-phase guard as a fallback for browser/portal combinations where
  // the Popover primitive cannot observe the document event directly.
  useEffect(() => {
    if (presentation !== "popover" || !open) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (
        target?.closest("[data-testid='notification-popover']") ||
        target?.closest("[data-testid='notification-trigger']")
      ) {
        return;
      }
      setPanelOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setPanelOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown, true);
    // Listen at the window as well as the portal's document so keyboard
    // events dispatched from a focused trigger (and test harnesses that
    // dispatch directly on window) close the card consistently.
    window.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      window.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [open, presentation, setPanelOpen]);

  const requestOsNotificationPermission = useCallback(async () => {
    if (typeof window === "undefined" || !("Notification" in window)) {
      setOsNotificationPermission("unsupported");
      return;
    }
    const permission = await window.Notification.requestPermission();
    setOsNotificationPermission(permission);
    if (permission === "granted") {
      const registration = await getNotificationServiceWorkerRegistration();
      if (registration) await ensureWebPushSubscription(registration);
      await syncOsNotifications(notifications);
    }
  }, [notifications, syncOsNotifications]);

  const sendTestOsNotification = useCallback(async () => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (window.Notification.permission !== "granted") {
      await requestOsNotificationPermission();
      return;
    }
    await showOsNotification({
      id: `test-${Date.now()}`,
      type: "reminder",
      title: "AoiTalk notification test",
      message: "ブラウザのOS通知は有効です。",
      task_id: null,
      is_read: false,
      created_at: new Date().toISOString(),
    });
  }, [requestOsNotificationPermission, showOsNotification]);

  useEffect(() => {
    if (typeof window === "undefined" || !("Notification" in window)) {
      return;
    }

    void (async () => {
      const registration = await getNotificationServiceWorkerRegistration();
      if (registration && window.Notification.permission === "granted") {
        await ensureWebPushSubscription(registration);
      }
    })();
  }, []);

  useEffect(() => {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
      return;
    }
    const handleServiceWorkerMessage = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; notificationId?: unknown };
      if (
        data?.type !== "aoitalk-push-delivered" ||
        typeof data.notificationId !== "string"
      ) {
        return;
      }
      // A push already displayed by the SW must not be replayed as a page
      // toast when SWR refreshes the durable inbox row.
      rememberPushedNotification(data.notificationId);
      void mutate();
    };
    navigator.serviceWorker.addEventListener(
      "message",
      handleServiceWorkerMessage,
    );
    return () =>
      navigator.serviceWorker.removeEventListener(
        "message",
        handleServiceWorkerMessage,
      );
  }, [mutate]);

  const unreadCount = notifications.filter((n) => !n.is_read).length;

  const notificationBody = (
    <>
      {osNotificationPermission === "denied" && (
        <div className="px-3 py-2 text-xs text-muted-foreground">
          ブラウザでOS通知がブロックされています
        </div>
      )}
      {loading && notifications.length === 0 && (
        <div className="px-4 py-3 text-center text-xs text-muted-foreground">
          読み込み中...
        </div>
      )}
      {!loading && notifications.length === 0 && (
        <div className="px-4 py-3 text-center text-xs text-muted-foreground">
          通知はありません
        </div>
      )}
      <div className="max-h-[min(24rem,60vh)] overflow-y-auto">
        {notifications.map((n) => (
          <button
            key={n.id}
            type="button"
            onClick={() => void handleNotificationClick(n)}
            className={`w-full text-left px-3 py-2 border-b border-border/50 hover:bg-accent/50 transition-colors ${
              n.is_read ? "opacity-60" : ""
            }`}
          >
            <div className="flex items-start gap-2">
              {!n.is_read && (
                <span className="mt-1.5 inline-block size-2 shrink-0 rounded-full bg-blue-500" />
              )}
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] font-medium text-muted-foreground uppercase">
                    {notificationTypeLabel(n.type)}
                  </span>
                </div>
                <p className="truncate text-sm font-medium">{n.title}</p>
                {n.message && (
                  <p className="truncate text-xs text-muted-foreground">
                    {n.message}
                  </p>
                )}
                <span className="text-[10px] text-muted-foreground">
                  {formatRelativeTime(n.created_at)}
                </span>
              </div>
            </div>
          </button>
        ))}
      </div>
    </>
  );

  const notificationActions = (
    <div className="flex items-center gap-0.5">
      <button
        type="button"
        onClick={() => void markAllAsRead()}
        disabled={unreadCount === 0}
        className="p-1 rounded hover:bg-accent disabled:cursor-not-allowed disabled:opacity-40"
        title="すべて確認済みにする"
        aria-label="すべて確認済みにする"
      >
        <CheckCheck className="size-3.5" />
      </button>
      {osNotificationPermission !== "unsupported" &&
        osNotificationPermission !== "denied" && (
          <button
            type="button"
            onClick={() =>
              void (osNotificationPermission === "granted"
                ? sendTestOsNotification()
                : requestOsNotificationPermission())
            }
            className="p-1 rounded hover:bg-accent"
            title={
              osNotificationPermission === "granted"
                ? "OS通知をテスト"
                : "OS通知を許可"
            }
            aria-label={
              osNotificationPermission === "granted"
                ? "OS通知をテスト"
                : "OS通知を許可"
            }
          >
            <Bell className="size-3.5" />
          </button>
        )}
      <button
        type="button"
        onClick={() => setPanelOpen(false)}
        className="p-1 rounded hover:bg-accent"
        title="閉じる"
        aria-label="通知を閉じる"
      >
        <X className="size-3.5" />
      </button>
    </div>
  );

  if (presentation === "popover") {
    return (
      <Popover open={open} onOpenChange={setPanelOpen}>
        <PopoverTrigger
          render={
            <button
              type="button"
              className={triggerClassName ?? "ao-global-rail-button"}
              aria-label={`${open ? "通知を閉じる" : "通知を開く"}${unreadCount > 0 ? `（${unreadCount}件）` : ""}`}
              aria-pressed={open}
              title={`${open ? "通知を閉じる" : "通知を開く"}${unreadCount > 0 ? `（${unreadCount}件）` : ""}`}
              data-testid="notification-trigger"
            />
          }
        >
          <span className="relative">
            <Bell className="size-[17px]" />
            {unreadCount > 0 && (
              <span
                className="absolute -right-2 -top-2 flex min-w-3.5 items-center justify-center rounded-full bg-red-500 px-0.5 text-[9px] font-bold leading-3 text-white"
                aria-hidden="true"
              >
                {unreadCount > 99 ? "99+" : unreadCount}
              </span>
            )}
          </span>
        </PopoverTrigger>
        <PopoverContent
          side="right"
          align="end"
          sideOffset={8}
          className="w-[min(24rem,calc(100vw-1rem))] overflow-hidden p-0"
          positionerClassName="z-[80]"
          data-testid="notification-popover"
        >
          <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
            <PopoverTitle className="text-sm font-semibold">通知</PopoverTitle>
            {notificationActions}
          </div>
          <div className="text-popover-foreground">{notificationBody}</div>
        </PopoverContent>
      </Popover>
    );
  }

  return (
    <SidebarGroup>
      <div className="flex items-center justify-between px-2">
        <button
          onClick={() => setPanelOpen(!open)}
          className="flex items-center gap-1.5 px-1 py-1 rounded hover:bg-accent transition-colors"
          title="通知"
        >
          <div className="relative">
            <Bell className="size-4" />
            {unreadCount > 0 && (
              <span className="absolute -top-1.5 -right-1.5 flex size-4 items-center justify-center rounded-full bg-red-500 text-[10px] font-bold text-white">
                {unreadCount > 9 ? "9+" : unreadCount}
              </span>
            )}
          </div>
          <SidebarGroupLabel className="p-0">通知</SidebarGroupLabel>
        </button>
        {open && notificationActions}
      </div>
      {open && (
        <SidebarGroupContent>{notificationBody}</SidebarGroupContent>
      )}
    </SidebarGroup>
  );
}

export function NotificationBellPopover({
  mobileOnly = false,
  triggerClassName,
  onOpenChange,
}: NotificationBellPopoverProps) {
  const isMobile = useResolvedMobileSurface();
  if (isMobile === null || isMobile !== mobileOnly) return null;
  return (
    <NotificationPanel
      listenGlobal
      presentation="popover"
      triggerClassName={triggerClassName}
      onOpenChange={onOpenChange}
    />
  );
}

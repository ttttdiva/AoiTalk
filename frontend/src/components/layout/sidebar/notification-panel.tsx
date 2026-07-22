"use client";

import { useRouter } from "next/navigation";
import { useState, useEffect, useCallback, useRef } from "react";
import useSWR from "swr";
import { CheckCheck, Bell, X } from "lucide-react";
import { formatRelativeTime } from "@/lib/utils";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
} from "@/components/ui/sidebar";

// ─── 通知 ───
const OS_NOTIFICATION_SEEN_KEY = "aoitalk-os-notification-seen";
const OS_NOTIFICATION_SEEN_LIMIT = 200;
const OS_NOTIFICATION_STALE_MS = 24 * 60 * 60 * 1000;
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

function notificationTimestamp(notification: InAppNotification): number {
  const raw = notification.delivered_at || notification.created_at;
  const time = new Date(raw).getTime();
  return Number.isFinite(time) ? time : Date.now();
}

async function getNotificationServiceWorkerRegistration() {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
    return null;
  }
  try {
    return await navigator.serviceWorker.register(
      NOTIFICATION_SERVICE_WORKER_URL,
      { scope: "/" },
    );
  } catch {
    return null;
  }
}

export function NotificationPanel() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [osNotificationPermission, setOsNotificationPermission] =
    useState<OsNotificationPermission>(() => {
      // ブラウザ API を初期化子で同期的に読み取る（SSR/未対応環境では unsupported）。
      // 権限依存 UI は open=false の初期描画に現れないため hydration 差異は生じない。
      if (typeof window === "undefined" || !("Notification" in window)) {
        return "unsupported";
      }
      return window.Notification.permission;
    });
  const osNotificationSeenIdsRef = useRef<Set<string>>(new Set());
  const osNotificationInitializedRef = useRef(false);
  // syncOsNotifications は本フック定義より後方で宣言されるため、ref 経由で
  // SWR の onSuccess から最新実装を呼ぶ（宣言順の循環を避ける）。
  const syncOsNotificationsRef = useRef<(next: InAppNotification[]) => void>(
    () => {},
  );

  // 通知一覧の取得・30秒ポーリングを SWR に委譲。従来の
  // 「初回取得 + setInterval(30s)」と等価にするため refreshInterval=30000 とし、
  // タブ非表示・オフラインでも従来同様に動かす。取得成功時のみ OS 通知同期を行う。
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
      refreshInterval: 30000,
      refreshWhenHidden: true,
      refreshWhenOffline: true,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      keepPreviousData: true,
      dedupingInterval: 0,
      onSuccess: (nextNotifications) =>
        syncOsNotificationsRef.current(nextNotifications),
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

  const persistOsNotificationSeenIds = useCallback(() => {
    if (typeof window === "undefined") return;
    const ids = Array.from(osNotificationSeenIdsRef.current).slice(
      -OS_NOTIFICATION_SEEN_LIMIT,
    );
    osNotificationSeenIdsRef.current = new Set(ids);
    window.localStorage.setItem(OS_NOTIFICATION_SEEN_KEY, JSON.stringify(ids));
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
      } catch {
        // エラー時は何もしない
      }
    },
    [mutate],
  );

  const markAllAsRead = useCallback(async () => {
    const unreadIds = notifications.filter((n) => !n.is_read).map((n) => n.id);
    if (unreadIds.length === 0) return;

    const rollback = () =>
      mutate(
        (prev) =>
          (prev ?? EMPTY_NOTIFICATIONS).map((n) =>
            unreadIds.includes(n.id) ? { ...n, is_read: false } : n,
          ),
        { revalidate: false },
      );

    // 楽観的に全件既読化。失敗時は対象のみ未読へ戻す。
    void mutate(
      (prev) =>
        (prev ?? EMPTY_NOTIFICATIONS).map((n) => ({ ...n, is_read: true })),
      { revalidate: false },
    );
    try {
      const res = await fetch("/api/notifications/read-all", {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) void rollback();
    } catch {
      void rollback();
    }
  }, [mutate, notifications]);

  const handleNotificationClick = useCallback(
    async (notification: InAppNotification) => {
      if (!notification.is_read) {
        await markAsRead(notification.id);
      }
      if (notification.task_id) {
        setOpen(false);
        router.push(`/tasks/${notification.task_id}`);
      }
    },
    [markAsRead, router],
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
          setOpen(false);
          router.push(`/tasks/${notification.task_id}`);
        }
      };
    },
    [markAsRead, notificationTypeLabel, router],
  );

  const syncOsNotifications = useCallback(
    (nextNotifications: InAppNotification[]) => {
      if (typeof window === "undefined") return;

      if (!osNotificationInitializedRef.current) {
        const staleBefore = Date.now() - OS_NOTIFICATION_STALE_MS;
        nextNotifications.forEach((notification) => {
          if (notificationTimestamp(notification) < staleBefore) {
            osNotificationSeenIdsRef.current.add(notification.id);
          }
        });
        osNotificationInitializedRef.current = true;
        persistOsNotificationSeenIds();
      }

      if (
        !("Notification" in window) ||
        window.Notification.permission !== "granted"
      ) {
        return;
      }

      const unreadNewNotifications = nextNotifications
        .filter(
          (notification) =>
            !notification.is_read &&
            !osNotificationSeenIdsRef.current.has(notification.id),
        )
        .sort(
          (a, b) =>
            new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
        );

      unreadNewNotifications.forEach((notification) => {
        osNotificationSeenIdsRef.current.add(notification.id);
      });
      if (unreadNewNotifications.length === 0) return;
      persistOsNotificationSeenIds();

      unreadNewNotifications.slice(-3).forEach((notification) => {
        void showOsNotification(notification);
      });
    },
    [persistOsNotificationSeenIds, showOsNotification],
  );

  // SWR の onSuccess から最新の syncOsNotifications を呼べるよう ref を更新。
  useEffect(() => {
    syncOsNotificationsRef.current = syncOsNotifications;
  }, [syncOsNotifications]);

  const requestOsNotificationPermission = useCallback(async () => {
    if (typeof window === "undefined" || !("Notification" in window)) {
      setOsNotificationPermission("unsupported");
      return;
    }
    const permission = await window.Notification.requestPermission();
    setOsNotificationPermission(permission);
    if (permission === "granted") {
      await getNotificationServiceWorkerRegistration();
      syncOsNotifications(notifications);
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

    void getNotificationServiceWorkerRegistration();
    try {
      const stored = window.localStorage.getItem(OS_NOTIFICATION_SEEN_KEY);
      const parsed = stored ? JSON.parse(stored) : [];
      if (Array.isArray(parsed)) {
        osNotificationSeenIdsRef.current = new Set(
          parsed.filter((id): id is string => typeof id === "string"),
        );
      }
    } catch {
      osNotificationSeenIdsRef.current = new Set();
    }
  }, []);

  const unreadCount = notifications.filter((n) => !n.is_read).length;

  return (
    <SidebarGroup>
      <div className="flex items-center justify-between px-2">
        <button
          onClick={() => setOpen(!open)}
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
        {open && (
          <div className="flex items-center gap-0.5">
            <button
              type="button"
              onClick={() => void markAllAsRead()}
              disabled={unreadCount === 0}
              className="p-1 rounded hover:bg-accent disabled:cursor-not-allowed disabled:opacity-40"
              title="すべて確認済みにする"
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
                >
                  <Bell className="size-3.5" />
                </button>
              )}
            <button
              onClick={() => setOpen(false)}
              className="p-1 rounded hover:bg-accent"
              title="閉じる"
            >
              <X className="size-3.5" />
            </button>
          </div>
        )}
      </div>
      {open && (
        <SidebarGroupContent>
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
          <div className="max-h-[300px] overflow-y-auto">
            {notifications.map((n) => (
              <button
                key={n.id}
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
        </SidebarGroupContent>
      )}
    </SidebarGroup>
  );
}
